# calsync

calsync keeps Jared Cluff's six calendars in step. The personal Google calendar gets a full copy of every work event. Each work calendar gets an opaque "Busy" block for every event on every other calendar, so colleagues at one company see when he is booked elsewhere and nothing else.

The hub is standard-library Python 3 (3.9 or later, no pip installs). The work-Mac agent is JavaScript for Automation plus `curl`, both of which ship with macOS.

## Calendars

| Name | Where | How calsync reaches it |
|---|---|---|
| personal | Google, jbcluff@gmail.com | Google Calendar API v3 from the hub |
| gigabug | Google Workspace, jared.cluff@gigabugpro.com | Google Calendar API v3 from the hub |
| labalicious | Google Workspace, jared.cluff@labalicious.com | Google Calendar API v3 from the hub |
| peregryn | Microsoft 365, Jared.Cluff@peregryn.com | Microsoft Graph from the hub (existing Entra app, device-code sign-in) |
| phdata | Exchange, on the work Mac only | EventKit agent on the work Mac, through the hub API |
| kiewit | Exchange, on the work Mac only | EventKit agent on the work Mac, through the hub API |

## Privacy matrix

Rows are where an event starts. Columns are where calsync writes a copy.

| Source | personal | gigabug | labalicious | peregryn | phdata | kiewit |
|---|---|---|---|---|---|---|
| personal | (source) | Busy | Busy | Busy | Busy | Busy |
| gigabug | full details, "[Gigabug] " | (source) | Busy | Busy | Busy | Busy |
| labalicious | full details, "[Labalicious] " | Busy | (source) | Busy | Busy | Busy |
| peregryn | full details, "[Peregryn] " | Busy | Busy | (source) | Busy | Busy |
| phdata | full details, "[phData] " | Busy | Busy | Busy | (source) | Busy |
| kiewit | full details, "[Kiewit] " | Busy | Busy | Busy | Busy | (source) |

A full-details copy has the tagged title, start and end, location, description, and the online-meeting link (put at the top of the description). It is private and has no reminders.

A Busy block has the title "Busy", is marked busy and private, and has no reminders, attendees, location or description. EventKit cannot set Exchange's private flag, so blocks the agent writes on phdata and kiewit are Busy-titled with empty fields but not flagged private.

### What counts as a real event

calsync mirrors an event instance only if all of these hold:

- it does not carry a calsync marker (calsync never mirrors its own copies, so copies cannot loop);
- it is not free. Google `transparency: transparent`, Graph `showAs` of `free` or `workingElsewhere`, Google `workingLocation` events and EventKit availability "free" all count as free;
- Jared has not declined it;
- it is not cancelled;
- it is not an all-day event, unless `include_all_day` is true in the config.

Tentative events and out-of-office events count as busy.

The window runs from 1 day ago to 60 days ahead (`window.days_back`, `window.days_ahead`). calsync expands recurring series into instances (Google `singleEvents=true`, Graph `calendarView`, EventKit predicates) and writes each copy as a single, non-recurring event.

## Safety rules

- **Markers.** Every copy carries its mirror id. On Google it is `extendedProperties.private.calsync`. On Graph it is a single-value extended property `String {b22593c1-1406-4831-82c5-a149e02be708} Name calsync` plus the category `calsync`. On EventKit it is the URL `calsync://<mirror id>` plus a `calsync-mirror:` line in the notes.
- **Mirror ids** look like `<install id>.<source calendar>.<24 hex>`. The install id is 12 hex characters, created once and stored in the state database. Orphan cleanup deletes only marked events whose install id matches this installation.
- **Never touch unmarked events.** The engine updates or deletes only events it has just listed with its own marker. The Google and Graph connectors fetch the event again and check the marker before every update or delete. The agent checks again before every EventKit write.
- **A calendar that fails to read is frozen.** If a token has expired, the network is down, or the agent has never pushed, calsync leaves that calendar's existing copies alone everywhere and carries on with the others. Status is recorded per calendar (`calsync status`).
- **Stale agent.** If the work Mac has not pushed for `agent_stale_minutes` (default 30), the hub keeps using the last pushed set and marks the calendar STALE. It does not delete anything because the laptop is asleep.
- **Aged-out copies stay.** When an event slides out of the back of the window, calsync forgets the mapping and leaves the old copy in place.
- **Kill switch.** If `~/.config/calsync/DISABLED` exists, `calsync sync` does nothing and `GET /agent/blocks` returns 503. The agent has its own switch at `~/.config/calsync-agent/DISABLED` on the work Mac.
- **Backoff.** HTTP 429 and 5xx responses and network errors retry with exponential backoff (honouring `Retry-After`), up to 5 times. Creating an event is the exception: after a timeout or 5xx the event may already exist, so a create retries only on 429 and is otherwise left to the next pass.
- **Duplicates.** If two events on a target carry the same mirror id (a create whose response was lost), the engine deletes the extra copy and keeps the one in its state database.
- **One sync at a time.** `calsync sync` takes a file lock next to the state database.

