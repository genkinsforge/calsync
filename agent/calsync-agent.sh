#!/bin/sh
# calsync agent wrapper: EventKit work in JXA, HTTP with the system curl. Nothing to install.
#
#   calsync-agent.sh                  push events, then apply the hub's Busy blocks
#   calsync-agent.sh --dry-run        export and fetch, print planned changes; no push, no writes
#   calsync-agent.sh --list-calendars what EventKit can see
#   calsync-agent.sh --self-test      logic tests, no calendar access
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
JS="${CALSYNC_AGENT_JS:-$DIR/calsync-agent.js}"
CONFIG="${CALSYNC_AGENT_CONFIG:-$HOME/.config/calsync-agent/config.json}"
STOP="$HOME/.config/calsync-agent/DISABLED"
jxa() { /usr/bin/osascript -l JavaScript "$JS" --config "$CONFIG" "$@"; }
say() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >&2; }

MODE="sync"
DRY=""
case "${1:-}" in
  --help|-h) /usr/bin/osascript -l JavaScript "$JS" --help; exit 0 ;;
  --self-test) exec /usr/bin/osascript -l JavaScript "$JS" --self-test ;;
  --list-calendars) exec /usr/bin/osascript -l JavaScript "$JS" --list-calendars ;;
  --dry-run) DRY="--dry-run" ;;
  "") ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

if [ -e "$STOP" ]; then say "kill switch $STOP present; nothing done"; exit 0; fi
[ -r "$CONFIG" ] || { say "config not found: $CONFIG"; exit 1; }

cfg() { /usr/bin/plutil -extract "$1" raw -o - "$CONFIG" 2>/dev/null || true; }
HUB="$(cfg hub_url)"; HUB="${HUB%/}"
TOKEN="$(cfg token)"
[ -n "$HUB" ] && [ -n "$TOKEN" ] || { say "config needs hub_url and token"; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/calsync-agent.XXXXXX")"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT INT TERM
# Keep the token out of the process list: curl reads the header from a 0600 config file.
umask 077
printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$WORK/auth.curl"
CURL="/usr/bin/curl -sS --fail --max-time 60 --retry 3 --retry-delay 5 -K $WORK/auth.curl"

say "exporting events"
jxa --export "$WORK/events.json"

if [ -z "$DRY" ]; then
  $CURL -H 'Content-Type: application/json' --data-binary "@$WORK/events.json" \
    -o "$WORK/push.out" "$HUB/agent/events" || { say "push to $HUB failed"; exit 1; }
  say "pushed: $(cat "$WORK/push.out")"
else
  say "dry run: not pushing events"
fi

rc=0
for CAL in $(jxa --calendar-names); do
  if ! $CURL -o "$WORK/blocks-$CAL.json" "$HUB/agent/blocks?calendar=$CAL"; then
    say "[$CAL] could not fetch blocks (hub not synced yet, disabled, or unreachable); leaving calendar alone"
    rc=1
    continue
  fi
  jxa --apply "$WORK/blocks-$CAL.json" --calendar "$CAL" $DRY || rc=1
done
exit $rc
