"""Watch the pinned Hermes/OpenCode model ids against live catalogs.

Compares OpenRouter and OpenCode catalogs, reports newer cheaper same-family models,
other-family suggestions, and models missing from the catalog. OpenCode mode rewrites
delegate.sh chains in place. Stdlib-only at import time so it runs with `python3 -I`.

Run in the pod: `python -m model_watch hermes`
Run on a Mac:   `python3 -I model_watch/watch.py opencode`
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path

OR_MODELS = "https://openrouter.ai/api/v1/models"
OR_IMAGE_MODELS = "https://openrouter.ai/api/v1/models?output_modalities=image"
OR_ZDR = "https://openrouter.ai/api/v1/endpoints/zdr"
GO_MODELS = "https://opencode.ai/inference/go/v1/models"
ZEN_MODELS = "https://opencode.ai/zen/v1/models"
ECI_CSV = "https://epoch.ai/data/eci_scores.csv"

DEFAULT_HOME = "/opt/data"
DEFAULT_DELEGATE = "/Users/brais/VSCODE/.claude/skills/opencode-delegate/delegate.sh"
USER_AGENT = "model-watch"
TIMEOUT = 30

TIERS = {
    "flash", "pro", "max", "mini", "lite", "nano", "plus", "turbo", "code",
    "highspeed", "small", "medium", "large", "ultra",
}

JUDGE_REASON = "juez: pasar tasks/judge_provider_probe.py antes"
CODE_REASON = "valor por defecto en código: cambiar por PR"
DEARER_REASON = "más caro"
SUGGEST_REASON = "capacidad sin A/B propio"
MISSING_OR_REASON = "no está en el catálogo de OpenRouter"
MISSING_OC_REASON = "ya no está en el catálogo"
ECI_MISSING_NOTE = "ECI no disponible"
ZDR_FAILED_NOTE = "Lista ZDR no disponible: no se propone ningún candidato de OpenRouter."
OC_CATALOGS_FAILED_NOTE = "Catálogos de OpenCode no disponibles: familias deducidas del nombre."
IMAGE_CATALOG_FAILED_NOTE = "Catálogo de imagen no disponible."

SECTIONS = (
    ("falta", "Ya no están en el catálogo"),
    ("auto", "Actualizado / en cola (automático)"),
    ("avisar", "Avisar: misma familia, más capacidad, más caro"),
    ("sugerir", "Sugerencia: otra familia, más capacidad, más barato"),
)

TEXT_TABLE_HEADER = "modelo | AA | ECI | entrada $/M | salida $/M | contexto | fecha"
IMAGE_TABLE_HEADER = "modelo | $/imagen | $/M img | fecha"

CHAIN_LINE = re.compile(r'^(\s*(?:FREE|GO|MAX|ZEN)=")([^"]*)(")(.*)$')
DAILY_SUFFIX = re.compile(r"(?:-\d{8}|-\d{4})$")
# Catalog data is untrusted: a model id ends up inside delegate.sh (a bash script) and in
# Coolify env values, so only plain ids pass, and only the model variables can be queued.
SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
SAFE_STORE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MODEL_ENV_VARS = frozenset({
    "HERMES_DEFAULT_MODEL", "HERMES_FALLBACK_MODEL", "HERMES_AUDITOR_ORCHESTRATOR_MODEL",
    "HERMES_AUDITOR_SYSTEM_MODEL", "HERMES_AUDITOR_CONTENT_MODEL", "AUXILIARY_VISION_MODEL",
    "OPENROUTER_IMAGE_MODEL",
})


def _safe_id(value) -> bool:
    return isinstance(value, str) and SAFE_MODEL_ID.fullmatch(value) is not None


# ── Network access (the only place tests need to patch) ─────────────────────


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read().decode("utf-8")


def fetch_json(url: str):
    return json.loads(fetch_text(url))


# ── Pure helpers ─────────────────────────────────────────────────────────────


def _num(value):
    """Numeric value of a catalog field, or None when it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def base_name(model_id) -> str:
    """Last id segment, lowercased, without a :suffix or a trailing date."""
    tail = str(model_id or "").rsplit("/", 1)[-1].lower()
    tail = tail.split(":", 1)[0]
    return DAILY_SUFFIX.sub("", tail)


def fallback_family(model_id) -> str:
    """Family key derived from the name alone: <stem>:<tier>[:free]."""
    base = base_name(model_id)
    match = re.match(r"[a-z]+", base)
    stem = match.group(0) if match else base
    tier = base
    for token in base.split("-"):
        if token in TIERS:
            tier = token
            break
    family = f"{stem}:{tier}"
    if base.endswith("-free"):
        family += ":free"
    return family


