# Corpus Management (issue #4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Manage the knowledge corpus at runtime — versioned entries `(name, ref)` in a `kg_repos` store with an enabled flag that gates searchability, a management API with background ingest jobs, and a Manage page in the web UI — per the approved spec in GitHub issue #4 and its design comments.

**Architecture:** A new mutable stream `kg_repos` is the corpus source of truth (`repos.toml` becomes the seed). Graph rows are keyed by the entry key `name@ref` in the existing `repo` column — no node/edge schema change; two releases of one repo coexist as distinct entries. `KnowledgeGraph` filters every query path by the enabled entry set (fetched live, short TTL) and resolves `read_source` paths dynamically, so UI toggles apply without restart. `tpk serve` gains `/api/*` management endpoints backed by a single-worker background job queue for ingest; the React app gains a Manage tab.

**Tech Stack:** existing stack (Python 3.11/uv, FastAPI, timeplus-connect, React+Vite). No new dependencies.

**Spec:** https://github.com/timeplus-io/timeplus-knowledge/issues/4 (body + all five design comments are binding).

## Global Constraints

- Corpus unit is `(name, ref)`. Entry key = `f"{name}@{ref}"` when `ref` is non-empty, else `name` (local-path entries). Repo names must not contain `@` (validated).
- `kg_nodes`/`kg_edges` schema is UNCHANGED. The entry key is stored in the existing `repo` column; node ids already hash it.
- `enabled` lives ONLY in `kg_repos`; toggling is one row flip. Filtering is query-side: `AND repo IN (<enabled entry keys>)` in `search_entities`, `list_communities`, `_nodes_by_ids`, `_edges_touching`. **Empty/absent `kg_repos` ⇒ no filtering** (back-compat for tests/standalone).
- Disabled entries: rows retained, not searchable, instant re-enable. Delete is explicit with `purge` flag (purges `kg_nodes`/`kg_edges` rows for the entry key). `kg_ingest_log` is append-only history — never purged.
- Enabled-set and path resolution are fetched live with a short cache (5s TTL) — UI changes take effect without restarting `tpk serve` or the MCP server.
- Legacy migration: pre-existing rows use bare-name keys; after this change, a successful ingest of an entry also stale-deletes rows under the bare `name` key.
- Management endpoints: when env `TPK_ADMIN_TOKEN` is set, every `/api/*` request must carry header `X-Admin-Token` matching it (401 otherwise); unset ⇒ open (v1 parity).
- Ingest jobs run in-process on a single background worker thread with its own DB client; jobs never block `/chat`.
- UI: Timeplus Console tokens (existing `app.css` variables); destructive actions use `--red-500`, confirmations required for delete.
- Conventional commits; full suite green (`TIMEPLUS_HOST=localhost uv run pytest -q`) before each commit; `npm run build` and `npm run check:sanitize` green for UI changes.
- Finishing: push the feature branch and open a PR (`gh pr create`, body `Closes #4`) — do NOT merge to main; the user reviews the PR.

## File Structure

```
src/tpk/config.py       # + RepoConfig.enabled, entry_key(), validation: name has no '@'
src/tpk/db.py           # + kg_repos stream in ensure/drop
src/tpk/corpus.py       # NEW: seed/list/upsert/toggle/delete/enabled_keys/entry paths
src/tpk/tools.py        # KnowledgeGraph: enabled-filter + dynamic read_source paths
src/tpk/ingest.py       # entry-key writes + legacy bare-name cleanup
src/tpk/cli.py          # ingest reads corpus from store (seeded from repos.toml)
src/tpk/agent.py        # system_prompt from entries (@ref labels); live-prompt provider
src/tpk/api.py          # NEW: /api router + JobManager + admin gate
src/tpk/server.py       # include api router; corpus-aware agent build
src/tpk/mcp_server.py   # corpus-aware repo paths
web/src/App.tsx         # tab nav Chat | Manage
web/src/Manage.tsx      # NEW: repo table, add form, toggle/reindex/delete, job status
web/src/app.css         # manage styles (tokens)
web/vite.config.ts      # proxy /api
docker-compose.yml      # agent service: GITHUB_TOKEN, TPK_ADMIN_TOKEN
.env.example, README.md
tests/test_corpus.py, tests/test_api.py + extensions of existing test files
```

---

### Task 1: `kg_repos` store + corpus module

**Files:**
- Modify: `src/tpk/config.py` (add `enabled` field, `entry_key`, name validation), `src/tpk/db.py` (schema)
- Create: `src/tpk/corpus.py`
- Test: `tests/test_corpus.py`, extend `tests/test_config.py`

**Interfaces:**
- Consumes: `db.ensure_schema/drop_schema`, `RepoConfig`, `resolved_repo_path`, `tp` conftest fixture.
- Produces:
  - `tpk.config.RepoConfig` gains `enabled: bool = True`. `tpk.config.entry_key(cfg: RepoConfig) -> str` = `f"{cfg.name}@{cfg.ref}"` if `cfg.ref` else `cfg.name`. `load_repos` raises `ValueError` if a repo name contains `@`.
  - `{prefix}kg_repos` mutable stream: `name string, ref string, github string, path string, enabled bool, visibility string, extraction string, description string, updated_at datetime64(3,'UTC')` PRIMARY KEY `(name, ref)`; created in `ensure_schema`, dropped in `drop_schema`.
  - `tpk.corpus` (every function takes `client` first, `prefix: str = ""` keyword):
    - `upsert_entry(client, cfg: RepoConfig, prefix="") -> None`
    - `list_entries(client, prefix="") -> list[RepoConfig]` (sorted by name, ref)
    - `find_entry(client, name, ref, prefix="") -> RepoConfig | None`
    - `set_enabled(client, name, ref, enabled, prefix="") -> bool` (False when entry missing)
    - `delete_entry(client, name, ref, prefix="", purge=False) -> bool` (purge deletes `kg_nodes`/`kg_edges` rows for the entry key; never touches `kg_ingest_log`)
    - `enabled_keys(client, prefix="") -> list[str]`
    - `seed_from_toml(client, toml_path: Path, prefix="") -> int` (inserts entries missing from the store; never overwrites existing rows; returns count added)
    - `entry_paths(entries: list[RepoConfig]) -> dict[str, Path]` (entry key → `resolved_repo_path`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_entry_key_and_enabled_default(tmp_path: Path):
    from tpk.config import entry_key

    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.docs]\ngithub = "timeplus-io/docs"\nref = "main"\nvisibility = "public"\n\n'
        '[repos.local]\npath = "/x"\nvisibility = "internal"\n'
    )
    repos = load_repos(toml_path)
    assert repos["docs"].enabled is True
    assert entry_key(repos["docs"]) == "docs@main"
    assert entry_key(repos["local"]) == "local"


def test_load_repos_rejects_at_sign_in_name(tmp_path: Path):
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[repos."bad@name"]\npath = "/x"\nvisibility = "internal"\n')
    with pytest.raises(ValueError, match="@"):
        load_repos(toml_path)
```

Create `tests/test_corpus.py`:

```python
from datetime import datetime, timezone
from pathlib import Path

from conftest import requires_timeplus
from tpk import corpus
from tpk.config import RepoConfig, entry_key
from tpk.model import Node

pytestmark = requires_timeplus


