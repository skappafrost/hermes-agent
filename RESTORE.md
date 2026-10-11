# Home-state backup — 2026-10-11

Snapshot of the **out-of-repo** Hermes customisation on this Windows box (`%LOCALAPPDATA%\hermes\`,
`~/.agentmemory`, `~/.hermes-relay`). Staged by `home/scripts/home-backup-stage.py`, which masks
credential values and runs a publish gate replicating GitHub push protection. Gate result for this
snapshot: **OK** — 370 files / 5.90 MiB, 42 values masked across 6 files, 0 secret-shaped values left.

## What is here

| Path in this branch | On disk | Notes |
|---|---|---|
| `home/plugins/` | `%LOCALAPPDATA%\hermes\plugins\` | agentmemory, hermes-gemini-live, hermes-relay, hermes-resetwatch, hermes-talk, homeassistant, provider-usage, web (incl. apinex). `.install-metadata.json` records each install source. |
| `home/scripts/` | `%LOCALAPPDATA%\hermes\scripts\` | engine autostart + MCP bridge wrappers, probes/selftests, reconfig helpers, this stager |
| `home/profiles/<p>/*.md` | per-profile identity/SOUL files | default, neo_agent, nexus_agent, vex_agent, zen_agent |
| `home/profiles/<p>/config.yaml` | per-profile config | credential values masked |
| `home/profiles/<p>/env.keys.txt` | — | key NAMES per `.env` (61 / 76 / 38 / 54 / 26), values never backed up |
| `agentmemory/env.masked` | `~/.agentmemory/.env` | engine env, NIM key masked |

**Changed since `backup/home-state-20260925`:** `plugins/image_gen/` is gone from disk; hermes-talk,
hermes-gemini-live, hermes-resetwatch, homeassistant, provider-usage are new. `add_tree` now also
skips `.github/` and `tests/` under `plugins/` — they are upstream files already public in each
plugin's own repo, and one hermes-talk test's `AQ.<...>` canary literal trips push protection.

Excluded by design: `skills/` (re-installable), `logs/`, `sessions/`, `*.db`, state-snapshots, media
and browser caches, every credential value.

## Repo code needs no push here

The `hermes-agent` checkout at `%LOCALAPPDATA%\hermes\hermes-agent\` was clean at `0670ba4524`, and
`my-fork/main` already contains it plus 82 upstream commits (`git merge-base --is-ancestor main
my-fork/main` → true; `origin/main..my-fork/main` → 0 fork-only commits). So `main` on this fork is a
upstream mirror, ahead of the local checkout — nothing custom to carry.

## What is NOT recoverable from this branch

- **API keys / tokens** — names only. Real values live in `%LOCALAPPDATA%\hermes\.env` and
  `profiles/<p>/.env`, plus `~/.agentmemory/.env`. Their local-only copies are listed below.
- **agentmemory store** (`~/.iii`, REST `127.0.0.1:3111`) — off-box copy is in `aux-state.tar.gz`.
- **Session/state DBs** (`state.db`, `hermes_state*`), skills, and the venv (recreate via installer).

## Local-only archives (contain secrets — never push to a public repo)

All four are in `C:\Users\Ha Trung\hermes-backup-20261011\` on this box:

| Archive | Contents |
|---|---|
| `hermes-home.zip` | `hermes backup` full HERMES_HOME: configs, `.env`, DBs, sessions, media caches, skills |
| `lean-state.tar.gz` | HERMES_HOME minus caches/venvs/node/recordings/snapshots/logs |
| `aux-state.tar.gz` | `~/.iii`, `~/.agentmemory`, `~/.hermes-relay`, `~/.qoder/memory` |
| `qoder-project-memory.tar.gz` | `~/.qoder/projects/*/memory` (per-project auto-memory) |

## Restore order on a fresh box

1. Install Hermes so `%LOCALAPPDATA%\hermes\hermes-agent\` + venv exist.
2. Unpack `hermes-home.zip` and run `hermes import` on it (that restores `.env`, DBs, skills, plugins
   in one step); this branch is the fallback when no archive survives.
3. Fallback path: copy `home/plugins/*` → `%LOCALAPPDATA%\hermes\plugins\`, `home/scripts/*` →
   `...\hermes\scripts\`, re-apply each `home/profiles/<p>/config.yaml`, and paste real keys into
   `.env` using `env.keys.txt` as the checklist.
4. Recreate provider junctions: `scripts/install-agentmemory-provider.cmd`.
5. `npm i -g @agentmemory/agentmemory`, restore `agentmemory/env.masked` → `~/.agentmemory/.env`
   (fill the NIM key), start the engine via `scripts/agentmemory-engine.cmd`.
6. `hermes plugins install` for any plugin whose source is public (hermes-talk, hermes-gemini-live).
