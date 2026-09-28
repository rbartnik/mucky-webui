"""A tiny in-process telnet-ish server for tests."""

from __future__ import annotations

import asyncio


class FakeMuck:
    """Accepts connections on localhost and records the lines clients send."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.connections = 0
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> "FakeMuck":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.writers.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                self.received.append(line.decode().rstrip("\r\n"))
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def send(self, text: str) -> None:
        writer = self.writers[-1]
        writer.write(text.encode())
        await writer.drain()

    async def drop_clients(self) -> None:
        for w in self.writers:
            w.close()
        self.writers.clear()

    async def stop(self) -> None:
        await self.drop_clients()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def wait_for(predicate, timeout: float = 3.0, interval: float = 0.02) -> None:
    """Poll ``predicate`` until it is truthy or fail after ``timeout`` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


def write_config(path, port: int, log_dir, *, reconnect: bool = False, extra_chars: str = "") -> str:
    path.write_text(
        f"""
log_dir: {log_dir}
gags:
  - "^GAGME"
highlights:
  - pattern: "Fluffy"
    color: "bold red"
servers:
  - name: Fake
    host: 127.0.0.1
    port: {port}
    reconnect: {str(reconnect).lower()}
    characters:
      - name: Fluffy
        login: "connect Fluffy pw"
        autoconnect: true
{extra_chars}"""
    )
    return str(path)
