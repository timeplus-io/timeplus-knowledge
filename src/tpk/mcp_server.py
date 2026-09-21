"""MCP server exposing the Timeplus knowledge graph to Claude Code / Cursor."""

# The installed `mcp` distribution (2.0.0) no longer ships
# `mcp.server.fastmcp.FastMCP`; the equivalent decorator-based server class
# lives at `mcp.server.mcpserver.MCPServer`. Same API surface (`.tool()`,
# `.list_tools()`, `.call_tool()`, `.run()`) that the rest of this module
# relies on.
import sys

import anyio.to_thread
from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as FastMCP

from tpk import corpus, db
from tpk.config import Settings, config_path, load_repos
from tpk.config import repo_paths as resolved_repo_paths
from tpk.tools import KnowledgeGraph
from tpk.version import get_version

REPOS_TOML = config_path()


async def _unguarded(ctx, fn, *, tool: str, needs_source: bool = False):
    """stdio: local, single-user, unrestricted -- run the KG call as-is, but
    off the event loop: the tools are async, and blocking DB/file work on the
    loop thread would stall the whole stdio server for its duration."""
    return await anyio.to_thread.run_sync(fn)


def build_server(kg, guard=None) -> FastMCP:
    """`guard` wraps every KG call: `await guard(ctx, fn, tool=..., needs_source=...)`.
    None (stdio) runs it unrestricted; the remote HTTP endpoint passes
    mcp_http.make_guard(), which applies the caller's role scope and the
    source:view gate (#74)."""
    run = guard or _unguarded
    server = FastMCP("timeplus-knowledge", version=get_version()[0])

    @server.tool()
    async def search_entities(ctx: Context, query: str, kinds: list[str] | None = None,
                              repos: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Find code/doc entities in the Timeplus knowledge graph by keyword.
        Every whitespace-separated token must match the entity's name,
        qualified name, or summary (case-insensitive). `limit` is capped at 200."""
        return await run(ctx, lambda: kg.search_entities(query, kinds=kinds, repos=repos, limit=limit),
                         tool="search_entities")

    @server.tool()
    async def get_entity(ctx: Context, entity_id: str) -> dict | None:
        """Fetch the full record for one entity by its id."""
        return await run(ctx, lambda: kg.get_entity(entity_id), tool="get_entity")

    @server.tool()
    async def neighbors(ctx: Context, entity_id: str, rels: list[str] | None = None,
                        direction: str = "both", depth: int = 1,
                        confidence: str | None = None) -> dict:
        """Local subgraph around an entity (BFS, depth capped at 3).
        direction: out|in|both. confidence: EXTRACTED|INFERRED to filter edges."""
        return await run(ctx, lambda: kg.neighbors(entity_id, rels=rels, direction=direction,
                                                   depth=depth, confidence=confidence),
                         tool="neighbors")

    @server.tool()
    async def path_between(ctx: Context, id_a: str, id_b: str, max_depth: int = 8,
                           mode: str = "auto") -> dict:
        """How are two entities connected? Returns {found, mode, direction, path,
        note}. mode="auto" (default) looks for a DIRECTED CALL CHAIN first
        (A calls ... calls B, or the reverse -- `direction` says which; the
        path is listed caller first), then falls back to a "related" path
        over other relations, which is NOT a call chain -- always check
        `mode` before describing the result. mode="calls" / "related" force
        one. When found is false, `callees_of_a` / `callers_of_b` list the
        direct calls at each end: the extracted call graph is incomplete
        (C++ virtual dispatch, untyped member calls), so continue from those
        with neighbors() and read_source rather than concluding "no
        connection". max_depth is capped at 12."""
        return await run(ctx, lambda: kg.path_between(id_a, id_b, max_depth=max_depth, mode=mode),
                         tool="path_between")

    @server.tool()
    async def list_communities(ctx: Context, repo: str | None = None,
                               limit: int | None = None, min_nodes: int = 1) -> dict:
        """Cluster overview, largest first. BOUNDED: returns at most `limit`
        communities (default 50, max 200) with at least `min_nodes` nodes, as
        {communities, total, returned, truncated, by_repo}. Check `truncated`
        / `total`; pass `repo` (an entry key from `by_repo`) to drill into one
        repo, raise `min_nodes` to skip tiny clusters. A community id is an
        opaque number unique only within its repo -- read `top_dirs` /
        `top_files` to see what a cluster is about."""
        return await run(ctx, lambda: kg.list_communities(repo=repo, limit=limit, min_nodes=min_nodes),
                         tool="list_communities")

    @server.tool()
    async def read_source(ctx: Context, repo: str, file_path: str, line_start: int, line_end: int) -> str:
        """Read exact lines from a repo checkout so answers can quote real code."""
        return await run(ctx, lambda: kg.read_source(repo, file_path, line_start, line_end),
                         tool="read_source", needs_source=True)

    return server


def main() -> None:
    settings = Settings.from_env()
    try:
        client = db.get_client(settings)
        db.ensure_schema(client, settings.stream_prefix)
    except Exception as exc:
        # An MCP client shows a crashed stdio server only as "Connection
        # closed" and hides the traceback -- so say why in ONE line (#77).
        # The driver's first line is generic; the server's own "Code: ..." line
        # (e.g. "Authentication failed") is the one worth showing.
        lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
        reason = next((ln for ln in lines if "DB::Exception" in ln),
                      lines[0] if lines else type(exc).__name__)[:240]
        print(
            f"tpk-mcp: cannot use Timeplus at {settings.host}:{settings.port} as user "
            f"{settings.user!r}: {reason} -- set TIMEPLUS_HOST / TIMEPLUS_USER / "
            f"TIMEPLUS_PASSWORD for this server (claude mcp add ... -e TIMEPLUS_USER=... "
            f"-e TIMEPLUS_PASSWORD=...), or use the remote /mcp endpoint instead.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Same stream prefix as `tpk serve` and the rest of the CLI (#84).
    prefix = settings.stream_prefix
    corpus.seed_from_toml(client, REPOS_TOML, prefix=prefix)
    repos = load_repos(REPOS_TOML)
    kg = KnowledgeGraph(client, stream_prefix=prefix, repo_paths=resolved_repo_paths(repos))
    build_server(kg).run()  # stdio transport


if __name__ == "__main__":
    main()
