#!/usr/bin/env python3
"""Generate ``docs/WORK_PACKAGES.md`` from the live state store.

The table is generated rather than written because a hand-maintained status
table is a status table that will disagree with the store on the day it matters
— and the store, not a document, is what the gate reads.

Titles come from the store, which reads them from the source catalogue at seed
time. Nothing is renamed here.

Usage:
    python3 scripts/generate_work_packages_doc.py [--check]

``--check`` re-generates into memory and exits non-zero if the file on disk
differs, so a stale or hand-edited document fails rather than misleading.
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "state" / "dume.db"
OUT = REPO / "docs" / "WORK_PACKAGES.md"

STREAM_BLURB = {
    "01_FOUNDATION":
        "Measure the host, draw the boundaries, lock the toolchain. Nothing "
        "above this layer is trustworthy if this one is guessed at.",
    "02_LOCAL_QWEN":
        "A local model that costs no external quota and whose failure does not "
        "depend on someone else's billing period.",
    "03_BUZZ":
        "The collaboration substrate: channels, identity, mentions, audit. A "
        "verdict needs an addressee.",
    "04_SUPERPOWERS":
        "The engineering discipline, pinned and projected into the agents that "
        "are held to it.",
    "05_MODEL_RUNTIME":
        "Runtimes as a catalogue with capabilities, health, cost and failure "
        "classes — availability is not eligibility.",
    "06_DUME_CORE":
        "The harness proper: packets, cohorts, context projection, "
        "orchestration.",
    "07_ENGINEERING_PIPELINE":
        "Worktree to gate: implement, review twice, verify independently, then "
        "decide by machine.",
    "08_SECURITY_CONTROL":
        "What an agent may touch, what a human may command, and what arrives "
        "from outside.",
    "09_OBSERVABILITY_RECOVERY":
        "Seeing what happened, and surviving a restart without inventing state.",
    "10_QUALIFICATION":
        "A model is not qualified by assertion. Trials, per role, recorded.",
    "11_PILOT_COMMISSIONING":
        "Prove it end to end — synthetically first, then for real.",
}

STATE_MEANING = {
    "DISCOVERED": "registered from the catalogue; not started",
    "READY": "every hard dependency has cleared",
    "IN_PROGRESS": "a cohort is bound and working",
    "TECH_COMPLETE": "the implementation is done — which is not acceptance",
    "MERGE_ELIGIBLE": "passed the deterministic gate",
    "ACCEPTANCE_READY":
        "walked the pipeline and reached the gate; awaiting an independent "
        "acceptance identity",
    "ACCEPTED": "independently accepted",
    "BLOCKED": "a precondition is unmet, and improvising past it is not an option",
}


def render() -> str:
    if not DB.is_file():
        sys.exit(f"no state store at {DB} — run `python3 -m dume.cli seed` first")
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = list(conn.execute(
        "SELECT wp_id, title, workstream, wave, state FROM wp ORDER BY wp_id"))
    if not rows:
        sys.exit("the state store holds no work packages — run `dume seed`")
    deps: dict[str, list[str]] = collections.defaultdict(list)
    for wp, dep in conn.execute("SELECT wp_id, depends_on FROM dependency"):
        deps[wp].append(dep)

    counts = collections.Counter(r[4] for r in rows)
    waves = {r[3] for r in rows}
    by_stream: dict[str, list] = {}
    for r in sorted(rows, key=lambda r: (r[2], r[3], r[0])):
        by_stream.setdefault(r[2], []).append(r)

    out: list[str] = []
    add = out.append
    add(f"# The {len(rows)} commissioning work packages\n")
    add("DUM-E is commissioned by the same pipeline it provides. These are its own")
    add("bring-up packages — not work for whatever repository it is later pointed")
    add("at. Each is registered in the state store with its wave and hard")
    add("dependencies, and every state below is **read live from `state/dume.db`**,")
    add("not transcribed.\n")
    add("```bash")
    add("python3 -m dume.cli seed                 # register every package and its dependencies")
    add("python3 -m dume.cli status               # the live table")
    add("python3 -m dume.cli history WP-001       # every transition, with its actor")
    add("python3 -m dume.cli evidence WP-001      # the receipts bound to each candidate")
    add("```\n")
    add("## Where it stands\n")
    add("| State | Count | Means |")
    add("|---|---|---|")
    for state, n in counts.most_common():
        add(f"| `{state}` | {n} | {STATE_MEANING.get(state, '')} |")
    add("")
    if not counts.get("ACCEPTED"):
        add("No package is `ACCEPTED`. That is not a technical gap: acceptance requires")
        add("an independent verifier identity, and the store refuses to let the actor")
        add("that produced this code accept it.\n")
    add("## Waves\n")
    add("A package becomes `READY` only when every hard dependency has cleared. The")
    add(f"{len(rows)} packages resolve into {len(waves)} waves; a wave number is the")
    add("earliest wave a package could start in, not a schedule.\n")
    for stream, items in by_stream.items():
        number, _, name = stream.partition("_")
        add(f"### {number} — {name.replace('_', ' ').title()}\n")
        if stream in STREAM_BLURB:
            add(STREAM_BLURB[stream] + "\n")
        add("| WP | Wave | Title | Depends on | State |")
        add("|---|---|---|---|---|")
        for wp, title, _stream, wave, state in items:
            depends = ", ".join(sorted(deps[wp])) or "—"
            add(f"| `{wp}` | {wave} | {title} | {depends} | `{state}` |")
        add("")
    add("## Regenerating this file\n")
    add("Generated from the live state store. Re-run after a transition rather than")
    add("editing by hand — an edit here is a divergence nothing can detect.\n")
    add("```bash")
    add("python3 scripts/generate_work_packages_doc.py           # write")
    add("python3 scripts/generate_work_packages_doc.py --check   # fail if stale")
    add("```")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="fail if the file on disk is stale or hand-edited")
    args = ap.parse_args()
    rendered = render()
    if args.check:
        current = OUT.read_text() if OUT.is_file() else ""
        if current != rendered:
            print(f"{OUT.relative_to(REPO)} is stale or hand-edited — regenerate it",
                  file=sys.stderr)
            return 1
        print(f"{OUT.relative_to(REPO)} matches the state store")
        return 0
    OUT.write_text(rendered)
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
