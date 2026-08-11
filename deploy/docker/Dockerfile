# All-in-one image: Timeplus Enterprise (timeplusd) + the tpk knowledge-graph
# toolkit (ingest CLI, tools, MCP server). timeplusd remains the container's
# main process via the base entrypoint; tpk is used via `docker exec`.
#
#   docker build -f deploy/docker/Dockerfile -t timeplus/tpk:dev .
#   docker run -d --name tpk -p 8123:8123 -p 3218:3218 \
#     -v ~/Code/timeplus:/repos:ro timeplus/tpk:dev
#   docker exec tpk tpk ingest
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
    && chown -R 101:101 /opt/tpk

# tpk inside the container talks to its own timeplusd (password defaults to
# empty in tpk.config.Settings; set TIMEPLUS_PASSWORD at runtime if needed).
# PYTHONUNBUFFERED keeps tpk's progress lines live through `docker exec -T`.
ENV TIMEPLUS_HOST=localhost \
    TIMEPLUS_USER=default \
    PYTHONUNBUFFERED=1

USER 101:101
