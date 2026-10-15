from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


EXAMPLE_STRATEGY = '''"""시간대별 중앙 스케줄과 공통 봉 API를 사용하는 예제."""

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


KIS_CONDITION_BOLLINGER_STRATEGY = '''"""KIS HTS 조건검색 매수 + 3분봉 볼린저 상단 이탈 매도 예제.

조건검색은 KIS 실전 API 연결에서만 제공됩니다. 주문 계좌는 별도로 선택할 수
있으므로, 실전 조건검색 결과를 KIS 모의 계좌에서 시험하는 구성도 가능합니다.
"""

import asyncio
from datetime import datetime, time
from decimal import Decimal

from trade_router import (
    OrderStatus,
    RiskLimits,
    RouterError,
    Strategy,
    StrategyConfig,
    Venue,
)


# 설정에서 등록한 이름과 HTS 조건식 번호를 입력하세요.
TRADING_ACCOUNT = "등록한 거래 계좌 별칭"
CONDITION_SOURCE = "등록한 KIS 실전 API 연결 이름"
HTS_USER_ID = "등록한 HTS ID"
CONDITION_ID = "0"

BUY_BUDGET_PER_SYMBOL = Decimal("50000")
TOTAL_OPERATING_BUDGET = Decimal("150000")
MAX_NEW_BUYS_PER_SCAN = 5
BOLLINGER_PERIOD = 55
BOLLINGER_STDDEV = Decimal("1.04")
TAKE_PROFIT_MULTIPLIER = Decimal("1.03")
STOP_LOSS_MULTIPLIER = Decimal("0.97")


