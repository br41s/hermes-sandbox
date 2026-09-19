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
    """Get image bytes from what image_generate returned — a URL or a local path.

    FAL-backed models return a hosted URL; other providers (e.g. the
    OpenRouter image_gen plugin) download the result themselves and return
    a local file path instead (see plugins/image_gen/openrouter). Handle
    both so the cover-image flow works regardless of which image_gen
    provider a profile is configured for.

    Streams up to _MAX_IMAGE_BYTES + 1 so an oversized response is rejected
    without buffering it all. Raises RuntimeError on transport/size errors.
    """
    if url.startswith("http://") or url.startswith("https://"):
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read(_MAX_IMAGE_BYTES + 1)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code} fetching image from {url}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not fetch image from {url}: {e.reason}") from e
    else:
        try:
            with open(url, "rb") as f:
                data = f.read(_MAX_IMAGE_BYTES + 1)
        except OSError as e:
            raise RuntimeError(f"Could not read local image file {url}: {e}") from e
    if not data:
        raise RuntimeError(f"Image source returned no data: {url}")
    if len(data) > _MAX_IMAGE_BYTES:
        raise RuntimeError("Image exceeds the 12 MB upload limit")
    return data


def _get_site_credentials() -> tuple[Optional[str], Optional[str]]:
    from hermes_cli.config import get_env_value

    url = (get_env_value("BL_SITE_URL") or "").strip().rstrip("/")
    password = (get_env_value("BL_SITE_PANEL_PASSWORD") or "").strip()
    return url or None, password or None


def _get_automation_key() -> Optional[str]:
    """Shared secret this client's site accepts to skip Turnstile on login.

    Optional: a site with no Turnstile configured (or one this profile has
    no key for) never sends the header, and login works exactly as before.
    """
    from hermes_cli.config import get_env_value

    key = (get_env_value("BL_SITE_AUTOMATION_KEY") or "").strip()
    return key or None


def _http_json(
    method: str,
    url: str,
    body: Optional[dict] = None,
    token: Optional[str] = None,
    headers: Optional[dict] = None,
) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e


