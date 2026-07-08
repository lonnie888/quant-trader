#!/usr/bin/env python3
"""Entry point: start the Flask dev server on 0.0.0.0:5050 with auto-fallback."""

import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("web")

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import create_app

app = create_app()


def find_port(start: int = 5050, max_attempts: int = 10) -> int:
    import socket

    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port found in range {start}-{start + max_attempts - 1}")


if __name__ == "__main__":
    port = find_port()
    log.info("Starting Quant Trader Web Dashboard on 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)