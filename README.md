# Steam Deck Flipper Remote

Use your Flipper Zero's saved files from a Steam Deck, over Bluetooth: Sub-GHz signals,
Infrared remotes, 125 kHz RFID keys, NFC cards and iButton keys. Tap to send or emulate, hold to keep sending.

It comes in two parts. Use either one, or both:

| | What it is | Where you use it |
|---|---|---|
| **Desktop app** | A touch-friendly window with your Flipper's files and pins | Desktop Mode (and Game Mode, added to Steam as a non-Steam game) |
| **Decky plugin** | Your pins in the Quick Access Menu (**…** button), plus Browse files | Game Mode, with [Decky Loader](https://decky.xyz) |

They share the same pins and take turns with the Flipper, which only takes one Bluetooth connection at a time.

## Download

Get the files from the **[latest release](https://github.com/CodeKonga/flipper-remote/releases/latest)**:

- `flipper-remote-<version>.zip`: the desktop app
- `flipper-remote-decky-<version>.zip`: the Decky plugin
- `install-flipper-remote-plugin.tar.gz`: installs the plugin by hand, if Decky's own installer gets stuck

## Install the desktop app

1. In Desktop Mode, install a web browser from Discover if you don't have one (Brave, Chrome, Chromium, Edge,
   Vivaldi or Firefox). The app shows its window in it.
2. In Dolphin, right-click `flipper-remote-<version>.zip` and choose **Extract → Extract archive here**.
3. Open the extracted `flipper-remote` folder, right-click `install.sh` and choose **Run In Konsole**. When it
   says "FLIPPER REMOTE INSTALLATION SUCCESSFUL.", close the Konsole window.
4. Open **Flipper Remote** from the app menu (under Utilities) and tap **Pair a Flipper**. You only pair once.

Installing a newer version over an older one keeps your pins and settings. More in the
[app's README](flipper-remote/README.md).

## Install the Decky plugin

You need [Decky Loader](https://decky.xyz). The plugin isn't in the Decky Plugin Store, so it's installed from
the zip:

1. Put `flipper-remote-decky-<version>.zip` on the Deck, for example in Downloads.
2. In Game Mode, press **…**, open **Decky** (the plug icon), then the gear.
3. Under **General**, turn on **Developer mode**.
4. Open **Developer**, and next to **Install Plugin from ZIP File** choose **Browse**. Pick the zip.

**If it gets stuck on "Opening zip file":** in Desktop Mode, download `install-flipper-remote-plugin.tar.gz` into
Downloads, next to the zip. In Dolphin, right-click it and choose **Extract → Extract archive here**. Then
right-click `install-flipper-remote-plugin.sh` and choose **Run In Konsole**.

It asks for your password, installs the newest plugin zip it finds and restarts Decky. When it says
"FLIPPER REMOTE PLUGIN INSTALLATION SUCCESSFUL.", close the Konsole window. To update the plugin later, do the
same with the newer zip.

Then press **…** in Game Mode: **Flipper Remote** is in the Decky tab. Pair from **Pair a Flipper** at the bottom
of its panel (or in the desktop app). More in the [plugin's README](flipper-remote-decky/README.md).

## Good to know

- **One connection at a time.** If your phone's Flipper app is connected, the Deck can't connect (and the other
  way round). Close the phone app or turn off the phone's Bluetooth while you use the Deck.
- **Pair from Flipper Remote, not the Deck's Bluetooth settings.** The Flipper only pairs when you type the code
  it shows, and the Deck's own Bluetooth settings can't do that.
- **Firmware:** works with the official firmware, Unleashed and Xtreme.
- **Logs:** `~/.cache/flipper-remote/flipper-remote.log`. Lines from the plugin start with `Panel:`.
- **Nothing leaves your Deck.** The app talks to the Deck's Bluetooth service directly, and its window is
  served only to `127.0.0.1`.

## What's in this repository

| Folder | What's in it |
|---|---|
| `flipper-remote/` | The desktop app: one Python file (standard library only), its window (`ui.html`) and the installer |
| `flipper-remote-decky/` | The Decky plugin. `bash build.sh` builds the zip (needs pnpm); it copies the app's `flipper_remote.py` in |
| `tests/` | The app's tests, with a mock Flipper on a private D-Bus |

To run the tests you need Python 3, pytest, dbus-fast, protobuf, grpcio-tools and `dbus-daemon`. The plugin's
panel tests also use Playwright and Chromium.

1. Run `bash tests/make_pb.sh` first. It generates the mock Flipper's protobuf code from
   [Flipper's .proto files](https://github.com/flipperdevices/flipperzero-protobuf), which aren't copied here.
2. Then run `pytest tests`.
3. For the plugin, run `bash build.sh` and `npm ci` in `flipper-remote-decky/tests/harness`, then
   `pytest flipper-remote-decky/tests`.

## License

[MIT](LICENSE): use it, change it and share it, keeping the copyright notice. The Decky plugin's download also
contains code from other projects, under their own licenses: their notices are in
[flipper-remote-decky/LICENSE](flipper-remote-decky/LICENSE).

---

Not affiliated with Flipper Devices. Flipper Zero is their trademark.
