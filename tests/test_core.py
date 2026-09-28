"""Tests for the UI-independent ClientCore."""

from __future__ import annotations

import asyncio
import subprocess
import sys

from mucky.config import load_config
from mucky.core import ClientCore, LinesAdded, SessionClosed, find_links

from .fakemuck import FakeMuck, wait_for, write_config

FLUFFY = "conn_fake_fluffy"


def run(coro):
    asyncio.run(coro)


def output(core: ClientCore, conn_id: str = FLUFFY) -> list[str]:
    return [line.plain for line in core.scrollback(conn_id) if not line.status]


def test_core_does_not_import_textual():
    code = "import sys, mucky.core; print('textual' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.stdout.strip() == "False", result.stderr


def test_lines_triggers_links_and_partial_flush(tmp_path):
    async def scenario():
        server = await FakeMuck().start()
        core = ClientCore(load_config(write_config(tmp_path / "c.yaml", server.port, tmp_path / "logs")))
        events = []
        core.subscribe(events.append)
        await core.start()
        await wait_for(lambda: server.received == ["connect Fluffy pw"])

        await server.send("Hi \x1b[1;32mFluffy\x1b[0m\r\nGAGME x\nhttps://e.com/a_(b) and (https://e.com/c).\nprompt> ")
        await wait_for(lambda: "prompt> " in output(core))
        assert output(core) == ["Hi Fluffy", "https://e.com/a_(b) and (https://e.com/c).", "prompt> "]

        hi = next(l for l in core.scrollback(FLUFFY) if l.plain == "Hi Fluffy")
        assert hi.ansi == "Hi \x1b[1;32mFluffy\x1b[0m"
        assert hi.highlights == [(3, 9, "bold red")]
        links = next(l for l in core.scrollback(FLUFFY) if l.plain.startswith("https")).links
        assert [url for _, _, url in links] == ["https://e.com/a_(b)", "https://e.com/c"]

        # Complete lines arrive as one batch; the prompt follows after the flush delay.
        batches = [e.lines for e in events if isinstance(e, LinesAdded) and not e.lines[0].status]
        assert [len(b) for b in batches] == [2, 1]

        await core.submit(FLUFFY, "look")
        await core.submit(FLUFFY, "look")
        await core.submit(FLUFFY, "   ")
        await wait_for(lambda: server.received[-2:] == ["look", "   "])
        assert core.history(FLUFFY) == ["look"]

        await core.submit(FLUFFY, "/clear")
        assert core.scrollback(FLUFFY) == []

        await core.submit(FLUFFY, "/disconnect")
        assert core.open_sessions() == []
        assert isinstance(events[-1], SessionClosed)
        await core.shutdown()
        await server.stop()

    run(scenario())


def test_connect_during_reconnect_backoff_does_not_duplicate(tmp_path):
    async def scenario():
        server = await FakeMuck().start()
        core = ClientCore(load_config(
            write_config(tmp_path / "c.yaml", server.port, tmp_path / "logs", reconnect=True)
        ))
        await core.start()
        await wait_for(lambda: core.state(FLUFFY) == "connected")
        await server.drop_clients()
        await wait_for(lambda: core.state(FLUFFY) == "reconnecting")

        # F5 / "/connect" while waiting to reconnect must not start a second connection.
        core.start_connection(FLUFFY)
        await core.submit(FLUFFY, "/connect")
        await wait_for(lambda: server.connections == 2, timeout=5)
        await asyncio.sleep(0.3)
        assert server.connections == 2
        assert len(list((tmp_path / "logs" / "Fluffy").glob("*.log"))) == 1
        await core.shutdown()
        await server.stop()

    run(scenario())


def test_reconnect_closes_previous_log(tmp_path):
    async def scenario():
        server = await FakeMuck().start()
        core = ClientCore(load_config(write_config(tmp_path / "c.yaml", server.port, tmp_path / "logs")))
        await core.start()
        await wait_for(lambda: core.state(FLUFFY) == "connected")
        first_log = core._logs[FLUFFY]
        # Session log file names have one-second resolution.
        await asyncio.sleep(1.1)
        await core.submit(FLUFFY, "/reconnect")
        await wait_for(lambda: server.connections == 2)
        assert first_log._fh.closed
        assert core._logs[FLUFFY] is not first_log
        await core.shutdown()
        await server.stop()

    run(scenario())


def test_reload_keeps_removed_open_session(tmp_path):
    async def scenario():
        server = await FakeMuck().start()
        path = write_config(tmp_path / "c.yaml", server.port, tmp_path / "logs")
        core = ClientCore(load_config(path))
        notices = []
        core.subscribe(notices.append)
        await core.start()
        await wait_for(lambda: core.state(FLUFFY) == "connected")

        text = (tmp_path / "c.yaml").read_text().replace("name: Fluffy", "name: Rex")
        (tmp_path / "c.yaml").write_text(text)
        await core.submit(FLUFFY, "/reload")
        assert [p.char_name for p in core.profiles] == ["Rex"]
        assert core.profile(FLUFFY).char_name == "Fluffy"

        (tmp_path / "c.yaml").write_text("servers: [")
        await core.submit(FLUFFY, "/reload")
        assert notices[-1].severity == "error"
        assert [p.char_name for p in core.profiles] == ["Rex"]
        await core.shutdown()
        await server.stop()

    run(scenario())


def test_find_links_trims_trailing_punctuation():
    assert find_links("see https://x.org/y. ok") == [(4, 19, "https://x.org/y")]
    assert find_links("none here") == []
