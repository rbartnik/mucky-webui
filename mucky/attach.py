"""``mucky attach``: a plain line-mode client for a running ``mucky serve``.

It prints every session's output prefixed with its tab name and sends what
you type to the current session. Type ``>Name`` to switch session. Exiting
(Ctrl+D, Ctrl+C or ``/quit``) detaches; the server keeps the connections.
Mainly a test tool until the web client exists.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys

import aiohttp

# Scrollback lines to print per session when attaching.
REPLAY_LINES = 20


class Attached:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.current: str | None = None

    def show(self, conn_id: str, line: dict) -> None:
        name = self.names.get(conn_id, conn_id)
        text = line["ansi"]
        if line.get("status"):
            text = f"\x1b[2;3m{text}\x1b[0m"
        print(f"[{name}] {text}\x1b[0m", flush=True)

    def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "welcome":
            for p in msg["profiles"]:
                self.names[p["id"]] = p["tab_name"]
            for s in msg["sessions"]:
                self.names[s["conn_id"]] = s["profile"]["tab_name"]
                for line in s["scrollback"][-REPLAY_LINES:]:
                    self.show(s["conn_id"], line)
                self.current = s["conn_id"]
            open_names = ", ".join(self.names[s["conn_id"]] for s in msg["sessions"])
            print(f"*** attached; open: {open_names or 'none'}", flush=True)
            self.prompt()
        elif kind == "lines":
            for line in msg["lines"]:
                self.show(msg["conn_id"], line)
        elif kind == "opened":
            if msg.get("profile"):
                self.names[msg["conn_id"]] = msg["profile"]["tab_name"]
            if self.current is None:
                self.current = msg["conn_id"]
        elif kind == "focus":
            self.current = msg["conn_id"]
            self.prompt()
        elif kind == "closed":
            if self.current == msg["conn_id"]:
                self.current = None
                self.prompt()
        elif kind == "reloaded":
            for p in msg["profiles"]:
                self.names[p["id"]] = p["tab_name"]
        elif kind == "state":
            name = self.names.get(msg["conn_id"], msg["conn_id"])
            print(f"*** {name}: {msg['state']}", flush=True)
        elif kind in ("notice", "error"):
            print(f"*** {msg['text']}", flush=True)
        elif kind == "help":
            print("*** Type to send to the current session; >Name switches session; "
                  "/connect, /disconnect, /reconnect, /clear, /reload work as in the "
                  "terminal client; Ctrl+D or /quit detaches.", flush=True)

    def prompt(self) -> None:
        name = self.names.get(self.current, "none") if self.current else "none"
        print(f"*** sending to: {name}", flush=True)

    def switch(self, name: str) -> None:
        low = name.strip().lower()
        for cid, tab in self.names.items():
            if tab.lower() == low or cid == name.strip():
                self.current = cid
                self.prompt()
                return
        print(f"*** no session named {name.strip()!r}", flush=True)


async def _run(url: str, token: str) -> int:
    state = Attached()
    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession() as http:
        try:
            ws = await http.ws_connect(url, heartbeat=30)
        except aiohttp.ClientError as exc:
            print(f"error: cannot reach {url}: {exc}", file=sys.stderr)
            return 1
        await ws.send_json({"type": "hello", "token": token})

        welcomed = asyncio.Event()

        async def read_stdin() -> None:
            # Know which sessions are open before sending anything.
            await welcomed.wait()
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    await ws.close()
                    return
                line = line.rstrip("\n")
                if line.startswith(">"):
                    state.switch(line[1:])
                    continue
                await ws.send_json({"type": "input", "conn_id": state.current, "text": line})

        reader = asyncio.create_task(read_stdin())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    state.handle(data)
                    if data.get("type") == "welcome":
                        welcomed.set()
        finally:
            reader.cancel()
    print("*** detached", flush=True)
    return 0


def attach(url: str, token: str | None = None) -> int:
    token = os.environ.get("MUCKY_TOKEN") or token or getpass.getpass("mucky token: ")
    try:
        return asyncio.run(_run(url, token))
    except KeyboardInterrupt:
        print("\n*** detached", flush=True)
        return 0
