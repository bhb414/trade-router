from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trade_router import (
    AccountOrder,
    Balance,
    BrokerCapabilities,
    BuyingPower,
    InstrumentId,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ProviderOrderAck,
    RouterError,
    RouterErrorCode,
)
from trade_router_studio.orders import (
    OrderService,
    _broker_order_matches_unknown,
    _provider_notice_event_key,
)
from trade_router_studio.runtime_store import RuntimeStore
from trade_router_studio.sessions import StrategyManifest


SEOUL = ZoneInfo("Asia/Seoul")


def test_best_limit_risk_partial_fill_reconcile_and_cancel(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "best-limit.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            request = OrderRequest(
                instrument=InstrumentId("005930"), side=OrderSide.BUY,
                order_type=OrderType.BEST_LIMIT, quantity=Decimal("3"),
                client_order_id="best-limit", route=OrderRoute.AUTO,
            )
            try:
                await service.submit(
                    strategy_name="strategy.py", manifest=manifest(max_order_amount="250000"),
                    request=request,
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.RISK_LIMIT_EXCEEDED
            else:
                raise AssertionError("zero wire price must not bypass order amount limits")
            assert gateway.submissions == 0
            submitted = await service.submit(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="500000"),
                request=OrderRequest(
                    instrument=request.instrument, side=request.side,
                    order_type=request.order_type, quantity=request.quantity,
                    client_order_id="best-limit-allowed", route=request.route,
                ),
            )
            assert submitted.request.order_type is OrderType.BEST_LIMIT
            assert store.order(submitted.id)["request"]["reserved_amount"] == "300000"
            # One fill and an external cancellation of one share reduce the
            # broker's effective total to two. Assigned quote must not turn
            # the remaining best-limit order into a user-priced limit order.
            gateway.orders = (AccountOrder(
                provider_order_id=submitted.provider_order_id or "",
                account_alias="broker-account", instrument=request.instrument,
                side=OrderSide.BUY, order_type=OrderType.BEST_LIMIT,
                status=OrderStatus.PARTIALLY_FILLED,
                requested_quantity=Decimal("3"), filled_quantity=Decimal("1"),
                remaining_quantity=Decimal("1"), ordered_at=datetime.now(SEOUL),
                provider=gateway.provider, price=Decimal("73000"),
                average_fill_price=Decimal("73000"),
            ),)
            partial = await service.refresh_unresolved_orders("broker-account")
            assert partial[0]["status"] == OrderStatus.PARTIALLY_FILLED.value
            cancelled = await service.cancel(
                strategy_name="strategy.py", manifest=manifest(max_order_amount="500000"),
                order_id=submitted.id, quantity=None, route=None,
            )
            assert cancelled.status is OrderStatus.CANCEL_PENDING
            assert cancelled.request.order_type is OrderType.BEST_LIMIT
            assert cancelled.request.price is None
            assert cancelled.request.quantity == 2
            assert cancelled.filled_quantity == 1
            assert gateway.submissions == 1
        finally:
            store.close()
    asyncio.run(run())



class SimulationGateway:
    def __init__(self, provider: str = "contract-simulation") -> None:
        self.provider = provider
        self.submissions = 0
        self.fail_with: RouterError | None = None
        self.account_limits: dict[str, str | int | None] = {}
        self.orders: tuple[AccountOrder, ...] = ()
        self.open_orders: tuple[AccountOrder, ...] | None = None
        self.changes: list[tuple[str, str, Decimal | None]] = []
        self.cost_rates: dict[str, str | None] = {}
        self.buying_power_routes: list[OrderRoute] = []

    def capabilities(self, _: str) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider=self.provider,
            accounts=True,
            order_history=True,
            cash_orders=True,
            amend_orders=True,
            cancel_orders=True,
            order_routes=(OrderRoute.KRX, OrderRoute.AUTO),
        )

    def account_risk_limits(self, _: str) -> dict[str, str | int | None]:
        return dict(self.account_limits)

    def account_cost_rates(self, _: str) -> dict[str, str | None]:
        return dict(self.cost_rates)

    async def balance(self, _: str) -> Balance:
        return Balance(
            cash=Decimal("1000000"),
            withdrawable_cash=Decimal("1000000"),
            total_value=Decimal("1100000"),
            positions=(
                Position(
                    instrument=InstrumentId("005930"),
                    quantity=Decimal("2"),
                    available_quantity=Decimal("2"),
                    average_price=Decimal("70000"),
                    market_price=Decimal("75000"),
                ),
            ),
            as_of=datetime.now(SEOUL),
        )

    async def account_orders(self, *_: object, **kwargs: object) -> tuple[AccountOrder, ...]:
        if kwargs.get("state") == "open" and self.open_orders is not None:
            return self.open_orders
        return self.orders

    async def buying_power(
        self,
        account_reference: str,
        code: str,
        *,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
        route: OrderRoute = OrderRoute.AUTO,
    ) -> BuyingPower:
        self.buying_power_routes.append(route)
        return BuyingPower(
            instrument=InstrumentId(code),
            account_alias=account_reference,
            order_type=order_type,
            requested_price=price or Decimal("0"),
            calculation_price=Decimal("100000"),
            cash_available=Decimal("1000000"),
            maximum_quantity=Decimal("10"),
            provider=self.provider,
            as_of=datetime.now(SEOUL),
            route=route,
        )

    async def submit_order(
        self,
        _: str,
        request: OrderRequest,
    ) -> ProviderOrderAck:
        self.submissions += 1
        if self.fail_with is not None:
            raise self.fail_with
        return ProviderOrderAck(
            provider_order_id=f"broker-{self.submissions}",
            provider=self.provider,
            acknowledged_at=datetime.now(SEOUL),
            correlation_id=f"correlation-{self.submissions}",
        )

    async def amend_order(
        self,
        _: str,
        provider_order_id: str,
        *,
        quantity: Decimal,
        order_type: OrderType,
        price: Decimal | None,
        route: OrderRoute,
    ) -> ProviderOrderAck:
        self.changes.append(("amend", provider_order_id, quantity))
        return ProviderOrderAck(
            provider_order_id=f"{provider_order_id}-amended",
            provider=self.provider,
            acknowledged_at=datetime.now(SEOUL),
            correlation_id="amend-1",
        )

    async def cancel_order(
        self,
        _: str,
        provider_order_id: str,
        *,
        quantity: Decimal | None,
        route: OrderRoute,
    ) -> ProviderOrderAck:
        self.changes.append(("cancel", provider_order_id, quantity))
        return ProviderOrderAck(
            provider_order_id=provider_order_id,
            provider=self.provider,
            acknowledged_at=datetime.now(SEOUL),
            correlation_id="cancel-1",
        )


