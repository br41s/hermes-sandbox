"""Regression lock for the merge-time mass-deletion safety net (auditor/safety.py).

Hermetic: monkeypatches the gh-api file fetch, so no network or gh binary is
touched. The load-bearing properties: a mass deletion is blocked (server-side
merge bypasses the pre-commit guard), a fetch failure fails SAFE (do not merge),
and the threshold honours ``> limit`` (exactly at the limit is allowed), matching
scripts/git-guard/check-mass-deletion.sh.
"""
from auditor import safety


def _patch_statuses(monkeypatch, statuses):
    monkeypatch.setattr(safety, "_pr_file_statuses", lambda repo, number: statuses)


def test_under_limit_is_safe(monkeypatch):
    _patch_statuses(monkeypatch, ["modified", "added", "removed"])
    safe, reason = safety.check_mass_deletion("o/r", 1)
    assert safe is True
    assert "1 file" in reason


def test_over_limit_blocks(monkeypatch):
    _patch_statuses(monkeypatch, ["removed"] * 11)
    safe, reason = safety.check_mass_deletion("o/r", 1)
    assert safe is False
    assert "mass-deletion" in reason


def test_exactly_at_default_limit_is_allowed(monkeypatch):
    # 10 removed, limit 10 -> not > limit -> allowed (matches the shell guard).
    _patch_statuses(monkeypatch, ["removed"] * 10)
    safe, _ = safety.check_mass_deletion("o/r", 1)
    assert safe is True


def test_fetch_failure_fails_safe(monkeypatch):
    _patch_statuses(monkeypatch, None)
    safe, reason = safety.check_mass_deletion("o/r", 1)
    assert safe is False
    assert "could not fetch" in reason


def test_limit_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_MASS_DELETION_LIMIT", "2")
    _patch_statuses(monkeypatch, ["removed"] * 3)
    assert safety.check_mass_deletion("o/r", 1)[0] is False
    _patch_statuses(monkeypatch, ["removed"] * 2)
    assert safety.check_mass_deletion("o/r", 1)[0] is True


def test_main_exit_codes(monkeypatch):
    _patch_statuses(monkeypatch, ["removed"] * 11)
    assert safety.main(["--repo", "o/r", "--number", "1"]) == 1
    _patch_statuses(monkeypatch, ["modified"])
    assert safety.main(["--repo", "o/r", "--number", "1"]) == 0
