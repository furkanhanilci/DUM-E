# DUME-ADR-0003 — The specification and target workspaces are declared but unbound

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-002, `config/dume.config.json`, the upstream lock

## Context

DUM-E's purpose is to build *something else*, so the natural workspace layout
binds a specification read-only and a target writable from the start. The
commissioning decision for this bring-up was that DUM-E is brought into service
**in isolation**: it is commissioned, tested and proven with no target repository
present at all.

An integration that cannot be built without touching the thing it integrates
with has not been proven to be separable.

## Decision

`SPEC_MOUNT` and `BUILD_TARGET` exist as configuration slots with their modes
fixed (`READ_ONLY` and `READ_WRITE`), no path, and `bound: false`. Nothing about
a target appears in the upstream lock — a target is not an upstream of the
harness that builds it.

The boundary mechanism is verified against a synthetic three-workspace fixture
that reproduces the same shape — a read-only specification, a writable target
and append-only evidence — so the control is tested without any real repository
being present.

## Consequences

- **An unbound slot grants nothing.** `Boundary.check_write` refuses every path
  outside a *bound* workspace, so a fresh clone cannot write anywhere it was not
  explicitly told to: the path is outside every bound root.
- A work package that genuinely needs the specification is `BLOCKED` on a human
  binding it, which is the correct state rather than an improvised default.
- Binding is a deliberate, human, **recorded** act — `bind_workspace` sits at
  `DANGEROUS_ACTION` in the command gateway. When it happens, WP-002's read-only
  mount must be verified with `dume workspace --probe`, because `mode:
  READ_ONLY` in configuration is DUM-E's own rule, and a real specification
  mount should also be one **the kernel** refuses to write.
- The harness holds no opinion about what the target is. That is what makes it
  reusable rather than a fixture of one project.

## Evidence

`tests/test_workspace_boundary.py` — eleven behavioural tests including symlink
escape, `../` traversal, nested-workspace precedence and an actual write probe
against a chmod-500 directory.
