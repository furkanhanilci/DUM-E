"""WP-002 boundary, attacked rather than described.

The work package is explicit that a prose warning is not a control if the system
can still perform the unsafe action. So each test here performs the unsafe
action and requires the boundary to refuse it.
"""
import json
import os

import pytest

from dume.workspace import Boundary, BoundaryViolation, probe_write


def _cfg(tmp_path, spec_ro=True):
    """A three-workspace fixture: read-only spec, writable target, evidence."""
    spec = tmp_path / "SPEC"
    target = tmp_path / "TARGET"
    evidence = tmp_path / "EVIDENCE"
    for d in (spec, target, evidence):
        d.mkdir()
    (spec / "sealed.md").write_text("frozen acceptance criteria\n")
    return {
        "schema_version": 1,
        "workspaces": {
            "SPEC": {"path": str(spec), "mode": "READ_ONLY" if spec_ro else "READ_WRITE",
                     "bound": True},
            "TARGET": {"path": str(target), "mode": "READ_WRITE", "bound": True},
            "EVIDENCE": {"path": str(evidence), "mode": "APPEND_ONLY", "bound": True},
            "UNBOUND_THING": {"path": None, "mode": "READ_WRITE", "bound": False},
        },
    }


@pytest.fixture
def boundary(tmp_path):
    return Boundary(_cfg(tmp_path))


def test_read_only_specification_refuses_writes(boundary, tmp_path):
    """The whole point of a sealed specification."""
    with pytest.raises(BoundaryViolation, match="READ_ONLY"):
        boundary.guard_write(tmp_path / "SPEC" / "sealed.md")


def test_writes_to_the_target_are_allowed(boundary, tmp_path):
    assert boundary.guard_write(tmp_path / "TARGET" / "src" / "new.py")


def test_path_traversal_cannot_escape_a_workspace(boundary, tmp_path):
    """`TARGET/../SPEC/sealed.md` must be judged as SPEC, not as TARGET."""
    escape = tmp_path / "TARGET" / ".." / "SPEC" / "sealed.md"
    decision = boundary.check_write(escape)
    assert not decision
    assert decision.workspace == "SPEC"


def test_symlink_planted_in_a_writable_workspace_cannot_reach_the_specification(
        boundary, tmp_path):
    """The attack the work package names: a link inside an allowed workspace
    used as a door into a protected one."""
    link = tmp_path / "TARGET" / "shortcut"
    link.symlink_to(tmp_path / "SPEC")
    decision = boundary.check_write(link / "sealed.md")
    assert not decision, "a symlink into the read-only spec must not be writable"
    assert decision.workspace == "SPEC"


def test_symlink_to_outside_every_workspace_is_refused(boundary, tmp_path):
    link = tmp_path / "TARGET" / "etc"
    link.symlink_to("/etc")
    decision = boundary.check_write(link / "passwd")
    assert not decision
    assert decision.workspace is None


def test_paths_outside_every_workspace_are_refused(boundary):
    assert not boundary.check_write("/etc/passwd")
    assert not boundary.check_write("/tmp/anything")


def test_unbound_workspace_grants_nothing(boundary):
    """An unbound slot must not silently behave as 'allow everything'."""
    assert "UNBOUND_THING" in boundary.unbound()
    assert not boundary.check_write("/anywhere/at/all")


def test_nested_workspace_is_judged_by_the_innermost_rule(tmp_path):
    """EVIDENCE inside TARGET must stay APPEND_ONLY, not inherit READ_WRITE."""
    cfg = _cfg(tmp_path)
    nested = tmp_path / "TARGET" / "evidence"
    nested.mkdir()
    cfg["workspaces"]["EVIDENCE"]["path"] = str(nested)
    boundary = Boundary(cfg)
    existing = nested / "receipt.json"
    existing.write_text("{}")
    decision = boundary.check_write(existing)
    assert not decision, "existing evidence must not be overwritable"
    assert decision.workspace == "EVIDENCE"


def test_append_only_permits_a_new_artefact(tmp_path):
    boundary = Boundary(_cfg(tmp_path))
    assert boundary.check_write(tmp_path / "EVIDENCE" / "brand-new.json")


def test_write_probe_reports_what_the_os_actually_did(tmp_path):
    """The falsifiable probe: a directory that really is read-only refuses."""
    d = tmp_path / "locked"
    d.mkdir()
    assert probe_write(d)[0] == "WROTE"
    os.chmod(d, 0o500)
    try:
        outcome, detail = probe_write(d)
        assert outcome == "REFUSED"
        assert "PermissionError" in detail or "Errno 13" in detail
    finally:
        os.chmod(d, 0o700)


def test_a_missing_directory_proves_nothing_rather_than_passing(tmp_path):
    """An absent workspace refuses writes for a reason that has nothing to do
    with the boundary. Counting that as a working control would be exactly the
    self-congratulation this harness exists to prevent."""
    outcome, detail = probe_write(tmp_path / "not-created-yet")
    assert outcome == "MISSING"
    assert "nothing was proven" in detail


def test_guard_returns_a_resolved_path_so_callers_cannot_re_introduce_the_link(
        boundary, tmp_path):
    link = tmp_path / "TARGET" / "inner"
    real = tmp_path / "TARGET" / "real"
    real.mkdir()
    link.symlink_to(real)
    resolved = boundary.guard_write(link / "f.txt")
    assert resolved == (real / "f.txt").resolve()
