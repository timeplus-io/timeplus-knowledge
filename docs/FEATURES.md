# timeplus-knowledge — Features & Functions

An introduction to what the Timeplus knowledge agent does and the capabilities it ships today.

## What it is

**timeplus-knowledge** turns a set of source repositories into a queryable **knowledge graph** stored in Timeplus, and puts a **chat agent** and **MCP tools** in front of it so people and coding assistants can ask questions about the Timeplus codebase and docs and get grounded, source-cited answers.

The whole system is one pipeline:

```
GitHub repos (pinned tags)
      │  ingest  (graphify: AST + optional LLM extraction)
      ▼
Timeplus mutable streams  (kg_nodes, kg_edges, kg_ingest_log, kg_repos, …)
      │  KnowledgeGraph read layer  (6 query tools)
      ├──────────────┬───────────────┐
      ▼              ▼               ▼
  Chat agent     MCP server      Management API
  + web UI     (Claude Code)     + Manage/Users UI
```

Everything runs as a Docker Compose stack: an all-in-one `tpk` service (timeplusd + the toolkit) and an `agent` service (chat + web UI).

---

## 1. Knowledge-graph ingest

Builds the graph from a pinned, versioned corpus.

- **GitHub-pinned corpus.** Each repo in `repos.toml` is fetched at an exact tag/release (`github = "org/repo"` + `ref = "v3.3.1"`) into a checkout cache; private repos use `GITHUB_TOKEN`. A local `path =` mode exists for development.
- **Two extraction modes.**
  - `code-only` (default): local tree-sitter AST parsing. Free, offline, deterministic. Covers ~30 languages (Python, C/C++, Go, Rust, TypeScript/JS, Java, C#, and more).
  - `semantic`: adds an LLM pass over doc/config files (Markdown, YAML, reST, HTML). **Code is AST-parsed in both modes** — semantic never sends code to the LLM — so LLM cost scales with doc count, not code size.
- **Pluggable LLM backends.** Anthropic or OpenAI, direct or through a gateway (AWS Bedrock via LiteLLM / Bedrock Access Gateway), configured in the `[llm]` section.
- **Live progress.** An ingest reports its phase (`fetch` → `extract` → `parse` → `upsert` → `done`) with running node/edge counts — as progress lines in `tpk ingest` and on the job in the Manage tab — instead of going silent for the length of a large repo.
- **Per-repo isolation & history.** A failing repo logs `failed` and leaves its previous graph intact; `kg_ingest_log` keeps append-only run history (SHA, node/edge counts, status).

## 2. The knowledge graph

Stored entirely in Timeplus streams — no separate graph database.

- **Entities** (`kg_nodes`): each with kind, name, qualified name, source location, and a summary. Code kinds are `function` (functions and methods, named `Class::method`), `class`, `member` (a field, or a method that is only declared), `file`, and `symbol` (a bare type/alias reference); doc kinds are `document`, `concept`, `rationale`, `paper`, `image`. graphify has no class/method notion of its own, so tpk derives the code kinds from the graph structure — e.g. an out-of-class C++ definition (`BlockIO Foo::execute() {…}`) is recognised as the method `Foo::execute` via the class's `defines` edge.
- **Relationships** (`kg_edges`): calls, imports, containment, and semantic links, each tagged with a confidence bucket (extracted vs. inferred).
- **Communities**: clusters of densely connected entities, found by graphify's community detection at ingest — roughly a subsystem. Ids are opaque numbers unique only within one `repo@ref`; `list_communities` labels each with its dominant directories and files.
- Node IDs hash a versioned entry key so multiple releases of the same repo coexist without collision.

## 3. Graph query tools

The `KnowledgeGraph` read layer exposes six composable tools — the same set the chat agent, the MCP server, and the CLI all use:

| Tool | What it answers |
|------|-----------------|
| `search_entities` | Find code/doc entities by keyword (filterable by kind, repo). |
| `get_entity` | Fetch one entity's full record. |
| `neighbors` | Local subgraph around an entity (BFS, direction- and relation-filtered). |
| `path_between` | How two entities are connected: a **directed call chain** first (either direction, listed caller-first), else a "related" path over other relations that is labelled as *not* a call chain and never routes through a shared file or type symbol. When nothing is found it returns each end's direct callers/callees as next steps. |
| `list_communities` | Bounded cluster overview (top N by size, default 50 / max 200, `min_nodes` filter) with `total`/`truncated`, a per-repo summary, and a directory/file label per cluster. |
| `read_source` | Read exact source lines so answers can quote real code. |

Every result is bounded, because it lands in an LLM's context (the chat agent's or a remote MCP client's): `search_entities` caps `limit` at 200, `neighbors` at depth 3 / 200 nodes per hop, `path_between` at depth 12 within a 5,000-entity search budget, `read_source` at 400 lines, and `list_communities` at 200 rows (default 50).

All queries run against a single serialized Timeplus session and are transparently filtered by the active corpus and the caller's role scope (see §7).

## 4. Chat agent & web UI

- A **LangGraph ReAct agent** reasons over the six graph tools to answer natural-language questions, citing the source it read.
- **Streaming web UI** (React, Timeplus Console styling) served on port **8000**, with token-by-token SSE streaming. The sidebar shows only the pages the caller's capabilities allow:
  - **Chat** — live tool-call display, a collapsible **thinking trace** (the model's reasoning, including OpenAI-compatible reasoning models such as gpt-oss), and `[n]` citations backed by a **Sources** panel of the exact lines the agent read. The header shows the active chat model.
  - **Explorer** — search the graph, inspect an entity, walk its neighbors on a radial subgraph, read its source, and hand an entity to Chat ("Ask about this").
  - **API tokens** — personal tokens for connecting a coding agent over MCP (§5).
  - **Manage** — the corpus (§6), with live ingest progress.
  - **Users** — accounts and roles (§7).
- **Source-code protection.** Raw source — the thinking trace, citation fragments, Explorer's source view, MCP `read_source` — is shown only to roles holding `source:view`; everyone else still gets grounded answers, just without the code itself.
- **Per-user daily token budget.** Non-admin chat usage is metered per user per day: a per-user limit overrides the role's, which overrides the global default (`TPK_DAILY_TOKEN_LIMIT`, 500k; `0` = unlimited). Only an admin can change a budget.
- **Dual provider support** (Anthropic / OpenAI) with gateway compatibility; chat model and extraction model are configured independently.

## 5. MCP integration (Claude Code, Cursor, …)

The same six tools are available to coding agents as an MCP server, two ways.

**Remote — a deployed tpk (streamable HTTP at `/mcp`).** Served by `tpk serve` on the same port as the UI (no extra Service, port or ingress rule; stateless, so it works behind a load balancer).

```
claude mcp add --transport http timeplus-knowledge https://<host>/mcp \
  --header "Authorization: Bearer tpk_…"
```

- **The caller is a tpk user.** A token acts as its owner: `explore` to connect, the role's corpus scope on every tool call, `source:view` for `read_source`. Role or capability changes apply on the next call; nothing is cached in the token.
- **Personal API tokens.** Created on the **API tokens** page (shown once, with a ready-to-paste `claude mcp add` command): optional expiry (never / 30 / 90 / 365 days), up to 20 per user, stored only as SHA-256 hashes, valid **only** at `/mcp` (never for the REST API). Users revoke their own; a `users:manage` holder can revoke anyone's; disabling or deleting a user revokes theirs.
- **Fail-closed and auditable.** 401 / 403 / 503 mirror the REST API; denied requests are logged at WARNING on `tpk.mcp` (tokens are never logged); internal errors are masked for remote callers. `TPK_MCP_HTTP_ENABLED=0` turns the endpoint off; `TPK_MCP_ALLOWED_HOSTS` restricts accepted `Host` headers.

**Local — stdio.** Unrestricted (no login, no role scope); runs where the DB credentials are:

```
claude mcp add timeplus-knowledge -- docker compose exec -T app tpk-mcp
```

From a checkout, pass `TIMEPLUS_HOST` / `TIMEPLUS_USER` / `TIMEPLUS_PASSWORD` with `-e` (or `make mcp-register TIMEPLUS_PASSWORD=…`). If it cannot connect, the server exits with one line saying why.

## 6. Corpus management (issue #4)

Manage what the graph indexes at runtime, without editing files or restarting.

- **Versioned entries.** The corpus is a store (`kg_repos`) of `(name, ref)` entries keyed `name@ref`, so multiple releases of a repo live side by side.
- **Enable / disable.** A one-row flip gates whether an entry is searchable — instant, reversible, with a short-TTL live cache so changes take effect without a restart.
- **Explicit delete with purge.** Removing an entry can optionally scrub its `kg_nodes`/`kg_edges` rows; ingest history is never purged.
- **Background jobs.** Add / reindex operations run on a background worker; job status is pollable.
- **Manage tab** in the web UI drives all of it (add, toggle, reindex, delete).
- **Release-upgrade workflow:** add the new ref → ingest → verify → flip enabled; a legacy migration re-keys old bare-name rows on first ingest.
- **Export / import (issue #29).** `tpk export` dumps the ingested graph + corpus registry to a portable bundle (proton `FORMAT Parquet`, one file per stream + manifest); `tpk import` loads it into a new environment — no re-ingest, no LLM calls. Idempotent upsert by default (`--replace` to reset); auth streams excluded.

## 7. Authentication & role-based access (issue #8)

Login-based access control, with roles that scope what each user can query.

- **Users, roles, sessions** stored in Timeplus streams (`kg_users`, `kg_roles`, `kg_sessions`), plus the remote-MCP API tokens (`kg_api_tokens` and their last-use timestamps in `kg_api_token_usage`). Passwords are argon2id-hashed; session and API tokens are stored only as SHA-256 hashes.
- **Seeded admin + forced change.** A fresh deployment seeds `admin` / `changeme`; the admin must change the password on first login before anything else.
- **Function capabilities per role (issue #23).** A role grants any subset of `chat`, `explore`, `corpus:view`/`corpus:manage`, `users:view`/`users:manage` (`:manage` implies `:view`), and `source:view` (see raw source: thinking trace, citation fragments, Explorer source, MCP `read_source`), enforced identically on the API and the UI — the sidebar and each screen show only what the caller holds. The built-in `admin` role is the reserved super-role with every capability. Existing roles migrate to chat-only on upgrade.
- **Role-scoped chat.** Orthogonal to capabilities: a role lists the exact `name@ref` corpus entries its members may query, and a non-admin's chat/explore results are transparently restricted to that scope (admin, the local stdio MCP server, and the CLI are unrestricted; the remote `/mcp` endpoint runs as the token's user and is scoped like chat). Isolation is enforced server-side across every graph tool path.
- **Bounded delegation.** A non-admin with `users:manage` can never mint admins or manage admin users, and can only grant capabilities and corpus entries within its own grant — no self-promotion path.
- **Admin console.** A Users/Roles console manages accounts, role assignments, password resets, per-role capabilities, and per-role entry-key access; last-admin lockout is prevented, and `tpk auth reset-admin` recovers the admin account from the command line if it happens anyway.
- **Hardened DB layer.** The compose stack provisions a dedicated `tpk` timeplusd user and password-locks the previously open `default` user.

## Deployment surfaces

| Port / entry | Purpose |
|--------------|---------|
| `8000` | Chat agent + web UI (login-gated), REST API, and `/mcp` |
| `/mcp` | Remote MCP endpoint (streamable HTTP, per-user API token) |
| `/healthz` | Liveness |
| `8123` | Timeplusd SQL over HTTP (ClickHouse-compatible; override with `TIMEPLUS_PORT`) |
| `3218` | Timeplus REST ingest API |
| `tpk-mcp` | stdio MCP server (via `docker compose exec -T app tpk-mcp`) |
| `tpk` CLI | `ingest`, `status`, `serve`, `export` / `import`, `auth reset-admin` (break-glass admin recovery), and `eval` (run a fixed question set through the agent and report its tool usage) |

**Images** (multi-arch: `linux/amd64` + `linux/arm64`, published per release tag by GitHub Actions): `timeplus/tpk-app` — the app alone, for a separate timeplusd; `timeplus/tpk` — all-in-one (OSS proton + tpk).

**Ways to run it:** docker compose (DB + app), the all-in-one container, or Kubernetes — all-in-one, DB + app, or app-only against an existing Timeplus Enterprise (see [`deploy/k8s`](../deploy/k8s/README.md), including the upgrade procedure).

## Getting started

```bash
cp .env.example .env        # fill in LLM keys, GITHUB_TOKEN, TIMEPLUS_PASSWORD
docker compose up -d
open http://localhost:8000  # log in as admin / changeme (forced change on first login)
```

See the [README](../README.md) for full setup, ingest, corpus-management, and users-&-roles documentation.
