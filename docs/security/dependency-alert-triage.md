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
| Electron / `tar` / `extract-zip` | `apps/desktop` build chain, not in the production image. Time-boxed, so the desktop app answers for them on its own cadence instead of inheriting silence. |
| `pytest`, `Pygments` | dev dependency-group, not installed in the production image. |

### Unreachable — patched anyway, because it was cheap

`website/package-lock.json`: 25 npm advisories → 5, critical cleared, entirely
within semver-compatible ranges (`npm audit fix`, no `--force`). The remaining
5 are `mermaid` → `chevrotain`, which needs a major bump of mermaid and is a
breaking change to the docs site — left open deliberately, not suppressed.

Nothing in the docs site was *accepted*; a static site is cheap to patch, and
83 ignore entries would have been a worse artifact than a lock bump.

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
