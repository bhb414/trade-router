from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
import json
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from trade_router_studio.app import create_app
from trade_router_studio.block_packages import DEFAULT_CONDITION_BOLLINGER_SETTINGS, compile_block_strategy
from trade_router_studio.diagnostics import build_diagnostics, MAX_LOGS, SEOUL, _reason, _number, _cost_projection, _sell_settings
from test_studio import workspace


NOW = datetime(2026, 10, 22, 13, 10, tzinfo=SEOUL)
SECRETS = ["PRIVATE_API_KEY_SENTINEL", "PRIVATE_TOKEN_SENTINEL", "12345678-91",
           "PRIVATE_HTS_ID", "PRIVATE_ACCOUNT_LABEL", "PRIVATE_STRATEGY_NAME.py",
           "PRIVATE_STRATEGY_ID", "PRIVATE_PROVIDER_ORDER_ID", "PRIVATE_CLIENT_ORDER_ID",
           "C:\\Users\\PRIVATE_USER\\profile", "https://private.example:9999",
           "192.168.91.33", "987654321.876", "PRIVATE_INSTRUMENT_CODE"]


@pytest.mark.parametrize("flow, expected", [
    ({"sell_start_time": "16:00:00", "sell_end_time": "19:59:00"}, ("POSITION_SCOPE", "16:00:00", "19:59:00")),
    ({"target_mode": "BUY_FLOWS", "sell_start_time": "00:00", "sell_end_time": "23:59:59"}, ("BUY_FLOWS", "08:00", "20:00")),
    ({"target_mode": "POSITION_SCOPE", "operating_start_time": "10:10:01", "operating_end_time": "14:20:30"}, ("POSITION_SCOPE", "10:10:01", "14:20:30")),
])
def test_diagnostics_reports_operating_window_for_old_and_new_sell_rules(flow, expected):
    settings = _sell_settings({"sell_enabled": True, "sell_flows": [flow]})
    exported = settings["flows"][0]
    assert (exported["target_mode"], exported["start"], exported["end_exclusive"]) == expected


def unpack(content):
    with ZipFile(BytesIO(content)) as archive:
        assert set(archive.namelist()) == {"README.txt", "diagnostics.json"}
        text = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
        return json.loads(archive.read("diagnostics.json")), text


def fixture_input():
    configuration = DEFAULT_CONDITION_BOLLINGER_SETTINGS.model_copy(update={
        "strategy_id": SECRETS[6], "trading_account": SECRETS[4], "hts_user_id": SECRETS[3],
        "expected_condition_name": SECRETS[0],
    })
    source = compile_block_strategy(configuration)
    message = "실시간 익절·손절 매도 실패 [ORDER_REJECTED]: " + " ".join(SECRETS)
    logs = [{"timestamp": NOW.isoformat(), "level": "error", "message": message}]
    manager = SimpleNamespace(
        list=lambda: [{"name": SECRETS[5], "strategy_type": "block", "running": True,
                      "stop_requested": False, "runtime_managed": True,
                      "started_at": (NOW - timedelta(hours=2)).isoformat()}],
        read=lambda name: source,
        logs=lambda name: {"entries": logs, "next": len(logs)},
    )
    orders = [{"id": SECRETS[7], "strategy_id": SECRETS[6], "account_alias": SECRETS[4],
               "client_order_id": SECRETS[8], "provider_order_id": SECRETS[7],
               "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(), "status": "REJECTED",
               "request": {"side": "SELL", "order_type": "MARKET", "route": "KRX",
                           "code": SECRETS[13], "quantity": SECRETS[12], "price": SECRETS[12]},
               "filled_quantity": SECRETS[12], "average_fill_price": SECRETS[12],
               "message": message, "provider_metadata": {"secret": SECRETS}}]
    events = [{"source": SECRETS[5], "occurred_at": NOW.isoformat(),
               "event_type": "order.rejected", "message": message,
               "details": {"order_id": SECRETS[7], "error_code": "ORDER_REJECTED", "secrets": SECRETS}}]
    store = SimpleNamespace(orders=lambda limit: orders,
                            events=lambda limit, event_types=None: [item for item in events
                                if event_types is None or item["event_type"] in event_types],
                            unresolved_orders=lambda: [])
    arguments = dict(version="0.1.70", manager=manager, store=store, now=NOW,
                     permissions=[{"scope_type": "account", "scope_id": SECRETS[4], "armed": True, "reconciled": True},
                                  {"scope_type": "strategy", "scope_id": SECRETS[6], "armed": True}],
                     accounts=[{"alias": SECRETS[4], "id": SECRETS[2], "cost_rates": {"sell_tax_rate": "0.002"},
                                "risk_limits": {"max_order_amount": SECRETS[12]}}],
                     connection_states=[{"id": SECRETS[10], "state": "connected", "message": message}],
                     realtime_states=[{"connection_id": SECRETS[10], "state": "connected", "subscriptions": 3,
                                       "last_received_at": NOW.isoformat(), "message": message}])
    return arguments, logs, orders, events


