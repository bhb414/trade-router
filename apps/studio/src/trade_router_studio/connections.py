from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from trade_router import (
    AccountOrder,
    AccessToken,
    Balance,
    Bar,
    BrokerCapabilities,
    BrokerRequestIntent,
    BuyingPower,
    ConditionDefinition,
    ConditionMatch,
    ConnectionTarget,
    ExtensionCapability,
    InstrumentId,
    OrderRoute,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ProviderOrderAck,
    Quote,
    RankingEntry,
    RankingKind,
    RateLimitManager,
    RateLimitPolicy,
    RateLimitScope,
    RequestManager,
    RequestPriority,
    RouterError,
    RouterErrorCode,
    SubscriptionQuotaManager,
    TokenManager,
    TokenScopeKey,
    Venue,
    WebSocketLimitPolicy,
)

from .broker_policies import rest_rate_limit_profile, websocket_rate_limit_profile
from .settings import SettingsError, SettingsStore


KIS_BASE_URLS = {
    ConnectionTarget.LIVE: "https://openapi.koreainvestment.com:9443",
    ConnectionTarget.PAPER: "https://openapivts.koreainvestment.com:29443",
}
NAMUH_BASE_URL = "https://api.nhplug.com:8443"
NAMUH_BASE_URLS = {
    ConnectionTarget.LIVE: "https://api.nhplug.com:8443",
    ConnectionTarget.PAPER: "https://moapi.nhplug.com:8443",
}

SEOUL = ZoneInfo("Asia/Seoul")
KIS_VENUE_CODES = {
    Venue.KRX: "J",
    Venue.NXT: "NX",
    Venue.INTEGRATED: "UN",
}
NAMUH_VENUE_CODES = {
    Venue.KRX: "KRX",
    Venue.NXT: "NXT",
    Venue.INTEGRATED: "UNT",
}
KIS_ORDER_ROUTE_CODES = {
    OrderRoute.KRX: "KRX",
    OrderRoute.NXT: "NXT",
    OrderRoute.SOR: "SOR",
}
KIS_RANKING_MARKETS = {
    "ALL": "0000",
    "KOSPI": "0001",
    "KOSDAQ": "1001",
    "KOSPI200": "2001",
}


class SettingsTokenStore:
    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings

    async def load(self, scope: TokenScopeKey) -> AccessToken | None:
        return await asyncio.to_thread(self._settings.load_token, scope)

    async def save(self, scope: TokenScopeKey, token: AccessToken) -> None:
        await asyncio.to_thread(self._settings.save_token, scope, token)

    async def delete(self, scope: TokenScopeKey) -> None:
        await asyncio.to_thread(self._settings.delete_token, scope)


