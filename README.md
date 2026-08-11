# timeplus-knowledge

Knowledge graph + agent tooling for answering questions about Timeplus —
code, design, architecture, and devops. See
`docs/superpowers/specs/2026-08-10-timeplus-knowledge-agent-design.md`.

## Setup

`make` lists every dev command (setup, db, tests, ingest, MCP, chat agent /
web UI, docker image). The underlying steps:

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a running
Timeplus Enterprise (mutable streams are an Enterprise feature):

    docker run -d --name timeplusd -p 8123:8123 -p 3218:3218 \
      -v $(pwd)/deploy/timeplusd-dev/small-segments.yaml:/etc/timeplusd-server/config.d/small-segments.yaml:ro \
      docker.timeplus.com/timeplus/timeplusd:latest
    uv sync
    export TIMEPLUS_HOST=localhost TIMEPLUS_USER=default TIMEPLUS_PASSWORD=

The `-v` mount is required on a laptop: the default timeplusd preallocates
2GB of nativelog per stream shard, which fills a Docker VM's disk fast once
you have a handful of streams. `deploy/timeplusd-dev/small-segments.yaml`
overrides this to 64MB segments with no preallocation for local/dev use —
do not use it in production.

## Docker compose (quickest start)

    cp .env.example .env        # add ANTHROPIC_API_KEY / OPENAI_API_KEY there
    docker compose up -d        # or: make up
    docker compose exec tpk tpk ingest
    claude mcp add timeplus-knowledge -- docker compose exec -T tpk tpk-mcp

`.env` (gitignored) carries the LLM keys for semantic extraction and an
optional `REPOS_MOUNT` override; graph data lives in the named volume
`tpk-data` and survives `docker compose down`.

## Docker image (all-in-one)

`deploy/docker/Dockerfile` builds a single image on top of the Timeplus
Enterprise image: timeplusd runs unchanged as the main process, with the
`tpk` CLI, the `graphify` extractor, a Python 3.11 venv, and the
small-segments override baked in. Repo checkouts are mounted at `/repos`
(paths come from the baked-in `deploy/docker/repos.container.toml`).

    docker build -f deploy/docker/Dockerfile -t timeplus/tpk:dev .
    docker run -d --name tpk -p 8123:8123 -p 3218:3218 \
      -v ~/Code/timeplus:/repos:ro timeplus/tpk:dev

    docker exec tpk tpk ingest              # build the graph inside
    docker exec tpk tpk status
    claude mcp add timeplus-knowledge -- docker exec -i tpk tpk-mcp

For semantic-extraction repos, pass the LLM key at run time:
`docker run ... -e ANTHROPIC_API_KEY` (or `-e OPENAI_API_KEY`). The
read-only `/repos` mount is fine — graphify's in-checkout cache writes are
skipped harmlessly.

## Ingest

Corpus lives in `repos.toml`. Each repo declares **one source** — the
default is a GitHub repo pinned to a release tag (`github = "org/repo"` +
`ref = "v3.3.1"`), fetched into the checkout cache (`TPK_CHECKOUT_DIR`,
default `~/.tpk/checkouts`; a shared volume in docker). Private repos need
`GITHUB_TOKEN` in the environment. Alternatively `path = ...` indexes a
local checkout (dev mode). Re-ingesting the same tag is a cheap cache hit;
`tpk status` shows the resolved SHA and ref per repo. Each repo also has a
`visibility` (`internal`/`public`), a short `description`, and an
`extraction` mode:

- `code-only` (default) — local tree-sitter AST parsing. Free, offline, no
  API key; skips non-code files (YAML, Markdown), so all-YAML repos yield
  zero nodes in this mode.
