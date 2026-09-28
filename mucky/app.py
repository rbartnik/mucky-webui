"""Textual UI: tabbed connections, output pane, and an input bar with char count."""

from __future__ import annotations

import asyncio
import re
from functools import partial

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Header, Input, Static, TabbedContent, TabPane

from .config import Config, load_config
from .connection import Connection
from .session_log import SessionLog

# How long to wait for more data before flushing a partial line (e.g. a prompt
# with no trailing newline) to the output pane.
_PARTIAL_FLUSH_DELAY = 0.08

# Matches http(s) URLs in output. Trailing sentence punctuation is trimmed
# separately so it isn't swept into the link.
_URL_RE = re.compile(r"https?://[^\s\x00-\x1f<>\"'`]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?)]}>\"'"

# Human-readable label and color for each connection state.
_STATE_DISPLAY = {
    "idle": ("Idle", "grey50"),
    "connecting": ("Connecting…", "yellow"),
    "connected": ("Connected", "green"),
    "reconnecting": ("Reconnecting…", "yellow"),
    "error": ("Error", "red"),
    "disconnected": ("Disconnected", "red"),
}


class OutputLine(Static):
    """A single selectable line of server output."""


class OutputLog(VerticalScroll):
    """A scrollable output pane built from one Static per line.

    Unlike ``RichLog``, each line is a real ``Static`` widget whose rendered
    visual is ``Text``, so Textual's mouse text-selection (and Cmd/Ctrl+C copy)
    works across lines. Exposes a ``RichLog``-compatible ``write``/``clear``.
    """

    DEFAULT_CSS = """
    OutputLog {
        background: $surface;
        scrollbar-gutter: stable;
    }
    OutputLog > OutputLine {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self, *args, max_lines: int = 5000, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._max_lines = max_lines

    def write(self, renderable, *, scroll_end: bool | None = None) -> None:
        # Late output (status/state notices, a pending partial-line flush) can
        # arrive while the app is shutting down and this pane is being removed;
        # mounting a child into a detached widget raises MountError.
        if not self.is_attached:
            return
        follow = self.is_vertical_scroll_end if scroll_end is None else scroll_end
        self.mount(OutputLine(renderable))
        lines = self.query(OutputLine)
        excess = len(lines) - self._max_lines
        if excess > 0:
            for line in list(lines)[:excess]:
                line.remove()
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def clear(self) -> None:
        self.query(OutputLine).remove()


class CommandInput(Input):
    """Input that emits history-navigation messages on Up/Down."""

    class History(Message):
        def __init__(self, step: int) -> None:
            self.step = step
            super().__init__()

    # When True, the next arrow key is part of an Esc-prefixed chord:
    # Up/Down navigate command history, Left/Right cycle tabs.
    _esc_pending: bool = False

    def on_key(self, event: events.Key) -> None:
        if self._esc_pending:
            self._esc_pending = False
            if event.key in ("left", "right", "up", "down"):
                event.stop()
                event.prevent_default()
                if event.key == "left":
                    self.app.action_prev_tab()
                elif event.key == "right":
                    self.app.action_next_tab()
                elif event.key == "up":
                    self.post_message(self.History(-1))
                else:
                    self.post_message(self.History(1))
                return

        if event.key == "ctrl+up":
            event.stop()
            event.prevent_default()
            self.post_message(self.History(-1))
        elif event.key == "ctrl+down":
            event.stop()
            event.prevent_default()
            self.post_message(self.History(1))
        elif event.key == "ctrl+left":
            event.stop()
            event.prevent_default()
            self.app.action_prev_tab()
        elif event.key == "ctrl+right":
            event.stop()
            event.prevent_default()
            self.app.action_next_tab()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self._esc_pending = True


class OutputMessage(Message):
    """Decoded server text for a given connection."""

    def __init__(self, conn_id: str, text: str) -> None:
        self.conn_id = conn_id
        self.text = text
        super().__init__()


class StatusMessage(Message):
    """A lifecycle/status notice for a given connection."""

    def __init__(self, conn_id: str, text: str) -> None:
        self.conn_id = conn_id
        self.text = text
        super().__init__()


class StateMessage(Message):
    """A connection-state change for a given connection."""

    def __init__(self, conn_id: str, state: str) -> None:
        self.conn_id = conn_id
        self.state = state
        super().__init__()


class ConnectCommands(Provider):
    """Command-palette entries for connecting to each configured character."""

    def _entries(self):
        """(display, help, conn_id) for every configured character."""
        app: MuckyApp = self.app  # type: ignore[assignment]
        for p in app.profiles:
            scheme = "telnets" if p.tls else "telnet"
            display = f"Connect: {p.char_name}"
            help_text = f"{p.server_name} · {scheme}://{p.host}:{p.port}"
            yield display, help_text, p.id

    async def discover(self) -> Hits:
        # Shown when the palette is opened with no query typed yet.
        for display, help_text, conn_id in self._entries():
            yield DiscoveryHit(
                display,
                partial(self.app._connect, conn_id),
                help=help_text,
            )

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for display, help_text, conn_id in self._entries():
            score = matcher.match(display)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(display),
                    partial(self.app._connect, conn_id),
                    help=help_text,
                )


class MuckyApp(App):
    """The main application."""

    COMMANDS = App.COMMANDS | {ConnectCommands}

    CSS = """
    #bottom {
        dock: bottom;
        height: 4;
    }
    #statusbar {
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
    }
    #inputbar {
        height: 3;
        background: $panel;
    }
    #input {
        width: 1fr;
    }
    #counter {
        width: 9;
        content-align: right middle;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+right", "next_tab", "Next tab"),
        Binding("ctrl+left", "prev_tab", "Prev tab"),
        Binding("f2", "command_palette", "Characters"),
        Binding("f5", "connect", "Connect"),
        Binding("f6", "disconnect", "Disconnect"),
        Binding("pageup", "scroll_output('up')", "Scroll up", show=False),
        Binding("pagedown", "scroll_output('down')", "Scroll down", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._config_path = config.source
        self.profiles = config.profiles
        self._by_id = {p.id: p for p in self.profiles}
        self._conns: dict[str, Connection] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._logs: dict[str, SessionLog] = {}
        self._linebuf: dict[str, str] = {}
        self._flush_timers: dict[str, object] = {}
        self._states: dict[str, str] = {p.id: "idle" for p in self.profiles}
        self._history: dict[str, list[str]] = {p.id: [] for p in self.profiles}
        self._history_idx: dict[str, int] = {}
        self._history_draft: dict[str, str] = {}
        self._unread: dict[str, int] = {p.id: 0 for p in self.profiles}

    # ----- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # Tabs are opened on demand via /connect; start with none.
        yield TabbedContent(id="tabs")
        with Vertical(id="bottom"):
            yield Static("", id="statusbar")
            with Horizontal(id="inputbar"):
                yield CommandInput(
                    id="input", placeholder="Type a command (/help for commands)"
                )
                yield Static("0", id="counter")

    async def on_mount(self) -> None:
        self.title = "mucky"
        for p in self.profiles:
            self._linebuf[p.id] = ""
        self._refresh_statusbar()
        self.query_one("#input", Input).focus()
        for p in self.profiles:
            if p.autoconnect:
                await self._connect(p.id)

    # ----- connection management -----------------------------------------

    def _open_ids(self) -> list[str]:
        """Connection ids of currently open tabs, in tab order."""
        tabs = self.query_one("#tabs", TabbedContent)
        return [pane.id for pane in tabs.query(TabPane) if pane.id]

    async def _open_tab(self, conn_id: str) -> None:
        """Open (and activate) the tab for a connection, creating it if needed."""
        tabs = self.query_one("#tabs", TabbedContent)
        profile = self._by_id[conn_id]
        if conn_id not in self._open_ids():
            self._unread[conn_id] = 0
            await tabs.add_pane(
                TabPane(profile.tab_name, OutputLog(id=f"log-{conn_id}"), id=conn_id)
            )
        tabs.active = conn_id
        self.query_one("#input", Input).focus()

    async def _connect(self, conn_id: str) -> None:
        """Open the tab for a profile and start its connection."""
        await self._open_tab(conn_id)
        self._start_connection(conn_id)

    async def _close_tab(self, conn_id: str) -> None:
        """Remove a connection's tab and cycle to the previous open tab."""
        tabs = self.query_one("#tabs", TabbedContent)
        ids = self._open_ids()
        if conn_id not in ids:
            return
        idx = ids.index(conn_id)
        timer = self._flush_timers.pop(conn_id, None)
        if timer is not None:
            timer.stop()
        self._linebuf[conn_id] = ""
        self._unread[conn_id] = 0
        self._states[conn_id] = "idle"
        log = self._logs.pop(conn_id, None)
        if log is not None:
            log.close()
        await tabs.remove_pane(conn_id)
        remaining = [i for i in ids if i != conn_id]
        if remaining:
            tabs.active = remaining[(idx - 1) % len(remaining)]
            self.query_one("#input", Input).focus()
        else:
            self._refresh_statusbar()

    def _start_connection(self, conn_id: str) -> None:
        if conn_id in self._conns and self._conns[conn_id].connected:
            return
        profile = self._by_id.get(conn_id)
        if profile is None:
            return

        self._logs[conn_id] = SessionLog(self.config.log_dir, profile.char_name)
        self._status(conn_id, f"--- session log: {self._logs[conn_id].path} ---")

        conn = Connection(
            profile,
            on_text=lambda t, cid=conn_id: self.post_message(OutputMessage(cid, t)),
            on_status=lambda t, cid=conn_id: self.post_message(StatusMessage(cid, t)),
            on_state=lambda s, cid=conn_id: self.post_message(StateMessage(cid, s)),
        )
        self._conns[conn_id] = conn
        self._tasks[conn_id] = asyncio.create_task(conn.run())

    async def _stop_connection(self, conn_id: str) -> None:
        conn = self._conns.get(conn_id)
        if conn is not None:
            await conn.close()
        task = self._tasks.pop(conn_id, None)
        if task is not None:
            task.cancel()

    # ----- message handlers ----------------------------------------------

    def on_output_message(self, message: OutputMessage) -> None:
        self._append_text(message.conn_id, message.text)

    def on_status_message(self, message: StatusMessage) -> None:
        self._status(message.conn_id, message.text)

    def on_state_message(self, message: StateMessage) -> None:
        self._states[message.conn_id] = message.state
        if message.conn_id == self._active_conn_id():
            self._refresh_statusbar()

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        self._history_idx.clear()
        self._history_draft.clear()
        active = self._active_conn_id()
        if active is not None:
            self._unread[active] = 0
            self._update_tab_label(active)
            # Show the most recent output when switching to a tab.
            widget = self._rich_log(active)
            if widget is not None:
                widget.call_after_refresh(widget.scroll_end, animate=False)
        self._refresh_statusbar()
        # Any tab change (click, arrows, palette) returns focus to the input.
        self.query_one("#input", Input).focus()

    # ----- status bar -----------------------------------------------------

    def _refresh_statusbar(self) -> None:
        bar = self.query_one("#statusbar", Static)
        conn_id = self._active_conn_id()
        if conn_id is None:
            bar.update("")
            return
        profile = self._by_id.get(conn_id)
        if profile is None:
            bar.update("")
            return
        state = self._states.get(conn_id, "idle")
        label, color = _STATE_DISPLAY.get(state, (state, "white"))
        scheme = "telnets" if profile.tls else "telnet"
        text = Text.assemble(
            (profile.char_name, "bold"),
            f"  {scheme}://{profile.host}:{profile.port}  ",
            ("● ", color),
            (label, color),
        )
        bar.update(text)

    # ----- output rendering ----------------------------------------------

    def _rich_log(self, conn_id: str) -> OutputLog | None:
        try:
            return self.query_one(f"#log-{conn_id}", OutputLog)
        except NoMatches:
            return None

    def _autoscroll(self, widget: OutputLog) -> bool:
        """Whether new output should follow to the bottom.

        Stays put while the user has an active text selection or has scrolled
        up to read back, so mouse highlighting isn't disrupted by live output.
        """
        if self.screen.selections:
            return False
        return widget.is_vertical_scroll_end

    def _render_line(self, conn_id: str, line: str) -> Text | None:
        """Build a renderable line, or ``None`` if a gag drops it.

        Applies ANSI colors, clickable http(s) links, and highlight recoloring.
        Gag matching runs against the visible (ANSI-stripped) text.
        """
        text = Text.from_ansi(line.rstrip("\r"))
        profile = self._by_id[conn_id]
        plain = text.plain
        if any(rx.search(plain) for rx in profile.gags):
            return None
        self._linkify(text)
        for hl in profile.highlights:
            for match in hl.pattern.finditer(plain):
                if match.start() != match.end():
                    text.stylize(hl.style, match.start(), match.end())
        return text

    def _emit_line(self, conn_id: str, widget: OutputLog, line: str, scroll_end: bool) -> None:
        """Render, display, and log one output line (unless gagged)."""
        text = self._render_line(conn_id, line)
        if text is None:
            return
        widget.write(text, scroll_end=scroll_end)
        self._mark_unread(conn_id)
        log = self._logs.get(conn_id)
        if log is not None:
            log.write(text.plain + "\n")

    def _linkify(self, text: Text) -> None:
        """Style any http(s) URLs as clickable links that open the browser."""
        plain = text.plain
        for match in _URL_RE.finditer(plain):
            start, end = match.start(), match.end()
            url = match.group()
            while url and url[-1] in _URL_TRAILING:
                ch = url[-1]
                # Keep a closing bracket that pairs with one inside the URL
                # (e.g. Wikipedia links), strip it only when unbalanced.
                pair = {")": "(", "]": "[", "}": "{"}.get(ch)
                if pair and url.count(pair) >= url.count(ch):
                    break
                url = url[:-1]
                end -= 1
            if not url:
                continue
            text.stylize("underline #6cb6ff", start, end)
            text.stylize(
                Style.from_meta({"@click": f"app.open_link({url!r})"}), start, end
            )

    def _append_text(self, conn_id: str, text: str) -> None:
        """Buffer incoming text and write complete lines, preserving ANSI color."""
        widget = self._rich_log(conn_id)
        if widget is None:
            return
        buffer = self._linebuf.get(conn_id, "") + text
        *lines, remainder = buffer.split("\n")
        scroll_end = self._autoscroll(widget)
        for line in lines:
            self._emit_line(conn_id, widget, line, scroll_end)
        self._linebuf[conn_id] = remainder
        self._schedule_partial_flush(conn_id)

    def _schedule_partial_flush(self, conn_id: str) -> None:
        timer = self._flush_timers.get(conn_id)
        if timer is not None:
            timer.stop()
        self._flush_timers[conn_id] = self.set_timer(
            _PARTIAL_FLUSH_DELAY, lambda cid=conn_id: self._flush_partial(cid)
        )

    def _flush_partial(self, conn_id: str) -> None:
        remainder = self._linebuf.get(conn_id, "")
        if remainder:
            widget = self._rich_log(conn_id)
            if widget is None:
                self._linebuf[conn_id] = ""
                return
            self._emit_line(
                conn_id, widget, remainder, self._autoscroll(widget)
            )
            self._linebuf[conn_id] = ""

    def _status(self, conn_id: str, text: str) -> None:
        # Flush any pending partial line so status notices stay in order.
        self._flush_partial(conn_id)
        widget = self._rich_log(conn_id)
        if widget is None:
            return
        widget.write(
            Text(text, style="italic yellow"), scroll_end=self._autoscroll(widget)
        )
        self._mark_unread(conn_id)

    # ----- input handling -------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#counter", Static).update(str(len(event.value)))

    # ----- command history ------------------------------------------------

    def _record_history(self, conn_id: str, command: str) -> None:
        history = self._history[conn_id]
        if command.strip() and (not history or history[-1] != command):
            history.append(command)
        self._history_idx.pop(conn_id, None)
        self._history_draft.pop(conn_id, None)

    def on_command_input_history(self, message: CommandInput.History) -> None:
        conn_id = self._active_conn_id()
        if conn_id is None:
            return
        history = self._history[conn_id]
        if not history:
            return
        input_widget = self.query_one("#input", CommandInput)
        idx = self._history_idx.get(conn_id)

        if message.step < 0:  # older
            if idx is None:
                self._history_draft[conn_id] = input_widget.value
                idx = len(history) - 1
            else:
                idx = max(0, idx - 1)
        else:  # newer
            if idx is None:
                return
            idx += 1
            if idx >= len(history):
                self._history_idx.pop(conn_id, None)
                input_widget.value = self._history_draft.pop(conn_id, "")
                input_widget.cursor_position = len(input_widget.value)
                return

        self._history_idx[conn_id] = idx
        input_widget.value = history[idx]
        input_widget.cursor_position = len(input_widget.value)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value
        input_widget = self.query_one("#input", Input)
        input_widget.value = ""
        self.query_one("#counter", Static).update("0")

        if command.startswith("/"):
            await self._handle_slash(command)
            return

        conn_id = self._active_conn_id()
        if conn_id is None:
            return

        self._record_history(conn_id, command)

        conn = self._conns.get(conn_id)
        if conn is not None:
            await conn.send(command)
        else:
            self._status(conn_id, "Not connected. Press F5 or use /connect.")

    def _resolve_profile(self, name: str):
        """Find a profile by character name, tab name, or connection id."""
        low = name.lower()
        for p in self.profiles:
            if low in (p.char_name.lower(), p.tab_name.lower()) or name == p.id:
                return p
        return None

    async def _handle_slash(self, command: str) -> None:
        parts = command[1:].split()
        name = parts[0].lower() if parts else ""
        arg = " ".join(parts[1:]) if len(parts) > 1 else None
        conn_id = self._active_conn_id()
        if name in ("connect", "c"):
            await self._cmd_connect(arg)
        elif name in ("disconnect", "dc"):
            if conn_id is None:
                self.notify("No active connection to disconnect.")
                return
            await self._stop_connection(conn_id)
            await self._close_tab(conn_id)
        elif name in ("reconnect", "rc"):
            if conn_id is None:
                self.notify("No active connection to reconnect.")
                return
            await self._stop_connection(conn_id)
            self._start_connection(conn_id)
        elif name == "clear":
            widget = self._rich_log(conn_id) if conn_id else None
            if widget is not None:
                widget.clear()
        elif name == "reload":
            self._cmd_reload()
        elif name in ("quit", "q", "exit"):
            await self.action_quit()
        elif name == "help":
            self.notify(
                "Commands: /connect <character> /disconnect /reconnect "
                "/reload /clear /quit  | Keys: F2 character palette, "
                "Esc then Up/Down history, F5/F6 connect/disconnect, "
                "Ctrl+Left/Right (or Esc then Left/Right) tabs, Ctrl+Q quit  | "
                "Mouse: drag to select, "
                "Cmd/Ctrl+C to copy, click links to open",
                timeout=12,
            )
        else:
            self.notify(f"Unknown command: /{name} (try /help)")

    def _cmd_reload(self) -> None:
        """Re-read the config file and apply it without restarting.

        New characters become available immediately; trigger (gag/highlight)
        and tab-name changes apply to open tabs at once. Connection settings
        (host/port/tls/...) take effect on the next (re)connect. Open tabs whose
        character was removed from the config keep working with their old
        profile until closed.
        """
        if not self._config_path:
            self.notify("No config file to reload.", severity="warning")
            return
        try:
            config = load_config(self._config_path)
        except (FileNotFoundError, ValueError) as exc:
            self.notify(f"Reload failed: {exc}", severity="error", timeout=10)
            return

        self.config = config
        self.profiles = config.profiles
        new_by_id = {p.id: p for p in config.profiles}
        # Preserve profiles for open tabs that vanished, so rendering/labels
        # keep working until those tabs are closed.
        for cid in self._open_ids():
            if cid not in new_by_id and cid in self._by_id:
                new_by_id[cid] = self._by_id[cid]
        self._by_id = new_by_id

        for p in config.profiles:
            self._states.setdefault(p.id, "idle")
            self._history.setdefault(p.id, [])
            self._linebuf.setdefault(p.id, "")
            self._unread.setdefault(p.id, 0)

        # Refresh open tab labels (tab_name may have changed) and the statusbar.
        for cid in self._open_ids():
            self._update_tab_label(cid)
        self._refresh_statusbar()

        self.notify(
            f"Reloaded {self._config_path} — {len(config.profiles)} character(s).",
            timeout=6,
        )

    async def _cmd_connect(self, arg: str | None) -> None:
        if arg is None:
            conn_id = self._active_conn_id()
            if conn_id is not None:
                self._start_connection(conn_id)
            else:
                names = ", ".join(p.char_name for p in self.profiles)
                self.notify(f"Usage: /connect <character>. Available: {names}")
            return
        profile = self._resolve_profile(arg)
        if profile is None:
            self.notify(f"Unknown character: {arg}")
            return
        await self._connect(profile.id)

    # ----- actions / tab navigation --------------------------------------

    def _active_conn_id(self) -> str | None:
        active = self.query_one("#tabs", TabbedContent).active
        return active or None

    def _mark_unread(self, conn_id: str) -> None:
        """Bump the unread counter for an inactive tab and refresh its label."""
        if conn_id == self._active_conn_id():
            return
        self._unread[conn_id] = self._unread.get(conn_id, 0) + 1
        self._update_tab_label(conn_id)

    def _update_tab_label(self, conn_id: str) -> None:
        """Show the unread count as a ``[n]`` suffix on the tab (or none)."""
        name = self._by_id[conn_id].tab_name
        count = self._unread.get(conn_id, 0)
        label = f"{name} [{count}]" if count else name
        try:
            tab = self.query_one("#tabs", TabbedContent).get_tab(conn_id)
        except Exception:
            return
        tab.label = label

    def _cycle_tab(self, step: int) -> None:
        ids = self._open_ids()
        if not ids:
            return
        tabs = self.query_one("#tabs", TabbedContent)
        try:
            i = ids.index(tabs.active)
        except ValueError:
            i = 0
        tabs.active = ids[(i + step) % len(ids)]
        self.query_one("#input", Input).focus()

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def action_prev_tab(self) -> None:
        self._cycle_tab(-1)

    def action_open_link(self, url: str) -> None:
        """Open a clicked http(s) link in the system default browser."""
        self.open_url(url)

    def action_scroll_output(self, direction: str) -> None:
        """Page the active tab's output buffer up or down."""
        conn_id = self._active_conn_id()
        if conn_id is None:
            return
        widget = self._rich_log(conn_id)
        if widget is None:
            return
        if direction == "up":
            widget.scroll_page_up()
        else:
            widget.scroll_page_down()

    def action_connect(self) -> None:
        conn_id = self._active_conn_id()
        if conn_id is not None:
            self._start_connection(conn_id)
        else:
            self.notify("Use /connect <character> to open a connection.")

    async def action_disconnect(self) -> None:
        # F6 drops the connection but keeps the tab open (so F5 can reconnect);
        # use /disconnect to also close the tab.
        conn_id = self._active_conn_id()
        if conn_id is not None:
            await self._stop_connection(conn_id)

    async def action_quit(self) -> None:
        for conn_id in list(self._conns):
            await self._stop_connection(conn_id)
        self.exit()

    def on_unmount(self) -> None:
        for log in self._logs.values():
            log.close()
