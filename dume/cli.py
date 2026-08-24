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
from .cohort.compiler import compile_cohort
from .control import pilot as pilot_module
from .packets.wp_packet_builder import PacketBuilder
from .control.command_gateway import (ACTIONS, CommandGateway, CommandRefused,
                                      Principal)
from .control.intent_handler import IntentHandler
from .review import discipline as discipline_module
from .review import skills as skills_module
from .runtimes import qualification as qualification_module
from .runtimes import qwen as qwen_module
from .runtimes.probe import probe as probe_runtimes
from .runtimes.profiles import NoEligibleRuntime, RuntimeRegistry
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


def cmd_packet(args) -> int:
    store = _store()
    states = {r["wp_id"]: dict(r) for r in store.all_wps()}
    store.close()
    builder = PacketBuilder(spec_revision=args.spec_revision)
    packet = builder.build(args.wp, dependency_states=states)
    out = builder.write(packet, EVIDENCE / args.wp)
    print(f"{packet.wp_id}  {packet.title}")
    print(f"  workstream      {packet.workstream}  wave {packet.wave}")
    print(f"  owner           {packet.owner}")
    print(f"  verifier role   {packet.verifier_role}")
    print(f"  frozen sections {', '.join(s.name for s in packet.sections)}")
    print(f"  deliverables    {len(packet.deliverables)}")
    print(f"  dependencies    " + (", ".join(
        f"{d['wp_id']}({d['state']})" for d in packet.dependencies) or "none"))
    print(f"  scenarios       " + (", ".join(
        s["scenario"] for s in packet.acceptance_scenarios) or "none yet"))
    print(f"  schemas/ADRs    {len(packet.schemas)}/{len(packet.adrs)}")
    print(f"  forbidden       {len(packet.forbidden)} actions")
    print(f"  non-waivable    {len(packet.non_waivable_rules)} rules")
    print(f"  digest          {packet.packet_sha256}")
    print(f"  written         {out}")
    _emit(packet.as_dict(include_text=False), args.json)
    return 0


def cmd_cohort(args) -> int:
    store = _store()
    states = {r["wp_id"]: dict(r) for r in store.all_wps()}
    store.close()
    packet = PacketBuilder().build(args.wp, dependency_states=states)
    cohort = compile_cohort(packet)
    print(f"{cohort.wp_id}  assurance {cohort.assurance_level}")
    print("  signals: " + ", ".join(k for k, v in cohort.signals.items()
                                    if v is True) or "  signals: none")
    for slot in cohort.roles:
        extra = f" — {slot.task}" if slot.task else ""
        print(f"  {slot.role_id:<28}{extra}")
        for requirement in slot.independence_requirements:
            print(f"      {requirement}")
    if cohort.specialists:
        print("  specialists:")
        for spec in cohort.specialists:
            print(f"      {spec['specialist']:<14} {spec['trigger']}")
    print(f"  embargo: {cohort.context_projection['embargo']}")
    _emit(cohort.as_dict(), args.json)
    return 0


def cmd_runtime(args) -> int:
    registry = RuntimeRegistry.load()
    if args.probe:
        result = probe_runtimes(registry)
        registry.save()
        print(f"probed {len(result['results'])} runtime(s)")
    for row in registry.table():
        print(f"  {row['icon']} {row['runtime']:<18} {row['status']:<17} "
              f"{row['mode']:<9} {row['model'][:34]}")
        if row["reason"]:
            print(f"      {row['reason'][:100]}")
    usable = [r for r in registry.runtimes.values() if r.usable()]
    qualified = [r for r in registry.runtimes.values() if r.qualified_roles]
    print(f"\nusable now: {len(usable)}   qualified for any role: {len(qualified)}")
    if args.bind:
        try:
            binding = registry.bind(args.bind)
            print(f"would bind {args.bind} → {binding.runtime_id}")
        except NoEligibleRuntime as exc:
            print(f"\nBLOCKED_RUNTIME\n{exc}")
            return 1
    _emit({"runtimes": registry.table()}, args.json)
    return 0