- `semantic` — graphify's LLM extraction also processes YAML/Markdown/docs.
  Requires a usable backend in the environment: `ANTHROPIC_API_KEY` (backend
  `claude`) or `OPENAI_API_KEY` (backend `openai`) — or a custom endpoint via
  `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` for self-hosted servers and
  gateways. The `[llm]` section in `repos.toml` picks the backend (`auto`
  uses whichever is exported) and can pin a model:

      [llm]
      backend = "openai"
      model = "gpt-5.2"     # optional; passed to graphify --model.
                            # Env alternative: OPENAI_MODEL / ANTHROPIC_MODEL.

  **AWS Bedrock:** Bedrock speaks SigV4, not the OpenAI/Anthropic wire
  protocols, so put an OpenAI-compatible gateway in front of it (LiteLLM
  proxy or AWS Bedrock Access Gateway) and set `OPENAI_BASE_URL` to the
  gateway, `OPENAI_API_KEY` to whatever the gateway expects, and
  `OPENAI_MODEL` (or `[llm].model`) to the Bedrock model id. See
  `.env.example`. Ingest fails fast with a clear error if a semantic repo
  has neither a key nor a base URL.

    uv run tpk ingest                # all repos
    uv run tpk ingest --repo docs    # one repo
    uv run tpk status                # last run per repo

Ingest is per-repo isolated: a failing repo logs `failed` in
`kg_ingest_log` and leaves its previous graph untouched.

**Semantic repos need multiple passes for full coverage.** LLM extraction
covers only a subset of files per run (failed chunks are dropped, and the
per-file cache accumulates successes across runs), so re-run
`tpk ingest --repo docs` until the stored node count stabilizes —
observed: 170 -> 217 -> 241 -> 298 covered files over four passes. The
cache lives in the container's `.graphify_out/`; recreating the container
resets it.

Ingest runs `graphify extract` (package `graphifyy` on PyPI, CLI
`graphify`) and reads `<dir>/graphify-out/graph.json`.

### M1 smoke run (2026-08-10)

Ran against real checkouts on this machine:

    docs           ok   163 nodes   151 edges
    timeplus-cli   ok   473 nodes   839 edges

Both reported `ok` via `tpk status`, and `SELECT count() FROM table(kg_nodes)`
matches the parsed counts exactly per repo (163 + 473 = 636 stored rows;
edges likewise 151 + 839 = 990). `kg_nodes` is a mutable stream keyed on an
id derived from `(repo, kind, qualified_name)`; that id is deterministic
*within one parsed graph.json* — `qualified_name` for `file`-kind nodes
(graphify maps every non-callable "code" node to `file`, not just the one
node that is literally the file — see `graphify_runner.py`) is disambiguated
with graphify's own per-node id, which is guaranteed unique within a single
`graph.json`, so distinct entities in the same source file no longer
collapse onto one row within a run. (An earlier ingest run, before this fix,
saw parsed counts of 166/473 collapse to only 91/99 stored rows —
root-caused and fixed; see
`tests/test_graphify_runner.py::test_file_kind_nodes_in_same_file_get_distinct_ids`.)

