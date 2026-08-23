#!/usr/bin/env python3
"""Generate the Obsidian reading mirror of the DUM-E commissioning programme.

The canonical material lives in two places that DUM-E does not own jointly:
the implementation pack (the frozen plan) and this repository (what has actually
been built and measured). The Obsidian tree under
``<vault>/10 - Projects/DUM-E/`` is a **generated reading copy** of both.

Editing the mirror directly creates a divergence nothing can detect, because
nothing seals the mirror. Content changes go into the canonical file first and
propagate from here.

Tags use the ``dume/`` namespace deliberately. The vault's controlled vocabulary
governs ``aethrion/`` only — ``check_vault.py`` validates a tag against the
vocabulary just when it starts with that prefix — so a separate project uses a
separate namespace rather than enlarging someone else's.

Usage:
    python3 scripts/mirror_dume.py [--vault PATH] [--check]

``--check`` writes nothing and exits non-zero if the mirror differs from what
would be generated.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACK = Path("/home/otonom/Desktop/FH/DUME_COMMISSIONING_IMPLEMENTATION_PACK")
DEFAULT_VAULT = Path("/home/otonom/Documents/Obsidian Vault")
PROJECT = "10 - Projects/DUM-E"

WORKSTREAM_TAG = {
    "01_FOUNDATION": "01-foundation", "02_LOCAL_QWEN": "02-local-qwen",
    "03_BUZZ": "03-buzz", "04_SUPERPOWERS": "04-superpowers",
    "05_MODEL_RUNTIME": "05-model-runtime", "06_DUME_CORE": "06-dume-core",
    "07_ENGINEERING_PIPELINE": "07-engineering-pipeline",
    "08_SECURITY_CONTROL": "08-security-control",
    "09_OBSERVABILITY_RECOVERY": "09-observability-recovery",
    "10_QUALIFICATION": "10-qualification",
    "11_PILOT_COMMISSIONING": "11-pilot-commissioning",
}

STATE_TAG = {
    "NOT_STARTED": "not-started", "READY": "ready", "IN_PROGRESS": "in-progress",
    "TECH_COMPLETE": "tech-complete", "ACCEPTED": "accepted",
    "REJECTED": "rejected", "BLOCKED": "blocked",
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# The pack names a package's files `WP-001.md`, `WP-001.tests.md` and
# `WP-001.acceptance.md`, and keeps the descriptive title on the *directory*.
# The vault's habit is the opposite: the descriptive slug belongs in the file
# name, so a search or a graph node says what it is. This map is built once from
# the directory names and is what makes link rewriting resolve.
_SLUGS: dict[str, str] = {}


def build_slug_map() -> dict[str, str]:
    """`WP-001` -> `wp_001_host_hardware_os_and_capacity_inventory`."""
    if _SLUGS:
        return _SLUGS
    for ws_dir in (PACK / "work_packages").iterdir():
        if not ws_dir.is_dir():
            continue
        for wp_dir in ws_dir.iterdir():
            if not wp_dir.is_dir():
                continue
            m = re.match(r"^(WP-\d+)_(.+)$", wp_dir.name)
            if m:
                _SLUGS[m.group(1)] = f"{m.group(1).lower().replace('-', '_')}_{m.group(2)}"
    return _SLUGS


def mirror_name(name: str) -> str:
    """Map a pack file name onto its Obsidian mirror name."""
    slugs = build_slug_map()
    m = re.match(r"^(WP-\d+)((?:\.tests|\.acceptance)?\.md)$", name)
    if m and m.group(1) in slugs:
        return slugs[m.group(1)] + m.group(2)
    m = re.match(r"^(ACC)-(D?\d+)_(.+)$", name)
    if m:
        return f"{m.group(1).lower()}_{m.group(2).lower()}_{m.group(3)}"
    if name == "README.md":
        return "index.md"
    return name


def frontmatter(*, title: str, dume_id: str, note_type: str, category: str,
                tags: list[str], summary: str, source: str,
                status: str = "active", extra: dict | None = None) -> str:
    lines = ["---", f'title: "{title}"', f"dume_id: {dume_id}", f"type: {note_type}",
             f"category: {category}", f"status: {status}",
             f'summary: "{summary}"', "generated: true",
             "provenance: scripts/mirror_dume.py", f'source: "{source}"']
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value}")
    lines.append("tags:")
    lines += [f"  - {t}" for t in tags]
    lines += ["cssclasses:", "  - dume-note", "---", ""]
    return "\n".join(lines)


def rewrite_links(body: str, folder: str) -> str:
    """Turn the pack's relative Markdown links into wikilinks that resolve.

    A link that resolves in the pack directory and lands on nothing in Obsidian
    is the whole of the vault linter's "broken link" count, so they are
    rewritten rather than copied.
    """
    def repl(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return m.group(0)
        target = target.split("#")[0]
        if not target.endswith(".md"):
            return m.group(0)
        name = mirror_name(Path(target).name)[:-3]
        return f"[[{PROJECT}/01 - Commissioning/{folder}/{name}|{label}]]"

    body = re.sub(r"\[`?([^\]`]+)`?\]\(([^)]+)\)", repl, body)
    # Strip a leading H1: the note title lives in frontmatter, and two titles
    # make the vault linter's duplicate-title check meaningless.
    return body


def strip_h1(body: str) -> tuple[str, str]:
    lines = body.splitlines()
    title = ""
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            return title, "\n".join(lines[:i] + lines[i + 1:]).lstrip("\n")
    return title, body


@dataclass
class Note:
    rel: str
    text: str


def wp_notes(states: dict[str, dict]) -> list[Note]:
    notes: list[Note] = []
    for ws_dir in sorted((PACK / "work_packages").iterdir()):
        if not ws_dir.is_dir():
            continue
        ws = ws_dir.name
        for wp_dir in sorted(ws_dir.iterdir()):
            if not wp_dir.is_dir():
                continue
            for src in sorted(wp_dir.glob("*.md")):
                wp_id = src.name.split("_")[0].split(".")[0]
                kind = ("test-procedure" if src.name.endswith(".tests.md")
                        else "acceptance-criteria" if src.name.endswith(".acceptance.md")
                        else "work-package")
                title, body = strip_h1(src.read_text())
                st = states.get(wp_id, {})
                tags = [f"dume/{kind}", "dume/commissioning",
                        f"dume/workstream/{WORKSTREAM_TAG.get(ws, slug(ws))}"]
                if st:
                    tags.append(f"dume/wave/w{st['wave']}")
                    tags.append(f"dume/state/{STATE_TAG.get(st['state'], 'not-started')}")
                extra = {}
                if st:
                    extra = {"wp_state": st["state"], "wave": st["wave"],
                             "workstream": ws}
                    if st.get("candidate_revision"):
                        extra["candidate_revision"] = st["candidate_revision"]
                mirrored = mirror_name(src.name)
                notes.append(Note(
                    f"01 - Commissioning/{ws}/{mirrored}",
                    frontmatter(
                        title=title or src.stem,
                        dume_id=f"DUME-{wp_id}-{kind.upper().replace('-', '_')}",
                        note_type=kind, category="commissioning", tags=tags,
                        summary=f"{wp_id} — {kind.replace('-', ' ')}.",
                        source=str(src.relative_to(PACK)),
                        status=st.get("state", "NOT_STARTED").lower().replace("_", "-"),
                        extra=extra)
                    + f"# {title}\n\n" + rewrite_links(body, ws)))
    return notes


def scenario_notes() -> list[Note]:
    notes: list[Note] = []
    executed = {}
    report = REPO / "evidence" / "acceptance_scenarios.json"
    if report.is_file():
        for r in json.loads(report.read_text())["results"]:
            executed[r["scenario"]] = r
    for src in sorted((PACK / "acceptance_scenarios").glob("*.md")):
        if src.name == "README.md":
            continue
        sid = src.name.split("_")[0]
        title, body = strip_h1(src.read_text())
        result = executed.get(sid, {})
        verdict = result.get("verdict", "NOT_RUN")
        tags = ["dume/acceptance-scenario", "dume/commissioning",
                f"dume/scenario/{verdict.lower().replace('_', '-')}"]
        run_block = ""
        if result:
            run_block = (f"\n> [!{'success' if verdict == 'PASS' else 'info'}] "
                         f"Executed result: **{verdict}**\n"
                         f"> {result.get('observed', '')}\n")
            if result.get("steps"):
                run_block += "\n### What was actually done\n\n" + "\n".join(
                    f"- {s}" for s in result["steps"]) + "\n"
        notes.append(Note(
            f"01 - Commissioning/12_ACCEPTANCE_SCENARIOS/{mirror_name(src.name)}",
            frontmatter(
                title=title or src.stem, dume_id=f"DUME-{sid}",
                note_type="acceptance-scenario", category="commissioning",
                tags=tags, summary=f"{sid} — adversarial acceptance scenario.",
                source=str(src.relative_to(PACK)),
                status=verdict.lower().replace("_", "-"),
                extra={"scenario_verdict": verdict})
            + f"# {title}\n{run_block}\n" + body))
    return notes


def doc_notes() -> list[Note]:
    """Architecture, contracts, runtime and evidence, from both canonical roots."""
    notes: list[Note] = []
    spec = [
        # (source root, source rel, vault rel, type, category, tags, summary)
        (PACK, "01_DUME_ARCHITECTURE.md", "02 - Architecture/dume_architecture.md",
         "architecture", "architecture", ["dume/architecture"],
         "What DUM-E owns, and what it deliberately does not."),
        (PACK, "02_INVARIANTS_AND_AUTHORITY.md",
         "02 - Architecture/invariants_and_authority.md", "architecture",
         "architecture", ["dume/architecture", "dume/invariant"],
         "The twenty-five rules no work package may weaken."),
        (PACK, "05_WORKSPACE_AND_REPOSITORY_LAYOUT.md",
         "02 - Architecture/workspace_and_repository_layout.md", "architecture",
         "architecture", ["dume/architecture"],
         "Three workspaces, one rule each."),
        (PACK, "06_MODEL_AND_RUNTIME_STRATEGY.md",
         "03 - Runtime and Models/model_and_runtime_strategy.md", "execution",
         "runtime", ["dume/runtime", "dume/model-routing"],
         "Which model does which work, and what happens when none is eligible."),
        (PACK, "07_DEFINITION_OF_READY_AND_DONE.md",
         "01 - Commissioning/definition_of_ready_and_done.md", "commissioning",
         "commissioning", ["dume/commissioning"],
         "READY, TECH_COMPLETE and ACCEPTED are three different claims."),
        (PACK, "09_DEPENDENCY_AND_WAVE_PLAN.md",
         "01 - Commissioning/dependency_and_wave_plan.md", "commissioning",
         "commissioning", ["dume/commissioning", "dume/plan"],
         "Twenty-eight waves, derived from hard dependencies."),
        (PACK, "10_ACCEPTANCE_STRATEGY.md",
         "01 - Commissioning/acceptance_strategy.md", "commissioning",
         "commissioning", ["dume/commissioning"],
         "Four layers, and why a green run on an old candidate proves nothing."),
        (PACK, "04_UPSTREAMS_AND_LINKS.md",
         "03 - Runtime and Models/upstreams_and_links.md", "component",
         "runtime", ["dume/component", "dume/upstream"],
         "Every external mechanism, its role and its authority boundary."),
        (REPO, "docs/COMMISSIONING_STATUS.md",
         "05 - Evidence/commissioning_status.md", "evidence", "evidence",
         ["dume/evidence", "dume/status"],
         "What is built, what is measured, and what is explicitly not accepted."),
        (REPO, "README.md", "02 - Architecture/dume_harness_readme.md",
         "execution", "implementation", ["dume/execution"],
         "The harness as built: commands, controls and layout."),
    ]
    for root, rel, vault_rel, note_type, category, tags, summary in spec:
        src = root / rel
        if not src.is_file():
            continue
        title, body = strip_h1(src.read_text())
        notes.append(Note(vault_rel, frontmatter(
            title=title or Path(rel).stem, dume_id=f"DUME-{slug(Path(rel).stem).upper()}",
            note_type=note_type, category=category, tags=tags, summary=summary,
            source=rel) + f"# {title}\n\n" + body))

    for src in sorted((REPO / "docs" / "adr").glob("*.md")):
        title, body = strip_h1(src.read_text())
        notes.append(Note(f"02 - Architecture/{slug(src.stem)}.md", frontmatter(
            title=title or src.stem, dume_id=f"DUME-{src.stem.split('-')[0]}-{src.stem.split('-')[1]}",
            note_type="decision", category="architecture",
            tags=["dume/adr", "dume/architecture", "dume/decision"],
            summary=title, source=f"docs/adr/{src.name}") + f"# {title}\n\n" + body))

    for src in sorted((PACK / "schemas").glob("*.md")):
        title, body = strip_h1(src.read_text())
        notes.append(Note(f"04 - Contracts/{slug(src.stem)}.md", frontmatter(
            title=f"{title or src.stem} — logical contract", dume_id=f"DUME-SCHEMA-{slug(src.stem).upper()}",
            note_type="contract", category="contracts", tags=["dume/contract"],
            summary=f"Logical contract: {src.stem.replace('.schema', '')}.",
            source=f"schemas/{src.name}") + f"# {title} — logical contract\n\n" + body))

    for src in sorted((PACK / "runbooks").glob("*.md")):
        title, body = strip_h1(src.read_text())
        notes.append(Note(f"03 - Runtime and Models/{slug(src.stem)}.md", frontmatter(
            title=title or src.stem, dume_id=f"DUME-RUNBOOK-{slug(src.stem).upper()}",
            note_type="execution", category="runtime", tags=["dume/runbook", "dume/execution"],
            summary=f"Runbook: {src.stem.replace('_', ' ').lower()}.",
            source=f"runbooks/{src.name}") + f"# {title}\n\n" + body))

    for src in sorted((PACK / "diagrams").glob("*.mmd")):
        title = src.stem.split("_", 1)[1].replace("_", " ").title()
        notes.append(Note(f"02 - Architecture/diagram_{slug(src.stem)}.md", frontmatter(
            title=f"Diagram — {title}", dume_id=f"DUME-DIAGRAM-{slug(src.stem).upper()}",
            note_type="architecture", category="architecture",
            tags=["dume/architecture", "dume/diagram"],
            summary=f"Architecture flow: {title.lower()}.",
            source=f"diagrams/{src.name}")
            + f"# Diagram — {title}\n\n```mermaid\n{src.read_text().strip()}\n```\n"))
    return notes


def read_states() -> dict[str, dict]:
    sys.path.insert(0, str(REPO))
    try:
        from dume.state import Store
    except ImportError:
        return {}
    db = REPO / "state" / "dume.db"
    if not db.is_file():
        return {}
    store = Store(db)
    try:
        return {r["wp_id"]: dict(r) for r in store.all_wps()}
    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--check", action="store_true",
                    help="write nothing; exit non-zero if the mirror is stale")
    args = ap.parse_args()

    states = read_states()
    notes = wp_notes(states) + scenario_notes() + doc_notes()
    notes += index_notes(states, notes)

    root = args.vault / PROJECT
    stale: list[str] = []
    for note in notes:
        target = root / note.rel
        if args.check:
            if not target.is_file() or target.read_text() != note.text:
                stale.append(note.rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(note.text)

    if args.check:
        for rel in stale:
            print(f"stale: {rel}")
        print(f"{len(stale)} of {len(notes)} notes differ")
        return 1 if stale else 0

    link_projects_index(args.vault)
    print(f"mirrored {len(notes)} notes into {root}")
    return 0


def index_notes(states: dict[str, dict], notes: list[Note]) -> list[Note]:
    """The cockpit and one index per area, so nothing is an orphan."""
    out: list[Note] = []
    counts: dict[str, int] = {}
    for st in states.values():
        counts[st["state"]] = counts.get(st["state"], 0) + 1

    rev = ""
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        pass

    state_rows = "\n".join(
        f"| `{s}` | {c} |" for s, c in sorted(counts.items(), key=lambda kv: -kv[1]))

    scenario_report = REPO / "evidence" / "acceptance_scenarios.json"
    scen = json.loads(scenario_report.read_text()) if scenario_report.is_file() else {}

    cockpit = f"""# DUM-E — Navigation and Execution Cockpit

