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