class UnsupportedOrderGateway(SimulationGateway):
    def capabilities(self, _: str) -> BrokerCapabilities:
        return BrokerCapabilities(provider="read-only-broker", accounts=True)


def manifest(**limits: str | int | None) -> StrategyManifest:
    return StrategyManifest(
        strategy_id="neutral-strategy",
        trading_account="broker-account",
        market_sources={"primary": "market-source"},
        required_capabilities={},
        risk_limits=limits,
    )


def market_buy(client_order_id: str = "005930-buy-1") -> OrderRequest:
    return OrderRequest(
        instrument=InstrumentId("005930"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        client_order_id=client_order_id,
        route=OrderRoute.AUTO,
    )


def _unknown_request_payload(code: str = "005930") -> dict[str, object]:
    return {
        "code": code,
        "country": "KR",
        "asset": "EQUITY",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": "1",
        "price": None,
        "route": "AUTO",
        "provider_options": {},
    }


def _unknown_candidate(
    provider_order_id: str,
    ordered_at: datetime,
    *,
    code: str = "005930",
    quantity: str = "1",
) -> AccountOrder:
    return AccountOrder(
        provider_order_id=provider_order_id,
        account_alias="broker-account",
        instrument=InstrumentId(code),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        status=OrderStatus.ACKNOWLEDGED,
        requested_quantity=Decimal(quantity),
        filled_quantity=Decimal("0"),
        remaining_quantity=Decimal(quantity),
        ordered_at=ordered_at,
        provider="contract-simulation",
        route=OrderRoute.KRX,
    )


class HistoricalOrderGateway(SimulationGateway):
    def __init__(self) -> None:
        super().__init__()
        self.history: dict[date, tuple[AccountOrder, ...]] = {}
        self.order_queries: list[tuple[str, date | None]] = []
        self.fresh_queries: list[bool] = []

    async def account_orders(
        self, *_: object, **kwargs: object
    ) -> tuple[AccountOrder, ...]:
        state = str(kwargs.get("state") or "all")
        business_date = kwargs.get("business_date")
        assert business_date is None or isinstance(business_date, date)
        self.order_queries.append((state, business_date))
        self.fresh_queries.append(bool(kwargs.get("fresh")))
        if state == "open":
            return ()
        return self.history.get(
            business_date or datetime.now(SEOUL).date(), ()
        )


def test_unknown_match_allows_sor_execution_venue_but_keeps_explicit_routes_exact() -> None:
    submitted_at = datetime.now(SEOUL).replace(microsecond=0)
    row = {
        "id": "route-match",
        "created_at": submitted_at.isoformat(),
        "updated_at": submitted_at.isoformat(),
        "provider_metadata": {"first_unknown_at": submitted_at.isoformat()},
        "request": {**_unknown_request_payload(), "route": "SOR"},
    }
    krx = replace(
        _unknown_candidate("krx-child", submitted_at), route=OrderRoute.KRX
    )
    nxt = replace(
        _unknown_candidate("nxt-child", submitted_at), route=OrderRoute.NXT
    )
    assert _broker_order_matches_unknown(row, krx, enforce_time_window=True)
    assert _broker_order_matches_unknown(row, nxt, enforce_time_window=True)

    explicit_krx = {**row, "request": {**row["request"], "route": "KRX"}}
    assert not _broker_order_matches_unknown(
        explicit_krx, nxt, enforce_time_window=True
    )


def test_legacy_unknown_without_first_unknown_does_not_use_late_updated_at() -> None:
    submitted_at = datetime.now(SEOUL).replace(microsecond=0)
    row = {
        "id": "legacy-time-window",
        "created_at": submitted_at.isoformat(),
        "updated_at": (submitted_at + timedelta(hours=4)).isoformat(),
        "provider_metadata": {},
        "request": _unknown_request_payload(),
    }
    for seconds in (30, 90):
        candidate = _unknown_candidate(
            f"manual-{seconds}", submitted_at + timedelta(seconds=seconds)
        )
        assert not _broker_order_matches_unknown(
            row, candidate, enforce_time_window=True
        )


def test_legacy_unknown_first_reconcile_keeps_submission_time_window(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "legacy-reconcile-window.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            day = datetime.now(SEOUL).date()
            submitted_at = datetime.combine(day, time(9, 0), tzinfo=SEOUL)
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="legacy-reconcile-window",
                request=_unknown_request_payload(),
            )
            row = store.update_order(
                str(row["id"]), status=OrderStatus.UNKNOWN_RECONCILING
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (
                        submitted_at.isoformat(),
                        (submitted_at + timedelta(hours=4)).isoformat(),
                        row["id"],
                    ),
                )

            first = await service.reconcile_unknown_order(str(row["id"]))
            assert first["match_result"] == "none"
            after_first = store.order(str(row["id"]))
            assert after_first["provider_metadata"]["first_unknown_at"] == submitted_at.isoformat()

            gateway.history[day] = tuple(
                _unknown_candidate(
                    f"late-{seconds}", submitted_at + timedelta(seconds=seconds)
                )
                for seconds in (30, 90)
            )
            second = await service.reconcile_unknown_order(str(row["id"]))
            assert second["match_result"] == "none"
            assert second["candidate_count"] == 0
        finally:
            store.close()

    asyncio.run(run())


def test_provider_notice_event_key_is_scoped_to_business_date() -> None:
    notice = {
        "provider_order_id": "000123",
        "execution_code": "2",
        "time": "093001",
    }
    first = _provider_notice_event_key(notice, "2026-10-26")
    repeated = _provider_notice_event_key(notice, "2026-10-26")
    next_day = _provider_notice_event_key(notice, "2026-10-27")
    assert first == repeated
    assert first != next_day


def test_provider_neutral_order_pipeline_is_idempotent(tmp_path: Path) -> None:
    async def run(provider: str) -> None:
        store = RuntimeStore(tmp_path / f"{provider}.sqlite3")
        gateway = SimulationGateway(provider)
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            first = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=market_buy(),
            )
            repeated = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=market_buy(),
            )
            assert first.id == repeated.id
            assert first.status is OrderStatus.ACKNOWLEDGED
            assert gateway.submissions == 1
            assert gateway.buying_power_routes == [OrderRoute.AUTO]
        finally:
            store.close()

    # The central workflow does not branch on a broker name.
    asyncio.run(run("provider-a"))
    asyncio.run(run("provider-b"))


