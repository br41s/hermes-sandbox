#!/usr/bin/env python3
"""Find indexed 404s: URLs Google has served in search that are now dead.

WHY THIS EXISTS: same reason as build_sitestate.py — the cron sandbox blocks
`execute_code` and `python3 -c/-e`, so the mechanical parts of detection
(diffing a historical page list against the current site, checking live HTTP
status, remembering what was dead last time) have to ship as a committed,
invokable script rather than inline code.

Division of labor with the agent: Google never shipped a bulk "index coverage"
export, so there is no single API call that returns "pages Google indexed that
now 404". The practical proxy is: a URL Search Console's Search Analytics has
served clicks/impressions for (proof it was indexed and shown) that no longer
exists on the current site. The agent already has a tool for the GSC half
(gsc_search_analytics) — it calls that itself and passes the result here as
--gsc-pages. This script does everything after that: diff against the current
site-state, confirm dead-ness with a live HTTP check, and require the SAME
URL to look dead on two separate runs before calling it confirmed, so a
transient outage during one scan can't get permanently redirected away.

Deterministic, stdlib-only except for the live HTTP check (urllib, same as
bl_site_health_tool.py's _fetch). No GSC/OAuth calls happen here — the
optional, quota-limited URL Inspection corroboration (gsc_inspect_url) is left
to the agent, on the handful of top candidates this script surfaces.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REQUEST_TIMEOUT = 20
USER_AGENT = "hermes-onsite-seo-dead-url-check/1"
# Only these mean "the content is actually gone". A 5xx or a transport failure
# is evidence of an outage, not of removal, and must not count toward the
# two-run confirmation — that would redirect away a page that's just down.
DEAD_STATUSES = (404, 410)
HISTORY_KEEP_DAYS = 60


def _fetch_status(url: str) -> dict:
    """One GET, following redirects. Never raises — records status 0 on any
    transport failure, matching bl_site_health_tool.py's `_fetch` convention."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return {"status": resp.status, "final_url": resp.geturl()}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "final_url": url}
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
        return {"status": 0, "final_url": url, "error": str(getattr(exc, "reason", exc))}


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json_atomic(path: str, data) -> None:
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _gsc_rows_to_pages(gsc_response: dict) -> list[dict]:
    """Rows of a `dimensions=["page"]` searchAnalytics response → a flat list.

    Tolerant of an empty/absent "rows" key (no historical data is a valid,
    boring answer, not an error).
    """
    out = []
    for row in gsc_response.get("rows", []) or []:
        keys = row.get("keys") or []
        if not keys:
            continue
        out.append({
            "url": keys[0],
            "clicks": row.get("clicks", 0),
            "impressions": row.get("impressions", 0),
        })
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="Find indexed URLs that are now dead.")
    p.add_argument("--gsc-pages", required=True,
                    help="Path to the raw JSON response of a gsc_search_analytics call "
                         "with dimensions=['page'].")
    p.add_argument("--site-state", required=True,
                    help="Path to site-state.json (built by build_sitestate.py) — its "
                         "internal_link_graph keys are the current, live URL set.")
    p.add_argument("--history", required=True,
                    help="Path to this script's own small history file (created if absent). "
                         "Used to require two separate runs before calling a URL confirmed dead.")
    p.add_argument("--out", required=True, help="Output candidates JSON path.")
    p.add_argument("--min-impressions", type=int, default=1,
                    help="Ignore historical pages with fewer impressions than this (default 1).")
    p.add_argument("--cap", type=int, default=50,
                    help="Max historical pages to live-check per run, ranked by clicks then "
                         "impressions (default 50) — bounds one run's HTTP traffic.")
    args = p.parse_args(argv)

    gsc_response = _load_json(args.gsc_pages, {})
    site_state = _load_json(args.site_state, {})
    history = _load_json(args.history, {"dead_urls": {}})
    history.setdefault("dead_urls", {})

    current_urls = set((site_state.get("internal_link_graph") or {}).keys())

    pages = _gsc_rows_to_pages(gsc_response)
    pages = [pg for pg in pages if pg["impressions"] >= args.min_impressions]
    # A URL still on the current site is not a candidate at all — cheapest,
    # zero-HTTP-call filter, applied before ranking so the cap isn't wasted on
    # URLs that were never going to be checked.
    full_candidates = [pg for pg in pages if pg["url"] not in current_urls]
    full_candidates.sort(key=lambda pg: (pg["clicks"], pg["impressions"]), reverse=True)
    candidates = full_candidates[: args.cap]
    list_capped = len(full_candidates) > args.cap

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    confirmed_dead = []
    pending_confirmation = []
    now_alive = []
    inconclusive = []

    for pg in candidates:
        url = pg["url"]
        result = _fetch_status(url)
        status = result["status"]
        prior = history["dead_urls"].get(url)

        if status in DEAD_STATUSES:
            record = {**pg, "status": status, "checked_at": now_iso}
            if prior and prior.get("last_status") in DEAD_STATUSES:
                record["first_seen_dead"] = prior.get("first_seen_dead", now_iso)
                confirmed_dead.append(record)
            else:
                record["first_seen_dead"] = now_iso
                pending_confirmation.append(record)
            history["dead_urls"][url] = {
                "last_status": status,
                "last_checked": now_iso,
                "first_seen_dead": record["first_seen_dead"],
            }
        elif status == 200:
            now_alive.append({**pg, "status": status})
            history["dead_urls"].pop(url, None)
        else:
            # 5xx or transport failure (status 0): outage, not removal. Do not
            # advance or reset the two-run counter — leave history exactly as
            # it was so a flaky check can't either confirm a false redirect or
            # erase a real one that's mid-confirmation.
            inconclusive.append({**pg, "status": status, "error": result.get("error")})

    # Prune history entries older than HISTORY_KEEP_DAYS with no current-run
    # match, so a URL fixed weeks ago doesn't linger forever.
    cutoff = now.timestamp() - HISTORY_KEEP_DAYS * 86400
    pruned = {}
    for url, rec in history["dead_urls"].items():
        try:
            last = datetime.fromisoformat(rec["last_checked"]).timestamp()
        except (KeyError, ValueError):
            continue
        if last >= cutoff:
            pruned[url] = rec
    history["dead_urls"] = pruned
    _save_json_atomic(args.history, history)

    output = {
        "updated": now_iso,
        "checked": len(candidates),
        "cap": args.cap,
        "list_capped": list_capped,
        # Eligible for the write path — dead on this run AND on a prior run.
        "confirmed_dead": confirmed_dead,
        # Dead just now — one more confirming run required before acting.
        "pending_confirmation": pending_confirmation,
        # Sanity-check output: pages that looked gone in GSC but resolve fine.
        "now_alive": now_alive,
        "inconclusive": inconclusive,
    }
    _save_json_atomic(args.out, output)

    print(f"dead-url scan written: {args.out}")
    print(
        f"candidates={len(candidates)} confirmed_dead={len(confirmed_dead)} "
        f"pending={len(pending_confirmation)} inconclusive={len(inconclusive)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
