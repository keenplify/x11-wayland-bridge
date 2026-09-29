#!/usr/bin/env python3
"""Experimental X11 proxy for root screenshots and XScreenSaver idle queries.

Run in the host desktop session with DISPLAY set to the real Xwayland display:
    python3 xgetimage_wayland_bridge.py --listen :99
Then run an X11 application with DISPLAY=:99. The proxy passes ordinary X11
traffic through unchanged, replacing root-window GetImage replies and answering
XScreenSaver QueryVersion/QueryInfo with KDE Wayland idle notifications.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

SCREENSAVER_OPCODE = 200


class IdleMonitor:
    """Approximate KDE Wayland idle time using compositor idle/resume notifications."""

    def __init__(self) -> None:
        from PyQt6 import sip
        from PyQt6.QtCore import QObject

        self.since: float | None = None
        library = ctypes.CDLL("libKF6IdleTime.so.6")
        instance = getattr(library, "_ZN9KIdleTime8instanceEv")
        instance.restype = ctypes.c_void_p
        pointer = instance()
        if not pointer:
            raise RuntimeError("KIdleTime could not start")
        self.object = sip.wrapinstance(pointer, QObject)
        self.object.timeoutReached.connect(self._idle)
        self.object.resumingFromIdle.connect(self._resumed)
        self.catch_resume = getattr(library, "_ZN9KIdleTime20catchNextResumeEventEv")
        self.catch_resume.argtypes = [ctypes.c_void_p]
        self.add_timeout = getattr(library, "_ZN9KIdleTime14addIdleTimeoutEi")
        self.add_timeout.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.add_timeout.restype = ctypes.c_int
        self.pointer = pointer
        if self.add_timeout(pointer, 1000) <= 0:
            raise RuntimeError("KDE Wayland idle notifications unavailable")

    def _idle(self, _identifier: int, milliseconds: int) -> None:
        self.since = time.monotonic() - milliseconds / 1000
        print(f"Wayland idle notification at {milliseconds} ms", file=sys.stderr)
        self.catch_resume(self.pointer)

    def _resumed(self) -> None:
        self.since = None
        print("Wayland input resumed", file=sys.stderr)

    def milliseconds(self) -> int:
        return 0 if self.since is None else min(0xFFFFFFFF, max(0, int((time.monotonic() - self.since) * 1000)))


def read_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        chunk = sock.recv(count)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def padded(length: int) -> int:
    return (length + 3) & ~3


def xauthority_cookie(display_number: str) -> bytes:
    auth_file = Path(os.environ.get("XAUTHORITY", str(Path.home() / ".Xauthority")))
    data = auth_file.read_bytes()
    offset = 0
    candidates = []
    while offset < len(data):
        if offset + 2 > len(data):
            break
        offset += 2  # address family
        fields = []
        for _ in range(4):
            if offset + 2 > len(data):
                raise ValueError("truncated Xauthority file")
            size = int.from_bytes(data[offset : offset + 2], "big")
            offset += 2
            fields.append(data[offset : offset + size])
            offset += size
        _address, number, name, cookie = fields
        if name == b"MIT-MAGIC-COOKIE-1" and number == display_number.encode():
            candidates.append(cookie)
    if not candidates:
        raise RuntimeError(f"no MIT-MAGIC-COOKIE-1 entry for display :{display_number}")
    return candidates[0]


def display_socket(display: str) -> Path:
    if not display.startswith(":") or not display[1:].split(".", 1)[0].isdigit():
        raise ValueError("only local X11 displays such as :0 are supported")
    return Path("/tmp/.X11-unix") / ("X" + display[1:].split(".", 1)[0])


@dataclass(frozen=True)
class Screen:
    root: int
    width: int
    height: int
    visual: int
    depth: int
    bpp: int
    image_byte_order: int
    red_mask: int
    green_mask: int
    blue_mask: int


@dataclass(frozen=True)
class GetImage:
    x: int
    y: int
    width: int
    height: int
    plane_mask: int


def parse_screen(setup: bytes, order: str) -> Screen:
    if len(setup) < 32:
        raise ValueError("short X11 setup")
    vendor_length = struct.unpack_from(order + "H", setup, 16)[0]
    format_count = setup[21]
    image_order = setup[22]
    offset = 32 + padded(vendor_length)
    formats = {}
    for index in range(format_count):
        depth, bpp, _scanline_pad = struct.unpack_from("BBB", setup, offset + index * 8)
        formats[depth] = bpp
    offset += format_count * 8
    if len(setup) < offset + 40:
        raise ValueError("no X11 screen in setup")
    root = struct.unpack_from(order + "I", setup, offset)[0]
    width, height = struct.unpack_from(order + "HH", setup, offset + 20)
    root_visual = struct.unpack_from(order + "I", setup, offset + 32)[0]
    depth = setup[offset + 38]
    depth_count = setup[offset + 39]
    offset += 40
    masks = None
    for _ in range(depth_count):
        _depth, _unused, visual_count = struct.unpack_from(order + "BBH", setup, offset)
        offset += 8
        for _ in range(visual_count):
            visual = struct.unpack_from(order + "I", setup, offset)[0]
            if visual == root_visual:
                masks = struct.unpack_from(order + "III", setup, offset + 8)
            offset += 24
    if masks is None:
        raise ValueError("root visual not found in X11 setup")
    return Screen(root, width, height, root_visual, depth, formats[depth], image_order, *masks)


def capture_desktop(screen: Screen) -> Image.Image:
    with tempfile.TemporaryDirectory(prefix="xgetimage-bridge-") as directory:
        filename = Path(directory) / "desktop.png"
        command = ["spectacle", "--background", "--nonotify", "--fullscreen", "--output", str(filename)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if result.returncode or not filename.is_file():
            raise RuntimeError(f"Spectacle failed ({result.returncode}): {result.stderr.strip()}")
        with Image.open(filename) as screenshot:
            image = screenshot.convert("RGB")
    if image.size != (screen.width, screen.height):
        image = image.resize((screen.width, screen.height), Image.Resampling.BILINEAR)
    return image


def image_reply(screen: Screen, request: GetImage, sequence: int, order: str) -> bytes:
    if screen.bpp != 32 or screen.image_byte_order != 0:
        raise RuntimeError("this proxy currently requires 32-bit little-endian X11 pixels")
    if (screen.red_mask, screen.green_mask, screen.blue_mask) != (0xFF0000, 0xFF00, 0xFF):
        raise RuntimeError("unsupported X11 visual color masks")
    image = capture_desktop(screen)
    region = Image.new("RGB", (request.width, request.height))
    region.paste(image, (-request.x, -request.y))
    pixels = region.tobytes("raw", "BGRX")
    header = bytearray(32)
    header[0] = 1
    header[1] = screen.depth
    struct.pack_into(order + "HII", header, 2, sequence, len(pixels) // 4, screen.visual)
    return bytes(header) + pixels


class Connection:
    def __init__(self, client: socket.socket, upstream: socket.socket, screen: Screen, order: str, idle: IdleMonitor):
        self.client = client
        self.upstream = upstream
        self.screen = screen
        self.order = order
        self.idle = idle
        self.pending: dict[int, GetImage | str] = {}
        self.pending_lock = threading.Lock()
        self.client_send_lock = threading.Lock()
        self.sequence = 0

    def requests(self) -> None:
        try:
            while True:
                header = read_exact(self.client, 4)
                units = struct.unpack_from(self.order + "H", header, 2)[0]
                if units == 0:  # BIG-REQUESTS extension
                    extra = read_exact(self.client, 4)
                    units = struct.unpack(self.order + "I", extra)[0]
                    request = header + extra + read_exact(self.client, units * 4 - 8)
                else:
                    request = header + read_exact(self.client, units * 4 - 4)
                self.sequence = (self.sequence + 1) & 0xFFFF
                synthetic = None
                if request[0] == 98 and len(request) >= 8:
                    name_len = struct.unpack_from(self.order + "H", request, 4)[0]
                    if request[8:8 + name_len] == b"MIT-SCREEN-SAVER":
                        synthetic = "extension"
                elif request[0] == SCREENSAVER_OPCODE and request[1] in (0, 1):
                    synthetic = "version" if request[1] == 0 else "info"
                if synthetic:
                    with self.pending_lock:
                        self.pending[self.sequence] = synthetic
                    # GetInputFocus yields one ordered reply with the same sequence.
                    self.upstream.sendall(bytes((43, 0, 1, 0)) if self.order == "<" else bytes((43, 0, 0, 1)))
                    continue
                if request[0] == 73 and request[1] == 2 and len(request) >= 20:  # GetImage, ZPixmap
                    drawable = struct.unpack_from(self.order + "I", request, 4)[0]
                    if drawable == self.screen.root:
                        x, y, width, height, mask = struct.unpack_from(self.order + "hhHHI", request, 8)
                        with self.pending_lock:
                            self.pending[self.sequence] = GetImage(x, y, width, height, mask)
                self.upstream.sendall(request)
        except (EOFError, ConnectionError, BrokenPipeError):
            pass

    def responses(self) -> None:
        while True:
            header = read_exact(self.upstream, 32)
            kind = header[0] & 0x7F
            extra = b""
            if kind in (1, 35):  # reply or GenericEvent
                units = struct.unpack_from(self.order + "I", header, 4)[0]
                extra = read_exact(self.upstream, units * 4)
            sequence = struct.unpack_from(self.order + "H", header, 2)[0]
            request = None
            if kind in (0, 1):
                with self.pending_lock:
                    request = self.pending.pop(sequence, None)
            output = header + extra
            if isinstance(request, str):
                reply = bytearray(32)
                reply[0] = 1
                struct.pack_into(self.order + "H", reply, 2, sequence)
                if request == "extension":
                    reply[8:12] = bytes((1, SCREENSAVER_OPCODE, 0, 0))
                elif request == "version":
                    struct.pack_into(self.order + "HH", reply, 8, 1, 1)
                else:
                    struct.pack_into(self.order + "I", reply, 16, self.idle.milliseconds())
                output = bytes(reply)
            elif request is not None:
                try:
                    output = image_reply(self.screen, request, sequence, self.order)
                    print(f"bridged XGetImage {request.width}x{request.height}", file=sys.stderr)
                except Exception as error:
                    print(f"capture failed; passing through X11 response: {error}", file=sys.stderr)
            with self.client_send_lock:
                self.client.sendall(output)


def serve_client(client: socket.socket, upstream_path: Path, cookie: bytes, idle: IdleMonitor) -> None:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(str(upstream_path))
        prefix = read_exact(client, 12)
        order = "<" if prefix[0] == ord("l") else ">" if prefix[0] == ord("B") else None
        if order is None:
            raise ValueError("invalid X11 byte order")
        name_length, data_length = struct.unpack_from(order + "HH", prefix, 6)
        read_exact(client, padded(name_length) + padded(data_length))
        name = b"MIT-MAGIC-COOKIE-1"
        handshake = bytearray(prefix)
        struct.pack_into(order + "HH", handshake, 6, len(name), len(cookie))
        upstream.sendall(handshake + name + bytes(padded(len(name)) - len(name)) + cookie + bytes(padded(len(cookie)) - len(cookie)))
        response = read_exact(upstream, 8)
        extra_length = struct.unpack_from(order + "H", response, 6)[0] * 4
        setup = read_exact(upstream, extra_length)
        client.sendall(response + setup)
        if response[0] != 1:
            raise RuntimeError("upstream X11 authentication failed")
        screen = parse_screen(setup, order)
        connection = Connection(client, upstream, screen, order, idle)
        producer = threading.Thread(target=connection.requests, daemon=True)
        producer.start()
        connection.responses()
    except (EOFError, ConnectionError, BrokenPipeError):
        pass
    except Exception as error:
        print(f"connection failed: {error}", file=sys.stderr)
    finally:
        client.close()
        upstream.close()


def main() -> None:
    from PyQt6.QtGui import QGuiApplication

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default=":99", help="new local X11 display (default: :99)")
    parser.add_argument("--upstream", default=os.environ.get("DISPLAY", ":0"), help="real local X11 display")
    args = parser.parse_args()
    app = QGuiApplication(sys.argv[:1])
    idle = IdleMonitor()
    listen_path = display_socket(args.listen)
    upstream_path = display_socket(args.upstream)
    if listen_path == upstream_path:
        parser.error("--listen and --upstream must differ")
    cookie = xauthority_cookie(args.upstream[1:].split(".", 1)[0])
    if listen_path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(listen_path))
        except OSError:
            listen_path.unlink()
        else:
            parser.error(f"display {args.listen} is already in use")
        finally:
            probe.close()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(listen_path))
    os.chmod(listen_path, 0o600)
    listener.listen(16)
    print(f"X11 Wayland bridge listening on {args.listen}, forwarding to {args.upstream}", flush=True)
    def stop(_signal: int, _frame: object) -> None:
        app.quit()

    signal.signal(signal.SIGTERM, stop)
    def accept_clients() -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                break
            threading.Thread(target=serve_client, args=(client, upstream_path, cookie, idle), daemon=True).start()

    threading.Thread(target=accept_clients, daemon=True).start()
    try:
        app.exec()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        listener.close()
        listen_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