def test_strategy_exposure_ignores_manual_account_holdings(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "strategy-exposure.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            owned, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="owned-position",
                request={
                    "code": "005930",
                    "side": "BUY",
                    "quantity": "1",
                    "reserved_amount": "70000",
                },
            )
            store.update_order(
                owned["id"],
                status=OrderStatus.ACKNOWLEDGED,
                provider_order_id="owned-position-provider",
            )
            store.update_order(
                owned["id"],
                status=OrderStatus.FILLED,
                filled_quantity=Decimal("1"),
                average_fill_price=Decimal("70000"),
            )
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")

            # The account owns two shares, but only one belongs to this strategy.
            # 75,000 won of strategy exposure plus the new 100,000 won order fits
            # the 200,000 won operating budget.
            order = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_exposure="200000"),
                request=market_buy("exposure-allows-manual-holding"),
            )
            assert order.status is OrderStatus.ACKNOWLEDGED
            assert gateway.submissions == 1
        finally:
            store.close()

    asyncio.run(run())


def test_startup_reconciliation_persists_fill_quantity_for_ownership(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "runtime.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="restart-fill",
                request={"code": "005930", "side": "BUY", "quantity": "1"},
            )
            store.update_order(
                row["id"],
                status=OrderStatus.ACKNOWLEDGED,
                provider_order_id="broker-restart",
            )
            gateway.orders = (
                AccountOrder(
                    provider_order_id="broker-restart",
                    account_alias="broker-account",
                    instrument=InstrumentId("005930"),
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    status=OrderStatus.FILLED,
                    requested_quantity=Decimal("1"),
                    filled_quantity=Decimal("1"),
                    remaining_quantity=Decimal("0"),
                    ordered_at=datetime.now(SEOUL),
                    provider=gateway.provider,
                    average_fill_price=Decimal("73500"),
                ),
            )

            await service.reconcile_account("broker-account")

            updated = store.order(row["id"])
            assert updated is not None
            assert updated["status"] == OrderStatus.FILLED.value
            assert updated["filled_quantity"] == "1"
            assert updated["average_fill_price"] == "73500"
            assert store.strategy_position_quantities("neutral-strategy") == {
                "005930": Decimal("1")
            }
        finally:
            store.close()

    asyncio.run(run())


def test_startup_reconciliation_publishes_unknown_resolution_before_gate(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "startup-unknown-publish.sqlite3")
        gateway = SimulationGateway()
        published: list[dict[str, object]] = []

        async def publish(
            _strategy_id: str,
            event: dict[str, object],
            _strategy_name: str | None,
        ) -> None:
            published.append(event)

        service = OrderService(store, gateway, event_publisher=publish)
        try:
            submitted_at = datetime.now(SEOUL).replace(microsecond=0)
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="startup-unknown-publish",
                request=_unknown_request_payload(),
            )
            row = store.update_order(
                str(row["id"]),
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"first_unknown_at": submitted_at.isoformat()},
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (submitted_at.isoformat(), submitted_at.isoformat(), row["id"]),
                )
            gateway.orders = (
                replace(
                    _unknown_candidate("startup-provider", submitted_at),
                    status=OrderStatus.FILLED,
                    filled_quantity=Decimal("1"),
                    remaining_quantity=Decimal("0"),
                    average_fill_price=Decimal("73000"),
                ),
            )

            result = await service.reconcile_account("broker-account")

            assert result["reconciled"] is True
            assert [event["event_type"] for event in published] == ["order", "fill"]
            stored = store.order(str(row["id"]))
            assert stored["status"] == "FILLED"
            assert store.events(event_types=("order.reconciled",), limit=1)[0][
                "details"
            ]["order_id"] == row["id"]
        finally:
            store.close()

    asyncio.run(run())


