# Installing DUM-E

Bring-up is layered, and the layers are in dependency order for a reason: each
one is verifiable on its own, and a later layer failing never leaves an earlier
one in an unknown state. Stop at whichever layer covers what you need — **layer 0
alone is a working, useful, fully-tested installation.**

| Layer | Gives you | Needs |
|---|---|---|
| 0 — Foundation | Inventory, boundaries, locks, state, scenarios | `python3` ≥ 3.10, `git` |
| 1 — Tests | The 293-test suite | `uv` or `pip`, `pytest` |
| 2 — Discipline | Superpowers skills projected into role prompts | vendored — nothing to install |
| 3 — Runtimes | Agents that can actually run | at least one model runtime |
| 4 — Collaboration | Buzz relay, narration, operator view | Docker |
| 5 — Control | Telegram command bridge | a bot token |

---

## Layer 0 — Foundation

The foundation deliberately carries **no third-party runtime dependency**. It
has to run as the first thing on a new host, before anything is installed
([ADR-0001](adr/ADR-0001-foundation-has-no-third-party-dependencies.md)) — a
bring-up tool whose first step is "install our dependencies" cannot report on
the host it is being installed onto.

```bash
git clone https://github.com/furkanhanilci/DUM-E.git
cd DUM-E

python3 -m dume.cli inventory     # measure this host: CPU, RAM, GPUs, disk, host class
python3 -m dume.cli workspace     # what is bound, and what is not
python3 -m dume.cli toolchain     # which tools exist, and from which wave each is needed
python3 -m dume.cli upstream      # is every pin still what upstream serves?
python3 -m dume.cli scenarios -v  # attack the controls and watch them hold
```

`inventory` classifies the host (`SINGLE_GPU_CONSTRAINED`, and so on). That
class is what later layers consult before claiming a model will fit — a claim
about capacity that was never measured is the failure mode this exists to
prevent.

### Seed the lifecycle

The 54 commissioning packages live in an implementation pack read at seed time
rather than retyped, so a hand-copied dependency list cannot disagree with the
plan on the day it matters:

```bash
python3 -m dume.cli seed                  # register all 54 packages + 122 dependencies
python3 -m dume.cli status                # what state each one is in
python3 -m dume.cli history WP-001        # every transition, with its actor
```

State lives in `state/dume.db` (SQLite, deliberately untracked — transitions
must survive a process restart, not a fresh clone). If the pack is not present,
seeding fails loudly rather than registering a partial catalogue. See
[`WORK_PACKAGES.md`](WORK_PACKAGES.md).

### Bind a target

`SPEC_MOUNT` and `BUILD_TARGET` ship **unbound** and grant nothing until a human
binds them ([ADR-0003](adr/ADR-0003-specification-and-target-workspaces-are-unbound.md)).
Until then, `check_write` refuses every path outside a bound workspace, and any
package that needs a target is `BLOCKED` rather than improvised onto a guess.

Edit `config/dume.config.json`:

```json
"BUILD_TARGET": {
  "path": "/absolute/path/to/the/repository/being/built",
  "mode": "READ_WRITE",
  "bound": true
}
```

Then prove the boundary is real, rather than trusting the word `READ_ONLY` in a
config file:

```bash
python3 -m dume.cli workspace --probe     # actually attempts a write in each workspace
python3 -m dume.cli check-write /some/path
```

A read-only specification mount should be one **the kernel refuses to write**,
not one the harness politely declines to. `--probe` is what tells the two apart.

### Credentials

```bash
python3 -m dume.cli secrets config/       # scan a path for credential material
```

The scanner **fails closed**, and deliberately lets commit SHAs, model digests
and placeholders through — a scanner that cannot be satisfied is a scanner that
gets switched off ([ADR-0007](adr/ADR-0007-secrets-need-a-filesystem-that-enforces-permissions.md)).

---

## Layer 1 — Tests

```bash
uv venv .venv && uv pip install pytest
.venv/bin/python -m pytest -q
```

`pytest` is the only test dependency. Most of the 293 tests are attacks: symlink
escapes, `../` traversal, stale evidence, a candidate that edits its own frozen
acceptance criteria, a producer trying to accept its own work.

---

## Layer 2 — Engineering discipline

Nothing to install. Superpowers is **vendored** into `vendor/superpowers/` at the
pinned revision, and that is what the agents are held to. See
[`SUPERPOWERS_ROLES.md`](SUPERPOWERS_ROLES.md) for why it is vendored rather than
read out of a plugin cache.

```bash
python3 -m dume.cli skills                # which discipline each role is held to
python3 -m dume.cli upstream              # has the pinned revision drifted?
```

---

## Layer 3 — Runtimes

A role that needs a model needs a runtime bound to it, and a runtime that has
not been **qualified** for that role is not eligible however available it is.

```bash
python3 -m dume.cli runtime --probe                    # what actually answers right now
python3 -m dume.cli runtime --probe --bind implementer # bind a role
python3 -m dume.cli qualify                            # run the qualification trials
```

