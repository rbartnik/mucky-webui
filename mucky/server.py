"""``mucky serve``: run the client core as a long-lived server clients attach to.

The server owns the MUCK connections and session logs. Front ends connect over
a WebSocket at ``/ws``, authenticate with the configured token, receive a
snapshot of every open session (state, history, scrollback), then get live
events. Closing a front end never touches the connections.

Protocol (JSON text frames):

client -> server
    {"type": "hello", "token": "..."}                 must be the first frame
    {"type": "input", "conn_id": "..."|null, "text": "..."}
    {"type": "open", "conn_id": "..."}                  open + connect a character

server -> client
    {"type": "welcome", "profiles": [...], "sessions": [...]}
    {"type": "lines", "conn_id": ..., "lines": [line, ...]}
    {"type": "state", "conn_id": ..., "state": ...}
    {"type": "opened"|"closed"|"cleared"|"focus", "conn_id": ...}
    {"type": "reloaded", "profiles": [...]}
    {"type": "notice", "text": ..., "severity": ..., "timeout": ...}
    {"type": "help"}
    {"type": "error", "text": ...}                      then the socket closes

A line is {"ansi", "plain", "highlights": [[start, end, style]],
"links": [[start, end, url]], "status"}. Notices, help and focus replies go
only to the client whose input caused them. ``/quit`` from a client detaches
that client; it does not stop the server.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from aiohttp import WSMsgType, web

from .config import Config, Profile
from .core import (
    Cleared,
    ClientCore,
    ConfigReloaded,
    Event,
    FocusRequested,
    HelpRequested,
    Line,
    LinesAdded,
    Notice,
    QuitRequested,
    SessionClosed,
    SessionOpened,
    StateChanged,
)

log = logging.getLogger("mucky.server")

# The token shipped in config.example.yaml; refuse to run with it.
PLACEHOLDER_TOKEN = "change-me-to-a-long-random-string"

# The browser client: index.html at "/", everything else under "/static/".
WEB_DIR = Path(__file__).parent / "web"

# How long a client has to send its hello frame.
HELLO_TIMEOUT = 10

# Events a client may fall behind by before it is disconnected, so one stalled
# client can't grow the server's memory without bound.
CLIENT_QUEUE_LIMIT = 10000


def profile_info(p: Profile) -> dict:
    return {
        "id": p.id,
        "tab_name": p.tab_name,
        "char_name": p.char_name,
        "server_name": p.server_name,
        "host": p.host,
        "port": p.port,
        "tls": p.tls,
    }


def line_info(line: Line) -> dict:
    return asdict(line)


class Client:
    """One attached front end, with its own outgoing queue."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue(CLIENT_QUEUE_LIMIT)

    def send(self, message: dict | None) -> None:
        """Queue a message (``None`` asks the sender to close the socket)."""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("client fell too far behind; disconnecting it")
            # Make room for the close request; the client is being dropped.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)


