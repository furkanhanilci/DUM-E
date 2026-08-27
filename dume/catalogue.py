"""Load the 54-package commissioning catalogue into durable state.

The catalogue is read from the implementation pack rather than retyped, because
a hand-copied dependency list is a dependency list that will disagree with the
plan on the day it matters. The pack is the source; DUM-E state is the record of
what has actually happened to each package.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .state import Store

DEFAULT_PACK = Path("/home/otonom/Desktop/FH/DUME_COMMISSIONING_IMPLEMENTATION_PACK")

# Three packages are stated in the source catalogue in terms of one specific
# target repository. DUM-E holds no opinion about what its target is — the
# mechanism each package builds is identical whatever it is pointed at — so they
# are registered by what is being built rather than by whose repository it is
# being built for. Overriding at seed time rather than editing the pack keeps the
# pack as the single source for everything else: waves, dependencies, streams.
TITLE_OVERRIDES: dict[str, str] = {
    "WP-029": "Deterministic WP Packet Builder",
    "WP-053": "First Real Low-Risk Target-Repository Pilot",
    "WP-054": "Two Heterogeneous Pilots and DUM-E v0.1 Acceptance",
}


def seed(store: Store, pack: Path | None = None) -> dict:
    """Register every package and its hard dependencies. Idempotent."""
    pack = Path(pack) if pack else DEFAULT_PACK
    manifest = pack / "wp_manifest.csv"
    if not manifest.is_file():
        raise FileNotFoundError(f"no work-package manifest at {manifest}")

    rows = list(csv.DictReader(manifest.open()))
    for row in rows:
        deps = [d.strip() for d in (row["dependencies"] or "").split(";") if d.strip()]
        # The manifest uses a comma inside quoted titles, so dependencies are
        # split on both separators the pack uses in practice.
        if len(deps) == 1 and "," in deps[0]:
            deps = [d.strip() for d in deps[0].split(",") if d.strip()]
        store.register(
            wp_id=row["wp"],
            title=TITLE_OVERRIDES.get(row["wp"], row["title"]),
            workstream=row["stream"],
            wave=int(row["wave"]), depends_on=deps)

    dangling = []
    known = {r["wp"] for r in rows}
    for row in rows:
        for dep in store.dependencies(row["wp"]):
            if dep not in known:
                dangling.append((row["wp"], dep))

    return {
        "packages": len(rows),
        "waves": sorted({int(r["wave"]) for r in rows}),
        "dangling_dependencies": dangling,
        "source": str(manifest),
    }
