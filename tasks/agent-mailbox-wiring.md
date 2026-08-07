# agent_mailbox wiring — emitter (SEO) + consumer (gap-hunter)

Builds on the v1 foundation (`onsite-seo/mailbox.py`, merged #92). One additive
handoff: SEO flags a thin article → gap-hunter (`ce583d11dedd`) expands it → PR →
auditor. Reactive via the `cronjob` trigger, with an async fallback.

Paths (per-profile, outside the clone):
- BUZÓN  `/opt/data/profiles/biglobster/agent-mailbox.json`
- HELPER `/opt/data/profiles/biglobster/mailbox.py`
- site-state now carries `word_count` per page (added to build_sitestate.py).

---

## A) Emitter — already in `onsite-seo/seo-agent.prompt`
Section `[HANDOFF INTER-AGENTE — EMISOR]`. Splice it into the live SEO `jobs.json`
prompt (after `[ESTADO DEL SITIO]`). Logic: expire-sweep → list article URLs with
word_count < 400 → enqueue (≤2/cycle, helper dedupes) → trigger job `ce583d11dedd`
(fallback: leave queued + alert) → report a "🤝 Handoffs" section.

Also re-deploy the updated `build_sitestate.py` (it now emits `word_count`) — see (C).

## B) Consumer — splice into the gap-hunter `jobs.json` prompt (volume; no repo copy)

Add this block at the START of the gap-hunter's run, before its normal work:

```
**[BUZÓN INTER-AGENTE — CONSUMIDOR]**

Al inicio de cada ejecución, antes de tu trabajo normal, revisa tu bandeja y atiende
UNA petición si la hay. NO uses execute_code ni python3 -c/-e; invoca el helper POR RUTA.

  BUZÓN  : /opt/data/profiles/biglobster/agent-mailbox.json
  HELPER : /opt/data/profiles/biglobster/mailbox.py

1. Lee tu bandeja (array JSON de peticiones abiertas dirigidas a ti):
     python3 /opt/data/profiles/biglobster/mailbox.py --mailbox <BUZÓN> inbox --agent content
2. Si hay alguna, toma la más antigua e intenta reclamarla (lock por-página):
     python3 .../mailbox.py --mailbox <BUZÓN> claim --id <ID> --agent content
   Si imprime LOCKED (exit 6): otra petición trabaja esa URL — sáltala, prueba la
   siguiente. Si OK: es tuya.
3. Atiende según `type`:
   - expand_thin_content: abre payload.url → web/blog/<slug>.html, amplía el contenido
     con tu proceso normal (respeta tono y restricciones del sitio).
4. Abre un PR con tu flujo normal (gh pr create → lo revisa el auditor). NUNCA hagas
   push directo a main para trabajo de buzón.
5. Cierra la petición con el enlace del PR:
     python3 .../mailbox.py --mailbox <BUZÓN> done --id <ID> --pr <URL_DEL_PR>
6. UNA petición por ejecución. Si reclamar/atender falla, deja la petición abierta
   (no la marques done) y repórtalo.
```

## C) Deploy on the volume (as root, then chown)

1. Install `mailbox.py` at `/opt/data/profiles/biglobster/mailbox.py` (base64 from the
   assistant message), then `chown hermes:hermes`.
2. Re-install the UPDATED `build_sitestate.py` (now emits word_count) at
   `/opt/data/profiles/biglobster/seo/build_sitestate.py`, `chown hermes:hermes`.
3. The mailbox file auto-creates on first enqueue; pre-create + chown the profile dir
   if needed so the agent (hermes) owns it.

## D) Dry-run (verify the full path without waiting for a naturally-thin page)

The site is well-templated, so a genuinely <400-word article may not exist. To exercise
the CONSUMER path now, manually enqueue one request, then trigger the gap-hunter:

```bash
python3 /opt/data/profiles/biglobster/mailbox.py \
  --mailbox /opt/data/profiles/biglobster/agent-mailbox.json \
  enqueue --type expand_thin_content --from onsite-seo --to content \
  --url https://biglobster.top/es/blog/<a-real-shortish-article>.html \
  --payload '{"word_count": 320, "reason": "dry-run"}'
chown hermes:hermes /opt/data/profiles/biglobster/agent-mailbox.json
```
Then trigger job `ce583d11dedd` from the panel and read the trace. Verify:
- gap-hunter `inbox` returns the request → `claim` OK (status=claimed)
- it expands the page and opens a PR (auditor-gated) — confirms (a)
- `done` sets status=done + result_pr
- re-running the emitter produces NO duplicate (dedupe), and two agents never touch the
  same file (lock).
- Whether SEO's `cronjob` trigger worked or fell back — confirms (b).

## Verify after applying
- site-state.json entries now include `word_count`.
- `agent-mailbox.json` shows the request lifecycle open→claimed→done.
- gap-hunter PR appears; SEO did NOT edit the article itself.