def cmd_qwen(args) -> int:
    if args.serve:
        result = qwen_module.serve(force=args.force)
        print(result.detail)
        if not result.started:
            return 1
    ready = qwen_module.preflight()
    for check in ready["checks"]:
        mark = "ok " if check["passed"] else "NO "
        print(f"  {mark} {check['check']:<22} {check['detail'][:72]}")
    template = qwen_module.template_asserts()
    if template.get("checked"):
        print(f"  {'NO ' if template['needs_patch'] else 'ok '} "
              f"{'chat_template':<22} {template['detail'][:72]}")
    health = qwen_module.health()
    print(f"\nendpoint {health['endpoint']} — "
          + (f"up, serving {', '.join(health['models'])}" if health["up"]
             else f"down ({health.get('detail', '')[:80]})"))
    if health["up"] and args.probe:
        probe = qwen_module.tool_call_probe()
        print(f"tool-call probe: {'called' if probe['called'] else 'NO CALL'} — "
              f"{probe.get('detail', '')}")
    _emit({"preflight": ready, "template": template, "health": health}, args.json)
    return 0 if health["up"] else 1


def cmd_qualify(args) -> int:
    result = qualification_module.qualify(args.runtime, args.endpoint,
                                          repeats=args.repeats)
    for trial in result.trials:
        mark = "PASS" if trial.passed else "FAIL"
        print(f"  {mark}  {trial.name:<26} {trial.seconds:6.1f}s  {trial.detail[:60]}")
    print(f"\nqualified for: {', '.join(result.qualified_roles) or 'nothing'}")
    for role, why in result.refused_roles.items():
        print(f"  refused {role:<16} {why}")
    if args.record:
        registry = RuntimeRegistry.load()
        runtime = registry.get(args.runtime)
        runtime.qualified_roles = list(result.qualified_roles)
        registry.save()
        print(f"\nrecorded against {args.runtime} in the runtime registry")
    out = EVIDENCE / "qualification" / f"{args.runtime}.json"
    print(f"recorded: {out}  sha256={json_dump(result.as_dict(), out)}")
    _emit(result.as_dict(), args.json)
    return 0 if result.qualified_roles else 1


def cmd_skills(args) -> int:
    """What discipline each role is actually held to."""
    lock = json.loads((REPO_ROOT / "config" / "upstream.lock.json").read_text())
    expected = next((u["pinned_revision"] for u in lock["upstreams"]
                     if u["name"] == "superpowers"), None)
    try:
        installed = skills_module.installed_revision()
    except skills_module.SkillsUnavailable as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    match = installed == expected
    print(f"installed {installed or '—'}")
    print(f"pinned    {expected or '—'}   {'match' if match else 'DRIFT'}")
    print()
    report = {}
    for role in sorted(skills_module.ROLE_BUNDLES):
        try:
            bundle = skills_module.bundle_for(role, expected_revision=expected)
        except skills_module.SkillsUnavailable as exc:
            print(f"  {role:<16} unavailable: {exc}")
            continue
        report[role] = bundle.as_dict()
        names = ", ".join(("*" if s.primary else "") + s.name for s in bundle.skills)
        print(f"  {role:<16} {len(bundle.text):>6} chars   {names}")
        if args.show == role:
            print()
            print(bundle.text)
    print("\n* = injected whole. The rest are their own description and overview.")
    print("Injection is an input. Whether it was obeyed is answered by the "
          "red-then-green exit codes and the independent reviews.")
    _emit(report, args.json)
    return 0 if match else 1


def cmd_reliability(args) -> int:
    from .control import reliability
    report = reliability.run(times=args.runs, wp_id=args.wp)
    for attempt in report["attempts"]:
        print(f"  #{attempt['index']}  {attempt['verdict']:<16} reached "
              f"{attempt['reached']:<26} {attempt['seconds']:>6.1f}s")
        if attempt["stopped_at"]:
            print(f"      stopped at {attempt['stopped_at']}: {attempt['detail'][:80]}")
    print(f"\n{report['merge_eligible']}/{report['runs']} reached MERGE_ELIGIBLE "
          f"({report['success_rate']:.0%})")
    print(f"deepest stage reached: {report['deepest_stage']}")
    if report["stopped_at"]:
        print("stopped at: " + ", ".join(f"{k} ×{v}" for k, v in
                                         report["stopped_at"].items()))
        print("same failure every time" if report["same_failure_every_time"]
              else "different failures — a reliability problem, not one bug")
    out = EVIDENCE / "reliability" / f"{args.wp}.json"
    print(f"recorded: {out}  sha256={json_dump(report, out)}")
    _emit(report, args.json)
    return 0 if report["merge_eligible"] else 1


