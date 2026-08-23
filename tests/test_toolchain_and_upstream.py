"""WP-004 and the upstream lock. Both exist to make drift visible."""
import json

import pytest

from dume import toolchain, upstream


def test_lock_records_what_is_present_and_what_is_missing(tmp_path):
    lock = toolchain.write_lock(current_wave=1, path=tmp_path / "toolchain.lock.json")
    assert lock["environment_digest"]
    names = {t["name"] for t in lock["tools"]}
    assert {"git", "python3"} <= names
    assert (tmp_path / "toolchain.lock.json").is_file()


def test_a_tool_needed_only_later_does_not_block_the_current_wave(tmp_path):
    """A wave-5 dependency absent at wave 1 is a plan item, not a blocker."""
    lock = toolchain.write_lock(current_wave=1, path=tmp_path / "l.json")
    later = {m["name"] for m in lock["missing_for_later_waves"]}
    assert all(name not in lock["missing_required"] for name in later)


def test_verify_detects_a_changed_version(tmp_path):
    path = tmp_path / "l.json"
    toolchain.write_lock(current_wave=1, path=path)
    doctored = json.loads(path.read_text())
    for tool in doctored["tools"]:
        if tool["name"] == "git" and tool["present"]:
            tool["version"] = "0.0.1"
    path.write_text(json.dumps(doctored))
    result = toolchain.verify(path)
    assert result["status"] == "DRIFT"
    assert any(d["tool"] == "git" and d["change"] == "VERSION_CHANGED"
               for d in result["drift"])


def test_verify_reports_no_lock_rather_than_passing(tmp_path):
    assert toolchain.verify(tmp_path / "absent.json")["status"] == "NO_LOCK"


def _lock_file(tmp_path, **overrides):
    entry = {"name": "fixture", "role": "test", "source": str(tmp_path / "origin.git"),
             "pinned_revision": "0" * 40}
    entry.update(overrides)
    path = tmp_path / "upstream.lock.json"
    path.write_text(json.dumps({"schema": "dume.upstream_lock/1", "upstreams": [entry]}))
    return path


def test_an_unreachable_upstream_is_never_reported_as_agreement(tmp_path):
    """A network failure must not be able to look like NO_DRIFT."""
    result = upstream.check(_lock_file(tmp_path))
    assert result["results"][0]["status"] == "UNREACHABLE"
    assert result["verdict"] == "INCOMPLETE"


def test_drift_is_detected_against_a_real_repository(tmp_path):
    """A local git repository stands in for upstream, so the test needs no network."""
    import subprocess
    origin = tmp_path / "origin"
    origin.mkdir()
    run = lambda *a: subprocess.run(a, cwd=origin, capture_output=True, check=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@t")
    run("git", "config", "user.name", "t")
    (origin / "f").write_text("1")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "one")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin,
                          capture_output=True, text=True).stdout.strip()

    matching = upstream.check(_lock_file(tmp_path, source=str(origin),
                                         pinned_revision=head))
    assert matching["verdict"] == "CLEAN"
    assert matching["results"][0]["status"] == "NO_DRIFT"

    drifted = upstream.check(_lock_file(tmp_path, source=str(origin),
                                        pinned_revision="a" * 40))
    assert drifted["verdict"] == "DRIFT"
    assert drifted["results"][0]["live_revision"] == head
