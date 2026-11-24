import os, sys, time, threading, json
HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "flipper-remote"))
from mock_flipper import MockFlipper, STATIC_SUB, BLOCKED_SUB, raw_sub
os.environ["XDG_CACHE_HOME"] = "/tmp/fr-ui-cache"; os.environ["XDG_CONFIG_HOME"] = "/tmp/fr-ui-config"
import shutil; shutil.rmtree("/tmp/fr-ui-cache", True); shutil.rmtree("/tmp/fr-ui-config", True)
mock = MockFlipper(firmware="xfw", name="Ufakoko")
fs = {"/ext/subghz": None, "/ext/subghz/assets": None, "/ext/subghz/Ceiling_Fan": None, "/ext/subghz/Lights": None,
      "/ext/subghz/Captures": None,
      "/ext/subghz/Ceiling_Fan/Fan_Off.sub": STATIC_SUB, "/ext/subghz/Ceiling_Fan/Fan_Low.sub": STATIC_SUB,
      "/ext/subghz/Ceiling_Fan/Fan_Medium.sub": STATIC_SUB, "/ext/subghz/Ceiling_Fan/Fan_High.sub": STATIC_SUB,
      "/ext/subghz/Ceiling_Fan/Fan_Light_Toggle.sub": raw_sub(1400),
      "/ext/subghz/Lights/Living_Room_On.sub": STATIC_SUB, "/ext/subghz/Lights/Living_Room_Off.sub": STATIC_SUB,
      "/ext/subghz/Lights/Porch.sub": BLOCKED_SUB,
      "/ext/subghz/Garage_Door.sub": BLOCKED_SUB, "/ext/subghz/Doorbell_Test.sub": STATIC_SUB}
mock.fs = fs
os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = mock.start()
import flipper_remote as fr
c = fr.Controller(); c.start(); c.submit("connect")
c.update_settings({"show": "subghz"})   # this walkthrough is about the Sub-GHz view
srv = fr.UIServer(c, 0, "tok"); threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.port}/"
from playwright.sync_api import sync_playwright
shots = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "shots")
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1280, "height": 800}, has_touch=True)
    logs = []
    pg.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
    pg.on("pageerror", lambda e: logs.append(f"PAGEERROR: {e}"))
    pg.goto(url)
    time.sleep(0.15); pg.screenshot(path=f"{shots}/01_connecting.png")
    pg.wait_for_selector(".folder", timeout=15000); time.sleep(1.5)
    pg.screenshot(path=f"{shots}/02_root.png")
    pg.click('.folder:has-text("Ceiling Fan")'); pg.wait_for_selector(".key", timeout=10000); time.sleep(2.0)
    pg.screenshot(path=f"{shots}/03_fan_folder.png")
    # pin two signals
    pg.click('[aria-label="Pin Fan Off"]'); time.sleep(0.4)
    pg.click('[aria-label="Pin Fan Light Toggle"]'); time.sleep(0.6)
    # tap Fan High -> capture the in-progress state
    key = pg.locator('.key[data-key="/ext/subghz/Ceiling_Fan/Fan_High.sub"] .send').first
    box = key.bounding_box()
    pg.mouse.move(box["x"] + 40, box["y"] + 30); pg.mouse.down(); time.sleep(0.12); pg.mouse.up()
    time.sleep(0.35); pg.screenshot(path=f"{shots}/04_sending.png")
    time.sleep(2.0)
    # hold Fan Light Toggle (RAW) for 2 s
    key2 = pg.locator('#listing .key[data-key="/ext/subghz/Ceiling_Fan/Fan_Light_Toggle.sub"] .send')
    box = key2.bounding_box()
    pg.mouse.move(box["x"] + 40, box["y"] + 30); pg.mouse.down(); time.sleep(1.6)
    pg.screenshot(path=f"{shots}/05_holding.png"); time.sleep(0.5); pg.mouse.up()
    time.sleep(2.5)
    # go to Lights and try the region-locked Porch light
    pg.click('#crumbs button:has-text("Sub-GHz")'); pg.wait_for_selector('.folder:has-text("Lights")'); pg.click('.folder:has-text("Lights")')
    pg.wait_for_selector('.key[data-key="/ext/subghz/Lights/Porch.sub"]'); time.sleep(1.2)
    k3 = pg.locator('#listing .key[data-key="/ext/subghz/Lights/Porch.sub"] .send'); box = k3.bounding_box()
    pg.mouse.move(box["x"] + 40, box["y"] + 30); pg.mouse.down(); time.sleep(0.1); pg.mouse.up()
    time.sleep(2.5); pg.screenshot(path=f"{shots}/06_error.png")
    pg.click("#popupBtn"); pg.click("#themeBtn"); time.sleep(0.5)
    pg.screenshot(path=f"{shots}/07_dark.png")
    pg.click("#themeBtn")
    # phone-sized
    pg.set_viewport_size({"width": 400, "height": 820}); time.sleep(0.5)
    pg.screenshot(path=f"{shots}/08_narrow.png", full_page=True)
    pg.set_viewport_size({"width": 1280, "height": 800})
    # Flipper becomes unreachable
    mock.reachable = False; mock.drop_link(); time.sleep(0.6)
    pg.click("#linkBtn")  # "Connect"
    time.sleep(12); pg.screenshot(path=f"{shots}/09_unreachable.png")
    print("\n".join(logs) or "no console output")
    print("tx_log:", [(f.rsplit('/',1)[1], round(b_-a, 2), w) for f, a, b_, w in mock.tx_log])
    print("violations:", mock.violations)
    b.close()
c.stop(); srv.shutdown(); mock.stop()
