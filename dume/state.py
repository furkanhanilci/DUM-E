"""Durable commissioning state.

SQLite plus immutable evidence files is the whole of DUM-E's durability. A
runtime session is not state: every transition here must survive a DUM-E
process restart.

The invariants this module exists to enforce *mechanically* — a prose warning is
not a control if the system can still perform the unsafe action:

* **I-05** ``DONE`` is not ``TECH_COMPLETE`` is not ``ACCEPTED``. The transition
  table is closed; there is no edge from implementation straight to acceptance.
* **I-06** A producer may not control its own reviewer or verifier. Acceptance
  by the actor that produced the candidate is refused.
* **I-08/I-23** No acceptance without verification evidence bound to *the exact*
  candidate revision under review. Evidence from an older candidate is stale and
  is refused.
* **I-24** Retry preserves prior failed evidence. The evidence table is
  append-only; this module offers no delete path.
* **READY** requires every hard dependency to be genuinely ``ACCEPTED``.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# The closed lifecycle. Anything not in this map is not a reachable transition.
TRANSITIONS: dict[str, set[str]] = {
    "NOT_STARTED": {"READY", "BLOCKED"},
    "READY": {"IN_PROGRESS", "BLOCKED"},
    "IN_PROGRESS": {"TECH_COMPLETE", "BLOCKED", "REJECTED"},
    "TECH_COMPLETE": {"ACCEPTED", "REJECTED", "BLOCKED"},
    # Terminal-for-now states can be reopened, because a rejected package is
    # corrected and re-run rather than abandoned.
    "REJECTED": {"IN_PROGRESS", "BLOCKED"},
    "BLOCKED": {"READY", "IN_PROGRESS", "NOT_STARTED"},
    "ACCEPTED": set(),
}

STATES = frozenset(TRANSITIONS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wp (
    wp_id              TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    workstream         TEXT NOT NULL,
    wave               INTEGER NOT NULL,
    state              TEXT NOT NULL,
    candidate_revision TEXT,
    producer_actor     TEXT,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dependency (
    wp_id      TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (wp_id, depends_on)
);
CREATE TABLE IF NOT EXISTS transition (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wp_id       TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    reason      TEXT,
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    wp_id              TEXT NOT NULL,
    kind               TEXT NOT NULL,
    candidate_revision TEXT NOT NULL,
    actor              TEXT NOT NULL,
    verdict            TEXT,
    artefact_path      TEXT,
    artefact_sha256    TEXT,
    detail             TEXT,
    at                 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finding (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    wp_id     TEXT NOT NULL,
    severity  TEXT NOT NULL,
    summary   TEXT NOT NULL,
    status    TEXT NOT NULL,
    at        TEXT NOT NULL
);
"""

BLOCKING_SEVERITIES = ("CRITICAL", "HIGH")