def test_account_reconciliation_does_not_cap_unresolved_rows_at_one_thousand(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "unbounded-account-reconcile.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            broker_orders: list[AccountOrder] = []
            for index in range(1_001):
                code = f"{index + 1:06d}"
                row, _ = store.reserve_order(
                    strategy_id="neutral-strategy",
                    account_alias="broker-account",
                    client_order_id=f"unbounded-{index}",
                    request={
                        **_unknown_request_payload(),
                        "code": code,
                    },
                )
                provider_id = f"provider-{index}"
                store.update_order(
                    str(row["id"]),
                    status=OrderStatus.ACKNOWLEDGED,
                    provider_order_id=provider_id,
                )
                broker_orders.append(
                    _unknown_candidate(
                        provider_id,
                        datetime.now(SEOUL),
                        code=code,
                    )
                )
            gateway.orders = tuple(broker_orders)

            result = await service.reconcile_account("broker-account")

            assert result["reconciled"] is True
            assert result["broker_order_count"] == 1_001
            assert len(store.unresolved_orders(account_alias="broker-account")) == 1_001
        finally:
            store.close()

    asyncio.run(run())


def test_order_pipeline_blocks_limits_and_unknown_resubmission(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "runtime.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            try:
                await service.submit(
                    strategy_name="strategy.py",
                    manifest=manifest(max_order_amount="50000"),
                    request=market_buy("risk-block"),
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.RISK_LIMIT_EXCEEDED
            else:
                raise AssertionError("risk limit did not block the order")
            assert gateway.submissions == 0

            gateway.account_limits = {"max_order_amount": "50000"}
            try:
                await service.submit(
                    strategy_name="strategy.py",
                    manifest=manifest(max_order_amount="200000"),
                    request=market_buy("account-risk-block"),
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.RISK_LIMIT_EXCEEDED
                assert "계좌" in exc.message
            else:
                raise AssertionError("account limit did not block the order")
            gateway.account_limits = {}

            gateway.fail_with = RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN,
                "response lost",
            )
            unknown = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=market_buy("unknown-order"),
            )
            assert unknown.status is OrderStatus.UNKNOWN_RECONCILING
            repeated = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=market_buy("unknown-order"),
            )
            assert repeated.id == unknown.id
            assert gateway.submissions == 1
        finally:
            store.close()

    asyncio.run(run())


def test_daily_buy_count_limits_block_before_another_submission(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "daily-count.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_daily_buy_count=1),
                request=market_buy("first-buy"),
            )
            second = OrderRequest(
                instrument=InstrumentId("000660"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                client_order_id="second-buy",
                route=OrderRoute.AUTO,
            )
            try:
                await service.submit(
                    strategy_name="strategy.py",
                    manifest=manifest(max_daily_buy_count=1),
                    request=second,
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.RISK_LIMIT_EXCEEDED
                assert "일일 누적 매수횟수" in exc.message
            else:
                raise AssertionError("daily buy count limit did not block the order")
            assert gateway.submissions == 1

            gateway.account_limits = {"max_daily_buy_count": 1}
            account_limited_manifest = StrategyManifest(
                strategy_id="second-strategy",
                trading_account="broker-account",
                market_sources={"primary": "market-source"},
                required_capabilities={},
                risk_limits={},
            )
            service.arm_strategy("second-strategy")
            try:
                await service.submit(
                    strategy_name="second.py",
                    manifest=account_limited_manifest,
                    request=second,
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.RISK_LIMIT_EXCEEDED
                assert "계좌" in exc.message
            else:
                raise AssertionError("account daily buy count limit did not block the order")
            assert gateway.submissions == 1
        finally:
            store.close()

    asyncio.run(run())


def test_external_open_order_blocks_same_account_symbol(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "external-open.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            gateway.orders = (
                AccountOrder(
                    provider_order_id="mobile-order-1",
                    account_alias="broker-account",
                    instrument=InstrumentId("005930"),
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    status=OrderStatus.ACKNOWLEDGED,
                    requested_quantity=Decimal("1"),
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("1"),
                    ordered_at=datetime.now(SEOUL),
                    provider=gateway.provider,
                    price=Decimal("74000"),
                ),
            )

            try:
                await service.submit(
                    strategy_name="strategy.py",
                    manifest=manifest(max_order_amount="150000"),
                    request=market_buy("blocked-by-mobile"),
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
                assert exc.details["provider_order_ids"] == ["mobile-order-1"]
            else:
                raise AssertionError("external broker order did not block submission")
            assert gateway.submissions == 0
        finally:
            store.close()

    asyncio.run(run())


def test_missing_from_broker_open_orders_does_not_close_live_router_orders(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "open-snapshot-gap.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            rows: list[dict[str, object]] = []
            broker_orders: list[AccountOrder] = []
            for index, (status, filled_quantity) in enumerate(
                (
                    (OrderStatus.ACKNOWLEDGED, Decimal("0")),
                    (OrderStatus.PARTIALLY_FILLED, Decimal("0.5")),
                ),
                start=1,
            ):
                provider_order_id = f"broker-live-{index}"
                row, _ = store.reserve_order(
                    strategy_id="neutral-strategy",
                    account_alias="broker-account",
                    client_order_id=f"live-order-{index}",
                    request={
                        "code": f"00593{index}",
                        "side": "BUY",
                        "quantity": "1",
                    },
                )
                row = store.update_order(
                    row["id"],
                    status=OrderStatus.ACKNOWLEDGED,
                    provider_order_id=provider_order_id,
                )
                if status is OrderStatus.PARTIALLY_FILLED:
                    row = store.update_order(
                        row["id"],
                        status=status,
                        filled_quantity=filled_quantity,
                        average_fill_price=Decimal("73500"),
                    )
                rows.append(row)
                broker_orders.append(
                    AccountOrder(
                        provider_order_id=provider_order_id,
                        account_alias="broker-account",
                        instrument=InstrumentId(f"00593{index}"),
                        side=OrderSide.BUY,
                        order_type=OrderType.LIMIT,
                        status=status,
                        requested_quantity=Decimal("1"),
                        filled_quantity=filled_quantity,
                        remaining_quantity=Decimal("1") - filled_quantity,
                        ordered_at=datetime.now(SEOUL) - timedelta(seconds=30),
                        provider=gateway.provider,
                        price=Decimal("74000"),
                        average_fill_price=(
                            Decimal("73500")
                            if status is OrderStatus.PARTIALLY_FILLED
                            else None
                        ),
                    )
                )
            gateway.orders = tuple(broker_orders)
            gateway.open_orders = ()

            updated = await service.refresh_unresolved_orders("broker-account")
            await service.reconcile_account("broker-account")

            assert updated == []
            persisted = [store.order(str(row["id"])) for row in rows]
            assert [row["status"] for row in persisted if row is not None] == [
                OrderStatus.ACKNOWLEDGED.value,
                OrderStatus.PARTIALLY_FILLED.value,
            ]
            assert {
                row["id"]
                for row in store.unresolved_orders(account_alias="broker-account")
            } == {row["id"] for row in rows}
        finally:
            store.close()

    asyncio.run(run())


def test_unsupported_broker_is_rejected_before_preflight_or_submission(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "unsupported.sqlite3")
        gateway = UnsupportedOrderGateway()
        service = OrderService(store, gateway)
        try:
            # Set persisted permissions directly because account reconciliation
            # itself is unavailable for this read-only adapter.
            store.set_order_permission("account", "broker-account", armed=True)
            store.set_order_permission("strategy", "neutral-strategy", armed=True)
            try:
                await service.submit(
                    strategy_name="strategy.py",
                    manifest=manifest(),
                    request=market_buy("unsupported"),
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY
            else:
                raise AssertionError("unsupported broker reached submission")
            assert gateway.submissions == 0
        finally:
            store.close()

    asyncio.run(run())


def test_order_tracker_updates_partial_and_full_fill(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "tracked.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=market_buy("tracked-order"),
            )
            assert submitted.status is OrderStatus.ACKNOWLEDGED

            gateway.orders = (
                AccountOrder(
                    provider_order_id="broker-1",
                    account_alias="broker-account",
                    instrument=InstrumentId("005930"),
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    status=OrderStatus.PARTIALLY_FILLED,
                    requested_quantity=Decimal("1"),
                    filled_quantity=Decimal("0.5"),
                    remaining_quantity=Decimal("0.5"),
                    ordered_at=datetime.now(SEOUL),
                    provider=gateway.provider,
                    average_fill_price=Decimal("73500"),
                ),
            )
            partial = await service.refresh_unresolved_orders("broker-account")
            assert partial[0]["status"] == OrderStatus.PARTIALLY_FILLED.value
            assert partial[0]["filled_quantity"] == "0.5"
            assert partial[0]["average_fill_price"] == "73500"

            gateway.orders = (
                AccountOrder(
                    provider_order_id="broker-1",
                    account_alias="broker-account",
                    instrument=InstrumentId("005930"),
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    status=OrderStatus.FILLED,
                    requested_quantity=Decimal("1"),
                    filled_quantity=Decimal("1"),
                    remaining_quantity=Decimal("0"),
                    ordered_at=datetime.now(SEOUL),
                    provider=gateway.provider,
                    average_fill_price=Decimal("73600"),
                ),
            )
            filled = await service.refresh_unresolved_orders("broker-account")
            assert filled[0]["status"] == OrderStatus.FILLED.value
            assert filled[0]["filled_quantity"] == "1"
            assert filled[0]["average_fill_price"] == "73600"
            assert store.unresolved_orders(account_alias="broker-account") == []
        finally:
            store.close()

    asyncio.run(run())


def test_order_events_amend_cancel_and_execution_costs(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "events.sqlite3")
        gateway = SimulationGateway()
        gateway.cost_rates = {
            "buy_commission_rate": "0.001",
            "sell_commission_rate": "0.001",
            "sell_tax_rate": "0.002",
        }
        events: list[dict[str, object]] = []

        async def publish(
            strategy_id: str,
            event: dict[str, object],
            strategy_name: str | None,
        ) -> None:
            assert strategy_id == "neutral-strategy"
            events.append(event)

        service = OrderService(store, gateway, event_publisher=publish)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            submitted = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="500000"),
                request=OrderRequest(
                    instrument=InstrumentId("005930"),
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=Decimal("3"),
                    client_order_id="change-me",
                    route=OrderRoute.AUTO,
                ),
            )
            amended = await service.amend(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="500000"),
                order_id=submitted.id,
                quantity=Decimal("3"),
                order_type=OrderType.LIMIT,
                price=Decimal("99000"),
                route=None,
            )
            assert amended.provider_order_id == "broker-1-amended"
            assert amended.request.quantity == Decimal("3")
            assert amended.request.order_type is OrderType.LIMIT
            assert amended.request.price == Decimal("99000")
            assert store.order_by_provider_id("broker-account", "broker-1")["id"] == submitted.id
            assert store.order_by_provider_id("broker-account", "broker-1-amended")["id"] == submitted.id
            cancelled = await service.cancel(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="500000"),
                order_id=submitted.id,
                quantity=None,
                route=None,
            )
            assert cancelled.status is OrderStatus.CANCEL_PENDING
            assert cancelled.request.quantity == Decimal("3")
            assert cancelled.request.order_type is OrderType.LIMIT
            assert cancelled.request.price == Decimal("99000")
            assert gateway.changes == [
                ("amend", "broker-1", Decimal("3")),
                ("cancel", "broker-1-amended", None),
            ]

            # A separate filled order verifies cumulative fill delta and costs.
            filled_request = OrderRequest(
                instrument=InstrumentId("000660"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                client_order_id="filled-cost",
                route=OrderRoute.AUTO,
            )
            filled = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=filled_request,
            )
            gateway.orders = (
                AccountOrder(
                    provider_order_id=filled.provider_order_id or "",
                    account_alias="broker-account",
                    instrument=InstrumentId("000660"),
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    status=OrderStatus.FILLED,
                    requested_quantity=Decimal("1"),
                    filled_quantity=Decimal("1"),
                    remaining_quantity=Decimal("0"),
                    ordered_at=datetime.now(SEOUL),
                    provider=gateway.provider,
                    average_fill_price=Decimal("100000"),
                ),
            )
            await service.refresh_unresolved_orders("broker-account")
            stored = store.order(filled.id)
            assert stored is not None
            assert stored["commission"] == "100.000"
            assert stored["costs_complete"] is True
            assert any(event["event_type"] == "fill" for event in events)
            assert any(event["event_type"] == "order" for event in events)
        finally:
            store.close()

    asyncio.run(run())


def test_realtime_fill_notice_is_idempotent(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "notice.sqlite3")
        gateway = SimulationGateway()
        events: list[dict[str, object]] = []

        async def publish(
            _strategy_id: str,
            event: dict[str, object],
            _strategy_name: str | None,
        ) -> None:
            events.append(event)

        service = OrderService(store, gateway, event_publisher=publish)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            order = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=market_buy("realtime-fill"),
            )
            notice = {
                "provider_order_id": order.provider_order_id,
                "original_order_id": "",
                "code": "005930",
                "quantity": "1",
                "price": "73500",
                "time": "093000",
                "rejected": "0",
                "execution_code": "2",
            }
            first = await service.apply_realtime_notice("broker-account", notice)
            repeated = await service.apply_realtime_notice("broker-account", notice)
            assert first is not None
            assert repeated is None
            assert store.order(order.id)["filled_quantity"] == "1"
            assert len(store.fills(order_id=order.id)) == 1
            assert [event["event_type"] for event in events].count("fill") == 1
        finally:
            store.close()

    asyncio.run(run())


def test_unknown_submit_metadata_and_historical_unique_match_excludes_linked_order(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "unknown-historical.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            gateway.fail_with = RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN,
                "response lost",
                details={
                    "unknown_origin": "transport_error",
                    "exception_type": "ReadTimeout",
                    "winerror": 10060,
                },
            )
            unknown = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=market_buy("historical-unknown"),
            )
            unknown_row = store.order(unknown.id)
            assert unknown_row is not None
            metadata = unknown_row["provider_metadata"]
            assert metadata["unknown_origin"] == "transport_error"
            assert metadata["exception_type"] == "ReadTimeout"
            assert metadata["winerror"] == 10060
            assert metadata["first_unknown_at"]
            assert metadata["match_result"] == "not_checked"
            unknown_event = store.events(
                event_types=("order.unknown",), limit=1
            )[0]
            assert "response lost" not in unknown_event["message"]
            assert unknown_event["details"] == {
                "order_id": unknown.id,
                "unknown_origin": "transport_error",
                "failure_phase": "submit",
                "provider_id_present": False,
                "exception_type": "ReadTimeout",
                "winerror": 10060,
                "code": "ORDER_STATUS_UNKNOWN",
            }

            linked, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="already-linked",
                request=_unknown_request_payload(),
            )
            store.update_order(
                str(linked["id"]),
                status=OrderStatus.ACKNOWLEDGED,
                provider_order_id="broker-linked",
            )
            previous_day = datetime.now(SEOUL).date() - timedelta(days=1)
            submitted_at = datetime.combine(
                previous_day, time(9, 42, 35), tzinfo=SEOUL
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (submitted_at.isoformat(), submitted_at.isoformat(), unknown.id),
                )
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (
                        (submitted_at - timedelta(minutes=5)).isoformat(),
                        (submitted_at - timedelta(minutes=5)).isoformat(),
                        linked["id"],
                    ),
                )
            gateway.fail_with = None
            gateway.history[previous_day] = (
                _unknown_candidate(
                    "broker-linked", submitted_at - timedelta(seconds=9)
                ),
                _unknown_candidate(
                    "broker-recovered", submitted_at + timedelta(seconds=2)
                ),
            )

            refreshed = await service.refresh_unresolved_orders("broker-account")

            assert [row["id"] for row in refreshed] == [unknown.id]
            recovered = store.order(unknown.id)
            assert recovered is not None
            assert recovered["status"] == OrderStatus.ACKNOWLEDGED.value
            assert recovered["provider_order_id"] == "broker-recovered"
            assert recovered["provider_metadata"]["resolution"] == "auto_linked"
            assert ("all", previous_day) in gateway.order_queries
        finally:
            store.close()

    asyncio.run(run())


