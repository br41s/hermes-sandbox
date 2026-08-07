"""Profile-scoped delegate: the subprocess must run in the profile's own
workspace (not the shared default HERMES_HOME root), and unknown profiles must
return an error rather than raise. Covers run_delegate_in_profile."""
from __future__ import annotations

import os
from unittest.mock import patch

from hermes_cli import delegate_core


class _FakeCompleted:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _ok_stdout() -> str:
    return (
        delegate_core.RESULT_PREFIX
        + '{"final_response": "ok", "error": null}'
        + delegate_core.RESULT_SUFFIX
    )


def test_subprocess_cwd_scoped_to_profile_workspace(tmp_path):
    profile_home = tmp_path / "profiles" / "grow-shop"
    profile_home.mkdir(parents=True)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        result = delegate_core.run_delegate_in_profile("task-1", "do it", "grow-shop")

    assert result["final_response"] == "ok"
    # cwd is the profile's OWN workspace, created on demand — not /opt/data.
    expected_workdir = str(profile_home / "workspace")
    assert captured["cwd"] == expected_workdir
    assert os.path.isdir(expected_workdir)
    # HERMES_HOME still points at the profile home (memory/session isolation).
    assert captured["env"]["HERMES_HOME"] == str(profile_home)


def test_cwd_falls_back_to_profile_root_if_workspace_uncreatable(tmp_path):
    profile_home = tmp_path / "profiles" / "grow-shop"
    profile_home.mkdir(parents=True)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("os.makedirs", side_effect=OSError("read-only fs")), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "grow-shop")

    assert captured["cwd"] == str(profile_home)


class _KilledCompleted:
    """A subprocess result reaped by a signal: negative returncode, no sentinel."""

    def __init__(self, returncode: int):
        self.stdout = "partial output, no sentinel"
        self.stderr = ""
        self.returncode = returncode


def test_signal_kill_gateway_lane_returns_recoverable_message(tmp_path):
    """A SIGTERM-killed delegate (exit=-15) on the gateway lane must yield a
    friendly, recoverable reply instead of the cryptic 'no parseable result'."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", return_value=_KilledCompleted(-15)):
        result = delegate_core.run_delegate_in_profile(
            "task-1", "do it", "finview", no_delegate_prompt=True
        )

    # User-facing recoverable message (Spanish, matches the topic notices).
    assert result["final_response"]
    assert "interrumpió" in result["final_response"]
    assert "no parseable result" not in result["final_response"]
    # Diagnostic names the signal so logs can tell restart (SIGTERM) from OOM.
    assert "SIGTERM" in result["error"]


def test_signal_kill_orchestrator_lane_returns_diagnostic(tmp_path):
    """The orchestrator lane (no gateway flag) gets an English diagnostic error
    and an empty final_response, naming the signal."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", return_value=_KilledCompleted(-15)):
        result = delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert result["final_response"] == ""
    assert "SIGTERM" in result["error"]
    assert "restart" in result["error"]


def test_unparseable_nonsignal_exit_keeps_no_parseable_message(tmp_path):
    """A non-signal failure (positive exit code) must still surface the original
    'no parseable result' diagnostic with the tail — not the signal path."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", return_value=_KilledCompleted(1)):
        result = delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert result["final_response"] == ""
    assert "no parseable result" in result["error"]
    assert "exit=1" in result["error"]


def test_unknown_profile_returns_error_not_raise():
    with patch("hermes_cli.profiles.resolve_profile_env",
               side_effect=FileNotFoundError("nope")):
        result = delegate_core.run_delegate_in_profile("task-1", "p", "ghost")
    assert result["final_response"] == ""
    assert "Invalid delegate profile" in result["error"]


def test_no_delegate_prompt_sets_env_var(tmp_path):
    """no_delegate_prompt=True must inject HERMES_DELEGATE_NO_PROMPT=1 into the subprocess env."""
    profile_home = tmp_path / "profiles" / "grow-shop"
    profile_home.mkdir(parents=True)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile(
            "task-1", "do it", "grow-shop", no_delegate_prompt=True
        )

    assert captured["env"].get("HERMES_DELEGATE_NO_PROMPT") == "1"


def test_delegate_prompt_absent_by_default(tmp_path):
    """Without no_delegate_prompt the env var must NOT be set."""
    profile_home = tmp_path / "profiles" / "grow-shop"
    profile_home.mkdir(parents=True)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "grow-shop")

    assert "HERMES_DELEGATE_NO_PROMPT" not in captured["env"]


def test_delegate_env_pins_home_to_profile_subprocess_home(tmp_path):
    """When <profile_home>/home exists, the subprocess HOME must point at it so
    git/gh use the profile's own credentialed HOME (not the gateway default)."""
    profile_home = tmp_path / "profiles" / "finview"
    (profile_home / "home").mkdir(parents=True)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert captured["env"]["HOME"] == str(profile_home / "home")


