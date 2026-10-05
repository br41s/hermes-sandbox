# Dependency alerts — triage of record and standing owner

**Date:** 2026-09-18 · **Scope:** `uv.lock`, `package-lock.json`, `website/package-lock.json`

## Why 242 alerts appeared in one morning

They are not new vulnerabilities. `osv-scanner.yml` has been in the repo for a
while, but GitHub Actions only started running on this repo on 2026-09-18, so
this is the first time its SARIF ever reached the Security tab. The queue is a
backlog that nobody had been shown, not an incident.

Two counting notes, both of which mislead if you skip them:

- **`gh api ... /code-scanning/alerts` without `--paginate` returns 30.** The
  first read of this queue said "30 alerts" and was wrong by 8x.
- **242 alerts is not 242 problems.** osv-scanner raises one alert per
  *(advisory × lockfile location)*, so one pin can produce a dozen. The 133
  critical+high alerts collapse to **75 distinct advisories** across
  **52 (lockfile, package) pairs**. Pillow alone was 13 of them. Triage by
  package; the alert count is an artifact of the tool.

## Reachability tiers — what actually ships

This is the fact that does most of the triage work, and it is not visible from
the Security tab at all:

| Lockfile | Ships to production? | Evidence |
|---|---|---|
| `uv.lock` | **Yes** — the agent runtime | `Dockerfile`, uv sync of pyproject extras |
| `package-lock.json` | **Partly** — root/web/ui-tui only | `Dockerfile` installs those three; `apps/*` (the Electron desktop app) is never `npm install`ed in the image |
| `website/package-lock.json` | **No** | never copied into the image; Docusaurus builds a **static** site, so dev-server and build-time packages have no runtime at all |

Consequence worth stating plainly: **neither "critical" was a production
exposure.** `tar` (CVE-2026-59873) is a devDependency of the Electron packager,
and `websocket-driver` (CVE-2026-54466) belongs to webpack-dev-server in the
docs site. Both are fixed below anyway — they were cheap — but the severity
label and the actual risk pointed in different directions, which is the whole
argument for triage over a mass bump.

The genuinely urgent item was rated *high*, not critical: **Pillow 12.2.0**,
carrying three heap out-of-bounds **writes** (CVE-2026-59197 / 59199 / 59205)
on a core dependency that parses attacker-supplied images on the vision path.

## Classification

### Real exposure — patched

| Package | Was → now | Closed | Why it mattered |
|---|---|---|---|
| `Pillow` | 12.2.0 → 12.3.0 | 13 (11 high) | Core dep. Heap OOB writes + decompression bombs reachable from any image an agent is handed. |
| `python-multipart` | 0.0.27 → 0.0.31 | 4 | Quadratic form-parse DoS on the dashboard's multipart upload endpoint. |
| `aiohttp` | 3.14.1 → 3.14.3 | 3 | OOB heap read in the C response parser; WebSocket request smuggling. |

### Patchable — transitive security floors

Bumped to the exact CVE-fixed version, patch/minor only, via a new
`[tool.uv] constraint-dependencies` block. **`uv lock --upgrade-package` alone
would not have held**: these packages are declared nowhere, so the next
unrelated `uv lock` is free to resolve back down. A constraint pins the floor
without adding a declared dependency, leaving the core dependency surface — and
the supply-chain blast radius — unchanged.

`pyasn1` 0.6.4 · `tornado` 6.5.8 · `msgpack` 1.2.1 · `httplib2` 0.32.0 ·
`cbor2` 5.9.0 · `h2` 4.4.1 — 10 advisories, all reached through production
paths (google-auth, python-telegram-bot, fal-client, httpx).

### Accepted — recorded in `osv-scanner.toml`

| Package | Why not patched |
|---|---|
| `starlette` 1.0.1 | **Upgrading regresses the fix we pinned for.** 1.0.1 is held for CVE-2026-48710 (BadHost), which is exactly the desync the dashboard OAuth gate depends on not happening. The scanner's 1.3.x suggestion does not carry it forward. Upstream holds 1.0.1 too. |
| `cryptography` 46.0.7 | Bundled-OpenSSL class, open on upstream/main as well. PR #38 decided to adopt when upstream does rather than freelance a transitive pin ahead of it. **Time-boxed to 2026-12-31** so the wait cannot become permanent. |
| `hermes-agent` (us) | Two VulDB advisories against our own published package, neither with a fixed version — so *no upgrade can ever clear them* and they close by decision or not at all. CVE-2026-10224 (resource consumption in the Feishu webhook handler): the code is at `plugins/platforms/feishu/adapter.py`, not the advisory's `gateway/platforms/feishu.py`, and is bounded against exactly this class — per-IP rate limit, Content-Type guard, early Content-Length reject, 1 MB bounded reader, 30s read timeout, `client_max_size` backstop. CVE-2026-10221 states "up to 0.12.0"; we ship 0.19.0. |
| Electron / `tar` / `extract-zip` / `@xmldom/xmldom` / `fast-uri` / `shell-quote` | `apps/desktop` build chain, **verified absent** from `/opt/hermes/node_modules` in the running container. Time-boxed, so the desktop app answers for them on its own cadence instead of inheriting silence. |
| `pytest`, `Pygments` | dev dependency-group, not installed in the production image. |

