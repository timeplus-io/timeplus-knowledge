# Dev commands for timeplus-knowledge. Run `make` (or `make help`) to list.

.DEFAULT_GOAL := help

TIMEPLUS_HOST ?= localhost
IMAGE         ?= timeplus/tpk:dev
DB_CONTAINER  ?= timeplusd
REPO          ?=

help: ## Show available targets
	@grep -E '^[a-zA-Z][a-zA-Z0-9_-]*:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- environment -------------------------------------------------------------

sync: ## Install/sync Python dependencies (uv)
	uv sync

db-up: ## Start local timeplusd with the dev small-segments override
	docker run -d --name $(DB_CONTAINER) -p 8123:8123 -p 3218:3218 \
	  -v $(PWD)/deploy/timeplusd-dev/small-segments.yaml:/etc/timeplusd-server/config.d/small-segments.yaml:ro \
	  docker.timeplus.com/timeplus/timeplusd:latest

db-down: ## Stop and remove local timeplusd (DELETES its data volume)
	docker rm -f -v $(DB_CONTAINER)

db-logs: ## Tail local timeplusd logs
	docker logs -f $(DB_CONTAINER)

# --- tests -------------------------------------------------------------------

test: ## Run the full test suite against local Timeplus
	TIMEPLUS_HOST=$(TIMEPLUS_HOST) uv run pytest -q

test-unit: ## Run only unit tests (integration tests skip without TIMEPLUS_HOST)
	uv run pytest -q

# --- knowledge graph ---------------------------------------------------------

ingest: ## Ingest all repos from repos.toml
	TIMEPLUS_HOST=$(TIMEPLUS_HOST) uv run tpk ingest

ingest-repo: ## Ingest one repo: make ingest-repo REPO=docs
	@test -n "$(REPO)" || { echo "usage: make ingest-repo REPO=<name>"; exit 1; }
	TIMEPLUS_HOST=$(TIMEPLUS_HOST) uv run tpk ingest --repo $(REPO)

status: ## Show the last ingest run per repo
	TIMEPLUS_HOST=$(TIMEPLUS_HOST) uv run tpk status

# --- MCP ---------------------------------------------------------------------

mcp: ## Run the MCP server on stdio (Ctrl-D to exit)
	TIMEPLUS_HOST=$(TIMEPLUS_HOST) uv run python -m tpk.mcp_server

mcp-register: ## Register the MCP server with Claude Code (this checkout)
	claude mcp add timeplus-knowledge -- uv --directory $(PWD) run python -m tpk.mcp_server

# --- chat agent & web UI ------------------------------------------------------

serve: ## Run the chat server locally (needs agent env + populated graph)
	TIMEPLUS_HOST=$(TIMEPLUS_HOST) uv run tpk serve

web-build: ## Build the React UI into web/dist
	cd web && npm install && npm run build

web-dev: ## Run the Vite dev server (proxies /chat to :8000)
	cd web && npm install && npm run dev

# --- docker compose: DB + App (production-shaped) ----------------------------

up: ## Start the DB + App stack via docker compose (.env for API keys)
	docker compose up -d --build

down: ## Stop the DB + App stack (named data volumes are kept)
	docker compose down

compose-ingest: ## Ingest inside the running app container
	docker compose exec app tpk ingest

compose-status: ## tpk status inside the running app container
	docker compose exec app tpk status

# --- docker compose: all-in-one (single container, test/local) ---------------

ALLINONE ?= docker-compose.allinone.yml

up-allinone: ## Start the single-container all-in-one stack
	docker compose -f $(ALLINONE) up -d --build

down-allinone: ## Stop the all-in-one stack (named data volumes are kept)
	docker compose -f $(ALLINONE) down

# --- docker image builds -----------------------------------------------------
# Two images map to the two deployment modes:
#   all-in-one image  -> all-in-one mode (timeplusd + tpk in one container)
#   app image         -> DB + App mode   (the db service uses stock timeplusd)

APP_IMAGE ?= timeplus/tpk-app:dev

# Local builds target the host arch. Set PLATFORMS for a cross/multi-arch build
# via buildx, e.g.
#   make docker-build-app PLATFORMS=linux/arm64
#   make docker-build PLATFORMS=linux/amd64,linux/arm64 DOCKER_BUILD_FLAGS=--push
# (a multi-platform build can't be loaded into the local daemon; add --push).
PLATFORMS ?=
DOCKER_BUILD_FLAGS ?=
DOCKER_BUILD = docker $(if $(PLATFORMS),buildx build --platform $(PLATFORMS),build) $(DOCKER_BUILD_FLAGS)

docker-build-allinone: ## Build the all-in-one image (proton + tpk); PLATFORMS=... for multi-arch
	$(DOCKER_BUILD) -f deploy/docker/Dockerfile --target allinone -t $(IMAGE) .

docker-build-app: ## Build the tpk app-only image (pure-Python; no timeplusd); PLATFORMS=... for multi-arch
	$(DOCKER_BUILD) -f deploy/docker/Dockerfile --target app -t $(APP_IMAGE) .

docker-build: docker-build-allinone docker-build-app ## Build both deployable images

# --- hygiene -----------------------------------------------------------------

clean: ## Remove local scratch (graphify output, __pycache__)
	rm -rf .graphify_out
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +

.PHONY: help sync db-up db-down db-logs test test-unit ingest ingest-repo \
        status mcp mcp-register serve web-build web-dev up down compose-ingest \
        compose-status up-allinone down-allinone docker-build-allinone \
        docker-build-app docker-build clean
