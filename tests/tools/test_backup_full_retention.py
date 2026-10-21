"""ops/backup-full.sh local retention: delete a local zip only once Drive has it.

Until 2026-10-21 `hermes backup` pruned hermes-backup-*.zip to its default 3
before the script uploaded anything, so local deletion never depended on the
Drive copy. The script now passes `--keep 0`, keeps the newest LOCAL_KEEP zips,
and deletes older ones only after `rclone check --one-way` verifies them.

Hermetic: a fake rclone backed by a directory stands in for Drive.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_FULL = REPO_ROOT / "ops" / "backup-full.sh"
BASH = shutil.which("bash")

FAKE_RCLONE = r'''#!PYTHON
import filecmp, os, shutil, sys
drive = os.environ["FAKE_DRIVE"]
args = [a for a in sys.argv[1:]]
if args[:1] == ["--config"]:
    args = args[2:]
with open(os.environ["FAKE_RCLONE_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")

def remote(p):
    return os.path.join(drive, p.split(":", 1)[1].lstrip("/").split("/", 1)[1]) if ":" in p else p

cmd = args[0]
if cmd == "copy":
    dest = os.path.join(drive, os.path.basename(args[1]))
    if os.environ.get("FAKE_COPY_FAIL"):
        sys.exit(1)
    if os.path.exists(dest) and not filecmp.cmp(args[1], dest, shallow=False):
        sys.exit(1)  # --immutable: never overwrite a differing file
    shutil.copy2(args[1], dest)
elif cmd == "check":
    name = args[args.index("--include") + 1].lstrip("/")
    src, dst = os.path.join(args[1], name), os.path.join(drive, name)
    sys.exit(0 if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False) else 1)
elif cmd == "lsf":
    print("\n".join(sorted(os.listdir(drive))))
elif cmd == "deletefile":
    os.remove(remote(args[1]))
'''


def _bash_major() -> int:
    if BASH is None:
        return 0
    out = subprocess.run([BASH, "-c", 'echo "${BASH_VERSINFO[0]}"'],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


pytestmark = pytest.mark.skipif(_bash_major() < 4, reason="backup-full.sh uses mapfile (bash >= 4)")


def _exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def _setup(tmp_path: Path, old: dict[str, bytes], on_drive: dict[str, bytes]):
    """Local `old` zips (oldest first) and Drive contents; returns (env, outdir, drive)."""
    outdir, drive = tmp_path / "backups", tmp_path / "drive"
    outdir.mkdir()
    drive.mkdir()
    for i, (name, data) in enumerate(old.items()):
        p = outdir / name
        p.write_bytes(data)
        os.utime(p, (1_000_000 + i, 1_000_000 + i))  # ls -1t order: later = newer
    for name, data in on_drive.items():
        (drive / name).write_bytes(data)
    hermes = _exe(tmp_path / "hermes", '#!/bin/sh\necho "$*" >"$HERMES_ARGV"\necho new >"$3"\n')
    rclone = _exe(tmp_path / "rclone", FAKE_RCLONE.replace("#!PYTHON", f"#!{sys.executable}"))
    conf = tmp_path / "rclone.conf"
    conf.write_text("[hermesdrive]\ntype = drive\n")
    env = {
        **os.environ,
        "BACKUP_HERMES": str(hermes),
        "BACKUP_OUTDIR": str(outdir),
        "BACKUP_RCLONE": str(rclone),
        "BACKUP_RCLONE_CONF": str(conf),
        "FAKE_DRIVE": str(drive),
        "FAKE_RCLONE_LOG": str(tmp_path / "rclone.log"),
        "HERMES_ARGV": str(tmp_path / "hermes.argv"),
    }
    return env, outdir, drive


def _run(env):
    return subprocess.run([BASH, str(BACKUP_FULL)], env=env, capture_output=True, text=True)


def _local(outdir: Path) -> list[str]:
    return sorted(p.name for p in outdir.glob("hermes-backup-*.zip"))


OLD = {
    "hermes-backup-20261018-130038.zip": b"a",
    "hermes-backup-20261019-130038.zip": b"b",
    "hermes-backup-20261020-130046.zip": b"c",
}


def test_hermes_backup_runs_without_its_own_prune(tmp_path: Path) -> None:
    env, _, _ = _setup(tmp_path, {}, {})
    assert _run(env).returncode == 0
    assert "--keep 0" in Path(env["HERMES_ARGV"]).read_text()


def test_verified_old_zips_are_pruned_to_two(tmp_path: Path) -> None:
    env, outdir, drive = _setup(tmp_path, OLD, OLD)
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == ""
    local = _local(outdir)
    assert len(local) == 2
    assert "hermes-backup-20261020-130046.zip" in local  # newest old one stays
    assert set(local) <= {p.name for p in drive.iterdir()}  # both are on Drive too


def test_zip_that_missed_drive_is_uploaded_then_pruned(tmp_path: Path) -> None:
    # A failed night left 20261018 off Drive; only the newest zip is uploaded
    # each night, so without the retry it would block pruning forever.
    on_drive = {k: v for k, v in OLD.items() if "20261018" not in k}
    env, outdir, drive = _setup(tmp_path, OLD, on_drive)
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (drive / "hermes-backup-20261018-130038.zip").read_bytes() == b"a"
    assert len(_local(outdir)) == 2


def test_mismatched_drive_copy_is_kept_locally_and_alerts(tmp_path: Path) -> None:
    corrupt = {**OLD, "hermes-backup-20261019-130038.zip": b"truncated"}
    env, outdir, drive = _setup(tmp_path, OLD, corrupt)
    proc = _run(env)
    assert proc.returncode == 1
    assert "DRIVE SYNC FAILED" in proc.stdout
    assert "not verified" in proc.stdout
    local = _local(outdir)
    assert "hermes-backup-20261019-130038.zip" in local  # unverified: kept
    assert "hermes-backup-20261018-130038.zip" not in local  # verified: pruned anyway
    assert (drive / "hermes-backup-20261019-130038.zip").read_bytes() == b"truncated"


def test_drive_is_pruned_to_seven_even_when_a_zip_is_unverified(tmp_path: Path) -> None:
    extra = {f"hermes-backup-202610{d:02d}-130000.zip": b"x" for d in range(1, 9)}
    corrupt = {**extra, **OLD, "hermes-backup-20261019-130038.zip": b"truncated"}
    env, _, drive = _setup(tmp_path, OLD, corrupt)
    assert _run(env).returncode == 1
    assert len(list(drive.iterdir())) == 7


def test_failed_upload_deletes_nothing(tmp_path: Path) -> None:
    env, outdir, _ = _setup(tmp_path, OLD, OLD)
    env["FAKE_COPY_FAIL"] = "1"
    proc = _run(env)
    assert proc.returncode == 1
    assert "DRIVE SYNC FAILED" in proc.stdout
    assert len(_local(outdir)) == 4
