# Profile-topic reliability: `exit=-15` kills, slow failover, model choice

Diagnosis + fixes for the profile-routed Telegram forum topics (finview =
thread 61, grow-shop = thread 3) that were "barely usable": replies taking
minutes, and intermittent hard failures with

```
Delegate subprocess produced no parseable result (exit=-15, profile 'finview')
```

## Architecture recap

A profile topic routes through the **delegate lane**, not the in-process agent:

```
gateway/platforms/base.py::_process_message_background  (auto_profile branch, ~L4050)
  → loop.run_in_executor(run_delegate_in_profile)        hermes_cli/delegate_core.py
      → subprocess.run([python -m hermes_cli.delegate_runner <task_id>])
          → run_delegate_agent()  → AIAgent.run_conversation()
```

The subprocess exists so each tenant's `HERMES_HOME` (session DB + memory) is
isolated without the web-server process having to mutate its own env per task.

## Root cause of `exit=-15` (SIGTERM)

`exit=-15` is a **negative** returncode, i.e. the child was killed by signal 15
(SIGTERM) — it did **not** exit on its own and never printed its result
sentinel, so `_parse_runner_output` returns `None` and the parent emits the
"no parseable result" string.

Walking the candidates:

| Candidate | Verdict |
|---|---|
| Parent's own `subprocess.run(timeout=…)` | **Ruled out.** `TimeoutExpired` is caught at `delegate_core.py` and returned as a clean `{"error": "timed out after …"}` — a different message, and the code never calls `proc.terminate()`. |
| Kernel OOM killer | **Ruled out as the -15 cause.** The Linux OOM killer sends **SIGKILL (-9)**, not SIGTERM (-15). An OOM would show `exit=-9`. |
| Gateway/container recycle reaping the child | **This is it.** |

The subprocess is spawned with `subprocess.run(...)` **without
`start_new_session=True`** (`delegate_core.py`, the `run_delegate_in_profile`
spawn). So it lives in the **same process group / session** as the gateway
process. When the gateway is recycled **mid-run**, the in-flight delegate child
is reaped along with it as SIGTERM:

- a **Zeabur redeploy / container restart** (SIGTERM to the container's
  processes during graceful stop),
- an **s6 / supervisor recycle** of the gateway service (see the orphan-gateway
  history — the gateway has been restarted by supervision before),
- the gateway's **own shutdown drain** (`gateway/shutdown_forensics.py`
  documents the gateway receiving SIGTERM and draining; any child mid-flight is
  collateral).

Why it shows up *often* rather than rarely: the primary model
(`openrouter/owl-alpha`, free + rate-limited) makes a single turn take
**minutes**. The longer each delegate runs, the wider the window in which any
restart lands on top of an in-flight child. The slow model and the `-15` kills
are the same problem viewed twice — fix the latency and the kill rate drops with
it.

### Fix applied

We cannot stop Zeabur/supervisor restarts from inside the app, and isolating the
child with `start_new_session=True` would just trade a clean kill for an
**orphan** subprocess that keeps burning model capacity after the gateway is
gone (and a container stop SIGKILLs everything regardless). So the fix makes the
kill **legible and recoverable** instead of cryptic:

`run_delegate_in_profile` now detects a negative returncode and returns a
graceful result (`_signal_kill_result`):

- **gateway lane** (`no_delegate_prompt=True`): a friendly, recoverable
  Spanish reply — *"La respuesta se interrumpió porque el servicio se reinició
  a mitad de la tarea. Vuelve a enviarme el mensaje y lo retomo."* — so the user
  knows to resend instead of staring at an internal error string.
- **orchestrator lane**: an English diagnostic naming the signal
  (`terminated by SIGTERM …`) so logs distinguish a restart (SIGTERM) from an
  OOM (SIGKILL) at a glance.

## Slow / flailing failover

`agent/conversation_loop.py` already fails over **eagerly** on a clean HTTP 429
or billing error (`is_rate_limited` → `try_activate_fallback`, `retry_count=0`,
no backoff). The latency came from the **non-429** face of saturation —
capacity 5xx, stalled streams, request timeouts — which go through the *generic*
retry path: up to `agent.api_max_retries` (**default 3**) attempts on the **same
saturated model** with `jittered_backoff(base_delay=2, max_delay=60)` *before*
the fallback is tried. On a saturated free model where each attempt itself
hangs, that is the "minutes" the user saw.

