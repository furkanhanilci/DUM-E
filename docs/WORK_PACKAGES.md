# The 54 commissioning work packages

DUM-E is commissioned by the same pipeline it provides. These are its own
bring-up packages — not work for whatever repository it is later pointed
at. Each is registered in the state store with its wave and hard
dependencies, and every state below is **read live from `state/dume.db`**,
not transcribed.

```bash
python3 -m dume.cli seed                 # register every package and its dependencies
python3 -m dume.cli status               # the live table
python3 -m dume.cli history WP-001       # every transition, with its actor
python3 -m dume.cli evidence WP-001      # the receipts bound to each candidate
```

## Where it stands

| State | Count | Means |
|---|---|---|
| `DISCOVERED` | 52 | registered from the catalogue; not started |
| `ACCEPTANCE_READY` | 1 | walked the pipeline and reached the gate; awaiting an independent acceptance identity |
| `BLOCKED` | 1 | a precondition is unmet, and improvising past it is not an option |

No package is `ACCEPTED`. That is not a technical gap: acceptance requires
an independent verifier identity, and the store refuses to let the actor
that produced this code accept it.

## Waves

A package becomes `READY` only when every hard dependency has cleared. The
54 packages resolve into 28 waves; a wave number is the
earliest wave a package could start in, not a schedule.

### 01 — Foundation

Measure the host, draw the boundaries, lock the toolchain. Nothing above this layer is trustworthy if this one is guessed at.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-001` | 1 | Host Hardware, OS and Capacity Inventory | — | `ACCEPTANCE_READY` |
| `WP-002` | 2 | Three-Workspace Boundary and Read-Only Specification Mount | WP-001 | `BLOCKED` |
| `WP-003` | 3 | Secrets, Credentials and Local Trust Foundation | WP-001, WP-002 | `DISCOVERED` |
| `WP-004` | 3 | Pinned Toolchain, Reproducible Environment and Provenance Lock | WP-001, WP-002 | `DISCOVERED` |

### 02 — Local Qwen

A local model that costs no external quota and whose failure does not depend on someone else's billing period.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-005` | 4 | Local Qwen Deployment Profile Decision | WP-001, WP-004 | `DISCOVERED` |
| `WP-006` | 5 | Qwen3.8-27B Model Acquisition, License and Digest Lock | WP-005 | `DISCOVERED` |
| `WP-007` | 6 | High-Throughput Qwen Service: vLLM/SGLang | WP-006 | `DISCOVERED` |
| `WP-008` | 6 | Quantized Qwen Fallback: llama.cpp/GGUF | WP-006 | `DISCOVERED` |
| `WP-009` | 7 | Qwen Tool Calling, Structured Output and Reasoning Compatibility | WP-007 | `DISCOVERED` |
| `WP-010` | 8 | Qwen Service Hardening, Health, Restart and Capacity Control | WP-009 | `DISCOVERED` |

### 03 — Buzz

The collaboration substrate: channels, identity, mentions, audit. A verdict needs an addressee.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-011` | 4 | Buzz Upstream Pin, Build and Developer Bring-Up | WP-004 | `DISCOVERED` |
| `WP-012` | 5 | Buzz Relay Persistent Self-Hosted Deployment | WP-003, WP-011 | `DISCOVERED` |
| `WP-013` | 6 | Buzz Identity, Persona and Team Baseline | WP-012 | `DISCOVERED` |
| `WP-014` | 7 | Buzz Managed-Agent Lifecycle and Runtime Mutation | WP-013 | `DISCOVERED` |
| `WP-015` | 8 | Buzz ACP and External Runtime Harness Integration | WP-014 | `DISCOVERED` |
| `WP-018` | 8 | Buzz Channels, Threads, Mentions, Access and Operational Audit | WP-013, WP-014 | `DISCOVERED` |
| `WP-016` | 9 | Local Qwen Harness Integration Bake-Off | WP-009, WP-014, WP-015 | `DISCOVERED` |
| `WP-017` | 10 | Buzz Model Discovery and Manual Runtime/Model Switching | WP-014, WP-015, WP-016 | `DISCOVERED` |

### 04 — Superpowers

The engineering discipline, pinned and projected into the agents that are held to it.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-019` | 4 | Superpowers Upstream Pin, License and Skill Inventory | WP-004 | `DISCOVERED` |
| `WP-020` | 9 | Superpowers Installation Across Claude, Codex and Hermes | WP-015, WP-019 | `DISCOVERED` |
| `WP-021` | 10 | Superpowers Enforcement for Selected Local-Qwen Harness | WP-016, WP-019 | `DISCOVERED` |
| `WP-022` | 11 | Mandatory Engineering Protocol State Machine | WP-020, WP-021 | `DISCOVERED` |

### 05 — Model Runtime

