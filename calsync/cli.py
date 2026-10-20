"""calsync command line."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import google, microsoft, net
from .config import CONFIG_DIR, Config, ConfigError
from .engine import Engine, KillSwitch
from .server import make_server, new_agent_token
from .state import Store


def build_connectors(cfg: Config) -> dict:
    out = {}
    tokens_dir = cfg.path("tokens_dir")
    for name, c in cfg.calendars.items():
        if c["type"] == "google":
            auth = google.GoogleAuth(name, _gclient(cfg), tokens_dir)
            out[name] = google.GoogleConnector(name, auth, c.get("calendar_id", "primary"))
        elif c["type"] == "microsoft":
            auth = microsoft.GraphAuth(name, c["tenant"], c["client_id"], cfg.data["microsoft"]["scopes"], tokens_dir)
            out[name] = microsoft.GraphConnector(name, auth, c.get("calendar_id", ""))
    return out


def _gclient(cfg: Config) -> Path:
    return Path(os.path.expanduser(cfg.data["google"]["client_file"]))


def cmd_auth(cfg: Config, args):
    c = cfg.calendars.get(args.name)
    if not c or c["type"] != args.provider:
        sys.exit(f"{args.name} is not a {args.provider} calendar in the config")
    if args.provider == "google":
        p = google.authorize(args.name, c.get("account", ""), _gclient(cfg), cfg.path("tokens_dir"),
                             cfg.data["google"]["scopes"], port=args.port, open_browser=not args.no_browser)
    else:
        p = microsoft.authorize(args.name, c.get("account", ""), c["tenant"], c["client_id"],
                                cfg.data["microsoft"]["scopes"], cfg.path("tokens_dir"))
    print(f"Refresh token saved to {p} (mode 0600).")
    conn = build_connectors(cfg)[args.name]
    try:
        now = datetime.now(timezone.utc)
        n = len(conn.list_events(now, now + timedelta(days=7)))
        print(f"Read check OK: {n} events in the next 7 days on {args.name}.")
    except Exception as e:
        print(f"Read check failed: {e}")


def cmd_sync(cfg: Config, args):
    lock_path = cfg.path("state_db").with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another sync is running")
        store = Store(cfg.path("state_db"))
        eng = Engine(cfg, store, build_connectors(cfg))
        started = time.time()
        try:
            res = eng.run(dry_run=args.dry_run, only=args.only)
        except KillSwitch as e:
            print(f"calsync: {e}; nothing done")
            return 0
        print(f"done in {time.time() - started:.1f}s; frozen={res['frozen']} stale={res['stale']}")
        bad = [k for k, v in res["summary"].items() if "error" in v or v.get("errors")]
        return 1 if (bad or res["frozen"]) and not args.dry_run else 0


def cmd_status(cfg: Config, args):
    store = Store(cfg.path("state_db"))
    st, counts = store.statuses(), store.mapping_counts()
    print(f"install id: {store.install_id(cfg.data.get('install_id'))}")
    print(f"kill switch: {'ON (' + str(cfg.kill_switch) + ')' if cfg.kill_switch.exists() else 'off'}")
    for name, c in cfg.calendars.items():
        s = st.get(name, {})
        line = f"{name:12} {c['type']:9}"
        line += f" read={'ok' if s.get('read_ok') else ('FAIL' if s.get('read_ok') == 0 else '-')}"
        line += f" write={'ok' if s.get('write_ok') else ('FAIL' if s.get('write_ok') == 0 else '-')}"
        if s.get("stale"):
            line += " STALE"
        if c["type"] == "agent":
            push = store.get_push(name)
            line += f" last_push={_ago(push['pushed_at']) if push else 'never'}"
            plan = store.get_plan(name)
            line += f" blocks={len(plan['blocks']) if plan else 0}"
        else:
            line += f" mirrors={counts.get(name, 0)}"
        line += f" last_ok={_ago(s['last_ok_at']) if s.get('last_ok_at') else 'never'}"
        print(line)
        for k in ("read_msg", "write_msg"):
            if s.get(k):
                print(f"{'':12} {k[:-4]}: {s[k]}")


def _ago(ts: float) -> str:
    m = int((time.time() - ts) / 60)
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago"


def cmd_serve(cfg: Config, args):
    store = Store(cfg.path("state_db"))
    srv = make_server(cfg, store, port=args.port)
    host, port = srv.server_address[:2]
    print(f"calsync hub API listening on http://{host}:{port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_agent_token(cfg: Config, args):
    if args.name not in cfg.data["agents"]:
        sys.exit(f"agent {args.name!r} is not in config 'agents'")
    token = new_agent_token(cfg.path("agent_tokens_file"), args.name)
    print("New bearer token (shown once; only its hash is stored). Put it in the agent's config.json:\n")
    print(token)


def main(argv=None):
    p = argparse.ArgumentParser(prog="calsync", description="Keep Jared's calendars in step with privacy rules.")
    p.add_argument("--config", default=os.environ.get("CALSYNC_CONFIG", str(CONFIG_DIR / "config.json")))
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth", help="sign in to a calendar and store its refresh token")
    a.add_argument("provider", choices=["google", "microsoft"])
    a.add_argument("name")
    a.add_argument("--port", type=int, default=0, help="Google loopback port (fixed port helps with ssh -L)")
    a.add_argument("--no-browser", action="store_true")
    s = sub.add_parser("sync", help="run one sync pass")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--only", help="sync only this target calendar")
    sub.add_parser("status")
    v = sub.add_parser("serve", help="run the agent API")
    v.add_argument("--port", type=int)
    t = sub.add_parser("agent-token", help="create or rotate an agent bearer token")
    t.add_argument("name")
    args = p.parse_args(argv)
    try:
        cfg = Config.load(args.config)
    except (ConfigError, json.JSONDecodeError) as e:
        sys.exit(f"config error: {e}")
    try:
        rc = {"auth": cmd_auth, "sync": cmd_sync, "status": cmd_status, "serve": cmd_serve,
              "agent-token": cmd_agent_token}[args.cmd](cfg, args)
    except net.AuthError as e:
        sys.exit(f"auth error: {e}")
    return rc or 0
