"""SQLite state: mirror mappings, agent pushes, agent block plans, per-calendar status."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mapping (
    target_cal TEXT NOT NULL,
    mirror_id TEXT NOT NULL,
    source_cal TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_event_id TEXT NOT NULL,
    hash TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (target_cal, mirror_id)
);
CREATE INDEX IF NOT EXISTS mapping_source ON mapping (source_cal, source_id);
CREATE TABLE IF NOT EXISTS agent_push (
    calendar TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    pushed_at REAL NOT NULL,
    events_json TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS agent_plan (
    calendar TEXT PRIMARY KEY,
    generated_at REAL NOT NULL,
    blocks_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cal_status (
    calendar TEXT PRIMARY KEY,
    read_ok INTEGER,
    read_msg TEXT,
    write_ok INTEGER,
    write_msg TEXT,
    stale INTEGER DEFAULT 0,
    last_ok_at REAL,
    updated_at REAL
);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)

    # ---------------------------------------------------------------- meta
    def install_id(self, override: str | None = None) -> str:
        if override:
            return override
        row = self.db.execute("SELECT value FROM meta WHERE key='install_id'").fetchone()
        if row:
            return row["value"]
        value = secrets.token_hex(6)
        self.db.execute("INSERT INTO meta (key, value) VALUES ('install_id', ?)", (value,))
        self.db.commit()
        return value

    # ---------------------------------------------------------------- mapping
    def mappings(self, target_cal: str) -> dict:
        rows = self.db.execute("SELECT * FROM mapping WHERE target_cal=?", (target_cal,)).fetchall()
        return {r["mirror_id"]: dict(r) for r in rows}

    def put_mapping(self, target_cal, mirror_id, source_cal, source_id, target_event_id, hash_, end_utc):
        self.db.execute(
            "INSERT OR REPLACE INTO mapping VALUES (?,?,?,?,?,?,?,?)",
            (target_cal, mirror_id, source_cal, source_id, target_event_id, hash_, end_utc, time.time()),
        )
        self.db.commit()

    def drop_mapping(self, target_cal, mirror_id):
        self.db.execute("DELETE FROM mapping WHERE target_cal=? AND mirror_id=?", (target_cal, mirror_id))
        self.db.commit()

    def mapping_counts(self) -> dict:
        rows = self.db.execute("SELECT target_cal, COUNT(*) n FROM mapping GROUP BY target_cal").fetchall()
        return {r["target_cal"]: r["n"] for r in rows}

    # ---------------------------------------------------------------- agent
    def save_push(self, calendar, agent, events, error=None, at=None):
        self.db.execute(
            "INSERT OR REPLACE INTO agent_push VALUES (?,?,?,?,?)",
            (calendar, agent, at if at is not None else time.time(), json.dumps(events), error),
        )
        self.db.commit()

    def get_push(self, calendar):
        r = self.db.execute("SELECT * FROM agent_push WHERE calendar=?", (calendar,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["events"] = json.loads(d.pop("events_json"))
        return d

    def save_plan(self, calendar, blocks, at=None):
        self.db.execute(
            "INSERT OR REPLACE INTO agent_plan VALUES (?,?,?)",
            (calendar, at if at is not None else time.time(), json.dumps(blocks)),
        )
        self.db.commit()

    def get_plan(self, calendar):
        r = self.db.execute("SELECT * FROM agent_plan WHERE calendar=?", (calendar,)).fetchone()
        if not r:
            return None
        return {"generated_at": r["generated_at"], "blocks": json.loads(r["blocks_json"])}

    # ---------------------------------------------------------------- status
    def set_status(self, calendar, *, read=None, write=None, stale=None):
        now = time.time()
        cur = self.db.execute("SELECT * FROM cal_status WHERE calendar=?", (calendar,)).fetchone()
        row = dict(cur) if cur else {"calendar": calendar, "read_ok": None, "read_msg": None,
                                     "write_ok": None, "write_msg": None, "stale": 0, "last_ok_at": None}
        if read is not None:
            row["read_ok"], row["read_msg"] = int(read[0]), read[1]
        if write is not None:
            row["write_ok"], row["write_msg"] = int(write[0]), write[1]
        if stale is not None:
            row["stale"] = int(stale)
        if (read is None or read[0]) and (write is None or write[0]):
            row["last_ok_at"] = now
        row["updated_at"] = now
        self.db.execute(
            "INSERT OR REPLACE INTO cal_status VALUES (:calendar,:read_ok,:read_msg,:write_ok,:write_msg,:stale,:last_ok_at,:updated_at)",
            row,
        )
        self.db.commit()

    def statuses(self) -> dict:
        return {r["calendar"]: dict(r) for r in self.db.execute("SELECT * FROM cal_status")}
