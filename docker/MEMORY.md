GitHub HTTPS auth: pre-configured at every boot via 03-biglobster-config. git credential.helper store is set, ~/.git-credentials contains x-access-token:$GITHUB_TOKEN, and git identity is "Hermes Agent <hermes@agent.local>". Standard git push/clone/fetch over HTTPS works out of the box — no manual setup needed. If it fails, check GITHUB_TOKEN is set in Zeabur Variables (restart required after adding it).
§
GITHUB_TOKEN is available as an env var (set via Zeabur) and in /opt/data/.env. Token is refreshed in .git-credentials on every boot, so a rotated token takes effect after a Zeabur restart. gh CLI is NOT installed — use git + curl with Authorization: token $GITHUB_TOKEN for GitHub API calls.
§
git identity must be configured before any commit: git config --global user.name "Hermes Agent" && git config --global user.email "hermes@agent.local". Check first with: git config --global user.name.
§
ping is not available in this container. Use curl for connectivity checks: curl -sf --max-time 5 https://github.com -o /dev/null && echo OK || echo FAIL
§
vision tool: AUXILIARY_VISION_MODEL env var sets the vision model. If the configured model does not support image input, vision fails with 404 and the tool auto-retries with the default backend (google/gemini-3-flash-preview via OpenRouter). Check AUXILIARY_VISION_MODEL value before debugging vision failures. (The original case was owl-alpha, text-only and since retired; the main model is now deepseek/deepseek-v4.1-flash, which does accept images — so a 404 here now points at AUXILIARY_VISION_MODEL itself, not at the main model.)
§
Memory limit is 6000 chars. When near-full, compact entries: merge related facts, drop stale context. Never let memory fill to capacity — future writes will be silently rejected.
§
Per-profile repo sync: each profile that has a repos.txt gets its declared GitHub repos cloned (depth 1) into profiles/<name>/workspace/ at every boot (git pull --ff-only on subsequent boots). grow-shop profile has workspace/grow-shop-api/ and workspace/grow-shop-landing/ auto-managed. To add repos for a new profile, create docker/profiles/<name>/repos.txt in the image and rebuild.
