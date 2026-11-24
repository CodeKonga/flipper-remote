"""Flipper Remote: the Decky plugin's backend.

Decky runs this file in its own bundled Python. The real work happens in py_modules/flipper_helper.py, which runs
with the Steam Deck's own Python (the one the Flipper Remote app uses), so the plugin drives the Flipper with
exactly the app's code. This file starts that helper when the panel first needs it, passes the panel's requests
to it, and passes its live updates back to the panel as "flipper" events.
"""
import asyncio
import json
import os
import shutil

import decky

PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR") or os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(PLUGIN_DIR, "py_modules", "flipper_helper.py")
CALL_TIMEOUT_S = 45


def _deck_python():
    """The Steam Deck's own Python 3 (Decky's bundled one can't run a script on its own)."""
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin")


def _helper_env():
    env = dict(os.environ)
    # Decky is a PyInstaller bundle: its library path points at its own copies of system libraries,
    # which the Deck's Python mustn't load.
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE"):
        env.pop(key, None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class Plugin:
    def __init__(self):
        self.proc = None
        self.pending = {}        # request id -> (helper process, future)
        self.next_id = 0
        self.starting = None     # asyncio.Lock, made on first use (inside Decky's event loop)
        self.unloading = False

    # -- Decky's lifecycle ----------------------------------------------------------------------------
    async def _main(self):
        decky.logger.info("Flipper Remote plugin loaded (helper: %s)", HELPER)

    async def _unload(self):
        self.unloading = True
        await self._stop_helper()
        decky.logger.info("Flipper Remote plugin unloaded")

    async def _uninstall(self):
        pass   # pins and settings belong to the Flipper Remote app (~/.config/flipper-remote): they stay

    # -- the helper -------------------------------------------------------------------------------------
    async def _ensure_helper(self):
        if self.starting is None:
            self.starting = asyncio.Lock()
        async with self.starting:
            if self.proc is not None and self.proc.returncode is None:
                return self.proc
            python = _deck_python()
            if not python:
                raise Exception("Couldn't find the Steam Deck's Python 3 (/usr/bin/python3).")
            if not os.path.exists(HELPER):
                raise Exception("The plugin's helper is missing. Reinstall Flipper Remote.")
            proc = await asyncio.create_subprocess_exec(
                python, "-u", HELPER,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=_helper_env(), cwd=PLUGIN_DIR, limit=16 * 1024 * 1024)
            self.proc = proc
            decky.logger.info("Started the helper (pid %d) with %s", proc.pid, python)
            asyncio.get_running_loop().create_task(self._read_messages(proc))
            asyncio.get_running_loop().create_task(self._read_errors(proc))
            return proc

    async def _read_messages(self, proc):
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    decky.logger.warning("The helper said something unreadable: %r", line[:200])
                    continue
                if "event" in msg:
                    await decky.emit("flipper", msg["event"], msg.get("data"))
                    continue
                entry = self.pending.pop(msg.get("id"), None)
                if entry is not None and not entry[1].done():
                    if msg.get("ok"):
                        entry[1].set_result(msg.get("result"))
                    else:
                        entry[1].set_exception(Exception(msg.get("error") or "Something went wrong."))
        except asyncio.CancelledError:
            # Decky is stopping this plugin without unloading it first. Waiting for the helper here would wait
            # forever (it's waiting for its input to close), so end it: the lock goes with it.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise
        except Exception as e:  # noqa: BLE001
            decky.logger.error("Reading from the helper failed: %s", e)
        code = await proc.wait()   # its output ended: it has exited, or is about to
        if self.proc is proc:
            self.proc = None
        for mid, (owner, fut) in list(self.pending.items()):
            if owner is proc:
                self.pending.pop(mid, None)
                if not fut.done():
                    fut.set_exception(Exception("The plugin's helper stopped. Try again."))
        if not self.unloading:
            decky.logger.warning("The helper stopped (exit code %s)", code)
            await decky.emit("flipper", "helper_exit", {"code": code})

    async def _read_errors(self, proc):
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                decky.logger.warning("helper: %s", text)

    async def _stop_helper(self):
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.stdin.close()   # the helper lets go of the Flipper and exits
            await asyncio.wait_for(proc.wait(), 25)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _call(self, method, **params):
        proc = await self._ensure_helper()
        self.next_id += 1
        mid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = (proc, fut)
        try:
            proc.stdin.write((json.dumps({"id": mid, "method": method, "params": params}) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            return await asyncio.wait_for(fut, CALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise Exception("The plugin's helper didn't answer. Try again.") from None
        except (BrokenPipeError, ConnectionResetError):
            raise Exception("The plugin's helper stopped. Try again.") from None
        finally:
            self.pending.pop(mid, None)

    async def _api(self, method, **params):
        """Ordinary problems (the window has the Flipper, it's out of range...) come back as a result the panel
        shows, rather than as an exception that Decky would also log with a traceback."""
        try:
            return {"ok": True, "result": await self._call(method, **params)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e) or "Something went wrong."}

    # -- called by the panel ------------------------------------------------------------------------------
    async def hello(self):
        return await self._api("hello")

    async def heartbeat(self):
        return await self._api("heartbeat")

    async def closed(self):
        if self.proc is None:
            return {"ok": True, "result": True}   # nothing running, so nothing to let go of
        return await self._api("closed")

    async def list_folder(self, path, refresh=False):
        return await self._api("list", path=path, refresh=bool(refresh))

    async def send(self, path, signal=None, via=""):
        return await self._api("send", path=path, signal=signal, via=via)

    async def hold(self, path, signal=None, action="start", via=""):
        return await self._api("hold", path=path, signal=signal, action=action, via=via)

    async def stop(self, path, signal=None, via=""):
        return await self._api("stop", path=path, signal=signal, via=via)

    async def set_favorite(self, path, signal=None, on=True, label=None, via=""):
        return await self._api("set_favorite", path=path, signal=signal, on=bool(on), label=label, via=via)

    async def set_settings(self, changes=None, via=""):
        return await self._api("set_settings", changes=changes or {}, via=via)

    async def pair(self, action, address=None, remove=None, code=None, via=""):
        return await self._api("pair", action=action, address=address, remove=remove, code=code, via=via)

    async def connect(self):
        return await self._api("connect")

    async def disconnect(self):
        return await self._api("disconnect")
