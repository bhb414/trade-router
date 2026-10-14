from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Trade Router Studio.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8890, type=int)
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()

    if not arguments.no_browser:
        url = f"http://{arguments.host}:{arguments.port}"
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "trade_router_studio.app:app",
        host=arguments.host,
        port=arguments.port,
        reload=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
