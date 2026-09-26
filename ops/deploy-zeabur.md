# Deploying the Hermes dashboard to Zeabur

The live panel at **https://blhermes.zeabur.app** does **not** build from GitHub
directly. The image is built by **Google Cloud Build** from your local working
tree, pushed to **GHCR**, and pulled by Zeabur.

```
local checkout ──gcloud builds submit──▶ Cloud Build ──push──▶ ghcr.io/br41s/hermes-sandbox:latest ──pull──▶ Zeabur
```

The Dockerfile does `COPY . .` then `uv pip install -e .`, so the image bakes in
**whatever commit your local checkout is on at submit time** — not whatever is on
`main` in GitHub. This is the single most important thing to get right.

## Prerequisites

- `gcloud` authenticated against the project that owns the Cloud Build trigger.
- A GitHub PAT with **`write:packages`** scope for pushing to GHCR, passed as the
  `_GITHUB_TOKEN` substitution. (If you rotate secrets, this token may be one of
  them — use the current one.)
- `cloudbuild.yaml` in the repo root (untracked, kept locally alongside the clone).

## Deploy steps

```sh
cd /Users/brais/VSCODE/hermes-sandbox

# 1. Get the code you actually want to ship. THE FROZEN STEP — skipping this
#    ships a stale image even though main on GitHub is correct.
git checkout main
git pull --ff-only
git rev-parse --short HEAD            # note this SHA — it's what the image will contain

# 2. (Optional but recommended) verify the specific change is in the working tree,
#    e.g. for the auth-cache perf fix:
grep -c _VERIFIED_CACHE hermes_cli/dashboard_auth/middleware.py

# 3. Build + push via Cloud Build (uploads the working tree, builds, pushes to GHCR)
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_GITHUB_TOKEN=<ghcr-pat-with-write:packages>

# 4. Force Zeabur to pull the new image. The tag is :latest (same string, new
#    digest), so a plain Restart can keep serving the cached old image —
#    use Redeploy / force re-pull.
```

## Verifying the deploy landed

Zeabur's console → Hermes service → **Terminal**, then check the running code:

```sh
/opt/hermes/.venv/bin/python -c "import hermes_cli; print(hermes_cli.__version__)"
# spot-check a known change is present:
grep -c _VERIFIED_CACHE /opt/hermes/hermes_cli/dashboard_auth/middleware.py
```

Or from anywhere, hit the public liveness endpoint:

```sh
curl -s https://blhermes.zeabur.app/api/status   # auth_required, auth_providers, gateway_state
```

## Gotchas (each one has bitten us)

| Symptom | Cause | Fix |
|---------|-------|-----|
| New code not running after a "rebuild" | Local checkout was behind `main` → built a stale image | `git pull --ff-only` and verify the SHA **before** `gcloud builds submit` |
| Code still old after redeploy | Zeabur served the cached `:latest` digest | Use **Redeploy** (force re-pull), not Restart |
| "There's no redeploy button" | Restart ≠ Redeploy on Zeabur | Restart reuses the current image; Redeploy re-pulls. For new code you need a re-pull. |
| `docker login ghcr.io` fails in Cloud Build | `_GITHUB_TOKEN` expired / rotated | Pass a current PAT with `write:packages` |
| `curl https://<existing-domain>/health` hits the wrong service | An existing public domain (e.g. the dashboard's) is bound to a different container port than the one you're testing | Zeabur supports multiple public domains per service, each bound to its own container port — under Networking → Public, click **Generate Domain** again and pick the correct port instead of reusing an existing domain |
| A private port (e.g. a new `EXPOSE` in the Dockerfile) shows up under Networking → Private but isn't reachable from outside | Private ports are internal-only by default; nothing external routes to them until you bind a domain | Add a domain under the Public section for that specific port — Private and Public exposure are separate steps |
| No Redeploy button at all, and `zeabur service redeploy` CLI fails with `Cannot redeploy in-place ... bind a GitHub repository` | This service is `Template: PREBUILT_V2` (image-pull based, `GitTrigger: null`) — the generic redeploy command only applies to git-triggered builds | Force a re-pull with `npx zeabur@latest service update tag --id <service-id> --env-id <env-id> --tag latest -y` instead |

## Restart vs Redeploy vs Rebuild — what each does

- **Restart** — bounces the container on the **same image**. Picks up changes in
  the `/opt/data` persisted volume (e.g. `config.yaml` edits), **not** new code.
- **Redeploy** — re-pulls the image tag. Needed for new code shipped under the
  same `:latest` tag.
- **Rebuild (Cloud Build)** — produces a new image from your local source. Required
  for any code change; the two steps above only matter *after* this.