class KisConditionBollingerStrategy(Strategy):
    config = StrategyConfig(
        id="kis-condition-bollinger",
        trading_account=TRADING_ACCOUNT,
        market_sources={"primary": CONDITION_SOURCE},
        required_capabilities={"primary": ("conditions", "quotes", "bars")},
        limits=RiskLimits(max_exposure=TOTAL_OPERATING_BUDGET),
    )

    async def on_start(self, ctx):
        ctx.scheduler.every(
            seconds=30,
            schedule_id="condition-scan",
            between=("09:00", "15:00"),
            aligned=True,
        )
        ctx.scheduler.on_bar_close(
            "3m",
            schedule_id="bollinger-sell",
            between=("09:00", "15:20"),
        )
        ctx.scheduler.daily_at(
            "15:20",
            schedule_id="close-all",
        )
        ctx.scheduler.every(
            seconds=30,
            schedule_id="close-retry",
            between=("15:20:30", "15:30"),
            aligned=True,
        )
        ctx.scheduler.daily_at(
            "15:30:05",
            schedule_id="close-confirm",
        )
        now = datetime.now().astimezone()
        if time(15, 20) <= now.time().replace(tzinfo=None) < time(15, 30):
            await self._liquidate_owned_positions(
                ctx,
                now,
                reason="15:20 청산 시각 이후 시작 보완",
            )
        elif time(15, 30) <= now.time().replace(tzinfo=None) < time(15, 31):
            await self._liquidate_owned_positions(
                ctx,
                now,
                reason="장 종료 후 청산 결과 확인",
                submit_orders=False,
            )
        conditions = await ctx.discovery.conditions(HTS_USER_ID)
        selected = next((item for item in conditions if item.id == CONDITION_ID), None)
        if selected is None:
            available = ", ".join(f"{item.id}:{item.name}" for item in conditions)
            raise RuntimeError(
                f"조건식 {CONDITION_ID}을 찾지 못했습니다. 사용 가능: {available or '없음'}"
            )
        ctx.log.info(f"조건식 연결: {selected.id} · {selected.name}")

    def _sync_tracked_positions(self, ctx, balance):
        positions = {item.instrument.code: item for item in balance.positions}
        tracked = dict(ctx.state.get("strategy_positions", {}))
        if not ctx.state.get("ownership_initialized", False):
            for position in balance.positions:
                if (
                    position.strategy_quantity is not None
                    and position.strategy_quantity > 0
                ):
                    tracked[position.instrument.code] = str(
                        min(position.strategy_quantity, position.quantity)
                    )
            ctx.state.set("ownership_initialized", True)

        for code in list(tracked):
            position = positions.get(code)
            if position is None or position.quantity <= 0:
                tracked.pop(code, None)
                continue
            tracked_quantity = Decimal(str(tracked[code]))
            if position.strategy_quantity is not None:
                tracked_quantity = min(
                    tracked_quantity,
                    position.strategy_quantity,
                )
            tracked_quantity = min(tracked_quantity, position.quantity)
            if tracked_quantity <= 0:
                tracked.pop(code, None)
            else:
                tracked[code] = str(tracked_quantity)
        ctx.state.set("strategy_positions", tracked)
        return tracked

    async def _liquidate_owned_positions(
        self, ctx, scheduled_at, *, reason, submit_orders=True
    ):
        try:
            balance = await ctx.account.balance()
            open_orders = await ctx.account.open_orders()
        except RouterError as exc:
            ctx.log.warning(
                f"전량 청산 확인 보류 [{exc.code.value}]: {exc.message} · "
                "장 마감 전 다음 청산 확인에서 재시도합니다."
            )
            return

        positions = {item.instrument.code: item for item in balance.positions}
        pending_codes = {
            item.instrument.code for item in open_orders if item.remaining_quantity > 0
        }
        if open_orders:
            ctx.log.info(
                "전량 청산 확인 중인 계좌 미체결: "
                + ", ".join(
                    f"{item.instrument.code} {item.side.value} "
                    f"{item.remaining_quantity}주"
                    for item in open_orders
                    if item.remaining_quantity > 0
                )
            )
        tracked = self._sync_tracked_positions(ctx, balance)

        submitted = 0
        for code in list(tracked):
            position = positions.get(code)
            if position is None or position.quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if position.strategy_quantity is not None and position.strategy_quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if code in pending_codes:
                ctx.log.info(f"{code} 전량 청산 대기: 계좌에 미체결 주문이 있습니다.")
                continue

            if not submit_orders:
                ctx.log.warning(
                    f"{code} 장 종료 후 잔여 확인: 전략 소유 기록 "
                    f"{tracked[code]}주 · 실제 보유 {position.quantity}주"
                )
                continue

            tracked_quantity = (
                position.strategy_quantity
                if position.strategy_quantity is not None
                else Decimal(str(tracked[code]))
            )
            quantity = min(tracked_quantity, position.available_quantity)
            if quantity <= 0:
                ctx.log.warning(
                    f"{code} 전량 청산 대기: 전략 소유 {tracked_quantity}주 · "
                    f"계좌 매도 가능 {position.available_quantity}주 · "
                    f"실제 보유 {position.quantity}주"
                )
                continue
            try:
                await asyncio.sleep(1.1)
                order = await ctx.orders.market_sell(
                    code=code,
                    quantity=quantity,
                    route="KRX",
                    client_order_id=(
                        f"close-{code}-{scheduled_at:%Y%m%d%H%M%S}-sell"
                    ),
                )
            except RouterError as exc:
                ctx.log.error(
                    f"{code} 전량 청산 실패 [{exc.code.value}]: {exc.message}"
                )
                continue
            submitted += 1
            ctx.log.info(
                f"{code} {reason} · 전략 소유 {quantity}주 시장가 매도 요청: "
                f"{order.status.value}"
            )
            if order.status is OrderStatus.FILLED:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)

        if submitted == 0 and reason == "15:20 정시 전량 청산":
            ctx.log.info("15:20 전량 청산 확인: 새로 제출할 전략 소유 수량이 없습니다.")

    async def on_timer(self, ctx, event):
        if event.schedule_id in {"close-all", "close-retry", "close-confirm"}:
            await self._liquidate_owned_positions(
                ctx,
                event.scheduled_at,
                reason=(
                    "15:20 정시 전량 청산"
                    if event.schedule_id == "close-all"
                    else (
                        "15:20 전량 청산 재확인"
                        if event.schedule_id == "close-retry"
                        else "장 종료 후 청산 결과 확인"
                    )
                ),
                submit_orders=event.schedule_id != "close-confirm",
            )
            return
        if event.schedule_id != "condition-scan":
            return
        matches = await ctx.discovery.matches(HTS_USER_ID, CONDITION_ID)
        ctx.log.info(f"조건검색 {len(matches)}종목 · 신규 매수 후보를 확인합니다.")
        if not matches:
            # 후보가 없을 때는 계좌 REST 조회를 생략해 3분봉 매도 판단과의 충돌을 피한다.
            return
        try:
            balance = await ctx.account.balance()
            open_orders = await ctx.account.open_orders()
        except RouterError as exc:
            ctx.log.warning(
                f"매수 판단 보류 [{exc.code.value}]: {exc.message} · 다음 조건검색에서 재시도합니다."
            )
            return
        held_codes = {item.instrument.code for item in balance.positions if item.quantity > 0}
        pending_codes = {
            item.instrument.code for item in open_orders if item.remaining_quantity > 0
        }
        tracked = self._sync_tracked_positions(ctx, balance)
        submitted = 0

        for match in matches:
            code = match.instrument.code
            if code in held_codes or code in pending_codes or code in tracked:
                continue
            buying_power = await ctx.account.buying_power(
                code,
                order_type="MARKET",
            )
            budget_quantity = int(
                BUY_BUDGET_PER_SYMBOL // buying_power.calculation_price
            )
            broker_quantity = int(buying_power.maximum_quantity)
            quantity = min(broker_quantity, budget_quantity)
            if quantity < 1:
                if budget_quantity < 1:
                    ctx.log.info(
                        f"{code} 건너뜀: 시장가 계산단가 "
                        f"{buying_power.calculation_price}원이 종목당 예산 "
                        f"{BUY_BUDGET_PER_SYMBOL}원보다 높습니다."
                    )
                else:
                    ctx.log.info(
                        f"{code} 건너뜀: 증권사가 현금 주문 가능 수량을 "
                        f"{buying_power.maximum_quantity}주로 반환했습니다. "
                        f"주문 가능 현금 {buying_power.cash_available}원 · "
                        f"시장가 계산단가 {buying_power.calculation_price}원"
                    )
                continue
            try:
                # Leave a quiet interval after quote/buying-power REST calls.
                # KIS can reject an otherwise valid order when it immediately
                # follows a burst of account and market-data requests.
                await asyncio.sleep(1.1)
                order = await ctx.orders.market_buy(
                    code=code,
                    quantity=quantity,
                    route="KRX",
                    client_order_id=(
                        f"condition-{code}-{event.scheduled_at:%Y%m%d%H%M%S}-buy"
                    ),
                )
            except RouterError as exc:
                if (
                    exc.code.value == "RISK_LIMIT_EXCEEDED"
                    and exc.message == "전략의 최대 노출 한도를 초과합니다."
                ):
                    # 종목당 예산으로 계산한 수량이 남은 운용 예산보다 크면
                    # 주문을 포기하지 않고 현재 보유·미체결을 뺀 범위로 줄인다.
                    exposure = Decimal(
                        str(exc.details.get("exposure", TOTAL_OPERATING_BUDGET))
                    )
                    remaining = max(
                        Decimal("0"), TOTAL_OPERATING_BUDGET - exposure
                    )
                    unit_price = buying_power.calculation_price
                    adjusted_quantity = int(
                        min(
                            quantity - 1,
                            buying_power.maximum_quantity,
                            remaining // unit_price,
                        )
                    )
                    if adjusted_quantity < 1:
                        ctx.log.info(
                            f"{code} 건너뜀: 남은 운용 예산 {remaining}원으로 "
                            f"1주를 주문할 수 없습니다."
                        )
                        continue
                    try:
                        await asyncio.sleep(1.1)
                        order = await ctx.orders.market_buy(
                            code=code,
                            quantity=adjusted_quantity,
                            route="KRX",
                            client_order_id=(
                                f"condition-{code}-{event.scheduled_at:%Y%m%d%H%M%S}"
                                f"-buy-q{adjusted_quantity}"
                            ),
                        )
                        quantity = adjusted_quantity
                    except RouterError as retry_exc:
                        ctx.log.error(
                            f"{code} 조정 매수 요청 실패 [{retry_exc.code.value}]: "
                            f"{retry_exc.message}"
                        )
                        continue
                else:
                    ctx.log.error(
                        f"{code} 매수 요청 실패 [{exc.code.value}]: {exc.message}"
                    )
                    continue
            if order.status is not OrderStatus.REJECTED:
                tracked[code] = str(quantity)
                ctx.state.set("strategy_positions", tracked)
                submitted += 1
                ctx.log.info(
                    f"{code} 시장가 {quantity}주 매수 요청: "
                    f"{order.status.value}"
                )
            if submitted >= MAX_NEW_BUYS_PER_SCAN:
                break

    async def on_bar(self, ctx, event):
        try:
            balance = await ctx.account.balance()
            open_orders = await ctx.account.open_orders()
        except RouterError as exc:
            ctx.log.warning(
                f"매도 판단 보류 [{exc.code.value}]: {exc.message} · 다음 완성 3분봉에서 재시도합니다."
            )
            return
        positions = {item.instrument.code: item for item in balance.positions}
        pending_codes = {
            item.instrument.code for item in open_orders if item.remaining_quantity > 0
        }
        tracked = self._sync_tracked_positions(ctx, balance)

        for code in list(tracked):
            position = positions.get(code)
            if position is None or position.quantity <= 0:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if (
                position.strategy_quantity is not None
                and position.strategy_quantity <= 0
            ):
                # The central fill ledger is authoritative. A stale local
                # intent must never turn a manual holding into strategy stock.
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)
                continue
            if code in pending_codes:
                continue
            bars = await ctx.market.bars(
                code,
                "3m",
                # The newest API bar can still be forming. Request one spare
                # row so complete_only still yields all 55 completed bars.
                count=BOLLINGER_PERIOD + 1,
                venue=Venue.KRX,
                complete_only=True,
            )
            if len(bars) < BOLLINGER_PERIOD:
                ctx.log.warning(
                    f"{code} 매도 판단 보류: 완성 3분봉 {len(bars)}/{BOLLINGER_PERIOD}개"
                )
                continue
            closes = [bar.close for bar in bars[-BOLLINGER_PERIOD:]]
            mean = sum(closes, Decimal("0")) / Decimal(BOLLINGER_PERIOD)
            variance = sum(
                ((value - mean) ** 2 for value in closes), Decimal("0")
            ) / Decimal(BOLLINGER_PERIOD)
            upper = mean + BOLLINGER_STDDEV * variance.sqrt()
            latest = bars[-1]
            average_price = position.average_price
            take_profit_price = average_price * TAKE_PROFIT_MULTIPLIER
            stop_loss_price = average_price * STOP_LOSS_MULTIPLIER
            sell_reason = None
            if average_price > 0 and latest.close >= take_profit_price:
                sell_reason = (
                    f"익절: 종가 {latest.close}원 >= "
                    f"평균 매수가 대비 +3% {take_profit_price:.2f}원"
                )
            elif average_price > 0 and latest.close <= stop_loss_price:
                sell_reason = (
                    f"손절: 종가 {latest.close}원 <= "
                    f"평균 매수가 대비 -3% {stop_loss_price:.2f}원"
                )
            elif latest.close < upper:
                sell_reason = (
                    f"볼린저 청산: 종가 {latest.close}원 < 상단 {upper:.2f}원"
                )

            if sell_reason is None:
                ctx.log.info(
                    f"{code} 보유 유지: 종가 {latest.close}원 · "
                    f"손절 {stop_loss_price:.2f}원 < 현재 < "
                    f"익절 {take_profit_price:.2f}원 · 볼린저 상단 {upper:.2f}원 이상"
                )
                continue

            tracked_quantity = (
                position.strategy_quantity
                if position.strategy_quantity is not None
                else Decimal(str(tracked[code]))
            )
            quantity = min(tracked_quantity, position.available_quantity)
            if quantity <= 0:
                continue
            try:
                # Historical bars may require several REST pages. Keep the
                # order submission outside that request burst.
                await asyncio.sleep(1.1)
                order = await ctx.orders.market_sell(
                    code=code,
                    quantity=quantity,
                    route="KRX",
                    client_order_id=(
                        f"exit-{code}-{event.scheduled_at:%Y%m%d%H%M%S}-sell"
                    ),
                )
            except RouterError as exc:
                ctx.log.error(f"{code} 매도 요청 실패 [{exc.code.value}]: {exc.message}")
                continue
            ctx.log.info(
                f"{code} {sell_reason} · 시장가 {quantity}주 매도 요청: "
                f"{order.status.value}"
            )
            if order.status is OrderStatus.FILLED:
                tracked.pop(code, None)
                ctx.state.set("strategy_positions", tracked)

    async def on_stop(self, ctx):
        ctx.log.info("KIS 조건검색 볼린저 전략을 종료합니다.")
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
        elif example.is_file():
            # Updates preserve user strategies. Replace only the exact obsolete
            # bundled sample; any user-edited source remains untouched.
            existing = example.read_text(encoding="utf-8")
            if existing.strip() == LEGACY_EXAMPLE_STRATEGY.strip():
                example.write_text(EXAMPLE_STRATEGY, encoding="utf-8")
        condition_example = self.strategies / "kis_condition_bollinger.py"
        if not condition_example.exists():
            condition_example.write_text(
                KIS_CONDITION_BOLLINGER_STRATEGY,
                encoding="utf-8",
            )
