"""One-time: get a Google Drive refresh token for ChipHunter (run on your own computer).

    python tools/drive_auth.py CLIENT_ID CLIENT_SECRET

Opens the browser, you approve, and it prints the refresh token to paste into GitHub secrets.
Scope is drive.file: ChipHunter can only see files it created itself (the CV library), nothing else in your Drive.
"""
import http.server
import json
import sys
import urllib.parse
import urllib.request
import webbrowser

cid, secret = sys.argv[1], sys.argv[2]
PORT = 8765
redirect = f"http://localhost:{PORT}"
url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
    "client_id": cid, "redirect_uri": redirect, "response_type": "code",
    "scope": "https://www.googleapis.com/auth/drive.file", "access_type": "offline", "prompt": "consent"})
code = {}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code["c"] = q.get("code", [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>אפשר לסגור את החלון ולחזור לטרמינל ✅</h2>".encode())

    def log_message(self, *a):
        pass


print("Opening browser for Google sign-in...")
webbrowser.open(url)
http.server.HTTPServer(("localhost", PORT), H).handle_request()
data = urllib.parse.urlencode({"code": code["c"], "client_id": cid, "client_secret": secret,
                               "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
tok = json.loads(urllib.request.urlopen("https://oauth2.googleapis.com/token", data).read())
print("\nGDRIVE_REFRESH_TOKEN =", tok.get("refresh_token") or tok)
