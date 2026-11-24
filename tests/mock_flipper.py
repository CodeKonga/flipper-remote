"""
A fake BlueZ + Flipper Zero for testing flipper_remote.py without hardware.

* Runs a private dbus-daemon.
* Exports org.bluez objects with dbus-fast (an independent D-Bus implementation).
* Emulates the Flipper's BLE serial service (indications, flow-control characteristic with the
  firmware's exact "notify only when the window hits zero" semantics) and its protobuf RPC,
  decoding requests with classes generated from the official flipperzero-protobuf .proto files.
* Emulates the Sub-GHz RPC scene for both official firmware and Xtreme (whose press/release
  don't persist the scene state, so release fails and TX stops on exit).
* Emulates the Infrared RPC scene (load a remote, press/release a button by name) and the
  125 kHz RFID, NFC and iButton RPC scenes (loading a file starts emulating it; exit stops it).
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "pb"))
import application_pb2  # noqa: E402,F401
import flipper_pb2  # noqa: E402
import storage_pb2  # noqa: E402

import random  # noqa: E402

from dbus_fast import BusType, Message, MessageType, Variant  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402
from dbus_fast.errors import DBusError  # noqa: E402
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method  # noqa: E402

SERIAL = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
U61 = "19ed82ae-ed21-4c9d-4145-228e61fe0000"
U62 = "19ed82ae-ed21-4c9d-4145-228e62fe0000"
U63 = "19ed82ae-ed21-4c9d-4145-228e63fe0000"
U64 = "19ed82ae-ed21-4c9d-4145-228e64fe0000"

BUS_CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={sock}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

STATIC_SUB = b"""Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 433920000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: Princeton
Bit: 24
Key: 00 00 00 00 00 95 D5 D4
TE: 400
"""

BLOCKED_SUB = b"""Filetype: Flipper SubGhz Key File
Version: 1
Frequency: 868350000
Preset: FuriHalSubGhzPresetOok650Async
Protocol: CAME
Bit: 12
Key: 00 00 00 00 00 00 0F 1A
"""


IR_REMOTE = b"""Filetype: IR signals file
Version: 1
#
name: Power
type: parsed
protocol: NEC
address: 04 00 00 00
command: 08 00 00 00
#
name: Vol_up
type: parsed
protocol: NEC
address: 04 00 00 00
command: 02 00 00 00
#
name: Input
type: raw
frequency: 38000
duty_cycle: 0.330000
data: 9024 4512 579 552 579 552 579 1683 579 552
"""
NFC_CARD = b"""Filetype: Flipper NFC device
Version: 4
# Device type can be ISO14443-3A, ISO14443-3B, ISO14443-4A, NTAG/Ultralight, Mifare Classic, Mifare DESFire
Device type: NTAG/Ultralight
UID: 04 A1 B2 C3 D4 E5 F6
ATQA: 00 44
SAK: 00
"""
RFID_KEY = b"""Filetype: Flipper RFID key
Version: 1
Key type: EM4100
Data: 01 23 45 67 89
"""
IBUTTON_KEY = b"""Filetype: Flipper iButton key
Version: 2
Protocol: DS1990
Rom Data: 01 23 45 67 89 AB CD EF
"""
# what each app calls itself, and the first line its files start with
MOCK_APPS = {"Sub-GHz": "subghz", "SubGHz": "subghz", "subghz": "subghz", "Infrared": "infrared",
             "125 kHz RFID": "lfrfid", "NFC": "nfc", "iButton": "ibutton"}
FILETYPES = {"subghz": b"Filetype: Flipper SubGhz", "infrared": b"Filetype: IR signals file",
             "lfrfid": b"Filetype: Flipper RFID key", "nfc": b"Filetype: Flipper NFC device",
             "ibutton": b"Filetype: Flipper iButton key"}


def raw_sub(total_ms: int) -> bytes:
    # RAW_Data lines of +/- 500 us pulses adding up to total_ms
    pulses = total_ms * 1000 // 500
    lines, line = [], []
    for i in range(pulses):
        line.append(str(500 if i % 2 == 0 else -500))
        if len(line) == 512:
            lines.append("RAW_Data: " + " ".join(line))
            line = []
    if line:
        lines.append("RAW_Data: " + " ".join(line))
    head = ("Filetype: Flipper SubGhz RAW File\nVersion: 1\nFrequency: 315000000\n"
            "Preset: FuriHalSubGhzPresetOok650Async\nProtocol: RAW\n")
    return (head + "\n".join(lines) + "\n").encode()


def default_fs():
    return {
        "/ext/subghz": None,
        "/ext/subghz/assets": None,
        "/ext/subghz/assets/setting_user": b"x",
        "/ext/subghz/Fan": None,
        "/ext/subghz/Lights": None,
        "/ext/subghz/Fan/Fan_Off.sub": STATIC_SUB,
        "/ext/subghz/Fan/Fan_High.sub": STATIC_SUB.replace(b"95 D5 D4", b"95 D5 D1"),
        "/ext/subghz/Fan/Fan_Light_RAW.sub": raw_sub(1800),
        "/ext/subghz/Lights/Lamp_On.sub": STATIC_SUB,
        "/ext/subghz/Garage_868.sub": BLOCKED_SUB,
        "/ext/subghz/Broken.sub": b"not a sub file",
        "/ext/subghz/readme.txt": b"hello",
        **{f"/ext/subghz/Lights/Bulk_{i:02d}.sub": STATIC_SUB for i in range(1, 15)},
    }


class Radio:
    """Optional Bluetooth-like timing for the simulator (mock.radio = Radio()). Off by default.

    The idle Flipper advertises every 1-2.5 s, and ten times a second for a minute after a disconnect;
    BlueZ connects on the first advertisement it hears. GATT requests take two connection intervals each,
    one at a time; writes and indications one interval. BlueZ needs `resolve` seconds to check its cached
    copy of the Flipper's services."""

    def __init__(self, ci=0.045, slow=(1.0, 2.5), fast=(0.08, 0.10), resolve=0.3,
                 hear_connecting=1.0, hear_scanning=1.0, connect_gives_up=42.0):
        self.ci, self.slow, self.fast, self.resolve = ci, slow, fast, resolve
        # The chance that BlueZ hears any one advertisement: while it's only waiting to connect (it
        # listens passively, for that one device), and while a scan is running. A Steam Deck with a
        # Bluetooth headset playing behaved like hear_connecting ~0.1, hear_scanning ~0.9.
        self.hear_connecting, self.hear_scanning = hear_connecting, hear_scanning
        self.connect_gives_up = connect_gives_up   # then BlueZ reports le-connection-abort-by-local
        self.fast_until = 0.0
        self.fail_next_connect = False   # the next connect hears the Flipper but the link fails to form
        self._gatt = None
        self._ind_at = 0.0

    def next_advert_in(self) -> float:
        if time.monotonic() < self.fast_until:
            return random.uniform(*self.fast)
        return random.uniform(0, random.uniform(*self.slow))

    def adverts(self):
        """Seconds to each of the Flipper's next advertisements, one after another."""
        fast = time.monotonic() < self.fast_until
        interval = random.uniform(*(self.fast if fast else self.slow))
        yield random.uniform(0, interval)
        while True:
            yield interval

    def first_heard_by_scan_in(self) -> float:
        waited = 0.0
        for gap in self.adverts():
            waited += gap
            if random.random() < self.hear_scanning:
                return waited

    async def gatt_request(self):
        if self._gatt is None:
            self._gatt = asyncio.Lock()
        async with self._gatt:
            await asyncio.sleep(2 * self.ci)

    def indication_at(self) -> float:
        """Indications go out one per connection interval or so, in order."""
        self._ind_at = max(time.monotonic() + self.ci, self._ind_at + 0.002)
        return self._ind_at


