"""Guard: a shared bl-site-package prompt must not name one customer.

`*/bl-site-package-*.prompt` is package-wide content — one copy is frozen into
a cron job for *every* rented client. A customer's name, domain or distributor
written into one of them is that customer's identity leaking into the next
client's agent run, and nothing downstream catches it: prompts are plain text,
so there is no build, no schema and no lint between the edit and production.

Three had leaked this way before this test existed:

    onboarding-content  "Para una tienda (p.ej. `shoroban.com`)"
    infographic         "como el que falló como SVG en Shoroban"
    gap-hunter          a facets example whose brand was the real distributor

Two independent checks, because they catch different mistakes:

1. ``CUSTOMER_IDENTIFIERS`` — a name written as prose ("en Shoroban"), which no
   structural rule can recognise. Hand-maintained: add a client here when one
   is onboarded. There is no machine-readable customer registry in this repo —
   customers exist at runtime as ``bl-<client>`` profiles.
2. A domain-shaped token — catches a client this list has never heard of, which
   is the case the list cannot cover. Add genuinely generic domains to
   ``ALLOWED_DOMAINS``.

Use ``OLD_SITE_URL`` / ``BL_SITE_URL`` for the site the agent is working on;
that is what makes the example unnecessary in the first place.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Shared, package-wide prompts. Globbed rather than listed so a new one is
# covered the day it lands.
SHARED_PROMPTS = "*/bl-site-package-*.prompt"
# The Infographic Engineer serves biglobster AND every rental from one file,
# so its name carries no bl-site-package- prefix. It still ships to clients,
# and this test already caught a customer name in its predecessor, so it is
# named explicitly rather than left to the glob.
EXTRA_SHARED_PROMPTS = ("infographic/infographic-engineer.prompt",)

# Customer names, their distributors, and any identifier tied to a single
# client. Case-insensitive, matched on word boundaries.
CUSTOMER_IDENTIFIERS = (
    "shoroban",
    "liderpapel",
    "solutex",
    "20603",  # sFTP user of one client's distributor feed
)

DOMAIN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]*\.(?:com|es|net|org|io|eu|shop|store|online|info)\b",
    re.IGNORECASE,
)

# Domains that are generic by construction, or not a customer's.
ALLOWED_DOMAINS = {
    "example.com",
    "example.es",
    "example.org",
    "schema.org",
}


def _shared_prompts():
    found = set(REPO_ROOT.glob(SHARED_PROMPTS))
    found.update(REPO_ROOT / rel for rel in EXTRA_SHARED_PROMPTS)
    return sorted(p for p in found if p.exists())


def test_the_glob_still_finds_the_shared_prompts():
    """A rename must not quietly leave this guard watching nothing."""
    found = _shared_prompts()
    assert len(found) >= 9, f"{SHARED_PROMPTS} matched only {[p.name for p in found]}"


@pytest.mark.parametrize("path", _shared_prompts(), ids=lambda p: p.parent.name)
def test_no_customer_identifier(path):
    hits = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for name in CUSTOMER_IDENTIFIERS:
            if re.search(rf"\b{re.escape(name)}\b", line, re.IGNORECASE):
                hits.append(f"{path.name}:{lineno} names {name!r}: {line.strip()}")
    assert not hits, (
        "A shared bl-site-package prompt names one customer — every other "
        "client's agent reads this:\n  " + "\n  ".join(hits)
    )


@pytest.mark.parametrize("path", _shared_prompts(), ids=lambda p: p.parent.name)
def test_no_customer_domain(path):
    hits = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for match in DOMAIN_RE.finditer(line):
            if match.group(0).lower() in ALLOWED_DOMAINS:
                continue
            hits.append(f"{path.name}:{lineno} hardcodes {match.group(0)!r}")
    assert not hits, (
        "A shared bl-site-package prompt hardcodes a site — use OLD_SITE_URL / "
        "BL_SITE_URL, or add it to ALLOWED_DOMAINS if it is generic:\n  "
        + "\n  ".join(hits)
    )
