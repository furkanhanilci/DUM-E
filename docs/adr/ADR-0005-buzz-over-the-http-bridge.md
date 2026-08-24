# DUME-ADR-0005 — Reach Buzz over its HTTP bridge, not its CLI

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-011, WP-012, WP-013, WP-014, WP-015, WP-017, WP-031
- **Reuse class:** `DEPENDENCY` (the relay) + `STANDARD` (Nostr / NIP-98)

## What Buzz is, at the pinned revision

Verified by reading `0720f5380ce8a6c050afac159f8462c06cd51ab5`, not from
documentation about it. Buzz is a self-hostable **Nostr relay** that is the
single signed event log for a workspace, plus clients on top of it: a Tauri
desktop application, a Flutter mobile app, a CLI, an ACP harness and an MCP tool
server. Thirty-one Rust crates. Messages, channels, personas, teams, git patches
and agent telemetry are all signed Nostr events — 129 kind constants in
`crates/buzz-core/src/kind.rs`.

## The finding that shapes everything else

**There is no headless way to create a managed agent or a team.** Those live in
the desktop application's own storage (`~/.local/share/xyz.block.buzz.app/agents/`),
`buzz agents draft-create` only opens a prefilled form in that application, and
there is no `teams` subcommand at all. Kind 30177 is an explicitly redacted
public *projection* — it deliberately omits the secret key, the auth tag and the
runtime fields, so publishing one does not make an agent runnable.

`get_agent_models` is likewise a Tauri desktop command, not a relay endpoint.
The transcript's premise that DUM-E inherits Buzz's model discovery for free is
**wrong at this revision**; headless, the equivalent is `buzz-acp models`, which
asks the harness rather than the provider.

The commissioning host is headless, the desktop AppImage requires glibc 2.39
against this box's 2.35, and WebKitGTK on the proprietary NVIDIA driver has an
open crash report. So the desktop path is closed, and the question is what
remains.

## Decision

Use the relay's **NIP-98-authenticated HTTP bridge** — `POST /events`,
`POST /query`, `POST /count` — signed directly from Python, and build on the
three primitives that genuinely are headless:

| Concept | Mechanism |
|---|---|
| Identity | a secp256k1 keypair; possession is authentication |
| Channel | a UUID in an `h` tag |
| Waking a participant | a `p` tag naming their pubkey |

That is enough for what DUM-E actually needs from a collaboration substrate: a
place for a cohort to talk, addressable by role, with a durable operational
record. It is not enough to make Buzz the agent supervisor, and DUM-E does not
ask it to be — the orchestrator owns its own agent processes, which is also what
the repository's own Python reference (`benchmarks/harbor-buzz-orchestra/`)
does.

### Why not the CLI

The `buzz` CLI is one of thirty-one Rust crates. Building it plus `buzz-acp`,
`buzz-agent` and `buzz-dev-mcp` costs roughly 6–9 GiB of `target/` plus ~1.5 GiB
of toolchain plus ~2 GiB of registry — 10–14 GiB on a host with under 40 GiB
free at 89% used — to obtain a JSON-over-stdout wrapper around the same three
endpoints. The bridge is reached in about two hundred lines of Python with one
dependency (`coincurve`) for the signature.

This is not a rejection of the CLI. If the harness later needs `buzz-acp` to
drive a real ACP runtime, that build becomes worth its disk. It is not worth it
to send a message.

### Why the relay stays closed

`BUZZ_REQUIRE_RELAY_MEMBERSHIP=true`. A freshly minted agent identity cannot
write until an invite minted by the owner is redeemed. That is two decisions by
two parties — the owner admits, the identity joins — and DUM-E preserves the
split rather than routing around it, because an agent's ability to speak being
grantable and revocable is a property worth keeping.

## Channel identifiers are derived, not allocated

The relay will allocate a channel UUID if none is supplied, but then the mapping
from work package to channel exists only in its database and DUM-E needs a
lookup table to find its own channel after a restart. Channel ids are instead
`uuid5(DUME_NAMESPACE, wp_id)` — the same on every run and every relay. A
derivation cannot be lost; a file can.

## Authority boundary

Invariant 11 holds unchanged and is enforced by construction: **nothing in
`dume/collaboration/` can move a work package or record a review.** It publishes
and reads messages. Every verdict is recorded through the state store, by an
identity the store checks for independence. A message in a channel is
operational, and if the relay is down the packages do not move — which is
correct, and is reported as `BuzzUnavailable`, distinct from a failure of the
work (Invariant 16).

## Verified bring-up

- `ghcr.io/block/buzz:sha-0720f53`, pinned. Postgres 17, Redis 7, MinIO.
- Relay healthy on port 3000; 8080 and 9102 are container-internal.
- Secrets generated to a filesystem that can enforce `0600` — see
  [ADR-0007](ADR-0007-secrets-need-a-filesystem-that-enforces-permissions.md).
- Owner publishes; a new identity is refused until admitted; after admission it
  publishes and its events read back. A seven-role cohort was deployed to a
  derived channel with seven distinct identities and six mention tags.

## Residual risks

| Risk | Note |
|---|---|
| `sha-0720f53` is a branch build, not a semver release | Retention could remove it. Mirror the image locally. |
| `switch_model` is implemented ahead of its own NIP-AO spec | Treat runtime model switching as unstable |
| `/api/admin/v1/*` is gated only by a `Host` header match | Never expose the relay beyond localhost |
| No headless human client | The operator reads the channel through DUM-E, or through the desktop app on another machine |