This does **not** mean identical output run-over-run on an unchanged repo:
graphify keeps its own per-file extraction cache under
`<out-dir>/graphify-out/cache/`, and a warm-cache run against an unchanged
checkout was observed to emit 3 fewer nodes/edges than a cold run (166/154
vs 163/151 for `docs`, git-clean, same commit both times — reproduced
on-demand: a fresh `--out` dir gives 166/154, re-running `graphify extract`
against that same `--out` dir immediately after gives 163/151, consistently
losing the file that graphify's own "had syntax errors, partially
extracted" warning applies to). This is graphify-internal, not
`tpk`-specific. It doesn't corrupt the stored graph: each `tpk ingest`
inserts that run's full parsed node/edge set and then deletes any previous
row for that repo not touched by the run (`upsert_graph`'s stale-delete), so
the graph always reflects exactly the most recent run's output — it just
means "most recent run's output" can itself vary by a handful of nodes
between otherwise-identical runs. Don't rely on `tpk status`/`kg_nodes`
counts being bit-for-bit reproducible across ingests of the same commit.

## Chat agent & web UI

`tpk serve` runs a FastAPI server exposing a streaming chat agent (`POST
/chat`, Server-Sent Events: each event is a `data: {...}\n\n` line with
`type` one of `token` | `tool` | `done` | `error`) over the knowledge graph,
plus the built React web UI at `/` (mounted from `web/dist` when present) and
`GET /healthz`.

The agent needs its own LLM configuration, separate from graphify's
extraction backend — set in `.env` or the shell:

- `TPK_AGENT_PROVIDER` — `anthropic` or `openai`; if unset, inferred from
  whichever of `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` or
  `OPENAI_API_KEY`/`OPENAI_BASE_URL` is set.
- `TPK_AGENT_MODEL` — defaults to `claude-sonnet-5` (anthropic) or `gpt-5.2`
  (openai).

Model choice matters when pointing `TPK_AGENT_PROVIDER=openai` at a gateway
rather than OpenAI directly: both `openai.gpt-oss-120b` and
`qwen.qwen3-coder-next` are verified working via the Bedrock OpenAI-compatible
gateway (the earlier gpt-oss-120b hangs were root-caused to tpk's shared-client
concurrency bug and to tool exceptions aborting the stream — both now fixed in
commits e4f093e and faec3a3). Anthropic models are not served by that
endpoint's `/v1/chat/completions` surface; use `TPK_AGENT_PROVIDER=anthropic`
with a direct key or Anthropic-compatible gateway instead. Note: the Bedrock
gateway occasionally returns transient 500s mid-conversation — retry. See
`docs/eval/m4-smoke.md` for the full compatibility record.

Local dev:

    make web-build     # build web/dist once (or after UI changes)
    make serve         # tpk serve, reads WEB_DIST=web/dist if present
    make web-dev       # Vite dev server with hot reload, proxies /chat to :8000

Via docker compose, the `agent` service builds the same image as `tpk`
(the web build stage runs during `docker compose build`) and runs `tpk serve`
against the `tpk` service's database:

    docker compose up -d
    open http://localhost:8000

## Manage the corpus

Beyond `repos.toml` + `tpk ingest`, the running server exposes a management
API and a **Manage** tab in the web UI (`http://localhost:8000`, next to the
chat view) for adding, versioning, enabling/disabling, reindexing, and
deleting corpus entries without editing config or restarting the server.

**Versioning model.** Each corpus entry's identity is `(name, ref)`, keyed in
the graph as `name@ref` (e.g. `helm-charts@timeplus-enterprise-v13.0.6`; a
local-path entry with no `ref` keys as the bare `name`). Multiple `ref`s of
the same `name` can coexist side by side — their nodes/edges live under
distinct `repo` keys in `kg_nodes`/`kg_edges` and don't collide. The
`enabled` flag on each entry (stored in `kg_repos`, the corpus source of
truth) gates whether it's searchable: the chat agent and `search_entities`
only see entries where `enabled = true` (`corpus.list_entries(...).enabled`
filters both the live agent's corpus prompt and `KnowledgeGraph`'s query
paths), so a disabled entry's data stays in the graph but stops showing up
in answers. **Delete** removes the `kg_repos` row; pass `purge` to also
delete its `kg_nodes`/`kg_edges` rows (irreversible — the Manage tab asks for
confirmation and shows a "also delete indexed data" checkbox for this).

**Release-upgrade workflow** (e.g. bumping `helm-charts` to a new chart
release without losing the old one while you verify):

1. Add the new `(name, ref)` via the API or the Manage tab's "Add corpus
   entry" form — this creates a second entry alongside the existing one,
   both enabled by default.
2. Let its ingest job run (submitted automatically on add, or trigger
   **Reindex**) and confirm it reaches `ok` with a plausible node/edge count.
3. Verify: ask the chat agent a question that should cite the new ref, or
   query `kg_nodes` directly for its `repo` key.
4. Flip the old ref's **Enabled** toggle off (keep the new one on) once
   you're satisfied — this is a soft cutover, so you can flip back instantly
   if the new ingest looks wrong, and both old and new are searchable side
   by side until you do.

**Management API** (all under `/api`, admin-gated when `TPK_ADMIN_TOKEN` is
set — see below):

| Method & path            | Body                                                | Notes |
|---------------------------|-----------------------------------------------------|-------|
| `GET /api/repos`          | —                                                     | List all entries: `name`, `ref`, `entry_key`, source, `enabled`, `node_count`, last ingest status/sha/time |
| `POST /api/repos`         | `{name, github\|path, ref, visibility, extraction, description, enabled, ingest}` | Create/update an entry; `ingest: true` (default) submits a background ingest job immediately |
| `POST /api/repos/toggle`  | `{name, ref, enabled}`                                | Flip searchability without touching indexed data |
| `POST /api/repos/reindex` | `{name, ref}`                                         | Re-run ingest for an existing entry (submits a job) |
| `POST /api/repos/delete`  | `{name, ref, purge}`                                  | Remove the entry; `purge: true` also deletes its `kg_nodes`/`kg_edges` rows |
| `GET /api/jobs`           | —                                                     | Last 50 ingest jobs (`queued`/`running`/`ok`/`failed`), newest first |
| `GET /api/jobs/{id}`      | —                                                     | Single job status |

Ingest jobs run on a background worker thread inside the server process, so
`POST` calls return immediately with a `job_id`; poll `/api/jobs` (the
Manage tab does this every 5s) until it reaches `ok` or `failed`.

**Input validation.** `name` must match `^[A-Za-z0-9._-]+$`; `ref` allows
`/` (for refs like `release/1.0`) but rejects a leading `-` or `/`, `..`
path components, and any other character outside `[A-Za-z0-9._/-]` — both
return `422`/`400`. `path`-type entries (`{"path": "..."}` instead of
`{"github": ...}`) are only accepted through this API when
`TPK_ADMIN_TOKEN` is set (`403` otherwise): on the default open admin gate, a
path entry would map an arbitrary server-filesystem path into the corpus,
which the chat agent's `read_source` tool then treats as readable — i.e. an
arbitrary-file-read primitive reachable over the network. Seeding path-type
entries from `repos.toml` or the `tpk` CLI is unaffected; this gate applies
only to the `/api/repos` create/update route.

**Admin gate.** Set `TPK_ADMIN_TOKEN` in `.env`/the environment to require
every `/api/*` call to carry a matching `X-Admin-Token` header (checked
against the exact env value in `_require_admin`); requests without it, or
with the wrong value, get `401`. Leaving `TPK_ADMIN_TOKEN` unset leaves the
management API open — fine for local dev, not for anything reachable outside
localhost. The Manage tab has a token field (top toolbar) that's sent as
`X-Admin-Token` on every request and cached in `sessionStorage` so you don't
retype it each visit.

**Legacy-key migration note.** Before versioned entries, graph rows were
keyed by bare `repo` name with no `@ref` suffix. The first `tpk ingest`
(CLI or a job the management API submits) after upgrading to this version
re-keys that repo's data: it writes the new run under `name@ref` *and*
deletes any stale rows still sitting under the bare `name` (`ingest_repo`
passes both `{key, repo_cfg.name}` to the stale-delete pass in
`upsert_graph`) — so a repo you haven't re-ingested since upgrading may
briefly show both a bare-name entry and a `name@ref` entry in
`kg_ingest_log`/query results until its next ingest runs. No manual cleanup
is needed; re-ingesting each repo once (`tpk ingest`, or Reindex from the
Manage tab) completes the migration.

## Use from Claude Code (MCP)

    claude mcp add timeplus-knowledge -- uv --directory /Users/gangtao/Code/timeplus/timeplus-knowledge run python -m tpk.mcp_server

If you're running from a worktree or a separate clone rather than the main
checkout, `--directory` must point at *that* checkout (the one containing
`pyproject.toml`), not the path above — pointing it at a directory with no
`pyproject.toml`/`src/tpk` registers successfully but the server itself
fails to start, which shows up in `claude mcp list` as
`✘ Failed to connect — -32000: MCP error -32000: Connection closed`.

Tools: `search_entities`, `get_entity`, `neighbors`, `path_between`,
`list_communities`, `read_source`.

Built on the `mcp` SDK 2.0 (`FastMCP`); verify registration with
`claude mcp list` and exercise the tools by asking Claude Code a question
that should trigger `search_entities` (e.g. "using the timeplus-knowledge
tools, what does the docs repo say about Quickstart?").

## Tests

    uv run pytest                    # integration tests skip without TIMEPLUS_HOST
    TIMEPLUS_HOST=localhost uv run pytest

Tests marked as requiring Timeplus are skipped automatically when
`TIMEPLUS_HOST` is unset (see `tests/conftest.py`); set it (plus
`TIMEPLUS_USER`/`TIMEPLUS_PASSWORD` if not using the `default` user with an
empty password) to run the full suite against a live timeplusd.

## Manual queries

Every hand query against the graph goes through `table(...)`, e.g.:

    echo "SELECT kind, count() FROM table(kg_nodes) GROUP BY kind ORDER BY count() DESC" | \
      curl "http://${TIMEPLUS_HOST}:8123/" -u "${TIMEPLUS_USER:-default}:${TIMEPLUS_PASSWORD:-}" --data-binary @-
