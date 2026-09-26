"""Fork's own tests for the langfuse observability plugin, kept out of upstream's file so upstream merges do not conflict."""
from __future__ import annotations

import importlib
import sys


class _RecordingObservation:
    """Records the calls the plugin makes against a root/child observation.

    Stands in for langfuse's ``LangfuseChain`` / ``LangfuseGeneration`` so the
    test doesn't need the optional SDK installed (the default test env omits
    the ``observability`` extra).  Mirrors the v4 span surface the plugin
    uses: ``update``, ``start_observation``, ``end``. ``set_trace_io`` is kept
    on this fake only because ``_RecordingLangfuse.start_as_current_observation``
    below uses it to record the input passed at creation — the plugin itself
    no longer calls it (deprecated trace-level I/O; see TestRootTraceIO)."""

    def __init__(self, name=None):
        self.name = name
        self.trace_input = None
        self.trace_output = None
        self.updates: list[dict] = []
        self.children: list["_RecordingObservation"] = []
        self.ended = False

    def set_trace_io(self, *, input=None, output=None):
        if input is not None:
            self.trace_input = input
        if output is not None:
            self.trace_output = output
        return self

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self

    def start_observation(self, *, name, as_type, input=None, metadata=None,
                          model=None, model_parameters=None):
        child = _RecordingObservation(name=name)
        self.children.append(child)
        return child

    def end(self, **_):
        self.ended = True
        return self


class _RootContext:
    """Context-manager wrapper returned by ``start_as_current_observation``."""

    def __init__(self, observation):
        self._observation = observation

    def __enter__(self):
        return self._observation

    def __exit__(self, *exc):
        return False


class _RecordingLangfuse:
    """Records ``create_trace_id`` / ``start_as_current_observation`` / ``flush``."""

    def __init__(self):
        self.flushed = 0
        self.root_observations: list[_RecordingObservation] = []

    def create_trace_id(self, *, seed=None):
        return f"trace::{seed}"

    def start_as_current_observation(self, *, trace_context=None, name, as_type="span",
                                     input=None, metadata=None, end_on_exit=None):
        obs = _RecordingObservation(name=name)
        if input is not None:
            obs.set_trace_io(input=input)
        self.root_observations.append(obs)
        return _RootContext(obs)

    def flush(self):
        self.flushed += 1


class TestRootTraceIO:
    """Regression guard for the v3→v4 fix: the trace ROOT observation must be
    created with name="Hermes turn" plus the user message as input, and the
    final reply must land as the root's output on finish.  This is the
    trace-level data that rendered as "Unnamed span" / null I/O before the SDK
    bump (server >= 3.187.0 no longer honored the v3 trace-attribute format).

    Root input/output must flow through ``start_as_current_observation(input=)``
    / ``root_span.update(output=)`` only — not the deprecated
    ``set_trace_io()`` escape hatch, which exists solely for legacy
    trace-level LLM-as-a-judge evaluators. The hermes Langfuse project has
    none (verified via the Evaluators/Evaluation Rules API), so retaining it
    would be dead compatibility code."""

    def _make_mod(self, monkeypatch):
        sys.modules.pop("plugins.observability.langfuse", None)
        mod = importlib.import_module("plugins.observability.langfuse")
        mod._TRACE_STATE.clear()
        # No SDK in the default test env → propagate_attributes is None and the
        # plugin takes the direct start_as_current_observation path. Pin that.
        monkeypatch.setattr(mod, "propagate_attributes", None, raising=False)
        return mod

    def test_root_observation_has_name_and_input_and_output(self, monkeypatch):
        mod = self._make_mod(monkeypatch)
        client = _RecordingLangfuse()
        monkeypatch.setattr(mod, "_get_langfuse", lambda: client)

        mod.on_pre_llm_request(
            task_id="task-1", session_id="sess-1", api_call_count=1,
            request_messages=[
                {"role": "system", "content": "you are hermes"},
                {"role": "user", "content": "What is 2+2?"},
            ],
            model="gpt-x", provider="openai",
        )

        assert len(client.root_observations) == 1, "expected exactly one root observation"
        root = client.root_observations[0]
        # Name is set at creation (server uses it as the trace name).
        assert root.name == "Hermes turn"
        # Trace-level input is the last user message, not null.
        assert root.trace_input == {"role": "user", "content": "What is 2+2?"}

        # Finish the turn with a content-only assistant reply (no tool calls)
        # → the plugin closes the trace and records the reply as output.
        mod.on_post_llm_call(
            task_id="task-1", session_id="sess-1", api_call_count=1,
            assistant_response="2+2 = 4",
        )

        assert any(u.get("output") for u in root.updates), "root.update(output=...) not called"
        assert root.updates[-1]["output"] == {"content": "2+2 = 4", "reasoning": None, "tool_calls": []}
        assert root.trace_output is None, "set_trace_io(output=...) must not be called (deprecated, no legacy evaluators)"
        assert root.ended is True
        assert client.flushed >= 1

    def test_finish_trace_detaches_root_context(self, monkeypatch):
        """Regression: _finish_trace must call root_ctx.__exit__ to detach the
        OTel context that _start_root_trace attached via root_ctx.__enter__().

        Leaking it (the pre-fix behavior — only root_span.end() was called)
        left the attachment to be torn down by GC at interpreter shutdown in a
        different context, raising the noisy "Token was created in a different
        Context" / GeneratorExit cascade in errors.log on every SIGTERM."""
        mod = self._make_mod(monkeypatch)
        client = _RecordingLangfuse()
        monkeypatch.setattr(mod, "_get_langfuse", lambda: client)

        exits = []

        class _TrackingRootCtx:
            ended = False

            def __exit__(self, *exc):
                exits.append(exc)
                return False

            def set_trace_io(self, **kw):
                pass

            def update(self, **kw):
                pass

            def end(self):
                self.ended = True

        ctx = _TrackingRootCtx()
        state = mod.TraceState(trace_id="t", root_ctx=ctx, root_span=ctx)
        task_key = mod._trace_key("task-1", "sess-1")
        monkeypatch.setitem(mod._TRACE_STATE, task_key, state)

        mod._finish_trace(task_key, output="done")

        assert ctx.ended is True, "root span was not ended"
        assert exits, "_finish_trace did not call root_ctx.__exit__ (OTel context leak)"
        assert exits[0] == (None, None, None)


