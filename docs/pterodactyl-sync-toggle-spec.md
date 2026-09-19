# Spec — Per-proxy "Sync to Pterodactyl" toggle

Status: Ready to hand to OpenCode
Repo: `cheekysim/proxy-manager`

## Problem Statement

Today every proxy managed by this app is **always** mirrored to Pterodactyl: adding a proxy
creates a Pterodactyl allocation, removing deletes it, editing reconciles it, and the
startup/hourly reconciliation creates allocations for **any** local config that lacks one. There is
no way to expose a port through nginx without also registering it as a Pterodactyl allocation, or
to stop a proxy from being kept in sync with Pterodactyl. The operator wants per-proxy control over
whether a proxy syncs to Pterodactyl.

## Solution

Add a per-proxy `sync_to_pterodactyl` flag:

- **Add Proxy modal**: a "Sync to Pterodactyl" toggle, **on by default**. When on (default),
  behaviour is unchanged (allocation created). When off, the nginx config is still created but no
  Pterodactyl allocation is made for it.
- **Proxy table**: a "Pterodactyl" on/off switch per row. Flipping it on creates the allocation
  (if missing); flipping it off deletes the allocation (if present). The switch reflects each
  proxy's current state.
- **Reconciliation** (startup + hourly `sync_allocations`) respects the flag so a sync-off proxy is
  neither created nor resurrected, and any pre-existing allocation for a sync-off proxy is cleaned
  up.

## User Stories

1. As an operator, I want to add a proxy with the "Sync to Pterodactyl" option **off**, so that a
   port is exposed through nginx without creating a Pterodactyl allocation.
2. As an operator, I want the "Sync to Pterodactyl" option in the Add Proxy dialog to be **on by
   default**, so that the current out-of-the-box behaviour is preserved.
3. As an operator, I want a per-proxy switch in the table to turn Pterodactyl sync **on**, so that
   a previously-excluded proxy gets its allocation created on demand.
4. As an operator, I want a per-proxy switch to turn Pterodactyl sync **off**, so that the proxy's
   allocation is removed and it stops being re-synced.
5. As an operator, I want the hourly/startup sync to honour the flag, so that a sync-off proxy is
   never re-created or resurrected automatically.
6. As an operator, I want removing a proxy to clean up its Pterodactyl allocation as before when
   sync is on, and to still remove the proxy (only) when sync is off.
7. As an operator, I want editing a proxy to preserve its sync flag across an ip/port/protocol
   change, so the setting isn't lost.
8. As an operator, I want existing proxies (created before this change) to default to synced, so
   nothing changes for current deployments.

## Implementation Decisions

### Persistence — new model
- Add a `ProxySetting` model (SQLite via the existing `db`): `id`, `ip`, `port`, `protocol`,
  `sync_to_pterodactyl` (boolean, default `True`), with a unique constraint on `(ip, port,
  protocol)`.
- **Backwards compatibility**: the absence of a row means "synced" — `True`. No migration of
  existing rows is required; existing proxies behave exactly as before.

### Single seam — lookup helper
- Introduce one helper: `get_sync_flag(ip, port, protocol) -> bool` returning `True` when no row
  exists, and a `set_sync_flag(ip, port, protocol, value)` upsert. All call sites (Add/Remove/Edit/
  toggle endpoint/sync loop) go through these so behaviour is consistent in one place.

### Add Proxy (`/api/add`)
- Accept an optional `sync_to_pterodactyl` in the payload (default `True`). Write the nginx config
  as today. Create the Pterodactyl allocation **only if** the flag is on. Persist the flag.
- Error/rollback behaviour is unchanged (on allocation failure, revert the config + stored flag).

### Remove Proxy (`/api/remove`)
- Keep current behaviour when sync is on (delete the allocation). When sync is off, still delete
  the config; delete a lingering allocation if one exists (cleanup), and remove the `ProxySetting`
  row.

### Edit Proxy (`/api/edit`)
- When sync is on, keep current create/delete-allocation logic. When sync is off, update the nginx
  config and port the `ProxySetting` row to the new (ip/port/protocol) with the same flag; make no
  allocation changes.

### New endpoint — `POST /api/sync_toggle`
- Body: `{ ip, port, protocol, sync_to_pterodactyl }`.
- When turning **on**: create the allocation if missing (reuse `find_allocation_by_ip_port` /
  `create_allocation`), then set the flag; roll back the flag if the Pterodactyl call fails.
- When turning **off**: delete the allocation if present (reuse `find_allocation_by_ip_port` /
  `delete_allocation`), then set the flag; on failure restore the prior state.
- Requires an authenticated session (uses `@token_required`). Returns `{ok: true}` or an error
  following the existing rollback conventions.

### List (`/api/proxies`)
- `list_items` returns `sync_to_pterodactyl` alongside each proxy's existing `ip`/`port`/
  `protocol`/`filename` (via the lookup helper), so the table can render the switch's state.

### Reconciliation (`sync_allocations`)
- Direction **config → Pterodactyl**: only `create_allocation` for local proxies whose flag is on.
- Direction **Pterodactyl → config** (creates a config for an alloc with no local file): unchanged,
  but before creating, skip/normalise so a sync-off proxy that only exists downstream is not
  silently brought back — if a proxy has a flag=off setting, reconcile by deleting its allocation
  rather than trusting it. (net effect: the flag is authoritative.)

### Frontend (`index.html`)
- **Add Proxy dialog**: a Bootstrap `form-check form-switch` "Sync to Pterodactyl", checked by
  default; include `sync_to_pterodactyl: true/false` in the `/api/add` POST body.
- **Proxy table**: add a "Pterodactyl" header column; each row gets an `form-check form-switch`
  checked per `sync_to_pterodactyl`. On change, `POST /api/sync_toggle` with the row's
  ip/port/protocol and the new value; optimistically update the row, revert on a non-2xx response,
  and surface the error. Table wiring mirrors the existing `data-role` template pattern.

## Testing Decisions

- **Seam tested:** `get_sync_flag` / `set_sync_flag`; the toggle endpoint; and that
  `sync_allocations` skips sync-off proxies. Keep tests at the Flask test-client / handler level
  (this repo has no test suite yet — keep it light, prefer assertions on external behaviour:
  given a flag, does the right allocation op happen).
- **Manual acceptance:** add proxy with toggle off → no allocation created (verified in Pterodactyl
  panel / API); flip table switch on → allocation appears; flip off → allocation removed; run the
  hourly sync twice → sync-off proxy is not resurrected; existing proxies render with the switch on.

## Out of Scope

- Syncing to Pterodactyl **nodes** (this only manages allocations on the configured node).
- Batch toggle/select-all UI.
- Anything in the pending `security-hardening` PR (SECRET_KEY/rate-limit/CSRF). The new
  `POST /api/sync_toggle` endpoint should be written so it will get the same auth/CSRF treatment
  when that PR lands (use `@token_required` now).

## Further Notes

- The flag lives in the local app DB only — it is **not** stored in Pterodactyl. If the local DB is
  rebuilt, all proxies default back to synced (acceptable for this tool).
- Turning sync **off** removes the proxy's allocation from the configured Pterodactyl node; only do
  this for ports that don't need to be registered in Wings/Pterodactyl.