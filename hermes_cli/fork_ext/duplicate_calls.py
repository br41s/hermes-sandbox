"""Cross-turn duplicate tool-call detection (#236), called from ``agent/tool_executor.py``."""

from __future__ import annotations

import hashlib
import json
import logging
import threading

logger = logging.getLogger(__name__)

# The registry lives on the AIAgent instance
# (per-run, discarded with it) and is guarded by this module-level lock rather
# than a per-agent one: the critical section is a dict lookup plus a hash
# compare, so contention between concurrent agents is irrelevant, and a single
# lock avoids the double-checked-locking subtlety of lazily creating a
# per-agent lock from inside a parallel tool batch.
#
# Deliberately NOT a process-global registry keyed by task_id. A shared
# mutable keyed on anything coarser than the agent leaks one run's state into
# another's — the class of bug that let one cron profile's sandbox carry its
# git identity into the next profile's job (see
# tests/tools/test_profile_sandbox_identity_leak.py).
_DUP_REGISTRY_LOCK = threading.Lock()

_DUP_NOTICE = (
    "\n\n[note: this exact tool call was already made earlier in this run "
    "(occurrence {count}) and returned an identical result]"
)


def duplicate_call_notice(agent, tool_call, args, result, *, name: str = "") -> str:
    """Return a one-line notice when this call repeats an earlier identical one.

    Fires only when BOTH the arguments and the result are byte-identical to a
    previous call in the same run. Requiring the result to match is what makes
    this safe to append unconditionally: re-running a test suite after a fix
    has identical arguments but different output, and must not be flagged as
    redundant — that is the agent verifying its own work, which is the
    behaviour we want.

    The comparison is against the PREVIOUS identical-argument call, not the
    first, so an alternating result (fail, pass, fail) is never reported as
    stable.

    tool_call may be None (the shared commit path only has the parsed
    call): the signature then comes from name plus canonical JSON of args.

    Returns "" whenever anything is off — a non-string result (multimodal
    blocks are never compared), an agent object without a ``__dict__``, or an
    unreadable argument payload. This is advisory output appended to a tool
    result; it must never be able to fail a tool call.

    Why a notice and not a block: the within-turn deduplicator
    (``AIAgent._deduplicate_tool_calls``) already removes repeats inside one
    batch, and measured production data shows zero within-turn duplicates but
    14-44% cross-turn ones. Blocking those would break every agent that
    legitimately re-runs a command to verify a change.
    """
    if not isinstance(result, str) or not result:
        return ""
    try:
        raw_args = getattr(getattr(tool_call, "function", None), "arguments", None)
        if not isinstance(raw_args, str):
            raw_args = json.dumps(args, sort_keys=True, default=str)
        name = getattr(getattr(tool_call, "function", None), "name", "") or name
        arg_sig = hashlib.sha256(
            (name + "\x00" + raw_args).encode("utf-8", "replace")
        ).hexdigest()
        res_sig = hashlib.sha256(result.encode("utf-8", "replace")).hexdigest()
    except Exception:
        logger.debug("duplicate-call notice: unreadable arguments", exc_info=True)
        return ""

    with _DUP_REGISTRY_LOCK:
        registry = getattr(agent, "_cross_turn_tool_calls", None)
        if registry is None:
            registry = {}
            try:
                agent._cross_turn_tool_calls = registry
            except Exception:
                logger.debug("duplicate-call notice: no registry on this agent", exc_info=True)
                return ""
        entry = registry.get(arg_sig)
        if entry is None:
            registry[arg_sig] = [1, res_sig]
            return ""
        entry[0] += 1
        count = entry[0]
        prior_res, entry[1] = entry[1], res_sig

    if prior_res != res_sig:
        return ""
    return _DUP_NOTICE.format(count=count)
