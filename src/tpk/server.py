"""FastAPI server: SSE /chat over the knowledge agent + static web UI."""

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tpk.config import as_bool, setting

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


def _chunk_thinking(chunk) -> str:
    """Extract reasoning/thinking deltas from a model stream chunk.

    Two shapes:
    - OpenAI-compatible reasoning models (gpt-oss, DeepSeek, Qwen, ...) stream
      reasoning as a `reasoning` / `reasoning_content` delta field, preserved
      onto `additional_kwargs` by agent._ReasoningChatOpenAI.
    - Anthropic emits `thinking` content blocks when extended thinking is
      enabled and the endpoint exposes readable thinking text.

    Models that expose neither (or that redact thinking) yield nothing, so the
    caller emits no thinking events and the UI is unchanged."""
    ak = getattr(chunk, "additional_kwargs", None)
    if isinstance(ak, dict):
        reasoning = ak.get("reasoning") or ak.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            return reasoning
    content = getattr(chunk, "content", "")
    if isinstance(content, list):
        return "".join(
            b.get("thinking", "") for b in content
            if isinstance(b, dict) and b.get("type") == "thinking"
        )
    return ""


def _build_kg_and_repos():
    """Production path: one shared `KnowledgeGraph` (+ the parsed repo
    config it needs) built up front in `create_app`, reused by both the
    lazily-built chat agent and the graph API router -- instead of each
    separately opening its own client and repeating schema/corpus setup."""
    from tpk import corpus, db
    from tpk.config import Settings, load_repos
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
    return kg, repos


def _build_production_agent(kg, repos):
    from tpk import corpus, db
    from tpk.agent import build_agent
    from tpk.config import AgentConfig, Settings

    # Separate client for the live corpus provider: it is called from the
    # async worker thread pool on every chat turn, and sharing the
    # KnowledgeGraph's client across threads causes concurrent-session
    # errors against Timeplus. But this second client is itself shared
    # across concurrent /chat requests (one agent, cached for the app's
    # lifetime) and timeplus_connect forbids concurrent queries within one
    # session -- so its own access must be serialized too, the same way
    # KnowledgeGraph._query_rows serializes access to `kg.client` (see
    # tools.py:57-61).
    provider_client = db.get_client(Settings.from_env())
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


