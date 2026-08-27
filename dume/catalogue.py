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
            wp_id=row["wp"], title=row["title"], workstream=row["stream"],
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