### Unreachable — patched anyway, because it was cheap

`website/package-lock.json`: 25 npm advisories → 5, critical cleared, entirely
within semver-compatible ranges (`npm audit fix`, no `--force`). The remaining
5 are `mermaid` → `chevrotain`, which needs a major bump of mermaid and is a
breaking change to the docs site — left open deliberately, not suppressed.

Nothing in the docs site was *accepted*; a static site is cheap to patch, and
83 ignore entries would have been a worse artifact than a lock bump.

## "devDependency" is not a reachability argument

The Dockerfile's `npm install` carries no `--omit=dev`, so dev packages **do**
ship in the image. Checking the container rather than the `dev: true` flag split
the remaining npm queue in two, and the two halves got opposite treatment:

| verified in `/opt/hermes/node_modules` | packages | action |
|---|---|---|
| **absent** | `@xmldom/xmldom`, `fast-uri`, `shell-quote` (and `electron`, `tar`) | accepted, time-boxed |
| **present** | `brace-expansion`, `js-yaml`, `browserslist`, `undici`←`jsdom` | **patched** |

The present ones reach the image through ESLint, jsdom and glob, and nothing in
the agent's runtime executes them — but "present and probably not executed" is
not a decision this register should be asked to carry, and it is exactly the
kind of reason that is plausible enough to stop being re-read. They were cheap
to patch, so they were patched.

Worth stating because it nearly went the other way: the first draft of this
section said "not in the production image" for all of them, which would have
been false for four. The `dev: true` flag in the lockfile answers a different
question than the one that matters.

## 2026-09-27 — incident-watcher batch (8 packages)

| Package | Tier | Was → now | Decision |
|---|---|---|---|
| `httpx2`, `httpcore2` | **`uv.lock` — ships** | 2.7.0 → 2.12.0 | **Real exposure — patched.** GHSA-7mj9-2mp8-4m2p (both), GHSA-8xx6-hgc6-gc2m (httpx2). mcp 2.x's HTTP stack, used by every HTTP/SSE MCP server connection. Pin bumped in all three extras (`dev`, `mcp`, `computer-use`) and in `tools/lazy_deps.py`'s `tool.computer_use` self-heal pin, which has to match. |
| `@xmldom/xmldom` | root lock, apps/desktop + tests-js | 0.8.13 → 0.8.15, 0.9.10 → 0.9.12 | **Patched, acceptance withdrawn.** Accepted on 2026-09-18 as absent from the image; the fixes are now past the 14-day release-age gate, so the patch was cheaper than a fresh set of ignores. Its 10 entries were removed from `osv-scanner.toml`. |
| `fast-uri` | root lock (apps/desktop) + website | 3.1.5 → 3.1.7 | Same as xmldom: 6 ignore entries removed. **3.1.7, not 3.1.8**: 3.1.8 is 12 days old and got through only because of a stale `min-release-age-exclude`, now deleted. |
| `browserslist`, `svgo` | website — never ships | semver-compatible | `npm audit fix`, no `--force`. |
| `js-yaml`, `nanoid` | website — never ships | 4.3.1 → 4.3.2, 3.3.17 → 3.3.18 | Held back by exact `overrides` in `website/package.json` left over from the last fix. `audit fix` cannot move them past an override; they are bumped by hand and clear the docusaurus cascade. |

Two process traps showed up here, and either one quietly undoes the release-age
policy:

- **Re-lock with `npm@12`, never the npm 10 bundled with Node.** npm 10 ignores
  `min-release-age` (it resolved `fast-uri` 3.1.8) and rewrites the lockfile with
  `"peer": true` churn. Every workflow here already does `npm i -g npm@12`; locally,
  `npx -y npm@12 …`.
