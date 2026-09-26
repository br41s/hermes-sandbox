# Incident postmortem: XMRig cryptominer via public dashboard

**Status:** Resolved — 2026-06-05
**Severity:** High (RCE-as-a-service via unauthenticated write access; attempted container escape)
**Service:** Hermes dashboard, https://blhermes.zeabur.app (Zeabur)

## Summary

The public Hermes dashboard bound `0.0.0.0` on Zeabur with `auth_required=false`.
Mutating `/api/*` endpoints "required" an ephemeral `_SESSION_TOKEN`, but that
token was **embedded in the public root HTML** served to anonymous clients —
making the panel effectively unauthenticated for writes. An attacker scraped the
token and bulk-injected ~26 fake MCP servers, each an XMRig (Monero) cryptominer
dropper that also attempted host container escape.

## Root cause

Token leaked in public HTML + public bind = unauthenticated write access to
`POST /api/mcp/servers`. The OAuth auth gate existed in code but was disabled
because `HERMES_DASHBOARD_INSECURE` was set truthy and no OAuth `client_id` was
configured.

## What the malware did

- **26 MCP entries**, all `command=bash -c <identical payload>`, random names
  `hu_/ns_/pf_/dx_/au_/fm_<12hex>` (prefixes are attacker camouflage, not Hermes
  categories).
- Payload downloaded **XMRig 6.22.2** from GitHub → mined Monero to
  `pool.supportxmr.com:443` (worker pass = the server's own name).
- **Attempted container escape:** if `/proc/1/root/tmp` was writable, copy miner
  to the host, `chroot` in, and append a root cron (`*/15 * * * *`) to
  `/proc/1/root/etc/crontab` for host persistence.
- **Crash-loop side effect:** gateway start connects to all enabled MCP servers in
  parallel (`asyncio.gather`); 26 XMRig at `max-threads-hint:100` → OOM → Zeabur
  BackOff. (This, plus the miner CPU, is what made the panel "barely load.")

## Remediation (in order performed)

1. **Locked the panel** — registered the agent with Nous Portal, set
   `HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:{id}` + `HERMES_DASHBOARD_PUBLIC_URL`,
   removed `HERMES_DASHBOARD_INSECURE`. OAuth gate engaged; the leaked
   `_SESSION_TOKEN` is now inert (legacy HTTP middleware short-circuits under the
   gate, WS requires a single-use ticket, token no longer injected into HTML).
   Full runbook: [`dashboard-auth-lockdown.md`](./dashboard-auth-lockdown.md).
2. **Purged the payload** — removed the poisoned `mcp_servers:` block from the
   persisted `/opt/data/config.yaml` (all 25 remaining entries were malicious;
   zero legitimate MCP servers live in Hermes config — real ones are per-project
   `.mcp.json`). Backed up first; confirmed `mcp_servers = {}` survives restart
   **and** image rebuild.
3. **Confirmed no persistence** — container inspection: no miner processes,
   `loadavg` idle, no `/tmp/.cache/.xmr`, all cron empty. **PID 1 = `s6-svscan`**
   (the container's own init, not the host) → the container escape **failed**; no
   host-level persistence possible from inside an unprivileged container.
4. **Rotated all secrets** that were readable while the panel was public
   (`/opt/data/.env`, `config.yaml`: API keys, `HERMES_CALLBACK_SECRET`, provider
   and OAuth credentials).
5. **Hardened the gate path** — deployed a verified-session cache + threadpool
   offload (PR #8) so the now-mandatory per-request OAuth verification doesn't
   freeze the single-worker event loop.

## Verification

- `GET /api/status` → `auth_required: true`, `auth_providers: ["nous"]`.
- `GET /` → `302 /login`.
- Pre-auth HTML no longer contains the session token (0 matches).
- `mcp_servers = {}` persisted across pod restarts and a full image rebuild.
- Gateway returned to `running` / platform `connected` once the miners were gone.

## Lessons / preventions

- **Never serve an auth token in client-reachable HTML.** "Token-gated" writes are
  unauthenticated if the token ships to anonymous clients.
- **A disabled-by-default gate must fail closed, not silently off.** The fix relies
  on `should_require_auth` engaging on any non-loopback bind unless `--insecure` is
  explicit. Don't reintroduce host-derived `--insecure`.
- **Injected MCP servers are arbitrary code execution.** `POST /api/mcp/servers`
  must always sit behind the gate.
- **Treat any secret exposed during a public window as compromised** and rotate,
  even after closing the hole — closing it doesn't un-leak what already left.
