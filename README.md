# timeplus-knowledge

Knowledge graph + agent tooling for answering questions about Timeplus —
code, design, architecture, and devops. See
`docs/superpowers/specs/2026-08-10-timeplus-knowledge-agent-design.md`.

## Quick start

The fastest way to try it — OSS proton, the chat agent, and the web UI in a
single container (fully open-source, no separate database, no build):

    docker run -d --name tpk -p 8000:8000 \
      -e OPENAI_API_KEY=sk-... \
      timeplus/tpk:latest

    docker exec tpk tpk ingest        # build the knowledge graph
    open http://localhost:8000        # chat + web UI — log in as admin / changeme

The image is multi-arch (`linux/amd64` + `linux/arm64`), so it runs natively on
Apple Silicon. Use `-e ANTHROPIC_API_KEY=...` instead for Claude; the same key
powers both chat and semantic ingest. `tpk ingest` fetches the repos from the image's baked
config (`deploy/docker/repos.container.toml`) — private repos need
`-e GITHUB_TOKEN=...`. You'll be prompted to change the seed `admin` password on
first login.

For the production shape (separate Enterprise timeplusd + app), compose files,
configuration, roles, and everything else, read on.

## Setup

`make` lists every dev command (setup, db, tests, ingest, MCP, chat agent /
web UI, docker image). The underlying steps:

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a running
Timeplus — either **Timeplus Enterprise (timeplusd)** or **OSS
[proton](https://github.com/timeplus-io/proton)** (see [Database
backend](#database-backend) below):

    docker run -d --name timeplusd -p 8123:8123 -p 3218:3218 \
      -v $(pwd)/deploy/timeplusd-dev/small-segments.yaml:/etc/timeplusd-server/config.d/small-segments.yaml:ro \
      docker.timeplus.com/timeplus/timeplusd:latest
    uv sync
    export TIMEPLUS_HOST=localhost TIMEPLUS_USER=default TIMEPLUS_PASSWORD=

### Database backend

`TPK_DB_BACKEND` selects the backend (default `timeplusd`):

These name a **stream-semantics mode**, not strictly a server product:

- **`timeplusd`** (default) — keyed state (`kg_nodes`, `kg_users`, …) lives in
  **mutable streams** (upsert by primary key, real `DELETE`). Mutable streams
  are a **Timeplus Enterprise** feature, so this mode requires Enterprise.
- **`proton`** — keyed state uses `versioned_kv` streams plus a `deleted`
  tombstone column: upserts overwrite by key, `DELETE` becomes a tombstone
  write, and reads filter it out. `versioned_kv` exists in **both OSS proton
  and Enterprise** (Enterprise supports the full proton feature set), so this
  is the **OSS-compatible mode that runs on either server** — not proton-only.
  Set `export TPK_DB_BACKEND=proton`; to run OSS proton itself:

      docker run -d --name proton -p 8123:8123 ghcr.io/timeplus-io/proton:latest

So: run OSS proton → use `TPK_DB_BACKEND=proton`. Run Enterprise timeplusd →
either mode works (`timeplusd` for mutable streams, or `proton` for the
versioned_kv semantics). The choice only changes DDL and delete semantics
(localized to `db.py`); everything else behaves identically. See issue #50.

The `-v` mount is required on a laptop: the default timeplusd preallocates
2GB of nativelog per stream shard, which fills a Docker VM's disk fast once
you have a handful of streams. `deploy/timeplusd-dev/small-segments.yaml`
disables preallocation (segments grow as written) and raises the disk-usage
guard for local/dev use — do not use it in production.

## Configuration

Every non-secret setting can be set **either** in the config file (`repos.toml`,
or the file named by `TPK_CONFIG`) **or** via an environment variable. When both
are present the environment variable wins:

    environment variable  >  config-file value  >  built-in default

Put settings in `[db]`, `[agent]`, and `[server]` sections of the config file
(the shipped `repos.toml` documents each with its default commented out), or
export the matching env var — whichever suits your deployment. Secrets are
**env-only** and never read from the file.

| Env var | File `[section].key` | Default | Purpose |
|---|---|---|---|
| `TIMEPLUS_HOST` | `[db].host` | `localhost` | DB host |
| `TIMEPLUS_USER` | `[db].user` | `default` | DB user |
| `TIMEPLUS_PORT` | `[db].port` | `8123` | DB HTTP port |
| `TIMEPLUS_DATABASE` | `[db].database` | `tpk` | Database all tpk streams live under |
| `TPK_DB_BACKEND` | `[db].backend` | `timeplusd` | Stream-semantics mode (`timeplusd`\|`proton`) |
| `TPK_STREAM_PREFIX` | `[db].stream_prefix` | `` | Namespace prefix for all streams — honoured by every entry point (`serve`, `ingest`, `export`/`import`, `auth`, stdio MCP) |
| `TPK_DB_WAIT_SECONDS` | `[db].wait_seconds` | `60` | `serve`: DB connect retry budget |
| `TPK_AGENT_PROVIDER` | `[agent].provider` | auto | Chat LLM backend (`anthropic`\|`openai`) |
| `TPK_AGENT_MODEL` | `[agent].model` | per-provider | Chat model override |
| `TPK_AGENT_REASONING_EFFORT` | `[agent].reasoning_effort` | unset | OpenAI reasoning effort (e.g. `low`) |
| `TPK_CHECKOUT_DIR` | `[server].checkout_dir` | `~/.tpk/checkouts` | GitHub checkout cache |
| `TPK_SESSION_TTL` | `[server].session_ttl` | `86400` | Login session lifetime (seconds) |
| `TPK_CHAT_AUDIT` | `[server].chat_audit` | `true` | Chat Q&A auditing (`false`/`0` disables) |
| `TPK_DAILY_TOKEN_LIMIT` | `[server].daily_token_limit` | `500000` | Global fallback per-user daily token budget for non-admins (`0` = unlimited; a per-user or role `daily_token_limit` wins) |
| `TPK_MCP_HTTP_ENABLED` | `[server].mcp_http` | `true` | Remote MCP endpoint `/mcp` (`false`/`0` disables it) |
| `TPK_MCP_ALLOWED_HOSTS` | `[server].mcp_allowed_hosts` | `` | Comma-separated `Host` allow-list for `/mcp` (empty = no check) |
| `TPK_EXTRACTION_BACKEND` | `[llm].backend` | `auto` | Semantic-extraction backend (`auto`\|`claude`\|`openai`) |
| `TPK_EXTRACTION_MODEL` | `[llm].model` | backend default | Semantic-extraction model |

Secrets — **env-only**, never in the file: `TIMEPLUS_PASSWORD`,
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN` (plus the
`ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` / `*_MODEL` endpoint variables the LLM
clients read directly).

### The `tpk` database

All tpk streams (`kg_nodes`, `kg_users`, `chat_audit_log`, …) live under a
dedicated database — **`tpk`** by default, set via `TIMEPLUS_DATABASE` /
`[db].database`. tpk creates it on startup (`CREATE DATABASE IF NOT EXISTS`) and
qualifies every stream as `tpk.<name>`, so its objects don't clutter the server's
`default` database and can be granted or dropped as a unit. This matters most
when pointing tpk at a **shared** Timeplus Enterprise (the k8s app-only mode):
the connecting user needs `CREATE DATABASE` (first run) plus read/write on `tpk`.

**Upgrading an existing deployment:** streams created before this change live in
`default` and are **not** moved automatically — after upgrading, tpk looks in
`tpk` and finds an empty graph. Either re-ingest (`tpk ingest`), or carry data
over with `tpk export` (old version) → `tpk import` (new version). Point
`TIMEPLUS_DATABASE=default` to keep using the old location instead.

## Docker compose (quickest start)

Two deployment modes are provided:

- **DB + App** (`docker-compose.yml`, the default) — the **stock timeplusd
  image** (no build; config + user provisioning bind-mounted) plus a
  pure-Python `app` container (chat agent + web UI + ingest + MCP).
  Production-shaped: independent lifecycle, DB tracks upstream timeplusd. Swap
  the `db` image line for OSS proton if you prefer.
- **All-in-one** (`docker-compose.allinone.yml`) — **OSS proton** + tpk in a
  single container (fully open-source, `TPK_DB_BACKEND=proton`). For tests,
  demos, and quick local runs.

<!-- -->

    cp .env.example .env        # add ANTHROPIC_API_KEY / OPENAI_API_KEY, and
                                 # set TIMEPLUS_PASSWORD (see below)

    # DB + App (default):
    docker compose up -d        # or: make up
    docker compose exec app tpk ingest
    claude mcp add timeplus-knowledge -- docker compose exec -T app tpk-mcp

    # …or all-in-one (single container):
    docker compose -f docker-compose.allinone.yml up -d   # or: make up-allinone
    docker compose -f docker-compose.allinone.yml exec tpk tpk ingest

    open http://localhost:8000  # log in as admin / changeme (forced change)

`.env` (gitignored) carries the LLM keys for semantic extraction; graph data
lives in the named volume `tpk-data` and survives `docker compose down`.

**`TIMEPLUS_PASSWORD` is required** — `docker compose up` fails fast without
it. It provisions two DB users: a dedicated `tpk` user (used by the app to
talk to timeplusd) and the built-in `default` user, locked to the same
password rather than left with the base image's empty password. The app
authenticates as `tpk`, not `default`; use `TIMEPLUS_USER=tpk
TIMEPLUS_PASSWORD=...` if you connect to the compose stack's DB directly
(`uv run tpk status`, manual SQL, `pytest`).
This only applies inside docker compose — a bare `timeplusd` container
started per the [Setup](#setup) section above still runs with the
unauthenticated `default` user unless you configure it otherwise.

## Docker image (all-in-one)

`deploy/docker/Dockerfile` is multi-stage with two targets (see issue #48):
`app` (pure-Python tpk, no DB) and `allinone` (**OSS proton** + tpk in one
container — fully open-source, `TPK_DB_BACKEND=proton`, the default final
stage). The all-in-one image runs proton plus `tpk serve` (chat + web UI on
:8000) via `deploy/docker/allinone-entrypoint.sh`.
(The DB + App file's `db` service uses the stock timeplusd image directly — no
build.)

Two build commands map to the two deployment modes:

    make docker-build-allinone   # all-in-one image  -> all-in-one mode
    make docker-build-app        # tpk app-only image -> DB + App mode
    make docker-build            # both

Equivalently, `docker build -f deploy/docker/Dockerfile --target <allinone|app> -t <tag> .`.
Repo checkouts are mounted at `/repos` (paths come from the baked-in
`deploy/docker/repos.container.toml`):

    make docker-build-allinone
    docker run -d --name tpk -p 8000:8000 -p 8123:8123 -p 3218:3218 \
      -v ~/Code/timeplus:/repos:ro timeplus/tpk:dev

    docker exec tpk tpk ingest              # build the graph inside
    docker exec tpk tpk status
    claude mcp add timeplus-knowledge -- docker exec -i tpk tpk-mcp

(The compose files are the easier path — see [Docker compose](#docker-compose-quickest-start).)

For semantic-extraction repos, pass the LLM key at run time:
`docker run ... -e ANTHROPIC_API_KEY` (or `-e OPENAI_API_KEY`). The
read-only `/repos` mount is fine — graphify's in-checkout cache writes are
skipped harmlessly.

## Kubernetes

`deploy/k8s/` has manifests for the same two modes — all-in-one (OSS proton +
tpk in one pod) and DB + App (Enterprise timeplusd StatefulSet + a separate app
Deployment) — plus a Secret template and a walkthrough. See
[`deploy/k8s/README.md`](deploy/k8s/README.md):

    kubectl create namespace timeplus-knowledge
    kubectl -n timeplus-knowledge create secret generic tpk-secrets \
      --from-literal=TIMEPLUS_PASSWORD='...' --from-literal=OPENAI_API_KEY='...'
    kubectl apply -f deploy/k8s/allinone.yaml      # or enterprise.yaml
    kubectl -n timeplus-knowledge exec -it tpk-allinone-0 -- tpk ingest
    kubectl -n timeplus-knowledge port-forward svc/tpk-allinone 8000:8000

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

### Which mode for a repo with both code and docs?

Code files are parsed by the local AST extractor in **both** modes —
`semantic` never sends code through the LLM. The modes differ only in
what happens to non-code files: `code-only` skips them; `semantic` runs
an additional LLM pass over them, producing the `document`/`concept`
nodes the agent uses for conceptual questions. Pick by what the docs are
worth:

- Docs are incidental (a README, a changelog) → `code-only`. Free,
  offline, deterministic.
- The repo has meaningful docs, or is mostly config/YAML/Markdown →
  `semantic`. LLM cost and wall-clock scale with the number of doc/config
  files, **not** with code size — the code half is still AST-parsed for
  free in the same run, so a large mixed repo is fine.
- To trim the LLM bill on a semantic repo, drop an untracked
  `.graphifyignore` into its checkout excluding doc dirs you don't need
  (e.g. `static/`/images — some gateways reject image modality anyway).

One failure-mode caveat (verified): if **every** LLM chunk fails (dead
gateway, bad model id), graphify exits non-zero without writing
`graph.json` — the successful AST results are discarded, the repo's
ingest is logged `failed`, and the previous graph is left untouched.
Partial chunk failures still produce a graph; re-run to fill the gaps.

### Supported languages and file types

The AST pass (runs in both modes) covers: Python,
JavaScript/TypeScript (incl. JSX/TSX and Vue/Svelte/Astro components),
Go, Rust, C, C++ (incl. CUDA/Metal), Java, Groovy/Gradle, C#, Kotlin,
Scala, Swift, Ruby, PHP, Objective-C, Elixir, Lua, Julia, Fortran, Dart,
Zig, PowerShell, Bash, Pascal/Delphi, Verilog, Apex, JSON, and .NET
project files (`.sln`/`.csproj`/`.xaml`/`.razor`). SQL and
Terraform/HCL need graphify's optional `sql`/`terraform` extras, which
this project does not install — those files are skipped with a warning.

The LLM pass (`semantic` mode only) covers doc/config files: Markdown
(`.md`/`.mdx`), reStructuredText, plain text, HTML, and YAML — plus
PDFs, images, and Office files if present (exclude images via
`.graphifyignore` when your gateway lacks image modality).

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
`type` one of `token` | `thinking` | `tool` | `tool_result` | `source` | `done` |
`error`; `thinking` and `source` are sent only to callers holding `source:view`)
over the knowledge graph,
plus the built React web UI at `/` (mounted from `web/dist` when present) and
`GET /healthz` — unauthenticated, returning `{"status": "ok", "version":
"0.0.5", "commit": "bc549ef"}`.

**Which version is running?** The same string everywhere: `tpk --version`,
`/healthz`, the web UI (sidebar footer and login page), the `tpk serve` startup
line, and MCP `serverInfo.version`. Published images report their release tag
(the Docker workflow bakes `TPK_VERSION` / `TPK_COMMIT` in), a git checkout
reports `git describe` (e.g. `0.0.5-3-gbc549ef`), and a local `docker build`
without those build-args reports `dev`.

The web UI's sidebar shows only the pages the caller's capabilities allow:
**Chat** (live tool calls, a collapsible thinking trace, `[n]` citations with a
Sources panel), **Explorer** (search, entity detail, neighbors graph, source),
**API tokens** (personal tokens for [MCP](#use-from-claude-code-mcp)),
**Manage** (the corpus, with live ingest progress) and **Users** (accounts and
roles).

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
commits e4f093e and faec3a3). Note: the Bedrock gateway occasionally returns
transient 500s mid-conversation — retry. See `docs/eval/m4-smoke.md` for the
full compatibility record.

### Anthropic (Claude) via a Bedrock gateway

Anthropic models are **not** served by the gateway's OpenAI-compatible
`/v1/chat/completions` surface. To run Claude, point `TPK_AGENT_PROVIDER=anthropic`
at the gateway's **Anthropic-protocol surface** (for the `bedrock-mantle` gateway
that path is `/anthropic`, which the client hits as `…/anthropic/v1/messages`):

    # .env — chat agent on Claude Opus through the Bedrock gateway
    ANTHROPIC_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/anthropic
    ANTHROPIC_API_KEY=<gateway key>          # same credential the gateway expects
    TPK_AGENT_PROVIDER=anthropic
    TPK_AGENT_MODEL=anthropic.claude-opus-4-8

The gateway authenticates the Anthropic surface with the standard `x-api-key`
header (what `ChatAnthropic` sends), typically the same credential as the
OpenAI surface.

### Split: Claude for chat, gpt-oss for ingest

The chat agent and [ingest](#ingest) are configured independently, so a common
setup is a strong model for chat and a cheap/fast one for bulk extraction:

| Path       | Config source              | Backend / surface                    | Model                        |
|------------|----------------------------|--------------------------------------|------------------------------|
| Chat agent | `.env` `TPK_AGENT_*`       | Anthropic — `ANTHROPIC_BASE_URL` (`…/anthropic`) | `anthropic.claude-opus-4-8`  |
| Ingest     | `repos.toml` `[llm]`       | OpenAI — `OPENAI_BASE_URL` (`…/v1`)  | `openai.gpt-oss-120b`        |

> **Gotcha — pin `[llm].backend`, don't rely on `auto`.** With this split
> **both** `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` are exported, so
> `[llm].backend = "auto"` becomes ambiguous (graphify would pick a backend on
> its own). Set the ingest backend explicitly in `repos.toml`:
>
>     [llm]
>     backend = "openai"                 # or "claude" to extract with Opus too
>     model   = "openai.gpt-oss-120b"    # anthropic.claude-opus-4-8 for claude
>     token_budget = 16000
>
> To move ingest onto Claude as well, flip these to `backend = "claude"` /
> `model = "anthropic.claude-opus-4-8"` — no `.env` change needed, since the
> `ANTHROPIC_*` vars are already set. Semantic extraction only sends doc/config
> files to the LLM (code is AST-parsed locally), but it is still many chunks, so
> gpt-oss is the economical choice for full-corpus ingest.

Local dev:

    make web-build     # build web/dist once (or after UI changes)
    make serve         # tpk serve, reads WEB_DIST=web/dist if present
    make web-dev       # Vite dev server with hot reload, proxies /chat to :8000

Via docker compose (DB + App), the `app` service builds the `app` image (the
web build stage runs during `docker compose build`) and runs `tpk serve`
against the `db` service's database:

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

**Management API** (all under `/api`, always admin-gated — see
[Users & roles](#users--roles) below):

| Method & path            | Auth               | Body                                                | Notes |
|---------------------------|--------------------|-----------------------------------------------------|-------|
| `GET /api/repos`          | admin bearer token | —                                                     | List all entries: `name`, `ref`, `entry_key`, source, `enabled`, `node_count`, last ingest status/sha/time |
| `POST /api/repos`         | admin bearer token | `{name, github\|path, ref, visibility, extraction, description, enabled, ingest}` | Create/update an entry; `ingest: true` (default) submits a background ingest job immediately |
| `POST /api/repos/toggle`  | admin bearer token | `{name, ref, enabled}`                                | Flip searchability without touching indexed data |
| `POST /api/repos/reindex` | admin bearer token | `{name, ref}`                                         | Re-run ingest for an existing entry (submits a job) |
| `POST /api/repos/delete`  | admin bearer token | `{name, ref, purge}`                                  | Remove the entry; `purge: true` also deletes its `kg_nodes`/`kg_edges` rows |
| `GET /api/jobs`           | admin bearer token | —                                                     | Last 50 ingest jobs (`queued`/`running`/`ok`/`failed`), newest first |
| `GET /api/jobs/{id}`      | admin bearer token | —                                                     | Single job status |

Ingest jobs run on a background worker thread inside the server process, so
`POST` calls return immediately with a `job_id`; poll `/api/jobs` (the
Manage tab does this every 5s) until it reaches `ok` or `failed`.

**Input validation.** `name` must match `^[A-Za-z0-9._-]+$` and must not be
`.` or `..` (both match that regex but, used as a filesystem path segment
for checkout/out-dir paths, would walk outside the checkout cache); `ref`
allows `/` (for refs like `release/1.0`) but rejects a leading `-` or `/`,
`..` path components, and any other character outside `[A-Za-z0-9._/-]` —
all return `400`. `path`-type entries (`{"path": "..."}` instead of
`{"github": ...}`) would map an arbitrary server-filesystem path into the
corpus, which the chat agent's `read_source` tool then treats as
readable — i.e. an arbitrary-file-read primitive reachable over the
network — but every `/api/repos` create/update call already requires an
admin bearer token (see [Users & roles](#users--roles)), so this isn't
reachable without admin credentials in the first place. Seeding path-type
entries from `repos.toml` or the `tpk` CLI is unaffected.

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

### Export / import the corpus (skip re-ingest)

Ingest is expensive — AST parsing plus (for `semantic` repos) LLM extraction
through the gateway. To stand up a **new environment** without paying that
cost again, dump the already-ingested graph from one deployment and load it
into another. Export/import leverages proton's own `FORMAT Parquet`
serialization (the engine encodes/decodes; no extra dependency):

    tpk export --out corpus-dump/        # writes one <stream>.parquet per stream + manifest.json
    tpk export --out dump/ --format Native   # ClickHouse Native instead of Parquet

    tpk import corpus-dump/               # load into this environment (upsert)
    tpk import corpus-dump/ --replace     # reset the target streams first (drop stale rows)

The bundle covers the expensive/portable streams — `kg_nodes`, `kg_edges`,
`kg_repos`, `kg_ingest_log` — plus a `manifest.json` (format, per-stream
columns + row counts, `created_at`). **Auth streams
(`kg_users`/`kg_roles`/`kg_sessions`/`kg_api_tokens`/`kg_api_token_usage`) are
deliberately excluded** — they are environment-specific and secret-bearing;
seed the admin normally on the new deployment.

`import` runs `ensure_schema` first, so a fresh environment gets the streams
(with the current columns) before loading. `kg_nodes`/`kg_edges`/`kg_repos`
are mutable and primary-keyed, so a plain `import` is an idempotent **upsert**
— re-running it converges rather than duplicating. `kg_ingest_log` is
append-only (no key), so a repeated plain `import` **appends** its provenance
rows (harmless for `tpk status`, which takes the latest run per repo, but not
deduped). Use `--replace` when the target has stale rows not in the bundle
(e.g. repos you've since dropped) or to reset the ingest log; it drops and
recreates each bundled stream — one at a time, immediately before reloading
it — before loading. A bundle whose columns don't
all exist in the target deployment is rejected up front (schema drift between
versions), before anything is written. Bundle files stream to/from disk, so a
300k-node graph never lands wholly in memory.

Streams under `TPK_STREAM_PREFIX` are exported/imported when that env var is
set, matching the rest of the CLI.

### Users & roles

`tpk serve` requires a login: `GET /healthz` is the only unauthenticated
route, everything else — the chat UI at `/`, `POST /chat`, and every
`/api/*` management call — needs a valid session (`401` without one, or with
an expired/invalid token).

**Login model.** `POST /auth/login` with `{username, password}` returns a
bearer `token` (send as `Authorization: Bearer <token>` on every subsequent
call), the user's `role`, and `must_change_password`. Sessions live in
`kg_users`/`kg_roles`/`kg_sessions` (mutable streams, same store as the
graph; personal API tokens for `/mcp` live alongside them in
`kg_api_tokens`, with last-use timestamps in `kg_api_token_usage` — both
session and API tokens are stored as sha256 hashes only, never in clear) and
expire after `TPK_SESSION_TTL` seconds (default `86400` = 24h;
expired sessions are deleted lazily on next access, not by a background
sweep). `POST /auth/logout` deletes the current session; `GET /auth/me`
returns the caller's identity.

**Seeded admin.** The first time `tpk serve` starts against an empty
`kg_users` table, it seeds one user: `admin` / `changeme`, with
`must_change_password = true`. Every route except `/auth/*` (login,
change-password, me, logout) returns `403 password_change_required` for a
user in that state — `POST /auth/change-password` with
`{old_password, new_password}` (new password ≥ 8 characters, not the
seeded default, different from the old one) clears the flag and revokes
every other session for that user. Change it before doing anything else.

**Roles.** A role is `{name, entry_keys, capabilities, description}` and
governs access along two orthogonal axes:

- **Function capabilities** — which features a member can reach. Any subset
  of `chat`, `explore`, `corpus:view`, `corpus:manage`, `users:view`,
  `users:manage` (`:manage` implies `:view`) and `source:view`, enforced
  identically on the
  API (each endpoint requires its capability) and the UI (the sidebar and
  each screen show only what the caller holds). Omitting `capabilities` when
  creating a role defaults it to chat-only; roles created before this
  feature migrate to chat-only on upgrade.
  `explore` also covers the **API tokens** page and connecting a coding agent
  over [remote MCP](#remote-http--a-deployed-tpk). `source:view` gates raw
  source everywhere it can appear — the chat thinking trace and citation
  fragments, Explorer's source view, and the MCP `read_source` tool; without
  it a member still gets grounded answers, just not the code itself.
- **Corpus scope** — `entry_keys`, each a corpus entry identity in the same
  `name@ref` (or bare `name` for local-path entries) form used throughout
  [Manage the corpus](#manage-the-corpus). A non-admin's `chat`/`explore`
  results are scoped to the union of their role's `entry_keys`: the agent
  answers only from those entries and won't cite anything outside the list.

`admin` is reserved — it isn't a `kg_roles` row; a user with `role =
"admin"` is the super-role, holding every capability with unscoped corpus
access. **Bounded delegation:** a non-admin with `users:manage` can never
mint admins or manage admin users, and can only grant capabilities and
corpus entries within its own grant. Role names must match
`^[A-Za-z0-9._-]+$`; a role can't be deleted while any user still has it
assigned.

**User/role management API** (each route requires its capability — see the
table — with `403` when the caller lacks it, `401` unauthenticated):

| Method & path           | Capability      | Body                                                      | Notes |
|--------------------------|-----------------|------------------------------------------------------------|-------|
| `GET /api/users`         | `users:view`    | —                                                            | List users: `username`, `role`, `must_change_password`, `disabled` (no password hashes) |
| `POST /api/users`        | `users:manage`  | `{username, password, role, must_change_password}`          | Create a user; `role` must be `admin` or an existing role name |
| `POST /api/users/update` | `users:manage`  | `{username, role?, password?, must_change_password?, disabled?}` | Partial update; setting `password` forces a reset (`must_change_password` defaults to `true` unless given) and revokes the user's other sessions; `disabled: true` also revokes their API tokens |
| `POST /api/users/delete` | `users:manage`  | `{username}`                                                 | Delete a user, their sessions and their API tokens |
| `GET /api/roles`         | `users:view`    | —                                                            | List roles: `name`, `entry_keys`, `capabilities`, `description` |
| `POST /api/roles`        | `users:manage`  | `{name, entry_keys, capabilities?, description}`             | Create/update a role; `entry_keys` must be non-empty strings; unknown capabilities and `name = "admin"` are rejected |
| `POST /api/roles/delete` | `users:manage`  | `{name}`                                                      | Delete a role; `409` if any user still has it assigned |
| `GET /api/tokens`        | `explore`       | `?username=` (optional)                                      | List the caller's API tokens (metadata only, never the token); another user's needs `users:manage` |
| `POST /api/tokens`       | `explore`       | `{name, expires_days?}`                                      | Mint an API token for the caller; the plaintext is returned once. `expires_days` is `30`, `90` or `365` (omit = never) |
| `POST /api/tokens/revoke` | `explore`      | `{token_id, username?}`                                      | Revoke one token; `username` (another user's) needs `users:manage` |
| `POST /api/tokens/revoke-all` | `users:manage` | `{username}`                                            | Revoke every API token of that user |

The corpus routes under `/api/repos` and `/api/jobs` are gated the same way
(`corpus:view` to read, `corpus:manage` to mutate). The last enabled
`admin` account is protected: demoting, disabling, or deleting it when it's
the only one left returns `400`. The web UI's **Users** console (visible
with `users:view`) covers all of this — create/edit/delete users and roles,
tick capabilities and corpus entries by checkbox — without hand-writing
these requests; it is read-only for a `users:view`-only caller.

**Break-glass (locked out of every admin account).** The API's last-admin
guard only stops you from doing this through `/api`; if every admin is
disabled, deleted, or its password is lost, there is no in-app way back. Run
the recovery command where the DB credentials are — inside the app container:

    docker compose exec app tpk auth reset-admin
    # all-in-one:  docker exec -it tpk tpk auth reset-admin
    # Kubernetes:  kubectl -n timeplus-knowledge exec -it deploy/tpk-app -- tpk auth reset-admin

It resets `admin` to `admin` / `changeme` (enabled, admin role,
`must_change_password = true`) and deletes `admin`'s sessions and API tokens
— credentials are keyed to a *username*, so anything left behind would
authenticate as the recovered account. Every other user and all roles are
untouched, and it takes effect immediately (no restart). **Log in and change
the password right away**: until you do, the well-known seed password works.

If you suspect a compromise rather than a lockout, add `--revoke-all` to also
delete **every** user's sessions and API tokens (accounts and roles are kept;
users log in again and re-create their tokens). `--yes` skips the confirmation
prompt. The command works on both DB backends and honours `TIMEPLUS_DATABASE`
/ `TPK_STREAM_PREFIX`, unlike hand-written `DELETE` statements. Anyone able to
run it already holds the database credentials, so it grants nothing new.

## Use from Claude Code (MCP)

### Remote (HTTP) — a deployed tpk

`tpk serve` exposes the same six tools over MCP streamable HTTP at `/mcp`.
Your agent authenticates with a personal API token and acts **as you**: same
corpus scope, same permissions (`explore` to connect, `source:view` for
`read_source`). Changes an admin makes to your role apply on the next call.

1. In the web UI open **API tokens → New token**, name it, pick an expiry,
   and copy the token (shown once).
2. Register it:

       claude mcp add --transport http timeplus-knowledge https://<host>/mcp \
         --header "Authorization: Bearer tpk_…"

3. `claude mcp list` should show `timeplus-knowledge ✔ Connected`.

Tokens are valid only at `/mcp` (not the REST API). Revoke them on the same
page; disabling or deleting a user revokes theirs. Admins can revoke a
user's tokens from **Users**. Disable the endpoint with
`TPK_MCP_HTTP_ENABLED=0`; restrict accepted `Host` headers with
`TPK_MCP_ALLOWED_HOSTS`: each entry matches the exact `Host` header value
(`host` or `host:port`, with `host:*` as a port wildcard), and enabling the
allow-list also turns on the SDK's `Origin` check with an empty allow-list, so
any request carrying an `Origin` header is rejected. Denied `/mcp` requests
(bad, missing, revoked or expired token, no `explore`, etc.) are logged at
WARNING on the `tpk.mcp` logger — tokens themselves are never logged.

### Local (stdio)

The stdio server talks to Timeplus directly, so it needs the same DB
credentials as `tpk serve`. Easiest is to run it **inside the app container**,
which already has them:

    claude mcp add timeplus-knowledge -- docker compose exec -T app tpk-mcp
    # all-in-one image:  claude mcp add timeplus-knowledge -- docker exec -i tpk tpk-mcp

To run it from a checkout instead, pass the credentials to the server with
`-e` (the compose DB only accepts the `tpk` user with your `TIMEPLUS_PASSWORD`;
`default` with no password is rejected):

    claude mcp add timeplus-knowledge \
      -e TIMEPLUS_HOST=localhost -e TIMEPLUS_USER=tpk -e TIMEPLUS_PASSWORD=<password> \
      -- uv --directory /path/to/timeplus-knowledge run python -m tpk.mcp_server

or `make mcp-register TIMEPLUS_PASSWORD=<password>` from the checkout. `-e`
stores the password in Claude Code's local config for this project.

If the server can't connect it exits with one line on stderr saying why
(host, user, and the database's own error, e.g. `Authentication failed`). MCP
clients hide that and only report `Failed to connect — Connection closed`, so
run `make mcp` (or the command after `--`) in a terminal to see it. A local
database (`localhost` / `127.0.0.1`) is always reached directly, even when
`HTTP_PROXY` is set in your shell.

If you're running from a worktree or a separate clone rather than the main
checkout, `--directory` must point at *that* checkout (the one containing
`pyproject.toml`), not the path above — pointing it at a directory with no
`pyproject.toml`/`src/tpk` registers successfully but the server itself
fails to start, which shows up in `claude mcp list` as
`✘ Failed to connect — -32000: MCP error -32000: Connection closed`.

Tools: `search_entities`, `get_entity`, `neighbors`, `path_between`,
`list_communities`, `read_source`. `path_between` returns `{found, mode,
direction, path, note}`: `mode: "calls"` is a real directed call chain,
`mode: "related"` only an association; when nothing is found it lists each
end's direct callers/callees, because the extracted C++ call graph is
incomplete (virtual dispatch, untyped member calls). Every result is bounded (it
lands in the agent's context): `search_entities` caps `limit` at 200, and
`list_communities` returns the top 50 clusters by default (max 200, `min_nodes`
to skip tiny ones) as `{communities, total, returned, truncated, by_repo}`, each
cluster labelled with its dominant directories and files — pass `repo` to drill
into one corpus entry.

Built on the `mcp` SDK 2.0 (`FastMCP`); verify registration with
`claude mcp list` and exercise the tools by asking Claude Code a question
that should trigger `search_entities` (e.g. "using the timeplus-knowledge
tools, what does the docs repo say about Quickstart?").

stdio MCP is local and unrestricted: no login, no role scope.

## Tests

    uv run pytest                    # integration tests skip without TIMEPLUS_HOST
    TIMEPLUS_HOST=localhost uv run pytest

Tests marked as requiring Timeplus are skipped automatically when
`TIMEPLUS_HOST` is unset (see `tests/conftest.py`); set it (plus
`TIMEPLUS_USER`/`TIMEPLUS_PASSWORD` if not using the `default` user with an
empty password) to run the full suite against a live timeplusd.

Against the docker-compose stack (which password-locks `default` — see
[Docker compose](#docker-compose-quickest-start)), `default`/empty-password
auth fails, so point the suite at the `tpk` user instead:

    TIMEPLUS_HOST=localhost TIMEPLUS_USER=tpk TIMEPLUS_PASSWORD=<your TIMEPLUS_PASSWORD> \
      uv run pytest -q

This also exercises the credentialed connection path end-to-end, not just
the unauthenticated dev default. The test fixtures address their streams
without a database qualifier, so if the suite fails with `Stream
default.test_… doesn't exist`, add `TIMEPLUS_DATABASE=default`.

`TIMEPLUS_PORT` points the suite (and everything else) at a timeplusd on a
non-default port — handy when 8123 is taken by another project. A local
database (`localhost` / `127.0.0.1`) is always reached directly, so a
shell-wide `HTTP_PROXY` does not get in the way.

## Manual queries

Every hand query against the graph goes through `table(...)`, e.g.:

    echo "SELECT kind, count() FROM table(kg_nodes) GROUP BY kind ORDER BY count() DESC" | \
      curl "http://${TIMEPLUS_HOST}:8123/" -u "${TIMEPLUS_USER:-default}:${TIMEPLUS_PASSWORD:-}" --data-binary @-
