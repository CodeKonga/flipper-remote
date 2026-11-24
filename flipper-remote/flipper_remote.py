#!/usr/bin/env python3
"""
Flipper Remote - use your Flipper Zero's saved Sub-GHz signals, Infrared remotes, 125 kHz RFID
keys, NFC cards and iButton keys from a Steam Deck (or any Linux PC with BlueZ) over Bluetooth LE.

It does the same thing the official Flipper mobile app does when you press "Send" or "Emulate":
it talks to the Flipper's Bluetooth serial service, speaks the Flipper's protobuf RPC protocol,
opens the right app (Sub-GHz, Infrared, 125 kHz RFID, NFC or iButton) in remote mode, loads the
file, presses and releases (or emulates until told to stop) and closes the app again.

No third-party packages are needed: this file talks to BlueZ over the system D-Bus
with a tiny built-in D-Bus client, encodes the handful of protobuf messages it needs
by hand, and serves its touch-friendly UI from a local web server (127.0.0.1 only).

Works with official firmware and with Xtreme / Unleashed / Momentum style firmwares.
"""

from __future__ import annotations

import argparse
import errno
import itertools
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "Flipper Remote"
APP_ID = "flipper-remote"
VERSION = "1.2.2"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), APP_ID)
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), APP_ID)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CACHE_FILE = os.path.join(CACHE_DIR, "cache.json")
INSTANCE_FILE = os.path.join(CACHE_DIR, "instance.json")
LOG_FILE = os.path.join(CACHE_DIR, "flipper-remote.log")

SUBGHZ_ROOT = "/ext/subghz"
DEFAULT_PORT = 8730

# The kinds of saved file the app works with: the Flipper's folder for them, their extension, the Flipper app
# that plays them (the official name first, then names other firmwares use), what to call them, and whether they
# are sent like a button press (Sub-GHz, Infrared) or emulated until stopped (RFID, NFC, iButton).
KINDS = {
    "subghz": {"root": "/ext/subghz", "ext": ".sub", "apps": ("Sub-GHz", "SubGHz", "subghz"),
               "title": "Sub-GHz", "emulate": False, "filetype": "flipper subghz"},
    "infrared": {"root": "/ext/infrared", "ext": ".ir", "apps": ("Infrared", "infrared"),
                 "title": "Infrared", "emulate": False, "filetype": "ir signals file"},
    "lfrfid": {"root": "/ext/lfrfid", "ext": ".rfid", "apps": ("125 kHz RFID", "RFID", "lfrfid"),
               "title": "RFID 125 kHz", "emulate": True, "filetype": "flipper rfid key"},
    "nfc": {"root": "/ext/nfc", "ext": ".nfc", "apps": ("NFC", "nfc"),
            "title": "NFC", "emulate": True, "filetype": "flipper nfc device"},
    "ibutton": {"root": "/ext/ibutton", "ext": ".ibtn", "apps": ("iButton", "ibutton"),
                "title": "iButton", "emulate": True, "filetype": "flipper ibutton key"},
}
ALL_ROOT = "/ext"            # "All" shows the kinds' folders side by side, under this
SHOW_CHOICES = ("all",) + tuple(KINDS)   # "All" first: the default, and the top of the menu
HIDDEN_AT_ROOT = {"assets"}  # the Flipper's own libraries (universal remotes, keys dictionaries...)
EMULATE_S = 30               # how long a tap emulates a card or key, unless it's tapped again


def kind_of(path: str):
    """Which kind a folder or file belongs to, by where it lives; None if it's none of them."""
    for kind, k in KINDS.items():
        if path == k["root"] or path.startswith(k["root"] + "/"):
            return kind
    return None


def file_kind(path: str):
    """The kind of a saved file (a .sub in /ext/subghz, an .ir in /ext/infrared...), or None."""
    kind = kind_of(path)
    return kind if kind and path != KINDS[kind]["root"] and path.lower().endswith(KINDS[kind]["ext"]) else None


def key_of(path: str, signal=None) -> str:
    """What the window calls one thing it can press: a file, or one button of an Infrared remote."""
    return f"{path}#{signal}" if signal else path


def split_key(key: str):
    m = re.match(r"(?s)(.*\.ir)#(.+)$", key, re.IGNORECASE)
    return (m.group(1), m.group(2)) if m else (key, None)

log = logging.getLogger(APP_ID)


# =====================================================================================
#  Errors (messages are written for the person using the app)
# =====================================================================================


class FlipperError(Exception):
    """An error with a message that can be shown directly to the user."""
    kind = ""


class NotFoundError(FlipperError):
    kind = "not_found"


class MissingOnFlipper(FlipperError):
    """A folder or file that isn't on the Flipper's SD card."""


class ConnectError(FlipperError):
    pass


class PairingProblem(ConnectError):
    """The Flipper and this computer aren't (properly) paired."""
    kind = "pairing"


class DisconnectedError(FlipperError):
    def __init__(self, message: str = "", pressed: bool = False):
        super().__init__(message or "The Flipper disconnected.")
        self.pressed = pressed


class BusyError(FlipperError):
    pass


class RegionError(FlipperError):
    pass


class RpcTimeout(FlipperError):
    pass


# =====================================================================================
#  Minimal D-Bus client (just enough for BlueZ)
# =====================================================================================


class DBusError(Exception):
    def __init__(self, name: str, message: str = ""):
        self.name = name or "org.freedesktop.DBus.Error.Failed"
        self.message = message or ""
        super().__init__(f"{self.name}: {self.message}" if self.message else self.name)


class Variant:
    __slots__ = ("signature", "value")

    def __init__(self, signature: str, value):
        self.signature = signature
        self.value = value

    def __repr__(self):
        return f"Variant({self.signature!r}, {self.value!r})"


_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "h": 4,
          "s": 4, "o": 4, "g": 1, "a": 4, "(": 8, "{": 8, "v": 1}
_FMT = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d", "h": "I"}


def _type_end(sig: str, i: int) -> int:
    c = sig[i]
    if c == "a":
        return _type_end(sig, i + 1)
    if c in "({":
        close = ")" if c == "(" else "}"
        i += 1
        while sig[i] != close:
            i = _type_end(sig, i)
        return i + 1
    if c not in _ALIGN:
        raise ValueError(f"bad D-Bus signature {sig!r}")
    return i + 1


def split_signature(sig: str):
    out, i = [], 0
    while i < len(sig):
        j = _type_end(sig, i)
        out.append(sig[i:j])
        i = j
    return out


class _Marshaller:
    def __init__(self):
        self.buf = bytearray()

    def align(self, n: int):
        self.buf += b"\0" * ((-len(self.buf)) % n)

    def write(self, t: str, v):
        c = t[0]
        if c in _FMT:
            self.align(_ALIGN[c])
            if c == "b":
                v = 1 if v else 0
            self.buf += struct.pack("<" + _FMT[c], v)
        elif c in "so":
            data = v.encode("utf-8")
            self.align(4)
            self.buf += struct.pack("<I", len(data)) + data + b"\0"
        elif c == "g":
            data = v.encode("ascii")
            self.buf += bytes([len(data)]) + data + b"\0"
        elif c == "v":
            if not isinstance(v, Variant):
                raise TypeError("D-Bus variants must be wrapped in Variant()")
            self.write("g", v.signature)
            self.write(v.signature, v.value)
        elif c == "a":
            et = t[1:]
            self.align(4)
            len_pos = len(self.buf)
            self.buf += b"\0\0\0\0"
            self.align(_ALIGN[et[0]])
            start = len(self.buf)
            if et == "y":
                self.buf += bytes(v)
            elif et[0] == "{":
                kt, vt = split_signature(et[1:-1])
                for key, val in v.items():
                    self.align(8)
                    self.write(kt, key)
                    self.write(vt, val)
            else:
                for item in v:
                    self.write(et, item)
            struct.pack_into("<I", self.buf, len_pos, len(self.buf) - start)
        elif c == "(":
            self.align(8)
            for st, item in zip(split_signature(t[1:-1]), v):
                self.write(st, item)
        else:
            raise ValueError(f"can't marshal type {t!r}")


class _Unmarshaller:
    def __init__(self, data: bytes, endian: str, pos: int = 0):
        self.d = data
        self.e = endian
        self.p = pos

    def align(self, n: int):
        self.p += (-self.p) % n

    def read(self, t: str):
        c = t[0]
        if c in _FMT:
            self.align(_ALIGN[c])
            fmt = self.e + _FMT[c]
            v = struct.unpack_from(fmt, self.d, self.p)[0]
            self.p += struct.calcsize(fmt)
            return bool(v) if c == "b" else v
        if c in "so":
            self.align(4)
            n = struct.unpack_from(self.e + "I", self.d, self.p)[0]
            self.p += 4
            s = bytes(self.d[self.p:self.p + n]).decode("utf-8", "replace")
            self.p += n + 1
            return s
        if c == "g":
            n = self.d[self.p]
            s = bytes(self.d[self.p + 1:self.p + 1 + n]).decode("ascii")
            self.p += n + 2
            return s
        if c == "v":
            return self.read(self.read("g"))
        if c == "a":
            self.align(4)
            n = struct.unpack_from(self.e + "I", self.d, self.p)[0]
            self.p += 4
            et = t[1:]
            self.align(_ALIGN[et[0]])
            end = self.p + n
            if et == "y":
                v = bytes(self.d[self.p:end])
                self.p = end
                return v
            if et[0] == "{":
                kt, vt = split_signature(et[1:-1])
                out = {}
                while self.p < end:
                    self.align(8)
                    k = self.read(kt)
                    out[k] = self.read(vt)
                return out
            items = []
            while self.p < end:
                items.append(self.read(et))
            return items
        if c == "(":
            self.align(8)
            return tuple(self.read(st) for st in split_signature(t[1:-1]))
        raise ValueError(f"can't unmarshal type {t!r}")


_METHOD_CALL, _METHOD_RETURN, _ERROR, _SIGNAL = 1, 2, 3, 4
_F_PATH, _F_INTERFACE, _F_MEMBER, _F_ERROR_NAME, _F_REPLY_SERIAL = 1, 2, 3, 4, 5
_F_DESTINATION, _F_SENDER, _F_SIGNATURE = 6, 7, 8
_FIELD_SIG = {1: "o", 2: "s", 3: "s", 4: "s", 5: "u", 6: "s", 7: "s", 8: "g", 9: "u"}


class _Message:
    __slots__ = ("type", "flags", "serial", "fields", "body")


def _encode_message(mtype: int, serial: int, fields: dict, signature: str = "", args=(), flags: int = 0) -> bytes:
    types = split_signature(signature)
    args = list(args)
    if len(types) != len(args):
        raise ValueError(f"signature {signature!r} needs {len(types)} args, got {len(args)}")
    body = _Marshaller()
    for t, v in zip(types, args):
        body.write(t, v)
    fields = dict(fields)
    if signature:
        fields[_F_SIGNATURE] = signature
    m = _Marshaller()
    m.buf += struct.pack("<4BII", ord("l"), mtype, flags, 1, len(body.buf), serial)
    m.write("a(yv)", [(code, Variant(_FIELD_SIG[code], val)) for code, val in sorted(fields.items())])
    m.align(8)
    return bytes(m.buf) + bytes(body.buf)


def _decode_message(data: bytes) -> _Message:
    endian = "<" if data[0:1] == b"l" else ">"
    msg = _Message()
    msg.type, msg.flags = data[1], data[2]
    body_len, msg.serial = struct.unpack_from(endian + "II", data, 4)
    r = _Unmarshaller(data, endian, 12)
    msg.fields = {code: val for code, val in r.read("a(yv)")}
    r.align(8)
    if len(data) - r.p != body_len:
        raise ValueError("D-Bus message length mismatch")
    msg.body = [r.read(t) for t in split_signature(msg.fields.get(_F_SIGNATURE, ""))]
    return msg


class _Waiter:
    __slots__ = ("event", "result", "error")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class _SignalHandler:
    __slots__ = ("callback", "interface", "member", "path", "path_namespace", "rule")

    def matches(self, path, interface, member) -> bool:
        if self.interface and interface != self.interface:
            return False
        if self.member and member != self.member:
            return False
        if self.path and path != self.path:
            return False
        if self.path_namespace and not (path == self.path_namespace or (path or "").startswith(self.path_namespace + "/")):
            return False
        return True


def _bus_addresses(address: str):
    for entry in address.split(";"):
        transport, _, params = entry.partition(":")
        kv = dict(p.split("=", 1) for p in params.split(",") if "=" in p)
        if transport == "unix":
            if "path" in kv:
                yield urllib.parse.unquote(kv["path"])
            elif "abstract" in kv:
                yield "\0" + urllib.parse.unquote(kv["abstract"])


class DBusConnection:
    """A small, thread-safe D-Bus connection: method calls + signal subscriptions."""

    def __init__(self, address: str):
        self.closed = False
        last = None
        self._sock = None
        for path in _bus_addresses(address):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(path)
                self._sock = s
                break
            except OSError as e:
                last = e
        if self._sock is None:
            raise DBusError("org.freedesktop.DBus.Error.FileNotFound", f"can't connect to D-Bus at {address}: {last}")
        self._rbuf = bytearray()
        self._send_lock = threading.Lock()
        self._serials = itertools.count(1)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._handlers = []
        self._handlers_lock = threading.Lock()
        self._exports = {}
        self._signals = queue.Queue()
        self._authenticate()
        threading.Thread(target=self._read_loop, name="dbus-reader", daemon=True).start()
        threading.Thread(target=self._dispatch_loop, name="dbus-signals", daemon=True).start()
        self.unique_name = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "Hello")[0]

    @classmethod
    def system(cls) -> "DBusConnection":
        address = os.environ.get("DBUS_SYSTEM_BUS_ADDRESS") or "unix:path=/run/dbus/system_bus_socket;unix:path=/var/run/dbus/system_bus_socket"
        return cls(address)

    # -- low level ------------------------------------------------------------------
    def _recv_more(self):
        chunk = self._sock.recv(65536)
        if not chunk:
            raise EOFError("D-Bus socket closed")
        self._rbuf += chunk

    def _authenticate(self):
        self._sock.sendall(b"\0")
        uid = str(os.geteuid()).encode().hex().encode()
        self._sock.sendall(b"AUTH EXTERNAL " + uid + b"\r\n")
        while b"\r\n" not in self._rbuf:
            self._recv_more()
        line, _, rest = bytes(self._rbuf).partition(b"\r\n")
        self._rbuf = bytearray(rest)
        if not line.startswith(b"OK"):
            raise DBusError("org.freedesktop.DBus.Error.AuthFailed", line.decode(errors="replace"))
        self._sock.sendall(b"BEGIN\r\n")

    def _recv_exact(self, n: int) -> bytes:
        while len(self._rbuf) < n:
            self._recv_more()
        out = bytes(self._rbuf[:n])
        del self._rbuf[:n]
        return out

    def _read_message(self) -> _Message:
        head = self._recv_exact(16)
        endian = "<" if head[0:1] == b"l" else ">"
        body_len, _serial, fields_len = struct.unpack_from(endian + "III", head, 4)
        header_len = 16 + fields_len
        header_len += (-header_len) % 8
        return _decode_message(head + self._recv_exact(header_len - 16 + body_len))

    def _send_raw(self, data: bytes):
        with self._send_lock:
            self._sock.sendall(data)

    def _read_loop(self):
        try:
            while True:
                self._handle(self._read_message())
        except Exception as e:  # noqa: BLE001 - any failure ends the connection
            if not self.closed:
                log.debug("D-Bus connection ended: %r", e)
        finally:
            self.closed = True
            with self._pending_lock:
                waiters = list(self._pending.values())
                self._pending.clear()
            for w in waiters:
                w.error = DBusError("org.freedesktop.DBus.Error.Disconnected", "D-Bus connection closed")
                w.event.set()
            self._signals.put(None)

    def _handle(self, msg: _Message):
        if msg.type in (_METHOD_RETURN, _ERROR):
            with self._pending_lock:
                w = self._pending.pop(msg.fields.get(_F_REPLY_SERIAL), None)
            if w is None:
                return
            if msg.type == _ERROR:
                text = msg.body[0] if msg.body and isinstance(msg.body[0], str) else ""
                w.error = DBusError(msg.fields.get(_F_ERROR_NAME, ""), text)
            else:
                w.result = msg.body
            w.event.set()
        elif msg.type == _SIGNAL:
            self._signals.put(msg)
        elif msg.type == _METHOD_CALL:
            path, iface = msg.fields.get(_F_PATH), msg.fields.get(_F_INTERFACE)
            handler = self._exports.get((path, iface))
            if handler is None and iface is None:
                handler = next((h for (p, _i), h in self._exports.items() if p == path), None)
            if handler is None:
                handler = self._unknown_method
            # Handlers may wait for the user (e.g. a pairing code), so never run them on the reader thread.
            threading.Thread(target=self._serve_call, args=(msg, handler), name="dbus-call", daemon=True).start()

    @staticmethod
    def _unknown_method(member, _args):
        raise DBusError("org.freedesktop.DBus.Error.UnknownMethod", f"{member} isn't supported")

    def _serve_call(self, msg, handler):
        try:
            signature, values = handler(msg.fields.get(_F_MEMBER), msg.body)
            mtype, fields = _METHOD_RETURN, {_F_REPLY_SERIAL: msg.serial}
        except DBusError as e:
            signature, values = "s", [e.message]
            mtype, fields = _ERROR, {_F_REPLY_SERIAL: msg.serial, _F_ERROR_NAME: e.name}
        except Exception as e:  # noqa: BLE001
            log.exception("D-Bus method handler failed")
            signature, values = "s", [str(e)]
            mtype, fields = _ERROR, {_F_REPLY_SERIAL: msg.serial, _F_ERROR_NAME: "org.freedesktop.DBus.Error.Failed"}
        if msg.flags & 1:  # caller doesn't want a reply
            return
        if msg.fields.get(_F_SENDER):
            fields[_F_DESTINATION] = msg.fields[_F_SENDER]
        try:
            self._send_raw(_encode_message(mtype, next(self._serials), fields, signature, values))
        except OSError:
            pass

    def _dispatch_loop(self):
        while True:
            msg = self._signals.get()
            if msg is None:
                return
            path = msg.fields.get(_F_PATH)
            iface = msg.fields.get(_F_INTERFACE)
            member = msg.fields.get(_F_MEMBER)
            with self._handlers_lock:
                handlers = [h for h in self._handlers if h.matches(path, iface, member)]
            for h in handlers:
                try:
                    h.callback(path, msg.body)
                except Exception:  # noqa: BLE001
                    log.exception("D-Bus signal handler failed")

    # -- public API -----------------------------------------------------------------
    def call(self, destination, path, interface, member, signature="", args=(), timeout=30.0):
        return self.wait_reply(self.call_async(destination, path, interface, member, signature, args), timeout)

    def call_async(self, destination, path, interface, member, signature="", args=()):
        """Send a call without waiting for its answer, so several can be on their way at once (BlueZ then
        sends their Bluetooth requests back to back). Collect each answer with wait_reply()."""
        if self.closed:
            raise DBusError("org.freedesktop.DBus.Error.Disconnected", "not connected to D-Bus")
        serial = next(self._serials)
        w = _Waiter()
        with self._pending_lock:
            self._pending[serial] = w
        fields = {_F_PATH: path, _F_MEMBER: member}
        if interface:
            fields[_F_INTERFACE] = interface
        if destination:
            fields[_F_DESTINATION] = destination
        try:
            self._send_raw(_encode_message(_METHOD_CALL, serial, fields, signature, args))
        except OSError as e:
            with self._pending_lock:
                self._pending.pop(serial, None)
            raise DBusError("org.freedesktop.DBus.Error.Disconnected", str(e)) from None
        return serial, w, member

    def reply_ready(self, pending, timeout: float) -> bool:
        """Wait up to `timeout` for a call's answer, without giving up on it."""
        return pending[1].event.wait(timeout)

    def wait_reply(self, pending, timeout=30.0):
        serial, w, member = pending
        if not w.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(serial, None)
            raise DBusError("org.freedesktop.DBus.Error.NoReply", f"{member} timed out after {timeout:.0f}s")
        if w.error is not None:
            raise w.error
        return w.result

    def export(self, path: str, interface: str, handler):
        """Serve calls to `interface` at `path`. handler(member, args) -> (reply signature, reply values)."""
        self._exports[(path, interface)] = handler

    def unexport(self, path: str, interface: str):
        self._exports.pop((path, interface), None)

    def subscribe(self, callback, interface=None, member=None, path=None, path_namespace=None, sender=None):
        h = _SignalHandler()
        h.callback, h.interface, h.member, h.path, h.path_namespace = callback, interface, member, path, path_namespace
        parts = ["type='signal'"]
        for key, val in (("sender", sender), ("interface", interface), ("member", member),
                         ("path", path), ("path_namespace", path_namespace)):
            if val:
                parts.append(f"{key}='{val}'")
        h.rule = ",".join(parts)
        with self._handlers_lock:
            self._handlers.append(h)
        self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch", "s", [h.rule])
        return h

    def unsubscribe(self, handler):
        with self._handlers_lock:
            if handler in self._handlers:
                self._handlers.remove(handler)
        try:
            self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RemoveMatch", "s",
                      [handler.rule], timeout=5)
        except DBusError:
            pass

    def close(self):
        self.closed = True
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# =====================================================================================
#  BlueZ + the Flipper's Bluetooth serial service
# =====================================================================================

