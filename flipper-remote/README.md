# Flipper Remote

Use your Flipper Zero's saved Sub-GHz signals (lights, ceiling fan, …), Infrared remotes, 125 kHz RFID
keys, NFC cards and iButton keys from a Steam Deck over Bluetooth, the same way the official Flipper
app's **Send** and **Emulate** buttons do.

It is small (one Python file and one HTML page) and needs nothing installed beyond what SteamOS already
has: it talks to the Deck's Bluetooth service directly, and shows its touch-friendly window in a web
browser you already have. Nothing leaves your Deck; the window is served only to `127.0.0.1`.

Works with the official firmware, Unleashed and Xtreme.

---

## One-time setup

1. **Have a web browser installed.** Flipper Remote shows its window in your default browser. Brave,
   Chrome, Chromium, Edge and Vivaldi open it as a clean app window of its own; Firefox works too.
   (SteamOS doesn't ship a browser; any of these from Discover will do.) The window runs as a
   separate copy of the browser with a small profile of its own, so it opens at the right size even
   when your browser is already open, and never touches your tabs, extensions or settings.
2. **Install Flipper Remote.** In Desktop Mode, extract the zip, open Konsole in the extracted
   `flipper-remote` folder and run:

   ```
   bash install.sh
   ```

   This copies the app to `~/.local/share/flipper-remote`, adds a `flipper-remote` command and a
   **Flipper Remote** entry in the app menu (under Utilities). No root, no `steamos-readonly`, and it
   survives SteamOS updates. Installing a new version over an old one keeps your pins and settings.
3. **Pair the Flipper with the Deck, from Flipper Remote.** On the Flipper, turn Bluetooth on
   (*Settings → Bluetooth*) and go back to its main screen; turn off your phone's Bluetooth for a
   minute. Open **Flipper Remote**, tap **Pair a Flipper** at the bottom of the window, tap your
   Flipper in the list, and type the 6-digit code the Flipper shows on the number pad. You only do
   this once. (In Konsole, `flipper-remote pair` does the same thing.)

   Don't use the Deck's own Bluetooth settings for this: the Flipper only accepts pairing where you
   type the code it shows, and KDE's pairing can't type codes, so it always fails there.

If the Flipper ever forgets the Deck (or you reset its Bluetooth), the window says so and offers
**Pair a Flipper** right there. Choosing a Flipper that's already paired never breaks a pairing that
works: the app first checks whether that pairing still connects. If it does, nothing is changed. If it
doesn't, the app asks before removing it and pairing again (*Keep the pairing* is the default).

> **A Flipper talks to one device at a time.** If your iPhone's Flipper app is connected, the Deck
> can't connect (and vice versa). Close the phone app or turn off the phone's Bluetooth while you use
> the Deck. Flipper Remote disconnects when you close its window, so the phone can reconnect.

## Using it

Open **Flipper Remote** from the app menu. It connects to your Flipper and shows what you've saved on
it. The **Show** menu next to *Pinned only* picks what's on screen: **All** (a folder for each kind,
and every pin), **Sub-GHz**, **Infrared**, **RFID 125 kHz**, **NFC** or **iButton**. It shows **All**
until you pick something else; after that the window remembers your choice and opens showing just
that, along with the folder you were in for each choice.

| Do this | What happens |
|---|---|
| Tap a Sub-GHz signal | Sends it once (like tapping **Send** in the phone app). RAW recordings play all the way through. |
| Tap an Infrared remote | Opens it: one key per button on the remote. |
| Tap a remote's button | Sends it once. Hold it to keep sending, like holding a button on the real remote (30 s at most). |
| Hold a Sub-GHz signal | Keeps sending until you let go (30 s at most). |
| Tap an RFID key, NFC card or iButton key | The Flipper emulates it: hold the Flipper to the reader. The key counts down from 30 s; tap it again to stop sooner. Pressing anything else also stops it. |
| Tap the ☆ | Pins it to the top of the window. Pinned remote buttons keep the remote's name ("TV Power"). The pinned row shows the pins for what *Show* is set to (all of them under **All**). |
| *Pinned only* | Hides everything except your pins (for what *Show* is set to) and shrinks the window to fit them, for a tidy remote. Tap it again to see all your files. |
| Tap a folder | Opens it. Use the breadcrumb or ↑ to go back. |
| *Disconnect* | Frees the Flipper for your phone without closing the window. |
| *Pair a Flipper* | Pairs a Flipper with the Deck, or pairs it again if its pairing stopped working. |

