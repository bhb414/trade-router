"""Bounded, allowlisted support export. Never attach raw logs or user files."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import re
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from trade_router import OrderStatus, RouterErrorCode

from .block_packages import block_package_from_source
from .process_diagnostics import EXCEPTION_TYPES, safe_exit_details

SEOUL = ZoneInfo("Asia/Seoul")
MAX_STRATEGIES = 50
MAX_HISTORY = 1000
MAX_CRITICAL_HISTORY = 1000
MAX_LOGS = 500
MAX_EXITS = 200
ERROR_CODES = {item.value for item in RouterErrorCode}
METRICS = frozenset({
    "open", "high", "low", "close", "volume", "heikin_open", "heikin_high",
    "heikin_low", "heikin_close", "bollinger_upper", "bollinger_middle",
    "bollinger_lower", "obv", "atr", "average_price", "profit_rate", "value",
})
EVENT_TYPES = {
    "strategy.start", "strategy.stop", "strategy.process_exit", "strategy.manifest",
    "strategy.schedules", "strategy.restart_restore", "strategy.restart_restore_error",
    "order.submitting", "order.acknowledged", "order.partially_filled", "order.filled",
    "order.rejected", "order.unknown", "order.cancelled", "order.cancel_pending",
    "order.amended", "order.reconcile_error", "order.unknown_reconciling",
    "order.reconciled", "order.manually_resolved", "order.unknown_resolution_error",
} | {
    f"service.{service}.{operation}.{outcome}"
    for service, operations in {
        "market": ("quote", "bars", "conditions", "condition_matches", "ranking"),
        "account": ("balance", "orders", "buying_power"),
        "order": ("submit",), "scheduler": ("deliver", "register"),
    }.items()
    for operation in operations for outcome in ("success", "error")
}
CRITICAL_ORDER_EVENT_TYPES = (
    "order.submitting", "order.acknowledged", "order.partially_filled",
    "order.filled", "order.rejected", "order.unknown", "order.cancelled",
    "order.cancel_pending", "order.amended", "order.reconcile_error",
    "order.unknown_reconciling", "order.reconciled", "order.manually_resolved",
    "order.unknown_resolution_error", "service.order.submit.success",
    "service.order.submit.error",
)
EVENT_REASONS = {
    "order.unknown": "ORDER_STATUS_UNKNOWN",
    "order.unknown_reconciling": "ORDER_STATUS_UNKNOWN",
    "order.reconcile_error": "ORDER_RECONCILE_ERROR",
    "order.unknown_resolution_error": "ORDER_RECONCILE_ERROR",
}
SOURCE_KINDS = frozenset({"system", "strategy", "account"})
UNKNOWN_ORIGINS = frozenset({
    "transport_error", "success_without_order_id", "restart_submitting",
    "unexpected_adapter_error", "legacy_unknown", "amend_exception",
    "cancel_exception", "http_status_unknown",
})
FAILURE_PHASES = frozenset({"submit", "modify", "reconcile"})
MATCH_RESULTS = frozenset({
    "not_checked", "none", "ambiguous", "unique", "linked", "not_accepted",
    "ambiguous_modification", "modification_pending", "linked_modification",
    "manual_required_no_order_time", "amendment_successor_missing",
    "manual_required_missing_parent_relation", "manual_linked_modification",
    "auto_cancel_reconciled", "manual_modification_not_accepted",
    "realtime_rejected", "broker_amendment_rejected",
    "manual_required_legacy_modification", "legacy_modification_pending",
    "amendment_rejected_parent_missing",
})
RESOLUTIONS = frozenset({
    "auto_linked", "manual_linked", "manual_not_accepted",
    "auto_amendment_linked", "broker_terminal",
    "manual_modification_not_accepted", "realtime_rejected",
    "manual_amendment_linked", "auto_cancel_reconciled",
    "broker_amendment_rejected",
})
# Match diagnostic meaning, not text to retain. No substring from a message is exported.
REASONS = (
    ("사용자 요청으로 전략을 중지", "USER_STOPPED"),
    ("전략 프로세스가 종료", "PROCESS_EXITED"),
    ("계좌의 매수·매도 수수료율과 매도 세율을 모두 설정", "COST_RATES_MISSING"),
    ("계좌 수수료율·세율에 잘못된 값", "COST_RATES_INVALID"),
    ("비용 설정 누락", "COST_RATES_MISSING"),
    ("비용 설정 오류", "COST_RATES_INVALID"),
    ("순수익률 조건 판단을 보류", "COST_RATES_INVALID"),
    ("순수익률 조건 판단을 재개", "COST_RATES_READY"),
    ("실시간 익절·손절 보완 시세 조회 실패", "FALLBACK_QUOTE_FAILED"),
    ("실시간 익절·손절 보완 조회 보류", "FALLBACK_ACCOUNT_FAILED"),
    ("실시간 익절·손절 확인 보류", "LIVE_EXIT_ACCOUNT_FAILED"),
    ("실시간 익절·손절 매도 실패", "LIVE_EXIT_ORDER_FAILED"),
    ("실시간 익절·손절 매도 건너뜀", "LIVE_EXIT_SESSION_SKIPPED"),
    ("실시간 익절·손절 매도 중단", "LIVE_EXIT_RETRY_SUPPRESSED"),
    ("실시간 익절·손절 구독 보류", "LIVE_EXIT_SUBSCRIPTION_FAILED"),
    ("조건 자료 조회 실패", "CONDITION_DATA_FAILED"),
    ("주문 행동이 없습니다", "NO_ORDER_ACTION"),
    ("매도 흐름 없음", "SELL_DISABLED"),
    ("매도 판단 보류", "SELL_CHECK_FAILED"),
    ("매도 요청 실패", "SELL_ORDER_FAILED"),
    ("미체결 주문", "PENDING_ORDER"),
    ("적용 시간이 종료", "OUTSIDE_FLOW_TIME"),
    ("주문 허용", "ORDER_PERMISSION"),
    ("조회 제한", "RATE_LIMITED"),
    ("주문을 차단", "ORDER_BLOCKED"),
    ("시세가 오래", "STALE_MARKET_DATA"),
    ("실시간 구독", "REALTIME_SUBSCRIPTION"),
    ("실시간 현재가 감시", "LIVE_EXIT_WATCHDOG_READY"),
    ("Traceback", "SCRIPT_EXCEPTION"),
)


def _enum(value, allowed, default="UNKNOWN"):
    return value if isinstance(value, str) and value in allowed else default


def _timestamp(value):
    try:
        text = str(value)
        if len(text) > 40:
            return None
        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=SEOUL)
        return stamp.astimezone(SEOUL).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _number(value, low, high):
    try:
        text = str(value)
        if len(text) > 64:
            return None
        number = Decimal(text)
        if number.is_finite() and low <= number <= high:
            return str(number)
    except (InvalidOperation, ValueError, TypeError):
        pass
    return None


def _clock(value):
    text = str(value)
    return text if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", text) else None


def _cost_projection(rates):
    states, values = {}, {}
    for key in ("buy_commission_rate", "sell_commission_rate", "sell_tax_rate"):
        raw = rates.get(key)
        blank = raw is None or not str(raw).strip()
        parsed = _number("0" if blank else str(raw).strip(), 0, 1)
        value = Decimal(parsed) if parsed is not None and Decimal(parsed) < 1 else None
        values[key] = value
        states[key] = "blank_zero" if blank else "valid" if value is not None else "invalid"
    sell_fee, tax = values["sell_commission_rate"], values["sell_tax_rate"]
    return {"cost_fields": states, "combined_sell_cost_valid": (
        sell_fee is not None and tax is not None and sell_fee + tax < 1)}


def _reason(message, details=None, *, event_type=None):
    text = str(message or "")[:8192]
    details = details if isinstance(details, dict) else {}
    error = next((details.get(key) for key in ("error_code", "code")
                  if isinstance(details.get(key), str) and details.get(key) in ERROR_CODES), None)
    if error is None:
        error = next((code for code in sorted(ERROR_CODES) if f"[{code}]" in text), None)
    reason = EVENT_REASONS.get(event_type)
    if reason is None:
        reason = next((reason for pattern, reason in REASONS if pattern in text), "UNCLASSIFIED")
    if reason == "UNCLASSIFIED" and "실시간 현재가" in text and "매도 요청:" in text:
        reason = "LIVE_EXIT_ORDER_ATTEMPT"
    return {"reason": reason, "error_code": error}


def _integer(value, low=0, high=1_000_000):
    if type(value) is int and low <= value <= high:
        return value
    return None


def _safe_unknown_fields(raw, *, provider_id_present=None):
    """Project only bounded enums/counters from uncertain-order metadata."""

    source = raw if isinstance(raw, dict) else {}
    present = provider_id_present
    if type(present) is not bool:
        present = source.get("provider_id_present")
    if type(present) is not bool:
        present = None
    exception_type = source.get("exception_type")
    winerror = source.get("winerror")
    return {
        "provider_id_present": present,
        "unknown_origin": _enum(source.get("unknown_origin"), UNKNOWN_ORIGINS, None),
        "failure_phase": _enum(source.get("failure_phase"), FAILURE_PHASES, None),
        "exception_type": (
            exception_type
            if isinstance(exception_type, str) and exception_type in EXCEPTION_TYPES
            else None
        ),
        "winerror": (
            winerror if type(winerror) is int and 0 <= winerror <= 65535 else None
        ),
        "http_status": _integer(source.get("http_status"), 100, 599),
        "first_unknown_at": _timestamp(source.get("first_unknown_at")),
        "reconcile_attempt_count": _integer(source.get("reconcile_attempt_count")),
        "last_reconcile_at": _timestamp(source.get("last_reconcile_at")),
        "candidate_count": _integer(source.get("candidate_count"), 0, 10_000),
        "match_result": _enum(source.get("match_result"), MATCH_RESULTS, None),
        "resolution": _enum(
            source.get("resolution") or source.get("modification_resolution"),
            RESOLUTIONS,
            None,
        ),
    }


class _Ids:
    def __init__(self):
        self.values = {}

    def get(self, kind, value):
        if value is None or value == "":
            return None
        group = self.values.setdefault(kind, {})
        return group.setdefault(str(value), f"{kind}-{len(group) + 1}")

    def alias(self, kind, value, anonymous):
        if value:
            self.values.setdefault(kind, {})[str(value)] = anonymous


def snapshot_manager(manager):
    """Copy live deques on their event loop before doing file/DB/ZIP work in a thread."""
    rows = manager.list()
    logs = {row["name"]: manager.logs(row["name"]) for row in rows[:MAX_STRATEGIES]}
    return SimpleNamespace(list=lambda: rows, logs=lambda name: logs[name], read=manager.read)


def _expression(raw, *, allow_constant=False, depth=0):
    if not isinstance(raw, dict) or depth > 5:
        return None
    kind = raw.get("kind")
    if kind == "constant":
        # Only percentage thresholds are exported; prices, budgets and quantities are not.
        return {"kind": "constant", "value": _number(raw.get("value"), -100, 1000)
                if allow_constant else None, "value_omitted": not allow_constant}
    if kind == "binary":
        return {"kind": "binary", "operator": _enum(raw.get("operator"),
                {"ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}),
                "left": _expression(raw.get("left"), depth=depth + 1),
                "right": _expression(raw.get("right"), depth=depth + 1)}
    return {"kind": "metric", "metric": _enum(raw.get("metric"), METRICS),
            "offset": _number(raw.get("offset", 0), 0, 200),
            "bar_interval": raw.get("bar_interval") if re.fullmatch(
                r"(?:[1-9]\d{0,2}m|1d)", str(raw.get("bar_interval"))) else None,
            "bar_data_mode": _enum(raw.get("bar_data_mode"), {"COMPLETE_ONLY", "INCLUDE_CURRENT"}),
            "bollinger_period": _number(raw.get("bollinger_period"), 2, 2000),
            "bollinger_stddev": _number(raw.get("bollinger_stddev"), 0, 20),
            "atr_period": _number(raw.get("atr_period"), 1, 400)}


def _condition(raw):
    if not isinstance(raw, dict):
        return None
    expressions = []
    for side in ("left", "right"):
        expression = raw.get(f"{side}_expression")
        if not expression:
            if raw.get(side) == "value":
                expression = {"kind": "constant", "value": raw.get("value")}
            else:
                expression = {"kind": "metric", "metric": raw.get(side), **{
                    key: raw.get(f"{side}_{key}") for key in (
                        "offset", "bar_interval", "bar_data_mode", "bollinger_period",
                        "bollinger_stddev", "atr_period")}}
        expressions.append(expression)
    direct_profit = any(isinstance(item, dict) and item.get("kind") == "metric"
                        and item.get("metric") == "profit_rate" for item in expressions)
    return {"operator": _enum(raw.get("operator"), {"LT", "LTE", "GT", "GTE", "EQ", "CROSS_UP", "CROSS_DOWN"}),
            "left": _expression(expressions[0], allow_constant=direct_profit),
            "right": _expression(expressions[1], allow_constant=direct_profit)}


def _tree(raw, depth=0):
    if not isinstance(raw, dict) or depth > 8:
        return None
    action = raw.get("action")
    result = {"kind": _enum(raw.get("kind"), {"condition", "logic"}), "action": None}
    if isinstance(action, dict):
        result["action"] = {"side": _enum(action.get("side"), {"BUY", "SELL"}),
                            "order_type": _enum(action.get("order_type"), {"MARKET", "LIMIT", "BEST_LIMIT"})}
    if raw.get("kind") == "condition":
        result["condition"] = _condition(raw.get("condition"))
    else:
        result["operator"] = _enum(raw.get("operator"), {"ALL", "ANY"})
        result["children"] = [_tree(item, depth + 1) for item in raw.get("children", [])[:20]]
    return result


def _sell_settings(config):
    flows = config.get("sell_flows") or [config]
    def operating_window(flow):
        legacy = flow.get("target_mode", "LEGACY_TIME") == "LEGACY_TIME"
        return (
            _clock(flow.get("operating_start_time", flow.get("sell_start_time", "09:00") if legacy else "08:00")),
            _clock(flow.get("operating_end_time", flow.get("sell_end_time", "15:20") if legacy else "20:00")),
        )
    return {
        "sell_enabled": config.get("sell_enabled") is True,
        "position_scope": _enum(config.get("sell_position_scope"), {"STRATEGY", "ACCOUNT"}),
        "market_venue": _enum(config.get("market_venue"), {"KRX", "NXT", "INTEGRATED"}),
        "route": _enum(config.get("route"), {"KRX", "NXT", "SOR", "AUTO"}),
        "sell_order_type": _enum(config.get("sell_order_type"), {"MARKET", "LIMIT", "BEST_LIMIT"}),
        "flows": [{"flow": index + 1,
                   "target_mode": _enum("POSITION_SCOPE" if flow.get("target_mode", "LEGACY_TIME") == "LEGACY_TIME" else flow.get("target_mode"), {"BUY_FLOWS", "POSITION_SCOPE"}),
                   "linked_flow_count": min(len(flow.get("buy_flow_ids") or []), 64),
                   "linked_node_count": min(len(flow.get("buy_node_refs") or []), 64),
                   "start": operating_window(flow)[0],
                   "end_exclusive": operating_window(flow)[1],
                   "scan_seconds": _number(flow.get("sell_scan_seconds", 30), 1, 86400),
                   "rules": _tree(flow.get("sell_rule_tree")),
                   "legacy_groups": [[_condition(condition) for condition in group.get("conditions", [])[:20]]
                                     for group in flow.get("sell_condition_groups", [])[:8]]}
                  for index, flow in enumerate(flows[:8])],
    }


def build_diagnostics(*, version, manager, store, permissions, accounts,
                      connection_states, realtime_states, manifests=None, hours=72, now=None):
    """Read local state only; return ZIP bytes and a non-identifying filename."""
    now = now or datetime.now(SEOUL)
    now = now.astimezone(SEOUL)
    cutoff = now - timedelta(hours=hours)
    ids = _Ids()
    rows = manager.list()
    strategies, logs = [], []
    history_meta = {}
    permission_map = {(item.get("scope_type"), item.get("scope_id")): item for item in permissions}
    # Strategies may refer to the account by its ID or one of its previous aliases.
    for account in accounts[:100]:
        anonymous = ids.get("account", account.get("alias"))
        ids.alias("account", account.get("id"), anonymous)
        for previous in account.get("previous_aliases", []):
            ids.alias("account", previous, anonymous)

    def recent(items, key):
        return [item for item in items if (stamp := _timestamp(item.get(key)))
                and cutoff <= datetime.fromisoformat(stamp) <= now]

    for row in rows[:MAX_STRATEGIES]:
        name = row["name"]
        anonymous = ids.get("strategy", name)
        config = None
        if row.get("strategy_type") == "block":
            try:
                source = manager.read(name)
                if len(source.encode("utf-8")) <= 512 * 1024:
                    config = block_package_from_source(name, source).settings.model_dump(mode="json")
            except (OSError, ValueError, TypeError, SyntaxError):
                pass
        manifest = (manifests or {}).get(name)
        strategy_id = config.get("strategy_id") if config else getattr(manifest, "strategy_id", None)
        ids.alias("strategy", strategy_id, anonymous)
        account = config.get("trading_account") if config else getattr(manifest, "trading_account", None)
        permission = permission_map.get(("strategy", strategy_id), {})
        file_permission = permission_map.get(("strategy_file", name), {})
        strategies.append({
            "strategy": anonymous, "type": _enum(row.get("strategy_type"), {"block", "script"}),
            "running": row.get("running") is True, "stop_requested": row.get("stop_requested") is True,
            "runtime_managed": row.get("runtime_managed") is True,
            "started_at": _timestamp(row.get("started_at")), "stopped_at": _timestamp(row.get("stopped_at")),
            "order_permission": permission.get("armed") is True,
            "saved_order_permission": file_permission.get("armed") is True,
            "account": ids.get("account", account),
            "saved_sell_settings": _sell_settings(config) if config else None,
        })
        raw_logs = manager.logs(name)
        entries = raw_logs.get("entries", [])
        selected = recent(entries[-MAX_LOGS:], "timestamp")
        logs.extend({"strategy": anonymous, "timestamp": _timestamp(item.get("timestamp")),
                     "level": _enum(item.get("level"), {"info", "warning", "error", "success", "output"}),
                     **_reason(item.get("message"))} for item in selected)
        history_meta[anonymous] = {"retained_entries": len(entries), "exported_entries": len(selected),
                                  "limit": MAX_LOGS,
                                  "older_entries_unavailable_or_omitted": int(raw_logs.get("next", 0)) > len(selected)}

    account_rows = []
    for account in accounts[:100]:
        alias = account.get("alias")
        anonymous = ids.get("account", alias)
        ids.alias("account", account.get("id"), anonymous)
        for previous in account.get("previous_aliases", []):
            ids.alias("account", previous, anonymous)
        permission = permission_map.get(("account", alias), {})
        rates = account.get("cost_rates") or {}
        risk = account.get("risk_limits") or {}
        account_rows.append({"account": anonymous, "order_permission": permission.get("armed") is True,
                             "reconciled": permission.get("reconciled") is True,
                             "risk_limits_configured": {key: risk.get(key) not in (None, "") for key in (
                                 "max_order_amount", "max_daily_buy_amount", "max_daily_buy_count",
                                 "max_exposure", "max_open_orders")},
                             **_cost_projection(rates)})
    raw_orders = store.orders(limit=MAX_HISTORY)
    # An old unresolved order can still block a new stop-loss outside the history window.
    unresolved = store.unresolved_orders()
    pending_rows = []
    for item in unresolved[:MAX_HISTORY]:
        request = item.get("request") or {}
        metadata = item.get("provider_metadata") or {}
        pending_rows.append({
            "order": ids.get("order", item.get("id")),
            "strategy": ids.get("strategy", item.get("strategy_id")),
            "account": ids.get("account", item.get("account_alias")),
            "instrument": ids.get("instrument", request.get("code")),
            "created_at": _timestamp(item.get("created_at")),
            "updated_at": _timestamp(item.get("updated_at")),
            "status": _enum(item.get("status"), {state.value for state in OrderStatus}),
            "side": _enum(request.get("side"), {"BUY", "SELL"}),
            "order_type": _enum(request.get("order_type"), {"MARKET", "LIMIT", "BEST_LIMIT"}),
            "route": _enum(request.get("route"), {"KRX", "NXT", "SOR", "AUTO"}),
            **_safe_unknown_fields(
                metadata,
                provider_id_present=bool(item.get("provider_order_id")),
            ),
        })
    order_rows = []
    for item in recent(raw_orders, "updated_at"):
        request = item.get("request") or {}
        metadata = item.get("provider_metadata") or {}
        status = _enum(item.get("status"), {state.value for state in OrderStatus})
        order_rows.append({"order": ids.get("order", item.get("id")),
                           "strategy": ids.get("strategy", item.get("strategy_id")),
                           "account": ids.get("account", item.get("account_alias")),
                           "instrument": ids.get("instrument", request.get("code")),
                           "created_at": _timestamp(item.get("created_at")),
                           "updated_at": _timestamp(item.get("updated_at")),
                           "status": status,
                           "side": _enum(request.get("side"), {"BUY", "SELL"}),
                           "order_type": _enum(request.get("order_type"), {"MARKET", "LIMIT", "BEST_LIMIT"}),
                           "route": _enum(request.get("route"), {"KRX", "NXT", "SOR", "AUTO"}),
                           **_safe_unknown_fields(
                               metadata,
                               provider_id_present=bool(item.get("provider_order_id")),
                           ),
                           **_reason(
                               item.get("message"),
                               event_type=("order.unknown" if status == OrderStatus.UNKNOWN_RECONCILING.value else None),
                           )})
    raw_events = store.events(limit=MAX_HISTORY)

    def project_event(item):
        details = item.get("details") or {}
        event_type = item.get("event_type")
        source = item.get("source")
        source_kind = details.get("source_kind")
        if source_kind not in SOURCE_KINDS:
            source_kind = (
                "system" if source == "system"
                or event_type == "order.unknown_resolution_error"
                else "account" if event_type == "order.reconcile_error"
                else "strategy"
            )
        projected = {"at": _timestamp(item.get("occurred_at")),
                           "source_kind": source_kind,
                           "strategy": ids.get("strategy", source) if source_kind == "strategy" else None,
                           "account": ids.get("account", source) if source_kind == "account" else None,
                           "event": _enum(event_type, EVENT_TYPES),
                           "duration_ms": _number(details.get("duration_ms"), 0, 3600000),
                           "order": ids.get("order", details.get("order_id")),
                           **_safe_unknown_fields(details),
                           **_reason(item.get("message"), details, event_type=event_type)}
        if event_type == "strategy.process_exit":
            summary = safe_exit_details(details)
            projected.update(summary)
            projected["reason"] = summary["exit_reason"]
        return projected

    event_rows = [project_event(item) for item in recent(raw_events, "occurred_at")]
    raw_critical_events = store.events(
        limit=MAX_CRITICAL_HISTORY,
        event_types=CRITICAL_ORDER_EVENT_TYPES,
    )
    critical_event_rows = [
        project_event(item) for item in recent(raw_critical_events, "occurred_at")
    ]
    # Busy quote polling must not crowd the actual stop reason out of an export.
    raw_exits = store.events(limit=MAX_EXITS, event_types=("strategy.process_exit",))
    exit_rows = [project_event(item) for item in recent(raw_exits, "occurred_at")]
    data = {
        "schema_version": 2, "app_version": version,
        "exported_at": now.isoformat(),
        "history_window": {"from": cutoff.isoformat(), "to": now.isoformat(), "hours": hours},
        "current_snapshot": {"at": now.isoformat(), "strategies": strategies, "accounts": account_rows,
                             "unresolved_orders": pending_rows,
                             "connections": [{"connection": ids.get("connection", item.get("id")),
                                              "state": _enum(item.get("state"), {"connected", "disconnected", "error", "checking", "unknown"})}
                                             for item in connection_states[:100]],
                             "realtime": [{"connection": ids.get("connection", item.get("connection_id")),
                                           "state": _enum(item.get("state"), {"connected", "waiting", "idle", "reconnecting", "error", "disconnected"}),
                                           "subscriptions": _number(item.get("subscriptions"), 0, 10000),
                                           "last_received_at": _timestamp(item.get("last_received_at"))}
                                          for item in realtime_states[:100]]},
        "history": {"orders": order_rows, "events": event_rows,
                    "critical_order_events": critical_event_rows, "strategy_logs": logs,
                    "strategy_exits": exit_rows},
        "coverage": {"strategy_limit": MAX_STRATEGIES, "strategy_limit_reached": len(rows) > MAX_STRATEGIES,
                     "unresolved_orders": {"limit": MAX_HISTORY, "read": len(unresolved), "limit_reached": len(unresolved) >= MAX_HISTORY},
                     "orders": {"limit": MAX_HISTORY, "read": len(raw_orders), "exported": len(order_rows), "limit_reached": len(raw_orders) >= MAX_HISTORY},
                     "events": {"limit": MAX_HISTORY, "read": len(raw_events), "exported": len(event_rows), "limit_reached": len(raw_events) >= MAX_HISTORY},
                     "critical_order_events": {"limit": MAX_CRITICAL_HISTORY,
                                               "read": len(raw_critical_events),
                                               "exported": len(critical_event_rows),
                                               "limit_reached": len(raw_critical_events) >= MAX_CRITICAL_HISTORY},
                     "strategy_exits": {"limit": MAX_EXITS, "read": len(raw_exits), "exported": len(exit_rows), "limit_reached": len(raw_exits) >= MAX_EXITS},
                     "strategy_logs": history_meta,
                     "raw_messages_excluded": True,
                     "unknown_reason_count": sum(item["reason"] == "UNCLASSIFIED" for item in [*logs, *event_rows, *order_rows])},
    }
    readme = """Trade Router 개인정보 제외 진단 자료

