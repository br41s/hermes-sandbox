#!/usr/bin/env python3
"""Drive a bl-site-package client's Liderpapel catalogue sync from Hermes.

bl-site-package schedules its own sync in-process with node-cron
(``src/sync/liderpapel/scheduler.js``, 06:00 daily). That works on Zeabur,
where the Node process is long-lived, and does not work on Passenger, which
shuts application processes down when no request has arrived for a few
minutes — so on a low-traffic client site the timer is simply never alive at
06:00. Observed on Shoroban: the catalogue was seeded 2026-08-19 13:27 and had
not refreshed since, meaning the shop was quoting prices and stock from a
frozen snapshot, with `Prices` published every 12 h and `Stocks` every 10 min
upstream.

The schedule therefore has to live outside the client's web process. This is
that: a ``no_agent`` cron script, one HTTP call the client's own panel already
exposes, no model in the loop. Run it per profile — credentials resolve from
whichever profile the job runs under, through ``bl_site_publish_tool``'s
helpers rather than a second copy, so a client's URL and password can never be
resolved two different ways.

Silent when the sync succeeds: Hermes delivers a ``no_agent`` job's stdout, and
an empty stdout is no delivery. A failure, a stall, or a sync that reports
success without advancing prints a report and exits non-zero.

Usage (as run by cron)::

    python3 scripts/bl_site_liderpapel_sync.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

from tools.bl_site_publish_tool import _get_jwt, _get_site_credentials

# POST /api/sync/liderpapel/run is synchronous: the handler awaits the whole
# sync — sFTP download of ~175 MB, parse, upsert, Eleventy rebuild — before it
# answers. On a client's shared host that can outlast any sane socket timeout,
# and a timeout here says nothing about whether the sync failed, because it
# carries on server-side regardless. So the POST is best-effort and the real
# answer always comes from polling status.
RUN_TIMEOUT = 600
STATUS_TIMEOUT = 30
POLL_INTERVAL = 20
POLL_DEADLINE = 1800  # 30 min. Shoroban's full sync is ~14.5k products.


def _request(method: str, url: str, token: str, timeout: int) -> Optional[dict]:
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _status(site_url: str, token: str) -> dict:
    return _request("GET", f"{site_url}/api/sync/liderpapel/status", token, STATUS_TIMEOUT) or {}


def _fail(*lines: str) -> int:
    for line in lines:
        print(line)
    return 1


def main() -> int:
    site_url, password = _get_site_credentials()
    if not site_url or not password:
        return _fail(
            "⚠️ Sync de Liderpapel no ejecutado: este perfil no tiene "
            "BL_SITE_URL y BL_SITE_PANEL_PASSWORD configurados."
        )

    try:
        token = _get_jwt(site_url, password)
    except Exception as err:  # noqa: BLE001 — any failure here is worth reporting verbatim
        return _fail(f"⚠️ No se pudo autenticar contra {site_url}: {err}")

    try:
        before = _status(site_url, token)
    except Exception as err:  # noqa: BLE001
        return _fail(f"⚠️ No se pudo leer el estado del sync en {site_url}: {err}")

    if not before.get("hasPassword") or not before.get("supplierCode"):
        return _fail(
            f"⚠️ {site_url} no tiene la conexión con Liderpapel configurada "
            "(faltan credenciales sFTP o el código de proveedor). El sync no "
            "puede ejecutarse hasta que se rellenen en el panel."
        )

    previous_sync_at = before.get("lastSyncAt")

    try:
        _request("POST", f"{site_url}/api/sync/liderpapel/run", token, RUN_TIMEOUT)
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        return _fail(f"⚠️ El sync de Liderpapel falló en {site_url}: HTTP {err.code} — {detail}")
    except Exception:  # noqa: BLE001
        # Timeout or dropped connection. The sync keeps running server-side, so
        # this is not an outcome — fall through to polling for the real one.
        pass

    deadline = time.monotonic() + POLL_DEADLINE
    while True:
        try:
            current = _status(site_url, token)
        except Exception as err:  # noqa: BLE001
            return _fail(f"⚠️ Se perdió el contacto con {site_url} durante el sync: {err}")

        state = current.get("lastSyncStatus")
        if state != "running":
            break
        if time.monotonic() > deadline:
            return _fail(
                f"⚠️ El sync de Liderpapel en {site_url} lleva más de "
                f"{POLL_DEADLINE // 60} minutos en ejecución. Sigue marcado como "
                "'running'; revisa el estado en el panel."
            )
        time.sleep(POLL_INTERVAL)

    if current.get("lastSyncStatus") != "ok":
        return _fail(
            f"⚠️ El sync de Liderpapel falló en {site_url}: "
            f"{current.get('lastSyncMessage') or 'sin mensaje de error'}"
        )

    # "ok" with an unchanged timestamp means we are reading the *previous* run's
    # result: this one never started, or died without recording anything.
    if current.get("lastSyncAt") == previous_sync_at:
        return _fail(
            f"⚠️ El sync de Liderpapel en {site_url} terminó sin registrar una "
            f"ejecución nueva (sigue en {previous_sync_at}). El catálogo no se "
            "ha actualizado."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