BLUEZ = "org.bluez"
IFACE_ADAPTER = "org.bluez.Adapter1"
IFACE_DEVICE = "org.bluez.Device1"
IFACE_CHAR = "org.bluez.GattCharacteristic1"
IFACE_BATTERY = "org.bluez.Battery1"
IFACE_PROPS = "org.freedesktop.DBus.Properties"
IFACE_OBJMGR = "org.freedesktop.DBus.ObjectManager"

# Same UUIDs as the official apps / firmware (targets/f7/ble_glue/services/serial_service_uuid.inc)
SERIAL_SERVICE_UUID = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
UUID_FROM_FLIPPER = "19ed82ae-ed21-4c9d-4145-228e61fe0000"  # Flipper "TX": indications to us
UUID_TO_FLIPPER = "19ed82ae-ed21-4c9d-4145-228e62fe0000"    # Flipper "RX": we write here
UUID_FLOW_CONTROL = "19ed82ae-ed21-4c9d-4145-228e63fe0000"  # uint32 BE: free bytes in Flipper's buffer
UUID_RPC_STATUS = "19ed82ae-ed21-4c9d-4145-228e64fe0000"
# 16-bit service UUID the Flipper advertises (0x3080 | body colour)
FLIPPER_ADV_UUIDS = {f"0000308{i}-0000-1000-8000-00805f9b34fb" for i in range(4)}

PAIRING_HELP = ("Pair it again with “Pair a Flipper” at the bottom of the Flipper Remote window (or run "
                "flipper-remote pair). KDE's Bluetooth settings can't pair a Flipper.")
PAIRING_STEPS = """KDE's Bluetooth settings can't pair a Flipper: the Flipper only accepts pairing where you type the
6-digit code it shows, and KDE's pairing agent can't type codes. Pair it with:

  flipper-remote pair

or with “Pair a Flipper” at the bottom of the Flipper Remote window. If you'd rather use bluetoothctl
(Desktop Mode, Konsole; make sure your phone isn't connected to the Flipper):

  bluetoothctl
  agent off
  agent KeyboardDisplay
  default-agent
  devices                      (if your Flipper is listed, run:  remove <its address>)
  scan on                      (wait for a line like:  [NEW] Device 80:E1:26:xx:xx:xx Flipper ...)
  scan off
  pair 80:E1:26:xx:xx:xx       (when it asks "Enter passkey", type the code shown on the Flipper)
  trust 80:E1:26:xx:xx:xx
  quit
"""
UNREACHABLE_HELP = ("Make sure the Flipper's Bluetooth is ON, it's nearby, and it isn't connected to your "
                    "phone right now (a Flipper can only talk to one device at a time).")


def _is_auth_error(e: DBusError) -> bool:
    text = f"{e.name} {e.message}".lower()
    return any(k in text for k in ("notauthorized", "notpermitted", "authentication", "authorization",
                                   "insufficient", "encryption", "auth"))


def _explain_gatt_error(e: DBusError, what: str) -> str:
    if _is_auth_error(e):
        return f"The Flipper refused {what} because this computer isn't properly paired with it. {PAIRING_HELP}"
    if e.name.endswith(".NotConnected") or "not connected" in e.message.lower():
        return "The Flipper disconnected."
    return f"Bluetooth error during {what}: {e.message or e.name}"


def _gatt_exception(e: DBusError, what: str) -> FlipperError:
    message = _explain_gatt_error(e, what)
    return PairingProblem(message) if _is_auth_error(e) else ConnectError(message)


def _explain_connect_error(e, name: str) -> str:
    if e is None:
        return f"Couldn't connect to {name}. {UNREACHABLE_HELP}"
    if isinstance(e, DBusError):
        if e.name.endswith(".NotReady"):
            return "Bluetooth isn't ready yet. Wait a moment and try again."
        if _is_auth_error(e) or "authentication" in e.message.lower():
            return f"Pairing with {name} failed. {PAIRING_HELP}"
    return f"Couldn't connect to {name}. {UNREACHABLE_HELP}"


def bluez_error_to_flipper(e: DBusError) -> FlipperError:
    if e.name in ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"):
        return FlipperError("The Bluetooth service (BlueZ) isn't running on this computer.")
    if e.name == "org.freedesktop.DBus.Error.AccessDenied":
        return FlipperError("This user isn't allowed to use Bluetooth over D-Bus.")
    return FlipperError(f"Bluetooth error: {e.message or e.name}")


def find_flippers(bus: DBusConnection):
    """Return Bluetooth devices that look like a Flipper Zero, best candidates first."""
    try:
        objects = bus.call(BLUEZ, "/", IFACE_OBJMGR, "GetManagedObjects")[0]
    except DBusError as e:
        raise bluez_error_to_flipper(e) from None
    adapters = {p: i[IFACE_ADAPTER] for p, i in objects.items() if IFACE_ADAPTER in i}
    found = []
    for path, ifaces in objects.items():
        dev = ifaces.get(IFACE_DEVICE)
        if not dev:
            continue
        uuids = {str(u).lower() for u in dev.get("UUIDs", [])}
        name = dev.get("Name") or ""
        alias = dev.get("Alias") or name
        has_serial = SERIAL_SERVICE_UUID in uuids
        looks_like = has_serial or bool(uuids & FLIPPER_ADV_UUIDS) or \
            name.lower().startswith("flipper") or alias.lower().startswith("flipper")
        if not looks_like:
            continue
        adapter = dev.get("Adapter") or path.rsplit("/", 1)[0]
        found.append({
            "path": path,
            "address": dev.get("Address", ""),
            "name": alias or name or dev.get("Address", "Flipper"),
            "paired": bool(dev.get("Paired")),
            "connected": bool(dev.get("Connected")),
            "adapter": adapter,
            "adapter_powered": bool(adapters.get(adapter, {}).get("Powered", True)),
            "battery": (ifaces.get(IFACE_BATTERY) or {}).get("Percentage"),
            "rssi": dev.get("RSSI"),   # only present while BlueZ is currently seeing it
        })
    found.sort(key=lambda d: (not d["connected"], not d["paired"], d["name"].lower()))
    return found


class _LinkDropped(Exception):
    """The Bluetooth link went down before the Flipper's services showed up."""


class FlipperBLE:
    """The Flipper's BLE serial pipe: a write characteristic, an indicate characteristic and
    a flow-control characteristic that tells us how many bytes the Flipper can accept."""

    QUICK_FAILURE_S = 8.0     # a connect that fails sooner than this got as far as hearing the Flipper
    CONNECT_TIMEOUT_S = 45.0  # BlueZ itself keeps trying for up to about 40 s; don't stop listening sooner
    STILL_LOOKING_S = 10.0    # after this long, say so
    CONNECT_BUDGET_S = 90.0   # all attempts together

    def __init__(self, bus: DBusConnection, device: dict, on_data, on_disconnect):
        self.bus = bus
        self.device = device
        self.path = device["path"]
        self._on_data = on_data
        self._on_disconnect = on_disconnect
        self._chars = {}
        self._mtu = 23
        self._window = None  # None = Flipper didn't expose flow control
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()
        self._sub = None
        self._seen = threading.Event()
        self._scanning = False              # we have an LE scan running
        self._changed = threading.Event()   # the device's Connected/ServicesResolved changed
        self.connected = False
        self.we_connected = False
        self.rpc_active = None
        self.timings = {}   # seconds spent in each step of open(), for the log

    @property
    def mtu(self):
        return self._mtu

    def open(self, status=lambda msg: None):
        name = self.device["name"]
        try:
            powered = self.bus.call(BLUEZ, self.device["adapter"], IFACE_PROPS, "Get", "ss",
                                    [IFACE_ADAPTER, "Powered"])[0]
        except DBusError as e:
            raise bluez_error_to_flipper(e) from None
        if not powered:
            raise ConnectError("Bluetooth is turned off on this Steam Deck. Turn it on and try again.")
        self._sub = self.bus.subscribe(self._on_properties_changed, interface=IFACE_PROPS,
                                       member="PropertiesChanged", path_namespace=self.path, sender=BLUEZ)
        try:
            step = time.monotonic()
            for attempt in (1, 2):
                if attempt == 2 or not self._device_props().get("Connected"):
                    self._connect(status)
                    self.we_connected = True
                link_up = time.monotonic()
                status(f"Setting up {name}…")
                try:
                    self._wait_services_resolved()
                    break
                except _LinkDropped:
                    # Happens now and then while BlueZ sets the link up. The Flipper is advertising
                    # ten times a second again straight away, so connecting again is quick.
                    if attempt == 2:
                        raise ConnectError(f"{name} disconnected while connecting. {UNREACHABLE_HELP}") from None
                    log.info("The link to %s dropped while setting up; connecting again", name)
            self.timings["link"], self.timings["services"] = link_up - step, time.monotonic() - link_up
            step = time.monotonic()
            self._find_characteristics()
            self._set_up_pipe()
            self.timings["setup"] = time.monotonic() - step
            self.connected = True
            log.info("BLE ready: %s (%s), MTU %d, Flipper buffer %s bytes", name, self.device["address"],
                     self._mtu, self._window)
        except BaseException:
            self.close(disconnect=self.we_connected)
            raise

    def _device_props(self) -> dict:
        try:
            return self.bus.call(BLUEZ, self.path, IFACE_PROPS, "GetAll", "s", [IFACE_DEVICE])[0]
        except DBusError as e:
            if e.name == "org.freedesktop.DBus.Error.UnknownObject":
                raise NotFoundError("The Flipper is no longer known to Bluetooth. Pair it again.") from None
            raise bluez_error_to_flipper(e) from None

    def _connect(self, status):
        """Connect. BlueZ waits up to 20 s to hear the Flipper. A failure well before that means it was heard
        and the link fell over while being set up, which happens now and then; the Flipper advertises 10 times
        a second straight after one, so try again at once. A failure at the end of the wait means it wasn't
        heard at all: listen for it briefly, and only try again if it turns up."""
        # Scan the whole time. BlueZ connects on the first advertisement it hears from the Flipper, and on
        # some Bluetooth chips its own listening for that (on the Steam Deck, at least with a headset playing)
        # can stay deaf for 40 s or more while a scan hears the Flipper within a second. Real logs: connects
        # stuck for 30-43 s finished within 1-3 s of a scan starting.
        self._scan(True)
        try:
            self._connect_attempts(status)
        finally:
            self._scan(False)

    def _connect_attempts(self, status):
        name = self.device["name"]
        last = None
        quick_retries = slow_retries = 0
        deadline = time.monotonic() + self.CONNECT_BUDGET_S
        for _ in range(8):
            budget = min(self.CONNECT_TIMEOUT_S, deadline - time.monotonic())
            if budget <= 0:
                break
            status(f"Connecting to {name}…")
            started = time.monotonic()
            try:
                pending = self.bus.call_async(BLUEZ, self.path, IFACE_DEVICE, "Connect")
                if not self.bus.reply_ready(pending, min(self.STILL_LOOKING_S, budget)):
                    status(f"Still looking for {name}…")   # Bluetooth audio, for one, can make this slow
                self.bus.wait_reply(pending, timeout=max(0.1, budget - (time.monotonic() - started)))
                log.info("Bluetooth link to %s up after %.1f s", name, time.monotonic() - started)
                return
            except DBusError as e:
                last, took = e, time.monotonic() - started
                log.info("Connect to %s failed after %.1f s: %s", name, took, e)
                if e.name.endswith(".AlreadyConnected"):
                    return
                if e.name.endswith(".InProgress"):
                    # An earlier attempt is still going (one we stopped waiting for, or another app's): let it finish.
                    status(f"Still looking for {name}…")
                    if self._wait_connected(min(self.CONNECT_TIMEOUT_S, deadline - time.monotonic())):
                        log.info("Bluetooth link to %s up after %.1f s (an earlier attempt got there)", name,
                                 time.monotonic() - started)
                        return
                    continue
                if _is_auth_error(e):
                    break
                if took < self.QUICK_FAILURE_S and quick_retries < 2:
                    quick_retries += 1
                    continue
                if slow_retries >= 1:
                    break
                slow_retries += 1
                status(f"Looking for {name}…")
                if not self._scan_briefly(8.0):
                    log.info("%s isn't advertising; giving up", name)
                    break
        message = _explain_connect_error(last, name)
        raise PairingProblem(message) if last is not None and _is_auth_error(last) else ConnectError(message)

    def _wait_connected(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self._changed.clear()   # before reading, so a change that lands after the read still wakes us
            if self._device_props().get("Connected"):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._changed.wait(min(remaining, 1.0))

    def _scan(self, on: bool):
        """Start or stop our LE scan (BlueZ "discovery"). Other apps' scans aren't affected."""
        adapter = self.device["adapter"]
        if on and not self._scanning:
            try:
                self.bus.call(BLUEZ, adapter, IFACE_ADAPTER, "SetDiscoveryFilter", "a{sv}",
                              [{"Transport": Variant("s", "le")}], timeout=5)
            except DBusError:
                pass
            try:
                self.bus.call(BLUEZ, adapter, IFACE_ADAPTER, "StartDiscovery", timeout=5)
                self._scanning = True
            except DBusError as e:
                log.debug("StartDiscovery: %s", e)
        elif not on and self._scanning:
            self._scanning = False
            try:
                self.bus.call(BLUEZ, adapter, IFACE_ADAPTER, "StopDiscovery", timeout=5)
            except DBusError:
                pass

    def _scan_briefly(self, seconds: float) -> bool:
        """Listen for the Flipper's advertisements for up to `seconds`. True if it was heard. Leaves the
        scan running (the caller stops it)."""
        self._scan(False)   # a fresh scan, so a sighting now shows up as new (BlueZ resets RSSI per scan)
        self._seen.clear()
        self._scan(True)
        return self._seen.wait(seconds)

    def _wait_services_resolved(self, timeout: float = 25.0):
        deadline = time.monotonic() + timeout
        while True:
            self._changed.clear()   # before reading, so a change that lands after the read still wakes us
            props = self._device_props()
            if not props.get("Connected"):
                raise _LinkDropped()
            if props.get("ServicesResolved"):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectError("Connected, but the Flipper's Bluetooth services never showed up. Try again.")
            self._changed.wait(min(remaining, 1.0))   # BlueZ says when; the timeout is only a safety net

    def _set_up_pipe(self):
        """Turn on the Flipper's indications and read its buffer size and RPC status. The four requests go
        out together; BlueZ sends them back to back, in this order, instead of one per round trip."""
        chars = self._chars
        notify = [self.bus.call_async(BLUEZ, chars[u], IFACE_CHAR, "StartNotify")
                  for u in (UUID_FROM_FLIPPER, UUID_FLOW_CONTROL) if u in chars]
        window = (self.bus.call_async(BLUEZ, chars[UUID_FLOW_CONTROL], IFACE_CHAR, "ReadValue", "a{sv}", [{}])
                  if UUID_FLOW_CONTROL in chars else None)
        rpc = (self.bus.call_async(BLUEZ, chars[UUID_RPC_STATUS], IFACE_CHAR, "ReadValue", "a{sv}", [{}])
               if UUID_RPC_STATUS in chars else None)
        for pending in notify:
            try:
                self.bus.wait_reply(pending, timeout=25)
            except DBusError as e:
                if not (e.name.endswith(".InProgress") or "already" in e.message.lower()):
                    raise _gatt_exception(e, "access") from None
        if window is not None:
            try:
                self._update_window(self.bus.wait_reply(window, timeout=20)[0])
            except DBusError as e:
                raise _gatt_exception(e, "access") from None
        self.rpc_active = None
        if rpc is not None:   # the Flipper sets this to 1 once it has opened a remote-control session for us
            try:
                value = bytes(self.bus.wait_reply(rpc, timeout=10)[0])
                self.rpc_active = any(value) if value else None
            except DBusError as e:
                log.debug("RPC status read failed: %s", e)

    def _find_characteristics(self):
        try:
            objects = self.bus.call(BLUEZ, "/", IFACE_OBJMGR, "GetManagedObjects")[0]
        except DBusError as e:
            raise bluez_error_to_flipper(e) from None
        prefix = self.path + "/"
        wanted = (UUID_FROM_FLIPPER, UUID_TO_FLIPPER, UUID_FLOW_CONTROL, UUID_RPC_STATUS)
        for path, ifaces in objects.items():
            char = ifaces.get(IFACE_CHAR)
            if not char or not path.startswith(prefix):
                continue
            uuid = str(char.get("UUID", "")).lower()
            if uuid in wanted:
                self._chars[uuid] = path
                if uuid == UUID_TO_FLIPPER:
                    self._mtu = int(char.get("MTU") or 23)
        if UUID_FROM_FLIPPER not in self._chars or UUID_TO_FLIPPER not in self._chars:
            raise ConnectError("This device doesn't offer the Flipper's serial service. Make sure it's a Flipper "
                               "Zero with Bluetooth ON and that no Bluetooth app (like BLE Remote) is running on it.")

    def _on_properties_changed(self, path, body):
        if len(body) < 2:
            return
        iface, changed = body[0], body[1]
        if iface == IFACE_CHAR and "Value" in changed:
            value = changed["Value"]
            if path == self._chars.get(UUID_FROM_FLIPPER):
                if log.isEnabledFor(logging.DEBUG):
                    log.debug("<- %s", bytes(value).hex())
                self._on_data(bytes(value))
            elif path == self._chars.get(UUID_FLOW_CONTROL):
                self._update_window(value)
        elif iface == IFACE_DEVICE and path == self.path:
            if "RSSI" in changed:
                self._seen.set()
            if "ServicesResolved" in changed or "Connected" in changed:
                self._changed.set()
            if changed.get("Connected") is False and self.connected:
                log.info("Bluetooth link to %s dropped", self.device["name"])
                self.connected = False
                with self._cv:
                    self._cv.notify_all()
                self._on_disconnect()

    def _update_window(self, value):
        value = bytes(value)
        if len(value) < 4:
            return
        size = struct.unpack(">I", value[:4])[0]
        with self._cv:
            self._window = size
            self._cv.notify_all()
        log.debug("Flipper buffer free: %d", size)

    def _reserve(self, wanted: int) -> int:
        """Wait until the Flipper has buffer space, then claim up to `wanted` bytes of it."""
        deadline = time.monotonic() + 4.0
        while True:
            with self._cv:
                while self._window is not None and self._window <= 0 and self.connected:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cv.wait(remaining)
                if not self.connected:
                    raise DisconnectedError()
                if self._window is None:
                    return wanted
                if self._window > 0:
                    n = min(wanted, self._window)
                    self._window -= n
                    return n
            # No buffer update for a while: re-read the characteristic and carry on.
            log.warning("No flow-control update from the Flipper; re-reading it")
            try:
                value = self.bus.call(BLUEZ, self._chars[UUID_FLOW_CONTROL], IFACE_CHAR, "ReadValue", "a{sv}",
                                      [{}], timeout=10)[0]
                self._update_window(value)
            except DBusError as e:
                raise DisconnectedError(_explain_gatt_error(e, "the transfer")) from None
            deadline = time.monotonic() + 4.0

    def write(self, data: bytes):
        if not self.connected:
            raise DisconnectedError()
        chunk_max = max(20, min(self._mtu - 3, 240))
        with self._write_lock:
            off = 0
            while off < len(data):
                n = self._reserve(min(chunk_max, len(data) - off))
                chunk = data[off:off + n]
                if log.isEnabledFor(logging.DEBUG):
                    log.debug("-> %s", chunk.hex())
                try:
                    self.bus.call(BLUEZ, self._chars[UUID_TO_FLIPPER], IFACE_CHAR, "WriteValue", "aya{sv}",
                                  [chunk, {"type": Variant("s", "request")}], timeout=15)
                except DBusError as e:
                    gone = ("org.freedesktop.DBus.Error.Disconnected", "org.freedesktop.DBus.Error.UnknownObject",
                            "org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NoReply")
                    if not self.connected or e.name.endswith(".NotConnected") or e.name in gone[:3]:
                        self.connected = False
                        raise DisconnectedError() from None
                    if e.name == "org.freedesktop.DBus.Error.NoReply":
                        raise DisconnectedError("The Flipper stopped responding over Bluetooth.") from None
                    raise FlipperError(_explain_gatt_error(e, "the transfer")) from None
                off += n

    def battery(self):
        try:
            return int(self.bus.call(BLUEZ, self.path, IFACE_PROPS, "Get", "ss", [IFACE_BATTERY, "Percentage"],
                                     timeout=5)[0])
        except (DBusError, TypeError, ValueError):
            return None

    def close(self, disconnect: bool = True):
        was_connected = self.connected
        self.connected = False
        with self._cv:
            self._cv.notify_all()
        if self._sub is not None:
            self.bus.unsubscribe(self._sub)
            self._sub = None
        if not disconnect and was_connected:
            for uuid in (UUID_FROM_FLIPPER, UUID_FLOW_CONTROL):
                if uuid in self._chars:
                    try:
                        self.bus.call(BLUEZ, self._chars[uuid], IFACE_CHAR, "StopNotify", timeout=5)
                    except DBusError:
                        pass
        if disconnect:
            try:
                self.bus.call(BLUEZ, self.path, IFACE_DEVICE, "Disconnect", timeout=10)
                log.info("Disconnected from %s", self.device["name"])
            except DBusError as e:
                log.debug("Disconnect: %s", e)


# =====================================================================================
#  Protobuf (hand-rolled for the few messages we need) + Flipper RPC
# =====================================================================================

# Field numbers from flipperzero-protobuf (flipper.proto, PB.Main)
F_EMPTY = 4
F_PING_REQUEST, F_PING_RESPONSE = 5, 6
F_STORAGE_LIST_REQUEST, F_STORAGE_LIST_RESPONSE = 7, 8
F_STORAGE_READ_REQUEST, F_STORAGE_READ_RESPONSE = 9, 10
F_APP_START_REQUEST = 16
F_DEVICE_INFO_REQUEST, F_DEVICE_INFO_RESPONSE = 32, 33
F_APP_EXIT_REQUEST = 47
F_APP_LOAD_FILE_REQUEST = 48
F_APP_BUTTON_PRESS_REQUEST = 49
F_APP_BUTTON_RELEASE_REQUEST = 50
F_APP_STATE_RESPONSE = 58
F_PROPERTY_GET_REQUEST, F_PROPERTY_GET_RESPONSE = 61, 62
F_APP_GET_ERROR_REQUEST, F_APP_GET_ERROR_RESPONSE = 63, 64

ST_OK = 0
ST_ERROR_DECODE = 2
ST_ERROR_INVALID_PARAMETERS = 15
ST_ERROR_APP_SYSTEM_LOCKED = 17
ST_ERROR_APP_NOT_RUNNING = 21
STATUS_NAMES = {
    0: "OK", 1: "ERROR", 2: "ERROR_DECODE", 3: "ERROR_NOT_IMPLEMENTED", 4: "ERROR_BUSY",
    5: "ERROR_STORAGE_NOT_READY", 6: "ERROR_STORAGE_EXIST", 7: "ERROR_STORAGE_NOT_EXIST",
    8: "ERROR_STORAGE_INVALID_PARAMETER", 9: "ERROR_STORAGE_DENIED", 10: "ERROR_STORAGE_INVALID_NAME",
    11: "ERROR_STORAGE_INTERNAL", 12: "ERROR_STORAGE_NOT_IMPLEMENTED", 13: "ERROR_STORAGE_ALREADY_OPEN",
    14: "ERROR_CONTINUOUS_COMMAND_INTERRUPTED", 15: "ERROR_INVALID_PARAMETERS", 16: "ERROR_APP_CANT_START",
    17: "ERROR_APP_SYSTEM_LOCKED", 18: "ERROR_STORAGE_DIR_NOT_EMPTY", 19: "ERROR_VIRTUAL_DISPLAY_ALREADY_STARTED",
    20: "ERROR_VIRTUAL_DISPLAY_NOT_STARTED", 21: "ERROR_APP_NOT_RUNNING", 22: "ERROR_APP_CMD_ERROR",
    58: "ERROR_GPIO_MODE_INCORRECT", 59: "ERROR_GPIO_UNKNOWN_PIN_MODE",
}


def status_name(code: int) -> str:
    return STATUS_NAMES.get(code, f"status {code}")


class ProtoError(Exception):
    pass


class _Incomplete(Exception):
    pass


def pb_varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf, pos: int):
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise _Incomplete()
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint too long")