class Props(ServiceInterface):
    """A ServiceInterface whose properties live in a dict (set by the mock)."""


class AgentManager(ServiceInterface):
    def __init__(self, mock):
        super().__init__("org.bluez.AgentManager1")
        self.mock = mock

    @method()
    def RegisterAgent(self, path: "o", capability: "s"):
        self.mock.agent = (self.mock._last_sender, path, capability)
        self.mock.events.append(("register_agent", capability))

    @method()
    def UnregisterAgent(self, path: "o"):
        self.mock.agent = None
        self.mock.events.append(("unregister_agent",))

    @method()
    def RequestDefaultAgent(self, path: "o"):
        pass


class Adapter(ServiceInterface):
    def __init__(self, mock):
        super().__init__("org.bluez.Adapter1")
        self.mock = mock
        self.discovering = False

    @method()
    def StartDiscovery(self):
        self.mock.events.append(("scan_start",))
        self.discovering = True
        if self.mock.reachable:
            r = self.mock.radio
            self.mock.loop.call_later(0.05 + r.first_heard_by_scan_in() if r else 0.3, self.mock.rediscover)

    @method()
    def StopDiscovery(self):
        self.discovering = False

    @method()
    def RemoveDevice(self, path: "o"):
        self.mock.events.append(("remove_device",))
        if path == self.mock.dev_path and not self.mock.removed:
            self.mock.link_down()
            self.mock.paired = False
            self.mock.trusted = False
            self.mock.removed = True
            self.mock.bus.unexport(self.mock.dev_path)
            if self.discovering and self.mock.reachable:
                self.mock.loop.call_later(0.5, self.mock.rediscover)

    @method()
    def SetDiscoveryFilter(self, f: "a{sv}"):
        pass

    @dbus_property(access=PropertyAccess.READ)
    def Powered(self) -> "b":
        return self.mock.powered

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> "s":
        return "AA:BB:CC:DD:EE:FF"


