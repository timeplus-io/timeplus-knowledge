# timeplus-knowledge

Knowledge graph + agent tooling for answering questions about Timeplus —
code, design, architecture, and devops. See
`docs/superpowers/specs/2026-08-10-timeplus-knowledge-agent-design.md`.

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and a running
Timeplus Enterprise (mutable streams are an Enterprise feature):

    docker run -d --name timeplusd -p 8123:8123 -p 3218:3218 \
      -v $(pwd)/deploy/timeplusd-dev/small-segments.yaml:/etc/timeplusd-server/config.d/small-segments.yaml:ro \
      docker.timeplus.com/timeplus/timeplusd:latest
    uv sync
    export TIMEPLUS_HOST=localhost TIMEPLUS_USER=default TIMEPLUS_PASSWORD=

The `-v` mount is required on a laptop: the default timeplusd preallocates
2GB of nativelog per stream shard, which fills a Docker VM's disk fast once
you have a handful of streams. `deploy/timeplusd-dev/small-segments.yaml`
overrides this to 64MB segments with no preallocation for local/dev use —
do not use it in production.

## Ingest

Corpus lives in `repos.toml` (repo name -> local checkout path -> visibility).

    uv run tpk ingest                # all repos
    uv run tpk ingest --repo docs    # one repo
    uv run tpk status                # last run per repo

Ingest is per-repo isolated: a failing repo logs `failed` in
`kg_ingest_log` and leaves its previous graph untouched.

Ingest runs `graphify extract <repo> --code-only --out <dir>` (package
`graphifyy` on PyPI, CLI `graphify`) and reads
`<dir>/graphify-out/graph.json`. `--code-only` is hardcoded in
`run_graphify` so ingest never requires an LLM API key, but it also means
non-code files (markdown docs, etc.) are skipped entirely — a repo that is
mostly prose (rather than a doc site's source code) can come back with
few or zero nodes. This is expected for M1 and not treated as a failure as
long as `graphify` itself exits 0.

### M1 smoke run (2026-08-10)

Ran against real checkouts on this machine:

    docs           ok   163 nodes   151 edges
    timeplus-cli   ok   473 nodes   839 edges

Both reported `ok` via `tpk status`, and `SELECT count() FROM table(kg_nodes)`
matches the parsed counts exactly per repo (163 + 473 = 636 stored rows;
edges likewise 151 + 839 = 990). `kg_nodes` is a mutable stream keyed on a
stable id derived from `(repo, kind, qualified_name)`; `qualified_name` for
`file`-kind nodes (graphify maps every non-callable "code" node to `file`,
not just the one node that is literally the file — see `graphify_runner.py`)
is disambiguated with graphify's own per-node id, which is guaranteed unique
within a `graph.json`, so distinct entities in the same source file no
longer collapse onto one row. (An earlier ingest run, before this fix, saw
parsed counts of 166/473 collapse to only 91/99 stored rows — root-caused
and fixed; see `tests/test_graphify_runner.py::test_file_kind_nodes_in_same_file_get_distinct_ids`.)

## Use from Claude Code (MCP)

    claude mcp add timeplus-knowledge -- uv --directory /Users/gangtao/Code/timeplus/timeplus-knowledge run python -m tpk.mcp_server

Tools: `search_entities`, `get_entity`, `neighbors`, `path_between`,
`list_communities`, `read_source`.

Built on the `mcp` SDK 2.0 (`FastMCP`); verify registration with
`claude mcp list` and exercise the tools by asking Claude Code a question
that should trigger `search_entities` (e.g. "using the timeplus-knowledge
tools, what does the docs repo say about Quickstart?").

## Tests

    uv run pytest                    # integration tests skip without TIMEPLUS_HOST
    TIMEPLUS_HOST=localhost uv run pytest

Tests marked as requiring Timeplus are skipped automatically when
`TIMEPLUS_HOST` is unset (see `tests/conftest.py`); set it (plus
`TIMEPLUS_USER`/`TIMEPLUS_PASSWORD` if not using the `default` user with an
empty password) to run the full suite against a live timeplusd.

## Manual queries

Every hand query against the graph goes through `table(...)`, e.g.:

    echo "SELECT kind, count() FROM table(kg_nodes) GROUP BY kind ORDER BY count() DESC" | \
      curl "http://${TIMEPLUS_HOST}:8123/" -u "${TIMEPLUS_USER:-default}:${TIMEPLUS_PASSWORD:-}" --data-binary @-
