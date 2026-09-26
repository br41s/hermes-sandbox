"""Regression lock for safety net #4 — cron routing.

Guards against a repeat of the post-migration misroute: scheduled jobs silently
posting to a frozen 'origin' (old private DM) instead of an explicit forum
destination. Reuses the real routing auditor over synthetic jobs.
"""
from fork_evals.checks.cron_routing import audit_cron_routing, audit_summary, routing_hazard
from fork_evals.run import evaluate, load_case

FORUM_CHAT = -1004224848555  # supergroup/forum (negative)
DM_CHAT = 123456789          # private DM (positive)


def _job(**kw):
    base = {"id": "j1", "deliver": None, "origin": None}
    base.update(kw)
    return base


class TestFrozenOriginHazards:
    def test_origin_to_private_dm_is_flagged(self):
        job = _job(deliver="origin", origin={"platform": "telegram", "chat_id": DM_CHAT})
        hz = routing_hazard(job)
        assert hz and "DM" in hz

    def test_default_deliver_with_dm_origin_is_flagged(self):
        # deliver unset -> defaults to 'origin' when origin present (create_job rule)
        job = _job(deliver=None, origin={"platform": "telegram", "chat_id": DM_CHAT})
        assert routing_hazard(job) is not None

    def test_origin_to_group_without_thread_is_flagged(self):
        job = _job(deliver="origin", origin={"platform": "telegram", "chat_id": FORUM_CHAT})
        hz = routing_hazard(job)
        assert hz and "thread" in hz.lower()

    def test_incomplete_origin_is_flagged(self):
        job = _job(deliver="origin", origin={"note": "created somewhere"})
        assert routing_hazard(job) is not None


class TestSafeRoutingIsNotFlagged:
    def test_explicit_forum_thread_origin_is_safe(self):
        job = _job(deliver="origin", origin={"platform": "telegram", "chat_id": FORUM_CHAT, "thread_id": 2})
        assert routing_hazard(job) is None

    def test_explicit_platform_delivery_is_safe(self):
        job = _job(deliver="telegram", origin={"platform": "telegram", "chat_id": DM_CHAT})
        assert routing_hazard(job) is None

    def test_local_delivery_is_safe(self):
        assert routing_hazard(_job(deliver="local")) is None

    def test_no_deliver_no_origin_is_local_and_safe(self):
        assert routing_hazard(_job(deliver=None, origin=None)) is None


class TestAuditAggregation:
    def test_audit_collects_only_hazardous_jobs(self):
        jobs = [
            _job(id="bad", deliver="origin", origin={"platform": "telegram", "chat_id": DM_CHAT}),
            _job(id="good", deliver="origin", origin={"platform": "telegram", "chat_id": FORUM_CHAT, "thread_id": 2}),
        ]
        flagged = audit_cron_routing(jobs)
        ids = [jid for jid, _ in flagged]
        assert ids == ["bad"]

    def test_summary_ok_when_clean(self):
        jobs = [_job(deliver="local")]
        assert audit_summary(jobs).startswith("OK")


class TestEvalHarnessAgreement:
    def test_seed_case_runs_against_live_jobs(self):
        # Live jobs may be empty in CI; the case must at least evaluate cleanly.
        case = load_case("cron_routing")
        output, results = evaluate(case, use_llm=False)
        assert "OK" in output or "FAIL" in output
        assert results
