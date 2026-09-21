"""tpk command-line interface."""

import sys
import time
from pathlib import Path

import typer

from tpk import corpus, db
from tpk.config import Settings, config_path, entry_key, load_llm, setting
from tpk.ingest import IngestResult, ingest_repo

_PHASE_LABEL = {
    "fetch": "fetching…",
    "extract": "extracting…",
    "parse": "parsing…",
    "upsert": "saving…",
    "done": "done",
}


def _fmt_elapsed(seconds: float) -> str:
    """Format elapsed seconds as human-readable string (e.g., '1m30s')."""
    s = int(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s"


def _progress_line(
    i: int, n: int, key: str, phase: str, elapsed_s: float, message: str = ""
) -> str:
    """Format a progress line for ingest (unit-testable pure function).

    Args:
        i: Current entry index (1-based)
        n: Total entries
        key: Corpus entry key (e.g., 'docs@main')
        phase: Ingest phase (e.g., 'extract', 'parse')
        elapsed_s: Elapsed seconds
        message: Optional last graphify message

    Returns:
        Formatted progress string, truncated to 200 chars if needed.
    """
    label = _PHASE_LABEL.get(phase, phase)
    parts = [f"[{i}/{n}] {key}", label, _fmt_elapsed(elapsed_s)]
    if message:
        parts.append(message)
    line = " · ".join(parts)
    return line[:197] + "…" if len(line) > 200 else line

app = typer.Typer(help="Timeplus knowledge graph toolkit")


def _print_version(value: bool) -> None:
    if value:
        from tpk.version import version_string

        typer.echo(f"tpk {version_string()}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_print_version, is_eager=True,
        help="Show the tpk version and exit",
    ),
):
    """Timeplus knowledge graph toolkit"""

REPOS_TOML = config_path()


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
    prefix = setting("TPK_STREAM_PREFIX", "db", "stream_prefix", "")
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
        started = time.monotonic()
        tty = sys.stdout.isatty()
        last_emit = [0.0]

        def _on_progress(p, _i=i, _cfg=cfg, _started=started, _last=last_emit):
            if verbose:
                return  # graphify's own output is already streaming
            elapsed = time.monotonic() - _started
            line = _progress_line(
                _i, len(entries), entry_key(_cfg), p.phase, elapsed, p.message
            )
            if tty:
                sys.stdout.write("\r\033[K" + line)
                sys.stdout.flush()
            elif elapsed - _last[0] >= 5 or p.phase in ("parse", "upsert", "done"):
                _last[0] = elapsed
                typer.echo(line)

        result = ingest_repo(
            client,
            cfg,
            prefix=prefix,
            backend=backend,
            model=model,
            token_budget=llm.token_budget,
            stream=verbose,
            on_progress=_on_progress,
        )
        if tty and not verbose:
            sys.stdout.write("\r\033[K")  # clear the refreshing line
            sys.stdout.flush()
        typer.echo(f"{result.repo}: {result.status} ({result.nodes} nodes, {result.edges} edges)")


@app.command()
def status():
    """Show the latest ingest run per repo."""
    settings = Settings.from_env()
    client = db.get_client(settings)
    rows = client.query(
        "SELECT repo, max(_tp_time) AS last_run, arg_max(status, _tp_time) AS status,"
        " arg_max(nodes, _tp_time) AS nodes, arg_max(edges, _tp_time) AS edges"
        f" FROM table({db.qualified('kg_ingest_log')}) GROUP BY repo ORDER BY repo"
    ).result_rows
    for repo, last_run, status_, nodes, edges in rows:
        typer.echo(f"{repo:35s} {status_:7s} {nodes:>8} nodes {edges:>8} edges  {last_run}")


@app.command()
def export(
    out: Path = typer.Option(..., "--out", "-o", help="Output bundle directory"),
    fmt: str = typer.Option("Parquet", "--format", help="Bundle format: Parquet or Native"),
):
    """Export the ingested graph + corpus registry to a portable bundle."""
    from tpk import transfer

    client = db.get_client(Settings.from_env())
    prefix = setting("TPK_STREAM_PREFIX", "db", "stream_prefix", "")
    manifest = transfer.export_bundle(client, out, prefix=prefix, fmt=fmt)
    for stream, info in manifest["streams"].items():
        typer.echo(f"{stream:16s} {info['rows']:>9} rows -> {info['file']}")
    typer.echo(f"bundle written to {out}")


@app.command(name="import")
def import_bundle(
    src: Path = typer.Argument(..., help="Bundle directory to load"),
    replace: bool = typer.Option(
        False, "--replace", help="Reset target streams before loading (else upsert)"
    ),
):
    """Load a corpus bundle into this environment — no re-ingest."""
    from tpk import transfer

    client = db.get_client(Settings.from_env())
    prefix = setting("TPK_STREAM_PREFIX", "db", "stream_prefix", "")
    manifest = transfer.import_bundle(client, src, prefix=prefix, replace=replace)
    for stream, info in manifest["streams"].items():
        typer.echo(f"{stream:16s} loaded {info['rows']:>9} rows")
    typer.echo("import complete")