def test_unknown_auto_match_keeps_zero_and_multiple_candidates_unresolved(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "unknown-candidates.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            day = datetime.now(SEOUL).date()
            submitted_at = datetime.combine(day, time(10, 0), tzinfo=SEOUL)
            rows: dict[str, dict[str, object]] = {}
            for code in ("005930", "000660"):
                row, _ = store.reserve_order(
                    strategy_id="neutral-strategy",
                    account_alias="broker-account",
                    client_order_id=f"unknown-{code}",
                    request=_unknown_request_payload(code),
                )
                row = store.update_order(
                    str(row["id"]),
                    status=OrderStatus.UNKNOWN_RECONCILING,
                    provider_metadata={"first_unknown_at": submitted_at.isoformat()},
                )
                with store._connection:
                    store._connection.execute(
                        "UPDATE order_ledger SET created_at = ? WHERE id = ?",
                        (submitted_at.isoformat(), row["id"]),
                    )
                rows[code] = row
            gateway.history[day] = (
                # Identical but five minutes before the router submit: it can
                # be linked manually by id, never by the automatic matcher.
                _unknown_candidate(
                    "early-manual",
                    submitted_at - timedelta(minutes=5),
                    code="005930",
                ),
                _unknown_candidate(
                    "late-manual-30s",
                    submitted_at + timedelta(seconds=30),
                    code="005930",
                ),
                _unknown_candidate(
                    "late-manual-90s",
                    submitted_at + timedelta(seconds=90),
                    code="005930",
                ),
                _unknown_candidate("candidate-a", submitted_at, code="000660"),
                _unknown_candidate(
                    "candidate-b", submitted_at + timedelta(seconds=1), code="000660"
                ),
            )

            none_result = await service.reconcile_unknown_order(
                str(rows["005930"]["id"])
            )
            ambiguous_result = await service.reconcile_unknown_order(
                str(rows["000660"]["id"])
            )

            assert none_result["resolved"] is False
            assert none_result["match_result"] == "none"
            assert none_result["candidate_count"] == 0
            assert ambiguous_result["resolved"] is False
            assert ambiguous_result["match_result"] == "ambiguous"
            assert ambiguous_result["candidate_count"] == 2
            assert all(
                store.order(str(row["id"]))["provider_order_id"] is None
                for row in rows.values()
            )
        finally:
            store.close()

    asyncio.run(run())


def test_provider_order_id_conflicts_are_scoped_to_business_date(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "provider-id-business-date.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            today = datetime.now(SEOUL).date()
            previous_day = today - timedelta(days=1)
            submitted_at = datetime.combine(today, time(9, 10), tzinfo=SEOUL)

            prior, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="prior-day-reused-id",
                request=_unknown_request_payload(),
            )
            store.update_order(
                str(prior["id"]),
                status=OrderStatus.ACKNOWLEDGED,
                provider_order_id="reused-id",
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (
                        datetime.combine(previous_day, time(9, 0), tzinfo=SEOUL).isoformat(),
                        datetime.combine(previous_day, time(9, 0), tzinfo=SEOUL).isoformat(),
                        prior["id"],
                    ),
                )

            current, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="current-reused-id",
                request=_unknown_request_payload(),
            )
            current = store.update_order(
                str(current["id"]),
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"first_unknown_at": submitted_at.isoformat()},
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (submitted_at.isoformat(), submitted_at.isoformat(), current["id"]),
                )
            gateway.history[today] = (
                _unknown_candidate("reused-id", submitted_at + timedelta(seconds=1)),
            )

            result = await service.reconcile_unknown_order(str(current["id"]))
            assert result["resolved"] is True
            assert store.order(str(current["id"]))["provider_order_id"] == "reused-id"

            same_day_linked, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="same-day-linked",
                request=_unknown_request_payload("000660"),
            )
            store.update_order(
                str(same_day_linked["id"]),
                status=OrderStatus.ACKNOWLEDGED,
                provider_order_id="same-day-id",
            )
            same_day_unknown, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="same-day-unknown",
                request=_unknown_request_payload("000660"),
            )
            same_day_unknown = store.update_order(
                str(same_day_unknown["id"]),
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"first_unknown_at": submitted_at.isoformat()},
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (submitted_at.isoformat(), submitted_at.isoformat(), same_day_unknown["id"]),
                )
            gateway.history[today] = (
                _unknown_candidate(
                    "same-day-id",
                    submitted_at + timedelta(seconds=1),
                    code="000660",
                ),
            )

            blocked = await service.reconcile_unknown_order(
                str(same_day_unknown["id"])
            )
            assert blocked["resolved"] is False
            assert blocked["candidate_count"] == 0
            assert store.order(str(same_day_unknown["id"]))["provider_order_id"] is None
        finally:
            store.close()

    asyncio.run(run())


