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
- **Per-repo isolation & history.** A failing repo logs `failed` and leaves its previous graph intact; `kg_ingest_log` keeps append-only run history (SHA, node/edge counts, status).

## 2. The knowledge graph

Stored entirely in Timeplus streams — no separate graph database.

- **Entities** (`kg_nodes`): functions, classes, files, documents, concepts — each with kind, name, qualified name, source location, and a summary.
- **Relationships** (`kg_edges`): calls, imports, containment, and semantic links, each tagged with a confidence bucket (extracted vs. inferred).
- **Communities**: clustered groupings of related entities for higher-level navigation.
- Node IDs hash a versioned entry key so multiple releases of the same repo coexist without collision.

## 3. Graph query tools

The `KnowledgeGraph` read layer exposes six composable tools — the same set the chat agent, the MCP server, and the CLI all use:

| Tool | What it answers |
|------|-----------------|
| `search_entities` | Find code/doc entities by keyword (filterable by kind, repo). |
| `get_entity` | Fetch one entity's full record. |
| `neighbors` | Local subgraph around an entity (BFS, direction- and relation-filtered). |
| `path_between` | Shortest connection between two entities. |
| `list_communities` | Cluster overview across the corpus. |
| `read_source` | Read exact source lines so answers can quote real code. |

All queries run against a single serialized Timeplus session and are transparently filtered by the active corpus and the caller's role scope (see §7).

## 4. Chat agent & web UI

- A **LangGraph ReAct agent** reasons over the six graph tools to answer natural-language questions, citing the source it read.
- **Streaming web UI** (React, Timeplus Console styling) served on port **8000**, with token-by-token SSE streaming and live tool-call display.
- **Dual provider support** (Anthropic / OpenAI) with gateway compatibility; chat model and extraction model are configured independently.

## 5. MCP integration (Claude Code)

The graph is available to Claude Code (and other MCP clients) as a first-class tool server:

```
claude mcp add timeplus-knowledge -- docker compose exec -T tpk tpk-mcp
```

The MCP server exposes the same six tools, so a coding assistant can search the Timeplus codebase, trace call paths, and quote source directly in its own workflow.

For a deployed `tpk serve`, the same six tools are also reachable remotely over streamable HTTP at `/mcp`: create a personal API token in the web UI ("API tokens" page) and register it with `claude mcp add --transport http` and an `Authorization: Bearer <token>` header. The call then runs as the token's user, scoped like chat.

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
- **Function capabilities per role (issue #23).** A role grants any subset of `chat`, `explore`, `corpus:view`/`corpus:manage`, `users:view`/`users:manage` (`:manage` implies `:view`), enforced identically on the API and the UI — the sidebar and each screen show only what the caller holds. The built-in `admin` role is the reserved super-role with every capability. Existing roles migrate to chat-only on upgrade.
- **Role-scoped chat.** Orthogonal to capabilities: a role lists the exact `name@ref` corpus entries its members may query, and a non-admin's chat/explore results are transparently restricted to that scope (admin, the local stdio MCP server, and the CLI are unrestricted; the remote `/mcp` endpoint runs as the token's user and is scoped like chat). Isolation is enforced server-side across every graph tool path.
- **Bounded delegation.** A non-admin with `users:manage` can never mint admins or manage admin users, and can only grant capabilities and corpus entries within its own grant — no self-promotion path.
- **Admin console.** A Users/Roles console manages accounts, role assignments, password resets, per-role capabilities, and per-role entry-key access; last-admin lockout is prevented.
- **Hardened DB layer.** The compose stack provisions a dedicated `tpk` timeplusd user and password-locks the previously open `default` user.

## Deployment surfaces

| Port / entry | Purpose |
|--------------|---------|
| `8000` | Chat agent + web UI (login-gated) |
| `8123` | Timeplusd SQL over HTTP (ClickHouse-compatible) |
| `3218` | Timeplus REST ingest API |
| `tpk-mcp` | MCP server (via `docker compose exec`) |
| `/mcp` | Remote MCP endpoint (streamable HTTP, per-user API token) |
| `tpk` CLI | `ingest`, `status`, `serve`, and corpus commands |

## Getting started

```bash
cp .env.example .env        # fill in LLM keys, GITHUB_TOKEN, TIMEPLUS_PASSWORD
docker compose up -d
open http://localhost:8000  # log in as admin / changeme (forced change on first login)
```

See the [README](../README.md) for full setup, ingest, corpus-management, and users-&-roles documentation.
