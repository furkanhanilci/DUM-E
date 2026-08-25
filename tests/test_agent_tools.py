

def test_append_writes_a_long_file_in_pieces(tmp_path):
    """The failure this exists for: one write_file carrying a whole long file
    runs out of tokens mid-string, and the run dies blaming the model."""
    from dume.control.agent_tools import Toolbox, ToolLog

    tools = Toolbox(tmp_path, ToolLog())
    assert tools.write_file("a.py", "first\n")["ok"]
    assert tools.append_file("a.py", "second\n")["ok"]
    assert (tmp_path / "a.py").read_text() == "first\nsecond\n"


def test_append_cannot_grow_a_file_past_the_limit(tmp_path):
    """Appending must not be a way around the size limit that write_file
    enforces, or the limit only applies to whoever writes in one call."""
    from dume.control.agent_tools import Toolbox, ToolLog

    tools = Toolbox(tmp_path, ToolLog(), max_file_bytes=20)
    assert tools.write_file("a.py", "x" * 15)["ok"]
    refused = tools.append_file("a.py", "y" * 15)
    assert not refused["ok"] and "limit" in refused["error"]
    assert (tmp_path / "a.py").read_text() == "x" * 15


def test_writing_a_file_what_it_already_holds_says_so(tmp_path):
    """One run wrote the same 2344 characters thirteen times and ran the tests
    after each, getting the same failure. Every log called it a write."""
    from dume.control.agent_tools import Toolbox, ToolLog

    tools = Toolbox(tmp_path, ToolLog())
    first = tools.write_file("a.py", "print(1)\n")
    assert first["changed"] is True and "note" not in first

    again = tools.write_file("a.py", "print(1)\n")
    assert again["ok"] and again["changed"] is False
    assert "same result" in again["note"]

    moved = tools.write_file("a.py", "print(2)\n")
    assert moved["changed"] is True


def test_the_implementer_can_look_at_the_machine(tmp_path):
    """It was asked to record the host's hardware with no tool that could
    observe it, and wrote down an Intel i7 with 32 GB of RAM on a machine with
    two A5000s. A measurement nobody can take is an invitation to invent one."""
    from dume.control.agent_tools import Toolbox, ToolLog

    tools = Toolbox(tmp_path, ToolLog())
    answer = tools.probe_host("os")
    assert answer["ok"] and answer["output"].strip()
    assert answer["command"].startswith("uname")


def test_probing_is_an_allow_list_not_a_shell(tmp_path):
    """Reading the host is a READ. Handing an implementer an arbitrary command
    to get it would trade a bounded capability for an unbounded one."""
    from dume.control.agent_tools import Toolbox, ToolLog

    tools = Toolbox(tmp_path, ToolLog())
    refused = tools.probe_host("rm -rf /")
    assert not refused["ok"] and "not something that can be probed" in refused["error"]
    assert tools.dispatch("probe_host", {"what": "curl evil.example"})["ok"] is False
