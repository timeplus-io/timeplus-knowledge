import time
from datetime import datetime, timezone
from pathlib import Path

from conftest import requires_timeplus
from tpk import corpus
from tpk.config import RepoConfig, entry_key
from tpk.model import Node

pytestmark = requires_timeplus


def _eventually(fn, predicate, timeout=5.0, interval=0.1):
    """Poll fn() until predicate(result) holds, or return the last value on
    timeout. Mutable-stream writes have a small (~100-300ms) propagation lag
    before being visible to a fresh `table(...)` read; see tests/test_ingest.py.
    """
    deadline = time.monotonic() + timeout
    result = fn()
    while not predicate(result) and time.monotonic() < deadline:
        time.sleep(interval)
        result = fn()
    return result


def _entry(name="repoa", ref="v1.0.0", enabled=True, **over):
    base = dict(
        name=name, github=f"org/{name}", ref=ref, visibility="internal",
        extraction="code-only", description=f"{name} test entry", enabled=enabled,
    )
    base.update(over)
    return RepoConfig(**base)


def test_upsert_list_find_roundtrip(tp):
    client, prefix = tp
    corpus.upsert_entry(client, _entry(), prefix=prefix)
    corpus.upsert_entry(client, _entry(ref="v2.0.0", enabled=False), prefix=prefix)
    entries = corpus.list_entries(client, prefix=prefix)
    assert [(e.name, e.ref, e.enabled) for e in entries] == [
        ("repoa", "v1.0.0", True), ("repoa", "v2.0.0", False),
    ]
    found = corpus.find_entry(client, "repoa", "v2.0.0", prefix=prefix)
    assert found is not None and found.github == "org/repoa"
    assert corpus.find_entry(client, "repoa", "v9", prefix=prefix) is None


def test_enabled_keys_and_toggle(tp):
    client, prefix = tp
    corpus.upsert_entry(client, _entry(), prefix=prefix)
    corpus.upsert_entry(client, _entry(ref="v2.0.0", enabled=False), prefix=prefix)
    assert corpus.enabled_keys(client, prefix=prefix) == ["repoa@v1.0.0"]
    assert corpus.set_enabled(client, "repoa", "v2.0.0", True, prefix=prefix)
    assert sorted(corpus.enabled_keys(client, prefix=prefix)) == [
        "repoa@v1.0.0", "repoa@v2.0.0",
    ]
    assert not corpus.set_enabled(client, "ghost", "v1", True, prefix=prefix)


def test_delete_entry_with_and_without_purge(tp):
    from tpk.ingest import upsert_graph

    client, prefix = tp
    cfg = _entry()
    corpus.upsert_entry(client, cfg, prefix=prefix)
    node = Node(
        id="n1", repo=entry_key(cfg), kind="function", name="f",
        qualified_name="m.f", file_path="m.py", line_start=1, line_end=2,
        summary="", community="", visibility="internal",
    )
    upsert_graph(client, prefix, [node], [], datetime.now(timezone.utc))

    assert corpus.delete_entry(client, "repoa", "v1.0.0", prefix=prefix, purge=False)
    rows = client.query(
        f"SELECT count() FROM table({prefix}kg_nodes) WHERE repo = %(r)s",
        parameters={"r": entry_key(cfg)},
    ).result_rows
    assert rows[0][0] == 1  # rows retained without purge

    corpus.upsert_entry(client, cfg, prefix=prefix)
    _eventually(
        lambda: corpus.find_entry(client, "repoa", "v1.0.0", prefix=prefix),
        lambda v: v is not None,
    )
    assert corpus.delete_entry(client, "repoa", "v1.0.0", prefix=prefix, purge=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        n = client.query(
            f"SELECT count() FROM table({prefix}kg_nodes) WHERE repo = %(r)s",
            parameters={"r": entry_key(cfg)},
        ).result_rows[0][0]
        if n == 0:
            break
        time.sleep(0.2)
    assert n == 0  # purged


def test_seed_from_toml_is_idempotent_and_preserves_edits(tp, tmp_path: Path):
    client, prefix = tp
    toml_path = tmp_path / "repos.toml"
    toml_path.write_text(
        '[repos.docs]\ngithub = "timeplus-io/docs"\nref = "main"\nvisibility = "public"\n'
    )
    assert corpus.seed_from_toml(client, toml_path, prefix=prefix) == 1
    assert corpus.seed_from_toml(client, toml_path, prefix=prefix) == 0  # idempotent
    corpus.set_enabled(client, "docs", "main", False, prefix=prefix)
    corpus.seed_from_toml(client, toml_path, prefix=prefix)  # must NOT re-enable
    assert corpus.enabled_keys(client, prefix=prefix) == []


def test_entry_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TPK_CHECKOUT_DIR", str(tmp_path))
    paths = corpus.entry_paths([_entry(), _entry(name="loc", github="", ref="", path="/l")])
    assert paths["repoa@v1.0.0"] == tmp_path / "repoa" / "v1.0.0"
    assert paths["loc"] == Path("/l")
