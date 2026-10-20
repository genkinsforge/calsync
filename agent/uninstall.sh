#!/bin/sh
# Stops and removes the calsync agent. Leaves ~/.config/calsync-agent (token) and any Busy
# blocks already on the calendars; delete the config by hand if you want it gone.
set -eu
LABEL="com.genkinsforge.calsync-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
rm -rf "$HOME/Library/Application Support/calsync-agent"
echo "Removed $LABEL. Config kept at $HOME/.config/calsync-agent (contains the hub token)."
