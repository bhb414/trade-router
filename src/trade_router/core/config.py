from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path


@dataclass(frozen=True, slots=True)
class RouterPaths:
    home: Path
    database: Path
    secrets: Path
    pid: Path
    daemon_log: Path

    @classmethod
    def discover(cls) -> "RouterPaths":
        override = os.environ.get("TRADE_ROUTER_HOME", "").strip()
        home = (
            Path(override).expanduser().resolve()
            if override
            else user_data_path(
                appname="TradeRouter",
                appauthor="bhb414",
                roaming=False,
                ensure_exists=False,
            )
        )
        return cls(
            home=home,
            database=home / "router.db",
            secrets=home / "secrets.dpapi",
            pid=home / "daemon.pid",
            daemon_log=home / "daemon.log",
        )

    def ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8877
API_VERSION = "v1"


def daemon_base_url() -> str:
    host = os.environ.get("TRADE_ROUTER_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(os.environ.get("TRADE_ROUTER_PORT", str(DEFAULT_PORT)))
    return f"http://{host}:{port}"