def create_app(
    agent=None, stream_prefix: str = "", auth=None, kg=None, audit_sink=None
) -> FastAPI:
    import tpk.auth as auth_mod
    from tpk.agent import RECURSION_LIMIT
    from tpk.api import create_api_router
    from tpk.auth import AuthLayer, User, create_auth_router
    from tpk.tools import ROLE_SCOPE

    auth = auth or AuthLayer(stream_prefix)
    app = FastAPI(title="timeplus-knowledge")
    app.include_router(create_auth_router(auth))
    app.include_router(create_api_router(prefix=stream_prefix, auth=auth))

    # Support-history audit sink: one row per chat turn. Tests inject their
    # own `audit_sink`; production builds a best-effort Timeplus writer unless
    # chat auditing is disabled (TPK_CHAT_AUDIT=0 or [server].chat_audit=false).
    # A None sink means "don't audit".
    if audit_sink is None and as_bool(
        setting("TPK_CHAT_AUDIT", "server", "chat_audit", True)
    ):
        from tpk import audit, db
        from tpk.config import Settings

        audit_sink = audit.make_db_sink(
            stream_prefix, lambda: db.get_client(Settings.from_env())
        )

    repos_for_agent = None
    if agent is None and kg is None:
        # Production path only: the test path always injects `agent`
        # (fake/no-op) and, when it wants the graph router mounted, its own
        # `kg` built over the test stream prefix -- so this branch never
        # runs there and never touches the network in unit tests.
        kg, repos_for_agent = _build_kg_and_repos()

    state = {"agent": agent}

    def _agent():
        if state["agent"] is None:
            state["agent"] = _build_production_agent(kg, repos_for_agent)
        return state["agent"]

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/chat/model")
    def chat_model(user: User = Depends(auth.require_cap(auth_mod.CAP_CHAT))):
        """The provider/model the chat agent runs on, for display in the UI.
        Not sensitive; returns nulls if the agent LLM isn't configured."""
        from tpk.config import AgentConfig

        try:
            cfg = AgentConfig.from_env()
            return {"provider": cfg.provider, "model": cfg.model}
        except Exception:
            return {"provider": None, "model": None}

    @app.post("/chat")
    async def chat(req: ChatRequest, user: User = Depends(auth.require_cap(auth_mod.CAP_CHAT))):
        messages = [(t.role, t.content) for t in req.history] + [("user", req.message)]

        # Provider/model stamped on the audit row. Best-effort: an
        # unconfigured LLM must not stop the chat request.
        try:
            from tpk.config import AgentConfig

            _cfg = AgentConfig.from_env()
            audit_provider, audit_model = _cfg.provider, _cfg.model
        except Exception:
            audit_provider, audit_model = "", ""

        scope = None
        if user.role != auth_mod.ROLE_ADMIN:
            try:
                # Off the event loop: against an unreachable-but-not-refusing
                # store this is a blocking TCP connect timeout, which would
                # otherwise stall every other request on the server.
                role = await run_in_threadpool(
                    lambda: auth_mod.get_role(auth._client(), user.role, prefix=stream_prefix)
                )
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
            # Audit accumulators (read in the finally block below).
            started = time.monotonic()
            tool_calls: list[dict] = []
            sources: list[dict] = []
            answer_text = ""
            audit_status = "ok"
            audit_error = ""
            try:
                full: list[str] = []
                final_text = ""
                source_index: dict[tuple, int] = {}
                try:
                    async for event in _agent().astream_events(
                        {"messages": messages},
                        version="v2",
                        config={"recursion_limit": RECURSION_LIMIT},
                    ):
                        kind = event.get("event")
                        if kind == "on_chat_model_stream":
                            chunk = event["data"]["chunk"]
                            # Reasoning deltas (when the model exposes them)
                            # stream ahead of the answer text and interleave
                            # with tool calls in event order — the UI groups
                            # consecutive deltas into a thinking step.
                            thinking = _chunk_thinking(chunk)
                            if thinking:
                                yield _sse({"type": "thinking", "text": thinking})
                            text = _chunk_text(chunk)
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
                        elif kind == "on_tool_end":
                            name = event.get("name", "")
                            args = event.get("data", {}).get("input") or {}
                            # One audit entry per tool call; count/unit filled in
                            # below when the tool exposes a result count.
                            audit_entry = {"name": name, "input": args}
                            tool_calls.append(audit_entry)
                            # Result count for the tool row (mockup t7):
                            # search_entities -> number of matches. read_source's
                            # "N lines" is derived on the client from the source
                            # event's line range below.
                            if name == "search_entities":
                                try:
                                    # LangGraph's ToolNode wraps the tool's list
                                    # return in a ToolMessage whose .content is a
                                    # JSON string (json.dumps), so decode that —
                                    # not a raw list — to count matches.
                                    out = event.get("data", {}).get("output")
                                    items = out if isinstance(out, list) else getattr(out, "content", out)
                                    if isinstance(items, str):
                                        try:
                                            items = json.loads(items)
                                        except ValueError:
                                            items = None
                                    if isinstance(items, list):
                                        audit_entry["count"] = len(items)
                                        audit_entry["unit"] = "matches"
                                        yield _sse({"type": "tool_result", "name": name,
                                                    "input": args, "count": len(items),
                                                    "unit": "matches"})
                                except Exception:
                                    logger.exception("failed to emit search tool_result; skipping")
                            # Surface cited sources for the read_source rows.
                            # Guarded end-to-end: a malformed event (missing args,
                            # unexpected shape) must never break the token stream,
                            # so any failure here is logged and skipped.
                            try:
                                if name == "read_source":
                                    repo = args["repo"]
                                    file_path = args["file_path"]
                                    line_start = args["line_start"]
                                    line_end = args["line_end"]
                                    key = (repo, file_path, line_start)
                                    if key not in source_index:
                                        source_index[key] = len(sources) + 1
                                        source = {
                                            "type": "source",
                                            "n": source_index[key],
                                            "repo": repo,
                                            "file_path": file_path,
                                            "line_start": line_start,
                                            "line_end": line_end,
                                            "kind": "file",
                                        }
                                        sources.append(source)
                                        yield _sse(source)
                            except Exception:
                                logger.exception(
                                    "failed to process on_tool_end source event; skipping"
                                )
                    done_text = final_text or "".join(full) or (
                        "The model returned no answer text for this question "
                        "(it may have spent its turns on tool calls). Please retry "
                        "or rephrase."
                    )
                    answer_text = done_text
                    yield _sse({"type": "done", "text": done_text, "sources": sources})
                except Exception as exc:  # stream errors must reach the client
                    # Log the full exception server-side; the client only gets
                    # the exception's class name, never the raw message, which
                    # can leak internal details (stack context, credentials in
                    # a driver error, etc.) into the browser.
                    audit_status = "error"
                    audit_error = type(exc).__name__
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
                # Support-history audit: one row per turn, written after the
                # answer has streamed so it adds no user-visible latency, and
                # best-effort so a sink failure never surfaces to the client.
                if audit_sink is not None:
                    try:
                        from tpk.audit import AuditRecord

                        record = AuditRecord(
                            ts=datetime.now(timezone.utc),
                            turn_id=uuid.uuid4().hex,
                            username=user.username,
                            role=user.role,
                            provider=audit_provider,
                            model=audit_model,
                            question=req.message,
                            answer=answer_text,
                            history_len=len(req.history),
                            latency_ms=int((time.monotonic() - started) * 1000),
                            status=audit_status,
                            error=audit_error,
                            tool_calls=tool_calls,
                            sources=sources,
                        )
                        await run_in_threadpool(audit_sink, record)
                    except Exception:
                        logger.exception("chat audit failed; skipping")

        return StreamingResponse(stream(), media_type="text/event-stream")

    if kg is not None:
        from tpk.graph_api import create_graph_router

        app.include_router(create_graph_router(kg, auth, prefix=stream_prefix))

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="ui")

    return app
