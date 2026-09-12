"""Cross-turn duplicate tool-call notice.

Measured in production (12 runs per agent, 2026-09-13): the within-turn
deduplicator ``AIAgent._deduplicate_tool_calls`` leaves **zero** duplicates
inside a single batch, but 14-44% of tool calls in long runs are exact repeats
ACROSS turns — auditor 172/387, Shoroban Product Sheets 132/760, Shoroban Gap
Hunter 35/252. Those repeats re-insert ~227KB, ~264KB and ~60KB of identical
tool output back into context respectively.

The agent cannot see this from its own history: its reasoning IS replayed
(verified against the stored ``reasoning_content`` and a live provider probe),
it simply does not act on it. So the notice puts the fact in the tool result,
which is the channel the model reliably attends to.

The load-bearing safety property is that it fires only when the RESULT is also
identical. Re-running a command to verify a change is the behaviour we want,
and it must never be labelled redundant.
"""

import threading

import pytest

from agent.tool_executor import _duplicate_call_notice


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Call:
    def __init__(self, name="terminal", arguments='{"command": "ls"}'):
        self.function = _Fn(name, arguments)


class _Agent:
    """Minimal stand-in — the helper only ever touches one attribute."""


def test_first_call_is_never_flagged():
    agent = _Agent()
    assert _duplicate_call_notice(agent, _Call(), {}, "output") == ""


def test_identical_call_and_result_is_flagged():
    agent = _Agent()
    _duplicate_call_notice(agent, _Call(), {}, "output")
    notice = _duplicate_call_notice(agent, _Call(), {}, "output")
    assert "already made earlier in this run" in notice
    assert "occurrence 2" in notice


def test_occurrence_count_climbs():
    agent = _Agent()
    for _ in range(3):
        _duplicate_call_notice(agent, _Call(), {}, "output")
    assert "occurrence 4" in _duplicate_call_notice(agent, _Call(), {}, "output")


def test_same_arguments_different_result_is_not_flagged():
    """The safety property: re-running a test suite after a fix has identical
    arguments and different output. That is the agent verifying its own work
    and must never be called redundant."""
    agent = _Agent()
    call = _Call("terminal", '{"command": "pytest"}')
    assert _duplicate_call_notice(agent, call, {}, "1 failed") == ""
    assert _duplicate_call_notice(agent, call, {}, "2 passed") == ""


def test_alternating_result_never_reports_stable():
    """Compare against the PREVIOUS identical-argument call, not the first, so
    fail -> pass -> fail is never reported as an unchanged repeat."""
    agent = _Agent()
    call = _Call("terminal", '{"command": "pytest"}')
    assert _duplicate_call_notice(agent, call, {}, "fail") == ""
    assert _duplicate_call_notice(agent, call, {}, "pass") == ""
    assert _duplicate_call_notice(agent, call, {}, "fail") == ""


def test_different_arguments_are_independent():
    agent = _Agent()
    a = _Call("terminal", '{"command": "ls"}')
    b = _Call("terminal", '{"command": "pwd"}')
    assert _duplicate_call_notice(agent, a, {}, "same") == ""
    assert _duplicate_call_notice(agent, b, {}, "same") == ""
    assert "occurrence 2" in _duplicate_call_notice(agent, a, {}, "same")


def test_different_tool_names_are_independent():
    agent = _Agent()
    args = '{"path": "x"}'
    assert _duplicate_call_notice(agent, _Call("read_file", args), {}, "same") == ""
    assert _duplicate_call_notice(agent, _Call("write_file", args), {}, "same") == ""


def test_registry_is_per_agent_not_global():
    """Two concurrent runs must not see each other's calls. A registry keyed
    coarser than the agent is the leak class that let one cron profile's
    sandbox carry its git identity into the next profile's job."""
    a, b = _Agent(), _Agent()
    assert _duplicate_call_notice(a, _Call(), {}, "output") == ""
    # Same call, different agent — must still be a first occurrence.
    assert _duplicate_call_notice(b, _Call(), {}, "output") == ""


def test_multimodal_result_is_skipped():
    agent = _Agent()
    blocks = [{"type": "text", "text": "x"}, {"type": "image_url"}]
    assert _duplicate_call_notice(agent, _Call(), {}, blocks) == ""
    assert _duplicate_call_notice(agent, _Call(), {}, blocks) == ""


def test_empty_result_is_skipped():
    agent = _Agent()
    assert _duplicate_call_notice(agent, _Call(), {}, "") == ""
    assert _duplicate_call_notice(agent, _Call(), {}, "") == ""


def test_falls_back_to_parsed_args_when_arguments_missing():
    """Some providers hand back tool calls without a raw argument string."""
    class _Bare:
        function = None

    agent = _Agent()
    assert _duplicate_call_notice(agent, _Bare(), {"a": 1}, "out") == ""
    assert "occurrence 2" in _duplicate_call_notice(agent, _Bare(), {"a": 1}, "out")


def test_unhashable_args_do_not_raise():
    """Advisory output must never be able to fail a tool call."""
    class _Bare:
        function = None

    agent = _Agent()
    unserializable = {"fn": lambda: None}
    assert _duplicate_call_notice(agent, _Bare(), unserializable, "out") == ""


def test_agent_without_dict_does_not_raise():
    class _Slotted:
        __slots__ = ()

    assert _duplicate_call_notice(_Slotted(), _Call(), {}, "out") == ""


def test_parallel_batch_is_thread_safe():
    """Tools run on a DaemonThreadPoolExecutor, so the registry is mutated
    concurrently. Exactly one of N identical concurrent calls may be the
    first; the rest must be flagged, and none may raise."""
    agent = _Agent()
    results = []
    errors = []

    def worker():
        try:
            results.append(_duplicate_call_notice(agent, _Call(), {}, "output"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 24
    assert sum(1 for r in results if r == "") == 1, "exactly one first occurrence"
    counts = sorted(
        int(r.split("occurrence ")[1].split(")")[0]) for r in results if r
    )
    assert counts == list(range(2, 25)), "every occurrence numbered exactly once"


def test_signature_survives_oversized_results():
    """Regression: the signature must be taken on the RAW result.

    ``maybe_persist_tool_result`` replaces an oversized result with a pointer
    string built from ``tool_use_id``, which is unique per call. Hashing the
    post-persistence string would make two identical large results look
    different and silently disable the notice for exactly the results whose
    repetition is most expensive — the auditor's duplicates included 3947-byte
    payloads, and 227KB of duplicate output across 12 runs.

    This pins the helper's contract: identical large content, different
    call ids, still flagged.
    """
    agent = _Agent()
    big = "x" * 200_000
    assert _duplicate_call_notice(agent, _Call(), {}, big) == ""
    notice = _duplicate_call_notice(agent, _Call(), {}, big)
    assert "occurrence 2" in notice
