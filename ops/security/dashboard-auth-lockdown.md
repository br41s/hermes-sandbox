# Dashboard Auth Lockdown Runbook

**Target:** Hermes dashboard at https://blhermes.zeabur.app (service `br41s/hermes-sandbox` on Zeabur)
**Goal:** Close the public, no-auth hole by engaging the bundled Nous Portal OAuth gate.
**Owner action required:** CEO (Brais) — steps touch Zeabur env + Nous Portal, which Claude cannot access.
**Code change required:** **None.** The gate already exists and is wired; it is only disabled by env config.

---

## TL;DR

The panel is open because **two env conditions** hold on Zeabur right now:

1. `HERMES_DASHBOARD_INSECURE` is truthy → the OAuth gate is bypassed (`--insecure`).
2. `HERMES_DASHBOARD_OAUTH_CLIENT_ID` is unset → the Nous auth provider never registers.

Fix = register the agent in the Portal, set the OAuth env vars, verify, **then** remove
`HERMES_DASHBOARD_INSECURE`. Order matters (fail-closed safety, §4).

---

## 1. Code path — re-confirmed (read-only verification)

All four anchors verified in this repo. No edits needed.

| Claim | Location | Verified behaviour |
|-------|----------|--------------------|
| Gate engages on public bind without `--insecure` | [`should_require_auth`](../../hermes_cli/web_server.py:175) | `return (host not in _LOOPBACK_HOST_VALUES) and (not allow_public)`. Zeabur binds `0.0.0.0` (non-loopback) → when `allow_public` is False the gate is **required**. |
| `--insecure` is opt-in via env | [`docker/s6-rc.d/dashboard/run`](../../docker/s6-rc.d/dashboard/run) | `insecure=""`; only set to `--insecure` when `HERMES_DASHBOARD_INSECURE` ∈ {1,true,yes,…}. Default = gate active. |
| Fail-closed when gate on but no provider | [`start_server`](../../hermes_cli/web_server.py:7725) | If `auth_required` and `not list_providers()` → `raise SystemExit(...)` — server refuses to bind. Recovery is re-adding `--insecure`. |
| Nous provider auto-registers on `client_id` | [`plugins/dashboard_auth/nous/__init__.py` `register()`](../../plugins/dashboard_auth/nous/__init__.py) | Resolves `HERMES_DASHBOARD_OAUTH_CLIENT_ID` (env wins over `config.yaml`); registers `NousDashboardAuthProvider` only if non-empty **and** starts with `agent:`. Otherwise writes `LAST_SKIP_REASON` and returns (no provider). |
| Middleware enforces the session on gated routes | [`gated_auth_middleware`](../../hermes_cli/dashboard_auth/middleware.py:171) | No-op when `auth_required` is False; when True, verifies the session cookie, lets `/auth/*` + a small public allowlist through, redirects everything else to `/login`. |
| Redirect URI is derived from the public URL | [`_redirect_uri`](../../hermes_cli/dashboard_auth/routes.py:50) + [`resolve_public_url`](../../hermes_cli/dashboard_auth/prefix.py:135) | Returns `{HERMES_DASHBOARD_PUBLIC_URL}/auth/callback`. This is the **exact** value that must be allow-listed at the Portal (§2). |

**Contract constants** (from the nous provider): scope = `agent_dashboard:access`; token = RS256 JWT verified against `{portal}/.well-known/jwks.json`; audience = the bare `client_id`; Portal default = `https://portal.nousresearch.com`; **no refresh tokens** in v1 (expiry → re-login).

> ⚠️ **Honesty flag:** The Zeabur env *values* (`HERMES_DASHBOARD_INSECURE` truthy, `client_id` unset) match the live `auth_required:false` symptom and the code, but were not directly read from Zeabur — Claude has no Zeabur access. Confirm them in the Zeabur dashboard before changing (§3, step 0).

---

## 2. Nous Portal — register this agent instance, get the `client_id`

