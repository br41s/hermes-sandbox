#!/usr/bin/env python3
"""directory_outreach — profile-scoped outreach ledger + sender for the Hermes
business-directory agent.

Cron-safe: stdlib-only, invoked BY PATH (never execute_code / python3 -c). Same
trust model as onsite-seo/mailbox.py — the dangerous guards live HERE, in a
committed and reviewable file, not in prompt prose that a model can reason its
way around.

WHY THE GUARDS ARE IN PYTHON: this sends unsolicited B2B email. The rules that
keep that lawful and non-spammy (one message per business ever, permanent
suppression, an unsubscribe path, a postal address, a hard stop when bounces
spike) are not style preferences — they are the reason the mechanism is
defensible at all. An instruction in a .prompt is advisory; a non-zero exit is
not.

The ledger is a JSON object at --ledger, on the profile volume OUTSIDE the site
clone. It holds real email addresses, so it must never be written into the
public biglobster repo — the directory JSON there carries `contactPage` URLs
only.

Sub-commands: candidates, send, record, suppress, unsuppress, stats.

Exit codes: 0 ok / 3 already-contacted / 4 suppressed / 5 rate-cap reached
/ 6 halted on bounce rate / 7 missing SMTP credentials / 8 send failed
/ 4 not-found (suppress/record on unknown host). Non-zero lets the calling
agent branch without parsing prose.
"""
import argparse
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

# --- policy constants -------------------------------------------------------
# Deliberately conservative. Raising these is a decision for a human, in a
# commit, not something an agent can pass as a flag on a bad day.
DEFAULT_MAX_PER_DAY = 15          # sends allowed in any trailing 24h window
BOUNCE_HALT_MIN_SENDS = 25        # don't judge a bounce rate on a tiny sample
BOUNCE_HALT_RATE = 0.35           # above this, stop sending entirely
MAX_BODY_BYTES = 20000

DEFAULT_POSTAL = "Biglobster LLC, 1209 Mountain Road Pl NE Ste N, Albuquerque, NM 87110, USA"
DEFAULT_SMTP_HOST = "smtp-relay.brevo.com"
DEFAULT_SMTP_PORT = 587

TERMINAL = ("bounced", "replied", "linked", "declined")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now():
    return datetime.now(timezone.utc)


