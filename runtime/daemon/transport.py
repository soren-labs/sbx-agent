"""Daemon transport — connects OUTBOUND to the control-plane ingress.

Endpoints:
- ``tcp://host:port`` — newline-delimited JSON frames (local loopback; the
  RFC's permitted local path with identical frame semantics).
- ``ws(s)://host/path`` — minimal RFC 6455 client (text frames, client
  masking, TLS via ssl module) used when the ingress is a WebSocket URL
  (deployed topology).
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from collections.abc import Callable
from urllib.parse import urlparse

from protocol.runtime import decode_frame, encode_frame

_RECV_LIMIT = 8 * 1024 * 1024


class TransportClosed(Exception):
    pass


class _SocketTransport:
    """newline-delimited JSON over a connected socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""
        self._send_lock = threading.Lock()

    def send(self, frame: dict) -> None:
        data = encode_frame(frame)
        with self._send_lock:
            self._sock.sendall(data)

    def recv(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise TransportClosed("peer closed")
            self._buf += chunk
            if len(self._buf) > _RECV_LIMIT:
                raise TransportClosed("frame buffer limit")
        line, self._buf = self._buf.split(b"\n", 1)
        return decode_frame(line)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class _WebSocketTransport:
    """Minimal client-side RFC 6455 (text frames only, masked sends)."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._send_lock = threading.Lock()
        self._buf = b""

    @staticmethod
    def connect(url: str, timeout: float = 30.0) -> _WebSocketTransport:
        parsed = urlparse(url)
        use_tls = parsed.scheme == "wss"
        host = parsed.hostname or ""
        port = parsed.port or (443 if use_tls else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        # Clear the connect timeout — see tcp path; the channel must idle.
        raw.settimeout(None)
        sock: socket.socket
        if use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            sock = raw
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise TransportClosed("handshake: peer closed")
            response += chunk
            if len(response) > 64 * 1024:
                sock.close()
                raise TransportClosed("handshake: oversized response")
        head, _, rest = response.partition(b"\r\n\r\n")
        if b" 101" not in head.split(b"\r\n", 1)[0]:
            sock.close()
            raise TransportClosed(f"handshake rejected: {head.splitlines()[0]!r}")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if accept.encode() not in head:
            sock.close()
            raise TransportClosed("handshake: bad accept key")
        transport = _WebSocketTransport(sock)
        transport._buf = rest
        return transport

    def send(self, frame: dict) -> None:
        payload = encode_frame(frame).rstrip(b"\n")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(bytes(header) + masked)

    def recv(self) -> dict:
        while True:
            frame = self._read_ws_frame()
            if frame is None:
                continue
            opcode, payload = frame
            if opcode == 0x8:
                raise TransportClosed("ws close")
            if opcode == 0x9:  # ping
                self._send_pong(payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                return decode_frame(payload)

    def _send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytes([0x8A, 0x80 | len(payload)]) + mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self._sock.sendall(header + masked)

    def _read_ws_frame(self) -> tuple[int, bytes] | None:
        header = self._read_exact(2)
        if header is None:
            raise TransportClosed("ws read: closed")
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            ext = self._read_exact(2)
            if ext is None:
                raise TransportClosed("ws read: closed")
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._read_exact(8)
            if ext is None:
                raise TransportClosed("ws read: closed")
            length = struct.unpack(">Q", ext)[0]
        if length > _RECV_LIMIT:
            raise TransportClosed("ws frame limit")
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length)
        if payload is None:
            raise TransportClosed("ws read: closed")
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _read_exact(self, n: int) -> bytes | None:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def listen(port: int, host: str = "0.0.0.0") -> socket.socket:
    """Serve mode: the runtime binds inside the executor and the control
    plane dials in (Modal sandbox topology — the sandbox cannot reach a
    control-plane loopback, so the channel direction inverts while the
    frame protocol stays identical)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(4)
    return sock


def accept(listener: socket.socket, timeout: float = 30.0) -> _SocketTransport:
    """Accept one inbound control-plane connection."""
    listener.settimeout(timeout)
    conn, _peer = listener.accept()
    conn.settimeout(None)
    return _SocketTransport(conn)


def connect(endpoint: str, timeout: float = 30.0):
    """Dial the ingress. ``tcp://host:port`` → JSONL; ``ws(s)://`` → RFC6455."""
    if endpoint.startswith(("ws://", "wss://")):
        return _WebSocketTransport.connect(endpoint, timeout)
    parsed = urlparse(endpoint if "//" in endpoint else f"tcp://{endpoint}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 0
    if port == 0:
        raise ValueError(f"bad endpoint {endpoint!r}")
    sock = socket.create_connection((host, port), timeout=timeout)
    # create_connection leaves `timeout` on the socket — recv() would raise
    # socket.timeout after `timeout` seconds of idle and look like a transport
    # loss. The channel is meant to idle between operations.
    sock.settimeout(None)
    return _SocketTransport(sock)


Transport = _SocketTransport | _WebSocketTransport
TransportFactory = Callable[[str], Transport]
