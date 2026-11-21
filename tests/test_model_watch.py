"""Tests for model_watch/watch.py.

No network: every catalog comes from tests/fixtures/model_watch and fetch_json /
fetch_text are faked by ``install_catalogs``.
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from model_watch import watch

FIXTURES = Path(__file__).parent / "fixtures" / "model_watch"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _or_models():
    return {model["id"]: model for model in _load("or_models.json")["data"]}


def _image_models():
    return {model["id"]: model for model in _load("or_image_models.json")["data"]}


def _zen_records():
    return _load("zen_models.json")["data"]


def install_catalogs(monkeypatch, fail_url=None, fail_urls=()):
    payloads = {
        watch.OR_MODELS: _load("or_models.json"),
        watch.OR_IMAGE_MODELS: _load("or_image_models.json"),
        watch.OR_ZDR: _load("or_zdr.json"),
        watch.GO_MODELS: _load("go_models.json"),
        watch.ZEN_MODELS: _load("zen_models.json"),
    }
    eci_text = (FIXTURES / "eci_scores.csv").read_text(encoding="utf-8")
    failed = set(fail_urls)
    if fail_url:
        failed.add(fail_url)

    def fake_fetch_json(url):
        if url in failed:
            raise OSError("boom")
        return payloads[url]

    def fake_fetch_text(url):
        if url in failed:
            raise OSError("boom")
        if url == watch.ECI_CSV:
            return eci_text
        return json.dumps(payloads[url])

    monkeypatch.setattr(watch, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(watch, "fetch_text", fake_fetch_text)


def _ctx(zdr_slugs=(), families=None, today=date(2026, 1, 1), catalog="openrouter",
         or_records=(), eci=None):
    return watch.Context(
        catalog,
        families=families or watch.Families(),
        zdr_slugs=set(zdr_slugs),
        today=today,
        or_records=or_records,
        eci=eci or {},
    )


def _site(model_id="deepseek/deepseek-v4-flash", *, source="env", **kwargs):
    return watch.Site("Principal", model_id, source=source, **kwargs)


def _only(findings, kind):
    return [finding for finding in findings if finding.kind == kind]


# ── Names and families ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("deepseek/deepseek-v4.1-flash-1017", "deepseek-v4.1-flash"),
        ("qwen/qwen3.8-max-0902", "qwen3.8-max"),
        ("opencode/deepseek-v4.1-flash-free", "deepseek-v4.1-flash-free"),
        ("deepseek/deepseek-v4.1-flash:free", "deepseek-v4.1-flash"),
        ("model-with-20250101", "model-with"),
    ],
)
def test_base_name(model_id, expected):
    assert watch.base_name(model_id) == expected


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("kimi-k3.1-pro", "kimi:pro"),
        ("mimo-v2.6-flash", "mimo:flash"),
        ("hy4", "hy:hy4"),
        ("seedream-4.5", "seedream:seedream-4.5"),
        ("glm-5.3", "glm:glm-5.3"),
        ("qwen3.8-max", "qwen:max"),
        ("deepseek-v4.1-flash-free", "deepseek:flash:free"),
    ],
)
def test_fallback_family(model_id, expected):
    assert watch.fallback_family(model_id) == expected


def test_catalog_family_wins_over_fallback():
    families = watch.Families(zen_records=[
        {"id": "opencode/glm-5.3", "canonical_id": None, "family": "glm"},
        {"id": "opencode/glm-5.4", "canonical_id": None, "family": "glm"},
        {"id": "opencode/deepseek-v4.1-flash", "canonical_id": None,
         "family": "deepseek-flash"},
        {"id": "opencode/deepseek-v4.1-pro", "canonical_id": None,
         "family": "deepseek-pro"},
    ])
    assert families.same_family("opencode/glm-5.3", "opencode/glm-5.4")
    assert not families.same_family(
        "opencode/deepseek-v4.1-flash", "opencode/deepseek-v4.1-pro"
    )
    assert watch.fallback_family("glm-5.3") != watch.fallback_family("glm-5.4")


def test_same_family_falls_back_when_a_model_is_missing():
    families = watch.Families(zen_records=_zen_records())
    assert families.same_family("other/foo-flash", "other/foo-flash-0925")
    assert not families.same_family("other/foo-flash", "other/foo-pro")
    # One side known to the catalog, the other not: fallback keys on both.
    assert families.same_family("opencode/deepseek-v4.1-flash", "other/deepseek-flash")


# ── Prices ──────────────────────────────────────────────────────────────────


def test_le_price_with_a_key_only_in_the_candidate():
    assert not watch.le_price({"input": 1.0, "image": 0.1}, {"input": 1.0})
    assert watch.le_price({"input": 1.0}, {"input": 1.0, "image": 0.1})
    assert not watch.lower_price({"input": 1.0, "image": 0.1}, {"input": 1.0})
    assert watch.lower_price({"input": 0.5}, {"input": 1.0})


# ── classify ────────────────────────────────────────────────────────────────


def test_classify_newer_same_family_cheaper_is_auto():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [models["deepseek/deepseek-v4.2-flash"]],
        _ctx(zdr_slugs=["deepseek/deepseek-v4.2-flash"]),
    )
    assert [finding.kind for finding in findings] == ["auto"]
    assert findings[0].cand_id == "deepseek/deepseek-v4.2-flash"
    assert findings[0].reasons == []


def test_classify_newer_same_family_dearer_is_avisar():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [models["deepseek/deepseek-v4.3-flash"]],
        _ctx(zdr_slugs=["deepseek/deepseek-v4.3-flash"]),
    )
    assert [finding.kind for finding in findings] == ["avisar"]
    assert findings[0].reasons == [watch.DEARER_REASON]


def test_classify_same_family_newer_with_lower_index_is_silent():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [models["deepseek/deepseek-v4.4-flash"]],
        _ctx(zdr_slugs=["deepseek/deepseek-v4.4-flash"]),
    )
    assert findings == []


def test_classify_other_family_more_capable_cheaper_is_sugerir():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [models["qwen/qwen3.8-max"]],
        _ctx(zdr_slugs=["qwen/qwen3.8-max"]),
    )
    assert [finding.kind for finding in findings] == ["sugerir"]
    assert findings[0].reasons == [watch.SUGGEST_REASON]


def test_classify_other_family_without_index_is_silent():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [models["mimo/mimo-v2.6-flash"]],
        _ctx(zdr_slugs=["mimo/mimo-v2.6-flash"]),
    )
    assert findings == []


def test_classify_ignores_candidates_without_zdr():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [models["nvidia/nemotron-ultra"]],
        _ctx(),
    )
    assert findings == []


def test_classify_ignores_free_suffix_and_anthropic():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [
            models["deepseek/deepseek-v4.5-flash:free"],
            models["anthropic/claude-opus-9"],
        ],
        _ctx(zdr_slugs=[
            "deepseek/deepseek-v4.5-flash",
            "anthropic/claude-opus-9",
        ]),
    )
    assert findings == []


def test_classify_judge_site_downgrades_auto_to_avisar():
    models = _or_models()
    findings = watch.classify(
        _site("qwen/qwen3.7-max", is_judge=True, needs_tools=False),
        models["qwen/qwen3.7-max"],
        [models["qwen/qwen3.9-max"]],
        _ctx(zdr_slugs=["qwen/qwen3.9-max"]),
    )
    assert [finding.kind for finding in findings] == ["avisar"]
    assert watch.JUDGE_REASON in findings[0].reasons


def test_classify_judge_site_ignores_mandatory_reasoning():
    models = _or_models()
    findings = watch.classify(
        _site("qwen/qwen3.7-max", is_judge=True, needs_tools=False),
        models["qwen/qwen3.7-max"],
        [models["qwen/qwen4-max"]],
        _ctx(zdr_slugs=["qwen/qwen4-max"]),
    )
    assert findings == []


def test_classify_needs_tools_excludes_models_without_tools():
    models = _or_models()
    current = models["kimi/kimi-k2-flash"]
    candidate = models["kimi/kimi-k3-flash"]
    ctx = _ctx(zdr_slugs=["kimi/kimi-k3-flash"])
    assert watch.classify(
        _site("kimi/kimi-k2-flash"), current, [candidate], ctx
    ) == []
    findings = watch.classify(
        _site("kimi/kimi-k2-flash", needs_tools=False), current, [candidate], ctx
    )
    assert [finding.kind for finding in findings] == ["auto"]


def test_classify_image_site_never_sugests():
    images = _image_models()
    findings = watch.classify(
        _site("bytedance-seed/seedream-4.5", is_image=True, needs_tools=False),
        images["bytedance-seed/seedream-4.5"],
        [images["bytedance-seed/seedream-5.0"]],
        _ctx(
            zdr_slugs=[
                "bytedance-seed/seedream-4.5",
                "bytedance-seed/seedream-5.0",
            ],
            families=watch.Families(zen_records=_zen_records()),
        ),
    )
    assert [finding.kind for finding in findings] == ["auto"]
    assert not _only(findings, "sugerir")


def test_classify_keeps_at_most_one_auto():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [
            models["deepseek/deepseek-v4.2-flash"],
            models["deepseek/deepseek-v4.6-flash"],
        ],
        _ctx(zdr_slugs=[
            "deepseek/deepseek-v4.2-flash",
            "deepseek/deepseek-v4.6-flash",
        ]),
    )
    autos = _only(findings, "auto")
    assert len(autos) == 1
    assert autos[0].cand_id == "deepseek/deepseek-v4.2-flash"


def test_classify_code_default_downgrades_auto_to_avisar():
    models = _or_models()
    findings = watch.classify(
        _site(source="code"),
        models["deepseek/deepseek-v4-flash"],
        [models["deepseek/deepseek-v4.2-flash"]],
        _ctx(zdr_slugs=["deepseek/deepseek-v4.2-flash"]),
    )
    assert [finding.kind for finding in findings] == ["avisar"]
    assert watch.CODE_REASON in findings[0].reasons


def test_classify_drops_avisar_for_the_auto_family():
    models = _or_models()
    findings = watch.classify(
        _site(),
        models["deepseek/deepseek-v4-flash"],
        [
            models["deepseek/deepseek-v4.2-flash"],
            models["deepseek/deepseek-v4.3-flash"],
        ],
        _ctx(zdr_slugs=[
            "deepseek/deepseek-v4.2-flash",
            "deepseek/deepseek-v4.3-flash",
        ]),
    )
    assert [finding.kind for finding in findings] == ["auto"]


# ── Hermes mode ─────────────────────────────────────────────────────────────


def _set_hermes_env(monkeypatch, primary="deepseek/deepseek-v4-flash"):
    monkeypatch.setenv("HERMES_DEFAULT_MODEL", primary)
    monkeypatch.setenv("HERMES_FALLBACK_MODEL", "deepseek/deepseek-v4.2-flash")
    monkeypatch.setenv(
        "HERMES_AUDITOR_ORCHESTRATOR_MODEL", "deepseek/deepseek-v4.2-flash"
    )
    monkeypatch.setenv("OPENROUTER_IMAGE_MODEL", "bytedance-seed/seedream-4.5")
    for var in (
        "HERMES_AUDITOR_SYSTEM_MODEL",
        "HERMES_AUDITOR_CONTENT_MODEL",
        "AUXILIARY_VISION_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_job(home, model, name="pinned", store="main"):
    from cron.jobs import create_job, use_cron_store

    store_home = home if store == "main" else home / "profiles" / store
    store_home.mkdir(parents=True, exist_ok=True)
    with use_cron_store(store_home):
        return create_job("task", "0 9 * * *", name=name, model=model)


def test_hermes_end_to_end_state_and_queue(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _set_hermes_env(monkeypatch)
    install_catalogs(monkeypatch)
    job = _make_job(home, "deepseek/deepseek-v4-flash")

    assert watch.main(["hermes", "--hermes-home", str(home)]) == 0
    first = capsys.readouterr().out
    assert "🔎 Modelos" in first
    assert "deepseek/deepseek-v4-flash → deepseek/deepseek-v4.2-flash" in first
    assert "Main" not in first

    assert watch.main(["hermes", "--hermes-home", str(home)]) == 0
    assert capsys.readouterr().out == ""

    queue = json.loads((home / "model-watch" / "queue.json").read_text(encoding="utf-8"))
    entries = queue["entries"]
    entry = next(
        item for item in entries
        if item["old"] == "deepseek/deepseek-v4-flash"
        and item["new"] == "deepseek/deepseek-v4.2-flash"
    )
    assert entry["status"] == "queued"
    assert entry["env_vars"] == ["HERMES_DEFAULT_MODEL"]
    assert entry["jobs"] == [{"store": "main", "id": job["id"]}]
    assert entry["queued_at"]

    state = json.loads((home / "model-watch" / "state.json").read_text(encoding="utf-8"))
    assert len(state["fingerprint"]) == 64


def test_hermes_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _set_hermes_env(monkeypatch)
    install_catalogs(monkeypatch)

    assert watch.main(["hermes", "--dry-run", "--hermes-home", str(home)]) == 0
    assert "🔎 Modelos" in capsys.readouterr().out
    assert not (home / "model-watch").exists()


def test_hermes_reports_a_retired_model(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _set_hermes_env(monkeypatch, primary="ghost/model-gone")
    install_catalogs(monkeypatch)

    assert watch.main(["hermes", "--hermes-home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "Ya no están en el catálogo" in out
    assert "• ghost/model-gone" in out
    assert "  en: Principal" in out
    assert "```" not in out.split("Ya no están en el catálogo")[1].split("\n\nActualizado")[0]


def test_hermes_catalog_failure_exits_one(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _set_hermes_env(monkeypatch)
    install_catalogs(monkeypatch, fail_url=watch.OR_MODELS)

    assert watch.main(["hermes", "--hermes-home", str(home)]) == 1
    assert capsys.readouterr().out.strip() == (
        f"model_watch: no pude leer {watch.OR_MODELS}: OSError"
    )


# ── queue-env ───────────────────────────────────────────────────────────────


def _write_queue(home, entries):
    path = home / "model-watch" / "queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    return path


def test_queue_env_prints_only_vars_at_the_old_value(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _write_queue(home, [
        {
            "old": "old/model",
            "new": "new/model",
            "env_vars": ["HERMES_DEFAULT_MODEL", "OTHER_MODEL"],
            "jobs": [],
            "status": "queued",
        },
        {
            "old": "a/b",
            "new": "c/d",
            "env_vars": ["APPLIED_MODEL"],
            "jobs": [],
            "status": "applied",
        },
    ])
    monkeypatch.setenv("HERMES_DEFAULT_MODEL", "old/model")
    monkeypatch.setenv("OTHER_MODEL", "other/model")
    monkeypatch.setenv("APPLIED_MODEL", "a/b")

    assert watch.main(["queue-env", "--hermes-home", str(home)]) == 0
    assert capsys.readouterr().out == "HERMES_DEFAULT_MODEL=new/model\n"


def test_queue_env_exits_zero_without_a_queue(tmp_path, capsys):
    assert watch.main(["queue-env", "--hermes-home", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


# ── apply-pins ──────────────────────────────────────────────────────────────


def _pinned_home(tmp_path, monkeypatch, env_value):
    home = tmp_path / "home"
    job = _make_job(home, "old/model", name="main job")
    profile_job = _make_job(home, "old/model", name="team job", store="team")
    _write_queue(home, [
        {
            "old": "old/model",
            "new": "new/model",
            "env_vars": ["HERMES_DEFAULT_MODEL"],
            "jobs": [
                {"store": "main", "id": job["id"]},
                {"store": "team", "id": profile_job["id"]},
            ],
            "status": "queued",
        }
    ])
    monkeypatch.setenv("HERMES_DEFAULT_MODEL", env_value)
    return home, job, profile_job


def _read_job(home, job_id, store="main"):
    from cron.jobs import get_job, use_cron_store

    store_home = home if store == "main" else home / "profiles" / store
    with use_cron_store(store_home):
        return get_job(job_id)


def _queue_entry(home):
    data = json.loads(
        (home / "model-watch" / "queue.json").read_text(encoding="utf-8")
    )
    return data["entries"][0]


def test_apply_pins_applies_jobs_and_env(tmp_path, monkeypatch, capsys):
    home, job, profile_job = _pinned_home(tmp_path, monkeypatch, "new/model")

    assert watch.main(["apply-pins", "--hermes-home", str(home)]) == 0
    assert "[model_watch] applied old/model -> new/model" in capsys.readouterr().out
    assert _read_job(home, job["id"])["model"] == "new/model"
    assert _read_job(home, profile_job["id"], store="team")["model"] == "new/model"
    entry = _queue_entry(home)
    assert entry["status"] == "applied"
    assert entry["applied_at"]


def test_apply_pins_keeps_the_entry_queued_at_the_old_value(tmp_path, monkeypatch, capsys):
    home, job, _ = _pinned_home(tmp_path, monkeypatch, "old/model")

    assert watch.main(["apply-pins", "--hermes-home", str(home)]) == 0
    assert capsys.readouterr().out == ""
    assert _read_job(home, job["id"])["model"] == "old/model"
    assert _queue_entry(home)["status"] == "queued"


def test_apply_pins_marks_an_unexpected_value_stale(tmp_path, monkeypatch, capsys):
    home, job, _ = _pinned_home(tmp_path, monkeypatch, "another/model")

    assert watch.main(["apply-pins", "--hermes-home", str(home)]) == 0
    assert "[model_watch] stale old/model -> new/model" in capsys.readouterr().out
    assert _read_job(home, job["id"])["model"] == "old/model"
    assert _queue_entry(home)["status"] == "stale"


def test_apply_pins_without_a_queue_is_a_no_op(tmp_path, capsys):
    assert watch.main(["apply-pins", "--hermes-home", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


# ── OpenCode mode ───────────────────────────────────────────────────────────


def _delegate_copy(tmp_path):
    target = tmp_path / "delegate.sh"
    shutil.copyfile(FIXTURES / "delegate.sh", target)
    return target


def test_opencode_rewrites_chains_and_deduplicates(tmp_path, monkeypatch, capsys):
    delegate = _delegate_copy(tmp_path)
    install_catalogs(monkeypatch)

    assert watch.main(["opencode", "--delegate", str(delegate)]) == 0
    out = capsys.readouterr().out
    assert "🔎 Modelos" in out
    assert "(OpenCode)" in out

    text = delegate.read_text(encoding="utf-8")
    assert 'FREE="opencode/deepseek-v4.1-flash-free opencode/deepseek-v4.2-flash"' in text
    assert 'GO="opencode-go/kimi-k3.2-pro"' in text
    assert 'MAX="opencode/deepseek-v4.2-flash"' in text
    assert 'ZEN="opencode/deepseek-v4.2-flash"' in text
    assert 'OTHER="keep   spacing  exactly"' in text
    assert '# FREE="opencode/deepseek-v4.1-flash"' in text


def test_opencode_dry_run_leaves_the_file_untouched(tmp_path, monkeypatch, capsys):
    delegate = _delegate_copy(tmp_path)
    original = delegate.read_bytes()
    install_catalogs(monkeypatch)

    assert watch.main(["opencode", "--dry-run", "--delegate", str(delegate)]) == 0
    assert "🔎 Modelos" in capsys.readouterr().out
    assert delegate.read_bytes() == original


def test_opencode_catalog_failure_exits_one(tmp_path, monkeypatch, capsys):
    delegate = _delegate_copy(tmp_path)
    install_catalogs(monkeypatch, fail_url=watch.GO_MODELS)

    assert watch.main(["opencode", "--delegate", str(delegate)]) == 1
    assert capsys.readouterr().out.strip() == (
        f"model_watch: no pude leer {watch.GO_MODELS}: OSError"
    )


def test_opencode_prefixed_chain_id_matches_bare_catalog(tmp_path, monkeypatch, capsys):
    """Regression #1: prefixed chain id must resolve against bare catalog ids."""
    delegate = _delegate_copy(tmp_path)
    install_catalogs(monkeypatch)

    assert watch.main(["opencode", "--delegate", str(delegate)]) == 0
    out = capsys.readouterr().out

    # Bare id is in catalog: must NOT be reported missing.
    if "Ya no están en el catálogo" in out:
        section = out.split("Ya no están en el catálogo", 1)[1]
        for heading in ("Actualizado / en cola (automático)", "Avisar:", "Sugerencia:", "Notas"):
            if heading in section:
                section = section.split(heading, 1)[0]
        assert "opencode-go/kimi-k3.1-pro" not in section

    # Auto upgrade written back WITH the same prefix.
    text = delegate.read_text(encoding="utf-8")
    assert 'GO="opencode-go/kimi-k3.2-pro"' in text


