import json
import subprocess
import signal
import logging
import os
import re
import struct
import sys
import threading
import time
import urllib.request

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "pb"))
sys.path.insert(0, os.environ.get("FR_APP_DIR", os.path.join(HERE, "..", "flipper-remote")))

import application_pb2  # noqa: E402
import flipper_pb2  # noqa: E402
import storage_pb2  # noqa: E402
from mock_flipper import (MockFlipper, Radio, raw_sub, IR_REMOTE, NFC_CARD, RFID_KEY,  # noqa: E402
                          IBUTTON_KEY, STATIC_SUB)

os.environ["XDG_CACHE_HOME"] = "/tmp/fr-test-cache"
os.environ["XDG_CONFIG_HOME"] = "/tmp/fr-test-config"
import flipper_remote as fr  # noqa: E402


# ---------------------------------------------------------------------------------------------
# D-Bus wire format, checked against dbus-fast
# ---------------------------------------------------------------------------------------------
from dbus_fast import Message, MessageType  # noqa: E402
from dbus_fast import Variant as FVariant  # noqa: E402
from dbus_fast._private.marshaller import Marshaller as FMarshaller  # noqa: E402

CASES = [
    ("s", ["hello"]),
    ("o", ["/org/bluez/hci0/dev_80_E1_26_12_34_56"]),
    ("aya{sv}", [b"\x01\x02\x03", {"type": ("s", "request")}]),
    ("a{sv}", [{}]),
    ("a{sv}", [{"Transport": ("s", "le"), "RSSI": ("n", -60), "Flag": ("b", True), "MTU": ("q", 247)}]),
    ("ss", ["org.bluez.Device1", "Connected"]),
    ("a{oa{sa{sv}}}", [{"/a": {"org.x": {"A": ("u", 7), "B": ("as", ["x", "yy"])}}, "/b": {}}]),
    ("(yv)", [(3, ("s", "member"))]),
    ("a(yv)", [[(1, ("o", "/")), (5, ("u", 9)), (8, ("g", "sa{sv}as"))]]),
    ("tdx", [2 ** 40, 1.5, -5]),
    ("ay", [b""]),
    ("aay", [[b"ab", b"", b"c"]]),
]


