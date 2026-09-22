"""Regression lock for the Phase-1 registry + mode state.

Hermetic: mode tests inject ``path``; registry tests build synthetic Incidents.
Guards the two invariants that keep gated→auto safe: (1) only genuinely transient
cron failures match the retry class — a hard fault (auth/config/missing code)
never does; (2) mode state fails safe to the registry default, never silently to
``auto``; (3) markers are scanned against the ERROR text, never the timestamp
the sweep prefixes onto it.
"""
from datetime import timedelta

import pytest

from incidents import sweep
from incidents.sweep import Incident
from remediation import clone_safety, modes, reconcile, registry
from remediation.cli import cmd_promote
from remediation.clone_safety import assess_reset, realign_clone
from remediation.registry import (
    CRON_TRANSIENT_FAILURE,
    SHARED_CLONE_BRANCH_CONFUSION,
    classify,
)


def _cron_inc(jid="j1", err="Provider returned error (502)"):
    return Incident(
        id=f"cron:{jid}:2026-06-26T10:00:00+00:00",
        kind="cron",
        title=f"Cron job '{jid}' failed (agent error)",
        detail=f"when: 2026-06-26T10:00:00+00:00\nerror: {err}",
        handoff=f"cron job id {jid}",
    )


class TestTransientHeuristic:
    @pytest.mark.parametrize("err", [
        "Provider returned error", "HTTP 503 Service Unavailable",
        "Read timed out", "rate limit exceeded (429)", "connection refused",
    ])
    def test_transient_errors_match(self, err):
        assert classify(_cron_inc(err=err)) is CRON_TRANSIENT_FAILURE

    @pytest.mark.parametrize("err", [
        "ModuleNotFoundError: no module named cron",
        "401 Unauthorized", "No models provided", "permission denied",
        "FileNotFoundError: no such file",
    ])
    def test_hard_faults_do_not_match(self, err):
        assert classify(_cron_inc(err=err)) is None

    def test_hard_fault_vetoes_even_with_transient_marker(self):
        # "timeout" is transient, but a traceback/ModuleNotFound veto wins.
        inc = _cron_inc(err="connection timeout then ModuleNotFoundError")
        assert classify(inc) is None

    def test_non_cron_incident_does_not_match(self):
        inc = Incident(id="trace:abc", kind="langfuse", title="t", detail="503", handoff="trace-id abc")
        assert classify(inc) is None


class TestProposalAndJobId:
    def test_proposal_names_the_job(self):
        text = CRON_TRANSIENT_FAILURE.proposal(_cron_inc(jid="c19bb95c0a62"))
        assert "c19bb95c0a62" in text and "retry" in text.lower()

    def test_signature_is_the_incident_id(self):
        # The debounce key is the incident id verbatim — re-run produces a new id.
        inc = _cron_inc()
        assert inc.id == "cron:j1:2026-06-26T10:00:00+00:00"


class TestModeState:
    def test_default_is_registry_default_when_unset(self, tmp_path):
        assert modes.mode_for("cron-transient-failure", path=tmp_path / "m.json") == "gated"

    def test_promote_then_persists_auto(self, tmp_path):
        p = tmp_path / "m.json"
        assert modes.promote("cron-transient-failure", path=p) == "auto"
        assert modes.is_auto("cron-transient-failure", path=p) is True

    def test_demote_returns_to_gated(self, tmp_path):
        p = tmp_path / "m.json"
        modes.promote("cron-transient-failure", path=p)
        assert modes.demote("cron-transient-failure", path=p) == "gated"
        assert modes.is_auto("cron-transient-failure", path=p) is False

    def test_corrupt_override_fails_safe_to_default(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text('{"cron-transient-failure": "YOLO"}', encoding="utf-8")
        assert modes.mode_for("cron-transient-failure", path=p) == "gated"

    def test_invalid_set_mode_raises(self, tmp_path):
        with pytest.raises(ValueError):
            modes.set_mode("cron-transient-failure", "turbo", path=tmp_path / "m.json")

    def test_unknown_class_defaults_gated(self, tmp_path):
        assert modes.mode_for("does-not-exist", path=tmp_path / "m.json") == "gated"


# --- shared-clone-branch-confusion -----------------------------------------

class FakeGit:
    """Scripted git runner for hermetic clone-safety tests.

    ``script`` is a list of ``(needle, (rc, out, err))`` pairs; the first whose
    needle is a substring of the joined args wins. Unmatched commands default to
    success with no output, so a test only specifies the commands it cares about.
    """

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, args, cwd):
        joined = " ".join(args)
        self.calls.append(joined)
        for needle, resp in self.script:
            if needle in joined:
                return resp
        return (0, "", "")

    def ran(self, needle):
        return any(needle in c for c in self.calls)