# ── Corrective spec tests ────────────────────────────────────────────────────


def test_eci_normalization_matches_spec_examples():
    table = watch._parse_eci("Model,Display name,eci\nx,Kimi K3.1 Pro,111\n")
    record = {"id": "moonshotai/kimi-k3.1-pro", "canonical_slug": None, "canonical_id": None}
    assert watch._fmt_eci(record, table) == 111.0

    table = watch._parse_eci("Model,Display name,eci\nx,GLM-5.3-Flash,77\n")
    record = {"id": "z-ai/glm-5.3-flash", "canonical_slug": None, "canonical_id": None}
    assert watch._fmt_eci(record, table) == 77.0


def test_eci_prefers_the_snapshot_row_over_the_alias():
    table = watch._parse_eci(
        "Model,Display name,eci\n"
        "a,DeepSeek V4.1 Flash 1017,159.41\n"
        "b,DeepSeek V4.1 Flash,157.01\n")
    snapshot = {"id": "deepseek/deepseek-v4.1-flash-1017", "canonical_slug": None}
    alias = {"id": "deepseek/deepseek-v4.1-flash", "canonical_slug": None}
    assert watch._fmt_eci(snapshot, table) == 159.41
    assert watch._fmt_eci(alias, table) == 157.01


def test_grouped_report_renders_one_block_per_finding_group(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    # Two sites pin the same model -> same auto finding should be grouped.
    monkeypatch.setenv("HERMES_DEFAULT_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("HERMES_FALLBACK_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.setenv("HERMES_AUDITOR_ORCHESTRATOR_MODEL", "deepseek/deepseek-v4.2-flash")
    monkeypatch.setenv("OPENROUTER_IMAGE_MODEL", "bytedance-seed/seedream-4.5")
    for var in (
        "HERMES_AUDITOR_SYSTEM_MODEL",
        "HERMES_AUDITOR_CONTENT_MODEL",
        "AUXILIARY_VISION_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    install_catalogs(monkeypatch)

    assert watch.main(["hermes", "--dry-run", "--hermes-home", str(home)]) == 0
    out = capsys.readouterr().out
    assert out.count("• deepseek/deepseek-v4-flash → deepseek/deepseek-v4.2-flash") == 1
    assert "  en: Principal, Fallback" in out
    # single comparison table for the grouped block
    block = out.split("• deepseek/deepseek-v4-flash → deepseek/deepseek-v4.2-flash", 1)[1]
    block = block.split("\n\n")[0]
    assert block.count(watch.TEXT_TABLE_HEADER) == 1


def test_missing_models_use_falta_section_and_queue_ignores_them(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _set_hermes_env(monkeypatch, primary="ghost/model-gone")
    install_catalogs(monkeypatch)

    assert watch.main(["hermes", "--hermes-home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "Ya no están en el catálogo" in out
    assert "• ghost/model-gone" in out

    queue = json.loads((home / "model-watch" / "queue.json").read_text(encoding="utf-8"))
    assert all(e.get("old") != "ghost/model-gone" for e in queue.get("entries", []))


def test_hermes_zdr_failure_adds_note_and_no_candidates(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _set_hermes_env(monkeypatch)
    install_catalogs(monkeypatch, fail_url=watch.OR_ZDR)

    assert watch.main(["hermes", "--dry-run", "--hermes-home", str(home)]) == 0
    out = capsys.readouterr().out
    assert watch.ZDR_FAILED_NOTE in out
    # no auto/avisar/sugerir from OpenRouter because ZDR blocks eligibility
    assert "Actualizado / en cola (automático)" not in out
    assert "Sugerencia: otra familia" not in out


def test_hermes_oc_catalog_failure_adds_note(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _set_hermes_env(monkeypatch)
    install_catalogs(monkeypatch, fail_url=watch.GO_MODELS)

    assert watch.main(["hermes", "--dry-run", "--hermes-home", str(home)]) == 0
    out = capsys.readouterr().out
    assert watch.OC_CATALOGS_FAILED_NOTE in out


def test_hermes_image_catalog_failure_adds_note_and_skips_image_site(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    _set_hermes_env(monkeypatch)
    install_catalogs(monkeypatch, fail_url=watch.OR_IMAGE_MODELS)

    assert watch.main(["hermes", "--dry-run", "--hermes-home", str(home)]) == 0
    out = capsys.readouterr().out
    assert watch.IMAGE_CATALOG_FAILED_NOTE in out
    assert "bytedance-seed/seedream-4.5" not in out


def test_hermes_notes_only_run_still_prints_and_fingerprint_includes_notes(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    # Pin only an image model and fail its catalog -> only note, no findings.
    monkeypatch.setattr(watch, "_load_defaults", lambda: (None, None, None))
    for var in (
        "HERMES_DEFAULT_MODEL",
        "HERMES_FALLBACK_MODEL",
        "HERMES_AUDITOR_ORCHESTRATOR_MODEL",
        "HERMES_AUDITOR_SYSTEM_MODEL",
        "HERMES_AUDITOR_CONTENT_MODEL",
        "AUXILIARY_VISION_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENROUTER_IMAGE_MODEL", "bytedance-seed/seedream-4.5")
    install_catalogs(monkeypatch, fail_url=watch.OR_IMAGE_MODELS)

    assert watch.main(["hermes", "--hermes-home", str(home)]) == 0
    out = capsys.readouterr().out
    assert watch.IMAGE_CATALOG_FAILED_NOTE in out
    assert "Ya no están en el catálogo" not in out
    state = json.loads((home / "model-watch" / "state.json").read_text(encoding="utf-8"))
    fp = state["fingerprint"]

    # Same notes => no print on second run
    assert watch.main(["hermes", "--hermes-home", str(home)]) == 0
    assert capsys.readouterr().out == ""
    state2 = json.loads((home / "model-watch" / "state.json").read_text(encoding="utf-8"))
    assert state2["fingerprint"] == fp


def test_aa_missing_note_lists_models_without_index(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setattr(watch, "_load_defaults", lambda: (None, None, None))
    for var in (
        "HERMES_FALLBACK_MODEL",
        "HERMES_AUDITOR_ORCHESTRATOR_MODEL",
        "HERMES_AUDITOR_SYSTEM_MODEL",
        "HERMES_AUDITOR_CONTENT_MODEL",
        "AUXILIARY_VISION_MODEL",
        "OPENROUTER_IMAGE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_DEFAULT_MODEL", "mimo/mimo-v2.6-flash")
    install_catalogs(monkeypatch)

    assert watch.main(["hermes", "--dry-run", "--hermes-home", str(home)]) == 0
    out = capsys.readouterr().out
    assert "Sin índice AA (no se comparan otras familias): mimo/mimo-v2.6-flash" in out


# ── Untrusted catalog / queue data (security review, 2026-11-21) ─────────────


def test_a_hostile_catalog_id_never_reaches_delegate_sh():
    text = 'FREE="opencode/a-free opencode/b-free"\n'
    bad = 'opencode/x"; touch /tmp/pwned; echo "'
    out = watch._rewrite_chains(text, {"opencode/a-free": bad, "opencode/b-free": "opencode/c-free"})
    assert out == 'FREE="opencode/a-free opencode/c-free"\n'


def test_unsafe_ids_are_not_candidates():
    site = watch.Site("x", "opencode/a", source="chain", catalog="opencode-zen")
    ctx = watch.Context("opencode-zen")
    cand = {"id": "opencode/$(id)", "type": "chat", "capabilities": {"tools": {"supported": True}}}
    assert not watch._eligible_opencode(site, {"id": "opencode/a"}, cand, ctx)


def test_queue_env_prints_only_model_variables_and_plain_ids(tmp_path, monkeypatch, capsys):
    qdir = tmp_path / "model-watch"
    qdir.mkdir()
    (qdir / "queue.json").write_text(json.dumps({"entries": [
        {"old": "a/m", "new": "a/m2", "env_vars": ["HERMES_DEFAULT_MODEL", "TELEGRAM_ALLOWED_USERS"],
         "jobs": [], "status": "queued"},
        {"old": "a/m", "new": "a/m2\nTELEGRAM_ALLOWED_USERS=1", "env_vars": ["HERMES_FALLBACK_MODEL"],
         "jobs": [], "status": "queued"},
    ]}))
    for var in ("HERMES_DEFAULT_MODEL", "TELEGRAM_ALLOWED_USERS", "HERMES_FALLBACK_MODEL"):
        monkeypatch.setenv(var, "a/m")
    assert watch.main(["queue-env", "--hermes-home", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "HERMES_DEFAULT_MODEL=a/m2\n"


def test_apply_pins_ignores_a_store_outside_profiles(tmp_path, monkeypatch):
    qdir = tmp_path / "model-watch"
    qdir.mkdir()
    (qdir / "queue.json").write_text(json.dumps({"entries": [
        {"old": "a/m", "new": "a/m2", "env_vars": [],
         "jobs": [{"store": "../..", "id": "j1"}], "status": "queued"},
    ]}))
    calls = []
    import cron.jobs as jobs_mod

    def fake_store(home):
        calls.append(home)
        raise AssertionError("a store outside profiles/ must never be opened")

    monkeypatch.setattr(jobs_mod, "use_cron_store", fake_store)
    assert watch.main(["apply-pins", "--hermes-home", str(tmp_path)]) == 0
    assert calls == []
