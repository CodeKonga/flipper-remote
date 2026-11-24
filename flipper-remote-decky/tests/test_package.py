"""The built zip, unpacked the way Decky unpacks it (zipfile.extractall into its plugins folder), run from there
with the environment Decky's bundled Python has (its own library path, and a PYTHONHOME that would stop the Deck's
Python from starting if it were passed on)."""
import asyncio
import importlib.util
import json
import os
import tempfile
import zipfile

from test_backend import Panel, _wait, decky, events, lock_free, mock, until  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(ROOT, "out", "flipper-remote-decky-1.1.0.zip")


def test_installed_zip_works_from_decky(mock, monkeypatch):
    plugins = tempfile.mkdtemp(prefix="homebrew-plugins-")
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(plugins)
    plugin_dir = os.path.join(plugins, "flipper-remote")
    with open(os.path.join(plugin_dir, "plugin.json")) as f:
        manifest = json.load(f)
    assert manifest["name"] == "Flipper Remote" and manifest["api_version"] == 1 and "root" not in manifest["flags"]

    monkeypatch.setenv("DECKY_PLUGIN_DIR", plugin_dir)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123456")   # PyInstaller's own libraries
    monkeypatch.setenv("PYTHONHOME", "/tmp/_MEI123456")        # would make /usr/bin/python3 fail to start
    spec = importlib.util.spec_from_file_location("installed_main", os.path.join(plugin_dir, "main.py"))
    installed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installed)
    assert installed.HELPER == os.path.join(plugin_dir, "py_modules", "flipper_helper.py")

    async def scenario():
        plugin = installed.Plugin()
        await plugin._main()
        p = Panel(plugin)
        snap = await p.hello()
        assert snap["plugin_version"] == "1.1.0" and snap["version"] == "1.2.2"
        await p.heartbeat()
        assert await until(lambda: any(s["state"] == "connected" for s in events("status")))
        await p.send("/ext/subghz/Fan/Fan_Off.sub", None, "touch")
        assert await until(lambda: len(mock.tx_log) == 1)
        await plugin._unload()

    asyncio.run(asyncio.wait_for(scenario(), 60))
    assert lock_free()
    assert not os.path.exists(os.path.join(plugin_dir, "py_modules", "__pycache__")), "nothing written into the plugin"