def _clean_clone_script():
    """A clone that is on/behind origin/main with nothing unique to lose."""
    return [
        ("rev-parse --is-inside-work-tree", (0, "true", "")),
        ("status --porcelain", (0, "", "")),
        ("rev-list --count origin/main..HEAD", (0, "0", "")),
        ("diff --diff-filter=D", (0, "", "")),
    ]


def _branch_inc(jid="seo1", err="fatal: Your local changes to the following files "
                                "would be overwritten by checkout"):
    return Incident(
        id=f"cron:{jid}:2026-06-26T10:00:00+00:00",
        kind="cron",
        title=f"Cron job '{jid}' failed (agent error)",
        detail=f"when: 2026-06-26T10:00:00+00:00\nerror: {err}",
        handoff=f"cron job id {jid}",
    )


class TestBranchConfusionHeuristic:
    @pytest.mark.parametrize("err", [
        "fatal: Your local changes to the following files would be overwritten by checkout",
        "HEAD detached at a1b2c3d",
        "Author identity unknown\n*** Please tell me who you are.",
        "Your branch and 'origin/main' have diverged",
        "error: failed to push some refs (non-fast-forward)",
        "Updates were rejected because the tip of your current branch is behind",
        "empty ident name not allowed",
    ])
    def test_branch_confusion_matches(self, err):
        assert classify(_branch_inc(err=err)) is SHARED_CLONE_BRANCH_CONFUSION

    @pytest.mark.parametrize("err", [
        "Provider returned error (502)", "Read timed out",
        "rate limit exceeded (429)", "503 Service Unavailable",
    ])
    def test_plain_transient_does_not_get_destructive_class(self, err):
        # Transient errors must route to the harmless retry, never the reset.
        assert classify(_branch_inc(err=err)) is CRON_TRANSIENT_FAILURE

    @pytest.mark.parametrize("err", [
        "ModuleNotFoundError despite detached HEAD",
        "401 Unauthorized while on the wrong branch",
        "No models provided; branch diverged",
    ])
    def test_code_or_auth_fault_vetoes_destructive_class(self, err):
        # A deterministic fault a reset can't fix must NOT trigger the reset.
        assert classify(_branch_inc(err=err)) is None

    def test_non_cron_incident_does_not_match(self):
        inc = Incident(id="trace:x", kind="langfuse", title="t",
                       detail="detached HEAD", handoff="trace-id x")
        assert classify(inc) is None

    def test_proposal_flags_destructive_and_names_job(self):
        text = SHARED_CLONE_BRANCH_CONFUSION.proposal(_branch_inc(jid="be8a4add42b0"))
        assert "be8a4add42b0" in text and "DESTRUCTIVE" in text


