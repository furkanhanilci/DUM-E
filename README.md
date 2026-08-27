# DUM-E

**A supervised multi-model software engineering harness.**

DUM-E takes a frozen work package, hands it to a cohort of language models in
separate roles, and either produces a candidate that survives independent review
and verification — or refuses it, with the reason on the record. The point is not
that models write code. The point is that **no single model gets to write the
code, judge the code, and declare it done.**

It is deliberately small. DUM-E owns the glue between a work package and a
merge-eligible candidate and nothing else: packetisation, cohort and runtime
binding, model fallback, worktree execution, review and verification sequencing,
durable state, and a deterministic gate. It is not an agent framework, not a
model server, not an editor, and not the project it builds — the target is bound
as configuration, and the harness holds no opinion about what that target is.

## Status

**v0.1.0.dev0 — the pipeline runs end to end. Nothing is `ACCEPTED`.**

One task of WP-001 has walked the full pipeline against live models and reached
`MERGE_ELIGIBLE`, five times in a row. What is missing is not code: no
independent verifier identity is bound, and the state store refuses to let the
actor that produced this accept it. See
[`docs/COMMISSIONING_STATUS.md`](docs/COMMISSIONING_STATUS.md).

## The pipeline

```
READY → packet → cohort → runtime binding → worktree → plan → RED/GREEN
      → spec review → code review → fresh verification → machine gate
```

Each review stage is gated on the previous one having passed **on the current
candidate**, so a package cannot walk the whole pipeline with no verdict and be
caught only at the end. Verification must be independent of both reviewers.

## Roles, and why there are eight of them

A role is not an agent, not a persona, not a runtime, not a model and not a
provider account. Those five can change while the role stays the same, which is
what lets a runtime be swapped mid-task without the authority attached to the
work moving with it.

| Role | Decides | Independent of |
|---|---|---|
| `human_commander` | Scope, architecture conflicts, irreversible actions | — (a person; no runtime) |
| `commissioning_orchestrator` | Sequencing only — never whether a stage passed | — (the harness itself; no runtime) |
| `architect` | The shape of the change, and whether the requirement is satisfiable | — |
| `implementer` | **Nothing about its own correctness** | spec/code reviewer, verifier |
| `spec_reviewer` | Was the requirement met? Nothing about quality | implementer — *and a different model family* |
| `code_reviewer` | Is the implementation good? Nothing about whether it runs | implementer, spec reviewer (+ family) |
| `verifier` | Does it actually work? The only role whose PASS is evidence of behaviour | all three (+ family, + fresh context) |
| `specialist` | Only its own domain, and only as a finding — never a gate verdict | implementer |

Two kinds of independence, because they cost differently. *Identity*
independence means two agent instances answer two questions — one instance
answering both is one opinion wearing two hats. *Family* independence is
stricter and reserved for where it buys something: two agents of the same model
family fail the same way, so a check performed by the implementer's own family
is not independent evidence, it is the same blind spot twice.

Full detail, including which Superpowers skill each role is held to:
[`docs/SUPERPOWERS_ROLES.md`](docs/SUPERPOWERS_ROLES.md).

## Installing it

Start at [`docs/INSTALL.md`](docs/INSTALL.md). The short version:

```bash
git clone https://github.com/furkanhanilci/DUM-E.git && cd DUM-E
python3 -m dume.cli inventory     # measure this host — no dependencies needed
uv venv .venv && uv pip install pytest
.venv/bin/python -m pytest -q
```

The foundation carries **no third-party runtime dependency** — it must be able
to run as the first thing on a new host, before anything is installed
([ADR-0001](docs/adr/ADR-0001-foundation-has-no-third-party-dependencies.md)).
Everything past the foundation — the Buzz relay, local model serving, Telegram —
is optional and brought up in the order `docs/INSTALL.md` gives.

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
| Which discipline each role is held to | `dume skills` |
| Model qualification trials | `dume qualify` |
| Adversarial acceptance scenarios | `dume scenarios -v` |
| Synthetic end-to-end pilot with fault injection | `dume pilot -v` |
| A real run against a bound target | `dume commission WP-nnn` |
| Proof the engineering discipline was applied | `dume discipline --transcript FILE` |
| Durable lifecycle for all 54 packages | `dume seed`, `status`, `transition`, `evidence`, `history` |

## The controls are mechanisms, not instructions

A prose warning is not a control if the system can still perform the unsafe
action. Every claim below is enforced in code and attacked in `tests/` and
`dume/scenarios.py`:

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

## The target is a slot, not a dependency

`SPEC_MOUNT` and `BUILD_TARGET` ship **unbound**: declared, with their modes
fixed (`READ_ONLY` and `READ_WRITE`), and bound to nothing
([ADR-0003](docs/adr/ADR-0003-specification-and-target-workspaces-are-unbound.md)).
An unbound slot grants nothing — `Boundary.check_write` refuses every path
outside a *bound* workspace — so a fresh clone cannot write anywhere it was not
told to. Binding one is a deliberate, recorded, human act:

```
bind_workspace BUILD_TARGET /path/to/the/repository/being/built
```

The boundary mechanism is proven against a synthetic three-workspace fixture of
the same shape, so the control is tested without any real target being present.

## Layout

```
dume/
  packets/      the frozen work-package packet
  cohort/       role registry and cohort compilation
  runtimes/     runtime catalogue, probing, qualification, handoff, local serving
  control/      orchestrator, executors, command gateway, Telegram, narration
  review/       Superpowers skill projection and the discipline verifier
  acceptance/   the deterministic merge-eligibility gate
  state/        SQLite lifecycle, evidence, findings — where the invariants live
  collaboration/ Buzz relay over its NIP-98 bridge, GitHub, host identity
  workspace.py  the write boundary
  secrets.py    the credential boundary
  scenarios.py  adversarial acceptance scenarios
config/         configuration and the three locks
docs/adr/       DUME-ADRs
evidence/       commissioning receipts — append-only
tests/          293 tests, most of them attacks
```

## Documentation

| | |
|---|---|
| [`docs/INSTALL.md`](docs/INSTALL.md) | Bring-up from a bare host, in dependency order |
| [`docs/SUPERPOWERS_ROLES.md`](docs/SUPERPOWERS_ROLES.md) | How the engineering discipline reaches the agents |
| [`docs/WORK_PACKAGES.md`](docs/WORK_PACKAGES.md) | The 54 commissioning packages, by wave, with live state |
| [`docs/ARCHITECTURE_AS_BUILT.md`](docs/ARCHITECTURE_AS_BUILT.md) | The design mapped onto the code that implements it |
| [`docs/COMMISSIONING_STATUS.md`](docs/COMMISSIONING_STATUS.md) | What is running, what is demonstrated, what is not |
| [`docs/adr/`](docs/adr/) | Nine decisions, with their consequences |

## Obsidian mirror

`scripts/mirror_dume.py` generates a reading mirror of the commissioning
programme into `<vault>/10 - Projects/DUM-E/`, following the vault's existing
project conventions. Work-package notes carry live state (`wp_state`, `wave`,
`candidate_revision`) read from the DUM-E state store.

Tags stay inside the `dume/` namespace, so a note from here can never be a lint
finding in another project's controlled vocabulary.

```bash
python3 scripts/mirror_dume.py           # write the mirror
python3 scripts/mirror_dume.py --check   # fail if the mirror is stale or hand-edited
```

The mirror is generated. Edit the canonical file and re-run; an edit made in the
vault is a divergence nothing can detect.
