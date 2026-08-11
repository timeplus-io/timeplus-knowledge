# M4: Chat Agent + Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A citation-grounded chat agent over the Timeplus knowledge graph — LangGraph ReAct agent on the six `KnowledgeGraph` tools, streamed over a FastAPI SSE endpoint, with a React chat UI — completing milestone M4 of the spec.

**Architecture:** `tpk.agent` builds a provider-switchable chat model (Anthropic API, OpenAI API, or any OpenAI-compatible gateway such as AWS Bedrock) and a LangGraph `create_react_agent` whose tools are thin LangChain bindings of the existing `KnowledgeGraph` methods and whose system prompt embeds the corpus repo descriptions and citation rules. `tpk.server` exposes `POST /chat` as an SSE stream (token / tool / done / error events) and serves the built React UI; `tpk serve` runs it. The compose stack gains an `agent` service reusing the same image.

**Tech Stack:** Python 3.11+, uv, LangGraph + langchain-anthropic + langchain-openai, FastAPI + uvicorn, React 18 + Vite (TypeScript), pytest.

**Spec:** `docs/superpowers/specs/2026-08-10-timeplus-knowledge-agent-design.md` (M4 section). Prior milestones delivered `tpk.tools.KnowledgeGraph`, `tpk.config`, `tpk.db`, the MCP server, and the populated graph in the compose stack.

## Global Constraints

- Providers: BOTH `anthropic` and `openai` must work, plus OpenAI-compatible gateways (AWS Bedrock behind `OPENAI_BASE_URL`, model IDs like `openai.gpt-oss-120b`) and Anthropic-compatible gateways (`ANTHROPIC_BASE_URL`). Keys/URLs come only from env; a set `*_BASE_URL` without a key uses the placeholder-key pattern (same as `graphify_runner._child_env_with_placeholder_key`).
- Agent env vars: `TPK_AGENT_PROVIDER` (`anthropic` | `openai`; default inferred from which key/URL env is present, anthropic preferred when both), `TPK_AGENT_MODEL` (default `claude-sonnet-5` for anthropic, `gpt-5.2` for openai).
- Citations: the system prompt MUST require every factual claim to carry a `repo/file_path:line` citation and to say "I could not find this in the knowledge graph" rather than guess when tools return nothing relevant.
- Caps: agent invocations run with `recursion_limit=40` (constant `RECURSION_LIMIT` in `tpk/agent.py`). The existing KnowledgeGraph caps (depth ≤ 3, ≤ 200/hop, path ≤ 6, source ≤ 400 lines) remain the tool-side guardrails.
- SSE protocol (exact): each event is `data: <json>\n\n` where json is one of `{"type":"token","text":str}`, `{"type":"tool","name":str,"input":object}`, `{"type":"done","text":str}`, `{"type":"error","message":str}`. The stream always ends with a `done` or `error` event.
- No auth in v1 (spec). Server binds 127.0.0.1 by default; `--host 0.0.0.0` opt-in (compose uses it).
- Every read of Timeplus goes through `KnowledgeGraph` (already `table(...)`-wrapped). No new SQL in this plan.
- Python ≥ 3.11, uv, conventional commits. Full pytest suite green (`TIMEPLUS_HOST=localhost uv run pytest`) before each commit; UI build verified with `npm run build`.
- Web UI: minimal, clean, dark-capable chat page; CSS custom properties so Timeplus console tokens can be dropped in later. No UI framework beyond React.

## File Structure

```
src/tpk/config.py          # + AgentConfig (provider/model from env)
src/tpk/agent_tools.py     # NEW: 6 LangChain tool bindings over KnowledgeGraph
src/tpk/agent.py           # NEW: build_chat_model, system prompt, build_agent, RECURSION_LIMIT
src/tpk/server.py          # NEW: FastAPI app factory, SSE /chat, /healthz, static UI
src/tpk/cli.py             # + `tpk serve` command
web/                       # NEW: Vite + React chat UI (src/, dist/ built)
deploy/docker/Dockerfile   # + node build stage for web/dist, EXPOSE 8000
docker-compose.yml         # + agent service (same image, `tpk serve`, port 8000)
Makefile                   # + serve, web-build, web-dev targets
tests/test_agent_config.py # NEW
tests/test_agent_tools.py  # NEW
tests/test_agent.py        # NEW
tests/test_server.py       # NEW
```

