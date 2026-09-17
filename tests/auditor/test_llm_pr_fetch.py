"""Regression lock for the judge's non-pipe entry point (auditor/llm.py).

The defect this closes: auditor.prompt PASO 2d told the agent to pipe the rubric
and diff into `python -m auditor.llm`. Cron runs with approvals.cron_mode: deny,
and the Tirith scanner blocks every pipe into an interpreter with no user present
to approve it — so the judge call was refused on every run. Content merges and
system approvals were being decided by the orchestrator model alone, silently,
and the per-tier model split was inert.

So the load-bearing properties are: the command PASO 2d actually contains must
pass the security scanner, the --repo path must never touch stdin, and an
unfetchable or empty PR must be a loud error rather than a blank review — a judge
handed nothing approves everything.
"""
import json
import re
from pathlib import Path

import pytest

import auditor.llm as llm

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT = REPO_ROOT / "auditor" / "auditor.prompt"


def _fake_gh(mapping):
    """Replace llm._gh with a lookup keyed on the gh subcommand ('view'/'diff')."""
    def _gh(args, timeout=120):
        return mapping.get(args[1])
    return _gh


META = json.dumps({"title": "Fix the thing", "body": "why", "additions": 3,
                   "deletions": 1, "changedFiles": 2})


def test_paso_2d_command_passes_the_cron_security_scanner():
    """The bug in one assertion: the command in the prompt must be allowed."""
    from tools.tirith_security import check_command_security
    text = PROMPT.read_text(encoding="utf-8")
    # Match the WHOLE line any auditor.llm invocation sits on, so a reintroduced
    # `printf ... | python -m auditor.llm` is caught as a pipe rather than slipping
    # past a regex anchored on `python`.
    cmds = [ln.strip() for ln in text.splitlines() if "python -m auditor.llm" in ln]
    assert cmds, "PASO 2d no longer invokes auditor.llm — update this test"
    for cmd in cmds:
        concrete = (cmd.replace("<tier>", "system")
                       .replace("<repo>", "br41s/hermes-sandbox")
                       .replace("<number>", "247"))
        assert "|" not in concrete, (
            f"PASO 2d pipes into the interpreter again — cron blocks this: {concrete}")
        verdict = check_command_security(concrete).get("action")
        assert verdict == "allow", f"cron would refuse PASO 2d: {verdict} for {concrete}"


def test_repo_path_never_reads_stdin(monkeypatch, capsys):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": "diff --git a/x b/x\n+1\n"}))
    monkeypatch.setattr(llm, "review", lambda tier, content, **kw: "APPROVE")

    class Exploding:
        def read(self):
            raise AssertionError("--repo path must not read stdin (cron has none)")
    monkeypatch.setattr(llm.sys, "stdin", Exploding())

    assert llm.main(["--tier", "system", "--repo", "o/n", "--number", "5"]) == 0
    assert "APPROVE" in capsys.readouterr().out


def test_repo_and_number_must_come_together():
    assert llm.main(["--tier", "system", "--repo", "o/n"]) == 2
    assert llm.main(["--tier", "system", "--number", "5"]) == 2


def test_unfetchable_pr_is_an_error_not_a_blank_review(monkeypatch):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": None, "diff": None}))
    monkeypatch.setattr(llm, "review",
                        lambda *a, **k: pytest.fail("must not judge an unfetchable PR"))
    assert llm.main(["--tier", "system", "--repo", "o/n", "--number", "5"]) == 3


def test_empty_diff_refuses_to_judge_nothing(monkeypatch):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": "   \n"}))
    text, err = llm.fetch_pr_content("o/n", 5)
    assert text is None
    assert "empty diff" in err


def test_unreadable_metadata_is_an_error(monkeypatch):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": "not json", "diff": "d"}))
    text, err = llm.fetch_pr_content("o/n", 5)
    assert text is None and "unreadable" in err


def test_composed_review_carries_rubric_title_and_diff(monkeypatch):
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": "diff --git a/x b/x"}))
    text, err = llm.fetch_pr_content("o/n", 5)
    assert err is None
    assert "BLOCK or APPROVE" in text        # rubric travels with the request
    assert "o/n#5 — Fix the thing" in text   # the judge knows what it is reading
    assert "diff --git a/x b/x" in text


def test_oversized_diff_is_truncated_and_says_so(monkeypatch):
    big = "x" * (llm._MAX_DIFF_CHARS + 5000)
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": big}))
    text, err = llm.fetch_pr_content("o/n", 5)
    assert err is None
    assert "TRUNCATED" in text, "silent truncation would have the judge grade a fragment"
    assert len(text) < len(big) + 5000


# --- Credential resolution (2026-09-16 outage) -------------------------------
#
# The judge read OPENROUTER_API_KEY straight from os.environ. That variable is on
# the terminal backend's provider blocklist, so it is stripped from EVERY
# subprocess an agent spawns — and the cron orchestrator runs the judge as
# exactly such a subprocess. The key was therefore guaranteed absent no matter
# what the container env held, and the orchestrator degraded to an unaided
# review with a one-line footnote. These lock the resolution path and the loud
# exit code.