class TestSessionPropagationToChildren:
    """Regression guard for the v4 observations-first data model.

    v4 aggregates on observations, not on the trace, so a child observation
    without ``session_id`` is excluded from session filtering and from session
    cost — and generations are exactly where the cost lives.
    ``propagate_attributes`` only stamps spans opened inside its scope, and the
    root's scope closes at the end of ``_start_root_trace``; relying on the
    root's OTel context to carry into later hooks stamped children only
    intermittently (measured on production traces 2026-09-22: 236 of 286
    children missing the session id), because a turn's hooks can run on
    different threads. Children must therefore be opened inside a re-entered
    propagation scope.
    """

    def _make_mod(self, monkeypatch):
        sys.modules.pop("plugins.observability.langfuse", None)
        mod = importlib.import_module("plugins.observability.langfuse")
        mod._TRACE_STATE.clear()
        return mod

    def test_children_are_opened_inside_the_session_scope(self, monkeypatch):
        mod = self._make_mod(monkeypatch)
        events: list = []

        class _Scope:
            def __init__(self, session_id):
                self.session_id = session_id

            def __enter__(self):
                events.append(("enter", self.session_id))
                return self

            def __exit__(self, *exc):
                events.append(("exit", self.session_id))
                return False

        monkeypatch.setattr(
            mod, "propagate_attributes",
            lambda **kw: _Scope(kw.get("session_id")), raising=False,
        )

        class _Span:
            def start_observation(self, **kw):
                events.append(("start_observation", kw.get("name")))
                return _Span()

        state = mod.TraceState(
            trace_id="t", root_ctx=None, root_span=_Span(), session_id="sess-1",
        )
        mod._start_child_observation(
            state, client=object(), name="LLM call 1",
            as_type="generation", input_value={},
        )

        assert events == [
            ("enter", "sess-1"),
            ("start_observation", "LLM call 1"),
            ("exit", "sess-1"),
        ], "child must be created INSIDE propagate_attributes(session_id=...)"

    def test_root_records_the_session_it_propagated(self, monkeypatch):
        """The state must carry the same value propagated onto the root —
        including the task_key fallback, or children land in a different
        session than their own root."""
        mod = self._make_mod(monkeypatch)
        monkeypatch.setattr(mod, "propagate_attributes", None, raising=False)
        client = _RecordingLangfuse()
        monkeypatch.setattr(mod, "_get_langfuse", lambda: client)

        mod.on_pre_llm_request(
            task_id="task-1", session_id="sess-9", api_call_count=1,
            request_messages=[{"role": "user", "content": "hi"}],
        )
        state = mod._TRACE_STATE[mod._trace_key("task-1", "sess-9")]
        assert state.session_id == "sess-9"

        mod._TRACE_STATE.clear()
        mod.on_pre_llm_request(
            task_id="task-2", session_id="", api_call_count=1,
            request_messages=[{"role": "user", "content": "hi"}],
        )
        key = mod._trace_key("task-2", "")
        assert mod._TRACE_STATE[key].session_id == key

    def test_child_is_still_created_when_propagation_blows_up(self, monkeypatch):
        """Observability must fail open: a broken scope loses the session id,
        never the observation."""
        mod = self._make_mod(monkeypatch)

        def _boom(**kw):
            raise RuntimeError("no context")

        monkeypatch.setattr(mod, "propagate_attributes", _boom, raising=False)
        created = []

        class _Span:
            def start_observation(self, **kw):
                created.append(kw.get("name"))
                return _Span()

        state = mod.TraceState(
            trace_id="t", root_ctx=None, root_span=_Span(), session_id="sess-1",
        )
        obs = mod._start_child_observation(
            state, client=object(), name="Tool: terminal",
            as_type="tool", input_value={},
        )
        assert obs is not None
        assert created == ["Tool: terminal"], "exactly one observation, no duplicate"
