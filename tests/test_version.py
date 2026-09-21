"""The running version (#85): one source of truth, shown everywhere."""
import asyncio
import subprocess

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tpk import version as version_mod


class _NoAgent:
    async def astream_events(self, *a, **k):
        yield  # pragma: no cover


def _no_git(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(version_mod.subprocess, "run", boom)


def test_image_version_comes_from_the_build_env(monkeypatch):
    monkeypatch.setenv("TPK_VERSION", "1.2.3")
    monkeypatch.setenv("TPK_COMMIT", "abcdef1234567890")
    assert version_mod.get_version() == ("1.2.3", "abcdef1")
    assert version_mod.version_string() == "1.2.3 (abcdef1)"


def test_a_leading_v_is_dropped(monkeypatch):
    monkeypatch.setenv("TPK_VERSION", "v1.2.3")
    monkeypatch.delenv("TPK_COMMIT", raising=False)
    _no_git(monkeypatch)
    assert version_mod.get_version() == ("1.2.3", "")
    assert version_mod.version_string() == "1.2.3"


def test_dev_checkout_uses_git_describe(monkeypatch):
    monkeypatch.delenv("TPK_VERSION", raising=False)
    monkeypatch.delenv("TPK_COMMIT", raising=False)

    def fake_run(cmd, **kw):
        out = "v0.0.5-3-gbc549ef-dirty\n" if "describe" in cmd else "bc549ef1234\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(version_mod.subprocess, "run", fake_run)
    assert version_mod.get_version() == ("0.0.5-3-gbc549ef-dirty", "bc549ef")


def test_no_metadata_at_all_is_dev_not_a_stale_constant(monkeypatch):
    monkeypatch.delenv("TPK_VERSION", raising=False)
    monkeypatch.delenv("TPK_COMMIT", raising=False)
    _no_git(monkeypatch)
    assert version_mod.get_version() == ("dev", "")


def test_cli_version_flag(monkeypatch):
    from tpk.cli import app

    monkeypatch.setenv("TPK_VERSION", "1.2.3")
    monkeypatch.setenv("TPK_COMMIT", "abcdef1234")
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == "tpk 1.2.3 (abcdef1)"


def test_healthz_reports_the_version(monkeypatch):
    from tpk.server import create_app

    monkeypatch.setenv("TPK_VERSION", "1.2.3")
    monkeypatch.setenv("TPK_COMMIT", "abcdef1234")
    body = TestClient(create_app(agent=_NoAgent())).get("/healthz").json()
    assert body == {"status": "ok", "version": "1.2.3", "commit": "abcdef1"}


def test_mcp_server_info_carries_the_version(monkeypatch):
    from tpk.mcp_server import build_server

    monkeypatch.setenv("TPK_VERSION", "1.2.3")
    server = build_server(object())
    assert server._lowlevel_server.version == "1.2.3"
    assert asyncio.run(server.list_tools())  # still a working server
