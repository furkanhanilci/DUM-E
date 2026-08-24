# DUME-ADR-0006 — The harness supplies the proof Superpowers does not

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-019, WP-020, WP-021, WP-022, WP-036–WP-040
- **Reuse class:** `DEPENDENCY`

## What is installed

Superpowers 6.3.0 at `b36e0829c6d0140e93cfef2ca599b1b07d4a7797` — the exact
revision this programme pinned. Confirmed by
`~/.claude/plugins/installed_plugins.json` recording that `gitCommitSha`, and by
comparing it against `config/upstream.lock.json`. Fourteen skills, one
`SessionStart` hook, no compiled code.

A convenient fact: the official Claude marketplace already pins Superpowers to
this same commit, so the official install *is* the pinned install. No fork, no
vendoring.

## The finding

**Superpowers ships no machine gate, and says so.** `finishing-a-development-branch`
runs the local suite and then presents a human menu; there is no CI contract, no
exit-code gate, and — more fundamentally — **no stage transition in the whole
system is enforced by code.** Every gate is prose the model may skip.

| Stage | Skill | Enforcement |
|---|---|---|
| DESIGN | `brainstorming` | prose |
| PLAN | `writing-plans` | prose |
| WORKTREE | `using-git-worktrees` | prose |
| RED → GREEN → REFACTOR | `test-driven-development` | prose |
| LOCAL VERIFY | `verification-before-completion` | prose |
| SPEC REVIEW | `brainstorming` self-review + reviewer prompt | prose |
| CODE REVIEW | `requesting-code-review`, `receiving-code-review` | prose |
| FRESH VERIFY | `subagent-driven-development` | prose |
| **MACHINE GATE** | **none** | **— the gap** |

This is not a criticism of Superpowers. It is a skill system, and skills advise.
It does mean the harness cannot delegate assurance to it.

## Decision

DUM-E adopts Superpowers as the engineering methodology and supplies two things
it does not:

1. **The machine gate** (`dume/acceptance/gate.py`) — eleven checks over
   recorded facts, no model anywhere in it, with a test asserting it never
   reaches for one.
2. **The discipline verifier** (`dume/review/discipline.py`) — which reads the
   signals a session actually leaves behind and reports them in three separate
   classes, because conflating them is how "I invoked test-driven-development"
   comes to stand in for "the test failed before it passed":

   - **invocation** — a `Skill` tool call in the transcript, a `hook_response`
     carrying the bootstrap. Proves the skill was *entered*.
   - **artefact** — a design document with real content, a plan, a parseable
     ledger line. Proves something was *produced*.
   - **independent** — a test-only commit preceding the implementation, an exit
     code from a fresh checkout. Proves the work is *real*.

   Only the third class can support a verdict. The report says so explicitly,
   and when no independent signal is available it returns
   `INVOKED_BUT_UNPROVEN` rather than a pass.

## What the bootstrap actually does

`hooks/hooks.json` matches `startup|clear|compact` and injects the whole
`using-superpowers` skill as `additionalContext`. The `compact` matcher is the
part that matters for long commissioning runs: the discipline is re-injected
after a compaction rather than quietly lost. A durable ledger under
`.superpowers/sdd/<plan>/progress.md` carries machine-parseable lines
(`Task N: complete (commits b7..h7, review clean)`), which is what a harness
should read after a restart instead of trusting recollection.

Codex ships the same skills with `"hooks": {}` — **no session bootstrap**. On
that runtime the harness must inject `using-superpowers` itself, or the
discipline is present as files and absent as behaviour. Hermes has a
`pre_llm_call` hook but no post-compaction one.

## Consequence for evidence

A `Skill` invocation is never accepted as evidence of correctness anywhere in
DUM-E. The gate does not read the discipline report at all; it reads test exit
codes, digests and independent verdicts. The discipline report exists to answer
a different question — *was the method followed* — and it is allowed to say "I
cannot tell".

## Operational notes

- The SDD workspace is `rm -rf`'d on a clean finish, and its `.gitignore` is
  `*`, so its rulings never reach git. **Snapshot the ledger before finishing**
  or the trail is gone.
- `git clean -fdx` destroys the workspace. Documented upstream.
- The marketplace `sha` moves on the next release. Re-check it, or pin hard by
  installing from a local clone with `--plugin-dir`.
- `SUPERPOWERS_DISABLE_TELEMETRY=1` stops the brainstorming skill's optional
  companion fetching a remote asset.
