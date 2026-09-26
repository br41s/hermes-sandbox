"""Fork's own tests for agent/curator.py, kept out of upstream's file so upstream merges do not conflict."""

from __future__ import annotations

import importlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def curator_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME + freshly reloaded curator + skill_usage modules."""
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.skill_usage as usage
    importlib.reload(usage)
    import agent.curator as curator
    importlib.reload(curator)

    # Neutralize the real LLM pass by default — tests opt in per-case.
    monkeypatch.setattr(curator, "_run_llm_review", lambda prompt: "llm-stub")

    # Default: no config file → curator defaults. Tests can override.
    monkeypatch.setattr(curator, "_load_config", lambda: {})
    # Pin prune_builtins OFF by default so transition tests don't pick up
    # built-ins unless they explicitly enable it. Both config-reading paths
    # are pinned (curator reads via _load_config; skill_usage reads config
    # directly). Tests opt in with _enable_prune_builtins(...).
    monkeypatch.setattr(usage, "_prune_builtins_enabled", lambda: False)

    yield {"home": home, "curator": curator, "usage": usage}

    # Teardown: a curator review launched with synchronous=False spawns a
    # daemon "curator-review" thread that calls save_state() when it finishes.
    # save_state() resolves the state path from HERMES_HOME at write time, so a
    # straggler thread that outlives this test would write into whatever home
    # the *next* test has configured (or the default ~/.hermes once monkeypatch
    # restores the env) — corrupting an unrelated test's state file. This race
    # is invisible on a fast machine but flakes under CI load. Join any such
    # thread here, while HERMES_HOME is still pinned to this test's tmp home
    # (curator_env depends on monkeypatch, so this teardown runs before the
    # monkeypatch env is restored). See the salvage of #14261 CI flake.
    for t in threading.enumerate():
        if t.name == "curator-review" and t.is_alive():
            t.join(timeout=10.0)


def _write_skill(skills_dir: Path, name: str):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: x\n---\n", encoding="utf-8",
    )
    return d


def _backdate(u, name: str, days: int, *, use_count: int = 1):
    """Write an agent-created usage record whose activity is *days* old."""
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    data = u.load_usage()
    data[name] = u._empty_record()
    data[name]["created_by"] = "agent"
    data[name]["created_at"] = ts
    data[name]["last_used_at"] = ts if use_count else None
    data[name]["last_activity_at"] = ts if use_count else None
    data[name]["use_count"] = use_count
    u.save_usage(data)


# ---------------------------------------------------------------------------
# Intra-skill bloat signals
# ---------------------------------------------------------------------------

def test_candidate_list_flags_over_budget_skill_md(curator_env, monkeypatch):
    """A skill whose SKILL.md passed the write cap must be visible to the pass.

    Consolidation was scored skill-to-skill only, so auditor-cron could grow to
    100,529 chars against a 100,000 cap — frozen to every ordinary patch — and
    still read as a healthy single skill in the candidate list.
    """
    from tools.skill_manager_tool import MAX_SKILL_CONTENT_CHARS

    c = curator_env["curator"]
    u = curator_env["usage"]
    skills_dir = curator_env["home"] / "skills"
    obese = _write_skill(skills_dir, "obese")
    (obese / "SKILL.md").write_text(
        f"---\nname: obese\ndescription: x\n---\n" + "y" * MAX_SKILL_CONTENT_CHARS,
        encoding="utf-8",
    )
    _write_skill(skills_dir, "lean")
    _backdate(u, "obese", 1)
    _backdate(u, "lean", 1)
    monkeypatch.setattr(c, "_cron_referenced_skills", lambda: set())

    listing = c._render_candidate_list()
    obese_line = next(l for l in listing.splitlines() if l.startswith("- obese"))
    lean_line = next(l for l in listing.splitlines() if l.startswith("- lean"))
    assert "OVER-BUDGET" in obese_line
    assert "OVER-BUDGET" not in lean_line
    assert "BLOATED SKILLS" in listing
    assert "obese (OVER-BUDGET)" in listing


def test_candidate_list_flags_reference_pileup(curator_env, monkeypatch):
    """references/ accretes duplicates because nothing downstream merges them."""
    c = curator_env["curator"]
    u = curator_env["usage"]
    skills_dir = curator_env["home"] / "skills"
    packed = _write_skill(skills_dir, "packed")
    refs = packed / "references"
    refs.mkdir()
    for i in range(c.REFS_WARN_COUNT):
        (refs / f"blocked-commands-{i}.md").write_text("same topic\n", encoding="utf-8")
    _backdate(u, "packed", 1)
    monkeypatch.setattr(c, "_cron_referenced_skills", lambda: set())

    listing = c._render_candidate_list()
    line = next(l for l in listing.splitlines() if l.startswith("- packed"))
    assert f"refs={c.REFS_WARN_COUNT}" in line
    assert "REFS-BLOATED" in line


def test_size_metrics_survive_a_missing_skill(curator_env):
    """Metrics are decoration on the candidate list — never a crash source."""
    c = curator_env["curator"]
    m = c._skill_size_metrics("does-not-exist")
    assert m["skill_md_chars"] == 0
    assert m["refs"] == 0
    assert m["over_budget"] is False
