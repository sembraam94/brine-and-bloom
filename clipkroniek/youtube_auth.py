#!/usr/bin/env python3
"""
Run ONCE locally to mint a durable YouTube refresh token for the auto-poster.

Prereqs (see clipkroniek/CLAUDE.md → YouTube setup):
  - The Google Cloud OAuth consent screen is published to PRODUCTION (not Testing —
    Testing expires sensitive-scope refresh tokens after 7 days).
  - You created a "Desktop app" OAuth client and have its Client ID + Secret.

Usage (PowerShell):
    $env:YT_CLIENT_ID="..."; $env:YT_CLIENT_SECRET="..."; python youtube_auth.py
Usage (bash):
    YT_CLIENT_ID=... YT_CLIENT_SECRET=... python youtube_auth.py

It opens your browser, you approve on the CLIPKRONIEK channel (click through the
"unverified app" warning — that's expected for your own app), and it prints the
refresh token. Store that as the GitHub secret YT_REFRESH_TOKEN.
"""
import os
import sys
import http.server
import socketserver
import urllib.parse
import webbrowser
import requests

CLIENT_ID = os.environ.get("YT_CLIENT_ID")
CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET")
# Three scopes: upload (post videos) + read (videos.list stats) + analytics
# (watch-time/retention/subscribers). The read scopes power measure_youtube in
# analyze.py; if you only need uploading, youtube.upload alone is enough.
SCOPE = (
    "https://www.googleapis.com/auth/youtube.upload "
    "https://www.googleapis.com/auth/youtube.readonly "
    "https://www.googleapis.com/auth/yt-analytics.readonly"
)
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{PORT}"

_result = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _result["code"] = params.get("code", [None])[0]
        _result["error"] = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h2>Done. Close this tab and return to the terminal.</h2>")

    def log_message(self, *a):
        pass


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Set YT_CLIENT_ID and YT_CLIENT_SECRET in your environment first.")

    url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # required to receive a refresh token
        "prompt": "consent",        # force a fresh refresh token
    })
    print("Opening your browser to authorize...\n"
          "If it doesn't open, paste this into your browser:\n" + url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    with socketserver.TCPServer(("127.0.0.1", PORT), _Handler) as httpd:
        httpd.handle_request()      # serve exactly one request (the OAuth redirect)

    if _result.get("error"):
        sys.exit("OAuth error: " + _result["error"])
    code = _result.get("code")
    if not code:
        sys.exit("No authorization code received — try again.")

    r = requests.post(TOKEN_URI, data={
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    refresh = r.json().get("refresh_token")
    if not refresh:
        sys.exit("No refresh_token returned. Make sure the consent screen is in "
                 "PRODUCTION and re-run (it forces prompt=consent already).")
    print("\n=== Store this as the GitHub secret  YT_REFRESH_TOKEN  ===")
    print(refresh)
    print("=========================================================")
    print("(Do not paste it into chat — add it directly in GitHub → Settings → "
          "Secrets and variables → Actions.)")


if __name__ == "__main__":
    main()