- **A `min-release-age-exclude` has to be removed when its condition is met.** Each
  one says "remove when X is > 2 wks old", and three of them (`fast-uri`, `js-yaml`,
  `nanoid`) had outlived that. Left in place, an exclude lets the *next* release
  through on day zero. Removed from both `.npmrc` files.

Left open, not suppressed (medium/low, below the watcher's bar): `qs` in the docs
site; `vitest`, `colord`, `joi`, `sanitize-html` in the root lock.

## 2026-09-30 — incident-watcher batch (7 packages, 21 alerts, 1 critical)

All 21 alerts appeared at 03:17 UTC in one scan, and every advisory but one was
published on 2026-09-29. So this is one day's advisory drop, not a scanner change.
Every fix used here is at least 14 days old, so none needed a release-age exception.

| Package | Tier | Was → now | Decision |
|---|---|---|---|
| `PyJWT` | **`uv.lock` — ships** | 2.13.0 → 2.14.0 | **Real exposure, patched.** Six advisories, including the critical (GHSA-ffc3-869f-jxw9, a PEM-whitespace bypass of the HS/asymmetric confusion guard). The package is on the dashboard OAuth gate (`plugins/dashboard_auth/_shared.py`) and the Chronos cron-fire verifier. Both pin `algorithms` and take keys from JWKS, so the confusion class probably cannot be reached. GHSA-9v7f (PyJWKClient follows redirects) is not a confusion issue, though, and the patch costs nothing. 2.14.0 rather than 2.15.1, which is 2 days old. Upstream is still on 2.13.0. |
| `undici` | root lock: **`ui-tui` ships** (direct dependency, its `WebSocket` is the gateway client); also the photon sidecar, baked into the image | 6.28.0 → 6.28.1, 7.29.0 → 7.29.1 | **Patched.** GHSA-rfgv is a DoS through an unrequested WebSocket subprotocol, and it sits on the TUI's own connection. The peer is our own gateway, so the risk is low, but the fix is a patch release. Exact `overrides` in the root and sidecar `package.json`, plus `ui-tui`'s own pin. |
| `brace-expansion` | root lock (glob/ESLint/electron chains) + website | 1.x → 1.1.21, 2.x → 2.1.7, 5.0.9 → 5.0.12 | **Patched**, with per-major `overrides`. The relock also **fixed a broken lock**: `glob@7` → `minimatch@3.1.5` (declares `^1.1.7`) had been resolving to the hoisted 5.0.9, which is outside its range. That is the `expand is not a function` shape from 2026-09-18, and it came back in the v2026.9.24 upstream merge. Every minimatch major now resolves inside its own line, checked by loading each one. |
| `joi` | root lock (`wait-on`, dev) | 18.2.3 → 18.2.9 | `npm update`, patch only. |
| `webpack-dev-middleware` | website: never ships | 7.4.5 → 7.4.6 | Patched. The GHSA-wr3j advisory dates from 2024. On 2026-09-29 it was merged with GHSA-g84c, which extends the affected range to `<7.4.6`, and that is why an old CVE id showed up as new. |
| `electron` | root lock, apps/desktop | 40.10.2 (fix is ≥41.10.6) | **Accepted until 2026-12-31**, alongside the existing Electron block in `osv-scanner.toml`. The fix needs a major bump of the desktop app, and the package is absent from the image. |

Also removed: the `min-release-age-exclude[]=brace-expansion` line in both
`.npmrc` files. Its "remove when > 2 wks old" condition was met in August. Left
in place, it lets any new brace-expansion release through on day zero.

Resolved-tree check, done per path: no major crossings. One path was removed:
the hoisted `node_modules/brace-expansion`, which now lives nested under
`minimatch`. Additions: nested 1.x/2.x copies plus their `balanced-match` and
`concat-map`.

### Second batch, same day (10 highs, raised at 17:35 UTC by the scan of the merge commit)

| Package | Tier | Was → now | Decision |
|---|---|---|---|
| `urllib3` | **`uv.lock`, ships** (core, under `requests` on every web/tool path) | 2.7.0 → 2.8.0 | **Real exposure, patched.** GHSA-vxq7-64xx-v4gw: `read_chunked()` buffers an unbounded chunk-size line, so any hostile server an agent fetches from can exhaust memory. GHSA-8988: HTTPS proxy TLS config is ignored. The declared floor in `pyproject.toml` was raised, not just the lock. 2.8.0 is 15 days old. |
| `axios` | root lock, dev via `wait-on`, **absent from the image** | 1.18.1 → 1.20.0 | Patched anyway (7 advisories), inside `wait-on`'s `^1.16.0` range. |
| `@grpc/grpc-js` | photon sidecar, baked into the image | 1.14.4, **left open** | **Not reachable, not suppressed.** GHSA-m9gg only affects gRPC *servers* created with `requireClientCertificate: false`. The sidecar is a gRPC client, and its only server is `http.createServer`. The fix, 1.14.5, clears the 14-day release-age gate at 2026-10-01 19:47 UTC. Patch it then, rather than suppress it or waive the gate. |

## 2026-10-05 — residue from the 09-30 batches, plus two new docs-site advisories

| Package | Tier | Was → now | Decision |
|---|---|---|---|
| `tornado` | `uv.lock`, transitive (no direct import in our code) | 6.5.8 → 6.5.10 | **Patched**, floor raised to `>=6.5.9` in `[tool.uv] constraint-dependencies`. GHSA-3hv7 (query-string DoS), GHSA-c2m8 (`StaticFileHandler` symlink traversal), GHSA-chx6 (`CurlAsyncHTTPClient` has no size limit). We serve nothing through tornado, but the floor costs nothing. uv resolved 6.5.10 (2026-09-15), which is inside the release-age window. |
| `@grpc/grpc-js` | photon sidecar | 1.14.4 → 1.14.5 | **Patched**, now that it is past the release-age gate (it was left open on 09-30 for that reason). The advisory is server-side, and the sidecar is only a client. |
| `braces` | website, never ships | 3.0.3 (no fix exists) | **Accepted until 2026-12-31**, but it only took effect once the scan passed `--config=osv-scanner.toml` (the `ci:` PR after #397). Without that flag, osv-scanner reads the toml from each lockfile's own directory, so the root file never governed `website/` or the sidecar, and the alert stayed open at `ae7173854`. 3.0.3 is the latest release, and OSV lists it as `last_affected` with no fix, so no upgrade can clear it. |
| `http-cache-semantics` | root lock + website, **absent from the image** | 4.2.0, **left open** | OSV lists no fixed version. 4.3.0 came out on 2026-10-04, so it is inside the 14-day release-age window until 2026-10-18. Re-check then; do not suppress it in the meantime. |

## Part 2 — the standing owner

Nothing told anyone when a new alert appeared. That is why the queue reached
242 unnoticed, and it is the part worth fixing.

The signal is wired into the existing **`incidents/` + `incident-watcher` cron**
(job `f0d670b8e3b7`, hourly, Telegram thread 1904, silent when clean, 24h
heartbeat) as one more detector — `dependency_alert_incidents()` — rather than
as a new mechanism.

Four behaviours carry the design, each guarded by a test in
`tests/test_dependency_alert_incident.py`:

1. **The backlog is baselined, never delivered.** The first run adopts whatever
   is already open and stays silent. Without this the first sweep delivers all
   242 — the exact outcome the signal exists to prevent.
2. **Grouped by package, not by alert.** Pillow is one brief, not thirteen.
3. **Batches roll up.** More than six new packages in one sweep is a
   lockfile-wide shift or a scanner change, not six separate decisions.
4. **Critical/high only.** Medium and low stay in the Security tab.

Each brief carries the reachability tier from the table above, so a docs-site
advisory does not read like a runtime one.

Accepted alerts are suppressed at source in `osv-scanner.toml`, so they never
become alerts at all. That file is the *decision log*; the open queue is the
*backlog*. The 242 happened because nothing distinguished them — keep them apart.

## Alerting, not blocking — and why

`osv-scanner.yml` sets `fail-on-vuln=false`. Keep it that way.

A gate that failed PRs on any open high alert **would have blocked every pull
request in this repo today**, including the one that fixes the alerts. Worse,
the pressure it creates points the wrong way: the fastest path to green is a
mass bump, and this triage found three separate cases where the scanner's
recommended upgrade was wrong — starlette (regresses BadHost), cryptography
(diverges from upstream for no gain), and our own `hermes-agent` advisory (no
version exists that clears it).

Blocking is right when the correct response is mechanical. Here it is a
judgement call about reachability, so the correct mechanism is a signal to a
human on a queue that stays short enough to read.

The one thing that *should* block, and already does, is the orthogonal case:
`supply-chain-audit.yml` fails on malicious-code indicators in a PR diff. A
known CVE in a pinned dep is a scheduling problem; a `.pth` file in a diff is
an attack.

## Operating it

- New critical/high advisory on a package → brief in thread 1904, once.
- Decided not to fix it → add it to `osv-scanner.toml` **with the reason**, and
  a `ignoreUntil` date if you are waiting on someone else.
- Never suppress something merely because patching it is inconvenient. Leave it
  alerting; that is what the queue is for.