## Architecture

```
 Mac Studio (hub, always on)                                   Work Mac (corporate laptop)
 +-------------------------------------------------+           +-------------------------------+
 | launchd: calsync sync (every 5 min)             |           | launchd: calsync-agent.sh     |
 |   Google API  <--> personal, gigabug,           |           |   (every 5 min)               |
 |                    labalicious                  |           |                               |
 |   Graph API   <--> peregryn                     |           |  calsync-agent.js (JXA)       |
 |   SQLite state: mappings, agent pushes,         |           |    EventKit <--> phdata,      |
 |                 agent block plans, status       |           |                  kiewit       |
 |                                                 |  HTTPS    |                               |
 | launchd: calsync serve (KeepAlive)              | <-------- |  curl POST /agent/events      |
 |   127.0.0.1:8787  <-- cloudflared tunnel <------| --------> |  curl GET  /agent/blocks      |
 +-------------------------------------------------+           +-------------------------------+
```

Each pass, the hub:

1. reads every calendar in the window. Agent calendars come from the last push in SQLite.
2. keeps the real events.
3. for each target, builds the desired set of copies (details for personal, Busy for the rest).
4. for Google and Graph targets, creates, updates (when the content hash changes) and deletes to match, recording `(target, mirror id) -> (source calendar, source instance id, target event id, hash)` in SQLite.
5. for phdata and kiewit, stores the desired Busy blocks as a plan. The agent fetches the plan and applies it with EventKit.

The agent, every 5 minutes:

1. exports real events for phdata and kiewit (`--export`) and POSTs them to the hub;
2. GETs `/agent/blocks?calendar=X` for each, and creates, updates or deletes its marked Busy blocks to match (`--apply`). It removes only blocks whose install id matches the hub's and whose source calendar the hub read on that pass.

### Hub API

- `POST /agent/events` with body `{"calendars": {"phdata": {"events": [...]}, "kiewit": {"error": "calendar not found"}}}`. An `error` entry keeps the last good set and freezes that calendar.
- `GET /agent/blocks?calendar=phdata` returns `{"calendar", "install_id", "generated_at", "window": {"start", "end"}, "blocks": [{"mirror_id", "title", "start", "end", "all_day", "hash"}], "frozen_sources": [...]}`. It returns 409 before the first sync and 503 while the kill switch is on.
- `GET /agent/install` needs no key and holds no secret. It returns a shell script that asks for the key at the terminal, downloads the agent (`GET /agent/bundle.tar.gz`, key required), writes the hub address and key into the agent's config and installs it. Run it on the work Mac with `curl -fsSL https://calsync.jaredcluff.com/agent/install | sh`.
- `GET /agent/bundle.tar.gz` returns the `agent/` files, built from the repo on each request.
- `GET /status` (admin key) returns each calendar's last read and write result, mirror and block counts, and the agent's last push.
- `POST /sync` (admin key) starts a sync on the hub and returns at once; `POST /sync?dry_run=1` plans without writing. Output goes to `sync-on-demand.log` beside the state database.
- `GET /healthz` needs no auth.

An admin key is an entry in `agents` with `"admin": true` and no calendars, for example `"jared": {"admin": true, "calendars": []}`. Create its key with `calsync agent-token jared`.

Each agent has its own long random bearer token. The hub stores only its SHA-256 in `~/.config/calsync/agent-tokens.json` (mode 0600) and compares with `hmac.compare_digest`. An agent can push and read only the calendars assigned to it in `agents` in the config.

## Google scopes

calsync requests one Google scope: `https://www.googleapis.com/auth/calendar.events.owned` ("See, create, change, and delete events on Google calendars you own").

The Calendar API discovery document lists `calendar.events.owned` as accepted by `events.list`, `events.get`, `events.insert`, `events.update` and `events.delete`. Those are the only methods calsync calls, and it calls them on the `primary` calendar, which the user owns. calsync addresses the calendar as `primary`, so it never needs the calendar list, and `calendar.calendarlist.readonly` is not needed. The broader `calendar.events` scope covers every calendar the user can access, which calsync does not need. If Google ever rejects the narrower scope for a Workspace account, set `"scopes": ["https://www.googleapis.com/auth/calendar.events"]` under `google` in the config and run `calsync auth google <name>` again.

