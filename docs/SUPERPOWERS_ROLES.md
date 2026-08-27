# How the engineering discipline reaches the agents

Superpowers is a set of engineering-process skills — test-driven development,
systematic debugging, verification before completion, and so on. It ships as a
plugin for Claude Code, Codex and Hermes.

That is a problem this document exists to solve. **DUM-E's agents are not those
harnesses.** They are raw OpenAI-compatible endpoints behind llama.cpp or a
provider API. A plugin that shapes the *harness author's* session never reaches
Qwen or Mistral. Installing Superpowers and then reporting that the agents work
under it would be a claim with nothing behind it.

`dume/review/skills.py` closes that gap: it reads the actual `SKILL.md` files at
a pinned revision and projects a bundle into each role's system prompt.

## Two things it deliberately does not do

**It does not summarise a skill.** A skill is an instruction; a summary of an
instruction is a *different* instruction. The primary skill for a role goes in
whole, byte for byte.

**It does not claim the skill was obeyed.** Injection is an input. Whether the
agent followed it is answered somewhere else entirely — by the red-then-green
exit codes, by the independent reviews, and by `dume discipline`, which keeps
invocation, artefact and independent evidence in **separate columns** so that an
agent saying "I used TDD" can never be counted as evidence that it did
([ADR-0006](adr/ADR-0006-the-harness-supplies-the-proof-superpowers-does-not.md)).

## Vendored, not read from the plugin cache

The skills live in `vendor/superpowers/`, committed to this repository.

```
vendor/superpowers/
  REVISION            b36e0829c6d0140e93cfef2ca599b1b07d4a7797
  SKILLS.lock.json    a sha256 per skill file
  skills/             14 skills, as their upstream SKILL.md
  LICENSE             MIT
```

The plugin cache under `~/.claude/plugins/` is **not ours**: it is upgraded,
garbage-collected and rewritten by a tool with its own release schedule. A run
whose discipline changed underneath it produces evidence describing a version
nobody recorded. So the vendored copy is what every run reads, and the cache is
consulted only to notice that upstream has moved on.

Two independent checks guard it:

- **`drift()`** — vendored bytes against `SKILLS.lock.json`. A skill that changed
  without the lock changing is either an unrecorded edit or a corrupt file;
  either way the run must not proceed on it.
- **`dume upstream`** — the pinned revision against what upstream now serves.

If the vendored copy cannot be read, `SkillsUnavailable` is raised rather than
falling back to harness-authored prose. An agent running without the discipline
it is supposed to have is a fact the run must **record**, not paper over.

## What each role gets

One skill goes in whole (marked `*`); the rest go in as their own frontmatter
description plus overview. A role needs one discipline in full and awareness of
the others.

| Role | Primary (injected whole) | Secondary (description + overview) | Bundle |
|---|---|---|---|
| `commissioning_orchestrator` | `writing-plans` | `dispatching-parallel-agents`, `executing-plans` | 8 699 ch |
| `architect` | `brainstorming` | `writing-plans`, `writing-skills` | 11 689 ch |
| `implementer` | `test-driven-development` | `systematic-debugging`, `verification-before-completion`, `using-git-worktrees` | 10 622 ch |
| `spec_reviewer` | `verification-before-completion` | `brainstorming`, `receiving-code-review` | 5 545 ch |
| `code_reviewer` | `requesting-code-review` | `receiving-code-review`, `systematic-debugging` | 3 927 ch |
| `verifier` | `verification-before-completion` | `systematic-debugging`, `using-git-worktrees` | 4 619 ch |
| `specialist` | `systematic-debugging` | `test-driven-development` | 9 758 ch |

`human_commander` gets none: it is a person, not a prompt.

```bash
python3 -m dume.cli skills      # the table above, live, plus lock/upstream agreement
```

### The budget is a control, not a convenience

`PRIMARY_BUDGET = 9000`, `SECONDARY_BUDGET = 1200` characters. A skill that
crowds the frozen packet out of the context window has replaced *the requirement*
with *advice about how to meet it* — and the agent will then satisfy the advice.
When a bundle is truncated, the `InjectedSkill` record says `truncated: true`, so
the run's evidence never overstates what the agent actually saw.

## Why the bundles differ

The assignments are not decorative. Each role gets the discipline covering the
one thing that can go wrong in a way **nothing downstream will catch**:

- The **orchestrator** plans and dispatches. A bad plan or a bad fan-out is not
  caught by a reviewer looking at code.
- The **implementer** gets TDD as primary because the red-then-green exit codes
  are the machine-checkable trace of it. This is the only role whose discipline
  leaves a mechanically verifiable artefact.
- The **spec reviewer** and **verifier** both get `verification-before-completion`
  — the two roles whose whole job is refusing to accept a claim without evidence.
- The **code reviewer** gets `requesting-code-review`: it has to *ask the right
  questions*, and the skill that describes how to request a review is the one
  that names them.

## What is recorded per run

`evidence/<WP>/skills_injected.json` — for each role: skill name, whether primary,
its path, its sha256, character count, and whether it was truncated. Plus the
revision the bundle was built at.

That record is what makes the discipline a *versioned, pinned artefact with a
digest* rather than prose the harness author invented and can drift without
noticing.

## Roles are not agents

Worth restating, because the mapping is where this design earns its keep:

```
role  ──bound to──▶  runtime  ──served by──▶  model  ──from──▶  provider account
```

A role is not an agent, not a persona, not a runtime, not a model and not a
provider account. All of those can change while the role stays the same — which
is exactly what lets a runtime be swapped mid-task without the authority
attached to the work moving with it. `dume/runtimes/handoff.py` is where that
happens: the task crosses, the conversation does not.

The independence requirements attach to the **role**, and the cohort compiler
refuses to bind a cohort that cannot satisfy them:

- `implementer` must not share an *identity* with `spec_reviewer`,
  `code_reviewer` or `verifier` — one instance answering two questions is one
  opinion wearing two hats.
- `spec_reviewer`, `code_reviewer` and `verifier` must not share a *model family*
  with `implementer` — two agents of the same family fail the same way, so a
  check by the implementer's own family is not independent evidence, it is the
  same blind spot twice.
- `verifier` additionally requires a **fresh context**, and is the only role
  whose PASS is evidence of behaviour.

Family independence is not demanded everywhere, and that is deliberate:
demanding it of every role would require as many providers as roles. It is
demanded where it buys something.

```bash
python3 -m dume.cli cohort WP-nnn      # compile a cohort and see the requirements resolved
python3 -m dume.cli runtime --probe    # which runtimes are available, and which are eligible
```

## Refreshing the pin

Superpowers moving is a **change-controlled event**, not a background upgrade:

1. `dume upstream` — see that the pinned revision and upstream have diverged.
2. Copy the new `SKILL.md` files into `vendor/superpowers/skills/`.
3. Regenerate `SKILLS.lock.json` and `REVISION`.
4. Run the suite. A skill's wording changing is a change to what every agent is
   held to, and it lands in a commit that says so.

Never point the harness at the plugin cache to get a newer version. The cache is
upgraded out from under a run.
