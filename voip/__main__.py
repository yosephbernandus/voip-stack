"""
Run the VoIP signaling server.

Usage:
    python -m voip                          # defaults: 0.0.0.0:8080
    python -m voip --host 127.0.0.1 --port 9000
    python -m voip --static-dir /path/to/client
    python -m voip --log-level DEBUG
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from voip.server import SignalingServer


def main() -> None:
    # Default static dir: ../client relative to this file, which resolves to
    # <repo-root>/client/ where index.html lives.
    default_static_dir = str(Path(__file__).parent.parent / "client")

    parser = argparse.ArgumentParser(
        prog="voip",
        description="VoIP signaling server",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind port (default: 8080)",
    )
    parser.add_argument(
        "--static-dir",
        default=default_static_dir,
        help=f"Directory containing client/index.html (default: {default_static_dir})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve static_dir to an absolute path and validate it exists
    static_dir = str(Path(args.static_dir).resolve())
    index_html = Path(static_dir) / "index.html"
    if not index_html.exists():
        logging.warning(
            "index.html not found at %s, browser client will return 404",
            index_html,
        )

    server = SignalingServer(
        host=args.host,
        port=args.port,
        static_dir=static_dir,
    )

    print(f"VoIP signaling server starting on http://{args.host}:{args.port}")
    print(f"Open http://localhost:{args.port} in a browser to connect.")
    if index_html.exists():
        print(f"Serving client from: {static_dir}")

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
