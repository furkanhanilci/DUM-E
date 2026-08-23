# DUME-ADR-0002 — Quantised serving is a first-class candidate, not a fallback

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-005, WP-007, WP-008; the upstream lock entry for `llama.cpp`
- **Supersedes:** the pack's framing of WP-008 as "fallback if hardware requires it"

## Context

The pack orders the Qwen serving decision as vLLM/SGLang first, with
llama.cpp/GGUF as a verified fallback "if hardware requires it". WP-001 was run
on the actual commissioning host before that ordering was inherited.

Measured, not assumed:

| Fact | Value |
|---|---|
| GPUs | 2 × NVIDIA RTX A5000 |
| VRAM total | 48.0 GiB |
| VRAM free at probe time | 46.7 GiB |
| Usable for weights after a 12% runtime reserve | **41.1 GiB** |
| Interconnect | PCIe/NODE — **no NVLink** |
| 27B in bf16 | 50.3 GiB — **does not fit** |
| 27B at q4_k_m | 15.1 GiB — fits with headroom |
| Root filesystem free | 39 GiB — cannot even *store* bf16 weights |

The second GPU also drives a display, which is exactly the "total VRAM is not
usable VRAM" failure mode WP-001 names.

## Decision

For this host, quantised serving is a first-class candidate evaluated on equal
terms with vLLM/SGLang, not a consolation prize. The upstream lock records
`llama.cpp` with that status and the reason.

This does **not** decide the winner. WP-005 still chooses by measured end-to-end
behaviour — tool-calling fidelity and structured-output reliability under
WP-009 matter more than tokens per second, and a quantisation that degrades
tool calling is not a cheaper worker, it is a different and worse one.

## Consequences

- No serving stack is installed before WP-005 records a host-compatible profile.
- The 262144-token native ceiling is not a target. Operating context is chosen
  from measured KV-cache behaviour inside the 41.1 GiB envelope.
- Weight artefacts must not land on the root filesystem. `MODEL_CACHE` is
  configured to `/media/otonom/DATADRIVE1/dume-model-cache` (834 GiB free) and
  left **unbound** until WP-006 pins a licence and digest.
- If quantisation fails WP-009, the honest outcomes are a smaller model or
  `BLOCKED_RUNTIME` — never a reduction in required assurance (Invariant 13).

## Evidence

`evidence/WP-001/host_inventory.json`, produced by `dume inventory` on the
commissioning host.
