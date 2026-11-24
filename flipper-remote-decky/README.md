# Flipper Remote for the Quick Access Menu (Decky plugin)

Your Flipper Zero's pinned files in the Steam Deck's Quick Access Menu, in Game Mode:

- **Sub-GHz signals and Infrared buttons:** tap to send. Hold to keep sending until you let go.
- **RFID keys, NFC cards and iButton keys:** tap to emulate, with a countdown (30 s). Tap again to stop.
- **Everything, always:** the panel lists all your pins, and Browse files opens on "All" (Sub-GHz, Infrared,
  RFID 125 kHz, NFC, iButton). There's no Show menu in the plugin.
- **Browse files:** a full-screen page for finding files and pinning them (the star button next to each one).
- **Pair a Flipper:** at the bottom of the panel. The same steps as the app's "Pair a Flipper", in a dialog.

It's a companion to the Flipper Remote app, and uses the app's own code to talk to the Flipper.

## Install

You need [Decky Loader](https://decky.xyz) on the Steam Deck.

1. Put `flipper-remote-decky-1.1.0.zip` on the Deck, for example in Downloads.
2. In Game Mode, press **…** (Quick Access), open **Decky** (the plug icon), then the gear.
3. Under **General**, turn on **Developer mode**.
4. Open **Developer**, and next to **Install Plugin from ZIP File** choose **Browse**. Pick the zip.
5. Press **…** again: **Flipper Remote** is in the Decky tab.

### If Decky's "Install Plugin from ZIP File" gets stuck

If it stays on "Opening zip file", install it by hand instead (Decky's wiki lists copying a plugin into its plugins
folder as a supported way). In Desktop Mode, put `install-flipper-remote-plugin.sh` next to the zip (or keep the zip
in Downloads or Applications), and run it in Konsole:

    ./install-flipper-remote-plugin.sh

It asks for your password, copies the plugin into `~/homebrew/plugins/flipper-remote` with the same ownership Decky
gives plugins, and restarts Decky. It ends with "FLIPPER REMOTE PLUGIN INSTALLATION SUCCESSFUL." To update the
plugin, run it again with the newer zip: it uses the newest `flipper-remote-decky-*.zip` it finds.

## Sharing the Flipper with the Flipper Remote app

- **Same pins.** The plugin uses the app's settings file (`~/.config/flipper-remote/config.json`). The window's Show
  choice is the window's own: the plugin never changes it, and always shows everything.
- **One connection at a time.** The Flipper takes only one Bluetooth connection. The plugin uses it only while its
  panel or Browse page is open, and lets go about 12 seconds after you close them.
  - If the Flipper Remote window is open, the panel says so and leaves the Flipper to the window.
  - A window opened while the panel has the Flipper gets it as soon as the panel lets go.
  - This is the same lock the app's command-line tools use.
- **Pairing.** "Pair a Flipper" at the bottom of the panel opens a dialog with the app's own pairing steps:
  1. Pick your Flipper from the list.
  2. Type the 6-digit code it shows on the number pad.

  As in the app, a pairing that still works is never removed, and an old pairing is only replaced if you choose
  "Remove and pair again". The B button cancels. The window's "Pair a Flipper" works as before.

## Logs

- **What happened with the Flipper:** the app's own log, `~/.cache/flipper-remote/flipper-remote.log`. The plugin's
  lines start with `Panel:`, and its presses end in `, panel`.
- **Decky's log for the plugin:** `~/homebrew/logs/flipper-remote/`.

## Uninstall

In Decky's settings (the gear), open **Plugins** and choose **Uninstall** for Flipper Remote. Your pins and settings
stay: they belong to the app.

## Inside

- `main.py` runs inside Decky. It starts `py_modules/flipper_helper.py` with the Steam Deck's own Python, which drives
  the Flipper with the app's code (`py_modules/flipper_remote.py`, Flipper Remote 1.2.2, unchanged).
- `dist/index.js` is the panel and the Browse page, built from `src/` with `./build.sh`.
