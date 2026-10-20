"""Validation and normalisation of events pushed by the work-Mac (EventKit) agent."""
from __future__ import annotations

from .model import Event, find_meeting_url, parse_date, parse_dt

MAX_EVENTS = 5000
_STR_FIELDS = ("id", "title", "start", "end", "location", "notes", "url", "availability", "status",
               "self_status", "marker")


class PushError(ValueError):
    pass


def validate_events(events) -> list:
    if not isinstance(events, list):
        raise PushError("events must be a list")
    if len(events) > MAX_EVENTS:
        raise PushError(f"too many events ({len(events)} > {MAX_EVENTS})")
    clean = []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            raise PushError(f"event {i} is not an object")
        for k in ("id", "start", "end"):
            if not isinstance(e.get(k), str) or not e[k]:
                raise PushError(f"event {i}: missing {k}")
        out = {k: (str(e[k])[:20000] if e.get(k) is not None else None) for k in _STR_FIELDS}
        out["all_day"] = bool(e.get("all_day"))
        try:
            _times(out)
        except ValueError as ex:
            raise PushError(f"event {i}: {ex}")
        clean.append(out)
    return clean


def _times(e: dict):
    if e["all_day"]:
        return parse_date(e["start"]), parse_date(e["end"])
    return parse_dt(e["start"]), parse_dt(e["end"])


def events_from_push(events: list) -> list:
    out = []
    for e in events:
        start, end = _times(e)
        url = e.get("url") or ""
        notes = e.get("notes") or ""
        online = url if url.startswith("https://") else ""
        online = online or find_meeting_url(e.get("location") or "", notes)
        out.append(Event(
            id=e["id"], start=start, end=end, all_day=bool(e.get("all_day")),
            title=e.get("title") or "", location=e.get("location") or "", description=notes,
            online_url=online,
            cancelled=e.get("status") == "canceled",
            free=e.get("availability") == "free",
            declined=e.get("self_status") == "declined",
            marker=e.get("marker") or (url[len("calsync://"):] if url.startswith("calsync://") else None),
        ))
    return out
