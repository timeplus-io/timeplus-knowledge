# Timeplus Knowledge Agent — Design

**Date:** 2026-08-10
**Status:** Approved
**Repo:** `timeplus-knowledge` (greenfield monorepo)

## Purpose

A knowledge agent that answers questions about Timeplus — code, design, architecture, and especially devops — grounded in a knowledge graph built from Timeplus source repositories and documentation. Long-term audience is tiered (internal engineers get full code-grounded answers; customers get a restricted scope). **v1 is internal-only with no auth**; the data model reserves what v2 tiering needs.

## Decisions (settled during brainstorming)

| Decision | Choice |
|---|---|
| Audience | Tiered long-term; v1 internal-only, no auth |
| Corpus (v1) | proton, proton-enterprise, timeplus-enterprise, helm-charts, timeplus-enterprise-deployment, terraform-provider-timeplus, byoc, timeplus-cli, docs |
| Extraction | [Graphify](https://github.com/Graphify-Labs/graphify): tree-sitter AST for code, LLM extraction for docs; outputs graph.json with EXTRACTED/INFERRED confidence tags and Leiden communities |
| Graph store | Timeplus itself (dogfood): mutable streams for nodes/edges, SQL over HTTP port 8123 |
| Agent framework | LangGraph (open-source, model-agnostic); default model Anthropic |
| Architecture | Approach B: one tools layer exposed both to the web agent and as an MCP server for Claude Code/Cursor |
| Freshness | Manual on-demand CLI (`tpk ingest`); scheduled/CI re-index deferred to v2 |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  timeplus-knowledge (monorepo)                          │
│                                                         │
│  ingest/     graphify-based pipeline (Python CLI `tpk`) │
│     │        reads 9 local repo checkouts → graph.json  │
│     │        → merge + upsert into Timeplus streams     │
│     ▼                                                   │
│  Timeplus  ◄─── mutable streams: kg_nodes, kg_edges     │
│     ▲           append stream: kg_ingest_log            │
│     │                                                   │
│  tools/      one Python package: search_entities,       │
│     │        get_entity, neighbors, path_between,       │
│     │        list_communities, read_source              │
│     ├──────────────► mcp/   thin MCP server wrapper     │
│     │                       (Claude Code / Cursor)      │
│     ▼                                                   │
│  agent/      LangGraph agent (model-agnostic) with      │
│     │        citation-required answering                │
│     ▼                                                   │
│  server/     FastAPI: POST /chat (SSE), serves web UI   │
│     ▼                                                   │
│  web/        React chat UI, Timeplus console look       │
└─────────────────────────────────────────────────────────┘
```

### Components

1. **`ingest/`** — wraps graphify. `tpk ingest [--repo NAME]` runs graphify per repo (tree-sitter for code; LLM extraction for the `docs` repo), transforms `graph.json` into rows, and batch-inserts into Timeplus. Every node is tagged with `repo` and `visibility` (`public` for docs-derived nodes, `internal` for source-derived). `visibility` is unused in v1 but makes the v2 customer tier a query filter rather than a re-index.
2. **Timeplus** — the graph store. Mutable streams keyed by stable IDs so re-ingest is an upsert, not a rebuild.
3. **`tools/`** — the single source of truth for graph access. Pure Python functions issuing SQL to Timeplus. Multi-hop traversal (BFS) is implemented here with batched `IN`-clause queries. All consumers (agent, MCP) go through this package.
4. **`mcp/`** — thin MCP server exposing `tools/` so engineers can attach the knowledge graph to Claude Code/Cursor directly. Ships before the web UI (M2) so the graph is useful day one.
5. **`agent/`** — LangGraph agent: plan → tool loop → answer with citations (`repo/file:line`). Model-agnostic configuration, Anthropic by default.
6. **`server/` + `web/`** — FastAPI with an SSE `/chat` endpoint serving the built React UI. UI follows the Timeplus console design language. No auth in v1.

## Data model

```sql
CREATE MUTABLE STREAM kg_nodes (
  id string,               -- stable hash: repo + qualified_name + kind
  repo string,
  kind string,             -- function | class | file | module | config | concept | doc_section ...
  name string,
  qualified_name string,
  file_path string,
  line_start uint32,
  line_end uint32,
  summary string,          -- graphify's extracted description / signature
  community string,        -- Leiden cluster label from graphify
  visibility string,       -- 'internal' | 'public'  (reserved for v2 tier)
  updated_at datetime64
) PRIMARY KEY (id);

CREATE MUTABLE STREAM kg_edges (
  src string, dst string,
  rel string,              -- calls | imports | inherits | defines | documents | deploys ...
  confidence string,       -- EXTRACTED | INFERRED (graphify's tags)
  repo string,
  updated_at datetime64
) PRIMARY KEY (src, dst, rel);

CREATE STREAM kg_ingest_log (   -- append-only audit of ingest runs
  repo string, run_id string, nodes uint64, edges uint64,
  git_sha string, status string
);
```

**Source code text is not stored in Timeplus.** The graph holds structure and locations; the `read_source` tool reads files from the local repo checkouts, so quotes always match the checked-out SHA. `kg_ingest_log.git_sha` records what SHA each repo was indexed at.

## Data flow

**Ingest:** `tpk ingest --repo proton` → graphify extract → transform graph.json to rows → batch INSERT over HTTP (8123) → on success, delete rows for that repo with `updated_at` older than the run start (removes nodes for deleted code) → append a row to `kg_ingest_log`. Repos are independent: a failure in one leaves its previous graph intact and does not block others.

**Query (agent answering):** e.g. "why does a materialized view checkpoint fail on k8s restart?"
1. `search_entities("materialized view checkpoint")` — name/summary match over `kg_nodes`.
2. `neighbors(id, rel=?, depth≤3)` — pull the local subgraph; BFS in `tools/` with batched SQL; caller can filter by `confidence`.
3. `read_source(file_path, line_range)` — quote actual code when needed.
4. `path_between(a, b)` — for "how does X connect to Z" questions.
5. Agent composes an answer; every factual claim must carry a `repo/file:line` citation.

## Tool inventory (v1)

| Tool | Signature (informal) | Purpose |
|---|---|---|
| `search_entities` | (query, kinds?, repos?, limit) | Find candidate nodes by name/summary match |
| `get_entity` | (id) | Full node record |
| `neighbors` | (id, rels?, direction?, depth≤3, confidence?) | Local subgraph via BFS |
| `path_between` | (id_a, id_b, max_depth) | Shortest connection between two entities |
| `list_communities` | (repo?) | Cluster overview for orientation questions |
| `read_source` | (repo, file_path, line_start, line_end) | Quote code/doc text from local checkout |

## Error handling

- **Ingest:** per-repo isolation; failed extract → `status='failed'` in `kg_ingest_log`, previous graph untouched; stale-node deletion only after successful extract.
- **Agent:** tool errors return structured error strings so the LLM can retry or degrade. Hard caps: depth ≤ 3, fan-out ≤ 200 nodes/hop, ≤ 15 tool calls per turn. If an answer cannot be grounded in graph results, the agent must say so — the system prompt requires citations for factual claims.
- **Server:** SSE sends explicit `error` events; UI shows them with a retry action.

## Testing

- **`tools/`:** unit tests against a fixture graph loaded into a test Timeplus instance (proton docker image in CI) — search, BFS depth/caps, path-finding, visibility filtering.
- **`ingest/`:** golden-file test — tiny sample repo in-tree → assert expected node/edge rows.
- **`agent/`:** ~20-question eval set (devops, architecture, code) judged on "cited real files, called sensible tools"; run manually per release.
- **`server/`:** FastAPI TestClient smoke tests on `/chat`.

## Milestones

1. **M1 — Graph in Timeplus:** ingest works for `docs` + `timeplus-cli` (small repos first); streams queryable by hand.
2. **M2 — Tools + MCP:** tools package + MCP server usable from Claude Code. *First useful deliverable.*
3. **M3 — Full corpus:** proton and remaining repos ingested; traversal performance validated at scale.
4. **M4 — Agent + Web UI:** LangGraph agent, FastAPI server, React chat with citations.

## Out of scope (v2 candidates)

- Auth (SSO or tokens) and the customer tier (activates the `visibility` filter + answer sanitization)
- Scheduled or CI-triggered re-indexing
- Materialized views / UDF-based graph analytics inside Timeplus
- Expanding the corpus beyond the 9 v1 repos
