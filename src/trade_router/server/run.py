from __future__ import annotations

import os

import uvicorn

from ..core.config import DEFAULT_HOST, DEFAULT_PORT
from .app import create_app


def main() -> None:
    host = os.environ.get("TRADE_ROUTER_HOST", DEFAULT_HOST)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            "Trade Router may only bind to the local loopback interface."
        )
    port = int(os.environ.get("TRADE_ROUTER_PORT", str(DEFAULT_PORT)))
    app = create_app()
    config = uvicorn.Config(
        app, host=host, port=port, log_level="info", access_log=False
    )
    server = uvicorn.Server(config)
    app.state.server = server
    server.run()


if __name__ == "__main__":
    main()