class ConnectionRuntime:
    """Owns connection authentication and shared request throttling for Studio."""

    def __init__(
        self,
        settings: SettingsStore,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_http = http_client is None
        self._tokens = TokenManager(SettingsTokenStore(settings))
        self._rate_limits = RateLimitManager()
        self._websocket_quotas = SubscriptionQuotaManager()
        self._requests = RequestManager(self._rate_limits, worker_count=2)
        self._configured: set[str] = set()
        self._statuses: dict[str, dict[str, Any]] = {}
        self._approval_keys: dict[str, tuple[str, datetime]] = {}
        self._approval_locks: dict[str, asyncio.Lock] = {}
        self._kis_request_locks: dict[str, asyncio.Lock] = {}
        self._kis_last_request_completed: dict[str, float] = {}

    async def check(self, connection_id: str) -> dict[str, Any]:
        connection = self._settings.get_connection(connection_id)
        provider = connection["provider"]
        if provider not in {"kis", "namuh"}:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 연결 확인 어댑터는 아직 구현되지 않았습니다.",
            )

        try:
            token = await self._access_token(connection)
        except (SettingsError, RouterError) as exc:
            self._statuses[connection_id] = {
                "state": "error",
                "checked_at": datetime.now().astimezone().isoformat(),
                "message": str(exc),
            }
            raise

        return self._mark_connected(connection, token)

    async def restore_cached_statuses(self) -> None:
        for connection in self._settings.list_connections():
            status = await self._tokens.status(self._token_scope(connection))
            if status["usable"]:
                self._statuses[connection["id"]] = {
                    "connection_id": connection["id"],
                    "state": "connected",
                    "checked_at": datetime.now().astimezone().isoformat(),
                    "expires_at": status["expires_at"],
                    "message": "저장된 유효 토큰을 재사용합니다.",
                }

    async def invalidate(self, connection_id: str) -> None:
        connection = self._settings.get_connection(connection_id)
        await self._tokens.invalidate(self._token_scope(connection))
        self._statuses.pop(connection_id, None)

    def status(self, connection_id: str) -> dict[str, Any]:
        return dict(
            self._statuses.get(
                connection_id,
                {
                    "connection_id": connection_id,
                    "state": "unchecked",
                    "checked_at": None,
                    "expires_at": None,
                    "message": "아직 연결을 확인하지 않았습니다.",
                },
            )
        )

    def connected_count(self) -> int:
        return sum(1 for item in self._statuses.values() if item["state"] == "connected")

    async def request_status(self) -> dict[str, Any]:
        return {
            "queue": await self._requests.snapshot(),
            "limits": await self._rate_limits.snapshot(),
            "websocket": await self._websocket_quotas.snapshot(),
        }

    def capabilities(self, connection_reference: str) -> BrokerCapabilities:
        connection = self._connection_for_reference(connection_reference)
        if connection["provider"] == "kis":
            target = ConnectionTarget(connection["target"])
            routes = (
                (OrderRoute.KRX, OrderRoute.AUTO)
                if target is ConnectionTarget.PAPER
                else (
                    OrderRoute.KRX,
                    OrderRoute.NXT,
                    OrderRoute.SOR,
                    OrderRoute.AUTO,
                )
            )
            return BrokerCapabilities(
                provider="kis",
                quotes=True,
                orderbook=True,
                bars=True,
                accounts=True,
                order_history=True,
                realtime=True,
                cash_orders=True,
                amend_orders=True,
                cancel_orders=True,
                conditions=target is ConnectionTarget.LIVE,
                rankings=target is ConnectionTarget.LIVE,
                venues=(Venue.KRX, Venue.NXT, Venue.INTEGRATED),
                order_routes=routes,
                extensions=(
                    ExtensionCapability(
                        id="kis.hts_conditions.v1",
                        targets=(ConnectionTarget.LIVE,),
                    ),
                ),
            )
        if connection["provider"] == "namuh":
            return BrokerCapabilities(
                provider="namuh",
                quotes=True,
                orderbook=True,
                accounts=True,
                order_history=True,
                realtime=True,
                cash_orders=True,
                amend_orders=True,
                cancel_orders=True,
                venues=(Venue.KRX, Venue.NXT, Venue.INTEGRATED),
                order_routes=(
                    OrderRoute.KRX,
                    OrderRoute.NXT,
                    OrderRoute.SOR,
                    OrderRoute.AUTO,
                ),
            )
        return BrokerCapabilities(provider=connection["provider"])

    def realtime_connection(self, connection_reference: str) -> dict[str, Any]:
        """Return non-secret connection metadata used by the socket owner."""

        connection = self._settings.resolve_connection(connection_reference)
        provider = str(connection["provider"])
        if provider not in {"kis", "namuh"}:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 실시간 어댑터는 아직 구현되지 않았습니다.",
            )
        target = ConnectionTarget(connection["target"])
        rate_scope = self._configure_connection(
            connection["id"], provider=provider, target=target
        )
        return {
            "id": connection["id"],
            "provider": provider,
            "target": target,
            "rate_scope": rate_scope,
            "url": (
                (
                    "ws://ops.koreainvestment.com:31000"
                    if target is ConnectionTarget.PAPER
                    else "ws://ops.koreainvestment.com:21000"
                )
                if provider == "kis"
                else (
                    "wss://moapi.nhplug.com:17070/websocket"
                    if target is ConnectionTarget.PAPER
                    else "wss://api.nhplug.com:7070/websocket"
                )
            ),
            "control_interval": (
                websocket_rate_limit_profile(provider, target.value)[
                    "minimum_control_interval_ms"
                ]
                / 1000
            ),
        }

    async def realtime_approval_key(self, connection_reference: str) -> str:
        connection = self._settings.resolve_connection(connection_reference)
        if connection["provider"] == "namuh":
            return (await self._access_token(connection)).value
        if connection["provider"] != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 실시간 인증은 아직 구현되지 않았습니다.",
            )
        target = ConnectionTarget(connection["target"])
        rate_scope = self._configure_connection(
            connection["id"], provider="kis", target=target
        )
        approval_scope = str(rate_scope)
        now = datetime.now(SEOUL)
        cached = self._approval_keys.get(approval_scope)
        if cached is not None and cached[1] > now:
            return cached[0]
        lock = self._approval_locks.setdefault(approval_scope, asyncio.Lock())
        async with lock:
            cached = self._approval_keys.get(approval_scope)
            if cached is not None and cached[1] > datetime.now(SEOUL):
                return cached[0]
            credentials = self._settings.get_credentials(connection["id"])

            async def issue() -> str:
                try:
                    response = await self._http.post(
                        f"{KIS_BASE_URLS[target]}/oauth2/Approval",
                        headers={"Content-Type": "application/json; charset=UTF-8"},
                        json={
                            "grant_type": "client_credentials",
                            "appkey": credentials["app_key"],
                            "secretkey": credentials["app_secret"],
                        },
                    )
                except httpx.HTTPError as exc:
                    raise RouterError(
                        RouterErrorCode.AUTHENTICATION_FAILED,
                        "KIS 실시간 인증 서버에 연결하지 못했습니다.",
                    ) from exc
                payload = _safe_json(response)
                approval_key = str(payload.get("approval_key") or "").strip()
                if response.status_code >= 400 or not approval_key:
                    raise RouterError(
                        RouterErrorCode.AUTHENTICATION_FAILED,
                        str(payload.get("error_description") or payload.get("msg1") or "KIS 실시간 인증키를 받지 못했습니다."),
                    )
                return approval_key

            key = await self._requests.execute(
                BrokerRequestIntent(
                    connection_id=connection["id"],
                    operation="auth.websocket_approval",
                    priority=RequestPriority.RECONCILIATION,
                    rate_limit_scopes=(RateLimitScope(f"{rate_scope}:auth"),),
                    dedupe_key=f"{approval_scope}:websocket-approval",
                    cache_ttl=60.0,
                ),
                issue,
            )
            # KIS does not publish a separate lifetime in this response. Keep a
            # short in-memory reuse window; reconnects refresh after one hour.
            self._approval_keys[approval_scope] = (
                key,
                datetime.now(SEOUL) + timedelta(hours=1),
            )
            return key

    async def reserve_realtime_subscription(
        self,
        connection_reference: str,
        *,
        consumer_id: str,
        subscription_key: str,
    ):
        info = self.realtime_connection(connection_reference)
        return await self._websocket_quotas.subscribe(
            str(info["rate_scope"]),
            consumer_id=consumer_id,
            subscription_key=subscription_key,
        )

    async def release_realtime_subscription(self, lease, *, consumer_id: str) -> bool:
        return await self._websocket_quotas.unsubscribe(
            lease, consumer_id=consumer_id
        )

    def realtime_account_subscriptions(
        self, account_reference: str
    ) -> list[dict[str, Any]]:
        account = self._settings.get_account(account_reference)
        connection = self._settings.get_connection(account["connection_id"])
        if connection["provider"] not in {"kis", "namuh"}:
            return []
        if connection["provider"] == "namuh":
            return [
                {
                    "source": connection["id"],
                    "connection_id": connection["id"],
                    "account_alias": account["alias"],
                    "tr_id": tr_id,
                    "tr_key": "",
                }
                for tr_id in ("d2", "d3")
            ]
        user_id = str(
            self._settings.get_credentials(connection["id"]).get(
                "realtime_user_id", ""
            )
        ).strip()
        if not user_id:
            return []
        target = ConnectionTarget(connection["target"])
        return [
            {
                "source": connection["id"],
                "connection_id": connection["id"],
                "account_alias": account["alias"],
                "tr_id": "H0STCNI9" if target is ConnectionTarget.PAPER else "H0STCNI0",
                "tr_key": user_id,
            }
        ]

    def _connection_for_reference(self, reference: str) -> dict[str, Any]:
        try:
            return self._settings.resolve_connection(reference)
        except SettingsError:
            account = self._settings.get_account(reference)
            return self._settings.get_connection(account["connection_id"])

    def require_capabilities(
        self,
        connection_reference: str,
        required: tuple[str, ...] | list[str],
    ) -> None:
        capabilities = self.capabilities(connection_reference)
        supported = {
            "quotes": capabilities.quotes,
            "bars": capabilities.bars,
            "orderbook": capabilities.orderbook,
            "realtime": capabilities.realtime,
            "accounts": capabilities.accounts,
            "order_history": capabilities.order_history,
            "cash_orders": capabilities.cash_orders,
            "amend_orders": capabilities.amend_orders,
            "cancel_orders": capabilities.cancel_orders,
            "conditions": capabilities.conditions,
            "rankings": capabilities.rankings,
        }
        missing = [
            name
            for name in required
            if not (
                supported.get(name, False)
                if not name.startswith("extension:")
                else capabilities.supports_extension(name.removeprefix("extension:"))
            )
        ]
        if missing:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 API 연결이 전략에 필요한 기능을 지원하지 않습니다.",
                details={"connection": connection_reference, "missing": missing},
            )

    def account_risk_limits(
        self,
        account_reference: str,
    ) -> dict[str, str | int | None]:
        account = self._settings.get_account(account_reference)
        return dict(account.get("risk_limits", {}))

    def account_cost_rates(self, account_reference: str) -> dict[str, str | None]:
        account = self._settings.get_account(account_reference)
        return dict(account.get("cost_rates", {}))

    async def quote(
        self,
        connection_reference: str,
        code: str,
        venue: Venue = Venue.INTEGRATED,
        *,
        source_alias: str | None = None,
    ) -> Quote:
        connection = self._settings.resolve_connection(connection_reference)
        if connection["provider"] == "namuh":
            return await self._namuh_quote(
                connection,
                code,
                venue,
                source_alias=source_alias,
            )
        if connection["provider"] != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 현재가 어댑터는 아직 구현되지 않았습니다.",
            )
        instrument = InstrumentId(code)
        payload, response = await self._kis_get(
            connection,
            path="/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": KIS_VENUE_CODES[venue],
                "FID_INPUT_ISCD": instrument.code,
            },
            operation="market.quote",
            priority=RequestPriority.STRATEGY_DATA,
            group="market",
            dedupe_key=(
                f"{connection['id']}:quote:{instrument.code}:{venue.value}"
            ),
            cache_ttl=0.5,
        )
        output = payload.get("output")
        if not isinstance(output, dict):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "증권사 현재가 응답에 필요한 자료가 없습니다.",
            )
        now = datetime.now(SEOUL)
        market_time = _market_time(output.get("stck_cntg_hour"), now)
        return Quote(
            instrument=instrument,
            price=_decimal(output.get("stck_prpr")),
            venue=venue,
            provider="kis",
            source_alias=source_alias,
            market_time=market_time,
            received_at=now,
            correlation_id=(
                response.headers.get("x-correlation-id")
                or response.headers.get("x-transaction-id")
                or uuid4().hex
            ),
        )

    async def condition_definitions(
        self,
        connection_reference: str,
        user_id: str,
        *,
        source_alias: str | None = None,
    ) -> tuple[ConditionDefinition, ...]:
        connection = self._settings.resolve_connection(connection_reference)
        self._require_kis_live_conditions(connection)
        clean_user_id = user_id.strip()
        if not clean_user_id:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "한국투자증권 HTS ID를 입력하세요.",
            )
        payload, _ = await self._kis_get(
            connection,
            path="/uapi/domestic-stock/v1/quotations/psearch-title",
            tr_id="HHKST03900300",
            params={"user_id": clean_user_id},
            operation="discovery.conditions",
            priority=RequestPriority.STRATEGY_DATA,
            group="market",
            dedupe_key=f"{connection['id']}:conditions:{clean_user_id}",
            cache_ttl=30.0,
        )
        rows = payload.get("output2")
        if not isinstance(rows, list):
            rows = []
        results: list[ConditionDefinition] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            condition_id = _first_text(row, "seq", "condition_id", "id")
            name = _first_text(
                row,
                "condition_nm",
                "condition_name",
                "name",
                "hts_kor_isnm",
            )
            if condition_id and name:
                results.append(
                    ConditionDefinition(
                        id=condition_id,
                        name=name,
                        provider="kis",
                        source_alias=source_alias,
                    )
                )
        return tuple(results)

    async def condition_matches(
        self,
        connection_reference: str,
        user_id: str,
        condition_id: str,
        *,
        source_alias: str | None = None,
    ) -> tuple[ConditionMatch, ...]:
        connection = self._settings.resolve_connection(connection_reference)
        self._require_kis_live_conditions(connection)
        clean_user_id = user_id.strip()
        clean_condition_id = condition_id.strip()
        if not clean_user_id or not clean_condition_id:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "한국투자증권 HTS ID와 조건식 ID를 입력하세요.",
            )
        try:
            payload, _ = await self._kis_get(
                connection,
                path="/uapi/domestic-stock/v1/quotations/psearch-result",
                tr_id="HHKST03900400",
                params={"user_id": clean_user_id, "seq": clean_condition_id},
                operation="discovery.condition_matches",
                priority=RequestPriority.STRATEGY_DATA,
                group="market",
                dedupe_key=(
                    f"{connection['id']}:condition-matches:"
                    f"{clean_user_id}:{clean_condition_id}"
                ),
                cache_ttl=5.0,
            )
        except RouterError as exc:
            # KIS documents MCA05918/"종목코드 오류" as the empty-result
            # response for this endpoint rather than a request failure.
            if "MCA05918" in exc.message or "종목코드 오류" in exc.message:
                return ()
            raise
        rows = payload.get("output2")
        if not isinstance(rows, list):
            rows = []
        results: list[ConditionMatch] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = _first_text(
                row,
                "code",
                "pdno",
                "mksc_shrn_iscd",
                "stck_shrn_iscd",
                "jong_code",
            )
            if not code:
                continue
            results.append(
                ConditionMatch(
                    condition_id=clean_condition_id,
                    instrument=InstrumentId(code),
                    provider="kis",
                    name=_first_text(
                        row,
                        "name",
                        "hts_kor_isnm",
                        "kor_name",
                        "jong_name",
                    )
                    or None,
                    price=_optional_decimal(
                        row,
                        "price",
                        "stck_prpr",
                        "now_price",
                    ),
                    change_rate=_optional_decimal(
                        row,
                        "change_rate",
                        "prdy_ctrt",
                        "chgr",
                    ),
                    source_alias=source_alias,
                )
            )
        return tuple(results)

    async def ranking(
        self,
        connection_reference: str,
        kind: RankingKind | str,
        *,
        venue: Venue = Venue.KRX,
        market: str = "ALL",
        limit: int = 20,
        min_price: Decimal | None = None,
        max_price: Decimal | None = None,
        min_volume: Decimal | None = None,
        source_alias: str | None = None,
    ) -> tuple[RankingEntry, ...]:
        """Return a normalized KIS ranking through the shared request queue."""

        connection = self._settings.resolve_connection(connection_reference)
        self._require_kis_live_rankings(connection)
        try:
            normalized_kind = RankingKind(kind)
        except ValueError as exc:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "지원하지 않는 순위 유형입니다.",
                details={"kind": str(kind)},
            ) from exc
        clean_market = market.strip().upper()
        market_code = KIS_RANKING_MARKETS.get(clean_market)
        if market_code is None:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "시장은 ALL, KOSPI, KOSDAQ, KOSPI200 중에서 선택하세요.",
            )
        safe_limit = min(max(int(limit), 1), 100)
        if min_price is not None and min_price < 0:
            raise RouterError(RouterErrorCode.INVALID_REQUEST, "최소 주가는 0 이상이어야 합니다.")
        if max_price is not None and max_price < 0:
            raise RouterError(RouterErrorCode.INVALID_REQUEST, "최대 주가는 0 이상이어야 합니다.")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise RouterError(RouterErrorCode.INVALID_REQUEST, "최소 주가가 최대 주가보다 큽니다.")
        if min_volume is not None and min_volume < 0:
            raise RouterError(RouterErrorCode.INVALID_REQUEST, "최소 거래량은 0 이상이어야 합니다.")

        if normalized_kind is RankingKind.HTS_VIEW:
            if venue is not Venue.KRX or clean_market != "ALL" or any(
                value is not None for value in (min_price, max_price, min_volume)
            ):
                raise RouterError(
                    RouterErrorCode.UNSUPPORTED_CAPABILITY,
                    "KIS HTS 조회상위는 시장·가격·거래량 필터를 제공하지 않습니다.",
                )
            path = "/uapi/domestic-stock/v1/ranking/hts-top-view"
            tr_id = "HHMCM000100C0"
            params: dict[str, str] = {}
            rows_key = "output1"
            safe_limit = min(safe_limit, 20)
        elif normalized_kind in {RankingKind.VOLUME, RankingKind.TRADING_VALUE}:
            path = "/uapi/domestic-stock/v1/quotations/volume-rank"
            tr_id = "FHPST01710000"
            params = {
                "FID_COND_MRKT_DIV_CODE": KIS_VENUE_CODES[venue],
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": market_code,
                "FID_DIV_CLS_CODE": "0",
                "FID_BLNG_CLS_CODE": (
                    "0" if normalized_kind is RankingKind.VOLUME else "3"
                ),
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_INPUT_PRICE_1": _query_number(min_price),
                "FID_INPUT_PRICE_2": _query_number(max_price),
                "FID_VOL_CNT": _query_number(min_volume),
                "FID_INPUT_DATE_1": "",
            }
            rows_key = "output"
        else:
            if venue is not Venue.KRX:
                raise RouterError(
                    RouterErrorCode.UNSUPPORTED_CAPABILITY,
                    "KIS 상승률 순위는 국내주식(KRX) 분류에서만 조회할 수 있습니다.",
                )
            path = "/uapi/domestic-stock/v1/ranking/fluctuation"
            tr_id = "FHPST01700000"
            params = {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20170",
                "FID_INPUT_ISCD": market_code,
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_INPUT_CNT_1": str(safe_limit),
                "FID_PRC_CLS_CODE": "0",
                "FID_INPUT_PRICE_1": _query_number(min_price),
                "FID_INPUT_PRICE_2": _query_number(max_price),
                "FID_VOL_CNT": _query_number(min_volume),
                "FID_TRGT_CLS_CODE": "0",
                "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RSFL_RATE1": "",
                "FID_RSFL_RATE2": "",
            }
            rows_key = "output"

        dedupe_parts = ":".join(f"{key}={value}" for key, value in sorted(params.items()))
        payload, _ = await self._kis_get(
            connection,
            path=path,
            tr_id=tr_id,
            params=params,
            operation="discovery.ranking",
            priority=RequestPriority.STRATEGY_DATA,
            group="market",
            dedupe_key=(
                f"{connection['id']}:ranking:{normalized_kind.value}:"
                f"{safe_limit}:{dedupe_parts}"
            ),
            cache_ttl=15.0,
        )
        raw_rows = payload.get(rows_key)
        rows = raw_rows if isinstance(raw_rows, list) else []
        results: list[RankingEntry] = []
        for index, row in enumerate(rows[:safe_limit], start=1):
            if not isinstance(row, dict):
                continue
            code = _first_text(
                row,
                "mksc_shrn_iscd",
                "stck_shrn_iscd",
                "code",
                "pdno",
            )
            if not code:
                continue
            rank_text = _first_text(row, "data_rank", "rank")
            try:
                rank = max(1, int(rank_text)) if rank_text else index
            except ValueError:
                rank = index
            results.append(
                RankingEntry(
                    rank=rank,
                    kind=normalized_kind,
                    instrument=InstrumentId(code),
                    provider="kis",
                    name=_first_text(row, "hts_kor_isnm", "name", "kor_name") or None,
                    price=_optional_decimal(row, "stck_prpr", "price"),
                    change_rate=_optional_decimal(row, "prdy_ctrt", "change_rate"),
                    volume=_optional_decimal(row, "acml_vol", "volume"),
                    trading_value=_optional_decimal(
                        row, "acml_tr_pbmn", "trading_value"
                    ),
                    source_alias=source_alias,
                )
            )
        return tuple(results)

    @staticmethod
    def _require_kis_live_conditions(connection: dict[str, Any]) -> None:
        if connection.get("provider") != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사는 KIS HTS 조건검색을 지원하지 않습니다.",
            )
        if ConnectionTarget(connection["target"]) is not ConnectionTarget.LIVE:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "KIS HTS 조건검색은 실전 API 연결에서만 사용할 수 있습니다.",
            )

    @staticmethod
    def _require_kis_live_rankings(connection: dict[str, Any]) -> None:
        if connection.get("provider") != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사는 현재 공통 순위 조회 어댑터를 지원하지 않습니다.",
            )
        if ConnectionTarget(connection["target"]) is not ConnectionTarget.LIVE:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "KIS 순위 조회는 실전 API 연결에서만 사용할 수 있습니다.",
            )

    async def balance(self, account_reference: str) -> Balance:
        account = self._settings.get_account(account_reference)
        connection = self._settings.get_connection(account["connection_id"])
        if connection["provider"] == "namuh":
            return await self._namuh_balance(account, connection)
        if connection["provider"] != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 잔고 어댑터는 아직 구현되지 않았습니다.",
            )
        account_number = self._settings.get_account_number(account["id"])
        target = ConnectionTarget(connection["target"])
        tr_id = "VTTC8434R" if target is ConnectionTarget.PAPER else "TTTC8434R"
        payload, _ = await self._kis_get(
            connection,
            path="/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params={
                "CANO": account_number,
                "ACNT_PRDT_CD": account["product_code"],
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            operation="account.balance",
            priority=RequestPriority.ACCOUNT,
            group="account",
            dedupe_key=f"{connection['id']}:balance:{account['id']}",
            cache_ttl=1.0,
        )
        position_rows = payload.get("output1")
        summary_rows = payload.get("output2")
        if not isinstance(position_rows, list):
            position_rows = []
        if isinstance(summary_rows, list):
            summary = summary_rows[0] if summary_rows else {}
        elif isinstance(summary_rows, dict):
            summary = summary_rows
        else:
            summary = {}
        positions: list[Position] = []
        for row in position_rows:
            if not isinstance(row, dict) or not str(row.get("pdno", "")).strip():
                continue
            quantity = _decimal(row.get("hldg_qty"), default=Decimal("0"))
            if quantity <= 0:
                continue
            positions.append(
                Position(
                    instrument=InstrumentId(str(row["pdno"])),
                    quantity=quantity,
                    available_quantity=_decimal(
                        row.get("ord_psbl_qty"), default=Decimal("0")
                    ),
                    average_price=_decimal(
                        row.get("pchs_avg_pric"), default=Decimal("0")
                    ),
                    market_price=_decimal(
                        row.get("prpr"), default=Decimal("0")
                    ),
                )
            )
        cash = _decimal(summary.get("dnca_tot_amt"), default=Decimal("0"))
        # The balance response does not expose a single universally named
        # withdrawable-cash field. Keep the normalized value conservative and
        # use a dedicated withdrawal inquiry when that service is added.
        withdrawable = cash
        total = _decimal(summary.get("tot_evlu_amt"), default=cash)
        return Balance(
            cash=cash,
            withdrawable_cash=withdrawable,
            total_value=total,
            positions=tuple(positions),
            as_of=datetime.now(SEOUL),
        )

    async def account_orders(
        self,
        account_reference: str,
        *,
        state: str = "all",
        business_date: date | None = None,
    ) -> tuple[AccountOrder, ...]:
        """Return today's normalized orders for reconciliation.

        ``state`` is applied after the broker response so every caller shares
        the same short-lived account snapshot.
        """

        normalized_state = state.strip().lower()
        if normalized_state not in {"all", "open", "filled"}:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "주문 조회 상태는 all, open, filled 중 하나여야 합니다.",
            )
        account = self._settings.get_account(account_reference)
        connection = self._settings.get_connection(account["connection_id"])
        day = business_date or datetime.now(SEOUL).date()
        if connection["provider"] == "namuh":
            ordered = await self._namuh_account_orders(account, connection, day)
            if normalized_state == "open":
                return tuple(
                    item
                    for item in ordered
                    if item.status
                    in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}
                )
            if normalized_state == "filled":
                return tuple(item for item in ordered if item.filled_quantity > 0)
            return ordered
        if connection["provider"] != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 주문·체결 조회 어댑터는 아직 구현되지 않았습니다.",
            )

        account_number = self._settings.get_account_number(account["id"])
        target = ConnectionTarget(connection["target"])
        if normalized_state == "open":
            return await self._kis_open_orders(account, connection, day)
        tr_id = "VTTC0081R" if target is ConnectionTarget.PAPER else "TTTC0081R"
        day_text = day.strftime("%Y%m%d")
        fk100 = ""
        nk100 = ""
        rows: list[dict[str, Any]] = []
        for page in range(10):
            payload, response = await self._kis_get(
                connection,
                path="/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                tr_id=tr_id,
                params={
                    "CANO": account_number,
                    "ACNT_PRDT_CD": account["product_code"],
                    "INQR_STRT_DT": day_text,
                    "INQR_END_DT": day_text,
                    "SLL_BUY_DVSN_CD": "00",
                    "PDNO": "",
                    "CCLD_DVSN": "00",
                    "INQR_DVSN": "00",
                    "INQR_DVSN_3": "00",
                    "ORD_GNO_BRNO": "",
                    "ODNO": "",
                    "INQR_DVSN_1": "",
                    "CTX_AREA_FK100": fk100,
                    "CTX_AREA_NK100": nk100,
                    "EXCG_ID_DVSN_CD": "ALL",
                },
                operation="account.orders",
                priority=RequestPriority.RECONCILIATION,
                group="account",
                dedupe_key=(
                    f"{connection['id']}:orders:{account['id']}:{day_text}:"
                    f"{fk100}:{nk100}"
                ),
                cache_ttl=1.0,
                tr_cont="N" if page else "",
            )
            output = payload.get("output1")
            if isinstance(output, list):
                rows.extend(row for row in output if isinstance(row, dict))
            next_fk = str(payload.get("ctx_area_fk100") or "").strip()
            next_nk = str(payload.get("ctx_area_nk100") or "").strip()
            has_next = response.headers.get("tr_cont", "").upper() in {"M", "F"}
            if not has_next or not (next_fk or next_nk):
                break
            if (next_fk, next_nk) == (fk100, nk100):
                break
            fk100, nk100 = next_fk, next_nk

        ordered = _normalize_kis_account_orders(rows, account["alias"], day)
        if normalized_state == "open":
            return tuple(
                item
                for item in ordered
                if item.status
                in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}
            )
        if normalized_state == "filled":
            return tuple(item for item in ordered if item.filled_quantity > 0)
        return ordered

    async def _kis_open_orders(
        self,
        account: dict[str, Any],
        connection: dict[str, Any],
        business_date: date,
    ) -> tuple[AccountOrder, ...]:
        """Read the broker's authoritative amend/cancel-eligible order list."""

        target = ConnectionTarget(connection["target"])
        payload, _ = await self._kis_get(
            connection,
            path="/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            tr_id="VTTC0084R" if target is ConnectionTarget.PAPER else "TTTC0084R",
            params={
                "CANO": self._settings.get_account_number(account["id"]),
                "ACNT_PRDT_CD": account["product_code"],
                "INQR_DVSN_1": "0",
                "INQR_DVSN_2": "0",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            operation="account.open_orders",
            priority=RequestPriority.RECONCILIATION,
            group="account",
            dedupe_key=f"{connection['id']}:open-orders:{account['id']}",
            cache_ttl=1.0,
        )
        rows = payload.get("output")
        if not isinstance(rows, list):
            rows = payload.get("output1")
        if not isinstance(rows, list):
            rows = []
        orders = _normalize_kis_account_orders(
            [row for row in rows if isinstance(row, dict)],
            account["alias"],
            business_date,
        )
        return tuple(
            order
            for order in orders
            if order.status in {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}
            and order.remaining_quantity > 0
        )

    async def buying_power(
        self,
        account_reference: str,
        code: str,
        *,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> BuyingPower:
        account = self._settings.get_account(account_reference)
        connection = self._settings.get_connection(account["connection_id"])
        if connection["provider"] == "namuh":
            return await self._namuh_buying_power(
                account, connection, code, order_type=order_type, price=price
            )
        if connection["provider"] != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 주문 가능 수량 조회 어댑터는 아직 구현되지 않았습니다.",
            )
        instrument = InstrumentId(code)
        if order_type is OrderType.LIMIT and (price is None or price <= 0):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "지정가 주문 가능 수량을 조회하려면 가격이 필요합니다.",
            )
        requested_price = price or Decimal("0")
        account_number = self._settings.get_account_number(account["id"])
        target = ConnectionTarget(connection["target"])
        tr_id = "VTTC8908R" if target is ConnectionTarget.PAPER else "TTTC8908R"
        payload, _ = await self._kis_get(
            connection,
            path="/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=tr_id,
            params={
                "CANO": account_number,
                "ACNT_PRDT_CD": account["product_code"],
                "PDNO": instrument.code,
                "ORD_UNPR": str(requested_price),
                "ORD_DVSN": "01" if order_type is OrderType.MARKET else "00",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
            operation="account.buying_power",
            priority=RequestPriority.RISK,
            group="account",
            dedupe_key=(
                f"{connection['id']}:buying-power:{account['id']}:"
                f"{instrument.code}:{order_type.value}:{requested_price}"
            ),
            cache_ttl=0.5,
        )
        output = payload.get("output")
        if not isinstance(output, dict):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "증권사 주문 가능 수량 응답에 필요한 자료가 없습니다.",
            )
        return BuyingPower(
            instrument=instrument,
            account_alias=account["alias"],
            order_type=order_type,
            requested_price=requested_price,
            calculation_price=_decimal(
                output.get("psbl_qty_calc_unpr"), default=requested_price
            ),
            cash_available=_decimal(
                output.get("ord_psbl_cash"), default=Decimal("0")
            ),
            maximum_quantity=_decimal(
                output.get("nrcvb_buy_qty") or output.get("max_buy_qty"),
                default=Decimal("0"),
            ),
            provider="kis",
            as_of=datetime.now(SEOUL),
        )

    async def submit_order(
        self,
        account_reference: str,
        request: OrderRequest,
    ) -> ProviderOrderAck:
        """Submit one validated cash order through the account's adapter.

        Idempotency and risk checks are deliberately outside this method. This
        method performs exactly one broker submission and never retries an
        ambiguous transport failure.
        """

        account = self._settings.get_account(account_reference)
        connection = self._settings.get_connection(account["connection_id"])
        if connection["provider"] == "namuh":
            return await self._namuh_submit_order(account, connection, request)
        if connection["provider"] != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 국내주식 현금 주문 어댑터는 아직 구현되지 않았습니다.",
            )
        capabilities = self.capabilities(account_reference)
        if not capabilities.cash_orders:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 API 연결은 국내주식 현금 주문을 지원하지 않습니다.",
            )
        route = request.route
        if route is OrderRoute.AUTO:
            route = (
                OrderRoute.KRX
                if ConnectionTarget(connection["target"]) is ConnectionTarget.PAPER
                else OrderRoute.SOR
            )
        if route not in capabilities.order_routes:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                f"이 API 연결은 {route.value} 주문 경로를 지원하지 않습니다.",
            )

        allowed_options = {"sell_type", "condition_price"}
        unknown_options = set(request.provider_options) - allowed_options
        if unknown_options:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "KIS 주문에서 지원하지 않는 provider_options 항목이 있습니다.",
                details={"unknown": sorted(unknown_options)},
            )
        target = ConnectionTarget(connection["target"])
        if request.side is OrderSide.BUY:
            tr_id = "VTTC0012U" if target is ConnectionTarget.PAPER else "TTTC0012U"
        else:
            tr_id = "VTTC0011U" if target is ConnectionTarget.PAPER else "TTTC0011U"
        account_number = self._settings.get_account_number(account["id"])
        payload, response = await self._kis_order_post(
            connection,
            tr_id=tr_id,
            body={
                "CANO": account_number,
                "ACNT_PRDT_CD": account["product_code"],
                "PDNO": request.instrument.code,
                "ORD_DVSN": "01" if request.order_type is OrderType.MARKET else "00",
                "ORD_QTY": str(request.quantity.to_integral_value()),
                "ORD_UNPR": "0" if request.order_type is OrderType.MARKET else str(request.price),
                "EXCG_ID_DVSN_CD": KIS_ORDER_ROUTE_CODES[route],
                "SLL_TYPE": (
                    str(request.provider_options.get("sell_type", "01"))
                    if request.side is OrderSide.SELL
                    else ""
                ),
                "CNDT_PRIC": str(request.provider_options.get("condition_price", "")),
            },
        )
        output = payload.get("output")
        provider_order_id = ""
        if isinstance(output, dict):
            provider_order_id = str(
                output.get("ODNO") or output.get("odno") or ""
            ).strip()
        if not provider_order_id:
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN,
                "증권사가 성공 응답을 보냈지만 주문번호를 확인하지 못했습니다.",
                details={"correlation_id": _correlation_id(response)},
            )
        return ProviderOrderAck(
            provider_order_id=provider_order_id,
            provider="kis",
            acknowledged_at=datetime.now(SEOUL),
            correlation_id=_correlation_id(response),
            provider_options={
                "order_branch": str(
                    output.get("KRX_FWDG_ORD_ORGNO")
                    or output.get("krx_fwdg_ord_orgno")
                    or ""
                ).strip()
            },
            message=str(payload.get("msg1") or "주문이 접수되었습니다."),
        )

    async def amend_order(
        self,
        account_reference: str,
        provider_order_id: str,
        *,
        quantity: Decimal,
        order_type: OrderType,
        price: Decimal | None,
        route: OrderRoute,
    ) -> ProviderOrderAck:
        return await self._change_order(
            account_reference,
            provider_order_id,
            quantity=quantity,
            order_type=order_type,
            price=price,
            route=route,
            cancel=False,
        )

    async def cancel_order(
        self,
        account_reference: str,
        provider_order_id: str,
        *,
        quantity: Decimal | None,
        route: OrderRoute,
    ) -> ProviderOrderAck:
        return await self._change_order(
            account_reference,
            provider_order_id,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            price=None,
            route=route,
            cancel=True,
        )

    async def _change_order(
        self,
        account_reference: str,
        provider_order_id: str,
        *,
        quantity: Decimal | None,
        order_type: OrderType,
        price: Decimal | None,
        route: OrderRoute,
        cancel: bool,
    ) -> ProviderOrderAck:
        account = self._settings.get_account(account_reference)
        connection = self._settings.get_connection(account["connection_id"])
        if connection["provider"] == "namuh":
            return await self._namuh_change_order(
                account,
                connection,
                provider_order_id,
                quantity=quantity,
                order_type=order_type,
                price=price,
                route=route,
                cancel=cancel,
            )
        if connection["provider"] != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 주문 정정·취소 어댑터는 아직 구현되지 않았습니다.",
            )
        capabilities = self.capabilities(account_reference)
        if (cancel and not capabilities.cancel_orders) or (
            not cancel and not capabilities.amend_orders
        ):
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 API 연결은 요청한 주문 변경을 지원하지 않습니다.",
            )
        target = ConnectionTarget(connection["target"])
        actual_route = route
        if actual_route is OrderRoute.AUTO:
            actual_route = OrderRoute.KRX if target is ConnectionTarget.PAPER else OrderRoute.SOR
        if actual_route not in capabilities.order_routes:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                f"이 API 연결은 {actual_route.value} 주문 경로를 지원하지 않습니다.",
            )
        possible = await self._kis_cancellable_order(
            account, connection, provider_order_id
        )
        possible_quantity = _decimal(
            possible.get("psbl_qty")
            or possible.get("rmn_qty")
            or possible.get("ord_psbl_qty"),
            default=Decimal("0"),
        )
        if possible_quantity <= 0:
            raise RouterError(
                RouterErrorCode.ORDER_REJECTED,
                "증권사 조회에서 정정·취소 가능한 수량이 없습니다.",
            )
        requested_quantity = quantity or possible_quantity
        if requested_quantity > possible_quantity:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "요청 수량이 증권사의 정정·취소 가능 수량보다 큽니다.",
                details={"possible_quantity": str(possible_quantity)},
            )
        account_number = self._settings.get_account_number(account["id"])
        payload, response = await self._kis_order_post(
            connection,
            tr_id="VTTC0013U" if target is ConnectionTarget.PAPER else "TTTC0013U",
            path="/uapi/domestic-stock/v1/trading/order-rvsecncl",
            body={
                "CANO": account_number,
                "ACNT_PRDT_CD": account["product_code"],
                "KRX_FWDG_ORD_ORGNO": str(
                    possible.get("ord_gno_brno")
                    or possible.get("krx_fwdg_ord_orgno")
                    or ""
                ),
                "ORGN_ODNO": provider_order_id,
                "ORD_DVSN": "01" if order_type is OrderType.MARKET else "00",
                "RVSE_CNCL_DVSN_CD": "02" if cancel else "01",
                "ORD_QTY": str(requested_quantity.to_integral_value()),
                "ORD_UNPR": (
                    "0"
                    if cancel or order_type is OrderType.MARKET
                    else str(price or "0")
                ),
                "QTY_ALL_ORD_YN": "Y" if quantity is None else "N",
                "EXCG_ID_DVSN_CD": KIS_ORDER_ROUTE_CODES[actual_route],
                "CNDT_PRIC": "",
            },
        )
        output = payload.get("output")
        changed_id = provider_order_id
        metadata: dict[str, Any] = {}
        if isinstance(output, dict):
            changed_id = str(output.get("ODNO") or output.get("odno") or provider_order_id).strip()
            metadata["order_branch"] = str(
                output.get("KRX_FWDG_ORD_ORGNO")
                or output.get("krx_fwdg_ord_orgno")
                or possible.get("ord_gno_brno")
                or ""
            ).strip()
        return ProviderOrderAck(
            provider_order_id=changed_id,
            provider="kis",
            acknowledged_at=datetime.now(SEOUL),
            correlation_id=_correlation_id(response),
            provider_options=metadata,
            message=str(
                payload.get("msg1")
                or ("주문 취소가 접수되었습니다." if cancel else "주문 정정이 접수되었습니다.")
            ),
        )

    async def _kis_cancellable_order(
        self,
        account: dict[str, Any],
        connection: dict[str, Any],
        provider_order_id: str,
    ) -> dict[str, Any]:
        target = ConnectionTarget(connection["target"])
        payload, _ = await self._kis_get(
            connection,
            path="/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            tr_id="VTTC0084R" if target is ConnectionTarget.PAPER else "TTTC0084R",
            params={
                "CANO": self._settings.get_account_number(account["id"]),
                "ACNT_PRDT_CD": account["product_code"],
                "INQR_DVSN_1": "0",
                "INQR_DVSN_2": "0",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            operation="account.amend_cancel_possible",
            priority=RequestPriority.ORDER,
            group="order",
            dedupe_key=f"{connection['id']}:amend-cancel:{account['id']}:{provider_order_id}",
            cache_ttl=0,
        )
        rows = payload.get("output")
        if not isinstance(rows, list):
            rows = payload.get("output1")
        if not isinstance(rows, list):
            rows = []
        match = next(
            (
                row
                for row in rows
                if isinstance(row, dict)
                and str(row.get("odno") or row.get("ODNO") or "").strip()
                == provider_order_id
            ),
            None,
        )
        if match is None:
            raise RouterError(
                RouterErrorCode.ORDER_REJECTED,
                "증권사의 정정·취소 가능 주문 목록에서 원주문을 찾지 못했습니다.",
            )
        return match

    async def bars(
        self,
        connection_reference: str,
        code: str,
        interval: str,
        *,
        count: int = 100,
        venue: Venue = Venue.INTEGRATED,
        source_alias: str | None = None,
        end: datetime | None = None,
    ) -> tuple[Bar, ...]:
        connection = self._settings.resolve_connection(connection_reference)
        if connection["provider"] != "kis":
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 봉 조회 어댑터는 아직 구현되지 않았습니다.",
            )
        if count < 1 or count > 400:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "봉 개수는 1개부터 400개까지 요청할 수 있습니다.",
            )
        instrument = InstrumentId(code)
        normalized = interval.strip().lower()
        if normalized in {"1d", "d", "day"}:
            return await self._kis_period_bars(
                connection,
                instrument,
                interval="1d",
                period_code="D",
                count=count,
                venue=venue,
                source_alias=source_alias,
                end=end,
            )
        if normalized in {"1w", "w", "week"}:
            return await self._kis_period_bars(
                connection,
                instrument,
                interval="1w",
                period_code="W",
                count=count,
                venue=venue,
                source_alias=source_alias,
                end=end,
            )
        minutes = _minute_interval(normalized)
        return await self._kis_minute_bars(
            connection,
            instrument,
            minutes=minutes,
            count=count,
            venue=venue,
            source_alias=source_alias,
            end=end,
        )

    async def _kis_minute_bars(
        self,
        connection: dict[str, Any],
        instrument: InstrumentId,
        *,
        minutes: int,
        count: int,
        venue: Venue,
        source_alias: str | None,
        end: datetime | None,
    ) -> tuple[Bar, ...]:
        cursor = (end or datetime.now(SEOUL)).astimezone(SEOUL).replace(microsecond=0)
        received_at = datetime.now(SEOUL)
        raw: dict[datetime, tuple[Decimal, Decimal, Decimal, Decimal, Decimal]] = {}
        correlation_ids: list[str] = []
        # One KIS historical minute request returns at most 120 rows. A bounded
        # backward walk lets a 55 x 3-minute indicator obtain its required
        # source window without making one request per candle.
        for _ in range(12):
            payload, response = await self._kis_get(
                connection,
                path="/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
                tr_id="FHKST03010230",
                params={
                    "FID_COND_MRKT_DIV_CODE": KIS_VENUE_CODES[venue],
                    "FID_INPUT_ISCD": instrument.code,
                    "FID_INPUT_HOUR_1": cursor.strftime("%H%M%S"),
                    "FID_INPUT_DATE_1": cursor.strftime("%Y%m%d"),
                    "FID_PW_DATA_INCU_YN": "N",
                    # Include exchange-provided empty intervals when available;
                    # the router still never invents a price locally.
                    "FID_FAKE_TICK_INCU_YN": "Y",
                },
                operation="market.bars.minute",
                priority=RequestPriority.STRATEGY_DATA,
                group="market",
                dedupe_key=(
                    f"{connection['id']}:bars:minute:{instrument.code}:"
                    f"{venue.value}:{cursor:%Y%m%d%H%M%S}"
                ),
                cache_ttl=2.0,
            )
            correlation_ids.append(_correlation_id(response))
            rows = payload.get("output2")
            parsed_times: list[datetime] = []
            if isinstance(rows, list):
                for row in rows:
                    parsed = _parse_minute_row(row)
                    if parsed is None or parsed[0] > cursor:
                        continue
                    stamp, values = parsed
                    raw[stamp] = values
                    parsed_times.append(stamp)

            bars = _aggregate_minute_bars(
                instrument,
                raw,
                minutes=minutes,
                venue=venue,
                provider="kis",
                correlation_id=correlation_ids[0] if correlation_ids else uuid4().hex,
                source_alias=source_alias,
                received_at=received_at,
            )
            if len(bars) >= count:
                return tuple(bars[-count:])

            if parsed_times:
                next_cursor = min(parsed_times) - timedelta(minutes=1)
            else:
                next_cursor = datetime.combine(
                    cursor.date() - timedelta(days=1),
                    time(23, 59, 59),
                    tzinfo=SEOUL,
                )
            if next_cursor >= cursor:
                break
            cursor = next_cursor
        bars = _aggregate_minute_bars(
            instrument,
            raw,
            minutes=minutes,
            venue=venue,
            provider="kis",
            correlation_id=correlation_ids[0] if correlation_ids else uuid4().hex,
            source_alias=source_alias,
            received_at=received_at,
        )
        return tuple(bars[-count:])

    async def _kis_period_bars(
        self,
        connection: dict[str, Any],
        instrument: InstrumentId,
        *,
        interval: str,
        period_code: str,
        count: int,
        venue: Venue,
        source_alias: str | None,
        end: datetime | None,
    ) -> tuple[Bar, ...]:
        end_at = (end or datetime.now(SEOUL)).astimezone(SEOUL)
        calendar_days = max(370, count * (10 if period_code == "W" else 3))
        start_at = end_at - timedelta(days=calendar_days)
        payload, response = await self._kis_get(
            connection,
            path="/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id="FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": KIS_VENUE_CODES[venue],
                "FID_INPUT_ISCD": instrument.code,
                "FID_INPUT_DATE_1": start_at.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end_at.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": period_code,
                "FID_ORG_ADJ_PRC": "0",
            },
            operation="market.bars.period",
            priority=RequestPriority.STRATEGY_DATA,
            group="market",
            dedupe_key=(
                f"{connection['id']}:bars:{period_code}:{instrument.code}:"
                f"{venue.value}:{start_at:%Y%m%d}:{end_at:%Y%m%d}"
            ),
            cache_ttl=30.0,
        )
        rows = payload.get("output2")
        received_at = datetime.now(SEOUL)
        correlation_id = _correlation_id(response)
        bars: list[Bar] = []
        if isinstance(rows, list):
            for row in rows:
                parsed = _parse_period_row(
                    row,
                    instrument=instrument,
                    interval=interval,
                    days=7 if period_code == "W" else 1,
                    venue=venue,
                    provider="kis",
                    correlation_id=correlation_id,
                    source_alias=source_alias,
                    received_at=received_at,
                )
                if parsed is not None:
                    bars.append(parsed)
        bars.sort(key=lambda bar: bar.open_time)
        return tuple(bars[-count:])

    async def close(self) -> None:
        await self._requests.close()
        if self._owns_http:
            await self._http.aclose()

    def _configure_connection(
        self,
        connection_id: str,
        *,
        provider: str = "kis",
        target: ConnectionTarget | None = None,
    ) -> str:
        rate_scope = self._settings.rate_limit_identity(connection_id)
        if rate_scope in self._configured:
            return rate_scope
        profile = rest_rate_limit_profile(
            provider,
            (target or ConnectionTarget.LIVE).value,
        )
        requests_per_second = float(profile["requests_per_second"])
        websocket_profile = websocket_rate_limit_profile(
            provider,
            (target or ConnectionTarget.LIVE).value,
        )
        self._rate_limits.configure(
            f"{rate_scope}:auth",
            RateLimitPolicy(capacity=1, refill_per_second=1),
        )
        self._rate_limits.configure(
            f"{rate_scope}:rest-global",
            RateLimitPolicy(
                capacity=1,
                refill_per_second=requests_per_second,
            ),
        )
        for group in ("market", "account", "order"):
            self._rate_limits.configure(
                f"{rate_scope}:{group}",
                RateLimitPolicy(
                    capacity=1,
                    refill_per_second=requests_per_second,
                ),
            )
        self._websocket_quotas.configure(
            rate_scope,
            WebSocketLimitPolicy(
                max_sessions=int(
                    websocket_profile["operating_max_sessions_per_key"]
                ),
                max_subscriptions_per_session=int(
                    websocket_profile["max_subscriptions_per_session"]
                ),
                control_messages_per_second=float(
                    websocket_profile["control_messages_per_second"]
                ),
            ),
        )
        self._configured.add(rate_scope)
        return rate_scope

    async def _kis_guarded_request(
        self,
        rate_scope: str,
        operation: Any,
        *,
        order_submission: bool = False,
    ) -> httpx.Response:
        """Serialize one KIS key and reserve a quiet window before an order.

        KIS can return EGW00201 when a valid order immediately follows account
        or market-data REST traffic even though the documented aggregate rate
        has not been exceeded. Holding the per-key lock during this short quiet
        window prevents Studio refreshes and strategy reads from racing the
        final submission boundary.
        """

        lock = self._kis_request_locks.setdefault(rate_scope, asyncio.Lock())
        async with lock:
            if order_submission:
                now = asyncio.get_running_loop().time()
                last_completed = self._kis_last_request_completed.get(rate_scope)
                if last_completed is not None:
                    await asyncio.sleep(max(0.0, 1.1 - (now - last_completed)))
            try:
                return await operation()
            finally:
                self._kis_last_request_completed[rate_scope] = (
                    asyncio.get_running_loop().time()
                )

    def _token_scope(self, connection: dict[str, Any]) -> TokenScopeKey:
        return TokenScopeKey(
            provider=connection["provider"],
            connection_id=self._settings.rate_limit_identity(connection["id"]),
            target=ConnectionTarget(connection["target"]),
        )

    async def _access_token(self, connection: dict[str, Any]) -> AccessToken:
        provider = connection["provider"]
        if provider == "kis":
            issuer = lambda _: self._issue_kis_token(connection)
        elif provider == "namuh":
            issuer = lambda _: self._issue_namuh_token(connection)
        else:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                "이 증권사의 인증 어댑터는 아직 구현되지 않았습니다.",
            )
        token = await self._tokens.get(self._token_scope(connection), issuer)
        self._mark_connected(connection, token)
        return token

    def _mark_connected(
        self,
        connection: dict[str, Any],
        token: AccessToken,
    ) -> dict[str, Any]:
        result = {
            "connection_id": connection["id"],
            "state": "connected",
            "checked_at": datetime.now().astimezone().isoformat(),
            "expires_at": token.expires_at.isoformat(),
            "message": "접근 토큰을 확인했습니다.",
        }
        self._statuses[connection["id"]] = result
        return result

    async def _issue_kis_token(self, connection: dict[str, Any]) -> AccessToken:
        connection_id = connection["id"]
        target = ConnectionTarget(connection["target"])
        rate_scope = self._configure_connection(
            connection_id, provider="kis", target=target
        )
        credentials = self._settings.get_credentials(connection_id)
        url = f"{KIS_BASE_URLS[target]}/oauth2/tokenP"

        async def send() -> AccessToken:
            try:
                response = await self._http.post(
                    url,
                    headers={"Content-Type": "application/json; charset=UTF-8"},
                    json={
                        "grant_type": "client_credentials",
                        "appkey": credentials["app_key"],
                        "appsecret": credentials["app_secret"],
                    },
                )
            except httpx.HTTPError as exc:
                raise RouterError(
                    RouterErrorCode.AUTHENTICATION_FAILED,
                    "증권사 인증 서버에 연결하지 못했습니다.",
                ) from exc

            payload = _safe_json(response)
            if response.status_code != 200 or "access_token" not in payload:
                code = str(payload.get("error_code") or payload.get("msg_cd") or "")
                message = str(
                    payload.get("error_description")
                    or payload.get("msg1")
                    or "App Key 또는 App Secret 인증이 거부되었습니다."
                )
                suffix = f" [{code}]" if code else ""
                raise RouterError(
                    RouterErrorCode.AUTHENTICATION_FAILED,
                    f"{message}{suffix} (HTTP {response.status_code})",
                )

            now = datetime.now().astimezone()
            expires_at = _parse_kis_expiry(payload, now)
            return AccessToken(
                value=str(payload["access_token"]),
                token_type=str(payload.get("token_type", "Bearer")),
                issued_at=now,
                expires_at=expires_at,
            )

        intent = BrokerRequestIntent(
            connection_id=connection_id,
            operation="auth.issue_token",
            priority=RequestPriority.ACCOUNT,
            rate_limit_scopes=(RateLimitScope(f"{rate_scope}:auth"),),
            dedupe_key=f"{connection_id}:auth:token",
        )
        return await self._requests.execute(intent, send)

    async def _issue_namuh_token(self, connection: dict[str, Any]) -> AccessToken:
        connection_id = connection["id"]
        rate_scope = self._configure_connection(
            connection_id,
            provider="namuh",
            target=ConnectionTarget(connection["target"]),
        )
        credentials = self._settings.get_credentials(connection_id)

        async def send() -> AccessToken:
            try:
                response = await self._http.post(
                    f"{NAMUH_BASE_URL}/oauth2/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "appkey": credentials["app_key"],
                        "appsecretkey": credentials["app_secret"],
                        "grant_type": "client_credentials",
                        "scope": "oob",
                    },
                )
            except httpx.HTTPError as exc:
                raise RouterError(
                    RouterErrorCode.AUTHENTICATION_FAILED,
                    "증권사 인증 서버에 연결하지 못했습니다.",
                ) from exc
            payload = _safe_json(response)
            if response.status_code != 200 or "access_token" not in payload:
                code = str(payload.get("error_code") or payload.get("rsp_cd") or "")
                message = str(
                    payload.get("error_description")
                    or payload.get("rsp_msg")
                    or "App Key 또는 App Secret Key 인증이 거부되었습니다."
                )
                suffix = f" [{code}]" if code else ""
                raise RouterError(
                    RouterErrorCode.AUTHENTICATION_FAILED,
                    f"{message}{suffix} (HTTP {response.status_code})",
                )
            now = datetime.now().astimezone()
            try:
                seconds = max(60, int(payload.get("expires_in", 86_400)))
            except (TypeError, ValueError):
                seconds = 86_400
            return AccessToken(
                value=str(payload["access_token"]),
                token_type=str(payload.get("token_type", "Bearer")),
                issued_at=now,
                expires_at=now + timedelta(seconds=seconds),
            )

        return await self._requests.execute(
            BrokerRequestIntent(
                connection_id=connection_id,
                operation="auth.issue_token",
                priority=RequestPriority.ACCOUNT,
                rate_limit_scopes=(RateLimitScope(f"{rate_scope}:auth"),),
                dedupe_key=f"{connection_id}:auth:token",
            ),
            send,
        )

    async def _namuh_balance(
        self, account: dict[str, Any], connection: dict[str, Any]
    ) -> Balance:
        payload, _ = await self._namuh_post(
            connection,
            path="/krstock/inquiry/v1/balance",
            input_payload={
                "act_no": self._settings.get_account_number(account["id"]),
                "bnc_bse_cd": "5",
                "ltg_aot_dit_cd": "1",
                "aet_bse": "1",
                "qut_dit_cd": "UNT",
                "aly_qut_cd": "2",
            },
            operation="account.balance",
            priority=RequestPriority.ACCOUNT,
            group="account",
            dedupe_key=f"{connection['id']}:balance:{account['id']}",
            cache_ttl=1.0,
        )
        summary = payload.get("Output_0")
        rows = payload.get("Output_1")
        if not isinstance(summary, dict):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                str(payload.get("rsp_msg") or "NAMUH 잔고 응답에 필요한 자료가 없습니다."),
            )
        if not isinstance(rows, list):
            rows = []
        positions: list[Position] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("iem_cd") or "").strip()
            quantity = _decimal(row.get("itg_bnc_qty"), default=Decimal("0"))
            if not code or quantity <= 0:
                continue
            positions.append(
                Position(
                    instrument=InstrumentId(code),
                    quantity=quantity,
                    available_quantity=_decimal(
                        row.get("rsdl_qty"), default=Decimal("0")
                    ),
                    average_price=_decimal(
                        row.get("phs_pr"), default=Decimal("0")
                    ),
                    market_price=_decimal(
                        row.get("now_pr"), default=Decimal("0")
                    ),
                )
            )
        cash = _decimal(summary.get("dca"), default=Decimal("0"))
        withdrawable = _decimal(
            summary.get("drn_pbl_amt"), default=cash
        )
        total = _decimal(summary.get("tot_aet_amt"), default=cash)
        return Balance(
            cash=cash,
            withdrawable_cash=withdrawable,
            total_value=total,
            positions=tuple(positions),
            as_of=datetime.now(SEOUL),
        )

    async def _namuh_account_orders(
        self,
        account: dict[str, Any],
        connection: dict[str, Any],
        day: date,
        *,
        cache_ttl: float = 1.0,
    ) -> tuple[AccountOrder, ...]:
        input_payload = {
            "orr_dt": day.strftime("%Y%m%d"),
            "act_no": self._settings.get_account_number(account["id"]),
            "itg_orr_no": "",
            "orr_mkt_cd": "00",
            "ost_cns_dit": "0",
        }
        rows: list[dict[str, Any]] = []
        cts = ""
        cts_flag = ""
        seen: set[str] = set()
        for _ in range(10):
            payload, response = await self._namuh_post(
                connection,
                path="/krstock/inquiry/v1/dailyOrderExecution",
                input_payload=input_payload,
                operation="account.orders",
                priority=RequestPriority.RECONCILIATION,
                group="account",
                dedupe_key=(
                    f"{connection['id']}:orders:{account['id']}:"
                    f"{day.isoformat()}:{cts}"
                ),
                cache_ttl=cache_ttl,
                cts=cts,
                cts_flag=cts_flag,
            )
            output = payload.get("Output_0")
            if isinstance(output, dict):
                rows.append(output)
            elif isinstance(output, list):
                rows.extend(item for item in output if isinstance(item, dict))
            next_cts = str(response.headers.get("cts") or "").strip()
            next_flag = str(response.headers.get("cts_flag") or "").strip().upper()
            has_next = bool(next_cts) and (
                next_flag == "Y"
                or (
                    not next_flag
                    and str(payload.get("rsp_cd") or "") in {"00165", "00218"}
                )
            )
            if not has_next or next_cts in seen:
                break
            seen.add(next_cts)
            cts, cts_flag = next_cts, next_flag
        parsed = tuple(
            order
            for row in rows
            if isinstance(row, dict)
            and (order := _parse_namuh_account_order(row, account["alias"], day))
            is not None
        )
        unique = {order.provider_order_id: order for order in parsed}
        return tuple(
            sorted(unique.values(), key=lambda item: item.ordered_at, reverse=True)
        )

    async def _namuh_buying_power(
        self,
        account: dict[str, Any],
        connection: dict[str, Any],
        code: str,
        *,
        order_type: OrderType,
        price: Decimal | None,
    ) -> BuyingPower:
        instrument = InstrumentId(code)
        if order_type is OrderType.LIMIT and (price is None or price <= 0):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "지정가 주문 가능 수량을 조회하려면 가격이 필요합니다.",
            )
        requested_price = price or Decimal("0")
        if price is not None:
            _require_namuh_integer_price(price)
        input_payload: dict[str, Any] = {
            "ost_dit_cd": "1",
            "act_no": self._settings.get_account_number(account["id"]),
            "iem_cd": instrument.code,
            "nmn_pr_tp_cd": "05" if order_type is OrderType.MARKET else "01",
        }
        if price is not None:
            input_payload["orr_pr"] = int(price)
        payload, _ = await self._namuh_post(
            connection,
            path="/krstock/inquiry/v1/buyableQuantity",
            input_payload=input_payload,
            operation="account.buying_power",
            priority=RequestPriority.RISK,
            group="account",
            dedupe_key=(
                f"{connection['id']}:buying-power:{account['id']}:"
                f"{instrument.code}:{order_type.value}:{requested_price}"
            ),
            cache_ttl=0.5,
        )
        output = payload.get("Output_0")
        if not isinstance(output, dict):
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                str(payload.get("rsp_msg") or "NAMUH 주문 가능 수량 응답에 필요한 자료가 없습니다."),
            )
        maximum = _decimal(
            output.get("csh_orr_pbl_qty") or output.get("max_pbl_qty"),
            default=Decimal("0"),
        )
        maximum_amount = _decimal(
            output.get("csh_orr_pbl_amt") or output.get("max_pbl_amt"),
            default=Decimal("0"),
        )
        calculation_price = requested_price
        if maximum > 0 and maximum_amount > 0:
            calculation_price = maximum_amount / maximum
        return BuyingPower(
            instrument=instrument,
            account_alias=account["alias"],
            order_type=order_type,
            requested_price=requested_price,
            calculation_price=calculation_price,
            cash_available=_decimal(
                output.get("csh_orr_pbl_amt") or output.get("dca"),
                default=maximum_amount,
            ),
            maximum_quantity=maximum,
            provider="namuh",
            as_of=datetime.now(SEOUL),
        )

    async def _namuh_submit_order(
        self,
        account: dict[str, Any],
        connection: dict[str, Any],
        request: OrderRequest,
    ) -> ProviderOrderAck:
        route = request.route if request.route is not OrderRoute.AUTO else OrderRoute.SOR
        if route not in self.capabilities(account["alias"]).order_routes:
            raise RouterError(
                RouterErrorCode.UNSUPPORTED_CAPABILITY,
                f"이 API 연결은 {route.value} 주문 경로를 지원하지 않습니다.",
            )
        if request.provider_options:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "NAMUH 현금 주문은 현재 provider_options를 받지 않습니다.",
            )
        input_payload: dict[str, Any] = {
            "act_no": self._settings.get_account_number(account["id"]),
            "iem_cd": request.instrument.code,
            "orr_qty": int(request.quantity),
            "nmn_pr_tp_cd": "05" if request.order_type is OrderType.MARKET else "01",
            "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": route.value,
            "sor_mkt_sli_yn": "N",
        }
        if request.order_type is OrderType.LIMIT:
            limit_price = request.price or Decimal("0")
            _require_namuh_integer_price(limit_price)
            input_payload["orr_pr"] = int(limit_price)
        payload, response = await self._namuh_post(
            connection,
            path=(
                "/krstock/order/v1/cashBuy"
                if request.side is OrderSide.BUY
                else "/krstock/order/v1/cashSell"
            ),
            input_payload=input_payload,
            operation="order.submit",
            priority=RequestPriority.ORDER,
            group="order",
            dedupe_key=f"{connection['id']}:order-submit:{request.client_order_id}",
            cache_ttl=0,
            retry_auth=False,
        )
        output = payload.get("Output_0")
        provider_order_id = (
            _normalize_namuh_order_id(output.get("mkt_orr_no"))
            if isinstance(output, dict)
            else ""
        )
        if not provider_order_id:
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN,
                str(payload.get("rsp_msg") or "NAMUH 주문번호를 확인하지 못했습니다."),
                details={"correlation_id": _correlation_id(response)},
            )
        return ProviderOrderAck(
            provider_order_id=provider_order_id,
            provider="namuh",
            acknowledged_at=datetime.now(SEOUL),
            correlation_id=_correlation_id(response),
            message=str(payload.get("rsp_msg") or "주문이 접수되었습니다."),
        )

    async def _namuh_change_order(
        self,
        account: dict[str, Any],
        connection: dict[str, Any],
        provider_order_id: str,
        *,
        quantity: Decimal | None,
        order_type: OrderType,
        price: Decimal | None,
        route: OrderRoute,
        cancel: bool,
    ) -> ProviderOrderAck:
        orders = await self._namuh_account_orders(
            account,
            connection,
            datetime.now(SEOUL).date(),
            cache_ttl=0,
        )
        original = next(
            (
                item
                for item in orders
                if item.provider_order_id
                == _normalize_namuh_order_id(provider_order_id)
            ),
            None,
        )
        if original is None or original.remaining_quantity <= 0:
            raise RouterError(
                RouterErrorCode.ORDER_REJECTED,
                "NAMUH 미체결 주문 조회에서 원주문을 찾지 못했습니다.",
            )
        actual_route = original.route if route is OrderRoute.AUTO else route
        if actual_route is OrderRoute.AUTO:
            actual_route = OrderRoute.SOR
        requested_quantity = quantity or original.remaining_quantity
        if requested_quantity <= 0 or requested_quantity > original.remaining_quantity:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "정정·취소 수량이 NAMUH 미체결 수량보다 큽니다.",
            )
        full = requested_quantity == original.remaining_quantity
        normalized_provider_order_id = _normalize_namuh_order_id(provider_order_id)
        try:
            numeric_provider_order_id = int(normalized_provider_order_id)
        except ValueError as exc:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "NAMUH 원주문 번호는 숫자여야 합니다.",
            ) from exc
        input_payload: dict[str, Any] = {
            "act_no": self._settings.get_account_number(account["id"]),
            "org_mkt_orr_no": numeric_provider_order_id,
            "all_pat_dit_cd": "1" if full else "2",
            "iem_cd": original.instrument.code,
        }
        path = "/krstock/order/v1/cancel" if cancel else "/krstock/order/v1/modify"
        if cancel:
            if not full:
                input_payload["cor_qty"] = int(requested_quantity)
        else:
            if order_type is not OrderType.LIMIT or price is None or price <= 0:
                raise RouterError(
                    RouterErrorCode.INVALID_REQUEST,
                    "NAMUH 정정 주문은 지정가와 가격이 필요합니다.",
                )
            _require_namuh_integer_price(price)
            input_payload.update(
                {
                    "cor_qty": 0 if full else int(requested_quantity),
                    "cor_pr": int(price),
                    "sop_cnd_pr": "",
                    "rmt_mkt_cd": actual_route.value,
                    "sor_mkt_sli_yn": "N",
                }
            )
        payload, response = await self._namuh_post(
            connection,
            path=path,
            input_payload=input_payload,
            operation="order.cancel" if cancel else "order.amend",
            priority=RequestPriority.ORDER,
            group="order",
            dedupe_key=(
                f"{connection['id']}:{'cancel' if cancel else 'amend'}:"
                f"{provider_order_id}:{requested_quantity}:{price or ''}"
            ),
            cache_ttl=0,
            retry_auth=False,
        )
        output = payload.get("Output_0")
        changed_id = (
            _normalize_namuh_order_id(
                output.get("mkt_orr_no") or provider_order_id
            )
            if isinstance(output, dict)
            else ""
        )
        if not changed_id:
            raise RouterError(
                RouterErrorCode.ORDER_STATUS_UNKNOWN,
                str(payload.get("rsp_msg") or "NAMUH 주문 변경 결과를 확인하지 못했습니다."),
                details={"correlation_id": _correlation_id(response)},
            )
        return ProviderOrderAck(
            provider_order_id=changed_id,
            provider="namuh",
            acknowledged_at=datetime.now(SEOUL),
            correlation_id=_correlation_id(response),
            message=str(
                payload.get("rsp_msg")
                or ("주문 취소가 접수되었습니다." if cancel else "주문 정정이 접수되었습니다.")
            ),
        )

    async def _namuh_quote(
        self,
        connection: dict[str, Any],
        code: str,
        venue: Venue,
        *,
        source_alias: str | None,
    ) -> Quote:
        instrument = InstrumentId(code)
        payload, response = await self._namuh_post(
            connection,
            path="/krstock/quote/v1/currentPrice",
            input_payload={
                "iem_cd": instrument.code,
                "market_cd": NAMUH_VENUE_CODES[venue],
            },
            operation="market.quote",
            priority=RequestPriority.STRATEGY_DATA,
            group="market",
            dedupe_key=(
                f"{connection['id']}:quote:{instrument.code}:{venue.value}"
            ),
            cache_ttl=0.5,
        )
        output = payload.get("Output_0")
        if not isinstance(output, dict):
            message = str(payload.get("rsp_msg") or "증권사 현재가 응답에 필요한 자료가 없습니다.")
            raise RouterError(RouterErrorCode.INVALID_REQUEST, message)
        now = datetime.now(SEOUL)
        return Quote(
            instrument=instrument,
            price=_decimal(output.get("stck_prpr")),
            venue=venue,
            provider="namuh",
            source_alias=source_alias,
            market_time=_market_time(
                output.get("hoga_bsop_hour") or output.get("memb_bsop_hour"),
                now,
            ),
            received_at=now,
            correlation_id=_correlation_id(response),
        )

    async def _namuh_post(
        self,
        connection: dict[str, Any],
        *,
        path: str,
        input_payload: dict[str, Any],
        operation: str,
        priority: RequestPriority,
        group: str,
        dedupe_key: str,
        cache_ttl: float,
        retry_auth: bool = True,
        cts: str = "",
        cts_flag: str = "",
    ) -> tuple[dict[str, Any], httpx.Response]:
        connection_id = connection["id"]
        rate_scope = self._configure_connection(
            connection_id,
            provider="namuh",
            target=ConnectionTarget(connection["target"]),
        )
        credentials = self._settings.get_credentials(connection_id)
        token = await self._access_token(connection)

        async def send() -> tuple[dict[str, Any], httpx.Response]:
            try:
                headers = {
                    "x-client-id": credentials["app_key"],
                    "x-client-secret": credentials["app_secret"],
                    "authorization": f"{token.token_type} {token.value}",
                    "content-type": "application/json; charset=UTF-8",
                }
                if cts:
                    headers["cts"] = cts
                if cts_flag:
                    headers["cts_flag"] = cts_flag
                response = await self._http.post(
                    f"{NAMUH_BASE_URLS[ConnectionTarget(connection['target'])]}{path}",
                    headers=headers,
                    json={"Input_0": input_payload},
                )
            except httpx.HTTPError as exc:
                raise RouterError(
                    (
                        RouterErrorCode.ORDER_STATUS_UNKNOWN
                        if group == "order"
                        else RouterErrorCode.INVALID_REQUEST
                    ),
                    "증권사 API 요청에 실패했습니다.",
                ) from exc
            payload = _safe_json(response)
            code = str(payload.get("error_code") or payload.get("rsp_cd") or "")
            message = str(
                payload.get("error_description")
                or payload.get("rsp_msg")
                or "증권사 조회 요청이 거부되었습니다."
            )
            if response.status_code == 401 or code == "IGW40043":
                raise RouterError(RouterErrorCode.AUTHENTICATION_FAILED, message)
            if response.status_code == 429:
                raise RouterError(RouterErrorCode.RATE_LIMITED, message)
            if response.status_code != 200:
                suffix = f" [{code}]" if code else ""
                raise RouterError(
                    RouterErrorCode.INVALID_REQUEST,
                    f"{message}{suffix} (HTTP {response.status_code})",
                )
            return payload, response

        intent = BrokerRequestIntent(
            connection_id=connection_id,
            operation=operation,
            priority=priority,
            rate_limit_scopes=(
                RateLimitScope(f"{rate_scope}:rest-global"),
                RateLimitScope(f"{rate_scope}:{group}"),
            ),
            dedupe_key=dedupe_key,
            cache_ttl=cache_ttl,
        )
        try:
            return await self._requests.execute(intent, send)
        except RouterError as exc:
            if exc.code is not RouterErrorCode.AUTHENTICATION_FAILED or not retry_auth:
                raise
            await self._tokens.invalidate(self._token_scope(connection))
            return await self._namuh_post(
                connection,
                path=path,
                input_payload=input_payload,
                operation=operation,
                priority=priority,
                group=group,
                dedupe_key=dedupe_key,
                cache_ttl=cache_ttl,
                retry_auth=False,
                cts=cts,
                cts_flag=cts_flag,
            )

    async def _kis_get(
        self,
        connection: dict[str, Any],
        *,
        path: str,
        tr_id: str,
        params: dict[str, str],
        operation: str,
        priority: RequestPriority,
        group: str,
        dedupe_key: str,
        cache_ttl: float,
        tr_cont: str = "",
        retry_auth: bool = True,
        rate_limit_retries: int = 2,
    ) -> tuple[dict[str, Any], httpx.Response]:
        connection_id = connection["id"]
        target = ConnectionTarget(connection["target"])
        rate_scope = self._configure_connection(
            connection_id, provider="kis", target=target
        )
        credentials = self._settings.get_credentials(connection_id)
        token = await self._access_token(connection)
        url = f"{KIS_BASE_URLS[target]}{path}"

        async def send() -> tuple[dict[str, Any], httpx.Response]:
            try:
                response = await self._kis_guarded_request(
                    rate_scope,
                    lambda: self._http.get(
                        url,
                        headers={
                            "Content-Type": "application/json; charset=UTF-8",
                            "authorization": f"{token.token_type} {token.value}",
                            "appkey": credentials["app_key"],
                            "appsecret": credentials["app_secret"],
                            "tr_id": tr_id,
                            "custtype": "P",
                            **({"tr_cont": tr_cont} if tr_cont else {}),
                        },
                        params=params,
                    ),
                )
            except httpx.HTTPError as exc:
                raise RouterError(
                    RouterErrorCode.INVALID_REQUEST,
                    "증권사 API 요청에 실패했습니다.",
                ) from exc
            payload = _safe_json(response)
            code = str(payload.get("msg_cd") or "")
            message = str(payload.get("msg1") or "증권사 조회 요청이 거부되었습니다.")
            if response.status_code in {401, 403} or code in {"EGW00123", "EGW00105"}:
                raise RouterError(
                    RouterErrorCode.AUTHENTICATION_FAILED,
                    str(payload.get("msg1") or "증권사 인증이 만료되었거나 거부되었습니다."),
                )
            if (
                response.status_code == 429
                or code in {"EGW00201", "EGW00202"}
                or "초당 거래건수" in message
            ):
                raise RouterError(
                    RouterErrorCode.RATE_LIMITED,
                    message,
                    details={
                        "retry_after": _retry_after_seconds(
                            response.headers.get("retry-after"),
                            default=0.75,
                        )
                    },
                )
            if response.status_code != 200 or str(payload.get("rt_cd", "0")) != "0":
                suffix = f" [{code}]" if code else ""
                raise RouterError(
                    RouterErrorCode.INVALID_REQUEST,
                    f"{message}{suffix} (HTTP {response.status_code})",
                )
            return payload, response

        intent = BrokerRequestIntent(
            connection_id=connection_id,
            operation=operation,
            priority=priority,
            rate_limit_scopes=(
                RateLimitScope(f"{rate_scope}:rest-global"),
                RateLimitScope(f"{rate_scope}:{group}"),
            ),
            dedupe_key=dedupe_key,
            cache_ttl=cache_ttl,
        )
        try:
            return await self._requests.execute(intent, send)
        except RouterError as exc:
            if exc.code is RouterErrorCode.RATE_LIMITED and rate_limit_retries > 0:
                await asyncio.sleep(
                    _retry_after_seconds(
                        exc.details.get("retry_after"),
                        default=0.75,
                    )
                )
                return await self._kis_get(
                    connection,
                    path=path,
                    tr_id=tr_id,
                    params=params,
                    operation=operation,
                    priority=priority,
                    group=group,
                    dedupe_key=dedupe_key,
                    cache_ttl=cache_ttl,
                    tr_cont=tr_cont,
                    retry_auth=retry_auth,
                    rate_limit_retries=rate_limit_retries - 1,
                )
            if exc.code is not RouterErrorCode.AUTHENTICATION_FAILED or not retry_auth:
                raise
            await self._tokens.invalidate(self._token_scope(connection))
            return await self._kis_get(
                connection,
                path=path,
                tr_id=tr_id,
                params=params,
                operation=operation,
                priority=priority,
                group=group,
                dedupe_key=dedupe_key,
                cache_ttl=cache_ttl,
                tr_cont=tr_cont,
                retry_auth=False,
                rate_limit_retries=rate_limit_retries,
            )

    async def _kis_order_post(
        self,
        connection: dict[str, Any],
        *,
        tr_id: str,
        body: dict[str, str],
        path: str = "/uapi/domestic-stock/v1/trading/order-cash",
    ) -> tuple[dict[str, Any], httpx.Response]:
        connection_id = connection["id"]
        target = ConnectionTarget(connection["target"])
        rate_scope = self._configure_connection(
            connection_id, provider="kis", target=target
        )
        credentials = self._settings.get_credentials(connection_id)
        # Authentication finishes before the order enters the broker request
        # queue. An auth failure is therefore a definite non-submission.
        token = await self._access_token(connection)
        url = f"{KIS_BASE_URLS[target]}{path}"

        async def send() -> tuple[dict[str, Any], httpx.Response]:
            try:
                response = await self._kis_guarded_request(
                    rate_scope,
                    lambda: self._http.post(
                        url,
                        headers={
                            "Content-Type": "application/json; charset=UTF-8",
                            "authorization": f"{token.token_type} {token.value}",
                            "appkey": credentials["app_key"],
                            "appsecret": credentials["app_secret"],
                            "tr_id": tr_id,
                            "custtype": "P",
                        },
                        json=body,
                    ),
                    order_submission=True,
                )
            except httpx.HTTPError as exc:
                # The socket may have failed after KIS accepted the order.
                # The central service must reconcile and must never resend it.
                raise RouterError(
                    RouterErrorCode.ORDER_STATUS_UNKNOWN,
                    "주문 응답을 받지 못해 접수 여부를 확인해야 합니다.",
                ) from exc
            payload = _safe_json(response)
            code = str(payload.get("msg_cd") or "")
            message = str(payload.get("msg1") or "증권사가 주문을 거절했습니다.")
            if response.status_code in {401, 403} or code in {"EGW00123", "EGW00105"}:
                raise RouterError(RouterErrorCode.AUTHENTICATION_FAILED, message)
            if response.status_code == 429:
                raise RouterError(RouterErrorCode.RATE_LIMITED, message)
            if response.status_code != 200 or str(payload.get("rt_cd", "0")) != "0":
                suffix = f" [{code}]" if code else ""
                raise RouterError(
                    RouterErrorCode.ORDER_REJECTED,
                    f"{message}{suffix} (HTTP {response.status_code})",
                )
            return payload, response

        return await self._requests.execute(
            BrokerRequestIntent(
                connection_id=connection_id,
                operation="orders.change" if path.endswith("order-rvsecncl") else "orders.submit",
                priority=RequestPriority.ORDER,
                rate_limit_scopes=(
                    RateLimitScope(f"{rate_scope}:rest-global"),
                    RateLimitScope(f"{rate_scope}:order"),
                ),
                # Submission idempotency belongs to RuntimeStore. RequestManager
                # must not cache or retry a broker order.
                dedupe_key=None,
                cache_ttl=0,
            ),
            send,
        )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _retry_after_seconds(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(5.0, max(0.0, parsed))


def _parse_kis_expiry(payload: dict[str, Any], now: datetime) -> datetime:
    explicit = payload.get("access_token_token_expired")
    if explicit:
        text = str(explicit).strip()
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                return parsed.replace(tzinfo=now.tzinfo)
            except ValueError:
                continue
    try:
        seconds = max(60, int(payload.get("expires_in", 86_400)))
    except (TypeError, ValueError):
        seconds = 86_400
    return now + timedelta(seconds=seconds)


def _decimal(value: Any, *, default: Decimal | None = None) -> Decimal:
    try:
        text = str(value).strip().replace(",", "")
        if not text:
            raise InvalidOperation
        return Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        if default is not None:
            return default
        raise RouterError(
            RouterErrorCode.INVALID_REQUEST,
            "증권사 응답의 숫자 값을 해석하지 못했습니다.",
        )


def _first_text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _optional_decimal(row: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return _decimal(value, default=Decimal("0"))
    return None


def _query_number(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _market_time(value: Any, now: datetime) -> datetime:
    text = "".join(character for character in str(value or "") if character.isdigit())
    if len(text) >= 6:
        try:
            return now.replace(
                hour=int(text[0:2]),
                minute=int(text[2:4]),
                second=int(text[4:6]),
                microsecond=0,
            )
        except ValueError:
            pass
    return now


def _parse_kis_account_order(
    row: dict[str, Any],
    account_alias: str,
    fallback_date: date,
) -> AccountOrder | None:
    provider_order_id = str(row.get("odno") or row.get("ord_no") or "").strip()
    code = str(row.get("pdno") or row.get("shtn_pdno") or "").strip()
    if not provider_order_id or not code:
        return None

    requested = _decimal(row.get("ord_qty"), default=Decimal("0"))
    filled = _decimal(row.get("tot_ccld_qty"), default=Decimal("0"))
    remaining = _decimal(row.get("rmn_qty"), default=max(Decimal("0"), requested - filled))
    cancelled_quantity = _kis_cancelled_quantity(row)
    if cancelled_quantity > 0:
        remaining = min(
            remaining,
            max(Decimal("0"), requested - filled - cancelled_quantity),
        )
    # Some KIS responses do not carry rmn_qty. Derive it without allowing the
    # normalized quantities to exceed the requested amount.
    if requested > 0 and filled + remaining > requested:
        remaining = max(Decimal("0"), requested - filled)

    side_code = str(row.get("sll_buy_dvsn_cd") or "").strip()
    side_name = str(
        row.get("sll_buy_dvsn_cd_name")
        or row.get("sll_buy_dvsn_name")
        or ""
    )
    side = OrderSide.SELL if side_code == "01" or "매도" in side_name else OrderSide.BUY

    order_name = str(row.get("ord_dvsn_name") or "")
    order_type = OrderType.MARKET if "시장" in order_name else OrderType.LIMIT
    rejected = _decimal(row.get("rjct_qty"), default=Decimal("0"))
    cancelled = (
        str(row.get("cncl_yn") or "").strip().upper() == "Y"
        or cancelled_quantity > 0
        or str(row.get("rvse_cncl_dvsn_cd") or "").strip() == "02"
        or "취소" in order_name
    )
    if rejected > 0 and requested > 0 and rejected >= requested:
        status = OrderStatus.REJECTED
    elif cancelled and remaining <= 0:
        status = OrderStatus.CANCELLED
    elif requested > 0 and filled >= requested:
        status = OrderStatus.FILLED
    elif filled > 0:
        status = OrderStatus.PARTIALLY_FILLED
    else:
        status = OrderStatus.ACKNOWLEDGED

    route_text = str(
        row.get("excg_id_dvsn_cd") or row.get("excg_dvsn_cd") or ""
    ).strip().upper()
    try:
        route = OrderRoute(route_text)
    except ValueError:
        route = OrderRoute.AUTO

    return AccountOrder(
        provider_order_id=provider_order_id,
        account_alias=account_alias,
        instrument=InstrumentId(code),
        side=side,
        order_type=order_type,
        status=status,
        requested_quantity=requested,
        filled_quantity=filled,
        remaining_quantity=remaining,
        ordered_at=_kis_order_time(row, fallback_date),
        provider="kis",
        price=_decimal(row.get("ord_unpr"), default=Decimal("0")),
        average_fill_price=_decimal(
            row.get("avg_prvs") or row.get("avg_prvs_ccld_unpr"),
            default=Decimal("0"),
        ),
        route=route,
    )


def _normalize_kis_account_orders(
    rows: list[dict[str, Any]],
    account_alias: str,
    fallback_date: date,
) -> tuple[AccountOrder, ...]:
    """Fold KIS amend/cancel child rows into their original order.

    KIS can return a manual cancellation as a separate daily-history row whose
    ``orgn_odno`` points at the original order.  Keeping only rows keyed by
    ``odno`` leaves the original looking open forever.  The router ledger is
    keyed by that original broker order number, so cancellation quantities
    must be reflected on the original normalized order.
    """

    parsed: dict[str, AccountOrder] = {}
    source_rows: dict[str, dict[str, Any]] = {}
    cancelled_by_original: dict[str, Decimal] = {}
    cancellation_time: dict[str, datetime] = {}

    for row in rows:
        order = _parse_kis_account_order(row, account_alias, fallback_date)
        if order is None:
            continue
        parsed[order.provider_order_id] = order
        source_rows[order.provider_order_id] = row

        original_id = str(
            row.get("orgn_odno") or row.get("ORGN_ODNO") or ""
        ).strip()
        if not original_id or original_id == "0" or original_id == order.provider_order_id:
            continue
        order_name = str(row.get("ord_dvsn_name") or "")
        is_cancellation = (
            str(row.get("rvse_cncl_dvsn_cd") or "").strip() == "02"
            or str(row.get("cncl_yn") or "").strip().upper() == "Y"
            or "취소" in order_name
        )
        if not is_cancellation:
            continue
        quantity = _kis_cancelled_quantity(row)
        if quantity <= 0:
            quantity = _decimal(row.get("ord_qty"), default=Decimal("0"))
        cancelled_by_original[original_id] = (
            cancelled_by_original.get(original_id, Decimal("0")) + quantity
        )
        cancellation_time[original_id] = max(
            cancellation_time.get(original_id, order.ordered_at),
            order.ordered_at,
        )

    child_ids: set[str] = set()
    for child_id, row in source_rows.items():
        original_id = str(
            row.get("orgn_odno") or row.get("ORGN_ODNO") or ""
        ).strip()
        if original_id in cancelled_by_original and child_id != original_id:
            child_ids.add(child_id)

    for original_id, cancelled_quantity in cancelled_by_original.items():
        original = parsed.get(original_id)
        if original is None:
            continue
        remaining = min(
            original.remaining_quantity,
            max(
                Decimal("0"),
                original.requested_quantity
                - original.filled_quantity
                - cancelled_quantity,
            ),
        )
        if remaining <= 0:
            status = OrderStatus.CANCELLED
        elif original.filled_quantity > 0:
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.ACKNOWLEDGED
        parsed[original_id] = AccountOrder(
            provider_order_id=original.provider_order_id,
            account_alias=original.account_alias,
            instrument=original.instrument,
            side=original.side,
            order_type=original.order_type,
            status=status,
            requested_quantity=original.requested_quantity,
            filled_quantity=original.filled_quantity,
            remaining_quantity=remaining,
            ordered_at=max(
                original.ordered_at,
                cancellation_time.get(original_id, original.ordered_at),
            ),
            provider=original.provider,
            price=original.price,
            average_fill_price=original.average_fill_price,
            route=original.route,
        )

    return tuple(
        sorted(
            (order for order_id, order in parsed.items() if order_id not in child_ids),
            key=lambda item: item.ordered_at,
            reverse=True,
        )
    )


def _kis_cancelled_quantity(row: dict[str, Any]) -> Decimal:
    return _decimal(
        row.get("cncl_qty")
        or row.get("tot_cncl_qty")
        or row.get("cncl_cfrm_qty"),
        default=Decimal("0"),
    )


def _normalize_namuh_order_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return str(int(text))
    return text


def _require_namuh_integer_price(price: Decimal) -> None:
    if price <= 0 or price != price.to_integral_value():
        raise RouterError(
            RouterErrorCode.INVALID_REQUEST,
            "NAMUH 국내주식 주문 가격은 0보다 큰 원 단위 정수여야 합니다.",
        )


def _parse_namuh_account_order(
    row: dict[str, Any],
    account_alias: str,
    fallback_date: date,
) -> AccountOrder | None:
    provider_order_id = _normalize_namuh_order_id(row.get("itg_orr_no"))
    code = str(row.get("iem_cd") or "").strip()
    if not provider_order_id or not code:
        return None
    requested = _decimal(row.get("orr_qty"), default=Decimal("0"))
    filled = _decimal(row.get("tot_cns_qty"), default=Decimal("0"))
    remaining = _decimal(
        row.get("ny_cns_qty"), default=max(Decimal("0"), requested - filled)
    )
    cancelled_quantity = _decimal(row.get("can_qty"), default=Decimal("0"))
    rejected_reason = str(row.get("orr_rjt_rsn_cd_nm") or "").strip()
    if rejected_reason:
        status = OrderStatus.REJECTED
    elif requested > 0 and filled >= requested:
        status = OrderStatus.FILLED
    elif cancelled_quantity > 0 and filled + cancelled_quantity >= requested:
        status = OrderStatus.CANCELLED
    elif filled > 0:
        status = OrderStatus.PARTIALLY_FILLED
    else:
        status = OrderStatus.ACKNOWLEDGED
    side_name = str(row.get("sby_dit_cd_nm") or "")
    side = OrderSide.SELL if "매도" in side_name else OrderSide.BUY
    order_name = str(row.get("nmn_pr_tp_cd_nm") or "")
    order_type = OrderType.MARKET if "시장" in order_name else OrderType.LIMIT
    route_text = str(row.get("rmt_mkt_cd") or "AUTO").strip().upper()
    try:
        route = OrderRoute(route_text)
    except ValueError:
        route = OrderRoute.AUTO
    return AccountOrder(
        provider_order_id=provider_order_id,
        account_alias=account_alias,
        instrument=InstrumentId(code),
        side=side,
        order_type=order_type,
        status=status,
        requested_quantity=requested,
        filled_quantity=filled,
        remaining_quantity=remaining,
        ordered_at=datetime.combine(fallback_date, time(), tzinfo=SEOUL),
        provider="namuh",
        price=_decimal(row.get("orr_pr"), default=Decimal("0")),
        average_fill_price=_decimal(
            row.get("cns_avg_uit_pr"), default=Decimal("0")
        ),
        route=route,
    )


def _kis_order_time(row: dict[str, Any], fallback_date: date) -> datetime:
    date_text = "".join(
        character
        for character in str(row.get("ord_dt") or row.get("ord_date") or "")
        if character.isdigit()
    )
    time_text = "".join(
        character
        for character in str(row.get("ord_tmd") or row.get("ord_time") or "")
        if character.isdigit()
    )
    try:
        day = datetime.strptime(date_text[:8], "%Y%m%d").date() if len(date_text) >= 8 else fallback_date
    except ValueError:
        day = fallback_date
    try:
        clock = datetime.strptime(time_text[:6], "%H%M%S").time() if len(time_text) >= 6 else time()
    except ValueError:
        clock = time()
    return datetime.combine(day, clock, tzinfo=SEOUL)


def _minute_interval(value: str) -> int:
    clean = value.strip().lower()
    if clean.endswith("m") and clean[:-1].isdigit():
        minutes = int(clean[:-1])
        if 1 <= minutes <= 120:
            return minutes
    raise RouterError(
        RouterErrorCode.INVALID_REQUEST,
        "분봉 주기는 1m부터 120m까지 지정할 수 있습니다.",
    )


def _parse_minute_row(
    row: Any,
) -> tuple[datetime, tuple[Decimal, Decimal, Decimal, Decimal, Decimal]] | None:
    if not isinstance(row, dict):
        return None
    date_text = "".join(
        character for character in str(row.get("stck_bsop_date", "")) if character.isdigit()
    )
    time_text = "".join(
        character for character in str(row.get("stck_cntg_hour", "")) if character.isdigit()
    )
    if len(date_text) != 8 or len(time_text) < 4:
        return None
    time_text = time_text.ljust(6, "0")[:6]
    try:
        stamp = datetime.strptime(date_text + time_text, "%Y%m%d%H%M%S").replace(
            tzinfo=SEOUL
        )
        values = (
            _decimal(row.get("stck_oprc")),
            _decimal(row.get("stck_hgpr")),
            _decimal(row.get("stck_lwpr")),
            _decimal(row.get("stck_prpr")),
            _decimal(row.get("cntg_vol"), default=Decimal("0")),
        )
    except (ValueError, RouterError):
        return None
    if min(values[:4]) <= 0 or values[1] < max(values[0], values[2], values[3]):
        return None
    if values[2] > min(values[0], values[1], values[3]):
        return None
    return stamp, values


def _aggregate_minute_bars(
    instrument: InstrumentId,
    raw: dict[datetime, tuple[Decimal, Decimal, Decimal, Decimal, Decimal]],
    *,
    minutes: int,
    venue: Venue,
    provider: str,
    correlation_id: str,
    source_alias: str | None,
    received_at: datetime,
) -> list[Bar]:
    groups: dict[
        datetime,
        list[tuple[datetime, tuple[Decimal, Decimal, Decimal, Decimal, Decimal]]],
    ] = {}
    for stamp, values in raw.items():
        minute_of_day = stamp.hour * 60 + stamp.minute
        bucket_minute = minute_of_day - (minute_of_day % minutes)
        bucket = stamp.replace(
            hour=bucket_minute // 60,
            minute=bucket_minute % 60,
            second=0,
            microsecond=0,
        )
        groups.setdefault(bucket, []).append((stamp, values))

    result: list[Bar] = []
    for open_time, rows in sorted(groups.items()):
        rows.sort(key=lambda item: item[0])
        first = rows[0][1]
        last = rows[-1][1]
        close_time = open_time + timedelta(minutes=minutes)
        result.append(
            Bar(
                instrument=instrument,
                interval=f"{minutes}m",
                open_time=open_time,
                close_time=close_time,
                open=first[0],
                high=max(values[1] for _, values in rows),
                low=min(values[2] for _, values in rows),
                close=last[3],
                volume=sum((values[4] for _, values in rows), Decimal("0")),
                venue=venue,
                provider=provider,
                correlation_id=correlation_id,
                complete=close_time <= received_at,
                source_alias=source_alias,
                received_at=received_at,
            )
        )
    return result


def _parse_period_row(
    row: Any,
    *,
    instrument: InstrumentId,
    interval: str,
    days: int,
    venue: Venue,
    provider: str,
    correlation_id: str,
    source_alias: str | None,
    received_at: datetime,
) -> Bar | None:
    if not isinstance(row, dict):
        return None
    date_text = "".join(
        character for character in str(row.get("stck_bsop_date", "")) if character.isdigit()
    )
    if len(date_text) != 8:
        return None
    try:
        open_time = datetime.strptime(date_text, "%Y%m%d").replace(tzinfo=SEOUL)
        open_price = _decimal(row.get("stck_oprc"))
        high = _decimal(row.get("stck_hgpr"))
        low = _decimal(row.get("stck_lwpr"))
        close = _decimal(row.get("stck_clpr"))
        volume = _decimal(row.get("acml_vol"), default=Decimal("0"))
        return Bar(
            instrument=instrument,
            interval=interval,
            open_time=open_time,
            close_time=open_time + timedelta(days=days),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            venue=venue,
            provider=provider,
            correlation_id=correlation_id,
            complete=open_time.date() < received_at.date(),
            source_alias=source_alias,
            received_at=received_at,
        )
    except (ValueError, RouterError):
        return None


def _correlation_id(response: httpx.Response) -> str:
    return (
        response.headers.get("x-correlation-id")
        or response.headers.get("x-transaction-id")
        or uuid4().hex
    )
