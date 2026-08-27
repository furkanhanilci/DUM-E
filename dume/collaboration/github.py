"""Membership through GitHub, admission through the relay.

DUM-E keeps three things apart that a conventional account system merges:

  identity    a key held on the operator's machine. Nothing registers it, no
              service issues it, and none can revoke it.
  membership  a GitHub account that appears in this deployment's roster. This
              is the part a person can be added to and removed from.
  admission   a relay invite, minted only after the two are bound.

The separation matters because they fail differently. A lost key is a lost
identity and no reset recovers it. A revoked membership leaves the identity
intact and only stops it joining. Merging them would make "remove someone from
the project" and "destroy their identity" the same operation, which is how an
ordinary personnel change becomes unrecoverable.

The device flow is used rather than a browser redirect because this is a desktop
application: a redirect needs a loopback server and a registered callback, and
both are more moving parts than a code the operator types once.

The token GitHub returns never reaches the interface. It is used here, to read
one login, and then dropped.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
MEMBERSHIP_URL = "https://api.github.com/user/memberships/orgs/{org}"

# Read-only, and only what the roster check needs. `read:org` is requested only
# when the roster is an organisation; a roster of explicit logins needs nothing
# beyond the identity the device flow already establishes.
BASE_SCOPE = "read:user"
ORG_SCOPE = "read:user read:org"

CONFIG = Path.home() / ".dume" / "secrets" / "github-membership.json"
REQUESTS = Path.home() / ".dume" / "secrets" / "github-requests.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MembershipError(RuntimeError):
    """Membership could not be established, and why."""


class NotConfigured(MembershipError):
    """No OAuth app has been registered for this deployment."""


@dataclass
class Roster:
    """Who this deployment admits.

    `logins` is an explicit list. `org` admits anyone whose membership in that
    GitHub organisation is active. A deployment may set either or both; an empty
    roster admits nobody, which is the correct default — a membership system
    that admits everyone until configured is not a membership system.
    """
    client_id: str
    logins: tuple[str, ...] = ()
    org: str | None = None

    @property
    def scope(self) -> str:
        return ORG_SCOPE if self.org else BASE_SCOPE

    def is_empty(self) -> bool:
        return not self.logins and not self.org


def load_roster(path: Path | str = CONFIG) -> Roster:
    path = Path(path)
    if not path.is_file():
        raise NotConfigured(
            f"no GitHub membership config at {path}. Register an OAuth app "
            "(Settings → Developer settings → OAuth Apps, device flow enabled) "
            "and write its client id here."
        )
    data = json.loads(path.read_text())
    client_id = (data.get("client_id") or "").strip()
    if not client_id:
        raise NotConfigured(f"{path} has no client_id")
    return Roster(
        client_id=client_id,
        logins=tuple(str(login).lower() for login in data.get("logins", [])),
        org=(data.get("org") or None),
    )


def _post(url: str, payload: dict, *, token: str | None = None) -> dict:
    body = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(url, data=body, headers={
        "Accept": "application/json",
        "User-Agent": "DUM-E",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # GitHub explains itself in the body. Reporting only the status code
        # turns "device flow is not enabled on this app" into "400", and the
        # operator cannot tell that from a client id that does not exist.
        raise MembershipError(_explain(exc, url)) from exc
    except urllib.error.URLError as exc:
        raise MembershipError(f"GitHub could not be reached: {exc.reason}") from exc


def _explain(exc: urllib.error.HTTPError, url: str) -> str:
    path = urllib.parse.urlparse(url).path
    try:
        body = exc.read().decode()
    except Exception:
        body = ""
    try:
        parsed = json.loads(body)
        detail = (parsed.get("error_description") or parsed.get("message")
                  or parsed.get("error") or "")
    except json.JSONDecodeError:
        detail = body.strip()[:200]
    hint = ""
    if exc.code == 400 and "device" in path:
        hint = (" — GitHub says this when the OAuth app has no device flow: "
                "open the app and tick 'Enable Device Flow'. It says the same "
                "for a client id that does not exist.")
    return (f"GitHub returned {exc.code} for {path}"
            + (f": {detail}" if detail else "") + hint)


def _get(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "DUM-E",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise MembershipError(
            f"GitHub returned {exc.code} for {urllib.parse.urlparse(url).path}"
        ) from exc
    except urllib.error.URLError as exc:
        raise MembershipError(f"GitHub could not be reached: {exc.reason}") from exc


def begin(roster: Roster) -> dict:
    """Ask GitHub for a device code the operator can type."""
    data = _post(DEVICE_CODE_URL, {
        "client_id": roster.client_id, "scope": roster.scope})
    if "device_code" not in data:
        raise MembershipError(
            data.get("error_description")
            or f"GitHub refused the device request: {data.get('error', data)}")
    return {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data["verification_uri"],
        # GitHub's own floor is 5s; going under it earns slow_down and a longer
        # wait than polling politely would have taken.
        "interval": max(int(data.get("interval", 5)), 5),
        "expires_in": int(data.get("expires_in", 900)),
    }


def redeem(roster: Roster, device_code: str) -> str | None:
    """One poll. Returns a token, or None while the operator is still typing.

    Separating one poll from the loop is what lets the caller stay responsive
    and cancel; a function that blocks until authorised cannot be interrupted
    by the person who changed their mind.
    """
    data = _post(ACCESS_TOKEN_URL, {
        "client_id": roster.client_id,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    if token := data.get("access_token"):
        return token
    error = data.get("error")
    if error in ("authorization_pending", "slow_down"):
        return None
    raise MembershipError({
        "expired_token": "the code expired before it was entered",
        "access_denied": "the request was declined on GitHub",
        "incorrect_device_code": "that device code is not one we issued",
    }.get(error, data.get("error_description") or f"GitHub said {error!r}"))


def _load_requests(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # A corrupt request file must not lock the operator out of the roster
        # they already approved. It is replaced, and the loss is one queue of
        # people who can knock again.
        return {}


def record_request(login: str, *, path: Path = REQUESTS) -> dict:
    """Note that somebody asked, so the operator has something to approve.

    A refusal that leaves no trace makes the operator the one who has to
    remember; then the person is told to try again, and nothing has changed
    between the two attempts. The queue is what turns "not on the list" from a
    dead end into a decision somebody can make.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    requests = _load_requests(path)
    entry = requests.get(login.lower())
    if entry and entry.get("state") == "denied":
        # A denial is not overwritten by knocking again. Re-approving is an
        # explicit act.
        return entry
    entry = {"login": login, "state": "pending",
             "first_asked": (entry or {}).get("first_asked") or _now(),
             "last_asked": _now()}
    requests[login.lower()] = entry
    path.write_text(json.dumps(requests, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return entry


def pending(path: Path = REQUESTS) -> list[dict]:
    return [entry for entry in _load_requests(path).values()
            if entry.get("state") == "pending"]


def decide(login: str, *, approve: bool, config: Path = CONFIG,
           path: Path = REQUESTS) -> dict:
    """Approve or deny a request. Approval writes the roster; both are recorded.

    Approval edits the roster rather than the request queue, because the roster
    is what `admit` reads. Leaving the approval only in the queue would produce
    a person who is marked approved and still refused.
    """
    requests = _load_requests(path)
    entry = requests.get(login.lower())
    if not entry:
        raise MembershipError(f"{login} has not asked to join")
    entry["state"] = "approved" if approve else "denied"
    entry["decided_at"] = _now()
    requests[login.lower()] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(requests, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)

    if approve:
        data = json.loads(config.read_text()) if config.is_file() else {}
        logins = [str(name) for name in data.get("logins", [])]
        if login.lower() not in {name.lower() for name in logins}:
            logins.append(entry["login"])
        data["logins"] = logins
        config.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        config.chmod(0o600)
    return entry


def admit(roster: Roster, token: str) -> dict:
    """Who this is, and whether the roster admits them.

    Returns the login either way. A refusal that does not say who was refused
    leaves the operator unable to tell a typo from a missing entry.
    """
    user = _get(USER_URL, token)
    login = (user.get("login") or "").strip()
    if not login:
        raise MembershipError("GitHub returned no login for this token")

    reasons = []
    if login.lower() in roster.logins:
        return {"login": login, "admitted": True, "via": "roster"}
    if roster.logins:
        reasons.append("not named in the roster")

    if roster.org:
        membership = _get(MEMBERSHIP_URL.format(org=roster.org), token)
        state = membership.get("state")
        if state == "active":
            return {"login": login, "admitted": True,
                    "via": f"member of {roster.org}"}
        reasons.append(
            f"membership of {roster.org} is {state or 'absent'}"
            + (" — the invitation is still pending" if state == "pending" else ""))

    # Somebody proved a GitHub account and was turned away. Record it, so the
    # operator has a decision in front of them rather than a memory to keep.
    request = record_request(login)
    return {"login": login, "admitted": False,
            "state": request["state"],
            "reason": "; ".join(reasons) or "this deployment admits nobody yet"}


def wait(roster: Roster, device: dict, *, deadline: float | None = None) -> str:
    """Poll until authorised. Used by the CLI; the interface polls itself."""
    interval = device["interval"]
    deadline = deadline or (time.time() + device["expires_in"])
    while time.time() < deadline:
        token = redeem(roster, device["device_code"])
        if token:
            return token
        time.sleep(interval)
    raise MembershipError("the code expired before it was entered")
