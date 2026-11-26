"""ops/backup-full.sh retention: Drive keeps 3 (oldest removed before the upload).

Drive dropped to 15 GB shared with Gmail and Photos, so three slim copies are the
budget. The oldest Drive zip is deleted BEFORE the upload (make_room prunes to
KEEP-1, never KEEP), the newest zip is then uploaded and verified, and only then
are local zips past the newest LOCAL_KEEP deleted. Nothing local is deleted when
the Drive copy does not verify.

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
import filecmp, fnmatch, os, shutil, sys
drive = os.environ["FAKE_DRIVE"]
args = list(sys.argv[1:])
if args[:1] == ["--config"]:
    args = args[2:]
with open(os.environ["FAKE_RCLONE_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")

def opt(name):
    return args[args.index(name) + 1]

cmd = args[0]
if cmd == "copy":
    dest = os.path.join(drive, os.path.basename(args[1]))
    if os.environ.get("FAKE_COPY_FAIL"):
        sys.exit(1)
    if os.path.exists(dest) and not filecmp.cmp(args[1], dest, shallow=False):
        sys.exit(1)  # --immutable: never overwrite a differing file
    shutil.copy2(args[1], dest)
elif cmd == "check":
    name = opt("--include").lstrip("/")
    src, dst = os.path.join(args[1], name), os.path.join(drive, name)
    if os.environ.get("FAKE_CHECK_FAIL"):
        sys.exit(1)
    sys.exit(0 if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False) else 1)
elif cmd == "lsf":
    if os.environ.get("FAKE_LSF_FAIL"):
        sys.exit(1)
    pattern = opt("--include") if "--include" in args else None
    for name in sorted(os.listdir(drive)):
        if pattern is None or fnmatch.fnmatch("/" + name, pattern):
            print(name)
elif cmd == "deletefile":
    if os.environ.get("FAKE_DELETE_FAIL"):
        sys.exit(1)
    os.remove(os.path.join(drive, os.path.basename(args[1])))
'''

FAKE_SLIM = r'''#!PYTHON
import os, sys
sys.exit(1 if os.environ.get("FAKE_SLIM_FAIL") else 0)
'''


def _bash_major() -> int:
    if BASH is None:
        return 0
    out = subprocess.run([BASH, "-c", 'echo "${BASH_VERSINFO[0]}"'],
                         capture_output=True, text=True, check=False).stdout.strip()
    return int(out or 0)


pytestmark = pytest.mark.skipif(_bash_major() < 4, reason="backup-full.sh uses mapfile (bash >= 4)")


def _exe(path: Path, body: str) -> Path:
    path.write_text(body.replace("#!PYTHON", f"#!{sys.executable}"))
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
    slim = _exe(tmp_path / "backup_slim.py", FAKE_SLIM)
    rclone = _exe(tmp_path / "rclone", FAKE_RCLONE)
    conf = tmp_path / "rclone.conf"
    conf.write_text("[hermesdrive]\ntype = drive\n")
    env = {
        **os.environ,
        "BACKUP_HERMES": str(hermes),
        "BACKUP_PYTHON": sys.executable,
        "BACKUP_SLIM": str(slim),
        "BACKUP_HERMES_HOME": str(tmp_path),
        "BACKUP_OUTDIR": str(outdir),
        "BACKUP_RCLONE": str(rclone),
        "BACKUP_RCLONE_CONF": str(conf),
        "FAKE_DRIVE": str(drive),
        "FAKE_RCLONE_LOG": str(tmp_path / "rclone.log"),
        "HERMES_ARGV": str(tmp_path / "hermes.argv"),
    }
    return env, outdir, drive


def _run(env):
    return subprocess.run([BASH, str(BACKUP_FULL)], env=env, capture_output=True, text=True, check=False)


def _local(outdir: Path) -> list[str]:
    return sorted(p.name for p in outdir.glob("hermes-backup-*.zip"))


def _log(tmp_path: Path) -> list[str]:
    return (tmp_path / "rclone.log").read_text().splitlines()


def _new_zip(outdir: Path) -> str:
    return next(n for n in _local(outdir) if n not in OLD)


OLD = {
    "hermes-backup-20261018-130038.zip": b"a",
    "hermes-backup-20261019-130038.zip": b"b",
    "hermes-backup-20261020-130046.zip": b"c",
}


def test_hermes_backup_runs_without_its_own_prune(tmp_path: Path) -> None:
    env, _, _ = _setup(tmp_path, {}, {})
    assert _run(env).returncode == 0
    assert "--keep 0" in Path(env["HERMES_ARGV"]).read_text()


def test_drive_keeps_three_and_the_oldest_goes_before_the_upload(tmp_path: Path) -> None:
    env, outdir, drive = _setup(tmp_path, OLD, OLD)
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == ""  # silent on full success
    new = _new_zip(outdir)
    on_drive = sorted(p.name for p in drive.iterdir())
    assert len(on_drive) == 3
    assert on_drive == ["hermes-backup-20261019-130038.zip",
                        "hermes-backup-20261020-130046.zip", new]
    # Retention: the newest is verified, so local keeps only LOCAL_KEEP.
    assert len(_local(outdir)) == 2
    # Order: the delete precedes the upload, and it is not a trashed delete.
    log = _log(tmp_path)
    delete_at = next(i for i, line in enumerate(log) if line.startswith("deletefile "))
    copy_at = next(i for i, line in enumerate(log) if line.startswith("copy "))
    assert delete_at < copy_at
    assert log[delete_at] == ("deletefile hermesdrive:/HermesBackups/"
                              "hermes-backup-20261018-130038.zip --drive-use-trash=false")


def test_drive_with_seven_old_zips_ends_with_three(tmp_path: Path) -> None:
    seven = {f"hermes-backup-202610{d:02d}-130000.zip": b"x" for d in range(1, 8)}
    env, outdir, drive = _setup(tmp_path, OLD, seven)
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    on_drive = sorted(p.name for p in drive.iterdir())
    assert len(on_drive) == 3
    assert on_drive == ["hermes-backup-20261006-130000.zip",
                        "hermes-backup-20261007-130000.zip", _new_zip(outdir)]


def test_failed_upload_keeps_two_in_drive_and_every_local_zip(tmp_path: Path) -> None:
    env, outdir, drive = _setup(tmp_path, OLD, OLD)
    env["FAKE_COPY_FAIL"] = "1"
    proc = _run(env)
    assert proc.returncode == 1
    assert "DRIVE SYNC FAILED" in proc.stdout
    assert sorted(p.name for p in drive.iterdir()) == [
        "hermes-backup-20261019-130038.zip", "hermes-backup-20261020-130046.zip"]
    assert len(_local(outdir)) == 4  # three old + tonight's, none deleted


def test_three_failed_uploads_never_take_drive_below_two(tmp_path: Path) -> None:
    env, _, drive = _setup(tmp_path, OLD, OLD)
    env["FAKE_COPY_FAIL"] = "1"
    for _ in range(3):
        assert _run(env).returncode == 1
    assert sorted(p.name for p in drive.iterdir()) == [
        "hermes-backup-20261019-130038.zip", "hermes-backup-20261020-130046.zip"]


def test_slim_failure_alerts_and_uploads_nothing(tmp_path: Path) -> None:
    env, outdir, _ = _setup(tmp_path, OLD, OLD)
    env["FAKE_SLIM_FAIL"] = "1"
    proc = _run(env)
    assert proc.returncode == 1
    assert "ALERT: Hermes full backup FAILED at" in proc.stdout
    assert not (tmp_path / "rclone.log").exists()  # Drive is never contacted
    assert _local(outdir) == sorted(OLD)  # tonight's zip removed, old ones intact


def test_unverified_drive_copy_alerts_and_deletes_nothing_local(tmp_path: Path) -> None:
    env, outdir, _drive = _setup(tmp_path, OLD, OLD)
    env["FAKE_CHECK_FAIL"] = "1"
    proc = _run(env)
    assert proc.returncode == 1
    assert "DRIVE SYNC FAILED" in proc.stdout
    assert f"Drive copy not verified; kept locally: {_new_zip(outdir)}" in proc.stdout
    assert len(_local(outdir)) == 4


def test_lsf_failure_alerts_but_still_uploads(tmp_path: Path) -> None:
    env, outdir, drive = _setup(tmp_path, OLD, OLD)
    env["FAKE_LSF_FAIL"] = "1"
    proc = _run(env)
    assert proc.returncode == 1
    assert "DRIVE SYNC FAILED" in proc.stdout
    assert (drive / _new_zip(outdir)).read_bytes() == b"new\n"


def test_oversize_backup_warns(tmp_path: Path) -> None:
    env, _, _ = _setup(tmp_path, OLD, OLD)
    env["BACKUP_WARN_BYTES"] = "1"
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ("WARN: Hermes backup is" in proc.stdout
            and "MB; three copies must fit in Drive (15 GB shared with Gmail and Photos)."
            in proc.stdout)


def test_notes_txt_on_drive_is_never_deleted(tmp_path: Path) -> None:
    env, _outdir, drive = _setup(tmp_path, OLD, {**OLD, "notes.txt": b"keep me"})
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (drive / "notes.txt").read_bytes() == b"keep me"
    assert len([p for p in drive.iterdir() if p.name.startswith("hermes-backup-")]) == 3