def _entry(name="repoa", ref="v1.0.0", enabled=True, **over):
    base = dict(
        name=name, github=f"org/{name}", ref=ref, visibility="internal",
        extraction="code-only", description=f"{name} test entry", enabled=enabled,
    )
    base.update(over)
    return RepoConfig(**base)


def test_upsert_list_find_roundtrip(tp):
    client, prefix = tp
    corpus.upsert_entry(client, _entry(), prefix=prefix)
    corpus.upsert_entry(client, _entry(ref="v2.0.0", enabled=False), prefix=prefix)
    entries = corpus.list_entries(client, prefix=prefix)
    assert [(e.name, e.ref, e.enabled) for e in entries] == [
        ("repoa", "v1.0.0", True), ("repoa", "v2.0.0", False),
    ]
    found = corpus.find_entry(client, "repoa", "v2.0.0", prefix=prefix)
    assert found is not None and found.github == "org/repoa"
    assert corpus.find_entry(client, "repoa", "v9", prefix=prefix) is None


def test_enabled_keys_and_toggle(tp):
    client, prefix = tp
    corpus.upsert_entry(client, _entry(), prefix=prefix)
    corpus.upsert_entry(client, _entry(ref="v2.0.0", enabled=False), prefix=prefix)
    assert corpus.enabled_keys(client, prefix=prefix) == ["repoa@v1.0.0"]
    assert corpus.set_enabled(client, "repoa", "v2.0.0", True, prefix=prefix)
    assert sorted(corpus.enabled_keys(client, prefix=prefix)) == [
        "repoa@v1.0.0", "repoa@v2.0.0",
    ]
    assert not corpus.set_enabled(client, "ghost", "v1", True, prefix=prefix)


def test_delete_entry_with_and_without_purge(tp):
    from tpk.ingest import upsert_graph

    client, prefix = tp
    cfg = _entry()
    corpus.upsert_entry(client, cfg, prefix=prefix)
    node = Node(
        id="n1", repo=entry_key(cfg), kind="function", name="f",
        qualified_name="m.f", file_path="m.py", line_start=1, line_end=2,
        summary="", community="", visibility="internal",
    )
    upsert_graph(client, prefix, [node], [], datetime.now(timezone.utc))

    assert corpus.delete_entry(client, "repoa", "v1.0.0", prefix=prefix, purge=False)
    rows = client.query(
        f"SELECT count() FROM table({prefix}kg_nodes) WHERE repo = %(r)s",
        parameters={"r": entry_key(cfg)},
    ).result_rows
    assert rows[0][0] == 1  # rows retained without purge

    corpus.upsert_entry(client, cfg, prefix=prefix)
    assert corpus.delete_entry(client, "repoa", "v1.0.0", prefix=prefix, purge=True)
    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        n = client.query(
            f"SELECT count() FROM table({prefix}kg_nodes) WHERE repo = %(r)s",
            parameters={"r": entry_key(cfg)},
        ).result_rows[0][0]
        if n == 0:
            break
        time.sleep(0.2)
    assert n == 0  # purged


def test_seed_from_toml_is_idempotent_and_preserves_edits(tp, tmp_path: Path):
    client, prefix = tp
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.docs]\ngithub = "timeplus-io/docs"\nref = "main"\nvisibility = "public"\n'
    )
    assert corpus.seed_from_toml(client, toml_path, prefix=prefix) == 1
    assert corpus.seed_from_toml(client, toml_path, prefix=prefix) == 0  # idempotent
    corpus.set_enabled(client, "docs", "main", False, prefix=prefix)
    corpus.seed_from_toml(client, toml_path, prefix=prefix)  # must NOT re-enable
    assert corpus.enabled_keys(client, prefix=prefix) == []


def test_entry_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TPK_CHECKOUT_DIR", str(tmp_path))
    paths = corpus.entry_paths([_entry(), _entry(name="loc", github="", ref="", path="/l")])
    assert paths["repoa@v1.0.0"] == tmp_path / "repoa" / "v1.0.0"
    assert paths["loc"] == Path("/l")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_corpus.py tests/test_config.py -q`
Expected: FAIL (`No module named 'tpk.corpus'`, `entry_key` import error).

- [ ] **Step 3: Implement**

`src/tpk/config.py` — add to `RepoConfig`: `enabled: bool = True` (last field). Add after `repo_paths`:

```python
def entry_key(cfg: RepoConfig) -> str:
    """Graph identity of a corpus entry: name@ref, or bare name for
    local-path entries (which have no ref)."""
    return f"{cfg.name}@{cfg.ref}" if cfg.ref else cfg.name
```

In `load_repos`, first line of the loop body:

```python
        if "@" in name:
            raise ValueError(f"repo name {name!r} must not contain '@' (reserved for entry keys)")
```

`src/tpk/db.py` — in `ensure_schema`, after the `kg_ingest_log` block:

```python
    client.command(f"""
        CREATE MUTABLE STREAM IF NOT EXISTS {prefix}kg_repos (
          name string,
          ref string,
          github string,
          path string,
          enabled bool,
          visibility string,
          extraction string,
          description string,
          updated_at datetime64(3, 'UTC')
        ) PRIMARY KEY (name, ref)
    """)
```

and in `drop_schema` extend the tuple to `("kg_nodes", "kg_edges", "kg_ingest_log", "kg_repos")`.

Create `src/tpk/corpus.py`:

```python
"""The corpus store: kg_repos is the source of truth for what the graph
indexes. repos.toml seeds it; the management API and CLI mutate it."""

from datetime import datetime, timezone
from pathlib import Path

from tpk.config import RepoConfig, entry_key, load_repos, resolved_repo_path

_COLUMNS = [
    "name", "ref", "github", "path", "enabled", "visibility",
    "extraction", "description", "updated_at",
]


def _row(cfg: RepoConfig) -> list:
    return [
        cfg.name, cfg.ref, cfg.github, str(cfg.path) if cfg.path else "",
        cfg.enabled, cfg.visibility, cfg.extraction, cfg.description,
        datetime.now(timezone.utc),
    ]


def _to_cfg(row) -> RepoConfig:
    name, ref, github, path, enabled, visibility, extraction, description, _ = row
    return RepoConfig(
        name=name, ref=ref, github=github, path=Path(path) if path else None,
        enabled=bool(enabled), visibility=visibility, extraction=extraction,
        description=description,
    )


def upsert_entry(client, cfg: RepoConfig, prefix: str = "") -> None:
    client.insert(f"{prefix}kg_repos", [_row(cfg)], column_names=_COLUMNS)


def list_entries(client, prefix: str = "") -> list[RepoConfig]:
    rows = client.query(
        f"SELECT {', '.join(_COLUMNS)} FROM table({prefix}kg_repos)"
        " ORDER BY name, ref"
    ).result_rows
    return [_to_cfg(r) for r in rows]


def find_entry(client, name: str, ref: str, prefix: str = "") -> RepoConfig | None:
    rows = client.query(
        f"SELECT {', '.join(_COLUMNS)} FROM table({prefix}kg_repos)"
        " WHERE name = %(n)s AND ref = %(r)s",
        parameters={"n": name, "r": ref},
    ).result_rows
    return _to_cfg(rows[0]) if rows else None