> [!important] The boundary this project exists to hold
> **DUM-E builds AETHRION. AETHRION does the science.** DUM-E is a temporary
> commissioning harness. When AETHRION has commissioned equivalent capabilities,
> DUM-E shrinks into a thin commissioning profile or is retired. It must not
> grow into a second AETHRION.

> [!warning] This is a generated reading mirror
> Canonical sources: the implementation pack at
> `DUME_COMMISSIONING_IMPLEMENTATION_PACK/` (the frozen plan) and the harness
> repository at `Desktop/FH/DUM-E/` (what is actually built and measured).
> Edit those, then re-run `scripts/mirror_dume.py`. An edit made here is a
> divergence nothing can detect.

## Current state

| Field | Value |
|---|---|
| Harness candidate | `{rev}` |
| Verdict | `TECH_COMPLETE` for WP-001 — **not accepted** |
| Blocking decision | no independent verifier is bound yet |
| Host class | `SINGLE_GPU_CONSTRAINED` — 41.1 GiB usable VRAM, no NVLink |
| Next package | WP-005 — local Qwen deployment profile decision |

### Work-package states

| State | Packages |
|---|---|
{state_rows}

### Adversarial scenarios

{scen.get('passed', 0)} of {scen.get('executed', 0)} executed scenarios passed;
{scen.get('deferred', 0)} are deferred to the wave that builds their subject and
are listed rather than counted as passes.

