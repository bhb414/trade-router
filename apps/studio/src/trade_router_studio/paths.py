from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


EXAMPLE_STRATEGY = '''"""Trade Router strategy example.

This file is a normal Python program. Broker APIs will be injected here as the
router adapters are implemented. For now it demonstrates start, logging and stop.
"""

from datetime import datetime
import time


print("strategy started", flush=True)

while True:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{now}] waiting for a broker connection", flush=True)
    time.sleep(5)
'''


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    root: Path
    strategies: Path
    logs: Path
    data: Path
    config: Path

    @classmethod
    def discover(cls) -> "WorkspacePaths":
        configured = os.environ.get("TRADE_ROUTER_HOME")
        if configured:
            root = Path(configured).expanduser()
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            root = (
                Path(local_app_data) / "TradeRouter"
                if local_app_data
                else Path.home() / ".trade-router"
            )
        root = root.resolve()
        return cls(
            root=root,
            strategies=root / "strategies",
            logs=root / "logs",
            data=root / "data",
            config=root / "config",
        )

    def ensure(self) -> None:
        for path in (self.root, self.strategies, self.logs, self.data, self.config):
            path.mkdir(parents=True, exist_ok=True)
        example = self.strategies / "example_strategy.py"
        if not any(self.strategies.glob("*.py")):
            example.write_text(EXAMPLE_STRATEGY, encoding="utf-8")
