"""FastAPI server: SSE /chat over the knowledge agent + static web UI."""

import contextlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tpk.config import as_bool, config_path, setting

REPOS_TOML = config_path()
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


def _usage_tokens(msg) -> int:
    """Total tokens for one model call, read from an `on_chat_model_end`
    output message. Prefers LangChain's normalized `usage_metadata`, falling
    back to raw `response_metadata` token_usage (OpenAI-style) so gateways that
    only pass the raw shape still count. Returns 0 when usage is unavailable
    (some gateways omit it) — the caller treats a 0-cost turn as free."""
    if msg is None:
        return 0
    um = getattr(msg, "usage_metadata", None)
    if isinstance(um, dict):
        total = um.get("total_tokens")
        if total is None:
            total = (um.get("input_tokens") or 0) + (um.get("output_tokens") or 0)
        return max(int(total or 0), 0)
    rm = getattr(msg, "response_metadata", None) or {}
    tu = rm.get("token_usage") or rm.get("usage") or {}
    if isinstance(tu, dict):
        total = tu.get("total_tokens")
        if total is None:
            total = (tu.get("prompt_tokens") or 0) + (tu.get("completion_tokens") or 0)
        return max(int(total or 0), 0)
    return 0


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
    agent=None, stream_prefix: str = "", auth=None, kg=None, audit_sink=None,
    usage=None,
) -> FastAPI:
    import tpk.auth as auth_mod
    from tpk.agent import RECURSION_LIMIT
    from tpk.api import create_api_router
    from tpk.auth import AuthLayer, User, create_auth_router
    from tpk.config import daily_token_limit
    from tpk.tools import ROLE_SCOPE
    from tpk.usage import day_window, effective_daily_limit

    auth = auth or AuthLayer(stream_prefix)
    # The remote-MCP transport (#74) needs its session manager running for
    # the app's lifetime. The server is only known further down (it needs
    # `kg`), so the lifespan reads it from this holder.
    mcp_state = {"server": None}

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        if mcp_state["server"] is None:
            yield
            return
        # session_manager.run() is once-per-instance: one app object cannot be
        # entered in two lifespans (in tests, one `with TestClient(app)` per app).
        async with mcp_state["server"].session_manager.run():
            yield

    app = FastAPI(title="timeplus-knowledge", lifespan=_lifespan)
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
        # Per-user daily token budget (#62). Always-on in production (not gated
        # by the audit toggle); a fresh client per read/write. Tests inject
        # their own `usage` (or leave it None to disable enforcement).
        if usage is None:
            from tpk import db
            from tpk.config import Settings
            from tpk.usage import DbUsage

            usage = DbUsage(stream_prefix, lambda: db.get_client(Settings.from_env()))

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

    @app.get("/chat/usage")
    def chat_usage_status(user: User = Depends(auth.require_cap(auth_mod.CAP_CHAT))):
        """This user's daily token budget for the UI (#62): how much is used,
        how much is left, and when it resets. `limited: false` for admins, when
        the effective limit is 0 (unlimited), or when no usage store is active —
        the UI then shows no budget indicator. Sync def -> FastAPI runs the DB
        reads in a threadpool. Best-effort: a role-lookup failure falls back to
        the global default limit, and the usage read fails open (0 used)."""
        if user.role == auth_mod.ROLE_ADMIN or usage is None:
            return {"limited": False}
        try:
            role = auth_mod.get_role(auth._client(), user.role, prefix=stream_prefix)
        except Exception:
            role = None
        limit = effective_daily_limit(user.daily_token_limit, role, daily_token_limit())
        if limit <= 0:
            return {"limited": False}
        used = usage.used_today(user.username)
        _, reset = day_window()
        return {
            "limited": True,
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
            "reset": reset.isoformat(),
        }

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
        role = None
        turn_limit = 0  # effective daily token budget for this user (0 = unlimited)
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
            # Effective daily token budget by precedence: the user's own
            # override, else the role's limit, else the global fallback. admin
            # is never limited (this branch is skipped for admins).
            turn_limit = effective_daily_limit(user.daily_token_limit, role, daily_token_limit())

            # Enforce the budget BEFORE running the agent (#62). Enforcement is
            # necessarily next-turn: a turn's cost is only known once it runs, so
            # the turn that crosses the line completes and the NEXT one is
            # blocked. Reads fail open (usage.used_today swallows + returns 0).
            if usage is not None and turn_limit > 0:
                used = await run_in_threadpool(lambda: usage.used_today(user.username))
                if used >= turn_limit:
                    _, reset = day_window()
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "message": (
                                f"Daily token budget reached ({used}/{turn_limit}). "
                                f"Access resets at {reset.isoformat()}."
                            ),
                            "used": used,
                            "limit": turn_limit,
                            "reset": reset.isoformat(),
                        },
                    )

        # Whether this user may see the reasoning trace + source references.
        # Single source of truth: `effective_capabilities` already encodes
        # admin -> all, missing/unreadable role -> none (fail closed), else the
        # view-expanded role caps — the same helper the API/UI gates use.
        can_view_source = auth_mod.CAP_SOURCE_VIEW in auth_mod.effective_capabilities(user, role)

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
            turn_tokens = 0  # summed across the turn's model calls (#62)
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
                            if thinking and can_view_source:
                                yield _sse({"type": "thinking", "text": thinking})
                            text = _chunk_text(chunk)
                            if text:
                                full.append(text)
                                # Token deltas interleave the model's between-tool
                                # narration (reasoning) with answer text and can't
                                # be told apart mid-stream. For users without
                                # source:view the stream is answer-only: withhold
                                # token deltas entirely and deliver the answer in
                                # the `done` event (built from `full`/final_text),
                                # so no narration crosses the wire or lingers in
                                # the client buffer to surface on an error.
                                if can_view_source:
                                    yield _sse({"type": "token", "text": text})
                        elif kind == "on_chat_model_end":
                            # The last model turn's message is the authoritative
                            # answer — token deltas can miss it entirely for
                            # models that stream on a reasoning channel (gpt-oss).
                            output = event.get("data", {}).get("output")
                            end_text = _chunk_text(output)
                            if end_text:
                                final_text = end_text
                            # Sum token cost across every model call in the turn
                            # (the tool loop makes several) for the daily budget.
                            turn_tokens += _usage_tokens(output)
                        elif kind == "on_tool_start":
                            # Trace/source events are withheld from users without
                            # source:view — the API stream carries only the answer
                            # (token/done/error). Reference metadata (tool names,
                            # file paths, line ranges) must not cross the wire, not
                            # just be hidden client-side.
                            if can_view_source:
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
                                        # audit count is recorded above regardless;
                                        # only the client event is cap-gated.
                                        if can_view_source:
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
                                        # `sources` is accumulated for the audit
                                        # record below regardless; the client event
                                        # (the clickable citation) is cap-gated.
                                        sources.append(source)
                                        if can_view_source:
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
                    # Withhold the citation list from users without source:view —
                    # the done event carries the answer only.
                    yield _sse({"type": "done", "text": done_text,
                                "sources": sources if can_view_source else []})
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
                # Daily-budget metering (#62): record this turn's token cost,
                # off the event loop and best-effort. Written even on a partial
                # (errored) turn — tokens spent still count against the budget.
                if usage is not None and turn_tokens > 0:
                    try:
                        await run_in_threadpool(usage.record, user.username, turn_tokens)
                    except Exception:
                        logger.exception("usage record failed; skipping")

        return StreamingResponse(stream(), media_type="text/event-stream")

    if kg is not None:
        from tpk.graph_api import create_graph_router

        app.include_router(create_graph_router(kg, auth, prefix=stream_prefix))

        from tpk import mcp_http

        if mcp_http.enabled():
            mcp_route, mcp_state["server"] = mcp_http.create_mcp_route(
                kg, auth, prefix=stream_prefix)
            app.router.routes.append(mcp_route)

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="ui")

    return app
