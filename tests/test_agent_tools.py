

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