def _numeric_prices(pricing) -> dict:
    out = {}
    for key, value in (pricing or {}).items():
        if key == "discount":
            continue
        number = _num(value)
        if number is not None:
            out[key] = number
    return out


def le_price(cand, cur) -> bool:
    """True when every numeric price present in either dict is <= the other."""
    cand_prices = _numeric_prices(cand)
    cur_prices = _numeric_prices(cur)
    keys = set(cand_prices) | set(cur_prices)
    return all(cand_prices.get(key, 0.0) <= cur_prices.get(key, 0.0) for key in keys)


def lower_price(cand, cur) -> bool:
    """True when le_price and at least one shared/numeric price is strictly lower."""
    if not le_price(cand, cur):
        return False
    cand_prices = _numeric_prices(cand)
    cur_prices = _numeric_prices(cur)
    keys = set(cand_prices) | set(cur_prices)
    return any(cand_prices.get(key, 0.0) < cur_prices.get(key, 0.0) for key in keys)


class Families:
    """Family names from the Go+Zen catalogs, with the name-based fallback."""

    __slots__ = ("_by_base",)

    def __init__(self, go_records=(), zen_records=()):
        self._by_base = {}
        for record in list(go_records or []) + list(zen_records or []):
            family = record.get("family")
            if not family:
                continue
            for key in (record.get("canonical_id"), record.get("id")):
                if key:
                    self._by_base[base_name(key)] = family

    def family_of(self, model_id):
        return self._by_base.get(base_name(model_id))

    def same_family(self, a_id, b_id) -> bool:
        a_family = self.family_of(a_id)
        b_family = self.family_of(b_id)
        if a_family is not None and b_family is not None:
            return a_family == b_family
        return fallback_family(a_id) == fallback_family(b_id)


class Site:
    """One place where a model is pinned."""

    __slots__ = (
        "catalog",
        "current_id",
        "env_var",
        "is_image",
        "is_judge",
        "jobs",
        "name",
        "needs_image_input",
        "needs_tools",
        "source",
    )

    def __init__(
        self,
        name,
        current_id,
        *,
        env_var=None,
        needs_tools=True,
        is_judge=False,
        needs_image_input=False,
        is_image=False,
        source="env",
        catalog="openrouter",
        jobs=None,
    ):
        self.name = name
        self.current_id = current_id
        self.env_var = env_var
        self.needs_tools = needs_tools
        self.is_judge = is_judge
        self.needs_image_input = needs_image_input
        self.is_image = is_image
        self.source = source
        self.catalog = catalog
        self.jobs = list(jobs or [])

    @property
    def is_chain(self) -> bool:
        return self.source == "chain"

    @property
    def is_job(self) -> bool:
        return self.source == "job"


class Context:
    """Everything classify() needs to know about the catalog it is looking at."""

    __slots__ = ("catalog", "eci", "families", "or_records", "today", "zdr_slugs")

    def __init__(
        self,
        catalog="openrouter",
        *,
        families=None,
        zdr_slugs=None,
        today=None,
        or_records=(),
        eci=None,
    ):
        self.catalog = catalog
        self.families = families or Families()
        self.zdr_slugs = set(zdr_slugs or ())
        self.today = today or date.today()
        self.or_records = list(or_records or ())
        self.eci = eci or {}


class Finding:
    """One report line plus the numbers rendered in its comparison table."""

    __slots__ = (
        "cand_id",
        "cand_stats",
        "catalog",
        "current_id",
        "current_stats",
        "is_image",
        "kind",
        "reasons",
        "site",
        "site_ref",
    )

    def __init__(
        self,
        site,
        kind,
        current_id,
        cand_id,
        reasons=None,
        current_stats=None,
        cand_stats=None,
        catalog="openrouter",
        is_image=False,
        site_ref=None,
    ):
        self.site = site
        self.kind = kind
        self.current_id = current_id
        self.cand_id = cand_id
        self.reasons = list(reasons or [])
        self.current_stats = current_stats
        self.cand_stats = cand_stats
        self.catalog = catalog
        self.is_image = is_image
        self.site_ref = site_ref


