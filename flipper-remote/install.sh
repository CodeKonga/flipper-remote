#!/usr/bin/env bash
# Installs Flipper Remote for the current user (no root, survives SteamOS updates).
#   ~/.local/share/flipper-remote/     the app
#   ~/.local/bin/flipper-remote        command-line launcher
#   ~/.local/share/applications/       menu entry ("Flipper Remote")
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/flipper-remote"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "Flipper Remote needs Python 3.8 or newer (python3 wasn't found)." >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
if [ "$SRC" != "$APP_DIR" ]; then
  cp -f "$SRC/flipper_remote.py" "$SRC/ui.html" "$SRC/icon.svg" "$SRC/flipper-remote" \
        "$SRC/install.sh" "$SRC/uninstall.sh" "$SRC/README.md" "$APP_DIR/"
fi
chmod +x "$APP_DIR/flipper-remote" "$APP_DIR/install.sh" "$APP_DIR/uninstall.sh"
ln -sf "$APP_DIR/flipper-remote" "$BIN_DIR/flipper-remote"
cp -f "$APP_DIR/icon.svg" "$ICON_DIR/flipper-remote.svg"

cat > "$DESKTOP_DIR/flipper-remote.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Flipper Remote
GenericName=Flipper Zero remote
Comment=Use your Flipper Zero's saved Sub-GHz, Infrared, RFID, NFC and iButton files over Bluetooth
Exec="$APP_DIR/flipper-remote"
Icon=flipper-remote
StartupWMClass=flipper-remote
Terminal=false
Categories=Utility;
Keywords=flipper;subghz;infrared;rfid;nfc;ibutton;remote;bluetooth;
EOF
chmod +x "$DESKTOP_DIR/flipper-remote.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

echo "FLIPPER REMOTE INSTALLATION SUCCESSFUL."
echo
echo "  Open it:        app menu → Utilities → Flipper Remote"
echo "  Game Mode:      right-click it in the app menu → Add to Steam"
echo "  Command line:   flipper-remote doctor     (checks pairing and the connection)"
if pgrep -f "flipper-remote/flipper_remote.py" >/dev/null 2>&1; then
  echo
  echo "  Flipper Remote is open right now: close its window and open it again to start this version."
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo; echo "  Note: $BIN_DIR isn't on your PATH; run $APP_DIR/flipper-remote instead." ;;
esac
if ! ls ~/.local/share/flatpak/app /var/lib/flatpak/app 2>/dev/null | grep -qiE 'chrom|brave|edge|vivaldi|firefox|librewolf'; then
  echo
  echo "  No web browser found. Flipper Remote shows its window in one: install Brave, Chrome"
  echo "  or Firefox from Discover first. Chrome-based browsers give it a clean app window."
fi