def test_openrouter_key_is_stripped_from_agent_subprocesses():
    """The premise of the bug, asserted against the real blocklist.

    If this ever fails, the strip was relaxed upstream and the .env fallback
    below stops being load-bearing — but it must still not regress to a bare
    os.environ read, so the other tests here stay valid either way.
    """
    from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST
    assert "OPENROUTER_API_KEY" in _HERMES_PROVIDER_ENV_BLOCKLIST
    # The dedicated auditor key is NOT blocklisted — that is why it is tried
    # first: it survives into the subprocess env on its own name.
    assert "HERMES_AUDITOR_OPENROUTER_API_KEY" not in _HERMES_PROVIDER_ENV_BLOCKLIST


def test_key_resolves_from_profile_dotenv_when_env_is_stripped(monkeypatch, tmp_path):
    """os.environ stripped + profile .env populated => the judge still runs."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_AUDITOR_OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-from-profile-dotenv\n",
                                   encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import config as hermes_config
    hermes_config.invalidate_env_cache()
    try:
        assert llm.resolve_api_key() == "sk-from-profile-dotenv"
    finally:
        hermes_config.invalidate_env_cache()


def test_dedicated_auditor_key_wins_over_the_shared_one(monkeypatch):
    """Spend isolation: the auditor's own key must beat the fleet's shared key.

    The shared key hitting its weekly cap 402'd the whole auditor cron for a day
    on 2026-09-01; HERMES_AUDITOR_OPENROUTER_API_KEY exists to prevent that, so
    it must be preferred wherever both are visible.
    """
    seen = {"HERMES_AUDITOR_OPENROUTER_API_KEY": "sk-dedicated",
            "OPENROUTER_API_KEY": "sk-shared"}
    monkeypatch.setattr(llm, "_env_value", lambda name: seen.get(name, ""))
    assert llm.resolve_api_key() == "sk-dedicated"


def test_shared_key_is_the_documented_fallback(monkeypatch):
    seen = {"OPENROUTER_API_KEY": "sk-shared"}
    monkeypatch.setattr(llm, "_env_value", lambda name: seen.get(name, ""))
    assert llm.resolve_api_key() == "sk-shared"


def test_no_credential_exits_4_not_a_traceback(monkeypatch):
    """Exit 4 is 'the gate is broken', distinct from exit 3 'could not fetch'.

    The orchestrator branches on this (auditor.prompt PASO 2d): exit 3 degrades
    to an unaided review, exit 4 blocks content merges and escalates.
    """
    monkeypatch.setattr(llm, "_gh", _fake_gh({"view": META, "diff": "diff --git a/x b/x\n+1\n"}))
    monkeypatch.setattr(llm, "_env_value", lambda _name: "")
    assert llm.main(["--tier", "system", "--repo", "o/n", "--number", "5"]) == 4


def test_model_knobs_also_resolve_through_the_profile_dotenv(monkeypatch, tmp_path):
    """cont-init §1c stamps the model knobs into the auditor .env for exactly
    this reason. os.environ still wins when set."""
    monkeypatch.delenv("HERMES_AUDITOR_SYSTEM_MODEL", raising=False)
    monkeypatch.delenv("HERMES_AUDITOR_CONTENT_MODEL", raising=False)
    (tmp_path / ".env").write_text("HERMES_AUDITOR_SYSTEM_MODEL=vendor/from-dotenv\n",
                                   encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import config as hermes_config
    hermes_config.invalidate_env_cache()
    try:
        assert llm.resolve_model("system") == "vendor/from-dotenv"
    finally:
        hermes_config.invalidate_env_cache()


def test_prompt_branches_on_the_judge_exit_codes():
    """A silent degrade is what hid this for months — the prompt must
    distinguish 'could not fetch the PR' from 'the judge could not run'."""
    text = PROMPT.read_text(encoding="utf-8")
    assert "Exit 4" in text and "JUDGE UNAVAILABLE" in text, (
        "PASO 2d no longer tells the agent that a dead judge is a broken gate")


class TestCheckFlag:
    """`--check` answers 'can the gate run at all' without an LLM round-trip.

    A real review is a model call with a 120s timeout, which outlives the Zeabur
    exec gateway — it answers 504 and tells you nothing about the judge. That is
    how the credential outage stayed unverifiable from a shell.
    """

    def _run(self, monkeypatch, capsys, env):
        import auditor.llm as llm

        monkeypatch.setattr(llm, "_env_value", lambda name: env.get(name, ""))
        monkeypatch.setattr(llm, "resolve_model", lambda tier: "test/model")
        code = llm.main(["--tier", "content", "--check"])
        return code, capsys.readouterr().out

    def test_exit_0_when_the_dedicated_key_resolves(self, monkeypatch, capsys):
        code, out = self._run(
            monkeypatch, capsys, {"HERMES_AUDITOR_OPENROUTER_API_KEY": "sk-dedicated"}
        )
        assert code == 0
        assert "HERMES_AUDITOR_OPENROUTER_API_KEY" in out
        assert "OK" in out

    def test_exit_4_when_nothing_resolves(self, monkeypatch, capsys):
        code, out = self._run(monkeypatch, capsys, {})
        assert code == 4, "exit 4 is the agreed 'judge cannot run' code"
        assert "NOT RESOLVED" in out

    def test_never_prints_the_credential(self, monkeypatch, capsys):
        secret = "sk-do-not-print-me-12345"
        code, out = self._run(
            monkeypatch, capsys, {"HERMES_AUDITOR_OPENROUTER_API_KEY": secret}
        )
        assert code == 0
        assert secret not in out, "the credential must never reach a transcript"
        assert f"len {len(secret)}" in out

    def test_shared_key_resolves_but_is_flagged(self, monkeypatch, capsys):
        # Works, but warns: that name is on the subprocess env blocklist, so it
        # only resolved because it came from the profile .env.
        code, out = self._run(monkeypatch, capsys, {"OPENROUTER_API_KEY": "sk-shared"})
        assert code == 0
        assert "blocklist" in out

    def test_check_makes_no_model_call(self, monkeypatch, capsys):
        import auditor.llm as llm

        def _boom(*a, **k):
            raise AssertionError("--check must not call the model")

        monkeypatch.setattr(llm, "_build_request", _boom)
        monkeypatch.setattr(llm, "_env_value", lambda n: "sk-x" if "OPENROUTER" in n else "")
        monkeypatch.setattr(llm, "resolve_model", lambda tier: "test/model")
        assert llm.main(["--tier", "content", "--check"]) == 0


class TestJudgeDeadline:
    """A hung judge must become a fast, clean exit 4.

    `urlopen(timeout=...)` bounds each socket read, not the request. On
    br41s/biglobster#526 (2026-09-17) a judge call with `timeout=120` ran 300s,
    then 590s, and produced no verdict — holding the single-thread cron pool
    while the auditor waited. Exit 4 already means "gate broken"; these tests
    pin that a hang reaches it instead of hanging forever.
    """

    def test_default_deadline(self, monkeypatch):
        import auditor.llm as llm

        monkeypatch.setattr(llm, "_env_value", lambda n: "")
        assert llm.judge_deadline_seconds() == llm.JUDGE_DEADLINE_DEFAULT

    def test_deadline_is_configurable(self, monkeypatch):
        import auditor.llm as llm

        monkeypatch.setattr(
            llm, "_env_value",
            lambda n: "45" if n == "HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS" else "",
        )
        assert llm.judge_deadline_seconds() == 45

    def test_garbage_and_tiny_values_are_floored(self, monkeypatch):
        import auditor.llm as llm

        monkeypatch.setattr(
            llm, "_env_value",
            lambda n: "banana" if n == "HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS" else "",
        )
        assert llm.judge_deadline_seconds() == llm.JUDGE_DEADLINE_DEFAULT
        monkeypatch.setattr(
            llm, "_env_value",
            lambda n: "1" if n == "HERMES_AUDITOR_JUDGE_DEADLINE_SECONDS" else "",
        )
        assert llm.judge_deadline_seconds() == 10, "a typo must not disable reviews"

    def test_deadline_fires_on_a_slow_block(self):
        import time

        import auditor.llm as llm

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with llm.wall_clock_deadline(1, "test block"):
                time.sleep(5)
        assert time.monotonic() - started < 4, "it must cut the block short, not wait it out"

    def test_deadline_does_not_fire_on_a_fast_block(self):
        import auditor.llm as llm

        with llm.wall_clock_deadline(30, "test block"):
            pass  # must not raise

    def test_alarm_is_cleared_afterwards(self):
        import signal
        import time

        import auditor.llm as llm

        with llm.wall_clock_deadline(1, "test block"):
            pass
        # A leaked alarm would kill an unrelated later call.
        assert signal.alarm(0) == 0
        time.sleep(1.2)  # would have fired by now if it leaked

    def test_a_hung_judge_exits_4(self, monkeypatch, capsys):
        import time

        import auditor.llm as llm

        monkeypatch.setattr(llm, "fetch_pr_content", lambda r, n: ("diff", None))
        # Patch the resolver, not the env: judge_deadline_seconds() floors at
        # 10s, and a test that sleeps for the floor races it.
        monkeypatch.setattr(llm, "judge_deadline_seconds", lambda: 1)

        def _hang(*a, **k):
            time.sleep(30)

        monkeypatch.setattr(llm, "review", _hang)
        code = llm.main(["--tier", "system", "--repo", "o/r", "--number", "1"])
        assert code == 4, "a hang is a broken gate, not a degraded review"
        assert "deadline" in capsys.readouterr().err
