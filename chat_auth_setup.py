#!/opt/anaconda3/bin/python3
"""One-time OAuth authorization for reading the Vault Events Bot Google Chat
space. Run this once: it opens a browser for you to approve read-only access
to Chat messages (as whichever Google account is a member of that space),
then saves a refresh token to chat_token.json.

Requires CHAT_CLIENT_ID / CHAT_CLIENT_SECRET in .env (a Desktop-app OAuth
client with the Google Chat API enabled and the chat.messages.readonly scope
added to its consent screen).

Usage:
    python3 chat_auth_setup.py
"""
import http.server
import json
import os
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TOKEN_PATH = ROOT / "chat_token.json"
SCOPE = "https://www.googleapis.com/auth/chat.messages.readonly"
PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/"

auth_code = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorized -- you can close this tab." if auth_code else "No code received."
        self.wfile.write(f"<html><body>{msg}</body></html>".encode())

    def log_message(self, *args):
        pass  # keep stdout clean


def main():
    client_id = os.environ.get("CHAT_CLIENT_ID", "").strip()
    client_secret = os.environ.get("CHAT_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("CHAT_CLIENT_ID / CHAT_CLIENT_SECRET not set in .env")
        return

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    print("Opening browser for authorization. If it doesn't open, visit:\n")
    print(auth_url)
    print()
    webbrowser.open(auth_url)

    server = http.server.HTTPServer(("localhost", PORT), CallbackHandler)
    server.handle_request()  # blocks until the redirect comes back

    if not auth_code:
        print("Authorization failed -- no code received.")
        return

    data = urllib.parse.urlencode({
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        tokens = json.loads(resp.read().decode("utf-8"))

    if "refresh_token" not in tokens:
        print("No refresh_token in the response -- if you've authorized this client "
              "before, revoke prior access at https://myaccount.google.com/permissions "
              "and re-run this script.")
        print(tokens)
        return

    TOKEN_PATH.write_text(json.dumps(tokens, indent=2))
    print(f"Saved refresh token to {TOKEN_PATH}")


if __name__ == "__main__":
    main()