Microsoft uses the delegated permissions already on the Peregryn Entra app: `Calendars.ReadWrite`, `User.ReadBasic.All`, `offline_access`, `openid`.

## Setup, in order

Do every step with dry runs first. Nothing below runs automatically.

### 1. Google Cloud OAuth client (Google Cloud console, by hand)

1. Create a project (for example `calsync`) at console.cloud.google.com.
2. APIs & Services > Library > enable **Google Calendar API**.
3. APIs & Services > OAuth consent screen (Google Auth Platform). User type **External**. Add `jbcluff@gmail.com`, `jared.cluff@gigabugpro.com` and `jared.cluff@labalicious.com` as test users while setting up.
4. Data access > add the scope `https://www.googleapis.com/auth/calendar.events.owned`.
5. Audience > **Publish app** to put it in **In production** status. In Testing status, Google expires refresh tokens after 7 days, so the hub would stop every week. A production app that requests a sensitive scope and has not been verified shows a "Google hasn't verified this app" warning at sign-in. Click Advanced, then Go to calsync. An unverified app is capped at 100 users, which is enough here.
6. Clients > Create client > Application type **Desktop app**. Download the JSON and save it on the studio as `~/.config/calsync/google-client.json` (`chmod 600`).

The Workspace domains (gigabugpro.com, labalicious.com) may block third-party apps. If sign-in says the app is blocked, allow the client ID in the Workspace admin console under Security > API controls > App access control.

### 2. Install the hub on the studio

```sh
git clone git@github.com:genkinsforge/calsync.git ~/gitrepos/calsync
mkdir -p ~/.config/calsync ~/Library/Logs/calsync && chmod 700 ~/.config/calsync
cp ~/gitrepos/calsync/config.example.json ~/.config/calsync/config.json   # review it
cd ~/gitrepos/calsync && python3 -m unittest discover -s tests -t .
```

`bin/calsync` runs the package with `/usr/bin/python3` (override with `CALSYNC_PYTHON`).

### 3. Sign in to each calendar on the studio

Google uses a loopback redirect to `127.0.0.1`. On a headless studio, forward a fixed port from your laptop and open the printed URL there:

```sh
ssh -L 8765:127.0.0.1:8765 studio
~/gitrepos/calsync/bin/calsync auth google personal --port 8765 --no-browser
~/gitrepos/calsync/bin/calsync auth google gigabug --port 8765 --no-browser
~/gitrepos/calsync/bin/calsync auth google labalicious --port 8765 --no-browser
~/gitrepos/calsync/bin/calsync auth microsoft peregryn      # device code; sign in from any browser
```

Each command stores a refresh token in `~/.config/calsync/tokens/<name>.json` (mode 0600, directory 0700) and then reads the next 7 days as a check. calsync does not use the keychain, because the keychain is locked when the studio runs headless.

### 4. Dry run on the studio

```sh
~/gitrepos/calsync/bin/calsync sync --dry-run
~/gitrepos/calsync/bin/calsync sync --dry-run --only personal
~/gitrepos/calsync/bin/calsync status
```

Until the agent pushes, phdata and kiewit show "no push yet" and are frozen. That is expected.

### 5. First real sync, one target at a time

```sh
~/gitrepos/calsync/bin/calsync sync --only personal
~/gitrepos/calsync/bin/calsync sync --only gigabug
~/gitrepos/calsync/bin/calsync sync
```

### 6. Hub API and Cloudflare Tunnel

```sh
~/gitrepos/calsync/bin/calsync agent-token worklaptop   # prints the token once
```

Add a public hostname to the studio's existing cloudflared tunnel, for example `calsync.<your domain>`, pointing at `http://127.0.0.1:8787`. Restrict it to `/agent/*` and `/healthz` if you add a Cloudflare Access policy. A service token in that policy would need the agent to send two extra headers, which `calsync-agent.sh` does not do yet. Follow the studio cloudflared notes: do not use `brew services restart cloudflared`.

### 7. launchd jobs on the studio

```sh
~/gitrepos/calsync/launchd/render.sh /usr/bin/python3     # writes ./build/*.plist, installs nothing
cp ~/gitrepos/calsync/build/com.genkinsforge.calsync.*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.genkinsforge.calsync.serve.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.genkinsforge.calsync.sync.plist
```

The plists set `LimitLoadToSessionType` to `Aqua` and `Background`. The studio often runs with nobody logged in at the console, and then the `gui/<uid>` domain does not exist; load the jobs with `launchctl bootstrap user/$(id -u) <plist>` instead, which only works because of the `Background` entry. Use `gui/$(id -u)` when someone is logged in.

### 8. Agent on the work Mac

