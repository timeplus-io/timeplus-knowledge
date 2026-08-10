from pathlib import Path

from typer.testing import CliRunner

from tpk import cli as cli_mod
from tpk.cli import app


def test_ingest_unknown_repo_raises_bad_parameter(tmp_path: Path, monkeypatch):
    # No real Timeplus connection needed for this validation path: stub out
    # the client/schema calls so this stays a fast, DB-independent test.
    monkeypatch.setattr(cli_mod.db, "get_client", lambda settings: object())
    monkeypatch.setattr(cli_mod.db, "ensure_schema", lambda client: None)

    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[repos.docs]\npath = "/x/docs"\nvisibility = "public"\n')

    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--repo", "nope", "--repos-file", str(toml_path)])

    assert result.exit_code != 0
    assert not isinstance(result.exception, KeyError)
    assert "unknown repo" in result.output.lower()
