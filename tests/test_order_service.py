from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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
from trade_router_studio.orders import OrderService
from trade_router_studio.runtime_store import RuntimeStore
from trade_router_studio.sessions import StrategyManifest


SEOUL = ZoneInfo("Asia/Seoul")


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


def test_manual_cancel_missing_from_broker_open_orders_closes_router_order(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = RuntimeStore(tmp_path / "manual-cancel.sqlite3")
        gateway = SimulationGateway()
        service = OrderService(store, gateway)
        try:
            row, _ = store.reserve_order(
                strategy_id="neutral-strategy",
                account_alias="broker-account",
                client_order_id="cancelled-on-mobile",
                request={"code": "005930", "side": "BUY", "quantity": "1"},
            )
            store.update_order(
                row["id"],
                status=OrderStatus.ACKNOWLEDGED,
                provider_order_id="broker-manual-cancel",
            )
            gateway.orders = (
                AccountOrder(
                    provider_order_id="broker-manual-cancel",
                    account_alias="broker-account",
                    instrument=InstrumentId("005930"),
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    status=OrderStatus.ACKNOWLEDGED,
                    requested_quantity=Decimal("1"),
                    filled_quantity=Decimal("0"),
                    remaining_quantity=Decimal("1"),
                    ordered_at=datetime.now(SEOUL) - timedelta(seconds=30),
                    provider=gateway.provider,
                    price=Decimal("74000"),
                ),
            )
            gateway.open_orders = ()

            await service.reconcile_account("broker-account")

            updated = store.order(row["id"])
            assert updated is not None
            assert updated["status"] == OrderStatus.CANCELLED.value
            assert store.unresolved_orders(account_alias="broker-account") == []
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
                quantity=Decimal("2"),
                order_type=OrderType.LIMIT,
                price=Decimal("99000"),
                route=None,
            )
            assert amended.provider_order_id == "broker-1-amended"
            assert amended.request.quantity == Decimal("2")
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
            assert cancelled.request.quantity == Decimal("2")
            assert cancelled.request.order_type is OrderType.LIMIT
            assert cancelled.request.price == Decimal("99000")
            assert gateway.changes == [
                ("amend", "broker-1", Decimal("2")),
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
