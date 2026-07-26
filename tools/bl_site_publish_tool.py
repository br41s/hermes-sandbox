"""Publishing tool for rented agents working on a bl-site-package client site.

bl-site-package (the productized website sold/rented to clients) has no
per-client git repo — every instance shares one codebase, and a client's
actual content (page text, blog posts) lives in that instance's own SQLite
DB, editable only through its JWT-protected panel API. Agents rented out
to a client (Content Gap Hunter, SEO/GEO On-Site, etc.) publish through
this tool instead of the git+PR flow the biglobster.top-native agents use.

Credentials are resolved from the *profile* the cron job runs under
(``BL_SITE_URL`` / ``BL_SITE_PANEL_PASSWORD`` in that profile's .env),
via the same per-profile env resolution every other credential in this
codebase uses (see tools/xai_http.py) — so a client's own profile only
ever holds that client's own site URL and panel password, never another
client's.
"""

import base64
import json
from typing import Optional

import urllib.error
import urllib.request

_jwt_cache: dict[str, str] = {}

# Cap on the image we'll pull from FAL before base64-ing it into the upload
# body. The bl-site endpoint re-encodes to WebP and enforces its own 10 MB
# decoded limit; this is the client-side guard so a surprise huge file doesn't
# get read fully into memory here first.
_MAX_IMAGE_BYTES = 12 * 1024 * 1024