def _get_jwt(site_url: str, password: str) -> str:
    if site_url in _jwt_cache:
        return _jwt_cache[site_url]
    headers = {}
    automation_key = _get_automation_key()
    if automation_key:
        headers["X-Automation-Key"] = automation_key
    result = _http_json(
        "POST", f"{site_url}/api/auth/login", {"password": password}, headers=headers or None
    )
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
    post_id: Optional[str] = None,
    badges: Optional[str] = None,
    base_hash: Optional[str] = None,
    reason: Optional[str] = None,
    evidence: Optional[str] = None,
    author: Optional[str] = None,
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
            payload = {"title": title, "content": content, "excerpt": excerpt or "", "status": "published"}
            if cta_url:
                payload["cta_url"] = cta_url
                payload["cta_label"] = cta_label or "Ver ficha original"
            if image_url:
                payload["image_url"] = image_url
                if image_alt:
                    payload["image_alt"] = image_alt
            if badges:
                payload["badges"] = badges
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
                "status": "published",
                "note": "Published immediately — live on the blog now.",
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
            # drafts too. Agents never create drafts (create_blog_post only
            # publishes); these are the client's own, written in the panel
            # and not yet published. Agents still have to see them, or they
            # would rewrite a topic the client already has in progress.
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

        if action == "get_post":
            # Full row for ONE post, including `content` — which list_posts
            # deliberately omits to keep its listing small. The infographic
            # agent needs the real body to find its insertion anchor and to
            # check whether it already carries the sentinel.
            if not post_id:
                return tool_error("get_post requires 'post_id' (the id or slug of the post).")
            result = _http_json("GET", f"{site_url}/api/blog/posts/{post_id}", token=token)
            return json.dumps({
                "success": True,
                "id": result.get("id"),
                "title": result.get("title"),
                "slug": result.get("slug"),
                "status": result.get("status"),
                "content": result.get("content"),
                # The fingerprint of the body you are about to read. Pass it
                # back as `base_hash` on update_blog_post or propose_edit and
                # the site refuses the write if someone else changed the post
                # in between, instead of silently discarding their work.
                "content_hash": result.get("content_hash"),
            })

        if action == "update_blog_post":
            # Edits an EXISTING post in place. `status` is never sent: the API
            # COALESCEs omitted fields, so a published post STAYS published
            # (CEO decision — an infographic must never pull a live article
            # down for re-review) and a draft stays a draft.
            if not post_id:
                return tool_error("update_blog_post requires 'post_id'.")
            payload = {}
            for key, val in (
                ("title", title), ("content", content), ("excerpt", excerpt),
                ("cta_url", cta_url), ("cta_label", cta_label),
                ("image_url", image_url), ("image_alt", image_alt),
                ("badges", badges),
            ):
                if val is not None:
                    payload[key] = val
            if not payload:
                return tool_error("update_blog_post needs at least one field to change.")
            # Optional, and only meaningful if you actually read the post this
            # run: it is the `content_hash` get_post gave you. With it, a post
            # someone else edited in the meantime comes back as a refusal you
            # can act on. Without it the write still goes through, exactly as
            # it did before this parameter existed.
            if base_hash:
                payload["base_hash"] = base_hash
            # Stamps the revision this write supersedes, so the client's
            # Blog -> Historial shows "Agente: content-updater" instead of
            # attributing the change to them. A self-declared label, never an
            # identity: every agent on a rented site logs in with the same
            # panel password and the server cannot tell them apart.
            if author:
                payload["author"] = author
            try:
                result = _http_json(
                    "PUT",
                    f"{site_url}/api/blog/posts/{post_id}",
                    payload,
                    token=token,
                )
            except RuntimeError as exc:
                if "HTTP 409" in str(exc):
                    return tool_error(
                        "This post changed after you read it — another agent or the client "
                        "edited it. Your edit was NOT applied and nothing was lost. Call "
                        "get_post again, check your findings still hold against the new body, "
                        "and rewrite the change over that version. Never re-send the same edit "
                        f"with the old base_hash. Server said: {exc}"
                    )
                raise
            return json.dumps({
                "success": bool(result.get("success")),
                "id": result.get("id"),
                "slug": result.get("slug"),
                "status": result.get("status"),
                "fields_changed": sorted(payload.keys()),
            })

        if action == "propose_edit":
            # Submit a rewrite for a human to approve instead of publishing it.
            #
            # This is the only write path on this site that does NOT reach the
            # public page on the agent's own say-so. It exists because some
            # claims are not the agent's to change unattended however good its
            # sources are — a price, a legal threshold, a guarantee period. The
            # proposal sits in the client's panel until someone applies it.
            if not post_id:
                return tool_error("propose_edit requires 'post_id'.")
            if not base_hash:
                return tool_error(
                    "propose_edit requires 'base_hash' — the 'content_hash' from the get_post "
                    "call you read this post with. A proposal waits in a queue, so it has to "
                    "record which version it was written against."
                )
            payload = {"base_hash": base_hash}
            for key, val in (("title", title), ("content", content), ("excerpt", excerpt)):
                if val is not None:
                    payload[key] = val
            if not payload.keys() - {"base_hash"}:
                return tool_error("propose_edit needs at least one of title, content or excerpt.")
            if reason:
                payload["reason"] = reason
            if evidence:
                # The site validates this is JSON and bounds its size; sending
                # it as a string keeps the agent from having to build a nested
                # object in a tool call.
                payload["evidence"] = evidence
            try:
                result = _http_json(
                    "POST",
                    f"{site_url}/api/blog/posts/{post_id}/propose",
                    payload,
                    token=token,
                )
            except RuntimeError as exc:
                if "HTTP 409" in str(exc):
                    return tool_error(
                        "This post changed after you read it, so the proposal was not saved. "
                        "Call get_post again and rewrite it against the current body. "
                        f"Server said: {exc}"
                    )
                if "HTTP 404" in str(exc):
                    return tool_error(
                        "This site does not have the proposal endpoint — it is running a "
                        "bl-site-package older than 1.8.0. Do NOT fall back to "
                        "update_blog_post for a change that needed review; report it to the "
                        f"client as text instead. Server said: {exc}"
                    )
                raise
            return json.dumps({
                "success": True,
                "edit_id": result.get("id"),
                "status": "pending",
                "note": (
                    "Saved as a proposal. It is NOT on the live site and will not appear "
                    "until the client approves it in their panel."
                ),
            })

        return tool_error(
            f"Unknown action '{action}'. Use 'create_blog_post', 'update_blog_post', "
            "'propose_edit', 'update_page_text', 'get_post', 'list_posts', or 'upload_image'."
        )
    except RuntimeError as e:
        return tool_error(str(e))