class MuckyServer:
    def __init__(self, core: ClientCore, token: str) -> None:
        self.core = core
        self.token = token
        self.clients: set[Client] = set()
        core.subscribe(self._on_event)

    # ----- web app ---------------------------------------------------------

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._websocket)
        app.router.add_static("/static/", WEB_DIR)
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)
        return app

    async def _on_startup(self, app: web.Application) -> None:
        await self.core.start()

    async def _on_shutdown(self, app: web.Application) -> None:
        for client in list(self.clients):
            await client.ws.close()
        await self.core.shutdown()

    async def _index(self, request: web.Request) -> web.FileResponse:
        # The page holds no data; the WebSocket's token check guards everything.
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    # ----- snapshot and events ---------------------------------------------

    def snapshot(self) -> dict:
        core = self.core
        return {
            "type": "welcome",
            "profiles": [profile_info(p) for p in core.profiles],
            "sessions": [
                {
                    "conn_id": cid,
                    "profile": profile_info(core.profile(cid)),
                    "state": core.state(cid),
                    "history": core.history(cid),
                    "scrollback": [line_info(l) for l in core.scrollback(cid)],
                }
                for cid in core.open_sessions()
            ],
        }

    def _on_event(self, event: Event) -> None:
        message = self._encode(event)
        if message is None:
            return
        origin = getattr(event, "origin", None)
        for client in list(self.clients):
            if origin is None or origin is client:
                client.send(message)

    def _encode(self, event: Event) -> dict | None:
        if isinstance(event, LinesAdded):
            return {
                "type": "lines",
                "conn_id": event.conn_id,
                "lines": [line_info(l) for l in event.lines],
            }
        if isinstance(event, StateChanged):
            return {"type": "state", "conn_id": event.conn_id, "state": event.state}
        if isinstance(event, SessionOpened):
            profile = self.core.profile(event.conn_id)
            return {
                "type": "opened",
                "conn_id": event.conn_id,
                "profile": profile_info(profile) if profile else None,
            }
        if isinstance(event, SessionClosed):
            return {"type": "closed", "conn_id": event.conn_id}
        if isinstance(event, Cleared):
            return {"type": "cleared", "conn_id": event.conn_id}
        if isinstance(event, FocusRequested):
            return {"type": "focus", "conn_id": event.conn_id}
        if isinstance(event, ConfigReloaded):
            return {
                "type": "reloaded",
                "profiles": [profile_info(p) for p in self.core.profiles],
            }
        if isinstance(event, Notice):
            return {
                "type": "notice",
                "text": event.text,
                "severity": event.severity,
                "timeout": event.timeout,
            }
        if isinstance(event, HelpRequested):
            return {"type": "help"}
        if isinstance(event, QuitRequested):
            # A client's /quit detaches that client only.
            if isinstance(event.origin, Client):
                event.origin.send(None)
            return None
        return None

    # ----- websocket -------------------------------------------------------

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        if not await self._authenticate(ws):
            await ws.close()
            return ws

        client = Client(ws)
        # Snapshot and subscribe in one step, so no event is missed or repeated.
        client.send(self.snapshot())
        self.clients.add(client)
        log.info("client attached from %s", request.remote)
        sender = asyncio.create_task(self._send_loop(client))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                await self._handle(client, msg.data)
        finally:
            self.clients.discard(client)
            sender.cancel()
            log.info("client detached from %s", request.remote)
        return ws

    async def _authenticate(self, ws: web.WebSocketResponse) -> bool:
        try:
            msg = await ws.receive(timeout=HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            await ws.send_json({"type": "error", "text": "No hello received."})
            return False
        if msg.type != WSMsgType.TEXT:
            return False
        try:
            data = json.loads(msg.data)
        except ValueError:
            data = {}
        token = data.get("token") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("type") != "hello"
            or not isinstance(token, str)
            or not hmac.compare_digest(token.encode(), self.token.encode())
        ):
            await ws.send_json({"type": "error", "text": "Authentication failed."})
            return False
        return True

    async def _send_loop(self, client: Client) -> None:
        try:
            while True:
                message = await client.queue.get()
                if message is None:
                    await client.ws.close()
                    return
                await client.ws.send_json(message)
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def _handle(self, client: Client, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            client.send({"type": "error", "text": "Invalid JSON."})
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        conn_id = data.get("conn_id")
        if conn_id is not None and not isinstance(conn_id, str):
            return
        if kind == "input":
            text = data.get("text")
            if isinstance(text, str):
                await self.core.submit(conn_id, text, origin=client)
        elif kind == "open":
            if conn_id and self.core.profile(conn_id) is not None:
                await self.core.submit(None, f"/connect {conn_id}", origin=client)


def resolve_token(config: Config) -> str:
    """The token clients must present, or raise ``ValueError`` if unusable."""
    token = os.environ.get("MUCKY_TOKEN") or config.server.token
    if not token:
        raise ValueError(
            "mucky serve needs a token. Set server.token in the config "
            "(or the MUCKY_TOKEN environment variable)."
        )
    if token == PLACEHOLDER_TOKEN:
        raise ValueError(
            "server.token is still the example value; replace it with a long "
            "random string."
        )
    return token


def serve(config: Config, host: str | None = None, port: int | None = None) -> int:
    """Run the server until interrupted. Returns a process exit code."""
    token = resolve_token(config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    host = host or config.server.host
    port = port or config.server.port
    server = MuckyServer(ClientCore(config), token)
    web.run_app(
        server.make_app(),
        host=host,
        port=port,
        handle_signals=True,
        print=lambda _: log.info("mucky serving on http://%s:%s", host, port),
    )
    return 0