def pb_uint(field: int, n: int) -> bytes:
    return pb_varint(field << 3) + pb_varint(n) if n else b""


def pb_bytes(field: int, data: bytes) -> bytes:
    return pb_varint(field << 3 | 2) + pb_varint(len(data)) + data if data else b""


def pb_str(field: int, s: str) -> bytes:
    return pb_bytes(field, s.encode("utf-8"))


def pb_msg(field: int, payload: bytes) -> bytes:
    """A sub-message: always written, even when empty (that's how a oneof says which one)."""
    return pb_varint(field << 3 | 2) + pb_varint(len(payload)) + payload


def pb_fields(data: bytes):
    pos = 0
    try:
        while pos < len(data):
            key, pos = _read_varint(data, pos)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, pos = _read_varint(data, pos)
            elif wire == 2:
                n, pos = _read_varint(data, pos)
                if pos + n > len(data):
                    raise ProtoError("truncated field")
                value = bytes(data[pos:pos + n])
                pos += n
            elif wire == 1:
                value, pos = bytes(data[pos:pos + 8]), pos + 8
            elif wire == 5:
                value, pos = bytes(data[pos:pos + 4]), pos + 4
            else:
                raise ProtoError(f"unsupported wire type {wire}")
            yield field, wire, value
    except _Incomplete:
        raise ProtoError("truncated message") from None


class RpcMessage:
    __slots__ = ("command_id", "status", "has_next", "field", "content")

    def __init__(self):
        self.command_id = 0
        self.status = 0
        self.has_next = False
        self.field = 0
        self.content = b""

    def __repr__(self):
        return (f"RpcMessage(id={self.command_id}, status={status_name(self.status)}, "
                f"has_next={self.has_next}, field={self.field}, {len(self.content)}B)")


def decode_main(data: bytes) -> RpcMessage:
    m = RpcMessage()
    for field, wire, value in pb_fields(data):
        if field == 1 and wire == 0:
            m.command_id = value
        elif field == 2 and wire == 0:
            m.status = value
        elif field == 3 and wire == 0:
            m.has_next = bool(value)
        elif wire == 2:
            m.field, m.content = field, value
    return m


def encode_main(command_id: int, field: int, payload: bytes = b"") -> bytes:
    return pb_uint(1, command_id) + pb_msg(field, payload)


class RpcClient:
    """Length-delimited PB.Main messages over the BLE pipe. One request at a time."""

    def __init__(self, send_bytes):
        self._send = send_bytes
        self._buf = bytearray()
        self._buf_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_id = 1000 + secrets.randbelow(1_000_000)  # never mistake a stale reply for ours
        self.on_notification = None
        self.broken = None

    def feed(self, data: bytes):
        messages = []
        with self._buf_lock:
            self._buf += data
            while True:
                try:
                    length, start = _read_varint(self._buf, 0)
                except _Incomplete:
                    break
                except ProtoError:
                    length, start = -1, 0
                if length < 0 or length > 1 << 20:
                    log.error("RPC stream out of sync; dropping %d bytes", len(self._buf))
                    self._buf.clear()
                    self.fail(DisconnectedError("Lost sync with the Flipper."))
                    return
                if len(self._buf) < start + length:
                    break
                payload = bytes(self._buf[start:start + length])
                del self._buf[:start + length]
                try:
                    messages.append(decode_main(payload))
                except ProtoError as e:
                    log.warning("Couldn't decode a message from the Flipper: %s", e)
        for m in messages:
            self._dispatch(m)

    def _dispatch(self, m: RpcMessage):
        q = None
        if m.command_id:
            with self._pending_lock:
                q = self._pending.get(m.command_id)
        if q is not None:
            q.put(m)
            return
        log.debug("RPC notification: %r", m)
        if self.on_notification:
            try:
                self.on_notification(m)
            except Exception:  # noqa: BLE001
                log.exception("RPC notification handler failed")

    def fail(self, exc: Exception):
        self.broken = exc
        with self._pending_lock:
            queues = list(self._pending.values())
        for q in queues:
            q.put(exc)

    def request(self, field: int, payload: bytes = b"", timeout: float = 10.0, on_part=None):
        """Send one request; return the list of response parts (the last has has_next == False)."""
        with self._request_lock:
            if self.broken is not None:
                raise self.broken
            self._next_id = self._next_id % 0x7FFFFFF0 + 1
            cid = self._next_id
            q = queue.Queue()
            with self._pending_lock:
                self._pending[cid] = q
            try:
                frame = encode_main(cid, field, payload)
                log.debug("RPC request id=%d field=%d (%d bytes)", cid, field, len(frame))
                self._send(pb_varint(len(frame)) + frame)
                parts = []
                while True:
                    try:
                        m = q.get(timeout=timeout)
                    except queue.Empty:
                        raise RpcTimeout("The Flipper didn't answer in time.") from None
                    if isinstance(m, Exception):
                        raise m
                    parts.append(m)
                    if on_part:
                        on_part(m)
                    if not m.has_next:
                        return parts
            finally:
                with self._pending_lock:
                    self._pending.pop(cid, None)


# =====================================================================================
#  Sub-GHz files
# =====================================================================================

_PRESET_NAMES = {
    "FuriHalSubGhzPresetOok270Async": "AM270", "FuriHalSubGhzPresetOok650Async": "AM650",
    "FuriHalSubGhzPreset2FSKDev238Async": "FM238", "FuriHalSubGhzPreset2FSKDev476Async": "FM476",
    "FuriHalSubGhzPresetMSK99_97KbAsync": "MSK", "FuriHalSubGhzPresetGFSK9_99KbAsync": "GFSK",
    "FuriHalSubGhzPresetCustom": "Custom",
}

MIN_SEND_MS = 500          # what the official app holds a Sub-GHz "Send" for
MAX_HOLD_S = 30.0          # safety limit for press-and-hold
MAX_EMULATE_S = 300        # the longest emulation the command line can ask for
MAX_REMOTE_BYTES = 512 * 1024   # an Infrared remote bigger than this isn't opened (the universal libraries are)
MAX_RAW_MS = 120_000


def parse_sub_file(data: bytes) -> dict:
    """Pull out what we need from a Flipper .sub file (FlipperFormat text)."""
    info = {}
    raw_us = 0
    has_raw = False
    for line in data.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "RAW_Data":
            has_raw = True
            for token in value.split():
                try:
                    raw_us += abs(int(token))
                except ValueError:
                    pass
        elif key not in info:
            info[key] = value
    protocol = info.get("Protocol") or None
    try:
        frequency = int(info.get("Frequency", ""))
    except ValueError:
        frequency = None
    preset = info.get("Preset") or None
    raw_ms = raw_us // 1000 if (protocol or "").upper() == "RAW" and has_raw else None
    return {
        "protocol": protocol,
        "frequency": frequency,
        "preset": _PRESET_NAMES.get(preset, preset),
        "raw_ms": raw_ms,
        "valid": info.get("Filetype", "").lower().startswith("flipper subghz"),
    }


def _flipper_fields(data: bytes):
    """A FlipperFormat text file as (key, value) pairs, in order."""
    for line in data.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition(":")
        if sep and not key.startswith("#"):
            yield key.strip(), value.strip()


MAX_REMOTE_SIGNALS = 400


def parse_saved_file(kind: str, data: bytes) -> dict:
    """What the window shows about a saved file: frequency and protocol, an Infrared remote's buttons, a key's type."""
    if kind == "subghz":
        return parse_sub_file(data)
    first = {}
    signals = []
    for key, value in _flipper_fields(data):
        if kind == "infrared":
            if key == "name":
                signals.append({"name": value, "protocol": None})
            elif key in ("protocol", "type") and signals and not signals[-1]["protocol"]:
                signals[-1]["protocol"] = "RAW" if (key, value.lower()) == ("type", "raw") else (
                    value if key == "protocol" else None)
        first.setdefault(key, value)
    meta = {"valid": first.get("Filetype", "").lower().startswith(KINDS[kind]["filetype"])}
    if kind == "infrared":
        meta["signals"] = signals[:MAX_REMOTE_SIGNALS]
    elif kind == "lfrfid":
        meta.update(protocol=first.get("Key type"), data=first.get("Data"))
    elif kind == "nfc":
        meta.update(protocol=first.get("Device type"), data=first.get("UID"))
    elif kind == "ibutton":
        meta.update(protocol=first.get("Protocol") or first.get("Key type"),
                    data=first.get("Rom Data") or first.get("Data") or first.get("Key"))
    return meta


def shown_meta(kind: str, meta) -> dict:
    """The part of a file's details that goes to the window."""
    if not meta:
        return None
    if kind == "subghz":
        return {k: meta.get(k) for k in ("protocol", "frequency", "preset", "raw_ms")}
    if kind == "infrared":
        return {"signals": meta.get("signals") or []}
    return {k: meta.get(k) for k in ("protocol", "data")}


def send_duration_ms(meta) -> int:
    """How long to hold "send": the whole recording for RAW files (like the official app), else 500 ms."""
    raw_ms = (meta or {}).get("raw_ms")
    if raw_ms:
        return int(min(max(MIN_SEND_MS, raw_ms + 100), MAX_RAW_MS))
    return MIN_SEND_MS


def pretty_name(path: str) -> str:
    """A file's name the way the window shows it: no folder, no extension, spaces for underscores."""
    name = path.rsplit("/", 1)[-1]
    kind = file_kind(path)
    if kind and name.lower().endswith(KINDS[kind]["ext"]):
        name = name[:-len(KINDS[kind]["ext"])]
    return name.replace("_", " ")


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def resolve_saved_path(name: str, folder: bool = False) -> str:
    """A path from the command line: /ext/... as it is; "infrared/TV" or "nfc/Card" under /ext; anything else under
    /ext/subghz, as before. A file gets its kind's extension if it's left off."""
    path = name.strip()
    if not path.startswith("/"):
        rel = path.lstrip("./")
        roots = {k["root"].rsplit("/", 1)[-1]: k["root"] for k in KINDS.values()}
        path = f"{ALL_ROOT}/{rel}" if rel.split("/", 1)[0].lower() in roots else f"{SUBGHZ_ROOT}/{rel}"
    path = "/" + path.strip("/")
    kind = kind_of(path)
    if not folder and kind and path != KINDS[kind]["root"] and not path.lower().endswith(KINDS[kind]["ext"]):
        path += KINDS[kind]["ext"]
    return path


# =====================================================================================
#  A connected Flipper
# =====================================================================================


