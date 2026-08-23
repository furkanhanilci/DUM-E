"""The catalogue and configuration layer."""
import json

import pytest

from dume import config
from dume.catalogue import seed
from dume.state import Store


def test_the_whole_catalogue_loads_with_no_dangling_dependency(tmp_path):
    store = Store(tmp_path / "s.db")
    summary = seed(store)
    assert summary["packages"] == 54
    assert summary["dangling_dependencies"] == []
    assert summary["waves"][0] == 1
    store.close()


def test_seeding_twice_changes_nothing(tmp_path):
    store = Store(tmp_path / "s.db")
    seed(store)
    first = {r["wp_id"]: r["state"] for r in store.all_wps()}
    store.transition("WP-001", "READY", actor="human")
    seed(store)
    assert store.get("WP-001")["state"] == "READY", "re-seeding must not reset progress"
    assert len(store.all_wps()) == len(first)
    store.close()


def test_wave_one_has_no_dependencies_and_later_waves_do(tmp_path):
    store = Store(tmp_path / "s.db")
    seed(store)
    wave_one = [r for r in store.all_wps() if r["wave"] == 1]
    assert wave_one
    for row in wave_one:
        assert store.dependencies(row["wp_id"]) == []
    assert store.dependencies("WP-002") == ["WP-001"]
    store.close()


def test_real_configuration_loads_and_leaves_aethrion_unbound():
    """DUM-E is being commissioned in isolation; the spec/target slots are
    declared but deliberately not bound to any path."""
    cfg = config.load()
    assert cfg["workspaces"]["AETHRION_SPEC"]["bound"] is False
    assert cfg["workspaces"]["AETHRION_SPEC"]["path"] is None
    assert cfg["workspaces"]["AETHRION_SPEC"]["mode"] == "READ_ONLY"
    assert cfg["workspaces"]["AETHRION_TARGET"]["bound"] is False


def test_unknown_workspace_mode_is_refused(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text(json.dumps({"schema_version": 1, "workspaces": {
        "X": {"path": str(tmp_path), "mode": "WRITE_SOMETIMES"}}}))
    with pytest.raises(config.ConfigError, match="unknown mode"):
        config.load(bad)


def test_a_bound_workspace_without_a_path_is_refused(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text(json.dumps({"schema_version": 1, "workspaces": {
        "X": {"path": None, "mode": "READ_WRITE", "bound": True}}}))
    with pytest.raises(config.ConfigError, match="marked bound but has no path"):
        config.load(bad)


def test_malformed_configuration_fails_closed(tmp_path):
    bad = tmp_path / "c.json"
    bad.write_text("{not json")
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.load(bad)
