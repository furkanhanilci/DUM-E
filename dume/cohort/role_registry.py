"""The logical roles DUM-E deploys, and what each one is allowed to decide.

A role is not an agent, not a persona, not a runtime, not a model and not a
provider account. Those five can all change while the role stays the same, and
keeping them separate is what lets a runtime be swapped mid-task without the
authority attached to the work moving with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Role:
    role_id: str
    purpose: str
    decides: str
    # Roles this one must not share an *identity* with. Two distinct agent
    # instances answer two questions; one instance answering both is one
    # opinion wearing two hats.
    independent_of: tuple[str, ...] = ()
    # Roles this one must not share a *model family* with. A stricter and more
    # expensive requirement, reserved for where it actually buys something:
    # two agents from the same family fail the same way, so a check performed
    # by the same family as the work is not independent evidence — it is the
    # same blind spot, twice. Demanding it everywhere would require as many
    # providers as roles, which is why it is not demanded everywhere.
    family_independent_of: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    # A role that may not run on a model that has not been qualified for it.
    requires_qualification: bool = True
    # Whether a fresh context is mandatory rather than merely preferred.
    requires_fresh_context: bool = False
    # Whether this role needs a model at all. Two do not: the human commander is
    # a person, and the orchestrator is the harness itself — it moves work
    # between stages and decides nothing about whether a stage passed, so giving
    # it a model would add a voice with no vote and a quota bill.
    needs_runtime: bool = True


ROLES: dict[str, Role] = {
    "human_commander": Role(
        "human_commander",
        "Final escalation and architecture authority.",
        "Anything the machine may not: scope, architecture conflicts, "
        "irreversible actions, and whether an unsatisfiable requirement is "
        "changed or the design is.",
        requires_qualification=False, needs_runtime=False),

    "commissioning_orchestrator": Role(
        "commissioning_orchestrator",
        "Runs the work-package workflow and manages the cohort.",
        "Sequencing only. The orchestrator moves work between stages; it does "
        "not decide whether a stage passed.",
        requires_qualification=False, needs_runtime=False,
        skills=("brainstorming", "writing-plans")),

    "architect": Role(
        "architect",
        "Turns a frozen packet into an implementation plan.",
        "The shape of the change, and whether the requirement is satisfiable "
        "as written.",
        skills=("brainstorming", "writing-plans", "systems-architecture")),

    "implementer": Role(
        "implementer",
        "Produces the candidate under Superpowers discipline.",
        "Nothing about its own correctness.",
        independent_of=("spec_reviewer", "code_reviewer", "verifier"),
        skills=("test-driven-development", "systematic-debugging",
                "verification-before-completion")),

    "spec_reviewer": Role(
        "spec_reviewer",
        "Compares the candidate against the frozen specification.",
        "Was the requirement met? Nothing about code quality.",
        independent_of=("implementer",),
        family_independent_of=("implementer",),
        skills=("requirements-analysis", "verification-before-completion")),

    "code_reviewer": Role(
        "code_reviewer",
        "Judges implementation quality and architectural fit.",
        "Is the implementation good? Nothing about whether it runs.",
        # Also identity-independent of the spec reviewer: the two ask different
        # questions, and one identity answering both anchors the second answer
        # on the first, which is exactly what the independent-first embargo
        # exists to prevent. Family independence from the implementer is what
        # stops the review sharing the implementation's blind spot.
        independent_of=("implementer", "spec_reviewer"),
        family_independent_of=("implementer",),
        skills=("code-review", "systems-architecture")),

    "verifier": Role(
        "verifier",
        "Runs the acceptance suite from a fresh checkout and environment.",
        "Does it actually work? This is the only role whose PASS is evidence "
        "of behaviour.",
        independent_of=("implementer", "spec_reviewer", "code_reviewer"),
        family_independent_of=("implementer",),
        skills=("verification-before-completion", "systematic-debugging"),
        requires_fresh_context=True),

    "specialist": Role(
        "specialist",
        "Summoned for a bounded concern the cohort is not qualified to judge.",
        "Only its own domain, and only as a finding — never a gate verdict.",
        independent_of=("implementer",)),
}

# Specialists are triggered by evidence in the packet, not by someone
# remembering to ask for one.
SPECIALIST_TRIGGERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("security", "the package touches credentials, capability or trust boundaries",
     ("secret", "credential", "capability", "trust", "auth", "injection",
      "quarantine", "untrusted")),
    ("supply_chain", "the package adopts or pins external code",
     ("upstream", "license", "provenance", "drift", "supply-chain", "direct_adapt")),
    ("data", "the package changes durable state or evidence",
     ("sqlite", "state", "evidence", "persistence", "artifact store", "receipt")),
    ("runtime", "the package binds or switches models and runtimes",
     ("runtime", "quota", "fallback", "routing", "model", "qualification")),
    ("recovery", "the package must survive a crash or be reversible",
     ("restart", "crash", "recovery", "rollback", "resume", "kill-switch",
      "shutdown", "pause")),
)


def role(role_id: str) -> Role:
    try:
        return ROLES[role_id]
    except KeyError:
        raise KeyError(f"no such role: {role_id!r}") from None