class FlipperSession:
    # "Sub-GHz" is the official name; Xtreme-style loaders translate it, and also accept these.
    SUBGHZ_APP_NAMES = ("Sub-GHz", "SubGHz", "subghz")

    def __init__(self, bus: DBusConnection, device: dict):
        self.device = device
        self.rpc = RpcClient(self._write)
        self.ble = FlipperBLE(bus, device, on_data=self.rpc.feed, on_disconnect=self._on_ble_disconnect)
        self.rpc.on_notification = self._on_notification
        self._app_started = threading.Event()
        self._app_closed = threading.Event()
        self.alive = False
        self.on_lost = None
        self.fork = ""   # "Official", "Unleashed", "Xtreme", ... ("" if the firmware doesn't say)

    @property
    def name(self) -> str:
        return self.device["name"]

    def _write(self, data: bytes):
        self.ble.write(data)

    def _on_ble_disconnect(self):
        self.alive = False
        self.rpc.fail(DisconnectedError())
        if self.on_lost:
            self.on_lost(self)

    def _on_notification(self, m: RpcMessage):
        if m.field == F_APP_STATE_RESPONSE:
            state = 0
            for field, _wire, value in pb_fields(m.content):
                if field == 1:
                    state = value
            log.debug("Flipper app state: %s", "started" if state == 1 else "closed")
            (self._app_started if state == 1 else self._app_closed).set()
        elif m.status == ST_ERROR_DECODE:
            log.warning("The Flipper couldn't decode a request; it will reset the connection")

    # -- lifecycle --------------------------------------------------------------------
    NOT_ANSWERING = ("{name} is connected but isn't answering. If it's locked with a PIN, unlock it. Also make "
                     "sure it isn't plugged into qFlipper or lab.flipper.net over USB.")

    def open(self, status=lambda msg: None):
        started = time.monotonic()
        self.ble.open(status)
        if self.ble.rpc_active is False:
            self.ble.close(disconnect=self.ble.we_connected)
            raise ConnectError(self.NOT_ANSWERING.format(name=self.name))
        status(f"Talking to {self.name}…")
        step = time.monotonic()
        try:
            # Which firmware it runs is the first thing we need to know; getting the answer also shows
            # the Flipper is listening, so no separate ping.
            self.fork = self.read_fork(timeout=6.0, strict=True)
        except RpcTimeout:
            if not self.ble.we_connected:
                # The link was already up (e.g. a previous run died mid-message, so the Flipper is still
                # waiting for the rest of it). Reconnecting gives it a fresh session.
                log.info("No answer on an existing link; reconnecting to reset the Flipper's session")
                self.ble.close(disconnect=True)
                raise DisconnectedError() from None
            self.ble.close(disconnect=True)
            raise ConnectError(self.NOT_ANSWERING.format(name=self.name)) from None
        except DisconnectedError:
            # Typically: an earlier session left half a message in the Flipper's buffer, so it reset
            # the link. The caller reconnects once.
            self.ble.close(disconnect=False)
            raise
        t = dict(self.ble.timings, answer=time.monotonic() - step)
        log.info("Connected to %s in %.1f s (Bluetooth link %.1f s, services %.1f s, setup %.1f s, first answer "
                 "%.1f s). Firmware: %s", self.name, time.monotonic() - started, t.get("link", 0),
                 t.get("services", 0), t.get("setup", 0), t["answer"], self.fork or "unknown")
        self.alive = True

    @property
    def raw_loops_while_held(self) -> bool:
        """Unleashed keeps transmitting while "send" is held (RAW recordings loop), and its release only
        ends the loop: the current playback still runs to the end."""
        return self.fork.lower().startswith("unleashed")

    def read_fork(self, timeout: float = 5.0, strict: bool = False) -> str:
        """"Official", "Unleashed", "Xtreme"..., or "" if the firmware doesn't say. strict: a Flipper that
        doesn't answer raises RpcTimeout instead of counting as "doesn't say"."""
        try:
            parts = self._call(F_PROPERTY_GET_REQUEST, pb_str(1, "devinfo.firmware.origin.fork"), timeout=timeout)
        except RpcTimeout:
            if strict:
                raise
            return ""
        for m in parts:
            if m.field == F_PROPERTY_GET_RESPONSE:
                fields = {f: v for f, _w, v in pb_fields(m.content)}
                if fields.get(1, b"").endswith(b"origin.fork"):
                    return fields.get(2, b"").decode("utf-8", "replace")
        return ""

    def close(self, disconnect: bool = True):
        self.alive = False
        self.ble.close(disconnect=disconnect)
        self.rpc.fail(DisconnectedError())

    # -- basic RPC ----------------------------------------------------------------------
    def _call(self, field: int, payload: bytes = b"", timeout: float = 8.0, on_part=None):
        return self.rpc.request(field, payload, timeout=timeout, on_part=on_part)

    def ping(self, timeout: float = 5.0) -> float:
        t0 = time.monotonic()
        parts = self._call(F_PING_REQUEST, pb_bytes(1, b"deck"), timeout=timeout)
        if parts[-1].status != ST_OK:
            raise FlipperError(f"Ping failed ({status_name(parts[-1].status)})")
        return time.monotonic() - t0

    def device_info(self) -> dict:
        info = {}
        for m in self._call(F_DEVICE_INFO_REQUEST, b"", timeout=10):
            if m.field == F_DEVICE_INFO_RESPONSE:
                key = value = ""
                for field, _w, v in pb_fields(m.content):
                    if field == 1:
                        key = v.decode("utf-8", "replace")
                    elif field == 2:
                        value = v.decode("utf-8", "replace")
                if key:
                    info[key] = value
        return info

    def list_dir(self, path: str):
        parts = self._call(F_STORAGE_LIST_REQUEST, pb_str(1, path), timeout=15)
        status = parts[-1].status
        if status != ST_OK:
            if status_name(status) == "ERROR_STORAGE_NOT_EXIST":
                raise MissingOnFlipper(f"{path} doesn't exist on the Flipper's SD card.")
            if status_name(status) == "ERROR_STORAGE_NOT_READY":
                raise FlipperError("The Flipper's SD card isn't ready. Is it inserted?")
            raise FlipperError(f"Couldn't list {path} ({status_name(status)}).")
        entries = []
        for m in parts:
            if m.field != F_STORAGE_LIST_RESPONSE:
                continue
            for field, wire, value in pb_fields(m.content):
                if field == 1 and wire == 2:
                    entry = {"name": "", "is_dir": False, "size": 0}
                    for f2, _w2, v2 in pb_fields(value):
                        if f2 == 1:
                            entry["is_dir"] = v2 == 1
                        elif f2 == 2:
                            entry["name"] = v2.decode("utf-8", "replace")
                        elif f2 == 3:
                            entry["size"] = v2
                    if entry["name"]:
                        entries.append(entry)
        return entries

    def read_file(self, path: str, progress=None) -> bytes:
        data = bytearray()

        def on_part(m):
            if m.field == F_STORAGE_READ_RESPONSE:
                for field, _w, file_msg in pb_fields(m.content):
                    if field == 1:
                        for f2, _w2, v2 in pb_fields(file_msg):
                            if f2 == 4:
                                data.extend(v2)
                if progress:
                    progress(len(data))

        parts = self._call(F_STORAGE_READ_REQUEST, pb_str(1, path), timeout=15, on_part=on_part)
        status = parts[-1].status
        if status != ST_OK:
            if status_name(status) == "ERROR_STORAGE_NOT_EXIST":
                raise MissingOnFlipper(f"“{os.path.basename(path)}” isn't on the Flipper any more.")
            raise FlipperError(f"Couldn't read {path} ({status_name(status)}).")
        return bytes(data)

    # -- Sub-GHz ------------------------------------------------------------------------
    def _app_error(self):
        try:
            m = self._call(F_APP_GET_ERROR_REQUEST, b"", timeout=5)[-1]
        except (RpcTimeout, FlipperError):
            return None, ""
        if m.status != ST_OK or m.field != F_APP_GET_ERROR_RESPONSE:
            return None, ""
        code, text = 0, ""
        for field, _w, value in pb_fields(m.content):
            if field == 1:
                code = value
            elif field == 2:
                text = value.decode("utf-8", "replace")
        return code, text

    def _exit_app(self) -> bool:
        """Ask the remote-controlled app to close. True if it did."""
        self._app_closed.clear()
        try:
            m = self._call(F_APP_EXIT_REQUEST, b"", timeout=6)[-1]
        except (RpcTimeout, DisconnectedError):
            return False
        if m.status != ST_OK:
            return False
        self._app_closed.wait(3.0)
        time.sleep(0.15)  # give the Flipper's loader a moment to finish unloading
        return True

    def _open_subghz(self):
        self._open_app("subghz")

    def _open_app(self, kind: str):
        title = KINDS[kind]["title"]
        names = list(KINDS[kind]["apps"])
        name = names.pop(0)
        tried_exit = False
        for attempt in range(8):
            self._app_started.clear()
            self._app_closed.clear()
            m = self._call(F_APP_START_REQUEST, pb_str(1, name) + pb_str(2, "RPC"), timeout=10)[-1]
            if m.status == ST_OK:
                if self._app_started.wait(6.0):
                    return
                self._exit_app()
                if kind == "subghz":
                    raise FlipperError("The Flipper didn't open its Sub-GHz app. If the Flipper shows a message "
                                       "about its region, connect it to the official Flipper app once, then try again.")
                raise FlipperError(f"The Flipper didn't open its {title} app. Check its screen, then try again.")
            if m.status == ST_ERROR_APP_SYSTEM_LOCKED:
                if not tried_exit:
                    tried_exit = True
                    if self._exit_app():  # an earlier remote session was still open - closed it
                        log.info("Closed a remote-controlled app that was left open on the Flipper")
                        continue
                if attempt < 3:
                    time.sleep(0.6)
                    continue
                raise BusyError("Your Flipper has another app open. Press BACK on the Flipper until you're on "
                                "its home screen, then try again.")
            if m.status == ST_ERROR_INVALID_PARAMETERS and names:
                name = names.pop(0)
                continue
            raise FlipperError(f"The Flipper couldn't open {title} ({status_name(m.status)}).")
        raise BusyError("The Flipper stayed busy. Press BACK on the Flipper until you're on its home screen.")

    def _load(self, path: str, label: str, verb: str):
        m = self._call(F_APP_LOAD_FILE_REQUEST, pb_str(1, path), timeout=8)[-1]
        if m.status != ST_OK:
            _code, text = self._app_error()
            detail = f" ({text})" if text else ""
            raise FlipperError(f"“{label}” wasn't {verb}: the Flipper couldn't load the file{detail}.")

    def send_ir(self, path: str, signal: str, release_event=None, progress=lambda phase, **kw: None):
        """Open Infrared in remote mode, load the remote, press one of its buttons, release, close.

        A tap releases straight away: the Flipper always finishes the signal it started, so it goes out once.
        Held (release_event), the Flipper keeps repeating it until release, like holding a button on the remote."""
        remote = pretty_name(path)
        pressed = False
        try:
            progress("opening")
            self._open_app("infrared")
            try:
                progress("loading")
                self._load(path, f"{remote} {signal}", "sent")
                progress("sending")
                m = self._call(F_APP_BUTTON_PRESS_REQUEST, pb_str(1, signal), timeout=8)[-1]
                if m.status != ST_OK:
                    _code, text = self._app_error()
                    detail = f" ({text})" if text else ""
                    raise FlipperError(f"“{signal}” wasn't sent: it isn't a button of the {remote} remote any "
                                       f"more{detail}.")
                pressed = True
                if release_event is not None:
                    release_event.wait(MAX_HOLD_S)
                m = self._call(F_APP_BUTTON_RELEASE_REQUEST, b"", timeout=8)[-1]
                if m.status != ST_OK:
                    log.debug("Infrared release returned %s", status_name(m.status))
            finally:
                if self.alive and self.ble.connected and not self._exit_app():
                    log.warning("Couldn't close Infrared on the Flipper")
        except (DisconnectedError, RpcTimeout) as e:
            if pressed:
                log.warning("Link trouble right after sending %s %s", remote, signal)
                return
            if isinstance(e, DisconnectedError):
                raise DisconnectedError(str(e), pressed=False) from None
            raise

    def emulate(self, kind: str, path: str, stop_event, seconds: float, progress=lambda phase, **kw: None):
        """Open the RFID, NFC or iButton app in remote mode and load the file: the Flipper emulates it from then
        on. Keep going until stop_event is set, `seconds` pass, or someone presses BACK on the Flipper, then close
        the app. Returns why it stopped and after how long."""
        label = pretty_name(path)
        try:
            progress("opening")
            self._open_app(kind)
            closed_by_flipper = False
            try:
                progress("loading")
                self._load(path, label, "emulated")
                started = time.monotonic()
                progress("emulating", seconds=seconds)
                why = "time's up"
                while True:
                    left = seconds - (time.monotonic() - started)
                    if left <= 0:
                        break
                    if stop_event.wait(min(0.25, left)):
                        why = "stopped"
                        break
                    if self._app_closed.is_set():
                        why, closed_by_flipper = "closed on the Flipper", True
                        break
                    if not (self.alive and self.ble.connected):
                        raise DisconnectedError("Bluetooth dropped while emulating.", pressed=True)
                return why, time.monotonic() - started
            finally:
                if not closed_by_flipper and self.alive and self.ble.connected and not self._exit_app():
                    log.warning("Couldn't close %s on the Flipper", KINDS[kind]["title"])
        except RpcTimeout:
            raise FlipperError(f"The Flipper stopped answering while emulating “{label}”.") from None

    def transmit(self, path: str, duration_ms: int, release_event=None, progress=lambda phase: None,
                 release_early: bool = False):
        """Open Sub-GHz in remote mode, load `path`, press, hold, release, close.

        Same sequence the official app uses. On some firmwares (e.g. Xtreme) the release
        step reports an error and the signal stops when the app closes - that's expected.
        With `release_early` (for Unleashed RAW files) it releases right after the press, so the
        recording plays exactly once, then waits `duration_ms` for it to finish before closing.
        """
        label = os.path.basename(path)
        if label.lower().endswith(".sub"):
            label = label[:-4]
        label = label.replace("_", " ")
        pressed = False
        try:
            progress("opening")
            self._open_subghz()
            try:
                progress("loading")
                m = self._call(F_APP_LOAD_FILE_REQUEST, pb_str(1, path), timeout=8)[-1]
                if m.status != ST_OK:
                    _code, text = self._app_error()
                    detail = f" ({text})" if text else ""
                    raise FlipperError(f"“{label}” wasn't sent: the Flipper couldn't load the file{detail}.")
                progress("sending")
                m = self._call(F_APP_BUTTON_PRESS_REQUEST, b"", timeout=8)[-1]
                pressed_at = time.monotonic()
                if m.status != ST_OK:
                    code, text = self._app_error()
                    if code == 2:
                        reason = (text or "Transmission on this frequency is blocked by the Flipper's settings")
                        raise RegionError(f"“{label}” wasn't sent. {reason.rstrip('.')}.")
                    detail = text or status_name(m.status)
                    raise FlipperError(f"“{label}” wasn't sent ({detail}).")
                pressed = True
                if release_early:
                    m = self._call(F_APP_BUTTON_RELEASE_REQUEST, b"", timeout=8)[-1]
                    log.debug("Early release: %s", status_name(m.status))
                elif release_event is not None:
                    release_event.wait(MAX_HOLD_S)
                remaining = pressed_at + duration_ms / 1000.0 - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                if not release_early:
                    m = self._call(F_APP_BUTTON_RELEASE_REQUEST, b"", timeout=8)[-1]
                    if m.status != ST_OK:
                        log.debug("Release returned %s (normal on Xtreme-style firmware)", status_name(m.status))
            finally:
                if self.alive and self.ble.connected:
                    if not self._exit_app():
                        log.warning("Couldn't close Sub-GHz on the Flipper")
        except DisconnectedError as e:
            if pressed:
                # The signal went out; the Flipper closes the app by itself when the link drops.
                log.warning("Link dropped right after sending %s", label)
                return
            raise DisconnectedError(str(e), pressed=False) from None
        except RpcTimeout:
            if pressed:
                log.warning("Flipper stopped answering after sending %s", label)
                return
            raise


# =====================================================================================
#  Settings + cache
# =====================================================================================


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


_config_mutex = threading.Lock()


