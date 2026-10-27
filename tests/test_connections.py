from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from decimal import Decimal

from trade_router import (
    InstrumentId,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderStatus,
    OrderType,
    RankingKind,
    RouterError,
    RouterErrorCode,
    Venue,
)
from trade_router_studio.connections import (
    ConnectionRuntime,
    _aggregate_minute_bars,
    _is_market_minute,
    _parse_period_row,
    _parse_kis_account_order,
    _normalize_kis_account_orders,
    _normalize_namuh_account_orders,
)
from trade_router_studio.paths import WorkspacePaths
from trade_router_studio.settings import SettingsStore


def test_kis_best_limit_order_contract_and_changes(tmp_path: Path) -> None:
    """All requests use mock transport, including the LIVE-target contract."""
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "best-limit"))
        connection = settings.add_connection(
            provider="kis", target="LIVE", label="mock-only",
            app_key="test-key", app_secret="test-secret",
        )
        settings.add_account(
            alias="best-account", connection_id=connection["id"],
            account_number="12345678", product_code="01",
        )
        submissions: list[dict] = []
        changes: list[dict] = []
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                return httpx.Response(200, json={
                    "access_token": "mock-token", "token_type": "Bearer",
                    "access_token_token_expired": (
                        datetime.now().astimezone() + timedelta(hours=24)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                })
            if request.url.path.endswith("/inquire-psbl-order"):
                assert request.url.params["ORD_DVSN"] == "03"
                assert request.url.params["ORD_UNPR"] == "0"
                return httpx.Response(200, json={"rt_cd": "0", "output": {
                    "ord_psbl_cash": "150000", "psbl_qty_calc_unpr": "95000",
                    "nrcvb_buy_qty": "1",
                }})
            if request.url.path.endswith("/order-cash"):
                body = json.loads(request.content)
                assert body["ORD_DVSN"] == "03"
                assert body["ORD_UNPR"] == "0"
                assert body["EXCG_ID_DVSN_CD"] in {"NXT", "SOR"}
                assert request.headers["tr_id"] in {"TTTC0011U", "TTTC0012U"}
                submissions.append(body)
                return httpx.Response(200, json={"rt_cd": "0", "output": {"ODNO": "000123"}})
            if request.url.path.endswith("/inquire-psbl-rvsecncl"):
                return httpx.Response(200, json={"rt_cd": "0", "output": [{
                    "odno": "000123", "ord_gno_brno": "91252", "psbl_qty": "2",
                    "ord_dvsn_cd": "03",
                }]})
            if request.url.path.endswith("/order-rvsecncl"):
                body = json.loads(request.content)
                assert body["ORD_DVSN"] == "03"
                assert body["ORD_UNPR"] == "0"
                assert body["EXCG_ID_DVSN_CD"] == "NXT"
                changes.append(body)
                return httpx.Response(200, json={"rt_cd": "0", "output": {"ODNO": "000124"}})
            raise AssertionError(f"unexpected mock request: {request.url}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ConnectionRuntime(settings, http_client=client)
            try:
                power = await runtime.buying_power(
                    "best-account", "005930", order_type=OrderType.BEST_LIMIT,
                    price=Decimal("73000"), route=OrderRoute.NXT,
                )
                assert power.order_type is OrderType.BEST_LIMIT
                assert power.requested_price == 0
                assert power.calculation_price == 95000
                assert power.maximum_quantity == 1
                for side in (OrderSide.BUY, OrderSide.SELL):
                    for route in (OrderRoute.NXT, OrderRoute.AUTO):
                        result = await runtime.submit_order("best-account", OrderRequest(
                            instrument=InstrumentId("005930"), side=side,
                            order_type=OrderType.BEST_LIMIT, quantity=Decimal("2"),
                            price=Decimal("73000"), route=route,
                            client_order_id=f"mock-{side}-{route}",
                        ))
                        assert result.provider_order_id == "000123"
                await runtime.amend_order(
                    "best-account", "000123", quantity=Decimal("1"),
                    order_type=OrderType.BEST_LIMIT, price=None, route=OrderRoute.NXT,
                )
                await runtime.cancel_order(
                    "best-account", "000123", quantity=None, route=OrderRoute.NXT,
                )
                assert len(submissions) == 4
                assert [item["RVSE_CNCL_DVSN_CD"] for item in changes] == ["01", "02"]
            finally:
                await runtime.close()
    asyncio.run(run())


def test_kis_best_limit_history_keeps_type_with_zero_or_assigned_price() -> None:
    for division, name, price in (
        ("03", "", "0"), ("", "최유리지정가", "73000"),
        ("28", "취소", "0"), ("44", "", "73000"),
    ):
        order = _parse_kis_account_order({
            "odno": "000123", "pdno": "005930", "sll_buy_dvsn_cd": "01",
            "orgn_odno": "000122",
            "ord_dvsn_cd": division, "ord_dvsn_name": name,
            "ord_qty": "3", "tot_ccld_qty": "1", "rmn_qty": "2",
            "ord_unpr": price, "avg_prvs": "73000",
            "ord_dt": "20261021", "ord_tmd": "081500",
            "excg_id_dvsn_cd": "NXT",
        }, "best-account", datetime(2026, 10, 21).date())
        assert order is not None
        assert order.order_type is OrderType.BEST_LIMIT
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.remaining_quantity == 2
        assert order.route is OrderRoute.NXT
        assert order.original_provider_order_id == "000122"


def test_kis_amendment_child_without_time_preserves_parent_relation() -> None:
    order = _parse_kis_account_order(
        {
            "odno": "000124",
            "orgn_odno": "000123",
            "pdno": "005930",
            "sll_buy_dvsn_cd": "02",
            "ord_dvsn_cd": "03",
            "ord_qty": "2",
            "tot_ccld_qty": "0",
            "rmn_qty": "2",
            "ord_unpr": "0",
            "ord_dt": "20261027",
            # Some KIS history responses omit ord_tmd. Midnight is an
            # explicit sentinel and must force manual rather than auto link.
            "excg_id_dvsn_cd": "KRX",
        },
        "paper-account",
        datetime(2026, 10, 27).date(),
    )

    assert order is not None
    assert order.original_provider_order_id == "000123"
    assert order.ordered_at.strftime("%H%M%S") == "000000"


def test_namuh_best_limit_rejected_before_any_broker_request(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "namuh-best-limit"))
        connection = settings.add_connection(
            provider="namuh", target="PAPER", label="nh-mock",
            app_key="test-key", app_secret="test-secret",
        )
        settings.add_account(
            alias="nh-account", connection_id=connection["id"],
            account_number="12345012345", product_code="",
        )
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("unsupported order must not call broker")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runtime = ConnectionRuntime(settings, http_client=client)
            try:
                calls = [
                    lambda: runtime.buying_power(
                        "nh-account", "005930", order_type=OrderType.BEST_LIMIT,
                    ),
                    lambda: runtime.submit_order("nh-account", OrderRequest(
                        instrument=InstrumentId("005930"), side=OrderSide.SELL,
                        order_type=OrderType.BEST_LIMIT, quantity=Decimal("1"),
                        client_order_id="unsupported-nh",
                    )),
                    lambda: runtime.amend_order(
                        "nh-account", "123", quantity=Decimal("1"),
                        order_type=OrderType.BEST_LIMIT, price=None, route=OrderRoute.AUTO,
                    ),
                ]
                for call in calls:
                    try:
                        await call()
                    except RouterError as exc:
                        assert exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY
                        assert "최유리" in exc.message
                    else:
                        raise AssertionError("unsupported order unexpectedly accepted")
            finally:
                await runtime.close()
    asyncio.run(run())



def workspace(root: Path) -> WorkspacePaths:
    paths = WorkspacePaths(
        root=root,
        strategies=root / "strategies",
        logs=root / "logs",
        data=root / "data",
        config=root / "config",
    )
    paths.ensure()
    return paths


def test_daily_bar_completes_only_after_the_selected_market_closes() -> None:
    row = {
        "stck_bsop_date": "20261016",
        "stck_oprc": "100",
        "stck_hgpr": "110",
        "stck_lwpr": "90",
        "stck_clpr": "105",
        "acml_vol": "500",
    }
    seoul = ZoneInfo("Asia/Seoul")

    def parsed(venue: Venue, hour: int, minute: int):
        return _parse_period_row(
            row,
            instrument=InstrumentId("005930"),
            interval="1d",
            days=1,
            venue=venue,
            provider="kis",
            correlation_id="daily-complete-test",
            source_alias="primary",
            received_at=datetime(2026, 10, 16, hour, minute, tzinfo=seoul),
        )

    assert parsed(Venue.KRX, 15, 29).complete is False
    assert parsed(Venue.KRX, 15, 30).complete is True
    assert parsed(Venue.NXT, 19, 59).complete is False
    assert parsed(Venue.NXT, 20, 0).complete is True
    assert parsed(Venue.INTEGRATED, 20, 0).complete is True


def test_minute_bars_only_include_the_selected_market_session() -> None:
    seoul = ZoneInfo("Asia/Seoul")
    instrument = InstrumentId("003200")
    day = datetime(2026, 10, 16, tzinfo=seoul)
    values = (Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1"))
    raw = {
        day.replace(hour=7, minute=59): values,
        day.replace(hour=8, minute=0): values,
        day.replace(hour=8, minute=49): values,
        day.replace(hour=8, minute=50): values,
        day.replace(hour=9, minute=0): values,
        day.replace(hour=9, minute=0, second=30): values,
        day.replace(hour=15, minute=19, second=59): values,
        day.replace(hour=15, minute=20): values,
        day.replace(hour=15, minute=27): values,
        day.replace(hour=15, minute=30): values,
        day.replace(hour=15, minute=39, second=59): values,
        day.replace(hour=15, minute=40): values,
        day.replace(hour=19, minute=57): values,
        day.replace(hour=20, minute=0): values,
    }

    def opens(venue: Venue) -> list[str]:
        bars = _aggregate_minute_bars(
            instrument,
            raw,
            minutes=3,
            venue=venue,
            provider="kis",
            correlation_id="session-filter-test",
            source_alias="primary",
            received_at=day.replace(hour=20, minute=1),
        )
        return [bar.open_time.strftime("%H:%M") for bar in bars]

    assert opens(Venue.KRX) == ["09:00", "15:18", "15:27"]
    assert opens(Venue.NXT) == ["08:00", "08:48", "09:00", "15:18", "15:39", "19:57"]
    assert opens(Venue.INTEGRATED) == [
        "08:00",
        "08:48",
        "09:00",
        "15:18",
        "15:27",
        "15:39",
        "19:57",
    ]


def test_market_minute_boundaries_follow_each_venue_session() -> None:
    seoul = ZoneInfo("Asia/Seoul")

    def at(hour: int, minute: int, second: int = 0) -> datetime:
        return datetime(2026, 10, 16, hour, minute, second, tzinfo=seoul)

    assert _is_market_minute(at(8, 49, 59), Venue.NXT)
    assert not _is_market_minute(at(8, 50), Venue.NXT)
    assert not _is_market_minute(at(9, 0, 29), Venue.NXT)
    assert _is_market_minute(at(9, 0, 30), Venue.NXT)
    assert _is_market_minute(at(15, 19, 59), Venue.NXT)
    assert not _is_market_minute(at(15, 20), Venue.NXT)
    assert not _is_market_minute(at(15, 39, 59), Venue.NXT)
    assert _is_market_minute(at(15, 40), Venue.NXT)
    assert _is_market_minute(at(19, 59, 59), Venue.NXT)
    assert not _is_market_minute(at(20, 0), Venue.NXT)

    assert _is_market_minute(at(9, 0), Venue.KRX)
    assert _is_market_minute(at(15, 29, 59), Venue.KRX)
    assert not _is_market_minute(at(15, 30), Venue.KRX)

    assert _is_market_minute(at(8, 49, 59), Venue.INTEGRATED)
    assert not _is_market_minute(at(8, 50), Venue.INTEGRATED)
    assert _is_market_minute(at(9, 0), Venue.INTEGRATED)
    assert _is_market_minute(at(15, 29, 59), Venue.INTEGRATED)
    assert not _is_market_minute(at(15, 30), Venue.INTEGRATED)
    assert _is_market_minute(at(15, 40), Venue.INTEGRATED)


def test_rate_limit_scope_is_shared_by_the_same_api_key(tmp_path: Path) -> None:
    settings = SettingsStore(workspace(tmp_path / "workspace"))
    first = settings.add_connection(
        provider="kis",
        target="PAPER",
        label="paper-one",
        app_key="shared-app-key",
        app_secret="first-secret",
    )
    second = settings.add_connection(
        provider="kis",
        target="PAPER",
        label="paper-two",
        app_key="shared-app-key",
        app_secret="second-secret",
    )
    other_key = settings.add_connection(
        provider="kis",
        target="PAPER",
        label="paper-three",
        app_key="different-app-key",
        app_secret="third-secret",
    )
    live = settings.add_connection(
        provider="kis",
        target="LIVE",
        label="live-one",
        app_key="shared-app-key",
        app_secret="live-secret",
    )

    assert settings.rate_limit_identity(first["id"]) == settings.rate_limit_identity(
        second["id"]
    )
    assert settings.rate_limit_identity(first["id"]) != settings.rate_limit_identity(
        other_key["id"]
    )
    assert settings.rate_limit_identity(first["id"]) != settings.rate_limit_identity(
        live["id"]
    )


def test_kis_connection_token_quote_and_balance_share_runtime(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="paper-source",
            app_key="unit-app-key",
            app_secret="unit-app-secret",
        )
        settings.add_account(
            alias="paper-account",
            connection_id=connection["id"],
            account_number="12345678",
            product_code="01",
        )
        calls = {"token": 0, "quote": 0, "balance": 0, "minute": 0, "daily": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                calls["token"] += 1
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "encrypted-after-use",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            assert request.headers["authorization"] == "Bearer encrypted-after-use"
            if request.url.path.endswith("/inquire-price"):
                calls["quote"] += 1
                assert request.url.params["FID_INPUT_ISCD"] == "005930"
                return httpx.Response(
                    200,
                    headers={"x-correlation-id": "quote-test"},
                    json={
                        "rt_cd": "0",
                        "msg_cd": "MCA00000",
                        "output": {
                            "stck_prpr": "73100",
                            "stck_cntg_hour": "101530",
                        },
                    },
                )
            if request.url.path.endswith("/inquire-balance"):
                calls["balance"] += 1
                assert request.url.params["CANO"] == "12345678"
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "msg_cd": "MCA00000",
                        "output1": [
                            {
                                "pdno": "005930",
                                "hldg_qty": "2",
                                "ord_psbl_qty": "1",
                                "pchs_avg_pric": "70000",
                                "prpr": "73100",
                            }
                        ],
                        "output2": [
                            {
                                "dnca_tot_amt": "100000",
                                "prvs_rcdl_excc_amt": "90000",
                                "tot_evlu_amt": "246200",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/inquire-time-dailychartprice"):
                calls["minute"] += 1
                assert request.url.params["FID_FAKE_TICK_INCU_YN"] == "Y"
                return httpx.Response(
                    200,
                    headers={"x-correlation-id": "minute-test"},
                    json={
                        "rt_cd": "0",
                        "output2": [
                            {
                                "stck_bsop_date": "20261015",
                                "stck_cntg_hour": "090200",
                                "stck_oprc": "102",
                                "stck_hgpr": "104",
                                "stck_lwpr": "101",
                                "stck_prpr": "103",
                                "cntg_vol": "30",
                            },
                            {
                                "stck_bsop_date": "20261015",
                                "stck_cntg_hour": "090100",
                                "stck_oprc": "101",
                                "stck_hgpr": "103",
                                "stck_lwpr": "100",
                                "stck_prpr": "102",
                                "cntg_vol": "20",
                            },
                            {
                                "stck_bsop_date": "20261015",
                                "stck_cntg_hour": "090000",
                                "stck_oprc": "100",
                                "stck_hgpr": "102",
                                "stck_lwpr": "99",
                                "stck_prpr": "101",
                                "cntg_vol": "10",
                            },
                        ],
                    },
                )
            if request.url.path.endswith("/inquire-daily-itemchartprice"):
                calls["daily"] += 1
                return httpx.Response(
                    200,
                    headers={"x-correlation-id": "daily-test"},
                    json={
                        "rt_cd": "0",
                        "output2": [
                            {
                                "stck_bsop_date": "20261014",
                                "stck_oprc": "100",
                                "stck_hgpr": "110",
                                "stck_lwpr": "90",
                                "stck_clpr": "105",
                                "acml_vol": "500",
                            }
                        ],
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            first = await runtime.check(connection["id"])
            second = await runtime.check(connection["id"])
            assert first["state"] == second["state"] == "connected"
            quote1 = await runtime.quote("paper-source", "005930", Venue.KRX)
            quote2 = await runtime.quote("paper-source", "005930", Venue.KRX)
            assert quote1.price == quote2.price
            balance = await runtime.balance("paper-account")
            assert balance.cash == 100000
            assert balance.positions[0].quantity == 2
            minute = await runtime.bars(
                "paper-source",
                "005930",
                "3m",
                count=1,
                venue=Venue.KRX,
                end=datetime.fromisoformat("2026-10-15T09:03:00+09:00"),
            )
            assert len(minute) == 1
            assert minute[0].open == 100
            assert minute[0].high == 104
            assert minute[0].low == 99
            assert minute[0].close == 103
            assert minute[0].volume == 60
            daily = await runtime.bars(
                "paper-source",
                "005930",
                "1d",
                count=1,
                venue=Venue.KRX,
                end=datetime.fromisoformat("2026-10-15T09:03:00+09:00"),
            )
            assert daily[0].close == 105
            assert calls == {
                "token": 1,
                "quote": 1,
                "balance": 1,
                "minute": 1,
                "daily": 1,
            }
            encrypted = (settings.paths.config / "secrets.dpapi").read_bytes()
            assert b"encrypted-after-use" not in encrypted
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_integrated_minute_bars_fall_back_to_krx_history(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="kis",
            target="LIVE",
            label="live-source",
            app_key="live-app-key",
            app_secret="live-app-secret",
        )
        market_codes: list[str] = []

        def minute_row(hour: str, price: int) -> dict[str, str]:
            return {
                "stck_bsop_date": "20261019",
                "stck_cntg_hour": hour,
                "stck_oprc": str(price),
                "stck_hgpr": str(price + 10),
                "stck_lwpr": str(price - 10),
                "stck_prpr": str(price),
                "cntg_vol": "10",
            }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "integrated-fallback-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            assert request.url.path.endswith("/inquire-time-dailychartprice")
            market_code = request.url.params["FID_COND_MRKT_DIV_CODE"]
            market_codes.append(market_code)
            if market_code == "UN":
                # KIS behaves this way for a symbol unavailable on NXT: the
                # integrated endpoint keeps returning only the current row,
                # even when an older cursor is requested.
                rows = [minute_row("093600", 9300)]
            else:
                rows = [
                    minute_row(f"09{minute:02d}00", 9200 + minute)
                    for minute in range(27, 36)
                ]
            return httpx.Response(200, json={"rt_cd": "0", "output2": rows})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            bars = await runtime.bars(
                connection["id"],
                "266690",
                "3m",
                count=3,
                venue=Venue.INTEGRATED,
                end=datetime.fromisoformat("2026-10-19T09:37:00+09:00"),
            )
            assert len(bars) == 3
            assert bars[-1].open_time == datetime.fromisoformat(
                "2026-10-19T09:36:00+09:00"
            )
            assert bars[-1].venue is Venue.INTEGRATED
            assert any(bar.venue is Venue.KRX for bar in bars[:-1])
            assert market_codes == ["UN", "UN", "UN", "J"]
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_integrated_minute_bars_cross_the_overnight_boundary(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="kis",
            target="LIVE",
            label="live-integrated-source",
            app_key="integrated-app-key",
            app_secret="integrated-app-secret",
        )
        market_codes: list[str] = []

        def rows(day: str, hours: tuple[str, ...]) -> list[dict[str, str]]:
            return [
                {
                    "stck_bsop_date": day,
                    "stck_cntg_hour": hour,
                    "stck_oprc": "100",
                    "stck_hgpr": "101",
                    "stck_lwpr": "99",
                    "stck_prpr": "100",
                    "cntg_vol": "10",
                }
                for hour in hours
            ]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "integrated-overnight-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            market_code = request.url.params["FID_COND_MRKT_DIV_CODE"]
            market_codes.append(market_code)
            assert market_code == "UN"
            if request.url.params["FID_INPUT_DATE_1"] == "20261019":
                output = rows("20261019", ("080000", "080100", "080200"))
            else:
                output = rows(
                    "20261018",
                    ("195400", "195500", "195600", "195700", "195800", "195900"),
                )
            return httpx.Response(200, json={"rt_cd": "0", "output2": output})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            bars = await runtime.bars(
                connection["id"],
                "003200",
                "3m",
                count=3,
                venue=Venue.INTEGRATED,
                end=datetime.fromisoformat("2026-10-19T09:00:00+09:00"),
            )
            assert [bar.open_time.isoformat() for bar in bars] == [
                "2026-10-18T19:54:00+09:00",
                "2026-10-18T19:57:00+09:00",
                "2026-10-19T08:00:00+09:00",
            ]
            assert market_codes == ["UN", "UN", "UN"]
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_instrument_name_uses_stock_info_and_shared_cache(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="kis",
            target="LIVE",
            label="live-name-source",
            app_key="live-name-key",
            app_secret="live-name-secret",
        )
        calls = {"token": 0, "name": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                calls["token"] += 1
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "name-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            assert request.url.path.endswith("/search-stock-info")
            assert request.headers["tr_id"] == "CTPF1002R"
            assert request.url.params["PRDT_TYPE_CD"] == "300"
            assert request.url.params["PDNO"] == "005930"
            calls["name"] += 1
            return httpx.Response(
                200,
                json={
                    "rt_cd": "0",
                    "output": {
                        "pdno": "005930",
                        "prdt_name": "삼성전자",
                    },
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            first = await runtime.instrument_name(connection["id"], "005930")
            second = await runtime.instrument_name(connection["id"], "005930")
            assert first == second == "삼성전자"
            assert calls == {"token": 1, "name": 1}
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_live_condition_search_is_normalized_and_paper_is_blocked(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        live = settings.add_connection(
            provider="kis",
            target="LIVE",
            label="live-condition-source",
            app_key="live-condition-key",
            app_secret="live-condition-secret",
        )
        paper = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="paper-condition-source",
            app_key="paper-condition-key",
            app_secret="paper-condition-secret",
        )
        calls = {"token": 0, "titles": 0, "matches": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                calls["token"] += 1
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "condition-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            assert request.headers["authorization"] == "Bearer condition-token"
            assert request.url.params["user_id"] == "Rehtnus"
            if request.url.path.endswith("/psearch-title"):
                calls["titles"] += 1
                assert request.headers["tr_id"] == "HHKST03900300"
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output2": [
                            {"seq": "17", "condition_nm": "거래량 돌파"},
                        ],
                    },
                )
            if request.url.path.endswith("/psearch-result"):
                calls["matches"] += 1
                assert request.headers["tr_id"] == "HHKST03900400"
                assert request.url.params["seq"] == "17"
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output2": [
                            {
                                "code": "005930",
                                "name": "삼성전자",
                                "price": "73100",
                                "change_rate": "1.25",
                            }
                        ],
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            assert runtime.capabilities(live["id"]).conditions is True
            assert runtime.capabilities(paper["id"]).conditions is False
            definitions = await runtime.condition_definitions(
                "live-condition-source",
                "Rehtnus",
                source_alias="primary",
            )
            assert len(definitions) == 1
            assert definitions[0].id == "17"
            assert definitions[0].name == "거래량 돌파"
            matches = await runtime.condition_matches(
                "live-condition-source",
                "Rehtnus",
                "17",
                source_alias="primary",
            )
            assert len(matches) == 1
            assert matches[0].instrument.code == "005930"
            assert matches[0].name == "삼성전자"
            assert matches[0].price == Decimal("73100")
            assert matches[0].change_rate == Decimal("1.25")

            try:
                await runtime.condition_definitions(
                    "paper-condition-source",
                    "Rehtnus",
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY
            else:
                raise AssertionError("paper condition search was not rejected")
            assert calls == {"token": 1, "titles": 1, "matches": 1}
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_live_rankings_are_normalized_and_share_the_request_queue(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        live = settings.add_connection(
            provider="kis",
            target="LIVE",
            label="live-ranking-source",
            app_key="live-ranking-key",
            app_secret="live-ranking-secret",
        )
        paper = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="paper-ranking-source",
            app_key="paper-ranking-key",
            app_secret="paper-ranking-secret",
        )
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "ranking-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            calls.append(request.url.path)
            assert request.headers["authorization"] == "Bearer ranking-token"
            if request.url.path.endswith("/hts-top-view"):
                assert request.headers["tr_id"] == "HHMCM000100C0"
                assert not request.url.params
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output1": [
                            {"mrkt_div_cls_code": "J", "mksc_shrn_iscd": "005930"}
                        ],
                    },
                )
            if request.url.path.endswith("/volume-rank"):
                assert request.headers["tr_id"] == "FHPST01710000"
                assert request.url.params["FID_COND_MRKT_DIV_CODE"] == "UN"
                assert request.url.params["FID_BLNG_CLS_CODE"] == "3"
                assert request.url.params["FID_INPUT_ISCD"] == "1001"
                assert request.url.params["FID_INPUT_PRICE_1"] == "10000"
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output": [
                            {
                                "data_rank": "1",
                                "mksc_shrn_iscd": "000660",
                                "hts_kor_isnm": "SK하이닉스",
                                "stck_prpr": "410000",
                                "prdy_ctrt": "2.50",
                                "acml_vol": "1234567",
                                "acml_tr_pbmn": "987654321",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/fluctuation"):
                assert request.headers["tr_id"] == "FHPST01700000"
                assert request.url.params["FID_INPUT_CNT_1"] == "7"
                assert request.url.params["FID_COND_MRKT_DIV_CODE"] == "J"
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output": [
                            {
                                "data_rank": "2",
                                "stck_shrn_iscd": "035420",
                                "hts_kor_isnm": "NAVER",
                                "stck_prpr": "250000",
                                "prdy_ctrt": "8.75",
                                "acml_vol": "765432",
                            }
                        ],
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            assert runtime.capabilities(live["id"]).rankings is True
            assert runtime.capabilities(paper["id"]).rankings is False

            viewed = await runtime.ranking(
                "live-ranking-source",
                RankingKind.HTS_VIEW,
                source_alias="primary",
            )
            assert viewed[0].instrument.code == "005930"
            assert viewed[0].rank == 1
            assert viewed[0].source_alias == "primary"

            traded = await runtime.ranking(
                "live-ranking-source",
                RankingKind.TRADING_VALUE,
                venue=Venue.INTEGRATED,
                market="KOSDAQ",
                min_price=Decimal("10000"),
            )
            assert traded[0].name == "SK하이닉스"
            assert traded[0].trading_value == Decimal("987654321")

            risen = await runtime.ranking(
                "live-ranking-source",
                RankingKind.RISE,
                limit=7,
            )
            assert risen[0].instrument.code == "035420"
            assert risen[0].change_rate == Decimal("8.75")

            try:
                await runtime.ranking("paper-ranking-source", RankingKind.VOLUME)
            except RouterError as exc:
                assert exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY
            else:
                raise AssertionError("paper ranking request was not rejected")
            assert calls == [
                "/uapi/domestic-stock/v1/ranking/hts-top-view",
                "/uapi/domestic-stock/v1/quotations/volume-rank",
                "/uapi/domestic-stock/v1/ranking/fluctuation",
            ]
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_namuh_connection_and_quote_use_shared_token(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="namuh",
            target="LIVE",
            label="nh-source",
            app_key="nh-app-key",
            app_secret="nh-app-secret",
        )
        calls = {"token": 0, "quote": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                calls["token"] += 1
                assert b"appsecretkey=nh-app-secret" in request.content
                return httpx.Response(
                    200,
                    json={
                        "access_token": "nh-encrypted-token",
                        "token_type": "Bearer",
                        "expires_in": 86400,
                    },
                )
            if request.url.path == "/krstock/quote/v1/currentPrice":
                calls["quote"] += 1
                assert request.headers["authorization"] == "Bearer nh-encrypted-token"
                assert request.headers["x-client-id"] == "nh-app-key"
                assert b'"market_cd":"UNT"' in request.content
                return httpx.Response(
                    200,
                    headers={"x-correlation-id": "nh-quote-test"},
                    json={
                        "rsp_cd": "00000",
                        "rsp_msg": "OK",
                        "Output_0": {
                            "iem_cd": "005930",
                            "stck_prpr": 73100,
                            "hoga_bsop_hour": "101530",
                        },
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            assert (await runtime.check(connection["id"]))["state"] == "connected"
            quote = await runtime.quote("nh-source", "005930", Venue.INTEGRATED)
            assert quote.price == 73100
            assert quote.provider == "namuh"
            assert calls == {"token": 1, "quote": 1}
            encrypted = (settings.paths.config / "secrets.dpapi").read_bytes()
            assert b"nh-encrypted-token" not in encrypted
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_namuh_paper_account_orders_and_order_changes_use_official_contract(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="namuh",
            target="PAPER",
            label="nh-paper-source",
            app_key="nh-paper-key",
            app_secret="nh-paper-secret",
        )
        settings.add_account(
            alias="nh-paper-account",
            connection_id=connection["id"],
            account_number="12345678901",
        )
        submitted_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                assert request.url.host == "api.nhplug.com"
                return httpx.Response(
                    200,
                    json={
                        "access_token": "nh-paper-token",
                        "token_type": "Bearer",
                        "expires_in": 86400,
                    },
                )
            assert request.url.host == "moapi.nhplug.com"
            body = json.loads(request.content)["Input_0"]
            if request.url.path.endswith("/balance"):
                assert body["aly_qut_cd"] == "2"
                return httpx.Response(
                    200,
                    json={
                        "rsp_msg": "정상",
                        "Output_0": {
                            "dca": "1000000",
                            "drn_pbl_amt": "900000",
                            "tot_aet_amt": "1146000",
                        },
                        "Output_1": [
                            {
                                "iem_cd": "005930",
                                "itg_bnc_qty": "2",
                                "rsdl_qty": "1",
                                "phs_pr": "70000",
                                "now_pr": "73000",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/dailyOrderExecution"):
                assert body["ost_cns_dit"] == "0"
                return httpx.Response(
                    200,
                    json={
                        "rsp_msg": "정상",
                        "Output_0": [
                            {
                                "itg_orr_no": "0000000030",
                                "org_mkt_orr_no": "0000000029",
                                "orr_dt": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d"),
                                "orr_tm": "101530",
                                "iem_cd": "005930",
                                "sby_dit_cd_nm": "매수",
                                "nmn_pr_tp_cd_nm": "보통가",
                                "orr_qty": "2",
                                "orr_pr": "73000",
                                "tot_cns_qty": "1",
                                "cns_avg_uit_pr": "72900",
                                "ny_cns_qty": "1",
                                "can_qty": "0",
                                "orr_rjt_rsn_cd_nm": "",
                                "rmt_mkt_cd": "SOR",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/buyableQuantity"):
                assert body["nmn_pr_tp_cd"] == "05"
                return httpx.Response(
                    200,
                    json={
                        "rsp_msg": "정상",
                        "Output_0": {
                            "csh_orr_pbl_amt": "300000",
                            "csh_orr_pbl_qty": "3",
                            "max_pbl_amt": "300000",
                            "max_pbl_qty": "3",
                        },
                    },
                )
            if request.url.path.endswith("/cashBuy"):
                submitted_paths.append(request.url.path)
                assert body["rmt_mkt_cd"] == "SOR"
                assert body["nmn_pr_tp_cd"] == "05"
                return httpx.Response(
                    200,
                    json={
                        "rsp_msg": "주문 접수",
                        "Output_0": {"mkt_orr_no": 30},
                    },
                )
            if request.url.path.endswith("/modify"):
                submitted_paths.append(request.url.path)
                assert body == {
                    "act_no": "12345678901",
                    "org_mkt_orr_no": 30,
                    "all_pat_dit_cd": "1",
                    "iem_cd": "005930",
                    "cor_qty": 0,
                    "cor_pr": 73500,
                    "sop_cnd_pr": "",
                    "rmt_mkt_cd": "SOR",
                    "sor_mkt_sli_yn": "N",
                }
                return httpx.Response(
                    200,
                    json={"rsp_msg": "정정 접수", "Output_0": {"mkt_orr_no": 31}},
                )
            if request.url.path.endswith("/cancel"):
                submitted_paths.append(request.url.path)
                assert body["all_pat_dit_cd"] == "1"
                assert "cor_qty" not in body
                return httpx.Response(
                    200,
                    json={"rsp_msg": "취소 접수", "Output_0": {"mkt_orr_no": 32}},
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            capabilities = runtime.capabilities("nh-paper-account")
            assert capabilities.realtime is True
            assert capabilities.cash_orders is True
            assert capabilities.amend_orders is True
            assert capabilities.cancel_orders is True
            assert runtime.realtime_connection("nh-paper-source")["url"] == (
                "wss://moapi.nhplug.com:17070/websocket"
            )
            balance = await runtime.balance("nh-paper-account")
            assert balance.withdrawable_cash == 900000
            assert balance.positions[0].available_quantity == 1
            orders = await runtime.account_orders("nh-paper-account", state="open")
            assert orders[0].status is OrderStatus.PARTIALLY_FILLED
            assert orders[0].ordered_at.strftime("%H%M%S") == "101530"
            assert orders[0].original_provider_order_id == "29"
            power = await runtime.buying_power(
                "nh-paper-account", "005930", order_type=OrderType.MARKET
            )
            assert power.calculation_price == 100000
            ack = await runtime.submit_order(
                "nh-paper-account",
                OrderRequest(
                    instrument=InstrumentId("005930"),
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=Decimal("1"),
                    client_order_id="nh-contract-buy",
                    route=OrderRoute.AUTO,
                ),
            )
            assert ack.provider_order_id == "30"
            amended = await runtime.amend_order(
                "nh-paper-account",
                "30",
                quantity=Decimal("1"),
                order_type=OrderType.LIMIT,
                price=Decimal("73500"),
                route=OrderRoute.AUTO,
            )
            assert amended.provider_order_id == "31"
            cancelled = await runtime.cancel_order(
                "nh-paper-account",
                "30",
                quantity=None,
                route=OrderRoute.AUTO,
            )
            assert cancelled.provider_order_id == "32"
            assert submitted_paths == [
                "/krstock/order/v1/cashBuy",
                "/krstock/order/v1/modify",
                "/krstock/order/v1/cancel",
            ]
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("cancel_quantity", "expected_remaining", "expected_status"),
    (
        ("2", Decimal("5"), OrderStatus.PARTIALLY_FILLED),
        ("7", Decimal("0"), OrderStatus.CANCELLED),
    ),
)
def test_namuh_cancel_child_folds_into_original_order(
    cancel_quantity: str,
    expected_remaining: Decimal,
    expected_status: OrderStatus,
) -> None:
    day = datetime(2026, 10, 27).date()
    rows = [
        {
            "itg_orr_no": "0000000030",
            "orr_dt": "20261027",
            "orr_tm": "101500",
            "iem_cd": "005930",
            "sby_dit_cd_nm": "매수",
            "nmn_pr_tp_cd_nm": "보통가",
            "orr_qty": "10",
            "orr_pr": "73000",
            "tot_cns_qty": "3",
            "cns_avg_uit_pr": "72900",
            "ny_cns_qty": "7",
            "can_qty": "0",
            "orr_rjt_rsn_cd_nm": "",
            "rmt_mkt_cd": "KRX",
        },
        {
            "itg_orr_no": "0000000031",
            "org_mkt_orr_no": "0000000030",
            "orr_dt": "20261027",
            "orr_tm": "101600",
            "iem_cd": "005930",
            "sby_dit_cd_nm": "매수",
            "nmn_pr_tp_cd_nm": "취소",
            "orr_qty": cancel_quantity,
            "orr_pr": "0",
            "tot_cns_qty": "0",
            "ny_cns_qty": "0",
            "can_qty": cancel_quantity,
            "orr_rjt_rsn_cd_nm": "",
            "rmt_mkt_cd": "KRX",
        },
    ]

    orders = _normalize_namuh_account_orders(rows, "nh-account", day)

    assert [item.provider_order_id for item in orders] == ["30"]
    assert orders[0].filled_quantity == 3
    assert orders[0].remaining_quantity == expected_remaining
    assert orders[0].status is expected_status
    assert orders[0].ordered_at.strftime("%H%M%S") == "101500"


def test_namuh_rejected_cancel_child_does_not_reduce_original_order() -> None:
    day = datetime(2026, 10, 27).date()
    rows = [
        {
            "itg_orr_no": "30",
            "orr_dt": "20261027",
            "orr_tm": "101500",
            "iem_cd": "005930",
            "sby_dit_cd_nm": "매수",
            "nmn_pr_tp_cd_nm": "보통가",
            "orr_qty": "10",
            "orr_pr": "73000",
            "tot_cns_qty": "3",
            "cns_avg_uit_pr": "72900",
            "ny_cns_qty": "7",
            "can_qty": "0",
            "orr_rjt_rsn_cd_nm": "",
            "rmt_mkt_cd": "KRX",
        },
        {
            "itg_orr_no": "31",
            "org_mkt_orr_no": "30",
            "orr_dt": "20261027",
            "orr_tm": "101600",
            "iem_cd": "005930",
            "sby_dit_cd_nm": "매수",
            "nmn_pr_tp_cd_nm": "취소",
            "orr_qty": "7",
            "orr_pr": "0",
            "tot_cns_qty": "0",
            "ny_cns_qty": "0",
            "can_qty": "0",
            "orr_rjt_rsn_cd_nm": "거절",
            "rmt_mkt_cd": "KRX",
        },
    ]

    orders = _normalize_namuh_account_orders(rows, "nh-account", day)
    original = next(item for item in orders if item.provider_order_id == "30")
    assert original.remaining_quantity == 7
    assert original.status is OrderStatus.PARTIALLY_FILLED


def test_namuh_order_http_server_error_is_unknown_not_rejected(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "namuh-http-unknown"))
        connection = settings.add_connection(
            provider="namuh",
            target="PAPER",
            label="nh-paper-source",
            app_key="nh-paper-key",
            app_secret="nh-paper-secret",
        )
        settings.add_account(
            alias="nh-paper-account",
            connection_id=connection["id"],
            account_number="12345678901",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "nh-paper-token",
                        "token_type": "Bearer",
                        "expires_in": 86400,
                    },
                )
            if request.url.path.endswith("/cashBuy"):
                return httpx.Response(
                    503,
                    json={"rsp_cd": "SERVER", "rsp_msg": "PRIVATE_SERVER_BODY"},
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            with pytest.raises(RouterError) as ambiguous:
                await runtime.submit_order(
                    "nh-paper-account",
                    OrderRequest(
                        instrument=InstrumentId("005930"),
                        side=OrderSide.BUY,
                        order_type=OrderType.MARKET,
                        quantity=Decimal("1"),
                        client_order_id="nh-http-unknown",
                        route=OrderRoute.AUTO,
                    ),
                )
            assert ambiguous.value.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
            assert ambiguous.value.details == {
                "unknown_origin": "http_status_unknown",
                "exception_type": "HTTPStatusError",
                "http_status": 503,
            }
            assert "PRIVATE_SERVER_BODY" not in ambiguous.value.message
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_account_orders_and_buying_power_are_normalized(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="paper-source",
            app_key="unit-app-key",
            app_secret="unit-app-secret",
        )
        settings.add_account(
            alias="paper-account",
            connection_id=connection["id"],
            account_number="12345678",
            product_code="01",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "orders-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            if request.url.path.endswith("/inquire-daily-ccld"):
                assert request.headers["tr_id"] == "VTTC0081R"
                assert request.url.params["EXCG_ID_DVSN_CD"] == "ALL"
                return httpx.Response(
                    200,
                    headers={"tr_cont": ""},
                    json={
                        "rt_cd": "0",
                        "output1": [
                            {
                                "odno": "000001",
                                "pdno": "005930",
                                "sll_buy_dvsn_cd": "02",
                                "ord_dvsn_name": "시장가",
                                "ord_qty": "2",
                                "tot_ccld_qty": "1",
                                "rmn_qty": "1",
                                "ord_unpr": "0",
                                "avg_prvs": "73100",
                                "ord_dt": "20261015",
                                "ord_tmd": "101530",
                                "excg_id_dvsn_cd": "SOR",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/inquire-psbl-rvsecncl"):
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output": [
                            {
                                "odno": "000001",
                                "pdno": "005930",
                                "sll_buy_dvsn_cd": "02",
                                "ord_dvsn_name": "시장가",
                                "ord_qty": "2",
                                "tot_ccld_qty": "1",
                                "rmn_qty": "1",
                                "ord_unpr": "0",
                                "avg_prvs": "73100",
                                "ord_dt": "20261015",
                                "ord_tmd": "101530",
                                "excg_id_dvsn_cd": "SOR",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/inquire-psbl-order"):
                assert request.url.params["ORD_DVSN"] == "01"
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output": {
                            "ord_psbl_cash": "150000",
                            "psbl_qty_calc_unpr": "95000",
                            "nrcvb_buy_qty": "1",
                        },
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            orders = await runtime.account_orders(
                "paper-account",
                state="open",
                business_date=datetime(2026, 10, 15).date(),
            )
            assert len(orders) == 1
            assert orders[0].status is OrderStatus.PARTIALLY_FILLED
            assert orders[0].remaining_quantity == 1
            power = await runtime.buying_power(
                "paper-account",
                "005930",
                order_type=OrderType.MARKET,
            )
            assert power.cash_available == 150000
            assert power.calculation_price == 95000
            assert power.maximum_quantity == 1
            assert power.route is OrderRoute.KRX
            try:
                await runtime.buying_power(
                    "paper-account",
                    "005930",
                    order_type=OrderType.MARKET,
                    route=OrderRoute.NXT,
                )
            except RouterError as exc:
                assert exc.code is RouterErrorCode.UNSUPPORTED_CAPABILITY
            else:
                raise AssertionError("KIS paper accounts must reject explicit NXT preflight.")
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_fresh_order_history_bypasses_existing_one_second_cache(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "fresh-order-history"))
        connection = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="fresh-orders",
            app_key="unit-app-key",
            app_secret="unit-app-secret",
        )
        settings.add_account(
            alias="paper-account",
            connection_id=connection["id"],
            account_number="12345678",
            product_code="01",
        )
        order_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal order_calls
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "fresh-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            if request.url.path.endswith("/inquire-daily-ccld"):
                order_calls += 1
                output = []
                if order_calls > 1:
                    output = [
                        {
                            "odno": "fresh-order",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "02",
                            "ord_dvsn_name": "시장가",
                            "ord_qty": "1",
                            "tot_ccld_qty": "0",
                            "rmn_qty": "1",
                            "ord_dt": "20261015",
                            "ord_tmd": "090001",
                        }
                    ]
                return httpx.Response(
                    200,
                    headers={"tr_cont": ""},
                    json={"rt_cd": "0", "output1": output},
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            day = datetime(2026, 10, 15).date()
            assert await runtime.account_orders(
                "paper-account", state="all", business_date=day
            ) == ()
            fresh = await runtime.account_orders(
                "paper-account", state="all", business_date=day, fresh=True
            )
            assert order_calls == 2
            assert [item.provider_order_id for item in fresh] == ["fresh-order"]
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider", ("kis", "namuh"))
def test_order_history_transport_error_retains_only_safe_root_cause(
    tmp_path: Path, provider: str,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / f"{provider}-history-timeout"))
        connection = settings.add_connection(
            provider=provider,
            target="PAPER",
            label=f"{provider}-history-timeout",
            app_key="test-key",
            app_secret="test-secret",
        )
        settings.add_account(
            alias="history-account",
            connection_id=connection["id"],
            account_number=("12345678" if provider == "kis" else "12345678901"),
            **({"product_code": "01"} if provider == "kis" else {}),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path in {"/oauth2/tokenP", "/oauth2/token"}:
                if provider == "kis":
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "mock-token",
                            "token_type": "Bearer",
                            "access_token_token_expired": (
                                datetime.now().astimezone() + timedelta(hours=24)
                            ).strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "mock-token",
                        "token_type": "Bearer",
                        "expires_in": 86400,
                    },
                )
            if request.url.path.endswith(
                ("/inquire-daily-ccld", "/dailyOrderExecution")
            ):
                raise httpx.ReadTimeout(
                    "PRIVATE_TOKEN_SENTINEL C:\\Users\\PRIVATE_USER",
                    request=request,
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            with pytest.raises(RouterError) as failure:
                await runtime.account_orders(
                    "history-account",
                    state="all",
                    business_date=datetime(2026, 10, 27).date(),
                    fresh=True,
                )
            assert failure.value.code is RouterErrorCode.INVALID_REQUEST
            assert failure.value.details == {
                "unknown_origin": "transport_error",
                "exception_type": "ReadTimeout",
            }
            assert "PRIVATE_TOKEN_SENTINEL" not in json.dumps(
                failure.value.details
            )
            assert "PRIVATE_USER" not in failure.value.message
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_unknown_submission_errors_include_only_safe_classification(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "unknown-order-details"))
        connection = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="unknown-order-mock",
            app_key="test-key",
            app_secret="test-secret",
        )
        settings.add_account(
            alias="unknown-order-account",
            connection_id=connection["id"],
            account_number="12345678",
            product_code="01",
        )
        failure_mode = "missing-id"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal failure_mode
            if request.url.path == "/oauth2/tokenP":
                return httpx.Response(
                    200,
                    json={
                        "access_token": "mock-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": (
                            datetime.now().astimezone() + timedelta(hours=24)
                        ).strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
            if request.url.path.endswith("/order-cash"):
                if failure_mode == "transport":
                    raise httpx.ReadTimeout(
                        "contains-sensitive-request-data", request=request
                    )
                if failure_mode == "http-500":
                    return httpx.Response(
                        500,
                        json={"rt_cd": "1", "msg1": "PRIVATE_SERVER_BODY"},
                    )
                return httpx.Response(
                    200,
                    headers={"x-correlation-id": "safe-correlation"},
                    json={"rt_cd": "0", "msg1": "accepted", "output": {}},
                )
            raise AssertionError(f"unexpected request: {request.url}")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            runtime = ConnectionRuntime(settings, http_client=client)
            request = OrderRequest(
                instrument=InstrumentId("005930"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                client_order_id="unknown-details-1",
                route=OrderRoute.AUTO,
            )
            try:
                try:
                    await runtime.submit_order("unknown-order-account", request)
                except RouterError as exc:
                    assert exc.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
                    assert exc.details["unknown_origin"] == "success_without_order_id"
                    assert exc.details["exception_type"] == "MissingProviderOrderId"
                else:
                    raise AssertionError("missing provider order id was accepted")

                failure_mode = "transport"
                try:
                    await runtime.submit_order(
                        "unknown-order-account",
                        OrderRequest(
                            instrument=request.instrument,
                            side=request.side,
                            order_type=request.order_type,
                            quantity=request.quantity,
                            client_order_id="unknown-details-2",
                            route=request.route,
                        ),
                    )
                except RouterError as exc:
                    assert exc.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
                    assert exc.details == {
                        "unknown_origin": "transport_error",
                        "exception_type": "ReadTimeout",
                    }
                    assert "sensitive" not in json.dumps(exc.details)
                else:
                    raise AssertionError("transport ambiguity was accepted")

                failure_mode = "http-500"
                try:
                    await runtime.submit_order(
                        "unknown-order-account",
                        OrderRequest(
                            instrument=request.instrument,
                            side=request.side,
                            order_type=request.order_type,
                            quantity=request.quantity,
                            client_order_id="unknown-details-3",
                            route=request.route,
                        ),
                    )
                except RouterError as exc:
                    assert exc.code is RouterErrorCode.ORDER_STATUS_UNKNOWN
                    assert exc.details == {
                        "unknown_origin": "http_status_unknown",
                        "exception_type": "HTTPStatusError",
                        "http_status": 500,
                    }
                    assert "PRIVATE_SERVER_BODY" not in exc.message
                else:
                    raise AssertionError("HTTP 500 ambiguity was treated as rejection")
            finally:
                await runtime.close()

    asyncio.run(run())


def test_kis_manual_cancel_child_closes_the_original_order(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="kis",
            target="LIVE",
            label="live-source",
            app_key="unit-app-key",
            app_secret="unit-app-secret",
        )
        settings.add_account(
            alias="live-account",
            connection_id=connection["id"],
            account_number="12345678",
            product_code="01",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "orders-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            if request.url.path.endswith("/inquire-daily-ccld"):
                return httpx.Response(
                    200,
                    headers={"tr_cont": ""},
                    json={
                        "rt_cd": "0",
                        "output1": [
                            {
                                "odno": "0018533300",
                                "pdno": "032685",
                                "sll_buy_dvsn_cd": "02",
                                "ord_dvsn_name": "지정가",
                                "ord_qty": "6",
                                "tot_ccld_qty": "0",
                                "rmn_qty": "6",
                                "ord_unpr": "7160",
                                "ord_dt": "20261015",
                                "ord_tmd": "143001",
                            },
                            {
                                "odno": "0018534400",
                                "orgn_odno": "0018533300",
                                "pdno": "032685",
                                "sll_buy_dvsn_cd": "02",
                                "rvse_cncl_dvsn_cd": "02",
                                "cncl_yn": "Y",
                                "ord_dvsn_name": "취소",
                                "ord_qty": "6",
                                "tot_ccld_qty": "0",
                                "rmn_qty": "0",
                                "ord_dt": "20261015",
                                "ord_tmd": "144512",
                            },
                        ],
                    },
                )
            if request.url.path.endswith("/inquire-psbl-rvsecncl"):
                return httpx.Response(200, json={"rt_cd": "0", "output": []})
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            all_orders = await runtime.account_orders(
                "live-account",
                state="all",
                business_date=datetime(2026, 10, 15).date(),
            )
            assert len(all_orders) == 1
            assert all_orders[0].provider_order_id == "0018533300"
            assert all_orders[0].status is OrderStatus.CANCELLED
            assert all_orders[0].remaining_quantity == 0
            assert all_orders[0].ordered_at.strftime("%H%M%S") == "143001"

            open_orders = await runtime.account_orders(
                "live-account",
                state="open",
                business_date=datetime(2026, 10, 15).date(),
            )
            assert open_orders == ()
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_rejected_cancel_child_does_not_close_original_order() -> None:
    day = datetime(2026, 10, 15).date()
    rows = [
        {
            "odno": "0018533300",
            "pdno": "032685",
            "sll_buy_dvsn_cd": "02",
            "ord_dvsn_name": "지정가",
            "ord_qty": "6",
            "tot_ccld_qty": "0",
            "rmn_qty": "6",
            "ord_unpr": "7160",
            "ord_dt": "20261015",
            "ord_tmd": "143001",
        },
        {
            "odno": "0018534400",
            "orgn_odno": "0018533300",
            "pdno": "032685",
            "sll_buy_dvsn_cd": "02",
            "rvse_cncl_dvsn_cd": "02",
            "cncl_yn": "Y",
            "ord_dvsn_name": "취소",
            "ord_qty": "6",
            "rjct_qty": "6",
            "tot_ccld_qty": "0",
            "rmn_qty": "0",
            "ord_dt": "20261015",
            "ord_tmd": "144512",
        },
    ]

    orders = _normalize_kis_account_orders(rows, "live-account", day)

    original = next(
        item for item in orders if item.provider_order_id == "0018533300"
    )
    assert original.status is OrderStatus.ACKNOWLEDGED
    assert original.remaining_quantity == Decimal("6")


def test_kis_partial_cancel_rejection_folds_only_confirmed_quantity() -> None:
    day = datetime(2026, 10, 15).date()
    rows = [
        {
            "odno": "original",
            "pdno": "032685",
            "sll_buy_dvsn_cd": "02",
            "ord_dvsn_name": "지정가",
            "ord_qty": "6",
            "tot_ccld_qty": "0",
            "rmn_qty": "6",
            "ord_unpr": "7160",
            "ord_dt": "20261015",
            "ord_tmd": "143001",
        },
        {
            "odno": "cancel-child",
            "orgn_odno": "original",
            "pdno": "032685",
            "sll_buy_dvsn_cd": "02",
            "rvse_cncl_dvsn_cd": "02",
            "cncl_yn": "Y",
            "ord_dvsn_name": "취소",
            "ord_qty": "6",
            "rjct_qty": "2",
            "tot_ccld_qty": "0",
            "rmn_qty": "0",
            "ord_dt": "20261015",
            "ord_tmd": "144512",
        },
    ]

    orders = _normalize_kis_account_orders(rows, "live-account", day)

    original = next(item for item in orders if item.provider_order_id == "original")
    assert original.status is OrderStatus.ACKNOWLEDGED
    assert original.remaining_quantity == Decimal("2")


def test_kis_read_retries_a_transient_broker_rate_limit(tmp_path: Path) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="paper-rate-limit",
            app_key="unit-app-key",
            app_secret="unit-app-secret",
        )
        settings.add_account(
            alias="paper-rate-account",
            connection_id=connection["id"],
            account_number="12345678",
            product_code="01",
        )
        balance_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal balance_calls
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "rate-limit-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            if request.url.path.endswith("/inquire-balance"):
                balance_calls += 1
                if balance_calls == 1:
                    return httpx.Response(
                        429,
                        headers={"retry-after": "0"},
                        json={
                            "rt_cd": "1",
                            "msg_cd": "EGW00201",
                            "msg1": "초당 거래건수를 초과하였습니다.",
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output1": [],
                        "output2": [{"dnca_tot_amt": "100000", "tot_evlu_amt": "100000"}],
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            balance = await runtime.balance("paper-rate-account")
            assert balance.cash == 100000
            assert balance_calls == 2
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_order_adapter_maps_normalized_request_without_real_submission(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        connection = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="paper-order-source",
            app_key="unit-app-key",
            app_secret="unit-app-secret",
        )
        live_connection = settings.add_connection(
            provider="kis",
            target="LIVE",
            label="live-order-source",
            app_key="live-unit-app-key",
            app_secret="live-unit-app-secret",
        )
        settings.add_account(
            alias="paper-order-account",
            connection_id=connection["id"],
            account_number="12345678",
            product_code="01",
        )
        settings.add_account(
            alias="live-order-account",
            connection_id=live_connection["id"],
            account_number="87654321",
            product_code="01",
        )
        submitted: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "order-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            if request.url.path.endswith("/order-cash"):
                submitted.append(request)
                is_live = request.url.host == "openapi.koreainvestment.com"
                if is_live:
                    assert request.headers["tr_id"] == "TTTC0011U"
                    assert b'"CANO":"87654321"' in request.content
                    assert b'"PDNO":"000660"' in request.content
                    assert b'"ORD_DVSN":"00"' in request.content
                    assert b'"ORD_UNPR":"200000"' in request.content
                    assert b'"EXCG_ID_DVSN_CD":"SOR"' in request.content
                else:
                    assert request.headers["tr_id"] == "VTTC0012U"
                    assert b'"CANO":"12345678"' in request.content
                    assert b'"PDNO":"005930"' in request.content
                    assert b'"ORD_DVSN":"01"' in request.content
                    assert b'"EXCG_ID_DVSN_CD":"KRX"' in request.content
                return httpx.Response(
                    200,
                    headers={"x-correlation-id": "order-contract-test"},
                    json={
                        "rt_cd": "0",
                        "msg_cd": "APBK0013",
                        "msg1": "주문 전송 완료 되었습니다.",
                        "output": {"ODNO": "000001"},
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            capabilities = runtime.capabilities("paper-order-account")
            assert capabilities.cash_orders is True
            assert capabilities.order_routes == (OrderRoute.KRX, OrderRoute.AUTO)
            live_capabilities = runtime.capabilities(live_connection["id"])
            assert live_capabilities.cash_orders is True
            assert OrderRoute.NXT in live_capabilities.order_routes
            assert OrderRoute.SOR in live_capabilities.order_routes
            acknowledgement = await runtime.submit_order(
                "paper-order-account",
                OrderRequest(
                    instrument=InstrumentId("005930"),
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=Decimal("1"),
                    client_order_id="contract-only-no-real-order",
                    route=OrderRoute.AUTO,
                ),
            )
            assert acknowledgement.provider_order_id == "000001"
            assert acknowledgement.provider == "kis"
            live_acknowledgement = await runtime.submit_order(
                "live-order-account",
                OrderRequest(
                    instrument=InstrumentId("000660"),
                    side=OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=Decimal("2"),
                    price=Decimal("200000"),
                    client_order_id="live-contract-only-no-real-order",
                    route=OrderRoute.AUTO,
                ),
            )
            assert live_acknowledgement.provider_order_id == "000001"
            assert live_acknowledgement.provider == "kis"
            assert len(submitted) == 2
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())


def test_kis_amend_and_cancel_use_possible_order_lookup_before_submission(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        settings = SettingsStore(workspace(tmp_path / "workspace"))
        paper_connection = settings.add_connection(
            provider="kis",
            target="PAPER",
            label="paper-change-source",
            app_key="paper-change-key",
            app_secret="paper-change-secret",
        )
        live_connection = settings.add_connection(
            provider="kis",
            target="LIVE",
            label="live-change-source",
            app_key="live-change-key",
            app_secret="live-change-secret",
        )
        settings.add_account(
            alias="paper-change-account",
            connection_id=paper_connection["id"],
            account_number="12345678",
            product_code="01",
        )
        settings.add_account(
            alias="live-change-account",
            connection_id=live_connection["id"],
            account_number="87654321",
            product_code="01",
        )
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/tokenP":
                expiry = (datetime.now().astimezone() + timedelta(hours=24)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "change-order-token",
                        "token_type": "Bearer",
                        "access_token_token_expired": expiry,
                    },
                )
            if request.url.path.endswith("/inquire-psbl-rvsecncl"):
                calls.append(("lookup", request.headers["tr_id"]))
                expected = (
                    "TTTC0084R"
                    if request.url.host == "openapi.koreainvestment.com"
                    else "VTTC0084R"
                )
                assert request.headers["tr_id"] == expected
                return httpx.Response(
                    200,
                    json={
                        "rt_cd": "0",
                        "output": [
                            {
                                "odno": "000123",
                                "ord_gno_brno": "91252",
                                "psbl_qty": "3",
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/order-rvsecncl"):
                calls.append(("change", request.headers["tr_id"]))
                is_live = request.url.host == "openapi.koreainvestment.com"
                assert request.headers["tr_id"] == (
                    "TTTC0013U" if is_live else "VTTC0013U"
                )
                assert b'"ORGN_ODNO":"000123"' in request.content
                assert b'"KRX_FWDG_ORD_ORGNO":"91252"' in request.content
                if is_live:
                    assert b'"RVSE_CNCL_DVSN_CD":"01"' in request.content
                    assert b'"ORD_QTY":"2"' in request.content
                    assert b'"ORD_UNPR":"73500"' in request.content
                    assert b'"QTY_ALL_ORD_YN":"N"' in request.content
                    assert b'"EXCG_ID_DVSN_CD":"SOR"' in request.content
                else:
                    assert b'"RVSE_CNCL_DVSN_CD":"02"' in request.content
                    assert b'"ORD_QTY":"3"' in request.content
                    assert b'"ORD_UNPR":"0"' in request.content
                    assert b'"QTY_ALL_ORD_YN":"Y"' in request.content
                    assert b'"EXCG_ID_DVSN_CD":"KRX"' in request.content
                return httpx.Response(
                    200,
                    headers={"x-correlation-id": "change-order-contract"},
                    json={
                        "rt_cd": "0",
                        "msg1": "주문 변경 접수",
                        "output": {
                            "ODNO": "000124",
                            "KRX_FWDG_ORD_ORGNO": "91252",
                        },
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = ConnectionRuntime(settings, http_client=client)
        try:
            cancelled = await runtime.cancel_order(
                "paper-change-account",
                "000123",
                quantity=None,
                route=OrderRoute.AUTO,
            )
            assert cancelled.provider_order_id == "000124"
            amended = await runtime.amend_order(
                "live-change-account",
                "000123",
                quantity=Decimal("2"),
                order_type=OrderType.LIMIT,
                price=Decimal("73500"),
                route=OrderRoute.AUTO,
            )
            assert amended.provider_order_id == "000124"
            assert calls == [
                ("lookup", "VTTC0084R"),
                ("change", "VTTC0013U"),
                ("lookup", "TTTC0084R"),
                ("change", "TTTC0013U"),
            ]
        finally:
            await runtime.close()
            await client.aclose()

    asyncio.run(run())