class TestCloneSafetyNet:
    def test_clean_clone_is_safe(self):
        v = assess_reset("/opt/data/p", git=FakeGit(_clean_clone_script()))
        assert v.safe is True and v.files_lost == 0

    def test_uncommitted_work_refuses(self):
        script = _clean_clone_script()[:1] + [
            ("status --porcelain", (0, " M article.html\n D cover.png", "")),
            ("rev-list --count origin/main..HEAD", (0, "0", "")),
            ("diff --diff-filter=D", (0, "", "")),
        ]
        v = assess_reset("/opt/data/p", git=FakeGit(script))
        assert v.safe is False and v.uncommitted == 2 and "uncommitted" in v.reason

    def test_untracked_files_alone_are_not_a_loss(self):
        # "??" lines survive reset --hard, so they don't block.
        script = _clean_clone_script()[:1] + [
            ("status --porcelain", (0, "?? scratch.txt\n?? notes.md", "")),
            ("rev-list --count origin/main..HEAD", (0, "0", "")),
            ("diff --diff-filter=D", (0, "", "")),
        ]
        v = assess_reset("/opt/data/p", git=FakeGit(script))
        assert v.safe is True and v.uncommitted == 0

    def test_unpushed_commits_refuse(self):
        script = _clean_clone_script()[:2] + [
            ("rev-list --count origin/main..HEAD", (0, "3", "")),
            ("diff --diff-filter=D", (0, "", "")),
        ]
        v = assess_reset("/opt/data/p", git=FakeGit(script))
        assert v.safe is False and v.ahead == 3 and "ahead" in v.reason

    def test_mass_deletion_refuses_cover_wipe(self):
        deleted = "\n".join(f"covers/img{i}.png" for i in range(48))
        script = _clean_clone_script()[:3] + [
            ("diff --diff-filter=D", (0, deleted, "")),
        ]
        v = assess_reset("/opt/data/p", limit=10, git=FakeGit(script))
        assert v.safe is False and v.files_lost == 48 and "mass-deletion" in v.reason

    def test_deletions_within_limit_are_safe(self):
        deleted = "\n".join(f"f{i}.txt" for i in range(5))
        script = _clean_clone_script()[:3] + [
            ("diff --diff-filter=D", (0, deleted, "")),
        ]
        v = assess_reset("/opt/data/p", limit=10, git=FakeGit(script))
        assert v.safe is True and v.files_lost == 5

    def test_not_a_git_repo_fails_safe(self):
        v = assess_reset("/tmp/nope", git=FakeGit([
            ("rev-parse --is-inside-work-tree", (128, "", "not a git repository")),
        ]))
        assert v.safe is False and "not a git work tree" in v.reason

    def test_git_status_error_fails_safe(self):
        script = _clean_clone_script()[:1] + [
            ("status --porcelain", (1, "", "boom")),
        ]
        v = assess_reset("/opt/data/p", git=FakeGit(script))
        assert v.safe is False

    def test_realign_clean_runs_reset_and_repins(self):
        git = FakeGit(_clean_clone_script())
        ok, detail = realign_clone("/opt/data/p", git=git)
        assert ok is True
        assert git.ran("fetch origin main")
        assert git.ran("checkout main")
        assert git.ran("reset --hard origin/main")
        assert git.ran("config user.email hermes@agent.local")

    def test_realign_refuses_and_does_not_reset_when_unsafe(self):
        script = _clean_clone_script()[:1] + [
            ("status --porcelain", (0, " M cover.png", "")),
            ("rev-list --count origin/main..HEAD", (0, "0", "")),
            ("diff --diff-filter=D", (0, "", "")),
        ]
        git = FakeGit(script)
        ok, detail = realign_clone("/opt/data/p", git=git)
        assert ok is False
        assert not git.ran("reset --hard")   # the destructive step never ran

    def test_realign_refuses_when_fetch_fails(self):
        git = FakeGit([("fetch origin main", (1, "", "network down"))])
        ok, _ = realign_clone("/opt/data/p", git=git)
        assert ok is False
        assert not git.ran("reset --hard")

    def test_realign_refuses_when_checkout_fails(self):
        git = FakeGit([("checkout main", (1, "", "would be overwritten"))])
        ok, _ = realign_clone("/opt/data/p", git=git)
        assert ok is False
        assert not git.ran("reset --hard")


class TestBranchConfusionGatedOnly:
    def test_default_mode_is_gated(self):
        assert SHARED_CLONE_BRANCH_CONFUSION.default_mode == "gated"

    def test_is_not_auto_eligible(self):
        assert SHARED_CLONE_BRANCH_CONFUSION.auto_eligible is False

    def test_mode_resolves_gated_by_default(self, tmp_path):
        assert modes.mode_for("shared-clone-branch-confusion",
                              path=tmp_path / "m.json") == "gated"

    def test_cli_refuses_to_promote_gated_only_class(self, tmp_path):
        code, msg = cmd_promote("shared-clone-branch-confusion",
                                modes_path=tmp_path / "m.json",
                                ledger_path=tmp_path / "l.jsonl")
        assert code == 1 and "Refusing to promote" in msg
        # And the mode was never flipped.
        assert modes.is_auto("shared-clone-branch-confusion",
                             path=tmp_path / "m.json") is False

    def test_recommender_never_suggests_gated_only_class(self):
        # Even with K+ clean gated runs, a non-eligible class is not recommended.
        sig_base = "cron:seo:2026-06-26T"
        entries = [
            reconcile.ledger.make_entry(
                "shared-clone-branch-confusion", f"{sig_base}{i:02d}:00:00+00:00",
                "cron job id seo", modes.MODE_GATED, reconcile.ledger.EVENT_VERIFIED,
                outcome=reconcile.ledger.OUTCOME_SUCCESS)
            for i in range(reconcile.K_PROMOTION + 2)
        ]
        writes, msgs = reconcile.promotion_recommendations(entries)
        assert all("shared-clone-branch-confusion" not in m for m in msgs)
        assert writes == []

    def test_realign_fix_refuses_when_job_has_no_workdir(self, monkeypatch):
        monkeypatch.setattr("cron.jobs.get_job", lambda jid: {"id": jid, "workdir": None})
        ok, detail = registry._realign_shared_clone(_branch_inc(jid="seo"))
        assert ok is False and "workdir" in detail

    def test_direct_promote_is_refused_at_mode_chokepoint(self, tmp_path):
        # The "never auto" invariant must hold even if the CLI guard is bypassed.
        with pytest.raises(ValueError):
            modes.promote("shared-clone-branch-confusion", path=tmp_path / "m.json")

    def test_is_auto_false_even_if_json_hand_edited_to_auto(self, tmp_path):
        p = tmp_path / "m.json"
        p.write_text('{"shared-clone-branch-confusion": "auto"}', encoding="utf-8")
        # A hand-edited modes.json cannot make a gated-only class auto-act.
        assert modes.is_auto("shared-clone-branch-confusion", path=p) is False