class StateError(RuntimeError):
    """A refused transition. The message says which control refused it."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """The commissioning state store.

    Deliberately not a general ORM. Every method that can weaken an invariant
    refuses in code rather than trusting the caller.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # A crash between the evidence write and the transition write must not
        # leave an accepted package with no evidence.
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- registration ---------------------------------------------------

    def register(self, wp_id: str, title: str, workstream: str, wave: int,
                 depends_on: list[str] | None = None) -> None:
        """Register a work package. Idempotent — re-running the seed is safe."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO wp (wp_id,title,workstream,wave,state,updated_at) "
                "VALUES (?,?,?,?,'NOT_STARTED',?) ON CONFLICT(wp_id) DO UPDATE SET "
                "title=excluded.title, workstream=excluded.workstream, wave=excluded.wave",
                (wp_id, title, workstream, wave, _now()),
            )
            for dep in depends_on or []:
                self.conn.execute(
                    "INSERT OR IGNORE INTO dependency (wp_id,depends_on) VALUES (?,?)",
                    (wp_id, dep),
                )

    def get(self, wp_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM wp WHERE wp_id=?", (wp_id,)).fetchone()
        if row is None:
            raise StateError(f"unknown work package: {wp_id}")
        return row

    def all_wps(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM wp ORDER BY wave, wp_id").fetchall()

    def dependencies(self, wp_id: str) -> list[str]:
        return [r["depends_on"] for r in self.conn.execute(
            "SELECT depends_on FROM dependency WHERE wp_id=? ORDER BY depends_on",
            (wp_id,))]

    def unmet_dependencies(self, wp_id: str) -> list[str]:
        """Hard dependencies that are not genuinely ACCEPTED."""
        unmet = []
        for dep in self.dependencies(wp_id):
            row = self.conn.execute(
                "SELECT state FROM wp WHERE wp_id=?", (dep,)).fetchone()
            if row is None or row["state"] != "ACCEPTED":
                unmet.append(dep)
        return unmet

    # ---- evidence -------------------------------------------------------

    def add_evidence(self, wp_id: str, kind: str, candidate_revision: str,
                     actor: str, verdict: str | None = None,
                     artefact_path: str | None = None,
                     artefact_sha256: str | None = None,
                     detail: str | None = None) -> int:
        """Append one evidence record. There is no update and no delete (I-24)."""
        self.get(wp_id)
        if not candidate_revision:
            raise StateError("evidence must bind to a candidate revision")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO evidence (wp_id,kind,candidate_revision,actor,verdict,"
                "artefact_path,artefact_sha256,detail,at) VALUES (?,?,?,?,?,?,?,?,?)",
                (wp_id, kind, candidate_revision, actor, verdict, artefact_path,
                 artefact_sha256, detail, _now()),
            )
        return int(cur.lastrowid)

    def evidence(self, wp_id: str, candidate_revision: str | None = None,
                 kind: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM evidence WHERE wp_id=?"
        args: list = [wp_id]
        if candidate_revision:
            sql += " AND candidate_revision=?"
            args.append(candidate_revision)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    # ---- findings -------------------------------------------------------

    def add_finding(self, wp_id: str, severity: str, summary: str,
                    status: str = "OPEN") -> int:
        severity = severity.upper()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO finding (wp_id,severity,summary,status,at) VALUES (?,?,?,?,?)",
                (wp_id, severity, summary, status, _now()))
        return int(cur.lastrowid)

    def open_blocking_findings(self, wp_id: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(BLOCKING_SEVERITIES))
        return self.conn.execute(
            f"SELECT * FROM finding WHERE wp_id=? AND status='OPEN' "
            f"AND severity IN ({marks}) ORDER BY id",
            (wp_id, *BLOCKING_SEVERITIES)).fetchall()

    # ---- transitions ----------------------------------------------------

    def transition(self, wp_id: str, to_state: str, actor: str,
                   reason: str | None = None,
                   candidate_revision: str | None = None) -> None:
        """Move a work package, refusing every transition an invariant forbids."""
        if to_state not in STATES:
            raise StateError(f"unknown state: {to_state!r}")
        row = self.get(wp_id)
        current = row["state"]
        if to_state not in TRANSITIONS[current]:
            raise StateError(
                f"{wp_id}: {current} -> {to_state} is not a permitted transition")

        if to_state == "READY":
            unmet = self.unmet_dependencies(wp_id)
            if unmet:
                raise StateError(
                    f"{wp_id}: cannot be READY, hard dependencies not ACCEPTED: "
                    + ", ".join(unmet))

        if to_state == "IN_PROGRESS":
            # The actor that starts implementation is the producer, and is
            # remembered so that acceptance can refuse the same identity later.
            candidate_revision = candidate_revision or row["candidate_revision"]

        if to_state == "TECH_COMPLETE":
            if not (candidate_revision or row["candidate_revision"]):
                raise StateError(
                    f"{wp_id}: TECH_COMPLETE requires the candidate revision it "
                    "was reached on")

        if to_state == "ACCEPTED":
            self._check_acceptance(row, actor, candidate_revision)

        new_rev = candidate_revision or row["candidate_revision"]
        producer = row["producer_actor"]
        if to_state == "IN_PROGRESS":
            producer = actor
        with self.conn:
            self.conn.execute(
                "UPDATE wp SET state=?, candidate_revision=?, producer_actor=?, "
                "updated_at=? WHERE wp_id=?",
                (to_state, new_rev, producer, _now(), wp_id))
            self.conn.execute(
                "INSERT INTO transition (wp_id,from_state,to_state,actor,reason,at) "
                "VALUES (?,?,?,?,?,?)",
                (wp_id, current, to_state, actor, reason, _now()))

    def _check_acceptance(self, row: sqlite3.Row, actor: str,
                          candidate_revision: str | None) -> None:
        wp_id = row["wp_id"]
        candidate = candidate_revision or row["candidate_revision"]
        if not candidate:
            raise StateError(f"{wp_id}: acceptance requires a candidate revision")

        # I-23: acceptance is about *this* candidate, not a previous green run.
        if row["candidate_revision"] and candidate != row["candidate_revision"]:
            raise StateError(
                f"{wp_id}: acceptance candidate {candidate} does not match the "
                f"candidate under review {row['candidate_revision']}; "
                "re-verify before accepting")

        # I-06: the producer may not accept its own work.
        if row["producer_actor"] and actor == row["producer_actor"]:
            raise StateError(
                f"{wp_id}: producer {actor!r} may not accept its own package; "
                "an independent verifier is required")

        # I-08: acceptance needs verification evidence bound to this candidate.
        verifications = [e for e in self.evidence(wp_id, candidate, "verification")]
        if not verifications:
            raise StateError(
                f"{wp_id}: no verification evidence for candidate {candidate}; "
                "evidence from another candidate is stale and does not carry over")

        # The verifier that recorded the evidence must also be independent, so a
        # producer cannot self-verify and then have a bystander rubber-stamp it.
        for ev in verifications:
            if row["producer_actor"] and ev["actor"] == row["producer_actor"]:
                raise StateError(
                    f"{wp_id}: verification evidence #{ev['id']} was recorded by "
                    f"the producer {ev['actor']!r}; verification must be independent")
        if not any(e["verdict"] == "PASS" for e in verifications):
            raise StateError(
                f"{wp_id}: no PASSing verification verdict for candidate {candidate}")

        blocking = self.open_blocking_findings(wp_id)
        if blocking:
            raise StateError(
                f"{wp_id}: {len(blocking)} open Critical/High finding(s) block "
                "acceptance: " + "; ".join(f["summary"] for f in blocking))

    # ---- reporting ------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "generated_at": _now(),
            "work_packages": [dict(r) for r in self.all_wps()],
            "counts": {
                s: self.conn.execute(
                    "SELECT COUNT(*) c FROM wp WHERE state=?", (s,)).fetchone()["c"]
                for s in sorted(STATES)
            },
        }

    def history(self, wp_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM transition WHERE wp_id=? ORDER BY id", (wp_id,)).fetchall()


def sha256_file(path: Path | str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def json_dump(obj, path: Path | str) -> str:
    """Write deterministic JSON and return its digest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return sha256_file(path)
