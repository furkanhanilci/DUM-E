# DUM-E Commissioning Status

**Date:** 2026-08-24
**Candidate revision:** recorded in the state store — `python3 -m dume.cli history WP-001`.
The store, not this document, is authoritative.

## What is running right now

| Component | State | Evidence |
|---|---|---|
| **Buzz relay** | **running, healthy** — `ghcr.io/block/buzz:sha-0720f53` pinned, Postgres 17 + Redis 7 + MinIO, port 3000 | `docker ps`, NIP-11 metadata, a seven-identity cohort deployed to a derived channel |
| **Superpowers** | **installed and enabled** at `b36e0829`, the exact pinned revision | `installed_plugins.json` `gitCommitSha` matches `config/upstream.lock.json` |
| **Qwen local** | profile decided ([ADR-0004](adr/ADR-0004-llama-cpp-cuda-is-the-qwen-serving-profile.md)); artefact staging | `dume runtime --probe` |
| **DUM-E harness** | 150 tests green; synthetic pilot 5/5 | `dume pilot`, `pytest -q` |
| **Obsidian mirror** | 246 notes, live-watched, graph coloured | `mirror_dume.py --check` |

## The harness, as built

| Capability | Command | What it refuses |
|---|---|---|
| Host capacity inventory | `dume inventory` | inferring a serving profile from a datasheet |
| Workspace boundary | `dume workspace --probe` | a symlink out of a read-only workspace; a missing directory counted as a passing control |
| Credential boundary | `dume secrets PATH` | a credential reaching a packet, a log, evidence or a Buzz message |
| Toolchain lock | `dume toolchain --verify` | evidence produced under an environment that has since changed |
| Upstream pins | `dume upstream` | an unreachable upstream reported as agreement |
| Work-package packet | `dume packet WP-nnn` | a summary standing in for the frozen sources |
| Cohort compilation | `dume cohort WP-nnn` | assurance derived from an adjective rather than the work |
| Runtime control | `dume runtime --probe --bind ROLE` | an unqualified or UNKNOWN runtime treated as available |
| Adversarial scenarios | `dume scenarios -v` | a deferred scenario counted as a pass |
| Synthetic pilot | `dume pilot -v` | a pipeline that has only ever succeeded |
| Discipline proof | `dume discipline --transcript …` | a `Skill` invocation standing in for a test that failed first |
| Lifecycle | `dume status`, `transition`, `evidence`, `history` | self-review, self-acceptance, stale evidence, an empty artefact |

## Lifecycle

`DISCOVERED → READY → PACKAGED → PLANNED → EXECUTING → SPEC_REVIEW → CODE_REVIEW
→ VERIFYING → TECH_COMPLETE → ACCEPTANCE_READY → ACCEPTED`, with
`FAILED → RETRY → PLANNED` for corrections.

Each review stage is gated on the previous one having passed **on the current
candidate**, so a package cannot walk the pipeline with no verdict and be caught
only at the end. Verification must be independent of both reviewers — a verifier
who already argued the code was good would be checking their own conclusion.

## Package state

WP-001 is `EXECUTING` on a recorded candidate. It is **not** TECH_COMPLETE,
because the stricter lifecycle requires three independent review verdicts on
that candidate and none exist: the actor that built the foundation is the only
actor present. WP-002/003/004 are `BLOCKED` on the WP-001 chain. The remaining
fifty are `DISCOVERED`.

**Nothing here is ACCEPTED.** That is the blocking fact, and it is not a
technical one: no independent verifier is bound.

## Measured host facts

| Fact | Value |
|---|---|
| Host class | `SINGLE_GPU_CONSTRAINED` |
| GPUs | 2 × RTX A5000, driver 535.309.01, CUDA 12.2, **no NVLink** |
| VRAM total / usable | 48.0 GiB / **41.1 GiB** |
| CPU / RAM | 2 × Xeon Gold 5220R, 96 threads / 251 GiB |
| Root filesystem | ~38 GiB free, 90% used |
| Bulk storage | `/media/otonom/DATADRIVE1` — 834 GiB, **NTFS, cannot enforce chmod** |

## Adversarial results

Seven acceptance scenarios executed, seven passed. Twenty-nine deferred and
named — never counted as passes. The synthetic pilot runs five cases: one happy
path to `MERGE_ELIGIBLE` and four deliberate failures, including a candidate
that edits its own frozen acceptance and is caught before any reviewer wastes
effort on it.

## Findings raised against this candidate

Every one came from running a control against a real deployment, not from
reading it:

1. A missing directory was reported as a holding write control → now `INCONCLUSIVE`.
2. `ACC-D024` had no control behind it — evidence accepted an artefact path without
   opening the file → zero-byte, missing and digest-mismatched artefacts refused.
3. The secret-scan report wrote a credential into evidence, quoted inside its own
   suppression reason → redaction moved to where evidence becomes a file.
4. The pipeline check ran only at `TECH_COMPLETE`, so a package could walk every
   stage with no verdict → each stage transition is now gated on the previous one.
5. Identity independence was conflated with runtime independence, which would have
   demanded one provider per role → role, agent identity and runtime separated.
6. Cohort signals were read from card boilerplate every package shares, making all
   54 look identical → signals narrowed to package-specific sections.
7. Two implementers collapsed into one Buzz identity → one identity per role slot.
8. **The bulk filesystem is NTFS and silently discards `chmod`** → secrets moved to
   ext4 ([ADR-0007](adr/ADR-0007-secrets-need-a-filesystem-that-enforces-permissions.md)).
9. **The credential scanner reported a directory holding a private-key vault and five
   live passwords as clean** — it anchored on a word boundary that `POSTGRES_PASSWORD`
   does not have → rule rewritten, seventeen regression cases both directions.

## Residual risks

| Risk | Severity | Revisit when |
|---|---|---|
| No independent verifier bound | **High** | blocks every acceptance — the next real decision |
| `cargo`/`rustc` absent; `buzz` CLI and `buzz-acp` unbuilt | Medium | before a real ACP runtime is needed (~10–14 GiB) |
| Root filesystem at 90% | Medium | before any build that writes to it |
| Qwen 4-bit tool-calling accuracy unmeasured | Medium | WP-009 must measure, not inherit |
| `sha-0720f53` is a branch build, not a release | Low | mirror the image locally |
| Buzz `switch_model` ahead of its own spec | Low | treat runtime switching as unstable |
| AETHRION's graph generator removes DUM-E colours | Low | re-run `mirror_dume.py` |

## What an independent verifier needs

Nothing from the producer's conversation:

```bash
python3 -m dume.cli status
python3 -m dume.cli history WP-001      # the authoritative candidate
python3 -m dume.cli upstream            # re-check every pin
python3 -m dume.cli scenarios -v        # re-run the attacks
python3 -m dume.cli pilot -v            # re-run the end-to-end pilot
uv venv .venv && uv pip install pytest coincurve && .venv/bin/python -m pytest -q
```

Then, as an identity that is **not** the producer, record a verdict for each of
the three stages. The store refuses anything else.

## Next

WP-005 is decided in ADR-0004 and needs only the artefact staged and the server
started. It remains gated on the WP-001 acceptance chain, which is where a human
is needed.