def set_enabled(client, name: str, ref: str, enabled: bool, prefix: str = "") -> bool:
    cfg = find_entry(client, name, ref, prefix=prefix)
    if cfg is None:
        return False
    from dataclasses import replace

    upsert_entry(client, replace(cfg, enabled=enabled), prefix=prefix)
    return True


def delete_entry(
    client, name: str, ref: str, prefix: str = "", purge: bool = False
) -> bool:
    cfg = find_entry(client, name, ref, prefix=prefix)
    if cfg is None:
        return False
    client.command(
        f"DELETE FROM {prefix}kg_repos WHERE name = %(n)s AND ref = %(r)s",
        parameters={"n": name, "r": ref},
    )
    if purge:
        key = entry_key(cfg)
        for stream in ("kg_nodes", "kg_edges"):
            client.command(
                f"DELETE FROM {prefix}{stream} WHERE repo = %(k)s",
                parameters={"k": key},
            )
    return True


def enabled_keys(client, prefix: str = "") -> list[str]:
    rows = client.query(
        f"SELECT name, ref FROM table({prefix}kg_repos) WHERE enabled"
        " ORDER BY name, ref"
    ).result_rows
    return [f"{n}@{r}" if r else n for n, r in rows]


def seed_from_toml(client, toml_path: Path, prefix: str = "") -> int:
    existing = {(e.name, e.ref) for e in list_entries(client, prefix=prefix)}
    added = 0
    for cfg in load_repos(toml_path).values():
        if (cfg.name, cfg.ref) not in existing:
            upsert_entry(client, cfg, prefix=prefix)
            added += 1
    return added


def entry_paths(entries: list[RepoConfig]) -> dict[str, Path]:
    return {entry_key(e): resolved_repo_path(e) for e in entries}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_corpus.py tests/test_config.py -q`
Expected: all PASS. Note the mutable-stream visibility lag (~100-300ms): if `list_entries` right after `upsert_entry` flakes, reuse the bounded `_eventually`-style poll from `tests/test_ingest.py` inside the affected test — never `sleep` blindly.

- [ ] **Step 5: Full suite, commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/config.py src/tpk/db.py src/tpk/corpus.py tests/test_corpus.py tests/test_config.py
git commit -m "feat: kg_repos corpus store with versioned entries and enabled flag"
```

---

### Task 2: KnowledgeGraph enabled-filter + dynamic paths

**Files:**
- Modify: `src/tpk/tools.py`, `src/tpk/mcp_server.py`
- Test: extend `tests/test_tools.py`

**Interfaces:**
- Consumes: `corpus.enabled_keys`, `corpus.list_entries`, `corpus.entry_paths` (Task 1).
- Produces: `KnowledgeGraph.__init__(client, stream_prefix="", repo_paths=None, corpus_ttl=5.0)`. Behavior contract:
  - A cached `_corpus_state()` (TTL `corpus_ttl` seconds) reads `kg_repos`; on ANY exception or when the store has no rows it yields `(None, {})` = **no filtering, static repo_paths only**.
  - When entries exist: every row-returning query path (`search_entities`, `list_communities`, `_nodes_by_ids`, `_edges_touching`) adds `AND repo IN %(active_repos)s` with the enabled entry keys; `read_source` resolves paths from `entry_paths(entries)` first, then the static `repo_paths` dict.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools.py`:

```python
def _seed_corpus_entry(client, prefix, name, ref, enabled):
    from tpk import corpus
    from tpk.config import RepoConfig

    corpus.upsert_entry(
        client,
        RepoConfig(name=name, github=f"org/{name}", ref=ref, visibility="internal",
                   enabled=enabled),
        prefix=prefix,
    )


def test_disabled_entry_invisible_everywhere(tp, tmp_path):
    from datetime import datetime, timezone

    from tpk.ingest import upsert_graph
    from tpk.tools import KnowledgeGraph

    client, prefix = tp
    # two versions of one repo in the graph
    n1 = SEED_NODES[0].__class__(**{**SEED_NODES[0].__dict__, "id": "v1n", "repo": "r@v1"})
    n2 = SEED_NODES[0].__class__(**{**SEED_NODES[0].__dict__, "id": "v2n", "repo": "r@v2"})
    upsert_graph(client, prefix, [n1, n2], [], datetime.now(timezone.utc))
    _seed_corpus_entry(client, prefix, "r", "v1", enabled=True)
    _seed_corpus_entry(client, prefix, "r", "v2", enabled=False)

    kg = KnowledgeGraph(client, stream_prefix=prefix, corpus_ttl=0)
    _eventually(lambda: len(kg.search_entities("checkpoint")), lambda v: v >= 1)
    hits = kg.search_entities("checkpoint")
    assert {h["repo"] for h in hits} == {"r@v1"}          # search filtered
    assert kg.get_entity("v2n") is None                    # id lookup filtered
    assert kg.get_entity("v1n") is not None

    from tpk import corpus
    corpus.set_enabled(client, "r", "v2", True, prefix=prefix)
    _eventually(lambda: kg.get_entity("v2n"), lambda v: v is not None)  # live toggle


def test_empty_corpus_store_means_no_filter(kg):
    # the kg fixture seeds nodes but never touches kg_repos -> unfiltered
    assert kg.search_entities("checkpoint")
```

(`SEED_NODES[0].__dict__` works because `Node` is a plain frozen dataclass; build the copies with `dataclasses.replace` if the reviewer prefers: `replace(SEED_NODES[0], id="v1n", repo="r@v1")` — use `dataclasses.replace`, it is cleaner. Import it at the top of the test.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_tools.py -q`
Expected: new tests FAIL (`corpus_ttl` unexpected kwarg / unfiltered results).

- [ ] **Step 3: Implement in `src/tpk/tools.py`**

Add to `__init__`:

```python
    def __init__(self, client, stream_prefix: str = "",
                 repo_paths: dict[str, Path] | None = None,
                 corpus_ttl: float = 5.0):
        self.client = client
        self.prefix = stream_prefix
        self.repo_paths = repo_paths or {}
        self._lock = threading.Lock()
        self._corpus_ttl = corpus_ttl
        self._corpus_cached_at = 0.0
        self._corpus_keys: list[str] | None = None
        self._corpus_paths: dict[str, Path] = {}
```

Add the cached state reader (import `time` and, inside the method, `tpk.corpus` lazily to avoid an import cycle):

```python
    def _corpus_state(self) -> tuple[list[str] | None, dict[str, Path]]:
        """Enabled entry keys + their paths, cached for corpus_ttl seconds.
        (None, {}) means the store is absent/empty -> no filtering."""
        now = time.monotonic()
        if now - self._corpus_cached_at < self._corpus_ttl and self._corpus_cached_at:
            return self._corpus_keys, self._corpus_paths
        keys: list[str] | None = None
        paths: dict[str, Path] = {}
        try:
            from tpk import corpus

            with self._lock:
                entries = corpus.list_entries(self.client, prefix=self.prefix)
            if entries:
                keys = [k for k in corpus.enabled_keys_from(entries)]
                paths = corpus.entry_paths([e for e in entries if e.enabled])
        except Exception:
            keys, paths = None, {}
        self._corpus_keys, self._corpus_paths = keys, paths
        self._corpus_cached_at = now
        return keys, paths
```

Add the tiny helper to `corpus.py` (part of this task):

```python
def enabled_keys_from(entries: list[RepoConfig]) -> list[str]:
    return [entry_key(e) for e in entries if e.enabled]
```