`config/runtimes.json` is the catalogue: family, cost tier, mode
(`NORMAL`/`RESERVE`), qualified roles, and current status. Availability and
eligibility are separate columns on purpose.

### Local serving (optional)

The decided profile is llama.cpp CUDA in a container, Q4_K_M, one GPU
([ADR-0004](adr/ADR-0004-llama-cpp-cuda-is-the-qwen-serving-profile.md)) — vLLM's
CUDA floor does not admit every driver, and llama.cpp is the candidate stack that
runs quantised weights on this class of host.

```bash
python3 -m dume.cli qwen                  # preflight: driver, image, GPU, chat template, endpoint
python3 -m dume.cli qwen --serve          # start the container (--force replaces a running one)
python3 -m dume.cli qwen --probe          # send a real tool call and see what comes back
```

Weights go in `MODEL_CACHE`, outside Git — the root filesystem cannot hold them.

**Two families, not two copies** ([ADR-0008](adr/ADR-0008-two-local-families-not-two-copies.md)).
A second local model exists so review and verification can be independent of the
implementer without spending external quota. A second copy of the *same* weights
would buy availability and no independence at all.

---

## Layer 4 — Collaboration

The Buzz relay gives every work package a channel and every verdict an
addressee. DUM-E reaches it over its NIP-98 HTTP bridge, signed from Python
([ADR-0005](adr/ADR-0005-buzz-over-the-http-bridge.md)).

The relay is deployed **outside this repository** — it is upstream software,
pinned in `config/upstream.lock.json`, run from its own image
(`ghcr.io/block/buzz`) alongside Postgres, Redis and MinIO. DUM-E does not ship
a compose file for it: the harness's job is to *reach* a relay, not to own its
deployment. Once one is answering:

```bash
python3 -m dume.cli health                # is everything the harness depends on up?
                                          # this also asserts the 11 standing channels
python3 -m dume.cli view                  # read-only operator view, default :8080
python3 -m dume.cli view --once           # print one snapshot and exit
```

`health` re-asserting channels is the normal case, not a repair step: a channel
that already exists refuses creation and is reported as *asserted*, not
*created* — claiming "created" for eleven channels that were already there would
read as a fresh deployment every time health runs.

The relay image is pinned. `dume upstream` tells you when the pin and what
upstream serves have diverged; an unreachable upstream reports *unreachable* and
never "no drift".

---

## Layer 5 — Control

Optional. Telegram is a **surface, never an authority** — nothing said in a chat
constitutes a review, a verification or an acceptance. Those are records, and
they come from the gate.

```bash
python3 -m dume.cli telegram --token <BOT_TOKEN>     # write and verify the token
python3 -m dume.cli telegram --discover              # who has messaged, with numeric ids
python3 -m dume.cli telegram --authorise <USER_ID> --name "…" --max-class DANGEROUS_ACTION
python3 -m dume.cli telegram --broadcast <CHAT_ID>   # where DUM-E narrates
python3 -m dume.cli telegram --forum <CHAT_ID>       # one topic per workspace channel
python3 -m dume.cli telegram --check                 # verify bot + allowlist, then exit
python3 -m dume.cli telegram                         # start polling
```

Send `/here` in the chat to have it tell you its own chat and topic ids. The
command gateway exposes 19 actions in four classes; anything destructive sits at
`DANGEROUS_ACTION` and needs an authorised operator. There is no shell.

---

## Verifying the whole thing

```bash
python3 -m dume.cli scenarios -v     # the adversarial acceptance scenarios
python3 -m dume.cli pilot -v         # synthetic end-to-end run, with fault injection
.venv/bin/python -m pytest -q        # the suite
```

The synthetic pilot drives a package from packet to merge-eligible against a
disposable target it creates and destroys, including deliberate failures — among
them a candidate that edits its own frozen acceptance criteria and is caught
before any reviewer wastes effort on it. It needs no models and no network.

Only once that passes is a real run meaningful:

```bash
python3 -m dume.cli commission WP-nnn
```

which requires `BUILD_TARGET` bound, a qualified runtime per role, and
independence satisfiable across the cohort. If any of those is missing the
package is `BLOCKED`. That is the correct state — **never reduce assurance to
keep work moving.**

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `configuration not found` | run from outside the repo root | `cd` to the clone |
| `workspace 'X' is marked bound but has no path` | `bound: true` with `path: null` | set the path, or `bound: false` |
| `no work-package manifest at …` | `dume seed` cannot find the pack | point it at the pack, or skip seeding |
| A package is `BLOCKED` | `BUILD_TARGET` unbound, or no independent runtime | bind it, or bring a second family up |
| `RUNTIME_MISSING` on a local runtime | nothing answers on the OpenAI-compatible port | `dume qwen`, then `dume qwen --serve` |
| A role has no eligible runtime | available ≠ qualified | `dume qualify` |
| `dume upstream` reports unreachable | no network | it is *unreachable*, not "no drift" — do not treat it as agreement |