def update_config(change) -> dict:
    """Apply change(config) to config.json as it is on disk right now, save it, and return the result.
    Holds a lock file meanwhile, so whatever any other process saved in between is kept: a copy of the
    app holding an older idea of the pins can't write them back over newer ones."""
    import fcntl
    with _config_mutex:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fd = os.open(os.path.join(CONFIG_DIR, "config.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            config = _load_json(CONFIG_FILE, {})
            change(config)
            _save_json(CONFIG_FILE, config)
            return config
        finally:
            os.close(fd)


def _save_json(path: str, value):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Couldn't save %s: %s", path, e)


# =====================================================================================
#  Pairing (KDE's Bluetooth settings can't pair a Flipper; this can)
# =====================================================================================

AGENT_PATH = "/org/flipperremote/agent"
IFACE_AGENT = "org.bluez.Agent1"
IFACE_AGENT_MANAGER = "org.bluez.AgentManager1"


class PairingCancelled(FlipperError):
    pass


class PairingAgent:
    """A BlueZ pairing agent that can *type* a code.

    The Flipper only pairs with "passkey entry": it shows a 6-digit code and requires the other side
    to type it (MITM protection). KDE's agent declares it can only display/confirm codes, so BlueZ
    falls back to "Just Works", which the Flipper refuses. BlueZ uses the agent of whoever calls
    Pair(), so registering this one and calling Pair() ourselves is enough.
    """

    def __init__(self, bus: DBusConnection, ask_code):
        self.bus = bus
        self.ask_code = ask_code          # callable(cancelled_event) -> "123456" or None
        self.cancelled = threading.Event()

    def register(self):
        self.bus.export(AGENT_PATH, IFACE_AGENT, self._handle)
        try:
            self.bus.call(BLUEZ, "/org/bluez", IFACE_AGENT_MANAGER, "RegisterAgent", "os",
                          [AGENT_PATH, "KeyboardDisplay"], timeout=10)
        except DBusError as e:
            if not e.name.endswith(".AlreadyExists"):
                self.bus.unexport(AGENT_PATH, IFACE_AGENT)
                raise bluez_error_to_flipper(e) from None

    def unregister(self):
        try:
            self.bus.call(BLUEZ, "/org/bluez", IFACE_AGENT_MANAGER, "UnregisterAgent", "o", [AGENT_PATH], timeout=5)
        except DBusError:
            pass
        self.bus.unexport(AGENT_PATH, IFACE_AGENT)

    def _handle(self, member, args):
        log.info("Pairing agent: %s", member)
        if member in ("RequestPasskey", "RequestPinCode"):
            code = self.ask_code(self.cancelled)
            if code is None:
                raise DBusError("org.bluez.Error.Canceled", "Pairing cancelled")
            return ("u", [int(code)]) if member == "RequestPasskey" else ("s", [str(code)])
        if member == "RequestConfirmation":
            # Numeric comparison (not what the Flipper's serial profile uses). The Flipper asks its
            # own user to confirm too, so accepting here is safe for a pairing we started.
            return "", []
        if member == "Cancel":
            self.cancelled.set()
            return "", []
        if member in ("Release", "DisplayPasskey", "DisplayPinCode", "RequestAuthorization", "AuthorizeService"):
            return "", []
        raise DBusError("org.freedesktop.DBus.Error.UnknownMethod", member)


def explain_pair_error(e: DBusError, name: str) -> str:
    n = e.name
    if n.endswith(".AuthenticationFailed"):
        return "The code didn't match. Try again and type the code exactly as the Flipper shows it."
    if n.endswith(".AuthenticationCanceled") or n.endswith(".Canceled"):
        return "Pairing was cancelled."
    if n.endswith(".AuthenticationRejected"):
        return f"{name} turned the pairing down. Try again."
    if n.endswith(".AuthenticationTimeout") or n == "org.freedesktop.DBus.Error.NoReply":
        return "Pairing timed out. Type the code within about 30 seconds of it appearing."
    if n.endswith(".InProgress"):
        return "Another pairing is already in progress. Wait a moment and try again."
    if n.endswith(".ConnectionAttemptFailed") or "page timeout" in e.message.lower() or "abort" in e.message.lower():
        return f"Couldn't reach {name}. {UNREACHABLE_HELP}"
    return f"Pairing failed: {e.message or n}"


class WebPairing:
    """Drives a pairing from the UI: progress goes out as "pairing" events; choices come back via the API."""

    def __init__(self, emit):
        self._emit = emit
        self._choices = queue.Queue()
        self._confirms = queue.Queue()
        self._codes = queue.Queue()
        self._cancel = threading.Event()
        self._last_view = None
        self.device_path = None
        self.finished = False   # set once it has said how it ended; a new "Pair a Flipper" then starts afresh

    # called by the pairing job
    def found(self, devices):
        view = [{"address": d["address"], "name": d["name"], "paired": d["paired"], "nearby": d["rssi"] is not None}
                for d in devices]
        if view != self._last_view:
            self._last_view = view
            self.step("searching", devices=view)

    def wait_choice(self, timeout: float):
        try:
            return self._choices.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_confirm(self, timeout: float):
        try:
            return self._confirms.get(timeout=timeout)
        except queue.Empty:
            return None

    def step(self, step: str, **extra):
        if step in ("done", "works", "error", "cancelled"):
            self.finished = True
        self._emit("pairing", dict(extra, step=step))

    def ask_code(self, name: str, agent_cancelled: threading.Event):
        self.step("code", device=name)
        deadline = time.monotonic() + 58
        while time.monotonic() < deadline:
            if self._cancel.is_set() or agent_cancelled.is_set():
                return None
            try:
                return self._codes.get(timeout=0.3)
            except queue.Empty:
                continue
        return None

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # called from the API
    def choose(self, address: str):
        self._choices.put(address)

    def confirm(self, remove: bool):
        self._confirms.put(bool(remove))

    def code(self, code: str):
        self._codes.put(code)

    def cancel(self):
        self._cancel.set()


class ConsolePairing:
    """The same pairing flow in a terminal: flipper-remote pair"""

    def __init__(self):
        self._seen = {}
        self._start = time.monotonic()
        self._hinted = False
        self.device_path = None

    def found(self, devices):
        for d in devices:
            if d["rssi"] is not None and d["address"] not in self._seen:
                self._seen[d["address"]] = d
                print(f"  Found {d['name']} [{d['address']}]{'  (paired before)' if d['paired'] else ''}")

    def wait_choice(self, timeout: float):
        time.sleep(timeout)
        elapsed = time.monotonic() - self._start
        nearby = list(self._seen.values())
        if len(nearby) == 1 and elapsed > 4:
            return nearby[0]["address"]
        if len(nearby) > 1 and elapsed > 8:
            for i, d in enumerate(nearby, 1):
                print(f"  {i}. {d['name']} [{d['address']}]")
            while True:
                answer = input("  Which one? Type its number: ").strip()
                if answer.isdigit() and 1 <= int(answer) <= len(nearby):
                    return nearby[int(answer) - 1]["address"]
        if not nearby and elapsed > 15 and not self._hinted:
            self._hinted = True
            print("  Still looking. Is the Flipper's Bluetooth ON (Settings → Bluetooth), is it on its main screen,\n"
                  "  and is your phone's Bluetooth off?")
        return None

    def wait_confirm(self, timeout: float):
        answer = input("  Remove that pairing and pair again? Type yes to do that, or press Enter to keep it: ")
        return answer.strip().lower() in ("y", "yes")

    def step(self, step: str, device: str = "", message: str = "", **_extra):
        text = {"searching": "Looking for Flippers nearby…",
                "testing": f"{device} is already paired with this computer. Checking that pairing still works…",
                "works": f"{device} is already paired and connects fine, so nothing was changed.",
                "confirm": f"{device} is paired, but it didn't connect: {message}",
                "removing": f"Removing the old pairing with {device}…",
                "pairing": f"Pairing with {device}. Your Flipper will show a 6-digit code…",
                "done": f"Paired with {device}.",
                "cancelled": "Pairing cancelled."}.get(step)
        if text:
            print(f"  {text}")

    def ask_code(self, name: str, agent_cancelled: threading.Event):
        for _ in range(3):
            code = input("  Type the 6-digit code shown on your Flipper, then press Enter: ").strip()
            if agent_cancelled.is_set():
                return None
            if re.fullmatch(r"\d{6}", code):
                return code
            print("  That's not 6 digits.")
        return None

    def cancelled(self) -> bool:
        return False


# =====================================================================================
#  Controller: one worker thread owns the Flipper; everything else queues jobs
# =====================================================================================


class Job:
    _ids = itertools.count(1)

    def __init__(self, kind: str, **kw):
        self.id = next(Job._ids)
        self.kind = kind
        self.kw = kw
        self.done = threading.Event()
        self.result = None
        self.error = None

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise FlipperError("Timed out waiting for the Flipper.")
        if self.error is not None:
            raise self.error
        return self.result


class Controller:
    USER, BACKGROUND = 0, 5

    def __init__(self, device_hint: str = None, stay_connected: bool = False):
        self.device_hint = device_hint
        self._connect_budget = None   # a shorter limit on connecting, while pairing checks an existing pairing
        self._emulating = set()       # keys of cards/keys being emulated (their stop events are in _holds)
        self.stay_connected = stay_connected
        self.config = _load_json(CONFIG_FILE, {})
        self.cache = _load_json(CACHE_FILE, {})
        self.cache.setdefault("dirs", {})
        self.cache.setdefault("meta", {})
        self._lock = threading.RLock()
        self._jobs = queue.PriorityQueue()
        self._seq = itertools.count()
        self._bus = None
        self._session = None
        self._holds = {}
        self._meta_pending = set()
        self._list_pending = set()
        self._refreshed = set()
        self._last_failure = None
        self._last_address = None   # the Flipper paired most recently from this app
        self._pairing = None
        self._pending_user_jobs = 0
        self._listeners = []
        self._stopped = threading.Event()
        self.state = {"state": "idle", "message": "", "device": None, "address": None, "battery": None}
        self._worker = threading.Thread(target=self._run, name="flipper-worker", daemon=True)

    # -- plumbing -------------------------------------------------------------------------
    def add_listener(self, fn):
        self._listeners.append(fn)

    def _emit(self, event: str, data):
        for fn in list(self._listeners):
            try:
                fn(event, data)
            except Exception:  # noqa: BLE001
                log.exception("listener failed")

    def _set_state(self, state: str, message: str = "", kind: str = "", **extra):
        with self._lock:
            self.state.update({"state": state, "message": message, "kind": kind}, **extra)
            snapshot = dict(self.state)
        self._emit("status", snapshot)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": dict(self.state),
                "favorites": self._favorites_view(),
                "settings": self.settings(),
                "root": SUBGHZ_ROOT,
                "version": VERSION,
            }

    # -- window settings, kept in config.json so they survive restarts -----------------------------
    SETTINGS_DEFAULTS = {"pinned_only": False, "theme": None, "folder": SUBGHZ_ROOT, "show": "all",
                         "folders": {}, "window": {}}

    def settings(self) -> dict:
        out = dict(self.SETTINGS_DEFAULTS)
        saved = self.config.get("ui")
        if isinstance(saved, dict):
            out.update({k: v for k, v in saved.items() if k in out})
        if out["show"] not in SHOW_CHOICES:
            out["show"] = "all"
        folders = dict(out["folders"]) if isinstance(out["folders"], dict) else {}
        folders.setdefault("subghz", out["folder"])   # where Sub-GHz was, before there was more than Sub-GHz
        out["folders"] = folders
        return out

    @staticmethod
    def _folder_ok(show: str, path: str) -> bool:
        if ".." in path.split("/"):
            return False
        if show == "all":
            return path == ALL_ROOT or kind_of(path) is not None
        return kind_of(path) == show

    def update_settings(self, changes: dict, source: str = "") -> dict:
        clean = {}
        if "pinned_only" in changes:
            clean["pinned_only"] = bool(changes["pinned_only"])
            was = (self.config.get("ui") or {}).get("pinned_only") if isinstance(self.config.get("ui"), dict) else None
            if bool(was) != clean["pinned_only"]:
                log.info("Pinned only switched %s%s", "on" if clean["pinned_only"] else "off",
                         f" ({source})" if source else "")
        if changes.get("theme", "") in ("light", "dark"):
            clean["theme"] = changes["theme"]
        if "folder" in changes:
            folder = "/" + str(changes["folder"] or "").strip("/")
            if folder == SUBGHZ_ROOT or folder.startswith(SUBGHZ_ROOT + "/"):
                clean["folder"] = folder
        if changes.get("show") in SHOW_CHOICES:
            clean["show"] = changes["show"]
            if clean["show"] != self.settings()["show"]:
                title = KINDS[clean["show"]]["title"] if clean["show"] in KINDS else "All"
                log.info("Showing %s%s", title, f" ({source})" if source else "")
        if isinstance(changes.get("folders"), dict):
            folders = {}
            for show, folder in changes["folders"].items():
                folder = "/" + str(folder or "").strip("/")
                if show in SHOW_CHOICES and self._folder_ok(show, folder):
                    folders[show] = folder
            if folders:
                clean["folders"] = folders
        if isinstance(changes.get("window"), dict):
            # what the page measured: the window's frame (title bar etc.) and the size that fits the pins
            w = changes["window"]
            win = {}
            frame, fit = _size_pair(w.get("frame"), 0, 200), _size_pair(w.get("fit"), 200, 4000)
            if frame:
                win["frame"] = list(frame)
            if fit and isinstance(w.get("pins"), str) and len(w["pins"]) < 20000:
                win["fit"], win["pins"] = list(fit), w["pins"]
            if win:
                clean["window"] = win
        if clean:
            def change(config):
                ui = config.get("ui") if isinstance(config.get("ui"), dict) else {}
                if "window" in clean:
                    old = ui.get("window") if isinstance(ui.get("window"), dict) else {}
                    clean["window"] = dict(old, **clean["window"])
                if "folders" in clean:
                    old = ui.get("folders") if isinstance(ui.get("folders"), dict) else {}
                    clean["folders"] = dict(old, **clean["folders"])
                config["ui"] = dict(ui, **clean)
            with self._lock:
                self.config = update_config(change)
            self._emit("settings", self.settings())
        return self.settings()

    def _favorites_view(self):
        out = []
        for f in self.config.get("favorites", []):
            if not isinstance(f, dict):
                continue
            path, signal = str(f.get("path") or ""), f.get("signal") or None
            kind = file_kind(path)
            if not kind:
                continue
            item = {"key": key_of(path, signal), "path": path, "kind": kind, "label": f.get("label")}
            if signal:
                item["signal"] = signal
            meta = self.cache["meta"].get(path)
            if meta and signal:
                protocol = next((x.get("protocol") for x in meta.get("signals") or [] if x.get("name") == signal), None)
                item["meta"] = {"protocol": protocol}
            elif meta:
                item["meta"] = shown_meta(kind, meta)
                if kind == "infrared":
                    item["meta"] = {"buttons": len(item["meta"]["signals"])}
            out.append(item)
        return out

    def start(self):
        self._worker.start()

    def submit(self, kind: str, priority: int = USER, **kw) -> Job:
        job = Job(kind, **kw)
        if kind == "send":
            job.kw["job_id"] = job.id
        if priority == self.USER:
            with self._lock:
                if self._pending_user_jobs >= 6:
                    raise BusyError("Hang on, still working through the last few presses.")
                self._pending_user_jobs += 1
        job.priority = priority
        self._jobs.put((priority, next(self._seq), job))
        return job

    def stop(self, disconnect: bool = True):
        if self._stopped.is_set():
            return
        self._stopped.set()
        for ev in list(self._holds.values()):
            ev.set()
        done = threading.Event()
        self._jobs.put((-1, next(self._seq), ("stop", disconnect and not self.stay_connected, done)))
        if self._worker.is_alive():
            done.wait(20)

    def _run(self):
        while True:
            _prio, _seq, job = self._jobs.get()
            if isinstance(job, tuple) and job[0] == "stop":
                self._close_session(disconnect=job[1])
                if self._bus is not None:
                    self._bus.close()
                job[2].set()
                return
            try:
                job.result = getattr(self, "_job_" + job.kind)(**job.kw)
            except FlipperError as e:
                job.error = e
            except DBusError as e:
                job.error = bluez_error_to_flipper(e)
            except Exception as e:  # noqa: BLE001
                log.exception("Unexpected error in %s", job.kind)
                job.error = FlipperError(f"Unexpected error: {e}")
            finally:
                if job.priority == self.USER:
                    with self._lock:
                        self._pending_user_jobs -= 1
                job.done.set()

    # -- connection -----------------------------------------------------------------------
    def _pick_device(self) -> dict:
        devices = find_flippers(self._bus)
        want = (self.device_hint or self.config.get("device") or "").strip()
        if want:
            norm = want.upper().replace("_", ":")
            for d in devices:
                if d["address"].upper() == norm or d["name"].lower() == want.lower():
                    return d
            raise NotFoundError(f"Couldn't find a paired Bluetooth device matching “{want}”.")
        paired = [d for d in devices if d["paired"]]
        if paired:
            last = (self._last_address or "").upper()
            paired.sort(key=lambda d: (d["address"].upper() != last, not d["connected"]))
            return paired[0]
        if devices:
            return devices[0]
        raise NotFoundError("No paired Flipper found. Use “Pair a Flipper” at the bottom of the window (or run "
                            "flipper-remote pair). KDE's Bluetooth settings can't pair a Flipper.")

    def _session_ok(self) -> bool:
        s = self._session
        return s is not None and s.alive and s.ble.connected and self._bus is not None and not self._bus.closed

    def _ensure_session(self, auto: bool = False) -> FlipperSession:
        if self._session_ok():
            return self._session
        self._close_session(disconnect=False)
        if auto and self._last_failure and time.monotonic() - self._last_failure[0] < 20:
            raise self._last_failure[1]  # don't hammer a Flipper that just failed to connect
        self._set_state("connecting", "Looking for your Flipper…")
        try:
            return self._open_session()
        except FlipperError as e:
            self._last_failure = (time.monotonic(), e)
            raise

    def _open_session(self) -> FlipperSession:
        try:
            return self._open_session_once()
        except DisconnectedError:
            log.info("The link dropped while setting up; trying once more")
            time.sleep(0.5)
            try:
                return self._open_session_once()
            except DisconnectedError:
                err = ConnectError(FlipperSession.NOT_ANSWERING.format(name="The Flipper"))
                self._set_state("error", str(err))
                raise err from None

    def _open_session_once(self) -> FlipperSession:
        try:
            if self._bus is None or self._bus.closed:
                try:
                    self._bus = DBusConnection.system()
                except DBusError as e:
                    raise FlipperError(f"Can't reach the system D-Bus ({e.message}).") from None
            device = self._pick_device()
            session = FlipperSession(self._bus, device)
            if self._connect_budget:
                session.ble.CONNECT_BUDGET_S = self._connect_budget
            session.on_lost = self._on_session_lost
            session.open(status=lambda msg: self._set_state("connecting", msg, device=device["name"]))
        except DisconnectedError:
            raise   # _open_session tries once more; no point showing an error in between
        except FlipperError as e:
            self._set_state("error", str(e), kind=e.kind)
            raise
        except DBusError as e:
            err = bluez_error_to_flipper(e)
            self._set_state("error", str(err))
            raise err from None
        self._session = session
        self._last_failure = None
        self._set_state("connected", "", device=device["name"], address=device["address"],
                        battery=session.ble.battery())
        return session

    def _on_session_lost(self, session):
        if session is self._session:
            self._set_state("disconnected", "Flipper disconnected. It will reconnect when you press a button.")

    def _close_session(self, disconnect: bool):
        s, self._session = self._session, None
        if s is not None:
            try:
                s.close(disconnect=disconnect)
            except Exception:  # noqa: BLE001
                log.exception("closing session")

    def _with_session(self, fn, auto: bool = False):
        try:
            try:
                return fn(self._ensure_session(auto))
            except DisconnectedError as e:
                if e.pressed:
                    raise
                log.info("Connection dropped; reconnecting and retrying once")
                self._close_session(disconnect=False)
                return fn(self._ensure_session())
        except RpcTimeout:
            # The Flipper stopped answering mid-conversation. Start over with a fresh connection.
            log.warning("The Flipper stopped answering; dropping the connection")
            self._close_session(disconnect=True)
            self._set_state("disconnected", "The Flipper stopped answering. It will reconnect on the next press.")
            raise FlipperError("The Flipper stopped answering. Try again and it will reconnect.") from None

    # -- jobs ------------------------------------------------------------------------------
    def _job_connect(self):
        self._ensure_session()
        return True

    def _job_disconnect(self):
        self._close_session(disconnect=True)
        self._set_state("disconnected", "Disconnected.")
        return True

    def _job_list(self, path: str, auto: bool = False):
        if file_kind(path) == "infrared":
            return self._job_remote(path, auto=auto)
        kind = kind_of(path)
        try:
            entries = self._with_session(lambda s: s.list_dir(path), auto=auto)
        except MissingOnFlipper:
            if path != KINDS[kind]["root"]:
                self._emit("files_error", {"path": path, "message": f"{path} isn't on the Flipper's SD card any more."})
                raise
            entries = []   # nothing of this kind saved yet: an empty folder, not a problem
        except FlipperError as e:
            self._emit("files_error", {"path": path, "message": str(e)})
            raise
        finally:
            self._list_pending.discard(path)
        at_root = path.rstrip("/") == KINDS[kind]["root"]
        ext = KINDS[kind]["ext"]
        dirs = sorted((e for e in entries if e["is_dir"] and not e["name"].startswith(".")
                       and not (at_root and e["name"].lower() in HIDDEN_AT_ROOT)),
                      key=lambda e: natural_key(e["name"]))
        files = sorted((e for e in entries if not e["is_dir"] and e["name"].lower().endswith(ext)
                        and not e["name"].startswith(".")), key=lambda e: natural_key(e["name"]))
        listing = {"path": path, "dirs": [e["name"] for e in dirs],
                   "files": [{"name": e["name"], "size": e["size"]} for e in files], "time": time.time()}
        with self._lock:
            self.cache["dirs"][path] = listing
            self._refreshed.add(path)
        _save_json(CACHE_FILE, self.cache)
        self._emit("files", self._decorate(listing))
        # Read small files in the background so buttons can show frequency/protocol, a remote its buttons...
        for f in files[:40]:
            full = f"{path.rstrip('/')}/{f['name']}"
            meta = self.cache["meta"].get(full)
            if f["size"] <= 8192 and (not meta or meta.get("size") != f["size"]) and full not in self._meta_pending:
                self._meta_pending.add(full)
                self.submit("meta", priority=self.BACKGROUND, path=full, size=f["size"])
        return listing

    def _job_remote(self, path: str, auto: bool = False):
        """An Infrared remote, opened like a folder: its buttons."""
        try:
            size = self._known_size(path)
            if size is not None and size > MAX_REMOTE_BYTES:
                raise FlipperError(f"“{pretty_name(path)}” is too big to open here ({size // 1024} KB).")
            meta = self.cache["meta"].get(path)
            if not meta or size is None or meta.get("size") != size or path not in self._refreshed or not auto:
                meta = self._with_session(lambda s: self._read_meta(s, path, size), auto=auto)   # (not auto: Refresh)
        except FlipperError as e:
            self._emit("files_error", {"path": path, "message": str(e)})
            raise
        finally:
            self._list_pending.discard(path)
        if not meta.get("valid"):
            message = f"“{pretty_name(path)}” isn't an Infrared remote file the Flipper made."
            self._emit("files_error", {"path": path, "message": message})
            raise FlipperError(message)
        listing = {"path": path, "remote": True, "dirs": [], "files": [], "time": time.time()}
        with self._lock:
            self.cache["dirs"][path] = listing
            self._refreshed.add(path)
        _save_json(CACHE_FILE, self.cache)
        self._emit("files", self._decorate(listing))
        return listing

    def _decorate(self, listing: dict) -> dict:
        out = dict(listing)
        path = listing["path"]
        out["kind"] = kind_of(path)
        if listing.get("remote"):
            meta = self.cache["meta"].get(path) or {}
            out["signals"] = meta.get("signals") or []
            return out
        files = []
        for f in listing.get("files", []):
            full = f"{path.rstrip('/')}/{f['name']}"
            kind = file_kind(full)
            meta = self.cache["meta"].get(full)
            item = dict(f, path=full, kind=kind)
            if meta and meta.get("size") == f["size"]:
                item["meta"] = shown_meta(kind, meta)
                if kind == "infrared":   # in a folder, a remote just says how many buttons it has
                    item["meta"] = {"buttons": len(item["meta"]["signals"])}
            files.append(item)
        out["files"] = files
        return out

    def _read_meta(self, session: FlipperSession, path: str, size, progress=None) -> dict:
        data = session.read_file(path, progress=progress)
        kind = file_kind(path)
        meta = parse_saved_file(kind, data)
        meta["size"] = size if size is not None else len(data)
        with self._lock:
            self.cache["meta"][path] = meta
        _save_json(CACHE_FILE, self.cache)
        self._emit("meta", {"path": path, "kind": kind, "meta": shown_meta(kind, meta)})
        return meta

    def _job_meta(self, path: str, size: int):
        try:
            meta = self.cache["meta"].get(path)
            if meta and meta.get("size") == size:
                return meta
            if not self._session_ok():
                return None  # don't connect just for background work
            return self._read_meta(self._session, path, size)
        finally:
            self._meta_pending.discard(path)

    def _known_size(self, path: str):
        folder, name = path.rsplit("/", 1)
        listing = self.cache["dirs"].get(folder) or {}
        for f in listing.get("files", []):
            if f["name"] == name:
                return f["size"]
        return None

    def _job_send(self, path: str, signal=None, hold_event=None, job_id: int = 0, min_ms: int = 0,
                  source: str = ""):
        kind = file_kind(path)
        key = key_of(path, signal)
        note = f"; {source}" if source else ""

        def progress(phase, **extra):
            self._emit("job", dict({"id": job_id, "key": key, "path": path, "phase": phase}, **extra))

        def run(session: FlipperSession):
            if kind == "infrared":
                release = hold_event
                if release is None and min_ms:   # the command line's --hold: keep the button down that long
                    release = threading.Event()
                    threading.Timer(min(min_ms / 1000, MAX_HOLD_S), release.set).start()
                log.info("Sending %s: %s (%s%s)", path, signal, "press-and-hold" if release else "once", note)
                session.send_ir(path, signal, release_event=release, progress=progress)
                return
            if KINDS[kind]["emulate"]:
                seconds = min(max(min_ms / 1000, 1), MAX_EMULATE_S) if min_ms else EMULATE_S
                log.info("Emulating %s (up to %d s%s)", path, seconds, note)
                why, took = session.emulate(kind, path, hold_event or threading.Event(), seconds, progress=progress)
                log.info("Stopped emulating %s after %.1f s (%s)", path, took, why)
                return
            size = self._known_size(path)
            meta = self.cache["meta"].get(path)
            if not meta or (size is not None and meta.get("size") != size):
                progress("reading")
                meta = self._read_meta(session, path, size,
                                       progress=lambda n: progress("reading", bytes=n, size=size))
            duration = max(send_duration_ms(meta), int(min_ms or 0))
            raw_ms = (meta or {}).get("raw_ms")
            release_early = bool(raw_ms) and hold_event is None and not min_ms and session.raw_loops_while_held
            if release_early:
                duration = int(min(raw_ms + 350, MAX_RAW_MS))  # play once, then let it finish before closing
            log.info("Sending %s (%d ms%s%s%s)", path, duration, ", press-and-hold" if hold_event else "",
                     ", release early" if release_early else "", note)
            session.transmit(path, duration, release_event=hold_event, progress=progress,
                             release_early=release_early)

        try:
            if not self._session_ok():
                progress("connecting")
            self._with_session(run)
            progress("done")
        except FlipperError as e:
            progress("error", message=str(e))
            raise
        finally:
            with self._lock:
                if self._holds.get(key) is hold_event:
                    self._holds.pop(key, None)
                    self._emulating.discard(key)
        return True

    # -- pairing ---------------------------------------------------------------------------------
    def _bus_or_connect(self) -> DBusConnection:
        if self._bus is None or self._bus.closed:
            try:
                self._bus = DBusConnection.system()
            except DBusError as e:
                raise FlipperError(f"Can't reach the system D-Bus ({e.message}).") from None
        return self._bus

    def _powered_adapter(self) -> str:
        try:
            objects = self._bus.call(BLUEZ, "/", IFACE_OBJMGR, "GetManagedObjects")[0]
        except DBusError as e:
            raise bluez_error_to_flipper(e) from None
        adapters = sorted(p for p, i in objects.items() if IFACE_ADAPTER in i)
        if not adapters:
            raise FlipperError("This computer doesn't have a Bluetooth adapter.")
        powered = [p for p in adapters if objects[p][IFACE_ADAPTER].get("Powered")]
        if not powered:
            raise ConnectError("Bluetooth is turned off on this Steam Deck. Turn it on and try again.")
        return powered[0]

    def _pair_wait_for_choice(self, ui) -> dict:
        deadline = time.monotonic() + 120
        while True:
            if ui.cancelled():
                raise PairingCancelled("Pairing cancelled.")
            ui.found(find_flippers(self._bus))
            address = ui.wait_choice(1.0)
            if address:
                for d in find_flippers(self._bus):
                    if d["address"] == address:
                        return d
            if time.monotonic() > deadline:
                raise FlipperError("No Flipper showed up. Turn its Bluetooth ON (Settings → Bluetooth), go to its main "
                                   "screen, and turn off your phone's Bluetooth, then try again.")

    def _pair_wait_rediscovered(self, ui, address: str) -> dict:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if ui.cancelled():
                raise PairingCancelled("Pairing cancelled.")
            for d in find_flippers(self._bus):
                if d["address"] == address and not d["paired"] and d["rssi"] is not None:
                    return d
            time.sleep(0.5)
        raise FlipperError(f"Lost sight of the Flipper after removing the old pairing. {UNREACHABLE_HELP}")

    PAIR_TEST_BUDGET_S = 30.0   # how long pairing tries an existing pairing before offering to replace it

    def _pair_discover(self, bus, adapter) -> bool:
        try:
            bus.call(BLUEZ, adapter, IFACE_ADAPTER, "SetDiscoveryFilter", "a{sv}",
                     [{"Transport": Variant("s", "le")}], timeout=5)
        except DBusError:
            pass
        try:
            bus.call(BLUEZ, adapter, IFACE_ADAPTER, "StartDiscovery", timeout=10)
            return True
        except DBusError as e:
            if not e.name.endswith(".InProgress"):  # someone else is already scanning: fine
                raise bluez_error_to_flipper(e) from None
            return False

    def _pair_test_existing(self, ui, device: dict):
        """Does the pairing this computer already has with the Flipper still work? Connect to it and see."""
        s = self._session
        if self._session_ok() and s.device["address"].upper() == device["address"].upper():
            return True, ""
        ui.step("testing", device=device["name"])
        if self._session_ok():
            self._close_session(disconnect=True)   # connected to a different Flipper
        saved = (self.device_hint, self._connect_budget)
        self.device_hint, self._connect_budget = device["address"], self.PAIR_TEST_BUDGET_S
        try:
            self._ensure_session()
            works, reason = True, ""
        except FlipperError as e:
            works, reason = False, str(e)
        finally:
            self.device_hint, self._connect_budget = saved
        if ui.cancelled():
            raise PairingCancelled("Pairing cancelled.")
        return works, reason

    def _pair_wait_confirm(self, ui) -> bool:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if ui.cancelled():
                raise PairingCancelled("Pairing cancelled.")
            answer = ui.wait_confirm(1.0)
            if answer is not None:
                return bool(answer)
        return False

    def _announce_connected(self):
        s = self._session
        if s is not None and self._session_ok():
            self._set_state("connected", "", device=s.device["name"], address=s.device["address"],
                            battery=s.ble.battery())

    def _job_pair(self, ui):
        if ui.cancelled():
            return False
        was_connected = self._session_ok()
        with self._lock:
            before = {k: self.state.get(k, "") for k in ("state", "message", "kind")}
        bus = adapter = agent = None
        discovering = removed = kept = False
        chosen = {"name": "your Flipper"}
        failure = None
        try:
            # The connection stays up while the list is showing: nothing changes until a Flipper is picked.
            self._set_state("pairing", "Pairing a Flipper…")
            bus = self._bus_or_connect()
            adapter = self._powered_adapter()
            agent = PairingAgent(bus, ask_code=lambda cancelled: ui.ask_code(chosen["name"], cancelled))
            agent.register()
            discovering = self._pair_discover(bus, adapter)
            ui.step("searching", devices=[])
            device = self._pair_wait_for_choice(ui)
            chosen.update(device)
            log.info("Pairing: %s (%s) picked%s", device["name"], device["address"],
                     ", which is already paired" if device["paired"] else "")
            if device["paired"]:
                # A pairing that works is never thrown away. (Picking an already-paired Flipper here used to
                # remove its pairing at once; if the new pairing then didn't finish, the Flipper was unpaired.)
                works, reason = self._pair_test_existing(ui, device)
                if works:
                    log.info("Pairing: %s is already paired and connects, so nothing was changed", device["name"])
                    kept = True
                    ui.step("works", device=device["name"])
                else:
                    log.info("Pairing: %s is paired but didn't connect (%s). Asking before removing that pairing",
                             device["name"], reason.rstrip("."))
                    ui.step("confirm", device=device["name"], message=reason)
                    if not self._pair_wait_confirm(ui):
                        raise PairingCancelled("Kept the existing pairing.")
                    log.info("Pairing: removing the old pairing with %s", device["name"])
                    self._close_session(disconnect=True)
                    discovering = self._pair_discover(bus, adapter) or discovering  # the check may have ended it
                    ui.step("removing", device=device["name"])
                    removed = True
                    try:
                        bus.call(BLUEZ, adapter, IFACE_ADAPTER, "RemoveDevice", "o", [device["path"]], timeout=10)
                    except DBusError as e:
                        log.info("RemoveDevice: %s", e)
                    device = self._pair_wait_rediscovered(ui, device["address"])
            if not kept:
                self._close_session(disconnect=True)   # afterwards, connect to the Flipper being paired
                if discovering:  # pairing is more reliable with scanning stopped
                    try:
                        bus.call(BLUEZ, adapter, IFACE_ADAPTER, "StopDiscovery", timeout=5)
                    except DBusError:
                        pass
                    discovering = False
                ui.step("pairing", device=chosen["name"])
                ui.device_path = device["path"]
                try:
                    bus.call(BLUEZ, device["path"], IFACE_DEVICE, "Pair", timeout=90)
                except DBusError as e:
                    if not e.name.endswith(".AlreadyExists"):
                        if ui.cancelled():
                            raise PairingCancelled("Pairing cancelled.") from None
                        raise PairingProblem(explain_pair_error(e, chosen["name"])) from None
                try:
                    bus.call(BLUEZ, device["path"], IFACE_PROPS, "Set", "ssv",
                             [IFACE_DEVICE, "Trusted", Variant("b", True)], timeout=5)
                except DBusError as e:
                    log.info("Couldn't mark the Flipper as trusted: %s", e)
                log.info("Paired with %s (%s)", chosen["name"], device["address"])
                self._last_address = device["address"]
                ui.step("done", device=chosen["name"], address=device["address"])
        except PairingCancelled as e:
            log.info("Pairing: %s%s", e, " The old pairing was already removed." if removed else "")
            ui.step("cancelled")
            failure = e
        except FlipperError as e:
            log.info("Pairing didn't work: %s", e)
            ui.step("error", message=str(e))
            failure = e
        except DBusError as e:
            failure = bluez_error_to_flipper(e)
            log.info("Pairing didn't work: %s", failure)
            ui.step("error", message=str(failure))
        finally:
            ui.device_path = None
            if discovering and bus is not None:
                try:
                    bus.call(BLUEZ, adapter, IFACE_ADAPTER, "StopDiscovery", timeout=5)
                except DBusError:
                    pass
            if agent is not None:
                agent.unregister()
            with self._lock:
                if self._pairing is ui:
                    self._pairing = None
        if failure is not None:
            # Put things back the way they were, as far as that's possible.
            if removed:
                self._set_state("error", f"{chosen['name']} isn't paired with this computer any more. Use “Pair a "
                                         "Flipper” to pair it again.", kind="not_found")
            elif self._session_ok() or was_connected:
                try:
                    self._ensure_session()
                    self._announce_connected()
                except FlipperError:
                    pass
            elif before["state"] == "error":
                self._set_state("error", before["message"], kind=before["kind"])
            else:
                self._set_state("disconnected", "Not connected.")
            raise failure
        self._last_failure = None
        try:
            self._ensure_session()  # connect right away, so the window is ready to use
            self._announce_connected()
        except FlipperError:
            pass
        return True

    def pair_start(self):
        with self._lock:
            if self._pairing is not None and not self._pairing.finished and not self._pairing.cancelled():
                return self._pairing
            ui = WebPairing(self._emit)
            self._pairing = ui
        self.hold_stop()
        try:
            self.submit("pair", ui=ui)
        except BusyError:
            with self._lock:
                self._pairing = None
            raise
        return ui

    def pair_choose(self, address: str):
        ui = self._pairing
        if ui is None:
            raise FlipperError("Pairing isn't running any more. Start it again.")
        ui.choose(address)

    def pair_confirm(self, remove: bool):
        ui = self._pairing
        if ui is None:
            raise FlipperError("Pairing isn't running any more. Start it again.")
        ui.confirm(remove)

    def pairing_active(self) -> bool:
        ui = self._pairing
        return ui is not None and not ui.finished and not ui.cancelled()

    def pair_code(self, code: str):
        ui = self._pairing
        if ui is None:
            raise FlipperError("Pairing isn't running any more. Start it again.")
        if not re.fullmatch(r"\d{6}", code or ""):
            raise FlipperError("Type all 6 digits of the code.")
        ui.code(code)

    def pair_cancel(self):
        ui = self._pairing
        if ui is None:
            return
        ui.cancel()
        path = ui.device_path
        if path and self._bus is not None and not self._bus.closed:
            try:  # interrupt a Pair() that's waiting on the Flipper
                self._bus.call(BLUEZ, path, IFACE_DEVICE, "CancelPairing", timeout=5)
            except DBusError:
                pass

    # -- public helpers used by the UI server / CLI ---------------------------------------------
    def listing(self, path: str, refresh: bool = False):
        path = "/" + str(path or "").strip("/")
        if path == ALL_ROOT:   # "All": the kinds' folders, side by side
            return {"path": ALL_ROOT, "all": True, "kind": None, "files": [],
                    "dirs": [k["root"][len(ALL_ROOT) + 1:] for k in KINDS.values()]}
        kind = kind_of(path)
        if not kind or ".." in path.split("/"):
            raise FlipperError("Only the Sub-GHz, Infrared, RFID, NFC and iButton folders can be browsed.")
        if file_kind(path) and kind != "infrared":
            raise FlipperError("That's a saved file, not a folder.")
        cached = self.cache["dirs"].get(path)
        if (refresh or path not in self._refreshed) and path not in self._list_pending:
            try:
                self._list_pending.add(path)
                self.submit("list", priority=self.USER, path=path, auto=not refresh)
            except BusyError:
                self._list_pending.discard(path)
        if cached:
            return self._decorate(cached)
        return {"path": path, "kind": kind, "dirs": None, "files": None, "remote": bool(file_kind(path))}

    def _stop_emulations(self, keep: str = None):
        """A card or key being emulated gives way to whatever is pressed next."""
        with self._lock:
            events = [self._holds[k] for k in self._emulating if k != keep and k in self._holds]
        for ev in events:
            ev.set()

    def send(self, path: str, min_ms: int = 0, source: str = "", signal: str = None) -> Job:
        key = key_of(path, signal)
        self._stop_emulations(keep=key)
        kind = file_kind(path)
        if kind and KINDS[kind]["emulate"]:
            return self.emulate_start(path, min_ms=min_ms, source=source)
        job = self.submit("send", path=path, signal=signal, min_ms=min_ms, source=source)
        self._emit("job", {"id": job.id, "key": key, "path": path, "phase": "queued"})
        return job

    def emulate_start(self, path: str, min_ms: int = 0, source: str = ""):
        """Emulate a saved RFID key, NFC card or iButton key until it's tapped again (hold_stop) or time's up."""
        self._stop_emulations(keep=path)
        with self._lock:
            if path in self._holds:
                return None   # already going
            ev = threading.Event()
            self._holds[path] = ev
            self._emulating.add(path)
        try:
            job = self.submit("send", path=path, hold_event=ev, min_ms=min_ms, source=source)
        except BusyError:
            with self._lock:
                self._holds.pop(path, None)
                self._emulating.discard(path)
            raise
        self._emit("job", {"id": job.id, "key": path, "path": path, "phase": "queued", "emulate": True})
        return job

    def hold_start(self, path: str, source: str = "", signal: str = None):
        kind = file_kind(path)
        if kind and KINDS[kind]["emulate"]:
            return self.emulate_start(path, source=source)
        key = key_of(path, signal)
        self._stop_emulations(keep=key)
        with self._lock:
            if key in self._holds:
                return None
            ev = threading.Event()
            self._holds[key] = ev
        try:
            job = self.submit("send", path=path, signal=signal, hold_event=ev, source=source)
        except BusyError:
            with self._lock:
                self._holds.pop(key, None)
            raise
        self._emit("job", {"id": job.id, "key": key, "path": path, "phase": "queued", "hold": True})
        return job

    def hold_stop(self, key: str = None):
        """Let go of a held button or stop an emulation (one, by key, or all of them)."""
        with self._lock:
            if key is None:
                events = list(self._holds.values())
            else:
                events = [self._holds[key]] if key in self._holds else []
        for ev in events:
            ev.set()

    def set_favorite(self, path: str, on: bool, label: str = None, source: str = "", signal: str = None):
        key = key_of(path, signal)
        log.info("%s %s%s", "Pinned" if on else "Unpinned", key, f" ({source})" if source else "")

        def change(config):
            favs = [f for f in config.get("favorites") or []
                    if isinstance(f, dict) and key_of(f.get("path"), f.get("signal")) != key]
            if on:
                name = os.path.basename(path)[:-len(KINDS[file_kind(path)]["ext"])]   # the window tidies it up
                item = {"path": path, "label": (label or (f"{name} {signal}" if signal else name))[:120]}
                if signal:
                    item["signal"] = signal
                favs.append(item)
            config["favorites"] = favs
        with self._lock:
            self.config = update_config(change)
            favorites = self._favorites_view()
        self._emit("favorites", favorites)
        return favorites


# =====================================================================================
#  Local web UI
# =====================================================================================


class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._queues = []

    def subscribe(self, window: str = "?"):
        q = queue.Queue(maxsize=500)
        q.window = window
        with self._lock:
            self._queues.append(q)
        return q

    def windows(self) -> list:
        with self._lock:
            return [q.window for q in self._queues]

    def send_others(self, keep, event: str, data) -> int:
        """Send to every window except one; returns how many got it."""
        with self._lock:
            queues = [q for q in self._queues if q is not keep]
        for q in queues:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass
        return len(queues)

    def unsubscribe(self, q):
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    def broadcast(self, event: str, data):
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass

    def close_all(self):
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, controller: Controller, port: int, token: str):
        super().__init__(("127.0.0.1", port), UIHandler)
        self.controller = controller
        self.token = token
        self.port = self.server_address[1]
        self.hub = EventHub()
        controller.add_listener(self.hub.broadcast)
        self.clients = 0
        self.had_client = False
        self.last_client_left = time.monotonic()
        self._clients_lock = threading.Lock()
        self.quit_requested = threading.Event()
        self.quitting = False   # closing down: new launches wait for it to go instead of using it
        self.newest_window = None   # the window opened last: once it's up, any other window closes
        with open(os.path.join(HERE, "ui.html"), "r", encoding="utf-8") as f:
            self.page_template = f.read().replace("{{TOKEN}}", token).replace("{{VERSION}}", VERSION)
        icon_path = os.path.join(HERE, "icon.svg")
        self.icon = open(icon_path, "rb").read() if os.path.exists(icon_path) else b""

    def client_changed(self, delta: int):
        with self._clients_lock:
            self.clients += delta
            if delta > 0:
                self.had_client = True
            elif self.clients == 0:
                self.last_client_left = time.monotonic()
                self.controller.hold_stop()  # never leave a transmission running without a UI
                if self.controller.pairing_active():
                    log.info("Pairing: cancelled because the window closed")
                self.controller.pair_cancel()


