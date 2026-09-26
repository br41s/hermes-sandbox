"""Fork's own tests for the hermes_constants module, kept out of upstream's file so upstream merges do not conflict."""

import os
import time

import pytest

from hermes_constants import agent_browser_runnable


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell stubs; Windows uses .cmd shims")
class TestAgentBrowserRunnableProbeTimeout:
    """agent_browser_runnable()'s version probe must honour its timeout."""

    def _stub(self, tmp_path, name, body, mode=0o755):
        p = tmp_path / name
        p.write_text(body)
        p.chmod(mode)
        return p

    def test_probe_returns_promptly_when_child_leaves_a_grandchild(self, tmp_path):
        """A grandchild holding the inherited pipes must not stall the probe.

        ``subprocess.run(timeout=...)`` does not bound wall time while the
        output is piped: on TimeoutExpired it kills the direct child, then
        calls ``communicate()`` a second time to reap it -- and that call
        waits for EOF on the *pipes*, not for the process. agent-browser
        launches a browser that inherits those fds and outlives it, so EOF
        never arrives and the "10s" probe blocks until the grandchild dies.

        Not mocked on purpose: the defect lives in real fd/reaping behaviour,
        which a faked ``subprocess.run`` would hide. With pipes restored this
        fails twice over -- ~15s elapsed, and False via TimeoutExpired.
        """
        forking = self._stub(
            tmp_path,
            "agent-browser",
            # The backgrounded sleep inherits stdout/stderr and outlives its
            # parent, holding both fds open well past the probe's 10s cap.
            "#!/bin/sh\nsleep 15 &\necho 'agent-browser 0.27.1'\nexit 0\n",
        )

        start = time.monotonic()
        result = agent_browser_runnable(str(forking))
        elapsed = time.monotonic() - start

        assert result is True
        assert elapsed < 10, (
            f"probe blocked {elapsed:.1f}s on the grandchild's inherited pipe "
            "-- the timeout is unenforceable while stdout/stderr are pipes"
        )
