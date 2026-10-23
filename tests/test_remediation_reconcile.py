"""Phase-2 lock: verification + promotion recommender + promote/demote CLI.

Hermetic: synthetic jobs, tmp ledger/modes paths, injected `now`. Guards the
apprenticeship invariants — a fix only counts once its job re-ran AND recovered;
a still-failing job escalates; promotion is recommended at K, deduped, and only
the CEO's `promote` flips the mode.
"""
from datetime import timedelta

from incidents import sweep
from incidents.sweep import _now as now_fn
from remediation import cli, ledger, modes, reconcile

CLS = "cron-transient-failure"


def _applied(sig, jid="j1", ago_hours=2, mode="gated"):
    return ledger.make_entry(CLS, sig, f"cron job id {jid}", mode, ledger.EVENT_APPLIED,
                             outcome=ledger.OUTCOME_SUCCESS,
                             now=now_fn() - timedelta(hours=ago_hours))


def _job(jid="j1", err=None, ran_ago_hours=1):
    return {"id": jid, "name": "finview-cron", "last_error": err,
            "last_delivery_error": None,
            "last_run_at": (now_fn() - timedelta(hours=ran_ago_hours)).isoformat()}


def _seed(path, *entries):
    for e in entries:
        ledger.append(e, path=path)


class TestVerification:
    def test_recovered_job_verifies_success(self, tmp_path):
        # applied 2h ago; job re-ran 1h ago (after apply) and is healthy.
        entries = [_applied("sig1", ago_hours=2)]
        writes, esc = reconcile.verify_pending([_job(err=None, ran_ago_hours=1)], entries, now=now_fn())
        assert esc == []
        assert len(writes) == 1 and writes[0].event == ledger.EVENT_VERIFIED
        assert writes[0].outcome == ledger.OUTCOME_SUCCESS

    def test_still_failing_job_marks_failed_and_escalates(self, tmp_path):
        entries = [_applied("sig1", ago_hours=2)]
        writes, esc = reconcile.verify_pending([_job(err="boom", ran_ago_hours=1)], entries, now=now_fn())
        assert len(esc) == 1 and "did NOT clear" in esc[0]
        assert writes[0].event == ledger.EVENT_FAILED

    def test_job_not_rerun_since_apply_stays_pending(self, tmp_path):
        # applied 1h ago; job last ran 2h ago (BEFORE apply) -> defer.
        entries = [_applied("sig1", ago_hours=1)]
        writes, esc = reconcile.verify_pending([_job(err="boom", ran_ago_hours=2)], entries, now=now_fn())
        assert writes == [] and esc == []

    def test_absent_job_counts_success(self, tmp_path):
        entries = [_applied("sig1", jid="gone")]
        writes, _esc = reconcile.verify_pending([], entries, now=now_fn())
        assert writes[0].event == ledger.EVENT_VERIFIED

    def test_already_resolved_not_reverified(self, tmp_path):
        entries = [
            _applied("sig1", ago_hours=3),
            ledger.make_entry(CLS, "sig1", "cron job id j1", "gated",
                              ledger.EVENT_VERIFIED, outcome=ledger.OUTCOME_SUCCESS),
        ]
        writes, _esc = reconcile.verify_pending([_job(err=None)], entries, now=now_fn())
        assert writes == []


class TestPromotionRecommender:
    def _k_clean(self, k):
        return [ledger.make_entry(CLS, f"s{i}", "cron job id j1", "gated",
                                  ledger.EVENT_VERIFIED, outcome=ledger.OUTCOME_SUCCESS)
                for i in range(k)]

    def test_recommends_at_threshold(self, tmp_path):
        entries = self._k_clean(reconcile.K_PROMOTION)
        writes, msgs = reconcile.promotion_recommendations(entries, modes_path=tmp_path / "m.json", now=now_fn())
        assert len(msgs) == 1 and "Promote to auto" in msgs[0]
        assert writes[0].event == ledger.EVENT_RECOMMENDED

    def test_below_threshold_silent(self, tmp_path):
        entries = self._k_clean(reconcile.K_PROMOTION - 1)
        writes, msgs = reconcile.promotion_recommendations(entries, modes_path=tmp_path / "m.json", now=now_fn())
        assert msgs == [] and writes == []

    def test_recent_recommendation_is_deduped(self, tmp_path):
        entries = self._k_clean(reconcile.K_PROMOTION)
        entries.append(ledger.make_entry(CLS, f"promote:{CLS}", CLS, "gated",
                                         ledger.EVENT_RECOMMENDED, now=now_fn() - timedelta(hours=1)))
        _writes, msgs = reconcile.promotion_recommendations(entries, modes_path=tmp_path / "m.json", now=now_fn())
        assert msgs == []

    def test_already_auto_not_recommended(self, tmp_path):
        mp = tmp_path / "m.json"
        modes.promote(CLS, path=mp)
        entries = self._k_clean(reconcile.K_PROMOTION)
        _writes, msgs = reconcile.promotion_recommendations(entries, modes_path=mp, now=now_fn())
        assert msgs == []

    def test_clean_run_count_distinct_gated_success(self):
        entries = [
            ledger.make_entry(CLS, "s1", "t", "gated", ledger.EVENT_VERIFIED, outcome=ledger.OUTCOME_SUCCESS),
            ledger.make_entry(CLS, "s1", "t", "gated", ledger.EVENT_VERIFIED, outcome=ledger.OUTCOME_SUCCESS),  # dup sig
            ledger.make_entry(CLS, "s2", "t", "gated", ledger.EVENT_FAILED, outcome=ledger.OUTCOME_FAILURE),    # not success
            ledger.make_entry(CLS, "s3", "t", "auto", ledger.EVENT_VERIFIED, outcome=ledger.OUTCOME_SUCCESS),   # not gated
        ]
        assert reconcile.clean_run_count(CLS, entries) == 1


