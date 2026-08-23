"""The DUM-E commissioning command line.

Every subcommand either reports a measured fact or refuses. Nothing here accepts
a package, records a pass or clears a finding on the strength of a claim.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, inventory, scenarios, secrets, toolchain, upstream
from .catalogue import seed
from .state import Store, StateError, json_dump
from .workspace import Boundary, mount_is_read_only, probe_write

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DB = REPO_ROOT / "state" / "dume.db"
EVIDENCE = REPO_ROOT / "evidence"


def _store() -> Store:
    return Store(STATE_DB)


def _emit(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


# ---- commands -----------------------------------------------------------

def cmd_inventory(args) -> int:
    inv = inventory.collect()
    out = EVIDENCE / "WP-001" / "host_inventory.json"
    digest = json_dump(inv, out)
    gib = 1024 ** 3
    env = inv["capacity_envelope"]
    print(f"host class      : {inv['host_class']}")
    for reason in inv["classification_reasons"]:
        print(f"  · {reason}")
    print(f"usable VRAM     : {env['vram_usable_for_weights_bytes'] / gib:.1f} GiB "
          f"(of {env['vram_total_bytes'] / gib:.1f} GiB total)")
    print(f"model cache      : {env['model_cache_candidate']['mountpoint']} "
          f"({(env['model_cache_candidate']['free_bytes'] or 0) / gib:.0f} GiB free)")
    print(f"recorded        : {out}")
    print(f"sha256          : {digest}")
    _emit(inv, args.json)
    return 0


def cmd_workspace(args) -> int:
    boundary = Boundary()
    report = {"schema": "dume.workspace_report/1", "workspaces": [], "unbound": boundary.unbound()}
    for name, ws in boundary.cfg["workspaces"].items():
        entry = {"name": name, "mode": ws["mode"], "path": ws.get("path"),
                 "bound": ws.get("bound", False)}
        if entry["bound"] and entry["path"]:
            entry["mount_read_only"] = mount_is_read_only(entry["path"])
            if args.probe:
                outcome, detail = probe_write(entry["path"])
                entry["write_probe"] = {"outcome": outcome, "detail": detail}
                # A READ_ONLY workspace that accepts a write is a failed control.
                # A workspace that is not there proved nothing either way, and is
                # reported as INCONCLUSIVE rather than counted as a pass.
                if outcome == "MISSING":
                    entry["control"] = "INCONCLUSIVE"
                elif entry["mode"] == "READ_ONLY":
                    entry["control"] = "FAILED" if outcome == "WROTE" else "HOLDS"
                else:
                    entry["control"] = "HOLDS" if outcome == "WROTE" else "FAILED"
        report["workspaces"].append(entry)
        flag = "bound  " if entry["bound"] else "UNBOUND"
        print(f"{flag} {name:<16} {entry['mode']:<12} {entry['path'] or '—'}")
        if args.probe and entry["bound"] and "write_probe" in entry:
            print(f"         write probe: {entry['write_probe']['detail']} "
                  f"→ control {entry['control']}")
    if report["unbound"]:
        print(f"\nunbound slots: {', '.join(report['unbound'])} "
              "— a work package needing one of these is BLOCKED, not improvised.")
    out = EVIDENCE / "WP-002" / "workspace_report.json"
    print(f"\nrecorded: {out}  sha256={json_dump(report, out)}")
    _emit(report, args.json)
    return 0


def cmd_check_write(args) -> int:
    """Ask the boundary whether a path may be written. Exit 1 when refused."""
    decision = Boundary().check_write(args.path)
    verdict = "ALLOW" if decision else "DENY"
    print(f"{verdict}  {args.path}\n       {decision.reason}")
    return 0 if decision else 1


def cmd_secrets(args) -> int:
    root = Path(args.path)
    if root.is_file():
        findings = {k: v for k, v in {str(root): secrets.scan_file(root)}.items() if v}
        suppressed = []
    else:
        scanned = secrets.scan_tree_with_suppressions(root)
        findings, suppressed = scanned["findings"], scanned["suppressed"]
    total = sum(len(v) for v in findings.values())
    for path, hits in sorted(findings.items()):
        print(f"{path}")
        for hit in hits:
            print(f"    {hit.preview}")
    print(f"\n{total} unsuppressed credential(s) in {len(findings)} file(s) under {root}")
    if suppressed:
        print(f"{len(suppressed)} hit(s) suppressed by a reviewed allowlist entry:")
        for entry in suppressed:
            print(f"    {entry['path']}  {entry['kind']}  — {entry['reason']}")
    report = {"schema": "dume.secret_scan/1", "root": str(root), "total": total,
              "suppressed": suppressed,
              "findings": {k: [h.as_dict() for h in v] for k, v in findings.items()}}
    out = EVIDENCE / "WP-003" / "secret_scan.json"
    print(f"recorded: {out}  sha256={json_dump(report, out)}")
    _emit(report, args.json)
    return 1 if total else 0


def cmd_toolchain(args) -> int:
    if args.verify:
        result = toolchain.verify()
        print(f"status: {result['status']}")
        for drift in result.get("drift", []):
            print(f"  drift: {drift}")
        _emit(result, args.json)
        return 1 if result["status"] != "MATCH" else 0
    lock = toolchain.write_lock(current_wave=args.wave)
    for tool in lock["tools"]:
        mark = "ok " if tool["present"] else ("MISSING" if tool["required"] else "absent ")
        print(f"{mark:<8} {tool['name']:<12} {tool['version'] or '—':<10} {tool['required_for']}")
    if lock["missing_required"]:
        print(f"\nBLOCKING — required and missing: {', '.join(lock['missing_required'])}")
    if lock["missing_for_later_waves"]:
        print("\nneeded before a later wave, absent now:")
        for m in lock["missing_for_later_waves"]:
            print(f"  wave {m['needed_from_wave']}: {m['name']} — {m['required_for']}")
    print(f"\nenvironment digest: {lock['environment_digest']}")
    out = EVIDENCE / "WP-004" / "toolchain_lock.json"
    print(f"recorded: {out}  sha256={json_dump(lock, out)}")
    _emit(lock, args.json)
    return 1 if lock["missing_required"] else 0


def cmd_upstream(args) -> int:
    result = upstream.check()
    for r in result["results"]:
        pinned = (r["pinned_revision"] or "—")[:12]
        live = (r["live_revision"] or "—")[:12]
        print(f"{r['status']:<12} {r['name']:<24} pinned={pinned:<13} live={live}")
    print(f"\nverdict: {result['verdict']}  "
          f"(drift {result['drift_count']}, unreachable {result['unreachable_count']})")
    out = EVIDENCE / "upstream_check.json"
    print(f"recorded: {out}  sha256={json_dump(result, out)}")
    _emit(result, args.json)
    return 1 if result["verdict"] != "CLEAN" else 0


def cmd_scenarios(args) -> int:
    report = scenarios.run_all()
    for r in report["results"]:
        if r["verdict"] in {"PASS", "FAIL", "NOT_RUN"}:
            print(f"{r['verdict']:<8} {r['scenario']}  {r['title']}")
            if args.verbose:
                for step in r["steps"]:
                    print(f"           · {step}")
    print(f"\nexecuted {report['executed']} — {report['passed']} passed, "
          f"{report['failed']} failed, {report['not_run']} not run")
    print(f"deferred {report['deferred']} scenarios whose subject is not built yet "
          "(listed in the machine record, never counted as passes)")
    out = EVIDENCE / "acceptance_scenarios.json"
    print(f"recorded: {out}  sha256={json_dump(report, out)}")
    _emit(report, args.json)
    return 0 if report["verdict"] == "PASS" else 1


def cmd_seed(args) -> int:
    store = _store()
    summary = seed(store)
    print(f"registered {summary['packages']} work packages "
          f"across waves {min(summary['waves'])}–{max(summary['waves'])}")
    if summary["dangling_dependencies"]:
        print(f"DANGLING dependencies: {summary['dangling_dependencies']}")
    _emit(summary, args.json)
    store.close()
    return 1 if summary["dangling_dependencies"] else 0


def cmd_status(args) -> int:
    store = _store()
    rows = store.all_wps()
    if not rows:
        print("no work packages registered — run `dume seed` first")
        return 1
    if args.wave:
        rows = [r for r in rows if r["wave"] == args.wave]
    for row in rows:
        unmet = store.unmet_dependencies(row["wp_id"])
        blocked = f"  ← waiting on {', '.join(unmet)}" if unmet else ""
        print(f"w{row['wave']:<3} {row['wp_id']:<8} {row['state']:<14} "
              f"{row['title'][:52]:<52}{blocked}")
    snap = store.snapshot()
    print("\n" + "  ".join(f"{k}={v}" for k, v in snap["counts"].items() if v))
    _emit(snap, args.json)
    store.close()
    return 0


def cmd_transition(args) -> int:
    store = _store()
    try:
        store.transition(args.wp, args.to_state, actor=args.actor,
                         reason=args.reason, candidate_revision=args.candidate)
    except StateError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        store.close()
        return 1
    row = store.get(args.wp)
    print(f"{args.wp}: {row['state']} (candidate {row['candidate_revision'] or '—'})")
    store.close()
    return 0


def cmd_evidence(args) -> int:
    store = _store()
    if args.add:
        digest = None
        if args.artefact:
            from .state import sha256_file
            digest = sha256_file(args.artefact)
        eid = store.add_evidence(args.wp, kind=args.kind, candidate_revision=args.candidate,
                                 actor=args.actor, verdict=args.verdict,
                                 artefact_path=args.artefact, artefact_sha256=digest,
                                 detail=args.detail)
        print(f"evidence #{eid} recorded for {args.wp} on candidate {args.candidate}")
    for row in store.evidence(args.wp):
        print(f"  #{row['id']:<3} {row['kind']:<14} {row['verdict'] or '—':<7} "
              f"{row['actor']:<28} {(row['candidate_revision'] or '')[:12]}")
    store.close()
    return 0


def cmd_history(args) -> int:
    store = _store()
    for row in store.history(args.wp):
        print(f"{row['at']}  {row['from_state'] or '—':<14} → {row['to_state']:<14} "
              f"{row['actor']}  {row['reason'] or ''}")
    store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dume", description=__doc__)
    p.add_argument("--json", action="store_true", help="also emit the machine record")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("inventory", help="WP-001 host capacity probe").set_defaults(func=cmd_inventory)

    w = sub.add_parser("workspace", help="WP-002 workspace boundary report")
    w.add_argument("--probe", action="store_true",
                   help="actually attempt a write in each bound workspace")
    w.set_defaults(func=cmd_workspace)

    cw = sub.add_parser("check-write", help="ask the boundary about one path")
    cw.add_argument("path")
    cw.set_defaults(func=cmd_check_write)

    s = sub.add_parser("secrets", help="WP-003 credential scan")
    s.add_argument("path", nargs="?", default=str(REPO_ROOT))
    s.set_defaults(func=cmd_secrets)

    t = sub.add_parser("toolchain", help="WP-004 toolchain lock")
    t.add_argument("--verify", action="store_true", help="compare live against the lock")
    t.add_argument("--wave", type=int, default=1)
    t.set_defaults(func=cmd_toolchain)

    sub.add_parser("upstream", help="check every pin against upstream").set_defaults(func=cmd_upstream)

    sc = sub.add_parser("scenarios", help="run the adversarial acceptance scenarios")
    sc.add_argument("-v", "--verbose", action="store_true", help="show every step")
    sc.set_defaults(func=cmd_scenarios)
    sub.add_parser("seed", help="load the work-package catalogue").set_defaults(func=cmd_seed)

    st = sub.add_parser("status", help="commissioning state")
    st.add_argument("--wave", type=int)
    st.set_defaults(func=cmd_status)

    tr = sub.add_parser("transition", help="move a package through the lifecycle")
    tr.add_argument("wp")
    tr.add_argument("to_state")
    tr.add_argument("--actor", required=True)
    tr.add_argument("--reason")
    tr.add_argument("--candidate")
    tr.set_defaults(func=cmd_transition)

    ev = sub.add_parser("evidence", help="record or list evidence")
    ev.add_argument("wp")
    ev.add_argument("--add", action="store_true")
    ev.add_argument("--kind", default="verification")
    ev.add_argument("--candidate")
    ev.add_argument("--actor")
    ev.add_argument("--verdict")
    ev.add_argument("--artefact")
    ev.add_argument("--detail")
    ev.set_defaults(func=cmd_evidence)

    h = sub.add_parser("history", help="every transition for one package")
    h.add_argument("wp")
    h.set_defaults(func=cmd_history)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
