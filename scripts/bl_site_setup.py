#!/usr/bin/env python3
"""Deterministic bl-site-package template application — the "create your
website" step, with no human judgment call anywhere in it.

This is the code half of the *Site Launch* product (`site-setup` agent in
`scripts/provision_bl_client.py`). It takes a **structured questionnaire** —
the same fixed field list for every buyer — and applies it to a blank
bl-site-package instance:

1. completes the instance's first-run `/setup` (company name, sector, panel
   password, the client's own BYOK OpenRouter key),
2. writes the identity/legal/business fields **verbatim** from the
   questionnaire (never invented, never scraped),
3. uploads the buyer's logo from the URL they supplied,
4. pins the AI text/image models.

Everything here is a straight form-field → config-key copy. There is no
design decision, no scoping conversation, and no per-buyer variation in what
gets built: bl-site-package has a **fixed five-page structure** (inicio,
quiénes somos, servicios, contacto, blog) and a fixed theme, identical for
every customer. The only thing that differs between two buyers is the values
in their answers.

The prose that fills those five pages is written afterwards by the
`site-setup` cron job (`site-setup/bl-site-package-site-setup.prompt`), which
can only write into the same fixed field list. See AGENT_RENTAL_SETUP.md
("Site Launch — the bounded product") for why the boundary is drawn here.

Idempotent: re-running against an instance whose `/setup` already completed
skips step 1 and re-applies steps 2-4, so a retried provision converges
instead of failing.

Usage (module):
    from scripts.bl_site_setup import apply_site_template, SECTORS
    result = apply_site_template(site_url, panel_password, answers, openrouter_key)

Usage (CLI, for a manual re-apply):
    .venv/bin/python3 scripts/bl_site_setup.py \\
        --site-url https://blcliente.zeabur.app \\
        --panel-password '...' \\
        --openrouter-key sk-or-v1-... \\
        --answers answers.json
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Sectors offered by bl-site-package's own /setup wizard (web/setup.html).
# The questionnaire must pick one of these — a free-text sector would be the
# first crack through which "tell us about your business" scoping returns.
SECTORS = (
    "Fabricación",
    "Logística",
    "Distribución",
    "Instalaciones",
    "Construcción",
    "Alimentación",
    "Otro",
)

# schema.org LocalBusiness subtypes bl-site-package accepts for biz_type.
# Free text here would end up in the site's structured data, so it's an enum.
BIZ_TYPES = (
    "LocalBusiness",
    "Store",
    "Restaurant",
    "ProfessionalService",
    "HomeAndConstructionBusiness",
    "AutomotiveBusiness",
    "FoodEstablishment",
)

# Questionnaire answers copied VERBATIM into the site config. Every one of
# these is a fact the buyer types about themselves — none of it is a design
# or scope decision, and none of it is written by a model.
#
# answer key -> bl-site-package config key (POST /api/site/texts)
VERBATIM_FIELDS = {
    "whatsapp_number": "whatsapp_number",
    "notify_email": "notify_email",
    "legal_name": "legal_name",
    "legal_id": "legal_id",
    "legal_address": "legal_address",
    "legal_email": "legal_email",
    "biz_type": "biz_type",
    "biz_street": "biz_street",
    "biz_city": "biz_city",
    "biz_postal_code": "biz_postal_code",
    "biz_region": "biz_region",
    "biz_country": "biz_country",
    "biz_phone": "biz_phone",
    "biz_hours": "biz_hours",
    "biz_price_range": "biz_price_range",
    "biz_facebook": "biz_facebook",
    "biz_instagram": "biz_instagram",
}

REQUIRED_ANSWERS = ("company_name", "sector", "notify_email")

# bl-site-package caps the logo at 2 MB and accepts only raster formats
# (SVG is excluded there on purpose — inline <script> in an SVG served from
# /uploads is stored XSS). Mirror both limits client-side so a bad logo URL
# fails with a clear message instead of a multer error string.
MAX_LOGO_BYTES = 2 * 1024 * 1024
LOGO_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "logo.png"),
    (b"\xff\xd8\xff", "image/jpeg", "logo.jpg"),
)


class SiteSetupError(ValueError):
    """A questionnaire/instance problem the caller should surface, not retry.

    ``code`` is a stable machine-readable tag so the webhook receiver can map
    the failure to an HTTP status and a fix-it instruction without pattern
    matching on the message text.
    """

    def __init__(self, message: str, code: str = "site_setup_failed"):
        super().__init__(message)
        self.code = code


def _http_json(
    method: str,
    url: str,
    body: dict | None = None,
    token: str | None = None,
    timeout: int = 20,
) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _sniff_logo(data: bytes) -> tuple[str, str]:
    """Return (mimetype, filename) for logo bytes, or raise.

    Signature-based, never trusting the URL's extension or the server's
    Content-Type — the filename bl-site-package stores is derived from the
    mimetype it validates, so sending a mislabelled file just gets rejected
    there with a less useful message.
    """
    for magic, mime, filename in LOGO_SIGNATURES:
        if data.startswith(magic):
            return mime, filename
    # WebP: "RIFF" .... "WEBP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "logo.webp"
    raise SiteSetupError(
        "The logo must be a PNG, JPG or WebP image (SVG is not accepted)."
    )


def _download_logo(logo_url: str) -> tuple[bytes, str, str]:
    parsed = urllib.parse.urlparse(logo_url)
    if parsed.scheme not in ("http", "https"):
        raise SiteSetupError(f"logo_url must be http(s), got '{parsed.scheme or logo_url}'")
    try:
        with urllib.request.urlopen(logo_url, timeout=30) as resp:
            data = resp.read(MAX_LOGO_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise SiteSetupError(f"Could not fetch the logo: HTTP {exc.code} from {logo_url}")
    except urllib.error.URLError as exc:
        raise SiteSetupError(f"Could not fetch the logo from {logo_url}: {exc.reason}")
    if not data:
        raise SiteSetupError(f"The logo URL returned no data: {logo_url}")
    if len(data) > MAX_LOGO_BYTES:
        raise SiteSetupError("The logo exceeds bl-site-package's 2 MB limit.")
    mime, filename = _sniff_logo(data)
    return data, mime, filename


def _upload_logo(site_url: str, token: str, logo_url: str) -> str:
    """POST the buyer's logo to /api/site/logo (multipart, field name 'logo')."""
    data, mime, filename = _download_logo(logo_url)
    boundary = f"----hermes{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="logo"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(f"{site_url}/api/site/logo", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SiteSetupError(f"Logo upload rejected by the site: HTTP {exc.code} — {detail}")
    except urllib.error.URLError as exc:
        raise SiteSetupError(f"Logo upload could not reach {site_url}: {exc.reason}")
    return result.get("path", "")


