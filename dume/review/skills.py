"""WP-021 — putting the engineering discipline into the agents themselves.

Superpowers is installed, pinned and enabled, and until now it disciplined
nobody who matters here. It is a Claude Code / Codex / Hermes plugin, and DUM-E's
agents are raw OpenAI-compatible endpoints behind llama.cpp — so the plugin
shapes the harness author's session and never reaches Qwen or Mistral.

This module closes that. It reads the actual `SKILL.md` files out of the
installed plugin, at the revision the upstream lock pins, and projects a bundle
into each role's system prompt. What the agents are held to is then a versioned,
pinned artefact with a digest, rather than prose the harness author invented and
can drift without noticing.

Two things it deliberately does not do:

* **It does not summarise a skill.** A skill is an instruction; a summary of an
  instruction is a different instruction. Primary skills go in whole.
* **It does not claim the skill was obeyed.** Injection is an input. Whether the
  agent followed it is answered by the red-then-green exit codes and the
  independent reviews, and by `dume.review.discipline`, which keeps invocation,
  artefact and independent evidence in separate columns.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

PLUGIN_ROOT = Path.home() / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "superpowers"
PLUGIN_RECORD = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
PLUGIN_KEY = "superpowers@claude-plugins-official"

# Which skills each role is held to, and which one is primary — the primary goes
# in whole, the rest as their own frontmatter description plus overview, because
# a role needs one discipline in full and awareness of the others.
ROLE_BUNDLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "architect": ("brainstorming", ("writing-plans",)),
    "implementer": ("test-driven-development",
                    ("systematic-debugging", "verification-before-completion")),
    "spec_reviewer": ("verification-before-completion", ("brainstorming",)),
    "code_reviewer": ("requesting-code-review", ("receiving-code-review",)),
    "verifier": ("verification-before-completion", ("systematic-debugging",)),
    "specialist": ("systematic-debugging", ()),
}

# A budget, because a skill that crowds the frozen packet out of the context
# window has replaced the requirement with advice about how to meet it.
PRIMARY_BUDGET = 9000
SECONDARY_BUDGET = 1200


class SkillsUnavailable(RuntimeError):
    """The pinned Superpowers install could not be read.

    Raised rather than silently falling back to harness-authored prose: an agent
    running without the discipline it is supposed to have is a fact the run must
    record, not paper over.
    """


@dataclass
class InjectedSkill:
    name: str
    role: str
    primary: bool
    path: str
    sha256: str
    characters: int
    truncated: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SkillBundle:
    role: str
    revision: str | None
    text: str
    skills: list[InjectedSkill] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"role": self.role, "revision": self.revision,
                "characters": len(self.text),
                "skills": [s.as_dict() for s in self.skills]}


def installed_root() -> Path:
    """The versioned directory the plugin actually unpacked into."""
    if not PLUGIN_ROOT.is_dir():
        raise SkillsUnavailable(f"Superpowers is not installed at {PLUGIN_ROOT}")
    versions = sorted((p for p in PLUGIN_ROOT.iterdir() if p.is_dir()),
                      key=lambda p: p.name)
    if not versions:
        raise SkillsUnavailable(f"no version directory under {PLUGIN_ROOT}")
    return versions[-1]


def installed_revision() -> str | None:
    if not PLUGIN_RECORD.is_file():
        return None
    try:
        data = json.loads(PLUGIN_RECORD.read_text())
    except json.JSONDecodeError:
        return None

    def find(obj):
        if isinstance(obj, dict):
            if PLUGIN_KEY in obj:
                entry = obj[PLUGIN_KEY]
                entry = entry[0] if isinstance(entry, list) else entry
                return entry.get("gitCommitSha")
            for value in obj.values():
                found = find(value)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = find(item)
                if found:
                    return found
        return None

    return find(data)


def _read_skill(name: str) -> tuple[str, Path]:
    path = installed_root() / "skills" / name / "SKILL.md"
    if not path.is_file():
        raise SkillsUnavailable(f"no such skill: {name} (looked in {path})")
    return path.read_text(errors="replace"), path


def _strip_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not match:
        return {}, text
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, match.group(2)


def _overview(body: str, limit: int) -> str:
    """The skill's own opening, up to its second heading. Its words, not mine."""
    parts = re.split(r"\n## ", body, maxsplit=2)
    head = parts[0]
    if len(parts) > 1:
        head += "\n## " + parts[1]
    return head[:limit].rstrip()


def bundle_for(role: str, *, expected_revision: str | None = None) -> SkillBundle:
    """The discipline this role is held to, read from the pinned install."""
    if role not in ROLE_BUNDLES:
        raise SkillsUnavailable(f"no skill bundle defined for role {role!r}")
    revision = installed_revision()
    if expected_revision and revision != expected_revision:
        raise SkillsUnavailable(
            f"Superpowers is at {revision}, the lock pins {expected_revision}. "
            "Running agents against an unpinned discipline would make the "
            "evidence describe a version nobody recorded.")

    primary_name, secondary_names = ROLE_BUNDLES[role]
    sections: list[str] = [
        "# Engineering discipline you are held to",
        "",
        "The following is not advice from this harness. It is the pinned "
        f"Superpowers skill set at revision {(revision or 'unknown')[:12]}, "
        "reproduced verbatim. Where it and any other instruction disagree about "
        "method, it wins.",
        "",
    ]
    injected: list[InjectedSkill] = []

    raw, path = _read_skill(primary_name)
    meta, body = _strip_frontmatter(raw)
    text = body[:PRIMARY_BUDGET].rstrip()
    truncated = len(body) > PRIMARY_BUDGET
    if truncated:
        text += f"\n\n[…{len(body) - PRIMARY_BUDGET} further characters of this "
        text += "skill were not projected]"
    sections += [f"## PRIMARY SKILL — {primary_name}", text, ""]
    injected.append(InjectedSkill(
        primary_name, role, True, str(path),
        hashlib.sha256(raw.encode()).hexdigest(), len(text), truncated))

    for name in secondary_names:
        raw, path = _read_skill(name)
        meta, body = _strip_frontmatter(raw)
        text = _overview(body, SECONDARY_BUDGET)
        sections += [f"## ALSO IN FORCE — {name}",
                     f"_{meta.get('description', '')}_", text, ""]
        injected.append(InjectedSkill(
            name, role, False, str(path),
            hashlib.sha256(raw.encode()).hexdigest(), len(text),
            len(body) > SECONDARY_BUDGET))

    return SkillBundle(role=role, revision=revision,
                       text="\n".join(sections).strip(), skills=injected)


def bundles_for_cohort(roles: list[str], *, expected_revision: str | None = None
                       ) -> dict[str, SkillBundle]:
    out: dict[str, SkillBundle] = {}
    for role in roles:
        base = role.split("#")[0]
        if base in ROLE_BUNDLES and base not in out:
            out[base] = bundle_for(base, expected_revision=expected_revision)
    return out