---

### Task 1: AgentConfig + provider-switchable chat model

**Files:**
- Modify: `src/tpk/config.py` (append)
- Create: `src/tpk/agent.py` (model factory only in this task)
- Test: `tests/test_agent_config.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `tpk.config.AgentConfig` dataclass: `provider: str`, `model: str`; classmethod `AgentConfig.from_env() -> AgentConfig`. Provider resolution: explicit `TPK_AGENT_PROVIDER` wins (must be `anthropic`|`openai`, else `ValueError`); otherwise `anthropic` if `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` is set, else `openai` if `OPENAI_API_KEY` or `OPENAI_BASE_URL` is set, else `ValueError` with a message naming all four vars. Model: `TPK_AGENT_MODEL` if set, else `claude-sonnet-5` (anthropic) / `gpt-5.2` (openai).
  - `tpk.agent.build_chat_model(cfg: AgentConfig)` → a LangChain chat model: `ChatAnthropic(model=cfg.model, base_url=$ANTHROPIC_BASE_URL or None, api_key=$ANTHROPIC_API_KEY or "placeholder-gateway-key")` / `ChatOpenAI(model=cfg.model, base_url=$OPENAI_BASE_URL or None, api_key=$OPENAI_API_KEY or "placeholder-gateway-key")`.

- [ ] **Step 1: Add dependencies**

```bash
uv add langgraph langchain-anthropic langchain-openai fastapi "uvicorn[standard]"
```

- [ ] **Step 2: Write the failing tests**

`tests/test_agent_config.py`:

```python
import pytest

from tpk.config import AgentConfig

