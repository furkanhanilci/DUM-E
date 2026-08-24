# DUME-ADR-0007 — Secrets live on a filesystem that can enforce permissions

- **Status:** ACCEPTED
- **Date:** 2026-08-24
- **Scope:** WP-003, WP-043; the Buzz and Qwen bring-ups

## Context

The commissioning host's large filesystems are the two removable-media mounts.
`/media/otonom/DATADRIVE1` has 834 GiB free and was the obvious place for
upstream clones, model weights and the Buzz deployment.

Generated secrets were written there and `chmod 0600` was applied. The
permission did not take: `stat` reported `777`. The mount is **fuseblk** —
NTFS via ntfs-3g — with `default_permissions,allow_other`, and it cannot
represent a Unix mode at all. Every private key and password on it was
effectively world-readable, while the code that wrote them believed otherwise.

A `chmod` that silently does nothing is worse than no `chmod`, because it
produces a false belief with a plausible-looking audit trail.

## Decision

Anything secret lives on **ext4**, at `~/.dume/secrets/`, mode `0700` on the
directory and `0600` on each file. Where a tool insists on finding a file at a
fixed path on another filesystem — the Buzz compose bundle expects `.env` beside
its `compose.yml` — a **symlink** points at the real file, so the bytes stay
under an enforceable mode.

Bulk, non-secret material — upstream clones, model weights, container volumes —
stays on the large mount, where the absence of Unix permissions costs nothing.

## Consequences

- `~/.dume/secrets/buzz-identities.json` holds the relay owner, relay and
  orchestrator private keys. It is outside the repository and outside evidence.
- `~/.dume/secrets/buzz-relay.env` holds the deployment passwords, symlinked
  into the compose directory.
- Neither is reachable from a work-package packet, and `json_dump` redacts at the
  moment evidence becomes a file.

## The second finding

Scanning the new secret directory reported it **clean** — with a private-key
vault and five live passwords in it.

The credential detector anchored its generic rule on `\b`. `_` is a word
character, so there is no word boundary before `PASSWORD` in `POSTGRES_PASSWORD`
— which is the single most common shape a credential takes. `private_key` was
missing from the keyword list entirely, and nothing matched a name merely
*ending* in `_SECRET`.

The rule now allows a prefix ending at a separator, covers the keyword set that
actually appears in deployment files, and explicitly excludes names denoting a
public half (`PUBLIC_KEY`, `pubkey`) so that a correct configuration does not
make the scanner unusable. Seventeen regression cases in
`tests/test_secret_boundary.py` fix both directions: seven shapes that must be
caught, and seven public or innocent values — commit SHAs, artefact digests,
placeholders — that must not be.

Both findings came from running the control against a real deployment rather
than from reading it.