def test_delegate_env_leaves_home_unset_when_no_profile_home(tmp_path, monkeypatch):
    """No <profile_home>/home dir → HOME must be left as inherited (the existing
    credentialed fallback), never forced onto an empty/non-existent dir."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)  # note: no home/ subdir
    monkeypatch.setenv("HOME", "/gateway/home")

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    # HOME is whatever was inherited — not rewritten to a missing profile home.
    assert captured["env"]["HOME"] == "/gateway/home"
    assert captured["env"]["HOME"] != str(profile_home / "home")


def test_delegate_env_sources_token_from_git_credentials(tmp_path, monkeypatch):
    """The Zeabur gateway/delegate env carries NO token; the only live token is
    on disk in <profile>/home/.git-credentials. gh must get that exact token,
    exported as both GITHUB_TOKEN and GH_TOKEN, even with nothing in env."""
    profile_home = tmp_path / "profiles" / "finview"
    home = profile_home / "home"
    home.mkdir(parents=True)
    (home / ".git-credentials").write_text(
        "https://x-access-token:ghp_ondiskTOKEN@github.com\n"
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert captured["env"]["GITHUB_TOKEN"] == "ghp_ondiskTOKEN"
    assert captured["env"]["GH_TOKEN"] == "ghp_ondiskTOKEN"


def test_delegate_writes_gh_hosts_yml_from_credentials(tmp_path, monkeypatch):
    """The env-token path is stripped by the subprocess blocklist, so gh must
    auth from disk. The delegate must write <home>/.config/gh/hosts.yml with the
    same token git uses, mode 600, even with nothing in env."""
    import stat as _stat

    profile_home = tmp_path / "profiles" / "finview"
    home = profile_home / "home"
    home.mkdir(parents=True)
    (home / ".git-credentials").write_text(
        "https://x-access-token:ghp_ondiskTOKEN@github.com\n"
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def _fake_run(cmd, **kwargs):
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    hosts = home / ".config" / "gh" / "hosts.yml"
    assert hosts.is_file()
    body = hosts.read_text()
    assert "github.com:" in body
    assert "oauth_token: ghp_ondiskTOKEN" in body
    assert _stat.S_IMODE(hosts.stat().st_mode) == 0o600


def test_delegate_skips_gh_hosts_when_no_profile_home(tmp_path, monkeypatch):
    """No <profile>/home dir → no place to write hosts.yml; must not crash and
    must not create one outside the profile."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)  # no home/
    monkeypatch.setenv("GH_TOKEN", "ghp_envonly")

    def _fake_run(cmd, **kwargs):
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert not (profile_home / "home").exists()


def test_delegate_env_credential_token_overrides_stale_env(tmp_path, monkeypatch):
    """The credential-file token (what git uses) wins over a stale inherited env
    value, so gh and git never disagree."""
    profile_home = tmp_path / "profiles" / "finview"
    home = profile_home / "home"
    home.mkdir(parents=True)
    (home / ".git-credentials").write_text(
        "https://x-access-token:ghp_fresh@github.com\n"
    )
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_stale")

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert captured["env"]["GITHUB_TOKEN"] == "ghp_fresh"
    assert captured["env"]["GH_TOKEN"] == "ghp_fresh"


def test_delegate_env_mirrors_env_token_when_no_credential_file(tmp_path, monkeypatch):
    """No <profile>/home credential file → fall back to mirroring whatever token
    is in the env (GH_TOKEN→GITHUB_TOKEN, the var that survives the blocklist)."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)  # no home/ → no credential file
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_only_gh")

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert captured["env"]["GITHUB_TOKEN"] == "ghp_only_gh"
    assert captured["env"]["GH_TOKEN"] == "ghp_only_gh"


def test_delegate_env_does_not_invent_token_when_absent(tmp_path, monkeypatch):
    """No credential file and no env token → neither var is fabricated."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert "GITHUB_TOKEN" not in captured["env"]
    assert "GH_TOKEN" not in captured["env"]


