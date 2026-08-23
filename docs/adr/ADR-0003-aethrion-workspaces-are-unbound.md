# DUME-ADR-0003 — AETHRION workspaces are declared but unbound

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-002, `config/dume.config.json`, the upstream lock

## Context

DUM-E's purpose is to build AETHRION, so the pack's workspace layout binds
`AETHRION_SPEC` read-only and `AETHRION_TARGET` writable from the start. The
commissioning decision for this bring-up was that DUM-E is to be brought into
service **in isolation**: no AETHRION clone is created, read, pinned or
modified.

An integration that cannot be built without touching the thing it integrates
with has not been proven to be separable.

## Decision

`AETHRION_SPEC` and `AETHRION_TARGET` exist as configuration slots with their
modes fixed (`READ_ONLY` and `READ_WRITE`) and `bound: false`. `MODEL_CACHE` is
likewise declared and unbound. AETHRION is absent from the upstream lock.

The boundary mechanism is verified against a synthetic three-workspace fixture
that reproduces the same shape — a read-only specification, a writable target
and append-only evidence — so the control is tested without the real repository
being present.

## Consequences

- An unbound slot grants nothing. `Boundary.check_write` refuses every path
  outside a *bound* workspace, so today the harness cannot write to AETHRION
  even by accident: the path is outside every bound root.
- A work package that genuinely needs the specification is `BLOCKED` on a human
  binding it, which is the correct state rather than an improvised default.
- Binding is a deliberate, human, recorded act. When it happens, WP-002's
  read-only mount must be verified with `dume workspace --probe`, because
  `mode: READ_ONLY` in configuration is DUM-E's own rule, and a real
  specification mount should also be one the kernel refuses to write.

## Evidence

`tests/test_workspace_boundary.py` — eleven behavioural tests including symlink
escape, `../` traversal, nested-workspace precedence and an actual write probe
against a chmod-500 directory.
