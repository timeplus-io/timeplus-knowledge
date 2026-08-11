"""tpk command-line interface."""

from pathlib import Path

import typer

from tpk import db
from tpk.config import Settings, load_llm, load_repos
from tpk.ingest import ingest_repo

app = typer.Typer(help="Timeplus knowledge graph toolkit")

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"


@app.command()
def ingest(
    repo: str = typer.Option(None, help="Repo name from repos.toml; omit for all"),
    repos_file: Path = typer.Option(REPOS_TOML, help="Path to repos.toml"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Stream graphify's own output live"
    ),
):
    """Run graphify on repo checkouts and upsert the graph into Timeplus."""
    settings = Settings.from_env()
    client = db.get_client(settings)
    db.ensure_schema(client)
    repos = load_repos(repos_file)
    llm = load_llm(repos_file)
    backend = None if llm.backend == "auto" else llm.backend
    model = llm.model or None
    if repo and repo not in repos:
        raise typer.BadParameter(f"unknown repo {repo!r}; known: {sorted(repos)}")
    targets = [repos[repo]] if repo else list(repos.values())
    for i, cfg in enumerate(targets, 1):
        typer.echo(f"[{i}/{len(targets)}] {cfg.name}: extracting ({cfg.extraction})...")
        result = ingest_repo(client, cfg, backend=backend, model=model, stream=verbose)
        typer.echo(f"{result.repo}: {result.status} ({result.nodes} nodes, {result.edges} edges)")


@app.command()
def status():
    """Show the latest ingest run per repo."""
    settings = Settings.from_env()
    client = db.get_client(settings)
    rows = client.query(
        "SELECT repo, max(_tp_time) AS last_run, arg_max(status, _tp_time) AS status,"
        " arg_max(nodes, _tp_time) AS nodes, arg_max(edges, _tp_time) AS edges"
        " FROM table(kg_ingest_log) GROUP BY repo ORDER BY repo"
    ).result_rows
    for repo, last_run, status_, nodes, edges in rows:
        typer.echo(f"{repo:35s} {status_:7s} {nodes:>8} nodes {edges:>8} edges  {last_run}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address"),
    port: int = typer.Option(8000, help="Port"),
):
    """Run the knowledge agent chat server (SSE /chat + web UI)."""
    import uvicorn

    from tpk.server import create_app

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    app()
