"""Async telnet connection with minimal IAC handling, TLS, keepalive and reconnect."""

from __future__ import annotations

import asyncio
import ssl
from typing import Callable

from .config import Profile

# Telnet command bytes (RFC 854 et al.)
IAC = 255  # Interpret As Command
SE = 240   # End of subnegotiation
NOP = 241  # No operation (used for keepalive)
SB = 250   # Begin subnegotiation
WILL = 251
WONT = 252
DO = 253
DONT = 254


class TelnetFilter:
    """Strips telnet IAC sequences from an incoming byte stream.

    ``feed`` returns a tuple of ``(clean_bytes, response_bytes)`` where
    ``response_bytes`` are telnet negotiation replies that should be written
    back to the server. We politely refuse every offered/requested option.
    """

    def __init__(self) -> None:
        self._state = "data"
        self._cmd: int | None = None

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        out = bytearray()
        resp = bytearray()
        for b in data:
            if self._state == "data":
                if b == IAC:
                    self._state = "iac"
                else:
                    out.append(b)
            elif self._state == "iac":
                if b == IAC:  # escaped 0xFF literal
                    out.append(IAC)
                    self._state = "data"
                elif b in (DO, DONT, WILL, WONT):
                    self._cmd = b
                    self._state = "opt"
                elif b == SB:
                    self._state = "sb"
                else:  # standalone command (GA, NOP, ...) - ignore
                    self._state = "data"
            elif self._state == "opt":
                opt = b
                if self._cmd == DO:
                    resp += bytes([IAC, WONT, opt])
                elif self._cmd == WILL:
                    resp += bytes([IAC, DONT, opt])
                # DONT / WONT need no reply
                self._cmd = None
                self._state = "data"
            elif self._state == "sb":
                if b == IAC:
                    self._state = "sb_iac"
            elif self._state == "sb_iac":
                self._state = "data" if b == SE else "sb"
        return bytes(out), bytes(resp)


class Connection:
    """Manages a single telnet connection lifecycle.

    Decoded text is delivered via ``on_text`` and lifecycle/status notices via
    ``on_status``. Both callbacks must be safe to call from the asyncio loop;
    the app wraps them so they post Textual messages.
    """

    def __init__(
        self,
        profile: Profile,
        on_text: Callable[[str], None],
        on_status: Callable[[str], None],
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self.profile = profile
        self.on_text = on_text
        self.on_status = on_status
        self.on_state = on_state or (lambda state: None)

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._filter = TelnetFilter()
        self._keepalive_task: asyncio.Task | None = None
        self._closing = False
        self.connected = False

    async def run(self) -> None:
        """Connect (with optional auto-reconnect) until closed."""
        self._closing = False
        attempt = 0
        while not self._closing:
            try:
                await self._connect_once()
                attempt = 0
            except (OSError, ssl.SSLError, asyncio.IncompleteReadError) as exc:
                self.on_status(f"Connection error: {exc}")
                self.on_state("error")
            finally:
                self.connected = False
                self._cancel_keepalive()

            if self._closing or not self.profile.reconnect:
                break

            attempt += 1
            delay = min(30, 2 ** min(attempt, 5))
            self.on_status(f"Reconnecting in {delay}s...")
            self.on_state("reconnecting")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

        self.on_status("Disconnected.")
        self.on_state("disconnected")

    async def _connect_once(self) -> None:
        p = self.profile
        scheme = "telnets" if p.tls else "telnet"
        self.on_status(f"Connecting to {scheme}://{p.host}:{p.port} ...")
        self.on_state("connecting")

        ssl_ctx = None
        if p.tls:
            ssl_ctx = ssl.create_default_context()
            if p.insecure:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                # Lower the OpenSSL security level so legacy MUCK/MUD certs
                # (e.g. SHA-1 signed, weak keys) don't abort the handshake.
                ssl_ctx.set_ciphers("DEFAULT@SECLEVEL=0")
                self.on_status("WARNING: TLS certificate validation disabled (insecure).")
        self._reader, self._writer = await asyncio.open_connection(
            p.host, p.port, ssl=ssl_ctx
        )
        self._filter = TelnetFilter()
        self.connected = True
        self.on_status("Connected.")
        self.on_state("connected")

        if p.login:
            await self.send(p.login)
        if p.keepalive:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        await self._read_loop()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            data = await self._reader.read(4096)
            if not data:
                self.on_status("Connection closed by remote host.")
                return
            clean, resp = self._filter.feed(data)
            if resp and self._writer is not None:
                self._writer.write(resp)
                await self._writer.drain()
            if clean:
                self.on_text(clean.decode(self.profile.encoding, errors="replace"))

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.profile.keepalive)
                if self._writer is not None:
                    self._writer.write(bytes([IAC, NOP]))
                    await self._writer.drain()
        except asyncio.CancelledError:
            pass

    def _cancel_keepalive(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    async def send(self, line: str) -> None:
        """Send a command line to the server (CRLF terminated)."""
        if self._writer is None or not self.connected:
            self.on_status("Not connected.")
            return
        payload = (line + "\r\n").encode(self.profile.encoding, errors="replace")
        self._writer.write(payload)
        await self._writer.drain()

    async def close(self) -> None:
        """Close the connection and stop any reconnect attempts."""
        self._closing = True
        self._cancel_keepalive()
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
        self._writer = None
        self._reader = None
        self.connected = False