class Device(ServiceInterface):
    def __init__(self, mock):
        super().__init__("org.bluez.Device1")
        self.mock = mock
        self._connected = False
        self._resolved = False
        self._rssi = None
        self._connecting = False

    def seen(self):
        self._rssi = -60
        self.emit_properties_changed({"RSSI": -60})

    @method()
    async def Connect(self):
        m = self.mock
        m.events.append(("connect",))
        if self._connecting:   # like BlueZ: one attempt at a time, even if its caller stopped waiting
            raise DBusError("org.bluez.Error.InProgress", "In Progress")
        self._connecting = True
        try:
            await self._connect(m)
        finally:
            self._connecting = False

    async def _connect(self, m):
        if m.fail_connect_times > 0:
            m.fail_connect_times -= 1
            raise DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local")
        if not m.reachable:
            await asyncio.sleep(0.2)
            raise DBusError("org.bluez.Error.Failed", "Page Timeout")
        if self._connected:
            raise DBusError("org.bluez.Error.AlreadyConnected", "Already Connected")
        r = m.radio
        if r:
            started = time.monotonic()
            for gap in r.adverts():   # BlueZ connects on the first advertisement it hears
                await asyncio.sleep(gap)
                p = r.hear_scanning if m.adapter.discovering else r.hear_connecting
                if random.random() < p:
                    break
                if time.monotonic() - started > r.connect_gives_up:
                    raise DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local")
            if r.fail_next_connect:   # heard, but the link failed to be established (HCI 0x3E)
                r.fail_next_connect = False
                await asyncio.sleep(6 * r.ci)
                r.fast_until = time.monotonic() + 60
                raise DBusError("org.bluez.Error.Failed", "le-connection-abort-by-local")
            await asyncio.sleep(2 * r.ci)   # encryption
        else:
            await asyncio.sleep(0.1 + m.connect_delay)
        m.link_up()

    @method()
    def Disconnect(self):
        self.mock.events.append(("disconnect",))
        self.mock.link_down()

    @method()
    async def Pair(self):
        m = self.mock
        m.events.append(("pair",))
        if not m.reachable:
            await asyncio.sleep(0.2)
            raise DBusError("org.bluez.Error.ConnectionAttemptFailed", "Page Timeout")
        if m.paired:
            raise DBusError("org.bluez.Error.AlreadyExists", "Already Exists")
        # The Flipper is DisplayOnly and requires MITM: only a keyboard-capable agent gets passkey entry.
        if not m.agent or m.agent[2] not in ("KeyboardDisplay", "KeyboardOnly", ""):
            raise DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
        if not self._connected:
            m.link_up()
        m.displayed_code = f"{random.randint(0, 999999):06d}"
        m.pairing = True
        try:
            reply = await m.bus.call(Message(destination=m.agent[0], path=m.agent[1], interface="org.bluez.Agent1",
                                             member="RequestPasskey", signature="o", body=[m.dev_path]))
        finally:
            m.pairing = False
        if reply.message_type == MessageType.ERROR:
            m.link_down()
            raise DBusError("org.bluez.Error.AuthenticationCanceled", "Authentication Canceled")
        if f"{reply.body[0]:06d}" != m.displayed_code:
            m.link_down()
            raise DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
        m.paired = True
        self.emit_properties_changed({"Paired": True})

    @method()
    async def CancelPairing(self):
        m = self.mock
        m.events.append(("cancel_pairing",))
        if m.agent and m.pairing:
            await m.bus.call(Message(destination=m.agent[0], path=m.agent[1], interface="org.bluez.Agent1",
                                     member="Cancel"))

    @dbus_property()
    def Trusted(self) -> "b":
        return self.mock.trusted

    @Trusted.setter
    def Trusted(self, value: "b"):
        self.mock.trusted = value

    @dbus_property(access=PropertyAccess.READ)
    def Address(self) -> "s":
        return self.mock.address

    @dbus_property(access=PropertyAccess.READ)
    def Name(self) -> "s":
        return self.mock.name

    @dbus_property(access=PropertyAccess.READ)
    def Alias(self) -> "s":
        return self.mock.name

    @dbus_property(access=PropertyAccess.READ)
    def Paired(self) -> "b":
        return self.mock.paired

    @dbus_property(access=PropertyAccess.READ)
    def Connected(self) -> "b":
        return self._connected

    @dbus_property(access=PropertyAccess.READ)
    def ServicesResolved(self) -> "b":
        return self._resolved

    @dbus_property(access=PropertyAccess.READ)
    def Adapter(self) -> "o":
        return "/org/bluez/hci0"

    @dbus_property(access=PropertyAccess.READ)
    def RSSI(self) -> "n":
        return self._rssi or -100

    @dbus_property(access=PropertyAccess.READ)
    def UUIDs(self) -> "as":
        uuids = ["0000180a-0000-1000-8000-00805f9b34fb"]
        if self.mock.flipper_uuids:
            uuids.append("00003082-0000-1000-8000-00805f9b34fb")
            if self.mock.gatt_known:
                uuids.append(SERIAL)
        return uuids


