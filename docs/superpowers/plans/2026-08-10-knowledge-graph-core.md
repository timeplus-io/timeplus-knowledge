# Timeplus Knowledge Graph Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the knowledge-graph core of the Timeplus knowledge agent: a `tpk` CLI that ingests Timeplus repos via graphify into Timeplus mutable streams, a tools package for graph access, and an MCP server exposing those tools to Claude Code/Cursor.

**Architecture:** Graphify parses each repo into `graph.json`; an ingest pipeline transforms that into rows upserted into Timeplus mutable streams (`kg_nodes`, `kg_edges`) plus an append-only `kg_ingest_log`. A single `KnowledgeGraph` class in `tpk/tools.py` is the only read path (SQL over `table(...)`), consumed by an MCP server now and by the LangGraph agent in the follow-up plan.

**Tech Stack:** Python 3.11+, `uv` for env/packaging, `timeplus-connect` (Timeplus Python driver), `typer` (CLI), `mcp` SDK (FastMCP), `pytest`, graphify (installed from GitHub).

**Spec:** `docs/superpowers/specs/2026-08-10-timeplus-knowledge-agent-design.md`. This plan covers milestones M1–M2 and the mechanics needed for M3 (full-corpus ingest is a run of the same CLI). M4 (LangGraph agent + FastAPI + React UI) is a separate follow-up plan.

## Global Constraints

- Python ≥ 3.11; manage env and deps with `uv` (`uv sync`, `uv run`).
- All Timeplus access via env vars `TIMEPLUS_HOST`, `TIMEPLUS_USER`, `TIMEPLUS_PASSWORD` (defaults: `localhost`, `default`, empty). Never hardcode credentials.
- DDL/DML over the HTTP interface, port 8123, through `timeplus-connect`. Every read query wraps the stream in `table(...)` — a bare `SELECT FROM stream` is an unbounded streaming query and will hang.
- Stream names are exactly `kg_nodes`, `kg_edges`, `kg_ingest_log`. A `stream_prefix` (used only by tests, e.g. `test_ab12cd_`) namespaces test streams; production prefix is `""`.
- Node `visibility` is `'internal'` or `'public'` — nothing else. Docs-derived nodes are `public`, source-derived are `internal`.
- Agent-facing caps (enforced in `tools.py`): traversal depth ≤ 3, ≤ 200 nodes per hop.
- Integration tests require a running Timeplus Enterprise (`timeplusd`) because mutable streams are an Enterprise feature; tests skip cleanly when `TIMEPLUS_HOST` is unset. Local dev: `docker run -d -p 8123:8123 -p 3218:3218 docker.timeplus.com/timeplus/timeplusd:latest`.
- Commit messages: conventional style (`feat:`, `test:`, `chore:`), one commit per task minimum.

## File Structure

```
pyproject.toml                  # package `tpk`, deps, pytest config
repos.toml                      # corpus config: repo name → local path, visibility
src/tpk/__init__.py
src/tpk/config.py               # Settings (env), RepoConfig (repos.toml)
src/tpk/db.py                   # client factory, ensure_schema/drop_schema DDL
src/tpk/model.py                # Node/Edge dataclasses, stable IDs, row conversion
src/tpk/graphify_runner.py      # run graphify CLI; parse graph.json → Node/Edge lists
src/tpk/ingest.py               # per-repo ingest: upsert, stale delete, log
src/tpk/cli.py                  # typer app: `tpk ingest`, `tpk status`
src/tpk/tools.py                # KnowledgeGraph: search/get/neighbors/path/communities/read_source
src/tpk/mcp_server.py           # FastMCP wrapper over KnowledgeGraph
tests/conftest.py               # skip marker, prefixed-schema Timeplus fixture
tests/fixtures/sample_graph.json  # checked-in graphify-shaped output (golden input)
tests/fixtures/tinyrepo/        # 3-file toy repo for the real-graphify integration check
tests/test_config.py
tests/test_db.py
tests/test_model.py
tests/test_graphify_runner.py
tests/test_ingest.py
tests/test_tools.py
tests/test_mcp_server.py
README.md
```

---

### Task 1: Project scaffold + config

**Files:**
- Create: `pyproject.toml`, `src/tpk/__init__.py`, `src/tpk/config.py`, `repos.toml`, `tests/test_config.py`, `.gitignore`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `tpk.config.Settings` dataclass: `host: str`, `user: str`, `password: str`, `port: int = 8123`, `stream_prefix: str = ""`; classmethod `Settings.from_env() -> Settings` reading `TIMEPLUS_HOST`/`TIMEPLUS_USER`/`TIMEPLUS_PASSWORD` with defaults `localhost`/`default`/`""`.
  - `tpk.config.RepoConfig` dataclass: `name: str`, `path: Path`, `visibility: str` (`"internal"` or `"public"`).
  - `tpk.config.load_repos(toml_path: Path) -> dict[str, RepoConfig]`.

- [ ] **Step 1: Scaffold the package**

```bash
cd /Users/gangtao/Code/timeplus/timeplus-knowledge
uv init --lib --name tpk --python 3.11 .
uv add timeplus-connect typer "mcp[cli]"
uv add --dev pytest
```

Then replace the generated `src/tpk/__init__.py` content with just `__version__ = "0.1.0"`. Ensure `pyproject.toml` contains:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

And `.gitignore` contains:

```
.venv/
__pycache__/
*.egg-info/
.graphify_out/
```

- [ ] **Step 2: Write the failing tests**

`tests/test_config.py`:

