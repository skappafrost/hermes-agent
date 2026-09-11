# Hermes Profile Configuration Matrix

Single source-of-truth for the five Hermes runtime profiles on this Windows host.

## Matrix

| Profile name | Config path | AGENT_ID | TEAM_ID | USER_ID | Default scope | Vault home |
|---|---|---|---|---|---|---|
| main | `C:\Users\Ha Trung\AppData\Local\hermes\config.yaml` | `TBD` | `TBD` | `TBD` | `TBD` | `G:\My Drive\Hermes Memory` (default/active vault: **Shared Vault**) |
| vex_agent | `C:\Users\Ha Trung\AppData\Local\hermes\profiles\vex_agent\config.yaml` | `TBD` | `TBD` | `TBD` | `TBD` | `G:\My Drive\Hermes Memory` (default/active vault: **Vex Vault**) |
| neo_agent | `C:\Users\Ha Trung\AppData\Local\hermes\profiles\neo_agent\config.yaml` | `TBD` | `TBD` | `TBD` | `TBD` | `G:\My Drive\Hermes Memory` (default/active vault: **Neo Vault**) |
| nexus_agent | `C:\Users\Ha Trung\AppData\Local\hermes\profiles\nexus_agent\config.yaml` | `TBD` | `TBD` | `TBD` | `TBD` | `G:\My Drive\Hermes Memory` (default/active vault: **Nexus Vault**) |
| zen_agent | `C:\Users\Ha Trung\AppData\Local\hermes\profiles\zen_agent\config.yaml` | `TBD` | `TBD` | `TBD` | `TBD` | `G:\My Drive\Hermes Memory` (default/active vault: **Vex Vault**; see note below) |

## Identity columns

`AGENT_ID`, `TEAM_ID`, `USER_ID`, and the default `AGENTMEMORY_AGENT_SCOPE` are **runtime identifiers** consumed by the AgentMemory layer (via environment variables or the `mcp_servers.agentmemory` environment block). They are **not** currently persisted in any of the five `config.yaml` files.

- These values are intended to be set per profile at process start time.
- A sensible convention, used in recent AgentMemory identity tests, is:
  - `AGENT_ID`: the profile name (e.g., `vex_agent`, `neo_agent`, ...)
  - `TEAM_ID`: `skappa_team`
  - `USER_ID`: `skappa`
  - `AGENTMEMORY_AGENT_SCOPE`: `agent` for per-profile isolation; `shared` for the shared/main profile.
- Until the values are committed to a profile's environment/config, they are marked as `TBD` above.

## Vault home notes

- All profiles share the same physical `vault_home`: `G:\My Drive\Hermes Memory`.
- Each profile is configured to open its own vault directory, with `main` using **Shared Vault**.
- `zen_agent` currently points its `vault_path`, `default_vault`, and `active_vault` to **Vex Vault**. This may be intentional (shared design work) or may need alignment if Zen should have its own vault.

## How to update this matrix

1. Open the profile's `config.yaml` listed in the **Config path** column.
2. To update the **vault home** row, edit:
   ```yaml
   plugins:
     obsidian_vault:
       vault_home: "G:\\My Drive\\Hermes Memory"
       default_vault: "<Vault Name>"
       active_vault: "<Vault Name>"
       vault_path: "G:\\My Drive\\Hermes Memory\\<Vault Name>"
   ```
3. To fill in an `AGENT_ID`, `TEAM_ID`, `USER_ID`, or `AGENTMEMORY_AGENT_SCOPE` value:
   - Add it to the profile's `.env` file (or the `env:` block under `mcp_servers.agentmemory` in `config.yaml`), then update this table.
   - Restart Hermes / the AgentMemory MCP server for the change to take effect.
4. Update this markdown file and commit it so the matrix remains the single source of truth.

## Missing data

- `AGENT_ID`: not yet defined in any profile config or `.env`.
- `TEAM_ID`: not yet defined in any profile config or `.env`.
- `USER_ID`: not yet defined in any profile config or `.env`.
- `AGENTMEMORY_AGENT_SCOPE`: not yet defined in any profile config or `.env`.
