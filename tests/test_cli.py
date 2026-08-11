from pathlib import Path

from typer.testing import CliRunner

from conftest import requires_timeplus
from tpk import cli as cli_mod
from tpk.cli import app

pytestmark = requires_timeplus


def test_ingest_unknown_repo_raises_bad_parameter(tp, tmp_path: Path, monkeypatch):
    # The CLI now reads its targets from the corpus store, so this needs a
    # real client (the store must be queryable) rather than a bare stub.
    client, prefix = tp
    monkeypatch.setattr(cli_mod.db, "get_client", lambda settings: client)
    monkeypatch.setenv("TPK_STREAM_PREFIX", prefix)

    toml_path = tmp_path / "repos.toml"
    toml_path.write_text('[repos.docs]\npath = "/x/docs"\nvisibility = "public"\n')

    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "--repo", "nope", "--repos-file", str(toml_path)])

    assert result.exit_code != 0
    assert not isinstance(result.exception, KeyError)
    assert "no corpus entry matches" in result.output.lower()


def test_ingest_reads_corpus_store_and_matches_name_or_key(tp, monkeypatch, tmp_path):
    import typer
    from typer.testing import CliRunner

    from tpk import cli as cli_mod
    from tpk import corpus
    from tpk.config import RepoConfig

    client, prefix = tp
    # the CLI must consult the store: seed one entry directly (not in the toml)
    corpus.upsert_entry(
        client, RepoConfig(name="storeonly", github="o/s", ref="v1", visibility="internal"),
        prefix=prefix,
    )
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text("[repos]\n")  # empty seed

    calls = []
    monkeypatch.setattr(cli_mod, "ingest_repo", lambda c, cfg, **kw: calls.append(cfg.name) or
                        cli_mod.IngestResult(cfg.name, "x", 0, 0, "ok"))
    monkeypatch.setattr(cli_mod.db, "get_client", lambda s: client)
    monkeypatch.setattr(cli_mod.db, "ensure_schema", lambda c, p="": None)
    monkeypatch.setenv("TPK_STREAM_PREFIX", prefix)

    runner = CliRunner()
    res = runner.invoke(cli_mod.app, ["ingest", "--repos-file", str(toml_path),
                                      "--repo", "storeonly@v1"])
    assert res.exit_code == 0, res.output
    assert calls == ["storeonly"]
