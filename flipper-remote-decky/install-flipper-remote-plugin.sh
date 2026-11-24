#!/usr/bin/env bash
# Installs the Flipper Remote Decky plugin without Decky's "Install Plugin from ZIP File": unpacks the zip into
# Decky's plugins folder (~/homebrew/plugins/flipper-remote) and restarts Decky so it loads the plugin.
#
# Run it in Desktop Mode, in Konsole, as yourself (not with sudo). It asks for your password when it needs it.
#   ./install-flipper-remote-plugin.sh          (finds the zip next to it, or in Downloads or Applications)
#   ./install-flipper-remote-plugin.sh path/to/flipper-remote-decky-1.1.0.zip
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as yourself, without sudo. It asks for your password when it needs it." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
ZIP="${1:-}"
if [ -z "$ZIP" ]; then   # the newest one next to this script, or in Downloads or Applications
  ZIP="$(ls -t "$HERE"/flipper-remote-decky-*.zip "$HOME"/Downloads/flipper-remote-decky-*.zip \
               "$HOME"/Applications/flipper-remote-decky-*.zip 2>/dev/null | head -n 1 || true)"
fi
if [ -z "$ZIP" ] || [ ! -f "$ZIP" ]; then
  echo "Couldn't find flipper-remote-decky-*.zip next to this script, or in Downloads or Applications." >&2
  echo "Run it with the zip's location, like:  $0 ~/Downloads/flipper-remote-decky-1.1.0.zip" >&2
  exit 1
fi

PLUGINS="$HOME/homebrew/plugins"
if [ ! -d "$PLUGINS" ]; then
  echo "Decky Loader doesn't look installed: $PLUGINS doesn't exist." >&2
  exit 1
fi

WORK="$(mktemp -d)"
python3 - "$ZIP" "$WORK" <<'EOF'
import json, sys, zipfile
path, dest = sys.argv[1], sys.argv[2]
try:
    z = zipfile.ZipFile(path)
    damaged = z.testzip()
except (OSError, zipfile.BadZipFile) as e:
    sys.exit(f"Couldn't open {path}: {e}")
if damaged:
    sys.exit(f"{path} is damaged ({damaged}). Download it again.")
try:
    manifest = json.loads(z.read("flipper-remote/plugin.json"))
except KeyError:
    sys.exit(f"{path} isn't the Flipper Remote plugin (there's no flipper-remote/plugin.json in it).")
if manifest.get("name") != "Flipper Remote":
    sys.exit(f"{path} isn't the Flipper Remote plugin.")
z.extractall(dest)
EOF
VERSION="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' "$WORK/flipper-remote/package.json")"

echo "Installing the Flipper Remote plugin $VERSION into $PLUGINS/flipper-remote"
echo "(Decky's plugins folder belongs to root, so this needs your password.)"
# Decky runs as root. If root can't read the zip where it is, that's why Decky's own installer got stuck.
if ! sudo head -c 4 "$ZIP" > /dev/null 2>&1; then
  echo "Note: Decky (which runs as root) can't read the zip where it is. That's why its installer got stuck."
fi
sudo mkdir -p "$PLUGINS/flipper-remote"
sudo cp -R "$WORK/flipper-remote/." "$PLUGINS/flipper-remote/"
# What Decky does for a plugin that doesn't need root: its files are yours; the folder and plugin.json are root's.
sudo chown -R "$(id -u):$(id -g)" "$PLUGINS/flipper-remote"
sudo chown root:root "$PLUGINS/flipper-remote" "$PLUGINS/flipper-remote/plugin.json"
sudo chmod -R 755 "$PLUGINS/flipper-remote"

echo "Restarting Decky Loader so it loads the plugin"
sudo systemctl restart plugin_loader

echo "FLIPPER REMOTE PLUGIN INSTALLATION SUCCESSFUL."
