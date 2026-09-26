"""Fork's own tests for run_agent.py (AIAgent), kept out of upstream's file so upstream merges do not conflict."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    """Build minimal tool definition list accepted by AIAgent.__init__."""
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture(autouse=True)
def _fast_retry_backoff(monkeypatch):
    """Copied from tests/run_agent/conftest.py, which does not apply here:
    short-circuit retry backoff so the retry paths cost no wall-clock time."""
    import run_agent

    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)
    try:
        from agent import conversation_loop as _conv_loop
        monkeypatch.setattr(_conv_loop, "jittered_backoff", lambda *a, **k: 0.0)
    except ImportError:
        pass


@pytest.fixture()
def agent():
    """Minimal AIAgent with mocked OpenAI client and tool loading."""
    with (
        patch(
            "run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


def _mock_assistant_msg(
    content="Hello",
    tool_calls=None,
    reasoning=None,
    reasoning_content=None,
    reasoning_details=None,
):
    """Return a SimpleNamespace mimicking an OpenAI ChatCompletionMessage."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning is not None:
        msg.reasoning = reasoning
    if reasoning_content is not None:
        msg.reasoning_content = reasoning_content
    if reasoning_details is not None:
        msg.reasoning_details = reasoning_details
    return msg


def _mock_tool_call(name="web_search", arguments="{}", call_id=None):
    """Return a SimpleNamespace mimicking a tool call object."""
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(
    content="Hello",
    finish_reason="stop",
    tool_calls=None,
    reasoning=None,
    reasoning_content=None,
    reasoning_details=None,
    usage=None,
):
    """Return a SimpleNamespace mimicking an OpenAI ChatCompletion response."""
    msg = _mock_assistant_msg(
        content=content,
        tool_calls=tool_calls,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
        reasoning_details=reasoning_details,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    if usage:
        resp.usage = SimpleNamespace(**usage)
    else:
        resp.usage = None
    return resp


class TestTruncatedToolArgsRecovery:
    """run_conversation retries a truncated tool call with a smaller-writes
    nudge, and a model that complies recovers the turn."""

    def _setup_agent(self, agent):
        """Common setup for run_conversation tests."""
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False

    def test_truncated_tool_args_recover_when_model_writes_smaller(self, agent):
        """The nudged retry is a real recovery path: a model that responds to
        it with a chunked write executes normally instead of losing the run."""
        self._setup_agent(agent)
        agent.valid_tool_names.add("write_file")
        bad_tc = _mock_tool_call(
            name="write_file",
            arguments='{"path":"report.md","content":"partial',
            call_id="c1",
        )
        truncated = _mock_response(
            content="", finish_reason="tool_calls", tool_calls=[bad_tc],
        )
        good_tc = _mock_tool_call(
            name="write_file",
            arguments='{"path":"report.md","content":"chunk one"}',
            call_id="c2",
        )
        chunked = _mock_response(
            content="", finish_reason="tool_calls", tool_calls=[good_tc],
        )
        final_resp = _mock_response(content="Done!", finish_reason="stop")

        with (
            patch("run_agent.handle_function_call", return_value='{"success":true}') as mock_hfc,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            agent.client.chat.completions.create.side_effect = [
                truncated, chunked, final_resp,
            ]
            result = agent.run_conversation("write the report")

        assert result["final_response"] == "Done!"
        mock_hfc.assert_called_once()
        # The counter resets on the clean turn, so a later oversized write in
        # the same turn still gets its own full retry budget.
        assert agent._truncated_tool_args_retries == 0

    def test_truncated_tool_args_give_up_after_bounded_retries(self, agent):
        """When a router rewrites finish_reason from 'length' to 'tool_calls',
        truncated JSON arguments are still never executed — but the turn gets
        two bounded retries asking for smaller writes before it gives up."""
        self._setup_agent(agent)
        agent.valid_tool_names.add("write_file")
        bad_tc = _mock_tool_call(
            name="write_file",
            arguments='{"path":"report.md","content":"partial',
            call_id="c1",
        )
        resp = _mock_response(
            content="", finish_reason="tool_calls", tool_calls=[bad_tc],
        )
        agent.client.chat.completions.create.return_value = resp

        with (
            patch("run_agent.handle_function_call") as mock_handle_function_call,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("write the report")

        assert result["completed"] is False
        assert result["partial"] is True
        assert "truncated due to output length limit" in result["error"]
        mock_handle_function_call.assert_not_called()
        # One initial call + exactly 2 recovery attempts, not an unbounded loop.
        assert agent.client.chat.completions.create.call_count == 3
        # The retries told the model to write smaller, and never re-sent the
        # oversized arguments back to the provider.
        # (The alternation repairer may merge consecutive user turns, so count
        # occurrences of the nudge rather than the messages carrying them.)
        sent = agent.client.chat.completions.create.call_args[1]["messages"]
        nudge_count = sum(
            (m.get("content") or "").count("smaller tool calls") for m in sent
        )
        assert nudge_count == 2
        assert not any(m.get("tool_calls") for m in sent)
