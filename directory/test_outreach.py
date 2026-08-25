#!/usr/bin/env python3
"""Tests for outreach.py guards. Run: python3 directory/test_outreach.py

Exercises the guards that make unsolicited outreach defensible: one-message-per-
business, permanent suppression, the trailing-24h rate cap, the bounce halt, the
compliance headers/footer, and the refusal to treat a corrupt ledger as an empty
one. Stdlib-only, no pytest dependency — drives outreach.main(argv), the same
code path the agent uses via the CLI.

SMTP is stubbed: no test ever opens a socket.
"""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("outreach", os.path.join(HERE, "outreach.py"))
ou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ou)


# --- SMTP stub --------------------------------------------------------------

class _FakeSMTP:
    sent = []
    logins = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, *a, **kw):
        pass

    def login(self, user, password):
        _FakeSMTP.logins.append(user)

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


class _FailingSMTP(_FakeSMTP):
    def send_message(self, msg):
        raise OSError("connection reset")


def _install_smtp(cls=_FakeSMTP):
    ou.smtplib.SMTP = cls
    ou.smtplib.SMTP_SSL = cls
    cls.sent = []
    cls.logins = []


def _env(**over):
    base = {
        "DIRECTORY_FROM_EMAIL": "hola@biglobster.top",
        "DIRECTORY_FROM_NAME": "BigLobster",
        "BREVO_SMTP_USER": "smtp-user",
        "BREVO_SMTP_PASSWORD": "smtp-pass",
    }
    base.update(over)
    for k in ("DIRECTORY_FROM_EMAIL", "DIRECTORY_FROM_NAME", "BREVO_SMTP_USER",
              "BREVO_SMTP_PASSWORD", "DIRECTORY_UNSUBSCRIBE_EMAIL",
              "DIRECTORY_POSTAL_ADDRESS"):
        os.environ.pop(k, None)
    for k, v in base.items():
        if v is not None:
            os.environ[k] = v


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _write(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def run(ledger, *args, max_per_day=None):
    argv = ["--ledger", ledger]
    if max_per_day is not None:
        argv += ["--max-per-day", str(max_per_day)]
    return ou.main(argv + list(args))


def send_args(body, to="hi@acme.example", host=None, page="https://biglobster.top/directory/best-x-in-us.html"):
    a = ["send", "--to", to, "--subject", "You are listed", "--body-file", body,
         "--page-url", page, "--set-name", "us-x"]
    if host:
        a += ["--host", host]
    return a


SET_FIXTURE = {
    "country": "us", "countryName": "United States",
    "type": "law-firms", "typeName": "Law Firms", "lang": "en",
    "updated": "2026-08-25", "intro": "x",
    "listings": [
        {"name": "Acme", "url": "https://acme.example", "city": "NY", "summary": "s"},
        {"name": "Beta", "url": "https://www.beta.example", "city": "LA", "summary": "s"},
        {"name": "Gamma", "url": "https://gamma.example", "city": "TX", "summary": "s"},
    ],
}


def test():
    d = tempfile.mkdtemp()
    led = os.path.join(d, "directory-outreach.json")
    body = os.path.join(d, "body.txt")
    setfile = os.path.join(d, "us-law-firms.json")
    with open(body, "w", encoding="utf-8") as f:
        f.write("Hi - you are listed on our free directory page.")
    _write(setfile, SET_FIXTURE)

    # --- host normalisation must agree with site/_data/directory.js hostOf()
    assert ou.norm_host("https://www.Acme.example/contact?x=1") == "acme.example"
    assert ou.norm_host("hi@Acme.example") == "acme.example"
    assert ou.norm_host("acme.example:443") == "acme.example"

    # --- dry-run sends nothing and writes no ledger entry
    _env()
    _install_smtp()
    assert run(led, *send_args(body), "--dry-run") == 0
    assert _FakeSMTP.sent == []
    assert not os.path.exists(led), "dry-run must not create a ledger"

    # --- a real send records the business and actually hands SMTP a message
    assert run(led, *send_args(body)) == 0
    assert len(_FakeSMTP.sent) == 1
    msg = _FakeSMTP.sent[0]
    text = msg.get_content()
    # compliance surface, injected by the helper and not by the model
    assert msg["List-Unsubscribe"] == "<mailto:hola@biglobster.top?subject=unsubscribe>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "Albuquerque" in text, "postal address (CAN-SPAM) missing"
    assert "biglobster.top/directory/best-x-in-us.html" in text, "listing URL missing"
    assert "legitimate interest" in text
    assert "one-off message" in text, "must state there is no follow-up"
    led_data = _load(led)
    assert list(led_data["contacted"]) == ["acme.example"]
    assert led_data["contacted"]["acme.example"]["status"] == "sent"

    # --- one message per business, ever
    assert run(led, *send_args(body)) == 3
    assert len(_FakeSMTP.sent) == 1, "second send must not reach SMTP"
    # ...including via a different address at the same domain
    assert run(led, *send_args(body, to="legal@acme.example")) == 3
    assert len(_FakeSMTP.sent) == 1

    # --- Spanish footer variant
    assert run(led, *send_args(body, to="hola@beta.example"), "--lang", "es") == 0
    es = _FakeSMTP.sent[-1].get_content()
    assert "interes legitimo" in es and "Albuquerque" in es

    # --- suppression is permanent and beats everything else
    assert run(led, "suppress", "--host", "gamma.example", "--reason", "asked") == 0
    assert run(led, *send_args(body, to="hi@gamma.example")) == 4
    assert len(_FakeSMTP.sent) == 2
    # an address-level suppression also blocks its domain
    assert run(led, "suppress", "--email", "Owner@Delta.example") == 0
    assert run(led, *send_args(body, to="owner@delta.example")) == 4

    # --- a bounce auto-suppresses, so a dead address is never retried
    assert run(led, "record", "--host", "acme.example", "--status", "bounced") == 0
    assert "acme.example" in _load(led)["suppressed"]
    assert run(led, "record", "--host", "nobody.example", "--status", "replied") == 4

    # --- trailing-24h rate cap
    cap_led = os.path.join(d, "cap.json")
    _env()
    _install_smtp()
    assert run(cap_led, *send_args(body, to="a@one.example"), max_per_day=2) == 0
    assert run(cap_led, *send_args(body, to="a@two.example"), max_per_day=2) == 0
    assert run(cap_led, *send_args(body, to="a@three.example"), max_per_day=2) == 5
    assert len(_FakeSMTP.sent) == 2
    # a send older than 24h no longer counts against the window
    data = _load(cap_led)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    data["contacted"]["one.example"]["sent_at"] = old
    _write(cap_led, data)
    assert run(cap_led, *send_args(body, to="a@three.example"), max_per_day=2) == 0

    # --- bounce halt: past the threshold, nothing sends at all
    halt_led = os.path.join(d, "halt.json")
    now = datetime.now(timezone.utc).isoformat()
    contacted = {}
    for i in range(30):
        contacted[f"h{i}.example"] = {
            "email": f"a@h{i}.example", "sent_at": now,
            "status": "bounced" if i < 20 else "sent",
        }
    _write(halt_led, {"version": 1, "contacted": contacted, "suppressed": {}})
    _install_smtp()
    assert run(halt_led, *send_args(body, to="a@fresh.example"), max_per_day=999) == 6
    assert _FakeSMTP.sent == []
    assert run(halt_led, "stats") == 6
    assert run(halt_led, "candidates", "--set", setfile) == 6

    # --- missing credentials fail closed, and never name which value was set
    _env(BREVO_SMTP_PASSWORD=None)
    _install_smtp()
    assert run(os.path.join(d, "nc.json"), *send_args(body, to="a@nc.example")) == 7
    assert _FakeSMTP.sent == []
    _env(DIRECTORY_FROM_EMAIL=None)
    assert run(os.path.join(d, "nc.json"), *send_args(body, to="a@nc.example")) == 7

    # --- an SMTP failure must not record the business as contacted
    _env()
    _install_smtp(_FailingSMTP)
    fail_led = os.path.join(d, "fail.json")
    assert run(fail_led, *send_args(body, to="a@flaky.example")) == 8
    assert not os.path.exists(fail_led) or "flaky.example" not in _load(fail_led)["contacted"]

    # --- a corrupt ledger must NOT read as "nobody contacted yet"
    bad = os.path.join(d, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write('{"contacted": {"a.example"')
    _install_smtp()
    for argv in (send_args(body, to="a@a.example"), ["stats"]):
        try:
            run(bad, *argv)
            raise AssertionError("corrupt ledger must abort, not silently reset")
        except SystemExit as e:
            assert "LEDGER_UNREADABLE" in str(e)
    assert _FakeSMTP.sent == []

    # --- candidates: skips contacted and suppressed, honours www-stripping
    cand_led = os.path.join(d, "cand.json")
    _write(cand_led, {
        "version": 1,
        "contacted": {"acme.example": {"email": "x@acme.example", "sent_at": now, "status": "sent"}},
        "suppressed": {"beta.example": {"reason": "asked", "ts": now}},
    })
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert run(cand_led, "candidates", "--set", setfile) == 0
    cands = json.loads(buf.getvalue())
    # acme was contacted, beta is suppressed (and listed as www.beta.example in
    # the set, so this also proves the host key is normalised on both sides)
    assert [c["host"] for c in cands] == ["gamma.example"], cands

    # --- the cap bounds how many candidates are offered at once: this ledger
    # already holds one send inside the window, so a cap of 2 leaves room for 1
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert run(cand_led, "candidates", "--set", setfile, "--limit", "10", max_per_day=2) == 0
    assert len(json.loads(buf.getvalue())) == 1
    # ...and once the window is full there are no candidates at all
    assert run(cand_led, "candidates", "--set", setfile, max_per_day=1) == 5

    print("all outreach guard tests passed")


if __name__ == "__main__":
    test()