def _download_bytes(url: str) -> bytes:
    """Fetch an image from a URL (e.g. the FAL-hosted result of image_generate).

    Streams up to _MAX_IMAGE_BYTES + 1 so an oversized response is rejected
    without buffering it all. Raises RuntimeError on transport/size errors.
    """
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(_MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} fetching image from {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not fetch image from {url}: {e.reason}") from e
    if not data:
        raise RuntimeError(f"Image URL returned no data: {url}")
    if len(data) > _MAX_IMAGE_BYTES:
        raise RuntimeError("Image exceeds the 12 MB upload limit")
    return data


def _get_site_credentials() -> tuple[Optional[str], Optional[str]]:
    from hermes_cli.config import get_env_value

    url = (get_env_value("BL_SITE_URL") or "").strip().rstrip("/")
    password = (get_env_value("BL_SITE_PANEL_PASSWORD") or "").strip()
    return url or None, password or None


def _http_json(method: str, url: str, body: Optional[dict] = None, token: Optional[str] = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e


def _get_jwt(site_url: str, password: str) -> str:
    if site_url in _jwt_cache:
        return _jwt_cache[site_url]
    result = _http_json("POST", f"{site_url}/api/auth/login", {"password": password})
    token = result.get("token")
    if not token:
        raise RuntimeError(f"Login to {site_url} did not return a token: {result}")
    _jwt_cache[site_url] = token
    return token


def bl_site_publish(
    action: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    excerpt: Optional[str] = None,
    field: Optional[str] = None,
    value: Optional[str] = None,
    cta_url: Optional[str] = None,
    cta_label: Optional[str] = None,
    image_url: Optional[str] = None,
    image_alt: Optional[str] = None,
    image_base64: Optional[str] = None,
) -> str:
    from tools.registry import tool_error

    site_url, password = _get_site_credentials()
    if not site_url or not password:
        return tool_error(
            "BL_SITE_URL and/or BL_SITE_PANEL_PASSWORD are not set for this profile. "
            "This tool only works when run under a client's dedicated profile."
        )

    try:
        token = _get_jwt(site_url, password)

        if action == "upload_image":
            # Store an image on the client's site and get back its public URL,
            # so it can be attached as a blog cover (create_blog_post image_url)
            # or a page image (update_page_text on a page_*_image field).
            #
            # Preferred input is `image_url` — the FAL-hosted URL that
            # image_generate returns. We fetch those bytes and base64-encode
            # them HERE (never dragging the base64 through the agent's context),
            # then POST to the site's base64 upload endpoint, which re-encodes
            # to optimized WebP. `image_base64` is accepted for direct-byte
            # callers.
            b64 = image_base64
            if not b64:
                if not image_url:
                    return tool_error("upload_image requires 'image_url' (or 'image_base64').")
                b64 = base64.b64encode(_download_bytes(image_url)).decode("ascii")
            result = _http_json(
                "POST",
                f"{site_url}/api/site/upload-image",
                {"image_base64": b64},
                token=token,
            )
            if not result.get("url"):
                return tool_error(f"Upload did not return a URL: {result}")
            return json.dumps({"success": True, "url": result.get("url")})

        if action == "create_blog_post":
            if not title or not content:
                return tool_error("create_blog_post requires 'title' and 'content'.")
            payload = {"title": title, "content": content, "excerpt": excerpt or "", "status": "draft"}
            if cta_url:
                payload["cta_url"] = cta_url
                payload["cta_label"] = cta_label or "Ver ficha original"
            if image_url:
                payload["image_url"] = image_url
                if image_alt:
                    payload["image_alt"] = image_alt
            result = _http_json(
                "POST",
                f"{site_url}/api/blog/posts",
                payload,
                token=token,
            )
            return json.dumps({
                "success": True,
                "id": result.get("id"),
                "slug": result.get("slug"),
                "status": "draft",
                "note": "Saved as a draft — the client reviews and publishes it themselves.",
            })

        if action == "update_page_text":
            if not field or value is None:
                return tool_error("update_page_text requires 'field' and 'value'.")
            result = _http_json(
                "POST",
                f"{site_url}/api/site/texts",
                {field: value},
                token=token,
            )
            return json.dumps({"success": bool(result.get("success")), "field": field})

        if action == "list_posts":
            # Authenticated, unlike a plain GET /api/blog/posts — returns
            # drafts too, so agents can dedup against posts they already
            # created but the client hasn't published yet.
            result = _http_json("GET", f"{site_url}/api/blog/posts", token=token)
            posts = result.get("posts", [])
            return json.dumps({
                "success": True,
                "posts": [
                    {
                        "id": p.get("id"),
                        "title": p.get("title"),
                        "slug": p.get("slug"),
                        "status": p.get("status"),
                        "cta_url": p.get("cta_url"),
                        "image_url": p.get("image_url"),
                    }
                    for p in posts
                ],
            })

        return tool_error(
            f"Unknown action '{action}'. Use 'create_blog_post', 'update_page_text', "
            "'list_posts', or 'upload_image'."
        )
    except RuntimeError as e:
        return tool_error(str(e))


BL_SITE_PUBLISH_SCHEMA = {
    "name": "bl_site_publish",
    "description": (
        "Publish content to the bl-site-package client site this profile is dedicated to. "
        "Use action='create_blog_post' to save a new blog article as a draft (the client reviews "
        "and publishes it from their own panel — never assume it's live). "
        "Use action='update_page_text' to directly update one page-text field (e.g. "
        "'page_servicios_desc') — this applies immediately, no draft step, matching how the "
        "client's own built-in agent already edits page text. "
        "Use action='list_posts' to list ALL existing posts (drafts included) — use this to "
        "check what's already been created before writing new ones, so you don't duplicate "
        "a post the client hasn't published yet (a plain unauthenticated GET only returns "
        "published posts and will miss your own prior drafts). "
        "Use action='upload_image' to store an image on the site and get back its public URL. "
        "Pass the 'image_url' that image_generate returned; this tool fetches and uploads it "
        "(the site re-encodes to optimized WebP). Attach the returned URL as a blog cover "
        "(create_blog_post 'image_url' + 'image_alt') or a page image (update_page_text on a "
        "'page_<name>_image' field, with 'page_<name>_image_alt' for its alt text). "
        "Only ever touches the one site configured for this profile (BL_SITE_URL) — never another client's."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create_blog_post", "update_page_text", "list_posts", "upload_image"],
                "description": "Which operation to perform.",
            },
            "title": {"type": "string", "description": "Blog post title. Required for create_blog_post."},
            "content": {"type": "string", "description": "Blog post body text. Required for create_blog_post."},
            "excerpt": {"type": "string", "description": "Optional short excerpt for create_blog_post."},
            "field": {
                "type": "string",
                "description": (
                    "Config field to update for update_page_text, e.g. 'page_index_title', "
                    "'page_servicios_desc', or a page image field like 'page_servicios_image' / "
                    "'page_servicios_image_alt'. See the site's GET /api/site/config for current values."
                ),
            },
            "value": {"type": "string", "description": "New value for update_page_text."},
            "image_url": {
                "type": "string",
                "description": (
                    "For upload_image: the source image URL to store (the URL image_generate "
                    "returned). For create_blog_post: the cover image URL, normally the '/uploads/...' "
                    "URL that a prior upload_image returned."
                ),
            },
            "image_alt": {
                "type": "string",
                "description": "Optional descriptive alt text (Spanish) for the blog cover on create_blog_post.",
            },
            "cta_url": {
                "type": "string",
                "description": (
                    "Optional for create_blog_post: URL for a CTA button rendered at the end of "
                    "the post (e.g. a product's original page on the client's old site)."
                ),
            },
            "cta_label": {
                "type": "string",
                "description": "Optional label for the CTA button. Defaults to 'Ver ficha original' if cta_url is set but this isn't.",
            },
        },
        "required": ["action"],
    },
}

from tools.registry import registry  # noqa: E402

registry.register(
    name="bl_site_publish",
    toolset="bl_site_publish",
    schema=BL_SITE_PUBLISH_SCHEMA,
    handler=lambda args, **kw: bl_site_publish(
        action=args.get("action", ""),
        title=args.get("title"),
        content=args.get("content"),
        excerpt=args.get("excerpt"),
        field=args.get("field"),
        value=args.get("value"),
        cta_url=args.get("cta_url"),
        cta_label=args.get("cta_label"),
        image_url=args.get("image_url"),
        image_alt=args.get("image_alt"),
        image_base64=args.get("image_base64"),
    ),
)