ENV_VARS = (
    "TPK_AGENT_PROVIDER", "TPK_AGENT_MODEL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)


def test_explicit_provider_and_model(monkeypatch):
    monkeypatch.setenv("TPK_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("TPK_AGENT_MODEL", "openai.gpt-oss-120b")
    cfg = AgentConfig.from_env()
    assert (cfg.provider, cfg.model) == ("openai", "openai.gpt-oss-120b")


def test_provider_inferred_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://gw/v1")
    assert AgentConfig.from_env().provider == "openai"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")  # anthropic preferred when both
    assert AgentConfig.from_env().provider == "anthropic"


def test_default_models(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert AgentConfig.from_env().model == "claude-sonnet-5"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert AgentConfig.from_env().model == "gpt-5.2"


def test_no_provider_signal_raises():
    with pytest.raises(ValueError, match="TPK_AGENT_PROVIDER"):
        AgentConfig.from_env()


def test_bad_provider_raises(monkeypatch):
    monkeypatch.setenv("TPK_AGENT_PROVIDER", "grok")
    with pytest.raises(ValueError):
        AgentConfig.from_env()


def test_build_chat_model_openai_gateway(monkeypatch):
    from tpk.agent import build_chat_model
    from tpk.config import AgentConfig

    monkeypatch.setenv("OPENAI_BASE_URL", "http://gw/v1")
    model = build_chat_model(AgentConfig(provider="openai", model="openai.gpt-oss-120b"))
    assert model.model_name == "openai.gpt-oss-120b"
    assert "gw" in str(model.openai_api_base)


def test_build_chat_model_anthropic(monkeypatch):
    from tpk.agent import build_chat_model
    from tpk.config import AgentConfig

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    model = build_chat_model(AgentConfig(provider="anthropic", model="claude-sonnet-5"))
    assert model.model == "claude-sonnet-5"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_config.py -v`
Expected: FAIL with `ImportError` (`AgentConfig` not defined).

- [ ] **Step 4: Implement**

Append to `src/tpk/config.py`:

```python
AGENT_PROVIDERS = ("anthropic", "openai")


@dataclass(frozen=True)
class AgentConfig:
    provider: str  # "anthropic" | "openai"
    model: str

    @classmethod
    def from_env(cls) -> "AgentConfig":
        provider = os.environ.get("TPK_AGENT_PROVIDER", "")
        if provider and provider not in AGENT_PROVIDERS:
            raise ValueError(
                f"TPK_AGENT_PROVIDER must be one of {AGENT_PROVIDERS}, got {provider!r}"
            )
        if not provider:
            if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_BASE_URL"):
                provider = "anthropic"
            elif os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
                provider = "openai"
            else:
                raise ValueError(
                    "no agent LLM configured: set TPK_AGENT_PROVIDER plus "
                    "ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL or "
                    "OPENAI_API_KEY/OPENAI_BASE_URL"
                )
        default_model = "claude-sonnet-5" if provider == "anthropic" else "gpt-5.2"
        return cls(provider=provider, model=os.environ.get("TPK_AGENT_MODEL") or default_model)
```

Create `src/tpk/agent.py`:

```python
"""Chat agent over the knowledge graph: model factory (this task), tools +
graph assembly (later tasks)."""

import os

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from tpk.config import AgentConfig

_PLACEHOLDER_KEY = "placeholder-gateway-key"


def build_chat_model(cfg: AgentConfig):
    if cfg.provider == "anthropic":
        return ChatAnthropic(
            model=cfg.model,
            base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
            api_key=os.environ.get("ANTHROPIC_API_KEY") or _PLACEHOLDER_KEY,
        )
    return ChatOpenAI(
        model=cfg.model,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        api_key=os.environ.get("OPENAI_API_KEY") or _PLACEHOLDER_KEY,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_config.py -v`
Expected: 7 PASS. If a `ChatAnthropic`/`ChatOpenAI` attribute name differs in the installed version (e.g. `model_name` vs `model`, `openai_api_base` vs `base_url`), adjust the ASSERTION to the real attribute — the constructor arguments above are the stable public API.

- [ ] **Step 6: Run the full suite, then commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/config.py src/tpk/agent.py tests/test_agent_config.py pyproject.toml uv.lock
git commit -m "feat: agent config and provider-switchable chat model"
```

---

### Task 2: LangChain tool bindings over KnowledgeGraph

**Files:**
- Create: `src/tpk/agent_tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: any object with the six `KnowledgeGraph` methods (tests use a fake; production passes `tpk.tools.KnowledgeGraph`).
- Produces: `tpk.agent_tools.build_agent_tools(kg) -> list` of six LangChain tools named exactly `search_entities`, `get_entity`, `neighbors`, `path_between`, `list_communities`, `read_source`, each a thin passthrough (no logic).

- [ ] **Step 1: Write the failing tests**

`tests/test_agent_tools.py`:

```python
from tpk.agent_tools import build_agent_tools

EXPECTED = {
    "search_entities", "get_entity", "neighbors",
    "path_between", "list_communities", "read_source",
}


class FakeKG:
    def __init__(self):
        self.calls = []

    def search_entities(self, query, kinds=None, repos=None, limit=20):
        self.calls.append(("search_entities", query))
        return [{"id": "x", "name": query}]

    def get_entity(self, entity_id):
        return {"id": entity_id}

    def neighbors(self, entity_id, rels=None, direction="both", depth=1, confidence=None):
        return {"nodes": [], "edges": [], "depth_used": depth}

    def path_between(self, id_a, id_b, max_depth=4):
        return None

    def list_communities(self, repo=None):
        return []

    def read_source(self, repo, file_path, line_start, line_end):
        return "line"


def test_six_tools_with_exact_names():
    tools = build_agent_tools(FakeKG())
    assert {t.name for t in tools} == EXPECTED
    assert all(t.description for t in tools)  # every tool documents itself


def test_tools_delegate_to_kg():
    kg = FakeKG()
    tools = {t.name: t for t in build_agent_tools(kg)}
    out = tools["search_entities"].invoke({"query": "checkpoint"})
    assert out == [{"id": "x", "name": "checkpoint"}]
    assert kg.calls == [("search_entities", "checkpoint")]
    assert tools["get_entity"].invoke({"entity_id": "abc"}) == {"id": "abc"}
    assert tools["read_source"].invoke(
        {"repo": "r", "file_path": "f.py", "line_start": 1, "line_end": 2}
    ) == "line"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpk.agent_tools'`.

- [ ] **Step 3: Implement `src/tpk/agent_tools.py`**

```python
"""LangChain tool bindings over KnowledgeGraph — thin passthroughs, mirroring
the MCP server's tool surface."""

from langchain_core.tools import tool


def build_agent_tools(kg) -> list:
    @tool
    def search_entities(
        query: str,
        kinds: list[str] | None = None,
        repos: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Find code/doc entities in the Timeplus knowledge graph by keyword.
        Every whitespace-separated token must match the entity's name,
        qualified name, or summary (case-insensitive). Start here for any
        question; use kinds/repos to narrow."""
        return kg.search_entities(query, kinds=kinds, repos=repos, limit=limit)

    @tool
    def get_entity(entity_id: str) -> dict | None:
        """Fetch the full record for one entity by its id. Returns null if
        the id does not exist."""
        return kg.get_entity(entity_id)

    @tool
    def neighbors(
        entity_id: str,
        rels: list[str] | None = None,
        direction: str = "both",
        depth: int = 1,
        confidence: str | None = None,
    ) -> dict:
        """Local subgraph around an entity (BFS, depth capped at 3).
        direction: out|in|both. confidence: EXTRACTED|INFERRED filters edges."""
        return kg.neighbors(
            entity_id, rels=rels, direction=direction, depth=depth, confidence=confidence
        )

    @tool
    def path_between(id_a: str, id_b: str, max_depth: int = 4) -> list[dict] | None:
        """Shortest connection between two entities (depth capped at 6), or
        null if none found."""
        return kg.path_between(id_a, id_b, max_depth=max_depth)

    @tool
    def list_communities(repo: str | None = None) -> list[dict]:
        """Cluster overview: (repo, community, node_count), largest first.
        Useful for orientation questions about a repo's structure."""
        return kg.list_communities(repo=repo)

    @tool
    def read_source(repo: str, file_path: str, line_start: int, line_end: int) -> str:
        """Read exact lines from a repo checkout (capped at 400 lines) so
        answers can quote real code or docs."""
        return kg.read_source(repo, file_path, line_start, line_end)

    return [search_entities, get_entity, neighbors, path_between, list_communities, read_source]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_tools.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tpk/agent_tools.py tests/test_agent_tools.py
git commit -m "feat: LangChain tool bindings over KnowledgeGraph"
```

---

### Task 3: Agent assembly — system prompt + LangGraph ReAct agent

**Files:**
- Modify: `src/tpk/agent.py` (append)
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `build_chat_model` (Task 1), `build_agent_tools` (Task 2), `RepoConfig` (has `name`, `description`, `visibility`, `extraction`).
- Produces:
  - `tpk.agent.RECURSION_LIMIT = 40`
  - `tpk.agent.system_prompt(repos: dict[str, RepoConfig]) -> str`
  - `tpk.agent.build_agent(kg, cfg: AgentConfig, repos: dict[str, RepoConfig], model=None)` → a LangGraph agent (`create_react_agent`); `model` is injectable for tests.

- [ ] **Step 1: Write the failing tests**

`tests/test_agent.py`:

```python
from pathlib import Path

from tpk.agent import RECURSION_LIMIT, build_agent, system_prompt
from tpk.config import AgentConfig, RepoConfig

REPOS = {
    "proton": RepoConfig(
        name="proton", path=Path("/r/proton"), visibility="internal",
        description="Core streaming SQL engine",
    ),
    "docs": RepoConfig(
        name="docs", path=Path("/r/docs"), visibility="public",
        extraction="semantic", description="Public product documentation",
    ),
}


class FakeKG:
    def search_entities(self, query, kinds=None, repos=None, limit=20):
        return []

    def get_entity(self, entity_id):
        return None

    def neighbors(self, entity_id, rels=None, direction="both", depth=1, confidence=None):
        return {"nodes": [], "edges": [], "depth_used": depth}

    def path_between(self, id_a, id_b, max_depth=4):
        return None

    def list_communities(self, repo=None):
        return []

    def read_source(self, repo, file_path, line_start, line_end):
        return ""


def test_system_prompt_contains_corpus_and_citation_rules():
    p = system_prompt(REPOS)
    assert "proton" in p and "Core streaming SQL engine" in p
    assert "docs" in p and "Public product documentation" in p
    assert "repo/file_path:line" in p
    assert "could not find this in the knowledge graph" in p


def test_recursion_limit_constant():
    assert RECURSION_LIMIT == 40


def test_build_agent_answers_via_fake_model():
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    fake = GenericFakeChatModel(messages=iter([AIMessage(content="grounded answer")]))
    agent = build_agent(
        FakeKG(), AgentConfig(provider="openai", model="x"), REPOS, model=fake
    )
    result = agent.invoke({"messages": [("user", "what is proton?")]})
    assert result["messages"][-1].content == "grounded answer"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -v`
Expected: FAIL with `ImportError` (`RECURSION_LIMIT` etc. not defined).

- [ ] **Step 3: Implement (append to `src/tpk/agent.py`)**

```python
from langgraph.prebuilt import create_react_agent

from tpk.agent_tools import build_agent_tools
from tpk.config import RepoConfig

RECURSION_LIMIT = 40


def system_prompt(repos: dict[str, "RepoConfig"]) -> str:
    corpus = "\n".join(
        f"- {r.name} ({r.visibility}): {r.description or 'no description'}"
        for r in repos.values()
    )
    return f"""You are the Timeplus knowledge agent. You answer questions about
Timeplus — its code, design, architecture, and devops — using ONLY the
knowledge graph tools available to you.

The knowledge graph covers these repositories:
{corpus}

Rules:
1. Ground every answer in tool results. Start with search_entities, then
   use neighbors / path_between / get_entity to explore, and read_source
   to quote real code or docs.
2. Every factual claim MUST carry a citation in the form
   repo/file_path:line (use the entity's repo, file_path, line_start).
3. If the tools return nothing relevant, say "I could not find this in
   the knowledge graph" — never invent an answer.
4. Prefer doc/concept entities for conceptual questions and code entities
   for implementation questions. Keep answers concise and structured."""


def build_agent(kg, cfg, repos: dict[str, "RepoConfig"], model=None):
    return create_react_agent(
        model if model is not None else build_chat_model(cfg),
        build_agent_tools(kg),
        prompt=system_prompt(repos),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent.py -v`
Expected: 3 PASS. If `GenericFakeChatModel` lacks `bind_tools`, monkeypatch it in the test (`fake.bind_tools = lambda *a, **k: fake`) — the test verifies wiring, not tool-calling. If the installed langgraph names the prompt parameter differently (`state_modifier` in older versions), use the name the installed version accepts — check with `python -c "import inspect, langgraph.prebuilt as p; print(inspect.signature(p.create_react_agent))"`.

- [ ] **Step 5: Run full suite, commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/agent.py tests/test_agent.py
git commit -m "feat: LangGraph ReAct agent with corpus-aware citation prompt"
```

---

### Task 4: FastAPI server — SSE /chat, healthz, static UI, `tpk serve`

**Files:**
- Create: `src/tpk/server.py`
- Modify: `src/tpk/cli.py` (add `serve` command)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `build_agent`, `RECURSION_LIMIT` (Task 3); `Settings`, `load_repos`, `AgentConfig` (config); `db.get_client`; `KnowledgeGraph`.
- Produces:
  - `tpk.server.create_app(agent=None) -> FastAPI` — when `agent` is None, builds the production agent lazily on first `/chat` call (so import needs no LLM env). `agent` is injectable for tests.
  - `POST /chat` body `{"message": str, "history": [{"role": "user"|"assistant", "content": str}, ...]}` (history optional) → `text/event-stream` per the Global Constraints SSE protocol.
  - `GET /healthz` → `{"status": "ok"}`.
  - Static: serves `web/dist` at `/` when that directory exists.
  - CLI: `tpk serve [--host 127.0.0.1] [--port 8000]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_server.py`:

```python
import json

from fastapi.testclient import TestClient

from tpk.server import create_app


class FakeAgent:
    """Yields the astream_events shapes the server consumes."""

    def __init__(self, events):
        self._events = events

    async def astream_events(self, _input, version="v2", config=None):
        for e in self._events:
            yield e


def _tok(text):
    class Chunk:
        content = text

    return {"event": "on_chat_model_stream", "data": {"chunk": Chunk()}}


def _tool(name, inp):
    return {"event": "on_tool_start", "name": name, "data": {"input": inp}}


def _parse_sse(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


def test_healthz():
    client = TestClient(create_app(agent=FakeAgent([])))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_chat_streams_tokens_tools_and_done():
    agent = FakeAgent([_tool("search_entities", {"query": "q"}), _tok("Hello "), _tok("world")])
    client = TestClient(create_app(agent=agent))
    resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    assert {"type": "tool", "name": "search_entities", "input": {"query": "q"}} in events
    assert {"type": "token", "text": "Hello "} in events
    assert events[-1] == {"type": "done", "text": "Hello world"}


def test_chat_streams_error_event():
    class BoomAgent:
        async def astream_events(self, _input, version="v2", config=None):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

    client = TestClient(create_app(agent=BoomAgent()))
    events = _parse_sse(client.post("/chat", json={"message": "hi"}).text)
    assert events[-1]["type"] == "error"
    assert "model exploded" in events[-1]["message"]


def test_chat_rejects_empty_message():
    client = TestClient(create_app(agent=FakeAgent([])))
    assert client.post("/chat", json={"message": ""}).status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpk.server'`.

- [ ] **Step 3: Implement `src/tpk/server.py`**

```python
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
    def chat(req: ChatRequest):
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
```

Add to `src/tpk/cli.py` (new command; keep existing imports style):

```python
@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000, help="Port"),
):
    """Run the knowledge agent chat server (SSE /chat + web UI)."""
    import uvicorn

    from tpk.server import create_app

    uvicorn.run(create_app(), host=host, port=port)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: 4 PASS. Note: the SSE route is a sync `def chat` returning a `StreamingResponse` over an async generator — FastAPI supports this; if the installed version complains, make `chat` `async def`.

- [ ] **Step 5: Smoke the CLI wiring, run full suite, commit**

```bash
uv run tpk serve --help
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/server.py src/tpk/cli.py tests/test_server.py
git commit -m "feat: FastAPI SSE chat server and tpk serve command"
```

---

### Task 5: React chat UI

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/index.html`, `web/tsconfig.json`, `web/src/main.tsx`, `web/src/App.tsx`, `web/src/app.css`
- Modify: `.gitignore` (add `web/node_modules/`, `web/dist/`)

**Interfaces:**
- Consumes: the SSE protocol from Task 4 verbatim (`token`/`tool`/`done`/`error`).
- Produces: `web/dist/` static build served by `tpk serve`; dev server proxies `/chat` and `/healthz` to `:8000`.

- [ ] **Step 1: Scaffold files**

`web/package.json`:

```json
{
  "name": "timeplus-knowledge-ui",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0"
  },
  "devDependencies": {
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "typescript": "^5.6.0",
    "vite": "^5.4.0"
  }
}
```

`web/vite.config.ts`:

```ts
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/chat": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
});
```

`web/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["src"]
}
```

`web/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Timeplus Knowledge</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`web/src/main.tsx`:

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./app.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

- [ ] **Step 2: Implement the chat component**

`web/src/App.tsx`:

```tsx
import { useRef, useState } from "react";

type Turn = { role: "user" | "assistant"; content: string; tools?: string[] };

async function* sseEvents(resp: Response): AsyncGenerator<any> {
  const reader = resp.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      if (frame.startsWith("data: ")) yield JSON.parse(frame.slice(6));
    }
  }
}

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setBusy(true);
    const history = turns.map((t) => ({ role: t.role, content: t.content }));
    setTurns((ts) => [...ts, { role: "user", content: message }, { role: "assistant", content: "", tools: [] }]);

    const update = (fn: (t: Turn) => Turn) =>
      setTurns((ts) => [...ts.slice(0, -1), fn(ts[ts.length - 1])]);

    try {
      const resp = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, history }),
      });
      for await (const ev of sseEvents(resp)) {
        if (ev.type === "token") update((t) => ({ ...t, content: t.content + ev.text }));
        else if (ev.type === "tool") update((t) => ({ ...t, tools: [...(t.tools ?? []), ev.name] }));
        else if (ev.type === "error") update((t) => ({ ...t, content: t.content + `\n\n[error] ${ev.message}` }));
        bottomRef.current?.scrollIntoView({ behavior: "smooth" });
      }
    } catch (e) {
      update((t) => ({ ...t, content: t.content + `\n\n[error] ${String(e)}` }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shell">
      <header>
        <h1>Timeplus Knowledge</h1>
        <p>Ask anything about Timeplus — code, architecture, deployment.</p>
      </header>
      <main>
        {turns.map((t, i) => (
          <div key={i} className={`turn ${t.role}`}>
            {t.tools && t.tools.length > 0 && (
              <div className="tools">🔎 {t.tools.join(" → ")}</div>
            )}
            <div className="bubble">{t.content || (busy && i === turns.length - 1 ? "…" : "")}</div>
          </div>
        ))}
        <div ref={bottomRef} />
      </main>
      <footer>
        <textarea
          value={input}
          placeholder="e.g. How do materialized view checkpoints work?"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), send())}
        />
        <button onClick={send} disabled={busy || !input.trim()}>
          {busy ? "Thinking…" : "Send"}
        </button>
      </footer>
    </div>
  );
}
```

`web/src/app.css`:

```css
:root {
  --bg: #0e1116;
  --panel: #161b22;
  --border: #2a313c;
  --text: #e6e8eb;
  --muted: #8b949e;
  --accent: #d53f8c; /* placeholder — swap for Timeplus console token */
  --user: #1f2937;
}
* { box-sizing: border-box; margin: 0; }
body { background: var(--bg); color: var(--text); font: 15px/1.6 -apple-system, "Segoe UI", sans-serif; }
.shell { max-width: 860px; margin: 0 auto; display: flex; flex-direction: column; height: 100vh; padding: 0 16px; }
header { padding: 20px 4px 12px; border-bottom: 1px solid var(--border); }
header h1 { font-size: 18px; }
header p { color: var(--muted); font-size: 13px; }
main { flex: 1; overflow-y: auto; padding: 16px 4px; display: flex; flex-direction: column; gap: 14px; }
.turn.user { align-self: flex-end; max-width: 80%; }
.turn.assistant { align-self: stretch; }
.turn.user .bubble { background: var(--user); border-radius: 10px; padding: 10px 14px; }
.turn.assistant .bubble { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; white-space: pre-wrap; }
.tools { color: var(--muted); font-size: 12px; margin-bottom: 4px; }
footer { display: flex; gap: 10px; padding: 14px 4px 20px; border-top: 1px solid var(--border); }
textarea { flex: 1; resize: none; height: 52px; background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font: inherit; }
button { background: var(--accent); color: white; border: 0; border-radius: 8px; padding: 0 22px; font: inherit; cursor: pointer; }
button:disabled { opacity: 0.5; cursor: default; }
```

Add to `.gitignore`:

```
web/node_modules/
web/dist/
```

- [ ] **Step 3: Build and verify**

```bash
cd web && npm install && npm run build && cd ..
ls web/dist/index.html
```

Expected: build succeeds, `web/dist/index.html` exists.

- [ ] **Step 4: Serve-and-click smoke (needs the populated graph + agent env)**

```bash
TIMEPLUS_HOST=localhost uv run tpk serve &
sleep 2 && curl -s http://127.0.0.1:8000/healthz && curl -s http://127.0.0.1:8000/ | head -3
kill %1
```

Expected: healthz ok; HTML served. (Full chat behavior is exercised in Task 7.)

- [ ] **Step 5: Commit**

```bash
git add web .gitignore
git commit -m "feat: React chat UI with SSE streaming"
```

---

### Task 6: Docker image, compose service, Makefile, README

**Files:**
- Modify: `deploy/docker/Dockerfile` (web build stage), `docker-compose.yml` (agent service), `Makefile`, `README.md`, `.dockerignore`

**Interfaces:**
- Consumes: `tpk serve` (Task 4), `web/` (Task 5), existing image layout (`/opt/tpk`, wrappers, user 101).
- Produces: image serving the UI; compose `agent` service on port 8000.

- [ ] **Step 1: Add the web build stage to `deploy/docker/Dockerfile`**

At the very top of the file (before the existing `FROM`):

```dockerfile
FROM node:22-alpine AS webbuild
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build
```

After the existing `COPY src ./src` / `uv sync` block, add:

```dockerfile
COPY --from=webbuild /web/dist /opt/tpk/web/dist
```

Note: `server.py` resolves `WEB_DIST` as `parents[2]/web/dist` relative to `src/tpk/server.py` → `/opt/tpk/web/dist` in the image. Also append `EXPOSE 8000` near the env block, and remove `web/dist/` from build-context excludes if `.dockerignore` blocks it (`web/node_modules/` must stay excluded; the stage copies `web/` from context, so exclude `web/dist` too — the stage rebuilds it).

- [ ] **Step 2: Add the agent service to `docker-compose.yml`**

```yaml
  agent:
    image: timeplus/tpk:dev
    container_name: tpk-agent
    depends_on:
      - tpk
    entrypoint: ["tpk"]
    command: ["serve", "--host", "0.0.0.0", "--port", "8000"]
    ports:
      - "8000:8000"
    volumes:
      - ${REPOS_MOUNT:-${HOME}/Code/timeplus}:/repos:ro
    environment:
      TIMEPLUS_HOST: tpk
      TPK_AGENT_PROVIDER: ${TPK_AGENT_PROVIDER:-}
      TPK_AGENT_MODEL: ${TPK_AGENT_MODEL:-}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
      ANTHROPIC_BASE_URL: ${ANTHROPIC_BASE_URL:-}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      OPENAI_BASE_URL: ${OPENAI_BASE_URL:-}
    restart: unless-stopped
