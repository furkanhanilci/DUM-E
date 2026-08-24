# DUME-ADR-0004 — llama.cpp CUDA in Docker is the Qwen serving profile

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-005 (deployment profile decision), WP-007, WP-008, WP-009
- **Supersedes:** the ranking in [ADR-0002](ADR-0002-quantised-serving-is-a-first-class-candidate.md); its conclusion stands, its arithmetic is corrected below

## What the model actually is

Verified from the published `config.json`, not assumed:

| Fact | Value |
|---|---|
| Architecture | `Qwen3_5ForConditionalGeneration` — Qwen3.8 reuses the Qwen3.5 architecture |
| Modality | multimodal (vision tower, depth 27) |
| Layers | 64: **48 Gated-DeltaNet + 16 full-attention** (+1 MTP block) |
| Full-attention shape | GQA 24/4, `head_dim` 256 |
| Native context | 262 144 (1M via YaRN) |
| Licence | Apache-2.0 |

Three numbers in ADR-0002 were wrong and are corrected: bf16 is **51.8 GiB**, not 50.3; Q4_K_M is **15.9–16.5 GiB**, not 15.1; and the model is multimodal, which ADR-0002 did not mention at all.

## The arithmetic that changes the decision

Only the 16 full-attention layers grow with context:

`16 layers × 4 KV heads × 256 head_dim × 2 (K+V) × 2 bytes = 64 KiB per token`

| Context | KV bf16 | KV q8_0 |
|---|---|---|
| 32 K | 2.0 GiB | 1.0 GiB |
| 128 K | 8.0 GiB | 4.0 GiB |
| 262 144 | 16.0 GiB | 8.0 GiB |

The 48 Gated-DeltaNet layers cost **~78 MB per sequence, independent of length**.

So the constraint on this host is **concurrency, not context**. That inverts the
intuition the plan was written with: `--max-num-seqs` / `-np` is the knob to
cap, not `-c`. vLLM's default of 256 concurrent sequences would reserve roughly
19 GiB of Gated-DeltaNet state before a single token of KV cache.

## Decision

**llama.cpp CUDA via `ghcr.io/ggml-org/llama.cpp:server-cuda`, Q4_K_M, single
GPU.** Artefact `unsloth/Qwen3.8-27B-GGUF` Q4_K_M — 16.5 GB on disk, ~16 GiB
VRAM, which fits one A5000 with room to spare.

Two reasons, in order of weight:

1. **It is the only stack that grammar-constrains the tool call.** Qwen emits
   XML (`<tool_call><function=name><parameter=p>…`), and vLLM and SGLang parse
   that *after the fact* from unconstrained output. llama.cpp constrains
   generation with a PEG/GBNF grammar that names Qwen3.5 explicitly, so
   malformed tool syntax is structurally impossible rather than merely unlikely.
   A harness whose agents must call tools reliably should prefer a structural
   guarantee to a parser, and this outranks throughput.
2. **It has zero blockers on this host's driver.** The image is CUDA 12.8.1 and
   its `NVIDIA_REQUIRE_CUDA` explicitly admits `driver>=535,driver<536` — which
   is exactly the installed 535.309.01. `nvidia-container-toolkit` 1.19.1 is
   already present. No `nvcc`, no `cmake`, no driver change.

## What was rejected, and why it was not a close call

**vLLM 0.27.1** would be faster — a measured sm_86 reference reports 85–95 tok/s
for AWQ-INT4 on 2×3090 without NVLink. It is rejected *for now* on three
verified blockers:

- `pip install vllm` **fails on driver 535**. vLLM ≥0.20 defaults to CUDA 13.0,
  which requires driver ≥580. The `+cu129` GitHub release asset works but is not
  on PyPI, and that fragility class is real.
- A **CUDA-graph hang on exactly this GPU**: vLLM issue #52682 reports
  Qwen3.8-27B hanging at graph capture on an RTX A5000. `--enforce-eager` avoids
  it, at a throughput cost.
- **FP8 W8A8 is not native on sm_86.** Ampere falls back to a Marlin
  weight-only kernel, so the FP8 artefact is a memory win only, not a compute one.

**SGLang 0.5.18** is rejected on weaker grounds and could be revisited: it works
in principle, but has no Ampere validation for this model and both Qwen
tool-parser fix PRs (#26172, #26793) are still unmerged.

## Operating context

**65 536 tokens**, not 262 144. Not because the arithmetic forbids more — 262 K
of bf16 KV is only 16 GiB — but because llama.cpp has a measured decode cliff
past roughly 80 K (issue #27623: 33 → 1.4 tok/s). Choosing the native ceiling
because the model advertises it is the exact failure mode WP-001 names.

## Tensor parallelism across the two cards

**Not worth it.** Q4_K_M fits one card, vLLM's own documentation prefers
pipeline over tensor parallelism without NVLink, llama.cpp's `-sm row` is dead
on CUDA, and `-sm tensor` has three open crash reports on this architecture.
Run a second independent instance on GPU 1 instead — which also gives the cohort
two genuinely separate local endpoints.

## Two things that must be done before first serve

1. **Patch the chat template.** It carries five `raise_exception` asserts and
   returns HTTP 400 before emitting a token (issue #27107).
2. **Keep `-ctk` and `-ctv` the same quantisation type.** A mismatch silently
   disables flash attention and collapses prefill by roughly 20× (issue #27109).

## Residual risks

| Risk | Note |
|---|---|
| Issue #27367 — mid-conversation system message returns 500 | **Open**, and directly relevant to an agent harness |
| `tool_choice: required` accepted but not enforced (#27217) | The harness must verify a tool was called, not assume |
| 4-bit degradation of tool-calling accuracy | **No published data either way.** WP-009 must measure it rather than inherit an assumption |
| llama.cpp on CUDA 12.2 specifically | CI's floor is 12.6.2; the Docker path sidesteps this, a source build would not |

## The change that would reopen this decision

`nvidia-driver-580` (through 595) is installable from the already-configured
NVIDIA repository. One `apt install` plus a reboot clears the CUDA-13 wheel
blocker that currently rules out both vLLM and SGLang, and would make the
throughput comparison worth re-running. That is a host-wide change with its own
risk, and it is a human decision, not one this ADR takes.