def cmd_live(args) -> int:
    from .control import live
    report = live.run(args.wp, evidence_root=EVIDENCE / "live")
    for step in report.get("steps", []):
        print(f"  {step['name']:<28} {step['outcome']:<8} {step['detail'][:96]}")
    print(f"\nverdict: {report.get('verdict')}")
    if report.get("channel"):
        print(f"channel: {report['channel']}")
    if report.get("skills"):
        print(f"skills:  {', '.join(report['skills'])} held to the pinned install")
    _emit(report, args.json)
    return 0 if report.get("verdict") == "MERGE_ELIGIBLE" else 1


def cmd_pilot(args) -> int:
    out = EVIDENCE / "synthetic_pilot.json"
    report = pilot_module.run_all(out)
    for case in report["results"]:
        mark = "ok      " if case["matched"] else "MISMATCH"
        print(f"  {mark} {case['case']:<26} expected {case['expected']:<16} "
              f"got {case['verdict']}")
        if args.verbose:
            for step in case["steps"]:
                print(f"           {step['name']:<26} {step['outcome']:<8} "
                      f"{step['detail'][:88]}")
    print(f"\n{report['matched']}/{report['cases']} cases matched — "
          f"{report['verdict']}")
    print(report["note"])
    print(f"recorded: {out}")
    _emit(report, args.json)
    return 0 if report["verdict"] == "PASS" else 1


def cmd_discipline(args) -> int:
    expected = None
    lock = REPO_ROOT / "config" / "upstream.lock.json"
    if lock.is_file():
        data = json.loads(lock.read_text())
        expected = next((u["pinned_revision"] for u in data["upstreams"]
                         if u["name"] == "superpowers"), None)
    report = discipline_module.assess(
        transcript=args.transcript, repo=args.repo or str(REPO_ROOT),
        expected_revision=expected, base=args.base, head=args.head)
    print(f"superpowers   installed {report.installed_revision or '—'}")
    print(f"              expected  {report.expected_revision or '—'}")
    print(f"              enabled   {report.enabled}")
    print(f"              bootstrap {report.bootstrap_observed}")
    if report.skills_invoked:
        print(f"  skills invoked: {', '.join(report.skills_invoked)}")
    for signal in report.signals:
        mark = "yes" if signal.present else "no "
        print(f"  {mark} [{signal.kind:<11}] {signal.stage:<20} {signal.detail[:70]}")
    print(f"\nverdict: {report.verdict()}")
    for gap in report.gaps:
        print(f"  gap: {gap}")
    out = EVIDENCE / "discipline_report.json"
    print(f"recorded: {out}  sha256={json_dump(report.as_dict(), out)}")
    _emit(report.as_dict(), args.json)
    return 0


PAUSE_FLAG = REPO_ROOT / "state" / "PAUSED"


def _gateway(actor: str = "console") -> tuple:
    store = _store()
    registry = RuntimeRegistry.load()
    principals = {actor: Principal(actor_id=actor, display_name=actor,
                                   max_class="DANGEROUS_ACTION")}
    gateway = CommandGateway(principals,
                             audit_path=REPO_ROOT / "evidence" / "command_audit.jsonl")
    return store, gateway, IntentHandler(store, registry, PAUSE_FLAG)


def cmd_command(args) -> int:
    """Run one command through the same gateway Telegram and Buzz use."""
    if args.vocabulary:
        store, gateway, _ = _gateway()
        for entry in gateway.vocabulary():
            confirm = "  (needs confirmation)" if entry["needs_confirmation"] else ""
            params = " ".join(f"<{p}>" for p in entry["parameters"])
            print(f"  {entry['class']:<17} {entry['command']} {params}{confirm}")
            print(f"  {'':<17} {entry['summary']}")
        store.close()
        return 0
    store, gateway, handler = _gateway(args.actor)
    try:
        intent = gateway.translate(actor_id=args.actor, channel="cli",
                                   text=" ".join(args.text))
    except CommandRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        store.close()
        return 1
    if intent.authorization_result == "AWAITING_CONFIRMATION":
        print(f"{intent.action} is a DANGEROUS_ACTION.")
        print(f"confirm with: dume command --confirm {intent.confirmation_ref}")
        store.close()
        return 2
    print(handler(intent))
    print(f"\n[{intent.klass}] audit {intent.audit_ref}")
    store.close()
    return 0