Wire the filter into the four query paths. Pattern (apply the same two lines in `_nodes_by_ids`, `_edges_touching`, `search_entities`, `list_communities`):

```python
        active, _ = self._corpus_state()
        if active is not None:
            <where-clauses>.append("repo IN %(active_repos)s")
            params["active_repos"] = active or ["__none__"]
```

Notes: in `_nodes_by_ids` and `_edges_touching` the existing WHERE is built inline — convert to a small clause list first. `active == []` (store non-empty but everything disabled) must return nothing — hence the `["__none__"]` sentinel. In `read_source`, resolve the root as:

```python
        _, corpus_paths = self._corpus_state()
        root = corpus_paths.get(repo) or self.repo_paths.get(repo)
```

In `src/tpk/mcp_server.py` `main()`: after building the client, add `db.ensure_schema(client)` then `corpus.seed_from_toml(client, REPOS_TOML)` (import `corpus`, `db`) so a standalone MCP server sees the store; keep passing the static `repo_paths` as fallback.

- [ ] **Step 4: Run tests to verify they pass**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_tools.py tests/test_tools_concurrency.py -q`
Expected: PASS, including all pre-existing tests (empty store ⇒ unfiltered) and the concurrency tests (note `_corpus_state` takes the lock only around the client call).

- [ ] **Step 5: Full suite, commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/tools.py src/tpk/corpus.py src/tpk/mcp_server.py tests/test_tools.py
git commit -m "feat: enabled-entry filtering and dynamic paths in KnowledgeGraph"
```

---

### Task 3: Ingest writes entry keys; CLI reads the corpus store

**Files:**
- Modify: `src/tpk/ingest.py`, `src/tpk/cli.py`
- Test: extend `tests/test_ingest.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `entry_key` (Task 1), `corpus.seed_from_toml`/`list_entries` (Task 1).
- Produces: `ingest_repo` (signature unchanged) writes graph rows under `entry_key(repo_cfg)`; its stale-delete covers `{entry_key, bare name}` (legacy migration); `.graphify_out` subdir and `kg_ingest_log.repo` also use the entry key. `tpk ingest [--repo TARGET]` seeds the store from `repos.toml`, then ingests entries from the STORE (all enabled+disabled? No: ingest ALL entries whose name or entry key matches TARGET, else every entry — disabled entries may still be (re)indexed; only search visibility is gated by `enabled`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ingest.py`:

```python
def test_ingest_writes_entry_key_and_cleans_legacy_rows(tp, monkeypatch, tmp_path: Path):
    from datetime import datetime, timezone

    from tpk import ingest as ingest_mod
    from tpk.config import RepoConfig, entry_key
    from tpk.ingest import ingest_repo, upsert_graph
    from tpk.model import Node

    client, prefix = tp
    # legacy row under the bare name
    legacy = Node(id="old1", repo="vrepo", kind="function", name="f",
                  qualified_name="m.f", file_path="m.py", line_start=1, line_end=2,
                  summary="", community="", visibility="internal")
    upsert_graph(client, prefix, [legacy], [], datetime.now(timezone.utc))

    def fake_run_graphify(repo_path, out_dir, **kw):
        assert "vrepo@v1.0.0" in str(out_dir)
        return Path("unused")

    def fake_parse(graph_json, repo, default_visibility):
        assert repo == "vrepo@v1.0.0"
        n = Node(id="new1", repo=repo, kind="function", name="g",
                 qualified_name="m.g", file_path="m.py", line_start=1, line_end=2,
                 summary="", community="", visibility=default_visibility)
        return [n], []

    monkeypatch.setattr(ingest_mod, "run_graphify", fake_run_graphify)
    monkeypatch.setattr(ingest_mod, "parse_graph_json", fake_parse)
    cfg = RepoConfig(name="vrepo", github="org/vrepo", ref="v1.0.0", visibility="internal")
    result = ingest_repo(client, cfg, prefix=prefix, out_root=tmp_path)
    assert result.status == "ok" and result.repo == "vrepo@v1.0.0"

    def counts():
        rows = client.query(
            f"SELECT repo, count() FROM table({prefix}kg_nodes)"
            " WHERE repo IN ('vrepo', 'vrepo@v1.0.0') GROUP BY repo"
        ).result_rows
        return dict(rows)

    _eventually(lambda: counts(), lambda c: c.get("vrepo@v1.0.0") == 1 and "vrepo" not in c)
```

Append to `tests/test_cli.py`:

```python
def test_ingest_reads_corpus_store_and_matches_name_or_key(tp, monkeypatch, tmp_path):
    import typer
    from typer.testing import CliRunner

    from tpk import cli as cli_mod
    from tpk import corpus
    from tpk.config import RepoConfig

    client, prefix = tp
    # the CLI must consult the store: seed one entry directly (not in the toml)
    corpus.upsert_entry(
        client, RepoConfig(name="storeonly", github="o/s", ref="v1", visibility="internal"),
        prefix=prefix,
    )
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text("[repos]\n")  # empty seed

    calls = []
    monkeypatch.setattr(cli_mod, "ingest_repo", lambda c, cfg, **kw: calls.append(cfg.name) or
                        cli_mod.IngestResult(cfg.name, "x", 0, 0, "ok"))
    monkeypatch.setattr(cli_mod.db, "get_client", lambda s: client)
    monkeypatch.setattr(cli_mod.db, "ensure_schema", lambda c, p="": None)
    monkeypatch.setenv("TPK_STREAM_PREFIX", prefix)

    runner = CliRunner()
    res = runner.invoke(cli_mod.app, ["ingest", "--repos-file", str(toml_path),
                                      "--repo", "storeonly@v1"])
    assert res.exit_code == 0, res.output
    assert calls == ["storeonly"]
```

