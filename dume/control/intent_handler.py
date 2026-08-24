"""Executing an authorised intent.

Separate from the gateway on purpose: the gateway decides whether a request
becomes an intent, and this decides what an intent does. Keeping them apart is
what lets the same command vocabulary be driven from Telegram, from Buzz, or
from the CLI without three copies of the authorisation logic — and it means a
new surface cannot accidentally widen the vocabulary.

Nothing here can accept a package. `ACCEPTED` requires independent verification
evidence bound to a candidate, and no message from any surface is that.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..runtimes.profiles import RuntimeRegistry
from ..state import StateError, Store
from .command_gateway import CommandIntent


class IntentHandler:
    def __init__(self, store: Store, registry: RuntimeRegistry,
                 pause_flag: Path | str):
        self.store = store
        self.registry = registry
        self.pause_flag = Path(pause_flag)

    # ---- read -----------------------------------------------------------

    def _status(self) -> str:
        rows = self.store.all_wps()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        lines = [f"{len(rows)} packages"]
        lines += [f"  {state:<18} {count}" for state, count
                  in sorted(counts.items(), key=lambda kv: -kv[1])]
        active = [r for r in rows if r["state"] not in {"DISCOVERED", "ACCEPTED"}]
        if active:
            lines.append("")
            lines += [f"  {r['wp_id']}  {r['state']}  {r['title'][:40]}"
                      for r in active[:12]]
        if self.pause_flag.exists():
            lines.append("\nPAUSED — no new work will start.")
        return "\n".join(lines)

    def _show(self, wp: str) -> str:
        row = self.store.get(wp)
        unmet = self.store.unmet_dependencies(wp)
        evidence = self.store.evidence(wp)
        return "\n".join([
            f"{row['wp_id']} — {row['title']}",
            f"  state      {row['state']}",
            f"  candidate  {(row['candidate_revision'] or '—')[:12]}",
            f"  producer   {row['producer_actor'] or '—'}",
            f"  waiting on {', '.join(unmet) if unmet else 'nothing'}",
            f"  evidence   {len(evidence)} record(s)",
        ])

    def _history(self, wp: str) -> str:
        rows = self.store.history(wp)
        if not rows:
            return f"{wp} has no transitions."
        return "\n".join(f"{r['at'][11:19]}  {(r['from_state'] or '—'):<16}"
                         f"→ {r['to_state']:<16} {r['actor']}" for r in rows[-15:])

    def _findings(self, wp: str) -> str:
        rows = self.store.open_blocking_findings(wp)
        if not rows:
            return f"{wp}: no open Critical or High finding."
        return "\n".join(f"[{r['severity']}] {r['summary'][:120]}" for r in rows)

    def _runtimes(self) -> str:
        lines = []
        for row in self.registry.table():
            lines.append(f"{row['icon']} {row['runtime']:<16} {row['status']:<17}"
                         f" {row['mode']:<9} {len(row['qualified_for'])} role(s)")
        return "\n".join(lines) or "no runtimes configured"

    def _next(self) -> str:
        ready, blocked = [], []
        for row in self.store.all_wps():
            if row["state"] != "DISCOVERED":
                continue
            unmet = self.store.unmet_dependencies(row["wp_id"])
            (blocked if unmet else ready).append(
                (row["wp_id"], row["title"][:40], unmet))
        lines = [f"{len(ready)} package(s) could start now:"]
        lines += [f"  {wp}  {title}" for wp, title, _ in ready[:10]]
        if blocked:
            lines.append(f"\n{len(blocked)} waiting on dependencies, first few:")
            lines += [f"  {wp}  ← {', '.join(unmet)}"
                      for wp, _title, unmet in blocked[:6]]
        return "\n".join(lines)

    def _evidence(self, wp: str) -> str:
        rows = self.store.evidence(wp)
        if not rows:
            return f"{wp} has no evidence."
        return "\n".join(
            f"#{r['id']:<3} {r['kind']:<26} {r['verdict'] or '—':<5} "
            f"{r['actor'][:28]:<28} {(r['candidate_revision'] or '')[:10]}"
            for r in rows[-15:])

    def _roles(self) -> str:
        from ..cohort.role_registry import ROLES
        lines = ["The logical roles. A role is not an agent, an agent is not a "
                 "runtime, and none of them is a person.", ""]
        for name, role in ROLES.items():
            binding = self._binding_for(name)
            lines.append(f"@{name}")
            lines.append(f"   decides: {role.decides}")
            if not role.needs_runtime:
                lines.append("   runs on: the harness itself — it sequences and "
                             "decides nothing about whether a stage passed")
            else:
                lines.append(f"   bound to: {binding or 'nothing right now'}")
            if role.independent_of:
                lines.append("   must differ from: " + ", ".join(role.independent_of))
        return "\n".join(lines)

    def _binding_for(self, role: str) -> str | None:
        """What the last recorded run bound this role to."""
        import json
        from pathlib import Path
        report = Path(__file__).resolve().parents[2] / "evidence" / "live" / "run_result.json"
        if not report.is_file():
            return None
        try:
            data = json.loads(report.read_text())
        except json.JSONDecodeError:
            return None
        for key, binding in (data.get("bindings") or {}).items():
            if key.split("#")[0] == role:
                return f"{binding['runtime_id']} ({binding['family']})"
        return None

    def _ask(self, role: str, question: str) -> str:
        """Answer for a role from what it actually did.

        Deliberately not a fresh model call. A role's answer in a chat window
        would be a new opinion with no evidence behind it, and the one thing a
        commissioning record must not acquire is a second, softer version of a
        verdict. What comes back is the work that role recorded.
        """
        from ..cohort.role_registry import ROLES
        role = role.lstrip("@").replace("-", "_")
        if role not in ROLES:
            return (f"there is no role called {role!r}. The roles are: "
                    + ", ".join(f"@{r}" for r in ROLES))

        kind = {"spec_reviewer": "specification_compliance",
                "code_reviewer": "code_quality",
                "verifier": "verification"}.get(role)
        lines = [f"@{role} — {ROLES[role].decides}"]
        binding = self._binding_for(role)
        if binding:
            lines.append(f"currently bound to {binding}")
        lines.append("")

        found = False
        for row in self.store.all_wps():
            if row["state"] == "DISCOVERED":
                continue
            records = [e for e in self.store.evidence(row["wp_id"])
                       if kind is None or e["kind"] == kind]
            for record in records[-3:]:
                found = True
                lines.append(
                    f"{row['wp_id']} · {record['kind']} · "
                    f"{record['verdict'] or '—'} · candidate "
                    f"{(record['candidate_revision'] or '')[:12]}")
                if record["detail"]:
                    lines.append(f"   {record['detail'][:400]}")
                if record["artefact_path"]:
                    lines.append(f"   evidence: {record['artefact_path']}")
        if not found:
            lines.append("This role has recorded nothing yet.")
        lines.append("")
        lines.append(f"(you asked: {question[:120]!r} — answered from the record, "
                     "not by asking the model again)")
        return "\n".join(lines)

    # ---- control --------------------------------------------------------

    def _pause(self, actor: str) -> str:
        self.pause_flag.parent.mkdir(parents=True, exist_ok=True)
        self.pause_flag.write_text(f"paused by {actor}\n")
        return ("Paused. No new work will start; work already running finishes "
                "rather than being interrupted mid-candidate.")

    def _resume(self) -> str:
        if self.pause_flag.exists():
            self.pause_flag.unlink()
            return "Resumed."
        return "Was not paused."

    def _retry(self, wp: str, actor: str) -> str:
        try:
            self.store.transition(wp, "RETRY", actor=actor, reason="human retry")
            self.store.transition(wp, "PLANNED", actor=actor, reason="human retry")
        except StateError as exc:
            return f"refused: {exc}"
        return f"{wp} re-entered at PLANNED. A correction needs a plan, not a rerun."

    def _runtime_mode(self, runtime: str, mode: str) -> str:
        try:
            self.registry.set_mode(runtime, mode)
            self.registry.save()
        except (KeyError, ValueError) as exc:
            return f"refused: {exc}"
        note = {"RESERVE": " It will now only be spent on architecture-critical "
                           "work, spec conflicts and high-risk review.",
                "DISABLED": " It is out of the eligible pool; anything that needed "
                            "it is BLOCKED_RUNTIME rather than downgraded.",
                "NORMAL": ""}.get(mode, "")
        return f"{runtime} → {mode}.{note}"

    # ---- human decision -------------------------------------------------

    def _decide(self, wp: str, ruling: str, actor: str) -> str:
        self.store.add_evidence(wp, "human_ruling", "n/a", actor,
                                verdict=None, detail=ruling)
        return (f"Recorded against {wp} as a human ruling by {actor}.\n"
                "It is evidence, not a state change — a ruling does not accept "
                "a package.")

    def _block(self, wp: str, reason: str, actor: str) -> str:
        try:
            self.store.transition(wp, "BLOCKED", actor=actor, reason=reason)
        except StateError as exc:
            return f"refused: {exc}"
        self.store.add_finding(wp, "HIGH", f"blocked by {actor}: {reason}")
        return f"{wp} is BLOCKED. Reason recorded as a High finding."

    # ---- dangerous ------------------------------------------------------

    def _kill(self, actor: str) -> str:
        stopped = []
        for name in ("dume-qwen", "dume-mistral"):
            result = subprocess.run(["docker", "stop", name],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                stopped.append(name)
        self.pause_flag.parent.mkdir(parents=True, exist_ok=True)
        self.pause_flag.write_text(f"kill switch by {actor}\n")
        return ("Kill switch.\n"
                f"  stopped: {', '.join(stopped) or 'nothing was running'}\n"
                "  paused: no new work will start\n"
                "  the Buzz relay is left running: it holds the record of what "
                "happened, and destroying that is not part of stopping.")

    def _bind_workspace(self, name: str, path: str, actor: str) -> str:
        return ("Refused here on purpose. Binding a workspace is how a build "
                "agent first gains reach outside the harness, and it is a "
                "deliberate act at the console with the path in front of you — "
                "not something to do from a phone. Edit config/dume.config.json "
                "and run `dume workspace --probe` to prove the mount actually "
                "refuses writes.")

    # ---- dispatch -------------------------------------------------------

    # ---- the conversation -------------------------------------------------
    #
    # Telegram is a control surface, not a second client. What it shows of a
    # channel is what a phone can carry honestly: who said it, what class they
    # declared it as, and what it was about. Threads are named by their root
    # rather than drawn, because a thread drawn as a flat list is a thread
    # misrepresented — and a phone is exactly where somebody would act on the
    # misreading.

    def _relay(self):
        from pathlib import Path as _Path
        from ..collaboration.buzz import BuzzClient, load_identity
        store = _Path.home() / ".dume" / "secrets" / "buzz-identities.json"
        return BuzzClient("http://127.0.0.1:3000", load_identity(store, "owner"))

    # The short name a person types, and the space channel it means. The id
    # itself is derived in the collaboration layer, so this table names things
    # rather than holding addresses that can drift out of step with it.
    CHANNEL_NAMES = {
        "control": "dume-control", "implementation": "dume-implementation",
        "review": "dume-review", "verification": "dume-verification",
        "literature": "research-literature", "questions": "research-questions",
        "science": "review-science", "escalations": "decisions-escalations",
        "records": "decisions-records", "runtimes": "operations-runtimes",
        "incidents": "operations-incidents",
    }

    @property
    def CHANNELS(self) -> dict:  # noqa: N802
        from ..collaboration.buzz import SPACE_CHANNELS
        return {short: SPACE_CHANNELS[full]
                for short, full in self.CHANNEL_NAMES.items()}

    SPACES = {
        "DUM-E": ["control", "implementation", "review", "verification"],
        "Research": ["literature", "questions"],
        "Review": ["science"],
        "Decisions": ["escalations", "records"],
        "Operations": ["runtimes", "incidents"],
    }

    def _resolve_channel(self, name: str) -> str:
        key = name.lstrip("#").lower()
        if key in self.CHANNELS:
            return self.CHANNELS[key]
        # A work package has its own channel, derived from its id rather than
        # looked up, so it can be named directly: `read WP-001`.
        if key.upper().startswith("WP-"):
            from ..collaboration.buzz import channel_id_for
            return channel_id_for(key.upper())
        raise KeyError(
            f"{name!r} is not a channel. Try: " + ", ".join(sorted(self.CHANNELS)))

    # What each space decides, in its own words. Carried here because a list of
    # channel names tells a reader where to type and not what the place is for,
    # and the distinction between spaces is an authority distinction rather
    # than a filing one.
    SPACE_PURPOSE = {
        "DUM-E": "The commissioning harness that builds AETHRIONIS. Decides "
                 "merge eligibility, and that by a deterministic gate.",
        "Research": "Sources and open questions. Decides nothing — the Source "
                    "Registry owns bibliographic truth.",
        "Review": "Claims, evidence and rebuttal. A verdict is a record bound "
                  "to a candidate, not something said here.",
        "Decisions": "What could not be settled below. Signed, then announced.",
        "Operations": "Runtimes, quota, health. Availability is not "
                      "eligibility.",
    }

    def _spaces(self) -> str:
        """AETHRIONIS's spaces. DUM-E is one of them, and the first, because it
        is the harness that builds the rest — not because it is the product."""
        lines = ["AETHRIONIS", ""]
        for space, channels in self.SPACES.items():
            lines.append(f"▸ {space}")
            lines.append(f"   {self.SPACE_PURPOSE.get(space, '')}")
            lines.append("   " + "  ".join(f"#{c}" for c in channels))
            lines.append("")
        lines.append("read #<channel>          the last messages")
        lines.append("read WP-001              a package's own channel")
        lines.append("open                     what nobody has answered")
        lines.append("say #<channel> <text>    post a STATUS")
        lines.append("commands                 everything you may do")
        return "\n".join(lines)

    def _commands(self, principal_class: str = "DANGEROUS_ACTION") -> str:
        """The vocabulary, grouped by what a class of action can do.

        Printed by class rather than alphabetically because the classes are the
        point: they are what keeps "can act" and "can settle" separable.
        """
        from .command_gateway import ACTIONS, CLASS_ORDER
        groups: dict[str, list] = {}
        for action in ACTIONS.values():
            if CLASS_ORDER[action.klass] <= CLASS_ORDER[principal_class]:
                groups.setdefault(action.klass, []).append(action)
        order = sorted(groups, key=lambda k: CLASS_ORDER[k])
        lines = []
        for klass in order:
            lines.append(klass.replace("_", " ").title())
            for action in sorted(groups[klass], key=lambda a: a.name):
                params = " ".join(f"<{p}>" for p in action.parameters)
                lines.append(f"  {action.name} {params}".rstrip())
                lines.append(f"      {action.summary}")
            lines.append("")
        return "\n".join(lines).rstrip()

    @staticmethod
    def _render(events: list) -> list[str]:
        out = []
        from ..collaboration.buzz import declared_refs, declared_type
        for event in reversed(events):
            tags = event.get("tags", [])
            refs = declared_refs(tags)
            klass = declared_type(tags) or "—"
            who = (event.get("pubkey") or "?")[:8]
            body = " ".join((event.get("content") or "").split())[:260]
            line = f"[{klass}] {who}  {body}"
            if refs:
                line += f"\n     re: {', '.join(refs[:2])}"
            out.append(line)
        return out

    def _read(self, channel: str) -> str:
        target = self._resolve_channel(channel)
        try:
            events = self._relay().read(target, limit=12)
        except Exception as exc:
            # The relay being down is not a refusal and must not read as one.
            return f"the relay could not be reached: {str(exc)[:160]}"
        if not events:
            return f"#{channel.lstrip('#')} is empty."
        return f"#{channel.lstrip('#')} — last {len(events)}\n\n" + \
            "\n\n".join(self._render(events))

    def _open(self) -> str:
        """Messages whose class asks a question and that nobody answered.

        A CHALLENGE with no reply is the thing most worth carrying to a phone,
        because it is the thing most likely to be waiting on the person holding
        it.
        """
        asks = {"CHALLENGE", "REQUEST", "DISAGREEMENT", "BLOCKER"}
        try:
            client = self._relay()
        except Exception as exc:
            return f"the relay could not be reached: {str(exc)[:160]}"
        found = []
        targets = dict(self.CHANNELS)
        try:
            from ..collaboration.buzz import channel_id_for
            for row in self.store.all_wps():
                if row["state"] not in ("DISCOVERED", "ACCEPTED"):
                    targets[row["wp_id"]] = channel_id_for(row["wp_id"])
        except Exception:
            pass
        for name, target in targets.items():
            try:
                events = client.read(target, limit=40)
            except Exception:
                continue
            answered = {tag[1] for event in events for tag in event.get("tags", [])
                        if len(tag) > 1 and tag[0] == "e"}
            from ..collaboration.buzz import declared_type
            for event in events:
                klass = declared_type(event.get("tags", []))
                if klass in asks and event["id"] not in answered:
                    body = " ".join((event.get("content") or "").split())[:180]
                    found.append(f"#{name}  [{klass}] {body}")
        if not found:
            return "Nothing is waiting for an answer."
        return f"{len(found)} waiting for an answer:\n\n" + "\n\n".join(found[:12])

    def _say(self, channel: str, text: str, actor: str) -> str:
        target = self._resolve_channel(channel)
        try:
            self._relay().announce(target, f"{text}\n\n— {actor}, from Telegram",
                                   message_type="STATUS")
        except Exception as exc:
            return f"not posted: {str(exc)[:180]}"
        return f"posted to #{channel.lstrip('#')} as STATUS."

    # What a reference has to look like before it counts as one. A CHALLENGE is
    # required to name its subject, and the command parser splits on whitespace
    # — so an empty argument does not arrive as empty, it disappears, and the
    # first word of the sentence slides into its place. `challenge control ""
    # "no subject"` posted a challenge about "no". Checking the shape here is
    # what stops the parser deciding what a message is about.
    REFERENCE = re.compile(
        r"^(?:WP-\d{3}(?:/[\w.\-]+)*|[\w.\-]+/[\w.\-/]+|[0-9a-f]{8,64})$")

    def _challenge(self, channel: str, reference: str, text: str,
                   actor: str) -> str:
        reference = reference.strip()
        if not self.REFERENCE.match(reference):
            return (f"not posted: {reference!r} does not name anything. A "
                    "CHALLENGE has to say what it is about, or nobody can "
                    "answer, track or close it.\n\nTry a package (WP-001), a "
                    "candidate (WP-001/candidate/db4725af93ee) or an evidence "
                    "path.")
        target = self._resolve_channel(channel)
        try:
            self._relay().announce(
                target, f"{text}\n\n— {actor}, from Telegram",
                message_type="CHALLENGE", refs=[reference])
        except Exception as exc:
            # The contract refuses a CHALLENGE with no subject, and that refusal
            # arrives here as an ordinary error. Reporting it plainly is right:
            # the sender left out the one thing that makes it answerable.
            return f"not posted: {str(exc)[:180]}"
        return (f"posted to #{channel.lstrip('#')} as CHALLENGE about "
                f"{reference}.\n\nIt is a message. It moves nothing by itself.")

    def __call__(self, intent: CommandIntent) -> str:
        actor = intent.authenticated_actor
        args = intent.arguments
        try:
            return {
                "status": lambda: self._status(),
                "show": lambda: self._show(args["wp"]),
                "history": lambda: self._history(args["wp"]),
                "findings": lambda: self._findings(args["wp"]),
                "runtimes": lambda: self._runtimes(),
                "next": lambda: self._next(),
                "evidence": lambda: self._evidence(args["wp"]),
                "roles": lambda: self._roles(),
                "ask": lambda: self._ask(args["role"], args["question"]),
                "spaces": lambda: self._spaces(),
                "commands": lambda: self._commands(),
                "read": lambda: self._read(args["channel"]),
                "open": lambda: self._open(),
                "say": lambda: self._say(args["channel"], args["text"], actor),
                "challenge": lambda: self._challenge(
                    args["channel"], args["reference"], args["text"], actor),
                "pause": lambda: self._pause(actor),
                "resume": lambda: self._resume(),
                "retry": lambda: self._retry(args["wp"], actor),
                "reserve": lambda: self._runtime_mode(args["runtime"], "RESERVE"),
                "release": lambda: self._runtime_mode(args["runtime"], "NORMAL"),
                "disable": lambda: self._runtime_mode(args["runtime"], "DISABLED"),
                "enable": lambda: self._runtime_mode(args["runtime"], "NORMAL"),
                "decide": lambda: self._decide(args["wp"], args["ruling"], actor),
                "block": lambda: self._block(args["wp"], args["reason"], actor),
                "kill": lambda: self._kill(actor),
                "bind_workspace": lambda: self._bind_workspace(
                    args["name"], args["path"], actor),
            }[intent.action]()
        except StateError as exc:
            return f"refused: {exc}"
        except KeyError as exc:
            return f"unknown work package or runtime: {exc}"
