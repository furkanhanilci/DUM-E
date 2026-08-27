# DUM-E as built — the design, and where each piece lives

> A supervised multi-model engineering harness. No single model writes the code,
> judges the code, and declares it done.

This maps the commissioning design onto the code that now implements it, and is
honest about what is borrowed, what was rebuilt, and what is still absent.

## The flow

```
Human ──▶ Command Gateway ──▶ Orchestrator
                                  │
          SPEC_MOUNT (read-only) ─┴─▶ WP Packet Builder
                                        │
                                        ▼
                                  Cohort Compiler
                                        │
                                        ▼
                                 Runtime Control ──▶ Qwen (GPU 0)
                                        │        └──▶ Mistral (GPU 1)
                                        ▼
                                  Buzz (channel, mentions)
                                        │
                                        ▼
                        Superpowers skills ──▶ isolated worktree
                                        │
                    RED ─▶ GREEN ─▶ spec review ─▶ code review
                                        │
                                        ▼
                        fresh verification ─▶ machine gate
```

## Component by component

| Design element | Where it lives | Borrowed or built |
|---|---|---|
| WP Packet Builder | `dume/packets/wp_packet_builder.py` | **built** — mechanical, never a summary |
| Cohort Compiler | `dume/cohort/compiler.py` | **built** — signals read from package-specific text |
| Role registry | `dume/cohort/role_registry.py` | **built** — 8 roles, two kinds of independence |
| Runtime control | `dume/runtimes/profiles.py` | **built** — 10 statuses, RESERVE, eligible pool |
| Runtime switching | `dume/runtimes/handoff.py` | **built** — task crosses, conversation does not |
| Failure taxonomy | `dume/runtimes/failures.py` | **built** — 9 classes, retry decided per class |
| Local serving | `dume/runtimes/qwen.py` | **borrowed** — llama.cpp CUDA, pinned image |
| Model qualification | `dume/runtimes/qualification.py` | **built** — four trials, two about judgement |
| Engineering discipline | `dume/review/skills.py` | **borrowed** — Superpowers `SKILL.md`, injected whole |
| Discipline proof | `dume/review/discipline.py` | **built** — Superpowers ships no gate and says so |
| Worktree isolation | `dume/worktrees/manager.py` | **borrowed** — git worktrees, protected paths enforced |
| Agent capability boundary | `dume/control/agent_tools.py` | **built** — four tools, scoped to one worktree |
| The three reviews | `dume/control/model_executor.py` | **built** — three questions, three identities |
| Machine gate | `dume/acceptance/gate.py` | **built** — eleven checks, no model in it |
| Durable state | `dume/state/store.py` | **built** — SQLite, closed lifecycle |
| Collaboration | `dume/collaboration/buzz.py` | **borrowed** — Buzz relay over its NIP-98 bridge |
| Human commands | `dume/control/command_gateway.py` | **built** — 19 actions, four classes, no shell |
| Telegram | `dume/control/telegram.py` | **built** — a surface, never authority |

## What was borrowed, and how much of it

**Buzz** — `github.com/block/buzz`, pinned `0720f538`, Apache-2.0, running as a
relay with Postgres, Redis and MinIO. DUM-E uses three of its primitives: a
keypair is an identity, a UUID in an `h` tag is a channel, a `p` tag wakes a
participant. Reached over the relay's NIP-98 HTTP bridge, signed from Python.

Not used, and why: **personas (30175), teams (30176) and managed agents (30177)
are authored in Buzz's desktop application**, which cannot run on this host
(its AppImage needs glibc 2.39; this box has 2.35), and there is no headless API
for them — `buzz agents draft-create` only opens a form. `get_agent_models` is
likewise a desktop command. So DUM-E owns its own agent processes, which is the
path Buzz's own `docs/remote-agents.md` describes: *"anything that can set that
environment and exec the harness is a conforming launcher."* ACP and kind-24200
runtime switching are not used yet; building the Rust CLI to reach them costs
10–14 GiB and has not been worth it.

**Superpowers** — `github.com/obra/superpowers`, pinned `b36e0829` (v6.3.0),
MIT. The official marketplace pins the same commit, so the official install is
the pinned install. The agents receive the real `SKILL.md` text per role, the
primary skill whole. A drifted install refuses to run rather than substituting
harness prose.

What it does not supply, and says so: **a machine gate.** Every stage boundary
in Superpowers is prose the model may skip. So the gate and the discipline
verifier are DUM-E's, and the verifier keeps invocation, artefact and
independent evidence in separate columns — an invoked skill is never accepted as
evidence of correctness.

**llama.cpp** — pinned CUDA image whose `NVIDIA_REQUIRE_CUDA` admits exactly
this host's driver, and the only candidate stack that grammar-constrains Qwen's
XML tool call rather than parsing it afterwards.

## The invariants, and what enforces each

| Invariant | Enforced by |
|---|---|
| Producer ≠ reviewer ≠ verifier | `state/store.py` refuses the transition |
| `TECH_COMPLETE` ≠ `ACCEPTED` | closed transition table |
| Evidence binds to a candidate | stale evidence refused at the stage gate |
| Fresh verification | `_verify` clones the candidate and re-runs |
| A model may not overrule an exit code | the verifier interprets, the exit code decides |
| Sealed spec is read-only | `workspace.py` resolves symlinks before deciding |
| Protected paths untouched | checked on the diff before any review runs |
| Secrets never in packets, logs or evidence | `secrets.py`, redaction at `json_dump` |
| Untrusted content is data | `command_gateway.py` — closed vocabulary, no shell |
| No eligible runtime → `BLOCKED_RUNTIME` | `profiles.py`, with a per-runtime explanation |
| Runtime failure ≠ implementation failure | `failures.py`, and the switch that follows it |
| Assurance does not shrink | there is no code path that lowers it |

## What one live run actually did

Recorded in `evidence/live/`, 370.7 seconds, no synthetic executor anywhere:

| stage | what happened |
|---|---|
| packet | 3 frozen sections, 5 deliverables, digest `d0ae2a32d17c` |
| cohort | BASELINE, 7 role slots |
| binding | architect + implementer → qwen · the three reviews → mistral |
| worktree | `dume/WP-001/live-d0ae2a32` off a recorded base |
| implement | **RED exit 2 → GREEN exit 0**, 7 tool calls |
| protected paths | 4 files changed, none protected |
| specification review | PASS — a different family, shown only the frozen spec and the diff |
| code review | PASS — a second identity, not shown the first verdict |
| verification | PASS — fresh clone, `pytest exit=0`, 18 tests |
| machine gate | **MERGE_ELIGIBLE** — eleven checks over recorded facts |

The exit code decided verification; the model interpreted it and could not have
overruled it. `tests/test_gate_and_pilot.py` re-reads that evidence and asserts
the artefacts agree with each other — that every stage ran, that a failing test
preceded a passing one, that no reviewer shared the implementer's family, and
that the discipline injected was the pinned revision.

## What is still absent

- **ACP** — Buzz's harness protocol is not used; DUM-E speaks OpenAI-compatible
  HTTP directly. Closing this is WP-015/WP-016.
- **Personas and teams as Buzz records** — desktop-only at this revision.
- **Live runtime switching over kind 24200** — implemented in Buzz, not used here.
- **An accepted package** — no independent verifier identity is bound, and the
  store refuses to let the producer accept its own work.
- **A bound target** — `SPEC_MOUNT` and `BUILD_TARGET` are declared, unbound
  configuration slots ([ADR-0003](adr/ADR-0003-specification-and-target-workspaces-are-unbound.md)).
  Binding one is the single action this harness refuses to take on its own.
