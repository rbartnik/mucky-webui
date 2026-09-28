"""Command-line entry point for mucky."""

from __future__ import annotations

import argparse
import sys

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mucky", description="A terminal MUCK/MUD client with tabbed connections."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["tui", "serve", "attach"],
        default="tui",
        help=(
            "tui (default): run the terminal client. serve: keep connections "
            "and logs running as a server that clients attach to. attach: a "
            "plain line-mode client for a running server."
        ),
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml).",
    )
    parser.add_argument("--host", help="serve: address to listen on (overrides config).")
    parser.add_argument("--port", type=int, help="serve: port to listen on (overrides config).")
    parser.add_argument(
        "--url",
        default="ws://127.0.0.1:8765/ws",
        help="attach: server WebSocket URL (default: ws://127.0.0.1:8765/ws).",
    )
    args = parser.parse_args(argv)

    if args.command == "attach":
        from .attach import attach

        # Use the config's token when the config is at hand (e.g. attaching on
        # the server itself); otherwise MUCKY_TOKEN or a prompt.
        token = None
        try:
            token = load_config(args.config).server.token
        except (FileNotFoundError, ValueError):
            pass
        return attach(args.url, token)

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.command == "serve":
        from .server import serve

        try:
            return serve(config, host=args.host, port=args.port)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    from .app import MuckyApp

    MuckyApp(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
