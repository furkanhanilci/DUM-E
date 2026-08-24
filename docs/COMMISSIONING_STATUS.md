# DUM-E Commissioning Status

**Date:** 2026-08-24
**Candidate:** recorded in the state store — `python3 -m dume.cli history WP-001`.
The store, not this sentence, is authoritative.

## Running now

| | Where | State |
|---|---|---|
| **Buzz relay** | `127.0.0.1:3000` | healthy — `ghcr.io/block/buzz:sha-0720f53` pinned, + Postgres 17, Redis, MinIO |
| **Qwen3.8-27B** | `127.0.0.1:8000/v1` | healthy — GPU 0, Q4_K_M, ~18 GiB, qualified for six roles |
| **Mistral-Small-3.2-24B** | `127.0.0.1:8001/v1` | healthy — GPU 1, a *different family*, qualified for six roles |
| **Superpowers 6.3.0** | `~/.claude/plugins/` | installed at the pinned `b36e0829`, injected into every agent prompt |
| **Telegram** | `@dume_autonomous_bot` | polling; operator authorised at `DANGEROUS_ACTION` |
| **Operator view** | `127.0.0.1:8080` | read-only, refreshes every 10s |
| **Obsidian mirror** | `10 - Projects/DUM-E` | 248 notes, 11 colour groups, live-watched |

## What a run actually does

```
READY → packet → cohort → runtime binding → worktree → plan
      → RED → GREEN → spec review → code review → fresh verification → gate
```

Bound automatically, and the assignment falls out of the independence rule
rather than being written down anywhere:

| role | runtime | family |
|---|---|---|
| architect, implementer | qwen-local | qwen |
| spec_reviewer, code_reviewer, verifier | mistral-local | mistral |

The orchestrator and the human commander have no runtime: the first is the
harness's own sequencing and decides nothing about whether a stage passed, and
the second is a person.

## What has been demonstrated with live models

- A complete **red-then-green cycle**: a six-case test written first, pytest
  exit 2 observed, implementation written, exit 0 observed — seven tool calls.
- **Independent refusal**: Mistral, shown only WP-001's frozen specification and
  the diff, refused a candidate and named all five missing deliverables. It was
  right, and it had not been told what to look for.
- **Buzz narration**: every stage transition posted to the package's derived
  channel, verdicts `@`-addressing the role that gave them.
- **Runtime switching**: role preserved, runtime rebound, task state carried,
  conversation dropped.

- **Repeatably.** Five consecutive live runs, five `MERGE_ELIGIBLE`, all
  reaching the machine gate, nothing stopped at any stage, 337–390 seconds.
  Each produced a different candidate revision and each showed RED exit 2 →
  GREEN exit 0 independently, so what repeats is the discipline and not a
  memorised answer.

## What has not

No package is `ACCEPTED`. That is not a technical gap: acceptance needs an
independent verifier identity, and the store refuses to let the actor that
produced this code accept it.

## Nine decisions, and what forced each

| ADR | Forced by |
|---|---|
| 0001 foundation has no third-party dependency | a harness that verifies an environment cannot need that environment first |
| 0002 quantised serving is first-class | 41.1 GiB usable VRAM measured, not assumed |
| 0003 AETHRIONIS workspaces unbound | commissioning DUM-E in isolation |
| 0004 llama.cpp CUDA is the profile | only stack with no driver-535 blocker, only one that grammar-constrains the tool call |
| 0005 Buzz over the HTTP bridge | no headless agent/team API exists; the CLI costs 10–14 GiB to reach the same three endpoints |
| 0006 the harness supplies the proof | Superpowers enforces nothing and says so |
| 0007 secrets need a filesystem that enforces | the data drive is NTFS and silently discards `chmod` |
| 0008 two local families, not two copies | one family cannot review its own work |
| 0009 one run is one task | a package does not fit one agent turn, and the budget is not the constraint |

## Findings, all from running rather than reading

1. A missing directory reported as a holding control → now `INCONCLUSIVE`.
2. `ACC-D024` had no control behind it → empty, missing and mismatched
   artefacts refused.
3. The secret-scan report wrote a credential into evidence → redaction moved to
   where evidence becomes a file.
4. The pipeline check ran only at the end → each stage now gated on the previous.
5. Identity independence conflated with runtime independence → role, agent and
   runtime separated.
6. Cohort signals read from shared boilerplate → all 54 packages looked identical.
7. Two implementers collapsed into one Buzz identity → one identity per slot.
8. The bulk filesystem discards `chmod` → secrets moved to ext4.
9. **The credential scanner reported a directory holding a key vault and five
   live passwords as clean** — it anchored on a word boundary `POSTGRES_PASSWORD`
   does not have.
10. The GPU check read a list instead of probing → reported a working host as blocked.
11. The template scan used a 4 MiB window → 11 guards sat just past it.
12. `red` defined twice wrongly — as any non-zero exit (accepts an empty suite)
    and as exactly 1 (rejects the collection error a real cycle produces).
13. A truncated tool call reported as a parse error → named, and reclassified
    from implementation failure to runtime failure.
14. **An unrestricted reasoning phase spent the response budget deliberating**
    and the tool call never arrived → thinking budgeted and moved out of content.
15. The mirror watcher triggered on its own read of the state store.

## Residual risks

| Risk | Severity | Revisit |
|---|---|---|
| No independent verifier bound | **High** | blocks every acceptance |
| Reliability measured on one task only | Medium | repeat on a package of a different shape |
| Both reviewers share a family | Medium | a third family would fix it |
| ACP and Buzz agent lifecycle unused | Medium | WP-015/WP-016 |
| Qwen 4-bit tool-calling accuracy unmeasured | Medium | WP-009 must measure it |
| Root filesystem at 91% | Medium | before any build writing to it |

## For an independent verifier

```bash
python3 -m dume.cli status
python3 -m dume.cli history WP-001
python3 -m dume.cli upstream
python3 -m dume.cli skills
python3 -m dume.cli scenarios -v
python3 -m dume.cli pilot -v
uv venv .venv && uv pip install pytest coincurve && .venv/bin/python -m pytest -q
```

Then, as an identity that is **not** the producer, record a verdict for each of
the three stages. The store refuses anything else.
