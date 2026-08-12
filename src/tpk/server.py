"""FastAPI server: SSE /chat over the knowledge agent + static web UI."""

import json
import logging
import threading
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

logger = logging.getLogger(__name__)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[ChatTurn] = []


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _chunk_text(chunk) -> str:
    """Normalize model stream chunks: OpenAI yields str content, Anthropic
    may yield a list of content blocks."""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _build_production_agent():
    from tpk import corpus, db
    from tpk.agent import build_agent
    from tpk.config import AgentConfig, Settings, load_repos
    from tpk.config import repo_paths as resolved_repo_paths
    from tpk.tools import KnowledgeGraph

    repos = load_repos(REPOS_TOML)
    settings = Settings.from_env()
    client = db.get_client(settings)
    db.ensure_schema(client)
    corpus.seed_from_toml(client, REPOS_TOML)
    kg = KnowledgeGraph(
        client,
        repo_paths=resolved_repo_paths(repos),
    )
    # Separate client for the live corpus provider: it is called from the
    # async worker thread pool on every chat turn, and sharing the
    # KnowledgeGraph's client across threads causes concurrent-session
    # errors against Timeplus. But this second client is itself shared
    # across concurrent /chat requests (one agent, cached for the app's
    # lifetime) and timeplus_connect forbids concurrent queries within one
    # session -- so its own access must be serialized too, the same way
    # KnowledgeGraph._query_rows serializes access to `kg.client` (see
    # tools.py:57-61).
    provider_client = db.get_client(settings)
    provider_lock = threading.Lock()

    def _live_corpus():
        with provider_lock:
            return [e for e in corpus.list_entries(provider_client) if e.enabled]

    return build_agent(
        kg,
        AgentConfig.from_env(),
        repos,
        corpus_provider=_live_corpus,
    )


def create_app(agent=None, stream_prefix: str = "", auth=None) -> FastAPI:
    import tpk.auth as auth_mod
    from tpk.agent import RECURSION_LIMIT
    from tpk.api import create_api_router
    from tpk.auth import AuthLayer, User, create_auth_router
    from tpk.tools import ROLE_SCOPE

    auth = auth or AuthLayer(stream_prefix)
    app = FastAPI(title="timeplus-knowledge")
    app.include_router(create_auth_router(auth))
    app.include_router(create_api_router(prefix=stream_prefix, auth=auth))
    state = {"agent": agent}

    def _agent():
        if state["agent"] is None:
            state["agent"] = _build_production_agent()
        return state["agent"]

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/chat")
    async def chat(req: ChatRequest, user: User = Depends(auth.require_user)):
        messages = [(t.role, t.content) for t in req.history] + [("user", req.message)]

        scope = None
        if user.role != auth_mod.ROLE_ADMIN:
            try:
                role = auth_mod.get_role(auth._client(), user.role, prefix=stream_prefix)
            except Exception:
                role = None
            # A missing/unreadable role fails closed: frozenset() = empty
            # scope = tools see nothing, rather than falling through to
            # unrestricted (None) access.
            scope = frozenset(role.entry_keys) if role else frozenset()

        async def stream():
            # Set inside stream(), not the handler body: the generator runs
            # after `chat` returns (StreamingResponse drives it lazily), so
            # the ContextVar must wrap the agent run here to cover every
            # tool call langchain-core dispatches during it.
            token = ROLE_SCOPE.set(scope) if scope is not None else None
            try:
                full: list[str] = []
                final_text = ""
                try:
                    async for event in _agent().astream_events(
                        {"messages": messages},
                        version="v2",
                        config={"recursion_limit": RECURSION_LIMIT},
                    ):
                        kind = event.get("event")
                        if kind == "on_chat_model_stream":
                            text = _chunk_text(event["data"]["chunk"])
                            if text:
                                full.append(text)
                                yield _sse({"type": "token", "text": text})
                        elif kind == "on_chat_model_end":
                            # The last model turn's message is the authoritative
                            # answer — token deltas can miss it entirely for
                            # models that stream on a reasoning channel (gpt-oss).
                            end_text = _chunk_text(event.get("data", {}).get("output"))
                            if end_text:
                                final_text = end_text
                        elif kind == "on_tool_start":
                            yield _sse(
                                {
                                    "type": "tool",
                                    "name": event.get("name", ""),
                                    "input": event.get("data", {}).get("input", {}),
                                }
                            )
                    done_text = final_text or "".join(full) or (
                        "The model returned no answer text for this question "
                        "(it may have spent its turns on tool calls). Please retry "
                        "or rephrase."
                    )
                    yield _sse({"type": "done", "text": done_text})
                except Exception as exc:  # stream errors must reach the client
                    # Log the full exception server-side; the client only gets
                    # the exception's class name, never the raw message, which
                    # can leak internal details (stack context, credentials in
                    # a driver error, etc.) into the browser.
                    logger.exception("chat stream failed")
                    yield _sse(
                        {
                            "type": "error",
                            "message": f"{type(exc).__name__}: request failed; see server logs",
                        }
                    )
            finally:
                if token is not None:
                    ROLE_SCOPE.reset(token)

        return StreamingResponse(stream(), media_type="text/event-stream")

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="ui")

    return app