class Battery(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Battery1")

    @dbus_property(access=PropertyAccess.READ)
    def Percentage(self) -> "y":
        return 87


class Service(ServiceInterface):
    def __init__(self, dev_path):
        super().__init__("org.bluez.GattService1")
        self.dev_path = dev_path

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return SERIAL

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Device(self) -> "o":
        return self.dev_path


class Char(ServiceInterface):
    def __init__(self, mock, uuid, flags, svc_path):
        super().__init__("org.bluez.GattCharacteristic1")
        self.mock = mock
        self.uuid = uuid
        self.flags = flags
        self.svc_path = svc_path
        self.value = b""
        self.notifying = False

    @method()
    async def StartNotify(self):
        if self.mock.radio:
            await self.mock.radio.gatt_request()
        self.mock.check_link()
        self.mock.check_auth()
        self.notifying = True

    @method()
    def StopNotify(self):
        self.notifying = False

    @method()
    async def ReadValue(self, options: "a{sv}") -> "ay":
        if self.mock.radio:
            await self.mock.radio.gatt_request()
        self.mock.check_link()
        self.mock.check_auth()
        if self.uuid == U63:
            return self.mock.buff_size.to_bytes(4, "big")
        if self.uuid == U64:
            return (0 if self.mock.pin_locked else 1).to_bytes(4, "little")
        return self.value

    @method()
    async def WriteValue(self, value: "ay", options: "a{sv}"):
        self.mock.check_link()
        self.mock.check_auth()
        if self.uuid != U62:
            raise DBusError("org.bluez.Error.NotPermitted", "Write not permitted")
        wtype = options.get("type").value if "type" in options else None
        self.mock.write_types.add(wtype)
        if len(value) > self.mock.mtu - 3:
            self.mock.violations.append(f"write of {len(value)} bytes > MTU-3")
        await asyncio.sleep(self.mock.radio.ci if self.mock.radio else 0.005)
        self.mock.on_rx(bytes(value))

    def push(self, data: bytes):
        r = self.mock.radio
        if r and self.notifying:
            delay = r.indication_at() - time.monotonic()
            self.mock.loop.call_later(max(0.0, delay), self._push_now, data)
            return
        self._push_now(data)

    def _push_now(self, data: bytes):
        self.value = data
        if self.notifying:
            self.emit_properties_changed({"Value": data})

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self.svc_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self.flags

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":
        return self.notifying

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self.value

    @dbus_property(access=PropertyAccess.READ)
    def MTU(self) -> "q":
        return self.mock.mtu


class MockFlipper:
    def __init__(self, firmware="ofw", name="Ufakoko", address="80:E1:26:12:34:56"):
        self.firmware = firmware          # "ofw" | "xfw" | "strict" (only accepts "SubGHz")
        self.name = name
        self.address = address
        self.paired = True
        self.powered = True
        self.reachable = True
        self.authorized = True
        self.pin_locked = False
        self.gatt_known = True
        self.flipper_uuids = True
        self.trusted = True
        self.removed = False
        self.agent = None
        self.pairing = False
        self.displayed_code = None
        self._last_sender = None
        self.fail_connect_times = 0
        self.drop_during_setup_times = 0   # links that go down before the services show up
        self.radio = None                  # Radio() for Bluetooth-like timing
        self.connect_delay = 0.0           # extra seconds before a connect succeeds
        self.mtu = 247
        self.buff_size = 1024
        self.fs = default_fs()
        self.busy = False                 # someone opened an app on the Flipper itself
        self.blocked_freqs = {868350000}
        self.started_first = True         # send APP_STARTED before the start response (race)
        self.events = []
        self.violations = []
        self.write_types = set()
        self.tx_log = []                  # (file, start, stop)
        self.ir_log = []                  # (remote, button, start, stop)
        self.emu_log = []                 # (file, start, stop, why)
        self.dev_path = "/org/bluez/hci0/dev_" + address.replace(":", "_")
        self._reset_link_state()
        self.loop = None
        self.bus = None
        self._ready = threading.Event()

    # -- process / bus lifecycle -----------------------------------------------------------
    def start(self):
        self.tmp = tempfile.mkdtemp(prefix="fr-bus-")
        sock = os.path.join(self.tmp, "bus")
        conf = os.path.join(self.tmp, "bus.conf")
        with open(conf, "w") as f:
            f.write(BUS_CONF.format(sock=sock))
        self.daemon = subprocess.Popen(["dbus-daemon", f"--config-file={conf}", "--nofork", "--nopidfile"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.02)
        self.address_str = f"unix:path={sock}"
        self.thread = threading.Thread(target=self._thread, daemon=True)
        self.thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("mock bus didn't start")
        return self.address_str

    def _thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._setup())
        self._ready.set()
        self.loop.run_forever()

    async def _setup(self):
        self.bus = await MessageBus(bus_address=self.address_str).connect()
        self.bus.add_message_handler(self._sniff)
        self.bus.export("/org/bluez", AgentManager(self))
        self.adapter = Adapter(self)
        self.device = Device(self)
        self.battery = Battery()
        self.bus.export("/org/bluez/hci0", self.adapter)
        self.bus.export(self.dev_path, self.device)
        self.bus.export(self.dev_path, self.battery)
        svc = self.dev_path + "/service000c"
        self.svc_path = svc
        self.service = Service(self.dev_path)
        self.chars = {
            U61: Char(self, U61, ["read", "indicate"], svc),
            U62: Char(self, U62, ["read", "write-without-response", "write"], svc),
            U63: Char(self, U63, ["read", "notify"], svc),
            U64: Char(self, U64, ["read", "write", "notify"], svc),
        }
        self.char_paths = {U61: svc + "/char000d", U62: svc + "/char0010", U63: svc + "/char0012",
                           U64: svc + "/char0015"}
        await self.bus.request_name("org.bluez")

    def _sniff(self, msg):
        if msg.message_type == MessageType.METHOD_CALL and msg.member == "RegisterAgent":
            self._last_sender = msg.sender   # BlueZ talks to the agent of whoever registered it
        return None

    def rediscover(self):
        if self.removed:
            self.removed = False
            self.bus.export(self.dev_path, self.device)
            self.bus.export(self.dev_path, self.battery)
        self.device.seen()

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.daemon.terminate()
        self.daemon.wait(5)

    def call(self, fn, *args):
        """Run fn on the mock's loop and wait."""
        done = threading.Event()
        out = {}

        def run():
            try:
                out["v"] = fn(*args)
            finally:
                done.set()

        self.loop.call_soon_threadsafe(run)
        done.wait(5)
        return out.get("v")

    # -- link ---------------------------------------------------------------------------------
    def _reset_link_state(self):
        self.stream = bytearray()
        self.bytes_ready = self.buff_size
        if getattr(self, "emu_on", None):
            self._emu_stop("link lost")
        self.app = None
        self.app_kind = None
        self.app_state = "idle"
        self.err = (0, "")
        self.tx_on = None
        self.tx_end = None
        self.endless = False
        self.ir_on = None
        self.emu_on = None

    def _emu_stop(self, why):
        f, t0 = self.emu_on
        self.emu_log.append((f, t0, time.monotonic(), why))
        self.emu_on = None

    def press_back(self):
        """Someone presses BACK on the Flipper: the remote-controlled app closes."""
        def run():
            if not self.app:
                return
            if self.emu_on:
                self._emu_stop("back")
            self.app = None
            self.app_kind = None
            self._state_notify(False)
        self.loop.call_soon_threadsafe(run)

    def check_link(self):
        if not self.device._connected:
            raise DBusError("org.bluez.Error.NotConnected", "Not Connected")

    def check_auth(self):
        if not self.authorized or not self.paired:
            raise DBusError("org.bluez.Error.NotAuthorized", "Operation Not Authorized")

    def link_up(self):
        self._reset_link_state()
        d = self.device
        d._connected = True
        d.emit_properties_changed({"Connected": True})
        self.bus.export(self.svc_path, self.service)
        for uuid, ch in self.chars.items():
            ch.notifying = False
            self.bus.export(self.char_paths[uuid], ch)
        self.loop.call_later(self.radio.resolve if self.radio else 0.15, self._resolved)

    def _resolved(self):
        if self.drop_during_setup_times > 0:
            self.drop_during_setup_times -= 1
            self.link_down()
            return
        self.device._resolved = True
        self.device.emit_properties_changed({"ServicesResolved": True})

    def link_down(self):
        d = self.device
        if not d._connected:
            return
        if self.tx_on:
            self._tx_stop("link-down")
        d._connected = False
        d._resolved = False
        if self.radio:
            self.radio.fast_until = time.monotonic() + 60
        d.emit_properties_changed({"ServicesResolved": False})
        d.emit_properties_changed({"Connected": False})
        for uuid in self.chars:
            self.bus.unexport(self.char_paths[uuid])
        self.bus.unexport(self.svc_path)
        self._reset_link_state()

    def drop_link(self):
        self.call(self.link_down)

    # -- serial + RPC ---------------------------------------------------------------------------
    def on_rx(self, data: bytes):
        if len(data) > self.bytes_ready:
            self.violations.append(f"overflow: got {len(data)} bytes, window {self.bytes_ready}")
        self.bytes_ready -= min(self.bytes_ready, len(data))
        if self.pin_locked:
            return
        self.stream += data
        self.loop.call_later(0.003, self._process)

    def _process(self):
        while True:
            n, shift, pos = 0, 0, 0
            while pos < len(self.stream):
                b = self.stream[pos]
                n |= (b & 0x7F) << shift
                pos += 1
                if not b & 0x80:
                    break
                shift += 7
            else:
                break
            if len(self.stream) < pos + n:
                break
            payload = bytes(self.stream[pos:pos + n])
            del self.stream[:pos + n]
            if n == 0:
                self.violations.append("zero-length message")
                continue
            try:
                req = flipper_pb2.Main.FromString(payload)
            except Exception as e:  # noqa: BLE001
                self.violations.append(f"decode error {e}")
                self._send(flipper_pb2.Main(command_id=0, command_status=flipper_pb2.ERROR_DECODE))
                self.loop.call_later(0.05, self.link_down)
                return
            self._handle(req)
        if self.bytes_ready == 0:  # firmware: notify only when the client used the whole window
            self.bytes_ready = self.buff_size
            self.chars[U63].push(self.buff_size.to_bytes(4, "big"))

    def _send(self, msg):
        data = msg.SerializeToString()
        frame = _varint(len(data)) + data
        step = self.mtu - 3
        for i in range(0, len(frame), step):
            self.chars[U61].push(frame[i:i + step])

    def _reply(self, req, status=0, has_next=False, **content):
        m = flipper_pb2.Main(command_id=req.command_id, command_status=status, has_next=has_next)
        if content:
            ((k, v),) = content.items()
            getattr(m, k).CopyFrom(v)
        else:
            m.empty.SetInParent()
        self._send(m)

    def _state_notify(self, started: bool):
        m = flipper_pb2.Main(command_id=0)
        m.app_state_response.state = application_pb2.APP_STARTED if started else application_pb2.APP_CLOSED
        self._send(m)

    def _tx_begin(self):
        self.tx_on = (self.loaded, time.monotonic())
        raw = raw_ms_of(self.fs[self.loaded])
        # RAW playback ends by itself; static codes run until released (Unleashed) or stopped
        self.tx_end = time.monotonic() + raw / 1000 if raw else None

    def _unleashed_tick(self):
        if not self.app or not self.tx_on:
            return
        now = time.monotonic()
        complete = self.tx_end is not None and now >= self.tx_end
        if complete and not self.endless:
            self._tx_stop("complete")
            self.app_state = "idle"
            return
        if complete and self.endless and raw_ms_of(self.fs[self.loaded]):
            self._tx_stop("loop")
            self._tx_begin()                      # RAW restarts while the button is held
        self.loop.call_later(0.1, self._unleashed_tick)

    def _tx_stop(self, why):
        f, t0 = self.tx_on
        self.tx_log.append((f, t0, time.monotonic(), why))
        self.tx_on = None

    def _handle(self, req):
        kind = req.WhichOneof("content")
        self.events.append(("rpc", kind))
        if kind == "system_ping_request":
            m = flipper_pb2.Main(command_id=req.command_id)
            m.system_ping_response.data = req.system_ping_request.data
            self._send(m)
        elif kind == "property_get_request":
            key = req.property_get_request.key
            if self.firmware == "old":
                return self._reply(req, flipper_pb2.ERROR_NOT_IMPLEMENTED)
            fork = {"ofw": "Official", "xfw": "Xtreme", "strict": "Official", "unleashed": "Unleashed"}[self.firmware]
            if key == "devinfo.firmware.origin.fork":
                m = flipper_pb2.Main(command_id=req.command_id, has_next=True)
                m.property_get_response.key = "firmware.origin.fork"
                m.property_get_response.value = fork
                self._send(m)
            self._reply(req)
        elif kind == "system_device_info_request":
            items = [("firmware_version", "XFW-0053" if self.firmware == "xfw" else "1.4.3"),
                     ("hardware_name", "Ufakoko"), ("protobuf_version_major", "0"),
                     ("protobuf_version_minor", "25")]
            for i, (k, v) in enumerate(items):
                m = flipper_pb2.Main(command_id=req.command_id, has_next=i < len(items) - 1)
                m.system_device_info_response.key = k
                m.system_device_info_response.value = v
                self._send(m)
        elif kind == "storage_list_request":
            path = req.storage_list_request.path.rstrip("/")
            if path not in self.fs or self.fs[path] is not None:
                return self._reply(req, flipper_pb2.ERROR_STORAGE_NOT_EXIST)
            children = [p for p in self.fs if p.rsplit("/", 1)[0] == path and p != path]
            chunks = [children[i:i + 8] for i in range(0, len(children), 8)] or [[]]
            if len(children) % 8 == 0 and children:
                chunks.append([])
            for i, chunk in enumerate(chunks):
                m = flipper_pb2.Main(command_id=req.command_id, has_next=i < len(chunks) - 1)
                m.storage_list_response.SetInParent()
                for p in chunk:
                    f = m.storage_list_response.file.add()
                    f.name = p.rsplit("/", 1)[1]
                    f.type = storage_pb2.File.DIR if self.fs[p] is None else storage_pb2.File.FILE
                    f.size = 0 if self.fs[p] is None else len(self.fs[p])
                self._send(m)
        elif kind == "storage_read_request":
            data = self.fs.get(req.storage_read_request.path)
            if data is None:
                return self._reply(req, flipper_pb2.ERROR_STORAGE_NOT_EXIST)
            chunks = [data[i:i + 512] for i in range(0, len(data), 512)] or [b""]
            for i, chunk in enumerate(chunks):
                m = flipper_pb2.Main(command_id=req.command_id, has_next=i < len(chunks) - 1)
                m.storage_read_response.file.data = chunk
                self._send(m)
        elif kind == "app_start_request":
            name = req.app_start_request.name
            self.events.append(("start", name, req.app_start_request.args))
            accepted = ("SubGHz", "subghz") if self.firmware == "strict" else ("Sub-GHz", "SubGHz", "subghz")
            accepted += ("Infrared", "125 kHz RFID", "NFC", "iButton")
            self.events.append(("start_ok_names", accepted))
            if self.busy or self.app:
                return self._reply(req, flipper_pb2.ERROR_APP_SYSTEM_LOCKED)
            if name not in accepted:
                return self._reply(req, flipper_pb2.ERROR_INVALID_PARAMETERS)
            self.app = name
            self.app_kind = MOCK_APPS[name]
            self.app_state = "idle"
            if self.started_first:
                self._state_notify(True)
                self._reply(req)
            else:
                self._reply(req)
                self.loop.call_later(0.2, self._state_notify, True)
        elif kind in ("app_load_file_request", "app_button_press_request", "app_button_release_request",
                      "app_exit_request", "app_get_error_request"):
            if kind == "app_get_error_request":
                m = flipper_pb2.Main(command_id=req.command_id)
                m.app_get_error_response.code, m.app_get_error_response.text = self.err
                return self._send(m)
            if not self.app:
                return self._reply(req, flipper_pb2.ERROR_APP_NOT_RUNNING)
            self.err = (0, "")
            ok = False
            if kind == "app_load_file_request":
                path = req.app_load_file_request.path
                self.events.append(("load", path))
                if self.app_state == "idle":
                    data = self.fs.get(path)
                    if data and data.startswith(FILETYPES[self.app_kind]):
                        self.loaded = path
                        self.app_state = "loaded"
                        ok = True
                        if self.app_kind in ("lfrfid", "nfc", "ibutton"):   # loading starts emulating
                            self.app_state = "emulating"
                            self.emu_on = (path, time.monotonic())
                    else:
                        self.err = (1, "Cannot parse file")
            elif kind == "app_button_press_request" and self.app_kind == "infrared":
                name = req.app_button_press_request.args
                self.events.append(("press", name))
                names = [ln.split(":", 1)[1].strip() for ln in self.fs[self.loaded].decode().splitlines()
                         if ln.startswith("name:")] if self.app_state == "loaded" else []
                if name in names:
                    self.ir_on = (self.loaded, name, time.monotonic())
                    self.app_state = "tx"
                    ok = True
                else:
                    self.err = (4, "Cannot find signal")
            elif kind == "app_button_release_request" and self.app_kind == "infrared":
                self.events.append(("release",))
                if self.app_state == "tx":
                    f, name, t0 = self.ir_on
                    self.ir_log.append((f, name, t0, time.monotonic()))
                    self.ir_on = None
                    self.app_state = "loaded"
                    ok = True
            elif kind in ("app_button_press_request", "app_button_release_request") and \
                    self.app_kind in ("lfrfid", "nfc", "ibutton"):
                self.events.append(("press" if kind.endswith("press_request") else "release",))
            elif kind == "app_button_press_request":
                self.events.append(("press",))
                if self.app_state == "loaded":
                    freq = int(self.fs[self.loaded].split(b"Frequency: ")[1].split(b"\n")[0])
                    if freq in self.blocked_freqs:
                        self.err = (2, "Transmission on this frequency is restricted in your region")
                    else:
                        ok = True
                        self._tx_begin()
                        if self.firmware in ("ofw", "unleashed"):
                            self.app_state = "tx"   # XFW forgets to persist this
                        if self.firmware == "unleashed":
                            self.endless = True
                            self.loop.call_later(0.1, self._unleashed_tick)
            elif kind == "app_button_release_request":
                self.events.append(("release",))
                if self.firmware == "unleashed":
                    if self.app_state == "tx":
                        self.endless = False          # current playback runs to its end; tick stops it
                        if self.tx_on and self.tx_end is None:
                            self.tx_end = time.monotonic() + 0.2   # static: remaining repeats
                        ok = True
                else:
                    if self.app_state == "tx":
                        self._tx_stop("release")
                        ok = True
                    if self.firmware == "ofw":
                        self.app_state = "idle"
            elif kind == "app_exit_request":
                self.events.append(("exit",))
                if self.tx_on:
                    self._tx_stop("exit")
                if self.ir_on:
                    f, name, t0 = self.ir_on
                    self.ir_log.append((f, name, t0, time.monotonic()))
                    self.ir_on = None
                if self.emu_on:
                    self._emu_stop("exit")
                self.app = None
                self.app_kind = None
                self._reply(req)
                self.loop.call_later(0.05, self._state_notify, False)
                return
            self._reply(req, 0 if ok else flipper_pb2.ERROR_APP_CMD_ERROR)
        else:
            self._reply(req, flipper_pb2.ERROR_NOT_IMPLEMENTED)


def raw_ms_of(data: bytes) -> int:
    if b"Protocol: RAW" not in data:
        return 0
    total = 0
    for line in data.decode().splitlines():
        if line.startswith("RAW_Data:"):
            total += sum(abs(int(t)) for t in line[9:].split())
    return total // 1000


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)
