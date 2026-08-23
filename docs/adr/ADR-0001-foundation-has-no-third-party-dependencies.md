# DUME-ADR-0001 — The foundation layer carries no third-party dependency

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-001 – WP-004, `dume.config`, `dume.state`
- **Supersedes:** nothing

## Context

The pack's schema examples are written in YAML, and its configuration examples
assume a package manager is already working. The commissioning host runs
Python 3.10.12, which has no stdlib `tomllib`, so both TOML configuration and
YAML schemas would require a dependency to be installed before DUM-E can run.

That ordering is backwards. WP-004 exists to establish whether the toolchain is
sound; a harness that cannot start until its dependencies resolve cannot report
that its dependencies do not resolve.

## Decision

The foundation layer (`config`, `state`, `workspace`, `secrets`, `toolchain`,
`upstream`, `inventory`, `cli`) uses the standard library only. Machine records
are JSON. Human documents stay Markdown. `pytest` is a development dependency,
not a runtime one.

## Consequences

- `dume inventory`, `dume workspace`, `dume toolchain` and `dume upstream` run
  on a bare Python 3.10 with nothing installed. This is what makes them usable
  as the *first* thing run on a new host.
- The pack's YAML schema examples are honoured as logical contracts; their
  serialisation is JSON. Field names are unchanged, so a later YAML projection
  is mechanical.
- Later waves (Buzz, serving stacks) will need real dependencies. That is
  correct: by then the toolchain lock exists and can say what changed.

## Evidence

`.venv` is required only for `pytest`; every CLI subcommand was exercised under
`/usr/bin/python3` with no site-packages.
