"""Tests for ``mucky serve``: attach, detach, several clients, auth."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mucky.__main__ import main
from mucky.config import load_config
from mucky.core import ClientCore
from mucky.server import PLACEHOLDER_TOKEN, MuckyServer, resolve_token

from .fakemuck import FakeMuck, wait_for, write_config

FLUFFY = "conn_fake_fluffy"
SPOT = "conn_fake_spot"
TOKEN = "s3cret-token"


def run(coro):
    asyncio.run(coro)


class Viewer:
    """A test front end: collects every message the server sends it."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.messages: list[dict] = []
        self.task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for msg in self.ws:
            self.messages.append(msg.json())

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m["type"] == kind]

    def plain(self, conn_id: str = FLUFFY) -> list[str]:
        out = []
        for m in self.messages:
            if m["type"] == "welcome":
                for s in m["sessions"]:
                    if s["conn_id"] == conn_id:
                        out += [l["plain"] for l in s["scrollback"]]
            elif m["type"] == "lines" and m["conn_id"] == conn_id:
                out += [l["plain"] for l in m["lines"]]
        return out

    async def send(self, text: str, conn_id: str | None = FLUFFY) -> None:
        await self.ws.send_json({"type": "input", "conn_id": conn_id, "text": text})


async def start(tmp_path, extra_chars: str = ""):
    muck = await FakeMuck().start()
    cfg = load_config(write_config(tmp_path / "c.yaml", muck.port, tmp_path / "logs", extra_chars=extra_chars))
    server = MuckyServer(ClientCore(cfg), TOKEN)
    http = TestClient(TestServer(server.make_app()))
    await http.start_server()
    return muck, server, http


async def attach(http, token: str = TOKEN) -> Viewer:
    ws = await http.ws_connect("/ws")
    await ws.send_json({"type": "hello", "token": token})
    viewer = Viewer(ws)
    await wait_for(lambda: viewer.messages)
    return viewer


def test_wrong_token_is_rejected(tmp_path):
    async def scenario():
        muck, server, http = await start(tmp_path)
        viewer = await attach(http, token="nope")
        await wait_for(lambda: viewer.ws.closed)
        assert viewer.messages == [{"type": "error", "text": "Authentication failed."}]
        assert not server.clients
        await http.close()
        await muck.stop()

    run(scenario())


def test_detach_keeps_connection_and_reattach_replays(tmp_path):
    async def scenario():
        muck, server, http = await start(tmp_path)
        await wait_for(lambda: muck.received == ["connect Fluffy pw"])

        first = await attach(http)
        welcome = first.of("welcome")[0]
        assert [s["conn_id"] for s in welcome["sessions"]] == [FLUFFY]
        assert welcome["sessions"][0]["state"] == "connected"

        await muck.send("You see a \x1b[33mlamp\x1b[0m.\n")
        await wait_for(lambda: "You see a lamp." in first.plain())
        await first.send("look lamp")
        await wait_for(lambda: muck.received[-1:] == ["look lamp"])

        # Detach: the MUCK connection stays up and output keeps being logged.
        await first.ws.close()
        await wait_for(lambda: not server.clients)
        await muck.send("While you were away.\n")
        await asyncio.sleep(0.2)
        assert muck.connections == 1
        assert server.core.state(FLUFFY) == "connected"

        # Reattach: scrollback and history come back.
        second = await attach(http)
        session = second.of("welcome")[0]["sessions"][0]
        plain = [l["plain"] for l in session["scrollback"]]
        assert "You see a lamp." in plain and "While you were away." in plain
        lamp = next(l for l in session["scrollback"] if l["plain"] == "You see a lamp.")
        assert "\x1b[33m" in lamp["ansi"]
        assert session["history"] == ["look lamp"]
        await second.ws.close()
        await http.close()
        await muck.stop()

        log = next((tmp_path / "logs" / "Fluffy").glob("*.log")).read_text()
        assert "While you were away.\n" in log

    run(scenario())


def test_several_clients_share_output_but_not_replies(tmp_path):
    async def scenario():
        extra = "      - name: Spot\n        autoconnect: false\n"
        muck, server, http = await start(tmp_path, extra)
        await wait_for(lambda: muck.connections == 1)
        a = await attach(http)
        b = await attach(http)

        await muck.send("Hello both\n")
        await wait_for(lambda: "Hello both" in a.plain() and "Hello both" in b.plain())

        # Replies to one client's command go only to that client.
        await a.send("/nonsense")
        await wait_for(lambda: a.of("notice"))
        await a.send("/help")
        await wait_for(lambda: a.of("help"))
        await asyncio.sleep(0.1)
        assert not b.of("notice") and not b.of("help")

        # Opening a character: both see it open, only the opener is focused.
        await a.ws.send_json({"type": "open", "conn_id": SPOT})
        await wait_for(lambda: muck.connections == 2)
        await wait_for(lambda: b.of("opened"))
        await wait_for(lambda: a.of("focus"))
        assert a.of("focus")[0]["conn_id"] == SPOT
        assert b.of("opened")[0]["profile"]["tab_name"] == "Spot"
        assert not b.of("focus")

        # /quit detaches only the client that typed it.
        await a.send("/quit")
        await wait_for(lambda: a.ws.closed)
        await asyncio.sleep(0.1)
        assert not b.ws.closed
        assert server.core.state(FLUFFY) == "connected"
        assert len(server.clients) == 1

        # A command from b still reaches the MUCK.
        await b.send("say still here")
        await wait_for(lambda: "say still here" in muck.received)
        await b.ws.close()
        await http.close()
        await muck.stop()

    run(scenario())


def test_token_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("MUCKY_TOKEN", raising=False)
    cfg = load_config(write_config(tmp_path / "c.yaml", 1, tmp_path / "logs"))
    with pytest.raises(ValueError, match="needs a token"):
        resolve_token(cfg)
    cfg.server.token = PLACEHOLDER_TOKEN
    with pytest.raises(ValueError, match="example value"):
        resolve_token(cfg)
    cfg.server.token = "from-config"
    assert resolve_token(cfg) == "from-config"
    monkeypatch.setenv("MUCKY_TOKEN", "from-env")
    assert resolve_token(cfg) == "from-env"


def test_serve_command_refuses_to_start_without_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MUCKY_TOKEN", raising=False)
    path = write_config(tmp_path / "c.yaml", 1, tmp_path / "logs")
    assert main(["serve", "-c", path]) == 1
    assert "needs a token" in capsys.readouterr().err


def test_server_block_in_config(tmp_path):
    path = tmp_path / "c.yaml"
    write_config(path, 1, tmp_path / "logs")
    path.write_text(path.read_text() + "server:\n  host: 0.0.0.0\n  port: 9000\n  token: abc\n")
    cfg = load_config(path)
    assert (cfg.server.host, cfg.server.port, cfg.server.token) == ("0.0.0.0", 9000, "abc")
    assert load_config(write_config(tmp_path / "d.yaml", 1, tmp_path / "logs")).server.port == 8765
