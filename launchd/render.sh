#!/bin/sh
# Renders the launchd templates into ./build for review. Installs nothing.
#   launchd/render.sh [PYTHON3]
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
PY="${1:-/usr/bin/python3}"
OUT="$REPO/build"
mkdir -p "$OUT"
for t in "$HERE"/*.plist.template; do
  f="$OUT/$(basename "$t" .template)"
  sed -e "s#__PYTHON3__#$PY#g" -e "s#__CALSYNC_DIR__#$REPO#g" -e "s#__HOME__#$HOME#g" "$t" > "$f"
  plutil -lint "$f" >/dev/null
  echo "$f"
done
