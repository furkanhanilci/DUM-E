"""The WP Packet Builder — the one component DUM-E cannot borrow.

Handing an agent `WP-048.md` is not enough. Implementing a package correctly
depends on a dozen decisions recorded elsewhere: the tests that define done, the
acceptance criteria nobody may edit, the architecture decisions that constrain
the shape, the outputs of dependencies already accepted, the scenarios that will
attack the result, the paths that must not be touched.

The packet is assembled **mechanically**. It is never an LLM's summary of the
plan, because a summary loses exactly the clause that turns out to matter, and
loses it silently. If a packet is missing something, that is a bug in this file
and it is fixable; if a summary is missing something, nobody finds out until the
gate refuses the result or, worse, does not.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..secrets import assert_clean
from ..state import sha256_file

PACK = Path("/home/otonom/Desktop/FH/DUME_COMMISSIONING_IMPLEMENTATION_PACK")

# Changes an implementing agent may never make, carried in every packet. These
# are not advice: the worktree manager and the gate enforce them, and the packet
# states them so that no agent can claim it did not know.
FORBIDDEN = (
    "modify_frozen_acceptance",
    "modify_sealed_specification",
    "weaken_or_delete_a_failing_test",
    "grant_a_runtime_scientific_authority",
    "give_an_agent_a_credential_directly",
    "accept_or_review_own_work",
    "substitute_evidence_from_another_candidate",
)

# The engineering skills the protocol requires for any behavioural change.
REQUIRED_SKILLS = (
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
)


class PacketError(RuntimeError):
    """A packet could not be assembled from complete, non-contradictory sources."""


@dataclass
class Section:
    """One source document, carried whole with its digest."""
    name: str
    path: str
    sha256: str
    text: str

    def as_dict(self, include_text: bool = True) -> dict:
        d = {"name": self.name, "path": self.path, "sha256": self.sha256}
        if include_text:
            d["text"] = self.text
        return d


@dataclass
class WPPacket:
    wp_id: str
    title: str
    workstream: str
    wave: int
    owner: str
    verifier_role: str
    spec_revision: str
    sections: list[Section] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)
    acceptance_scenarios: list[dict] = field(default_factory=list)
    schemas: list[dict] = field(default_factory=list)
    adrs: list[dict] = field(default_factory=list)
    upstreams: list[dict] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    evidence_obligations: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    known_failure_modes: list[str] = field(default_factory=list)
    non_waivable_rules: list[str] = field(default_factory=list)
    packet_sha256: str = ""

    def as_dict(self, include_text: bool = True) -> dict:
        d = asdict(self)
        d["sections"] = [s.as_dict(include_text) for s in self.sections]
        return d


def _read(path: Path) -> Section:
    text = path.read_text()
    return Section(name=path.name, path=str(path), sha256=sha256_file(path), text=text)


def _bullets_under(text: str, heading: str) -> list[str]:
    """Every bullet under a Markdown heading, in order, unabridged."""
    m = re.search(rf"^#+\s*{re.escape(heading)}\s*$", text, re.M | re.I)
    if not m:
        return []
    rest = text[m.end():]
    stop = re.search(r"^#+\s", rest, re.M)
    block = rest[:stop.start()] if stop else rest
    return [re.sub(r"^[-*]\s+", "", line).strip()
            for line in block.splitlines() if line.strip().startswith(("- ", "* "))]


def _table_field(text: str, field_name: str) -> str:
    m = re.search(rf"^\|\s*{re.escape(field_name)}\s*\|\s*(.+?)\s*\|\s*$", text, re.M)
    return m.group(1).strip("` ") if m else ""


class PacketBuilder:
    """Assembles a work-package execution packet from the frozen plan."""

    def __init__(self, pack: Path | None = None, spec_revision: str = "pack"):
        self.pack = Path(pack) if pack else PACK
        self.spec_revision = spec_revision
        if not (self.pack / "wp_manifest.csv").is_file():
            raise PacketError(f"no commissioning plan at {self.pack}")
        self._dirs = self._index_directories()
        self._manifest = self._read_manifest()

    def _index_directories(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for ws in (self.pack / "work_packages").iterdir():
            if not ws.is_dir():
                continue
            for wp in ws.iterdir():
                if wp.is_dir() and "_" in wp.name:
                    index[wp.name.split("_")[0]] = wp
        return index

    def _read_manifest(self) -> dict[str, dict]:
        import csv
        rows = {}
        with (self.pack / "wp_manifest.csv").open() as fh:
            for row in csv.DictReader(fh):
                rows[row["wp"]] = row
        return rows

    # ---- assembly -------------------------------------------------------

    def build(self, wp_id: str, dependency_states: dict[str, dict] | None = None
              ) -> WPPacket:
        if wp_id not in self._manifest:
            raise PacketError(f"{wp_id} is not in the commissioning plan")
        meta = self._manifest[wp_id]
        wp_dir = self._dirs.get(wp_id)
        if wp_dir is None:
            raise PacketError(f"{wp_id} has no package directory in the plan")

        sections = []
        for suffix, name in ((".md", "card"), (".tests.md", "tests"),
                             (".acceptance.md", "acceptance")):
            path = wp_dir / f"{wp_id}{suffix}"
            if not path.is_file():
                raise PacketError(f"{wp_id} is missing its {name}: {path}")
            section = _read(path)
            section.name = name
            sections.append(section)

        card = sections[0].text
        packet = WPPacket(
            wp_id=wp_id, title=meta["title"], workstream=meta["stream"],
            wave=int(meta["wave"]), owner=meta["owner"],
            verifier_role=meta["verifier"], spec_revision=self.spec_revision,
            sections=sections,
            deliverables=_bullets_under(card, "Mandatory deliverables"),
            known_failure_modes=_bullets_under(
                card, "Known failure modes that MUST be tested or mechanically controlled"),
            required_skills=list(REQUIRED_SKILLS),
            forbidden=list(FORBIDDEN),
        )

        packet.dependencies = self._dependencies(wp_id, meta, dependency_states or {})
        packet.acceptance_scenarios = self._scenarios(wp_id, card)
        packet.schemas = self._schemas()
        packet.adrs = self._adrs()
        packet.upstreams = self._upstreams()
        packet.protected_paths = self._protected_paths()
        packet.evidence_obligations = self._evidence_obligations(sections[1].text)
        packet.non_waivable_rules = self._non_waivable_rules()
        packet.packet_sha256 = self._digest(packet)

        # A packet is handed to an agent and written to disk. Invariant 19 says
        # a credential never travels in one.
        assert_clean(json.dumps(packet.as_dict()), where=f"{wp_id} packet")
        return packet

    def _dependencies(self, wp_id: str, meta: dict, states: dict[str, dict]
                      ) -> list[dict]:
        raw = (meta.get("dependencies") or "").replace(";", ",")
        deps = [d.strip() for d in raw.split(",") if d.strip()]
        out = []
        for dep in deps:
            state = states.get(dep, {})
            entry = {
                "wp_id": dep,
                "title": self._manifest.get(dep, {}).get("title", ""),
                "state": state.get("state", "UNKNOWN"),
                "candidate_revision": state.get("candidate_revision"),
                "required_outputs": [],
            }
            dep_dir = self._dirs.get(dep)
            if dep_dir and (dep_dir / f"{dep}.md").is_file():
                entry["required_outputs"] = _bullets_under(
                    (dep_dir / f"{dep}.md").read_text(), "Mandatory deliverables")
            out.append(entry)
        return out

    def _scenarios(self, wp_id: str, card: str) -> list[dict]:
        """Scenarios that will attack this package's subject.

        Matched by the work package a scenario is deferred to, which is recorded
        in one place — the scenario runner — so the packet and the runner cannot
        disagree about which scenario belongs to which package.
        """
        from ..scenarios import DEFERRED
        out = []
        for sid, (title, gate) in sorted(DEFERRED.items()):
            if gate == wp_id:
                path = next((self.pack / "acceptance_scenarios").glob(f"{sid}_*.md"), None)
                out.append({"scenario": sid, "title": title,
                            "path": str(path) if path else None,
                            "sha256": sha256_file(path) if path else None})
        return out

    def _schemas(self) -> list[dict]:
        return [{"name": p.stem.replace(".schema", ""), "path": str(p),
                 "sha256": sha256_file(p), "text": p.read_text()}
                for p in sorted((self.pack / "schemas").glob("*.md"))]

    def _adrs(self) -> list[dict]:
        adr_dir = Path(__file__).resolve().parent.parent.parent / "docs" / "adr"
        if not adr_dir.is_dir():
            return []
        return [{"id": p.stem.split("-")[0] + "-" + p.stem.split("-")[1],
                 "path": str(p), "sha256": sha256_file(p),
                 "title": next((l[2:].strip() for l in p.read_text().splitlines()
                                if l.startswith("# ")), p.stem)}
                for p in sorted(adr_dir.glob("*.md"))]

    def _upstreams(self) -> list[dict]:
        lock = Path(__file__).resolve().parent.parent.parent / "config" / "upstream.lock.json"
        if not lock.is_file():
            return []
        data = json.loads(lock.read_text())
        return [{"name": u["name"], "pinned_revision": u.get("pinned_revision"),
                 "license": u.get("license"), "reuse_class": u.get("reuse_class"),
                 "authority_boundary": u.get("authority_boundary")}
                for u in data["upstreams"]]

    def _protected_paths(self) -> list[str]:
        cfg = Path(__file__).resolve().parent.parent.parent / "config" / "dume.config.json"
        if not cfg.is_file():
            return []
        return json.loads(cfg.read_text()).get("protected_paths", [])

    def _evidence_obligations(self, tests: str) -> list[str]:
        m = re.search(r"^##\s*Mandatory evidence\s*$", tests, re.M)
        if not m:
            return []
        rest = tests[m.end():]
        stop = re.search(r"^##\s", rest, re.M)
        block = (rest[:stop.start()] if stop else rest).strip()
        return [part.strip() for part in re.split(r",\s*(?![^(]*\))", block)
                if part.strip()][:12]

    def _non_waivable_rules(self) -> list[str]:
        path = self.pack / "02_INVARIANTS_AND_AUTHORITY.md"
        if not path.is_file():
            return []
        return [re.sub(r"^\d+\.\s*", "", line).strip()
                for line in path.read_text().splitlines()
                if re.match(r"^\d+\.\s", line)]

    def _digest(self, packet: WPPacket) -> str:
        material = json.dumps(packet.as_dict(include_text=False), sort_keys=True)
        return hashlib.sha256(material.encode()).hexdigest()

    # ---- output ---------------------------------------------------------

    def write(self, packet: WPPacket, out_dir: Path) -> Path:
        """Write the packet where an agent and a verifier can both read it."""
        from ..state import json_dump
        out_dir = Path(out_dir)
        path = out_dir / f"{packet.wp_id}.packet.json"
        json_dump(packet.as_dict(), path)
        return path