def _parse_iso(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _blank():
    return {"version": 1, "contacted": {}, "suppressed": {}}


def _load(path):
    if not os.path.exists(path):
        return _blank()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        # A corrupt ledger must NOT read as "nobody has been contacted" — that
        # would re-mail every business on the list. Refuse loudly instead.
        raise SystemExit(f"LEDGER_UNREADABLE {path} — refusing to run")
    if not isinstance(data, dict) or "contacted" not in data:
        raise SystemExit(f"LEDGER_MALFORMED {path} — refusing to run")
    data.setdefault("suppressed", {})
    return data


def _save(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)  # atomic


def norm_host(value):
    """Registrable-ish host key. Must match site/_data/directory.js hostOf()."""
    v = str(value or "").strip().lower()
    v = re.sub(r"^https?://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0]
    if "@" in v:
        v = v.split("@", 1)[1]
    v = v.split(":")[0]
    return v[4:] if v.startswith("www.") else v


def _is_suppressed(led, host, email=None):
    if host in led["suppressed"]:
        return host
    if email:
        e = email.strip().lower()
        if e in led["suppressed"]:
            return e
        if norm_host(e) in led["suppressed"]:
            return norm_host(e)
    return None


def _sends_since(led, since):
    n = 0
    for rec in led["contacted"].values():
        try:
            if _parse_iso(rec["sent_at"]) >= since:
                n += 1
        except (KeyError, ValueError):
            continue
    return n


def _bounce_state(led):
    total = len(led["contacted"])
    bounced = sum(1 for r in led["contacted"].values() if r.get("status") == "bounced")
    rate = (bounced / total) if total else 0.0
    halted = total >= BOUNCE_HALT_MIN_SENDS and rate > BOUNCE_HALT_RATE
    return total, bounced, rate, halted


# --- commands ---------------------------------------------------------------

def cmd_candidates(a):
    """Listings from a directory set that may still be contacted.

    Reads the PUBLIC set file (which has no email addresses) and filters it
    against the private ledger. The agent finds an address from each listing's
    own site; this only says who is still eligible.
    """
    led = _load(a.ledger)
    try:
        with open(a.set, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as err:
        print(f"SET_UNREADABLE {err}")
        return 4

    _, _, _, halted = _bounce_state(led)
    if halted:
        print("HALTED bounce rate above threshold — run `stats` and fix before sending")
        return 6

    remaining = max(0, a.max_per_day - _sends_since(led, _now() - timedelta(days=1)))
    if remaining == 0:
        print("RATE_CAP 0 sends left in the trailing 24h window")
        return 5

    out = []
    for entry in data.get("listings", []):
        host = norm_host(entry.get("url"))
        if not host or host in led["contacted"] or _is_suppressed(led, host):
            continue
        out.append({
            "name": entry.get("name"),
            "host": host,
            "url": entry.get("url"),
            "contactPage": entry.get("contactPage"),
            "city": entry.get("city"),
        })
        if len(out) >= min(a.limit, remaining):
            break

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def _footer(lang, postal, unsub_to, page_url):
    """Compliance footer. Appended by the helper, never by the model, so it
    cannot be paraphrased away: CAN-SPAM wants a real postal address and a
    working opt-out; LSSI-CE/GDPR want the basis and a route to object."""
    if lang == "es":
        return (
            "\n\n---\n"
            f"Apareces en este listado publico y gratuito: {page_url}\n"
            "Te escribimos una sola vez. No hay seguimiento ni recordatorios.\n"
            f"Para corregir tus datos o salir del listado, responde a este correo o escribe a {unsub_to}.\n"
            "Tratamos tu direccion profesional por interes legitimo (art. 6.1.f RGPD) y la borramos si lo pides.\n"
            "Politica de privacidad: https://biglobster.top/es/privacidad.html\n"
            f"{postal}\n"
        )
    return (
        "\n\n---\n"
        f"You are listed on this free, public page: {page_url}\n"
        "This is a one-off message. There is no follow-up sequence.\n"
        f"To correct your details or be removed, reply to this email or write to {unsub_to}.\n"
        "We process your business address under legitimate interest (GDPR art. 6(1)(f)) and delete it on request.\n"
        "Privacy policy: https://biglobster.top/privacy.html\n"
        f"{postal}\n"
    )


def _build_message(a, body, from_email, from_name, unsub_to, postal):
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = a.to
    msg["Subject"] = a.subject
    msg["Date"] = _now().strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])
    # One-click and mailto opt-out. Required for bulk mail at every major
    # inbox provider, and the thing that makes "reply to be removed" real.
    msg["List-Unsubscribe"] = f"<mailto:{unsub_to}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body + _footer(a.lang, postal, unsub_to, a.page_url))
    return msg


def cmd_send(a):
    led = _load(a.ledger)
    host = norm_host(a.host or a.to)
    now = _now()

    if not EMAIL_RE.match(a.to.strip()):
        print(f"BAD_ADDRESS {a.to}")
        return 8
    if not host:
        print("BAD_HOST could not derive a host key")
        return 8

    # --- guards, cheapest and most permanent first ---
    hit = _is_suppressed(led, host, a.to)
    if hit:
        print(f"SUPPRESSED {hit}")
        return 4
    if host in led["contacted"]:
        prev = led["contacted"][host]
        print(f"ALREADY_CONTACTED {host} at {prev.get('sent_at')} status={prev.get('status')}")
        return 3
    total, bounced, rate, halted = _bounce_state(led)
    if halted:
        print(f"HALTED bounce_rate={rate:.0%} over {total} sends")
        return 6
    sent_today = _sends_since(led, now - timedelta(days=1))
    if sent_today >= a.max_per_day:
        print(f"RATE_CAP {sent_today} sends in the trailing 24h (cap {a.max_per_day})")
        return 5

    try:
        with open(a.body_file, "r", encoding="utf-8") as fh:
            body = fh.read(MAX_BODY_BYTES + 1)
    except OSError as err:
        print(f"BODY_UNREADABLE {err}")
        return 8
    if len(body.encode("utf-8")) > MAX_BODY_BYTES or not body.strip():
        print("BODY_INVALID empty or over size limit")
        return 8

    from_email = (os.getenv("DIRECTORY_FROM_EMAIL") or "").strip()
    from_name = (os.getenv("DIRECTORY_FROM_NAME") or "BigLobster").strip()
    unsub_to = (os.getenv("DIRECTORY_UNSUBSCRIBE_EMAIL") or from_email).strip()
    postal = (os.getenv("DIRECTORY_POSTAL_ADDRESS") or DEFAULT_POSTAL).strip()
    user = os.getenv("BREVO_SMTP_USER") or ""
    password = os.getenv("BREVO_SMTP_PASSWORD") or ""

    if not from_email or not parseaddr(from_email)[1]:
        print("NO_CREDENTIALS DIRECTORY_FROM_EMAIL is unset")
        return 7
    if not a.dry_run and not (user and password):
        # Never echo which value was present — this text reaches a transcript.
        print("NO_CREDENTIALS BREVO_SMTP_USER/BREVO_SMTP_PASSWORD unset in this profile's .env")
        return 7

    msg = _build_message(a, body, from_email, from_name, unsub_to, postal)

    if a.dry_run:
        print("DRY_RUN — not sent\n")
        print(msg.as_string())
        return 0

    host_smtp = os.getenv("BREVO_SMTP_HOST", DEFAULT_SMTP_HOST)
    port = int(os.getenv("BREVO_SMTP_PORT", DEFAULT_SMTP_PORT))
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host_smtp, port, timeout=30,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host_smtp, port, timeout=30)
        with server:
            if port != 465:
                server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as err:
        # str(err) can carry the server's reply but never our credentials.
        print(f"SEND_FAILED {type(err).__name__}: {err}")
        return 8

    led["contacted"][host] = {
        "email": a.to.strip(),
        "name": a.name,
        "set": a.set_name,
        "page": a.page_url,
        "sent_at": now.isoformat(),
        "status": "sent",
    }
    _save(a.ledger, led)
    print(f"OK {host} ({sent_today + 1}/{a.max_per_day} in trailing 24h)")
    return 0


