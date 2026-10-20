"""Google Calendar API v3 connector and the installed-app OAuth flow (PKCE, loopback redirect)."""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path

from . import net, tokens
from .connectors import Connector, MarkerMismatch
from .model import Event, find_meeting_url, fmt_dt, parse_date, parse_dt

API = "https://www.googleapis.com/calendar/v3"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def load_client(client_file: Path) -> dict:
    if not Path(client_file).exists():
        raise net.AuthError(f"Google OAuth client file missing: {client_file} (see README, step 1)")
    data = json.loads(Path(client_file).read_text())
    c = data.get("installed") or data.get("web")
    if not c:
        raise net.AuthError(f"{client_file}: expected a Desktop app client JSON with an 'installed' key")
    return c


# ---------------------------------------------------------------- OAuth

def authorize(name: str, account: str, client_file: Path, tokens_dir: Path, scopes: list,
              port: int = 0, open_browser: bool = True) -> Path:
    """Run the installed-app flow and store the refresh token for calendar `name`."""
    client = load_client(client_file)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in q.items()})
            ok = "code" in q and q.get("state", [""])[0] == state
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"calsync: signed in, you can close this tab." if ok else b"calsync: sign-in failed.")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    params = {
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
    }
    if account:
        params["login_hint"] = account
    url = f"{client.get('auth_uri', AUTH_URI)}?{urllib.parse.urlencode(params)}"
    print(f"Sign in as {account or 'the calendar owner'} at:\n\n  {url}\n")
    print(f"Waiting for the redirect on {redirect} (Ctrl-C to abort).")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    server.timeout = 1
    try:
        while "code" not in result and "error" not in result:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise net.AuthError(f"Google sign-in refused: {result['error']}")
    if result.get("state") != state:
        raise net.AuthError("state mismatch on redirect; aborting")
    tok = net.request("POST", client.get("token_uri", TOKEN_URI), form={
        "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
        "code": result["code"], "code_verifier": verifier, "grant_type": "authorization_code",
        "redirect_uri": redirect,
    })
    if "refresh_token" not in tok:
        raise net.AuthError("Google returned no refresh token; remove calsync's access at "
                            "myaccount.google.com/permissions and run auth again")
    return tokens.save(tokens_dir, name, {
        "provider": "google", "account": account, "scope": tok.get("scope"),
        "refresh_token": tok["refresh_token"], "obtained_at": time.time(),
    })


class GoogleAuth:
    def __init__(self, name: str, client_file: Path, tokens_dir: Path):
        self.name, self.client_file, self.tokens_dir = name, client_file, tokens_dir
        self._access, self._exp = None, 0.0

    def token(self) -> str:
        if self._access and time.time() < self._exp - 60:
            return self._access
        saved = tokens.load(self.tokens_dir, self.name)
        if not saved:
            raise net.AuthError(f"{self.name}: not signed in; run `calsync auth google {self.name}`")
        client = load_client(self.client_file)
        try:
            out = net.request("POST", client.get("token_uri", TOKEN_URI), form={
                "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
                "refresh_token": saved["refresh_token"], "grant_type": "refresh_token",
            })
        except net.HttpError as e:
            if e.status in (400, 401):
                raise net.AuthError(f"{self.name}: refresh failed ({e.body[:120]}); run `calsync auth google {self.name}`")
            raise
        self._access, self._exp = out["access_token"], time.time() + float(out.get("expires_in", 3600))
        return self._access


# ---------------------------------------------------------------- connector

def normalize(item: dict) -> Event:
    s, e = item.get("start", {}), item.get("end", {})
    all_day = "date" in s
    start = parse_date(s["date"]) if all_day else parse_dt(s["dateTime"])
    end = parse_date(e["date"]) if all_day else parse_dt(e["dateTime"])
    me = next((a for a in item.get("attendees", []) if a.get("self")), None)
    online = item.get("hangoutLink") or ""
    for ep in item.get("conferenceData", {}).get("entryPoints", []):
        if not online and ep.get("entryPointType") == "video":
            online = ep.get("uri", "")
    online = online or find_meeting_url(item.get("location", ""), item.get("description", ""))
    return Event(
        id=item["id"], start=start, end=end, all_day=all_day,
        title=item.get("summary", ""), location=item.get("location", ""),
        description=item.get("description", ""), online_url=online,
        cancelled=item.get("status") == "cancelled",
        free=item.get("transparency") == "transparent" or item.get("eventType") == "workingLocation",
        declined=bool(me and me.get("responseStatus") == "declined"),
        marker=item.get("extendedProperties", {}).get("private", {}).get("calsync"),
        raw=item,
    )


def event_body(payload: dict, marker: str) -> dict:
    if payload["all_day"]:
        start, end = {"date": payload["start"]}, {"date": payload["end"]}
    else:
        start, end = {"dateTime": payload["start"], "timeZone": "UTC"}, {"dateTime": payload["end"], "timeZone": "UTC"}
    return {
        "summary": payload["title"],
        "description": payload["description"],
        "location": payload["location"],
        "start": start,
        "end": end,
        "transparency": "opaque",
        "visibility": "private",
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {"private": {"calsync": marker}},
        "guestsCanInviteOthers": False,
        "guestsCanSeeOtherGuests": False,
    }


class GoogleConnector(Connector):
    def __init__(self, name: str, auth: GoogleAuth, calendar_id: str = "primary", request=net.request):
        self.name, self.auth, self.calendar_id, self._request = name, auth, calendar_id, request

    def _url(self, suffix=""):
        return f"{API}/calendars/{urllib.parse.quote(self.calendar_id, safe='')}/events{suffix}"

    def _call(self, method, url, body=None):
        return self._request(method, url, headers={"Authorization": f"Bearer {self.auth.token()}"}, json_body=body)

    def list_events(self, start, end):
        out, page = [], None
        while True:
            q = {"timeMin": fmt_dt(start), "timeMax": fmt_dt(end), "singleEvents": "true",
                 "orderBy": "startTime", "maxResults": "2500"}
            if page:
                q["pageToken"] = page
            data = self._call("GET", self._url("?" + urllib.parse.urlencode(q)))
            out.extend(normalize(i) for i in data.get("items", []))
            page = data.get("nextPageToken")
            if not page:
                return out

    def _check(self, event_id, marker):
        item = self._call("GET", self._url("/" + urllib.parse.quote(event_id, safe="")))
        if item.get("extendedProperties", {}).get("private", {}).get("calsync") != marker:
            raise MarkerMismatch(f"{self.name}: event {event_id} lacks marker {marker}")

    def create(self, payload, marker):
        return self._call("POST", self._url("?sendUpdates=none"), event_body(payload, marker))["id"]

    def update(self, event_id, payload, marker):
        self._check(event_id, marker)
        self._call("PUT", self._url("/" + urllib.parse.quote(event_id, safe="") + "?sendUpdates=none"),
                   event_body(payload, marker))

    def delete(self, event_id, marker):
        self._check(event_id, marker)
        self._call("DELETE", self._url("/" + urllib.parse.quote(event_id, safe="") + "?sendUpdates=none"))
