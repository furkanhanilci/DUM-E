"""The Obsidian mirror's name mapping and frontmatter.

Only the pure parts are unit-tested. Whether the vault is currently in sync is
an *operational* question — `python3 scripts/mirror_dume.py --check` answers it —
and asserting it here would turn every commit into a failing test suite, because
the cockpit note records the candidate revision.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import mirror_dume as m


def test_a_package_file_takes_the_descriptive_slug_from_its_directory():
    """The pack keeps the title on the directory; the vault wants it on the file."""
    assert m.mirror_name("WP-001.md") == \
        "wp_001_host_hardware_os_and_capacity_inventory.md"


@pytest.mark.parametrize("companion", ["tests", "acceptance"])
def test_companion_files_keep_their_suffix(companion):
    name = m.mirror_name(f"WP-002.{companion}.md")
    assert name.startswith("wp_002_three_workspace_boundary")
    assert name.endswith(f".{companion}.md")


def test_scenario_files_are_lowercased_in_place():
    assert m.mirror_name("ACC-D024_empty_evidence_artifact.md") == \
        "acc_d024_empty_evidence_artifact.md"


def test_an_unrecognised_name_passes_through_rather_than_being_mangled():
    assert m.mirror_name("some_other_file.md") == "some_other_file.md"
    assert m.mirror_name("README.md") == "index.md"


def test_every_work_package_in_the_pack_has_a_slug():
    slugs = m.build_slug_map()
    assert len(slugs) == 54
    assert all(v.startswith(k.lower().replace("-", "_")) for k, v in slugs.items())


def test_frontmatter_carries_what_the_vault_linter_requires():
    """Generated pages need title, type, category, generated and tags, and must
    name their source."""
    fm = m.frontmatter(title="T", dume_id="DUME-X", note_type="index",
                       category="commissioning", tags=["dume/index"],
                       summary="S", source="a/b.md")
    for required in ("title:", "type:", "category:", "generated:", "tags:", "source:"):
        assert required in fm


def test_tags_stay_outside_the_aethrion_namespace():
    """The vault's controlled vocabulary governs `aethrion/` only. A DUM-E note
    emitting an `aethrion/` tag would be a lint finding in someone else's
    project."""
    fm = m.frontmatter(title="T", dume_id="D", note_type="work-package",
                       category="commissioning",
                       tags=["dume/work-package", "dume/wave/w1"],
                       summary="S", source="x.md")
    assert "aethrion/" not in fm


def test_relative_markdown_links_become_wikilinks_that_resolve():
    out = m.rewrite_links("See [`WP-001.tests.md`](WP-001.tests.md).", "01_FOUNDATION")
    assert "[[10 - Projects/DUM-E/01 - Commissioning/01_FOUNDATION/" in out
    assert "wp_001_host_hardware_os_and_capacity_inventory.tests|" in out


def test_external_links_are_left_alone():
    text = "See [the repo](https://github.com/block/buzz)."
    assert m.rewrite_links(text, "03_BUZZ") == text


def test_the_watchers_fingerprint_does_not_perturb_what_it_watches():
    """Opening a SQLite database touches the file. A watcher that timestamps the
    state store therefore re-triggers on its own read and mirrors forever — which
    it did, every five seconds, until the fingerprint was taken over the states
    themselves rather than the file's mtime."""
    first = m.source_fingerprint()
    second = m.source_fingerprint()
    assert first == second


def test_the_fingerprint_moves_when_a_package_moves(tmp_path, monkeypatch):
    before = m.source_fingerprint()
    fake = {"WP-001": {"state": "ACCEPTED", "candidate_revision": "deadbeef"}}
    monkeypatch.setattr(m, "read_states", lambda: fake)
    assert m.source_fingerprint() != before


def test_the_colour_groups_are_mutually_exclusive_and_documented():
    """Obsidian applies one group per node and the resolution order between
    overlapping groups is not dependable."""
    queries = [q for _label, q, _c, _w in m.DUME_GROUPS]
    assert len(queries) == len(set(queries))
    for label, query, colour, why in m.DUME_GROUPS:
        assert colour.startswith("#") and len(colour) == 7
        assert why, f"{label} has no stated reason to exist"
        assert "dume/" in query


def test_applying_colours_preserves_groups_it_did_not_write(tmp_path):
    """Another project's graph configuration is not this one's to discard."""
    import json
    obsidian = tmp_path / ".obsidian"
    obsidian.mkdir()
    (obsidian / "graph.json").write_text(json.dumps({
        "colorGroups": [{"query": "tag:#aethrion/work-package",
                         "color": {"a": 1, "rgb": 111}}],
        "scale": 1.0}))
    result = m.apply_colour_groups(tmp_path)
    assert result["status"] == "WRITTEN"
    assert result["foreign_groups_preserved"] == 1
    after = json.loads((obsidian / "graph.json").read_text())
    assert any("aethrion" in g["query"] for g in after["colorGroups"])
    assert after["scale"] == 1.0, "unrelated settings must survive"
    # ...and running it twice does not duplicate.
    m.apply_colour_groups(tmp_path)
    again = json.loads((obsidian / "graph.json").read_text())
    assert len(again["colorGroups"]) == len(after["colorGroups"])
