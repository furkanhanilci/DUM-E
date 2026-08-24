"""The real executor: what it enforces regardless of what the model says."""
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from dume.control import model_executor as mx
from dume.control.agent_tools import ToolDenied, Toolbox


def test_red_distinguishes_no_test_from_a_test_that_does_not_pass():
    """Both narrow readings are wrong, and both were tried.

    Accepting any non-zero exit lets 5 — an empty suite — count as red, so an
    implementer can claim a cycle it never performed by calling run_tests before
    writing anything. Accepting only 1 rejects 2, the collection error a correct
    test-first cycle actually produces first when the test imports a module that
    does not exist yet; the loop then never terminates while the model correctly
    reports it is done.
    """
    assert mx.RED_EXIT_CODES == {1, 2}
    assert mx.EMPTY_SUITE_EXIT == 5
    assert 5 not in mx.RED_EXIT_CODES
    assert 0 not in mx.RED_EXIT_CODES


def test_a_collection_error_is_the_normal_first_red(tmp_path):
    """The shape a real test-first cycle produces: the test imports a module
    that is not written yet."""
    (tmp_path / ".git").mkdir()
    toolbox = Toolbox(tmp_path)
    toolbox.write_file("test_thing.py",
                       "import thing\n\n\ndef test_v():\n    assert thing.v() == 1\n")
    result = toolbox.run_tests()
    assert result["exit_code"] == 2
    assert result["exit_code"] in mx.RED_EXIT_CODES


def test_the_implementer_cannot_finish_without_both_phases():
    source = inspect.getsource(mx.ModelExecutor.implement)
    assert "if red_exit is None or green_exit != 0:" in source
    assert "ImplementationRefused" in source


def test_the_verifier_cannot_overrule_the_exit_code():
    """The model interprets the run. It does not get to decide it."""
    source = inspect.getsource(mx.ModelExecutor._verify)
    assert 'actual = "PASS" if run.returncode == 0 else "FAIL"' in source
    assert '"verdict": actual' in source


def test_each_role_gets_its_own_transcript():
    """If every role shared one list, the embargo would be a comment."""
    executor = mx.ModelExecutor(worktrees=None, clients={}, evidence_dir=Path("."))
    executor._record("spec_reviewer", [{"role": "user", "content": "a"}], "x")
    executor._record("code_reviewer", [{"role": "user", "content": "b"}], "y")
    assert set(executor.transcripts) == {"spec_reviewer", "code_reviewer"}
    assert executor.transcripts["spec_reviewer"] != executor.transcripts["code_reviewer"]


def test_the_code_reviewer_is_not_handed_the_other_reviewers_question():
    # The comment wraps across lines, so strip the comment markers before
    # normalising whitespace — otherwise a '#' lands mid-sentence.
    raw = inspect.getsource(mx.ModelExecutor.review).replace("#", " ")
    source = " ".join(raw.split())
    assert "not being asked whether the requirement was met" in source
    assert "answer the other reviewer's question" in source


def test_every_role_card_states_one_question_and_its_limits():
    for role, card in mx.ROLE_CARDS.items():
        assert len(card) > 200, role
        assert "You" in card


def test_the_packet_brief_marks_where_it_truncated():
    class Section:
        name, path, text = "card", "/x", "y" * 500
    class Packet:
        wp_id, title, workstream, wave = "WP-001", "t", "w", 1
        packet_sha256 = "a" * 64
        sections = [Section()]
        deliverables = ["d"]
        known_failure_modes = ["f"]
        forbidden = ["nothing"]
    brief = mx._packet_brief(Packet(), limit=100)
    assert "more characters]" in brief, "truncation must be visible, not silent"


# ---- the capability boundary the implementer works inside ---------------

@pytest.fixture
def toolbox(tmp_path):
    (tmp_path / ".git").mkdir()
    return Toolbox(tmp_path)


@pytest.mark.parametrize("path", [
    "../escape.py", "/etc/passwd", "../../etc/shadow", ".git/HEAD",
    ".git/config"])
def test_a_write_outside_the_worktree_is_refused(toolbox, path):
    result = toolbox.write_file(path, "x")
    assert result["ok"] is False


def test_a_symlink_out_of_the_worktree_is_refused(tmp_path):
    (tmp_path / ".git").mkdir()
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside)
    toolbox = Toolbox(tmp_path)
    assert toolbox.write_file("link/x.py", "x")["ok"] is False


def test_an_oversized_write_is_refused(toolbox):
    assert toolbox.write_file("big.py", "x" * (300 * 1024))["ok"] is False


def test_the_tool_log_records_what_was_done_without_the_content(toolbox):
    toolbox.write_file("a.py", "secret content here" * 10)
    entry = toolbox.log.calls[-1]
    assert entry["outcome"] == "OK"
    assert "secret content here" not in str(entry)
    assert "chars" in str(entry["arguments"]["content"])


def test_run_tests_reports_the_exit_code_it_got(tmp_path):
    (tmp_path / ".git").mkdir()
    toolbox = Toolbox(tmp_path)
    empty = toolbox.run_tests()
    assert empty["exit_code"] == 5, "an empty suite is exit 5, not a failure"
    toolbox.write_file("test_x.py", "def test_x():\n    assert False\n")
    failing = toolbox.run_tests()
    assert failing["exit_code"] == 1 and failing["passed"] is False
    toolbox.write_file("test_x.py", "def test_x():\n    assert True\n")
    passing = toolbox.run_tests()
    assert passing["exit_code"] == 0 and passing["passed"] is True


def test_an_unknown_tool_is_refused_not_guessed_at(toolbox):
    assert toolbox.dispatch("exec_shell", {"cmd": "id"})["ok"] is False
