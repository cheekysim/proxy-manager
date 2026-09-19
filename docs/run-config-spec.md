# Spec — Configurable bind host/port for proxy-manager

Status: Ready for implementation (hand off to OpenCode)
Repo: `cheekysim/proxy-manager`
Context: Original **Tailscale** method restored; Pangolin/newt raw-resource integration is **shelved** (Pangolin requires a stack restart per new raw port — rejected).

---

## Problem Statement

proxy-manager's web GUI bind address/port are not configurable. `app.run()` (dev) starts on
Flask's default `127.0.0.1:5000`, and in production the README/systemd run command hardcodes the
bind as `gunicorn main:app -w 4 -b ...:5000`. The operator needs the GUI to run on a **different
port** on the VPS, without editing code or run commands.

## Solution

Read `HOST` and `PORT` from environment (`.env`), with backward-compatible defaults, and apply
them wherever the app binds:

1. `main.py` run block: `app.run(host=<HOST>, port=<PORT>)`.
2. Production run instructions / systemd (`README.md`): derive the gunicorn bind from the same
   `HOST`/`PORT` variables.
3. `.env.example`: document the two new variables next to the existing ones.
4. Defaults must keep today's behaviour when the vars are absent: `HOST=127.0.0.1`, `PORT=5000`.

## User Stories

1. As an operator, I want the GUI to bind a port I choose via `.env`, so that it can run on a
   port that is free/exposed on the VPS.
2. As an operator, I want the dev `flask run` and production `gunicorn` paths to use the same
   `HOST`/`PORT` config, so that behaviour is consistent across environments.
3. As an operator, I want the existing default (`127.0.0.1:5000`) preserved when no config is
   set, so that existing deployments are unaffected.

## Implementation Decisions

- Add `HOST` (default `127.0.0.1`) and `PORT` (default `5000`) to the `.env`-driven config read at
  startup alongside the existing `SECRET_KEY`/`ADMIN_*`/`CONFIG_FILES_PATH` vars.
- Use the same env-parsing style already present in the file (plain `os.getenv` with a default).
  `PORT` must be coerced to `int`; treat a non-numeric value as the default.
- `main.py` dev block:
  `app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")))`.
- README: update the "Run" section and systemd example so the bind reads
  `-b ${HOST:-127.0.0.1}:${PORT:-5000}` (or equivalent), and document the two vars in the config
  table.

## Testing Decisions

- Trivial, manual acceptance is sufficient: with `PORT` set, start the app, `curl` the admin
  guard on `http://127.0.0.1:<PORT>/` and expect an HTTP 200/redirect; confirm the default
  `127.0.0.1:5000` still works when unset.
- No new unit tests expected — this is a config-binding change with no new logic.

## Out of Scope

- Pangolin raw TCP/UDP resource integration (shelved — Tailscale method is the fallback; nginx on
  the VPS proxies to game nodes over Tailscale, `nginx -s reload` is dynamic).
- iptables / IONOS firewall rule automation — **pending decision** (see Further Notes).
- Changing game-port exposure behaviour, the Pterodactyl allocation sync, or the nginx config
  generation.

## Further Notes

- The original nginx method already supports `tcp`/`udp`/`both` and uses `nginx -s reload` (no
  restart), which is exactly what the operator needs.
- **Open question for Euan:** do you still want the app to automate the VPS iptables
  add/remove-per-port (`INPUT` policy is currently ACCEPT-all, so binding the port is enough for
  reachability today; an explicit idempotent rule would be belt-and-braces / audible guard). If
  yes, it can be a follow-up ticket against the nginx provider. If no, this spec is complete on
  its own.