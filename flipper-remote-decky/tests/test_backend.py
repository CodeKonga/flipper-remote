"""The Decky plugin's backend: main.py (with a stand-in for Decky's `decky` module) and the helper it starts,
against the mock Flipper from the app's own test suite. Also checks the plugin and the Flipper Remote window
take turns with the Flipper."""
import asyncio
import fcntl
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the plugin's source folder
REPO = os.path.dirname(ROOT)                                           # the folder with the app and its tests
APP = os.path.join(REPO, "flipper-remote", "flipper_remote.py")        # the Desktop app, as installed
sys.path.insert(0, os.path.join(REPO, "tests"))                        # the app's mock Flipper
sys.path.insert(0, os.path.join(ROOT, "tests", "stand_in"))

TMP = tempfile.mkdtemp(prefix="frd-test-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "config")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "cache")
os.environ["DECKY_PLUGIN_DIR"] = ROOT

from mock_flipper import IBUTTON_KEY, IR_REMOTE, NFC_CARD, RFID_KEY, MockFlipper  # noqa: E402

import decky  # noqa: E402  (the stand-in)

_spec = importlib.util.spec_from_file_location("plugin_main", os.path.join(ROOT, "main.py"))
plugin_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin_main)

CONFIG = os.path.join(os.environ["XDG_CONFIG_HOME"], "flipper-remote", "config.json")
CACHE_DIR = os.path.join(os.environ["XDG_CACHE_HOME"], "flipper-remote")
LOCK = os.path.join(CACHE_DIR, "instance.lock")
INSTANCE = os.path.join(CACHE_DIR, "instance.json")
LOG = os.path.join(CACHE_DIR, "flipper-remote.log")


@pytest.fixture
def mock():
    for d in (os.environ["XDG_CONFIG_HOME"], os.environ["XDG_CACHE_HOME"]):
        shutil.rmtree(d, ignore_errors=True)
    m = MockFlipper(firmware="ofw", name="Flipper Wizard")
    m.fs.update({
        "/ext/infrared": None, "/ext/infrared/TV.ir": IR_REMOTE,
        "/ext/nfc": None, "/ext/nfc/Badge.nfc": NFC_CARD,
        "/ext/lfrfid": None, "/ext/lfrfid/Gym.rfid": RFID_KEY,
        "/ext/ibutton": None, "/ext/ibutton/Intercom.ibtn": IBUTTON_KEY,
    })
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = m.start()
    decky.EVENTS.clear()
    yield m
    m.stop()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 120))


class Panel:
    """Calls the plugin the way the panel does, unwrapping its {"ok", "result" | "error"} answers."""
    def __init__(self, plugin):
        self.plugin = plugin

    def __getattr__(self, name):
        method = getattr(self.plugin, name)

        async def call(*args):
            res = await method(*args)
            assert isinstance(res, dict) and "ok" in res, res
            if not res["ok"]:
                raise Exception(res["error"])
            return res["result"]
        return call


async def until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(0.05)
    return check()


def events(name):
    return [args[1] for ev, args in decky.EVENTS if ev == "flipper" and args[0] == name]


def jobs_for(key):
    return [j for j in events("job") if j.get("key") == key]


def lock_holder():
    try:
        with open(LOCK) as f:
            pid, role = f.read().split()[:2]
        return int(pid), role
    except (OSError, ValueError):
        return 0, ""


