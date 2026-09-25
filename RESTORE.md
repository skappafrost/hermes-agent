# Home-state backup — 2026-09-25

Snapshot of the **out-of-repo** Hermes customisation on this Windows box. This content lives in
`%LOCALAPPDATA%\hermes\` and is NOT part of the `hermes-agent` git checkout, so `hermes update`
never touches it — and no repo push would cover it either. That is why this branch exists.

## What is here

| Path in this branch | On disk | Notes |
|---|---|---|
| `home/plugins/` | `%LOCALAPPDATA%\hermes\plugins\` | agentmemory MemoryProvider (`plugins/agentmemory/__init__.py`), `hermes-relay/`, `image_gen/`, `web/` (incl. `apinex`) |
| `home/scripts/` | `%LOCALAPPDATA%\hermes\scripts\` | engine autostart + MCP bridge wrappers, probe/verify/selftest scripts, reconfig helpers |
| `home/profiles/<p>/*.md` | per-profile identity/SOUL files | default, neo_agent, nexus_agent, vex_agent, zen_agent |
| `home/profiles/<p>/config.yaml` | per-profile config | **credential values masked** (20 values across 6 files) |
| `home/profiles/<p>/env.keys.txt` | — | key NAMES from each `.env`; values intentionally never backed up |
| `agentmemory/env.masked` | `~/.agentmemory/.env` | engine env with the NIM key masked |

Excluded by design: `skills/` (~31 MB, re-installable from upstream sources), `logs/`, `sessions/`,
state-snapshots, `*.db`, and every secret value.

## What is NOT recoverable from this branch

- **API keys / tokens.** Only names are recorded. The real values live in `%LOCALAPPDATA%\hermes\.env`
  (per profile) and `~/.agentmemory/.env`. Keep your own encrypted copy — this branch cannot restore them.
- **The agentmemory store** (memories/observations/sessions in `~/.agentmemory`, REST `127.0.0.1:3111`).
  Export separately via `POST /agentmemory/export` if you want it off-box.
- **Session history DBs** (`hermes_state*`), and the `.venv` (recreate via the installer / `uv sync`).

## Restore order on a fresh box

1. Install Hermes so `%LOCALAPPDATA%\hermes\hermes-agent\` + `venv` exist.
2. Copy `home/plugins/*` → `%LOCALAPPDATA%\hermes\plugins\`, `home/scripts/*` → `...\hermes\scripts\`.
3. For each profile, re-apply `config.yaml` (you must paste real keys back into `.env` first; the
   `env.keys.txt` files list exactly which keys each profile expects).
4. Recreate the provider junctions: `scripts/install-agentmemory-provider.cmd`
   (makes `profiles/<p>/plugins/agentmemory` a junction to `..\..\plugins\agentmemory`).
5. `npm i -g @agentmemory/agentmemory` (0.9.29 at snapshot time), restore `agentmemory/env.masked`
   to `~/.agentmemory/.env` and fill in the NIM key, then start the engine via
   `scripts/agentmemory-engine.cmd` + the `Startup\AgentMemory_Engine.vbs` autostart.
6. Enable the provider per profile: `memory.provider: agentmemory` (already in the backed-up configs).
7. Add the agentmemory MCP bridge block (`mcp_servers.agentmemory`) — already in the backed-up configs.

## Before running `hermes update`

`hermes update --list-venv-holders` returns exit 3 while anything holds the venv — at snapshot time
that was 1 gateway + 4 Desktop backends + 3 `hermes:relay` + 1 python kernel. Stop exactly those,
then update. The update scope is the checkout only; everything in this branch survives it.