Runtimes as a catalogue with capabilities, health, cost and failure classes — availability is not eligibility.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-023` | 12 | Runtime and Model Catalog with Capability Profiles | WP-017, WP-022 | `DISCOVERED` |
| `WP-024` | 13 | Runtime Health, Quota and Failure Classification | WP-023 | `DISCOVERED` |
| `WP-025` | 14 | Role-Aware Cost, Scarcity and Routing Policy | WP-023, WP-024 | `DISCOVERED` |
| `WP-026` | 15 | Safe Runtime Switching, Checkpoint and Handoff | WP-017, WP-024, WP-025 | `DISCOVERED` |
| `WP-027` | 16 | Independence-Aware Reviewer and Verifier Routing | WP-025, WP-026 | `DISCOVERED` |

### 06 — Dume Core

The harness proper: packets, cohorts, context projection, orchestration.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-028` | 4 | DUM-E Repository Skeleton, Configuration and SQLite State | WP-002, WP-003, WP-004 | `DISCOVERED` |
| `WP-029` | 5 | Deterministic WP Packet Builder | WP-028 | `DISCOVERED` |
| `WP-030` | 15 | Task Compiler and Cohort Manifest | WP-023, WP-025, WP-029 | `DISCOVERED` |
| `WP-031` | 16 | Role/Persona Binding and Buzz Cohort Deployment | WP-013, WP-014, WP-030 | `DISCOVERED` |
| `WP-034` | 16 | Fable-5 Commissioning Orchestrator and Control-Plane Fallback | WP-023, WP-025, WP-026, WP-030 | `DISCOVERED` |
| `WP-032` | 17 | Context Projection and Independent-First Embargo | WP-018, WP-030, WP-031 | `DISCOVERED` |
| `WP-033` | 17 | Dynamic Specialist Triggering and Routing | WP-027, WP-030 | `DISCOVERED` |

### 07 — Engineering Pipeline

Worktree to gate: implement, review twice, verify independently, then decide by machine.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-035` | 5 | Git Worktree Manager and Protected-Path Enforcement | WP-004, WP-028 | `DISCOVERED` |
| `WP-036` | 17 | Implementation Executor with RED→GREEN→REFACTOR Evidence | WP-022, WP-031, WP-035 | `DISCOVERED` |
| `WP-037` | 18 | Specification Compliance Review Stage | WP-032, WP-036 | `DISCOVERED` |
| `WP-038` | 19 | Code Quality and Architecture Review Stage | WP-027, WP-037 | `DISCOVERED` |
| `WP-039` | 20 | Fresh Independent Verification Stage | WP-027, WP-035, WP-038 | `DISCOVERED` |
| `WP-040` | 21 | Deterministic Merge-Eligibility Gate | WP-037, WP-038, WP-039 | `DISCOVERED` |
| `WP-041` | 22 | Evidence, Receipts and Commissioning Artifact Store | WP-028, WP-040 | `DISCOVERED` |
| `WP-042` | 23 | Failure Taxonomy, Retry and Correction Loop | WP-024, WP-036, WP-039, WP-041 | `DISCOVERED` |

### 08 — Security Control

What an agent may touch, what a human may command, and what arrives from outside.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-047` | 6 | Supply-Chain, License and Upstream-Drift Gate | WP-004, WP-006, WP-011, WP-019 | `DISCOVERED` |
| `WP-043` | 10 | Capability, Tool and Secret Boundary for Agents | WP-003, WP-014, WP-016, WP-028 | `DISCOVERED` |
| `WP-044` | 11 | Untrusted External Content Quarantine | WP-018, WP-043 | `DISCOVERED` |
| `WP-045` | 17 | Authenticated Human Command Gateway and Control Semantics | WP-018, WP-034, WP-043 | `DISCOVERED` |
| `WP-046` | 18 | Optional Telegram Control Bridge with Safe Command Translation | WP-045 | `DISCOVERED` |

### 09 — Observability Recovery

Seeing what happened, and surviving a restart without inventing state.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-048` | 23 | Structured Logs, Metrics and Operator Status View | WP-018, WP-024, WP-028, WP-041 | `DISCOVERED` |
| `WP-049` | 24 | DUM-E Restart, Crash Recovery and Idempotent Resume | WP-041, WP-048 | `DISCOVERED` |
| `WP-050` | 25 | Pause, Cancel, Kill-Switch and Safe Shutdown | WP-045, WP-049 | `DISCOVERED` |

### 10 — Qualification

A model is not qualified by assertion. Trials, per role, recorded.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-051` | 17 | DUM-E Model Qualification Arena and Role Matrix | WP-009, WP-017, WP-022, WP-023, WP-024, WP-025, WP-027 | `DISCOVERED` |

### 11 — Pilot Commissioning

Prove it end to end — synthetically first, then for real.

| WP | Wave | Title | Depends on | State |
|---|---|---|---|---|
| `WP-052` | 26 | Synthetic End-to-End Commissioning Pilot | WP-040, WP-041, WP-042, WP-047, WP-048, WP-049, WP-050, WP-051 | `DISCOVERED` |
| `WP-053` | 27 | First Real Low-Risk Target-Repository Pilot | WP-052 | `DISCOVERED` |
| `WP-054` | 28 | Two Heterogeneous Pilots and DUM-E v0.1 Acceptance | WP-053 | `DISCOVERED` |

## Regenerating this file

Generated from the live state store. Re-run after a transition rather than
editing by hand — an edit here is a divergence nothing can detect.

```bash
python3 scripts/generate_work_packages_doc.py           # write
python3 scripts/generate_work_packages_doc.py --check   # fail if stale
```