def _ours(v):
    if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) and v[0] in fr._ALIGN or (
            isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) and v[0].startswith(("a", "("))):
        return fr.Variant(v[0], _ours(v[1]))
    if isinstance(v, dict):
        return {k: _ours(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_ours(x) for x in v]
    if isinstance(v, tuple):
        return tuple(_ours(x) for x in v)
    return v


def _theirs(v):
    if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) and (v[0] in fr._ALIGN or v[0].startswith(("a", "("))):
        return FVariant(v[0], _theirs(v[1]))
    if isinstance(v, dict):
        return {k: _theirs(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_theirs(x) for x in v]
    if isinstance(v, tuple):
        return [_theirs(x) for x in v]
    return v


def _plain(v):
    """dbus-fast decoded value -> our representation (variants unwrapped)."""
    if isinstance(v, FVariant):
        return _plain(v.value)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


@pytest.mark.parametrize("sig,args", CASES)
def test_marshal_matches_dbus_fast(sig, args):
    ours = fr._Marshaller()
    for t, v in zip(fr.split_signature(sig), _ours(args)):
        ours.write(t, v)
    theirs = FMarshaller(sig, _theirs(args))
    assert bytes(ours.buf) == bytes(theirs.marshall())


@pytest.mark.parametrize("sig,args", CASES)
def test_decode_message_from_dbus_fast(sig, args):
    msg = Message(destination="org.bluez", path="/org/bluez", interface="x.y", member="Z", signature=sig,
                  body=_theirs(args), serial=77)
    decoded = fr._decode_message(msg._marshall(False))
    assert decoded.serial == 77
    assert decoded.fields[fr._F_MEMBER] == "Z"
    expected = [_plain(x) for x in _theirs(args)]
    got = [list(x) if isinstance(x, tuple) else x for x in decoded.body]
    expected = [list(x) if isinstance(x, tuple) else x for x in expected]
    assert _plain(got) == _plain(expected)


def test_encode_message_parses_in_dbus_fast():
    from dbus_fast._private.unmarshaller import Unmarshaller
    import io
    raw = fr._encode_message(fr._METHOD_CALL, 5, {fr._F_PATH: "/org/bluez/hci0/dev_X/char0010",
                                                   fr._F_MEMBER: "WriteValue",
                                                   fr._F_INTERFACE: "org.bluez.GattCharacteristic1",
                                                   fr._F_DESTINATION: "org.bluez"},
                             "aya{sv}", [b"\x05\x06", {"type": fr.Variant("s", "request")}])
    u = Unmarshaller(io.BytesIO(raw))
    m = u.unmarshall()
    assert m.member == "WriteValue" and m.serial == 5 and m.destination == "org.bluez"
    assert m.body[0] == b"\x05\x06" and m.body[1]["type"].value == "request"


# ---------------------------------------------------------------------------------------------
# Protobuf, checked against classes generated from the official .proto files
# ---------------------------------------------------------------------------------------------

def test_requests_decode_with_official_protobuf():
    m = flipper_pb2.Main.FromString(fr.encode_main(9, fr.F_APP_START_REQUEST,
                                                   fr.pb_str(1, "Sub-GHz") + fr.pb_str(2, "RPC")))
    assert m.command_id == 9 and m.WhichOneof("content") == "app_start_request"
    assert m.app_start_request.name == "Sub-GHz" and m.app_start_request.args == "RPC"

    m = flipper_pb2.Main.FromString(fr.encode_main(10, fr.F_APP_LOAD_FILE_REQUEST, fr.pb_str(1, "/ext/subghz/ä.sub")))
    assert m.app_load_file_request.path == "/ext/subghz/ä.sub"

    for field, name in ((fr.F_APP_BUTTON_PRESS_REQUEST, "app_button_press_request"),
                        (fr.F_APP_BUTTON_RELEASE_REQUEST, "app_button_release_request"),
                        (fr.F_APP_EXIT_REQUEST, "app_exit_request"),
                        (fr.F_APP_GET_ERROR_REQUEST, "app_get_error_request"),
                        (fr.F_DEVICE_INFO_REQUEST, "system_device_info_request")):
        m = flipper_pb2.Main.FromString(fr.encode_main(2 ** 31 - 2, field))
        assert m.WhichOneof("content") == name and m.command_id == 2 ** 31 - 2

    m = flipper_pb2.Main.FromString(fr.encode_main(3, fr.F_STORAGE_LIST_REQUEST, fr.pb_str(1, "/ext/subghz")))
    assert m.storage_list_request.path == "/ext/subghz"
    m = flipper_pb2.Main.FromString(fr.encode_main(3, fr.F_PING_REQUEST, fr.pb_bytes(1, b"deck")))
    assert m.system_ping_request.data == b"deck"


def test_responses_decode_with_ours():
    m = flipper_pb2.Main(command_id=300, command_status=flipper_pb2.ERROR_APP_CMD_ERROR, has_next=True)
    m.app_get_error_response.code = 2
    m.app_get_error_response.text = "Transmission on this frequency is restricted"
    d = fr.decode_main(m.SerializeToString())
    assert (d.command_id, d.status, d.has_next, d.field) == (300, 22, True, fr.F_APP_GET_ERROR_RESPONSE)
    fields = dict((f, v) for f, _w, v in fr.pb_fields(d.content))
    assert fields == {1: 2, 2: b"Transmission on this frequency is restricted"}

    m = flipper_pb2.Main(command_id=0)
    m.app_state_response.state = application_pb2.APP_STARTED
    d = fr.decode_main(m.SerializeToString())
    assert d.field == fr.F_APP_STATE_RESPONSE and list(fr.pb_fields(d.content)) == [(1, 0, 1)]

    m = flipper_pb2.Main(command_id=4)
    f = m.storage_list_response.file.add()
    f.name, f.type, f.size = "Fan", storage_pb2.File.DIR, 0
    f = m.storage_list_response.file.add()
    f.name, f.size = "x.sub", 70000
    d = fr.decode_main(m.SerializeToString())
    assert d.field == fr.F_STORAGE_LIST_RESPONSE


def test_rpc_stream_reassembly_across_odd_chunks():
    got = []
    rpc = fr.RpcClient(lambda b: None)
    rpc.on_notification = got.append
    stream = b""
    for i in range(5):
        m = flipper_pb2.Main(command_id=0)
        m.app_state_response.state = i % 2
        data = m.SerializeToString()
        stream += fr.pb_varint(len(data)) + data
    big = flipper_pb2.Main(command_id=0)
    big.storage_read_response.file.data = bytes(range(256)) * 3
    data = big.SerializeToString()
    stream += fr.pb_varint(len(data)) + data
    for i in range(0, len(stream), 7):
        rpc.feed(stream[i:i + 7])
    assert len(got) == 6 and got[-1].field == fr.F_STORAGE_READ_RESPONSE


def test_parse_sub_file_and_duration():
    meta = fr.parse_sub_file(raw_sub(1800))
    assert meta["protocol"] == "RAW" and meta["raw_ms"] == 1800 and meta["frequency"] == 315000000
    assert fr.send_duration_ms(meta) == 1900
    static = fr.parse_sub_file(b"Filetype: Flipper SubGhz Key File\nFrequency: 433920000\n"
                               b"Preset: FuriHalSubGhzPresetOok650Async\nProtocol: Princeton\n")
    assert static["preset"] == "AM650" and static["raw_ms"] is None and fr.send_duration_ms(static) == 500
    assert fr.resolve_saved_path("Fan/Fan_Off") == "/ext/subghz/Fan/Fan_Off.sub"
    assert fr.resolve_saved_path("/ext/subghz/x.sub") == "/ext/subghz/x.sub"


# ---------------------------------------------------------------------------------------------
# End to end against the simulated BlueZ + Flipper
# ---------------------------------------------------------------------------------------------

@pytest.fixture
def mock(request):
    firmware = getattr(request, "param", "ofw")
    m = MockFlipper(firmware=firmware)
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = m.start()
    for f in (fr.CACHE_FILE, fr.CONFIG_FILE, fr.INSTANCE_FILE):
        try:
            os.remove(f)
        except OSError:
            pass
    yield m
    m.stop()


@pytest.fixture
def ctl(mock):
    c = fr.Controller()
    c.start()
    yield c
    c.stop(disconnect=True)


def app_events(mock):
    return [e for e in mock.events if e[0] in ("start", "load", "press", "release", "exit")]


@pytest.mark.parametrize("mock", ["ofw", "xfw", "strict", "unleashed", "old"], indirect=True)
def test_send_static(mock, ctl):
    t0 = time.monotonic()
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(30)
    assert time.monotonic() - t0 < 5
    names = [e[1] for e in mock.events if e[0] == "start"]
    assert names[0] == "Sub-GHz" and all(e[2] == "RPC" for e in mock.events if e[0] == "start")
    seq = [e[0] for e in app_events(mock) if e[0] != "start"]
    assert seq == ["load", "press", "release", "exit"]
    (f, a, b, why), = mock.tx_log
    assert f.endswith("Fan_Off.sub") and 0.5 <= b - a < 1.2
    assert why in ({"ofw": ("release",), "unleashed": ("exit", "complete")}.get(mock.firmware, ("exit",)))
    if mock.firmware == "strict":
        assert names[:2] == ["Sub-GHz", "SubGHz"]
    assert mock.app is None and not mock.violations


@pytest.mark.parametrize("mock", ["ofw", "xfw", "unleashed", "old"], indirect=True)
def test_send_raw_plays_exactly_once(mock, ctl):
    ctl.send("/ext/subghz/Fan/Fan_Light_RAW.sub").wait(60)
    time.sleep(0.4)
    (f, a, b, why), = mock.tx_log           # one playback: no restart, no fragment of a second one
    assert 1.8 <= b - a < 2.6, b - a          # the whole 1.8 s recording went out
    assert why != "loop"
    expected_fork = {"ofw": "Official", "xfw": "Xtreme", "unleashed": "Unleashed", "old": ""}[mock.firmware]
    assert ctl._session.fork == expected_fork
    assert ("rpc", "storage_read_request") in mock.events
    # second send uses the cached duration: no second read
    reads = mock.events.count(("rpc", "storage_read_request"))
    ctl.send("/ext/subghz/Fan/Fan_Light_RAW.sub").wait(60)
    assert mock.events.count(("rpc", "storage_read_request")) == reads
    assert not mock.violations


@pytest.mark.parametrize("mock", ["unleashed"], indirect=True)
def test_unleashed_hold_loops_raw(mock, ctl):
    job = ctl.hold_start("/ext/subghz/Fan/Fan_Light_RAW.sub")
    time.sleep(4.5)
    ctl.hold_stop("/ext/subghz/Fan/Fan_Light_RAW.sub")
    job.wait(20)
    assert [w for *_x, w in mock.tx_log].count("loop") >= 1   # held = keeps repeating, as on the Flipper


def test_press_and_hold(mock, ctl):
    job = ctl.hold_start("/ext/subghz/Lights/Lamp_On.sub")
    time.sleep(2.0)
    ctl.hold_stop("/ext/subghz/Lights/Lamp_On.sub")
    job.wait(20)
    (f, a, b, why), = mock.tx_log
    assert 1.2 <= b - a < 2.6, b - a


def test_region_locked_frequency(mock, ctl):
    with pytest.raises(fr.RegionError) as e:
        ctl.send("/ext/subghz/Garage_868.sub").wait(30)
    assert "restricted in your region" in str(e.value) and "Garage 868" in str(e.value)
    assert mock.app is None and not mock.tx_log


def test_unparseable_file(mock, ctl):
    with pytest.raises(fr.FlipperError) as e:
        ctl.send("/ext/subghz/Broken.sub").wait(30)
    assert "Cannot parse file" in str(e.value) and mock.app is None


def test_flipper_busy(mock, ctl):
    mock.busy = True
    t0 = time.monotonic()
    with pytest.raises(fr.BusyError) as e:
        ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(30)
    assert "BACK" in str(e.value) and time.monotonic() - t0 < 8


def test_recovers_stale_remote_session(mock, ctl):
    ctl.submit("connect").wait(20)
    mock.app = "Sub-GHz"  # e.g. an earlier run died mid-send
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(30)
    assert len(mock.tx_log) == 1


def test_app_started_after_response(mock, ctl):
    mock.started_first = False
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(30)
    assert len(mock.tx_log) == 1


@pytest.mark.parametrize("buff", [40, 64, 100])
def test_flow_control_tiny_buffer(mock, ctl, buff):
    mock.buff_size = buff
    listing = ctl.submit("list", path="/ext/subghz/Lights").wait(30)
    assert len(listing["files"]) == 15
    ctl.send("/ext/subghz/Lights/Bulk_14.sub").wait(30)
    ctl.send("/ext/subghz/Lights/Bulk_13.sub").wait(30)
    assert not mock.violations and len(mock.tx_log) == 2


def test_small_mtu(mock, ctl):
    mock.mtu = 23
    ctl.send("/ext/subghz/Lights/Bulk_07.sub").wait(30)
    assert not mock.violations and len(mock.tx_log) == 1


def test_listing_and_background_metadata(mock, ctl):
    events = []
    ctl.add_listener(lambda ev, data: events.append((ev, data)))
    listing = ctl.submit("list", path="/ext/subghz").wait(30)
    assert listing["dirs"] == ["Fan", "Lights"]  # assets hidden
    assert [f["name"] for f in listing["files"]] == ["Broken.sub", "Garage_868.sub"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and sum(1 for e in events if e[0] == "meta") < 2:
        time.sleep(0.1)
    metas = {d["path"]: d["meta"] for e, d in events if e == "meta"}
    assert metas["/ext/subghz/Garage_868.sub"]["frequency"] == 868350000
    with pytest.raises(fr.FlipperError):
        ctl.submit("list", path="/ext/subghz/Nope").wait(30)


def test_reconnects_after_link_drop(mock, ctl):
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(30)
    mock.drop_link()
    time.sleep(0.3)
    assert ctl.state["state"] == "disconnected"
    ctl.send("/ext/subghz/Fan/Fan_High.sub").wait(30)
    assert len(mock.tx_log) == 2 and mock.events.count(("connect",)) == 2


def test_unreachable_flipper(mock, ctl):
    mock.reachable = False
    with pytest.raises(fr.ConnectError) as e:
        ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(120)
    assert "phone" in str(e.value)
    assert ("scan_start",) in mock.events
    assert ctl.state["state"] == "error"


def test_quick_connect_failure_retries_at_once(mock, ctl):
    # The link fell over while being set up: the Flipper was heard, so try again straight away, no scan.
    mock.fail_connect_times = 1
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(60)
    assert mock.events.count(("connect",)) == 2
    assert mock.events.count(("scan_start",)) == 1     # just the scan that runs while connecting
    assert len(mock.tx_log) == 1


def test_slow_connect_failure_scans_then_retries(mock, ctl, monkeypatch):
    # BlueZ waited the full time without hearing it: listen for it, and try again once it's heard.
    monkeypatch.setattr(fr.FlipperBLE, "QUICK_FAILURE_S", 0.0)
    mock.fail_connect_times = 1
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(60)
    assert mock.events.count(("connect",)) == 2
    assert mock.events.count(("scan_start",)) == 2     # a fresh scan to check it's there before trying again
    assert len(mock.tx_log) == 1


def test_link_dropping_during_setup_is_retried(mock, ctl):
    mock.drop_during_setup_times = 1
    states = []
    ctl.add_listener(lambda name, data: name == "status" and states.append(data["state"]))
    ctl.submit("connect").wait(30)
    assert mock.events.count(("connect",)) == 2 and ctl.state["state"] == "connected"
    assert "error" not in states                     # no error flashed up in between


def test_connection_timings_are_logged(mock, ctl, caplog):
    with caplog.at_level(logging.INFO, logger=fr.log.name):
        ctl.submit("connect").wait(30)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Connected to"))
    assert all(part in line for part in ("Bluetooth link", "services", "setup", "first answer", "Firmware:"))


def test_scans_while_connecting(mock, ctl):
    # From real logs (Steam Deck, headset playing): BlueZ's own listening for the Flipper stayed deaf for
    # 30-43 s, while a scan heard it within a second. Here it never hears it at all unless a scan is running.
    mock.radio = Radio(hear_connecting=0.0, hear_scanning=1.0)
    t0 = time.monotonic()
    ctl.submit("connect").wait(60)
    assert ctl.state["state"] == "connected" and time.monotonic() - t0 < 8
    assert mock.events.count(("connect",)) == 1
    assert not mock.adapter.discovering                # and the scan is stopped again


def test_link_slower_than_our_wait_still_connects(mock, ctl, monkeypatch):
    # From a real log (Bluetooth headset on): BlueZ needed longer than the app waited, kept going, and got
    # there; the app must follow that attempt instead of reporting an error.
    monkeypatch.setattr(fr.FlipperBLE, "CONNECT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(fr.FlipperBLE, "QUICK_FAILURE_S", 0.2)
    mock.connect_delay = 1.5
    states = []
    ctl.add_listener(lambda name, data: name == "status" and states.append(data["state"]))
    ctl.submit("connect").wait(30)
    assert ctl.state["state"] == "connected" and "error" not in states
    assert mock.events.count(("connect",)) >= 2      # the second one found the first still going


def test_long_connect_says_still_looking(mock, ctl, monkeypatch):
    monkeypatch.setattr(fr.FlipperBLE, "STILL_LOOKING_S", 0.3)
    mock.connect_delay = 1.0
    messages = []
    ctl.add_listener(lambda name, data: name == "status" and messages.append(data.get("message", "")))
    ctl.submit("connect").wait(30)
    assert ctl.state["state"] == "connected"
    assert any(m.startswith("Still looking for") for m in messages)


def test_unheard_flipper_gives_up_without_another_long_wait(mock, ctl, monkeypatch):
    monkeypatch.setattr(fr.FlipperBLE, "QUICK_FAILURE_S", 0.0)
    mock.reachable = False
    t0 = time.monotonic()
    with pytest.raises(fr.ConnectError) as e:
        ctl.submit("connect").wait(60)
    assert "phone" in str(e.value)
    assert mock.events.count(("connect",)) == 1 and ("scan_start",) in mock.events   # no second 20 s attempt
    assert time.monotonic() - t0 < 12


def test_bad_pairing_message(mock, ctl):
    mock.authorized = False
    with pytest.raises(fr.ConnectError) as e:
        ctl.submit("connect").wait(60)
    assert "pair it again" in str(e.value).lower() and "pair a flipper" in str(e.value).lower()


def test_pin_locked_message(mock, ctl):
    mock.pin_locked = True
    t0 = time.monotonic()
    with pytest.raises(fr.ConnectError) as e:
        ctl.submit("connect").wait(60)
    assert "PIN" in str(e.value) and time.monotonic() - t0 < 3   # RPC status flag -> no 6 s wait
    assert not mock.device._connected  # we connected it, so we disconnect it again


def test_recovers_from_half_message_on_existing_link(mock, ctl):
    mock.call(mock.link_up)            # link already up, e.g. a previous run died mid-transfer
    time.sleep(0.3)
    mock.stream += bytes([50]) + b"\x08" * 10   # the Flipper is waiting for 40 more bytes
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(60)
    assert len(mock.tx_log) == 1 and ("disconnect",) in mock.events


def test_rpc_ids_start_random(mock):
    ids = {fr.RpcClient(lambda b: None)._next_id for _ in range(5)}
    assert len(ids) > 1 and min(ids) >= 1000


def test_bluetooth_off(mock, ctl):
    mock.powered = False
    with pytest.raises(fr.ConnectError) as e:
        ctl.submit("connect").wait(30)
    assert "turned off" in str(e.value)


def test_detects_flipper_by_service_not_name(mock):
    mock.name = "Charlie's Zero"  # Xtreme advertises the custom name, without "Flipper"
    bus = fr.DBusConnection.system()
    assert [d["name"] for d in fr.find_flippers(bus)] == ["Charlie's Zero"]
    mock.flipper_uuids = False
    assert fr.find_flippers(bus) == []
    bus.close()


def test_no_flipper_message(mock, ctl):
    mock.flipper_uuids = False
    mock.name = "Speaker"
    with pytest.raises(fr.NotFoundError) as e:
        ctl.submit("connect").wait(30)
    assert "pair a flipper" in str(e.value).lower()


def test_cli_send_and_hold(mock):
    assert fr.main(["send", "Fan/Fan_Off"]) == 0
    assert fr.main(["send", "Lights/Lamp_On", "--hold", "1.5"]) == 0
    durations = [b - a for _f, a, b, _w in mock.tx_log]
    assert durations[0] < 1.2 and 1.5 <= durations[1] < 2.5
    assert not mock.device._connected  # CLI disconnects afterwards


def test_doctor_unpaired_shows_steps(mock, capsys):
    mock.paired = False
    assert fr.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "agent KeyboardDisplay" in out and "Enter passkey" in out


def test_doctor(mock, capsys):
    assert fr.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Firmware 1.4.3, RPC API 0.25" in out and "2 .sub files, 2 folders" in out


# ---------------------------------------------------------------------------------------------
# Local web UI server
# ---------------------------------------------------------------------------------------------

@pytest.fixture
def server(ctl):
    srv = fr.UIServer(ctl, 0, "tok")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.hub.close_all()
    srv.shutdown()


def _req(srv, path, body=None, token="tok", host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Token": token, "Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def test_server_security(server):
    assert _req(server, "/api/state", token="wrong")[0] == 403
    assert _req(server, "/api/state", host="evil.example:80")[0] == 403
    status, body = _req(server, "/api/send", {"path": "/etc/passwd"})
    assert status == 400
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as r:
        page = r.read().decode()
    assert '"tok"' in page or "'tok'" in page


def test_server_flow(mock, server):
    lines = []

    def sse():
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/events?t=tok")
        with urllib.request.urlopen(req, timeout=30) as r:
            for raw in r:
                lines.append(raw.decode().strip())

    threading.Thread(target=sse, daemon=True).start()
    time.sleep(0.3)
    assert server.clients == 1
    status, body = _req(server, "/api/list?path=/ext/subghz")
    assert status == 200 and body["listing"]["files"] is None  # nothing cached yet -> loading
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(line == "event: files" for line in lines):
        time.sleep(0.1)
    assert any(line == "event: files" for line in lines)
    status, body = _req(server, "/api/send", {"path": "/ext/subghz/Fan/Fan_Off.sub", "wait": True})
    assert status == 200 and body["ok"]
    status, body = _req(server, "/api/favorite", {"path": "/ext/subghz/Fan/Fan_Off.sub", "on": True})
    (fav,) = body["favorites"]
    assert fav["path"] == "/ext/subghz/Fan/Fan_Off.sub" and fav["label"] == "Fan_Off"
    assert fav["meta"]["frequency"] == 433920000
    _req(server, "/api/hold", {"path": "/ext/subghz/Fan/Fan_High.sub", "action": "start"})
    time.sleep(1.4)
    _req(server, "/api/hold", {"path": "/ext/subghz/Fan/Fan_High.sub", "action": "stop"})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(mock.tx_log) < 2:
        time.sleep(0.1)
    assert len(mock.tx_log) == 2 and mock.tx_log[1][2] - mock.tx_log[1][1] >= 1.0
    phases = [json.loads(line[6:])["phase"] for line in lines if line.startswith("data: {\"id\"")]
    assert "sending" in phases and "done" in phases


# ---------------------------------------------------------------------------------------------
# Pairing and saved settings
# ---------------------------------------------------------------------------------------------

class ScriptedPairing:
    """Plays the user: picks the first nearby Flipper, answers "pair again?" with confirm(), and types
    whatever code() returns."""
    device_path = None

    def __init__(self, code, confirm=None):
        self.code, self.steps, self.devices, self.confirm = code, [], [], confirm

    def wait_confirm(self, timeout):
        time.sleep(0.2)
        return self.confirm() if callable(self.confirm) else self.confirm

    def found(self, devices):
        self.devices = devices

    def wait_choice(self, timeout):
        time.sleep(0.2)
        near = [d for d in self.devices if d["rssi"] is not None]
        return near[0]["address"] if near else None

    def step(self, step, **kw):
        self.steps.append(step)

    def ask_code(self, name, cancelled):
        time.sleep(0.2)
        return self.code()

    def cancelled(self):
        return False


def test_unpaired_flipper_reports_pairing_problem(mock, ctl):
    mock.paired = False
    with pytest.raises(fr.PairingProblem):
        ctl.submit("connect").wait(30)
    assert ctl.state["kind"] == "pairing"


def test_pair_new_flipper(mock, ctl):
    mock.paired = False
    ui = ScriptedPairing(lambda: mock.displayed_code)
    ctl.submit("pair", ui=ui).wait(60)
    assert ui.steps == ["searching", "pairing", "done"]
    assert mock.paired and mock.trusted
    assert ("register_agent", "KeyboardDisplay") in mock.events and ("unregister_agent",) in mock.events
    assert ctl.state["state"] == "connected"
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(30)
    assert len(mock.tx_log) == 1


def test_picking_a_paired_flipper_that_works_changes_nothing(mock, ctl):
    # Charlie's log: the pairing flow removed a working pairing the moment the Flipper was picked, and the new
    # pairing never finished, so the Flipper ended up unpaired. Now a pairing that works is left alone.
    ui = ScriptedPairing(lambda: mock.displayed_code, confirm=True)
    ctl.submit("pair", ui=ui).wait(60)
    assert ui.steps == ["searching", "testing", "works"]
    assert ("remove_device",) not in mock.events and ("pair",) not in mock.events and mock.paired
    assert ctl.state["state"] == "connected"


def test_picking_the_connected_flipper_changes_nothing(mock, ctl):
    ctl.submit("connect").wait(30)
    mock.events.clear()
    ui = ScriptedPairing(lambda: mock.displayed_code, confirm=True)
    ctl.submit("pair", ui=ui).wait(60)
    assert ui.steps == ["searching", "works"]
    assert not {("remove_device",), ("pair",), ("disconnect",), ("connect",)} & set(mock.events)
    assert ctl.state["state"] == "connected"


def test_broken_pairing_is_replaced_only_when_confirmed(mock, ctl):
    ctl.PAIR_TEST_BUDGET_S = 3
    mock.fail_connect_times = 10 ** 6   # say the Flipper forgot this computer: connecting with the old pairing fails

    def yes():
        mock.fail_connect_times = 0
        return True
    ui = ScriptedPairing(lambda: mock.displayed_code, confirm=yes)
    ctl.submit("pair", ui=ui).wait(90)
    assert ui.steps == ["searching", "testing", "confirm", "removing", "pairing", "done"]
    assert ("remove_device",) in mock.events and mock.paired and ctl.state["state"] == "connected"


def test_broken_pairing_is_kept_when_not_confirmed(mock, ctl):
    ctl.PAIR_TEST_BUDGET_S = 3
    mock.fail_connect_times = 10 ** 6
    ui = ScriptedPairing(lambda: mock.displayed_code, confirm=False)
    with pytest.raises(fr.PairingCancelled):
        ctl.submit("pair", ui=ui).wait(90)
    assert ui.steps == ["searching", "testing", "confirm", "cancelled"]
    assert ("remove_device",) not in mock.events and mock.paired


def test_pair_wrong_code(mock, ctl):
    mock.paired = False
    ui = ScriptedPairing(lambda: "000000" if mock.displayed_code != "000000" else "111111")
    with pytest.raises(fr.PairingProblem) as e:
        ctl.submit("pair", ui=ui).wait(60)
    assert "didn't match" in str(e.value)
    assert not mock.paired and ("unregister_agent",) in mock.events and ui.steps[-1] == "error"


class BackingOut(ScriptedPairing):
    """Opens pairing, looks at the list, then cancels without choosing anything."""

    def __init__(self):
        super().__init__(lambda: None)
        self._gone = False

    def wait_choice(self, timeout):
        time.sleep(0.2)
        self._gone = True
        return None

    def cancelled(self):
        return self._gone


def test_cancel_pairing_reconnects(mock, ctl):
    ctl.submit("connect").wait(30)
    assert ctl.state["state"] == "connected"
    ui = BackingOut()
    with pytest.raises(fr.PairingCancelled):
        ctl.submit("pair", ui=ui).wait(60)
    assert ui.steps[-1] == "cancelled" and mock.paired
    assert ctl.state["state"] == "connected"          # back where it was


def test_cancel_pairing_keeps_the_error(mock, ctl):
    mock.paired = False
    with pytest.raises(fr.PairingProblem):
        ctl.submit("connect").wait(30)
    with pytest.raises(fr.PairingCancelled):
        ctl.submit("pair", ui=BackingOut()).wait(60)
    assert ctl.state["state"] == "error" and ctl.state["kind"] == "pairing"   # the "Pair a Flipper" panel stays


def test_failed_repair_says_to_pair_again(mock, ctl):
    ctl.PAIR_TEST_BUDGET_S = 3
    mock.fail_connect_times = 10 ** 6   # the old pairing doesn't work, and the user chose to pair again
    ui = ScriptedPairing(lambda: "000000" if mock.displayed_code != "000000" else "111111", confirm=True)
    with pytest.raises(fr.PairingProblem):
        ctl.submit("pair", ui=ui).wait(60)
    assert ("remove_device",) in mock.events and not mock.paired
    assert ctl.state["state"] == "error" and ctl.state["kind"] == "not_found"
    assert "Pair a Flipper" in ctl.state["message"]


def test_prefers_the_flipper_paired_last(monkeypatch):
    c = fr.Controller()
    devs = [dict(address="80:E1:26:00:00:01", name="Flipper A", paired=True, connected=False),
            dict(address="80:E1:26:00:00:02", name="Flipper B", paired=True, connected=False)]
    monkeypatch.setattr(fr, "find_flippers", lambda bus: devs)
    assert c._pick_device()["name"] == "Flipper A"
    c._last_address = "80:e1:26:00:00:02"
    assert c._pick_device()["name"] == "Flipper B"


def test_kde_style_agent_cant_pair(mock):
    """Why KDE fails: an agent that can't type codes gets 'Just Works', which the Flipper refuses."""
    mock.paired = False
    bus = fr.DBusConnection.system()
    bus.export("/test/agent", fr.IFACE_AGENT, lambda member, args: ("", []))
    bus.call(fr.BLUEZ, "/org/bluez", fr.IFACE_AGENT_MANAGER, "RegisterAgent", "os", ["/test/agent", "DisplayYesNo"])
    with pytest.raises(fr.DBusError) as e:
        bus.call(fr.BLUEZ, mock.dev_path, fr.IFACE_DEVICE, "Pair")
    assert e.value.name.endswith("AuthenticationFailed")
    bus.close()


def test_cli_pair(mock, monkeypatch, capsys):
    mock.paired = False
    monkeypatch.setattr("builtins.input", lambda prompt="": mock.displayed_code)
    assert fr.main(["pair"]) == 0
    out = capsys.readouterr().out
    assert "Paired with" in out and "Connected and working" in out
    assert mock.paired and not mock.device._connected  # paired, then left free for the phone


def _sse_reader(srv, events):
    def run():
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/events?t=tok")
        with urllib.request.urlopen(req, timeout=60) as r:
            name = None
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    events.append((name, json.loads(line[6:])))
    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.3)


def _wait_for(events, pred, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, data in list(events):
            if pred(name, data):
                return data
        time.sleep(0.05)
    raise AssertionError("event never arrived")


def test_web_pairing_flow(mock, server):
    mock.paired = False
    events = []
    _sse_reader(server, events)
    assert _req(server, "/api/pair", {"action": "start"})[0] == 200
    found = _wait_for(events, lambda n, d: n == "pairing" and any(x["nearby"] for x in d.get("devices", [])))
    address = [x for x in found["devices"] if x["nearby"]][0]["address"]
    _req(server, "/api/pair", {"action": "choose", "address": address})
    _wait_for(events, lambda n, d: n == "pairing" and d["step"] == "code")
    assert _req(server, "/api/pair", {"action": "code", "code": "12x"})[0] == 400   # not 6 digits
    _req(server, "/api/pair", {"action": "code", "code": mock.displayed_code})
    _wait_for(events, lambda n, d: n == "pairing" and d["step"] == "done")
    _wait_for(events, lambda n, d: n == "status" and d["state"] == "connected")
    assert mock.paired


def test_web_pairing_cancel(mock, server):
    mock.paired = False
    events = []
    _sse_reader(server, events)
    _req(server, "/api/pair", {"action": "start"})
    found = _wait_for(events, lambda n, d: n == "pairing" and any(x["nearby"] for x in d.get("devices", [])))
    _req(server, "/api/pair", {"action": "choose", "address": found["devices"][0]["address"]})
    _wait_for(events, lambda n, d: n == "pairing" and d["step"] == "code")
    _req(server, "/api/pair", {"action": "cancel"})
    _wait_for(events, lambda n, d: n == "pairing" and d["step"] == "cancelled")
    old = server.controller._pairing
    assert old is None or old.finished      # so pressing "Pair a Flipper" again right away starts afresh
    deadline = time.time() + 2              # the job tidies up (unregisters its agent) just after saying so
    while server.controller._pairing is not None and time.time() < deadline:
        time.sleep(0.05)
    assert not mock.paired and server.controller._pairing is None


def test_settings_saved_and_put_in_page(server):
    status, body = _req(server, "/api/settings", {"pinned_only": True, "theme": "dark", "folder": "/ext/subghz/Fan"})
    assert body["settings"] == {"pinned_only": True, "theme": "dark", "folder": "/ext/subghz/Fan", "window": {},
                                "show": "all", "folders": {"subghz": "/ext/subghz/Fan"}}
    _req(server, "/api/settings", {"theme": "pink", "folder": "/etc"})            # ignored
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as r:
        page = r.read().decode()
    assert ('const INITIAL = {"pinned_only": true, "theme": "dark", "folder": "/ext/subghz/Fan", "show": "all", '
            '"folders": {"subghz": "/ext/subghz/Fan"}, "window": {}};') in page
    assert fr.Controller().settings() == {"pinned_only": True, "theme": "dark", "folder": "/ext/subghz/Fan",
                                          "show": "all", "folders": {"subghz": "/ext/subghz/Fan"}, "window": {}}


def test_pins_come_with_the_page(server):
    _req(server, "/api/favorite", {"path": "/ext/subghz/Fan/Fan_Off.sub", "on": True, "label": "Fan_Off.sub"})
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as r:
        page = r.read().decode()
    line = next(l for l in page.splitlines() if l.startswith("const INITIAL_PINS = "))
    pins = json.loads(line[len("const INITIAL_PINS = "):-1])
    assert [p["path"] for p in pins] == ["/ext/subghz/Fan/Fan_Off.sub"]
    assert "{{" not in page


def test_window_opens_at_a_size_that_fits(monkeypatch):
    fav = lambda n: [{"path": f"/ext/subghz/{i}.sub"} for i in range(n)]
    assert fr.initial_window_size({"favorites": fav(5)}) == fr.NORMAL_WINDOW           # browsing: the usual size
    pinned = lambda n: fr.initial_window_size({"ui": {"pinned_only": True}, "favorites": fav(n)})
    assert pinned(0)[0] == pinned(2)[0] < pinned(3)[0] <= fr.NORMAL_WINDOW[0]           # 2 columns, then 3
    assert pinned(1)[1] == pinned(2)[1] < pinned(4)[1] < pinned(7)[1] <= fr.NORMAL_WINDOW[1]
    assert pinned(40)[1] == fr.NORMAL_WINDOW[1]
    monkeypatch.delenv("FLIPPER_REMOTE_BROWSER", raising=False)
    monkeypatch.setattr(fr, "browser_candidates", lambda: [("brave", "chromium", ["/usr/bin/brave"])])
    monkeypatch.setattr(fr, "initial_window_size", lambda: (740, 384))
    cmd = fr.browser_command("http://127.0.0.1:8730/", False)
    assert "--app=http://127.0.0.1:8730/?fit=740x384" in cmd and "--window-size=740,384" in cmd
    # its own profile, so an already-open browser can't ignore the size; and no welcome page or wallet prompts
    assert f"--user-data-dir={fr.CACHE_DIR}/browser" in cmd and set(fr.WINDOW_FLAGS) <= set(cmd)
    cmd = fr.browser_command("http://127.0.0.1:8730/", True)
    assert "--app=http://127.0.0.1:8730/" in cmd and "--start-fullscreen" in cmd       # Game Mode: never resized
    assert not any("fit" in c or "window-size" in c for c in cmd)
    monkeypatch.setattr(fr, "browser_candidates",
                        lambda: [("com.brave.Browser", "chromium", ["flatpak", "run", "com.brave.Browser"])])
    cmd = fr.browser_command("http://127.0.0.1:8730/", False)    # a Flatpak can only write in its own folder
    assert "--user-data-dir=" + os.path.expanduser("~/.var/app/com.brave.Browser/cache/flipper-remote") in cmd


def test_window_size_is_learned(server):
    _req(server, "/api/settings", {"window": {"frame": [0, 37], "fit": [740, 350], "pins": ""}})
    _req(server, "/api/settings", {"window": {"frame": [-5, 9999], "fit": [1, 2], "pins": 5}})   # nonsense: ignored
    cfg = fr._load_json(fr.CONFIG_FILE, {})
    assert cfg["ui"]["window"] == {"frame": [0, 37], "fit": [740, 350], "pins": ""}
    cfg["ui"]["pinned_only"] = True
    assert fr.initial_window_size(cfg) == (740, 387)               # exactly what the page measured, plus the frame
    cfg["favorites"] = [{"path": "/ext/subghz/A.sub"}]
    assert fr.initial_window_size(cfg) == (740, 225 + 119 + 37)    # pins changed: worked out, with the real frame
    cfg["ui"]["pinned_only"] = False
    assert fr.initial_window_size(cfg) == fr.NORMAL_WINDOW


# --- one copy of the app at a time, and saves that never undo another copy's -------------------------------

APP_SCRIPT = os.path.join(os.path.dirname(fr.__file__), "flipper_remote.py")


def _start_copy():
    """Run the app as its own process (no window), as a launch would, and wait until it answers."""
    p = subprocess.Popen([sys.executable, APP_SCRIPT, "--no-browser"],
                         env=dict(os.environ, FLIPPER_REMOTE_NO_DIALOGS="1"),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        inst = fr.running_instance()
        if inst and int(inst["pid"]) == p.pid:
            return p, inst
        time.sleep(0.1)
    p.kill()
    raise AssertionError("the copy didn't start")


def _end(p):
    if p.poll() is None:
        try:
            os.kill(p.pid, signal.SIGCONT)
        except OSError:
            pass
        p.terminate()
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def test_saves_keep_what_another_copy_saved(mock):
    a, b = fr.Controller(), fr.Controller()   # two copies, each with its own idea of the saved settings
    b.set_favorite("/ext/subghz/Fan/Fan_Off.sub", True)
    a.update_settings({"theme": "dark"})      # used to write a's pin-less copy back over b's pin
    a.set_favorite("/ext/subghz/Lights/On.sub", True)
    saved = fr._load_json(fr.CONFIG_FILE, {})
    assert [f["path"] for f in saved["favorites"]] == ["/ext/subghz/Fan/Fan_Off.sub", "/ext/subghz/Lights/On.sub"]
    assert saved["ui"]["theme"] == "dark"
    assert [f["path"] for f in a.snapshot()["favorites"]] == [f["path"] for f in saved["favorites"]]


def test_launch_waits_for_a_copy_that_isnt_answering(mock):
    p, _ = _start_copy()
    try:
        os.kill(p.pid, signal.SIGSTOP)
        threading.Timer(2.0, os.kill, (p.pid, signal.SIGCONT)).start()   # back after 2 s
        lock, existing = fr._claim_instance(wait=10)
        assert lock is None and existing and int(existing["pid"]) == p.pid   # used, not started alongside
    finally:
        _end(p)


def test_launch_ends_a_stuck_copy(mock):
    p, _ = _start_copy()
    try:
        os.kill(p.pid, signal.SIGSTOP)        # never comes back
        lock, existing = fr._claim_instance(wait=2)
        assert lock is not None and existing is None
        fr._release_lock(lock)
        p.wait(10)
        assert p.returncode == -signal.SIGKILL
    finally:
        _end(p)


def test_launch_waits_for_a_closing_copy(mock):
    mock.connect_delay = 6                    # it's busy connecting, so closing takes a while
    p, _ = _start_copy()
    try:
        time.sleep(0.5)
        os.kill(p.pid, signal.SIGTERM)
        time.sleep(0.3)
        lock, existing = fr._claim_instance(wait=30)
        assert existing is None and lock is not None   # waited for it to go, instead of using it
        assert p.poll() is not None
        fr._release_lock(lock)
    finally:
        _end(p)


def test_launch_waits_for_a_command_line_tool(mock):
    tool = fr._single_instance_lock("cli")   # e.g. `flipper-remote send` using the Flipper
    assert tool is not None
    threading.Timer(1.5, fr._release_lock, (tool,)).start()
    t0 = time.monotonic()
    lock, existing = fr._claim_instance(wait=0.5)   # a busy tool is waited for, never ended
    assert lock is not None and existing is None and time.monotonic() - t0 >= 1.4
    fr._release_lock(lock)


# ---------------------------------------------------------------------------------------------
# One window at a time, and a record of what set each action off
# ---------------------------------------------------------------------------------------------

def _window(srv, name):
    """An event stream for a window called name; returns the list its event names land in."""
    events = []

    def run():
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/events?t=tok&w={name}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                for raw in r:
                    line = raw.decode().strip()
                    if line.startswith("event: "):
                        events.append(line[7:])
        except OSError:
            pass
    threading.Thread(target=run, daemon=True).start()
    return events


def _until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


def test_new_window_closes_the_old_ones_once_it_is_up(server):
    server.newest_window = "aaa111"            # the window the app opened when it started
    first = _window(server, "aaa111")
    assert _until(lambda: "hello" in first)
    stray = _window(server, "zzz999")          # any other window: closed, since the newest one is open
    assert _until(lambda: "replaced" in stray)
    assert "replaced" not in first
    # Opening the app again: the open window stays until the new one is up, then it's told to close.
    assert _req(server, "/api/replace-windows", {"window": "bbb222"}) == (200, {"ok": True})
    time.sleep(0.3)
    assert "replaced" not in first
    second = _window(server, "bbb222")
    assert _until(lambda: "replaced" in first)
    time.sleep(0.2)
    assert "replaced" not in second and server.newest_window == "bbb222"


def test_old_window_stays_if_the_new_one_never_opens(server):
    server.newest_window = "aaa111"
    first = _window(server, "aaa111")
    assert _until(lambda: "hello" in first)
    _req(server, "/api/replace-windows", {"window": "bbb222"})
    time.sleep(0.5)
    late = _window(server, "ccc333")          # neither the newest nor the window that was open
    assert _until(lambda: "hello" in late)
    time.sleep(0.3)
    assert "replaced" not in first and "replaced" not in late


def test_actions_record_what_set_them_off(server, caplog):
    caplog.set_level(logging.INFO, logger="flipper-remote")

    def post(path, body, window="abc123"):
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=json.dumps(body).encode(),
                                     headers={"X-Token": "tok", "X-Window": window,
                                              "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    post("/api/favorite", {"path": "/ext/subghz/Fan/Fan_Off.sub", "on": True, "via": "touch@512,233"})
    post("/api/favorite", {"path": "/ext/subghz/Fan/Fan_Off.sub", "on": False, "via": "key:Enter:held"})
    post("/api/settings", {"pinned_only": True, "via": "mouse@10,20"})
    post("/api/settings", {"pinned_only": True, "via": "mouse@10,20"})     # no change: nothing to say
    post("/api/send", {"path": "/ext/subghz/Fan/Fan_Off.sub", "via": "controller", "wait": True})
    post("/api/pair", {"action": "start", "via": "<script>key:Enter"})
    post("/api/pair", {"action": "cancel", "via": "key:Escape"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Pinned /ext/subghz/Fan/Fan_Off.sub (touch@512,233, window abc123)" in text
    assert "Unpinned /ext/subghz/Fan/Fan_Off.sub (key:Enter:held, window abc123)" in text
    assert text.count("Pinned only switched on (mouse@10,20, window abc123)") == 1
    assert "Sending /ext/subghz/Fan/Fan_Off.sub (" in text and "; controller, window abc123)" in text
    assert "Pair a Flipper” opened (scriptkey:Enter, window abc123)" in text   # sanitised
    assert "Pairing: cancelled in the window (key:Escape, window abc123)" in text


def test_launcher_names_the_window(monkeypatch):
    monkeypatch.setattr(fr, "browser_candidates", lambda: [("brave", "chromium", ["brave"])])
    monkeypatch.setattr(fr, "initial_window_size", lambda: (1100, 469))
    cmd = fr.browser_command("http://127.0.0.1:8730/", False, "a1b2c3")
    assert "--app=http://127.0.0.1:8730/?fit=1100x469&w=a1b2c3" in cmd
    cmd = fr.browser_command("http://127.0.0.1:8730/", True, "a1b2c3")
    assert "--app=http://127.0.0.1:8730/?w=a1b2c3" in cmd


def test_window_only_takes_the_controller_in_game_mode(monkeypatch):
    # Charlie's log: in Desktop Mode, with his controls remapped in Steam, the window still read the controller
    # itself and unpinned all five signals ("Unpinned … (controller, …)"). Now it only does that when asked to.
    monkeypatch.setattr(fr, "browser_candidates", lambda: [("brave", "chromium", ["brave"])])
    monkeypatch.setattr(fr, "initial_window_size", lambda: (1100, 469))
    desktop = fr.browser_command("http://127.0.0.1:8730/", False, "a1b2c3")
    assert not any("pad=1" in arg for arg in desktop)
    game = fr.browser_command("http://127.0.0.1:8730/", True, "a1b2c3", controller=True)
    assert "--app=http://127.0.0.1:8730/?w=a1b2c3&pad=1" in game
    forced = fr.browser_command("http://127.0.0.1:8730/", False, "a1b2c3", controller=True)
    assert "--app=http://127.0.0.1:8730/?fit=1100x469&w=a1b2c3&pad=1" in forced
    page = open(os.path.join(os.path.dirname(fr.__file__), "ui.html")).read()
    assert 'get("pad") === "1"' in page and "if (PAD) {" in page



# ---------------------------------------------------------------------------------------------
# Infrared, 125 kHz RFID, NFC and iButton
# ---------------------------------------------------------------------------------------------

def _more_kinds(mock):
    mock.fs.update({
        "/ext/infrared": None, "/ext/infrared/assets": None, "/ext/infrared/assets/tv.ir": IR_REMOTE,
        "/ext/infrared/TV.ir": IR_REMOTE, "/ext/infrared/notes.txt": b"hello",
        "/ext/nfc": None, "/ext/nfc/assets": None, "/ext/nfc/Badge.nfc": NFC_CARD, "/ext/nfc/Badge.shd": b"x",
        "/ext/lfrfid": None, "/ext/lfrfid/Gym.rfid": RFID_KEY,
        "/ext/ibutton": None, "/ext/ibutton/Intercom.ibtn": IBUTTON_KEY,
    })


def _until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


def test_saved_files_are_read_for_each_kind():
    remote = fr.parse_saved_file("infrared", IR_REMOTE)
    assert remote["valid"] and [(x["name"], x["protocol"]) for x in remote["signals"]] == \
        [("Power", "NEC"), ("Vol_up", "NEC"), ("Input", "RAW")]
    assert fr.parse_saved_file("nfc", NFC_CARD) == {"valid": True, "protocol": "NTAG/Ultralight",
                                                    "data": "04 A1 B2 C3 D4 E5 F6"}
    assert fr.parse_saved_file("lfrfid", RFID_KEY) == {"valid": True, "protocol": "EM4100", "data": "01 23 45 67 89"}
    assert fr.parse_saved_file("ibutton", IBUTTON_KEY)["protocol"] == "DS1990"
    assert not fr.parse_saved_file("nfc", RFID_KEY)["valid"]
    assert fr.file_kind("/ext/nfc/Badge.nfc") == "nfc" and fr.file_kind("/ext/nfc/Badge.shd") is None
    assert fr.file_kind("/ext/infrared") is None and fr.kind_of("/ext/infrared") == "infrared"
    assert fr.split_key("/ext/infrared/TV.ir#Vol #2") == ("/ext/infrared/TV.ir", "Vol #2")
    assert fr.split_key("/ext/nfc/A#B.nfc") == ("/ext/nfc/A#B.nfc", None)


def test_command_line_paths():
    assert fr.resolve_saved_path("Fan/Fan_Off") == "/ext/subghz/Fan/Fan_Off.sub"      # as before
    assert fr.resolve_saved_path("nfc/Badge") == "/ext/nfc/Badge.nfc"
    assert fr.resolve_saved_path("infrared/TV.ir") == "/ext/infrared/TV.ir"
    assert fr.resolve_saved_path("/ext/lfrfid/Gym") == "/ext/lfrfid/Gym.rfid"
    assert fr.resolve_saved_path("ibutton/", folder=True) == "/ext/ibutton"


def test_each_kind_lists_its_own_files(mock, ctl):
    _more_kinds(mock)
    ir = ctl.submit("list", path="/ext/infrared").wait(30)
    assert ir["dirs"] == [] and [f["name"] for f in ir["files"]] == ["TV.ir"]      # assets and other files hidden
    nfc = ctl.submit("list", path="/ext/nfc").wait(30)
    assert nfc["dirs"] == [] and [f["name"] for f in nfc["files"]] == ["Badge.nfc"]
    assert [f["name"] for f in ctl.submit("list", path="/ext/lfrfid").wait(30)["files"]] == ["Gym.rfid"]
    del mock.fs["/ext/ibutton"], mock.fs["/ext/ibutton/Intercom.ibtn"]
    assert ctl.submit("list", path="/ext/ibutton").wait(30) ["files"] == []      # nothing saved yet: just empty
    assert ctl.listing("/ext") ["dirs"] == ["subghz", "infrared", "lfrfid", "nfc", "ibutton"]
    for bad in ("/ext/apps", "/ext/subghz/../apps", "/ext/nfc/Badge.nfc"):
        with pytest.raises(fr.FlipperError):
            ctl.listing(bad)


def test_infrared_remote_opens_like_a_folder(mock, ctl):
    _more_kinds(mock)
    ctl.submit("list", path="/ext/infrared").wait(30)
    ctl.submit("list", path="/ext/infrared/TV.ir").wait(30)
    remote = ctl.listing("/ext/infrared/TV.ir")
    assert remote["remote"] and [x["name"] for x in remote["signals"]] == ["Power", "Vol_up", "Input"]
    folder = ctl.listing("/ext/infrared")
    assert folder["files"][0]["meta"] == {"buttons": 3}


def test_infrared_button_tap_sends_once(mock, ctl):
    _more_kinds(mock)
    ctl.send("/ext/infrared/TV.ir", signal="Power").wait(30)
    assert [e for e in app_events(mock)] == [("start", "Infrared", "RPC"), ("load", "/ext/infrared/TV.ir"),
                                             ("press", "Power"), ("release",), ("exit",)]
    (f, name, a, b), = mock.ir_log
    assert (f, name) == ("/ext/infrared/TV.ir", "Power") and b - a < 0.5
    assert mock.app is None and not mock.violations


def test_infrared_button_held_keeps_sending(mock, ctl):
    _more_kinds(mock)
    job = ctl.hold_start("/ext/infrared/TV.ir", signal="Vol_up")
    assert _until(lambda: mock.ir_on is not None, 15)
    time.sleep(1.0)
    ctl.hold_stop("/ext/infrared/TV.ir#Vol_up")
    job.wait(30)
    (f, name, a, b), = mock.ir_log
    assert name == "Vol_up" and b - a >= 0.8


def test_infrared_button_that_is_gone(mock, ctl):
    _more_kinds(mock)
    with pytest.raises(fr.FlipperError, match="isn't a button of the TV remote"):
        ctl.send("/ext/infrared/TV.ir", signal="Mute").wait(30)
    assert mock.app is None


@pytest.mark.parametrize("path,app", [("/ext/nfc/Badge.nfc", "NFC"), ("/ext/lfrfid/Gym.rfid", "125 kHz RFID"),
                                      ("/ext/ibutton/Intercom.ibtn", "iButton")])
def test_tap_emulates_until_tapped_again(mock, ctl, path, app):
    _more_kinds(mock)
    job = ctl.send(path)
    assert _until(lambda: mock.emu_on is not None, 15)
    time.sleep(0.6)
    ctl.hold_stop(path)
    job.wait(30)
    (f, a, b, why), = mock.emu_log
    assert f == path and why == "exit" and 0.5 <= b - a < 3
    assert [e for e in app_events(mock)] == [("start", app, "RPC"), ("load", path), ("exit",)]
    assert mock.app is None and not mock.violations


def test_emulation_stops_by_itself(mock, ctl, monkeypatch):
    _more_kinds(mock)
    monkeypatch.setattr(fr, "EMULATE_S", 1)
    t0 = time.monotonic()
    ctl.send("/ext/lfrfid/Gym.rfid").wait(30)
    (f, a, b, why), = mock.emu_log
    assert 0.9 <= b - a < 2 and time.monotonic() - t0 < 8


def test_emulation_ends_when_closed_on_the_flipper(mock, ctl, caplog):
    caplog.set_level(logging.INFO, logger="flipper-remote")
    _more_kinds(mock)
    job = ctl.send("/ext/nfc/Badge.nfc")
    assert _until(lambda: mock.emu_on is not None, 15)
    mock.press_back()
    job.wait(30)
    assert mock.emu_log[0][3] == "back" and ("exit",) not in mock.events
    assert any("Stopped emulating /ext/nfc/Badge.nfc" in r.getMessage() and "closed on the Flipper" in r.getMessage()
               for r in caplog.records)


def test_pressing_something_else_stops_the_emulation(mock, ctl):
    _more_kinds(mock)
    first = ctl.send("/ext/ibutton/Intercom.ibtn")
    assert _until(lambda: mock.emu_on is not None, 15)
    ctl.send("/ext/subghz/Fan/Fan_Off.sub").wait(30)
    first.wait(5)
    assert mock.emu_log[0][0] == "/ext/ibutton/Intercom.ibtn" and len(mock.tx_log) == 1
    assert mock.emu_log[0][2] <= mock.tx_log[0][1]          # stopped before the signal went out


def test_any_kind_can_be_pinned_and_remote_buttons_on_their_own(mock, ctl):
    _more_kinds(mock)
    ctl.set_favorite("/ext/subghz/Fan/Fan_Off.sub", True)
    ctl.set_favorite("/ext/infrared/TV.ir", True, signal="Power")
    ctl.set_favorite("/ext/infrared/TV.ir", True, signal="Vol_up", label="Living room louder")
    ctl.set_favorite("/ext/nfc/Badge.nfc", True)
    view = ctl._favorites_view()
    assert [f["key"] for f in view] == ["/ext/subghz/Fan/Fan_Off.sub", "/ext/infrared/TV.ir#Power",
                                        "/ext/infrared/TV.ir#Vol_up", "/ext/nfc/Badge.nfc"]
    assert [f["kind"] for f in view] == ["subghz", "infrared", "infrared", "nfc"]
    assert [f["label"] for f in view][1:3] == ["TV Power", "Living room louder"]
    ctl.set_favorite("/ext/infrared/TV.ir", False, signal="Power")
    assert [f["key"] for f in ctl._favorites_view()] == ["/ext/subghz/Fan/Fan_Off.sub", "/ext/infrared/TV.ir#Vol_up",
                                                         "/ext/nfc/Badge.nfc"]
    saved = json.load(open(fr.CONFIG_FILE))["favorites"]
    assert saved[1] == {"path": "/ext/infrared/TV.ir", "signal": "Vol_up", "label": "Living room louder"}


def test_what_to_show_is_remembered(mock, ctl):
    assert ctl.settings()["show"] == "all"                   # until something is picked: everything
    ctl.update_settings({"folder": "/ext/subghz/Fan"})        # the old setting still counts for Sub-GHz
    ctl.update_settings({"show": "nfc", "folders": {"nfc": "/ext/nfc/Work", "infrared": "/ext/subghz/Fan",
                                                    "all": "/ext", "bogus": "/ext/nfc"}})
    again = fr.Controller()
    settings = again.settings()
    assert settings["show"] == "nfc"
    assert settings["folders"] == {"nfc": "/ext/nfc/Work", "all": "/ext", "subghz": "/ext/subghz/Fan"}
    ctl.update_settings({"show": "everything"})
    assert fr.Controller().settings()["show"] == "nfc"


def test_window_opens_sized_for_the_pins_shown(mock, ctl):
    for p in ("/ext/subghz/A.sub", "/ext/subghz/B.sub", "/ext/nfc/C.nfc", "/ext/nfc/D.nfc", "/ext/lfrfid/E.rfid"):
        ctl.set_favorite(p, True)
    ctl.set_favorite("/ext/infrared/TV.ir", True, signal="Power")
    sizes = {}
    for show in ("subghz", "nfc", "all"):
        ctl.update_settings({"pinned_only": True, "show": show})
        sizes[show] = fr.initial_window_size()
    assert sizes["subghz"][0] < sizes["nfc"][0] or sizes["subghz"] == sizes["nfc"]   # 2 pins: two columns
    assert sizes["all"][1] > sizes["subghz"][1]                                     # 6 pins: two rows


def test_web_api_for_the_other_kinds(server, mock):
    _more_kinds(mock)
    assert _req(server, "/api/send", {"path": "/ext/infrared/TV.ir"})[0] == 400          # which button?
    assert _req(server, "/api/send", {"path": "/ext/apps/x.fap"})[0] == 400
    code, res = _req(server, "/api/send", {"path": "/ext/infrared/TV.ir", "signal": "Power", "wait": True})
    assert code == 200 and mock.ir_log[0][1] == "Power"
    code, res = _req(server, "/api/send", {"path": "/ext/nfc/Badge.nfc"})
    assert code == 200 and _until(lambda: mock.emu_on is not None, 15)
    assert _req(server, "/api/stop", {"path": "/ext/nfc/Badge.nfc"}) == (200, {"ok": True})
    assert _until(lambda: mock.emu_on is None and len(mock.emu_log) == 1, 10)
    code, res = _req(server, "/api/favorite", {"path": "/ext/infrared/TV.ir", "signal": "Input", "on": True,
                                              "label": "TV Input"})
    assert code == 200 and res["favorites"][-1]["key"] == "/ext/infrared/TV.ir#Input"
    _req(server, "/api/list?path=/ext/infrared")
    _until(lambda: "/ext/infrared" in server.controller._refreshed, 15)
    _req(server, "/api/list?path=/ext/infrared/TV.ir")
    assert _until(lambda: (_req(server, "/api/list?path=/ext/infrared/TV.ir")[1]["listing"].get("signals") or []), 15)
    code, res = _req(server, "/api/list?path=/ext")
    assert res["listing"]["all"] and len(res["listing"]["dirs"]) == 5


def test_all_is_the_top_choice_and_the_default(server):
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as r:
        page = r.read().decode()
    options = re.findall(r'<option value="([a-z]+)">', page)
    assert options == ["all", "subghz", "infrared", "lfrfid", "nfc", "ibutton"]
    assert '"show": "all"' in page                        # never picked: All
    _req(server, "/api/settings", {"show": "nfc"})
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as r:
        assert '"show": "nfc"' in r.read().decode()       # picked once: remembered