@app.command(name="eval")
def eval_agent(
    out: Path = typer.Option(Path("eval-report.json"), "--out", "-o", help="Where to write the JSON report"),
    only: list[str] = typer.Option(None, "--only", help="Run only these categories (repeatable)"),
    limit: int = typer.Option(0, "--limit", help="Run at most N questions (0 = all)"),
    questions: Path = typer.Option(None, "--questions", help="Custom question TOML (default: the bundled set)"),
    compare: Path = typer.Option(None, "--compare", help="A previous report to diff against"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the cost confirmation"),
):
    """Run the fixed eval questions through the REAL chat agent and report how
    it answered: tools called, graph-edge usage, tool-call count, basic checks.

    Every question is a full agent run against the configured LLM (real token
    cost) and the live knowledge graph. Runs unscoped, like an admin."""
    import asyncio
    import json as _json

    from tpk import evals
    from tpk.agent import RECURSION_LIMIT, build_agent
    from tpk.config import AgentConfig
    from tpk.server import _build_kg_and_repos
    from tpk.version import version_string

    qs = evals.load_questions(questions)
    if only:
        qs = [q for q in qs if q.category in only]
    if limit:
        qs = qs[:limit]
    if not qs:
        raise typer.BadParameter("no questions selected")
    cfg = AgentConfig.from_env()
    typer.echo(f"{len(qs)} questions -> {cfg.provider}/{cfg.model} (each is a full agent run; LLM cost applies)")
    if not yes:
        typer.confirm("Run the eval?", abort=True)

    prefix = setting("TPK_STREAM_PREFIX", "db", "stream_prefix", "")
    kg, repos = _build_kg_and_repos(prefix)
    agent = build_agent(kg, cfg, repos)

    def _progress(rec):
        mark = "ok  " if rec["passed"] else "FAIL"
        typer.echo(f"  {mark} {rec['id']:32s} {len(rec['tools']):>2} calls  {rec['latency_s']:>5}s  "
                   + (rec["error"] or " ".join(rec["tools"]))[:90])

    records = asyncio.run(evals.run_eval(agent, qs, recursion_limit=RECURSION_LIMIT, on_result=_progress))
    report = evals.write_report(out, records, meta={
        "tpk": version_string(), "provider": cfg.provider, "model": cfg.model,
        "questions": len(qs), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    typer.echo("")
    typer.echo(evals.render_markdown(report))
    typer.echo(f"\nreport written to {out}")
    if compare:
        diff = evals.compare(_json.loads(Path(compare).read_text()), report)
        typer.echo(f"\nvs {compare}:")
        for k in ("passed", "graph_tool_rate", "avg_tool_calls", "errors"):
            typer.echo(f"  {k:16s} {diff[k][0]} -> {diff[k][1]}")
        typer.echo(f"  fixed: {diff['fixed'] or '-'}   regressed: {diff['regressed'] or '-'}")


auth_app = typer.Typer(help="Auth store maintenance (run where the DB credentials are)")
app.add_typer(auth_app, name="auth")


@auth_app.command("reset-admin")
def auth_reset_admin(
    revoke_all: bool = typer.Option(
        False, "--revoke-all",
        help="Also delete EVERY user's sessions and API tokens (suspected compromise). "
             "Accounts and roles are kept.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
):
    """Break-glass: restore the `admin` account when every admin is locked out.

    Resets `admin` to the seed password (enabled, admin role, must change
    password on first login) and deletes admin's sessions and API tokens.
    Other users and roles are untouched. Takes effect immediately -- no
    restart. Anyone who can run this already holds the DB credentials."""
    from tpk import auth as auth_mod

    prefix = setting("TPK_STREAM_PREFIX", "db", "stream_prefix", "")
    scope = ("EVERY user's sessions and API tokens" if revoke_all
             else f"`{auth_mod.SEED_USERNAME}`'s sessions and API tokens")
    if not yes:
        typer.confirm(
            f"Reset `{auth_mod.SEED_USERNAME}` to the seed password and delete {scope}?",
            abort=True,
        )
    client = db.get_client(Settings.from_env())
    db.ensure_schema(client, prefix)
    auth_mod.reset_admin(client, prefix=prefix, revoke_all=revoke_all)
    typer.echo(f"deleted {scope}")
    typer.echo(
        f"`{auth_mod.SEED_USERNAME}` reset: log in with password "
        f"`{auth_mod.SEED_PASSWORD}` NOW and change it -- until you do, that "
        f"well-known password works."
    )


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
        timeout_s=setting("TPK_DB_WAIT_SECONDS", "db", "wait_seconds", 60.0, cast=float),
    )
    # The whole server runs on the configured stream prefix, like ingest /
    # export / import / auth do (#84) -- otherwise a prefixed deployment would
    # ingest into one set of streams and serve from another.
    prefix = setting("TPK_STREAM_PREFIX", "db", "stream_prefix", "")
    db.ensure_schema(client, prefix)
    if REPOS_TOML.exists():
        corpus.seed_from_toml(client, REPOS_TOML, prefix=prefix)
    # Eager bootstrap (issue #6): seed the admin user before uvicorn starts
    # serving. `serve` here always runs as a single uvicorn process (no
    # `workers=` argument), so the list_users-then-upsert_user race in
    # seed_admin() cannot happen between two processes of this command; and
    # even if it somehow raced, kg_users' PRIMARY KEY makes a double-seed of
    # the same "admin" row converge to one final row anyway.
    auth_mod.seed_admin(client, prefix)
    from tpk.version import version_string

    typer.echo(f"tpk {version_string()} starting on {host}:{port}"
               + (f" (stream prefix {prefix!r})" if prefix else ""))
    uvicorn.run(create_app(stream_prefix=prefix), host=host, port=port)


if __name__ == "__main__":
    app()