### Fix applied

`run_delegate_agent` now caps the retry-before-failover budget **for the
interactive lane only** (`resume_history=True` **and** a `fallback_model`
configured) to **1** attempt, env-overridable via
`HERMES_DELEGATE_INTERACTIVE_MAX_RETRIES`. A standing conversational thread with
a fallback wired doesn't benefit from grinding the primary three times — one
failed attempt is enough signal to switch to the paid fallback. Safety is
preserved: `try_recover_primary_transport` still grants one extra rebuilt-client
primary attempt for genuine TCP blips, and one-shot orchestrator delegations
(`resume_history=False`) keep the full default budget.

## Model choice — operator action recommended (not auto-applied)

> **RESOLVED 2026-09-20.** The recommendation below was carried out: the main
> chain moved off owl-alpha (via hy3 and gpt-5.6-luna) to
> `deepseek/deepseek-v4.1-flash`, and owl-alpha has since been retired from
> OpenRouter entirely. The analysis is kept as written — it is the record of
> why the move happened, not current configuration.

`openrouter/owl-alpha` is a **free, rate-limited** model. It is structurally
unfit as the *primary* for interactive topics: it saturates, which is the root
of both the latency and (via the wide in-flight window) the `-15` kills. The
above fixes make saturation **degrade faster and more gracefully**, but they do
not make a free model reliable.

**Recommendation:** point the primary at a reliable, **OpenRouter-reachable**
paid model. (HuggingFace egress is blocked on Zeabur — `router.huggingface.co`
is unreachable — so HF-hosted models are not an option for the live path.)

This is controlled by the **`HERMES_DEFAULT_MODEL`** env var, which the boot
hook reconciles into `model.default` (`docker/cont-init.d/03-biglobster-config`
§2). The repo `docker/config.yaml` default is intentionally **left unchanged**
here because prod is driven by the env var and the ideal slug/price should be
chosen by the operator.

### Operator steps (Zeabur — do this manually, then redeploy)

1. Set `HERMES_DEFAULT_MODEL` to a paid OpenRouter model slug you've confirmed
   is reachable and tool-call capable, e.g. a `deepseek/…`, `openai/…`,
   `anthropic/…`, or `x-ai/…` model. **Verify availability + pricing on
   OpenRouter before committing** — do not assume a slug.
2. Confirm `fallback_model` points at a *different* reliable paid model (the
   fallback chain dedups same provider+model, so primary ≠ fallback).
3. Optionally set `HERMES_DELEGATE_INTERACTIVE_MAX_RETRIES` (default `1`) if you
   want a touch more primary resilience before failover.
4. Redeploy so the boot hook re-reconciles `model.default`.

Trade-off: a paid primary costs per-token but removes the rate-limit wall, the
minutes-long waits, and most of the `-15` kills (shorter turns = narrower
restart window). For interactive customer-facing topics that is the right trade.

## Resume-history bloat (item 4 — confirmed already bounded)

The profile lane **does** bound the replayed transcript: `run_delegate_agent`
calls `_bounded_resume_history(full_history)` (default **80 000 chars** ≈ 20K
tokens, env `HERMES_DELEGATE_RESUME_CHARS`) and passes the *bounded* slice as
`conversation_history`. So a 669-message / ~277K-token topic is **not** replayed
in full; a recent, turn-boundary-aligned window is. Bounding shrinks the
**request size** (helping a rate-limited model), but it does **not** shrink
per-turn *model* latency — the saturated primary remains the dominant factor,
which the model-choice recommendation addresses.

## Summary of changes

- `hermes_cli/delegate_core.py`
  - `_signal_kill_result()` + signal-name map; `run_delegate_in_profile`
    returns a graceful, signal-named result on a negative returncode.
  - `run_delegate_agent` caps `api_max_retries` to 1 for the interactive lane
    (resume + fallback), env-overridable.
- `tests/hermes_cli/test_delegate_core_profile_cwd.py` — coverage for both.
- This doc.

Operator (manual, documented above, **not** applied by this PR):
`HERMES_DEFAULT_MODEL` off `openrouter/owl-alpha` onto a paid OpenRouter model;
optional `HERMES_DELEGATE_INTERACTIVE_MAX_RETRIES`.