diagnostics.json은 허용한 항목만 추출한 자료입니다. 원본 로그/DB/설정/전략 소스는 포함하지 않습니다.
포함: 앱 버전, 시각, 익명 ID, 현재 실행·주문 허용 상태, 저장된 매도 시간·조건구조·수익률 기준,
주문 상태와 시각, 고정 오류 분류, 위험 한도 설정 여부, 실시간 구독수와 연결 수신 시각,
프로세스 종료 코드·사용자 중지 여부·허용된 예외 종류·Windows 오류 번호·고정 실패 영역,
미확정 주문의 실패 단계·안전한 예외 종류·조정 횟수·후보 수·해결 방식과 중요 주문 이벤트.
제외: API 키·토큰·계좌번호·HTS ID·연결주소·사용자 경로·별칭/이름·종목명/코드,
보유수량·금액·자산·실주문번호·원문 오류/로그·전략 소스. 숫자 조건 중 수익률 기준 외 직접 값도 제외합니다.

current_snapshot은 내보낸 시점의 현재 상태이며, 사고 당시 상태를 보장하지 않습니다.
saved_sell_settings는 저장된 설정이며 실행 당시 설정과 다를 수 있습니다.
history는 선택한 시간 안에서 보관된 최근 기록만 포함합니다. coverage에서 제한과 누락을 확인하세요.
전략 로그는 메모리에 보관되어 프로그램 재시작 후 이전 로그가 없을 수 있습니다.
strategy_exits는 DB에 남은 최근 종료 기록이며 재시작 후에도 포함합니다. 이전 버전에서 누락된 원인은 복원할 수 없습니다.
UNCLASSIFIED는 원문을 안전하게 제외하여 분류하지 못한 기록이며 오류가 없다는 뜻이 아닙니다.
realtime.last_received_at은 연결 전체의 마지막 수신 시각이며 특정 종목의 최신 시세를 보장하지 않습니다.
아무 곳에도 자동 전송하지 않습니다. 내용을 확인한 뒤 이 ZIP만 전달하세요.
"""
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme)
        archive.writestr("diagnostics.json", json.dumps(data, ensure_ascii=False, indent=2))
    return output.getvalue(), f"trade-router-diagnostics-{now:%Y%m%d-%H%M%S}.zip"
