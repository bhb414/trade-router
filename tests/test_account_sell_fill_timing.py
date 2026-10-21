"""Account-scope ownership follows fills, rather than order submission time."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from trade_router import OrderStatus
from trade_router_studio.app import _strategy_performance, _strategy_trade_history
from trade_router_studio.runtime_store import RuntimeStore


BASE = datetime(2026, 10, 21, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def reserve(store, owner, side, quantity, *, scope="STRATEGY"):
    row, _ = store.reserve_order(
        strategy_id=owner, account_alias="test-account",
        client_order_id=f"{owner}-{side}-{len(store.orders(limit=1000))}",
        request={
            "code": "005930", "side": side, "quantity": str(quantity),
            "order_type": "LIMIT", "sell_position_scope": scope,
        },
    )
    store.update_order(row["id"], status=OrderStatus.ACKNOWLEDGED)
    return row


def record(store, row, *, cumulative, quantity, price, second,
           average=None, commission="0", tax="0",
           total_commission=None, total_tax=None, complete=True):
    store.record_fill(
        order_id=row["id"], cumulative_quantity=Decimal(str(cumulative)),
        quantity=Decimal(str(quantity)), price=Decimal(str(price)),
        commission=Decimal(commission), tax=Decimal(tax), costs_complete=True,
        filled_at=BASE + timedelta(seconds=second),
    )
    return store.update_order(
        row["id"], status=OrderStatus.FILLED if complete else OrderStatus.PARTIALLY_FILLED,
        filled_quantity=str(cumulative), average_fill_price=str(price if average is None else average),
        commission=commission if total_commission is None else total_commission,
        tax=tax if total_tax is None else total_tax, costs_complete=True,
    )


def profit(store, owner):
    return _strategy_performance(
        store.strategy_accounting_orders(owner, account_alias="test-account"), None,
    )


def test_buy_filled_after_account_sale_does_not_get_consumed_retroactively(tmp_path):
    store = RuntimeStore(tmp_path / "fill-timing.sqlite3")
    try:
        # A pending limit buy already exists when another strategy sells one
        # manually acquired share. The pending buy fills only afterward.
        buy = reserve(store, "buyer", "BUY", 1)
        sale = reserve(store, "seller", "SELL", 1, scope="ACCOUNT")
        record(store, sale, cumulative=1, quantity=1, price=110, second=10)
        record(store, buy, cumulative=1, quantity=1, price=100, second=20)
        assert store.strategy_position_quantities("buyer", account_alias="test-account") == {"005930": Decimal("1")}
        result = profit(store, "buyer")
        assert Decimal(result["realized_profit"]) == 0
        assert result["positions"][0]["quantity"] == "1"
        assert Decimal(result["positions"][0]["cost_basis"]) == 100
    finally:
        store.close()


def test_account_partial_fills_keep_each_owners_execution_price_and_fees(tmp_path):
    store = RuntimeStore(tmp_path / "partial-prices.sqlite3")
    try:
        own = reserve(store, "seller", "BUY", 2)
        record(store, own, cumulative=2, quantity=2, price=100, second=1)
        other = reserve(store, "owner", "BUY", 2)
        record(store, other, cumulative=2, quantity=2, price=200, second=2)
        sale = reserve(store, "seller", "SELL", 4, scope="ACCOUNT")
        record(store, sale, cumulative=2, quantity=2, price=110, second=10,
               commission="2", tax="4", complete=False)
        assert Decimal(profit(store, "seller")["realized_profit"]) == 14
        assert store.strategy_position_quantities("owner", account_alias="test-account") == {"005930": Decimal("2")}
        record(store, sale, cumulative=4, quantity=2, price=210, second=20, average=160,
               commission="6", tax="8", total_commission="8", total_tax="12")
        # Completing the later part must not rewrite the earlier owner's sale
        # at the order's new cumulative average price or redistribute its fees.
        assert Decimal(profit(store, "seller")["realized_profit"]) == 14
        assert Decimal(profit(store, "owner")["realized_profit"]) == 6
        assert Decimal(profit(store, "seller")["commission"]) == 2
        assert Decimal(profit(store, "owner")["commission"]) == 6
        assert store.strategy_position_quantities("seller", account_alias="test-account") == {}
        assert store.strategy_position_quantities("owner", account_alias="test-account") == {}
    finally:
        store.close()


def test_later_partial_fill_consumes_new_lot_in_actual_fill_order_after_restart(tmp_path):
    path = tmp_path / "partial-order.sqlite3"
    store = RuntimeStore(path)
    try:
        first = reserve(store, "owner", "BUY", 1)
        record(store, first, cumulative=1, quantity=1, price=100, second=1)
        # Two shares are initially sellable: this owner's share and one manual
        # share. A new strategy acquires a share between the two sell fills.
        sale = reserve(store, "seller", "SELL", 2, scope="ACCOUNT")
        record(store, sale, cumulative=1, quantity=1, price=110, second=10, complete=False)
        new_buy = reserve(store, "new-owner", "BUY", 1)
        record(store, new_buy, cumulative=1, quantity=1, price=200, second=15)
        record(store, sale, cumulative=2, quantity=1, price=210, second=20, average=160)
    finally:
        store.close()
    store = RuntimeStore(path)
    try:
        assert store.strategy_position_quantities("new-owner", account_alias="test-account") == {}
        assert Decimal(profit(store, "owner")["realized_profit"]) == 10
        assert Decimal(profit(store, "new-owner")["realized_profit"]) == 10
        # Replaying the persisted fills twice does not debit either owner twice.
        assert store.strategy_position_quantities("new-owner", account_alias="test-account") == {}
    finally:
        store.close()


def test_legacy_cumulative_snapshots_without_fill_rows_remain_usable(tmp_path):
    store = RuntimeStore(tmp_path / "legacy-snapshots.sqlite3")
    try:
        buy = reserve(store, "owner", "BUY", 3)
        store.update_order(buy["id"], status=OrderStatus.FILLED, filled_quantity="3", average_fill_price="100")
        sale = reserve(store, "seller", "SELL", 2, scope="ACCOUNT")
        store.update_order(sale["id"], status=OrderStatus.PARTIALLY_FILLED, filled_quantity="1", average_fill_price="110")
        assert store.strategy_position_quantities("owner", account_alias="test-account") == {"005930": Decimal("2")}
        store.update_order(sale["id"], status=OrderStatus.FILLED, filled_quantity="2", average_fill_price="110")
        assert store.strategy_position_quantities("owner", account_alias="test-account") == {"005930": Decimal("1")}
        assert Decimal(profit(store, "owner")["realized_profit"]) == 20
    finally:
        store.close()


def test_identical_fill_timestamps_keep_recorded_execution_order(tmp_path):
    store = RuntimeStore(tmp_path / "equal-timestamps.sqlite3")
    try:
        buy = reserve(store, "buyer", "BUY", 1)
        sale = reserve(store, "seller", "SELL", 1, scope="ACCOUNT")
        # Event IDs are deliberately opposite lexical order. With an identical
        # provider timestamp, SQLite insertion order preserves observed order.
        with patch("trade_router_studio.runtime_store.uuid.uuid4", side_effect=[
            SimpleNamespace(hex="z-earlier-sale"), SimpleNamespace(hex="a-later-buy"),
        ]):
            record(store, sale, cumulative=1, quantity=1, price=110, second=10)
            record(store, buy, cumulative=1, quantity=1, price=100, second=10)
        assert store.strategy_position_quantities("buyer", account_alias="test-account") == {"005930": Decimal("1")}
        assert Decimal(profit(store, "buyer")["realized_profit"]) == 0
    finally:
        store.close()


def test_partial_fill_history_aggregates_one_order_without_inflating_win_count(tmp_path):
    store = RuntimeStore(tmp_path / "partial-history.sqlite3")
    try:
        buy = reserve(store, "owner", "BUY", 4)
        record(store, buy, cumulative=4, quantity=4, price=100, second=1)
        sale = reserve(store, "seller", "SELL", 4, scope="ACCOUNT")
        record(store, sale, cumulative=2, quantity=2, price=110, second=10, complete=False)
        record(store, sale, cumulative=4, quantity=2, price=90, second=20, average=100)
        accounting = store.strategy_accounting_orders("owner", account_alias="test-account")
        result = _strategy_performance(accounting, None)
        assert result["matched_sales"] == 1
        assert result["winning_sales"] == 0
        history = _strategy_trade_history(
            store.orders(strategy_id="owner", account_alias="test-account"),
            accounting_orders=accounting,
        )
        external_sales = [row for row in history if row["id"] == sale["id"]]
        assert len(external_sales) == 1
        displayed = external_sales[0]
        assert Decimal(displayed["filled_quantity"]) == 4
        assert Decimal(displayed["average_fill_price"]) == 100
        assert Decimal(displayed["matched_quantity"]) == 4
        assert Decimal(displayed["cost_basis"]) == 400
        assert Decimal(displayed["net_proceeds"]) == 400
        assert Decimal(displayed["realized_profit"]) == 0
    finally:
        store.close()
