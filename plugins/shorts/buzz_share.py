"""Hand a published short to BigLobster's Buzz workspace, where SocialBot posts it.

The split (shorts/STUDIO.md, "Who publishes where"): Hermes makes every short
and publishes it to YouTube; SocialBot, in Buzz, owns Facebook, Instagram and
X. So once a short is live on YouTube, this posts one message to the shorts
channel: the finished files as attachments and every network's copy, with
SocialBot mentioned. Shadow mode never posts here: SocialBot would publish it.

It goes through Block's ``buzz`` CLI, built into the image (Dockerfile,
``buzz_cli`` stage), the same client the Buzz gateway adapter uses.

Config (service env of the profile that runs the shorts jobs; never INJECTed
into other profiles):

    BUZZ_RELAY_URL         the community relay (wss://biglobster.communities.buzz.xyz)
    BUZZ_PRIVATE_KEY       Hermes's own Buzz identity (nsec), never a bot's from Fizz
    BUZZ_AUTH_TAG          the owner attestation that admits that identity
    SHORTS_BUZZ_CHANNEL    UUID of the channel shorts are handed over in
    SHORTS_BUZZ_MENTION    who is asked to post them (default SocialBot)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.shorts._env import env

TIMEOUT = 600          # three uploads of a few tens of MB each
_CREDENTIALS = ("BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG")


class BuzzError(RuntimeError):
    pass


def cli() -> Optional[str]:
    return shutil.which(env("BUZZ_CLI_PATH") or "buzz")


def channel() -> str:
    return env("SHORTS_BUZZ_CHANNEL")


def configured() -> bool:
    """True when shorts are handed to SocialBot in Buzz — and Meta is then SocialBot's.

    All three credentials, the attestation included: without it the relay
    refuses the post, and a half-configured hand-off would take Facebook and
    Instagram away from Hermes while giving them to nobody.
    """
    return bool(channel() and all(env(name) for name in _CREDENTIALS))


def _child_env() -> Dict[str, str]:
    """Only what the CLI needs: the scoped Buzz credentials, never the whole process env."""
    child = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    if os.environ.get("HOME"):
        child["HOME"] = os.environ["HOME"]
    for name in _CREDENTIALS:
        value = env(name)
        if value:
            child[name] = value
    return child


def strip_jpeg(data: bytes) -> bytes:
    """The JPEG without its APPn and COM segments (JFIF, EXIF, ICC, comments); the image itself untouched."""
    if data[:2] != b"\xff\xd8":
        raise BuzzError("not a JPEG")
    out = bytearray(b"\xff\xd8")
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:                      # SOS: the scan and the EOI follow, kept whole
            out += data[i:]
            return bytes(out)
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:   # standalone markers
            out += data[i:i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            out += data[i:i + 2 + length]
        i += 2 + length
    raise BuzzError("JPEG without a scan")


def canonical(path) -> Path:
    """A copy of ``path`` the relay accepts: no metadata, no extra channels.

    The Buzz relay refuses media that "contains metadata or a non-canonical
    metadata channel" (HTTP 422, found 2026-10-15 when the first live
    hand-off failed: every master.mp4 and cover.jpg the Studio renders carries
    tags). MP4s are rewritten with their streams copied and every tag, chapter
    and encoder stamp dropped; JPEGs lose their APPn and COM segments, which
    is what the relay checks (a PNG is refused whatever is done to it). The
    copy sits beside the original as ``<stem>.buzz<suffix>`` and is reused
    while the original is unchanged.
    """
    src = Path(path)
    suffix = src.suffix.lower()
    dst = src.with_name(f"{src.stem}.buzz{suffix}")
    if dst.exists() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    if suffix in (".jpg", ".jpeg"):
        dst.write_bytes(strip_jpeg(src.read_bytes()))
        return dst
    if suffix in (".mp4", ".m4v", ".mov"):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise BuzzError("ffmpeg is not installed: cannot strip the video's metadata for the relay")
        done = subprocess.run([ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-i", str(src),
                               "-map", "0", "-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1",
                               "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
                               "-movflags", "+faststart", str(dst)],
                              capture_output=True, text=True, errors="replace", timeout=300, check=False)
        if done.returncode != 0 or not dst.exists():
            raise BuzzError(f"could not strip {src.name} for the relay: {(done.stderr or '').strip()[-300:]}")
        return dst
    return src


def message(entry: Dict[str, Any], social: Dict[str, Any], youtube_url: Optional[str],
            attached: List[str]) -> str:
    mention = env("SHORTS_BUZZ_MENTION", "SocialBot").lstrip("@")
    lang = (entry.get("lang") or "").upper()
    lines = [
        f"🎬 Short {lang} listo para redes — {entry.get('title') or ''}",
        f"id: {entry.get('request_id')}",
        f"Artículo: {entry.get('article_url') or ''}",
        f"YouTube: {youtube_url or 'no publicado'}",
        f"Adjuntos: {', '.join(attached)}",
    ]
    if entry.get("synthetic"):
        lines.append("⚠️ Lleva escenas o avatar generados con IA: márcalo como contenido IA donde la red lo pida.")
    lines += [
        "", "— Instagram (Reel; la Story es story.mp4, sin texto) —", social.get("instagram") or "",
        "", "— Facebook —", social.get("facebook") or social.get("instagram") or "",
        "", "— X —", social.get("x") or "",
        "",
        f"@{mention}: publícalo en Facebook, Instagram (Reel con cover.jpg como portada, y Story) "
        "y X con estos textos y tus UTM. Responde a este mensaje con los enlaces.",
    ]
    return "\n".join(lines)


def post(entry: Dict[str, Any], files: Dict[str, str], social: Dict[str, Any],
         youtube_url: Optional[str]) -> Dict[str, Any]:
    """Post the short to the shorts channel. Raises BuzzError with the CLI's reason."""
    binary = cli()
    if not binary:
        raise BuzzError("the buzz CLI is not installed in this image")
    if not configured():
        raise BuzzError("Buzz is not configured (SHORTS_BUZZ_CHANNEL, BUZZ_RELAY_URL, BUZZ_PRIVATE_KEY, BUZZ_AUTH_TAG)")
    order = (("master", "master.mp4"), ("story", "story.mp4"), ("cover", "cover.jpg"))
    paths = [(name, str(canonical(files[key]))) for key, name in order
             if files.get(key) and Path(files[key]).exists()]
    if not paths:
        raise BuzzError("no rendered files to attach")
    args = [binary, "messages", "send", "--channel", channel()]
    for _name, path in paths:
        args += ["--file", path]
    args += ["--content", "-"]
    content = message(entry, social, youtube_url, [name for name, _ in paths])
    try:
        done = subprocess.run(args, input=content, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=TIMEOUT, env=_child_env(), check=False)
    except subprocess.TimeoutExpired as exc:
        raise BuzzError(f"buzz CLI timed out after {TIMEOUT}s") from exc
    if done.returncode != 0:
        tail = " / ".join((done.stderr or done.stdout or "").strip().splitlines()[-4:])
        raise BuzzError(f"buzz messages send failed ({done.returncode}): {tail[:400]}")
    return {"channel": channel(), "attached": [name for name, _ in paths],
            "output": (done.stdout or "").strip()[:300]}
