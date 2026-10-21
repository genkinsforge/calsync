"""Hub API for the work-Mac agent. Plain HTTP on 127.0.0.1; a Cloudflare Tunnel fronts it with TLS.

  POST /agent/events            {"calendars": {"phdata": {"events": [...]}, "kiewit": {"error": "..."}}}
  GET  /agent/blocks?calendar=X  desired Busy blocks for X, each with its mirror id and hash
  GET  /healthz                  liveness, no auth
  GET  /agent/install            installer script, no auth (holds no secret; asks for the key)
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tarfile
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
AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
REPO_DIR = AGENT_DIR.parent
BUNDLE_FILES = ("calsync-agent.js", "calsync-agent.sh", "install.sh", "uninstall.sh",
                "config.example.json", "com.genkinsforge.calsync-agent.plist.template")


def agent_bundle() -> bytes:
    """The agent directory as a gzipped tar, built fresh so it always matches the repo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in BUNDLE_FILES:
            tar.add(AGENT_DIR / name, arcname=f"calsync-agent/{name}")
    return buf.getvalue()


def install_script(hub_url: str) -> str:
    """A shell script that asks for the agent key, downloads the bundle with it, writes the hub
    address and key into the agent's config and installs the agent. It holds no secret, so it is
    served without a key. The key is read from the terminal and handed to curl and osascript
    through files and stdin, never as an argument, so it stays out of process listings, endpoint
    security logs and shell history."""
    q = lambda v: "'" + v.replace("'", "'\\''") + "'"
    return f"""#!/bin/sh
# calsync agent installer, served by {hub_url}. Uses only what ships with macOS.
#   curl -fsSL {hub_url}/agent/install | sh
set -eu
HUB={q(hub_url)}
CONF_DIR="$HOME/.config/calsync-agent"
CONF="$CONF_DIR/config.json"
umask 077
TMP="$(mktemp -d)"
trap 'stty echo </dev/tty 2>/dev/null || true; rm -rf "$TMP"' EXIT INT TERM
stty -echo </dev/tty
printf 'calsync agent key (typing is hidden): ' >/dev/tty
IFS= read -r TOKEN </dev/tty || TOKEN=""
stty echo </dev/tty
printf '\\n' >/dev/tty
case "$TOKEN" in
  ""|*[!A-Za-z0-9_-]*) echo "That is not a calsync key." >&2; exit 1 ;;
esac
printf 'header = "Authorization: Bearer %s"\\n' "$TOKEN" > "$TMP/auth.curl"
/usr/bin/curl -fsS -K "$TMP/auth.curl" "$HUB/agent/bundle.tar.gz" -o "$TMP/b.tgz" \\
  || {{ echo "The hub refused the download. Check the key." >&2; exit 1; }}
/usr/bin/tar -xzf "$TMP/b.tgz" -C "$TMP"
# Config first, so the launchd job's first run already has the hub address and key.
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
[ -e "$CONF" ] || cp "$TMP/calsync-agent/config.example.json" "$CONF"
cat > "$TMP/setkey.js" <<'JS'
function run(argv) {{
  ObjC.import('Foundation');
  var data = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
  var path = argv[0];
  var c = JSON.parse($.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null).js);
  c.hub_url = argv[1];
  c.token = $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js;
  if (!$(JSON.stringify(c, null, 2)).writeToFileAtomicallyEncodingError(path, true, $.NSUTF8StringEncoding, null))
    throw new Error('cannot write ' + path);
}}
JS
printf '%s' "$TOKEN" | /usr/bin/osascript -l JavaScript "$TMP/setkey.js" "$CONF" "$HUB"
chmod 600 "$CONF"
echo "Set hub_url and key in $CONF."
sh "$TMP/calsync-agent/install.sh"
echo
echo "Now map your calendars. This also triggers the macOS Calendar permission prompt:"
echo "  \\"$HOME/Library/Application Support/calsync-agent/calsync-agent.sh\\" --list-calendars"
"""