class UIHandler(BaseHTTPRequestHandler):
    server_version = f"FlipperRemote/{VERSION}"

    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    # -- helpers ------------------------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"):
            return True
        self.send_error(403)
        return False

    def _token_ok(self, query) -> bool:
        token = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        if secrets.compare_digest(token, self.server.token):
            return True
        self._json({"ok": False, "error": "Not authorised. Reload the page."}, 403)
        return False

    def _json(self, value, code: int = 200):
        body = json.dumps(value).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise FlipperError("Request too large.")
        try:
            value = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise FlipperError("Bad request.") from None
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _saved(body):
        """The file (and, for an Infrared remote, the button) a request is about."""
        path = str(body.get("path") or "")
        if ".." in path.split("/") or not file_kind(path):
            raise FlipperError("That isn't a saved Sub-GHz, Infrared, RFID, NFC or iButton file.")
        signal = body.get("signal")
        if file_kind(path) == "infrared":
            if not isinstance(signal, str) or not signal.strip() or len(signal) > 100:
                raise FlipperError("Which button of the remote?")
            return path, signal
        return path, None

    # -- routes ----------------------------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            # Saved settings and pins go straight into the page, so it opens complete, in the right view and size.
            c = self.server.controller
            settings = json.dumps(c.settings()).replace("</", "<\\/")
            with c._lock:
                pins = json.dumps(c._favorites_view()).replace("</", "<\\/")
            values = {"SETTINGS": settings, "PINS": pins}
            page = re.sub(r"\{\{(SETTINGS|PINS)\}\}", lambda m: values[m.group(1)], self.server.page_template)
            page = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if url.path == "/icon.svg" and self.server.icon:
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(self.server.icon)))
            self.end_headers()
            self.wfile.write(self.server.icon)
            return
        if url.path == "/api/hello":
            self._json({"app": APP_ID, "version": VERSION, "quitting": self.server.quitting})
            return
        if not url.path.startswith("/api/"):
            self.send_error(404)
            return
        if not self._token_ok(query):
            return
        c = self.server.controller
        try:
            if url.path == "/api/events":
                self._events()
            elif url.path == "/api/state":
                self._json(c.snapshot())
            elif url.path == "/api/list":
                path = (query.get("path") or [SUBGHZ_ROOT])[0]
                refresh = (query.get("refresh") or ["0"])[0] == "1"
                self._json({"ok": True, "listing": c.listing(path, refresh)})
            else:
                self.send_error(404)
        except FlipperError as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def do_POST(self):
        if not self._host_ok():
            return
        url = urllib.parse.urlparse(self.path)
        if not self._token_ok(urllib.parse.parse_qs(url.query)):
            return
        c = self.server.controller
        try:
            body = self._body()
            if url.path == "/api/send":
                try:
                    min_ms = min(int(body.get("min_ms") or 0), int(MAX_HOLD_S * 1000))
                except (TypeError, ValueError):
                    min_ms = 0
                path, signal = self._saved(body)
                if file_kind(path) and KINDS[file_kind(path)]["emulate"]:
                    try:
                        min_ms = min(int(body.get("min_ms") or 0), MAX_EMULATE_S * 1000)
                    except (TypeError, ValueError):
                        min_ms = 0
                job = c.send(path, min_ms=min_ms, source=self._source(body), signal=signal)
                if job is None:   # already emulating it
                    self._json({"ok": True, "job": None})
                    return
                if body.get("wait"):
                    job.wait(max(MAX_HOLD_S + MAX_RAW_MS / 1000, MAX_EMULATE_S) + 120)
                self._json({"ok": True, "job": job.id})
            elif url.path == "/api/hold":
                path, signal = self._saved(body)
                if body.get("action") == "start":
                    job = c.hold_start(path, source=self._source(body), signal=signal)
                    self._json({"ok": True, "job": job.id if job else None})
                else:
                    c.hold_stop(key_of(path, signal))
                    self._json({"ok": True})
            elif url.path == "/api/stop":
                path, signal = self._saved(body)
                if c._holds.get(key_of(path, signal)) is not None:
                    log.info("Stopping %s (%s)", key_of(path, signal), self._source(body))
                c.hold_stop(key_of(path, signal))
                self._json({"ok": True})
            elif url.path == "/api/favorite":
                path, signal = self._saved(body)
                self._json({"ok": True, "favorites": c.set_favorite(path, bool(body.get("on")), body.get("label"),
                                                                    self._source(body), signal=signal)})
            elif url.path == "/api/replace-windows":
                # The app was opened again. Its new window takes over; the open ones close once it's up.
                ticket = re.sub(r"[^a-z0-9]", "", str(body.get("window") or "").lower())[:8]
                if ticket:
                    self.server.newest_window = ticket
                log.info("Opening window %s; the %d open now will close once it's up", ticket or "?",
                         self.server.clients)
                if c.pairing_active():
                    log.info("Pairing: cancelled because a new window is opening")
                    c.pair_cancel()
                self._json({"ok": True})
            elif url.path == "/api/settings":
                self._json({"ok": True, "settings": c.update_settings(body, self._source(body))})
            elif url.path == "/api/pair":
                action = body.get("action")
                if action == "start":
                    log.info("Pairing: “Pair a Flipper” opened (%s)", self._source(body))
                    c.pair_start()
                elif action == "choose":
                    log.info("Pairing: a Flipper was picked in the list (%s)", self._source(body))
                    c.pair_choose(str(body.get("address") or ""))
                elif action == "confirm":
                    remove = bool(body.get("remove"))
                    log.info("Pairing: %s (%s)", "chose to remove the old pairing and pair again" if remove
                             else "chose to keep the old pairing", self._source(body))
                    c.pair_confirm(remove)
                elif action == "code":
                    c.pair_code(str(body.get("code") or ""))
                elif action == "cancel":
                    if c.pairing_active():
                        log.info("Pairing: cancelled in the window (%s)", self._source(body))
                    c.pair_cancel()
                else:
                    raise FlipperError("Unknown pairing action.")
                self._json({"ok": True})
            elif url.path == "/api/connect":
                c.submit("connect")
                self._json({"ok": True})
            elif url.path == "/api/disconnect":
                c.submit("disconnect")
                self._json({"ok": True})
            elif url.path == "/api/quit":
                self._json({"ok": True})
                self.server.quit_requested.set()
            else:
                self.send_error(404)
        except FlipperError as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def _source(self, body) -> str:
        """How the page says an action was set off (a tap, a click, a key, the controller), and by which window."""
        via = re.sub(r"[^A-Za-z0-9:@,._ -]", "", str(body.get("via") or "?"))[:32] or "?"
        return f"{via}, window {self._window_id()}"

    def _one_window(self, q, window: str):
        """Only the window opened last stays open: once it's up, any other window is asked to close."""
        newest = self.server.newest_window
        if not newest:
            return
        if window == newest:
            closing = self.server.hub.send_others(q, "replaced", {})
            if closing:
                log.info("Window %s is up; asked %d other window(s) to close", window, closing)
        elif newest in self.server.hub.windows():
            try:
                q.put_nowait(("replaced", {}))
            except queue.Full:
                pass
            log.info("Window %s isn't the newest one (%s); asked it to close", window, newest)

    def _window_id(self) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        raw = self.headers.get("X-Window") or (query.get("w") or ["?"])[0]
        return re.sub(r"[^a-z0-9]", "", raw.lower())[:8] or "?"

    def _events(self):
        window = self._window_id()
        log.info("Window %s connected", window)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = self.server.hub.subscribe(window)
        self.server.client_changed(+1)
        try:
            self._sse("hello", self.server.controller.snapshot())
            self._one_window(q, window)
            last_write = time.monotonic()
            while True:
                try:
                    item = q.get(timeout=1.0)
                except queue.Empty:
                    # Notice a closed window right away: the socket turns readable with EOF.
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        break
                    if time.monotonic() - last_write > 15:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        last_write = time.monotonic()
                    continue
                if item is None:
                    break
                self._sse(*item)
                last_write = time.monotonic()
        except OSError:
            pass
        finally:
            self.server.hub.unsubscribe(q)
            self.server.client_changed(-1)
            log.info("Window %s closed", window)

    def _sse(self, event: str, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()


# =====================================================================================
#  Opening the UI window
# =====================================================================================

_CHROMIUM_FLATPAKS = ("com.google.Chrome", "org.chromium.Chromium", "com.brave.Browser", "com.microsoft.Edge",
                      "com.vivaldi.Vivaldi", "io.github.ungoogled_software.ungoogled_chromium", "com.opera.Opera")
_FIREFOX_FLATPAKS = ("org.mozilla.firefox", "io.gitlab.librewolf-community", "one.ablaze.floorp")
_CHROMIUM_BINS = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "brave", "brave-browser",
                  "microsoft-edge", "vivaldi")
