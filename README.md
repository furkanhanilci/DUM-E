# DUM-E

> DUM-E builds AETHRIONIS. AETHRIONIS does the science.

DUM-E is a deliberately small **commissioning control harness**. The full
AETHRIONIS cannot be a prerequisite for building itself, so this harness owns the
commissioning glue and nothing else: work-package packetisation, cohort and
runtime binding, model fallback, worktree execution, review and verification
sequencing, minimal durable state and deterministic merge eligibility.

When AETHRIONIS has commissioned equivalent capabilities, DUM-E shrinks into a
thin commissioning profile or is retired. It must not grow into a second
AETHRIONIS.

## Status

**v0.1.0.dev0 — the pipeline runs end to end. Nothing is accepted.**

The Buzz relay is up, Superpowers is installed at the pinned revision, the
synthetic pilot drives a package from packet to merge-eligible against a
disposable target, and 150 tests are green. What is missing is not code: no
independent verifier is bound, and the state store refuses to let the actor that
produced this accept it. See
[`docs/COMMISSIONING_STATUS.md`](docs/COMMISSIONING_STATUS.md).

## What is actually built

| Capability | Command |
|---|---|
| Host capacity inventory | `dume inventory` |
| Workspace boundary and read-only specification mount | `dume workspace --probe`, `dume check-write PATH` |
| Credential boundary | `dume secrets PATH` |
| Toolchain lock and drift | `dume toolchain [--verify]` |
| Upstream pin verification | `dume upstream` |
| Deterministic work-package packet | `dume packet WP-nnn` |
| Cohort compilation with independence requirements | `dume cohort WP-nnn` |
| Runtime status, probing and role binding | `dume runtime --probe --bind ROLE` |
| Adversarial acceptance scenarios | `dume scenarios -v` |
| Synthetic end-to-end pilot with fault injection | `dume pilot -v` |
| Proof the engineering discipline was applied | `dume discipline --transcript FILE` |
| Durable lifecycle for all 54 packages | `dume seed`, `status`, `transition`, `evidence`, `history` |

### The pipeline

```
READY → packet → cohort → runtime binding → worktree → plan → RED/GREEN
      → spec review → code review → fresh verification → machine gate
```

Each review stage is gated on the previous one having passed **on the current
candidate**, so a package cannot walk the whole pipeline with no verdict and be
caught only at the end. Verification must be independent of both reviewers.

### What is deployed

- **Buzz relay** — `ghcr.io/block/buzz:sha-0720f53`, Postgres + Redis + MinIO,
  reached over its NIP-98 HTTP bridge signed from Python
  ([ADR-0005](docs/adr/ADR-0005-buzz-over-the-http-bridge.md)).
- **Superpowers 6.3.0** at the pinned `b36e0829`. It enforces nothing, so the
  harness supplies the machine gate and the discipline verifier
  ([ADR-0006](docs/adr/ADR-0006-the-harness-supplies-the-proof-superpowers-does-not.md)).
- **Qwen local** — profile decided: llama.cpp CUDA in Docker, Q4_K_M, single GPU
  ([ADR-0004](docs/adr/ADR-0004-llama-cpp-cuda-is-the-qwen-serving-profile.md)).

## Running it

The foundation carries **no third-party runtime dependency** — it must be able
to run as the first thing on a new host, before anything is installed
([ADR-0001](docs/adr/ADR-0001-foundation-has-no-third-party-dependencies.md)):

```bash
python3 -m dume.cli inventory     # measure this host
python3 -m dume.cli workspace     # what is bound, and what is not
python3 -m dume.cli upstream      # is every pin still what upstream serves?
python3 -m dume.cli scenarios -v  # attack the controls
```

Tests need `pytest` only:

```bash
uv venv .venv && uv pip install pytest
.venv/bin/python -m pytest -q
```

## The controls are mechanisms, not instructions

A prose warning is not a control if the system can still perform the unsafe
action. Every claim below is enforced in code and attacked in
`tests/` and `dume/scenarios.py`:

- **The producer cannot accept its own package**, and cannot launder that
  through a bystander who accepts producer-authored verification.
- **`TECH_COMPLETE` is not `ACCEPTED`.** The lifecycle has no edge from
  implementation straight to acceptance.
- **Evidence binds to an exact candidate revision.** A green result from an
  older candidate is refused as stale, and a candidate that changed after review
  invalidates the review.
- **A zero-byte, missing or digest-mismatched artefact is not evidence.**
- **Evidence is append-only.** A retry adds; it never erases what failed first.
- **The workspace boundary resolves symlinks before deciding**, so a link
  planted in a writable workspace cannot open a door into a read-only one, and
  `../` cannot escape.
- **An unbound workspace grants nothing.** A package that needs one is
  `BLOCKED`, not improvised.
- **An unreachable upstream never reports agreement.** A network failure cannot
  look like "no drift".
- **The credential scanner fails closed** — and lets commit SHAs, model digests
  and placeholders through, because a scanner that cannot be satisfied gets
  switched off.

## AETHRIONIS is not touched

This bring-up commissions DUM-E **in isolation**. No AETHRIONIS clone is created,
read, pinned or modified. `AETHRION_SPEC` and `AETHRION_TARGET` exist as
configuration slots with their modes fixed and `bound: false`
([ADR-0003](docs/adr/ADR-0003-aethrion-workspaces-are-unbound.md)). The boundary
is verified against a synthetic fixture of the same shape, so the control is
proven without the real repository being present.

## Layout

```
dume/           the harness
  state.py      lifecycle, evidence, findings — where the invariants live
  workspace.py  the write boundary
  secrets.py    the credential boundary
  inventory.py  host capacity
  toolchain.py  environment lock and drift
  upstream.py   upstream pins and drift
  scenarios.py  adversarial acceptance scenarios
  catalogue.py  the 54-package plan, read from the implementation pack
config/         configuration and the two locks
evidence/       commissioning receipts — append-only
docs/adr/       DUME-ADRs
tests/          57 tests, most of them attacks
```

## Obsidian mirror

`scripts/mirror_dume.py` generates a reading mirror of the commissioning
programme into `<vault>/10 - Projects/DUM-E/`, following the vault's existing
project conventions — numbered area folders, one index per area, `wp_001_*.md`
file naming, frontmatter that names its source. Work-package notes carry live
state (`wp_state`, `wave`, `candidate_revision`) read from the DUM-E state
store, and scenario notes carry their executed verdict and steps.

Tags use the `dume/` namespace. The vault's controlled vocabulary governs
`aethrionis/` only, so a separate project takes a separate namespace rather than
enlarging someone else's.

```bash
python3 scripts/mirror_dume.py           # write the mirror
python3 scripts/mirror_dume.py --check   # fail if the mirror is stale or hand-edited
```

The mirror is generated. Edit the canonical file — the pack or this repository —
and re-run. An edit made in the vault is a divergence nothing can detect.

## Source of the plan

`/home/otonom/Desktop/FH/DUME_COMMISSIONING_IMPLEMENTATION_PACK` — 54 work
packages across 28 waves, 36 adversarial acceptance scenarios, 9 logical
schemas. The plan is read from there rather than retyped, so a hand-copied
dependency list cannot disagree with it on the day it matters.
