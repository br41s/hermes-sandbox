"""Regression tests for the rclone root-ownership incident (2026-10-08..11).

rclone run as root rewrote /opt/data/.config/rclone/rclone.conf as root:hermes
0600, so the nightly ops/backup-full.sh (user hermes) could not load it and the
Drive sync failed four nights running. Three layers prevent a repeat:

1. docker/rclone-exec-shim.sh drops root to hermes before running rclone.
2. ops/backup-full.sh reports an unreadable config, with the fix, in the log the
   DRIVE SYNC FAILED alert tails.
3. docker/stage2-hook.sh re-chowns the rclone config dir to hermes at boot.

Hermetic: no Docker, no real rclone, no /opt/data.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIM = REPO_ROOT / "docker" / "rclone-exec-shim.sh"
STAGE2_HOOK = REPO_ROOT / "docker" / "stage2-hook.sh"
BACKUP_FULL = REPO_ROOT / "ops" / "backup-full.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"

SH = shutil.which("sh")
BASH = shutil.which("bash")


def _script(path: Path, body: str, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)
    return path


# --- layer 1: the shim -------------------------------------------------------


def _shim_env(tmp_path: Path, *, uid: str, real: bool = True, s6: bool = True):
    """Copy the shim with its absolute paths pointed into tmp_path.

    Returns (shim_path, env). A fake ``id`` on PATH reports ``uid``; the fake
    rclone and s6-setuidgid print their argv so the test sees which hop ran.
    """
    text = SHIM.read_text()
    real_bin = tmp_path / "volume" / "rclone"
    text = text.replace("/opt/data/scripts/bin/rclone", str(real_bin))
    text = text.replace("/usr/local/bin/rclone", str(tmp_path / "none-a"))
    text = text.replace("/usr/bin/rclone", str(tmp_path / "none-b"))
    s6_bin = tmp_path / "command" / "s6-setuidgid"
    text = text.replace("/command/s6-setuidgid", str(s6_bin))
    shim = _script(tmp_path / "shim" / "rclone", text)

    fakes = tmp_path / "fakes"
    _script(fakes / "id", f"#!/bin/sh\necho {uid}\n")
    if real:
        _script(real_bin, '#!/bin/sh\necho "real uid=$(id -u) args=$*"\n')
    if s6:
        _script(s6_bin, '#!/bin/sh\nu=$1; c=$2; shift 2\n'
                        'echo "s6 user=$u home=$HOME cmd=$c args=$*"\n')
    env = {"PATH": f"{fakes}:/usr/bin:/bin", "HOME": "/root"}
    return shim, env, real_bin


@pytest.mark.skipif(SH is None, reason="sh not available")
def test_shim_root_drops_to_hermes_and_execs_real_binary(tmp_path: Path) -> None:
    shim, env, real_bin = _shim_env(tmp_path, uid="0")
    r = subprocess.run([SH, str(shim), "lsd", "x:"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"s6 user=hermes home=/opt/data cmd={real_bin} args=lsd x:"


@pytest.mark.skipif(SH is None, reason="sh not available")
def test_shim_non_root_execs_real_binary_directly(tmp_path: Path) -> None:
    shim, env, _ = _shim_env(tmp_path, uid="10000")
    r = subprocess.run([SH, str(shim), "lsd", "x:"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "real uid=10000 args=lsd x:"


@pytest.mark.skipif(SH is None, reason="sh not available")
@pytest.mark.parametrize("uid", ["0", "10000"])
def test_shim_missing_binary_exits_127(tmp_path: Path, uid: str) -> None:
    shim, env, _ = _shim_env(tmp_path, uid=uid, real=False)
    r = subprocess.run([SH, str(shim), "version"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 127
    assert "no rclone binary found" in r.stderr


@pytest.mark.skipif(SH is None, reason="sh not available")
def test_shim_root_without_s6_refuses_to_run_as_root(tmp_path: Path) -> None:
    shim, env, _ = _shim_env(tmp_path, uid="0", s6=False)
    r = subprocess.run([SH, str(shim), "version"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 126
    assert "real uid" not in r.stdout


def test_dockerfile_installs_rclone_shim_on_path() -> None:
    text = DOCKERFILE.read_text()
    assert (
        "COPY --chmod=0755 docker/rclone-exec-shim.sh /opt/hermes/bin/rclone" in text
    )
    # Same dir as the hermes shim, which is first on PATH.
    assert 'ENV PATH="/opt/hermes/bin:' in text


# --- layer 3: boot self-heal -------------------------------------------------


def _rclone_boot_block() -> str:
    text = STAGE2_HOOK.read_text()
    m = re.search(
        r'(if \[ -d "\$HERMES_HOME/\.config/rclone" \].*?\nfi)\n', text, re.S
    )
    assert m, "stage2-hook.sh must re-chown $HERMES_HOME/.config/rclone at boot"
    return m.group(1)


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize(
    ("dir_exists", "mis_owned", "expect_chown"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_stage2_chowns_rclone_dir_only_when_present_and_mis_owned(
    tmp_path: Path, dir_exists: bool, mis_owned: bool, expect_chown: bool
) -> None:
    home = tmp_path / "data"
    (home / ".config" / "rclone").mkdir(parents=True)
    if not dir_exists:
        (home / ".config" / "rclone").rmdir()
    harness = (
        "set -eu\n"
        f'HERMES_HOME="{home}"\n'
        f"tree_has_non_hermes_owner() {{ {'true' if mis_owned else 'false'}; }}\n"
        'chown_hermes_tree() { echo "CHOWN $1"; }\n'
        f"{_rclone_boot_block()}\n"
    )
    r = subprocess.run([BASH, "-c", harness], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (f"CHOWN {home}/.config/rclone" in r.stdout) is expect_chown


# --- layer 2: backup-full.sh pre-check ---------------------------------------


def _bash_major() -> int:
    if BASH is None:
        return 0
    out = subprocess.run([BASH, "-c", 'echo "${BASH_VERSINFO[0]}"'],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def _run_backup(tmp_path: Path, conf_mode: int):
    """Run ops/backup-full.sh against fakes; returns (proc, rclone_marker)."""
    outdir = tmp_path / "backups"
    marker = tmp_path / "rclone-called"
    hermes = _script(tmp_path / "hermes", '#!/bin/sh\n: >"$3"\n')  # backup -o ZIP
    rclone = _script(tmp_path / "rclone", f'#!/bin/sh\necho "$*" >>"{marker}"\n')
    conf = tmp_path / "rclone.conf"
    conf.write_text("[hermesdrive]\ntype = drive\n")
    conf.chmod(conf_mode)
    env = {
        **os.environ,
        "BACKUP_HERMES": str(hermes),
        "BACKUP_OUTDIR": str(outdir),
        "BACKUP_RCLONE": str(rclone),
        "BACKUP_RCLONE_CONF": str(conf),
    }
    proc = subprocess.run([BASH, str(BACKUP_FULL)], env=env,
                          capture_output=True, text=True)
    return proc, marker, outdir, conf


needs_bash4 = pytest.mark.skipif(
    _bash_major() < 4, reason="backup-full.sh uses mapfile (bash >= 4)"
)


@needs_bash4
@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_backup_reports_unreadable_rclone_config_with_the_fix(tmp_path: Path) -> None:
    proc, marker, outdir, conf = _run_backup(tmp_path, conf_mode=0o000)
    assert proc.returncode == 1
    # The existing alert path carries the message.
    assert "DRIVE SYNC FAILED" in proc.stdout
    assert f"chown hermes:hermes {conf}" in proc.stdout
    assert "never as root by full path" in proc.stdout
    log = (outdir / "last-drive-sync.log").read_text()
    assert "not readable" in log and "----------" in log  # owner/mode via ls -l
    assert not marker.exists(), "rclone must not run against an unreadable config"
    assert list(outdir.glob("hermes-backup-*.zip")), "local zip must be kept"


@needs_bash4
def test_backup_with_readable_config_still_syncs_silently(tmp_path: Path) -> None:
    proc, marker, _, conf = _run_backup(tmp_path, conf_mode=0o600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == ""
    assert f"--config {conf} copy" in marker.read_text()
