"""Per-character session logging.

Each connection opens one log file in a character-specific folder, named by
the date/time the connection was established. ANSI color codes are stripped so
logs stay readable in any text viewer.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(name: str) -> str:
    cleaned = _UNSAFE_RE.sub("_", name).strip("_")
    return cleaned or "unnamed"


class SessionLog:
    """Append-only log file for a single connection session."""

    def __init__(self, base_dir: str | Path, character_name: str) -> None:
        self.dir = Path(base_dir).expanduser() / _sanitize(character_name)
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.path = self.dir / f"{stamp}.log"
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, text: str) -> None:
        """Write text with ANSI escape sequences removed."""
        self._fh.write(_ANSI_RE.sub("", text))
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
