# Multi-stage build for the two deployment modes (issue #48). Two targets:
#
#   app       pure-Python tpk (serve + ingest + MCP); no DB. Talks to a
#             separate DB over the network (TIMEPLUS_HOST). Small.
#   allinone  OSS proton + tpk in one container (serve + toolkit), fully
#             open-source, TPK_DB_BACKEND=proton. For tests / quick local runs.
#
# Build a specific image with --target, e.g.:
#   docker build -f deploy/docker/Dockerfile --target app      -t timeplus/tpk-app:dev .
#   docker build -f deploy/docker/Dockerfile --target allinone -t timeplus/tpk:dev     .
#
# docker-compose.yml uses stock timeplusd for its db service (no build) and
# builds the app target; docker-compose.allinone.yml builds the allinone target.

# --- shared: React chat UI (so runtime images don't need node) ---------------
FROM node:22-alpine AS webbuild
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- shared: resolve the tpk Python venv once, reused by app + allinone -------
# uv provisions a standalone Python 3.11 into a fixed, relocatable path so the
# resulting venv works on both the slim app base and the timeplusd base image.
FROM debian:12-slim AS pybuild
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
WORKDIR /opt/tpk
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv python install 3.11 \
    && uv sync --frozen --no-dev --python 3.11 \
    && chmod -R a+rX /opt/uv/python

# ============================================================================
# Target: app  — pure-Python tpk. No timeplusd.
# ============================================================================
FROM debian:12-slim AS app
# git: ingest records each repo's HEAD SHA in kg_ingest_log.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/tpk
COPY --from=pybuild /opt/uv/python /opt/uv/python
COPY --from=pybuild /opt/tpk /opt/tpk
COPY --from=webbuild /web/dist /opt/tpk/web/dist
COPY deploy/docker/repos.container.toml /opt/tpk/repos.toml
RUN printf '#!/bin/sh\nPATH=/opt/tpk/.venv/bin:$PATH exec /opt/tpk/.venv/bin/tpk "$@"\n' > /usr/local/bin/tpk \
    && printf '#!/bin/sh\nPATH=/opt/tpk/.venv/bin:$PATH exec /opt/tpk/.venv/bin/python -m tpk.mcp_server "$@"\n' > /usr/local/bin/tpk-mcp \
    && chmod +x /usr/local/bin/tpk /usr/local/bin/tpk-mcp \
    && mkdir -p /opt/tpk/.checkouts
# TIMEPLUS_HOST is overridden at runtime to point at the db service.
ENV TIMEPLUS_HOST=localhost \
    TIMEPLUS_USER=default \
    PYTHONUNBUFFERED=1 \
    TPK_CHECKOUT_DIR=/opt/tpk/.checkouts
EXPOSE 8000
ENTRYPOINT ["tpk"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]

# ============================================================================
# Target: allinone  — OSS proton + tpk in one container (serve + toolkit).
# Fully open-source: bundles proton (not Enterprise timeplusd) and runs the
# agent against it via TPK_DB_BACKEND=proton (versioned_kv keyed streams).
# ============================================================================
FROM ghcr.io/timeplus-io/proton:latest AS allinone
USER root
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/tpk
COPY --from=pybuild /opt/uv/python /opt/uv/python
COPY --from=pybuild /opt/tpk /opt/tpk
COPY --from=webbuild /web/dist /opt/tpk/web/dist
COPY deploy/docker/repos.container.toml /opt/tpk/repos.toml
# proton preallocates nativelog segments too; the same dev override applies
# (data.datastore.log keys are identical), just under proton's config dir.
COPY deploy/timeplusd-dev/small-segments.yaml /etc/proton-server/config.d/small-segments.yaml
COPY deploy/docker/render-users.sh /usr/local/bin/render-users.sh
COPY deploy/docker/allinone-entrypoint.sh /usr/local/bin/allinone-entrypoint.sh
RUN chmod +x /usr/local/bin/render-users.sh /usr/local/bin/allinone-entrypoint.sh \
    && chmod u+w /etc/proton-server/users.d \
    && printf '#!/bin/sh\nPATH=/opt/tpk/.venv/bin:$PATH exec /opt/tpk/.venv/bin/tpk "$@"\n' > /usr/local/bin/tpk \
    && printf '#!/bin/sh\nPATH=/opt/tpk/.venv/bin:$PATH exec /opt/tpk/.venv/bin/python -m tpk.mcp_server "$@"\n' > /usr/local/bin/tpk-mcp \
    && chmod +x /usr/local/bin/tpk /usr/local/bin/tpk-mcp \
    && mkdir -p /opt/tpk/.checkouts \
    && chown -R 101:101 /opt/tpk
ENV TIMEPLUS_HOST=localhost \
    TIMEPLUS_USER=default \
    TPK_DB_BACKEND=proton \
    PYTHONUNBUFFERED=1 \
    TPK_CHECKOUT_DIR=/opt/tpk/.checkouts
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/allinone-entrypoint.sh"]
USER 101:101