def cmd_telegram(args) -> int:
    from .control.telegram import Config, TelegramBridge, TelegramError
    try:
        config = Config.load()
    except TelegramError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    store, _unused, handler = _gateway()
    gateway = CommandGateway(config.principals(),
                             audit_path=REPO_ROOT / "evidence" / "command_audit.jsonl")
    bridge = TelegramBridge(config, gateway, handler)
    try:
        bot = bridge.whoami()
    except TelegramError as exc:
        print(f"cannot reach Telegram: {exc}", file=sys.stderr)
        store.close()
        return 1
    print(f"bot @{bot['username']} ({bot['id']})")
    who = ", ".join(config.allowed) or "NONE — nobody can command this bot"
    print(f"authorised principals: {who}")
    if args.check:
        store.close()
        return 0
    print("polling; Ctrl-C to stop")
    try:
        while True:
            for outcome in bridge.poll_once():
                print(f"  {outcome['outcome']:<22} {outcome.get('actor', '')} "
                      f"{outcome.get('action', outcome.get('reason', ''))[:70]}")
    except KeyboardInterrupt:
        print("stopped")
    finally:
        store.close()
    return 0


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

    pk = sub.add_parser("packet", help="build a deterministic work-package packet")
    pk.add_argument("wp")
    pk.add_argument("--spec-revision", default="pack")
    pk.set_defaults(func=cmd_packet)

    ch = sub.add_parser("cohort", help="compile the cohort a package needs")
    ch.add_argument("wp")
    ch.set_defaults(func=cmd_cohort)

    rt = sub.add_parser("runtime", help="runtime status, probing and binding")
    rt.add_argument("--probe", action="store_true", help="measure what can run here")
    rt.add_argument("--bind", metavar="ROLE", help="try to bind a role, or explain why not")
    rt.set_defaults(func=cmd_runtime)

    qw = sub.add_parser("qwen", help="the local model server")
    qw.add_argument("--serve", action="store_true", help="start it")
    qw.add_argument("--force", action="store_true", help="replace a running container")
    qw.add_argument("--probe", action="store_true", help="send a real tool call")
    qw.set_defaults(func=cmd_qwen)

    ql = sub.add_parser("qualify", help="measure whether a runtime may hold a role")
    ql.add_argument("runtime")
    ql.add_argument("--endpoint", default="http://127.0.0.1:8000/v1")
    ql.add_argument("--repeats", type=int, default=5)
    ql.add_argument("--record", action="store_true",
                    help="write the result into the runtime registry")
    ql.set_defaults(func=cmd_qualify)

    cm = sub.add_parser("command", help="run one command through the gateway")
    # REMAINDER so a hostile message reaches the gateway verbatim instead of
    # being eaten by the argument parser. The gateway is what refuses it, and it
    # cannot refuse what it never sees.
    cm.add_argument("text", nargs=argparse.REMAINDER)
    cm.add_argument("--actor", default="console")
    cm.add_argument("--vocabulary", action="store_true",
                    help="every command that exists — there is no shell")
    cm.add_argument("--confirm", help="confirmation reference for a dangerous action")
    cm.set_defaults(func=cmd_command)

    tg = sub.add_parser("telegram", help="the Telegram control bridge")
    tg.add_argument("--check", action="store_true",
                    help="verify the bot and the allowlist, then exit")
    tg.set_defaults(func=cmd_telegram)

    sk = sub.add_parser("skills", help="what discipline each role is held to")
    sk.add_argument("--show", metavar="ROLE", help="print one role's whole bundle")
    sk.set_defaults(func=cmd_skills)

    lv = sub.add_parser("live", help="a real commissioning run with live models")
    lv.add_argument("wp", nargs="?", default="WP-001")
    lv.set_defaults(func=cmd_live)

    rl = sub.add_parser("reliability", help="repeat a live run and report the spread")
    rl.add_argument("--wp", default="WP-001")
    rl.add_argument("--runs", type=int, default=5)
    rl.set_defaults(func=cmd_reliability)

    pl = sub.add_parser("pilot", help="run the synthetic end-to-end pilot")
    pl.add_argument("-v", "--verbose", action="store_true")
    pl.set_defaults(func=cmd_pilot)

    dc = sub.add_parser("discipline", help="prove the engineering discipline was applied")
    dc.add_argument("--transcript", help="a session transcript JSONL to read")
    dc.add_argument("--repo", help="repository to inspect for artefacts")
    dc.add_argument("--base")
    dc.add_argument("--head")
    dc.set_defaults(func=cmd_discipline)

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
