# tpk Docker Image — Design

**Date:** 2026-08-10
**Status:** Approved in session

## Purpose

One container that runs Timeplus Enterprise (`timeplusd`) unchanged and ships
the `tpk` toolkit (ingest CLI, KnowledgeGraph tools, MCP server) baked in —
an all-in-one dev/demo image for the knowledge graph.

## Shape

- **Base:** `docker.timeplus.com/timeplus/timeplusd:latest` (Ubuntu 22.04,
  entrypoint `/entrypoint.sh`, user `101:101`, system Python 3.10).
- **timeplusd stays the main process**; the base entrypoint/cmd are not
  modified. tpk is used via `docker exec`.
- **Python:** tpk needs ≥3.11; the image installs `uv` (copied from the
  official `ghcr.io/astral-sh/uv` image) and provisions a managed Python 3.11
  into a project venv at `/opt/tpk/.venv` (`uv sync --frozen --no-dev`).
  System Python is untouched.
- **Wrappers on PATH:** `/usr/local/bin/tpk` (venv PATH prepended so the
  `graphify` CLI resolves) and `/usr/local/bin/tpk-mcp` (runs
  `python -m tpk.mcp_server` over stdio).
- **Config baked in:** `small-segments.yaml` → `/etc/timeplusd-server/config.d/`
  (no host mount needed); `deploy/docker/repos.container.toml` →
  `/opt/tpk/repos.toml` with repo paths under `/repos/<name>` (same 9 repos,
  descriptions, visibility, extraction modes as the host repos.toml).
- **Repos are mounted at runtime:** `-v ~/Code/timeplus:/repos:ro`. Read-only
  is preferred (blocks graphify's cache droppings in checkouts); if graphify
  requires a writable scan dir, this is verified during testing and the docs
  say to drop `:ro`.
- **Ownership/permissions:** `/opt/tpk` chowned to `101:101` so ingest scratch
  (`.graphify_out`) and exec-time work run as the image's default user;
  `WORKDIR /opt/tpk` at the end.
- **Env defaults:** `TIMEPLUS_HOST=localhost`, `TIMEPLUS_USER=default`,
  `TIMEPLUS_PASSWORD=` — tpk inside the container talks to its own timeplusd.
  LLM keys (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) pass at `docker run -e`.
- **Graph starts empty**; ingest runs at runtime (checkouts are not in the
  build context).

## Files

- `deploy/docker/Dockerfile`
- `deploy/docker/repos.container.toml`
- `.dockerignore` (excludes `.venv`, `.git`, `.graphify_out`, `docs`, caches)
- README section "Docker image"

## Usage

```bash
docker build -f deploy/docker/Dockerfile -t timeplus/tpk:dev .
docker run -d --name tpk -p 8123:8123 -p 3218:3218 \
  -v ~/Code/timeplus:/repos:ro timeplus/tpk:dev
docker exec tpk tpk ingest                # build the graph inside
docker exec tpk tpk status
claude mcp add timeplus-knowledge -- docker exec -i tpk tpk-mcp
```

## Verification

Build the image; run it on alternate host ports (existing local timeplusd
keeps 8123); `docker exec` ingest of a small mounted code repo succeeds
read-only; `tpk status` shows the run; MCP stdio smoke (initialize +
tools/list) via `docker exec -i ... tpk-mcp`.
