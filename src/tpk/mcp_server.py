"""MCP server exposing the Timeplus knowledge graph to Claude Code / Cursor."""

from pathlib import Path

# The installed `mcp` distribution (2.0.0) no longer ships
# `mcp.server.fastmcp.FastMCP`; the equivalent decorator-based server class
# lives at `mcp.server.mcpserver.MCPServer`. Same API surface (`.tool()`,
# `.list_tools()`, `.call_tool()`, `.run()`) that the rest of this module
# relies on.
from mcp.server.mcpserver import MCPServer as FastMCP

from tpk import corpus, db
from tpk.config import Settings, load_repos
from tpk.config import repo_paths as resolved_repo_paths
from tpk.tools import KnowledgeGraph

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"


def build_server(kg) -> FastMCP:
    server = FastMCP("timeplus-knowledge")

    @server.tool()
    def search_entities(query: str, kinds: list[str] | None = None,
                        repos: list[str] | None = None, limit: int = 20) -> list[dict]:
        """Find code/doc entities in the Timeplus knowledge graph by keyword.
        Every whitespace-separated token must match the entity's name,
        qualified name, or summary (case-insensitive)."""
        return kg.search_entities(query, kinds=kinds, repos=repos, limit=limit)

    @server.tool()
    def get_entity(entity_id: str) -> dict | None:
        """Fetch the full record for one entity by its id."""
        return kg.get_entity(entity_id)

    @server.tool()
    def neighbors(entity_id: str, rels: list[str] | None = None, direction: str = "both",
                  depth: int = 1, confidence: str | None = None) -> dict:
        """Local subgraph around an entity (BFS, depth capped at 3).
        direction: out|in|both. confidence: EXTRACTED|INFERRED to filter edges."""
        return kg.neighbors(entity_id, rels=rels, direction=direction,
                            depth=depth, confidence=confidence)

    @server.tool()
    def path_between(id_a: str, id_b: str, max_depth: int = 4) -> list[dict] | None:
        """Shortest connection between two entities, or null if none within max_depth."""
        return kg.path_between(id_a, id_b, max_depth=max_depth)

    @server.tool()
    def list_communities(repo: str | None = None) -> list[dict]:
        """Cluster overview: (repo, community, node_count), largest first."""
        return kg.list_communities(repo=repo)

    @server.tool()
    def read_source(repo: str, file_path: str, line_start: int, line_end: int) -> str:
        """Read exact lines from a repo checkout so answers can quote real code."""
        return kg.read_source(repo, file_path, line_start, line_end)

    return server


def main() -> None:
    settings = Settings.from_env()
    client = db.get_client(settings)
    db.ensure_schema(client)
    corpus.seed_from_toml(client, REPOS_TOML)
    repos = load_repos(REPOS_TOML)
    kg = KnowledgeGraph(client, repo_paths=resolved_repo_paths(repos))
    build_server(kg).run()  # stdio transport


if __name__ == "__main__":
    main()
