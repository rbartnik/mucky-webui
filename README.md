# mucky

A terminal MUCK/MUD client with tabbed connections, mouse text-selection,
ANSI color, clickable links, per-character session logs, and configurable
gag/highlight triggers. Built on [Textual](https://textual.textualize.io/).

## Features

- **Tabbed connections** — one tab per character, opened on demand.
- **Character palette** — `F2` (or `Ctrl+P`) opens a searchable dropdown of all
  configured characters; pick one to connect.
- **Mouse selection & copy** — drag to select output, `⌘C`/`Ctrl+C` to copy.
- **Clickable links** — `http(s)` URLs in output open in your default browser.
- **ANSI color** rendering and **per-character session logs** (ANSI stripped).
- **Triggers** — `gags` (drop matching lines) and `highlights` (recolor matches),
  configurable globally / per-server / per-character.
- **Unread badges** — inactive tabs show a `[n]` count of new lines.
- **Command history**, partial-line (prompt) handling, TLS, keepalive, and
  exponential-backoff reconnect.

## Requirements

- Python **3.10+**
- A terminal that supports a TUI. For copy-to-clipboard via mouse selection,
  enable OSC 52 clipboard access (e.g. iTerm2 → Settings → General → Selection →
  "Applications in terminal may access clipboard").

## Install

### With pipx (recommended)

```bash
git clone <your-repo-url> mucky
cd mucky
pipx install .
```

This puts a `mucky` command on your PATH. Update later with
`pipx install --force .`.

### With a virtualenv (development)

```bash
git clone <your-repo-url> mucky
cd mucky
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Configure

Connection settings and credentials live in a YAML config. `config.yaml` is
git-ignored (it contains login passwords), so copy the template and edit it:

```bash
cp config.example.yaml config.yaml
# edit hosts, characters, logins, triggers
```

See [`config.example.yaml`](config.example.yaml) for all options. Key points:

- Each `(server, character)` pair defines a connection you can open as a tab.
- Tabs are **not** shown at startup — open one with `/connect <character>`, or
  set `autoconnect: true` to open it automatically.
- `log_dir` (default `./logs`) is created on first run; one folder per character.

## Run

```bash
mucky -c config.yaml
# or, from a source checkout without installing:
.venv/bin/python -m mucky -c config.yaml
```

## Usage

### Commands

| Command | Action |
| --- | --- |
| `/connect <character>` | Open the tab for a character and connect |
| `/connect` | Reconnect the active tab |
| `/disconnect` | Disconnect **and close** the active tab |
| `/reconnect` | Drop and reopen the active tab's connection |
| `/reload` | Re-read `config.yaml` without restarting (see below) |
| `/clear` | Clear the active tab's output |
| `/quit` | Quit (disconnects all open connections first) |
| `/help` | Show a command/key reference |

### Keys

| Key | Action |
| --- | --- |
| `F2` (or `Ctrl+P`) | Open the character palette — a searchable dropdown of all configured characters; pick one to connect |
| `F5` / `F6` | Connect / disconnect the active tab (keeps the tab open) |
| `Ctrl+Left` / `Ctrl+Right` | Previous / next tab |
| `Esc` then `Left` / `Right` | Previous / next tab |
| `Esc` then `Up` / `Down` | Command history back / forward |
| `PageUp` / `PageDown` | Scroll the output buffer |
| `Ctrl+Q` | Quit |

> `F6` disconnects but keeps the tab so `F5` can reconnect it; use
> `/disconnect` to also close the tab.

## Running on a server and attaching from anywhere

`mucky serve` keeps your connections open and your session logs running on
one machine (like running the terminal client under `screen`), while clients
on other machines attach and detach without interrupting them.

1. Add a `server:` block with a long random `token` to `config.yaml` (see
   `config.example.yaml`). The server refuses to start without one.
2. Start it on the server, under systemd or `screen`:
   ```bash
   mucky serve -c config.yaml
   ```
   It listens on `127.0.0.1:8765` by default. Characters with
   `autoconnect: true` connect at startup.
3. Reach it from another machine over an SSH tunnel
   (`ssh -L 8765:localhost:8765 yourserver`) or Tailscale.

Every attached client gets the open characters, their command history and
recent scrollback, then live output. Several clients can be attached at
once. Closing a client, or typing `/quit` in it, only detaches that client.
Stopping the server process closes the MUCK connections.

### In a browser

With the tunnel up, open <http://localhost:8765/> and enter the token. The page
shows each open character as a tab with live, colored output and an input line
(Up/Down for history, Alt+1..9 or Ctrl+PageUp/PageDown to switch characters,
**+** to open another character). It reattaches on its own if the server
restarts or the network drops. `/quit` in the page detaches just that page.

### From a terminal

`mucky attach` is a plain line-mode client:

```bash
mucky attach --url ws://127.0.0.1:8765/ws   # token from config, MUCKY_TOKEN, or a prompt
```

Type to send to the current character, `>Name` to switch character, and
Ctrl+D to detach.

## Reloading config without a restart

`/reload` re-reads the config file you started with and applies it live:

- **New characters** become available immediately (in `/connect` and the `F2`
  palette).
- **Trigger (gag/highlight) and tab-name changes** apply to already-open tabs at
  once.
- **Connection settings** (host, port, TLS, keepalive, …) take effect on the
  next connect/reconnect, not on currently-live connections.
- If the edited file is **invalid**, the reload is rejected with an error and the
  running config is left untouched.
- A tab whose character was **removed** from the config keeps working until you
  close it.

Connection tabs are identified by `server name + character name`, so reordering
or adding entries elsewhere in the file won't disturb an open tab.

## Development

The connection handling, triggers, history and slash commands live in
`mucky/core.py` (`ClientCore`), which has no Textual dependency. The Textual
app in `mucky/app.py` subscribes to the core's events and only handles display
and keys. `mucky/server.py` exposes the same core over a WebSocket; its module
docstring describes the message protocol. Run the tests with:

```bash
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

## Deploying to another machine

`mucky` is a standard pip-installable package, so the install steps above work
anywhere with Python 3.10+. Notes:

- **Don't copy `.venv/`** — it has machine-specific paths. Recreate it (or use
  `pipx`) on the target.
- **`config.yaml` is git-ignored** — recreate it from `config.example.yaml` on
  the target machine.
- To ship without the repo, build a wheel and copy just that:
  ```bash
  python -m build                 # produces dist/mucky-0.1.0-py3-none-any.whl
  # on the target:
  pipx install ./mucky-0.1.0-py3-none-any.whl
  ```
