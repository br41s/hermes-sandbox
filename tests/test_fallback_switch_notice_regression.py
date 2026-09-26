"""Regression lock for the fallback-switch notice (PR #30).

This is the artifact the self-repair eval loop "graduates" a passing case into:
a hermetic pytest that runs in CI with no model calls. It guards two things —
the notice's behaviour directly, and that the eval harness + seed case agree.

Seed case + loop: ``fork_evals/cases/fallback_switch_notice.yaml`` / ``fork_evals/run.py``.
"""
from types import SimpleNamespace

from agent.chat_completion_helpers import fallback_switch_notice
from fork_evals.run import evaluate, load_case


def _fallback_agent():
    return SimpleNamespace(
        _fallback_index=1,
        _primary_runtime={"model": "owl-alpha"},
        model="tencent/hy3-preview",
    )


class TestFallbackSwitchNotice:
    def test_notice_present_in_spanish_when_fallback_answered(self):
        notice = fallback_switch_notice(_fallback_agent())
        assert notice  # non-empty
        low = notice.lower()
        assert "saturado" in low and "respaldo" in low
        # Names both the primary and the backup model so the user knows what happened.
        assert "owl-alpha" in notice and "tencent/hy3-preview" in notice

    def test_no_notice_when_primary_model_answered(self):
        agent = SimpleNamespace(_fallback_index=0, _primary_runtime={"model": "owl-alpha"}, model="owl-alpha")
        assert fallback_switch_notice(agent) == ""

    def test_no_notice_when_current_equals_primary(self):
        agent = SimpleNamespace(_fallback_index=1, _primary_runtime={"model": "owl-alpha"}, model="owl-alpha")
        assert fallback_switch_notice(agent) == ""


class TestEvalHarnessAgreement:
    """The deterministic eval path must agree with the locked behaviour."""

    def test_seed_case_passes_deterministic_judge(self):
        case = load_case("fallback_switch_notice")
        output, results = evaluate(case, use_llm=False)
        assert "resumen" in output  # body preserved
        assert results, "case defines no assertions"
        for assertion, res in results:
            assert res.passed, f"assertion failed: {assertion.get('text')!r} -> {res.reason}"