def lock_free() -> bool:
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def start_window(port: int):
    """The Flipper Remote app, as its window runs it (without opening a browser)."""
    return subprocess.Popen([sys.executable, APP, "ui", "--no-browser", "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())


def test_panel_sends_holds_emulates_pins_and_lets_go(mock):
    async def scenario():
        plugin = plugin_main.Plugin()
        await plugin._main()
        p = Panel(plugin)
        snap = await p.hello()
        assert snap["status"]["state"] == "idle"
        assert snap["owner"] == {"live": False, "blocked": None}
        assert snap["settings"]["show"] == "all" and snap["favorites"] == []
        assert snap["version"] == "1.2.2" and snap["plugin_version"] == "1.1.0"

        # Opening the panel takes the Flipper, as a command-line tool would, and connects.
        owner = await p.heartbeat()
        assert owner == {"live": True, "blocked": None}
        assert lock_holder() == (plugin.proc.pid, "cli")
        assert await until(lambda: any(s["state"] == "connected" for s in events("status")))
        assert events("hello"), "the panel is sent a fresh snapshot once it has the Flipper"

        # Folders come back as live updates.
        listing = await p.list_folder("/ext/subghz")
        assert listing["path"] == "/ext/subghz"
        assert await until(lambda: any(f["path"] == "/ext/subghz" for f in events("files")))
        files = next(f for f in events("files") if f["path"] == "/ext/subghz")
        assert files["dirs"] == ["Fan", "Lights"] and files["kind"] == "subghz"

        # A tap sends once.
        res = await p.send("/ext/subghz/Fan/Fan_Off.sub", None, "controller:A")
        assert res["job"]
        assert await until(lambda: any(j["phase"] == "done" for j in jobs_for("/ext/subghz/Fan/Fan_Off.sub")))
        assert [t[0] for t in mock.tx_log] == ["/ext/subghz/Fan/Fan_Off.sub"]

        # Holding an Infrared button keeps it pressed until it's let go.
        await p.hold("/ext/infrared/TV.ir", "Power", "start", "touch")
        assert await until(lambda: mock.ir_on is not None)
        await asyncio.sleep(0.8)
        await p.hold("/ext/infrared/TV.ir", "Power", "stop", "touch")
        assert await until(lambda: len(mock.ir_log) == 1)
        remote, button, t0, t1 = mock.ir_log[0]
        assert (remote, button) == ("/ext/infrared/TV.ir", "Power") and t1 - t0 >= 0.6

        # A card: tap to emulate (with its countdown), tap again to stop.
        await p.send("/ext/nfc/Badge.nfc", None, "touch")
        assert await until(lambda: any(j["phase"] == "emulating" for j in jobs_for("/ext/nfc/Badge.nfc")))
        emulating = next(j for j in jobs_for("/ext/nfc/Badge.nfc") if j["phase"] == "emulating")
        assert emulating["seconds"] == 30
        await p.stop("/ext/nfc/Badge.nfc", None, "touch")
        assert await until(lambda: any(j["phase"] == "done" for j in jobs_for("/ext/nfc/Badge.nfc")))
        assert mock.emu_log and mock.emu_log[0][0] == "/ext/nfc/Badge.nfc"

        # Pins go into the app's own settings file, so the window shows them too.
        favs = await p.set_favorite("/ext/subghz/Fan/Fan_Off.sub", None, True, "Fan Off", "controller:A")
        assert [f["key"] for f in favs] == ["/ext/subghz/Fan/Fan_Off.sub"]
        favs = await p.set_favorite("/ext/infrared/TV.ir", "Power", True, "TV Power", "touch")
        assert [f["key"] for f in favs] == ["/ext/subghz/Fan/Fan_Off.sub", "/ext/infrared/TV.ir#Power"]
        with open(CONFIG) as f:
            saved = json.load(f)
        assert saved["favorites"] == [{"path": "/ext/subghz/Fan/Fan_Off.sub", "label": "Fan Off"},
                                      {"path": "/ext/infrared/TV.ir", "label": "TV Power", "signal": "Power"}]
        assert events("favorites")[-1] == favs
        favs = await p.set_favorite("/ext/infrared/TV.ir", "Power", False, None, "touch")
        assert [f["key"] for f in favs] == ["/ext/subghz/Fan/Fan_Off.sub"]

        # The plugin always shows everything: it only remembers the last folder browsed (under "All"), and can't
        # change the window's Show choice or its other settings.
        settings = await p.set_settings({"show": "nfc", "pinned_only": True, "theme": "light",
                                         "folders": {"all": "/ext/infrared"}}, "browse")
        assert settings["show"] == "all" and settings["pinned_only"] is False and settings["theme"] is None
        assert settings["folders"]["all"] == "/ext/infrared"
        assert events("settings")[-1]["folders"]["all"] == "/ext/infrared"
        with open(CONFIG) as f:
            ui = json.load(f)["ui"]
        assert ui == {"folders": {"all": "/ext/infrared"}}, ui

        # Bad requests are refused the way the window refuses them.
        with pytest.raises(Exception, match="isn't a saved"):
            await p.send("/ext/subghz/../../etc/passwd", None, "x")
        with pytest.raises(Exception, match="Which button"):
            await p.send("/ext/infrared/TV.ir", None, "x")

        # Unloading lets go of the Flipper.
        helper_pid = plugin.proc.pid
        await plugin._unload()
        assert lock_free()
        assert not os.path.exists(f"/proc/{helper_pid}") or open(f"/proc/{helper_pid}/stat").read().split()[2] == "Z"

    run(scenario())
    with open(LOG) as f:
        text = f.read()
    assert "Panel: opened; using the Flipper (plugin 1.1.0, app code 1.2.2)" in text
    assert "(controller:A, panel)" in text and "Pinned /ext/subghz/Fan/Fan_Off.sub (controller:A, panel)" in text
    assert "Panel: plugin unloading; letting go of the Flipper" in text


def test_panel_lets_go_of_the_flipper_after_it_closes(mock):
    async def scenario():
        plugin = plugin_main.Plugin()
        await plugin._main()
        p = Panel(plugin)
        await p.heartbeat()
        assert await until(lambda: any(s["state"] == "connected" for s in events("status")))
        assert not lock_free()
        t0 = time.monotonic()
        assert await until(lambda: any(o == {"live": False, "blocked": None} for o in events("owner")), timeout=25)
        waited = time.monotonic() - t0
        assert 11 <= waited <= 16, waited
        assert lock_free()
        assert events("status")[-1]["state"] == "idle"
        assert await until(lambda: not mock.device._connected, timeout=5)
        await plugin._unload()

    run(scenario())


def test_open_window_has_the_flipper_and_the_panel_waits_its_turn(mock):
    window = start_window(18731)
    try:
        assert _wait(lambda: os.path.exists(INSTANCE) and lock_holder()[1] == "ui", 20)

        async def scenario():
            plugin = plugin_main.Plugin()
            await plugin._main()
            p = Panel(plugin)
            owner = await p.heartbeat()
            assert owner == {"live": False, "blocked": "window"}
            with pytest.raises(Exception, match="The Flipper Remote window is open"):
                await p.send("/ext/subghz/Fan/Fan_Off.sub", None, "touch")
            # Browsing still shows what was seen before (nothing yet here), without asking the Flipper.
            listing = await p.list_folder("/ext/subghz")
            assert listing["offline"] is True and listing["files"] is None
            # Pairing waits too: the window has its own "Pair a Flipper".
            with pytest.raises(Exception, match="The Flipper Remote window is open"):
                await p.pair("start", None, None, None, "touch")
            # Pins can still be changed: the settings file is shared, and changes are merged.
            favs = await p.set_favorite("/ext/subghz/Fan/Fan_Off.sub", None, True, "Fan Off", "touch")
            assert len(favs) == 1

            window.send_signal(signal.SIGTERM)   # the window closes
            assert await until(lambda: window.poll() is not None, 25)
            owner = await p.heartbeat()
            assert owner == {"live": True, "blocked": None}
            assert lock_holder() == (plugin.proc.pid, "cli")
            await p.send("/ext/subghz/Fan/Fan_Off.sub", None, "touch")
            assert await until(lambda: len(mock.tx_log) == 1)
            await plugin._unload()

        run(scenario())
    finally:
        if window.poll() is None:
            window.kill()


def test_window_opened_while_the_panel_has_the_flipper_gets_it_after(mock):
    async def scenario():
        plugin = plugin_main.Plugin()
        await plugin._main()
        p = Panel(plugin)
        await p.heartbeat()
        assert await until(lambda: any(s["state"] == "connected" for s in events("status")))
        window = start_window(18732)
        try:
            await asyncio.sleep(3)
            assert window.poll() is None and not os.path.exists(INSTANCE), "the window waits for the panel"
            assert await until(lambda: os.path.exists(INSTANCE) and lock_holder()[1] == "ui", 30)
            assert any(o["live"] is False for o in events("owner"))
            owner = await p.heartbeat()   # the panel opened again: the window has it now
            assert owner == {"live": False, "blocked": "window"}
        finally:
            window.send_signal(signal.SIGTERM)
            window.wait(25)
        await plugin._unload()

    run(scenario())


def test_helper_that_dies_is_started_again(mock):
    async def scenario():
        plugin = plugin_main.Plugin()
        await plugin._main()
        p = Panel(plugin)
        await p.hello()
        first = plugin.proc.pid
        os.kill(first, signal.SIGKILL)
        assert await until(lambda: events("helper_exit"))
        snap = await p.hello()
        assert snap["status"]["state"] == "idle" and plugin.proc.pid != first
        await plugin._unload()

    run(scenario())


def _wait(check, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.1)
    return check()


def test_plugin_stopped_without_unloading_ends_its_helper(mock):
    seen = {}

    async def scenario():
        plugin = plugin_main.Plugin()
        await plugin._main()
        p = Panel(plugin)
        await p.heartbeat()
        assert await until(lambda: any(s["state"] == "connected" for s in events("status")))
        seen["pid"] = plugin.proc.pid
        # ...and Decky's event loop just stops (no _unload): its tasks get cancelled.

    t0 = time.monotonic()
    run(scenario())
    assert time.monotonic() - t0 < 30, "stopping must not hang"
    assert _wait(lock_free, 10), "the Flipper is free for the window"
    assert _wait(lambda: not os.path.exists(f"/proc/{seen['pid']}") or
                 open(f"/proc/{seen['pid']}/stat").read().split()[2] == "Z", 10)


def pairing_steps():
    return [d["step"] for d in events("pairing")]


def test_pair_a_flipper_from_the_panel(mock):
    mock.paired = False

    async def scenario():
        plugin = plugin_main.Plugin()
        await plugin._main()
        p = Panel(plugin)
        assert await p.pair("start", None, None, None, "controller:A") is True
        assert lock_holder() == (plugin.proc.pid, "cli")
        assert await until(lambda: any(any(d["nearby"] for d in ev.get("devices", [])) for ev in events("pairing")),
                           30)
        found = next(ev for ev in events("pairing") if any(d["nearby"] for d in ev.get("devices", [])))
        flipper = next(d for d in found["devices"] if d["nearby"])
        assert flipper["name"] == "Flipper Wizard" and flipper["paired"] is False
        await p.pair("choose", flipper["address"], None, None, "controller:A")
        assert await until(lambda: "code" in pairing_steps(), 30)
        with pytest.raises(Exception, match="Type all 6 digits of the code"):
            await p.pair("code", None, None, "12x", "")
        await p.pair("code", None, None, mock.displayed_code, "")
        assert await until(lambda: "done" in pairing_steps(), 30)
        assert mock.paired and mock.trusted
        assert await until(lambda: events("status")[-1]["state"] == "connected", 30)
        # It's ready to use straight away.
        await p.send("/ext/subghz/Fan/Fan_Off.sub", None, "touch")
        assert await until(lambda: len(mock.tx_log) == 1)
        await plugin._unload()

    run(scenario())
    with open(LOG) as f:
        text = f.read()
    assert "Pairing: “Pair a Flipper” opened (controller:A, panel)" in text
    assert "Pairing: a Flipper was picked in the list (controller:A, panel)" in text


def test_pairing_keeps_the_flipper_while_the_panel_is_closed_and_cancel_stops_it(mock):
    mock.paired = False

    async def scenario():
        plugin = plugin_main.Plugin()
        await plugin._main()
        p = Panel(plugin)
        await p.pair("start", None, None, None, "touch")
        assert await until(lambda: any(any(d["nearby"] for d in ev.get("devices", [])) for ev in events("pairing")),
                           30)
        found = next(ev for ev in events("pairing") if any(d["nearby"] for d in ev.get("devices", [])))
        await p.pair("choose", found["devices"][0]["address"], None, None, "touch")
        assert await until(lambda: "code" in pairing_steps(), 30)
        # No heartbeats for longer than the panel normally waits before letting go: pairing carries on.
        await asyncio.sleep(14)
        assert lock_holder() == (plugin.proc.pid, "cli")
        assert not any(o["live"] is False for o in events("owner"))
        assert "cancelled" not in pairing_steps() and "error" not in pairing_steps()
        # Cancel ends it, and then the Flipper is let go as usual.
        assert await p.pair("cancel", None, None, None, "controller:B") is True
        assert await until(lambda: "cancelled" in pairing_steps(), 10)
        assert not mock.paired
        assert await until(lambda: any(o == {"live": False, "blocked": None} for o in events("owner")), 25)
        assert lock_free()
        # Steps sent after it's over are refused plainly.
        with pytest.raises(Exception, match="Pairing isn't running any more"):
            await p.pair("code", None, None, "123456", "")
        await plugin._unload()

    run(scenario())
    with open(LOG) as f:
        text = f.read()
    assert "Pairing: cancelled in the panel (controller:B, panel)" in text