## Area map

| Area | Content | Index |
|---|---|---|
| `01 - Commissioning/` | 54 work packages, 36 acceptance scenarios | [[{PROJECT}/01 - Commissioning/commissioning_index\\|Commissioning Index]] |
| `02 - Architecture/` | Architecture, invariants, DUME-ADRs, diagrams | [[{PROJECT}/02 - Architecture/architecture_index\\|Architecture Index]] |
| `03 - Runtime and Models/` | Model routing, upstreams, runbooks | [[{PROJECT}/03 - Runtime and Models/runtime_index\\|Runtime Index]] |
| `04 - Contracts/` | Nine logical contracts | [[{PROJECT}/04 - Contracts/contracts_index\\|Contracts Index]] |
| `05 - Evidence/` | Measured results and commissioning status | [[{PROJECT}/05 - Evidence/evidence_index\\|Evidence Index]] |

## The rule for every step

A package is not done because an agent says so. `TECH_COMPLETE` is not
`ACCEPTED`; the producer may not accept its own work; evidence binds to an exact
candidate revision, and a green result from an older candidate is stale. These
are enforced in the harness, not requested of it — see
[[{PROJECT}/02 - Architecture/invariants_and_authority|Invariants and Authority]].
"""
    out.append(Note("00_navigation_and_execution_cockpit.md", frontmatter(
        title="DUM-E — Navigation and Execution Cockpit", dume_id="DUME-COCKPIT",
        note_type="project", category="project",
        tags=["dume/project", "dume/cockpit", "dume/plan"],
        summary="Where DUM-E commissioning stands, and where to go next.",
        source="generated", extra={"owner": "otonom",
                                   "candidate_revision": rev or "unknown"}) + cockpit))

    def listing(prefix: str) -> str:
        rows = []
        for n in sorted(notes, key=lambda n: n.rel):
            if not n.rel.startswith(prefix) or n.rel.endswith("_index.md"):
                continue
            title = re.search(r'^title: "(.+)"$', n.text, re.M)
            rows.append(f"- [[{PROJECT}/{n.rel[:-3]}|{title.group(1) if title else n.rel}]]")
        return "\n".join(rows)

    areas = [
        ("01 - Commissioning/commissioning_index.md", "Commissioning Index",
         "01 - Commissioning/", "commissioning",
         "54 work packages across 28 waves, and 36 adversarial acceptance scenarios."),
        ("02 - Architecture/architecture_index.md", "Architecture Index",
         "02 - Architecture/", "architecture",
         "What DUM-E owns, the invariants, the decisions and the flows."),
        ("03 - Runtime and Models/runtime_index.md", "Runtime Index",
         "03 - Runtime and Models/", "runtime",
         "Model routing, upstream mechanisms and bring-up runbooks."),
        ("04 - Contracts/contracts_index.md", "Contracts Index",
         "04 - Contracts/", "contracts",
         "The logical contracts a packet, handoff or record must satisfy."),
        ("05 - Evidence/evidence_index.md", "Evidence Index",
         "05 - Evidence/", "evidence",
         "Measured results bound to a candidate revision."),
    ]
    for rel, title, prefix, category, summary in areas:
        out.append(Note(rel, frontmatter(
            title=f"DUM-E — {title}", dume_id=f"DUME-{slug(title).upper()}",
            note_type="index", category=category,
            tags=["dume/index", f"dume/{category}"], summary=summary,
            source="generated")
            + f"# DUM-E — {title}\n\n{summary}\n\n"
              f"[[{PROJECT}/00_navigation_and_execution_cockpit|← Cockpit]]\n\n"
            + listing(prefix) + "\n"))
    return out


def link_projects_index(vault: Path) -> None:
    """Add DUM-E to the vault's Projects index, so the cockpit is not an orphan."""
    index = vault / "10 - Projects" / "projects.md"
    if not index.is_file():
        return
    text = index.read_text()
    link = f"- [[{PROJECT}/00_navigation_and_execution_cockpit|DUM-E — Navigation and Execution Cockpit]]"
    if link in text:
        return
    marker = "## Active system work"
    if marker not in text:
        return
    lines = text.split("\n")
    at = lines.index(marker)
    end = at + 1
    while end < len(lines) and (lines[end].startswith("- ") or not lines[end].strip()):
        end += 1
    lines.insert(end - 1, link)
    index.write_text("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