```

Also add `TPK_AGENT_PROVIDER=` / `TPK_AGENT_MODEL=` entries (commented, with a note that the agent model may differ from the extraction model) to `.env.example`.

- [ ] **Step 3: Makefile + README**

Makefile additions (in the appropriate sections, keep `.PHONY` updated):

```make
serve: ## Run the chat server locally (needs agent env + populated graph)
	TIMEPLUS_HOST=$(TIMEPLUS_HOST) uv run tpk serve

web-build: ## Build the React UI into web/dist
	cd web && npm install && npm run build

web-dev: ## Run the Vite dev server (proxies /chat to :8000)
	cd web && npm install && npm run dev
```

README: add a "Chat agent & web UI" section documenting `TPK_AGENT_PROVIDER`/`TPK_AGENT_MODEL`, `make serve` + `make web-build` locally, the compose `agent` service on http://localhost:8000, and the SSE protocol one-liner.

- [ ] **Step 4: Build and verify the stack**

```bash
docker compose build 2>&1 | tail -2
docker compose up -d
sleep 20
curl -s http://localhost:8000/healthz
curl -s http://localhost:8000/ | grep -o "<title>[^<]*"
```

Expected: `{"status":"ok"}` and `<title>Timeplus Knowledge`.

- [ ] **Step 5: Run full suite, commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add deploy/docker/Dockerfile docker-compose.yml Makefile README.md .env.example .dockerignore
git commit -m "feat: agent service in compose, web build stage, serve targets"
```

