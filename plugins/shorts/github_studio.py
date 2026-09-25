"""Talk to the Shorts Studio render farm (a GitHub Actions workflow).

Hermes never renders a Remotion short itself. It dispatches
``.github/workflows/shorts-studio.yml`` with the package inline, later finds
the run by its title (``shorts <request_id>``), and downloads the artifact.

Config (environment of the profile that runs the shorts jobs):

    SHORTS_STUDIO_GITHUB_TOKEN   fine-grained PAT on the studio repo:
                                 Actions read/write, Contents read/write
                                 (write only for avatar clip uploads)
    SHORTS_STUDIO_REPO           default br41s/hermes-sandbox
    SHORTS_STUDIO_REF            default main — the workflow must exist there
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
WORKFLOW = "shorts-studio.yml"
AVATAR_RELEASE_TAG = "shorts-avatars"
TIMEOUT = 60.0
MAX_ARTIFACT_BYTES = 400 * 1024 * 1024


class StudioError(RuntimeError):
    pass


def token() -> str:
    return (os.environ.get("SHORTS_STUDIO_GITHUB_TOKEN") or "").strip()


def repo() -> str:
    return (os.environ.get("SHORTS_STUDIO_REPO") or "br41s/hermes-sandbox").strip()


def ref() -> str:
    return (os.environ.get("SHORTS_STUDIO_REF") or "main").strip()


def configured() -> bool:
    return bool(token())


def _client(timeout: float = TIMEOUT):
    import httpx

    if not token():
        raise StudioError("SHORTS_STUDIO_GITHUB_TOKEN is not set")
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "Authorization": f"Bearer {token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-shorts-studio",
        },
    )


def _check(resp, what: str) -> None:
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        raise StudioError(f"{what}: HTTP {resp.status_code} {body}")


def encode_package(pkg: Dict[str, Any]) -> str:
    raw = json.dumps(pkg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")


def dispatch(request_id: str, pkg: Dict[str, Any]) -> Dict[str, Any]:
    """Start a render. Returns ``{"run_id", "run_url"}`` when GitHub reports them."""
    payload = {
        "ref": ref(),
        "inputs": {"request_id": request_id, "package": encode_package(pkg)},
        "return_run_details": True,
    }
    with _client() as client:
        resp = client.post(f"{API}/repos/{repo()}/actions/workflows/{WORKFLOW}/dispatches", json=payload)
    _check(resp, "dispatch render")
    details: Dict[str, Any] = {}
    if resp.status_code == 200 and resp.content:
        body = resp.json()
        details = {"run_id": body.get("workflow_run_id"), "run_url": body.get("html_url")}
    return details


def find_run(request_id: str) -> Optional[Dict[str, Any]]:
    """The most recent run titled ``shorts <request_id>``, or None."""
    title = f"shorts {request_id}"
    with _client() as client:
        resp = client.get(
            f"{API}/repos/{repo()}/actions/workflows/{WORKFLOW}/runs",
            params={"event": "workflow_dispatch", "per_page": 50},
        )
    _check(resp, "list runs")
    for run in resp.json().get("workflow_runs", []):
        if run.get("display_title") == title:
            return run
    return None


def get_run(run_id: int) -> Dict[str, Any]:
    with _client() as client:
        resp = client.get(f"{API}/repos/{repo()}/actions/runs/{run_id}")
    _check(resp, f"get run {run_id}")
    return resp.json()


def _safe_extract(archive: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for member in archive.infolist():
        target = (dest / member.filename).resolve()
        if dest not in target.parents and target != dest:
            raise StudioError(f"artifact member escapes its folder: {member.filename}")
    archive.extractall(dest)


def download_artifact(run_id: int, name: str, dest: Path) -> Optional[Path]:
    """Extract artifact ``name`` of ``run_id`` into ``dest``. None if absent."""
    with _client(timeout=300) as client:
        resp = client.get(f"{API}/repos/{repo()}/actions/runs/{run_id}/artifacts", params={"name": name})
        _check(resp, "list artifacts")
        artifacts = [a for a in resp.json().get("artifacts", []) if a.get("name") == name and not a.get("expired")]
        if not artifacts:
            return None
        art = artifacts[0]
        if art.get("size_in_bytes", 0) > MAX_ARTIFACT_BYTES:
            raise StudioError(f"artifact {name} is {art['size_in_bytes'] >> 20} MB, over the limit")
        # The download 302s to blob storage; httpx drops Authorization on the
        # cross-origin hop, which is what that storage expects.
        zresp = client.get(art["archive_download_url"])
        _check(zresp, "download artifact")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zresp.content)) as archive:
        _safe_extract(archive, dest)
    return dest


# ---------------------------------------------------------------------------
# Avatar clips — stored as release assets so the render farm can fetch them
# ---------------------------------------------------------------------------

def _avatar_release(client) -> Dict[str, Any]:
    resp = client.get(f"{API}/repos/{repo()}/releases/tags/{AVATAR_RELEASE_TAG}")
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code != 404:
        _check(resp, "find avatar release")
    resp = client.post(f"{API}/repos/{repo()}/releases", json={
        "tag_name": AVATAR_RELEASE_TAG,
        "name": "Shorts avatar clips",
        "body": "Avatar clips (Google Flow) used by the Shorts Studio. Managed by Hermes — do not edit by hand.",
        "prerelease": True,
    })
    _check(resp, "create avatar release")
    return resp.json()


def upload_avatar_clip(path: Path, name: str) -> Dict[str, Any]:
    """Upload a local MP4 as a release asset; returns its API download URL."""
    data = path.read_bytes()
    with _client(timeout=300) as client:
        release = _avatar_release(client)
        for asset in release.get("assets", []):
            if asset.get("name") == name:
                client.delete(f"{API}/repos/{repo()}/releases/assets/{asset['id']}")
        resp = client.post(
            f"{UPLOADS}/repos/{repo()}/releases/{release['id']}/assets",
            params={"name": name},
            content=data,
            headers={"Content-Type": "video/mp4"},
        )
    _check(resp, "upload avatar clip")
    asset = resp.json()
    # The API URL (not browser_download_url) is what works for a private repo
    # with a token and Accept: application/octet-stream — see build.py.
    return {"id": asset["id"], "url": asset["url"], "name": asset["name"], "size": asset.get("size")}


def wait_seconds(started: float, budget: float) -> float:
    return max(0.0, budget - (time.time() - started))