The app reads small files from the Flipper in the background to show what they are (a signal's
frequency, a card's type and UID, how many buttons a remote has) and remembers them. The Flipper's own
libraries (the *assets* folders, such as the universal remotes) are hidden.

**Keyboard:** arrow keys move between buttons, Enter sends or emulates (hold to keep sending), Esc goes
up a folder. On the *Show* menu, Enter opens the list.

**Controller:** in Desktop Mode the window ignores the controller itself and only goes by what Steam's
desktop layout turns it into (mouse and keys), so a button you've mapped to something else can't also
press, pin or unpin things in the window. In Game Mode the window reads the controller directly: the
D-pad moves between buttons, **A** sends or emulates (hold to keep sending) and steps through the *Show*
menu, **B** goes up a folder and **Y** pins.
To get that in Desktop Mode too, start it with `flipper-remote --controller`.

**Dark mode** (button at the bottom) switches to orange-on-black, like Xtreme's dark mode.

The window remembers *Show*, *Pinned only*, dark mode and the folder you were in, and opens the same
way next time. There's only ever one window: opening Flipper Remote while it's open brings up a new
window, and the old one closes as soon as the new one is ready.

## Game Mode

In Desktop Mode, right-click **Flipper Remote** in the app menu and choose **Add to Steam**. In Game
Mode it opens full screen and works with touch or the controller. If it ever opens in a small window
instead, or the controller does nothing, add `--fullscreen --controller` to the shortcut's launch options in
Steam (*Properties → Shortcut*). Quit
it from the Steam menu (*Exit game*); it disconnects from the Flipper on the way out.

## Command line

```
flipper-remote                              # open the window
flipper-remote list                         # what's on the Flipper
flipper-remote list Ceiling_Fan
flipper-remote send Ceiling_Fan/Fan_Off     # path under /ext/subghz, .sub optional
flipper-remote send Lights/Dimmer --hold 3  # keep sending for 3 seconds
flipper-remote list infrared/TV             # a remote's buttons
flipper-remote send infrared/TV --button Power
flipper-remote send nfc/Badge               # emulate for 30 s (--hold 60 for a minute)
flipper-remote send lfrfid/Gym              # same for ibutton/...
flipper-remote pair                         # pair (or re-pair) a Flipper; asks for the code
flipper-remote doctor                       # step-by-step check of Bluetooth, pairing and the Flipper
flipper-remote devices                      # Flipper-like Bluetooth devices this Deck knows
flipper-remote disconnect
```

`send` works whether or not the window is open; if it is, the command goes through the open window's
connection. That makes it easy to put on a KDE keyboard shortcut (System Settings → Shortcuts).
Add `--device AA:BB:CC:DD:EE:FF` if you have more than one Flipper paired.

## If something goes wrong

Start with `flipper-remote doctor` in Konsole: it walks through every step and says what failed.

| Message | Fix |
|---|---|
| *No paired Flipper found* | Tap **Pair a Flipper** (setup step 3). Custom Flipper names are fine; the app looks for the Flipper's Bluetooth service, not its name. |
| *Couldn't connect … isn't connected to your phone* | Turn on Bluetooth on the Flipper, bring it closer, and disconnect the phone. |
| *isn't properly paired* | Tap **Pair a Flipper** and choose your Flipper. The app checks the old pairing; when it asks, choose *Remove and pair again*. If that still fails, use *Settings → Bluetooth → Forget All Paired Devices* on the Flipper and pair again (your phone will need re-pairing too). |
| Your Flipper isn't in the *Pair a Flipper* list | Turn its Bluetooth on, go to its main screen and turn off your phone's Bluetooth. If it still doesn't appear, restart the Flipper (hold **Left** + **Back**). |
| Connecting takes a long time | Every connection writes a line like *Connected to Ufakoko in 2.1 s (Bluetooth link 1.4 s, services 0.3 s, setup 0.2 s, first answer 0.1 s)* to the log (below), which shows the slow step. A long *Bluetooth link* means the Deck is slow to hear the Flipper: after a minute idle it only announces itself every 1–2.5 s, and a Bluetooth controller or headset in use, or a busy 2.4 GHz Wi-Fi network, leaves the Deck less time to listen. |
| *The code didn't match* / *Pairing timed out* | Try again and type the code within about 30 seconds. The Flipper shows a new code each try. |
| *connected but isn't answering* | Unlock the Flipper if it has a PIN (on Xtreme you can instead turn on *Allow locked RPC commands*). Also unplug it from qFlipper or lab.flipper.net. |
| *Your Flipper has another app open* | Press **Back** on the Flipper until you're on its home screen. |
| *… wasn't sent. Transmission on this frequency is restricted …* | The Flipper's region or frequency settings block that frequency. The phone app would refuse too. |
| Window doesn't appear | Install a browser (setup step 2), or open the address it prints in any browser. |
| Window opens in the wrong browser | Set your default browser in System Settings, or add `"browser": "com.brave.Browser"` to `config.json`. |

Logs are in `~/.cache/flipper-remote/flipper-remote.log`. For a detailed log including the raw
Bluetooth traffic, run `flipper-remote --debug` (or `flipper-remote send … --debug`).

The log also records every change you make and what made it: each pin and unpin, *Pinned only*, each
step of pairing, and each signal sent. Every line names the input that did it (`touch@x,y` or
`mouse@x,y` with where on the window, `key:Enter` for a key, with `:held` if the key was held down, or
`controller`) and which window (`window 3fa2c1`). If something changes that you didn't change, those
lines show what did it:

```
grep -E "Pinned|Unpinned|Pairing|Window" ~/.cache/flipper-remote/flipper-remote.log | tail -40
```

### Pairing by hand

If you'd rather not use the app for pairing, `bluetoothctl` in Konsole works too:

```
bluetoothctl
agent off
agent KeyboardDisplay
default-agent
devices                      # if your Flipper is listed from an earlier try:  remove <its address>
scan on                      # wait for:  [NEW] Device 80:E1:26:xx:xx:xx Flipper <name>
scan off
pair 80:E1:26:xx:xx:xx       # at "Enter passkey", type the code the Flipper shows
trust 80:E1:26:xx:xx:xx
quit
```

(Type the lines one at a time; Tab completes the address.)

## How it works

The Flipper exposes a Bluetooth LE "serial" service that carries its protobuf RPC protocol, the one
the official apps use. For each send, Flipper Remote does exactly what the official app does:

1. Start the right app in remote mode (`app_start_request "Sub-GHz" "RPC"`, or "Infrared",
   "125 kHz RFID", "NFC", "iButton") and wait for it to report that it started.
2. Load the file (`app_load_file_request`). RFID, NFC and iButton start emulating right here.
3. Sub-GHz: press (`app_button_press_request`), hold for at least 500 ms (or the length of a RAW
   recording, or as long as you hold the button), then release. Infrared: press the remote's button by
   name and release it (the Flipper always finishes the signal it started, so a tap sends it once).
   RFID, NFC, iButton: wait until you tap again, 30 s pass, or someone presses BACK on the Flipper.
4. Close the app (`app_exit_request`).

It honours the Flipper's flow-control characteristic, so it never overruns the Flipper's 1 KB
receive buffer. It talks to BlueZ over the system D-Bus with a tiny built-in client, so there are no
Python packages to install.

### Firmware differences

The app asks the Flipper which firmware it runs when it connects, and adapts:

- **Unleashed:** while "send" is held, Unleashed keeps transmitting, and a RAW recording loops until
  you let go. So a tap on a RAW recording releases right away and waits for the recording to finish,
  which sends it exactly once. Holding a signal keeps it repeating, the same as holding the button on
  the Flipper. Short (non-RAW) codes are sent for half a second, as with the official app.
- **Official firmware:** the sequence above, unchanged.
- **Xtreme:** the Sub-GHz app is named "SubGHz" internally, but the official name works too (the app
  tries "SubGHz" if needed). Its release step reports an error and the signal stops when the app
  closes a moment later; that's expected, and the phone app behaves the same way. Xtreme also
  advertises the Flipper under its custom name, so the app finds Flippers by their Bluetooth service
  rather than by name.

## Files

| Path | What |
|---|---|
| `~/.local/share/flipper-remote/` | The app |
| `~/.config/flipper-remote/config.json` | Your pinned signals and window settings (*Pinned only*, dark mode, last folder). Optional: `"device": "AA:BB:…"` to pick a Flipper, `"browser": "com.brave.Browser"` to pick a browser other than your default |
| `~/.cache/flipper-remote/` | Folder listings, file info, log |
| `~/.var/app/com.brave.Browser/cache/flipper-remote/` | The window's own browser profile (for a Flatpak browser; otherwise it's in `~/.cache/flipper-remote/browser/`). Safe to delete |

Uninstall with `~/.local/share/flipper-remote/uninstall.sh` (add `--purge` to also delete pins,
cache and logs).