_FIREFOX_BINS = ("firefox", "librewolf")


def _flatpak_installed(app_id: str) -> bool:
    for root in (os.path.expanduser("~/.local/share/flatpak/app"), "/var/lib/flatpak/app"):
        if os.path.isdir(os.path.join(root, app_id, "current")):
            return True
    return False


def in_game_mode() -> bool:
    return os.environ.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope" or \
        os.environ.get("XDG_SESSION_DESKTOP", "").lower() == "gamescope" or \
        bool(os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")) or os.environ.get("SteamGamepadUI") == "1"


def tell_user(message: str, error: bool = False):
    """Print, and when started from the app menu (no terminal) also show a dialog."""
    print(message, file=sys.stderr if error else sys.stdout)
    if sys.stderr.isatty() or os.environ.get("FLIPPER_REMOTE_NO_DIALOGS"):
        return
    for cmd in (["kdialog", "--title", APP_NAME, "--error" if error else "--msgbox", message],
                ["zenity", "--error" if error else "--info", "--title", APP_NAME, "--text", message],
                ["notify-send", APP_NAME, message]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue


def _strip_desktop(desktop_id: str) -> str:
    desktop_id = desktop_id.strip()
    return desktop_id[:-8] if desktop_id.endswith(".desktop") else desktop_id


def default_browser_id() -> str:
    """The desktop's default web browser, e.g. 'com.brave.Browser' (from mimeapps.list or xdg-settings)."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    for path in (os.path.join(config_home, "mimeapps.list"),
                 os.path.expanduser("~/.local/share/applications/mimeapps.list")):
        defaults, section = {}, ""
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("["):
                        section = line
                    elif section == "[Default Applications]" and "=" in line:
                        key, _, value = line.partition("=")
                        defaults[key.strip()] = value.split(";")[0]
        except OSError:
            continue
        for key in ("x-scheme-handler/https", "x-scheme-handler/http", "text/html"):
            if defaults.get(key, "").strip():
                return _strip_desktop(defaults[key])
    if shutil.which("xdg-settings"):
        try:
            out = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True, text=True,
                                 timeout=3).stdout
            return _strip_desktop(out)
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def browser_candidates():
    """Browsers we know how to open as a window: (id, kind, command prefix). The default browser comes first."""
    found = []
    has_flatpak = shutil.which("flatpak") is not None
    for kind, flatpaks, binaries in (("chromium", _CHROMIUM_FLATPAKS, _CHROMIUM_BINS),
                                     ("firefox", _FIREFOX_FLATPAKS, _FIREFOX_BINS)):
        if has_flatpak:
            found += [(app_id, kind, ["flatpak", "run", app_id]) for app_id in flatpaks if _flatpak_installed(app_id)]
        for exe in binaries:
            path = shutil.which(exe)
            if path:
                found.append((exe, kind, [path]))
    preferred = (str(_load_json(CONFIG_FILE, {}).get("browser") or "") or default_browser_id()).lower()
    if preferred:
        found.sort(key=lambda item: 0 if (item[0].lower() == preferred or item[0].lower() in preferred
                                          or preferred in item[0].lower()) else 1)
    return found


NORMAL_WINDOW = (1100, 720)
WINDOW_FLAGS = ["--no-first-run", "--no-default-browser-check", "--password-store=basic",
                "--disable-background-networking", "--disable-component-update", "--hide-crash-restore-bubble"]


def _size_pair(value, low: int, high: int):
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, int) and not isinstance(v, bool)
                                                                    and low <= v <= high for v in value):
        return int(value[0]), int(value[1])
    return None


def initial_window_size(config=None):
    """The size to open the window at. In "Pinned only" it's snug around the pinned signals: exactly what the
    page measured last time if the pins haven't changed, otherwise worked out from the same layout numbers as
    ui.html. The window's frame (title bar) is whatever the page measured on the last launch."""
    config = _load_json(CONFIG_FILE, {}) if config is None else config
    ui = config.get("ui") if isinstance(config.get("ui"), dict) else {}
    if not ui.get("pinned_only"):
        return NORMAL_WINDOW
    win = ui.get("window") if isinstance(ui.get("window"), dict) else {}
    frame = _size_pair(win.get("frame"), 0, 200) or (0, 40)
    show = ui.get("show") if ui.get("show") in SHOW_CHOICES else "all"
    pins = [key_of(str(f.get("path")), f.get("signal")) for f in config.get("favorites") or []
            if isinstance(f, dict) and file_kind(str(f.get("path"))) and
            (show == "all" or file_kind(str(f.get("path"))) == show)]
    fit = _size_pair(win.get("fit"), 200, 4000)
    if fit and win.get("pins") == "\n".join(pins):
        return fit[0] + frame[0], fit[1] + frame[1]
    cols = 3 if len(pins) > 2 else 2
    rows = -(-len(pins) // cols)
    width = cols * 341 + (cols - 1) * 18 + 40
    inside = (225 if cols == 2 else 232) + rows * 119 if rows else 290   # header, 119 px per row of keys, footer
    return width + frame[0], min(inside + frame[1], NORMAL_WINDOW[1])


def window_profile_dir(base) -> str:
    """The window gets a small browser profile of its own. Then the browser always starts it as a separate copy,
    which opens it at the size we ask for (an already-open browser ignores that) and under our own name."""
    if base[:2] == ["flatpak", "run"]:   # a Flatpak browser can only write inside its own ~/.var/app folder
        return os.path.expanduser(f"~/.var/app/{base[-1]}/cache/flipper-remote")
    return os.path.join(CACHE_DIR, "browser")


def _prepare_window_profile(path: str):
    try:
        os.makedirs(path, exist_ok=True)
        sentinel = os.path.join(path, "First Run")   # tells the browser its first-run welcome is done
        if not os.path.exists(sentinel):
            open(sentinel, "a").close()
    except OSError as e:
        log.info("Couldn't prepare the window's browser profile %s: %s", path, e)


def browser_command(url: str, fullscreen: bool, window: str = "", controller: bool = False):
    # "w=...": the window's name. The app keeps only the window opened last, and its log says which window did what.
    # "pad=1": the window reacts to controller buttons itself. Only in Game Mode (or --controller): in Desktop Mode
    # Steam's desktop layout already turns the controller into keys and mouse, and the browser still sees the
    # controller too, so the window acting on it as well made buttons mapped to something else press things here.
    params = ([f"w={window}"] if window else []) + (["pad=1"] if controller else [])
    named = url + ("?" + "&".join(params) if params else "")
    custom = os.environ.get("FLIPPER_REMOTE_BROWSER")
    if custom:
        return shlex.split(custom) + [named]
    candidates = browser_candidates()
    if candidates:
        _id, kind, base = candidates[0]
        if kind == "chromium":  # Brave, Chrome, Chromium, Edge, Vivaldi...: a clean app window
            common = base + [f"--user-data-dir={window_profile_dir(base)}", "--class=flipper-remote"] + WINDOW_FLAGS
            if fullscreen:
                return common + [f"--app={named}", "--start-fullscreen"]
            # "fit=WxH": the size it opened at; the page may size its own window to what it shows
            width, height = initial_window_size()
            page = f"{url}?" + "&".join([f"fit={width}x{height}"] + params)
            return common + [f"--app={page}", f"--window-size={width},{height}"]
        return base + (["--kiosk", named] if fullscreen else ["--new-window", named])
    opener = shutil.which("xdg-open")
    return [opener, named] if opener else None


def open_window(url: str, fullscreen: bool, window: str = "", controller: bool = False):
    cmd = browser_command(url, fullscreen, window, controller)
    if not cmd:
        tell_user(f"No web browser was found. Open {url} in a browser, or install Brave, Chrome or "
                  "Firefox from Discover.")
        return None
    log.info("Opening UI: %s", " ".join(shlex.quote(c) for c in cmd))
    for arg in cmd:
        if arg.startswith("--user-data-dir="):
            _prepare_window_profile(arg.split("=", 1)[1])
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as e:
        tell_user(f"Couldn't start a browser ({e}). Open {url} in a browser yourself.")
        return None


# =====================================================================================
#  Talking to an already-running instance (so the CLI and UI don't fight over the Flipper)
# =====================================================================================


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a process that has exited but hasn't been reaped yet ("zombie") still answers kill(0)
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 127.0.0.1, never via a proxy


def running_instance():
    """The running copy's instance.json, plus its version and whether it's closing; None if there's none
    that answers."""
    info = _load_json(INSTANCE_FILE, {})
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0 or pid == os.getpid() or not _pid_alive(pid):
        return None
    try:
        with _local.open(f"http://127.0.0.1:{info['port']}/api/hello", timeout=2) as r:
            hello = json.load(r)
            if hello.get("app") == APP_ID:
                return dict(info, version=hello.get("version"), quitting=bool(hello.get("quitting")))
    except (OSError, ValueError, KeyError):
        pass
    return None


def instance_request(info: dict, path: str, body=None, timeout: float = 30.0):
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Token": info["token"], "Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with _local.open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}


# =====================================================================================
#  Commands
# =====================================================================================


_held_locks = []


def _release_lock(fd):
    if fd in _held_locks:
        _held_locks.remove(fd)
        try:
            os.close(fd)
        except OSError:
            pass


def _single_instance_lock(role: str = "ui"):
    """Hold a lock for the life of the process, so two launches (or a launch and a command-line tool)
    can't both drive the Flipper. The lock file says which process holds it, and as what."""
    import fcntl
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd = os.open(os.path.join(CACHE_DIR, "instance.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()} {role}".encode(), 0)
    _held_locks.append(fd)
    return fd


def _lock_holder():
    """(pid, role) of the process holding the instance lock, as it wrote them."""
    try:
        with open(os.path.join(CACHE_DIR, "instance.lock"), "r") as f:
            parts = f.read().split()
        return int(parts[0]), (parts[1] if len(parts) > 1 else "ui")
    except (OSError, ValueError, IndexError):
        return 0, ""


def _is_our_process(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"flipper_remote" in f.read()
    except OSError:
        return False


def _claim_instance(wait: float = 30.0):
    """Become the one running copy (returns (lock, None)) or find the copy already running (returns
    (None, its info)). Never runs a second copy alongside one that's there but not answering: two copies
    fight over the Flipper and save their own pins over each other's. A copy that's closing is waited for;
    an older version is asked to close; one that's stuck for `wait` seconds is ended."""
    deadline = time.monotonic() + wait
    give_up = time.monotonic() + 300
    asked = {}   # pid -> when we asked it to close
    while True:
        lock = _single_instance_lock("ui")
        if lock is not None:
            return lock, None
        existing = running_instance()
        if existing and not existing["quitting"]:
            if existing.get("version") == VERSION:
                return None, existing
            pid = int(existing["pid"])
            if pid not in asked:   # e.g. right after installing an update
                log.info("Replacing running version %s (pid %d) with %s", existing.get("version"), pid, VERSION)
                asked[pid] = time.monotonic()
                try:
                    os.kill(pid, signal.SIGTERM)   # it disconnects from the Flipper on the way out
                except OSError:
                    pass
        holder, role = _lock_holder()
        now = time.monotonic()
        if role == "cli" and _pid_alive(holder) and now < give_up:
            deadline = max(deadline, now + 5)   # a command-line send or list is using the Flipper: wait
        stuck = now > deadline or (holder in asked and now - asked[holder] > 20)
        if stuck and role != "cli" and holder and holder != os.getpid() and _is_our_process(holder):
            log.warning("Flipper Remote (pid %d) isn't responding; ending it", holder)
            try:
                os.kill(holder, signal.SIGKILL)
            except OSError:
                pass
            asked.pop(holder, None)
            deadline = now + 5
        elif now > deadline + 5:
            return None, None
        time.sleep(0.2)


def cmd_ui(args) -> int:
    lock, existing = _claim_instance()
    if lock is None and not existing:
        tell_user("Flipper Remote is already running but not responding, and couldn't be closed. "
                  "Restart the Steam Deck if this keeps happening.", error=True)
        return 1
    if existing:
        url = f"http://127.0.0.1:{existing['port']}/"
        print(f"{APP_NAME} is already running at {url}")
        if not args.no_browser:
            # One window at a time: the new window takes over, and the open one closes once the new one is up.
            window = secrets.token_hex(3)
            try:
                instance_request(existing, "/api/replace-windows", {"window": window}, timeout=5)
            except (OSError, ValueError) as e:
                log.info("Couldn't tell the app a new window is opening: %s", e)
            open_window(url, args.fullscreen or in_game_mode(), window, args.controller or in_game_mode())
        return 0
    if not os.path.exists(os.path.join(HERE, "ui.html")):
        tell_user(f"ui.html is missing next to {__file__}. Re-install Flipper Remote.", error=True)
        return 1

    controller = Controller(device_hint=args.device, stay_connected=args.stay_connected)
    pins = [os.path.basename(str(f.get("path"))) for f in controller.config.get("favorites") or [] if isinstance(f, dict)]
    log.info("Running (pid %d); %d pinned in %s: %s", os.getpid(), len(pins), CONFIG_FILE, ", ".join(pins) or "none")
    token = secrets.token_urlsafe(24)
    server = None
    for port in range(args.port, args.port + 10):
        try:
            server = UIServer(controller, port, token)
            break
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
    if server is None:
        tell_user(f"Couldn't start: no free local port between {args.port} and {args.port + 9}.", error=True)
        return 1
    url = f"http://127.0.0.1:{server.port}/"
    _save_json(INSTANCE_FILE, {"pid": os.getpid(), "port": server.port, "token": token})
    controller.start()
    controller.submit("connect")
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    print(f"{APP_NAME} {VERSION} running at {url}  (log: {LOG_FILE})")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass

    fullscreen = args.fullscreen or in_game_mode()
    browser = None
    if not args.no_browser:
        server.newest_window = secrets.token_hex(3)
        browser = open_window(url, fullscreen, server.newest_window, args.controller or in_game_mode())
    started = time.monotonic()
    try:
        while not stop.wait(0.5):
            if server.quit_requested.is_set():
                break
            if args.no_browser:
                continue
            now = time.monotonic()
            if fullscreen and browser is not None and browser.poll() is not None and server.clients == 0 \
                    and now - started > 5:
                log.info("Browser closed; exiting")
                break
            if server.had_client and server.clients == 0 and now - server.last_client_left > 12:
                log.info("UI window closed; exiting")
                break
            if not server.had_client and now - started > 180:
                log.info("Nobody opened the UI; exiting")
                break
    finally:
        print("Shutting down…")
        server.quitting = True   # from now on, new launches wait for this copy to go
        try:
            if _load_json(INSTANCE_FILE, {}).get("pid") == os.getpid():
                os.remove(INSTANCE_FILE)
        except OSError:
            pass
        server.hub.close_all()
        controller.stop(disconnect=True)
        server.shutdown()
    return 0


def _direct_controller(args) -> Controller:
    c = Controller(device_hint=getattr(args, "device", None), stay_connected=getattr(args, "stay_connected", False))
    c.add_listener(lambda ev, data: ev == "status" and data.get("state") == "connecting" and
                   print(f"  {data.get('message')}", file=sys.stderr))
    c.start()
    return c


def _cli_claim(wait: float = 30.0):
    """For the command-line tools: (the running window's copy to ask, None), or (None, the lock) to use the
    Flipper directly, or (None, None) if it stays busy. A copy that's closing still holds the Flipper, so
    wait for it to go first."""
    deadline = time.monotonic() + wait
    while True:
        lock = _single_instance_lock("cli")
        if lock is not None:
            return None, lock
        inst = running_instance()
        if inst and not inst["quitting"]:
            return inst, None
        if time.monotonic() > deadline:
            return None, None
        time.sleep(0.2)


BUSY = "Flipper Remote is busy with the Flipper (closing, or another command). Try again in a moment."


def cmd_send(args) -> int:
    path = resolve_saved_path(args.name)
    kind = file_kind(path)
    if not kind:
        print(f"Error: {path} isn't a saved Sub-GHz, Infrared, RFID, NFC or iButton file.", file=sys.stderr)
        return 1
    signal = args.button
    if kind == "infrared" and not signal:
        print("Error: say which button of the remote to send, with --button (for example --button Power).",
              file=sys.stderr)
        return 1
    emulate = KINDS[kind]["emulate"]
    hold_ms = int(min(args.hold or 0, MAX_EMULATE_S if emulate else MAX_HOLD_S) * 1000)
    done = (f"Emulated {path}" if emulate else f"Sent {path}" + (f": {signal}" if signal else ""))
    inst, lock = _cli_claim()
    if not inst and lock is None:
        print(BUSY, file=sys.stderr)
        return 1
    if inst:
        body = dict({"path": path, "wait": True, "min_ms": hold_ms}, **({"signal": signal} if signal else {}))
        res = instance_request(inst, "/api/send", body, timeout=MAX_EMULATE_S + 300)
        if not res.get("ok"):
            print(f"Error: {res.get('error')}", file=sys.stderr)
            return 1
        print(f"{done} (via the running {APP_NAME} window)")
        return 0
    c = _direct_controller(args)
    try:
        job = c.submit("send", path=path, signal=signal, min_ms=hold_ms)
        job.wait(MAX_EMULATE_S + MAX_RAW_MS / 1000 + 180)
        print(done)
        return 0
    except FlipperError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        c.stop(disconnect=True)


def cmd_list(args) -> int:
    path = resolve_saved_path(args.path or SUBGHZ_ROOT, folder=True)
    inst, lock = _cli_claim()
    if not inst and lock is None:
        print(BUSY, file=sys.stderr)
        return 1
    if inst:
        print("The UI is running; showing its cached view.", file=sys.stderr)
        res = instance_request(inst, f"/api/list?path={urllib.parse.quote(path)}")
        if not res.get("ok"):
            print(f"Error: {res.get('error')}", file=sys.stderr)
            return 1
        listing = res.get("listing") or {}
    else:
        c = _direct_controller(args)
        try:
            c.listing(path)   # checks the path
            listing = c._decorate(c.submit("list", path=path).wait(120))
        except FlipperError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        finally:
            c.stop(disconnect=True)
    for d in listing.get("dirs") or []:
        print(f"{path}/{d}/")
    for f in listing.get("files") or []:
        print(f"{path}/{f['name']}")
    for sig in listing.get("signals") or []:
        print(f"{path}: {sig['name']}")
    return 0


def cmd_pair(args) -> int:
    inst, lock = _cli_claim()
    if not inst and lock is None:
        print(BUSY, file=sys.stderr)
        return 1
    if inst:
        print("The Flipper Remote window is open: use “Pair a Flipper” at the bottom of it, or close the window "
              "and run this again.", file=sys.stderr)
        return 1
    print("Pairing a Flipper with this computer.\n"
          "  On the Flipper: Settings → Bluetooth → ON, then go back to its main screen.\n"
          "  Turn off your phone's Bluetooth while you pair.\n")
    c = Controller(device_hint=args.device)
    c.start()
    try:
        c.submit("pair", ui=ConsolePairing()).wait(600)
        if c.state.get("state") == "connected":
            print("  Connected and working. Open Flipper Remote from the app menu.")
        else:
            print(f"  Paired, but it didn't connect afterwards: {c.state.get('message') or 'unknown reason'}")
        return 0
    except PairingCancelled:
        return 1
    except FlipperError as e:
        print(f"  {e}", file=sys.stderr)
        return 1
    finally:
        c.stop(disconnect=True)


def cmd_devices(_args) -> int:
    try:
        bus = DBusConnection.system()
    except DBusError as e:
        print(f"Can't reach the system D-Bus: {e.message}", file=sys.stderr)
        return 1
    try:
        devices = find_flippers(bus)
    except FlipperError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        bus.close()
    if not devices:
        print("No Flipper-like Bluetooth devices are known to this computer.")
        return 1
    for d in devices:
        flags = ", ".join(x for x, on in (("paired", d["paired"]), ("connected", d["connected"])) if on) or "not paired"
        print(f"{d['address']}  {d['name']}  ({flags})")
    return 0


def cmd_disconnect(args) -> int:
    inst, lock = _cli_claim()
    if not inst and lock is None:
        print(BUSY, file=sys.stderr)
        return 1
    if inst:
        instance_request(inst, "/api/disconnect", {})
        print("Asked the running UI to disconnect.")
        return 0
    try:
        bus = DBusConnection.system()
        for d in find_flippers(bus):
            if d["connected"]:
                bus.call(BLUEZ, d["path"], IFACE_DEVICE, "Disconnect", timeout=10)
                print(f"Disconnected {d['name']}")
        bus.close()
    except (DBusError, FlipperError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def good(msg):
        print(f"  ✓ {msg}")

    def bad(msg):
        nonlocal ok
        ok = False
        print(f"  ✗ {msg}")

    print(f"{APP_NAME} {VERSION} — checking your setup\n")
    good(f"Python {sys.version.split()[0]}")
    inst, lock = _cli_claim()
    if not inst and lock is None:
        bad(BUSY)
        return 1
    if inst:
        bad("The Flipper Remote window is open. Close it first (it's using the Flipper).")
        return 1
    try:
        bus = DBusConnection.system()
        good("Connected to the system D-Bus")
    except DBusError as e:
        bad(f"Can't reach the system D-Bus: {e.message}")
        return 1
    session = None
    try:
        try:
            devices = find_flippers(bus)
        except FlipperError as e:
            bad(str(e))
            return 1
        good("Bluetooth service (BlueZ) is running")
        if not any(d["paired"] for d in devices):
            bad("No Flipper is paired with this computer yet.")
            print()
            print(PAIRING_STEPS)
            return 1
        for d in devices:
            print(f"    found {d['name']} [{d['address']}] paired={d['paired']} connected={d['connected']}")
        c = Controller(device_hint=args.device)
        c._bus = bus
        device = c._pick_device()
        if not device["adapter_powered"]:
            bad("Bluetooth is turned off.")
            return 1
        print(f"  … connecting to {device['name']} (up to ~30 s)")
        session = FlipperSession(bus, device)
        session.open(status=lambda m: print(f"    {m}"))
        good(f"Connected; Bluetooth MTU {session.ble.mtu}")
        good(f"Flipper answers (ping {session.ping() * 1000:.0f} ms)")
        try:
            info = session.device_info()
            fw = info.get("firmware_version") or "?"
            fork = info.get("firmware_origin_fork") or ""
            api = f"{info.get('protobuf_version_major', '?')}.{info.get('protobuf_version_minor', '?')}"
            good(f"Firmware {fw}{' (' + fork + ')' if fork else ''}, RPC API {api}, "
                 f"name {info.get('hardware_name', '?')}")
        except FlipperError as e:
            bad(f"Couldn't read device info: {e}")
        entries = session.list_dir(SUBGHZ_ROOT)
        files = [e for e in entries if not e["is_dir"] and e["name"].lower().endswith(".sub")]
        dirs = [e for e in entries if e["is_dir"] and e["name"].lower() != "assets" and not e["name"].startswith(".")]
        good(f"{SUBGHZ_ROOT}: {len(files)} .sub files, {len(dirs)} folders")
    except FlipperError as e:
        bad(str(e))
    finally:
        if session is not None:
            session.close(disconnect=True)
            print("  ✓ Disconnected")
        bus.close()
    print("\nAll good!" if ok else f"\nSomething needs attention. Details are in {LOG_FILE}")
    return 0 if ok else 1


def setup_logging(debug: bool, console: bool):
    os.makedirs(CACHE_DIR, exist_ok=True)
    root = logging.getLogger(APP_ID)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(process)d] %(threadName)s: %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        ch.setLevel(logging.DEBUG if debug else logging.WARNING)
        root.addHandler(ch)


def main(argv=None) -> int:
    # Options are accepted before or after the sub-command ("SUPPRESS" keeps the two from clobbering each other).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--device", default=argparse.SUPPRESS,
                        help="Bluetooth address or name of the Flipper to use (default: the paired one)")
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                        help="verbose logging, including raw Bluetooth traffic")
    common.add_argument("--stay-connected", action="store_true", default=argparse.SUPPRESS,
                        help="don't disconnect the Flipper when done")
    ui_opts = argparse.ArgumentParser(add_help=False)
    ui_opts.add_argument("--port", type=int, default=argparse.SUPPRESS,
                         help=f"local port for the UI (default {DEFAULT_PORT})")
    ui_opts.add_argument("--no-browser", action="store_true", default=argparse.SUPPRESS,
                         help="don't open a window; just serve the UI")
    ui_opts.add_argument("--fullscreen", action="store_true", default=argparse.SUPPRESS,
                         help="open full screen (automatic in Game Mode)")
    ui_opts.add_argument("--controller", action="store_true", default=argparse.SUPPRESS,
                         help="let the window react to controller buttons directly (automatic in Game Mode; in "
                              "Desktop Mode Steam's desktop layout drives it with keys and mouse instead)")

    p = argparse.ArgumentParser(prog=APP_ID, parents=[common, ui_opts],
                                description="Send your Flipper Zero's saved Sub-GHz signals over Bluetooth. "
                                            "With no command, opens the remote window.")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("ui", parents=[common, ui_opts], help="open the remote window (default)")
    s = sub.add_parser("send", parents=[common],
                       help="send a saved signal or emulate a saved card, e.g.  send Lights/Fan_On  or  send nfc/Badge")
    s.add_argument("name", help="a file under /ext/subghz (the .sub extension is optional), or under infrared/, "
                                "lfrfid/, nfc/ or ibutton/, or a full /ext/... path")
    s.add_argument("--button", metavar="NAME", help="for an Infrared remote: which button to press")
    s.add_argument("--hold", type=float, metavar="SECONDS",
                   help="keep sending (or emulating) for this long; cards and keys emulate for "
                        f"{EMULATE_S} s by default")
    ls = sub.add_parser("list", parents=[common], help="list saved files (or an Infrared remote's buttons)")
    ls.add_argument("path", nargs="?", help="folder under /ext/subghz, or infrared/, lfrfid/, nfc/, ibutton/..., "
                                            "or a full /ext/... path")
    sub.add_parser("pair", parents=[common], help="pair a Flipper with this computer (or pair it again)")
    sub.add_parser("devices", parents=[common], help="show Flipper-like Bluetooth devices")
    sub.add_parser("disconnect", parents=[common], help="disconnect the Flipper")
    sub.add_parser("doctor", parents=[common], help="check Bluetooth, pairing and the Flipper connection")

    args = p.parse_args(argv)
    for name, default in (("device", None), ("debug", False), ("stay_connected", False), ("port", DEFAULT_PORT),
                          ("no_browser", False), ("fullscreen", False), ("controller", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    command = args.command or "ui"
    setup_logging(args.debug, console=command != "ui" or sys.stderr.isatty())
    log.info("%s %s starting (%s), Python %s", APP_NAME, VERSION, command, sys.version.split()[0])
    handlers = {"ui": cmd_ui, "send": cmd_send, "list": cmd_list, "devices": cmd_devices, "pair": cmd_pair,
                "disconnect": cmd_disconnect, "doctor": cmd_doctor}
    try:
        return handlers[command](args)
    except KeyboardInterrupt:
        return 130
    finally:
        for fd in list(_held_locks):   # done with the Flipper
            _release_lock(fd)


if __name__ == "__main__":
    sys.exit(main())
