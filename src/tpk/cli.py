"""tpk command-line interface."""

import os
from pathlib import Path

import typer

from tpk import corpus, db
from tpk.config import Settings, entry_key, load_llm
from tpk.ingest import IngestResult, ingest_repo

app = typer.Typer(help="Timeplus knowledge graph toolkit")

REPOS_TOML = Path(__file__).resolve().parents[2] / "repos.toml"


@app.command()
def ingest(
    repo: str = typer.Option(
        None, help="Corpus entry name or name@ref (e.g. repo@v1.0.0); omit for all"
    ),
    repos_file: Path = typer.Option(REPOS_TOML, help="Path to repos.toml"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Stream graphify's own output live"
    ),
):
    """Run graphify on repo checkouts and upsert the graph into Timeplus."""
    settings = Settings.from_env()
    client = db.get_client(settings)
    prefix = os.environ.get("TPK_STREAM_PREFIX", "")
    db.ensure_schema(client, prefix)
    corpus.seed_from_toml(client, repos_file, prefix=prefix)
    llm = load_llm(repos_file)
    backend = None if llm.backend == "auto" else llm.backend
    model = llm.model or None
    entries = corpus.list_entries(client, prefix=prefix)
    if repo:
        entries = [e for e in entries if e.name == repo or entry_key(e) == repo]
        if not entries:
            raise typer.BadParameter(f"no corpus entry matches {repo!r}")
    for i, cfg in enumerate(entries, 1):
        typer.echo(f"[{i}/{len(entries)}] {entry_key(cfg)}: extracting ({cfg.extraction})...")
        result = ingest_repo(
            client,
            cfg,
            prefix=prefix,
            backend=backend,
            model=model,
            token_budget=llm.token_budget,
            stream=verbose,
        )
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

    from tpk import auth as auth_mod
    from tpk.server import create_app

    # Wait for timeplusd rather than crash-looping when the agent container
    # starts before the DB is ready (see db.connect_with_retry).
    client = db.connect_with_retry(
        Settings.from_env(),
        timeout_s=float(os.environ.get("TPK_DB_WAIT_SECONDS", "60")),
    )
    db.ensure_schema(client)
    if REPOS_TOML.exists():
        corpus.seed_from_toml(client, REPOS_TOML)
    # Eager bootstrap (issue #6): seed the admin user before uvicorn starts
    # serving. `serve` here always runs as a single uvicorn process (no
    # `workers=` argument), so the list_users-then-upsert_user race in
    # seed_admin() cannot happen between two processes of this command; and
    # even if it somehow raced, kg_users' PRIMARY KEY makes a double-seed of
    # the same "admin" row converge to one final row anyway.
    auth_mod.seed_admin(client)
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    app()
