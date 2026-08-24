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