PAGES = {
    "/": ("calsync", "<p>calsync keeps one person's calendars in step: each calendar gets a Busy block "
          "for time taken on the others, and only the owner's personal calendar receives event details. "
          "It is a private tool run by Jared Cluff for his own accounts and has no other users.</p>"
          '<p><a href="/privacy">Privacy policy</a></p>'),
    "/privacy": ("calsync privacy policy",
                 "<p>calsync is used only by its owner, Jared Cluff, on his own calendar accounts.</p>"
                 "<p><b>What it reads.</b> Events on the calendars the owner signs in: times, titles, "
                 "locations, descriptions and meeting links, and calsync's own Busy blocks.</p>"
                 "<p><b>What it writes.</b> Busy blocks, marked as calsync's, on the owner's calendars, and "
                 "detailed copies on the owner's personal calendar only. It never changes or deletes an "
                 "event it did not create.</p>"
                 "<p><b>Where data lives.</b> Sign-in tokens and sync state are stored on a computer the "
                 "owner controls. Nothing is sold, shared, or sent to anyone else, and Google user data "
                 "is not used for advertising or to train models.</p>"
                 "<p><b>Removing access.</b> Revoke calsync at myaccount.google.com/permissions; its stored "
                 "token then stops working. Contact: jbcluff@gmail.com.</p>"),
}


def page(path: str) -> bytes | None:
    if path not in PAGES:
        return None
    title, body = PAGES[path]
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{title}</title><style>body{{font:16px/1.5 '
            f'system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}}</style></head><body>'
            f'<h1>{title}</h1>{body}</body></html>').encode()


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

        def _send_raw(self, code, raw: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _admin(self):
            agent = self._agent()
            if agent and not cfg.data["agents"].get(agent, {}).get("admin"):
                self._send(403, {"error": "this key is not an admin key"})
                return None
            return agent

        def _hub_url(self):
            # Behind the tunnel the public host arrives in Host; X-Forwarded-Proto says https.
            proto = self.headers.get("X-Forwarded-Proto") or ("https" if self.headers.get("Cf-Ray") else "http")
            return f"{proto}://{self.headers.get('Host', '127.0.0.1')}"

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/healthz":
                return self._send(200, {"ok": True})
            if page(u.path) is not None:   # home page and privacy policy for Google's consent screen
                return self._send_raw(200, page(u.path), "text/html; charset=utf-8")
            if u.path == "/agent/install":
                return self._send_raw(200, install_script(self._hub_url()).encode(), "text/x-shellscript")
            if u.path == "/agent/bundle.tar.gz":
                if not self._agent():
                    return
                return self._send_raw(200, agent_bundle(), "application/gzip")
            if u.path == "/status":
                if not self._admin():
                    return
                with lock:
                    statuses, counts = store.statuses(), store.mapping_counts()
                    cals = {}
                    for name, c in cfg.calendars.items():
                        entry = {"type": c["type"], **statuses.get(name, {})}
                        if c["type"] == "agent":
                            push = store.get_push(name)
                            plan = store.get_plan(name)
                            entry["last_push_at"] = push["pushed_at"] if push else None
                            entry["blocks"] = len(plan["blocks"]) if plan else 0
                        else:
                            entry["mirrors"] = counts.get(name, 0)
                        cals[name] = entry
                return self._send(200, {"install_id": install_id, "kill_switch": cfg.kill_switch.exists(), "calendars": cals})
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
            if u.path == "/sync":
                if not self._admin():
                    return
                args = [sys.executable, "-m", "calsync"] + (["--config", str(cfg.file)] if cfg.file else []) + ["sync"]
                if urllib.parse.parse_qs(u.query).get("dry_run"):
                    args.append("--dry-run")
                # Detached: the request returns at once and the sync runs to completion on its own.
                with open(Path(cfg.path("state_db")).with_name("sync-on-demand.log"), "ab") as log:
                    subprocess.Popen(args, cwd=str(REPO_DIR), stdin=subprocess.DEVNULL, start_new_session=True,
                                     stdout=log, stderr=subprocess.STDOUT)
                return self._send(202, {"ok": True, "started": " ".join(args[args.index("sync"):])})
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
