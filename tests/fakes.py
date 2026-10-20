"""In-memory fake calendars and helpers for the tests."""
from __future__ import annotations

import copy
import io
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from calsync.config import Config
from calsync.connectors import Connector, MarkerMismatch
from calsync.engine import Engine
from calsync.model import Event, parse_date, parse_dt
from calsync.state import Store

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def at(hours: float) -> datetime:
    return NOW + timedelta(hours=hours)


class FakeCal(Connector):
    def __init__(self, name):
        self.name = name
        self.events: dict[str, Event] = {}
        self.ops: list = []
        self.fail: Exception | None = None
        self._n = 0

    def add(self, id, start, end, **kw) -> Event:
        ev = Event(id=id, start=start, end=end, **kw)
        self.events[id] = ev
        return ev

    def list_events(self, start, end):
        if self.fail:
            raise self.fail
        return [copy.deepcopy(e) for e in self.events.values() if e.end > start and e.start < end]

    def _from_payload(self, id, payload, marker):
        conv = parse_date if payload["all_day"] else parse_dt
        return Event(id=id, start=conv(payload["start"]), end=conv(payload["end"]), title=payload["title"],
                     all_day=payload["all_day"], location=payload["location"],
                     description=payload["description"], online_url=payload["online_url"], marker=marker,
                     raw={"payload": payload})

    def create(self, payload, marker):
        self._n += 1
        eid = f"{self.name}-m{self._n}"
        self.events[eid] = self._from_payload(eid, payload, marker)
        self.ops.append(("create", eid))
        return eid

    def update(self, event_id, payload, marker):
        ev = self.events.get(event_id)
        if not ev or ev.marker != marker:
            raise MarkerMismatch(event_id)
        self.events[event_id] = self._from_payload(event_id, payload, marker)
        self.ops.append(("update", event_id))

    def delete(self, event_id, marker):
        ev = self.events.get(event_id)
        if not ev or ev.marker != marker:
            raise MarkerMismatch(event_id)
        del self.events[event_id]
        self.ops.append(("delete", event_id))

    def mirrors(self):
        return [e for e in self.events.values() if e.marker]

    def real(self):
        return [e for e in self.events.values() if not e.marker]


def make_config(tmp: Path, **overrides) -> Config:
    data = {
        "details_calendar": "personal",
        "kill_switch_file": str(tmp / "DISABLED"),
        "state_db": str(tmp / "state.db"),
        "agent_tokens_file": str(tmp / "agent-tokens.json"),
        "install_id": "aaaaaaaaaaaa",
        "calendars": {
            "personal": {"type": "google", "tag": "Personal"},
            "gigabug": {"type": "google", "tag": "Gigabug"},
            "labalicious": {"type": "google", "tag": "Labalicious"},
            "peregryn": {"type": "microsoft", "tenant": "t", "client_id": "c", "tag": "Peregryn"},
            "phdata": {"type": "agent", "agent": "worklaptop", "tag": "phData"},
            "kiewit": {"type": "agent", "agent": "worklaptop", "tag": "Kiewit"},
        },
        "agents": {"worklaptop": {"calendars": ["phdata", "kiewit"]}},
    }
    data.update(overrides)
    return Config(data)


class Rig:
    """A full six-calendar setup: four fake API calendars, two agent calendars."""

    def __init__(self, **cfg_overrides):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_config(self.tmp, **cfg_overrides)
        self.store = Store(":memory:")
        self.cals = {n: FakeCal(n) for n in ("personal", "gigabug", "labalicious", "peregryn")}
        self.now = NOW
        self.out = io.StringIO()

    def engine(self):
        return Engine(self.cfg, self.store, self.cals, now=lambda: self.now, out=self.out)

    def sync(self, **kw):
        return self.engine().run(**kw)

    def push(self, cal, events, minutes_ago=0):
        self.store.save_push(cal, "worklaptop", events, at=self.now.timestamp() - minutes_ago * 60)

    def blocks(self, cal):
        plan = self.store.get_plan(cal)
        return plan["blocks"] if plan else []

    def close(self):
        self.store.close()
        self._tmp.cleanup()


def agent_event(id, start, end, **kw):
    d = {"id": id, "title": "Agent meeting", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "all_day": False, "location": "", "notes": "",
         "url": None, "availability": "busy", "status": "confirmed", "self_status": "accepted", "marker": None}
    d.update(kw)
    return d
