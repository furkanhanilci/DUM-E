

def test_a_killed_run_does_not_strand_the_package(tmp_path, monkeypatch):
    """A run that is killed leaves the package EXECUTING and nothing puts it
    back: the state is written on the way in, and the way out belongs to the
    process that is no longer there."""
    import os

    from dume.control import commission

    lock = tmp_path / "commissioning.pid"
    monkeypatch.setattr(commission, "LOCK", lock)

    assert commission._lock_holder() is None, "no file is no holder"

    lock.write_text("2147483646")  # a pid that cannot be running
    assert commission._lock_holder() is None, "a dead pid does not hold the run"

    lock.write_text(str(os.getpid()))
    assert commission._lock_holder() == os.getpid(), "a live pid holds it"

    lock.write_text("not a pid")
    assert commission._lock_holder() is None
