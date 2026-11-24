#!/usr/bin/env python3
"""Flipper Remote's helper for the Decky plugin.

Decky runs the plugin's main.py in its own bundled Python. This helper runs with the Steam Deck's own Python
instead, the same one the Flipper Remote app uses, and drives the Flipper with the app's own code
(flipper_remote.py, next to this file). It talks to main.py over stdin/stdout, one JSON message per line:

  in:   {"id": 7, "method": "send", "params": {"path": "/ext/subghz/Fan_On.sub", "via": "controller:A"}}
  out:  {"id": 7, "ok": true, "result": {...}}      or      {"id": 7, "ok": false, "error": "..."}
  out:  {"event": "job", "data": {...}}               (live updates, whenever they happen)

The Flipper takes one Bluetooth connection at a time, and the app's windows and command-line tools share a
lock so that only one of them drives it. The helper takes that lock, the way a command-line tool does, only
while the panel is in use and for a short while after. So the Flipper Remote window can always get the Flipper
back: when the window is open, the panel says so and leaves the Flipper alone.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import flipper_remote as fr   # noqa: E402  (the app's own code, shipped next to this file)

PROTOCOL = sys.stdout   # the channel to main.py
sys.stdout = sys.stderr   # a stray print() goes to Decky's log instead of corrupting the channel

PLUGIN_VERSION = "1.1.0"
IDLE_RELEASE_S = 12       # like the app's window: let go of the Flipper this long after the panel was last open
HOLD_ORPHAN_S = 7         # a press-and-hold with the panel gone (no heartbeat this long) is let go
WATCH_EVERY_S = 1.0
FORWARDED = {"status", "job", "files", "files_error", "meta", "favorites", "settings", "pairing"}
BLOCKED_TEXT = {
    "window": "The Flipper Remote window is open, so it has the Flipper. Close it to use the panel.",
    "busy": fr.BUSY,
}

log = fr.log


class AppendLogHandler(logging.Handler):
    """Writes to the app's own log (flipper-remote.log), opening it for every line. This helper can run for
    days, and the app rotates that file when it runs, so holding it open would keep writing to an old copy."""
    MAX_BYTES = 512 * 1024   # the app's limit, with the same two old copies kept

    def __init__(self):
        super().__init__()
        self._io = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(threadName)s: %(message)s"))

    def emit(self, record):
        try:
            line = self.format(record) + "\n"
            with self._io:
                os.makedirs(fr.CACHE_DIR, exist_ok=True)
                try:
                    if os.path.getsize(fr.LOG_FILE) > self.MAX_BYTES:
                        if os.path.exists(fr.LOG_FILE + ".1"):
                            os.replace(fr.LOG_FILE + ".1", fr.LOG_FILE + ".2")
                        os.replace(fr.LOG_FILE, fr.LOG_FILE + ".1")
                except OSError:
                    pass
                with open(fr.LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:  # noqa: BLE001
            self.handleError(record)


def setup_logging():
    root = logging.getLogger(fr.APP_ID)
    root.setLevel(logging.INFO)
    root.addHandler(AppendLogHandler())
    err = logging.StreamHandler(sys.stderr)   # problems also show in Decky's own log for the plugin
    err.setLevel(logging.WARNING)
    err.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(err)


def _mtime(path: str):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _via(text) -> str:
    """How the panel says an action was set off (controller:A, touch, ...), cleaned up like the app does."""
    via = re.sub(r"[^A-Za-z0-9:@,._ -]", "", str(text or "?"))[:32] or "?"
    return f"{via}, panel"


def _saved(path, signal=None):
    """The saved file (and, for an Infrared remote, the button) a request is about. As strict as the app."""
    path = str(path or "")
    if ".." in path.split("/") or not fr.file_kind(path):
        raise fr.FlipperError("That isn't a saved Sub-GHz, Infrared, RFID, NFC or iButton file.")
    if fr.file_kind(path) == "infrared":
        if not isinstance(signal, str) or not signal.strip() or len(signal) > 100:
            raise fr.FlipperError("Which button of the remote?")
        return path, signal
    return path, None


class Helper:
    def __init__(self):
        self._out = threading.Lock()
        self._lock = threading.RLock()   # taking and letting go of the Flipper
        self.lock_fd = None              # the app's instance lock, while the panel has the Flipper
        self.live = False                # self.ctl is running and may use the Flipper
        self.blocked = None              # why the panel can't have the Flipper right now: "window", "busy"
        self.last_seen = 0.0             # when the panel last said it's open
        self.ctl = self._controller()    # not running: still reads and changes pins and settings
        self.config_mtime = _mtime(fr.CONFIG_FILE)
        self.cache_mtime = _mtime(fr.CACHE_FILE)
        self.closing = False
        threading.Thread(target=self._watch, name="panel-watch", daemon=True).start()

    # -- talking to main.py ----------------------------------------------------------------------
    def _write(self, msg: dict):
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        with self._out:
            try:
                PROTOCOL.write(line)
                PROTOCOL.flush()
            except (OSError, ValueError):
                pass   # main.py has gone; stdin closing ends this process soon

    def emit(self, event: str, data):
        self._write({"event": event, "data": data})

    def _on_controller_event(self, ctl, event: str, data):
        if event in FORWARDED and ctl is self.ctl:   # (not a controller that was just retired)
            self.emit(event, data)

    def handle(self, msg: dict):
        mid, method = msg.get("id"), str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        fn = getattr(self, "do_" + method, None) if re.fullmatch(r"[a-z_]+", method) else None
        try:
            if fn is None:
                raise fr.FlipperError(f"Unknown request “{method}”.")
            result = fn(**params)
            self._write({"id": mid, "ok": True, "result": result})
        except fr.FlipperError as e:
            self._write({"id": mid, "ok": False, "error": str(e)})
        except TypeError as e:
            log.warning("Panel: bad request %s: %s", method, e)
            self._write({"id": mid, "ok": False, "error": "The panel and its helper don't match. Reinstall the plugin."})
        except Exception as e:  # noqa: BLE001
            log.exception("Panel: %s failed", method)
            self._write({"id": mid, "ok": False, "error": f"Unexpected error: {e}"})

    # -- the Flipper: taking it, and letting go ----------------------------------------------------------
    def _controller(self):
        ctl = fr.Controller()
        ctl.add_listener(lambda event, data, c=ctl: self._on_controller_event(c, event, data))
        return ctl

    def _owner(self) -> dict:
        return {"live": self.live, "blocked": self.blocked}

    def _set_blocked(self, why):
        if why != self.blocked:
            self.blocked = why
            if why:
                log.info("Panel: can't use the Flipper now (%s)", "the window has it" if why == "window" else "busy")
            self.emit("owner", self._owner())

    def _take(self) -> bool:
        """Get the Flipper for the panel, if nobody else has it. Call with self._lock held."""
        if self.live:
            return True
        if self.closing:
            return False
        existing = fr.running_instance()
        if existing and not existing.get("quitting"):
            self._set_blocked("window")
            return False
        fd = fr._single_instance_lock("cli")   # as the app's command-line tools do: the window waits for us
        if fd is None:
            self._set_blocked("busy")
            return False
        self.lock_fd = fd
        self.ctl = self._controller()   # fresh: the pins and saved folders as they are on disk now
        self.config_mtime, self.cache_mtime = _mtime(fr.CONFIG_FILE), _mtime(fr.CACHE_FILE)
        self.ctl.start()
        self.live = True
        self.blocked = None
        log.info("Panel: opened; using the Flipper (plugin %s, app code %s)", PLUGIN_VERSION, fr.VERSION)
        self.emit("hello", self._hello())
        try:
            self.ctl.submit("connect")
        except fr.BusyError:
            pass
        return True

    def _live_ctl(self):
        """The running controller, taking the Flipper first if need be. Call with self._lock held."""
        self.last_seen = time.monotonic()
        if not self._take():
            raise fr.FlipperError(BLOCKED_TEXT.get(self.blocked, fr.BUSY))
        return self.ctl

    def _release(self, why: str):
        """Let go of the Flipper: disconnect, and free the lock for the window or the command line."""
        with self._lock:
            if not self.live:
                return
            ctl = self.ctl
            log.info("Panel: %s; letting go of the Flipper", why)
            self.live = False
            self.ctl = self._controller()   # retired controller's events are ignored from here on
            try:
                ctl.hold_stop()
                ctl.stop(disconnect=True)
            finally:
                fr._release_lock(self.lock_fd)
                self.lock_fd = None
            self.emit("status", self.ctl.snapshot()["status"])
            self.emit("owner", self._owner())

    def _watch(self):
        while not self.closing:
            time.sleep(WATCH_EVERY_S)
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("Panel: watcher")

    def _tick(self):
        now = time.monotonic()
        with self._lock:
            if not self.live:
                if now - self.last_seen < IDLE_RELEASE_S:
                    self._refresh_from_disk()   # the window may have changed the pins meanwhile
                return
            ctl = self.ctl
            with ctl._lock:
                emulating = set(ctl._emulating)
                held = [k for k in ctl._holds if k not in emulating]
                pending = ctl._pending_user_jobs
            pairing = ctl.pairing_active()   # each pairing step has its own time limit (two minutes at most)
            idle = now - self.last_seen
            if held and idle > HOLD_ORPHAN_S:
                log.info("Panel: gone while holding %s; letting go", ", ".join(held))
                for key in held:
                    ctl.hold_stop(key)
                return   # let the press wind down first
            if idle < IDLE_RELEASE_S or emulating or held or pending or pairing:
                return   # in use, or still finishing a press, an emulation or a pairing
            self._release(f"closed {int(idle)} s ago")

    def _refresh_from_disk(self):
        """While the panel doesn't have the Flipper, the window may change the pins: pick that up."""
        if self.live:
            return
        mtime = _mtime(fr.CONFIG_FILE)
        if mtime != self.config_mtime:
            self.config_mtime = mtime
            config = fr._load_json(fr.CONFIG_FILE, {})
            with self.ctl._lock:
                self.ctl.config = config
                favorites = self.ctl._favorites_view()
            self.emit("favorites", favorites)
            self.emit("settings", self.ctl.settings())
        mtime = _mtime(fr.CACHE_FILE)
        if mtime != self.cache_mtime:
            self.cache_mtime = mtime
            cache = fr._load_json(fr.CACHE_FILE, {})
            cache.setdefault("dirs", {})
            cache.setdefault("meta", {})
            with self.ctl._lock:
                self.ctl.cache = cache

    def shutdown(self):
        self.closing = True
        self._release("plugin unloading")

    # -- requests from the panel ---------------------------------------------------------------------
    def _hello(self) -> dict:
        snap = self.ctl.snapshot()
        snap["owner"] = self._owner()
        snap["plugin_version"] = PLUGIN_VERSION
        return snap

    def do_hello(self):
        with self._lock:
            self._refresh_from_disk()
            return self._hello()

    def do_heartbeat(self):
        """The panel (or its Browse page) is open. Takes the Flipper if it can, and connects."""
        with self._lock:
            self.last_seen = time.monotonic()
            self._refresh_from_disk()
            self._take()
            return self._owner()

    def do_closed(self):
        """The panel was closed: let go of any press-and-hold now. Emulations and pairing carry on (the panel
        shows where a pairing has got to when it's opened again)."""
        with self._lock:
            if self.live:
                with self.ctl._lock:
                    held = [k for k in self.ctl._holds if k not in self.ctl._emulating]
                for key in held:
                    self.ctl.hold_stop(key)
        return True

    def do_list(self, path: str = "", refresh: bool = False):
        path = "/" + str(path or "").strip("/")
        with self._lock:
            self.last_seen = time.monotonic()
            if self.live or self._take():
                return self.ctl.listing(path, bool(refresh))
            # Nobody to ask the Flipper: what was saved last time, if anything.
            self._refresh_from_disk()
            if path == fr.ALL_ROOT:
                return self.ctl.listing(path)
            kind = fr.kind_of(path)
            if not kind or ".." in path.split("/"):
                raise fr.FlipperError("Only the Sub-GHz, Infrared, RFID, NFC and iButton folders can be browsed.")
            cached = self.ctl.cache["dirs"].get(path)
            if cached:
                return dict(self.ctl._decorate(cached), offline=True)
            return {"path": path, "kind": kind, "dirs": None, "files": None, "remote": bool(fr.file_kind(path)),
                    "offline": True}

    def do_send(self, path: str = "", signal=None, via: str = ""):
        path, signal = _saved(path, signal)
        with self._lock:
            job = self._live_ctl().send(path, source=_via(via), signal=signal)
        return {"job": job.id if job else None}

    def do_hold(self, path: str = "", signal=None, action: str = "start", via: str = ""):
        path, signal = _saved(path, signal)
        with self._lock:
            if action == "start":
                job = self._live_ctl().hold_start(path, source=_via(via), signal=signal)
                return {"job": job.id if job else None}
            if self.live:
                self.ctl.hold_stop(fr.key_of(path, signal))
        return {"job": None}

    def do_stop(self, path: str = "", signal=None, via: str = ""):
        path, signal = _saved(path, signal)
        key = fr.key_of(path, signal)
        with self._lock:
            if self.live:
                if self.ctl._holds.get(key) is not None:
                    log.info("Stopping %s (%s)", key, _via(via))
                self.ctl.hold_stop(key)
        return True

    def do_set_favorite(self, path: str = "", signal=None, on: bool = True, label=None, via: str = ""):
        path, signal = _saved(path, signal)
        label = str(label)[:120] if label else None
        with self._lock:
            favorites = self.ctl.set_favorite(path, bool(on), label, _via(via), signal=signal)
            self.config_mtime = _mtime(fr.CONFIG_FILE)
        return favorites

    def do_set_settings(self, changes=None, via: str = ""):
        changes = changes if isinstance(changes, dict) else {}
        # The plugin always shows everything: it only remembers the last folder browsed. The window's Show choice
        # and its other settings are the window's own.
        allowed = {k: v for k, v in changes.items() if k == "folders"}
        with self._lock:
            settings = self.ctl.update_settings(allowed, _via(via))
            self.config_mtime = _mtime(fr.CONFIG_FILE)
        return settings

    def do_pair(self, action: str = "", address=None, remove=None, code=None, via: str = ""):
        """"Pair a Flipper" in the panel: the app's own pairing (the window's steps and safety), unchanged."""
        with self._lock:
            if action == "start":
                ctl = self._live_ctl()
                log.info("Pairing: “Pair a Flipper” opened (%s)", _via(via))
                ctl.pair_start()
                return True
            if action == "cancel":
                if self.live:
                    if self.ctl.pairing_active():
                        log.info("Pairing: cancelled in the panel (%s)", _via(via))
                    self.ctl.pair_cancel()
                return True
            if not self.live:
                raise fr.FlipperError("Pairing isn't running any more. Start it again.")
            self.last_seen = time.monotonic()
            if action == "choose":
                log.info("Pairing: a Flipper was picked in the list (%s)", _via(via))
                self.ctl.pair_choose(str(address or ""))
            elif action == "confirm":
                log.info("Pairing: %s (%s)", "chose to remove the old pairing and pair again" if remove
                         else "chose to keep the old pairing", _via(via))
                self.ctl.pair_confirm(bool(remove))
            elif action == "code":
                self.ctl.pair_code(str(code or ""))
            else:
                raise fr.FlipperError("Unknown pairing action.")
        return True

    def do_connect(self):
        with self._lock:
            self._live_ctl().submit("connect")
        return True

    def do_disconnect(self):
        with self._lock:
            if self.live:
                self.ctl.submit("disconnect")
        return True


def main() -> int:
    setup_logging()
    helper = Helper()
    log.info("Panel helper started (pid %d, Python %s)", os.getpid(), sys.version.split()[0])

    def on_term(*_):
        helper.shutdown()
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("Panel: unreadable request from the plugin")
            continue
        if isinstance(msg, dict):
            threading.Thread(target=helper.handle, args=(msg,), name=f"panel-{msg.get('method')}",
                             daemon=True).start()
    helper.shutdown()   # stdin closed: the plugin is unloading (or Decky stopped)
    log.info("Panel helper stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