def test_export_allowlist_excludes_all_raw_credentials_and_financial_values():
    arguments, _, _, _ = fixture_input()
    content, filename = build_diagnostics(**arguments)
    data, text = unpack(content)
    assert filename == "trade-router-diagnostics-20261022-131000.zip"
    for secret in SECRETS:
        assert secret not in text
        assert json.dumps(secret, ensure_ascii=False)[1:-1] not in text
    strategy = data["current_snapshot"]["strategies"][0]
    assert strategy["running"] is True
    assert strategy["order_permission"] is True
    assert strategy["saved_sell_settings"]["flows"][0]["start"] == "09:00"
    # Original strategy names/IDs and account aliases are consistent pseudonyms.
    assert strategy["strategy"] == data["history"]["orders"][0]["strategy"]
    assert strategy["account"] == data["current_snapshot"]["accounts"][0]["account"]
    assert data["history"]["events"][0]["order"] == data["history"]["orders"][0]["order"]
    assert data["history"]["orders"][0]["instrument"] == "instrument-1"
    assert data["history"]["strategy_logs"][0]["reason"] == "LIVE_EXIT_ORDER_FAILED"
    assert data["history"]["strategy_logs"][0]["error_code"] == "ORDER_REJECTED"
    account = data["current_snapshot"]["accounts"][0]
    assert account["cost_fields"]["buy_commission_rate"] == "blank_zero"
    assert account["risk_limits_configured"]["max_order_amount"] is True
    assert "-3" in json.dumps(strategy["saved_sell_settings"])


def test_history_is_bounded_and_unknown_text_never_passes_through():
    arguments, logs, orders, events = fixture_input()
    old = (NOW - timedelta(hours=73)).isoformat()
    future = (NOW + timedelta(hours=1)).isoformat()
    logs[:] = [{"timestamp": NOW.isoformat(), "level": SECRETS[0], "message": SECRETS[0]} for _ in range(MAX_LOGS + 5)]
    logs[-1]["timestamp"] = old
    logs[-2]["timestamp"] = future
    orders[0]["updated_at"] = old
    orders[0]["status"] = "ACKNOWLEDGED"
    arguments["store"].unresolved_orders = lambda: orders
    events[0]["event_type"] = SECRETS[1]
    events[0]["details"]["error_code"] = {"secret": SECRETS[2]}
    events[0]["message"] = SECRETS[0]
    content, _ = build_diagnostics(**arguments)
    data, text = unpack(content)
    assert SECRETS[0] not in text and SECRETS[1] not in text
    assert len(data["history"]["strategy_logs"]) == MAX_LOGS - 2
    assert data["history"]["orders"] == []
    assert data["current_snapshot"]["unresolved_orders"][0]["status"] == "ACKNOWLEDGED"
    assert data["history"]["events"][0]["event"] == "UNKNOWN"
    assert data["history"]["events"][0]["reason"] == "UNCLASSIFIED"
    assert data["coverage"]["strategy_logs"]["strategy-1"]["older_entries_unavailable_or_omitted"] is True


