"""Configuration loading and validation."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .model import NAME_RE

CONFIG_DIR = Path(os.environ.get("CALSYNC_HOME", Path.home() / ".config" / "calsync"))
DEFAULTS = {
    "details_calendar": "personal",
    "window": {"days_back": 1, "days_ahead": 60},
    "include_all_day": False,
    "agent_stale_minutes": 30,
    "kill_switch_file": str(CONFIG_DIR / "DISABLED"),
    "state_db": str(CONFIG_DIR / "state.db"),
    "tokens_dir": str(CONFIG_DIR / "tokens"),
    "agent_tokens_file": str(CONFIG_DIR / "agent-tokens.json"),
    "server": {"host": "127.0.0.1", "port": 8787},
    "google": {
        "client_file": str(CONFIG_DIR / "google-client.json"),
        "scopes": ["https://www.googleapis.com/auth/calendar.events.owned"],
    },
    "microsoft": {
        "scopes": ["https://graph.microsoft.com/Calendars.ReadWrite", "offline_access", "openid",
                   "https://graph.microsoft.com/User.ReadBasic.All"],
    },
    "calendars": {},
    "agents": {},
}
TYPES = ("google", "microsoft", "agent")


class ConfigError(Exception):
    pass


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


class Config:
    def __init__(self, data: dict):
        merged = json.loads(json.dumps(DEFAULTS))
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict) and k not in ("calendars", "agents"):
                merged[k].update(v)
            else:
                merged[k] = v
        self.data = merged
        self.file = None
        self.validate()

    @classmethod
    def load(cls, path=None) -> "Config":
        path = Path(_expand(str(path or CONFIG_DIR / "config.json")))
        if not path.exists():
            raise ConfigError(f"config not found: {path} (copy config.example.json there)")
        cfg = cls(json.loads(path.read_text()))
        cfg.file = path
        return cfg

    def validate(self):
        cals = self.calendars
        if not cals:
            raise ConfigError("no calendars configured")
        for name, c in cals.items():
            if not NAME_RE.match(name):
                raise ConfigError(f"calendar name {name!r} must match [a-z0-9][a-z0-9_-]{{0,31}}")
            if c.get("type") not in TYPES:
                raise ConfigError(f"calendar {name}: type must be one of {TYPES}")
            if c["type"] == "agent" and c.get("agent") not in self.data["agents"]:
                raise ConfigError(f"calendar {name}: agent {c.get('agent')!r} not in 'agents'")
        if self.details_calendar not in cals:
            raise ConfigError(f"details_calendar {self.details_calendar!r} not in calendars")
        for aname, a in self.data["agents"].items():
            for c in a.get("calendars", []):
                if cals.get(c, {}).get("type") != "agent":
                    raise ConfigError(f"agent {aname}: {c} is not an agent calendar")

    # convenience accessors
    @property
    def calendars(self) -> dict:
        return self.data["calendars"]

    @property
    def details_calendar(self) -> str:
        return self.data["details_calendar"]

    @property
    def work_calendars(self) -> list:
        return [n for n in self.calendars if n != self.details_calendar]

    @property
    def include_all_day(self) -> bool:
        return bool(self.data["include_all_day"])

    def tag(self, name: str) -> str:
        return self.calendars[name].get("tag") or name.capitalize()

    def path(self, key: str) -> Path:
        return Path(_expand(self.data[key]))

    @property
    def kill_switch(self) -> Path:
        return self.path("kill_switch_file")

    def agent_calendars(self, agent: str) -> list:
        return list(self.data["agents"].get(agent, {}).get("calendars", []))