def validate_answers(answers: dict) -> dict:
    """Validate the questionnaire, tagging every failure ``invalid_questionnaire``.

    The wrapper exists so the webhook can answer 400-don't-retry on any schema
    problem without matching on message text — every rejection from the checks
    below is the buyer's form, not the infrastructure.
    """
    try:
        return _validate_answers(answers)
    except SiteSetupError as exc:
        exc.code = "invalid_questionnaire"
        raise


def _validate_answers(answers: dict) -> dict:
    """Validate the questionnaire against the fixed schema. Returns it cleaned.

    Deliberately strict: an unknown key means BigLobster's form and this
    schema drifted, and silently dropping it would ship a site missing data
    the buyer paid for. A free-text sector or biz_type is rejected outright —
    those enums are what keep the product a template rather than a brief.
    """
    if not isinstance(answers, dict):
        raise SiteSetupError("answers must be an object")

    known = set(REQUIRED_ANSWERS) | set(VERBATIM_FIELDS) | {"logo_url", "old_site_url"}
    unknown = sorted(set(answers) - known)
    if unknown:
        raise SiteSetupError(
            f"Unknown questionnaire field(s) {unknown} — the BigLobster form and "
            f"this schema have drifted. Known fields: {sorted(known)}"
        )

    cleaned: dict = {}
    for key, value in answers.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise SiteSetupError(f"Questionnaire field '{key}' must be a string")
        value = value.strip()
        if value:
            cleaned[key] = value

    missing = [k for k in REQUIRED_ANSWERS if not cleaned.get(k)]
    if missing:
        raise SiteSetupError(f"Missing required questionnaire field(s): {missing}")
    if cleaned["sector"] not in SECTORS:
        raise SiteSetupError(f"sector must be one of {list(SECTORS)}, got '{cleaned['sector']}'")
    if "biz_type" in cleaned and cleaned["biz_type"] not in BIZ_TYPES:
        raise SiteSetupError(f"biz_type must be one of {list(BIZ_TYPES)}, got '{cleaned['biz_type']}'")
    if "@" not in cleaned["notify_email"]:
        raise SiteSetupError(f"notify_email doesn't look like an address: '{cleaned['notify_email']}'")
    return cleaned


