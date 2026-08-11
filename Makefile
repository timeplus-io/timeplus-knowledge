# Dev commands for timeplus-knowledge. Run `make` (or `make help`) to list.

.DEFAULT_GOAL := help

TIMEPLUS_HOST ?= localhost
IMAGE         ?= timeplus/tpk:dev
DB_CONTAINER  ?= timeplusd
TPK_CONTAINER ?= tpk
REPOS_MOUNT   ?= $(HOME)/Code/timeplus
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

# --- docker compose ----------------------------------------------------------

up: ## Start the all-in-one stack via docker compose (.env for API keys)
	docker compose up -d

down: ## Stop the compose stack (named data volume is kept)
	docker compose down

# --- all-in-one docker image -------------------------------------------------

docker-build: ## Build the all-in-one image (timeplusd + tpk)
	docker build -f deploy/docker/Dockerfile -t $(IMAGE) .

docker-run: ## Run the all-in-one image with repos mounted read-only
	docker run -d --name $(TPK_CONTAINER) -p 8123:8123 -p 3218:3218 \
	  -v $(REPOS_MOUNT):/repos:ro $(IMAGE)

docker-ingest: ## Ingest inside the running all-in-one container
	docker exec $(TPK_CONTAINER) tpk ingest

docker-status: ## tpk status inside the running all-in-one container
	docker exec $(TPK_CONTAINER) tpk status

docker-stop: ## Stop and remove the all-in-one container (DELETES its data volume)
	docker rm -f -v $(TPK_CONTAINER)

# --- hygiene -----------------------------------------------------------------

clean: ## Remove local scratch (graphify output, __pycache__)
	rm -rf .graphify_out
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +

.PHONY: help sync db-up db-down db-logs test test-unit ingest ingest-repo \
        status mcp mcp-register serve web-build web-dev up down docker-build \
        docker-run docker-ingest docker-status docker-stop clean