# --- marker scope: the error text, never the timestamp glued to it ----------

class TestMarkersScopedToErrorNotTimestamp:
    """The classifier must read the ERROR, not the ``when:`` line in front of it.

    ``cron_failure_incidents`` renders ``detail`` as ``when: <iso>\\nerror: <text>``.
    Scanning that whole blob meant an ISO-8601 *microsecond* field containing
    ``401`` or ``403`` tripped the hard-fault veto, so a genuinely transient 502
    stopped classifying — roughly one failure in a thousand, and always failing
    silent: the incident simply never became a remediation proposal.
    """

    @staticmethod
    def _job(microsecond, err="Provider returned error (502)"):
        when = (sweep._now() - timedelta(hours=1)).replace(microsecond=microsecond)
        return {"id": "j1", "name": "finview-cron", "last_error": err,
                "last_delivery_error": None, "last_run_at": when.isoformat()}

    @pytest.mark.parametrize("microsecond,digits", [(401754, "401"), (403912, "403")])
    def test_transient_survives_a_veto_lookalike_in_the_timestamp(self, microsecond, digits):
        inc = sweep.cron_failure_incidents([self._job(microsecond)])[0]
        # Guard the guard: the hazard must actually be present in this fixture.
        assert digits in inc.detail.splitlines()[0]
        assert digits not in inc.error
        assert classify(inc) is CRON_TRANSIENT_FAILURE

    def test_transient_classifies_with_an_innocent_timestamp(self):
        inc = sweep.cron_failure_incidents([self._job(123456)])[0]
        assert classify(inc) is CRON_TRANSIENT_FAILURE

    def test_real_auth_fault_in_the_error_still_vetoes(self):
        # Narrowing the scan must not blunt the veto itself.
        inc = sweep.cron_failure_incidents(
            [self._job(123456, err="401 Unauthorized from provider")])[0]
        assert classify(inc) is None

    def test_incident_carries_the_raw_error_separately(self):
        inc = sweep.cron_failure_incidents([self._job(123456)])[0]
        assert inc.error == "Provider returned error (502)"
        assert inc.detail.startswith("when: ")

    def test_error_defaults_empty_and_falls_back_to_detail(self):
        # Producers that predate the field keep working: matching falls back to
        # ``detail`` rather than silently classifying nothing.
        inc = Incident(id="cron:j9:t", kind="cron", title="t",
                       detail="when: t\nerror: Read timed out", handoff="cron job id j9")
        assert inc.error == ""
        assert classify(inc) is CRON_TRANSIENT_FAILURE


class TestNumericMarkersUseWordBoundaries:
    """Bare status codes are collision-prone; boundaries only ever match LESS."""

    @pytest.mark.parametrize("err", [
        "job 8502341 failed",          # 502 inside a longer id
        "wrote 15034 bytes, aborted",  # 503 inside a byte count
    ])
    def test_digits_embedded_in_a_longer_number_are_not_a_status_code(self, err):
        assert classify(_cron_inc(err=err)) is None

    @pytest.mark.parametrize("err", [
        "Provider returned error (502)", "HTTP 503 Service Unavailable",
        "gateway timeout 504", "rate limit exceeded (429)",
    ])
    def test_real_status_codes_still_match(self, err):
        assert classify(_cron_inc(err=err)) is CRON_TRANSIENT_FAILURE
