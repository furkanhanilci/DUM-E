"""The Cohort Compiler.

After a packet exists, one question remains: which roles does *this* work need,
and who may not be the same identity as whom?

Answering it with `complexity == high → five agents` would be a guess dressed as
a policy. The signals below are read out of the packet itself — what the package
touches, what it must not weaken, which scenarios will attack it, how much
assurance its acceptance demands — so the cohort is derived from the work rather
than from an adjective someone attached to it.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..packets.wp_packet_builder import WPPacket
from .role_registry import ROLES, SPECIALIST_TRIGGERS, Role, role


@dataclass
class RoleSlot:
    role_id: str
    purpose: str
    decides: str
    independence_requirements: list[str] = field(default_factory=list)
    qualification_required: bool = True
    fresh_context_required: bool = False
    skill_bundle: list[str] = field(default_factory=list)
    task: str | None = None
    reason: str = ""


@dataclass
class CohortManifest:
    wp_id: str
    packet_sha256: str
    assurance_level: str
    signals: dict
    roles: list[RoleSlot] = field(default_factory=list)
    specialists: list[dict] = field(default_factory=list)
    communication_topology: dict = field(default_factory=dict)
    context_projection: dict = field(default_factory=dict)
    independence_matrix: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        return d

    def role_ids(self) -> list[str]:
        return [r.role_id for r in self.roles]


# Sections every package card carries verbatim. Reading them as evidence about
# *this* package is how a detector ends up calling all 54 packages
# security-critical: the boilerplate mentions credentials, rollback, evidence
# and authority in every single file. Signals must come from the parts that
# differ between packages.
BOILERPLATE_HEADINGS = (
    "Detailed execution sequence", "Mandatory engineering discipline",
    "Fresh-research obligation", "Definition of Ready",
    "Definition of Tech Complete", "Handoff packet to verifier",
)

PACKAGE_SPECIFIC_HEADINGS = (
    "Purpose", "Scope", "Mandatory deliverables",
    "Package-specific research obligations",
    "Known failure modes that MUST be tested or mechanically controlled",
)


def _section_text(card: str, heading: str) -> str:
    m = re.search(rf"^#+\s*{re.escape(heading)}\s*$", card, re.M | re.I)
    if not m:
        return ""
    rest = card[m.end():]
    stop = re.search(r"^#+\s", rest, re.M)
    return rest[:stop.start()] if stop else rest


def _haystack(packet: WPPacket) -> str:
    """The parts of the packet that are about *this* package, lowercased.

    Deliberately narrow. The card's shared scaffolding names every risk the
    programme cares about, so including it makes every signal fire for every
    package and the cohort stops being derived from the work at all.
    """
    card = next((s.text for s in packet.sections if s.name == "card"), "")
    parts = [packet.title, packet.workstream]
    parts += [_section_text(card, h) for h in PACKAGE_SPECIFIC_HEADINGS]
    parts += packet.deliverables + packet.known_failure_modes
    return " ".join(parts).lower()


def detect_signals(packet: WPPacket) -> dict:
    """What kind of work is this, judged from the packet."""
    text = _haystack(packet)
    scenario_count = len(packet.acceptance_scenarios)
    return {
        "touches_security_boundary": any(
            k in text for k in ("credential", "secret", "capability boundary",
                                "untrusted", "injection", "authenticated")),
        "touches_durable_state": any(
            k in text for k in ("sqlite", "durable", "persist", "state store",
                                "evidence", "receipt")),
        "touches_external_code": any(
            k in text for k in ("upstream", "license", "provenance",
                                "direct_adapt", "dependency")),
        "touches_runtime_binding": any(
            k in text for k in ("runtime", "quota", "fallback", "model",
                                "qualification")),
        "must_be_reversible": any(
            k in text for k in ("rollback", "recovery", "restart", "crash",
                                "resume", "kill switch", "kill-switch")),
        "is_architecture_bearing": any(
            k in text for k in ("architecture", "authority", "invariant",
                                "contract", "schema")),
        "adversarial_scenarios": scenario_count,
        "deliverable_count": len(packet.deliverables),
        "known_failure_modes": len(packet.known_failure_modes),
        "dependency_count": len(packet.dependencies),
        "wave": packet.wave,
    }


def assurance_level(signals: dict) -> str:
    """How much independent scrutiny this package's result has to survive.

    Assurance never shrinks because a package looks small. It grows when the
    package can hurt something that is hard to undo.
    """
    weight = 0
    if signals["touches_security_boundary"]:
        # Enough on its own to leave BASELINE. A package whose whole subject is
        # a trust boundary cannot be the cheapest tier of scrutiny, and a
        # boundary that fails is not a defect anyone notices from the outside.
        weight += 3
    if signals["touches_durable_state"]:
        weight += 1
    if signals["touches_external_code"]:
        weight += 1
    if signals["touches_runtime_binding"]:
        weight += 1
    if signals["must_be_reversible"]:
        weight += 1
    if signals["is_architecture_bearing"]:
        weight += 1
    weight += min(signals["adversarial_scenarios"], 2)
    if weight >= 5:
        return "HIGH"
    if weight >= 3:
        return "STANDARD"
    return "BASELINE"


def _implementer_tasks(packet: WPPacket) -> list[str]:
    """Split implementation only where the deliverables genuinely split.

    Two implementers on one indivisible deliverable is two people editing the
    same file; the split has to come from the work.
    """
    deliverables = [d for d in packet.deliverables if d]
    if len(deliverables) <= 3:
        return ["the whole package"]
    # Group by extension: a policy file, a module and a document are different
    # kinds of work even inside one package.
    code = [d for d in deliverables if re.search(r"\.(py|rs|ts|go)$", d)]
    config = [d for d in deliverables if re.search(r"\.(ya?ml|json|toml)$", d)]
    docs = [d for d in deliverables if d.endswith(".md")]
    groups = [g for g in (code, config, docs) if g]
    if len(groups) < 2:
        return ["the whole package"]
    return [", ".join(g) for g in groups]


def compile_cohort(packet: WPPacket) -> CohortManifest:
    signals = detect_signals(packet)
    level = assurance_level(signals)

    def slot(role_id: str, reason: str, task: str | None = None) -> RoleSlot:
        r: Role = role(role_id)
        return RoleSlot(
            role_id=r.role_id, purpose=r.purpose, decides=r.decides,
            independence_requirements=[
                f"must not be the same identity as {other}"
                for other in r.independent_of]
            + [f"must not share a model family with {other}"
               for other in r.family_independent_of],
            qualification_required=r.requires_qualification,
            fresh_context_required=r.requires_fresh_context,
            skill_bundle=list(r.skills), task=task, reason=reason)

    roles = [
        slot("commissioning_orchestrator", "every package needs sequencing"),
        slot("architect",
             "the plan is a separate artefact from the code, and the packet is "
             "frozen input to it"),
    ]
    for task in _implementer_tasks(packet):
        roles.append(slot("implementer", "produces the candidate", task=task))
    roles += [
        slot("spec_reviewer", "answers: was the requirement met?"),
        slot("code_reviewer", "answers: is the implementation good?"),
        slot("verifier", "answers: does it actually work?"),
    ]

    text = _haystack(packet)
    specialists = []
    for name, why, keywords in SPECIALIST_TRIGGERS:
        hits = sorted({k for k in keywords if k in text})
        if hits:
            specialists.append({
                "specialist": name, "trigger": why, "matched": hits,
                "authority": "raises findings only; never a gate verdict"})

    # A HIGH-assurance package gets a second, independently-bound verifier: one
    # verifier's environment can be wrong in a way only another environment
    # reveals.
    if level == "HIGH":
        roles.append(slot(
            "verifier",
            "HIGH assurance — a second verifier on an independently bound "
            "runtime, because one environment can be wrong in a way only "
            "another environment shows"))

    manifest = CohortManifest(
        wp_id=packet.wp_id, packet_sha256=packet.packet_sha256,
        assurance_level=level, signals=signals, roles=roles,
        specialists=specialists,
        communication_topology={
            "channel": f"DUME-{packet.wp_id}",
            "threads": ["plan", "implementation", "review", "verification"],
            "rule": "operational only — a message is never a gate verdict",
            "human_addressing": [f"@{r.role_id}" for r in roles],
        },
        context_projection={
            "every_role_receives": ["the frozen packet", "the candidate revision",
                                    "its own role card"],
            "implementer_also_receives": ["its worktree path", "the accepted plan"],
            "reviewer_receives": ["the candidate diff", "the frozen acceptance"],
            "verifier_receives": ["a fresh checkout", "the acceptance suite"],
            "nobody_receives": ["another agent's conversation history",
                                "an earlier reviewer's verdict before forming "
                                "their own", "any credential"],
            "embargo": "independent-first — a reviewer's verdict is not visible "
                       "to the next reviewer until their own is recorded, so "
                       "the second opinion is a second opinion",
        },
        independence_matrix=(
            [f"identity: {r.role_id} ≠ {other}"
             for r in roles for other in ROLES[r.role_id].independent_of]
            + [f"family: {r.role_id} ≠ {other}"
               for r in roles for other in ROLES[r.role_id].family_independent_of]),
    )
    return manifest
