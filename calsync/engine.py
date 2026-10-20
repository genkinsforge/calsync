"""The sync engine: read every calendar, compute desired mirrors, reconcile each target."""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import net
from .agentdata import events_from_push
from .config import Config
from .connectors import Connector, MarkerMismatch
from .model import (Event, Window, busy_payload, details_payload, mirror_id, parse_mirror_id,
                    payload_end, payload_hash, qualifies)
from .state import Store


class KillSwitch(Exception):
    pass


@dataclass
class Desired:
    source_cal: str
    source_id: str
    payload: dict
    hash: str


@dataclass
class Action:
    op: str                 # create | update | delete | forget
    mirror_id: str
    target_event_id: str = ""
    desired: Desired | None = None
    reason: str = ""


def plan_target(desired: dict, mappings: dict, listing: list, install_id: str,
                frozen: set, window_start: datetime) -> list:
    """Pure reconciliation for one API-backed target calendar.

    Only events in `listing` that carry a marker belonging to this installation
    can be updated or deleted. Unmarked events are never touched.
    """
    ours = {}
    for ev in listing:
        parsed = parse_mirror_id(ev.marker) if ev.marker else None
        if parsed and parsed[0] == install_id:
            ours[ev.marker] = ev
    actions = []
    for mid, d in desired.items():
        ev, row = ours.get(mid), mappings.get(mid)
        if ev is None:
            actions.append(Action("create", mid, desired=d, reason="new" if row is None else "missing on target"))
        elif row is None or row["hash"] != d.hash or row["target_event_id"] != ev.id:
            actions.append(Action("update", mid, ev.id, d, "changed" if row else "adopt existing"))
    for mid, row in mappings.items():
        if mid in desired:
            continue
        source = parse_mirror_id(mid)[1] if parse_mirror_id(mid) else row["source_cal"]
        if source in frozen:
            continue
        ev = ours.get(mid)
        if ev is None or ev.end <= window_start:
            actions.append(Action("forget", mid, row["target_event_id"], reason="aged out or gone"))
        else:
            actions.append(Action("delete", mid, ev.id, reason="source gone or no longer qualifies"))
    for mid, ev in ours.items():
        if mid in desired or mid in mappings:
            continue
        if parse_mirror_id(mid)[1] in frozen or ev.end <= window_start:
            continue
        actions.append(Action("delete", mid, ev.id, reason="orphan"))
    return actions


