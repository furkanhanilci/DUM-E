

def test_a_retried_package_can_actually_run_again():
    """`retry` moves a failed package FAILED → RETRY → PLANNED, and the
    orchestrator required READY. So a retry moved the package somewhere nothing
    could pick it up: it could be retried forever and never run again.

    The lifecycle allows no path from PLANNED back to READY — correctly, since
    a package that has been packaged and planned should not pretend otherwise —
    so the fix belongs in what a run will start from, not in the state machine.
    """
    from dume.state.store import TRANSITIONS

    # The dead end this guards: nothing leads from PLANNED to READY.
    assert "READY" not in TRANSITIONS["PLANNED"]
    assert "PLANNED" in TRANSITIONS["RETRY"]

    import inspect
    from dume.control import orchestrator

    source = inspect.getsource(orchestrator.Orchestrator.run)
    assert 'row["state"] not in ("READY", "PLANNED")' in source, (
        "a run must start from a re-planned package as well as a fresh one")
    assert "resuming" in source, (
        "a resumed run must skip the transitions it has already made")


def test_a_retried_package_can_actually_run_again():
    """`retry` moves a failed package FAILED → RETRY → PLANNED, and the
    orchestrator required READY. So a retry moved the package somewhere nothing
    could pick it up: it could be retried forever and never run again.

    The lifecycle allows no path from PLANNED back to READY — correctly, since
    a package that has been packaged and planned should not pretend otherwise —
    so the fix belongs in what a run will start from, not in the state machine.
    """
    import inspect

    from dume.control import orchestrator
    from dume.state.store import TRANSITIONS

    # The dead end this guards: nothing leads from PLANNED back to READY.
    assert "READY" not in TRANSITIONS["PLANNED"]
    assert "PLANNED" in TRANSITIONS["RETRY"]

    source = inspect.getsource(orchestrator.Orchestrator.run)
    assert 'row["state"] not in ("READY", "PLANNED")' in source, (
        "a run must start from a re-planned package as well as a fresh one")
    assert "if not resuming:" in source, (
        "a resumed run must skip the transitions it has already made")


def test_a_candidate_that_changed_nothing_is_not_a_candidate():
    """A real run reported `implement OK — candidate 7786fbfbfd74, RED exit=1,
    GREEN exit=0, 8 tool calls` over a diff of zero files. The candidate was
    the commit the worktree was cut from.

    Everything downstream then ran on it: the protected-paths check passed
    vacuously over nothing, and the specification reviewer — a live model, on
    the other GPU — was paid to conclude "the candidate diff is empty". It
    caught it, which is the system working. It should not have had to: the
    check is mechanical and the review is not.
    """
    import inspect

    from dume.control import orchestrator

    source = inspect.getsource(orchestrator.Orchestrator.run)
    assert "candidate == worktree.base_revision" in source, (
        "an empty candidate must be refused where it is produced")
    # And refused as an implementation failure: the machinery worked, the
    # output was empty.
    empty = source.split("candidate == worktree.base_revision", 1)[1][:600]
    assert "IMPLEMENTATION_FAILURE" in empty
