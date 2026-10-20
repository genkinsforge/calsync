#!/bin/sh
# Installs the calsync agent for the current user. Uses only what ships with macOS.
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/calsync-agent"
CONF_DIR="$HOME/.config/calsync-agent"
LABEL="com.genkinsforge.calsync-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$DEST" "$CONF_DIR" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod 700 "$CONF_DIR"
cp "$SRC/calsync-agent.js" "$SRC/calsync-agent.sh" "$SRC/uninstall.sh" "$DEST/"
chmod 755 "$DEST/calsync-agent.sh" "$DEST/uninstall.sh"
if [ ! -e "$CONF_DIR/config.json" ]; then
  cp "$SRC/config.example.json" "$CONF_DIR/config.json"
  echo "Wrote $CONF_DIR/config.json; edit hub_url, token and calendar mapping before the agent can sync."
fi
chmod 600 "$CONF_DIR/config.json"

/usr/bin/osascript -l JavaScript "$DEST/calsync-agent.js" --self-test

sed "s#__HOME__#$HOME#g" "$SRC/$LABEL.plist.template" > "$PLIST"
/usr/bin/plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Loaded $LABEL (every 5 minutes). Log: $HOME/Library/Logs/calsync-agent.log"
echo
echo "Next, from Terminal:"
echo "  \"$DEST/calsync-agent.sh\" --list-calendars   # triggers the Calendar permission prompt"
echo "  edit $CONF_DIR/config.json                   # map phdata/kiewit to what it listed"
echo "  \"$DEST/calsync-agent.sh\" --dry-run"