> **Honesty flag:** The exact Portal UI labels below are inferred from the OAuth contract the
> provider implements (`nous-account-service` agent-dashboard-oauth-contract, PR #180), **not**
> from a verified walkthrough — Claude cannot reach the Portal. Treat field *names* as approximate;
> the field *meanings* are exact. If a label differs, match by meaning.

- [ ] **2.1** Sign in to the Nous Portal: https://portal.nousresearch.com
- [ ] **2.2** Locate (or create) the **Agent instance** for this Hermes deployment. The
      `client_id` you need is shaped **`agent:{agent_instance_id}`** — the `{agent_instance_id}`
      is the Portal's identifier for this instance.
- [ ] **2.3** In the agent's OAuth / dashboard-access settings, register the **redirect URI**
      **exactly**:

      https://blhermes.zeabur.app/auth/callback

      This must match `_redirect_uri()` byte-for-byte (scheme + host + `/auth/callback`, no
      trailing slash, no path prefix). A mismatch makes the Portal reject the login with
      `redirect_uri_mismatch`.
- [ ] **2.4** Confirm the granted scope is **`agent_dashboard:access`** (the only scope the
      provider requests).
- [ ] **2.5** Copy the full `client_id` (the `agent:...` string). You will paste it into Zeabur
      as `HERMES_DASHBOARD_OAUTH_CLIENT_ID` in §3.

> If the Portal also provisions/injects env vars automatically (the contract notes Fly.io-style
> secret injection), the value it injects is the same `agent:{id}`. On Zeabur you set it manually.

---

## 3. Zeabur env changes — Hermes service

Open Zeabur → project → **Hermes** service → **Variables / Environment**.

- [ ] **3.0 (verify first)** Confirm the current state matches the diagnosis: `HERMES_DASHBOARD_INSECURE`
      is present & truthy, and `HERMES_DASHBOARD_OAUTH_CLIENT_ID` is absent. (If reality differs,
      stop and re-assess — the runbook assumes this starting point.)

- [ ] **3.1** **Add** `HERMES_DASHBOARD_OAUTH_CLIENT_ID` = `agent:{id from §2.5}`
      (the literal `agent:` prefix is mandatory — the provider rejects anything else).

- [ ] **3.2** **Add** `HERMES_DASHBOARD_PUBLIC_URL` = `https://blhermes.zeabur.app`
      (no trailing slash; drives the redirect URI from §2.3).

- [ ] **3.3** *(optional)* Leave `HERMES_DASHBOARD_PORTAL_URL` **unset** — it defaults to
      `https://portal.nousresearch.com`. Only set it if you registered the agent on a non-prod
      Portal (e.g. staging).

- [ ] **3.4** **Do NOT remove `HERMES_DASHBOARD_INSECURE` yet.** That happens in §4 after verification.

- [ ] **3.5** Save / redeploy so the new vars take effect.

After this redeploy the panel is still open (insecure still set), but the Nous provider is now
registered. That's exactly the state §4 verifies before flipping the gate on.

---

## 4. Fail-closed cutover (the safe order)

**Why order matters:** removing `HERMES_DASHBOARD_INSECURE` flips `auth_required` to True. If the
OAuth provider isn't registered/valid at that moment, `start_server` hits the `SystemExit`
fail-closed branch — **the dashboard refuses to bind and goes down** (a safe failure: closed, not
open). So we prove the provider is healthy *first*.

- [ ] **4.1 — Verify provider is registered (gate still bypassed).**
      With §3 applied and `HERMES_DASHBOARD_INSECURE` still set, hit the status endpoint:

      curl -s https://blhermes.zeabur.app/api/status

      Expect: `auth_required:false` (still insecure) **and** the `auth_providers` array contains
      `"nous"`. The `nous` entry proves `client_id` resolved and the provider registered — i.e. the
      cutover in 4.2 will NOT fail closed. If `auth_providers` is empty, **stop**: the `client_id`
      is wrong/missing. Re-check §3.1 (prefix, typos) before proceeding.

- [ ] **4.2 — Drop the escape hatch.** Delete the `HERMES_DASHBOARD_INSECURE` variable entirely
      (don't set it to `0`/`false` — just remove it). Save / redeploy.

- [ ] **4.3 — Verify the gate is live.**

      curl -s https://blhermes.zeabur.app/api/status   # expect auth_required:true
      curl -sI https://blhermes.zeabur.app/            # expect redirect to /login (302/3xx)

      Then in a browser: visiting the dashboard should bounce to the Nous Portal login, and a
      successful login should land you back on the panel. Confirm the session token is no longer
      embedded in the pre-auth HTML.

- [ ] **4.4 — Confirm the service stayed up.** Check Zeabur logs for the start-up banner
      `Dashboard binding to 0.0.0.0 with OAuth auth gate enabled. Providers: nous`. If instead you
      see `Refusing to bind dashboard ... no auth providers are registered`, the provider didn't
      register — execute recovery (§5).

---

## 5. Recovery (if the panel goes down)

The fail-closed branch took the server offline because `auth_required` was True with no valid
provider (bad/missing `client_id`, Portal/JWKS mismatch, etc.).

- [ ] **5.1 — Restore access immediately:** re-add `HERMES_DASHBOARD_INSECURE=1` and redeploy.
      The panel comes back open (insecure) within one deploy cycle.
- [ ] **5.2 — Read the logs:** the `SystemExit` message and the nous `LAST_SKIP_REASON` name the
      precise cause (e.g. "`HERMES_DASHBOARD_OAUTH_CLIENT_ID=... doesn't match shape agent:{id}`").
- [ ] **5.3 — Fix the root cause** (usually a `client_id` typo or a redirect-URI mismatch at the
      Portal), then re-run §4 from 4.1.

---

## Quick checklist (printable)

- [ ] §2 Portal: agent registered, redirect URI `https://blhermes.zeabur.app/auth/callback`, scope `agent_dashboard:access`, `client_id` copied
- [ ] §3.1 Zeabur: `HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:{id}`
- [ ] §3.2 Zeabur: `HERMES_DASHBOARD_PUBLIC_URL=https://blhermes.zeabur.app`
- [ ] §3 redeploy
- [ ] §4.1 verify `/api/status` shows `auth_providers` contains `nous` (still insecure)
- [ ] §4.2 remove `HERMES_DASHBOARD_INSECURE`, redeploy
- [ ] §4.3 verify `/api/status` → `auth_required:true`, `/` redirects to login
- [ ] §4.4 confirm service up (gate-on banner in logs)
- [ ] Recovery known: re-add `HERMES_DASHBOARD_INSECURE=1` if it fails closed

---

*Generated 2026-06-05. Code anchors verified read-only against this repo; Zeabur/Portal steps are
operator actions Claude cannot execute or directly observe.*