---

### Task 7: Live eval smoke (manual gate)

**Files:**
- Create: `docs/eval/m4-smoke.md` (results record)

**Interfaces:** consumes the running compose stack with the populated graph and the user's LLM env.

- [ ] **Step 1: Ask the smoke questions over the live API**

With `docker compose up -d` (both services) and the graph populated, run each and save outputs:

```bash
q() { curl -sN -X POST http://localhost:8000/chat -H 'Content-Type: application/json' \
  -d "{\"message\": \"$1\"}" | grep '^data: ' | tail -1; }
q "What is a materialized view checkpoint in proton and where is it implemented?"
q "How do I deploy Timeplus Enterprise on Kubernetes with the helm charts?"
q "What does the docs say about installing Timeplus?"
q "Which component handles Raft log storage and how big are its segments by default?"
q "What is the airspeed velocity of an unladen swallow?"
```

- [ ] **Step 2: Judge against the bar**

For each answer record in `docs/eval/m4-smoke.md`: the question, the final `done` text, and PASS/FAIL against:
- answers 1–4: at least one `repo/file_path:line`-style citation whose file actually exists in the corpus; tool events show ≥1 `search_entities` call; no invented file paths.
- question 5 (off-corpus): the agent must answer with "I could not find this in the knowledge graph" (or equivalent refusal), not a fabrication.

- [ ] **Step 3: Commit the record**

```bash
git add docs/eval
git commit -m "docs: M4 live eval smoke results"
```

If any answer FAILs on citations or refusal behavior, tune ONLY `system_prompt` wording (Task 3 file) and re-run this task; a failure caused by tool errors or server bugs goes back to the owning task instead.

---

## After this plan

- v2 items remain per spec: auth + customer tier (visibility filter + answer sanitization), scheduled re-index, UI polish to full Timeplus console tokens.
- Deferred technical debt tracked in the ledger: function-kind id collisions (~3% on proton), `_eventually`/REPOS_TOML DRY, `kg_ingest_log` error column.
