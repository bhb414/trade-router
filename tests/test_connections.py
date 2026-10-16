from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

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
from trade_router_studio.connections import ConnectionRuntime, _parse_period_row
from trade_router_studio.paths import WorkspacePaths
from trade_router_studio.settings import SettingsStore


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
