"""Load server/character configuration from a YAML file.

The config lists ``servers``; each server may contain one or more
``characters``. Every (server, character) pair becomes a connection profile
that is shown as its own tab in the UI.

Example::

    log_dir: ./logs
    servers:
      - name: FurryMUCK
        host: muck.furry.example
        port: 8888
        tls: false
        keepalive: 60
        reconnect: true
        characters:
          - name: Fluffy
            login: "connect Fluffy hunter2"
            autoconnect: true
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rich.errors import StyleSyntaxError
from rich.style import Style


@dataclass
class Highlight:
    """A regex whose matching text is recolored with a Rich style string."""

    pattern: re.Pattern
    style: str


@dataclass
class Profile:
    """A single connection (one character on one server)."""

    id: str
    tab_name: str
    server_name: str
    char_name: str
    host: str
    port: int
    tls: bool = False
    insecure: bool = False
    keepalive: int = 0
    reconnect: bool = False
    login: str | None = None
    autoconnect: bool = True
    encoding: str = "utf-8"
    # Output triggers. Gags drop a whole matching line (from screen and log);
    # highlights recolor the matching span. Each merges global -> server -> char.
    gags: list[re.Pattern] = field(default_factory=list)
    highlights: list[Highlight] = field(default_factory=list)


@dataclass
class Config:
    log_dir: str
    profiles: list[Profile] = field(default_factory=list)
    # Absolute path the config was loaded from, so the app can reload it.
    source: str | None = None


def _slug(text: str) -> str:
    """A Textual-id-safe slug (alphanumeric + underscore) for an id segment."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return s or "x"


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    return bool(value)


def _compile_gags(raw, where: str) -> list[re.Pattern]:
    """Compile a list of gag regex strings, raising on a bad pattern."""
    result: list[re.Pattern] = []
    for item in raw or []:
        try:
            result.append(re.compile(str(item)))
        except re.error as exc:
            raise ValueError(f"Invalid gag regex {item!r} in {where}: {exc}")
    return result


def _compile_highlights(raw, where: str) -> list[Highlight]:
    """Compile a list of highlight mappings.

    Each entry has a 'color' plus either a single 'pattern' (string) or a
    'patterns' group (list of strings) that all share that color. A group is
    expanded into one :class:`Highlight` per pattern.
    """
    result: list[Highlight] = []
    for item in raw or []:
        if not isinstance(item, dict):
            raise ValueError(
                f"Highlight in {where} must be a mapping with 'pattern'/'patterns' "
                f"and 'color', got {item!r}."
            )
        color = item.get("color") or item.get("style")
        if not color:
            raise ValueError(f"Highlight in {where} is missing 'color': {item!r}")
        try:
            Style.parse(str(color))
        except StyleSyntaxError as exc:
            raise ValueError(
                f"Invalid highlight color {color!r} in {where}: {exc}"
            )

        group = item.get("patterns")
        if group is not None:
            if not isinstance(group, list):
                raise ValueError(
                    f"Highlight 'patterns' in {where} must be a list, got {group!r}"
                )
            patterns = group
        else:
            single = item.get("pattern") or item.get("regex")
            if not single:
                raise ValueError(
                    f"Highlight in {where} is missing 'pattern'/'patterns': {item!r}"
                )
            patterns = [single]

        for pattern in patterns:
            if not pattern:
                raise ValueError(
                    f"Highlight in {where} has an empty pattern: {item!r}"
                )
            try:
                compiled = re.compile(str(pattern))
            except re.error as exc:
                raise ValueError(
                    f"Invalid highlight regex {pattern!r} in {where}: {exc}"
                )
            result.append(Highlight(pattern=compiled, style=str(color)))
    return result


def load_config(path: str | Path) -> Config:
    """Parse a YAML config file into a :class:`Config`."""

    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    log_dir = data.get("log_dir", "./logs")
    servers = data.get("servers")
    if not servers:
        raise ValueError(
            f"No 'servers' defined in {path}. See config.example.yaml for the format."
        )

    global_gags = _compile_gags(data.get("gags"), "top level")
    global_highlights = _compile_highlights(data.get("highlights"), "top level")

    profiles: list[Profile] = []
    used_ids: set[str] = set()
    idx = 0
    for server in servers:
        s_name = server.get("name") or server.get("host") or f"server{idx}"
        s_host = server.get("host")
        s_port = int(server.get("port", 23))
        s_tls = _as_bool(server.get("tls"), False)
        s_insecure = _as_bool(server.get("insecure"), False)
        s_keepalive = int(server.get("keepalive") or 0)
        s_reconnect = _as_bool(server.get("reconnect"), False)
        s_encoding = server.get("encoding", "utf-8")
        s_gags = _compile_gags(server.get("gags"), f"server '{s_name}'")
        s_highlights = _compile_highlights(
            server.get("highlights"), f"server '{s_name}'"
        )

        characters = server.get("characters") or [{}]
        for char in characters:
            c_name = char.get("name") or s_name
            host = char.get("host", s_host)
            if not host:
                raise ValueError(
                    f"Server '{s_name}' / character '{c_name}' has no host."
                )
            where = f"server '{s_name}' / character '{c_name}'"
            # Stable, position-independent id so a tab survives reordering or
            # additions elsewhere in the config across a /reload.
            base_id = f"conn_{_slug(s_name)}_{_slug(c_name)}"
            conn_id = base_id
            dedup = 2
            while conn_id in used_ids:
                conn_id = f"{base_id}_{dedup}"
                dedup += 1
            used_ids.add(conn_id)
            profiles.append(
                Profile(
                    id=conn_id,
                    tab_name=(
                        char.get("display_name")
                        or char.get("tab_name")
                        or c_name
                    ),
                    server_name=s_name,
                    char_name=c_name,
                    host=host,
                    port=int(char.get("port", s_port)),
                    tls=_as_bool(char.get("tls"), s_tls),
                    insecure=_as_bool(char.get("insecure"), s_insecure),
                    keepalive=int(char.get("keepalive", s_keepalive) or 0),
                    reconnect=_as_bool(char.get("reconnect"), s_reconnect),
                    login=char.get("login"),
                    autoconnect=_as_bool(char.get("autoconnect"), True),
                    encoding=char.get("encoding", s_encoding),
                    gags=global_gags + s_gags + _compile_gags(char.get("gags"), where),
                    highlights=global_highlights
                    + s_highlights
                    + _compile_highlights(char.get("highlights"), where),
                )
            )
            idx += 1

    return Config(log_dir=log_dir, profiles=profiles, source=str(path))
