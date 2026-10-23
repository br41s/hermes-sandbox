#!/usr/bin/env python3
"""agent_mailbox — profile-scoped inter-agent work-request queue for Hermes.

Cron-safe: stdlib-only, invoked BY PATH (never execute_code / python3 -c). One
committed home for the loop/race guards so they are NOT reimplemented per agent
prompt (where the loop/clobber hazard would spread).

The queue is a JSON array of request objects at --mailbox (a per-profile volume
path, OUTSIDE the clone). Sub-commands: enqueue, claim, inbox, done, expire-sweep.

Guards (the dangerous part — exercised by test_mailbox.py):
  - max-hop:   enqueue refuses when hops >= max-hops      (kills A->B->A loops)
  - dedupe:    enqueue refuses a duplicate LIVE request   (same content_hash)
  - TTL:       expire-sweep marks live requests past TTL as expired
  - page-lock: claim refuses when another LIVE request already holds the same url

Exit codes: 0 ok / no-op (dedupe), 3 max-hops, 4 not-found, 5 not-claimable,
6 page-locked. Non-zero lets the calling agent branch without parsing prose.
"""
import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone, timedelta, UTC

LIVE = ("open", "claimed")


def _now():
    return datetime.now(UTC)


def _parse_iso(s):
    return datetime.fromisoformat(str(s))


def _load(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(path, items):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)  # atomic


def _content_hash(frm, to, typ, url):
    return hashlib.sha256(f"{frm}|{to}|{typ}|{url}".encode()).hexdigest()[:16]


def _is_expired(req, now):
    try:
        created = _parse_iso(req["created"])
    except (KeyError, ValueError):
        return False
    return now > created + timedelta(days=int(req.get("ttl_days", 7)))


def _url_of(req):
    return (req.get("payload") or {}).get("url")


def cmd_enqueue(a):
    items = _load(a.mailbox)
    now = _now()
    chash = _content_hash(a.frm, a.to, a.type, a.url)
    for r in items:
        if r.get("content_hash") == chash and r.get("status") in LIVE and not _is_expired(r, now):
            print(f"DUPLICATE {r['id']}")  # dedupe guard
            return 0
    if a.hops >= a.max_hops:
        print(f"MAX_HOPS hops={a.hops} >= {a.max_hops}")  # loop guard
        return 3
    payload = json.loads(a.payload) if a.payload else {}
    payload.setdefault("url", a.url)
    req = {
        "id": str(uuid.uuid4()),
        "type": a.type, "from": a.frm, "to": a.to,
        "payload": payload,
        "hops": a.hops, "created": now.isoformat(), "ttl_days": a.ttl_days,
        "content_hash": chash, "status": "open",
        "claimed_by": None, "claimed_ts": None, "result_pr": None,
    }
    items.append(req)
    _save(a.mailbox, items)
    print(f"OK {req['id']}")
    return 0


def cmd_claim(a):
    items = _load(a.mailbox)
    now = _now()
    target = next((r for r in items if r["id"] == a.id), None)
    if target is None:
        print("NOT_FOUND")
        return 4
    if target["status"] != "open" or _is_expired(target, now):
        print(f"NOT_CLAIMABLE status={target['status']}")
        return 5
    url = _url_of(target)
    for r in items:  # page-lock: no two live requests edit the same url at once
        if r["id"] != target["id"] and _url_of(r) == url and r["status"] == "claimed" and not _is_expired(r, now):
            print(f"LOCKED url={url} by {r['claimed_by']}")
            return 6
    target["status"] = "claimed"
    target["claimed_by"] = a.agent
    target["claimed_ts"] = now.isoformat()
    _save(a.mailbox, items)
    print(f"OK {target['id']}")
    return 0


def cmd_inbox(a):
    items = _load(a.mailbox)
    now = _now()
    out = [r for r in items if r.get("to") == a.agent and r["status"] == "open" and not _is_expired(r, now)]
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_done(a):
    items = _load(a.mailbox)
    target = next((r for r in items if r["id"] == a.id), None)
    if target is None:
        print("NOT_FOUND")
        return 4
    target["status"] = "done"
    if a.pr:
        target["result_pr"] = a.pr
    _save(a.mailbox, items)
    print(f"OK {target['id']}")
    return 0


def cmd_expire(a):
    items = _load(a.mailbox)
    now = _now()
    n = 0
    for r in items:
        if r["status"] in LIVE and _is_expired(r, now):
            r["status"] = "expired"
            n += 1
    _save(a.mailbox, items)
    print(f"EXPIRED {n}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Hermes agent mailbox.")
    p.add_argument("--mailbox", required=True, help="Path to agent-mailbox.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enqueue")
    e.add_argument("--type", required=True)
    e.add_argument("--from", dest="frm", required=True)
    e.add_argument("--to", required=True)
    e.add_argument("--url", required=True)
    e.add_argument("--payload", help="JSON object string (merged into payload)")
    e.add_argument("--hops", type=int, default=1)
    e.add_argument("--max-hops", dest="max_hops", type=int, default=3)
    e.add_argument("--ttl-days", dest="ttl_days", type=int, default=7)
    e.set_defaults(func=cmd_enqueue)

    c = sub.add_parser("claim")
    c.add_argument("--id", required=True)
    c.add_argument("--agent", required=True)
    c.set_defaults(func=cmd_claim)

    i = sub.add_parser("inbox")
    i.add_argument("--agent", required=True)
    i.set_defaults(func=cmd_inbox)

    d = sub.add_parser("done")
    d.add_argument("--id", required=True)
    d.add_argument("--pr")
    d.set_defaults(func=cmd_done)

    x = sub.add_parser("expire-sweep")
    x.set_defaults(func=cmd_expire)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
