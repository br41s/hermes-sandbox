"""ops/backup_slim.py: drop regenerable members, recompress the rest with zstd.

The module is a standalone script, so it is loaded from its path. zstd members
need Python 3.14 (`zipfile.ZIP_ZSTANDARD`); older interpreters skip the file.
"""
from __future__ import annotations

import importlib.util
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SLIM_PY = REPO_ROOT / "ops" / "backup_slim.py"

_spec = importlib.util.spec_from_file_location("backup_slim", SLIM_PY)
assert _spec is not None and _spec.loader is not None
backup_slim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_slim)

pytestmark = pytest.mark.skipif(
    not hasattr(zipfile, "ZIP_ZSTANDARD"), reason="zstd zip members need Python 3.14+"
)


def _home(tmp_path: Path) -> Path:
    """A live home with every required file non-empty, plus a 'work' profile."""
    home = tmp_path / "home"
    for prefix in ("", "profiles/work/"):
        for rel in ("state.db", "config.yaml", ".env", "cron/jobs.json"):
            p = home / prefix / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
    return home


def _zip(path: Path, members: dict[str, bytes]) -> None:
    """Stored (uncompressed) source, so the zstd result is provably smaller."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for i, (name, data) in enumerate(members.items()):
            info = zipfile.ZipInfo(name, (2026, 11, 25, 3, i % 60, 0))
            info.external_attr = (stat.S_IFREG | 0o640) << 16
            zf.writestr(info, data)


def _partials(tmp_path: Path) -> list[str]:
    return [p.name for p in tmp_path.iterdir() if p.name.endswith(".slim-partial")]


@pytest.mark.parametrize(("name", "reason"), [
    # caches
    (".npm/a/b.js", "cache"),
    ("config/.next/cache/x", "cache"),
    (".local/share/pnpm/store/x", "cache"),
    ("lib/.local/share/pnpm/store/x", "cache"),
    ("profiles/work/.npm/a.js", "cache"),
    # git checkouts
    ("checkouts/hermes-agent/a.py", "checkout"),
    ("workspace/ghost/src/a.py", "checkout"),
    ("profiles/work/workspace/ghost/a.py", "checkout"),
    # scratch dirs
    ("scratch/a.py", "scratch"),
    ("tmp/a.py", "scratch"),
    ("tmpdl/a.py", "scratch"),
    ("scratch-2026-11-25/a.py", "scratch"),
    (".cron-abc-tmp-x/a.py", "scratch"),
    ("workspace/tmp/a.py", "scratch"),
    ("workspace/tmpdl.d/a.py", "scratch"),
    ("profiles/work/scratch/a.py", "scratch"),
    # finished shorts
    ("workspace/shorts/a.mp4", "video"),
    ("workspace/shorts/A.MP4", "video"),
    ("profiles/work/workspace/shorts/a.mp4", "video"),
    # kept
    ("checkouts/site/.env.local", None),  # untracked secrets: no repo has them
    ("workspace/ghost/certs/tls.pem", None),
    ("tmp/deploy.key", None),
    ("tmp_posts.json", None),
    ("workspace/social/x.json", None),
    ("workspace/growth/a.txt", None),  # no .git on disk
    ("cache/images/a.png", None),
    ("skills/scratch/SKILL.md", None),  # a skill, not a scratch dir
    ("profiles/work/state.db", None),
    ("_external/.npm/a", None),
    ("workspace/shorts/ledger.json", None),
    ("profiles/work/skills/scratch/SKILL.md", None),
])
def test_drop_reason(tmp_path: Path, name: str, reason: str | None) -> None:
    home = tmp_path / "home"
    (home / "workspace" / "ghost").mkdir(parents=True)
    (home / "workspace" / "ghost" / ".git").write_text("gitdir: x\n")  # a file counts
    (home / "profiles" / "work" / "workspace" / "ghost" / ".git").mkdir(parents=True)
    assert backup_slim.drop_reason(name, home) == reason


def test_slim_round_trip(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "workspace" / "ghost" / ".git").mkdir(parents=True)
    members = {
        "state.db": b"db" * 100,
        "config.yaml": b"a: 1\n",
        ".env": b"SECRET=1\n",
        "cron/jobs.json": b"[]",
        "profiles/work/state.db": b"profile db",
        "profiles/work/config.yaml": b"p: 1\n",
        "profiles/work/.env": b"P=1\n",
        "profiles/work/cron/jobs.json": b"[]",
        "logs/agent.log": b"A" * 200_000,
        "skills/scratch/SKILL.md": b"---\nname: s\n---\n",
        "workspace/social/keep.json": b'{"k": 1}',
        "workspace/ghost/src/main.py": b"print(1)\n",
        "workspace/shorts/clip.mp4": b"video-bytes",
        ".npm/cache/blob": b"c" * 1000,
    }
    zip_path = tmp_path / "hermes-backup-20261125-030000.zip"
    _zip(zip_path, members)
    zip_path.chmod(0o640)
    old_size = zip_path.stat().st_size

    summary = backup_slim.slim(zip_path, home)

    kept = ["state.db", "config.yaml", ".env", "cron/jobs.json",
            "profiles/work/state.db", "profiles/work/config.yaml",
            "profiles/work/.env", "profiles/work/cron/jobs.json",
            "logs/agent.log", "skills/scratch/SKILL.md",
            "workspace/social/keep.json"]
    assert summary == (f"slim: {old_size / 1_000_000:.1f} -> "
                       f"{zip_path.stat().st_size / 1_000_000:.1f} MB, "
                       f"kept 11 files, dropped 3")
    assert zip_path.stat().st_size < old_size  # the stored 200 KB blob compresses
    assert zip_path.exists() and not _partials(tmp_path)
    assert stat.S_IMODE(zip_path.stat().st_mode) == 0o640

    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == kept
        for info in infos:
            assert zf.read(info) == members[info.filename]
            assert info.compress_type == zipfile.ZIP_ZSTANDARD
            assert info.external_attr == (stat.S_IFREG | 0o640) << 16
            assert info.date_time == (2026, 11, 25, 3, kept.index(info.filename) % 60, 0)


def test_missing_required_profile_file_leaves_original(tmp_path: Path) -> None:
    home = _home(tmp_path)
    members = {
        "state.db": b"db", "config.yaml": b"a: 1\n", ".env": b"S=1\n",
        "cron/jobs.json": b"[]",
        "profiles/work/config.yaml": b"p: 1\n",
        "profiles/work/.env": b"P=1\n",
        "profiles/work/cron/jobs.json": b"[]",
    }
    zip_path = tmp_path / "hermes-backup-20261125-030000.zip"
    _zip(zip_path, members)
    original = zip_path.read_bytes()

    with pytest.raises(backup_slim.SlimError) as exc:
        backup_slim.slim(zip_path, home)

    assert "profiles/work/state.db" in str(exc.value)
    assert zip_path.read_bytes() == original
    assert not _partials(tmp_path)


def test_empty_required_file_on_disk_is_not_required(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "profiles" / "work" / "state.db").write_bytes(b"")
    members = {
        "state.db": b"db", "config.yaml": b"a: 1\n", ".env": b"S=1\n",
        "cron/jobs.json": b"[]",
        "profiles/work/config.yaml": b"p: 1\n",
        "profiles/work/.env": b"P=1\n",
        "profiles/work/cron/jobs.json": b"[]",
    }
    zip_path = tmp_path / "hermes-backup-20261125-030000.zip"
    _zip(zip_path, members)

    backup_slim.slim(zip_path, home)

    with zipfile.ZipFile(zip_path) as zf:
        assert "profiles/work/state.db" not in zf.namelist()


def test_corrupt_source_is_untouched(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = _home(tmp_path)
    zip_path = tmp_path / "hermes-backup-20261125-030000.zip"
    zip_path.write_bytes(b"not a zip")

    assert backup_slim.main([str(zip_path), str(home)]) == 1

    assert zip_path.read_bytes() == b"not a zip"
    assert capsys.readouterr().err.startswith("backup_slim: ")


def test_crc_mismatch_keeps_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    members = {
        "state.db": b"db", "config.yaml": b"a: 1\n", ".env": b"S=1\n",
        "cron/jobs.json": b"[]",
        "profiles/work/state.db": b"db", "profiles/work/config.yaml": b"p: 1\n",
        "profiles/work/.env": b"P=1\n", "profiles/work/cron/jobs.json": b"[]",
    }
    zip_path = tmp_path / "hermes-backup-20261125-030000.zip"
    _zip(zip_path, members)
    original = zip_path.read_bytes()

    # Copy garbage into the partial: sizes and CRCs then disagree with the source.
    monkeypatch.setattr(shutil, "copyfileobj", lambda src, dst, length=0: dst.write(b"junk"))

    with pytest.raises(backup_slim.SlimError):
        backup_slim.slim(zip_path, home)

    assert zip_path.read_bytes() == original
    assert not _partials(tmp_path)


def test_main_usage_error_is_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert backup_slim.main([]) == 2
    assert backup_slim.main(["only-one"]) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["absent", "empty"])
def test_wiped_home_never_counts_as_a_backup(tmp_path: Path, bad: str) -> None:
    # No state.db on disk either: its absence must not excuse the archive.
    home = tmp_path / "home"
    home.mkdir()
    members = {"config.yaml": b"a", ".env": b"b", "notes.md": b"c"}
    if bad == "empty":
        members["state.db"] = b""
    zip_path = tmp_path / "hermes-backup-20261125-030000.zip"
    _zip(zip_path, members)
    before = zip_path.read_bytes()
    with pytest.raises(backup_slim.SlimError, match="state.db"):
        backup_slim.slim(zip_path, home)
    assert zip_path.read_bytes() == before
    assert _partials(tmp_path) == []
