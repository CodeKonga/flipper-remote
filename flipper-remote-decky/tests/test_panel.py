"""The Quick Access panel, the Browse page and the "Pair a Flipper" dialog, as built (dist/index.js), run in
Chrome with stand-ins for Steam's components (tests/harness), against the plugin's real backend and the mock
Flipper. Checks what reaches the Flipper for taps and holds (touch and controller, whichever order Steam reports
a press in), emulation, pinning, that the plugin always shows everything, pairing, and the panel giving way to
the Flipper Remote window."""
import asyncio
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import expect, sync_playwright

from test_backend import (CONFIG, INSTANCE, LOG, ROOT, _wait, decky, lock_holder, mock, plugin_main,  # noqa: F401
                          start_window)

HARNESS = os.path.join(ROOT, "tests", "harness")
STATIC = {
    "/": os.path.join(HARNESS, "index.html"),
    "/static/react.js": os.path.join(HARNESS, "node_modules/react/umd/react.development.js"),
    "/static/react-dom.js": os.path.join(HARNESS, "node_modules/react-dom/umd/react-dom.development.js"),
    "/static/standins.js": os.path.join(HARNESS, "standins.js"),
    "/static/index.js": os.path.join(ROOT, "dist", "index.js"),
}
SHOTS = os.path.join(ROOT, "tests", "shots")