The agent needs nothing installed, and nothing needs copying: the hub serves it. On the work Mac, in Terminal:

```sh
curl -fsSL https://calsync.jaredcluff.com/agent/install | sh
```

It asks for the worklaptop key with typing hidden, downloads the agent from the hub with it, writes the hub address and key into the config, then runs `install.sh`. The key never appears in a command line, so it stays out of process listings, endpoint-security logs and shell history; for the same reason the installer hands it to `curl` in a 0600 file and to `osascript` on stdin. Running the installer again replaces the key, which is how a rotated key reaches the Mac. To install by hand instead, copy the `agent/` folder over and run `sh agent/install.sh`.

`install.sh` copies the agent to `~/Library/Application Support/calsync-agent/`, writes `~/.config/calsync-agent/config.json` (mode 0600) from the example if it is missing, runs the self-test, writes `~/Library/LaunchAgents/com.genkinsforge.calsync-agent.plist` and loads it with `launchctl bootstrap gui/$(id -u)`. `uninstall.sh` reverses it and keeps the config.

Then, in Terminal:

```sh
"$HOME/Library/Application Support/calsync-agent/calsync-agent.sh" --list-calendars
```

The first run triggers the macOS Calendar permission prompt. Choose Full Access. The list shows each calendar's title, account, type and identifier. Put the hub URL, the token from step 6 and the mapping in the config. Use `"id"` when two calendars share a title:

```json
{
  "hub_url": "https://calsync.example.com",
  "token": "...",
  "calendars": {
    "phdata": {"title": "Calendar", "source": "jared.cluff@phdata.io"},
    "kiewit": {"id": "6A1F...-calendarIdentifier"}
  }
}
```

Then run `calsync-agent.sh --dry-run`. It exports events and fetches blocks, prints what it would change, and pushes nothing. Run `calsync-agent.sh` once by hand, run `calsync sync` on the hub, and run the agent again to see the Busy blocks land. The log is `~/Library/Logs/calsync-agent.log`.

#### Agent caveats

- EventKit sees Exchange calendars only if the accounts are added to macOS Internet Accounts (that is, they show in Calendar.app). If phData or Kiewit exist only in Outlook, the agent cannot read them. It reports "not found in EventKit" with this explanation, sends an error for that calendar, and the hub freezes it. Reading legacy Outlook through its AppleScript dictionary is not implemented. New Outlook has no AppleScript support at all.
- Corporate MDM can block Calendar access for scripts or block adding the account to Internet Accounts.
- When launchd runs the agent, macOS may attribute the calendar request to `osascript` rather than Terminal. If the log shows "Calendar access is denied" or "notDetermined" under launchd while Terminal works, open System Settings > Privacy & Security > Calendars and allow the entry it lists. This path has not been tested on a corporate Mac.

## Command reference

```
calsync auth google <name> [--port N] [--no-browser]
calsync auth microsoft <name>
calsync sync [--dry-run] [--only <calendar>]
calsync status
calsync serve [--port N]
calsync agent-token <agent name>

calsync-agent.sh [--dry-run | --list-calendars | --self-test | --help]
osascript -l JavaScript calsync-agent.js --help
```

`--only` limits writes to one target. calsync still reads every calendar because every source feeds that target. `CALSYNC_CONFIG` or `--config` points at another config file.

## Configuration

`~/.config/calsync/config.json`. See `config.example.json`.

| Key | Default | Meaning |
|---|---|---|
| `details_calendar` | `personal` | the calendar that receives full details |
| `window.days_back`, `window.days_ahead` | 1, 60 | sync window |
| `include_all_day` | false | mirror all-day events |
| `agent_stale_minutes` | 30 | mark agent calendars stale after this long without a push |
| `kill_switch_file` | `~/.config/calsync/DISABLED` | presence stops all writes |
| `calendars.<name>.type` | | `google`, `microsoft` or `agent` |
| `calendars.<name>.tag` | capitalised name | prefix on personal copies, as `[Tag] ` |
| `calendars.<name>.calendar_id` | primary / default | non-default calendar to use |
| `agents.<agent>.calendars` | | which agent calendars that token may push and read |

Calendar names must match `[a-z0-9][a-z0-9_-]{0,31}` because they appear in mirror ids.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The tests use fake calendars and touch no real account. They cover the qualification rules, the privacy matrix, loop prevention, updates, deletes, unmarked and foreign-marked events, failed and stale sources, recurrence instances, all-day and declined events, the Google and Graph request shapes, backoff, and the hub API over real HTTP. On macOS they also compile the JXA agent, run its self-test, and run `calsync-agent.sh` against a live hub with a stub in place of EventKit.
