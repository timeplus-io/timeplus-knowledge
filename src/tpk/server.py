"""FastAPI server: SSE /chat over the knowledge agent + static web UI."""

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


class ChatTurn(BaseModel):
    role: str  # "user" | "assistant"
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
    from tpk import db
    from tpk.agent import build_agent
    from tpk.config import AgentConfig, Settings, load_repos
    from tpk.tools import KnowledgeGraph

    repos = load_repos(REPOS_TOML)
    kg = KnowledgeGraph(
        db.get_client(Settings.from_env()),
        repo_paths={n: c.path for n, c in repos.items()},
    )
    return build_agent(kg, AgentConfig.from_env(), repos)


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
                    elif kind == "on_tool_start":
                        yield _sse(
                            {
                                "type": "tool",
                                "name": event.get("name", ""),
                                "input": event.get("data", {}).get("input", {}),
                            }
                        )
                yield _sse({"type": "done", "text": "".join(full)})
            except Exception as exc:  # stream errors must reach the client
                yield _sse({"type": "error", "message": str(exc)})

        return StreamingResponse(stream(), media_type="text/event-stream")

    if WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="ui")

    return app
