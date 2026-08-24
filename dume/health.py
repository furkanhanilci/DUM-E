"""Is this deployment actually usable, and if not, which part is missing.

Every check answers one question a person would otherwise answer by trying
something and reading a confusing error. `read` said "the relay could not be
reached" when the relay was fine and the address was wrong; the app showed an
em dash where a field name was wrong, which looks the same as nothing recorded.
Each of those cost an hour, and each is one line here.

Nothing is inferred. A check either reaches the thing it is about, or reports
that it could not — "unknown" is an answer and is never rendered as "fine".
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Check:
    name: str
    ok: bool | None          # None means "could not tell", never "fine"
    detail: str

    @property
    def mark(self) -> str:
        return {True: "ok", False: "NO", None: "??"}[self.ok]


def _http(url: str, timeout: float = 5.0) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read(400).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        return None, str(exc)[:120]


def _port(host: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex((host, port)) == 0


def checks() -> list[Check]:
    from .collaboration.host import address, relay_http
    out: list[Check] = []
    host = address()

    # ---- the relay, and whether it knows who it is -------------------------
    status, _ = _http(f"{relay_http()}/health")
    out.append(Check("relay", status == 200,
                     f"{relay_http()} — {'ok' if status == 200 else status}"))

    # A relay that answers /health and has no community for this host answers
    # every real request with a 404 that reads as "the relay is down".
    status, body = _http(f"{relay_http()}/", 5)
    out.append(Check("relay_community", status == 200,
                     "a community is configured for this host" if status == 200
                     else f"no community for {host}:3000 — {status}"))

    # A report the operator cannot open is not a report. Both halves have to
    # hold: the channel exists, and they are in it.
    reachable: bool | None = None
    detail = "the relay was not reachable, so membership is unknown"
    if status == 200:
        try:
            from .collaboration.buzz import (BuzzClient, SPACE_CHANNELS,
                                             load_identity)
            operator_file = Path.home() / ".dume" / "secrets" / "operator"
            if not operator_file.is_file():
                detail = ("no operator pubkey recorded, so DUM-E cannot "
                          "mention anyone; write ~/.dume/secrets/operator")
            else:
                operator = operator_file.read_text().strip()
                client = BuzzClient(f"http://{host}:3000", load_identity(
                    Path.home() / ".dume" / "secrets" / "buzz-identities.json",
                    "owner"))
                # Re-asserting is how this stays true rather than how it is
                # measured: creating an existing channel and adding an existing
                # member are both no-ops, so the cheapest honest check is to
                # put the state back the way it must be and report what it took.
                from .collaboration.buzz import ensure_spaces
                outcome = ensure_spaces(client, operator)
                broke = [n for n, r in outcome.items() if "not added" in r]
                reachable = not broke
                detail = (f"{len(SPACE_CHANNELS)} space(s), operator "
                          f"{operator[:8]} is a member"
                          + (f"; could not add to {', '.join(broke)}" if broke else ""))
        except Exception as exc:
            reachable = False
            detail = f"the spaces could not be checked: {exc}"
    out.append(Check("spaces", reachable, detail))

    # ---- the models --------------------------------------------------------
    for label, port in (("qwen", 8000), ("mistral", 8001)):
        status, body = _http(f"http://127.0.0.1:{port}/v1/models")
        served = ""
        if status == 200:
            try:
                served = json.loads(body + "]}]}" if body.count("{") > body.count("}")
                                    else body)["data"][0]["id"]
            except Exception:
                served = "serving"
        out.append(Check(f"runtime_{label}", status == 200,
                         served or f"http://127.0.0.1:{port} — {status}"))

    # ---- the harness's own state -------------------------------------------
    db = ROOT / "state" / "dume.db"
    out.append(Check("state", db.is_file(),
                     str(db) if db.is_file() else f"no store at {db}"))

    from .config import load as load_config
    try:
        workspaces = load_config()["workspaces"]
        unbound = [n for n, w in workspaces.items() if not w.get("path")]
        out.append(Check("workspaces", not unbound,
                         "all bound" if not unbound
                         else f"unbound: {', '.join(unbound)} — a package "
                              "needing one is BLOCKED, not improvised"))
    except Exception as exc:
        out.append(Check("workspaces", None, str(exc)[:120]))

    from .control.commission import NotCommissionable, target_repo
    try:
        out.append(Check("target_repo", True, str(target_repo())))
    except NotCommissionable as exc:
        out.append(Check("target_repo", False, str(exc)[:140]))

    # ---- the discipline the agents are held to -----------------------------
    from .review.skills import SkillsUnavailable, drift, installed_revision
    try:
        moved = drift()
        out.append(Check("superpowers", not moved,
                         f"{(installed_revision() or '?')[:12]}, no drift" if not moved
                         else f"{len(moved)} skill(s) changed without the lock"))
    except SkillsUnavailable as exc:
        out.append(Check("superpowers", False, str(exc)[:140]))

    # ---- the surfaces a person uses ---------------------------------------
    status, _ = _http("http://127.0.0.1:8100/api/relay")
    out.append(Check("gateway", status == 200,
                     "http://127.0.0.1:8100" if status == 200
                     else f"not answering — the desktop reads DUM-E through it ({status})"))

    from .control.telegram import Config, TelegramError
    try:
        config = Config.load()
        from .control.forum import Topics
        topics = Topics.load()
        out.append(Check("telegram", bool(config.allowed),
                         f"{len(config.allowed)} principal(s), "
                         f"{len(topics.by_channel or {})} topic(s)"
                         + (", narrator separate" if config.broadcast else
                            ", no broadcast chat")))
    except TelegramError as exc:
        out.append(Check("telegram", False, str(exc)[:140]))

    out.append(Check("bot_running",
                     bool(shutil.which("pgrep")) and subprocess.run(
                         ["pgrep", "-f", "dume.cli telegram"],
                         capture_output=True).returncode == 0,
                     "the bridge is polling" ))
    return out


def report() -> tuple[list[Check], bool]:
    results = checks()
    return results, all(c.ok for c in results)
