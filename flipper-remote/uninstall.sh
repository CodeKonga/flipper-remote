#!/usr/bin/env bash
# Removes Flipper Remote. Add --purge to also delete your pinned signals, cache and logs.
set -u
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
rm -f "$HOME/.local/bin/flipper-remote" \
      "$DATA/applications/flipper-remote.desktop" \
      "$DATA/icons/hicolor/scalable/apps/flipper-remote.svg"
rm -rf "$DATA/flipper-remote"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/flipper-remote" "${XDG_CACHE_HOME:-$HOME/.cache}/flipper-remote"
  for profile in "$HOME"/.var/app/*/cache/flipper-remote; do   # the window's own browser profile
    [ -d "$profile" ] && rm -rf "$profile"
  done
fi
echo "Flipper Remote removed.${1:+ Settings and cache deleted too.}"