def test_delegate_fails_closed_when_credentials_file_broken(tmp_path, monkeypatch):
    """Regression: a profile WITH its own .git-credentials file that yields no
    usable token (empty/garbled/wrong host) must NOT fall back to the ambient
    identity. Falling back there silently overwrites this profile's hosts.yml
    with a different account's token — the same class of bug as the auditor
    identity leak on biglobster#408 (whichever token ends up on disk is what
    `gh` authenticates as). The profile is left unauthenticated for this run
    instead (fail-closed), and no hosts.yml is written."""
    profile_home = tmp_path / "profiles" / "finview"
    home = profile_home / "home"
    home.mkdir(parents=True)
    # Present but broken: no github.com entry parses to a token.
    (home / ".git-credentials").write_text("https://x-access-token:tok@gitlab.com\n")
    monkeypatch.setenv("GITHUB_TOKEN", "ceo-ambient-token")
    monkeypatch.setenv("GH_TOKEN", "ceo-ambient-token")

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert "GITHUB_TOKEN" not in captured["env"]
    assert "GH_TOKEN" not in captured["env"]
    assert not (home / ".config" / "gh" / "hosts.yml").exists()


def test_delegate_still_falls_back_when_no_credentials_file_at_all(tmp_path, monkeypatch):
    """No-regression guard: a profile home that exists but has never had a
    .git-credentials file at all (not "broken", just absent) keeps the
    original, intended fallback to the ambient/shared identity — this is how
    finview/grow-shop are meant to work (PRs #31/#32)."""
    profile_home = tmp_path / "profiles" / "finview"
    home = profile_home / "home"
    home.mkdir(parents=True)  # home/ exists, but no .git-credentials inside it
    monkeypatch.setenv("GITHUB_TOKEN", "ceo-ambient-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert captured["env"]["GITHUB_TOKEN"] == "ceo-ambient-token"
    assert captured["env"]["GH_TOKEN"] == "ceo-ambient-token"
    assert (home / ".config" / "gh" / "hosts.yml").is_file()


def test_token_from_git_credentials_parses_forms(tmp_path):
    """Parser handles x-access-token, user:token, and user-less token forms; and
    ignores non-github lines."""
    p = tmp_path / ".git-credentials"
    p.write_text(
        "https://gitlab.com:somethingelse@gitlab.com\n"
        "https://x-access-token:ghp_xat@github.com\n"
    )
    assert delegate_core._token_from_git_credentials(str(p)) == "ghp_xat"

    p.write_text("https://ghp_bareToken@github.com\n")
    assert delegate_core._token_from_git_credentials(str(p)) == "ghp_bareToken"

    p.write_text("https://someuser:ghp_userpw@github.com\n")
    assert delegate_core._token_from_git_credentials(str(p)) == "ghp_userpw"

    # Missing file / no github entry → None.
    assert delegate_core._token_from_git_credentials(str(tmp_path / "nope")) is None
    p.write_text("https://x-access-token:tok@gitlab.com\n")
    assert delegate_core._token_from_git_credentials(str(p)) is None


def test_auto_profile_field_on_message_event():
    """MessageEvent must carry auto_profile=None by default and accept a profile name."""
    from gateway.platforms.base import MessageEvent

    ev = MessageEvent(text="hello")
    assert ev.auto_profile is None

    ev2 = MessageEvent(text="hello", auto_profile="grow-shop")
    assert ev2.auto_profile == "grow-shop"


def test_resume_history_sets_env_var(tmp_path):
    """resume_history=True must inject HERMES_DELEGATE_RESUME=1 into the subprocess env."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile(
            "task-1", "do it", "finview", resume_history=True
        )

    assert captured["env"].get("HERMES_DELEGATE_RESUME") == "1"


def test_resume_history_absent_by_default(tmp_path):
    """Without resume_history the env var must NOT be set (one-shot delegations stay stateless)."""
    profile_home = tmp_path / "profiles" / "finview"
    profile_home.mkdir(parents=True)

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(_ok_stdout())

    with patch("hermes_cli.profiles.resolve_profile_env", return_value=str(profile_home)), \
            patch("subprocess.run", _fake_run):
        delegate_core.run_delegate_in_profile("task-1", "do it", "finview")

    assert "HERMES_DELEGATE_RESUME" not in captured["env"]


class _FakeSessionDB:
    def __init__(self, history):
        self._history = history

    def get_messages_as_conversation(self, session_id):
        # Echo the session_id so the test can assert we loaded by the right key.
        return [{"role": "assistant", "content": f"prior for {session_id}"}] \
            if self._history else []


class _FakeAgent:
    last_kwargs: dict = {}
    last_init_kwargs: dict = {}
    last_instance = None

    def __init__(self, **kwargs):
        _FakeAgent.last_init_kwargs = kwargs
        _FakeAgent.last_instance = self
        # Default: no fallback used this turn (notice helper returns "").
        self._fallback_index = 0
        self._primary_runtime = {"model": kwargs.get("model")}
        self.model = kwargs.get("model")

    def run_conversation(self, **kwargs):
        _FakeAgent.last_kwargs = kwargs
        return {"final_response": "ok", "error": None}


def _patch_agent_runtime(session_db, config=None):
    """Patch the heavy deps of run_delegate_agent so it runs as a pure unit."""
    return [
        patch("hermes_cli.config.load_config",
              return_value=config if config is not None else {"model": "x/y"}),
        patch("hermes_state.SessionDB", return_value=session_db),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider",
              side_effect=Exception("skip runtime resolution")),
        patch("run_agent.AIAgent", _FakeAgent),
    ]


def test_run_delegate_agent_rehydrates_when_resume():
    """resume_history=True loads the per-task transcript and passes it as conversation_history."""
    fake_db = _FakeSessionDB(history=True)
    patches = _patch_agent_runtime(fake_db)
    for p in patches:
        p.start()
    try:
        delegate_core.run_delegate_agent(
            "agent:main:telegram:group:-100:61", "sí, hazlo", resume_history=True
        )
    finally:
        for p in patches:
            p.stop()

    history = _FakeAgent.last_kwargs.get("conversation_history")
    assert history == [
        {"role": "assistant", "content": "prior for agent:main:telegram:group:-100:61"}
    ]


def test_run_delegate_agent_stateless_without_resume():
    """Without resume_history the agent starts with no conversation_history (one-shot lane)."""
    fake_db = _FakeSessionDB(history=True)
    patches = _patch_agent_runtime(fake_db)
    for p in patches:
        p.start()
    try:
        delegate_core.run_delegate_agent("one-shot-task", "do it")
    finally:
        for p in patches:
            p.stop()

    assert _FakeAgent.last_kwargs.get("conversation_history") is None


def test_bounded_resume_history_small_input_untouched():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert delegate_core._bounded_resume_history(msgs, budget=1000) == msgs


def test_bounded_resume_history_zero_budget_disables_bounding():
    msgs = [{"role": "user", "content": "x" * 10_000}]
    assert delegate_core._bounded_resume_history(msgs, budget=0) == msgs


def test_bounded_resume_history_trims_to_budget_and_user_boundary():
    # Old, large turns that must be dropped; a recent pair that fits the budget.
    msgs = [
        {"role": "user", "content": "OLD" * 1000},        # ~3000 chars, dropped
        {"role": "assistant", "content": "OLD" * 1000},   # dropped
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
    ]
    out = delegate_core._bounded_resume_history(msgs, budget=200)
    # Only the recent pair survives, and the window opens on a user turn.
    assert out == [
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
    ]
    assert out[0]["role"] == "user"


def test_bounded_resume_history_front_trim_drops_orphan_tool_result():
    # A budget that would otherwise open the window on a tool result (no matching
    # assistant tool_calls in-window) must be trimmed to the next user turn.
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "ok"},
    ]
    out = delegate_core._bounded_resume_history(msgs, budget=40)
    # Window must not start on the orphan tool row.
    assert out[0]["role"] in {"user", "assistant"}
    assert all(
        not (i == 0 and m.get("role") == "tool") for i, m in enumerate(out)
    )


def test_delegate_passes_fallback_model_from_config():
    """A configured fallback_model must be forwarded to the agent in the delegate lane."""
    fb = {"provider": "openrouter", "model": "tencent/hy3-preview"}
    cfg = {"model": "openrouter/owl-alpha", "fallback_model": fb}
    patches = _patch_agent_runtime(_FakeSessionDB(history=False), config=cfg)
    for p in patches:
        p.start()
    try:
        delegate_core.run_delegate_agent("t", "hi")
    finally:
        for p in patches:
            p.stop()
    assert _FakeAgent.last_init_kwargs.get("fallback_model") == fb


def test_delegate_omits_fallback_model_when_absent():
    """Without fallback_model in config the agent kwarg must not be set."""
    patches = _patch_agent_runtime(_FakeSessionDB(history=False), config={"model": "m"})
    for p in patches:
        p.start()
    try:
        delegate_core.run_delegate_agent("t", "hi")
    finally:
        for p in patches:
            p.stop()
    assert "fallback_model" not in _FakeAgent.last_init_kwargs


def test_interactive_lane_caps_api_max_retries_for_fast_failover():
    """The standing interactive lane (resume_history + fallback) must cap the
    retry-before-failover budget so a saturated primary fails over fast."""
    fb = {"provider": "openrouter", "model": "tencent/hy3-preview"}
    cfg = {"model": "openrouter/owl-alpha", "fallback_model": fb}
    patches = _patch_agent_runtime(_FakeSessionDB(history=True), config=cfg)
    for p in patches:
        p.start()
    try:
        delegate_core.run_delegate_agent("topic-task", "hi", resume_history=True)
    finally:
        for p in patches:
            p.stop()
    assert _FakeAgent.last_instance._api_max_retries == 1


def test_oneshot_lane_leaves_retry_budget_untouched():
    """A one-shot orchestrator delegation (no resume_history) must NOT have its
    retry budget overridden — the default config value stands."""
    fb = {"provider": "openrouter", "model": "tencent/hy3-preview"}
    cfg = {"model": "openrouter/owl-alpha", "fallback_model": fb}
    patches = _patch_agent_runtime(_FakeSessionDB(history=False), config=cfg)
    for p in patches:
        p.start()
    try:
        delegate_core.run_delegate_agent("one-shot", "do it")
    finally:
        for p in patches:
            p.stop()
    # The override never ran, so the fake agent has no forced retry attribute.
    assert not hasattr(_FakeAgent.last_instance, "_api_max_retries")


def test_interactive_lane_without_fallback_keeps_retry_budget():
    """No fallback configured → no override (capping retries with nowhere to
    fail over to would just fail faster, not better)."""
    cfg = {"model": "openrouter/owl-alpha"}
    patches = _patch_agent_runtime(_FakeSessionDB(history=True), config=cfg)
    for p in patches:
        p.start()
    try:
        delegate_core.run_delegate_agent("topic-task", "hi", resume_history=True)
    finally:
        for p in patches:
            p.stop()
    assert not hasattr(_FakeAgent.last_instance, "_api_max_retries")


def test_interactive_lane_retry_cap_env_overridable(monkeypatch):
    """HERMES_DELEGATE_INTERACTIVE_MAX_RETRIES lets prod tune the cap."""
    monkeypatch.setenv("HERMES_DELEGATE_INTERACTIVE_MAX_RETRIES", "2")
    fb = {"provider": "openrouter", "model": "tencent/hy3-preview"}
    cfg = {"model": "openrouter/owl-alpha", "fallback_model": fb}
    patches = _patch_agent_runtime(_FakeSessionDB(history=True), config=cfg)
    for p in patches:
        p.start()
    try:
        delegate_core.run_delegate_agent("topic-task", "hi", resume_history=True)
    finally:
        for p in patches:
            p.stop()
    assert _FakeAgent.last_instance._api_max_retries == 2


def test_fallback_switch_notice():
    from agent.chat_completion_helpers import fallback_switch_notice
    from types import SimpleNamespace

    # No fallback used → empty notice.
    assert fallback_switch_notice(
        SimpleNamespace(_fallback_index=0, _primary_runtime={"model": "p"}, model="p")
    ) == ""

    # Fallback used → names primary and current model.
    notice = fallback_switch_notice(
        SimpleNamespace(
            _fallback_index=1,
            _primary_runtime={"model": "openrouter/owl-alpha"},
            model="tencent/hy3-preview",
        )
    )
    assert "owl-alpha" in notice and "tencent/hy3-preview" in notice
    assert notice.startswith("ℹ️")

    # Index advanced but model unchanged (deduped chain) → no false notice.
    assert fallback_switch_notice(
        SimpleNamespace(_fallback_index=1, _primary_runtime={"model": "m"}, model="m")
    ) == ""


def test_delegate_appends_fallback_notice_to_reply():
    """When the agent ends on a fallback model, the delegate reply carries the notice."""
    class _FallbackAgent(_FakeAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._fallback_index = 1
            self._primary_runtime = {"model": "openrouter/owl-alpha"}
            self.model = "tencent/hy3-preview"

        def run_conversation(self, **kwargs):
            return {"final_response": "respuesta", "error": None}

    patches = [
        patch("hermes_cli.config.load_config",
              return_value={"model": "openrouter/owl-alpha"}),
        patch("hermes_state.SessionDB", return_value=_FakeSessionDB(history=False)),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider",
              side_effect=Exception("skip")),
        patch("run_agent.AIAgent", _FallbackAgent),
    ]
    for p in patches:
        p.start()
    try:
        result = delegate_core.run_delegate_agent("t", "hola")
    finally:
        for p in patches:
            p.stop()
    assert result["final_response"].startswith("respuesta")
    assert "tencent/hy3-preview" in result["final_response"]