class TestReconcileE2E:
    def test_verify_then_recommend_same_pass(self, tmp_path):
        lp = tmp_path / "l.jsonl"
        mp = tmp_path / "m.json"
        # 4 already-verified clean runs + 1 applied that will verify this pass = 5 -> K.
        seed = [ledger.make_entry(CLS, f"s{i}", "cron job id j1", "gated",
                                  ledger.EVENT_VERIFIED, outcome=ledger.OUTCOME_SUCCESS)
                for i in range(reconcile.K_PROMOTION - 1)]
        seed.append(_applied("s-final", ago_hours=2))
        _seed(lp, *seed)
        out = reconcile.reconcile([_job(err=None, ran_ago_hours=1)],
                                  ledger_path=lp, modes_path=mp, now=now_fn())
        assert "Promote to auto" in out
        rows = ledger.read(path=lp)
        assert any(r.event == ledger.EVENT_VERIFIED and r.signature == "s-final" for r in rows)
        assert any(r.event == ledger.EVENT_RECOMMENDED for r in rows)

    def test_clean_pass_is_silent(self, tmp_path):
        out = reconcile.reconcile([_job(err=None)], ledger_path=tmp_path / "l.jsonl",
                                  modes_path=tmp_path / "m.json", now=now_fn())
        assert out == ""

    def test_dry_run_writes_nothing(self, tmp_path):
        lp = tmp_path / "l.jsonl"
        _seed(lp, _applied("sig1", ago_hours=2))
        reconcile.reconcile([_job(err=None, ran_ago_hours=1)], ledger_path=lp,
                            modes_path=tmp_path / "m.json", now=now_fn(), dry_run=True)
        assert all(r.event == ledger.EVENT_APPLIED for r in ledger.read(path=lp))  # no verify added


class TestSweepIntegration:
    def test_escalation_surfaces_in_sweep_output(self, tmp_path):
        lp = tmp_path / "l.jsonl"
        _seed(lp, _applied("sig1", ago_hours=2))
        out = sweep.sweep(jobs=[_job(err="still broken", ran_ago_hours=1)], langfuse=[], blocked=[],
                          state_path=tmp_path / "s.json", ledger_path=lp, modes_path=tmp_path / "m.json")
        assert "did NOT clear" in out

    def test_clean_sweep_stays_silent_after_heartbeat(self, tmp_path):
        sp = tmp_path / "s.json"
        kw = dict(jobs=[_job(err=None)], langfuse=[], blocked=[],
                  ledger_path=tmp_path / "l.jsonl", modes_path=tmp_path / "m.json")
        sweep.sweep(state_path=sp, **kw)            # first run: heartbeat
        out = sweep.sweep(state_path=sp, **kw)      # within 24h, nothing to verify -> silent
        assert out == ""


class TestPromoteDemoteCLI:
    def test_promote_flips_mode_and_logs(self, tmp_path):
        mp, lp = tmp_path / "m.json", tmp_path / "l.jsonl"
        code, _msg = cli.cmd_promote(CLS, modes_path=mp, ledger_path=lp)
        assert code == 0 and modes.is_auto(CLS, path=mp)
        assert any(r.event == ledger.EVENT_PROMOTED for r in ledger.read(path=lp))

    def test_promote_unknown_class_errors(self, tmp_path):
        code, msg = cli.cmd_promote("nope", modes_path=tmp_path / "m.json", ledger_path=tmp_path / "l.jsonl")
        assert code == 1 and "Unknown" in msg

    def test_demote_returns_to_gated(self, tmp_path):
        mp = tmp_path / "m.json"
        modes.promote(CLS, path=mp)
        code, _msg = cli.cmd_demote(CLS, modes_path=mp)
        assert code == 0 and not modes.is_auto(CLS, path=mp)