```python
from pathlib import Path

from tpk.config import Settings, load_repos


def test_settings_from_env_defaults(monkeypatch):
    monkeypatch.delenv("TIMEPLUS_HOST", raising=False)
    monkeypatch.delenv("TIMEPLUS_USER", raising=False)
    monkeypatch.delenv("TIMEPLUS_PASSWORD", raising=False)
    s = Settings.from_env()
    assert s.host == "localhost"
    assert s.user == "default"
    assert s.password == ""
    assert s.port == 8123
    assert s.stream_prefix == ""


def test_settings_from_env_reads_vars(monkeypatch):
    monkeypatch.setenv("TIMEPLUS_HOST", "tp.example.com")
    monkeypatch.setenv("TIMEPLUS_USER", "eng")
    monkeypatch.setenv("TIMEPLUS_PASSWORD", "secret")
    s = Settings.from_env()
    assert (s.host, s.user, s.password) == ("tp.example.com", "eng", "secret")


def test_load_repos(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.docs]\npath = "/x/docs"\nvisibility = "public"\n\n'
        '[repos.proton]\npath = "/x/proton"\nvisibility = "internal"\n'
    )
    repos = load_repos(toml_path)
    assert set(repos) == {"docs", "proton"}
    assert repos["docs"].visibility == "public"
    assert repos["proton"].path == Path("/x/proton")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ImportError` / `ModuleNotFoundError` (config module doesn't exist).

- [ ] **Step 4: Implement `src/tpk/config.py`**

```python
"""Environment settings and corpus (repos.toml) configuration."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str
    user: str
    password: str
    port: int = 8123
    stream_prefix: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("TIMEPLUS_HOST", "localhost"),
            user=os.environ.get("TIMEPLUS_USER", "default"),
            password=os.environ.get("TIMEPLUS_PASSWORD", ""),
        )


@dataclass(frozen=True)
class RepoConfig:
    name: str
    path: Path
    visibility: str  # "internal" | "public"


def load_repos(toml_path: Path) -> dict[str, RepoConfig]:
    data = tomllib.loads(toml_path.read_text())
    return {
        name: RepoConfig(name=name, path=Path(cfg["path"]), visibility=cfg["visibility"])
        for name, cfg in data["repos"].items()
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Create the real `repos.toml`** (paths are the local checkouts; docs is the only `public` repo)

```toml
[repos.proton]
path = "/Users/gangtao/Code/timeplus/proton"
visibility = "internal"

[repos.proton-enterprise]
path = "/Users/gangtao/Code/timeplus/proton-enterprise"
visibility = "internal"

[repos.timeplus-enterprise]
path = "/Users/gangtao/Code/timeplus/timeplus-enterprise"
visibility = "internal"

[repos.helm-charts]
path = "/Users/gangtao/Code/timeplus/helm-charts"
visibility = "internal"

[repos.timeplus-enterprise-deployment]
path = "/Users/gangtao/Code/timeplus/timeplus-enterprise-deployment"
visibility = "internal"

[repos.terraform-provider-timeplus]
path = "/Users/gangtao/Code/timeplus/terraform-provider-timeplus"
visibility = "internal"

[repos.byoc]
path = "/Users/gangtao/Code/timeplus/byoc"
visibility = "internal"

[repos.timeplus-cli]
path = "/Users/gangtao/Code/timeplus/timeplus-cli"
visibility = "internal"

[repos.docs]
path = "/Users/gangtao/Code/timeplus/docs"
visibility = "public"
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .gitignore src/ tests/ repos.toml
git commit -m "feat: scaffold tpk package with env settings and repos.toml config"
```

---

### Task 2: Timeplus client + schema

**Files:**
- Create: `src/tpk/db.py`, `tests/conftest.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `tpk.config.Settings` (Task 1).
- Produces:
  - `tpk.db.get_client(settings: Settings)` → a `timeplus_connect` client.
  - `tpk.db.ensure_schema(client, prefix: str = "") -> None` — creates `{prefix}kg_nodes`, `{prefix}kg_edges` (mutable), `{prefix}kg_ingest_log` (append), idempotent.
  - `tpk.db.drop_schema(client, prefix: str) -> None` — drops the three streams (refuses when `prefix == ""` to protect production).
  - Test fixture `tp` in `conftest.py`: yields `(client, prefix)` with a unique prefixed schema, drops it on teardown; whole test module skips when `TIMEPLUS_HOST` is unset.

- [ ] **Step 1: Write the failing test**

`tests/conftest.py`:

```python
import os
import uuid

import pytest

requires_timeplus = pytest.mark.skipif(
    not os.environ.get("TIMEPLUS_HOST"),
    reason="TIMEPLUS_HOST not set; integration tests need a running timeplusd",
)


@pytest.fixture()
def tp():
    from tpk.config import Settings
    from tpk import db

    settings = Settings.from_env()
    prefix = f"test_{uuid.uuid4().hex[:6]}_"
    client = db.get_client(settings)
    db.ensure_schema(client, prefix)
    yield client, prefix
    db.drop_schema(client, prefix)
```

`tests/test_db.py`:

```python
import pytest

from conftest import requires_timeplus

pytestmark = requires_timeplus


def test_schema_created_and_idempotent(tp):
    from tpk import db

    client, prefix = tp
    # calling again must not raise (IF NOT EXISTS)
    db.ensure_schema(client, prefix)
    names = {r[0] for r in client.query("SHOW STREAMS").result_rows}
    assert {f"{prefix}kg_nodes", f"{prefix}kg_edges", f"{prefix}kg_ingest_log"} <= names


def test_drop_schema_refuses_empty_prefix(tp):
    from tpk import db

    client, _ = tp
    with pytest.raises(ValueError):
        db.drop_schema(client, "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpk.db'` (or AttributeError). If Timeplus isn't running locally, start it first: `docker run -d --name timeplusd -p 8123:8123 -p 3218:3218 docker.timeplus.com/timeplus/timeplusd:latest`.

- [ ] **Step 3: Implement `src/tpk/db.py`**

```python
"""Timeplus client factory and knowledge-graph schema DDL."""

import timeplus_connect

from tpk.config import Settings


def get_client(settings: Settings):
    return timeplus_connect.get_client(
        host=settings.host,
        port=settings.port,
        username=settings.user,
        password=settings.password,
    )


def ensure_schema(client, prefix: str = "") -> None:
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_nodes (
          id string,
          repo string,
          kind string,
          name string,
          qualified_name string,
          file_path string,
          line_start uint32,
          line_end uint32,
          summary string,
          community string,
          visibility string,
          updated_at datetime64(3, 'UTC')
        ) PRIMARY KEY (id)
    """)
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_edges (
          src string,
          dst string,
          rel string,
          confidence string,
          repo string,
          updated_at datetime64(3, 'UTC')
        ) PRIMARY KEY (src, dst, rel)
    """)
    client.command(f"""
        CREATE STREAM IF NOT EXISTS {prefix}kg_ingest_log (
          repo string,
          run_id string,
          nodes uint64,
          edges uint64,
          git_sha string,
          status string
        )
    """)


def drop_schema(client, prefix: str) -> None:
    if not prefix:
        raise ValueError("refusing to drop unprefixed (production) streams")
    for name in ("kg_nodes", "kg_edges", "kg_ingest_log"):
        client.command(f"DROP STREAM IF EXISTS {prefix}{name}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_db.py -v`
Expected: 2 PASS. Also run `uv run pytest tests/test_db.py -v` **without** `TIMEPLUS_HOST` and confirm both tests SKIP.

- [ ] **Step 5: Commit**

```bash
git add src/tpk/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: timeplus client and kg schema with prefixed test isolation"
```

---

### Task 3: Graph model — Node/Edge, stable IDs, row conversion

**Files:**
- Create: `src/tpk/model.py`, `tests/test_model.py`

**Interfaces:**
- Consumes: nothing from other modules (pure).
- Produces:
  - `tpk.model.Node` dataclass: `id: str`, `repo: str`, `kind: str`, `name: str`, `qualified_name: str`, `file_path: str`, `line_start: int`, `line_end: int`, `summary: str`, `community: str`, `visibility: str`.
  - `tpk.model.Edge` dataclass: `src: str`, `dst: str`, `rel: str`, `confidence: str`, `repo: str`.
  - `tpk.model.node_id(repo: str, kind: str, qualified_name: str) -> str` — deterministic 16-hex-char id.
  - `tpk.model.NODE_COLUMNS: list[str]`, `EDGE_COLUMNS: list[str]` — column orders matching the streams (both ending in `updated_at`).
  - `tpk.model.node_rows(nodes: list[Node], updated_at) -> list[list]`, `edge_rows(edges: list[Edge], updated_at) -> list[list]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_model.py`:

```python
from datetime import datetime, timezone

from tpk.model import (
    EDGE_COLUMNS,
    NODE_COLUMNS,
    Edge,
    Node,
    edge_rows,
    node_id,
    node_rows,
)


def test_node_id_is_deterministic_and_distinct():
    a = node_id("proton", "function", "Checkpoint::flush")
    b = node_id("proton", "function", "Checkpoint::flush")
    c = node_id("proton", "class", "Checkpoint::flush")
    assert a == b
    assert a != c
    assert len(a) == 16


def _node(**over):
    base = dict(
        id="abc", repo="docs", kind="doc_section", name="Install",
        qualified_name="install/index.md#Install", file_path="install/index.md",
        line_start=1, line_end=40, summary="How to install",
        community="c1", visibility="public",
    )
    base.update(over)
    return Node(**base)


def test_node_rows_match_column_order():
    ts = datetime(2026, 8, 10, tzinfo=timezone.utc)
    rows = node_rows([_node()], ts)
    assert len(rows) == 1
    assert len(rows[0]) == len(NODE_COLUMNS)
    assert rows[0][NODE_COLUMNS.index("id")] == "abc"
    assert rows[0][-1] == ts and NODE_COLUMNS[-1] == "updated_at"


def test_edge_rows_match_column_order():
    ts = datetime(2026, 8, 10, tzinfo=timezone.utc)
    e = Edge(src="abc", dst="def", rel="documents", confidence="EXTRACTED", repo="docs")
    rows = edge_rows([e], ts)
    assert rows[0][EDGE_COLUMNS.index("rel")] == "documents"
    assert rows[0][-1] == ts and EDGE_COLUMNS[-1] == "updated_at"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpk.model'`.

- [ ] **Step 3: Implement `src/tpk/model.py`**

```python
"""Graph domain model: nodes, edges, stable IDs, stream-row conversion."""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    id: str
    repo: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    line_start: int
    line_end: int
    summary: str
    community: str
    visibility: str


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    rel: str
    confidence: str
    repo: str


NODE_COLUMNS = [
    "id", "repo", "kind", "name", "qualified_name", "file_path",
    "line_start", "line_end", "summary", "community", "visibility", "updated_at",
]

EDGE_COLUMNS = ["src", "dst", "rel", "confidence", "repo", "updated_at"]


def node_id(repo: str, kind: str, qualified_name: str) -> str:
    return hashlib.sha256(f"{repo}:{kind}:{qualified_name}".encode()).hexdigest()[:16]


def node_rows(nodes: list[Node], updated_at) -> list[list]:
    return [
        [n.id, n.repo, n.kind, n.name, n.qualified_name, n.file_path,
         n.line_start, n.line_end, n.summary, n.community, n.visibility, updated_at]
        for n in nodes
    ]


def edge_rows(edges: list[Edge], updated_at) -> list[list]:
    return [[e.src, e.dst, e.rel, e.confidence, e.repo, updated_at] for e in edges]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_model.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tpk/model.py tests/test_model.py
git commit -m "feat: graph model with stable node ids and stream-row conversion"
```

---

### Task 4: Graphify runner + graph.json parser

**Files:**
- Create: `src/tpk/graphify_runner.py`, `tests/test_graphify_runner.py`, `tests/fixtures/sample_graph.json`, `tests/fixtures/tinyrepo/` (3 small files)

**Interfaces:**
- Consumes: `tpk.model.Node`, `Edge`, `node_id` (Task 3); `tpk.config.RepoConfig` (Task 1).
- Produces:
  - `tpk.graphify_runner.run_graphify(repo_path: Path, out_dir: Path) -> Path` — invokes the graphify CLI, returns the path to `graph.json`. Raises `GraphifyError` (defined here) on non-zero exit.
  - `tpk.graphify_runner.parse_graph_json(path: Path, repo: str, default_visibility: str) -> tuple[list[Node], list[Edge]]` — re-keys graphify node ids to our stable `node_id`, drops edges whose endpoints are unknown, tolerates the field-name variants graphify emits.

**IMPORTANT — format discovery:** graphify's exact `graph.json` field names must be verified against reality, not assumed. Step 1 installs graphify and runs it on the tiny fixture repo; Step 2 pins the observed shape into `tests/fixtures/sample_graph.json`. If the observed field names differ from the parser skeleton in Step 5, adapt the parser (and the fixture) to reality — the fixture must be a faithful copy of real graphify output.

- [ ] **Step 1: Create the tiny fixture repo and run real graphify against it**

Create `tests/fixtures/tinyrepo/` with three files:

`tests/fixtures/tinyrepo/app.py`:

```python
"""Tiny app used as a graphify fixture."""

from helper import add


def main():
    # NOTE: entry point for the fixture graph
    return add(1, 2)
```

`tests/fixtures/tinyrepo/helper.py`:

```python
def add(a, b):
    return a + b
```

`tests/fixtures/tinyrepo/README.md`:

```markdown
# tinyrepo

A three-file fixture. `main` calls `add`.
```

Install and run graphify:

```bash
uv add --dev "graphify @ git+https://github.com/Graphify-Labs/graphify"
uv run graphify extract tests/fixtures/tinyrepo --out /tmp/tinyrepo-graph
cat /tmp/tinyrepo-graph/graph.json | python3 -m json.tool | head -80
```

If the package name, CLI name, or flags differ (check `uv run graphify --help` and the repo README at https://github.com/Graphify-Labs/graphify), record the working invocation — Step 5's `run_graphify` must use the real one.

- [ ] **Step 2: Pin the observed output as the golden fixture**

Copy the real output: `cp /tmp/tinyrepo-graph/graph.json tests/fixtures/sample_graph.json`. Trim it by hand if huge, keeping: ≥ 4 nodes (including function `main`, function `add`, and at least one doc/file node), ≥ 2 edges (including a `calls`-like edge from `main` to `add`), preserving the exact field names graphify used.

- [ ] **Step 3: Write the failing parser test** (adjust the two assertions marked `# shape` to the real fixture contents from Step 2)

`tests/test_graphify_runner.py`:

```python
from pathlib import Path

from tpk.graphify_runner import parse_graph_json
from tpk.model import node_id

FIXTURE = Path(__file__).parent / "fixtures" / "sample_graph.json"


def test_parse_produces_stable_ids_and_edges():
    nodes, edges = parse_graph_json(FIXTURE, repo="tinyrepo", default_visibility="internal")
    assert len(nodes) >= 4  # shape
    assert len(edges) >= 2  # shape
    names = {n.name for n in nodes}
    assert {"main", "add"} <= names
    main = next(n for n in nodes if n.name == "main")
    assert main.id == node_id("tinyrepo", main.kind, main.qualified_name)
    assert main.repo == "tinyrepo"
    assert main.visibility == "internal"
    # every edge endpoint resolves to a parsed node id
    ids = {n.id for n in nodes}
    assert all(e.src in ids and e.dst in ids for e in edges)
    assert all(e.confidence in ("EXTRACTED", "INFERRED") for e in edges)


def test_parse_drops_edges_with_unknown_endpoints(tmp_path: Path):
    bad = tmp_path / "graph.json"
    bad.write_text(
        '{"nodes": [{"id": "n1", "name": "x", "type": "function", "file": "a.py"}],'
        ' "edges": [{"source": "n1", "target": "ghost", "type": "calls"}]}'
    )
    nodes, edges = parse_graph_json(bad, repo="r", default_visibility="internal")
    assert len(nodes) == 1
    assert edges == []
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_graphify_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpk.graphify_runner'`.

- [ ] **Step 5: Implement `src/tpk/graphify_runner.py`** (skeleton below assumes common field-name variants; reconcile with the real fixture from Step 2 — reality wins)

```python
"""Run graphify on a repo checkout and parse its graph.json output."""

import json
import subprocess
from pathlib import Path

from tpk.model import Edge, Node, node_id


class GraphifyError(RuntimeError):
    pass


def run_graphify(repo_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Reconcile with the invocation verified in Task 4 Step 1.
    proc = subprocess.run(
        ["graphify", "extract", str(repo_path), "--out", str(out_dir)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GraphifyError(f"graphify failed on {repo_path}: {proc.stderr[-2000:]}")
    graph_json = out_dir / "graph.json"
    if not graph_json.exists():
        raise GraphifyError(f"graphify produced no graph.json in {out_dir}")
    return graph_json


def _first(d: dict, keys: list[str], default=""):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


DOC_KINDS = {"doc", "doc_section", "document", "markdown", "concept"}


def parse_graph_json(
    path: Path, repo: str, default_visibility: str
) -> tuple[list[Node], list[Edge]]:
    data = json.loads(path.read_text())
    raw_nodes = data.get("nodes", [])
    raw_edges = data.get("edges") or data.get("links") or []

    nodes: list[Node] = []
    id_map: dict[str, str] = {}  # graphify id -> stable tpk id
    for rn in raw_nodes:
        kind = str(_first(rn, ["kind", "type", "label"], "entity")).lower()
        name = str(_first(rn, ["name", "title", "id"]))
        qualified = str(_first(rn, ["qualified_name", "qualifiedName", "fqn"], name))
        file_path = str(_first(rn, ["file_path", "file", "path"]))
        stable = node_id(repo, kind, qualified or f"{file_path}:{name}")
        raw_id = str(_first(rn, ["id", "name"]))
        id_map[raw_id] = stable
        visibility = "public" if kind in DOC_KINDS else default_visibility
        nodes.append(
            Node(
                id=stable,
                repo=repo,
                kind=kind,
                name=name,
                qualified_name=qualified,
                file_path=file_path,
                line_start=int(_first(rn, ["line_start", "lineStart", "line"], 0) or 0),
                line_end=int(_first(rn, ["line_end", "lineEnd"], 0) or 0),
                summary=str(_first(rn, ["summary", "description", "docstring"])),
                community=str(_first(rn, ["community", "cluster"])),
                visibility=visibility,
            )
        )

    edges: list[Edge] = []
    for re_ in raw_edges:
        src = id_map.get(str(_first(re_, ["source", "src", "from"])))
        dst = id_map.get(str(_first(re_, ["target", "dst", "to"])))
        if not src or not dst:
            continue  # endpoint outside this repo's parsed nodes
        confidence = str(_first(re_, ["confidence", "tag"], "EXTRACTED")).upper()
        if confidence not in ("EXTRACTED", "INFERRED"):
            confidence = "EXTRACTED"
        edges.append(
            Edge(
                src=src,
                dst=dst,
                rel=str(_first(re_, ["rel", "type", "relation", "label"], "related")).lower(),
                confidence=confidence,
                repo=repo,
            )
        )
    return nodes, edges
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_graphify_runner.py -v`
Expected: 2 PASS. If the parser needed changes to match real graphify output, re-check that `tests/fixtures/sample_graph.json` is still a faithful (possibly trimmed) copy of real output — never hand-craft it to fit the parser.

- [ ] **Step 7: Integration check against real graphify** (no assertion changes — just confirm end-to-end)

```bash
uv run python -c "
from pathlib import Path
from tpk.graphify_runner import run_graphify, parse_graph_json
p = run_graphify(Path('tests/fixtures/tinyrepo'), Path('/tmp/tinyrepo-check'))
nodes, edges = parse_graph_json(p, 'tinyrepo', 'internal')
print(f'{len(nodes)} nodes, {len(edges)} edges')
assert nodes and edges
"
```

Expected: prints a nonzero node/edge count, no traceback.

- [ ] **Step 8: Commit**

```bash
git add src/tpk/graphify_runner.py tests/test_graphify_runner.py tests/fixtures/ pyproject.toml uv.lock
git commit -m "feat: graphify runner and graph.json parser with golden fixture"
```

---

### Task 5: Ingest pipeline + `tpk` CLI

**Files:**
- Create: `src/tpk/ingest.py`, `src/tpk/cli.py`, `tests/test_ingest.py`
- Modify: `pyproject.toml` (add the `tpk` script entry)

**Interfaces:**
- Consumes: `db.get_client`/`ensure_schema` (Task 2); `model.node_rows`/`edge_rows`/`NODE_COLUMNS`/`EDGE_COLUMNS` (Task 3); `graphify_runner.run_graphify`/`parse_graph_json` (Task 4); `config` (Task 1).
- Produces:
  - `tpk.ingest.IngestResult` dataclass: `repo: str`, `run_id: str`, `nodes: int`, `edges: int`, `status: str` (`"ok"` | `"failed"`).
  - `tpk.ingest.ingest_repo(client, repo_cfg: RepoConfig, prefix: str = "", out_root: Path = Path(".graphify_out")) -> IngestResult` — full per-repo pipeline: graphify → parse → upsert → stale-delete → log. On any exception: logs `status="failed"`, leaves prior graph rows intact, returns (does not raise).
  - `tpk.ingest.upsert_graph(client, prefix, nodes, edges, run_started_at) -> None` — insert rows then delete stale rows for that repo (exposed separately so tests can drive it without graphify).
  - Console script `tpk` with commands `ingest` (`--repo NAME` optional, default all) and `status`.

- [ ] **Step 1: Write the failing tests** (drives `upsert_graph` + `ingest_repo`'s failure path; graphify itself is exercised via a stub so the test doesn't depend on the graphify binary)

`tests/test_ingest.py`:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import requires_timeplus
from tpk.config import RepoConfig
from tpk.model import Edge, Node

pytestmark = requires_timeplus


def _node(nid: str, name: str, repo: str = "tinyrepo") -> Node:
    return Node(
        id=nid, repo=repo, kind="function", name=name,
        qualified_name=f"mod.{name}", file_path="mod.py",
        line_start=1, line_end=5, summary=f"summary of {name}",
        community="c1", visibility="internal",
    )


def _count(client, prefix, stream, repo):
    return client.query(
        f"SELECT count() FROM table({prefix}{stream}) WHERE repo = %(r)s",
        parameters={"r": repo},
    ).result_rows[0][0]


def test_upsert_then_stale_delete(tp):
    from tpk.ingest import upsert_graph

    client, prefix = tp
    t0 = datetime.now(timezone.utc)
    nodes = [_node("n1", "alpha"), _node("n2", "beta")]
    edges = [Edge(src="n1", dst="n2", rel="calls", confidence="EXTRACTED", repo="tinyrepo")]
    upsert_graph(client, prefix, nodes, edges, run_started_at=t0)
    assert _count(client, prefix, "kg_nodes", "tinyrepo") == 2

    # second run: n2 disappeared, n1 updated -> n2 must be stale-deleted
    t1 = datetime.now(timezone.utc) + timedelta(seconds=1)
    upsert_graph(client, prefix, [_node("n1", "alpha")], [], run_started_at=t1)
    assert _count(client, prefix, "kg_nodes", "tinyrepo") == 1
    assert _count(client, prefix, "kg_edges", "tinyrepo") == 0


def test_ingest_repo_failure_leaves_graph_intact_and_logs(tp, monkeypatch, tmp_path: Path):
    from tpk import ingest as ingest_mod
    from tpk.ingest import ingest_repo, upsert_graph

    client, prefix = tp
    upsert_graph(client, prefix, [_node("n1", "alpha")], [], datetime.now(timezone.utc))

    def boom(repo_path, out_dir):
        raise RuntimeError("graphify exploded")

    monkeypatch.setattr(ingest_mod, "run_graphify", boom)
    cfg = RepoConfig(name="tinyrepo", path=tmp_path, visibility="internal")
    result = ingest_repo(client, cfg, prefix=prefix, out_root=tmp_path / "out")
    assert result.status == "failed"
    assert _count(client, prefix, "kg_nodes", "tinyrepo") == 1  # prior graph intact
    logs = client.query(
        f"SELECT status FROM table({prefix}kg_ingest_log) WHERE repo = 'tinyrepo'"
    ).result_rows
    assert ("failed",) in logs
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_ingest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpk.ingest'`.

- [ ] **Step 3: Implement `src/tpk/ingest.py`**

```python
"""Per-repo ingest: graphify -> parse -> upsert -> stale-delete -> log."""

import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tpk.config import RepoConfig
from tpk.graphify_runner import parse_graph_json, run_graphify
from tpk.model import EDGE_COLUMNS, NODE_COLUMNS, Edge, Node, edge_rows, node_rows


@dataclass(frozen=True)
class IngestResult:
    repo: str
    run_id: str
    nodes: int
    edges: int
    status: str  # "ok" | "failed"


def upsert_graph(
    client, prefix: str, nodes: list[Node], edges: list[Edge], run_started_at: datetime
) -> None:
    if nodes:
        client.insert(f"{prefix}kg_nodes", node_rows(nodes, run_started_at), column_names=NODE_COLUMNS)
    if edges:
        client.insert(f"{prefix}kg_edges", edge_rows(edges, run_started_at), column_names=EDGE_COLUMNS)
    repos = {n.repo for n in nodes} | {e.repo for e in edges}
    for repo in repos:
        for stream in ("kg_nodes", "kg_edges"):
            client.command(
                f"DELETE FROM {prefix}{stream} WHERE repo = %(r)s AND updated_at < %(t)s",
                parameters={"r": repo, "t": run_started_at},
            )


def _git_sha(repo_path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _log(client, prefix: str, result: IngestResult, git_sha: str) -> None:
    client.insert(
        f"{prefix}kg_ingest_log",
        [[result.repo, result.run_id, result.nodes, result.edges, git_sha, result.status]],
        column_names=["repo", "run_id", "nodes", "edges", "git_sha", "status"],
    )


def ingest_repo(
    client,
    repo_cfg: RepoConfig,
    prefix: str = "",
    out_root: Path = Path(".graphify_out"),
) -> IngestResult:
    run_id = uuid.uuid4().hex[:12]
    run_started_at = datetime.now(timezone.utc)
    try:
        graph_json = run_graphify(repo_cfg.path, out_root / repo_cfg.name)
        nodes, edges = parse_graph_json(graph_json, repo_cfg.name, repo_cfg.visibility)
        upsert_graph(client, prefix, nodes, edges, run_started_at)
        result = IngestResult(repo_cfg.name, run_id, len(nodes), len(edges), "ok")
    except Exception as exc:  # per-repo isolation: never propagate, never touch prior rows
        print(f"[tpk] ingest failed for {repo_cfg.name}: {exc}")
        result = IngestResult(repo_cfg.name, run_id, 0, 0, "failed")
    _log(client, prefix, result, _git_sha(repo_cfg.path))
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_ingest.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Implement the CLI**

`src/tpk/cli.py`:

```python
"""tpk command-line interface."""

from pathlib import Path

import typer

from tpk import db
from tpk.config import Settings, load_repos
from tpk.ingest import ingest_repo

app = typer.Typer(help="Timeplus knowledge graph toolkit")

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"


@app.command()
def ingest(
    repo: str = typer.Option(None, help="Repo name from repos.toml; omit for all"),
    repos_file: Path = typer.Option(REPOS_TOML, help="Path to repos.toml"),
):
    """Run graphify on repo checkouts and upsert the graph into Timeplus."""
    settings = Settings.from_env()
    client = db.get_client(settings)
    db.ensure_schema(client)
    repos = load_repos(repos_file)
    targets = [repos[repo]] if repo else list(repos.values())
    if repo and repo not in repos:
        raise typer.BadParameter(f"unknown repo {repo!r}; known: {sorted(repos)}")
    for cfg in targets:
        result = ingest_repo(client, cfg)
        typer.echo(f"{result.repo}: {result.status} ({result.nodes} nodes, {result.edges} edges)")


@app.command()
def status():
    """Show the latest ingest run per repo."""
    settings = Settings.from_env()
    client = db.get_client(settings)
    rows = client.query(
        "SELECT repo, max(_tp_time) AS last_run, arg_max(status, _tp_time) AS status,"
        " arg_max(nodes, _tp_time) AS nodes, arg_max(edges, _tp_time) AS edges"
        " FROM table(kg_ingest_log) GROUP BY repo ORDER BY repo"
    ).result_rows
    for repo, last_run, status_, nodes, edges in rows:
        typer.echo(f"{repo:35s} {status_:7s} {nodes:>8} nodes {edges:>8} edges  {last_run}")


if __name__ == "__main__":
    app()
```

Add to `pyproject.toml`:

```toml
[project.scripts]
tpk = "tpk.cli:app"
```

- [ ] **Step 6: Verify the CLI wires up**

Run: `uv sync && uv run tpk --help`
Expected: help text listing `ingest` and `status`. Then `TIMEPLUS_HOST=localhost uv run tpk status` — prints nothing (empty log) or prior runs, exits 0.

- [ ] **Step 7: Commit**

```bash
git add src/tpk/ingest.py src/tpk/cli.py tests/test_ingest.py pyproject.toml uv.lock
git commit -m "feat: per-repo ingest pipeline with stale-delete, audit log, and tpk CLI"
```

---

### Task 6: Tools package — `KnowledgeGraph`

**Files:**
- Create: `src/tpk/tools.py`, `tests/test_tools.py`

**Interfaces:**
- Consumes: `Settings`, `load_repos` (Task 1); `db.get_client` (Task 2); seeded rows via `ingest.upsert_graph` (Task 5, in tests).
- Produces — `tpk.tools.KnowledgeGraph`, constructed as `KnowledgeGraph(client, stream_prefix="", repo_paths=None)` where `repo_paths: dict[str, Path]` maps repo name → checkout path. Class constants `MAX_DEPTH = 3`, `MAX_NODES_PER_HOP = 200`. Methods (all reads via `table(...)`; all return plain dicts/lists so MCP and LangGraph can serialize them directly):
  - `search_entities(query: str, kinds: list[str] | None = None, repos: list[str] | None = None, limit: int = 20) -> list[dict]` — every whitespace-separated token must case-insensitively match name, qualified_name, or summary; exact name matches rank first.
  - `get_entity(entity_id: str) -> dict | None`
  - `neighbors(entity_id: str, rels: list[str] | None = None, direction: str = "both", depth: int = 1, confidence: str | None = None) -> dict` — returns `{"nodes": [...], "edges": [...]}`; depth clamped to `MAX_DEPTH`, each hop truncated to `MAX_NODES_PER_HOP`.
  - `path_between(id_a: str, id_b: str, max_depth: int = 4) -> list[dict] | None` — BFS over undirected edges; returns alternating node/edge dicts or `None`.
  - `list_communities(repo: str | None = None) -> list[dict]` — `(repo, community, node_count)` grouped rows, ordered by count desc.
  - `read_source(repo: str, file_path: str, line_start: int, line_end: int) -> str` — reads from `repo_paths[repo]`; raises `ValueError` on unknown repo or on paths escaping the checkout (`..`).

- [ ] **Step 1: Write the failing tests**

`tests/test_tools.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import requires_timeplus
from tpk.model import Edge, Node

pytestmark = requires_timeplus

# Seed graph: a() -> b() -> c(), doc D documents a; a,b in community "core", c,D in "docs"
SEED_NODES = [
    Node(id="a1", repo="r1", kind="function", name="alpha", qualified_name="m.alpha",
         file_path="m.py", line_start=1, line_end=3, summary="checkpoint flush entry",
         community="core", visibility="internal"),
    Node(id="b1", repo="r1", kind="function", name="beta", qualified_name="m.beta",
         file_path="m.py", line_start=5, line_end=8, summary="writes checkpoint state",
         community="core", visibility="internal"),
    Node(id="c1", repo="r1", kind="function", name="gamma", qualified_name="m.gamma",
         file_path="m.py", line_start=10, line_end=12, summary="fsync helper",
         community="docs", visibility="internal"),
    Node(id="d1", repo="docs", kind="doc_section", name="Checkpointing",
         qualified_name="ops/ckpt.md#Checkpointing", file_path="ops/ckpt.md",
         line_start=1, line_end=30, summary="how checkpoints work",
         community="docs", visibility="public"),
]
SEED_EDGES = [
    Edge(src="a1", dst="b1", rel="calls", confidence="EXTRACTED", repo="r1"),
    Edge(src="b1", dst="c1", rel="calls", confidence="INFERRED", repo="r1"),
    Edge(src="d1", dst="a1", rel="documents", confidence="EXTRACTED", repo="docs"),
]


@pytest.fixture()
def kg(tp, tmp_path: Path):
    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph

    client, prefix = tp
    upsert_graph(client, prefix, SEED_NODES, SEED_EDGES, datetime.now(timezone.utc))
    (tmp_path / "m.py").write_text("line1\nline2\nline3\nline4\nline5\n")
    return KnowledgeGraph(client, stream_prefix=prefix, repo_paths={"r1": tmp_path})


def test_search_all_tokens_must_match(kg):
    hits = kg.search_entities("checkpoint flush")
    assert [h["id"] for h in hits] == ["a1"]  # only alpha matches both tokens


def test_search_filters_by_kind_and_repo(kg):
    assert {h["id"] for h in kg.search_entities("checkpoint", kinds=["doc_section"])} == {"d1"}
    assert {h["id"] for h in kg.search_entities("checkpoint", repos=["docs"])} == {"d1"}


def test_get_entity(kg):
    assert kg.get_entity("a1")["name"] == "alpha"
    assert kg.get_entity("nope") is None


def test_neighbors_depth_and_confidence(kg):
    one_hop = kg.neighbors("a1", depth=1)
    assert {n["id"] for n in one_hop["nodes"]} == {"a1", "b1", "d1"}
    two_hop = kg.neighbors("a1", depth=2)
    assert "c1" in {n["id"] for n in two_hop["nodes"]}
    extracted_only = kg.neighbors("a1", depth=2, confidence="EXTRACTED")
    assert "c1" not in {n["id"] for n in extracted_only["nodes"]}  # b->c is INFERRED


def test_neighbors_depth_is_capped(kg):
    from tpk.tools import KnowledgeGraph

    result = kg.neighbors("a1", depth=99)
    assert result["depth_used"] == KnowledgeGraph.MAX_DEPTH


def test_path_between(kg):
    path = kg.path_between("d1", "c1")
    ids = [p["id"] for p in path if "id" in p]
    assert ids[0] == "d1" and ids[-1] == "c1"
    assert kg.path_between("a1", "missing") is None


def test_list_communities(kg):
    rows = kg.list_communities(repo="r1")
    by_name = {r["community"]: r["node_count"] for r in rows}
    assert by_name == {"core": 2, "docs": 1}


def test_read_source(kg):
    text = kg.read_source("r1", "m.py", 2, 3)
    assert text == "line2\nline3\n"
    with pytest.raises(ValueError):
        kg.read_source("unknown", "m.py", 1, 2)
    with pytest.raises(ValueError):
        kg.read_source("r1", "../etc/passwd", 1, 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_tools.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpk.tools'`.

- [ ] **Step 3: Implement `src/tpk/tools.py`**

```python
"""KnowledgeGraph: the single read path over the kg_* streams.

Consumed by the MCP server (this plan) and the LangGraph agent (follow-up plan).
"""

from collections import deque
from pathlib import Path

NODE_FIELDS = [
    "id", "repo", "kind", "name", "qualified_name", "file_path",
    "line_start", "line_end", "summary", "community", "visibility",
]
EDGE_FIELDS = ["src", "dst", "rel", "confidence", "repo"]


class KnowledgeGraph:
    MAX_DEPTH = 3
    MAX_NODES_PER_HOP = 200

    def __init__(self, client, stream_prefix: str = "", repo_paths: dict[str, Path] | None = None):
        self.client = client
        self.prefix = stream_prefix
        self.repo_paths = repo_paths or {}

    # -- internals ---------------------------------------------------------

    def _nodes_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        rows = self.client.query(
            f"SELECT {', '.join(NODE_FIELDS)} FROM table({self.prefix}kg_nodes)"
            " WHERE id IN %(ids)s",
            parameters={"ids": ids},
        ).result_rows
        return [dict(zip(NODE_FIELDS, r)) for r in rows]

    def _edges_touching(self, ids: list[str], rels, direction: str, confidence) -> list[dict]:
        clauses, params = [], {"ids": ids}
        if direction == "out":
            clauses.append("src IN %(ids)s")
        elif direction == "in":
            clauses.append("dst IN %(ids)s")
        else:
            clauses.append("(src IN %(ids)s OR dst IN %(ids)s)")
        if rels:
            clauses.append("rel IN %(rels)s")
            params["rels"] = rels
        if confidence:
            clauses.append("confidence = %(conf)s")
            params["conf"] = confidence
        rows = self.client.query(
            f"SELECT {', '.join(EDGE_FIELDS)} FROM table({self.prefix}kg_edges)"
            f" WHERE {' AND '.join(clauses)}",
            parameters=params,
        ).result_rows
        return [dict(zip(EDGE_FIELDS, r)) for r in rows]

    # -- public tools ------------------------------------------------------

    def search_entities(self, query, kinds=None, repos=None, limit=20):
        tokens = [t for t in query.split() if t]
        if not tokens:
            return []
        clauses, params = [], {"q": query.lower(), "limit": limit}
        for i, tok in enumerate(tokens):
            params[f"t{i}"] = f"%{tok.lower()}%"
            clauses.append(
                f"(lower(name) LIKE %(t{i})s OR lower(qualified_name) LIKE %(t{i})s"
                f" OR lower(summary) LIKE %(t{i})s)"
            )
        if kinds:
            clauses.append("kind IN %(kinds)s")
            params["kinds"] = kinds
        if repos:
            clauses.append("repo IN %(repos)s")
            params["repos"] = repos
        rows = self.client.query(
            f"SELECT {', '.join(NODE_FIELDS)} FROM table({self.prefix}kg_nodes)"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY (lower(name) = %(q)s) DESC, length(name) ASC"
            " LIMIT %(limit)s",
            parameters=params,
        ).result_rows
        return [dict(zip(NODE_FIELDS, r)) for r in rows]

    def get_entity(self, entity_id: str):
        hits = self._nodes_by_ids([entity_id])
        return hits[0] if hits else None

    def neighbors(self, entity_id, rels=None, direction="both", depth=1, confidence=None):
        depth_used = min(max(depth, 1), self.MAX_DEPTH)
        seen = {entity_id}
        frontier = [entity_id]
        all_edges: dict[tuple, dict] = {}
        for _ in range(depth_used):
            if not frontier:
                break
            edges = self._edges_touching(frontier, rels, direction, confidence)
            next_frontier = set()
            for e in edges:
                all_edges[(e["src"], e["dst"], e["rel"])] = e
                for endpoint in (e["src"], e["dst"]):
                    if endpoint not in seen:
                        next_frontier.add(endpoint)
            frontier = sorted(next_frontier)[: self.MAX_NODES_PER_HOP]
            seen.update(frontier)
        return {
            "nodes": self._nodes_by_ids(sorted(seen)),
            "edges": list(all_edges.values()),
            "depth_used": depth_used,
        }

    def path_between(self, id_a, id_b, max_depth: int = 4):
        if id_a == id_b:
            return self._nodes_by_ids([id_a])
        parents: dict[str, tuple[str, dict]] = {}
        seen = {id_a}
        queue = deque([(id_a, 0)])
        while queue:
            current, dist = queue.popleft()
            if dist >= max_depth:
                continue
            for e in self._edges_touching([current], None, "both", None):
                for nxt in (e["src"], e["dst"]):
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    parents[nxt] = (current, e)
                    if nxt == id_b:
                        return self._materialize_path(id_a, id_b, parents)
                    queue.append((nxt, dist + 1))
        return None

    def _materialize_path(self, id_a, id_b, parents):
        hops = []
        node = id_b
        while node != id_a:
            prev, edge = parents[node]
            hops.append((node, edge))
            node = prev
        hops.reverse()
        node_ids = [id_a] + [n for n, _ in hops]
        node_map = {n["id"]: n for n in self._nodes_by_ids(node_ids)}
        path: list[dict] = [node_map[id_a]]
        for nid, edge in hops:
            path.append(edge)
            path.append(node_map[nid])
        return path

    def list_communities(self, repo=None):
        clause, params = "", {}
        if repo:
            clause = " WHERE repo = %(repo)s"
            params["repo"] = repo
        rows = self.client.query(
            f"SELECT repo, community, count() AS node_count"
            f" FROM table({self.prefix}kg_nodes){clause}"
            " GROUP BY repo, community ORDER BY node_count DESC",
            parameters=params,
        ).result_rows
        return [dict(zip(["repo", "community", "node_count"], r)) for r in rows]

    def read_source(self, repo, file_path, line_start, line_end):
        root = self.repo_paths.get(repo)
        if root is None:
            raise ValueError(f"unknown repo {repo!r}; known: {sorted(self.repo_paths)}")
        target = (Path(root) / file_path).resolve()
        if not target.is_relative_to(Path(root).resolve()):
            raise ValueError(f"path {file_path!r} escapes repo checkout")
        lines = target.read_text(errors="replace").splitlines(keepends=True)
        start = max(line_start, 1)
        return "".join(lines[start - 1 : line_end])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_tools.py -v`
Expected: 8 PASS. If the driver rejects `IN %(ids)s` list binding, check timeplus-connect's parameter docs (it follows clickhouse-connect: sequences client-side-render into `IN` tuples) before changing approach.

- [ ] **Step 5: Run the whole suite**

Run: `TIMEPLUS_HOST=localhost uv run pytest -v`
Expected: all tests pass, none unexpectedly skipped.

- [ ] **Step 6: Commit**

```bash
git add src/tpk/tools.py tests/test_tools.py
git commit -m "feat: KnowledgeGraph tools - search, neighbors, path, communities, read_source"
```

---

### Task 7: MCP server

**Files:**
- Create: `src/tpk/mcp_server.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `KnowledgeGraph` (Task 6); `Settings`, `load_repos` (Task 1); `db.get_client` (Task 2).
- Produces:
  - `tpk.mcp_server.build_server(kg: KnowledgeGraph) -> FastMCP` — registers exactly six tools named `search_entities`, `get_entity`, `neighbors`, `path_between`, `list_communities`, `read_source`, each a thin passthrough to the same-named `KnowledgeGraph` method.
  - `tpk.mcp_server.main()` — builds a production `KnowledgeGraph` from env + `repos.toml` and serves over stdio; module runnable as `python -m tpk.mcp_server`.

- [ ] **Step 1: Write the failing test**

`tests/test_mcp_server.py`:

```python
import asyncio

from tpk.mcp_server import build_server

EXPECTED_TOOLS = {
    "search_entities", "get_entity", "neighbors",
    "path_between", "list_communities", "read_source",
}


class FakeKG:
    def search_entities(self, query, kinds=None, repos=None, limit=20):
        return [{"id": "x", "name": query}]


def test_all_six_tools_registered():
    server = build_server(FakeKG())
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_tool_calls_delegate_to_kg():
    server = build_server(FakeKG())
    result = asyncio.run(server.call_tool("search_entities", {"query": "checkpoint"}))
    assert "checkpoint" in str(result)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tpk.mcp_server'`.

- [ ] **Step 3: Implement `src/tpk/mcp_server.py`**

```python
"""MCP server exposing the Timeplus knowledge graph to Claude Code / Cursor."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from tpk import db
from tpk.config import Settings, load_repos
from tpk.tools import KnowledgeGraph

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"


def build_server(kg) -> FastMCP:
    server = FastMCP("timeplus-knowledge")

    @server.tool()
    def search_entities(query: str, kinds: list[str] | None = None,
                        repos: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Find code/doc entities in the Timeplus knowledge graph by keyword.
        Every whitespace-separated token must match the entity's name,
        qualified name, or summary (case-insensitive)."""
        return kg.search_entities(query, kinds=kinds, repos=repos, limit=limit)

    @server.tool()
    def get_entity(entity_id: str) -> dict | None:
        """Fetch the full record for one entity by its id."""
        return kg.get_entity(entity_id)

    @server.tool()
    def neighbors(entity_id: str, rels: list[str] | None = None, direction: str = "both",
                  depth: int = 1, confidence: str | None = None) -> dict:
        """Local subgraph around an entity (BFS, depth capped at 3).
        direction: out|in|both. confidence: EXTRACTED|INFERRED to filter edges."""
        return kg.neighbors(entity_id, rels=rels, direction=direction,
                            depth=depth, confidence=confidence)

    @server.tool()
    def path_between(id_a: str, id_b: str, max_depth: int = 4) -> list[dict] | None:
        """Shortest connection between two entities, or null if none within max_depth."""
        return kg.path_between(id_a, id_b, max_depth=max_depth)

    @server.tool()
    def list_communities(repo: str | None = None) -> list[dict]:
        """Cluster overview: (repo, community, node_count), largest first."""
        return kg.list_communities(repo=repo)

    @server.tool()
    def read_source(repo: str, file_path: str, line_start: int, line_end: int) -> str:
        """Read exact lines from a repo checkout so answers can quote real code."""
        return kg.read_source(repo, file_path, line_start, line_end)

    return server


def main() -> None:
    settings = Settings.from_env()
    client = db.get_client(settings)
    repos = load_repos(REPOS_TOML)
    kg = KnowledgeGraph(client, repo_paths={name: cfg.path for name, cfg in repos.items()})
    build_server(kg).run()  # stdio transport


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: 2 PASS. If `server.call_tool` isn't a public method on the installed `mcp` version, use `mcp.shared.memory.create_connected_server_and_client_session` to call the tool through a real in-memory session instead — the assertion stays the same.

- [ ] **Step 5: Commit**

```bash
git add src/tpk/mcp_server.py tests/test_mcp_server.py
git commit -m "feat: MCP server exposing the six knowledge-graph tools over stdio"
```

---

### Task 8: M1 smoke run + README (first useful deliverable)

**Files:**
- Create: `README.md`
- No code changes — this task runs the real pipeline on the two smallest repos (`docs`, `timeplus-cli`) and documents usage.

**Interfaces:**
- Consumes: everything above.
- Produces: a populated production graph for 2 repos; a README covering setup, ingest, MCP registration.

- [ ] **Step 1: Run the real ingest on the two small repos**

```bash
export TIMEPLUS_HOST=localhost  # adjust if remote
uv run tpk ingest --repo docs
uv run tpk ingest --repo timeplus-cli
uv run tpk status
```

Expected: both repos report `ok` with nonzero node/edge counts. If graphify fails on either repo, capture stderr, fix the runner (this is exactly the per-repo isolation path), and re-run.

- [ ] **Step 2: Verify the graph answers a hand query**

```bash
echo "SELECT kind, count() FROM table(kg_nodes) GROUP BY kind ORDER BY count() DESC" | \
  curl "http://${TIMEPLUS_HOST}:8123/" -u "${TIMEPLUS_USER:-default}:${TIMEPLUS_PASSWORD:-}" --data-binary @-
```

Expected: a kind histogram with plausible values (doc sections/files for docs; functions for timeplus-cli). Then spot-check search via Python:

```bash
uv run python -c "
from tpk.config import Settings, load_repos
from tpk import db
from tpk.tools import KnowledgeGraph
from pathlib import Path
repos = load_repos(Path('repos.toml'))
kg = KnowledgeGraph(db.get_client(Settings.from_env()),
                    repo_paths={n: c.path for n, c in repos.items()})
for hit in kg.search_entities('install', limit=5):
    print(hit['repo'], hit['kind'], hit['name'], hit['file_path'])
"
```

Expected: ≥ 1 sensible hit from the docs repo.

- [ ] **Step 3: Write `README.md`**

```markdown
# timeplus-knowledge

Knowledge graph + agent tooling for answering questions about Timeplus —
code, design, architecture, and devops. See
`docs/superpowers/specs/2026-08-10-timeplus-knowledge-agent-design.md`.

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a running
Timeplus Enterprise (mutable streams are an Enterprise feature):

    docker run -d --name timeplusd -p 8123:8123 -p 3218:3218 \
      docker.timeplus.com/timeplus/timeplusd:latest
    uv sync
    export TIMEPLUS_HOST=localhost TIMEPLUS_USER=default TIMEPLUS_PASSWORD=

## Ingest

Corpus lives in `repos.toml` (repo name → local checkout path → visibility).

    uv run tpk ingest                # all repos
    uv run tpk ingest --repo docs    # one repo
    uv run tpk status                # last run per repo

Ingest is per-repo isolated: a failing repo logs `failed` in
`kg_ingest_log` and leaves its previous graph untouched.

## Use from Claude Code (MCP)

    claude mcp add timeplus-knowledge -- uv --directory /Users/gangtao/Code/timeplus/timeplus-knowledge run python -m tpk.mcp_server

Tools: `search_entities`, `get_entity`, `neighbors`, `path_between`,
`list_communities`, `read_source`.

## Tests

    uv run pytest                    # integration tests skip without TIMEPLUS_HOST
    TIMEPLUS_HOST=localhost uv run pytest
```

- [ ] **Step 4: Register the MCP server with Claude Code and smoke it**

```bash
claude mcp add timeplus-knowledge -- uv --directory /Users/gangtao/Code/timeplus/timeplus-knowledge run python -m tpk.mcp_server
```

Then in a new Claude Code session ask: *"Using the timeplus-knowledge tools, what doc sections exist about installation?"* — confirm the model calls `search_entities` and gets rows back.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: setup, ingest, and MCP usage; M1+M2 smoke-verified"
```

---

## After this plan

- **M3 (full corpus):** run `uv run tpk ingest` across all 9 repos; watch proton's runtime/size, tune graphify flags if needed. Operational, no new code expected.
- **M4 (agent + web UI):** separate plan — LangGraph agent over `KnowledgeGraph`, FastAPI SSE `/chat`, React UI in Timeplus console style, citation-required answering.