class Engine:
    def __init__(self, cfg: Config, store: Store, connectors: dict, now=None, out=None):
        self.cfg, self.store, self.connectors = cfg, store, connectors
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.out = out or sys.stdout
        self.install_id = store.install_id(cfg.data.get("install_id"))

    def say(self, msg: str):
        print(msg, file=self.out, flush=True)

    # ---------------------------------------------------------------- reading
    def read_all(self, window: Window):
        listings, sources, frozen, stale = {}, {}, set(), set()
        for name, c in self.cfg.calendars.items():
            if c["type"] == "agent":
                events, err, is_stale = self._read_agent(name, window)
                if err:
                    frozen.add(name)
                    self.store.set_status(name, read=(False, err), stale=is_stale)
                    self.say(f"[{name}] read: {err}; existing mirrors kept")
                    continue
                if is_stale:
                    stale.add(name)
                    self.say(f"[{name}] agent data is stale; using the last known set")
                self.store.set_status(name, read=(True, f"{len(events)} events from agent"), stale=is_stale)
            else:
                conn: Connector | None = self.connectors.get(name)
                try:
                    if conn is None:
                        raise RuntimeError("no connector configured")
                    events = conn.list_events(window.start, window.end)
                except Exception as e:  # one calendar failing must not stop the others
                    frozen.add(name)
                    msg = f"{type(e).__name__}: {e}"
                    self.store.set_status(name, read=(False, msg[:500]))
                    self.say(f"[{name}] read failed: {msg[:300]}; its mirrors are left as they are")
                    continue
                listings[name] = events
                self.store.set_status(name, read=(True, f"{len(events)} events"), stale=False)
            sources[name] = [e for e in events if window.overlaps(e) and qualifies(e, self.cfg.include_all_day)]
        return listings, sources, frozen, stale

    def _read_agent(self, name: str, window: Window):
        push = self.store.get_push(name)
        if push is None:
            return [], "no push from the work-Mac agent yet", False
        age_min = (self.now().timestamp() - push["pushed_at"]) / 60
        is_stale = age_min > float(self.cfg.data["agent_stale_minutes"])
        if push.get("error"):
            return [], f"agent reported: {push['error']}", is_stale
        return events_from_push(push["events"]), None, is_stale

    # ---------------------------------------------------------------- desired
    def desired_for(self, target: str, sources: dict) -> dict:
        out = {}
        for src, events in sources.items():
            if src == target:
                continue
            for ev in events:
                if target == self.cfg.details_calendar:
                    payload = details_payload(ev, self.cfg.tag(src))
                else:
                    payload = busy_payload(ev)
                mid = mirror_id(self.install_id, src, ev.id, target)
                out[mid] = Desired(src, ev.id, payload, payload_hash(payload))
        return out

    # ---------------------------------------------------------------- run
    def run(self, dry_run: bool = False, only: str | None = None) -> dict:
        if self.cfg.kill_switch.exists():
            raise KillSwitch(f"kill switch present: {self.cfg.kill_switch}")
        if only and only not in self.cfg.calendars:
            raise ValueError(f"--only {only}: unknown calendar")
        now = self.now()
        w = self.cfg.data["window"]
        window = Window.around(now, float(w["days_back"]), float(w["days_ahead"]))
        listings, sources, frozen, stale = self.read_all(window)
        summary = {}
        for target, c in self.cfg.calendars.items():
            if only and target != only:
                continue
            desired = self.desired_for(target, sources)
            try:
                if c["type"] == "agent":
                    summary[target] = self._plan_agent(target, desired, frozen, dry_run)
                elif target not in listings:
                    summary[target] = {"skipped": "listing failed"}
                    if not dry_run:
                        self.store.set_status(target, write=(False, "skipped: could not read target"))
                else:
                    summary[target] = self._sync_api(target, desired, listings[target], frozen, window, dry_run)
            except Exception as e:
                summary[target] = {"error": f"{type(e).__name__}: {e}"}
                self.say(f"[{target}] sync failed: {e}")
                if not dry_run:
                    self.store.set_status(target, write=(False, str(e)[:500]))
        return {"summary": summary, "frozen": sorted(frozen), "stale": sorted(stale)}

    def _describe(self, a: Action) -> str:
        if a.desired:
            p = a.desired.payload
            return f"{p['title']!r} {p['start']} -> {p['end']} (from {a.desired.source_cal}; {a.reason})"
        return f"{a.target_event_id} ({a.reason})"

    def _sync_api(self, target, desired, listing, frozen, window, dry_run):
        actions = plan_target(desired, self.store.mappings(target), listing, self.install_id, frozen, window.start)
        counts = {"create": 0, "update": 0, "delete": 0, "forget": 0, "errors": 0, "unchanged": 0}
        counts["unchanged"] = len(desired) - sum(1 for a in actions if a.op in ("create", "update"))
        conn = self.connectors[target]
        prefix = "DRY-RUN " if dry_run else ""
        for a in actions:
            self.say(f"{prefix}[{target}] {a.op} {self._describe(a)}")
            if dry_run:
                counts[a.op] += 1
                continue
            try:
                if a.op == "create":
                    eid = conn.create(a.desired.payload, a.mirror_id)
                    self._map(target, a.mirror_id, a.desired, eid)
                elif a.op == "update":
                    conn.update(a.target_event_id, a.desired.payload, a.mirror_id)
                    self._map(target, a.mirror_id, a.desired, a.target_event_id)
                elif a.op == "delete":
                    conn.delete(a.target_event_id, a.mirror_id)
                    self.store.drop_mapping(target, a.mirror_id)
                elif a.op == "forget":
                    self.store.drop_mapping(target, a.mirror_id)
                counts[a.op] += 1
            except net.AuthError:
                raise
            except MarkerMismatch as e:
                counts["errors"] += 1
                self.store.drop_mapping(target, a.mirror_id)
                self.say(f"[{target}] refused: {e}")
            except Exception as e:
                counts["errors"] += 1
                self.say(f"[{target}] {a.op} failed: {type(e).__name__}: {str(e)[:300]}")
        if not dry_run:
            ok = counts["errors"] == 0
            self.store.set_status(target, write=(ok, _fmt_counts(counts)))
        self.say(f"{prefix}[{target}] {_fmt_counts(counts)}")
        return counts

    def _map(self, target, mid, d: Desired, event_id):
        end = payload_end(d.payload).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store.put_mapping(target, mid, d.source_cal, d.source_id, event_id, d.hash, end)

    def _plan_agent(self, target, desired, frozen, dry_run):
        old = self.store.get_plan(target)
        old_blocks = {b["mirror_id"]: b for b in (old["blocks"] if old else [])}
        blocks = {}
        for mid, d in desired.items():
            p = d.payload
            blocks[mid] = {"mirror_id": mid, "title": p["title"], "start": p["start"], "end": p["end"],
                           "all_day": p["all_day"], "hash": d.hash}
        for mid, b in old_blocks.items():
            parsed = parse_mirror_id(mid)
            if parsed and parsed[1] in frozen and mid not in blocks:
                blocks[mid] = b
        added = [m for m in blocks if m not in old_blocks]
        changed = [m for m in blocks if m in old_blocks and old_blocks[m]["hash"] != blocks[m]["hash"]]
        removed = [m for m in old_blocks if m not in blocks]
        prefix = "DRY-RUN " if dry_run else ""
        for m in added:
            self.say(f"{prefix}[{target}] agent will create Busy {blocks[m]['start']} -> {blocks[m]['end']}")
        for m in changed:
            self.say(f"{prefix}[{target}] agent will update Busy {blocks[m]['start']} -> {blocks[m]['end']}")
        for m in removed:
            self.say(f"{prefix}[{target}] agent will delete Busy {old_blocks[m]['start']} -> {old_blocks[m]['end']}")
        counts = {"blocks": len(blocks), "create": len(added), "update": len(changed), "delete": len(removed)}
        if not dry_run:
            self.store.save_plan(target, sorted(blocks.values(), key=lambda b: (b["start"], b["mirror_id"])),
                                 frozen=frozen)
            self.store.set_status(target, write=(True, f"plan published: {_fmt_counts(counts)}"))
        self.say(f"{prefix}[{target}] {_fmt_counts(counts)} (applied by the work-Mac agent)")
        return counts


def _fmt_counts(c: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in c.items())