def test_missing_broker_order_time_requires_manual_link_and_vetoes_not_accepted(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "missing-broker-time.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            day = datetime.now(SEOUL).date()
            submitted_at = datetime.combine(day, time(9, 5), tzinfo=SEOUL)
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="kis-missing-time",
                request=_unknown_request_payload(),
            )
            row = store.update_order(
                str(row["id"]),
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata={"first_unknown_at": submitted_at.isoformat()},
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (submitted_at.isoformat(), submitted_at.isoformat(), row["id"]),
                )
            gateway.history[day] = (
                replace(
                    _unknown_candidate(
                        "kis-no-time",
                        datetime.combine(day, time(), tzinfo=SEOUL),
                    ),
                    provider="kis",
                ),
            )

            automatic = await service.reconcile_unknown_order(str(row["id"]))
            assert automatic["resolved"] is False
            assert automatic["match_result"] == "manual_required_no_order_time"
            assert automatic["candidate_count"] == 1
            assert gateway.fresh_queries[-1] is True

            with pytest.raises(RouterError) as vetoed:
                await service.resolve_unknown_order(
                    str(row["id"]),
                    action="NOT_ACCEPTED",
                    confirmation="HTS_NOT_ACCEPTED",
                )
            assert vetoed.value.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
            assert store.order(str(row["id"]))["provider_metadata"]["match_result"] == (
                "manual_required_no_order_time"
            )

            linked = await service.resolve_unknown_order(
                str(row["id"]),
                action="LINK",
                provider_order_id="kis-no-time",
                confirmation="HTS_LINK_CONFIRMED",
            )
            assert linked["resolved"] is True
            assert store.order(str(row["id"]))["provider_order_id"] == "kis-no-time"
        finally:
            store.close()

    asyncio.run(run())


