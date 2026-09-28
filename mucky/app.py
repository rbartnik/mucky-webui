"""Textual UI: tabbed connections, output pane, and an input bar with char count."""

from __future__ import annotations

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


class CoreMessage(Message):
    """Carries a ``ClientCore`` event onto Textual's message queue."""

    def __init__(self, event: Event) -> None:
        self.event = event
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
        self.core = ClientCore(config)
        # Per-viewer state; everything else lives in the core.
        self._history_idx: dict[str, int] = {}
        self._history_draft: dict[str, str] = {}
        self._unread: dict[str, int] = {}

    @property
    def profiles(self) -> list[Profile]:
        return self.core.profiles

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
        self.core.subscribe(lambda event: self.post_message(CoreMessage(event)))
        self._refresh_statusbar()
        self.query_one("#input", Input).focus()
        await self.core.start()

    # ----- tabs -----------------------------------------------------------

    def _open_ids(self) -> list[str]:
        """Connection ids of currently open tabs, in tab order."""
        tabs = self.query_one("#tabs", TabbedContent)
        return [pane.id for pane in tabs.query(TabPane) if pane.id]

    def _activate(self, conn_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = conn_id
        self.query_one("#input", Input).focus()

    async def _connect(self, conn_id: str) -> None:
        """Show the tab for a profile and start its connection."""
        if conn_id in self._open_ids():
            self._activate(conn_id)
        self.core.connect(conn_id)

    async def _add_tab(self, conn_id: str) -> None:
        """Create (and activate) the tab for a newly opened session."""
        profile = self.core.profile(conn_id)
        if profile is None:
            return
        if conn_id not in self._open_ids():
            self._unread[conn_id] = 0
            tabs = self.query_one("#tabs", TabbedContent)
            await tabs.add_pane(
                TabPane(profile.tab_name, OutputLog(id=f"log-{conn_id}"), id=conn_id)
            )
        self._activate(conn_id)

    async def _close_tab(self, conn_id: str) -> None:
        """Remove a session's tab and cycle to the previous open tab."""
        tabs = self.query_one("#tabs", TabbedContent)
        ids = self._open_ids()
        if conn_id not in ids:
            return
        idx = ids.index(conn_id)
        self._unread[conn_id] = 0
        await tabs.remove_pane(conn_id)
        remaining = [i for i in ids if i != conn_id]
        if remaining:
            self._activate(remaining[(idx - 1) % len(remaining)])
        else:
            self._refresh_statusbar()

    # ----- core events ----------------------------------------------------

    async def on_core_message(self, message: CoreMessage) -> None:
        event = message.event
        if isinstance(event, LinesAdded):
            self._show_lines(event.conn_id, event.lines)
        elif isinstance(event, StateChanged):
            if event.conn_id == self._active_conn_id():
                self._refresh_statusbar()
        elif isinstance(event, SessionOpened):
            await self._add_tab(event.conn_id)
        elif isinstance(event, FocusRequested):
            if event.conn_id in self._open_ids():
                self._activate(event.conn_id)
        elif isinstance(event, SessionClosed):
            await self._close_tab(event.conn_id)
        elif isinstance(event, Cleared):
            widget = self._rich_log(event.conn_id)
            if widget is not None:
                widget.clear()
        elif isinstance(event, ConfigReloaded):
            # Tab names may have changed.
            for cid in self._open_ids():
                self._update_tab_label(cid)
            self._refresh_statusbar()
        elif isinstance(event, Notice):
            kwargs = {} if event.timeout is None else {"timeout": event.timeout}
            self.notify(event.text, severity=event.severity, **kwargs)
        elif isinstance(event, HelpRequested):
            self.notify(
                "Commands: /connect <character> /disconnect /reconnect "
                "/reload /clear /quit  | Keys: F2 character palette, "
                "Esc then Up/Down history, F5/F6 connect/disconnect, "
                "Ctrl+Left/Right (or Esc then Left/Right) tabs, Ctrl+Q quit  | "
                "Mouse: drag to select, "
                "Cmd/Ctrl+C to copy, click links to open",
                timeout=12,
            )
        elif isinstance(event, QuitRequested):
            await self.action_quit()

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
        profile = self.core.profile(conn_id) if conn_id else None
        if profile is None:
            bar.update("")
            return
        state = self.core.state(conn_id)
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

    def _show_lines(self, conn_id: str, lines: list[Line]) -> None:
        widget = self._rich_log(conn_id)
        if widget is None:
            return
        scroll_end = self._autoscroll(widget)
        for line in lines:
            widget.write(to_rich_text(line), scroll_end=scroll_end)
            self._mark_unread(conn_id)

    # ----- input handling -------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#counter", Static).update(str(len(event.value)))

    def on_command_input_history(self, message: CommandInput.History) -> None:
        conn_id = self._active_conn_id()
        if conn_id is None:
            return
        history = self.core.history(conn_id)
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
        conn_id = self._active_conn_id()
        if conn_id is not None and not command.startswith("/"):
            self._history_idx.pop(conn_id, None)
            self._history_draft.pop(conn_id, None)
        await self.core.submit(conn_id, command)

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
        profile = self.core.profile(conn_id)
        if profile is None:
            return
        count = self._unread.get(conn_id, 0)
        label = f"{profile.tab_name} [{count}]" if count else profile.tab_name
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
        self._activate(ids[(i + step) % len(ids)])

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
            self.core.start_connection(conn_id)
        else:
            self.notify("Use /connect <character> to open a connection.")

    async def action_disconnect(self) -> None:
        # F6 drops the connection but keeps the tab open (so F5 can reconnect);
        # use /disconnect to also close the tab.
        conn_id = self._active_conn_id()
        if conn_id is not None:
            await self.core.stop_connection(conn_id)

    async def action_quit(self) -> None:
        await self.core.shutdown()
        self.exit()

    async def on_unmount(self) -> None:
        await self.core.shutdown()


def to_rich_text(line: Line) -> Text:
    """Render a core ``Line`` as Rich ``Text`` with colors, links and highlights."""
    if line.status:
        return Text(line.plain, style="italic yellow")
    text = Text.from_ansi(line.ansi)
    for start, end, url in line.links:
        text.stylize("underline #6cb6ff", start, end)
        text.stylize(Style.from_meta({"@click": f"app.open_link({url!r})"}), start, end)
    for start, end, style in line.highlights:
        text.stylize(style, start, end)
    return text
