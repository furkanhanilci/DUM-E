# DUME-ADR-0009 — One live run is one task, not one work package

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-030 (Task Compiler), WP-036, and every live run
- **Evidence:** four live runs against WP-001, described below

## What happened

WP-001 asks for five mandatory deliverables. A live run was pointed at all five,
and the implementer — Qwen3.8-27B in a bounded tool loop — behaved like this:

| Run | Outcome |
|---|---|
| focus was one function (`usable_bytes`) | wrote a six-case test, observed pytest exit 2, wrote the implementation, observed exit 0. A complete red-then-green cycle in seven tool calls. |
| focus was all five deliverables | wrote the test file, then spent the whole response budget attempting the implementation in a single `write_file` and truncated |

The second failure is not a model that cannot code. It is a unit of work that
does not fit a single agent turn: a tool call carries the whole file as one JSON
string, and a module implementing `collect`, `classify`, `usable_vram_bytes` and
`write_deliverables` does not fit in a response budget that also has to hold the
model's reasoning.

Raising the budget did not fix it and could not: the budget is bounded by the
context slot, the slot is bounded by the model's context divided by concurrency,
and a large enough file will always exceed whatever is left. The constraint is
structural.

## Decision

**A live run commissions one task. A work package is finished by several.**

This is not a concession — it is what a Task Compiler is for, and the plan
already contains one (WP-030). The compiler's job is to turn a package into
units a bound runtime can actually complete, and until now DUM-E has been
handing it whole packages and calling the result a run.

Consequences for how a run is reported:

- A run's result names the **task** it commissioned, not the package, and says
  which of the package's deliverables that task covers.
- A package reaches `TECH_COMPLETE` when its tasks do, not when one run ends.
- The specification reviewer keeps judging the candidate against the **whole**
  frozen specification. It refused a one-function slice presented against
  WP-001's five deliverables and it was right to; the fix is to stop presenting
  a task as a package, not to narrow what the reviewer is shown.

## What a right-sized task looks like

Measured on this host, with Qwen3.8-27B Q4_K_M at ~30 tokens/second, a 4000
token response budget and a 32768 token slot:

- **fits comfortably** — one module of up to roughly 150 lines plus its test
  file, written across two or three `write_file` calls
- **does not fit** — a module with four public functions and file-writing side
  effects, attempted in one call

So the compiler's unit is closer to "one function and its tests" than to "one
deliverable", and a five-deliverable package is five to ten tasks.

## What this does not change

Nothing about assurance. Each task still passes through the same three reviews
by the same independent identities, and the machine gate still evaluates the
same eleven checks. More tasks means more evidence, not less.

## Residual risk

A package split into tasks can be salami-sliced past a reviewer: each task looks
reasonable and the whole misses the requirement. The specification reviewer
seeing the whole frozen specification is what catches that, and it is why the
reviewer's context must not be narrowed to the task even when the build is.
