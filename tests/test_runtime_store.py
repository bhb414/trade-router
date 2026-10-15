from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from trade_router_studio.runtime_store import RuntimeStore
from trade_router import OrderStatus


def test_runtime_store_uses_wal_and_filters_events(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    store = RuntimeStore(path)
    try:
        first = store.record(
            source="first.py",
            event_type="strategy.start",
            message="started",
            details={"safe": True},
        )
        second = store.record(
            source="second.py",
            event_type="strategy.stop",
            message="stopped",
        )
        assert second > first
        events = store.events(source="first.py")
        assert len(events) == 1
        assert events[0]["details"] == {"safe": True}

        reader = sqlite3.connect(path)
        try:
            assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            reader.close()
    finally:
        store.close()


def test_runtime_store_reopens_after_a_transient_read_connection_failure(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    try:
        store.record(
            source="example.py",
            event_type="strategy.start",
            message="started",
        )
        store._connection.close()

        events = store.events(source="example.py")

        assert len(events) == 1
        assert events[0]["message"] == "started"
    finally:
        store.close()


def test_runtime_store_archives_corruption_and_starts_disarmed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    path.write_bytes(b"this is not a sqlite database")
    Path(f"{path}-wal").write_bytes(b"damaged wal")

    store = RuntimeStore(path)
    try:
        assert store.recovery is not None
        backup = Path(str(store.recovery["backup_path"]))
        assert (backup / "runtime.sqlite3").read_bytes() == b"this is not a sqlite database"
        assert (backup / "recovery.json").is_file()
        assert store.order_permissions() == []
        assert store.unresolved_orders() == []
        assert store.events()[0]["event_type"] == "runtime.database.recovered"
        assert store._connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        store.close()


def test_order_ledger_reserves_idempotency_key_and_tracks_unresolved(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    request = {"code": "005930", "side": "BUY", "quantity": "1"}
    try:
        first, created = store.reserve_order(
            strategy_id="breakout",
            account_alias="kis-paper",
            client_order_id="005930-20261015-buy",
            request=request,
        )
        repeated, repeated_created = store.reserve_order(
            strategy_id="breakout",
            account_alias="kis-paper",
            client_order_id="005930-20261015-buy",
            request=request,
        )
        assert created is True
        assert repeated_created is False
        assert repeated["id"] == first["id"]
        assert len(store.unresolved_orders(strategy_id="breakout")) == 1

        acknowledged = store.update_order(
            first["id"],
            status=OrderStatus.ACKNOWLEDGED,
            provider_order_id="broker-1",
        )
        assert acknowledged["provider_order_id"] == "broker-1"
        completed = store.update_order(first["id"], status=OrderStatus.FILLED)
        assert completed["provider_order_id"] == "broker-1"
        assert store.unresolved_orders(strategy_id="breakout") == []

        try:
            store.update_order(first["id"], status=OrderStatus.ACKNOWLEDGED)
        except ValueError:
            pass
        else:
            raise AssertionError("terminal order state was reopened")

        try:
            store.reserve_order(
                strategy_id="breakout",
                account_alias="kis-paper",
                client_order_id="005930-20261015-buy",
                request={**request, "quantity": "2"},
            )
        except ValueError:
            pass
        else:
            raise AssertionError("conflicting idempotency request was accepted")
    finally:
        store.close()


def test_order_ledger_derives_strategy_owned_position_quantity(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    try:
        buy, _ = store.reserve_order(
            strategy_id="breakout",
            account_alias="kis-paper",
            client_order_id="buy-1",
            request={"code": "005930", "side": "BUY", "quantity": "3"},
        )
        store.update_order(buy["id"], status=OrderStatus.ACKNOWLEDGED)
        store.update_order(
            buy["id"],
            status=OrderStatus.FILLED,
            filled_quantity="3",
            average_fill_price="70000",
        )
        sell, _ = store.reserve_order(
            strategy_id="breakout",
            account_alias="kis-paper",
            client_order_id="sell-1",
            request={"code": "005930", "side": "SELL", "quantity": "1"},
        )
        store.update_order(sell["id"], status=OrderStatus.ACKNOWLEDGED)
        store.update_order(
            sell["id"],
            status=OrderStatus.FILLED,
            filled_quantity="1",
            average_fill_price="71000",
        )
        other, _ = store.reserve_order(
            strategy_id="other",
            account_alias="kis-paper",
            client_order_id="other-buy",
            request={"code": "005930", "side": "BUY", "quantity": "9"},
        )
        store.update_order(other["id"], status=OrderStatus.REJECTED)

        assert store.strategy_position_quantities("breakout") == {
            "005930": Decimal("2")
        }
        assert store.strategy_position_quantities(
            "breakout", account_alias="another-account"
        ) == {}
    finally:
        store.close()


def test_runtime_store_aggregates_service_results_by_strategy(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    business_date = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    try:
        store.record(
            source="alpha.py",
            event_type="service.market.quote.success",
            message="005930 현재가를 받았습니다.",
            details={"duration_ms": 20.0, "code": "005930"},
        )
        store.record(
            source="alpha.py",
            event_type="service.market.bars.error",
            message="분봉 조회에 실패했습니다.",
            details={"duration_ms": 40.0, "code": "005930"},
        )
        store.record(
            source="beta.py",
            event_type="service.market.quote.success",
            message="000660 현재가를 받았습니다.",
            details={"duration_ms": 10.0, "code": "000660"},
        )

        all_results = store.service_results(business_date)
        alpha_results = store.service_results(business_date, source="alpha.py")

        assert all_results["market"]["total"] == 3
        assert all_results["market"]["success"] == 2
        assert all_results["market"]["error"] == 1
        assert alpha_results["market"]["total"] == 2
        assert alpha_results["market"]["average_duration_ms"] == 30.0
        assert alpha_results["market"]["last_outcome"] == "error"
        assert alpha_results["market"]["last_details"]["code"] == "005930"

        events = store.service_events(business_date, source="alpha.py")
        assert list(events) == ["market"]
        assert [item["operation"] for item in events["market"]] == [
            "bars",
            "quote",
        ]
        assert events["market"][0]["outcome"] == "error"
        assert events["market"][0]["details"]["code"] == "005930"
    finally:
        store.close()
