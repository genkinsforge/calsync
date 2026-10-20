"""Calendar-neutral event model, qualification rules, mirror ids and payloads."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

MIRROR_RE = re.compile(r"^([0-9a-f]{12})\.([a-z0-9][a-z0-9_-]{0,31})\.([0-9a-f]{24})$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MEETING_RE = re.compile(
    r"https://(?:[\w.-]*teams\.microsoft\.com|[\w.-]*zoom\.us|meet\.google\.com|[\w.-]*webex\.com|[\w.-]*gotomeeting\.com|[\w.-]*chime\.aws)/[^\s<>\"')\]]+",
    re.I,
)
MAX_DESCRIPTION = 8000


@dataclass
class Event:
    """One event instance as read from a calendar.

    start/end are UTC datetimes for timed events. For all-day events they are
    midnight UTC on the start date and the (exclusive) end date.
    """

    id: str
    start: datetime
    end: datetime
    title: str = ""
    all_day: bool = False
    location: str = ""
    description: str = ""
    online_url: str = ""
    cancelled: bool = False
    free: bool = False          # transparent / showAs free / availability free
    declined: bool = False      # the calendar owner declined
    marker: Optional[str] = None  # calsync mirror id if calsync created it
    raw: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass
class Window:
    start: datetime
    end: datetime

    @classmethod
    def around(cls, now: datetime, days_back: float, days_ahead: float) -> "Window":
        return cls(now - timedelta(days=days_back), now + timedelta(days=days_ahead))

    def overlaps(self, ev: Event) -> bool:
        return ev.end > self.start and ev.start < self.end


# ---------------------------------------------------------------- time helpers

def parse_dt(value: str) -> datetime:
    """Parse ISO 8601 date-time (with Z, offset, or naive meaning UTC; any fraction length)."""
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?)(\.\d+)?(.*)$", s)
    if not m:
        raise ValueError(f"bad datetime: {value!r}")
    base, frac, tz = m.groups()
    if frac:
        frac = (frac[1:] + "000000")[:6]
        base = f"{base}.{frac}"
    dt = datetime.fromisoformat(base + (tz or ""))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_date(value: str) -> datetime:
    d = date.fromisoformat(value[:10])
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def find_meeting_url(*texts: str) -> str:
    for t in texts:
        if t:
            m = MEETING_RE.search(t)
            if m:
                return m.group(0)
    return ""


# ---------------------------------------------------------------- rules

def qualifies(ev: Event, include_all_day: bool = False) -> bool:
    """A 'real' event: not ours, not free, not declined, not cancelled; all-day only if enabled."""
    if ev.marker:
        return False
    if ev.cancelled or ev.free or ev.declined:
        return False
    if ev.all_day and not include_all_day:
        return False
    if ev.end < ev.start:
        return False
    return True


def mirror_id(install_id: str, source_cal: str, source_event_id: str, target_cal: str) -> str:
    """Deterministic id for the copy of one source instance on one target calendar.

    Format: <install id>.<source calendar>.<24 hex>. It names the installation
    (for orphan cleanup) and the source calendar (so mirrors of a calendar that
    failed to read are left alone).
    """
    h = hashlib.sha256(f"{source_cal}\x1f{source_event_id}\x1f{target_cal}".encode()).hexdigest()[:24]
    return f"{install_id}.{source_cal}.{h}"


def parse_mirror_id(value: str):
    """Return (install_id, source_cal, hash) or None if not a calsync mirror id."""
    m = MIRROR_RE.match(value or "")
    return m.groups() if m else None


def details_payload(ev: Event, tag: str) -> dict:
    desc_parts = []
    if ev.online_url:
        desc_parts.append(f"Join: {ev.online_url}")
    if ev.description:
        desc_parts.append(ev.description.strip())
    desc = "\n\n".join(desc_parts)[:MAX_DESCRIPTION]
    title = ev.title or "(no title)"
    return {
        "kind": "details",
        "title": f"[{tag}] {title}" if tag else title,
        "start": fmt_date(ev.start) if ev.all_day else fmt_dt(ev.start),
        "end": fmt_date(ev.end) if ev.all_day else fmt_dt(ev.end),
        "all_day": ev.all_day,
        "location": ev.location or "",
        "description": desc,
        "online_url": ev.online_url or "",
    }


def busy_payload(ev: Event) -> dict:
    return {
        "kind": "busy",
        "title": "Busy",
        "start": fmt_date(ev.start) if ev.all_day else fmt_dt(ev.start),
        "end": fmt_date(ev.end) if ev.all_day else fmt_dt(ev.end),
        "all_day": ev.all_day,
        "location": "",
        "description": "",
        "online_url": "",
    }


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def payload_end(payload: dict) -> datetime:
    return parse_date(payload["end"]) if payload["all_day"] else parse_dt(payload["end"])
