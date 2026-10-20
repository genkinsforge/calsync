"""Microsoft Graph connector and device-code sign-in (public client, delegated permissions)."""
from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

from . import net, tokens
from .connectors import Connector, MarkerMismatch
from .model import Event, find_meeting_url, fmt_dt, parse_date, parse_dt

GRAPH = "https://graph.microsoft.com/v1.0"
# Fixed namespace GUID for calsync's named string extended property. Never change it:
# existing mirrors are recognised by it.
PROP_ID = "String {b22593c1-1406-4831-82c5-a149e02be708} Name calsync"
CATEGORY = "calsync"
PREFER = 'outlook.timezone="UTC", outlook.body-content-type="text"'
FREE_SHOW_AS = {"free", "workingElsewhere"}


def _authority(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


def authorize(name: str, account: str, tenant: str, client_id: str, scopes: list, tokens_dir: Path,
              sleep=time.sleep) -> Path:
    scope = " ".join(scopes)
    dc = net.request("POST", f"{_authority(tenant)}/devicecode", form={"client_id": client_id, "scope": scope})
    print(dc.get("message") or f"Open {dc['verification_uri']} and enter {dc['user_code']}")
    if account:
        print(f"Sign in as {account}.")
    deadline = time.time() + float(dc.get("expires_in", 900))
    interval = float(dc.get("interval", 5))
    while time.time() < deadline:
        sleep(interval)
        try:
            out = net.request("POST", f"{_authority(tenant)}/token", retries=2, form={
                "client_id": client_id, "device_code": dc["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
        except net.HttpError as e:
            err = _err(e.body)
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            raise net.AuthError(f"device-code sign-in failed: {e.body[:300]}")
        return tokens.save(tokens_dir, name, {
            "provider": "microsoft", "account": account, "tenant": tenant, "client_id": client_id,
            "scope": out.get("scope"), "refresh_token": out["refresh_token"], "obtained_at": time.time(),
        })
    raise net.AuthError("device code expired before sign-in completed")


def _err(body: str) -> str:
    import json
    try:
        return json.loads(body).get("error", "")
    except ValueError:
        return ""


class GraphAuth:
    def __init__(self, name: str, tenant: str, client_id: str, scopes: list, tokens_dir: Path):
        self.name, self.tenant, self.client_id, self.scopes, self.tokens_dir = name, tenant, client_id, scopes, tokens_dir
        self._access, self._exp = None, 0.0

    def token(self) -> str:
        if self._access and time.time() < self._exp - 60:
            return self._access
        saved = tokens.load(self.tokens_dir, self.name)
        if not saved:
            raise net.AuthError(f"{self.name}: not signed in; run `calsync auth microsoft {self.name}`")
        try:
            out = net.request("POST", f"{_authority(self.tenant)}/token", form={
                "client_id": self.client_id, "grant_type": "refresh_token",
                "refresh_token": saved["refresh_token"], "scope": " ".join(self.scopes),
            })
        except net.HttpError as e:
            if e.status in (400, 401):
                raise net.AuthError(f"{self.name}: refresh failed ({_err(e.body)}); run `calsync auth microsoft {self.name}`")
            raise
        if out.get("refresh_token") and out["refresh_token"] != saved["refresh_token"]:
            saved["refresh_token"] = out["refresh_token"]   # Entra rotates refresh tokens
            tokens.save(self.tokens_dir, self.name, saved)
        self._access, self._exp = out["access_token"], time.time() + float(out.get("expires_in", 3600))
        return self._access


def _marker(item: dict):
    for p in item.get("singleValueExtendedProperties", []) or []:
        if p.get("id", "").lower() == PROP_ID.lower():
            return p.get("value")
    return None


def normalize(item: dict) -> Event:
    all_day = bool(item.get("isAllDay"))
    s, e = item["start"]["dateTime"], item["end"]["dateTime"]
    start = parse_date(s) if all_day else parse_dt(s)
    end = parse_date(e) if all_day else parse_dt(e)
    body = (item.get("body") or {}).get("content", "") or ""
    location = (item.get("location") or {}).get("displayName", "") or ""
    online = ((item.get("onlineMeeting") or {}).get("joinUrl") or item.get("onlineMeetingUrl")
              or find_meeting_url(location, body))
    return Event(
        id=item["id"], start=start, end=end, all_day=all_day,
        title=item.get("subject", "") or "", location=location, description=body, online_url=online or "",
        cancelled=bool(item.get("isCancelled")),
        free=item.get("showAs") in FREE_SHOW_AS,
        declined=(item.get("responseStatus") or {}).get("response") == "declined",
        marker=_marker(item), raw=item,
    )


def event_body(payload: dict, marker: str) -> dict:
    if payload["all_day"]:
        start, end = payload["start"] + "T00:00:00", payload["end"] + "T00:00:00"
    else:
        start, end = payload["start"].rstrip("Z"), payload["end"].rstrip("Z")
    return {
        "subject": payload["title"],
        "body": {"contentType": "text", "content": payload["description"]},
        "location": {"displayName": payload["location"]},
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "isAllDay": payload["all_day"],
        "showAs": "busy",
        "sensitivity": "private",
        "isReminderOn": False,
        "responseRequested": False,
        "categories": [CATEGORY],
        "singleValueExtendedProperties": [{"id": PROP_ID, "value": marker}],
    }


class GraphConnector(Connector):
    def __init__(self, name: str, auth: GraphAuth, calendar_id: str = "", request=net.request):
        self.name, self.auth, self._request = name, auth, request
        self.base = f"/me/calendars/{urllib.parse.quote(calendar_id, safe='')}" if calendar_id else "/me/calendar"

    def _call(self, method, path_or_url, body=None):
        url = path_or_url if path_or_url.startswith("https://") else GRAPH + path_or_url
        return self._request(method, url, json_body=body, headers={
            "Authorization": f"Bearer {self.auth.token()}", "Prefer": PREFER})

    def list_events(self, start, end):
        expand = f"singleValueExtendedProperties($filter=id eq '{PROP_ID}')"
        q = urllib.parse.urlencode({
            "startDateTime": fmt_dt(start), "endDateTime": fmt_dt(end), "$top": "200",
            "$select": "id,subject,body,start,end,isAllDay,location,showAs,isCancelled,responseStatus,"
                       "onlineMeeting,onlineMeetingUrl,categories,type",
            "$expand": expand,
        })
        url, out = f"{self.base}/calendarView?{q}", []
        while url:
            data = self._call("GET", url)
            out.extend(normalize(i) for i in data.get("value", []) if i.get("type") != "seriesMaster")
            url = data.get("@odata.nextLink")
        return out

    def _event_path(self, event_id):
        return f"/me/events/{urllib.parse.quote(event_id, safe='')}"

    def _check(self, event_id, marker):
        expand = urllib.parse.quote(f"singleValueExtendedProperties($filter=id eq '{PROP_ID}')")
        item = self._call("GET", f"{self._event_path(event_id)}?$select=id&$expand={expand}")
        if _marker(item) != marker:
            raise MarkerMismatch(f"{self.name}: event {event_id} lacks marker {marker}")

    def create(self, payload, marker):
        return self._call("POST", f"{self.base}/events", event_body(payload, marker))["id"]

    def update(self, event_id, payload, marker):
        self._check(event_id, marker)
        self._call("PATCH", self._event_path(event_id), event_body(payload, marker))

    def delete(self, event_id, marker):
        self._check(event_id, marker)
        self._call("DELETE", self._event_path(event_id))
