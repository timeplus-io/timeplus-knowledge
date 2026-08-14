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

# --- docker image builds (per target) ----------------------------------------

docker-build-app: ## Build the app image (pure-Python tpk; no timeplusd)
	docker build -f deploy/docker/Dockerfile --target app -t timeplus/tpk-app:dev .

docker-build-db: ## Build the db image (stock timeplusd + config/user provisioning)
	docker build -f deploy/docker/Dockerfile --target db -t timeplus/tpk-db:dev .

docker-build: ## Build the all-in-one image (timeplusd + tpk)
	docker build -f deploy/docker/Dockerfile --target allinone -t $(IMAGE) .

# --- hygiene -----------------------------------------------------------------

clean: ## Remove local scratch (graphify output, __pycache__)
	rm -rf .graphify_out
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +

.PHONY: help sync db-up db-down db-logs test test-unit ingest ingest-repo \
        status mcp mcp-register serve web-build web-dev up down compose-ingest \
        compose-status up-allinone down-allinone docker-build-app \
        docker-build-db docker-build clean