BL_SITE_PUBLISH_SCHEMA = {
    "name": "bl_site_publish",
    "description": (
        "Publish content to the bl-site-package client site this profile is dedicated to. "
        "Use action='create_blog_post' to publish a new blog article immediately — it goes live "
        "on the client's blog right away, no draft/review step. "
        "Use action='update_page_text' to directly update one page-text field (e.g. "
        "'page_servicios_desc') — this applies immediately, no draft step, matching how the "
        "client's own built-in agent already edits page text. "
        "Use action='list_posts' to list ALL existing posts (drafts included) — use this to "
        "check what's already been created before writing new ones, so you don't duplicate "
        "a post the client hasn't published yet (a plain unauthenticated GET only returns "
        "published posts and will miss your own prior drafts). "
        "Use action='get_post' with 'post_id' to read ONE post in full, including its 'content' "
        "body (list_posts omits the body). "
        "Use action='update_blog_post' with 'post_id' to edit an EXISTING post in place, passing "
        "only the fields you want to change. It never changes publication status: a published post "
        "stays published and a draft stays a draft. "
        "Use action='propose_edit' with 'post_id' and 'base_hash' to submit a rewrite of an "
        "existing post for the CLIENT to approve — it is saved as a pending proposal and does "
        "NOT appear on the live site until a human applies it in the panel. Use this instead of "
        "update_blog_post for any change a client would want to see before it goes public "
        "(prices, legal thresholds, guarantees, anything about their own business you did not "
        "read off their own site). "
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
                "enum": [
                    "create_blog_post", "update_blog_post", "propose_edit",
                    "update_page_text", "get_post", "list_posts", "upload_image",
                ],
                "description": "Which operation to perform.",
            },
            "post_id": {
                "type": "string",
                "description": (
                    "Id or slug of an existing post. Required for get_post, update_blog_post "
                    "and propose_edit. Take it from a prior list_posts call."
                ),
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
            "base_hash": {
                "type": "string",
                "description": (
                    "The 'content_hash' that get_post returned for this post. Required for "
                    "propose_edit; optional but strongly recommended for update_blog_post. It "
                    "proves you are editing the version you actually read: if another agent or "
                    "the client changed the post in between, the write is refused instead of "
                    "silently discarding their change. Never invent or reuse an old one."
                ),
            },
            "author": {
                "type": "string",
                "description": (
                    "Who is making this edit, e.g. 'content-updater'. Recorded against the "
                    "version being replaced and shown to the client in their panel under "
                    "Blog -> Historial. Always send it when editing an existing post: without "
                    "it the client's history attributes your change to them."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "For propose_edit: one short sentence, in the site's language, on why this "
                    "content is now wrong. The client reads this to decide whether to approve."
                ),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "For propose_edit: a JSON array string of the sources behind the change, "
                    "e.g. '[{\"claim\": \"IVA reducido\", \"old\": \"10%\", "
                    "\"new\": \"4%\", \"source_url\": \"https://...\"}]'. One entry per "
                    "claim you changed. Only URLs you actually opened this run."
                ),
            },
            "badges": {
                "type": "string",
                "description": (
                    "Optional for create_blog_post/update_blog_post: 3-4 short topic tags for "
                    "the post, comma-separated (e.g. 'seo, marketing, pymes'), matching the "
                    "post's subject and the client's sector. Rendered as a badge row above the "
                    "post title. No fixed vocabulary — pick whatever tags fit this post."
                ),
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
        post_id=args.get("post_id"),
        badges=args.get("badges"),
        base_hash=args.get("base_hash"),
        reason=args.get("reason"),
        evidence=args.get("evidence"),
        author=args.get("author"),
    ),
)
