"""Proving discipline was applied, and refusing to claim more than the signals show."""
import json

from dume.review import discipline


def test_installation_alone_reports_no_behavioural_signal():
    """The design says presence of skill files is not behavioural proof."""
    report = discipline.assess(repo=".")
    assert report.verdict() in {"NO_SIGNAL", "INVOKED_BUT_UNPROVEN"}
    assert any("proves nothing about behaviour" in g for g in report.gaps)


def test_a_revision_mismatch_is_reported_as_such():
    report = discipline.assess(repo=".", expected_revision="0" * 40)
    assert report.verdict() == "REVISION_MISMATCH"


def test_the_machine_gate_is_reported_as_a_gap_superpowers_does_not_fill():
    report = discipline.assess(repo=".")
    assert any("MACHINE_GATE" in g and "ships none" in g for g in report.gaps)


def test_invocation_signals_are_read_from_a_transcript(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "hook_response",
         "hook_name": "SessionStart:startup",
         "output": "<EXTREMELY_IMPORTANT>using-superpowers</EXTREMELY_IMPORTANT>"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "superpowers:brainstorming"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill",
             "input": {"skill": "superpowers:test-driven-development"}}]}},
        {"not": "json-shaped for our purposes"},
    ]))
    report = discipline.assess(transcript=transcript, repo=".")
    assert report.bootstrap_observed
    assert "superpowers:brainstorming" in report.skills_invoked
    stages = {s.stage: s.present for s in report.signals if s.kind == "invocation"}
    assert stages["DESIGN"] and stages["RED_GREEN_REFACTOR"]
    assert not stages["CODE_REVIEW"]


def test_a_malformed_transcript_line_does_not_abort_the_read(tmp_path):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text('{"broken\n{"type":"x","skill":"superpowers:writing-plans"}\n')
    assert discipline.read_transcript(transcript)["lines"] == 2


def test_invocation_is_never_reported_as_proof_of_correctness(tmp_path):
    """A Skill call proves the skill was entered, not that the model obeyed it."""
    transcript = tmp_path / "s.jsonl"
    transcript.write_text(json.dumps(
        {"skill": "superpowers:test-driven-development"}) + "\n")
    report = discipline.assess(transcript=transcript)
    assert report.verdict() != "DISCIPLINE_EVIDENCED"
    assert any("cannot show the work is correct" in g for g in report.gaps)


def test_red_green_is_judged_from_history_not_from_commit_messages(tmp_path):
    import subprocess
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],
                                    capture_output=True, check=False)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "seed").write_text("1")
    run("add", "-A")
    run("commit", "-qm", "seed")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    (repo / "test_thing.py").write_text("def test_x():\n    assert thing()\n")
    run("add", "-A")
    run("commit", "-qm", "RED")
    (repo / "thing.py").write_text("def thing():\n    return True\n")
    run("add", "-A")
    run("commit", "-qm", "GREEN")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    signal = discipline.red_green_signal(repo, base, head)
    assert signal.present and signal.kind == "independent"

    # A single commit mixing both cannot show a red phase — and the report says
    # that, rather than claiming the discipline was skipped.
    (repo / "test_two.py").write_text("def test_y():\n    assert True\n")
    (repo / "two.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-qm", "RED then GREEN, honestly")
    head2 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    mixed = discipline.red_green_signal(repo, head, head2)
    assert not mixed.present
    assert "not the same as proving there was none" in mixed.detail
