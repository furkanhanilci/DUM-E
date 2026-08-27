# DUM-E v0.1 — commissioning report

**Date:** 2026-08-24
**Verdict:** `TECH_COMPLETE` — the harness works end to end. Nothing is `ACCEPTED`.

## Revisions

| Component | Revision |
|---|---|
| DUM-E | see `git log`; the state store holds the candidate under review |
| Buzz | `0720f5380ce8a6c050afac159f8462c06cd51ab5`, Apache-2.0 |
| Superpowers | `b36e0829c6d0140e93cfef2ca599b1b07d4a7797` (v6.3.0), MIT |
| Qwen3.8-27B | `unsloth/Qwen3.8-27B-GGUF` Q4_K_M, Apache-2.0 |
| Mistral-Small-3.2-24B | `unsloth/…-2506-GGUF` Q4_K_M |
| Serving | `ghcr.io/ggml-org/llama.cpp:server-cuda`, MIT |
| Target repository | **deliberately absent** — commissioned in isolation |

## Hardware and local-model capacity

2 × RTX A5000, 48.0 GiB total and **41.1 GiB usable** after a 12% runtime
reserve, no NVLink, driver 535.309.01. 2 × Xeon Gold 5220R, 96 threads, 251 GiB.
Host class `SINGLE_GPU_CONSTRAINED`.

Measured, not assumed: Qwen3.8 in bf16 needs 51.8 GiB and fits neither the VRAM
nor the root filesystem. Q4_K_M needs ~16 GiB and fits one card. Only 16 of the
model's 64 layers grow with context; the other 48 cost ~78 MB *per sequence*, so
the constraint is concurrency and not context. Throughput ~30 tokens/second.

## Model qualification

Both local models passed four trials — repeated well-formed tool calls,
schema-obeying output, refusing an unsound claim, and admitting uncertainty. The
last two are what separate a reviewer from a source of agreeable evidence.

Neither is qualified by assertion: `evidence/qualification/` holds the measured
results, and an unqualified runtime is not eligible however available it is.

## Work packages

One task of WP-001 reached `MERGE_ELIGIBLE` through the full pipeline with live
models. Fifty-two packages remain `DISCOVERED`; three are `BLOCKED` on the
acceptance chain.

No package is `ACCEPTED`, and that is not a technical gap: acceptance requires
an independent verifier identity, and the store refuses to let the actor that
produced this code accept it.

## Acceptance scenarios

Seven executed, seven passed. Twenty-nine deferred to the wave that builds their
subject and **named** rather than counted as passes.

## Pilots

- **Synthetic**, five cases: one path to `MERGE_ELIGIBLE` and four deliberate
  failures including a candidate that edits its own frozen acceptance and is
  caught before any reviewer wastes effort on it. 5/5.
- **Live**, one task of WP-001: `MERGE_ELIGIBLE` in 370.7 seconds. RED exit 2 →
  GREEN exit 0, three independent reviews, fresh-checkout verification at exit 0,
  eleven gate checks.
- **Reliability**, five repeats: **5/5 `MERGE_ELIGIBLE`**, every one reaching
  the machine gate, nothing stopped at any stage. 337–390 seconds, a narrow
  band. Each run produced a *different* candidate revision and each independently
  showed RED exit 2 → GREEN exit 0, so what repeats is the discipline rather
  than a memorised output.

## Architecture changes

Nine DUME-ADRs, each forced by evidence rather than preference. The three that
changed the plan rather than confirming it:

- **0004** — llama.cpp over vLLM, because it is the only stack with no
  driver-535 blocker and the only one that grammar-constrains the tool call
  instead of parsing it afterwards.
- **0005** — Buzz reached over its HTTP bridge, because there is no headless way
  to create a managed agent or a team and the CLI costs 10–14 GiB to reach the
  same three endpoints.
- **0009** — one run commissions one task, not one package, because a tool call
  carries the whole file as one JSON string and the budget is bounded by the
  context slot. Structural, not a model limitation.

## Residual risks

| Risk | Severity | Revisit |
|---|---|---|
| No independent verifier bound | **High** | blocks every acceptance |
| Both reviewers share a family | Medium | a third family fixes it |
| Reliability measured on one task only | Medium | repeat on a package of a different shape |
| ACP and Buzz agent lifecycle unused | Medium | WP-015/WP-016 |
| Qwen 4-bit tool-calling accuracy unmeasured | Medium | WP-009 must measure, not inherit |
| Root filesystem at 91% | Medium | before any build writing to it |
| `sha-0720f53` is a branch build | Low | mirror the image locally |

## Scope check

DUM-E is 51 modules and 239 tests. It has no workflow engine, no message broker,
no graph database, no search index, no orchestrator cluster and no scientific
lifecycle — all of which are things it exists to help build.

What it owns is the commissioning glue: turning a frozen plan into a packet,
deriving a cohort from the work rather than from an adjective, binding runtimes
without letting a reviewer share the implementer's blind spot, isolating each
task in its own worktree, sequencing three reviews that ask three different
questions, and refusing merge eligibility on eleven recorded facts.

It borrows a relay, a skill set and a serving stack, and reimplements exactly
one thing it could not borrow — the machine gate, because Superpowers ships none
and says so.

What it deliberately is not: an agent framework, a model server, an editor, or
the project it builds. The target is bound as configuration, and the harness
holds no opinion about what that target is.