def _data(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return []


def _index(record, ctx: Context):
    if ctx.catalog == "openrouter":
        return _aa(record)
    canonical_id = record.get("canonical_id")
    if not canonical_id:
        return None
    for other in ctx.or_records:
        if other.get("id") == canonical_id:
            return _aa(other)
    target = base_name(canonical_id)
    for other in ctx.or_records:
        if base_name(other.get("canonical_slug") or "") == target:
            return _aa(other)
    return None


def _aa(record):
    try:
        value = record["benchmarks"]["artificial_analysis"]["intelligence_index"]
    except (KeyError, TypeError):
        return None
    return _num(value)


def _newer(cand, current, ctx: Context) -> bool:
    if ctx.catalog == "openrouter":
        cand_created = _num(cand.get("created")) or 0.0
        cur_created = _num(current.get("created")) or 0.0
        return cand_created > cur_created
    return str(cand.get("release_date") or "") > str(current.get("release_date") or "")


def _prices(record, ctx: Context) -> dict:
    if ctx.catalog == "openrouter":
        return record.get("pricing") or {}
    return {
        "input": _opencode_price(record, "input"),
        "output": _opencode_price(record, "output"),
    }


def _opencode_price(record, key):
    cost = (record.get("cost") or {}).get("text") or {}
    entry = cost.get(key) or {}
    return _num(entry.get("price"))


def _modalities(record, key) -> set:
    architecture = record.get("architecture") or {}
    return set(architecture.get(key) or [])


def _expired(record, today) -> bool:
    raw = record.get("expiration_date")
    if not raw:
        return False
    try:
        return date.fromisoformat(str(raw)[:10]) < today
    except ValueError:
        return False


def _judge_ok(record) -> bool:
    reasoning = record.get("reasoning")
    if not reasoning:
        return True
    if reasoning.get("mandatory"):
        return False
    return "none" in (reasoning.get("supported_efforts") or [])


def _eligible_openrouter(site, current, cand, ctx: Context) -> bool:
    cand_id = str(cand.get("id") or "")
    if not _safe_id(cand_id) or cand_id == current.get("id") or ":" in cand_id:
        return False
    if cand_id.startswith("anthropic/") or cand_id.startswith("google/gemini"):
        return False
    if cand_id.startswith("openai/gpt-") and not cand_id.startswith("openai/gpt-oss"):
        return False
    if _expired(cand, ctx.today):
        return False
    slug = cand.get("canonical_slug")
    if not slug or slug not in ctx.zdr_slugs:
        return False
    if _modalities(cand, "output_modalities") != _modalities(current, "output_modalities"):
        return False
    if not _modalities(current, "input_modalities") <= _modalities(cand, "input_modalities"):
        return False
    if site.needs_tools and "tools" not in (cand.get("supported_parameters") or []):
        return False
    if site.is_judge and not _judge_ok(cand):
        return False
    return True


def _eligible_opencode(site, current, cand, ctx: Context) -> bool:
    cand_id = str(cand.get("id") or "")
    if not _safe_id(cand_id) or cand_id == current.get("id"):
        return False
    if cand.get("type") != "chat":
        return False
    tools = ((cand.get("capabilities") or {}).get("tools") or {}).get("supported")
    if not tools:
        return False
    if ctx.catalog == "opencode-go" and (cand.get("go") or {}).get("limited_regions"):
        return False
    return True


def _fmt_eci(record, table):
    if not table:
        return None
    for field in ("id", "canonical_slug", "canonical_id"):
        value = record.get(field)
        if not value:
            continue
        # The snapshot tail first: Epoch scores "DeepSeek V4.1 Flash 1017" apart from the alias.
        tail = str(value).rsplit("/", 1)[-1].lower().split(":", 1)[0]
        for key in (_eci_norm(tail), _eci_norm(base_name(value))):
            if key and key in table:
                return table[key]
    return None


def _stats(record, ctx: Context) -> dict:
    if ctx.catalog == "openrouter":
        pricing = record.get("pricing") or {}
        price_in = _per_mtok(pricing.get("prompt", pricing.get("input")))
        price_out = _per_mtok(pricing.get("completion", pricing.get("output")))
        created = _num(record.get("created"))
        day = None
        if created:
            day = datetime.fromtimestamp(created, UTC).date().isoformat()
        context = record.get("context_length")
        if context is None:
            context = (record.get("top_provider") or {}).get("context_length")
        return {
            "id": record.get("id"),
            "aa": _aa(record),
            "eci": _fmt_eci(record, ctx.eci),
            "price_in": price_in,
            "price_out": price_out,
            "context": _num(context),
            "date": day,
            "newest": created or 0.0,
            "price_sum": (price_in or 0.0) + (price_out or 0.0),
            "image": _num(pricing.get("image")),
            "image_per_mtok": price_out,
        }
    price_in = _opencode_price(record, "input")
    price_out = _opencode_price(record, "output")
    context = record.get("context_length")
    if context is None:
        context = (record.get("limit") or {}).get("context")
    release = record.get("release_date")
    return {
        "id": record.get("id"),
        "aa": _index(record, ctx),
        "eci": _fmt_eci(record, ctx.eci),
        "price_in": price_in,
        "price_out": price_out,
        "context": _num(context),
        "date": release,
        "newest": release or "",
        "price_sum": (price_in or 0.0) + (price_out or 0.0),
        "image": None,
        "image_per_mtok": price_out,
    }


def _per_mtok(value):
    number = _num(value)
    return None if number is None else number * 1e6


def _idx_sort_key(finding):
    aa = (finding.cand_stats or {}).get("aa")
    return (aa is not None, aa if aa is not None else 0.0)


def _auto_sort_key(finding):
    stats = finding.cand_stats or {}
    aa = stats.get("aa")
    return (
        aa is not None,
        aa if aa is not None else 0.0,
        stats.get("newest") or 0,
        -(stats.get("price_sum") or 0.0),
    )


def _finding(site, kind, current, cand, reasons, ctx: Context, current_stats=None,
             cand_stats=None):
    return Finding(
        site=site.name,
        kind=kind,
        current_id=current.get("id"),
        cand_id=cand.get("id") if cand else None,
        reasons=reasons,
        current_stats=current_stats or _stats(current, ctx),
        cand_stats=cand_stats if cand_stats is not None else (_stats(cand, ctx) if cand else None),
        catalog=ctx.catalog,
        is_image=site.is_image,
        site_ref=site,
    )


def _select(site, findings, ctx: Context):
    autos = [finding for finding in findings if finding.kind == "auto"]
    if autos:
        autos = [max(autos, key=_auto_sort_key)]
    avisos = [finding for finding in findings if finding.kind == "avisar"]
    if autos:
        auto_id = autos[0].cand_id
        avisos = [
            finding for finding in avisos
            if not ctx.families.same_family(finding.cand_id, auto_id)
        ]
    avisos = sorted(avisos, key=_idx_sort_key, reverse=True)[:3]
    sugerencias = [finding for finding in findings if finding.kind == "sugerir"]
    sugerencias = sorted(sugerencias, key=_idx_sort_key, reverse=True)[:3]
    return autos + avisos + sugerencias


def classify(site, current, candidates, ctx: Context):
    """Return the findings for one site, filtered and capped by the report rules."""
    findings = []
    for cand in candidates or []:
        if ctx.catalog == "openrouter":
            if not _eligible_openrouter(site, current, cand, ctx):
                continue
        elif not _eligible_opencode(site, current, cand, ctx):
            continue

        same_family = ctx.families.same_family(cand.get("id"), current.get("id"))
        cand_index = _index(cand, ctx)
        cur_index = _index(current, ctx)

        if (
            same_family
            and _newer(cand, current, ctx)
            and (cand_index is None or cur_index is None or cand_index >= cur_index)
        ):
            if le_price(_prices(cand, ctx), _prices(current, ctx)):
                reasons = []
                if site.is_judge:
                    reasons.append(JUDGE_REASON)
                if site.source not in ("env", "job") and not site.is_chain:
                    reasons.append(CODE_REASON)
                kind = "avisar" if reasons else "auto"
                findings.append(_finding(site, kind, current, cand, reasons, ctx))
            else:
                findings.append(
                    _finding(site, "avisar", current, cand, [DEARER_REASON], ctx)
                )
            continue

        if (
            not same_family
            and not site.is_image
            and cand_index is not None
            and cur_index is not None
            and cand_index > cur_index
            and lower_price(_prices(cand, ctx), _prices(current, ctx))
        ):
            findings.append(
                _finding(site, "sugerir", current, cand, [SUGGEST_REASON], ctx)
            )

    return _select(site, findings, ctx)


# ── Rendering ────────────────────────────────────────────────────────────────


def _fmt_num(value):
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _fmt_price(value):
    if value is None:
        return "—"
    return f"{value:.3f}"


def _fmt_context(value):
    if value is None:
        return "—"
    return str(int(value))


def _text_row(stats):
    return " | ".join([
        stats.get("id") or "—",
        _fmt_num(stats.get("aa")),
        _fmt_num(stats.get("eci")),
        _fmt_price(stats.get("price_in")),
        _fmt_price(stats.get("price_out")),
        _fmt_context(stats.get("context")),
        stats.get("date") or "—",
    ])


def _image_row(stats):
    return " | ".join([
        stats.get("id") or "—",
        _fmt_price(stats.get("image")),
        _fmt_price(stats.get("image_per_mtok")),
        stats.get("date") or "—",
    ])


def _group_key(finding):
    return (
        finding.kind,
        finding.current_id or "",
        finding.cand_id or "",
        tuple(finding.reasons),
        finding.is_image,
    )


def _group_findings(findings):
    groups = []
    by_key = {}
    for finding in findings:
        key = _group_key(finding)
        group = by_key.get(key)
        if group is None:
            group = {"finding": finding, "sites": []}
            by_key[key] = group
            groups.append(group)
        site = finding.site
        if site not in group["sites"]:
            group["sites"].append(site)
    return groups


def _finding_block(group):
    finding = group["finding"]
    sites = group["sites"]
    lines = []
    if finding.cand_id:
        lines.append(f"• {finding.current_id} → {finding.cand_id}")
    else:
        lines.append(f"• {finding.current_id}")
    if not (len(sites) == 1 and sites[0] == finding.current_id):
        lines.append("  en: " + ", ".join(sites))
    if finding.kind == "falta":
        return "\n".join(lines)
    for reason in finding.reasons:
        lines.append(f"  · {reason}")
    lines.append("```")
    if finding.is_image:
        lines.append(IMAGE_TABLE_HEADER)
        for stats in (finding.current_stats, finding.cand_stats):
            if stats:
                lines.append(_image_row(stats))
    else:
        lines.append(TEXT_TABLE_HEADER)
        for stats in (finding.current_stats, finding.cand_stats):
            if stats:
                lines.append(_text_row(stats))
    lines.append("```")
    return "\n".join(lines)


def render_report(findings, notes) -> str:
    findings = list(findings or [])
    notes = list(notes or [])
    opencode = bool(findings) and all(f.catalog != "openrouter" for f in findings)
    mode = "OpenCode" if opencode else "OpenRouter"
    lines = [f"🔎 Modelos — {date.today().isoformat()} ({mode})"]
    for kind, title in SECTIONS:
        group = [finding for finding in findings if finding.kind == kind]
        if not group:
            continue
        lines.append("")
        lines.append(title)
        for grouped in _group_findings(group):
            lines.append(_finding_block(grouped))
    if notes:
        lines.append("")
        lines.append("Notas")
        for note in notes:
            lines.append(f"  · {note}")
    return "\n".join(lines)


# ── Files ───────────────────────────────────────────────────────────────────


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _write_text(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _fingerprint(findings, notes=()) -> str:
    tuples = sorted(
        (finding.site, finding.kind, finding.current_id or "", finding.cand_id or "")
        for finding in findings
    )
    payload = json.dumps(
        {"findings": tuples, "notes": list(notes or [])},
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ── Hermes mode ─────────────────────────────────────────────────────────────


def _hermes_home(args) -> Path:
    raw = args.hermes_home or os.environ.get("HERMES_HOME") or DEFAULT_HOME
    return Path(raw).expanduser()


def _eci_norm(value) -> str:
    return re.sub(r"[^a-z0-9.]", "", str(value or "").lower())


def _parse_eci(text) -> dict:
    table = {}
    for row in csv.DictReader(io.StringIO(text)):
        score = _num(row.get("eci"))
        if score is None:
            continue
        for field in ("Model", "Display name"):
            value = (row.get(field) or "").strip()
            if not value:
                continue
            for key in (_eci_norm(value), _eci_norm(base_name(value))):
                if key:
                    table[key] = score
    return table


def _load_defaults():
    try:
        from hermes_cli.fork_ext.boot_reconcile import (
            AUDITOR_ORCHESTRATOR_DEFAULT,
            CURATED_OPENROUTER_IMAGE_MODEL,
            FALLBACK_MODEL_DEFAULT,
        )
    except Exception:
        return (
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-flash-0731",
            "bytedance-seed/seedream-4.5",
        )
    return (
        FALLBACK_MODEL_DEFAULT,
        AUDITOR_ORCHESTRATOR_DEFAULT,
        CURATED_OPENROUTER_IMAGE_MODEL,
    )


def _env_site(name, var, *, default=None, needs_tools=True, is_judge=False,
              needs_image_input=False, is_image=False):
    value = (os.environ.get(var) or "").strip()
    if value:
        return Site(
            name,
            value,
            env_var=var,
            needs_tools=needs_tools,
            is_judge=is_judge,
            needs_image_input=needs_image_input,
            is_image=is_image,
            source="env",
        )
    if default is None:
        return None
    return Site(
        name,
        default,
        env_var=var,
        needs_tools=needs_tools,
        is_judge=is_judge,
        needs_image_input=needs_image_input,
        is_image=is_image,
        source="code",
    )


def _read_jobs_file(path):
    data = _read_json(path, [])
    if isinstance(data, dict):
        data = data.get("jobs") or []
    return data if isinstance(data, list) else []


def _job_sites(home: Path):
    stores = [("main", home / "cron" / "jobs.json")]
    profiles = home / "profiles"
    if profiles.is_dir():
        for profile in sorted(profiles.iterdir()):
            if profile.is_dir():
                stores.append((profile.name, profile / "cron" / "jobs.json"))
    sites = []
    for store, path in stores:
        for job in _read_jobs_file(path):
            if not isinstance(job, dict):
                continue
            model = str(job.get("model") or "").strip()
            if not model or job.get("no_agent") is True:
                continue
            provider = str(job.get("provider") or "").strip()
            if provider and provider != "openrouter":
                continue
            job_id = str(job.get("id") or "")
            job_name = str(job.get("name") or "")
            site = Site(
                f"job {store}/{job_id} {job_name}".rstrip(),
                model,
                needs_tools=True,
                source="job",
            )
            site.jobs = [{"store": store, "id": job_id}]
            sites.append(site)
    return sites


def _missing_finding(site, _reason=None):
    return Finding(
        site=site.name,
        kind="falta",
        current_id=site.current_id,
        cand_id=None,
        reasons=[],
        current_stats=None,
        cand_stats=None,
        catalog=site.catalog,
        is_image=site.is_image,
        site_ref=site,
    )


def _update_queue(home: Path, findings):
    path = home / "model-watch" / "queue.json"
    data = _read_json(path, {})
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        entries = []
    kept = [
        entry for entry in entries
        if isinstance(entry, dict) and entry.get("status") != "queued"
    ]

    queued = {}
    for finding in findings:
        if finding.kind != "auto":
            continue
        site = finding.site_ref
        if site is None or site.source not in ("env", "job"):
            continue
        key = (finding.current_id, finding.cand_id)
        entry = queued.get(key)
        if entry is None:
            entry = {
                "old": finding.current_id,
                "new": finding.cand_id,
                "env_vars": [],
                "jobs": [],
                "status": "queued",
                "queued_at": _now(),
            }
            queued[key] = entry
        if site.env_var and site.env_var not in entry["env_vars"]:
            entry["env_vars"].append(site.env_var)
        for job in site.jobs:
            if job not in entry["jobs"]:
                entry["jobs"].append(job)

    kept = [
        entry for entry in kept
        if (entry.get("old"), entry.get("new")) not in queued
    ]
    kept.sort(key=lambda entry: str(entry.get("applied_at") or entry.get("queued_at") or ""),
              reverse=True)
    kept = kept[:20]

    _write_json(path, {"entries": list(queued.values()) + kept})


def run_hermes(args) -> int:
    home = _hermes_home(args)
    notes = []

    try:
        or_models = _data(fetch_json(OR_MODELS))
    except Exception as exc:
        print(f"model_watch: no pude leer {OR_MODELS}: {type(exc).__name__}")
        return 1

    image_models = []
    image_catalog_ok = True
    try:
        image_models = _data(fetch_json(OR_IMAGE_MODELS))
    except Exception:
        image_models = []
        image_catalog_ok = False
        notes.append(IMAGE_CATALOG_FAILED_NOTE)

    zdr_slugs = set()
    try:
        for endpoint in _data(fetch_json(OR_ZDR)):
            name = endpoint.get("name") or ""
            if " | " in name:
                zdr_slugs.add(name.split(" | ", 1)[1].strip())
    except Exception:
        zdr_slugs = set()
        notes.append(ZDR_FAILED_NOTE)

    eci = {}
    try:
        eci = _parse_eci(fetch_text(ECI_CSV))
    except Exception:
        eci = {}
        notes.append(ECI_MISSING_NOTE)

    go_records = []
    zen_records = []
    oc_catalogs_ok = True
    try:
        go_records = _data(fetch_json(GO_MODELS))
    except Exception:
        go_records = []
        oc_catalogs_ok = False
    try:
        zen_records = _data(fetch_json(ZEN_MODELS))
    except Exception:
        zen_records = []
        oc_catalogs_ok = False
    if not oc_catalogs_ok:
        notes.append(OC_CATALOGS_FAILED_NOTE)
    families = Families(go_records, zen_records)

    fallback_default, orchestrator_default, image_default = _load_defaults()

    sites = []
    principal = _env_site("Principal", "HERMES_DEFAULT_MODEL")
    if principal:
        sites.append(principal)
    fallback = _env_site(
        "Fallback", "HERMES_FALLBACK_MODEL", default=fallback_default
    )
    if fallback:
        sites.append(fallback)
    orchestrator = _env_site(
        "Auditor orquestador",
        "HERMES_AUDITOR_ORCHESTRATOR_MODEL",
        default=orchestrator_default,
    )
    if orchestrator:
        sites.append(orchestrator)
    system_judge = _env_site(
        "Auditor juez sistema",
        "HERMES_AUDITOR_SYSTEM_MODEL",
        needs_tools=False,
        is_judge=True,
    )
    if system_judge:
        sites.append(system_judge)
    content_judge = _env_site(
        "Auditor juez contenido",
        "HERMES_AUDITOR_CONTENT_MODEL",
        needs_tools=False,
        is_judge=True,
    )
    if content_judge:
        sites.append(content_judge)
    vision = _env_site(
        "Visión auxiliar",
        "AUXILIARY_VISION_MODEL",
        needs_tools=False,
        needs_image_input=True,
    )
    if vision:
        sites.append(vision)
    image = _env_site(
        "Imagen",
        "OPENROUTER_IMAGE_MODEL",
        default=image_default,
        needs_tools=False,
        is_image=True,
    )
    if image:
        sites.append(image)

    sites.extend(_job_sites(home))

    ctx = Context(
        "openrouter",
        families=families,
        zdr_slugs=zdr_slugs,
        today=date.today(),
        eci=eci,
    )

    findings = []
    missing_aa = []
    for site in sites:
        if site.is_image and not image_catalog_ok:
            continue
        catalog = image_models if site.is_image else or_models
        by_id = {model.get("id"): model for model in catalog if isinstance(model, dict)}
        current = by_id.get(site.current_id)
        if current is None:
            findings.append(_missing_finding(site, MISSING_OR_REASON))
            continue
        if not site.is_image and _aa(current) is None and site.current_id not in missing_aa:
            missing_aa.append(site.current_id)
        findings.extend(classify(site, current, list(by_id.values()), ctx))

    if missing_aa:
        notes.append(
            "Sin índice AA (no se comparan otras familias): " + ", ".join(missing_aa)
        )

    if args.dry_run:
        if findings or notes:
            print(render_report(findings, notes))
        return 0

    state_dir = home / "model-watch"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    fingerprint = _fingerprint(findings, notes)
    previous = _read_json(state_path, {})
    stored = previous.get("fingerprint") if isinstance(previous, dict) else None
    if stored != fingerprint:
        if findings or notes:
            print(render_report(findings, notes))
        _write_json(state_path, {"fingerprint": fingerprint})

    _update_queue(home, findings)
    return 0


# ── queue-env / apply-pins ──────────────────────────────────────────────────


def run_queue_env(args) -> int:
    path = _hermes_home(args) / "model-watch" / "queue.json"
    data = _read_json(path, {})
    entries = data.get("entries") if isinstance(data, dict) else None
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get("status") != "queued":
            continue
        old = entry.get("old")
        new = entry.get("new")
        if not _safe_id(new):
            continue
        for var in entry.get("env_vars") or []:
            if var in MODEL_ENV_VARS and os.environ.get(var) == old:
                print(f"{var}={new}")
    return 0


def run_apply_pins(args) -> int:
    path = _hermes_home(args) / "model-watch" / "queue.json"
    data = _read_json(path, {})
    entries = data.get("entries") if isinstance(data, dict) else None
    if not entries:
        return 0

    from cron.jobs import get_job, update_job, use_cron_store

    home = _hermes_home(args)
    changed = False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "queued":
            continue
        old = entry.get("old")
        new = entry.get("new")
        if not _safe_id(new):
            continue
        env_vars = entry.get("env_vars") or []
        values = [os.environ.get(var) for var in env_vars]
        if all(value == new for value in values):
            for job in entry.get("jobs") or []:
                store = job.get("store") or "main"
                job_id = job.get("id")
                if not job_id:
                    continue
                if store != "main" and (
                        not SAFE_STORE.fullmatch(str(store))
                        or not (home / "profiles" / str(store)).is_dir()):
                    continue
                store_home = home if store == "main" else home / "profiles" / store
                with use_cron_store(store_home):
                    record = get_job(job_id)
                    if record and record.get("model") == old:
                        update_job(job_id, {"model": new})
            entry["status"] = "applied"
            entry["applied_at"] = _now()
            print(f"[model_watch] applied {old} -> {new}")
            changed = True
        elif any(value not in (old, new) for value in values):
            entry["status"] = "stale"
            print(f"[model_watch] stale {old} -> {new}")
            changed = True

    if changed:
        _write_json(path, data)
    return 0


# ── OpenCode mode ───────────────────────────────────────────────────────────


OC_PREFIXES = ("opencode-go/", "opencode/")


def _oc_prefix(model_id) -> str:
    s = str(model_id or "")
    for prefix in OC_PREFIXES:
        if s.startswith(prefix):
            return prefix
    return ""


def _strip_oc_prefix(model_id) -> str:
    s = str(model_id or "")
    for prefix in OC_PREFIXES:
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _chain_ids(text):
    ids = []
    for line in text.splitlines():
        match = CHAIN_LINE.match(line)
        if not match:
            continue
        for token in match.group(2).split():
            if not (token.startswith("opencode/") or token.startswith("opencode-go/")):
                continue
            if token not in ids:
                ids.append(token)
    return ids


def _rewrite_chains(text, mapping):
    lines = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        match = CHAIN_LINE.match(body)
        if not match:
            lines.append(line)
            continue
        tokens = [
            mapping[token] if _safe_id(mapping.get(token)) else token
            for token in match.group(2).split()
        ]
        unique = []
        for token in tokens:
            if token not in unique:
                unique.append(token)
        lines.append(f"{match.group(1)}{' '.join(unique)}{match.group(3)}{match.group(4)}{ending}")
    return "".join(lines)


def run_opencode(args) -> int:
    delegate = Path(args.delegate or DEFAULT_DELEGATE).expanduser()
    try:
        text = delegate.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"model_watch: no pude leer {delegate}: {type(exc).__name__}")
        return 1

    try:
        go_records = _data(fetch_json(GO_MODELS))
    except Exception as exc:
        print(f"model_watch: no pude leer {GO_MODELS}: {type(exc).__name__}")
        return 1
    try:
        zen_records = _data(fetch_json(ZEN_MODELS))
    except Exception as exc:
        print(f"model_watch: no pude leer {ZEN_MODELS}: {type(exc).__name__}")
        return 1

    notes = []
    eci = {}
    try:
        eci = _parse_eci(fetch_text(ECI_CSV))
    except Exception:
        eci = {}
        notes.append(ECI_MISSING_NOTE)

    or_records = []
    try:
        or_records = _data(fetch_json(OR_MODELS))
    except Exception:
        or_records = []

    families = Families(go_records, zen_records)
    catalogs = {"opencode-zen": zen_records, "opencode-go": go_records}

    findings = []
    missing_aa = []
    for model_id in _chain_ids(text):
        catalog = "opencode-go" if model_id.startswith("opencode-go/") else "opencode-zen"
        prefix = _oc_prefix(model_id)
        records = catalogs[catalog]
        by_bare = {}
        for record in records:
            if isinstance(record, dict) and record.get("id"):
                by_bare[_strip_oc_prefix(record["id"])] = record
        ctx = Context(
            catalog,
            families=families,
            today=date.today(),
            or_records=or_records,
            eci=eci,
        )
        site = Site(
            model_id,
            model_id,
            needs_tools=True,
            source="chain",
            catalog=catalog,
        )
        bare = _strip_oc_prefix(model_id)
        current = by_bare.get(bare)
        if current is None:
            findings.append(_missing_finding(site, MISSING_OC_REASON))
            continue
        current_prefixed = {**current, "id": model_id}
        if _index(current_prefixed, ctx) is None and model_id not in missing_aa:
            missing_aa.append(model_id)
        candidates = [
            {**record, "id": prefix + _strip_oc_prefix(record.get("id", ""))}
            for record in records
            if isinstance(record, dict) and record.get("id")
        ]
        findings.extend(classify(site, current_prefixed, candidates, ctx))

    if missing_aa:
        notes.append(
            "Sin índice AA (no se comparan otras familias): " + ", ".join(missing_aa)
        )

    mapping = {
        finding.current_id: finding.cand_id
        for finding in findings
        if finding.kind == "auto" and finding.cand_id
    }
    if mapping and not args.dry_run:
        rewritten = _rewrite_chains(text, mapping)
        if rewritten != text:
            _write_text(delegate, rewritten)

    if findings or notes:
        print(render_report(findings, notes))
    else:
        print("Sin cambios.")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="model_watch")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("hermes", "opencode", "queue-env", "apply-pins"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--dry-run", action="store_true")
        subparser.add_argument("--hermes-home", default=None)
        subparser.add_argument("--delegate", default=None)
    args = parser.parse_args(argv)

    if args.command == "hermes":
        return run_hermes(args)
    if args.command == "opencode":
        return run_opencode(args)
    if args.command == "queue-env":
        return run_queue_env(args)
    return run_apply_pins(args)


if __name__ == "__main__":
    sys.exit(main())
