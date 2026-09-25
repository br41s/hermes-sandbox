#!/usr/bin/env python3
"""Mint the YouTube refresh token for the Shorts publisher. Run once, locally.

    python3 scripts/youtube_oauth.py --client-id ... --client-secret ...

Standard library only; run it on your own machine (it opens a browser and
listens on 127.0.0.1 for Google's redirect). Sign in with the Google account
that owns the BigLobster channel — pick the channel if asked.

It prints YOUTUBE_REFRESH_TOKEN. Store the three values as Zeabur service env
vars (YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN) and
never paste them into a chat or a commit.

The OAuth client must be of type "Desktop app" (Google Cloud Console → APIs &
Services → Credentials), with the YouTube Data API v3 enabled on the project.
While the consent screen is in "Testing", Google expires refresh tokens after
7 days — set the publishing status to "In production" (no review is needed
for a project only you use, the unverified-app screen is expected).
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # thumbnails.set and captions.insert need this broader scope.
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    redirect = f"http://127.0.0.1:{args.port}/"
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400)
                self.end_headers()
                return
            result["code"] = query.get("code", [""])[0]
            result["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Done — you can close this tab and return to the terminal.".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    print("Opening the Google consent screen. If no browser opens, visit:\n" + url + "\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    if not result.get("code"):
        print(f"No authorization code received ({result.get('error') or 'timed out'}).", file=sys.stderr)
        return 1

    body = urllib.parse.urlencode({
        "code": result["code"],
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body)) as resp:
        token = json.load(resp)
    refresh = token.get("refresh_token")
    if not refresh:
        print("Google returned no refresh token. Revoke the app's access at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        return 1
    print("YOUTUBE_REFRESH_TOKEN=" + refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
