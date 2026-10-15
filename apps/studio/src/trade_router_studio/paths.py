from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


EXAMPLE_STRATEGY = '''"""연결·계좌 설정과 공통 시세 API 사용법을 보여주는 읽기 전용 예제.

아래 세 값만 자신의 설정에 맞게 바꾸면 실행할 수 있습니다. App Key, App Secret,
접근 토큰, 전체 계좌번호는 전략 파일에 적지 않습니다.
"""

from trade_router import Strategy, StrategyConfig


# 설정 > 계좌 탭의 "계좌 별칭"을 그대로 입력합니다.
# 계좌번호가 아니며, 예: "주식 주계좌"
TRADING_ACCOUNT = "등록한 계좌 별칭"

# 설정 > API 키 탭의 등록된 API 이름을 그대로 입력합니다.
# App Key가 아니며, 예: "KIS 주식 API"
MARKET_DATA_API = "등록한 API 이름"

# 조회할 국내주식 6자리 종목코드입니다. 삼성전자는 005930입니다.
SYMBOL = "005930"


class ExampleStrategy(Strategy):
    config = StrategyConfig(
        id="example-strategy",
        trading_account=TRADING_ACCOUNT,
        # "primary"는 이 전략 안에서만 쓰는 이름입니다. 여러 증권사의
        # 시세를 함께 쓸 때 primary, secondary처럼 항목을 추가할 수 있습니다.
        market_sources={"primary": MARKET_DATA_API},
        required_capabilities={"primary": ("quotes", "bars")},
    )

    async def on_start(self, ctx):
        ctx.scheduler.on_bar_close(
            "3m",
            schedule_id="morning-3m",
            between=("09:00", "10:00"),
        )
        ctx.scheduler.every(
            seconds=30,
            schedule_id="daytime-30s",
            between=("10:00", "15:00"),
            aligned=True,
        )
        ctx.log.info(
            f"예제 시작 · 계좌 {TRADING_ACCOUNT} · 시세 API {MARKET_DATA_API}"
        )

    async def on_bar(self, ctx, event):
        bars = await ctx.market.source("primary").bars(SYMBOL, "3m", count=55)
        if bars:
            ctx.log.info(
                f"{event.schedule_id}: 완성 3분봉 {len(bars)}개, "
                f"최근 종가 {bars[-1].close}"
            )

    async def on_timer(self, ctx, event):
        quote = await ctx.market.source("primary").quote(SYMBOL)
        ctx.log.info(f"{event.schedule_id}: {SYMBOL} 현재가 {quote.price}")

    async def on_stop(self, ctx):
        ctx.log.info("전략을 종료합니다.")
'''


PREVIOUS_EXAMPLE_STRATEGY = '''"""시간대별 중앙 스케줄과 공통 봉 API를 사용하는 예제."""

from trade_router import Strategy, StrategyConfig


class ExampleStrategy(Strategy):
    config = StrategyConfig(
        id="example-strategy",
        trading_account="등록한 계좌 별칭",
        market_sources={"primary": "등록한 API 연결 이름"},
    )

    async def on_start(self, ctx):
        ctx.scheduler.on_bar_close(
            "3m",
            schedule_id="morning-3m",
            between=("09:00", "10:00"),
        )
        ctx.scheduler.every(
            seconds=30,
            schedule_id="daytime-30s",
            between=("10:00", "15:00"),
            aligned=True,
        )
        ctx.log.info("전략을 시작했습니다.")

    async def on_bar(self, ctx, event):
        bars = await ctx.market.bars("005930", "3m", count=55)
        if bars:
            ctx.log.info(
                f"{event.schedule_id}: 완성 3분봉 {len(bars)}개, "
                f"최근 종가 {bars[-1].close}"
            )

    async def on_timer(self, ctx, event):
        quote = await ctx.market.quote("005930")
        ctx.log.info(f"{event.schedule_id}: 삼성전자 현재가 {quote.price}")

    async def on_stop(self, ctx):
        ctx.log.info("전략을 종료합니다.")
'''


BLOCK_EXAMPLE_STRATEGY = '''"""Trade Router가 관리하는 블록 전략 예제 실행 파일."""

from trade_router import Strategy, StrategyConfig


STRATEGY_ID = "block-example"
TRADING_ACCOUNT = "등록한 계좌 별칭"
MARKET_SOURCE = "등록한 API 연결 이름"
SYMBOL = "005930"
BAR_INTERVAL = "3m"
BAR_COUNT = 55


class BlockExampleStrategy(Strategy):
    config = StrategyConfig(
        id=STRATEGY_ID,
        trading_account=TRADING_ACCOUNT,
        market_sources={"primary": MARKET_SOURCE},
        required_capabilities={"primary": ("quotes", "bars")},
    )

    async def on_start(self, ctx):
        ctx.scheduler.on_bar_close(
            BAR_INTERVAL,
            schedule_id="regular-bars",
            between=("09:00", "15:20"),
        )
        ctx.log.info("블록 예제 전략을 시작했습니다.")

    async def on_bar(self, ctx, event):
        bars = await ctx.market.bars(SYMBOL, BAR_INTERVAL, count=BAR_COUNT)
        if bars:
            ctx.log.info(
                f"{event.schedule_id}: 완성 봉 {len(bars)}개 · "
                f"최근 종가 {bars[-1].close}"
            )

    async def on_stop(self, ctx):
        ctx.log.info("블록 예제 전략을 종료합니다.")
'''


BLOCK_EXAMPLE_METADATA = '''{
  "format": 1,
  "type": "block"
}
'''


LEGACY_EXAMPLE_STRATEGY = '''"""Trade Router strategy example.

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


# v0.1.4 and earlier created a personal KIS condition strategy in every new
# workspace. Remove only the untouched bundled copy during an upgrade.
OBSOLETE_KIS_EXAMPLE_SHA256 = (
    "5c4ea469c5a251f4084d45f3fede952f1162e2a00579ee993ec365201ac0be4f"
)


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
        elif example.is_file():
            # Updates preserve user strategies. Replace only the exact obsolete
            # bundled sample; any user-edited source remains untouched.
            existing = example.read_text(encoding="utf-8")
            if existing.strip() in {
                LEGACY_EXAMPLE_STRATEGY.strip(),
                PREVIOUS_EXAMPLE_STRATEGY.strip(),
            }:
                example.write_text(EXAMPLE_STRATEGY, encoding="utf-8")
        obsolete_kis_example = self.strategies / "kis_condition_bollinger.py"
        if obsolete_kis_example.is_file():
            existing = obsolete_kis_example.read_text(encoding="utf-8")
            digest = hashlib.sha256(existing.encode("utf-8")).hexdigest()
            if digest == OBSOLETE_KIS_EXAMPLE_SHA256:
                obsolete_kis_example.unlink()

        block_example = self.strategies / "블록 예제.py"
        if not block_example.exists():
            block_example.write_text(BLOCK_EXAMPLE_STRATEGY, encoding="utf-8")
        block_metadata = block_example.with_suffix(".strategy.json")
        if (
            not block_metadata.exists()
            and block_example.read_text(encoding="utf-8").strip()
            == BLOCK_EXAMPLE_STRATEGY.strip()
        ):
            block_metadata.write_text(BLOCK_EXAMPLE_METADATA, encoding="utf-8")
