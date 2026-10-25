"""docker/cont-init.d/03-biglobster-config section 4d: the dependency install state under
$HERMES_HOME/installs is handed back to hermes on every boot.

Every `hermes` start takes installs/<key>/.install.lock and exits 1 when it cannot write
it. A `hermes` command run as root on the pod created that lock root:root 0600 on
2026-10-23, and the 2026-10-25 deploy crash-looped dashboard and gateway-default.

The section is run for real with sh against a fake ``chown`` on PATH (the real one needs
root), so the test checks what the boot actually does, not the script's wording.
"""

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "docker" / "cont-init.d" / "03-biglobster-config"


def _section() -> str:
    text = INIT.read_text(encoding="utf-8")
    return text[text.index("# --- 4d:"):text.index("# --- 5:")]


def _run(tmp_path: Path, *, installs: bool, chown_code: int = 0):
    home = tmp_path / "data"
    home.mkdir()
    if installs:
        (home / "installs" / "351f81cc06284494").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "chown.calls"
    chown = bindir / "chown"
    chown.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit {chown_code}\n', encoding="utf-8")
    chown.chmod(chown.stat().st_mode | stat.S_IEXEC)
    script = tmp_path / "section.sh"
    script.write_text(_section(), encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}")
    result = subprocess.run(["sh", str(script)], capture_output=True, text=True, env=env, timeout=30, check=False)
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return result, lines, home


def test_the_install_state_is_chowned_to_hermes_recursively(tmp_path):
    result, calls, home = _run(tmp_path, installs=True)
    assert result.returncode == 0, result.stderr
    assert calls == [f"-R hermes:hermes {home / 'installs'}"]
    assert "Install state ownership enforced" in result.stdout


def test_a_fresh_volume_without_install_state_is_left_alone(tmp_path):
    result, calls, home = _run(tmp_path, installs=False)
    assert result.returncode == 0, result.stderr
    assert calls == []
    assert not (home / "installs").exists()        # hermes creates it, as itself


def test_a_failing_chown_is_reported_and_does_not_stop_the_boot(tmp_path):
    result, calls, home = _run(tmp_path, installs=True, chown_code=1)
    assert result.returncode == 0
    assert len(calls) == 1
    assert f"could not chown {home / 'installs'}" in result.stdout


def test_the_section_runs_before_any_boot_step_that_starts_hermes():
    text = INIT.read_text(encoding="utf-8")
    assert text.index("# --- 4d:") < text.index("# --- 5:")