def test_background_unknown_reconcile_backs_off_without_delaying_tracked_orders(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "unknown-backoff.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="unknown-backoff",
                request=_unknown_request_payload(),
            )
            row = store.update_order(
                str(row["id"]), status=OrderStatus.UNKNOWN_RECONCILING
            )

            first = await service.refresh_unresolved_orders("broker-account")
            first_query_count = len(gateway.order_queries)
            first_stored = store.order(str(row["id"]))
            assert first and first_query_count == 1
            assert first_stored["provider_metadata"]["reconcile_attempt_count"] == 1

            second = await service.refresh_unresolved_orders("broker-account")
            second_stored = store.order(str(row["id"]))
            assert second == []
            assert len(gateway.order_queries) == first_query_count
            assert second_stored["updated_at"] == first_stored["updated_at"]

            due_metadata = dict(second_stored["provider_metadata"])
            due_metadata["last_reconcile_at"] = (
                datetime.now(SEOUL) - timedelta(seconds=6)
            ).isoformat()
            store.update_order(
                str(row["id"]),
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata=due_metadata,
            )
            await service.refresh_unresolved_orders("broker-account")
            due_stored = store.order(str(row["id"]))
            assert len(gateway.order_queries) == first_query_count + 1
            assert due_stored["provider_metadata"]["reconcile_attempt_count"] == 2

            tracked, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="tracked-order",
                request=_unknown_request_payload("000660"),
            )
            store.update_order(
                str(tracked["id"]),
                status=OrderStatus.ACKNOWLEDGED,
                provider_order_id="tracked-provider",
            )
            before_tracked_poll = len(gateway.order_queries)
            await service.refresh_unresolved_orders("broker-account")
            assert len(gateway.order_queries) > before_tracked_poll
        finally:
            store.close()

    asyncio.run(run())


def test_background_uncertain_modification_with_provider_id_also_backs_off(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "unknown-modification-backoff.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="uncertain-cancel-backoff",
                request=_unknown_request_payload(),
            )
            row = store.update_order(
                str(row["id"]),
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_order_id="original-provider",
                provider_metadata={
                    "uncertain_modification": "cancel",
                    "pending_modification": {
                        "kind": "cancel",
                        "previous_status": "ACKNOWLEDGED",
                        "requested_at": str(row["created_at"]),
                        "quantity": "1",
                        "route": "AUTO",
                    },
                    "first_unknown_at": str(row["updated_at"]),
                },
            )
            day = datetime.fromisoformat(str(row["created_at"])).astimezone(SEOUL).date()
            gateway.history[day] = (
                _unknown_candidate(
                    "original-provider",
                    datetime.fromisoformat(str(row["created_at"])),
                ),
            )

            first = await service.refresh_unresolved_orders("broker-account")
            first_query_count = len(gateway.order_queries)
            first_stored = store.order(str(row["id"]))
            assert first and first_query_count == 1
            assert first_stored["status"] == "UNKNOWN_RECONCILING"
            assert first_stored["provider_metadata"]["reconcile_attempt_count"] == 1

            assert await service.refresh_unresolved_orders("broker-account") == []
            assert len(gateway.order_queries) == first_query_count
            assert store.order(str(row["id"]))["updated_at"] == first_stored["updated_at"]

            due_metadata = dict(first_stored["provider_metadata"])
            due_metadata["last_reconcile_at"] = (
                datetime.now(SEOUL) - timedelta(seconds=6)
            ).isoformat()
            store.update_order(
                str(row["id"]),
                status=OrderStatus.UNKNOWN_RECONCILING,
                provider_metadata=due_metadata,
            )
            await service.refresh_unresolved_orders("broker-account")
            assert len(gateway.order_queries) == first_query_count + 1
        finally:
            store.close()

    asyncio.run(run())