class Backend:
    """The plugin's main.py in its own event loop, as Decky runs it."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.plugin = plugin_main.Plugin()
        self.run(self.plugin._main())

    def run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self):
        self.run(self.plugin._unload())
        self.loop.call_soon_threadsafe(self.loop.stop)


def serve(backend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, ctype="application/json", code=200):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/favicon.ico":
                self._send(b"", "image/x-icon", 204)
                return
            if path == "/events":
                since = int(query.split("since=")[1]) if "since=" in query else 0
                evs = decky.EVENTS[since:]
                self._send(json.dumps({"events": [[e, list(a)] for e, a in evs], "next": since + len(evs)}))
                return
            f = STATIC.get(path)
            if not f:
                self._send("not found", "text/plain", 404)
                return
            with open(f, "rb") as fh:
                self._send(fh.read(), "text/html" if f.endswith(".html") else "text/javascript")

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            try:
                self._send(json.dumps({"result": backend.run(getattr(backend.plugin, req["name"])(*req["args"]))}))
            except Exception as e:  # noqa: BLE001
                self._send(json.dumps({"exception": str(e)}))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def screen(mock):
    backend = Backend()
    srv = serve(backend)
    errors = []
    with sync_playwright() as p:
        chrome = "/usr/bin/google-chrome"   # Chrome if it's installed, else Playwright's own Chromium
        browser = p.chromium.launch(executable_path=chrome if os.path.exists(chrome) else None, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        page.wait_for_function("window.harnessReady === true")
        yield page, errors
        browser.close()
    srv.shutdown()
    backend.close()


def sends(mock, name):
    return [t for t in mock.tx_log if t[0].endswith("/" + name)]


def press_a(page, button, hold_ms=60, click_on="up"):
    page.evaluate("([el, ms, on]) => harness.pressA(el, ms, on)", [button.element_handle(), hold_ms, click_on])


def shot(page, name):
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def test_panel_browse_pin_send_hold_emulate(screen, mock):
    page, errors = screen
    qam, pg = page.locator("#qam"), page.locator("#page")

    # Opening the Quick Access panel connects; nothing is pinned yet. No Show menu: the plugin shows everything.
    page.evaluate("harness.showQAM(true)")
    expect(qam).to_contain_text("Flipper Wizard", timeout=15000)
    expect(qam).to_contain_text("Connected, battery")
    expect(qam).to_contain_text("Nothing is pinned yet. Open Browse files and pin the ones you use.")
    expect(page.locator("select")).to_have_count(0)
    expect(qam.locator("button").last).to_have_text("Pair a Flipper")   # at the bottom
    shot(page, "1-panel-empty")

    # Browse files: the panel closes and the full page opens on "All".
    qam.get_by_role("button", name="Browse files").click()
    expect(qam).to_be_hidden()
    expect(pg).to_contain_text("Flipper Remote")
    expect(pg).to_contain_text("All. Tap to send or emulate. Hold Sub-GHz and Infrared to keep sending.")
    expect(page.locator("select")).to_have_count(0)
    for title in ("Sub-GHz", "Infrared", "RFID 125 kHz", "NFC", "iButton"):
        expect(pg.get_by_role("button", name=title, exact=True)).to_be_visible()
    pg.get_by_role("button", name="Sub-GHz", exact=True).click()
    expect(pg.get_by_role("button", name="Fan", exact=True)).to_be_visible(timeout=10000)
    pg.get_by_role("button", name="Fan", exact=True).click()
    fan_off = pg.locator(".fr-row", has_text="Fan Off")
    expect(fan_off).to_be_visible(timeout=10000)
    expect(fan_off).to_contain_text("433.92 MHz", timeout=10000)   # read in the background, like the window
    expect(pg).to_contain_text("Sub-GHz  ›  Fan")

    # Pin it.
    fan_off.get_by_role("button", name="Pin").click()
    expect(fan_off.get_by_role("button", name="Pinned")).to_be_visible()

    # A mouse or touch tap sends once.
    send = fan_off.locator("button").first
    send.click()
    assert _wait(lambda: len(sends(mock, "Fan_Off.sub")) == 1, 10)
    expect(send).to_contain_text("Sent")
    time.sleep(0.6)
    assert len(sends(mock, "Fan_Off.sub")) == 1

    # Pressed and held, it keeps sending until let go, and the click that comes with letting go sends nothing.
    high = pg.locator(".fr-row", has_text="Fan High").locator("button").first
    high.hover()
    page.mouse.down()
    expect(high).to_contain_text("Sending, let go to stop", timeout=5000)
    page.wait_for_timeout(1400)
    page.mouse.up()
    assert _wait(lambda: len(sends(mock, "Fan_High.sub")) == 1, 10)
    time.sleep(1.0)
    held = sends(mock, "Fan_High.sub")
    assert len(held) == 1 and held[0][2] - held[0][1] >= 1.0, held

    # The controller: a tap is one send whether Steam reports its click on press or on release...
    press_a(page, high, 60, "up")
    assert _wait(lambda: len(sends(mock, "Fan_High.sub")) == 2, 10)
    press_a(page, high, 60, "down")
    assert _wait(lambda: len(sends(mock, "Fan_High.sub")) == 3, 10)
    # ...and once it has reported a release, holding A keeps sending: still one transmission per press.
    press_a(page, high, 1500, "up")
    assert _wait(lambda: len(sends(mock, "Fan_High.sub")) == 4, 10)
    press_a(page, high, 1500, "down")
    assert _wait(lambda: len(sends(mock, "Fan_High.sub")) == 5, 10)
    time.sleep(1.2)
    high_sends = sends(mock, "Fan_High.sub")
    assert len(high_sends) == 5, high_sends
    assert high_sends[3][2] - high_sends[3][1] >= 1.0 and high_sends[4][2] - high_sends[4][1] >= 1.0

    # An Infrared remote opens like a folder; pin one of its buttons.
    pg.get_by_role("button", name="Up", exact=True).click()
    pg.get_by_role("button", name="Up", exact=True).click()
    pg.get_by_role("button", name="Infrared", exact=True).click()
    tv = pg.get_by_role("button", name="TV")
    expect(tv).to_be_visible(timeout=10000)
    expect(tv).to_contain_text("buttons", timeout=10000)
    tv.click()
    power = pg.locator(".fr-row", has_text="Power").first
    expect(power).to_be_visible(timeout=10000)
    power.get_by_role("button", name="Pin").click()
    expect(power.get_by_role("button", name="Pinned")).to_be_visible()
    shot(page, "2-browse-remote")

    # A card: tap to emulate, with its countdown; tap again to stop.
    pg.get_by_role("button", name="Up", exact=True).click()
    pg.get_by_role("button", name="Up", exact=True).click()
    pg.get_by_role("button", name="NFC", exact=True).click()
    badge = pg.locator(".fr-row", has_text="Badge").locator("button").first
    expect(badge).to_be_visible(timeout=10000)
    badge.click()
    expect(badge).to_contain_text("Emulating, 3", timeout=10000)
    expect(badge).to_contain_text("s. Tap to stop")
    shot(page, "3-browse-emulating")
    badge.click()
    expect(badge).to_contain_text("Stopped", timeout=10000)
    assert _wait(lambda: len(mock.emu_log) == 1, 5) and mock.emu_log[0][0] == "/ext/nfc/Badge.nfc"

    # Back to the panel: both pins are there; tapping one sends it.
    page.evaluate("harness.goHome(); harness.showQAM(true)")
    pins = qam.locator(".panel-section", has_text="Pinned")
    expect(pins).to_contain_text("Fan Off")
    expect(pins).to_contain_text("TV Power")
    expect(pins).to_contain_text("Tap to send or emulate. Hold Sub-GHz and Infrared to keep sending.")
    shot(page, "4-panel-pins")
    pins.get_by_role("button", name="Fan Off").click()
    assert _wait(lambda: len(sends(mock, "Fan_Off.sub")) == 2, 10)

    # Pins are saved for the window too. The plugin only remembers the last folder browsed; it leaves the
    # window's Show choice alone.
    saved = json.load(open(CONFIG))
    assert saved["favorites"] == [{"path": "/ext/subghz/Fan/Fan_Off.sub", "label": "Fan Off"},
                                  {"path": "/ext/infrared/TV.ir", "label": "TV Power", "signal": "Power"}], saved
    assert saved["ui"] == {"folders": {"all": "/ext/nfc"}}, saved["ui"]

    # The window's Show choice doesn't hide anything here: every pin is still listed.
    saved["ui"]["show"] = "infrared"
    with open(CONFIG, "w") as f:
        json.dump(saved, f)
    page.evaluate("harness.showQAM(false)")
    page.evaluate("harness.showQAM(true)")
    expect(pins).to_contain_text("Fan Off")
    expect(pins).to_contain_text("TV Power")

    # Browse files opens where it was left.
    qam.get_by_role("button", name="Browse files").click()
    expect(pg).to_contain_text("All  ›  NFC")
    expect(pg.locator(".fr-row", has_text="Badge")).to_be_visible(timeout=10000)
    assert not errors, "\n\n".join(errors)


def test_panel_gives_way_to_the_window(screen, mock):
    page, errors = screen
    qam = page.locator("#qam")
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump({"favorites": [{"path": "/ext/subghz/Fan/Fan_Off.sub", "label": "Fan Off"}]}, f)
    window = start_window(18741)
    try:
        assert _wait(lambda: os.path.exists(INSTANCE) and lock_holder()[1] == "ui", 20)
        page.evaluate("harness.showQAM(true)")
        expect(qam).to_contain_text("In use by the window", timeout=10000)
        expect(qam).to_contain_text("Close it to use the panel.")
        fan_off = qam.get_by_role("button", name="Fan Off")
        expect(fan_off).to_be_disabled()
        expect(qam.get_by_role("button", name="Pair a Flipper")).to_be_disabled()
        shot(page, "5-panel-window-open")
        window.send_signal(signal.SIGTERM)   # the window closes: the panel takes over at its next heartbeat
        window.wait(25)
        expect(qam).to_contain_text("Flipper Wizard", timeout=15000)
        expect(fan_off).to_be_enabled()
        fan_off.click()
        assert _wait(lambda: len(sends(mock, "Fan_Off.sub")) == 1, 10)
    finally:
        if window.poll() is None:
            window.kill()
    assert not errors, "\n\n".join(errors)


def test_pair_a_flipper_from_the_panel(screen, mock):
    page, errors = screen
    mock.paired = False
    qam, dialog = page.locator("#qam"), page.locator(".modal-root")

    page.evaluate("harness.showQAM(true)")
    expect(qam).to_contain_text("Not connected", timeout=20000)   # not paired yet
    qam.get_by_role("button", name="Pair a Flipper").click()

    # The dialog: the window's steps and words. The panel closes, so the dialog is all there is on screen.
    expect(dialog).to_be_visible()
    expect(qam).to_be_hidden()
    expect(dialog).to_contain_text("Pair a Flipper")
    expect(dialog).to_contain_text("On the Flipper, turn Bluetooth on (Settings → Bluetooth) and go back to its "
                                   "main screen. Turn off your phone's Bluetooth while you pair.")
    flipper = dialog.locator(".fr-dev", has_text="Flipper Wizard")
    expect(flipper).to_be_visible(timeout=30000)
    expect(flipper).to_be_focused()   # the Flipper found has the focus, so A picks it
    shot(page, "6-pairing-pick")
    press_a(page, flipper)

    # The number pad: the focus starts on it; a wrong digit can be deleted; six digits move the focus to OK.
    expect(dialog).to_contain_text("Type the 6-digit code shown on Flipper Wizard. You have about 30 seconds.",
                                   timeout=30000)
    keys = dialog.locator(".fr-key")
    expect(keys).to_have_count(12)
    expect(keys.first).to_be_focused()
    ok = dialog.locator(".fr-ok")
    expect(ok).to_be_disabled()
    code = mock.displayed_code
    dialog.get_by_role("button", name="9" if code[0] != "9" else "8", exact=True).click()
    dialog.locator(".fr-del").click()
    for digit in code[:5]:
        dialog.get_by_role("button", name=digit, exact=True).click()
    expect(ok).to_be_disabled()
    page.keyboard.press(code[5])   # a keyboard works too
    expect(dialog.locator(".fr-slot")).to_have_text(list(code))
    expect(ok).to_be_enabled()
    expect(ok).to_be_focused()
    shot(page, "7-pairing-code")
    press_a(page, ok)
    expect(dialog).to_contain_text("Paired with Flipper Wizard. It's ready to use.", timeout=30000)
    done = dialog.get_by_role("button", name="Done")
    expect(done).to_be_focused()
    shot(page, "8-pairing-done")
    assert mock.paired and mock.trusted
    done.click()
    expect(dialog).to_have_count(0)
    page.evaluate("harness.showQAM(true)")
    expect(qam).to_contain_text("Connected, battery", timeout=20000)

    # Again, now that it's paired: a pairing that works is left alone.
    qam.get_by_role("button", name="Pair a Flipper").click()
    expect(flipper).to_contain_text("Already paired with this Deck.", timeout=30000)
    flipper.click()
    expect(dialog).to_contain_text("Flipper Wizard is already paired with this Deck and it connects fine, so nothing "
                                   "was changed.", timeout=45000)
    dialog.get_by_role("button", name="Done").click()
    expect(dialog).to_have_count(0)
    assert mock.paired

    # The B button cancels.
    page.evaluate("harness.showQAM(true)")
    qam.get_by_role("button", name="Pair a Flipper").click()
    expect(flipper).to_be_visible(timeout=30000)
    assert page.evaluate("harness.pressB()")
    expect(dialog).to_have_count(0)
    assert _wait(lambda: "Pairing: cancelled in the panel (controller:B, panel)" in open(LOG).read(), 10)
    page.evaluate("harness.showQAM(true)")
    expect(qam.get_by_role("button", name="Pair a Flipper")).to_be_enabled()
    expect(qam).to_contain_text("Connected, battery", timeout=20000)
    text = open(LOG).read()
    assert "Pairing: “Pair a Flipper” opened (mouse, panel)" in text
    assert mock.paired
    assert not errors, "\n\n".join(errors)
