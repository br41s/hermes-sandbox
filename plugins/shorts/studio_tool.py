"""``shorts_studio`` — the one tool the shorts producer and publisher agents use.

Every step that can be decided mechanically is decided here, not in the
prompt: which posts are done, whether a figure is sourced, whether the look
repeats yesterday's, whether a short passed QA, whether publishing is live.
The agent writes the words and picks the angle; the tool keeps it honest.

Actions:

  posts          newest posts from the blog feed with no short yet
  article        the text of one post (what the short may cite)
  ledger         recent shorts, and the palettes/motifs that are now taken
  submit         validate + ground-check a package, then dispatch the render
  status         collect finished renders into the workspace, with QA
  publish        post a ready short to youtube | facebook | instagram_reel | instagram_story
  handoff        the message (with MEDIA: attachments) that hands a short to a human:
                 X is always posted by hand; in shadow mode, everything is
  avatars        the Google Flow avatar clips a package may use
  avatar_upload  add a Flow clip to the avatar library, with the line it speaks
  mark           close a short out (published / skipped) with a note
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_FEEDS = {
    "en": "https://biglobster.top/feed.xml",
    "es": "https://biglobster.top/es/feed.xml",
}
ARTICLE_CHARS = 14000
STATUS_BUDGET = 240.0            # seconds a single status call may spend downloading
PUBLISH_TARGETS = ("youtube", "facebook", "instagram_reel", "instagram_story")
KEEP_DAYS = 21                   # downloaded renders of finished shorts are pruned after this

SHORTS_STUDIO_SCHEMA: Dict[str, Any] = {
    "name": "shorts_studio",
    "description": (
        "BigLobster Shorts Studio: turn blog posts into branded vertical videos and publish them. "
        "posts → article → submit (renders in the cloud, ~5-10 min) → status (collects the "
        "finished video + QA) → publish. The tool keeps the ledger, checks every figure you "
        "use against the article, enforces a fresh palette/motif, and refuses to publish "
        "anything that failed QA or while publishing is in shadow mode."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["posts", "article", "ledger", "submit", "status", "publish",
                         "handoff", "avatars", "avatar_upload", "mark"],
            },
            "lang": {"type": "string", "enum": ["en", "es"],
                     "description": "posts: which blog feed to read. avatars/avatar_upload: the clip's language."},
            "limit": {"type": "integer", "description": "posts/ledger: how many (default 5/10)."},
            "url": {"type": "string", "description": "article: the post URL from `posts`."},
            "package": {
                "type": "object",
                "description": (
                    "submit: the short package — lang, article{url,title,slug}, style{palette,motif}, "
                    "beats[hook…cta], covers, social, music{mood}. The tool rejects it with a list of "
                    "problems when anything is off; fix them all and submit again."
                ),
            },
            "request_id": {"type": "string",
                           "description": "publish/mark/status: the short, as returned by submit."},
            "target": {"type": "string", "enum": list(PUBLISH_TARGETS),
                       "description": "publish: where to."},
            "file_path": {"type": "string",
                          "description": "avatar_upload: the local MP4 you received (inside this profile's home)."},
            "avatar": {"type": "string", "description": "avatar_upload: avatar name, e.g. martin or lucia."},
            "line": {"type": "string",
                     "description": "avatar_upload: exactly what the avatar says in the clip."},
            "state": {"type": "string", "enum": ["published", "skipped"], "description": "mark: final state."},
            "note": {"type": "string", "description": "mark: one line on why."},
        },
        "required": ["action"],
    },
}


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _fail(error: str, **payload: Any) -> str:
    return json.dumps({"success": False, "error": error, **payload}, ensure_ascii=False)


def publish_mode() -> str:
    """``live`` publishes; anything else is shadow mode (render and report only)."""
    return "live" if (os.environ.get("SHORTS_PUBLISH_MODE") or "").strip().lower() == "live" else "shadow"


def _feeds() -> Dict[str, str]:
    raw = (os.environ.get("SHORTS_FEEDS") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except ValueError:
            logger.warning("SHORTS_FEEDS is not valid JSON; using defaults")
    return dict(DEFAULT_FEEDS)


def _http_get(url: str) -> str:
    from tools.url_safety import is_safe_url
    import httpx

    if not is_safe_url(url):
        raise ValueError(f"refusing unsafe URL {url}")
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": "BigLobster-Shorts/1.0"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


def parse_feed(xml_text: str) -> List[Dict[str, str]]:
    """RSS items → ``[{title, url, slug, date}]`` in feed order (newest first)."""
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        path = urlparse(link).path
        slug = re.sub(r"\.html?$", "", path.rstrip("/").rsplit("/", 1)[-1]).lower()
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "url": link,
            "slug": slug,
            "date": (item.findtext("pubDate") or "").strip(),
        })
    return items


# ---------------------------------------------------------------------------
# posts / article / ledger
# ---------------------------------------------------------------------------

def _action_posts(args: Dict[str, Any]) -> str:
    from plugins.shorts import ledger

    lang = (args.get("lang") or "").strip()
    feeds = _feeds()
    if lang not in feeds:
        return _fail(f"lang must be one of {sorted(feeds)}")
    limit = max(1, min(int(args.get("limit") or 5), 20))
    try:
        items = parse_feed(_http_get(feeds[lang]))
    except Exception as exc:
        return _fail(f"could not read the {lang} feed {feeds[lang]}: {exc}")
    data = ledger.read()
    done = ledger.done_urls(data)
    by_slug = {}
    for e in ledger.entries(data):
        by_slug.setdefault(e.get("slug"), []).append(e)
    todo = []
    for item in items:
        if item["url"] in done:
            continue
        twins = [e for e in by_slug.get(item["slug"], []) if e.get("lang") != lang]
        if twins:
            item["twin"] = {"lang": twins[-1]["lang"], "palette": twins[-1].get("palette"),
                            "motif": twins[-1].get("motif"), "state": twins[-1].get("state")}
        todo.append(item)
        if len(todo) >= limit:
            break
    return _ok(lang=lang, feed=feeds[lang], total_in_feed=len(items), without_short=len(todo), posts=todo)


def _action_article(args: Dict[str, Any]) -> str:
    from plugins.shorts.studio.article import fetch_article
    from plugins.shorts.studio.package import figures

    url = (args.get("url") or "").strip()
    try:
        title, text = fetch_article(url)
    except ValueError as exc:
        return _fail(str(exc))
    truncated = len(text) > ARTICLE_CHARS
    return _ok(url=url, title=title, chars=len(text), truncated=truncated,
               figures=sorted(set(figures(text)))[:80], text=text[:ARTICLE_CHARS])


def _action_ledger(args: Dict[str, Any]) -> str:
    from plugins.shorts import ledger

    limit = max(1, min(int(args.get("limit") or 10), 50))
    data = ledger.read()
    recent = ledger.entries(data)[-limit:]
    slim = [{k: e.get(k) for k in ("request_id", "lang", "slug", "state", "palette", "motif",
                                   "created_at", "published", "qa")} for e in recent]
    return _ok(publish_mode=publish_mode(), style_taken=ledger.recent_style(data), shorts=slim)


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

def _request_id(pkg: Dict[str, Any]) -> str:
    stamp = time.strftime("%Y%m%d%H%M", time.gmtime())
    return f"{pkg['lang']}-{pkg['article']['slug'][:60].strip('-')}-{stamp}"


def _action_submit(args: Dict[str, Any]) -> str:
    from plugins.shorts import github_studio, ledger
    from plugins.shorts.studio.article import fetch_article
    from plugins.shorts.studio.package import PackageError, check_grounding, validate_package

    raw = args.get("package")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            return _fail(f"package is not valid JSON: {exc}")
    try:
        pkg = validate_package(raw or {})
    except PackageError as exc:
        return _fail("the package has problems — fix every one and submit again",
                     problems=exc.problems)

    problems: List[str] = []
    data = ledger.read()
    if pkg["article"]["url"] in ledger.done_urls(data):
        return _fail(f"{pkg['article']['url']} already has a short (see ledger)")
    taken = ledger.recent_style(data)
    if pkg["style"]["palette"] in taken["palettes"]:
        problems.append(f"style.palette {pkg['style']['palette']!r} was used by one of the last "
                        f"two shorts ({taken['palettes']}); pick another")
    if pkg["style"]["motif"] in taken["motifs"]:
        problems.append(f"style.motif {pkg['style']['motif']!r} was used by one of the last "
                        f"two shorts ({taken['motifs']}); pick another")

    library = {c["clip_url"]: c for c in ledger.avatars()}
    for i, beat in enumerate(pkg["beats"]):
        if beat["kind"] != "avatar":
            continue
        clip = library.get(beat["clip_url"])
        if clip is None:
            problems.append(f"beats[{i}].clip_url: not in the avatar library (see action 'avatars')")
            continue
        if " ".join(beat["vo"].split()) != clip["line"]:
            problems.append(f"beats[{i}].vo: must be the clip's line word for word: {clip['line']!r}")
        if clip.get("lang") != pkg["lang"]:
            problems.append(f"beats[{i}]: that clip speaks {clip.get('lang')}, the short is {pkg['lang']}")
        if clip.get("avatar") != beat["avatar"]:
            problems.append(f"beats[{i}].avatar: that clip is {clip.get('avatar')!r}")

    try:
        _, article_text = fetch_article(pkg["article"]["url"])
    except ValueError as exc:
        return _fail(f"could not read the article to check the facts: {exc}")
    problems += check_grounding(pkg, article_text)
    if problems:
        return _fail("the package has problems — fix every one and submit again", problems=problems)

    used = ledger.used_assets(data)
    pkg["avoid_broll_ids"] = sorted(set(pkg.get("avoid_broll_ids", []) + used["broll"]))
    pkg["music"]["avoid_ids"] = sorted(set(pkg["music"].get("avoid_ids", []) + used["music"]))
    request_id = _request_id(pkg)
    pkg["request_id"] = request_id

    try:
        details = github_studio.dispatch(request_id, pkg)
    except github_studio.StudioError as exc:
        return _fail(f"could not start the render: {exc}")

    workdir = ledger.shorts_root() / request_id
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "package.json").write_text(json.dumps(pkg, ensure_ascii=False, indent=1), encoding="utf-8")
    ledger.add({
        "request_id": request_id, "lang": pkg["lang"], "slug": pkg["article"]["slug"],
        "article_url": pkg["article"]["url"], "title": pkg["article"]["title"],
        "palette": pkg["style"]["palette"], "motif": pkg["style"]["motif"],
        "avatars": [b["avatar"] for b in pkg["beats"] if b["kind"] == "avatar"],
        "run_id": details.get("run_id"), "run_url": details.get("run_url"), "dir": str(workdir),
    })
    return _ok(request_id=request_id, state="rendering", run_url=details.get("run_url"),
               next="The render takes ~5-10 minutes. It is collected by `status` on the publisher's run; "
                    "do not wait for it here.")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _collect(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Advance one ``rendering`` short. Returns the fields to update."""
    from plugins.shorts import github_studio

    run = None
    if entry.get("run_id"):
        run = github_studio.get_run(int(entry["run_id"]))
    else:
        run = github_studio.find_run(entry["request_id"])
    if run is None:
        age = time.time() - calendar.timegm(time.strptime(entry["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
        if age > 3600:
            return {"state": "render_failed", "note": "no render run found an hour after submit"}
        return {}
    fields: Dict[str, Any] = {"run_id": run["id"], "run_url": run.get("html_url")}
    if run.get("status") != "completed":
        return fields
    out = Path(entry["dir"]) / "out"
    if out.exists():
        shutil.rmtree(out)
    got = github_studio.download_artifact(run["id"], f"short-{entry['request_id']}", out)
    manifest_path = out / "manifest.json"
    if not got or not manifest_path.exists():
        fields.update(state="render_failed", note=f"render {run.get('conclusion')}: no deliverables; see {run.get('html_url')}")
        return fields
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    qa_report = manifest.get("qa") or {}
    fields.update(
        state="ready" if qa_report.get("passed") else "qa_failed",
        qa={"passed": bool(qa_report.get("passed")), "errors": qa_report.get("errors", []),
            "warnings": qa_report.get("warnings", [])},
        duration_s=manifest.get("duration_s"),
        music_id=(manifest.get("music") or {}).get("id"),
        broll_ids=[b.get("id") for b in manifest.get("broll") or []],
    )
    return fields


def _files(entry: Dict[str, Any]) -> Dict[str, str]:
    out = Path(entry["dir"]) / "out"
    names = {"master": "master.mp4", "story": "story.mp4", "cover": "cover.jpg",
             "thumb": "thumb.jpg", "srt": "captions.srt"}
    return {k: str(out / v) for k, v in names.items() if (out / v).exists()}


def _prune() -> None:
    """Drop downloaded renders of shorts that finished more than KEEP_DAYS ago."""
    from plugins.shorts import ledger

    cutoff = time.time() - KEEP_DAYS * 86400
    for e in ledger.entries():
        if e.get("state") not in ("published", "skipped", "qa_failed", "render_failed"):
            continue
        try:
            updated = calendar.timegm(time.strptime(e.get("updated_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        out = Path(e.get("dir", "")) / "out"
        if updated < cutoff and out.exists():
            shutil.rmtree(out, ignore_errors=True)


def _action_status(args: Dict[str, Any]) -> str:
    from plugins.shorts import github_studio, ledger

    started = time.time()
    only = (args.get("request_id") or "").strip()
    report = []
    for entry in ledger.entries():
        if only and entry["request_id"] != only:
            continue
        if entry.get("state") == "rendering" and github_studio.wait_seconds(started, STATUS_BUDGET) > 0:
            try:
                fields = _collect(entry)
                if fields:
                    entry = ledger.update(entry["request_id"], **fields)
            except Exception as exc:
                logger.warning("shorts status %s: %s", entry["request_id"], exc)
                entry = {**entry, "collect_error": str(exc)}
        failed = entry.get("state") in ("qa_failed", "render_failed")
        if failed and entry.get("failure_reported") and not only:
            continue  # a failure is reported once, not on every run
        if entry.get("state") in ("rendering", "ready", "qa_failed", "render_failed") or only:
            if failed and not entry.get("failure_reported"):
                entry = ledger.update(entry["request_id"], failure_reported=True)
            item = {k: entry.get(k) for k in ("request_id", "lang", "slug", "title", "article_url",
                                              "state", "qa", "duration_s", "run_url", "published",
                                              "collect_error", "notes")}
            if entry.get("state") in ("ready", "qa_failed"):
                item["files"] = _files(entry)
                pkg_path = Path(entry["dir"]) / "package.json"
                if pkg_path.exists():
                    item["social"] = json.loads(pkg_path.read_text(encoding="utf-8")).get("social")
            report.append(item)
    try:
        _prune()
    except Exception as exc:  # housekeeping never fails the call
        logger.debug("shorts prune: %s", exc)
    return _ok(publish_mode=publish_mode(), shorts=report)


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------

def _enabled_targets() -> List[str]:
    from plugins.shorts.social import meta, youtube

    targets = []
    if youtube.configured():
        targets.append("youtube")
    if meta.configured():
        targets.append("facebook")
    if meta.configured(instagram=True):
        targets += ["instagram_reel", "instagram_story"]
    return targets


def _action_publish(args: Dict[str, Any]) -> str:
    from plugins.shorts import ledger
    from plugins.shorts.social import meta, youtube

    request_id = (args.get("request_id") or "").strip()
    target = (args.get("target") or "").strip()
    if target not in PUBLISH_TARGETS:
        return _fail(f"target must be one of {list(PUBLISH_TARGETS)}")
    if publish_mode() != "live":
        return _fail("publishing is in SHADOW mode (SHORTS_PUBLISH_MODE is not 'live'): render and "
                     "report only. Hand the files to the user instead.", shadow=True)
    try:
        entry = ledger.get(request_id)
    except KeyError as exc:
        return _fail(str(exc))
    if entry.get("state") != "ready" and not (entry.get("state") == "published"):
        return _fail(f"{request_id} is {entry.get('state')!r}; only a short that passed QA can be published")
    done = (entry.get("published") or {}).get(target) or {}
    if done.get("id"):
        return _ok(already=True, target=target, **{k: done.get(k) for k in ("id", "url")})
    if target not in _enabled_targets():
        return _fail(f"{target} is not configured on this profile (see shorts/STUDIO.md)")

    files = {k: Path(v) for k, v in _files(entry).items()}
    if "master" not in files:
        return _fail(f"the rendered files for {request_id} are missing; run status first")
    pkg = json.loads((Path(entry["dir"]) / "package.json").read_text(encoding="utf-8"))
    social = pkg.get("social") or {}
    synthetic = bool(entry.get("avatars"))

    try:
        if target == "youtube":
            result = youtube.upload(files["master"], files.get("thumb"), files.get("srt"), social,
                                    entry["lang"], synthetic=synthetic)
        elif target == "facebook":
            result = meta.facebook_reel(files["master"], social.get("facebook") or social.get("instagram") or "")
        elif target == "instagram_reel":
            result = meta.instagram(files["master"], kind="reel", caption=social.get("instagram") or "",
                                    cover=files.get("cover"), pending_container=done.get("pending"))
        else:
            result = meta.instagram(files["story"], kind="story", pending_container=done.get("pending"))
    except (youtube.YouTubeError, meta.MetaError) as exc:
        ledger.update(request_id, note=f"{target} failed: {exc}")
        return _fail(f"{target}: {exc}")

    entry = ledger.record_publish(request_id, target, result)
    if result.get("pending"):
        return _ok(target=target, pending=True,
                   message="Instagram is still processing the video; publish again later to finish it.")
    enabled = _enabled_targets()
    if all((entry.get("published") or {}).get(t, {}).get("id") for t in enabled):
        ledger.update(request_id, state="published")
    return _ok(target=target, **{k: v for k, v in result.items() if k != "pending"})


# ---------------------------------------------------------------------------
# avatar_upload / mark
# ---------------------------------------------------------------------------

def _action_avatar_upload(args: Dict[str, Any]) -> str:
    from hermes_constants import get_hermes_home
    from plugins.shorts import github_studio
    from plugins.shorts.studio import media
    from tools.path_security import validate_within_dir

    from plugins.shorts import ledger

    raw = (args.get("file_path") or "").strip()
    avatar = (args.get("avatar") or "").strip().lower()
    lang = (args.get("lang") or "").strip()
    line = " ".join((args.get("line") or "").split())
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", avatar):
        return _fail("avatar: give the avatar's name, e.g. 'martin' or 'lucia'")
    if lang not in ("en", "es"):
        return _fail("lang: the language the avatar speaks in the clip (en or es)")
    if not line:
        return _fail("line: write exactly what the avatar says in the clip — it becomes the captions")
    path = Path(raw).expanduser()
    if not raw or validate_within_dir(path, Path(get_hermes_home())) is not None:
        return _fail("file_path must be a file inside this profile's home (e.g. a clip received on Telegram)")
    if not path.is_file():
        return _fail(f"{raw} does not exist")
    try:
        info = media.probe(path)
    except Exception as exc:
        return _fail(f"not a readable video: {exc}")
    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        return _fail("the file has no video")
    if not any(s.get("codec_type") == "audio" for s in streams):
        return _fail("the avatar clip has no audio — the avatar must speak its line in the clip")
    seconds = float(info.get("format", {}).get("duration") or 0)
    if not 1.0 <= seconds <= 15.0:
        return _fail(f"avatar clips must be 1-15 seconds, this one is {seconds:.1f}s")
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    try:
        asset = github_studio.upload_avatar_clip(path, f"{avatar}-{digest}.mp4")
    except github_studio.StudioError as exc:
        return _fail(f"upload failed: {exc}")
    clip = ledger.add_avatar({"avatar": avatar, "lang": lang, "line": line,
                              "clip_url": asset["url"], "seconds": round(seconds, 2)})
    return _ok(**clip, use="Use it as an avatar beat: clip_url as given, vo = the line, word for word.")


def _action_avatars(args: Dict[str, Any]) -> str:
    from plugins.shorts import ledger

    lang = (args.get("lang") or "").strip()
    clips = [c for c in ledger.avatars() if not lang or c.get("lang") == lang]
    return _ok(lang=lang or "all", clips=clips,
               note="An avatar beat must use one of these clip_urls with its line as the vo." if clips
               else "No avatar clips yet — make the short without an avatar beat.")


def _action_handoff(args: Dict[str, Any]) -> str:
    """Build the human hand-off message for a finished short.

    Live mode: the X post (X has no free API), after the automatic targets.
    Shadow mode: everything — files and every network's copy — and the short
    moves to ``handed_off`` so it is not reported again.
    Paste ``message`` into your final response as-is: its MEDIA: lines are
    what attach the files on Telegram.
    """
    from plugins.shorts import ledger

    request_id = (args.get("request_id") or "").strip()
    try:
        entry = ledger.get(request_id)
    except KeyError as exc:
        return _fail(str(exc))
    if entry.get("state") not in ("ready", "published"):
        return _fail(f"{request_id} is {entry.get('state')!r}; only a finished short can be handed off")
    if entry.get("x_handoff_at"):
        return _ok(already=True, message="")
    files = _files(entry)
    if "master" not in files:
        return _fail("rendered files missing; run status first")
    pkg = json.loads((Path(entry["dir"]) / "package.json").read_text(encoding="utf-8"))
    social = pkg.get("social") or {}
    lang = entry.get("lang", "").upper()
    lines: List[str] = []
    if publish_mode() == "live":
        pub = entry.get("published") or {}
        lines.append(f"🎬 Short {lang} publicado — {entry.get('title')}")
        for key, label in (("youtube", "YouTube"), ("instagram_reel", "Instagram"),
                           ("facebook", "Facebook"), ("instagram_story", "Story")):
            if (pub.get(key) or {}).get("url") or (pub.get(key) or {}).get("id"):
                lines.append(f"• {label}: {pub[key].get('url') or pub[key].get('id')}")
        lines += ["", "X — publícalo tú (vídeo adjunto):", social.get("x") or "", f"MEDIA:{files['master']}"]
        ledger.update(request_id, x_handoff_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    else:
        qa = entry.get("qa") or {}
        yt = social.get("youtube") or {}
        lines += [
            f"🎬 [SHADOW] Short {lang} — {entry.get('title')} ({entry.get('duration_s')}s, "
            f"QA {'✅' if qa.get('passed') else '❌'})",
            f"Artículo: {entry.get('article_url')}",
            "", f"YouTube — {yt.get('title', '')}", yt.get("description", ""),
            "", "Instagram:", social.get("instagram") or "",
            "", "Facebook:", social.get("facebook") or "",
            "", "X:", social.get("x") or "",
            f"MEDIA:{files['master']}",
        ]
        if files.get("cover"):
            lines.append(f"MEDIA:{files['cover']}")
        ledger.update(request_id, state="handed_off",
                      x_handoff_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      note="shadow mode: handed to a human")
    return _ok(request_id=request_id, message="\n".join(lines))


def _action_mark(args: Dict[str, Any]) -> str:
    from plugins.shorts import ledger

    request_id = (args.get("request_id") or "").strip()
    state = (args.get("state") or "").strip()
    if state not in ("published", "skipped"):
        return _fail("state must be 'published' or 'skipped'")
    try:
        entry = ledger.update(request_id, state=state, note=(args.get("note") or "").strip()[:300] or None)
    except (KeyError, ValueError) as exc:
        return _fail(str(exc))
    return _ok(request_id=request_id, state=entry["state"])


_ACTIONS = {
    "posts": _action_posts,
    "article": _action_article,
    "ledger": _action_ledger,
    "submit": _action_submit,
    "status": _action_status,
    "publish": _action_publish,
    "handoff": _action_handoff,
    "avatars": _action_avatars,
    "avatar_upload": _action_avatar_upload,
    "mark": _action_mark,
}


def handle_shorts_studio(args: Dict[str, Any], **_kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    handler = _ACTIONS.get(action)
    if handler is None:
        return _fail(f"unknown action {action!r}; one of {sorted(_ACTIONS)}")
    try:
        return handler(args)
    except Exception as exc:  # pragma: no cover - defensive: tools return JSON, never raise
        logger.error("shorts_studio %s failed: %s", action, exc, exc_info=True)
        return _fail(f"unexpected failure in {action}: {exc}")


def check_studio_available() -> bool:
    """Visible only where the studio is configured — invisible to rented tenants."""
    from plugins.shorts import github_studio

    return github_studio.configured()
