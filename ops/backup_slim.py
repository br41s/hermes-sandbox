"""Rewrite a `hermes backup` zip smaller, in place and atomically.

Google Drive dropped to 15 GB shared by Gmail, Photos and these zips, so three
copies of a full backup have to fit. Slim drops what the live home can regrow
(package caches, git checkouts, scratch dirs, finished shorts videos) and
recompresses the rest with zstd, which `hermes import` in the Hermes image reads
directly (its Python is 3.14). A Mac's `unzip` cannot: extract with
`python3.14 -m zipfile -e <zip> <dir>` or 7zz.

The original is replaced only after the partial archive re-opens, matches every
member's CRC and size and still holds every required file.
"""
from __future__ import annotations

import contextlib
import functools
import os
import re
import shutil
import stat
import sys
import zipfile
from collections import Counter
from pathlib import Path

REQUIRED = ("state.db", "config.yaml", ".env", "cron/jobs.json")
LEVEL = 6  # 12 took over 28 min on 5 GB of state.db; the cron script limit is 60 min

# A scratch dir name: scratch/tmp/tmpdl (+ suffix) or a leftover .cron-*tmp*.
_SCRATCH = re.compile(r"(?:scratch|tmp|tmpdl)(?:[-_.].*)?|\.cron-.*tmp.*")


class SlimError(Exception):
    """The zip cannot be slimmed safely; the original stays untouched."""


@functools.cache
def _checkout(path: str) -> bool:
    # Once per directory: ~100k members, a stat each would dominate.
    return os.path.exists(os.path.join(path, ".git"))


def drop_reason(name: str, home: Path) -> str | None:
    """Why member `name` is left out of the slim zip, or None to keep it."""
    if name.startswith("_external/"):
        return None
    parts = name.split("/")
    dirs = parts[:-1]  # the file's own basename is never matched
    # rel is dirs relative to the member's own Hermes home (profile or main).
    rel = dirs[2:] if parts[0] == "profiles" and len(parts) >= 3 else dirs
    if any(d in (".npm", ".next") for d in dirs):
        return "cache"
    if any(dirs[i:i + 3] == [".local", "share", "pnpm"] for i in range(len(dirs))):
        return "cache"
    if parts[0] == "checkouts" and dirs:
        return "checkout"
    if rel[:1] == ["workspace"] and len(rel) >= 2:
        # Keep the profile prefix: home/profiles/<p>/workspace/<child>/.git.
        child = dirs[:len(dirs) - len(rel) + 2]
        if _checkout(str(home.joinpath(*child))):
            return "checkout"
    if rel:
        candidate = rel[1] if rel[0] == "workspace" and len(rel) >= 2 else rel[0]
        if _SCRATCH.fullmatch(candidate):
            return "scratch"
    # Finished shorts are already in Drive Flow/ and on YouTube.
    if rel[:2] == ["workspace", "shorts"] and parts[-1].lower().endswith(".mp4"):
        return "video"
    return None


def missing_required(names: set[str], home: Path) -> list[str]:
    """Required files present and non-empty on disk but absent from `names`."""
    homes: list[tuple[str, Path]] = [("", home)]
    profiles = home / "profiles"
    if profiles.is_dir():
        for p in sorted(profiles.iterdir()):
            if p.is_dir():
                homes.append((f"profiles/{p.name}/", p))
    missing: list[str] = []
    for prefix, h in homes:
        for req in REQUIRED:
            f = h / req
            if f.is_file() and f.stat().st_size > 0 and prefix + req not in names:
                missing.append(prefix + req)
    return missing


def _copy_member(src: zipfile.ZipFile, info: zipfile.ZipInfo, dst: zipfile.ZipFile) -> None:
    out = zipfile.ZipInfo(info.filename, info.date_time)
    out.compress_type = zipfile.ZIP_ZSTANDARD
    # ZipFile's own compresslevel is ignored when open() is given a ZipInfo.
    out.compress_level = LEVEL
    out.external_attr = info.external_attr
    out.create_system = info.create_system
    with src.open(info) as fin, dst.open(out, "w", force_zip64=True) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def slim(zip_path: Path, home: Path) -> str:
    """Rewrite `zip_path` in place; returns the one-line summary."""
    partial = zip_path.with_name("." + zip_path.name + ".slim-partial")
    old_size = zip_path.stat().st_size
    try:
        with zipfile.ZipFile(zip_path) as src:
            infos = src.infolist()
            names = [i.filename for i in infos]
            if len(set(names)) != len(names):
                dupes = sorted(n for n, c in Counter(names).items() if c > 1)
                raise SlimError("duplicate member names: " + ", ".join(dupes))
            absent = missing_required(set(names), home)
            if absent:
                raise SlimError("source zip is missing required files: " + ", ".join(absent))
            kept = [i for i in infos if drop_reason(i.filename, home) is None]
            # 0600 from the start: the archive holds every profile's secrets.
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(
                raw, "w", zipfile.ZIP_ZSTANDARD, strict_timestamps=False,
            ) as dst:
                for info in kept:
                    _copy_member(src, info, dst)
            _verify(partial, infos, kept, home)
        os.chmod(partial, stat.S_IMODE(zip_path.stat().st_mode))
        os.replace(partial, zip_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        raise
    new_size = zip_path.stat().st_size
    dropped = sum(
        1 for i in infos
        if not i.filename.endswith("/") and drop_reason(i.filename, home) is not None
    )
    return (f"slim: {old_size / 1_000_000:.1f} -> {new_size / 1_000_000:.1f} MB, "
            f"kept {sum(1 for i in kept if not i.filename.endswith('/'))} files, "
            f"dropped {dropped}")


def _verify(partial: Path, infos: list[zipfile.ZipInfo],
            kept: list[zipfile.ZipInfo], home: Path) -> None:
    """The partial must be exactly the kept members, intact and still complete."""
    by_name = {i.filename: i for i in infos}
    with zipfile.ZipFile(partial) as chk:
        new_names = [i.filename for i in chk.infolist()]
        if new_names != [i.filename for i in kept]:
            raise SlimError("partial archive does not hold the kept members")
        for info in chk.infolist():
            old = by_name[info.filename]
            if (info.CRC, info.file_size) != (old.CRC, old.file_size):
                raise SlimError(f"partial archive corrupt: {info.filename}")
        if chk.testzip() is not None:
            raise SlimError("partial archive failed its CRC test")
    absent = missing_required(set(new_names), home)
    if absent:
        raise SlimError("drop rules removed required files: " + ", ".join(absent))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: backup_slim.py <zip> <home>", file=sys.stderr)
        return 2
    if not hasattr(zipfile, "ZIP_ZSTANDARD"):
        print("backup_slim: this Python has no zipfile.ZIP_ZSTANDARD; needs 3.14+",
              file=sys.stderr)
        return 1
    try:
        print(slim(Path(args[0]), Path(args[1])))
    except (SlimError, OSError, zipfile.BadZipFile) as exc:
        print(f"backup_slim: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
