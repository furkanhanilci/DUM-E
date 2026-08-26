"""The idle detector must judge the turn it is looking at.

BZ-057. The live rehearsal failed at `implement` on two model families, and the
transcript the refusal used to destroy showed why: the implementer had written
the test, run it red, and written the code. Three correct turns, refused.

Two defects produced it, and they compounded:

  * `turn_idle` was computed at the bottom of the loop and read at the top of
    the next one, so every turn was judged by what the *previous* turn did. A
    write that followed a test run was idle however much it changed.
  * a lone `run_tests` was idle unconditionally — and the red run is one the
    protocol requires.

Together they spent the whole three-turn idle budget on the canonical cycle:
write the test, run it red, write the code, run it green. The only runs that
ever passed were the ones whose model happened to bundle the write and the run
into one turn, which is a property of the model, not of the discipline.
"""
import json
import subprocess
from pathlib import Path

import pytest

from dume.control import model_executor as mx
from dume.packets.wp_packet_builder import WPPacket
from dume.runtimes.client import Reply, ToolCall


class ScriptedImplementer:
    """Emits one tool call per turn, which is what a real model did."""

    def __init__(self, turns):
        self.turns, self.seen = list(turns), 0

    def chat(self, messages, tools=None, max_tokens=2048, **kw):
        if self.seen >= len(self.turns):
            return Reply(content="DONE")
        name, args = self.turns[self.seen]
        self.seen += 1
        if name is None:
            return Reply(content=args)
        return Reply(tool_calls=[ToolCall(id=f"c{self.seen}", name=name,
                                          arguments=args,
                                          raw_arguments=json.dumps(args))])


def Packet(deliverables=()):
    """The real packet type — a stand-in would drift from what it projects."""
    return WPPacket(wp_id="WP-TEST", title="a capacity helper",
                    workstream="test", wave=1, owner="implementer",
                    verifier_role="verifier", spec_revision="r1",
                    deliverables=list(deliverables), packet_sha256="0" * 64)


class Tree:
    def __init__(self, path):
        self.path = str(path)


TEST_SRC = ("import capacity\n\n\n"
            "def test_v():\n    assert capacity.v() == 1\n")
IMPL_SRC = "def v():\n    return 1\n"

CANONICAL = [
    ("write_file", {"path": "test_capacity.py", "content": TEST_SRC}),
    ("run_tests", {}),
    ("write_file", {"path": "capacity.py", "content": IMPL_SRC}),
    ("run_tests", {}),
    (None, "DONE"),
]


class Worktrees:
    """Only what `implement` asks of it after the loop."""

    @staticmethod
    def candidate_revision(worktree):
        return subprocess.run(["git", "-C", worktree.path, "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              check=True).stdout.strip()


def _worktree(tmp_path):
    """A real repository: the candidate is produced by committing."""
    tree = tmp_path / "wt"
    tree.mkdir(parents=True)
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True)
    return Tree(tree)


def _executor(turns, tmp_path):
    return mx.ModelExecutor(
        worktrees=Worktrees(),
        clients={"implementer": ScriptedImplementer(turns)},
        evidence_dir=tmp_path / "evidence")


def test_the_canonical_cycle_is_not_refused(tmp_path):
    """Write the test, run it red, write the code, run it green.

    One call per turn — the shape that failed. This is the discipline the
    class exists to enforce; refusing it was refusing the thing it wanted.
    """
    tree = _worktree(tmp_path)
    result = _executor(CANONICAL, tmp_path).implement(Packet(), {}, tree)
    assert "RED exit=2, GREEN exit=0" in result["discipline"]
    assert result["candidate_revision"]


def test_rerunning_the_tests_on_an_unchanged_tree_is_still_refused(tmp_path):
    """The loop the detector exists for has to keep dying.

    A run of the tests is evidence about a change. A second one with nothing
    touched in between cannot answer differently.
    """
    tree = _worktree(tmp_path)
    turns = [("write_file", {"path": "test_capacity.py", "content": TEST_SRC})]
    turns += [("run_tests", {})] * 8
    with pytest.raises(mx.ImplementationRefused) as exc:
        _executor(turns, tmp_path).implement(Packet(), {}, tree)
    assert "without changing a file" in str(exc.value)


def test_rewriting_a_file_with_what_it_already_held_is_still_refused(tmp_path):
    """A write that changed nothing looked like work in every log."""
    tree = _worktree(tmp_path)
    same = ("write_file", {"path": "test_capacity.py", "content": TEST_SRC})
    turns = [same, ("run_tests", {}), same, ("run_tests", {}),
             same, ("run_tests", {}), same, ("run_tests", {})]
    with pytest.raises(mx.ImplementationRefused):
        _executor(turns, tmp_path).implement(Packet(), {}, tree)


def test_a_refusal_leaves_the_transcript_behind(tmp_path):
    """The failed run is the one whose transcript is worth reading.

    It used to be written only after a clean return, so the runs that left no
    record of what the implementer did were exactly the runs that failed.
    """
    tree = _worktree(tmp_path)
    turns = [("run_tests", {})] * 8
    with pytest.raises(mx.ImplementationRefused):
        _executor(turns, tmp_path).implement(Packet(), {}, tree)
    log = tmp_path / "evidence" / "WP-TEST" / "tool_log.json"
    assert log.is_file()
    assert json.loads(log.read_text())["calls"]


def test_the_transcript_holds_every_call_the_run_reports(tmp_path):
    """It was written before the deliverable turns, and they use the tools.

    So the record of "what the agent actually did" stopped where the cycle
    stopped, and every call that produced a mandatory deliverable was missing
    from it — the phase the deliverables gate then returns a verdict on. Two
    live runs reported 19 and 17 tool calls against files holding 8 and 11.
    """
    tree = _worktree(tmp_path)
    # The main loop breaks the moment it has red and then green, so the
    # deliverable turns are served the entries after that — not after DONE,
    # which it never reaches.
    turns = CANONICAL[:4] + [
        ("write_file", {"path": "notes.md", "content": "# notes\n\nreal.\n"}),
        (None, "DONE")]
    result = _executor(turns, tmp_path).implement(
        Packet(deliverables=["notes.md"]), {}, tree)
    log = tmp_path / "evidence" / "WP-TEST" / "tool_log.json"
    calls = json.loads(log.read_text())["calls"]
    assert [c["tool"] for c in calls].count("write_file") == 3, (
        "the deliverable the gate judges is missing from the transcript")
    assert f"{len(calls)} tool calls" in result["discipline"]
    assert len(calls) == result["tool_calls"]
