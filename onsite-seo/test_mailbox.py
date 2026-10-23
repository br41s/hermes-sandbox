#!/usr/bin/env python3
"""Tests for mailbox.py guards. Run: python3 onsite-seo/test_mailbox.py

Exercises the four safety-critical guards: dedupe, max-hop, page-lock, TTL.
Stdlib-only, no pytest dependency — calls mailbox.main(argv) and inspects the
queue file, same code path the agents drive via the CLI.
"""
import importlib.util
import json
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("mailbox", os.path.join(HERE, "mailbox.py"))
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def run(mbx, *args):
    return mb.main(["--mailbox", mbx, *args])


def test():
    d = tempfile.mkdtemp()
    mbx = os.path.join(d, "agent-mailbox.json")

    # enqueue
    assert run(mbx, "enqueue", "--type", "expand_thin_content", "--from", "onsite-seo",
               "--to", "content", "--url", "https://x/a.html") == 0
    items = _load(mbx)
    assert len(items) == 1, items
    rid = items[0]["id"]
    assert items[0]["status"] == "open"

    # dedupe: identical from/to/type/url -> no new request
    assert run(mbx, "enqueue", "--type", "expand_thin_content", "--from", "onsite-seo",
               "--to", "content", "--url", "https://x/a.html") == 0
    assert len(_load(mbx)) == 1, "dedupe failed"

    # max-hop: hops >= max -> rejected, nothing appended
    assert run(mbx, "enqueue", "--type", "t", "--from", "a", "--to", "b",
               "--url", "https://x/b.html", "--hops", "3", "--max-hops", "3") == 3
    assert len(_load(mbx)) == 1, "max-hop should not append"

    # claim
    assert run(mbx, "claim", "--id", rid, "--agent", "content") == 0
    assert _load(mbx)[0]["status"] == "claimed"

    # page-lock: a second LIVE request on the same url cannot be claimed
    assert run(mbx, "enqueue", "--type", "other", "--from", "x", "--to", "content",
               "--url", "https://x/a.html") == 0
    rid2 = next(r for r in _load(mbx) if r["id"] != rid)["id"]
    assert run(mbx, "claim", "--id", rid2, "--agent", "content") == 6, "page-lock failed"

    # inbox: rid2 still open and addressed to content
    assert any(r["id"] == rid2 for r in json.loads(_capture(mbx, "inbox", "--agent", "content")))

    # done
    assert run(mbx, "done", "--id", rid, "--pr", "https://gh/pr/1") == 0
    t = next(r for r in _load(mbx) if r["id"] == rid)
    assert t["status"] == "done" and t["result_pr"].endswith("/1")

    # TTL: an old open request gets expired by the sweep
    items = _load(mbx)
    items.append({"id": "old", "type": "t", "from": "a", "to": "content",
                  "payload": {"url": "https://x/c.html"}, "hops": 1,
                  "created": "2020-01-01T00:00:00+00:00", "ttl_days": 7,
                  "content_hash": "zzz", "status": "open",
                  "claimed_by": None, "claimed_ts": None, "result_pr": None})
    with open(mbx, "w", encoding="utf-8") as f:
        json.dump(items, f)
    assert run(mbx, "expire-sweep") == 0
    assert next(r for r in _load(mbx) if r["id"] == "old")["status"] == "expired", "TTL sweep failed"

    print("ALL TESTS PASSED")


def _capture(mbx, *args):
    """Run inbox and capture stdout (the only command whose output we assert on)."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run(mbx, *args)
    return buf.getvalue()


if __name__ == "__main__":
    test()
