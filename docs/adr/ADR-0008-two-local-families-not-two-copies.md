# DUME-ADR-0008 — Two local model families, not two copies of one

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-005, WP-016, WP-023, WP-025, WP-027, WP-051
- **Extends:** [ADR-0004](ADR-0004-llama-cpp-cuda-is-the-qwen-serving-profile.md)

## The problem a working harness ran into

Qwen3.8-27B passed every qualification trial — five of five well-formed tool
calls, schema-obeying output, refused an unsound claim, admitted uncertainty —
and was recorded as qualified for all six roles.

It still could not commission a single work package. Bound as implementer,
nothing could then review it: a reviewer from the implementer's own model family
shares its blind spot, so its PASS is not independent evidence. The harness
returned `BLOCKED_RUNTIME`, named every runtime and why, and did not lower the
required assurance to get moving.

The available answers were:

1. **Spend an external quota** on Claude or GPT for the reviewer roles. Both
   CLIs are installed. But the operator's stated reason for wanting a local
   model in the first place is that those quotas run out mid-package, and making
   every review depend on the scarcest resource in the system inverts that.
2. **Relax family independence** to identity independence everywhere. Cheap, and
   wrong: two instances of one model are one opinion sampled twice.
3. **Serve a second local model of a different family.** Costs a GPU that was
   otherwise idle and about fourteen gigabytes of disk.

## Decision

Three. `Mistral-Small-3.2-24B-Instruct-2506` Q4_K_M on GPU 1, port 8001,
alongside Qwen on GPU 0.

A **different family**, not a second Qwen. That distinction is the whole point:
a second copy of the same weights would buy availability and no independence at
all, and availability was never what was missing.

| | GPU 0 | GPU 1 |
|---|---|---|
| Model | Qwen3.8-27B Q4_K_M | Mistral-Small-3.2-24B Q4_K_M |
| Family | `qwen` | `mistral` |
| VRAM | ~18 GiB of 24.5 | ~20 GiB of 24.5 |
| Cost tier | 1 — the bulk implementer | 2 — the reviewer |
| Qualified for | all six roles | all six roles |

Cost tier is how the model strategy's preference is expressed, since both are
free to run: the implementer takes the cheaper tier, and the reviewers are then
forced onto the other family by the independence rule rather than by a
hard-coded assignment. A cohort now binds:

```
architect       qwen-local     (qwen)
implementer     qwen-local     (qwen)
spec_reviewer   mistral-local  (mistral)
code_reviewer   mistral-local  (mistral)
verifier        mistral-local  (mistral)
```

## The orchestrator has no runtime

Compiling a cohort refused at `commissioning_orchestrator`, because no runtime
was qualified for it. The right fix was not to qualify one.

The orchestrator moves work between stages and decides nothing about whether a
stage passed. That is the harness's own Python. Giving it a model would add a
voice with no vote and a quota bill, so `Role` now carries `needs_runtime`, and
it and the human commander are both `False`.

## A defect the measurement had

Mistral initially failed the `structured_output` trial. Its answer was correct
and well argued; it was wrapped in a ` ```json ` fence, and the trial parsed with
a bare `json.loads` while the harness's own client already strips fences.

The trial was measuring more strictly than the system operates, which
manufactures a failure. A capable reviewer would have been disqualified for a
formatting habit that never reaches the harness. Qualification now parses
exactly as the runtime path parses.

The general form is worth keeping: **a qualification test that is stricter than
production produces false negatives, and false negatives here cost independence
— the expensive thing.**

## Consequences

- No external quota is spent to review local work. Claude and Codex stay for
  what they are actually better at, and `RESERVE` keeps them there.
- Both GPUs are in use; the second was idle.
- Cross-family independence is available for every package without a network
  call, so `BLOCKED_RUNTIME` now means something is genuinely wrong rather than
  something is merely absent.
- A third family would allow spec review, code review and verification to be
  three families rather than two. Not required — identity independence already
  separates them and only the implementer's family is a correlated-failure risk
  — but it is the next cheap improvement if a GPU frees up.

## Residual risk

Both reviewers are the same family. If Mistral has a systematic blind spot, spec
review and code review share it. They remain distinct identities with distinct
contexts and an embargo between them, which is what the design requires; a third
family would make it stronger. Recorded rather than resolved.