def test_unknown_manual_link_and_not_accepted_confirmation_are_guarded(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "unknown-manual.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            day = datetime.now(SEOUL).date()
            submitted_at = datetime.combine(day, time(9, 0), tzinfo=SEOUL)
            link_row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="manual-link",
                request=_unknown_request_payload(),
            )
            link_row = store.update_order(
                str(link_row["id"]), status=OrderStatus.UNKNOWN_RECONCILING
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ? WHERE id = ?",
                    (submitted_at.isoformat(), link_row["id"]),
                )
            # Outside the narrow automatic window, but still verifiable as
            # an exact same-day request when the user supplies its broker id.
            gateway.history[day] = (
                _unknown_candidate(
                    "manual-provider", submitted_at + timedelta(hours=2)
                ),
            )
            automatic = await service.reconcile_unknown_order(str(link_row["id"]))
            assert automatic["match_result"] == "none"
            for missing_confirmation in ("", "WRONG_CONFIRMATION"):
                try:
                    await service.resolve_unknown_order(
                        str(link_row["id"]),
                        action="LINK",
                        provider_order_id="manual-provider",
                        confirmation=missing_confirmation,
                    )
                except RouterError as exc:
                    assert exc.code is RouterErrorCode.INVALID_REQUEST
                else:
                    raise AssertionError("unconfirmed broker id was linked")
            try:
                await service.resolve_unknown_order(
                    str(link_row["id"]),
                    action="LINK",
                    provider_order_id="does-not-exist",
                    confirmation="HTS_LINK_CONFIRMED",
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.INVALID_REQUEST
            else:
                raise AssertionError("unknown broker id was linked")
            linked = await service.resolve_unknown_order(
                str(link_row["id"]),
                action="LINK",
                provider_order_id="manual-provider",
                confirmation="HTS_LINK_CONFIRMED",
            )
            assert linked["resolved"] is True
            assert store.order(str(link_row["id"]))["provider_order_id"] == "manual-provider"

            rejected_row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="manual-not-accepted",
                request=_unknown_request_payload("035420"),
            )
            rejected_row = store.update_order(
                str(rejected_row["id"]), status=OrderStatus.UNKNOWN_RECONCILING
            )
            try:
                await service.resolve_unknown_order(
                    str(rejected_row["id"]), action="NOT_ACCEPTED"
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.INVALID_REQUEST
            else:
                raise AssertionError("missing confirmation marked order rejected")
            rejected = await service.resolve_unknown_order(
                str(rejected_row["id"]),
                action="NOT_ACCEPTED",
                confirmation="HTS_NOT_ACCEPTED",
            )
            assert rejected["resolved"] is True
            stored_rejected = store.order(str(rejected_row["id"]))
            assert stored_rejected["status"] == OrderStatus.REJECTED.value
            assert (
                stored_rejected["provider_metadata"]["resolution"]
                == "manual_not_accepted"
            )
        finally:
            store.close()

    asyncio.run(run())


def test_unexpected_submit_exception_records_safe_unknown_event(tmp_path: Path) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "unexpected-unknown.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            await service.arm_account("broker-account")
            service.arm_strategy("neutral-strategy")
            gateway.fail_with = RuntimeError("secret response body")  # type: ignore[assignment]
            unknown = await service.submit(
                strategy_name="strategy.py",
                manifest=manifest(max_order_amount="150000"),
                request=market_buy("unexpected-unknown"),
            )
            assert unknown.status is OrderStatus.UNKNOWN_RECONCILING
            event = store.events(event_types=("order.unknown",), limit=1)[0]
            assert event["details"] == {
                "order_id": unknown.id,
                "unknown_origin": "unexpected_adapter_error",
                "failure_phase": "submit",
                "provider_id_present": False,
                "exception_type": "RuntimeError",
            }
            assert "secret" not in json.dumps(event["details"])
        finally:
            store.close()

    asyncio.run(run())


def test_restart_submitting_does_not_link_a_later_same_day_order(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "restart-submitting.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            day = datetime.now(SEOUL).date()
            submitted_at = datetime.combine(day, time(9, 0), tzinfo=SEOUL)
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="crashed-submit",
                request=_unknown_request_payload(),
            )
            with store._connection:
                store._connection.execute(
                    "UPDATE order_ledger SET created_at = ?, updated_at = ? WHERE id = ?",
                    (
                        submitted_at.isoformat(),
                        (submitted_at + timedelta(hours=3)).isoformat(),
                        row["id"],
                    ),
                )
            gateway.history[day] = (
                _unknown_candidate(
                    "later-manual", submitted_at + timedelta(hours=1)
                ),
            )

            changed = await service.refresh_unresolved_orders("broker-account")

            assert changed and changed[0]["status"] == "UNKNOWN_RECONCILING"
            stored = store.order(str(row["id"]))
            assert stored["provider_order_id"] is None
            assert stored["provider_metadata"]["unknown_origin"] == "restart_submitting"
            assert stored["provider_metadata"]["first_unknown_at"] == submitted_at.isoformat()
            assert stored["provider_metadata"]["candidate_count"] == 0
        finally:
            store.close()

    asyncio.run(run())


def test_not_accepted_ignores_far_order_but_vetoes_near_candidate(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "not-accepted-window.sqlite3")
        gateway = HistoricalOrderGateway()
        service = OrderService(store, gateway)
        try:
            day = datetime.now(SEOUL).date()
            submitted_at = datetime.combine(day, time(9, 0), tzinfo=SEOUL)

            async def unknown(code: str, suffix: str) -> dict[str, object]:
                row, _ = store.reserve_order(
                    strategy_id="neutral-strategy",
                    account_alias="broker-account",
                    client_order_id=f"not-accepted-{suffix}",
                    request=_unknown_request_payload(code),
                )
                row = store.update_order(
                    str(row["id"]),
                    status=OrderStatus.UNKNOWN_RECONCILING,
                    provider_metadata={"first_unknown_at": submitted_at.isoformat()},
                )
                with store._connection:
                    store._connection.execute(
                        "UPDATE order_ledger SET created_at = ? WHERE id = ?",
                        (submitted_at.isoformat(), row["id"]),
                    )
                return row

            far_row = await unknown("005930", "far")
            near_row = await unknown("000660", "near")
            gateway.history[day] = (
                _unknown_candidate(
                    "far-independent",
                    submitted_at + timedelta(hours=2),
                    code="005930",
                ),
                _unknown_candidate(
                    "near-accepted",
                    submitted_at + timedelta(seconds=2),
                    code="000660",
                ),
            )

            far_result = await service.resolve_unknown_order(
                str(far_row["id"]),
                action="NOT_ACCEPTED",
                confirmation="HTS_NOT_ACCEPTED",
            )
            assert far_result["resolved"] is True
            assert store.order(str(far_row["id"]))["status"] == "REJECTED"

            try:
                await service.resolve_unknown_order(
                    str(near_row["id"]),
                    action="NOT_ACCEPTED",
                    confirmation="HTS_NOT_ACCEPTED",
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
                assert exc.details["candidate_count"] == 1
            else:
                raise AssertionError("near accepted order was marked not accepted")
            assert store.order(str(near_row["id"]))["status"] == "UNKNOWN_RECONCILING"
        finally:
            store.close()

    asyncio.run(run())
