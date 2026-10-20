"""Hub API for the work-Mac agent. Plain HTTP on 127.0.0.1; a Cloudflare Tunnel fronts it with TLS.

  POST /agent/events            {"calendars": {"phdata": {"events": [...]}, "kiewit": {"error": "..."}}}
  GET  /agent/blocks?calendar=X  desired Busy blocks for X, each with its mirror id and hash
  GET  /healthz                  liveness, no auth
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agentdata import PushError, validate_events
from .config import Config
from .model import Window, fmt_dt
from .state import Store

MAX_BODY = 8 * 1024 * 1024


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_agent_tokens(path: Path) -> dict:
    return json.loads(Path(path).read_text()).get("agents", {}) if Path(path).exists() else {}


def new_agent_token(path: Path, agent: str) -> str:
    """Create (or rotate) the bearer token for `agent`; only its SHA-256 is stored."""
    path = Path(path)
    data = json.loads(path.read_text()) if path.exists() else {"agents": {}}
    token = secrets.token_urlsafe(32)
    data["agents"][agent] = {"sha256": _hash(token), "created": time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(path.with_suffix(".tmp"), path)
    os.chmod(path, 0o600)
    return token


def authenticate(header: str, agents: dict):
    """Return the agent name for a valid 'Bearer <token>' header, else None. Constant-time per agent."""
    if not header or not header.startswith("Bearer "):
        return None
    presented = _hash(header[len("Bearer "):].strip())
    match = None
    for name, rec in agents.items():
        if hmac.compare_digest(presented, rec.get("sha256", "")):
            match = name
    return match


def make_server(cfg: Config, store: Store, host=None, port=None, tokens_file=None, now=None):
    lock = threading.Lock()
    tokens_path = Path(tokens_file or cfg.path("agent_tokens_file"))
    install_id = store.install_id(cfg.data.get("install_id"))
    now = now or (lambda: datetime.now(timezone.utc))

    class Handler(BaseHTTPRequestHandler):
        server_version = "calsync"
        sys_version = ""

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _agent(self):
            agent = authenticate(self.headers.get("Authorization", ""), load_agent_tokens(tokens_path))
            if not agent:
                self._send(401, {"error": "unauthorized"})
            return agent

        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/healthz":
                return self._send(200, {"ok": True})
            if u.path != "/agent/blocks":
                return self._send(404, {"error": "not found"})
            agent = self._agent()
            if not agent:
                return
            cal = urllib.parse.parse_qs(u.query).get("calendar", [""])[0]
            if cal not in cfg.agent_calendars(agent):
                return self._send(403, {"error": f"calendar {cal!r} is not assigned to this agent"})
            if cfg.kill_switch.exists():
                return self._send(503, {"error": "calsync is disabled (kill switch)"})
            with lock:
                plan = store.get_plan(cal)
            if plan is None:
                return self._send(409, {"error": "no plan yet; the hub has not completed a sync"})
            w = cfg.data["window"]
            win = Window.around(now(), float(w["days_back"]), float(w["days_ahead"]))
            self._send(200, {
                "calendar": cal, "install_id": install_id, "generated_at": plan["generated_at"],
                "window": {"start": fmt_dt(win.start), "end": fmt_dt(win.end)},
                "blocks": plan["blocks"],
                # Sources the hub could not read this pass; the agent must not delete their blocks.
                "frozen_sources": plan["frozen_sources"],
            })

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/agent/events":
                return self._send(404, {"error": "not found"})
            agent = self._agent()
            if not agent:
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                return self._send(413 if length > MAX_BODY else 400, {"error": "bad body length"})
            try:
                body = json.loads(self.rfile.read(length))
                cals = body["calendars"]
                if not isinstance(cals, dict) or not cals:
                    raise PushError("calendars must be a non-empty object")
                allowed = cfg.agent_calendars(agent)
                parsed = {}
                for cal, data in cals.items():
                    if cal not in allowed:
                        return self._send(403, {"error": f"calendar {cal!r} is not assigned to this agent"})
                    if data.get("error"):
                        parsed[cal] = (None, str(data["error"])[:500])
                    else:
                        parsed[cal] = (validate_events(data.get("events")), None)
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                return self._send(400, {"error": f"bad request: {e}"})
            stored = {}
            with lock:
                for cal, (events, err) in parsed.items():
                    if err:
                        prev = store.get_push(cal)
                        # keep the last good event set; record the error alongside it
                        store.save_push(cal, agent, prev["events"] if prev else [], error=err)
                        stored[cal] = f"error recorded: {err}"
                    else:
                        store.save_push(cal, agent, events)
                        stored[cal] = len(events)
            self._send(200, {"ok": True, "stored": stored})

    return ThreadingHTTPServer((host or cfg.data["server"]["host"], int(port if port is not None else cfg.data["server"]["port"])), Handler)
