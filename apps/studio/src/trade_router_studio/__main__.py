from __future__ import annotations

import argparse
import os
import threading
import webbrowser

import uvicorn

from .app import app


RESTART_EXIT_CODE = 75
UPDATE_EXIT_CODE = 76


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Trade Router Studio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8890, type=int)
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    os.environ["TRADE_ROUTER_URL"] = f"http://{arguments.host}:{arguments.port}"

    if not arguments.no_browser:
        url = f"http://{arguments.host}:{arguments.port}"
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    config = uvicorn.Config(
        app,
        host=arguments.host,
        port=arguments.port,
        reload=False,
        access_log=False,
    )
    server = uvicorn.Server(config)

    def process_control(action: str) -> None:
        app.state.process_exit_code = (
            RESTART_EXIT_CODE
            if action == "restart"
            else UPDATE_EXIT_CODE if action == "update" else 0
        )
        server.should_exit = True

    app.state.process_exit_code = 0
    app.state.process_control = process_control
    try:
        server.run()
    except KeyboardInterrupt:
        # Ctrl+C is the normal way to stop the single visible command window.
        # Uvicorn has already completed its application shutdown at this point.
        pass
    raise SystemExit(app.state.process_exit_code)


if __name__ == "__main__":
    main()
