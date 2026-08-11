"""FastAPI server: SSE /chat over the knowledge agent + static web UI."""

import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
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
    # errors against Timeplus.
    provider_client = db.get_client(settings)
    return build_agent(
        kg,
        AgentConfig.from_env(),
        repos,
        corpus_provider=lambda: [e for e in corpus.list_entries(provider_client) if e.enabled],
    )


def create_app(agent=None) -> FastAPI:
    from tpk.agent import RECURSION_LIMIT

    app = FastAPI(title="timeplus-knowledge")
    state = {"agent": agent}

    def _agent():
        if state["agent"] is None:
            state["agent"] = _build_production_agent()
        return state["agent"]

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/chat")
    async def chat(req: ChatRequest):
        messages = [(t.role, t.content) for t in req.history] + [("user", req.message)]

        async def stream():
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

        return StreamingResponse(stream(), media_type="text/event-stream")

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="ui")

    return app
