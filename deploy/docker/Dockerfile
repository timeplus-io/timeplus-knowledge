# All-in-one image: Timeplus Enterprise (timeplusd) + the tpk knowledge-graph
# toolkit (ingest CLI, tools, MCP server, chat agent + web UI). timeplusd
# remains the container's main process via the base entrypoint; tpk is used
# via `docker exec` (or as the compose `agent` service's entrypoint, for
# `tpk serve`).
#
#   docker build -f deploy/docker/Dockerfile -t timeplus/tpk:dev .
#   docker run -d --name tpk -p 8123:8123 -p 3218:3218 \
#     -v ~/Code/timeplus:/repos:ro timeplus/tpk:dev
#   docker exec tpk tpk ingest

# Build the React chat UI separately so the final image doesn't need node.
FROM node:22-alpine AS webbuild
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM docker.timeplus.com/timeplus/timeplusd:latest

USER root

# git: used by ingest to record each repo's HEAD SHA in kg_ingest_log.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# uv provisions Python 3.11 for tpk (base image only has system Python 3.10,
# which stays untouched).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python

WORKDIR /opt/tpk
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv python install 3.11 \
    && uv sync --frozen --no-dev --python 3.11 \
    && chmod -R a+rX /opt/uv/python

# Prebuilt web UI, served by `tpk serve` (server.py resolves WEB_DIST as
# parents[2]/web/dist relative to src/tpk/server.py -> /opt/tpk/web/dist).
COPY --from=webbuild /web/dist /opt/tpk/web/dist

# Corpus config with paths under /repos (mounted at runtime) and the
# nativelog small-segments override (2GB/stream preallocation is a disk
# killer on dev machines).
COPY deploy/docker/repos.container.toml /opt/tpk/repos.toml
COPY deploy/timeplusd-dev/small-segments.yaml /etc/timeplusd-server/config.d/small-segments.yaml

# Wrappers on PATH. The venv bin dir is prepended so run_graphify's
# subprocess finds the `graphify` CLI.
RUN printf '#!/bin/sh\nPATH=/opt/tpk/.venv/bin:$PATH exec /opt/tpk/.venv/bin/tpk "$@"\n' > /usr/local/bin/tpk \
    && printf '#!/bin/sh\nPATH=/opt/tpk/.venv/bin:$PATH exec /opt/tpk/.venv/bin/python -m tpk.mcp_server "$@"\n' > /usr/local/bin/tpk-mcp \
    && chmod +x /usr/local/bin/tpk /usr/local/bin/tpk-mcp \
    && mkdir -p /opt/tpk/.checkouts \
    && chown -R 101:101 /opt/tpk

# tpk inside the container talks to its own timeplusd (password defaults to
# empty in tpk.config.Settings; set TIMEPLUS_PASSWORD at runtime if needed).
# PYTHONUNBUFFERED keeps tpk's progress lines live through `docker exec -T`.
# TPK_CHECKOUT_DIR is where github-sourced repos are fetched (shared with
# the agent service via the tpk-checkouts volume so read_source can quote).
ENV TIMEPLUS_HOST=localhost \
    TIMEPLUS_USER=default \
    PYTHONUNBUFFERED=1 \
    TPK_CHECKOUT_DIR=/opt/tpk/.checkouts

# timeplusd's own ports (8123, 3218) are declared by the base image;
# 8000 is `tpk serve` (chat agent + web UI), used by the compose `agent`
# service which runs this same image with entrypoint tpk.
EXPOSE 8000

USER 101:101