def cmd_record(a):
    led = _load(a.ledger)
    host = norm_host(a.host)
    rec = led["contacted"].get(host)
    if rec is None:
        print(f"NOT_FOUND {host}")
        return 4
    rec["status"] = a.status
    rec["status_at"] = _now().isoformat()
    # A bounce is also a permanent suppression: never retry a dead address.
    if a.status == "bounced":
        led["suppressed"][host] = {"reason": "bounced", "ts": _now().isoformat()}
    _save(a.ledger, led)
    print(f"OK {host} {a.status}")
    return 0


def cmd_suppress(a):
    led = _load(a.ledger)
    key = a.email.strip().lower() if a.email else norm_host(a.host)
    if not key:
        print("BAD_KEY need --host or --email")
        return 4
    led["suppressed"][key] = {"reason": a.reason or "requested", "ts": _now().isoformat()}
    _save(a.ledger, led)
    print(f"OK suppressed {key}")
    return 0


def cmd_unsuppress(a):
    led = _load(a.ledger)
    key = a.email.strip().lower() if a.email else norm_host(a.host)
    if key not in led["suppressed"]:
        print(f"NOT_FOUND {key}")
        return 4
    del led["suppressed"][key]
    _save(a.ledger, led)
    print(f"OK unsuppressed {key}")
    return 0


def cmd_stats(a):
    led = _load(a.ledger)
    total, bounced, rate, halted = _bounce_state(led)
    by_status = {}
    for rec in led["contacted"].values():
        by_status[rec.get("status", "sent")] = by_status.get(rec.get("status", "sent"), 0) + 1
    sent_today = _sends_since(led, _now() - timedelta(days=1))
    print(json.dumps({
        "contacted_total": total,
        "by_status": by_status,
        "suppressed": len(led["suppressed"]),
        "bounce_rate": round(rate, 3),
        "sent_trailing_24h": sent_today,
        "remaining_trailing_24h": max(0, a.max_per_day - sent_today),
        "halted": halted,
    }, indent=2))
    return 6 if halted else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Hermes directory outreach ledger and sender.")
    p.add_argument("--ledger", required=True, help="Path to directory-outreach.json")
    p.add_argument("--max-per-day", dest="max_per_day", type=int, default=DEFAULT_MAX_PER_DAY,
                   help=f"Sends allowed in any trailing 24h window (default {DEFAULT_MAX_PER_DAY})")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("candidates", help="Listings in a set that may still be contacted")
    c.add_argument("--set", required=True, help="Path to a site/directory-data/*.json file")
    c.add_argument("--limit", type=int, default=10)
    c.set_defaults(func=cmd_candidates)

    s = sub.add_parser("send", help="Send one outreach email and record it")
    s.add_argument("--to", required=True)
    s.add_argument("--host", help="Business host key (defaults to the address domain)")
    s.add_argument("--name", help="Business name, for the ledger")
    s.add_argument("--subject", required=True)
    s.add_argument("--body-file", dest="body_file", required=True)
    s.add_argument("--page-url", dest="page_url", required=True,
                   help="The LIVE directory page the business is listed on")
    s.add_argument("--set-name", dest="set_name", default="")
    s.add_argument("--lang", choices=["en", "es"], default="en")
    s.add_argument("--dry-run", dest="dry_run", action="store_true")
    s.set_defaults(func=cmd_send)

    r = sub.add_parser("record", help="Update the outcome for a contacted business")
    r.add_argument("--host", required=True)
    r.add_argument("--status", required=True, choices=list(TERMINAL))
    r.set_defaults(func=cmd_record)

    u = sub.add_parser("suppress", help="Never contact this host/address again")
    u.add_argument("--host")
    u.add_argument("--email")
    u.add_argument("--reason")
    u.set_defaults(func=cmd_suppress)

    n = sub.add_parser("unsuppress", help="Undo a suppression (human decision)")
    n.add_argument("--host")
    n.add_argument("--email")
    n.set_defaults(func=cmd_unsuppress)

    t = sub.add_parser("stats", help="Counters and the halt state")
    t.set_defaults(func=cmd_stats)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
