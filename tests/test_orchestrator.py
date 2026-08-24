

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


def test_every_step_goes_to_a_channel_the_operator_can_read():
    """A step routed to an unknown name silently falls back to the package's
    own private channel, which is where a hundred messages sat unread. The
    four standing channels are the ones the desktop subscribes to."""
    from dume.collaboration.buzz import SPACE_CHANNELS
    from dume.control.orchestrator import Orchestrator

    readable = {"dume-control", "dume-implementation",
                "dume-review", "dume-verification"}
    unreadable = set(Orchestrator.STEP_CHANNEL.values()) - readable
    assert not unreadable, f"steps the operator cannot read: {sorted(unreadable)}"
    for name in Orchestrator.STEP_CHANNEL.values():
        assert name in SPACE_CHANNELS, f"{name} has no channel id"


def test_a_deliverable_of_headings_is_not_a_deliverable(tmp_path):
    """A reviewer spent a whole run reporting "mandatory deliverables exist but
    are empty or incomplete". host_inventory.json was the two characters {}."""
    from dume.control.orchestrator import _is_hollow

    hollow = tmp_path / "report.md"
    hollow.write_text("# Disk Capacity Report\n\n## Throughput\n\n## Headroom\n")
    assert _is_hollow(hollow)

    filled = tmp_path / "filled.md"
    filled.write_text("# Disk Capacity Report\n\nThe root disk has 40 GiB.\n")
    assert not _is_hollow(filled)

    empty_json = tmp_path / "inventory.json"
    empty_json.write_text("{}")
    assert _is_hollow(empty_json)

    real_json = tmp_path / "real.json"
    real_json.write_text('{"gpus": 2}')
    assert not _is_hollow(real_json)

    # Malformed JSON is a different complaint, and the reviewer's to make.
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert not _is_hollow(broken)


def test_a_model_that_will_not_use_tools_gets_the_other_runtime():
    """The implementer answered in prose for two turns without touching a
    tool. No candidate existed, so nothing candidate-implicating could be
    about it -- yet the run failed the package and named the implementer as
    owner, while the other runtime was never asked."""
    import inspect
    from dume.control import orchestrator

    source = inspect.getsource(orchestrator.Orchestrator.run)
    assert "except (ModelError, ImplementationRefused)" in source, (
        "a refusal still fails the package instead of switching runtime")
