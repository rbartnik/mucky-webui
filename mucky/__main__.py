"""Command-line entry point for mucky."""

from __future__ import annotations

import argparse
import sys

from .app import MuckyApp
from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mucky", description="A terminal MUCK/MUD client with tabbed connections."
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml).",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    MuckyApp(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