def test_malformed_block_source_is_not_attached_or_executed():
    arguments, _, _, _ = fixture_input()
    arguments["manager"].read = lambda name: "raise RuntimeError('" + SECRETS[0] + "')"
    content, _ = build_diagnostics(**arguments)
    data, text = unpack(content)
    assert data["current_snapshot"]["strategies"][0]["saved_sell_settings"] is None
    assert SECRETS[0] not in text


@pytest.mark.parametrize("message,reason", [
    ("전략 프로세스가 종료되어 일정·실시간 구독·세션을 정리했습니다.", "PROCESS_EXITED"),
    ("순수익률 조건 보류: 계좌의 매수·매도 수수료율과 매도 세율을 모두 설정해야 합니다.", "COST_RATES_MISSING"),
    ("순수익률 조건 보류: 계좌 수수료율·세율에 잘못된 값이 있습니다.", "COST_RATES_INVALID"),
    ("계좌 비용 설정 확인 · 순수익률 조건 판단을 재개합니다.", "COST_RATES_READY"),
    ("종목 · 흐름 · 실시간 현재가 123원 (조건) · MARKET 1주 매도 요청: ACKNOWLEDGED", "LIVE_EXIT_ORDER_ATTEMPT"),
    ("종목 실시간 익절·손절 매도 건너뜀: 세션에서 지원하지 않습니다", "LIVE_EXIT_SESSION_SKIPPED"),
    ("종목 실시간 익절·손절 매도 중단 [ORDER_REJECTED]: 당일 재시도하지 않습니다", "LIVE_EXIT_RETRY_SUPPRESSED"),
    ("종목 KRX 실시간 익절·손절 구독 보류 [RATE_LIMITED]: 요청 제한", "LIVE_EXIT_SUBSCRIPTION_FAILED"),
])
def test_actual_runtime_messages_are_classified_without_retaining_text(message, reason):
    assert _reason(message)["reason"] == reason


def test_numeric_projection_rejects_unbounded_decimal_strings():
    assert _number("0." + "0" * 10000 + "1", 0, 1000) is None


@pytest.mark.parametrize("rates,combined", [
    ({}, True),
    ({"sell_commission_rate": "0.6", "sell_tax_rate": "0.4"}, False),
    ({"sell_commission_rate": "0.9999999", "sell_tax_rate": "0"}, True),
    ({"sell_commission_rate": "1", "sell_tax_rate": "0"}, False),
])
def test_combined_sell_cost_validity_matches_runtime_without_exporting_rates(rates, combined):
    result = _cost_projection(rates)
    assert result["combined_sell_cost_valid"] is combined
    assert not any(value in json.dumps(result) for value in ("0.6", "0.4", "0.9999999"))


def test_endpoint_download_is_read_only_local_and_never_calls_broker(tmp_path, monkeypatch):
    app = create_app(workspace(tmp_path))
    with TestClient(app) as client:
        def no_network(*args, **kwargs):
            raise AssertionError("Diagnostic export must not call a broker")
        for name in ("balance", "account_orders", "quote", "submit_order", "check"):
            monkeypatch.setattr(app.state.connections, name, no_network)
        before = client.get("/api/runtime/order-permissions").json()
        response = client.get("/api/system/diagnostics/export?hours=24")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment;" in response.headers["content-disposition"]
        assert "no-store" in response.headers["cache-control"]
        data, text = unpack(response.content)
        assert data["history_window"]["hours"] == 24
        assert str(tmp_path) not in text
        assert client.get("/api/runtime/order-permissions").json() == before
        assert client.get("/api/system/diagnostics/export", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.get("/api/system/diagnostics/export", headers={"Origin": "https://untrusted.example"}).status_code == 403
        assert client.get("/api/system/diagnostics/export", headers={"Origin": "http://testserver"}).status_code == 200


@pytest.mark.parametrize("hours", [0, 169, "bad"])
def test_export_rejects_unbounded_time_window(tmp_path, hours):
    with TestClient(create_app(workspace(tmp_path))) as client:
        assert client.get(f"/api/system/diagnostics/export?hours={hours}").status_code == 422