(If wiring the test prefix through the CLI is awkward, add an env override `TPK_STREAM_PREFIX` read in the `ingest` command — default `""` — and set it via `monkeypatch.setenv`; that is the sanctioned mechanism, keep it undocumented-internal.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_ingest.py tests/test_cli.py -q`
Expected: new tests FAIL.

- [ ] **Step 3: Implement**

`src/tpk/ingest.py` — inside `ingest_repo`: compute `key = entry_key(repo_cfg)` (import from config). Use `key` for: `out_root / key` (graphify out dir), `parse_graph_json(graph_json, key, ...)`, `upsert_graph(..., repos={key, repo_cfg.name})` (set literal gives legacy cleanup when they differ), `IngestResult(key, ...)` for both ok and failed paths.

`src/tpk/cli.py` — `ingest` command body becomes:

```python
    settings = Settings.from_env()
    client = db.get_client(settings)
    prefix = os.environ.get("TPK_STREAM_PREFIX", "")
    db.ensure_schema(client, prefix)
    corpus.seed_from_toml(client, repos_file, prefix=prefix)
    llm = load_llm(repos_file)
    backend = None if llm.backend == "auto" else llm.backend
    model = llm.model or None
    entries = corpus.list_entries(client, prefix=prefix)
    if repo:
        entries = [e for e in entries if e.name == repo or entry_key(e) == repo]
        if not entries:
            raise typer.BadParameter(f"no corpus entry matches {repo!r}")
    for i, cfg in enumerate(entries, 1):
        typer.echo(f"[{i}/{len(entries)}] {entry_key(cfg)}: extracting ({cfg.extraction})...")
        result = ingest_repo(client, cfg, prefix=prefix, backend=backend, model=model,
                             token_budget=llm.token_budget, stream=verbose)
        typer.echo(f"{result.repo}: {result.status} ({result.nodes} nodes, {result.edges} edges)")
```

(imports: `os`, `from tpk import corpus`, `from tpk.config import ..., entry_key`; keep `IngestResult` import for the test monkeypatch.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_ingest.py tests/test_cli.py -q`
Expected: PASS (pre-existing ingest tests keep passing — path-mode `RepoConfig` without ref has `entry_key == name`, so their behavior is unchanged).

- [ ] **Step 5: Full suite, commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/ingest.py src/tpk/cli.py tests/test_ingest.py tests/test_cli.py
git commit -m "feat: ingest under versioned entry keys with legacy cleanup; CLI reads corpus store"
```

---

### Task 4: Agent prompt from live corpus entries

**Files:**
- Modify: `src/tpk/agent.py`, `src/tpk/server.py` (agent build only)
- Test: extend `tests/test_agent.py`

**Interfaces:**
- Consumes: `corpus.list_entries`, `entry_key`.
- Produces: `system_prompt(repos)` accepts a dict (legacy) OR an iterable of `RepoConfig`; entries with a ref are listed as `name@ref (visibility): description`. `build_agent(kg, cfg, repos, model=None, corpus_provider=None)`: when `corpus_provider` (a `Callable[[], list[RepoConfig]]`) is given, the langgraph prompt is a callable that renders `system_prompt(corpus_provider())` per invocation (so UI corpus edits reach the prompt without restart); otherwise static as today.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent.py`:

```python
def test_system_prompt_labels_versioned_entries():
    from tpk.config import RepoConfig

    entries = [RepoConfig(name="proton-enterprise", github="o/pe", ref="v3.3.1",
                          visibility="internal", description="Enterprise engine")]
    p = system_prompt(entries)
    assert "proton-enterprise@v3.3.1" in p
    assert "Enterprise engine" in p


def test_build_agent_with_corpus_provider_renders_live_prompt():
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from tpk.config import RepoConfig

    fake = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    seen = []

    def provider():
        seen.append(1)
        return [RepoConfig(name="live", github="o/l", ref="v9", visibility="public",
                           description="live entry")]

    agent = build_agent(
        FakeKG(), AgentConfig(provider="openai", model="x"), {},
        model=fake, corpus_provider=provider,
    )
    result = agent.invoke({"messages": [("user", "hi")]})
    assert result["messages"][-1].content == "ok"
    assert seen  # provider consulted during invocation
```

(Reuse the existing `bind_tools` monkeypatch pattern from this file if `GenericFakeChatModel` needs it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent.py -q` — FAIL (`system_prompt` chokes on a list / unexpected kwarg).

- [ ] **Step 3: Implement in `src/tpk/agent.py`**

```python
def system_prompt(repos) -> str:
    entries = list(repos.values()) if isinstance(repos, dict) else list(repos)
    from tpk.config import entry_key

    corpus = "\n".join(
        f"- {entry_key(r)} ({r.visibility}): {r.description or 'no description'}"
        for r in entries
    )
    ...  # rest of the existing f-string body unchanged
```

(`entry_key` returns the bare name for path entries, so existing dict-based callers/tests keep their labels.)

```python
def build_agent(kg, cfg, repos, model=None, corpus_provider=None):
    chat_model = model if model is not None else build_chat_model(cfg)
    tools = build_agent_tools(kg)
    if corpus_provider is None:
        return create_react_agent(chat_model, tools, prompt=system_prompt(repos))

    def _live_prompt(state):
        return [{"role": "system", "content": system_prompt(corpus_provider())}] + list(
            state["messages"]
        )

    return create_react_agent(chat_model, tools, prompt=_live_prompt)
```

`src/tpk/server.py` `_build_production_agent`: after building the client, run `db.ensure_schema(client)` and `corpus.seed_from_toml(client, REPOS_TOML)`; construct `KnowledgeGraph(client, repo_paths=resolved_repo_paths(repos))` as today, and pass `corpus_provider=lambda: [e for e in corpus.list_entries(client) if e.enabled]` to `build_agent`. (The lambda runs inside the async worker thread pool per turn; the KnowledgeGraph lock does not apply — corpus reads happen through the same client, so wrap the lambda body with the kg lock? No: use a SEPARATE client for the provider to avoid concurrent-session errors: build `provider_client = db.get_client(Settings.from_env())` alongside and close over it.)

- [ ] **Step 4: Run tests, full suite, commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/agent.py src/tpk/server.py tests/test_agent.py
git commit -m "feat: version-labeled, live corpus prompt for the agent"
```

---

### Task 5: Management API + background ingest jobs + admin gate

**Files:**
- Create: `src/tpk/api.py`
- Modify: `src/tpk/server.py` (mount router, thread prefix)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `corpus.*` (Task 1), `ingest_repo` (Task 3), `load_llm`, `Settings`, `db`.
- Produces: `tpk.api.create_api_router(prefix: str = "") -> fastapi.APIRouter` and `tpk.api.JobManager`. `create_app(agent=None, stream_prefix="")` mounts the router at `/api`. Endpoints (all JSON; admin gate applies to ALL of them):
  - `GET /api/repos` → `[{name, ref, entry_key, github, path, enabled, visibility, extraction, description, node_count, last_status, last_sha, last_time}]`
  - `POST /api/repos` body `{name, github="", ref="", path="", visibility="internal", extraction="code-only", description="", enabled=true, ingest=true}` → 422 on validation error (mirrors `load_repos` rules incl. no `@` in name), else `{entry_key, job_id|null}`
  - `POST /api/repos/toggle` `{name, ref, enabled}` → `{ok: true}` or 404
  - `POST /api/repos/delete` `{name, ref, purge=false}` → `{ok: true}` or 404
  - `POST /api/repos/reindex` `{name, ref}` → `{job_id}` or 404
  - `GET /api/jobs` → most recent 50, newest first; `GET /api/jobs/{job_id}` → record or 404
  - Job record: `{id, entry_key, status: "queued"|"running"|"ok"|"failed", nodes, edges, error, submitted_at, finished_at}`
  - Admin gate: if env `TPK_ADMIN_TOKEN` is set, requests without header `X-Admin-Token: <token>` get 401.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api.py`:

```python
import time

from fastapi.testclient import TestClient

from conftest import requires_timeplus
from tpk.server import create_app

pytestmark = requires_timeplus


class _NoAgent:
    async def astream_events(self, *a, **k):
        yield  # pragma: no cover


def _client(tp, monkeypatch):
    client, prefix = tp
    monkeypatch.delenv("TPK_ADMIN_TOKEN", raising=False)
    import tpk.api as api_mod

    # jobs must not run real graphify: stub ingest to a fast success
    from tpk.ingest import IngestResult

    def fake_ingest(client_, cfg, prefix="", backend=None, model=None,
                    token_budget=0, stream=False, out_root=None, **kw):
        from tpk.config import entry_key
        return IngestResult(entry_key(cfg), "j", 3, 2, "ok")

    monkeypatch.setattr(api_mod, "ingest_repo", fake_ingest)
    return TestClient(create_app(agent=_NoAgent(), stream_prefix=prefix)), prefix


def test_add_list_toggle_delete_flow(tp, monkeypatch):
    c, prefix = _client(tp, monkeypatch)
    r = c.post("/api/repos", json={"name": "demo", "github": "o/demo", "ref": "v1",
                                   "visibility": "internal", "ingest": False})
    assert r.status_code == 200 and r.json()["entry_key"] == "demo@v1"

    rows = c.get("/api/repos").json()
    assert [x["entry_key"] for x in rows] == ["demo@v1"]
    assert rows[0]["enabled"] is True

    assert c.post("/api/repos/toggle",
                  json={"name": "demo", "ref": "v1", "enabled": False}).json()["ok"]
    assert c.get("/api/repos").json()[0]["enabled"] is False

    assert c.post("/api/repos/delete", json={"name": "demo", "ref": "v1"}).json()["ok"]
    assert c.get("/api/repos").json() == []
    assert c.post("/api/repos/toggle",
                  json={"name": "demo", "ref": "v1", "enabled": True}).status_code == 404


def test_add_validation(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    assert c.post("/api/repos", json={"name": "x"}).status_code == 422           # no source
    assert c.post("/api/repos", json={"name": "x", "github": "o/x"}).status_code == 422  # no ref
    assert c.post("/api/repos", json={"name": "a@b", "path": "/p"}).status_code == 422   # @ in name


def test_reindex_job_runs_to_ok(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    c.post("/api/repos", json={"name": "demo", "github": "o/demo", "ref": "v1",
                               "ingest": False})
    job_id = c.post("/api/repos/reindex", json={"name": "demo", "ref": "v1"}).json()["job_id"]
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = c.get(f"/api/jobs/{job_id}").json()["status"]
        if status in ("ok", "failed"):
            break
        time.sleep(0.1)
    assert status == "ok"
    jobs = c.get("/api/jobs").json()
    assert jobs[0]["entry_key"] == "demo@v1" and jobs[0]["nodes"] == 3


def test_admin_token_gate(tp, monkeypatch):
    c, _ = _client(tp, monkeypatch)
    monkeypatch.setenv("TPK_ADMIN_TOKEN", "sekret")
    assert c.get("/api/repos").status_code == 401
    assert c.get("/api/repos", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert c.get("/api/repos", headers={"X-Admin-Token": "sekret"}).status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_api.py -q` — FAIL (`create_app` has no `stream_prefix`, no `/api`).

- [ ] **Step 3: Implement `src/tpk/api.py`**

```python
"""Management API: corpus CRUD + background ingest jobs."""

import os
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from tpk import corpus, db
from tpk.config import RepoConfig, Settings, entry_key, load_llm
from tpk.ingest import ingest_repo

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"


def _require_admin(x_admin_token: str | None) -> None:
    expected = os.environ.get("TPK_ADMIN_TOKEN")
    if expected and x_admin_token != expected:
        raise HTTPException(status_code=401, detail="missing or invalid X-Admin-Token")


class AddRepo(BaseModel):
    name: str
    github: str = ""
    ref: str = ""
    path: str = ""
    visibility: str = "internal"
    extraction: str = "code-only"
    description: str = ""
    enabled: bool = True
    ingest: bool = True


class EntryRef(BaseModel):
    name: str
    ref: str = ""


class ToggleRepo(EntryRef):
    enabled: bool


class DeleteRepo(EntryRef):
    purge: bool = False


def _validate(body: AddRepo) -> RepoConfig:
    if "@" in body.name:
        raise HTTPException(422, "repo name must not contain '@'")
    if bool(body.github) == bool(body.path):
        raise HTTPException(422, "set exactly one of 'github' or 'path'")
    if body.github and not body.ref:
        raise HTTPException(422, "'github' requires a 'ref' (tag/branch)")
    if body.visibility not in ("internal", "public"):
        raise HTTPException(422, "visibility must be internal|public")
    if body.extraction not in ("code-only", "semantic"):
        raise HTTPException(422, "extraction must be code-only|semantic")
    return RepoConfig(
        name=body.name, github=body.github, ref=body.ref,
        path=Path(body.path) if body.path else None,
        visibility=body.visibility, extraction=body.extraction,
        description=body.description, enabled=body.enabled,
    )


class JobManager:
    """One worker thread; jobs use their own DB client (never the KG's)."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.jobs: dict[str, dict] = {}
        self._q: queue.Queue = queue.Queue()
        self._started = False
        self._lock = threading.Lock()

    def _ensure_worker(self):
        with self._lock:
            if not self._started:
                threading.Thread(target=self._run, daemon=True).start()
                self._started = True

    def submit(self, cfg: RepoConfig) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.jobs[job_id] = {
            "id": job_id, "entry_key": entry_key(cfg), "status": "queued",
            "nodes": 0, "edges": 0, "error": None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }
        self._q.put((job_id, cfg))
        self._ensure_worker()
        return job_id

    def _run(self):
        while True:
            job_id, cfg = self._q.get()
            rec = self.jobs[job_id]
            rec["status"] = "running"
            try:
                client = db.get_client(Settings.from_env())
                llm = load_llm(REPOS_TOML) if REPOS_TOML.exists() else None
                backend = None if (llm is None or llm.backend == "auto") else llm.backend
                result = ingest_repo(
                    client, cfg, prefix=self.prefix, backend=backend,
                    model=(llm.model or None) if llm else None,
                    token_budget=llm.token_budget if llm else 0,
                )
                rec["nodes"], rec["edges"] = result.nodes, result.edges
                rec["status"] = result.status  # "ok" | "failed"
                if result.status == "failed":
                    rec["error"] = "ingest failed; see server logs"
            except Exception as exc:
                rec["status"] = "failed"
                rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["finished_at"] = datetime.now(timezone.utc).isoformat()


def create_api_router(prefix: str = "") -> APIRouter:
    router = APIRouter(prefix="/api")
    jobs = JobManager(prefix=prefix)

    def _client():
        return db.get_client(Settings.from_env())

    @router.get("/repos")
    def list_repos(x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        client = _client()
        entries = corpus.list_entries(client, prefix=prefix)
        counts = dict(
            client.query(
                f"SELECT repo, count() FROM table({prefix}kg_nodes) GROUP BY repo"
            ).result_rows
        )
        last: dict[str, tuple] = {}
        for repo, sha, status, t in client.query(
            f"SELECT repo, arg_max(git_sha, _tp_time), arg_max(status, _tp_time),"
            f" max(_tp_time) FROM table({prefix}kg_ingest_log) GROUP BY repo"
        ).result_rows:
            last[repo] = (sha, status, t)
        out = []
        for e in entries:
            key = entry_key(e)
            sha, status, t = last.get(key, (None, None, None))
            out.append({
                "name": e.name, "ref": e.ref, "entry_key": key,
                "github": e.github, "path": str(e.path) if e.path else "",
                "enabled": e.enabled, "visibility": e.visibility,
                "extraction": e.extraction, "description": e.description,
                "node_count": counts.get(key, 0),
                "last_status": status, "last_sha": sha,
                "last_time": str(t) if t else None,
            })
        return out

    @router.post("/repos")
    def add_repo(body: AddRepo, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        cfg = _validate(body)
        corpus.upsert_entry(_client(), cfg, prefix=prefix)
        job_id = jobs.submit(cfg) if body.ingest else None
        return {"entry_key": entry_key(cfg), "job_id": job_id}

    @router.post("/repos/toggle")
    def toggle_repo(body: ToggleRepo, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        if not corpus.set_enabled(_client(), body.name, body.ref, body.enabled, prefix=prefix):
            raise HTTPException(404, "no such corpus entry")
        return {"ok": True}

    @router.post("/repos/delete")
    def delete_repo(body: DeleteRepo, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        if not corpus.delete_entry(_client(), body.name, body.ref, prefix=prefix,
                                   purge=body.purge):
            raise HTTPException(404, "no such corpus entry")
        return {"ok": True}

    @router.post("/repos/reindex")
    def reindex_repo(body: EntryRef, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        cfg = corpus.find_entry(_client(), body.name, body.ref, prefix=prefix)
        if cfg is None:
            raise HTTPException(404, "no such corpus entry")
        return {"job_id": jobs.submit(cfg)}

    @router.get("/jobs")
    def list_jobs(x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        return sorted(jobs.jobs.values(), key=lambda j: j["submitted_at"], reverse=True)[:50]

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, x_admin_token: str | None = Header(None)):
        _require_admin(x_admin_token)
        if job_id not in jobs.jobs:
            raise HTTPException(404, "no such job")
        return jobs.jobs[job_id]

    return router
```

`src/tpk/server.py`: `create_app(agent=None, stream_prefix="")`; after the app is constructed add `app.include_router(create_api_router(prefix=stream_prefix))` (import from `tpk.api`) BEFORE the static mount (StaticFiles at `/` must be mounted last so `/api` wins).

- [ ] **Step 4: Run tests to verify they pass**

Run: `TIMEPLUS_HOST=localhost uv run pytest tests/test_api.py tests/test_server.py -q`
Expected: PASS (server tests unaffected — `stream_prefix` defaults to `""`).

- [ ] **Step 5: Full suite, commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add src/tpk/api.py src/tpk/server.py tests/test_api.py
git commit -m "feat: corpus management API with background ingest jobs and admin gate"
```

---

### Task 6: Manage UI

**Files:**
- Create: `web/src/Manage.tsx`
- Modify: `web/src/App.tsx` (tab nav), `web/src/app.css`, `web/vite.config.ts` (proxy `/api`)

**Interfaces:**
- Consumes: the exact `/api` shapes from Task 5.
- Produces: a `Manage` tab rendering the repo table with enable/disable, reindex (job status inline), delete (confirm + purge checkbox), and an add form. Timeplus tokens; destructive = `--red-500`.

- [ ] **Step 1: vite proxy + tab nav**

`web/vite.config.ts` — add `"/api": "http://localhost:8000",` to the proxy map.

`web/src/App.tsx` — add at top: `import Manage from "./Manage";`, add state `const [view, setView] = useState<"chat" | "manage">("chat");`, and change the header to:

```tsx
      <header>
        <div className="header-row">
          <div>
            <h1>Timeplus Knowledge</h1>
            <p>Ask anything about Timeplus — code, architecture, deployment.</p>
          </div>
          <nav className="tabs">
            <button className={view === "chat" ? "tab active" : "tab"}
                    onClick={() => setView("chat")}>Chat</button>
            <button className={view === "manage" ? "tab active" : "tab"}
                    onClick={() => setView("manage")}>Manage</button>
          </nav>
        </div>
      </header>
```

and wrap the existing `<main>` + `<footer>` in `{view === "chat" ? (<>…existing…</>) : <Manage />}`.

- [ ] **Step 2: Create `web/src/Manage.tsx`**

```tsx
import { useEffect, useState } from "react";

type Repo = {
  name: string; ref: string; entry_key: string; github: string; path: string;
  enabled: boolean; visibility: string; extraction: string; description: string;
  node_count: number; last_status: string | null; last_sha: string | null;
  last_time: string | null;
};
type Job = { id: string; entry_key: string; status: string; nodes: number;
  edges: number; error: string | null };

const EMPTY_FORM = { name: "", github: "", ref: "", path: "", visibility: "internal",
  extraction: "code-only", description: "" };

async function api(path: string, body?: unknown) {
  const resp = await fetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
  return resp.json();
}

export default function Manage() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [purge, setPurge] = useState(false);

  async function refresh() {
    try {
      setRepos(await api("/api/repos"));
      setJobs(await api("/api/jobs"));
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const activeJob = (key: string) =>
    jobs.find((j) => j.entry_key === key && (j.status === "queued" || j.status === "running"));

  async function act(fn: () => Promise<unknown>) {
    try { await fn(); await refresh(); } catch (e) { setError(String(e)); }
  }

  return (
    <main className="manage">
      {error && <div className="manage-error">{error}</div>}
      <table className="repo-table">
        <thead>
          <tr><th>Entry</th><th>Source</th><th>Mode</th><th>Nodes</th>
              <th>Last ingest</th><th>Enabled</th><th>Actions</th></tr>
        </thead>
        <tbody>
          {repos.map((r) => (
            <tr key={r.entry_key} className={r.enabled ? "" : "disabled-row"}>
              <td><strong>{r.entry_key}</strong><div className="muted">{r.description}</div></td>
              <td>{r.github ? `${r.github}@${r.ref}` : r.path}</td>
              <td>{r.extraction}<div className="muted">{r.visibility}</div></td>
              <td>{r.node_count}</td>
              <td>{activeJob(r.entry_key)
                ? <span className="running">{activeJob(r.entry_key)!.status}…</span>
                : <>{r.last_status ?? "never"}<div className="muted">{r.last_sha?.slice(0, 12)}</div></>}
              </td>
              <td>
                <button className="secondary"
                        onClick={() => {
                          // nudge: one enabled ref per repo (issue #4)
                          const sibling = !r.enabled && repos.find(
                            (o) => o.name === r.name && o.ref !== r.ref && o.enabled);
                          if (sibling && !window.confirm(
                            `${sibling.entry_key} is already enabled. Enable ${r.entry_key} too? ` +
                            "(Both versions will appear in answers.)")) return;
                          act(() => api("/api/repos/toggle",
                            { name: r.name, ref: r.ref, enabled: !r.enabled }));
                        }}>
                  {r.enabled ? "Disable" : "Enable"}
                </button>
              </td>
              <td className="actions">
                <button className="secondary" disabled={!!activeJob(r.entry_key)}
                        onClick={() => act(() => api("/api/repos/reindex",
                          { name: r.name, ref: r.ref }))}>Reindex</button>
                {confirming === r.entry_key ? (
                  <span className="confirm">
                    <label><input type="checkbox" checked={purge}
                                  onChange={(e) => setPurge(e.target.checked)} />
                      also delete indexed data</label>
                    <button className="danger"
                            onClick={() => { setConfirming(null);
                              act(() => api("/api/repos/delete",
                                { name: r.name, ref: r.ref, purge })); }}>Confirm</button>
                    <button className="secondary"
                            onClick={() => setConfirming(null)}>Cancel</button>
                  </span>
                ) : (
                  <button className="danger"
                          onClick={() => { setConfirming(r.entry_key); setPurge(false); }}>
                    Delete</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>Add corpus entry</h2>
      <form className="add-form" onSubmit={(e) => { e.preventDefault();
        act(async () => { await api("/api/repos", { ...form, ingest: true });
          setForm({ ...EMPTY_FORM }); }); }}>
        <input placeholder="name" required value={form.name}
               onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input placeholder="github org/repo (or leave empty for local path)"
               value={form.github}
               onChange={(e) => setForm({ ...form, github: e.target.value })} />
        <input placeholder="ref (tag/branch, required with github)" value={form.ref}
               onChange={(e) => setForm({ ...form, ref: e.target.value })} />
        <input placeholder="local path (dev mode)" value={form.path}
               onChange={(e) => setForm({ ...form, path: e.target.value })} />
        <select value={form.visibility}
                onChange={(e) => setForm({ ...form, visibility: e.target.value })}>
          <option value="internal">internal</option>
          <option value="public">public</option>
        </select>
        <select value={form.extraction}
                onChange={(e) => setForm({ ...form, extraction: e.target.value })}>
          <option value="code-only">code-only</option>
          <option value="semantic">semantic</option>
        </select>
        <input placeholder="description" value={form.description}
               onChange={(e) => setForm({ ...form, description: e.target.value })} />
        <button type="submit">Add &amp; index</button>
      </form>
    </main>
  );
}
```

- [ ] **Step 3: Styles (append to `web/src/app.css`)**

```css
.header-row { display: flex; justify-content: space-between; align-items: center; }
.tabs { display: flex; gap: 8px; }
.tab {
  background: var(--white); color: var(--gray-200);
  border: 1px solid var(--gray-600); height: 32px; padding: 0 16px;
}
.tab.active { background: var(--pink-500); color: var(--white); border-color: var(--pink-500); }
.manage { flex: 1; overflow-y: auto; padding: 16px 0; }
.manage-error { color: var(--red-500); font-size: 12px; margin-bottom: 8px; }
.repo-table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--white); }
.repo-table th, .repo-table td {
  border: 1px solid var(--gray-700); padding: 8px 10px; text-align: left; vertical-align: top;
}
.repo-table th { font-weight: 600; background: var(--gray-900); }
.disabled-row td { color: var(--gray-500); }
.muted { color: var(--gray-500); font-size: 12px; }
.running { color: var(--pink-400); font-weight: 600; }
.actions { white-space: nowrap; }
.actions button { margin-right: 6px; }
button.secondary {
  background: var(--white); color: var(--gray-200); border: 1px solid var(--gray-600);
}
button.secondary:hover:not(:disabled) { background: var(--gray-900); }
button.danger { background: var(--red-500); }
button.danger:hover:not(:disabled) { background: #751025; }
.confirm { display: inline-flex; gap: 8px; align-items: center; font-size: 12px; }
.add-form { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 8px; }
.add-form input, .add-form select {
  height: 40px; background: var(--white); color: var(--gray-100);
  border: 1px solid var(--gray-600); border-radius: var(--radius);
  padding: 0 12px; font: 400 14px/1.4 Inter, sans-serif;
}
.add-form input:focus, .add-form select:focus {
  outline: 2px solid var(--pink-500); outline-offset: 1px;
}
h2 { font-size: 14px; font-weight: 600; margin-top: 24px; color: var(--gray-200); }
```

- [ ] **Step 4: Build + checks**

```bash
cd web && npm run build && npm run check:sanitize && cd ..
```

Expected: strict tsc + vite clean; sanitize 8/8.

- [ ] **Step 5: Full pytest suite (unchanged python), commit**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q
git add web
git commit -m "feat: Manage tab - corpus table, add/toggle/reindex/delete with jobs"
```

---

### Task 7: Compose/env/docs + live E2E + PR

**Files:**
- Modify: `docker-compose.yml` (agent service: `GITHUB_TOKEN`, `TPK_ADMIN_TOKEN`), `.env.example`, `README.md`
- No new tests; this is the live gate.

- [ ] **Step 1: Config plumbing**

`docker-compose.yml` `agent` service `environment`: add `GITHUB_TOKEN: ${GITHUB_TOKEN:-}` (jobs fetch github-sourced repos) and `TPK_ADMIN_TOKEN: ${TPK_ADMIN_TOKEN:-}`. `.env.example`: add a commented `# TPK_ADMIN_TOKEN=change-me — gates /api management endpoints when set`. README: new "Manage the corpus" section — Manage tab, the (name, ref) versioning model (multiple releases side by side, enable/disable gates searchability, delete+purge semantics), the release-upgrade workflow (add new ref → ingest → verify → flip enabled), API endpoints incl. `X-Admin-Token`, and the legacy-key migration note (first ingest after upgrade re-keys entries to `name@ref` and cleans bare-name rows).

- [ ] **Step 2: Rebuild and run the stack from THIS branch**

```bash
docker compose build && docker compose up -d && sleep 10
curl -s http://localhost:8000/healthz
```

- [ ] **Step 3: Live E2E against the real stack**

```bash
# seed happened via server start; list shows the 4 entries with @ref keys
curl -s http://localhost:8000/api/repos | python3 -m json.tool | head -30
# migration: re-ingest one repo, verify bare-name rows are gone afterwards
docker compose exec -T tpk tpk ingest --repo helm-charts
echo "SELECT repo, count() FROM table(kg_nodes) WHERE repo LIKE 'helm-charts%' GROUP BY repo" | \
  curl -s "http://localhost:8123/" -u default: --data-binary @-   # only helm-charts@<ref>
# disable it; the agent must stop seeing it (search via chat or MCP)
curl -s -X POST http://localhost:8000/api/repos/toggle \
  -H 'Content-Type: application/json' -d '{"name":"helm-charts","ref":"timeplus-enterprise-v13.0.6","enabled":false}'
# reindex via job; poll to ok
curl -s -X POST http://localhost:8000/api/repos/reindex \
  -H 'Content-Type: application/json' -d '{"name":"docs","ref":"main"}'
curl -s http://localhost:8000/api/jobs | python3 -m json.tool | head -20
# add a small public repo end-to-end, then delete it with purge
curl -s -X POST http://localhost:8000/api/repos -H 'Content-Type: application/json' \
  -d '{"name":"gluon","github":"timeplus-io/gluon","ref":"main","visibility":"internal","description":"Python SDK"}'
```

Record each result. Judge: entries listed with `@ref` keys; disable removes hits from a `/chat` question that previously cited helm-charts; jobs reach `ok`; added repo becomes searchable; delete with purge removes its rows. Re-enable helm-charts when done.

- [ ] **Step 4: Full suite + UI checks one final time**

```bash
TIMEPLUS_HOST=localhost uv run pytest -q && cd web && npm run build && npm run check:sanitize && cd ..
```

- [ ] **Step 5: Commit docs, push branch, open PR (do NOT merge)**

```bash
git add docker-compose.yml .env.example README.md
git commit -m "docs: corpus management - compose env, README, migration note"
git push -u origin <branch>
gh pr create --repo timeplus-io/timeplus-knowledge --base main \
  --title "feat: corpus management - versioned entries, enable/disable, manage UI" \
  --body "Closes #4 ... (summarize: kg_repos store, name@ref entry keys, query-side enabled filter, management API + jobs + admin gate, Manage tab, migration; include E2E evidence)"
```

---

## After this plan

- Scheduled refresh (`tpk refresh`) — separate issue.
- `tpk status` filtering to current corpus entries — cosmetic backlog item.
