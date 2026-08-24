"""Who a message is for, and what it is asking.

Two questions, and they are separate.

**Who.** A group holds two accounts. `@dume` is the harness — it answers about
work packages, candidates, reviews, gates and runs. `@aethrionis` is the
workspace — spaces, channels, membership, what is waiting for an answer. Naming
one is how a person says which they mean, and a message addressed to one is not
answered by the other.

**What.** The command vocabulary is fixed and there is no shell. That does not
mean a person has to remember the exact verb: "hangi paketler bekliyor" and
"next" ask the same thing, and refusing the first while accepting the second
teaches nothing except that the machine is fussy.

So plain text is mapped onto the vocabulary, and the mapping is shown. What is
never done is guessing: an unrecognised sentence is refused with the vocabulary
rather than turned into the closest-looking command, because a wrong guess at
`kill` or `decide` is not a small mistake.
"""
from __future__ import annotations

import re

# The two accounts, by every name a person might type. Matched case-insensitively
# and only at a word boundary, so "aethrionis" inside a sentence still counts and
# "dumela" does not.
DUME = ("dume", "dum-e", "dume_autonomous_bot", "dum_e")
AETHRIONIS = ("aethrionis", "aethrionis_bot", "aethrion", "studio")

# Which commands belong to which addressee. A command asked of the wrong one is
# still answered — refusing it would be pedantry — but the answer says which it
# was, so the distinction stays visible rather than becoming folklore.
DUME_COMMANDS = frozenset({
    "status", "show", "history", "findings", "evidence", "next", "roles",
    "runtimes", "retry", "commission", "pause", "resume", "decide", "block",
    "reserve", "release", "disable", "enable", "kill", "ask"})
AETHRIONIS_COMMANDS = frozenset({
    "spaces", "read", "open", "say", "challenge", "commands", "bind_workspace"})

# Plain text, mapped onto the vocabulary. Deliberately small and deliberately
# literal: each entry is a phrase somebody actually types, not a pattern that
# might match one. Ambiguity is left unmatched rather than resolved.
PHRASES: tuple[tuple[str, str], ...] = (
    # Turkish and English, because both get typed here.
    (r"\b(durum|ne durumda|nasıl gidiyor|how is it going|state of play)\b", "status"),
    (r"\b(hangi paketler|ne başlayabilir|what can start|what.s next"
     r"|sırada ne|sıradaki|hangisi başlayabilir|next up)\b", "next"),
    (r"\b(kim bekliyor|cevap bekleyen|bekleyen var mı|what.s waiting"
     r"|unanswered|waiting on me)\b", "open"),
    (r"\b(alanlar|kanallar|spaces|channels)\b", "spaces"),
    (r"\b(ne yapabilirim|komutlar|what can i do|vocabulary|help)\b", "commands"),
    (r"\b(çalışma zamanları|runtimes|modeller|models)\b", "runtimes"),
    (r"\b(roller|roles)\b", "roles"),
)

# A work package named in prose: "WP-001 ne durumda", "show me wp 12".
PACKAGE = re.compile(r"\bwp[\s_-]?(\d{1,3})\b", re.I)


def addressee(text: str) -> str | None:
    """Which account a message names, if it names one."""
    lowered = text.lower()
    for name in DUME:
        if re.search(rf"@?\b{re.escape(name)}\b", lowered):
            return "dume"
    for name in AETHRIONIS:
        if re.search(rf"@?\b{re.escape(name)}\b", lowered):
            return "aethrionis"
    return None


def strip_address(text: str) -> str:
    """The message without the name it was addressed to."""
    out = text
    for name in DUME + AETHRIONIS:
        out = re.sub(rf"@?\b{re.escape(name)}\b[,:]?\s*", "", out, flags=re.I)
    return out.strip()


def interpret(text: str) -> tuple[str | None, str | None]:
    """Map plain text onto the vocabulary.

    Returns the command and the phrase that produced it, or (None, None) when
    nothing matched. Nothing is guessed: an unmatched sentence stays unmatched,
    because the caller can refuse with the whole vocabulary and a wrong guess at
    `kill` or `decide` is not a small mistake.
    """
    body = strip_address(text)
    if not body:
        return None, None

    # An exact command wins outright — someone who typed `next` meant `next`.
    first = body.split()[0].lower().lstrip("/")
    if first in DUME_COMMANDS | AETHRIONIS_COMMANDS:
        return None, None

    package = PACKAGE.search(body)
    for pattern, command in PHRASES:
        if re.search(pattern, body, re.I):
            if package and command in ("status", "next"):
                # "WP-001 ne durumda" is about one package, not all of them.
                return f"show WP-{int(package.group(1)):03d}", pattern
            return command, pattern

    if package:
        # A package named with nothing else recognisable: showing it is the
        # only reading that cannot be wrong in a costly way.
        return f"show WP-{int(package.group(1)):03d}", "a package id"
    return None, None


def belongs_to(command: str) -> str:
    """Which account a command is really about."""
    if command in AETHRIONIS_COMMANDS:
        return "aethrionis"
    if command in DUME_COMMANDS:
        return "dume"
    return "aethrionis"
