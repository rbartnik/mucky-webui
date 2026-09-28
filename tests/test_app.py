"""Headless end-to-end tests of the Textual app against a fake server."""

from __future__ import annotations

import asyncio

from mucky.app import MuckyApp, OutputLine, OutputLog
from mucky.config import load_config

from .fakemuck import FakeMuck, wait_for, write_config

FLUFFY = "conn_fake_fluffy"
SPOT = "conn_fake_spot"

def lines(app, conn_id=FLUFFY) -> list[str]:
    log = app.query_one(f"#log-{conn_id}", OutputLog)
    return [line.content.plain for line in log.query(OutputLine)]

def run(coro):
    asyncio.run(coro)

def test_output_input_and_logging(tmp_path):
    async def scenario():
        server = await FakeMuck().start()
        cfg = load_config(write_config(tmp_path / "c.yaml", server.port, tmp_path / "logs"))
        app = MuckyApp(cfg)
        async with app.run_test() as pilot:
            await wait_for(lambda: server.received == ["connect Fluffy pw"])
            await server.send(
                "Hello \x1b[31mred\x1b[0m Fluffy\r\nGAGME secret\nsee https://example.com/x.\nprompt> "
            )
            await wait_for(lambda: "prompt> " in lines(app))
            out = lines(app)
            assert "Hello red Fluffy" in out
            assert "see https://example.com/x." in out
            assert not any("GAGME" in l for l in out)

            # ANSI color, highlight and link styling survive.
            log = app.query_one(f"#log-{FLUFFY}", OutputLog)
            hello = next(l.content for l in log.query(OutputLine) if l.content.plain.startswith("Hello"))
            styles = {str(s.style) for s in hello.spans}
            assert any("red" in s for s in styles)
            assert "bold red" in styles
            link = next(l.content for l in log.query(OutputLine) if l.content.plain.startswith("see"))
            link_spans = [s for s in link.spans if "underline" in str(s.style)]
            assert link.plain[link_spans[0].start:link_spans[0].end] == "https://example.com/x"

            # Typing a command sends it and records history.
            await pilot.press(*"look", "enter")
            await wait_for(lambda: server.received[-1:] == ["look"])
            await pilot.press("escape", "up")
            assert app.query_one("#input").value == "look"

            # /disconnect closes the tab.
            app.query_one("#input").value = ""
            await pilot.press(*"/disconnect", "enter")
            await wait_for(lambda: not app.query(OutputLog))
        await server.stop()

        logs = list((tmp_path / "logs" / "Fluffy").glob("*.log"))
        assert len(logs) == 1
        text = logs[0].read_text()
        assert "Hello red Fluffy\n" in text
        assert "GAGME" not in text
        assert "\x1b" not in text

    run(scenario())

def test_unread_badge_on_inactive_tab(tmp_path):
    async def scenario():
        server = await FakeMuck().start()
        extra = "      - name: Spot\n        autoconnect: false\n"
        cfg = load_config(write_config(tmp_path / "c.yaml", server.port, tmp_path / "logs", extra_chars=extra))
        app = MuckyApp(cfg)
        async with app.run_test() as pilot:
            await wait_for(lambda: server.connections == 1)
            await pilot.press(*"/connect Spot", "enter")
            await wait_for(lambda: server.connections == 2)
            tabs = app.query_one("#tabs")
            assert tabs.active == SPOT
            # The first connection is Fluffy's; talk on it while Spot is active.
            fluffy_writer = server.writers[0]
            fluffy_writer.write(b"one\ntwo\n")
            await fluffy_writer.drain()
            await wait_for(lambda: "two" in lines(app, FLUFFY))
            label = str(tabs.get_tab(FLUFFY).label)
            assert label.startswith("Fluffy [") and label.endswith("]")
            await pilot.press("ctrl+left")
            await wait_for(lambda: str(tabs.get_tab(FLUFFY).label) == "Fluffy")

            # /connect on an already-open character switches to its tab.
            await pilot.press(*"/connect Spot", "enter")
            await wait_for(lambda: tabs.active == SPOT)
            assert server.connections == 2
        await server.stop()

    run(scenario())