def apply_site_template(
    site_url: str,
    panel_password: str,
    answers: dict,
    openrouter_key: str,
    ai_model: str | None = None,
    image_model: str | None = None,
) -> dict:
    """Apply the fixed bl-site-package template from a validated questionnaire.

    Returns a report dict (which steps ran, which fields were written). Raises
    SiteSetupError for anything the caller should surface to the CEO rather
    than retry blindly (unreachable instance, bad logo, drifted schema).
    """
    site_url = site_url.rstrip("/")
    cleaned = validate_answers(answers)

    report: dict = {"site_url": site_url, "setup_completed": False, "fields_written": [], "logo": None}

    # 1. First-run /setup. Already-configured instances 409 on
    #    /api/setup/complete by design (it's an unauthenticated endpoint, so it
    #    self-seals after first use); treat that as "already done" so a retried
    #    provision converges instead of failing.
    try:
        status = _http_json("GET", f"{site_url}/api/setup/status")
    except urllib.error.HTTPError as exc:
        raise SiteSetupError(
            f"Instance {site_url} answered HTTP {exc.code} on /api/setup/status",
            code="site_unreachable",
        )
    except urllib.error.URLError as exc:
        raise SiteSetupError(
            f"Instance {site_url} is unreachable: {exc.reason}", code="site_unreachable"
        )
    except json.JSONDecodeError:
        raise SiteSetupError(
            f"{site_url}/api/setup/status did not return JSON — is this a "
            "bl-site-package instance?",
            code="site_unreachable",
        )

    if not status.get("configured"):
        try:
            _http_json(
                "POST",
                f"{site_url}/api/setup/complete",
                {
                    "companyName": cleaned["company_name"],
                    "sector": cleaned["sector"],
                    "panelPassword": panel_password,
                    "openrouterApiKey": openrouter_key,
                },
            )
            report["setup_completed"] = True
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                detail = exc.read().decode("utf-8", errors="replace")
                raise SiteSetupError(f"/api/setup/complete failed: HTTP {exc.code} — {detail}")

    # 2. Authenticate for the remaining (protected) writes.
    try:
        login = _http_json("POST", f"{site_url}/api/auth/login", {"password": panel_password})
    except urllib.error.HTTPError as exc:
        raise SiteSetupError(
            f"Panel login to {site_url} failed with HTTP {exc.code}. If the instance was "
            "already configured with a different password, it can't be claimed for "
            "this order.",
            code="site_already_claimed",
        )
    token = login.get("token")
    if not token:
        raise SiteSetupError(
            f"Panel login to {site_url} returned no token: {login}",
            code="site_already_claimed",
        )

    # 3. Verbatim identity/legal/business fields, plus the model choices.
    texts: dict = {"site_url": site_url}
    for answer_key, config_key in VERBATIM_FIELDS.items():
        if answer_key in cleaned:
            texts[config_key] = cleaned[answer_key]
    if ai_model:
        texts["ai_model"] = ai_model
    if image_model:
        texts["image_model"] = image_model
    try:
        _http_json("POST", f"{site_url}/api/site/texts", texts, token=token)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SiteSetupError(f"Writing site config failed: HTTP {exc.code} — {detail}")
    report["fields_written"] = sorted(texts)

    # 4. Logo. Optional — a buyer without one still gets a complete site, so
    #    this must never be the thing that fails a paid order.
    if cleaned.get("logo_url"):
        try:
            report["logo"] = _upload_logo(site_url, token, cleaned["logo_url"])
        except SiteSetupError as exc:
            report["logo"] = f"skipped: {exc}"

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--panel-password", required=True)
    parser.add_argument("--openrouter-key", required=True)
    parser.add_argument("--answers", required=True, help="Path to the questionnaire JSON")
    parser.add_argument("--ai-model", default=None)
    parser.add_argument("--image-model", default=None)
    args = parser.parse_args()

    try:
        with open(args.answers, encoding="utf-8") as fh:
            answers = json.load(fh)
        result = apply_site_template(
            args.site_url,
            args.panel_password,
            answers,
            args.openrouter_key,
            ai_model=args.ai_model,
            image_model=args.image_model,
        )
    except (SiteSetupError, OSError, json.JSONDecodeError) as exc:
        print(f"Site setup failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
