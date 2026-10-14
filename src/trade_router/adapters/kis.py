from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import websockets

from ..core.time import KST, now_kst
from ..errors import (
    OrderUncertainError,
    ProviderError,
    RouterError,
    UnsupportedCapabilityError,
)
from ..models import (
    Balance,
    Bar,
    BrokerCapabilities,
    BuyingPower,
    Capability,
    Instrument,
    InstrumentId,
    MarketDataVenue,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderPreview,
    OrderRequest,
    OrderRoute,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ProviderEnvironment,
    Quote,
    StreamEvent,
    StreamKind,
    StreamSubscription,
    TimeInForce,
    Trade,
)
from .base import ProviderAdapterV1
from .utils import decimal_of, first, int_of, redact_text, source_metadata

_REST = {
    ProviderEnvironment.LIVE: "https://openapi.koreainvestment.com:9443",
    ProviderEnvironment.PAPER: "https://openapivts.koreainvestment.com:29443",
}
_WS = {
    ProviderEnvironment.LIVE: "ws://ops.koreainvestment.com:21000",
    ProviderEnvironment.PAPER: "ws://ops.koreainvestment.com:31000",
}
_QUOTE_MARKET = {
    MarketDataVenue.KRX: "J",
    MarketDataVenue.NXT: "NX",
    MarketDataVenue.INTEGRATED: "UN",
}
_ORDER_ROUTE = {
    OrderRoute.KRX: "KRX",
    OrderRoute.NXT: "NXT",
    OrderRoute.SOR: "SOR",
    OrderRoute.AUTO: "SOR",
}


def _guard_kis_url(value: str, *, websocket: bool = False) -> str:
    parsed = urlparse(value)
    allowed_hosts = {"openapi.koreainvestment.com", "openapivts.koreainvestment.com"}
    if websocket:
        allowed_hosts = {"ops.koreainvestment.com"}
    allowed_schemes = {"ws", "wss"} if websocket else {"https"}
    if (
        parsed.scheme not in allowed_schemes
        or (parsed.hostname or "").lower() not in allowed_hosts
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RouterError(
            "PROVIDER_HOST_INVALID",
            "KIS credentials may only be sent to official KIS hosts.",
        )
    return value.rstrip("/")


def _account_parts(account_number: str) -> tuple[str, str]:
    compact = account_number.replace("-", "").replace(" ", "")
    if len(compact) < 10 or not compact.isdigit():
        raise RouterError(
            "ACCOUNT_NUMBER_INVALID",
            "KIS account number must include the product code.",
        )
    return compact[:-2], compact[-2:]


def _time_today(value: str | None) -> datetime:
    raw = (value or "").replace(":", "").ljust(6, "0")[:6]
    try:
        parsed = datetime.strptime(raw, "%H%M%S").time()
    except ValueError:
        return now_kst()
    return datetime.combine(now_kst().date(), parsed, tzinfo=KST)


class KisAdapter(ProviderAdapterV1):
    provider_id = "kis"
    adapter_version = "0.1.0"

    def __init__(self, context) -> None:
        super().__init__(context)
        self.base_url = _guard_kis_url(
            str(context.settings.get("rest_url") or _REST[context.profile.environment])
        )
        self.ws_url = _guard_kis_url(
            str(context.settings.get("ws_url") or _WS[context.profile.environment]),
            websocket=True,
        )
        self.http = httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(10, connect=5)
        )
        self._token_lock = asyncio.Lock()
        self._approval_lock = asyncio.Lock()

    @classmethod
    def capabilities(cls) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider=cls.provider_id,
            adapter_version=cls.adapter_version,
            environments={ProviderEnvironment.LIVE, ProviderEnvironment.PAPER},
            features={
                Capability.INSTRUMENT_SEARCH,
                Capability.QUOTE,
                Capability.ORDERBOOK,
                Capability.BARS_MINUTE,
                Capability.BARS_DAY,
                Capability.BARS_WEEK,
                Capability.STREAM_QUOTE,
                Capability.STREAM_ORDERBOOK,
                Capability.STREAM_TRADES,
                Capability.ACCOUNT_BALANCE,
                Capability.ACCOUNT_POSITIONS,
                Capability.ACCOUNT_ORDERS,
                Capability.BUYING_POWER,
                Capability.SELLABLE_QUANTITY,
                Capability.ORDER_MARKET,
                Capability.ORDER_LIMIT,
                Capability.ORDER_AMEND,
                Capability.ORDER_CANCEL,
            },
            market_data_venues=set(MarketDataVenue),
            order_routes=set(OrderRoute),
            bar_intervals={"1m", "3m", "5m", "10m", "15m", "30m", "1d", "1w"},
            provider_option_schema={
                "condition_code": {"type": "string"},
                "sell_type": {"type": "string", "default": "01"},
            },
        )

    async def close(self) -> None:
        await self.http.aclose()

    def _credentials(self) -> tuple[str, str]:
        key = str(self.context.secrets.get("app_key", ""))
        secret = str(self.context.secrets.get("app_secret", ""))
        if not key or not secret:
            raise RouterError(
                "PROFILE_SECRET_MISSING", "KIS App Key and App Secret are required."
            )
        return key, secret

    async def _limit(self, group: str, *, priority: int) -> None:
        if self.context.limiters is None:
            return
        calls = int(self.context.settings.get("rate_limit_per_second", 18))
        await self.context.limiters.get(
            self.context.profile.id, group, calls=calls
        ).acquire(priority=priority)

    async def _token(self, *, force: bool = False) -> str:
        async with self._token_lock:
            cached = self.context.secrets.get("access_token")
            expires = self.context.secrets.get("token_expires_at")
            if not force and cached and expires:
                try:
                    if datetime.fromisoformat(str(expires)) - now_kst() > timedelta(
                        minutes=2
                    ):
                        return str(cached)
                except ValueError:
                    pass
            key, secret = self._credentials()
            await self._limit("auth", priority=0)
            response = await self.http.post(
                "/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": key,
                    "appsecret": secret,
                },
            )
            if response.status_code >= 400:
                raise self._http_error(
                    response, "KIS token issuance failed", key, secret
                )
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise ProviderError(
                    "PROVIDER_AUTH_FAILED",
                    "KIS did not return an access token.",
                    provider="kis",
                )
            lifetime = int(payload.get("expires_in", 86400))
            self.context.secrets["access_token"] = token
            self.context.secrets["token_expires_at"] = (
                now_kst() + timedelta(seconds=lifetime)
            ).isoformat()
            self.context.save_secrets()
            return str(token)

    async def _approval_key(self, *, force: bool = False) -> str:
        async with self._approval_lock:
            cached = self.context.secrets.get("ws_approval_key")
            created = self.context.secrets.get("ws_approval_created_at")
            if not force and cached and created:
                try:
                    if now_kst() - datetime.fromisoformat(str(created)) < timedelta(
                        hours=20
                    ):
                        return str(cached)
                except ValueError:
                    pass
            key, secret = self._credentials()
            await self._limit("auth", priority=0)
            response = await self.http.post(
                "/oauth2/Approval",
                json={
                    "grant_type": "client_credentials",
                    "appkey": key,
                    "secretkey": secret,
                },
            )
            if response.status_code >= 400:
                raise self._http_error(
                    response, "KIS WebSocket approval failed", key, secret
                )
            approval = response.json().get("approval_key")
            if not approval:
                raise ProviderError(
                    "PROVIDER_AUTH_FAILED",
                    "KIS did not return a WebSocket approval key.",
                    provider="kis",
                )
            self.context.secrets["ws_approval_key"] = approval
            self.context.secrets["ws_approval_created_at"] = now_kst().isoformat()
            self.context.save_secrets()
            return str(approval)

    def _http_error(
        self, response: httpx.Response, message: str, *sensitive: Any
    ) -> ProviderError:
        try:
            payload = response.json()
        except Exception:
            payload = {}
        return ProviderError(
            "PROVIDER_HTTP_ERROR",
            redact_text(
                payload.get("msg1") or payload.get("message") or message, *sensitive
            ),
            retryable=response.status_code in {408, 429, 500, 502, 503, 504},
            status_code=502,
            provider=self.provider_id,
            provider_code=str(payload.get("msg_cd") or response.status_code),
            correlation_id=response.headers.get("x-correlation-id"),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        tr_id: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        group: str = "read",
        order: bool = False,
    ) -> dict[str, Any]:
        key, secret = self._credentials()
        attempts = 1 if order else 3
        for attempt in range(attempts):
            token = await self._token(force=False)
            await self._limit(group, priority=0 if order else 20)
            headers = {
                "authorization": f"Bearer {token}",
                "appkey": key,
                "appsecret": secret,
                "tr_id": tr_id,
                "custtype": "P",
                "content-type": "application/json; charset=utf-8",
            }
            try:
                response = await self.http.request(
                    method, path, params=params, json=body, headers=headers
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if order:
                    raise OrderUncertainError(
                        self.provider_id,
                        "KIS order response was not received; reconciliation is required.",
                        correlation_id=uuid4().hex,
                    ) from exc
                if attempt + 1 == attempts:
                    raise ProviderError(
                        "PROVIDER_UNAVAILABLE",
                        "KIS request failed.",
                        retryable=True,
                        status_code=502,
                        provider="kis",
                    ) from exc
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            if response.status_code in {401, 403} and attempt == 0 and not order:
                await self._token(force=True)
                continue
            if response.status_code >= 400:
                if order and response.status_code >= 500:
                    raise OrderUncertainError(
                        self.provider_id,
                        "KIS returned a server error after order submission; reconciliation is required.",
                        correlation_id=response.headers.get("x-correlation-id")
                        or uuid4().hex,
                    )
                raise self._http_error(
                    response,
                    "KIS request failed",
                    key,
                    secret,
                    params or {},
                    body or {},
                )
            payload = response.json()
            if str(payload.get("rt_cd", "0")) != "0":
                raise ProviderError(
                    "PROVIDER_REJECTED",
                    redact_text(
                        payload.get("msg1") or "KIS rejected the request.",
                        key,
                        secret,
                        params or {},
                        body or {},
                    ),
                    retryable=False,
                    status_code=502,
                    provider=self.provider_id,
                    provider_code=str(payload.get("msg_cd") or ""),
                    correlation_id=response.headers.get("x-correlation-id"),
                )
            return payload
        raise AssertionError("unreachable")

    async def health_check(self) -> dict[str, Any]:
        await self._token()
        return {
            "ok": True,
            "provider": self.provider_id,
            "environment": self.context.profile.environment.value,
        }

    async def search_instruments(
        self, query: str, *, limit: int = 20
    ) -> list[Instrument]:
        query = query.strip()
        if not query.isdigit():
            raise RouterError(
                "INSTRUMENT_SEARCH_LIMITED",
                "KIS direct search requires a product code; use a local instrument master for name search.",
            )
        payload = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/search-stock-info",
            tr_id="CTPF1002R",
            params={"PRDT_TYPE_CD": "300", "PDNO": query},
        )
        row = payload.get("output") or {}
        if not row:
            return []
        return [
            Instrument(
                instrument=InstrumentId(
                    code=str(first(row, "pdno", "shtn_pdno", default=query))
                ),
                name=first(row, "prdt_name", "prdt_abrv_name"),
                market=first(row, "mket_id_cd", "prdt_clsf_name"),
                nxt_eligible=None,
                etf=str(first(row, "prdt_type_cd", default="")) in {"ETF", "302"},
            )
        ][:limit]

    async def _quote_row(
        self, code: str, venue: MarketDataVenue
    ) -> tuple[dict[str, Any], str | None]:
        payload = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": _QUOTE_MARKET[venue],
                "FID_INPUT_ISCD": code,
            },
        )
        return payload.get("output") or {}, payload.get("msg_cd")

    async def get_quote(self, code: str, venue: MarketDataVenue) -> Quote:
        row, correlation = await self._quote_row(code, venue)
        last = decimal_of(first(row, "stck_prpr", "bstp_nmix_prpr"))
        if last is None:
            raise ProviderError(
                "PROVIDER_DATA_INVALID",
                "KIS quote did not contain a price.",
                provider="kis",
            )
        return Quote(
            instrument=InstrumentId(code=code),
            venue=venue,
            last=last,
            open=decimal_of(first(row, "stck_oprc", "bstp_nmix_oprc")),
            high=decimal_of(first(row, "stck_hgpr", "bstp_nmix_hgpr")),
            low=decimal_of(first(row, "stck_lwpr", "bstp_nmix_lwpr")),
            previous_close=decimal_of(first(row, "stck_prdy_clpr")),
            change=decimal_of(first(row, "prdy_vrss", "bstp_nmix_prdy_vrss")),
            change_rate=decimal_of(first(row, "prdy_ctrt", "bstp_nmix_prdy_ctrt")),
            bid=decimal_of(first(row, "bidp")),
            ask=decimal_of(first(row, "askp")),
            volume=int_of(first(row, "acml_vol")),
            trading_status=str(
                first(row, "iscd_stat_cls_code", "mrkt_warn_cls_code", default="")
            )
            or None,
            source=source_metadata(
                provider="kis",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
                correlation_id=correlation,
            ),
        )

    async def get_orderbook(self, code: str, venue: MarketDataVenue) -> OrderBook:
        payload = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            tr_id="FHKST01010200",
            params={
                "FID_COND_MRKT_DIV_CODE": _QUOTE_MARKET[venue],
                "FID_INPUT_ISCD": code,
            },
        )
        row = payload.get("output1") or payload.get("output") or {}
        asks = [
            OrderBookLevel(
                price=price, quantity=int_of(row.get(f"askp_rsqn{i}"), 0) or 0
            )
            for i in range(1, 11)
            if (price := decimal_of(row.get(f"askp{i}"))) is not None
        ]
        bids = [
            OrderBookLevel(
                price=price, quantity=int_of(row.get(f"bidp_rsqn{i}"), 0) or 0
            )
            for i in range(1, 11)
            if (price := decimal_of(row.get(f"bidp{i}"))) is not None
        ]
        return OrderBook(
            instrument=InstrumentId(code=code),
            venue=venue,
            bids=bids,
            asks=asks,
            source=source_metadata(
                provider="kis",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
            ),
        )

    async def get_bars(
        self,
        code: str,
        *,
        interval: str,
        count: int,
        venue: MarketDataVenue,
        until: date | None = None,
    ) -> list[Bar]:
        count = min(max(count, 1), 1000)
        if interval.endswith("m"):
            minutes = int(interval[:-1])
            raw_count = min(count * minutes, 30)
            payload = await self._request(
                "GET",
                "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                tr_id="FHKST03010200",
                params={
                    "FID_ETC_CLS_CODE": "",
                    "FID_COND_MRKT_DIV_CODE": _QUOTE_MARKET[venue],
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_HOUR_1": now_kst().strftime("%H%M%S"),
                    "FID_PW_DATA_INCU_YN": "Y",
                },
            )
            rows = payload.get("output2") or []
            one_minute: list[Bar] = []
            trading_day = until or now_kst().date()
            for row in reversed(rows[:raw_count]):
                started = _time_today(
                    str(first(row, "stck_cntg_hour", default=""))
                ).replace(
                    year=trading_day.year, month=trading_day.month, day=trading_day.day
                )
                one_minute.append(
                    Bar(
                        instrument=InstrumentId(code=code),
                        venue=venue,
                        interval="1m",
                        started_at=started,
                        ended_at=started + timedelta(minutes=1),
                        open=decimal_of(row.get("stck_oprc"), Decimal(0)) or Decimal(0),
                        high=decimal_of(row.get("stck_hgpr"), Decimal(0)) or Decimal(0),
                        low=decimal_of(row.get("stck_lwpr"), Decimal(0)) or Decimal(0),
                        close=decimal_of(row.get("stck_prpr"), Decimal(0))
                        or Decimal(0),
                        volume=int_of(first(row, "cntg_vol", "acml_vol"), 0) or 0,
                        complete=True,
                        source=source_metadata(
                            provider="kis",
                            profile=self.context.profile.id,
                            generation=self.context.connection_generation,
                            timestamp=started + timedelta(minutes=1),
                        ),
                    )
                )
            return self._aggregate_minutes(one_minute, minutes, interval)[-count:]
        period = "W" if interval == "1w" else "D"
        end = until or now_kst().date()
        payload = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id="FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": _QUOTE_MARKET[venue],
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": (
                    end - timedelta(days=max(count * (8 if period == "W" else 2), 120))
                ).strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        bars: list[Bar] = []
        for row in reversed((payload.get("output2") or [])[:count]):
            raw_date = str(first(row, "stck_bsop_date", default=end.strftime("%Y%m%d")))
            started = datetime.strptime(raw_date, "%Y%m%d").replace(tzinfo=KST)
            step = timedelta(days=7 if interval == "1w" else 1)
            bars.append(
                Bar(
                    instrument=InstrumentId(code=code),
                    venue=venue,
                    interval=interval,
                    started_at=started,
                    ended_at=started + step,
                    open=decimal_of(row.get("stck_oprc"), Decimal(0)) or Decimal(0),
                    high=decimal_of(row.get("stck_hgpr"), Decimal(0)) or Decimal(0),
                    low=decimal_of(row.get("stck_lwpr"), Decimal(0)) or Decimal(0),
                    close=decimal_of(row.get("stck_clpr"), Decimal(0)) or Decimal(0),
                    volume=int_of(row.get("acml_vol"), 0) or 0,
                    complete=True,
                    source=source_metadata(
                        provider="kis",
                        profile=self.context.profile.id,
                        generation=self.context.connection_generation,
                        timestamp=started + step,
                    ),
                )
            )
        return bars[-count:]

    @staticmethod
    def _aggregate_minutes(rows: list[Bar], minutes: int, interval: str) -> list[Bar]:
        if minutes == 1:
            return rows
        result: list[Bar] = []
        for offset in range(0, len(rows), minutes):
            group = rows[offset : offset + minutes]
            if len(group) < minutes:
                continue
            result.append(
                group[0].model_copy(
                    update={
                        "interval": interval,
                        "ended_at": group[-1].ended_at,
                        "high": max(item.high for item in group),
                        "low": min(item.low for item in group),
                        "close": group[-1].close,
                        "volume": sum(item.volume for item in group),
                    }
                )
            )
        return result

    async def _balance_payload(self, account_number: str) -> dict[str, Any]:
        cano, product = _account_parts(account_number)
        tr_id = (
            "TTTC8434R"
            if self.context.profile.environment == ProviderEnvironment.LIVE
            else "VTTC8434R"
        )
        return await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": product,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            group="account",
        )

    async def get_balance(self, account_alias: str, account_number: str) -> Balance:
        payload = await self._balance_payload(account_number)
        row = (payload.get("output2") or [{}])[0]
        return Balance(
            account_alias=account_alias,
            cash=decimal_of(first(row, "dnca_tot_amt", "prvs_rcdl_excc_amt")),
            available_cash=decimal_of(first(row, "prvs_rcdl_excc_amt", "dnca_tot_amt")),
            buying_power=decimal_of(first(row, "nass_amt", "dnca_tot_amt")),
            total_equity=decimal_of(first(row, "tot_evlu_amt")),
            market_value=decimal_of(first(row, "scts_evlu_amt")),
            unrealized_pnl=decimal_of(first(row, "evlu_pfls_smtl_amt")),
            source=source_metadata(
                provider="kis",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
            ),
        )

    async def get_positions(
        self, account_alias: str, account_number: str
    ) -> list[Position]:
        payload = await self._balance_payload(account_number)
        result: list[Position] = []
        for row in payload.get("output1") or []:
            quantity = int_of(row.get("hldg_qty"), 0) or 0
            if quantity <= 0:
                continue
            result.append(
                Position(
                    account_alias=account_alias,
                    instrument=InstrumentId(code=str(row.get("pdno"))),
                    name=row.get("prdt_name"),
                    quantity=quantity,
                    available_quantity=int_of(first(row, "ord_psbl_qty", "hldg_qty")),
                    average_price=decimal_of(row.get("pchs_avg_pric")),
                    market_price=decimal_of(row.get("prpr")),
                    market_value=decimal_of(row.get("evlu_amt")),
                    unrealized_pnl=decimal_of(row.get("evlu_pfls_amt")),
                    unrealized_pnl_rate=decimal_of(row.get("evlu_pfls_rt")),
                    source=source_metadata(
                        provider="kis",
                        profile=self.context.profile.id,
                        generation=self.context.connection_generation,
                    ),
                )
            )
        return result

    async def get_buying_power(
        self, account_alias: str, account_number: str, request: OrderRequest
    ) -> BuyingPower:
        cano, product = _account_parts(account_number)
        tr_id = (
            "TTTC8908R"
            if self.context.profile.environment == ProviderEnvironment.LIVE
            else "VTTC8908R"
        )
        payload = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=tr_id,
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": product,
                "PDNO": request.instrument.code,
                "ORD_UNPR": str(request.price or 0),
                "ORD_DVSN": self._order_division(request),
                "CMA_EVLU_AMT_ICLD_YN": "Y",
                "OVRS_ICLD_YN": "N",
            },
            group="account",
        )
        row = payload.get("output") or {}
        return BuyingPower(
            account_alias=account_alias,
            instrument=request.instrument,
            order_type=request.order_type,
            price=request.price,
            max_quantity=int_of(first(row, "max_buy_qty", "nrcvb_buy_qty"), 0) or 0,
            available_amount=decimal_of(first(row, "ord_psbl_cash", "nrcvb_buy_amt")),
            calculation_price=decimal_of(first(row, "psbl_qty_calc_unpr", "ord_unpr")),
            source=source_metadata(
                provider="kis",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
            ),
        )

    async def preview_order(
        self, account_number: str, request: OrderRequest
    ) -> OrderPreview:
        if request.time_in_force == TimeInForce.GTP:
            return OrderPreview(
                request=request,
                allowed=False,
                block_code="UNSUPPORTED_TIME_IN_FORCE",
                block_reason="KIS cash orders do not support GTP here.",
            )
        if request.side == OrderSide.SELL:
            positions = await self.get_positions(request.account_alias, account_number)
            position = next(
                (
                    item
                    for item in positions
                    if item.instrument.code == request.instrument.code
                ),
                None,
            )
            available = position.available_quantity if position else 0
            price = (
                request.price
                or (
                    await self.get_quote(
                        request.instrument.code, MarketDataVenue.INTEGRATED
                    )
                ).last
            )
            return OrderPreview(
                request=request,
                allowed=(available or 0) >= request.quantity,
                estimated_price=price,
                estimated_notional=price * request.quantity,
                max_notional=price * request.quantity,
                available_quantity=available or 0,
                block_code=None
                if (available or 0) >= request.quantity
                else "INSUFFICIENT_POSITION",
                block_reason=None
                if (available or 0) >= request.quantity
                else "Sell quantity exceeds available position.",
            )
        buying = await self.get_buying_power(
            request.account_alias, account_number, request
        )
        quote_row, _ = await self._quote_row(
            request.instrument.code, MarketDataVenue.INTEGRATED
        )
        upper = decimal_of(quote_row.get("stck_mxpr"))
        conservative = request.price or max(
            (value for value in (upper, buying.calculation_price) if value is not None),
            default=None,
        )
        allowed = buying.max_quantity >= request.quantity and conservative is not None
        return OrderPreview(
            request=request,
            allowed=allowed,
            estimated_price=request.price or buying.calculation_price,
            estimated_notional=(request.price or buying.calculation_price)
            * request.quantity
            if (request.price or buying.calculation_price)
            else None,
            max_notional=conservative * request.quantity if conservative else None,
            available_quantity=buying.max_quantity,
            available_amount=buying.available_amount,
            block_code=None if allowed else "INSUFFICIENT_BUYING_POWER",
            block_reason=None
            if allowed
            else "KIS buying power or conservative market price is insufficient.",
        )

    @staticmethod
    def _order_division(request: OrderRequest) -> str:
        if request.time_in_force == TimeInForce.IOC:
            return "13" if request.order_type == OrderType.MARKET else "10"
        if request.time_in_force == TimeInForce.FOK:
            return "23" if request.order_type == OrderType.MARKET else "20"
        return "01" if request.order_type == OrderType.MARKET else "00"

    async def submit_order(
        self, account_number: str, request: OrderRequest, *, router_order_id: str
    ) -> Order:
        cano, product = _account_parts(account_number)
        live = self.context.profile.environment == ProviderEnvironment.LIVE
        tr_id = (
            ("TTTC0012U" if request.side == OrderSide.BUY else "TTTC0011U")
            if live
            else ("VTTC0012U" if request.side == OrderSide.BUY else "VTTC0011U")
        )
        payload = await self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            group="order",
            order=True,
            body={
                "CANO": cano,
                "ACNT_PRDT_CD": product,
                "PDNO": request.instrument.code,
                "ORD_DVSN": self._order_division(request),
                "ORD_QTY": str(request.quantity),
                "ORD_UNPR": str(request.price or 0),
                "EXCG_ID_DVSN_CD": _ORDER_ROUTE[request.route],
                "SLL_TYPE": str(request.provider_options.get("sell_type", "01"))
                if request.side == OrderSide.SELL
                else "",
                "CNDT_PRIC": "",
            },
        )
        row = payload.get("output") or {}
        now = now_kst()
        return Order(
            router_order_id=router_order_id,
            client_order_id=request.client_order_id,
            strategy_id=request.strategy_id,
            account_alias=request.account_alias,
            provider="kis",
            provider_order_id=str(first(row, "ODNO", "odno", default="")) or None,
            instrument=request.instrument,
            side=request.side,
            order_type=request.order_type,
            route=request.route,
            time_in_force=request.time_in_force,
            quantity=request.quantity,
            price=request.price,
            status=OrderStatus.ACKNOWLEDGED,
            submitted_at=now,
            updated_at=now,
            message=str(payload.get("msg1") or "KIS acknowledged the order."),
            correlation_id=uuid4().hex,
        )

    async def cancel_order(self, account_number: str, order: Order) -> Order:
        return await self._revise_cancel(
            account_number, order, cancel=True, quantity=None, price=None
        )

    async def amend_order(
        self,
        account_number: str,
        order: Order,
        *,
        quantity: int | None,
        price: str | None,
    ) -> Order:
        return await self._revise_cancel(
            account_number, order, cancel=False, quantity=quantity, price=price
        )

    async def _revise_cancel(
        self,
        account_number: str,
        order: Order,
        *,
        cancel: bool,
        quantity: int | None,
        price: str | None,
    ) -> Order:
        if not order.provider_order_id:
            raise RouterError(
                "PROVIDER_ORDER_ID_MISSING", "KIS order number is required."
            )
        cano, product = _account_parts(account_number)
        tr_id = (
            "TTTC0013U"
            if self.context.profile.environment == ProviderEnvironment.LIVE
            else "VTTC0013U"
        )
        await self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-rvsecncl",
            tr_id=tr_id,
            group="order",
            order=True,
            body={
                "CANO": cano,
                "ACNT_PRDT_CD": product,
                "KRX_FWDG_ORD_ORGNO": "",
                "ORGN_ODNO": order.provider_order_id,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02" if cancel else "01",
                "ORD_QTY": str(
                    quantity or max(order.quantity - order.filled_quantity, 0)
                ),
                "ORD_UNPR": str(price or order.price or 0),
                "QTY_ALL_ORD_YN": "Y" if cancel else "N",
                "EXCG_ID_DVSN_CD": _ORDER_ROUTE[order.route],
            },
        )
        return order.model_copy(
            update={
                "status": OrderStatus.CANCEL_PENDING if cancel else order.status,
                "quantity": quantity or order.quantity,
                "price": Decimal(price) if price else order.price,
                "updated_at": now_kst(),
            }
        )

    async def get_orders(
        self, account_alias: str, account_number: str, *, open_only: bool = False
    ) -> list[Order]:
        cano, product = _account_parts(account_number)
        today = now_kst().strftime("%Y%m%d")
        tr_id = (
            "TTTC0081R"
            if self.context.profile.environment == ProviderEnvironment.LIVE
            else "VTTC0081R"
        )
        payload = await self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id=tr_id,
            group="account",
            params={
                "CANO": cano,
                "ACNT_PRDT_CD": product,
                "INQR_STRT_DT": today,
                "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",
                "INQR_DVSN": "00",
                "PDNO": "",
                "CCLD_DVSN": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "EXCG_ID_DVSN_CD": "ALL",
            },
        )
        result: list[Order] = []
        for row in payload.get("output1") or []:
            provider_id = str(first(row, "odno", "ODNO", default=""))
            if not provider_id:
                continue
            quantity = int_of(row.get("ord_qty"), 0) or 0
            filled = int_of(first(row, "tot_ccld_qty", "ccld_qty"), 0) or 0
            cancelled = str(first(row, "cncl_yn", default="N")).upper() == "Y"
            rejected = (int_of(row.get("rjct_qty"), 0) or 0) > 0 and filled == 0
            if rejected:
                status = OrderStatus.REJECTED
            elif cancelled:
                status = OrderStatus.CANCELLED
            elif filled >= quantity and quantity > 0:
                status = OrderStatus.FILLED
            elif filled > 0:
                status = OrderStatus.PARTIALLY_FILLED
            else:
                status = OrderStatus.ACKNOWLEDGED
            if open_only and status not in {
                OrderStatus.ACKNOWLEDGED,
                OrderStatus.PARTIALLY_FILLED,
            }:
                continue
            side = (
                OrderSide.SELL
                if str(row.get("sll_buy_dvsn_cd")) == "01"
                or "매도" in str(row.get("sll_buy_dvsn_cd_name", ""))
                else OrderSide.BUY
            )
            division_name = str(row.get("ord_dvsn_name") or "")
            order_type = (
                OrderType.MARKET if "시장" in division_name else OrderType.LIMIT
            )
            route_raw = str(first(row, "excg_id_dvsn_cd", default="KRX")).upper()
            route = (
                OrderRoute(route_raw)
                if route_raw in {item.value for item in OrderRoute}
                else OrderRoute.KRX
            )
            raw_dt = (
                str(first(row, "ord_dt", default=today))
                + str(first(row, "ord_tmd", default="000000"))
                .replace(":", "")
                .ljust(6, "0")[:6]
            )
            try:
                submitted = datetime.strptime(raw_dt, "%Y%m%d%H%M%S").replace(
                    tzinfo=KST
                )
            except ValueError:
                submitted = now_kst()
            result.append(
                Order(
                    router_order_id=f"provider:kis:{today}:{provider_id}",
                    client_order_id=f"provider:{provider_id}",
                    strategy_id="__provider__",
                    account_alias=account_alias,
                    provider="kis",
                    provider_order_id=provider_id,
                    instrument=InstrumentId(code=str(row.get("pdno"))),
                    side=side,
                    order_type=order_type,
                    route=route,
                    time_in_force=TimeInForce.DAY,
                    quantity=quantity,
                    price=decimal_of(row.get("ord_unpr")),
                    filled_quantity=filled,
                    average_fill_price=decimal_of(first(row, "avg_prvs", "avg_pric")),
                    status=status,
                    submitted_at=submitted,
                    updated_at=now_kst(),
                    message=str(row.get("rjct_rson") or "") or None,
                    correlation_id=uuid4().hex,
                )
            )
        return result

    async def stream(
        self, subscriptions: list[StreamSubscription]
    ) -> AsyncIterator[StreamEvent]:
        supported = {StreamKind.QUOTE, StreamKind.ORDERBOOK, StreamKind.TRADE}
        if any(item.kind not in supported or not item.code for item in subscriptions):
            raise UnsupportedCapabilityError(self.provider_id, "requested stream")
        approval = await self._approval_key()
        async with websockets.connect(
            self.ws_url, ping_interval=20, ping_timeout=20
        ) as socket:
            sent: set[tuple[str, str]] = set()
            for item in subscriptions:
                tr_id = self._stream_tr_id(item)
                registration = (tr_id, str(item.code))
                if registration in sent:
                    continue
                sent.add(registration)
                await socket.send(
                    json.dumps(
                        {
                            "header": {
                                "approval_key": approval,
                                "custtype": "P",
                                "tr_type": "1",
                                "content-type": "utf-8",
                            },
                            "body": {"input": {"tr_id": tr_id, "tr_key": item.code}},
                        }
                    )
                )
                await asyncio.sleep(0.05)
            sequence = 0
            async for raw in socket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if raw.startswith("{"):
                    control = json.loads(raw)
                    if (
                        str(control.get("header", {}).get("tr_id", "")).upper()
                        == "PINGPONG"
                    ):
                        await socket.send(raw)
                    continue
                parts = raw.split("|", 3)
                if len(parts) != 4:
                    continue
                tr_id, payload = parts[1], parts[3]
                values = payload.split("^")
                event = self._parse_stream(tr_id, values)
                if event is None:
                    continue
                if event.kind == StreamKind.ORDERBOOK:
                    book = OrderBook.model_validate(event.payload)
                    code = book.instrument.code
                    venue = book.venue
                    if any(
                        item.kind == StreamKind.ORDERBOOK
                        and item.code == code
                        and item.venue == venue
                        for item in subscriptions
                    ):
                        sequence += 1
                        yield event.model_copy(update={"sequence": sequence})
                    continue
                quote = Quote.model_validate(event.payload)
                code = quote.instrument.code
                venue = quote.venue
                if any(
                    item.kind == StreamKind.QUOTE
                    and item.code == code
                    and item.venue == venue
                    for item in subscriptions
                ):
                    sequence += 1
                    yield event.model_copy(update={"sequence": sequence})
                if any(
                    item.kind == StreamKind.TRADE
                    and item.code == code
                    and item.venue == venue
                    for item in subscriptions
                ):
                    sequence += 1
                    trade = Trade(
                        instrument=quote.instrument,
                        venue=quote.venue,
                        price=quote.last,
                        quantity=int_of(values[12], 0) or 0,
                        source=quote.source,
                    )
                    yield StreamEvent(
                        kind=StreamKind.TRADE,
                        subscription_key=f"TRADE:{venue.value}:{code}",
                        sequence=sequence,
                        event_id=uuid4().hex,
                        occurred_at=now_kst(),
                        payload=trade.model_dump(mode="json"),
                    )

    @staticmethod
    def _stream_tr_id(item: StreamSubscription) -> str:
        if item.kind == StreamKind.ORDERBOOK:
            return {
                MarketDataVenue.KRX: "H0STASP0",
                MarketDataVenue.NXT: "H0NXASP0",
                MarketDataVenue.INTEGRATED: "H0UNASP0",
            }[item.venue]
        return {
            MarketDataVenue.KRX: "H0STCNT0",
            MarketDataVenue.NXT: "H0NXCNT0",
            MarketDataVenue.INTEGRATED: "H0UNCNT0",
        }[item.venue]

    def _parse_stream(self, tr_id: str, values: list[str]) -> StreamEvent | None:
        venue = (
            MarketDataVenue.KRX
            if tr_id.startswith("H0ST")
            else MarketDataVenue.NXT
            if tr_id.startswith("H0NX")
            else MarketDataVenue.INTEGRATED
        )
        code = values[0] if values else ""
        source = source_metadata(
            provider="kis",
            profile=self.context.profile.id,
            generation=self.context.connection_generation,
            timestamp=_time_today(values[1] if len(values) > 1 else None),
        )
        if "ASP" in tr_id and len(values) >= 43:
            book = OrderBook(
                instrument=InstrumentId(code=code),
                venue=venue,
                asks=[
                    OrderBookLevel(
                        price=Decimal(values[2 + i]),
                        quantity=int_of(values[22 + i], 0) or 0,
                    )
                    for i in range(1, 11)
                    if decimal_of(values[2 + i]) is not None
                ],
                bids=[
                    OrderBookLevel(
                        price=Decimal(values[12 + i]),
                        quantity=int_of(values[32 + i], 0) or 0,
                    )
                    for i in range(1, 11)
                    if decimal_of(values[12 + i]) is not None
                ],
                source=source,
            )
            kind = StreamKind.ORDERBOOK
            payload = book.model_dump(mode="json")
        elif len(values) >= 14:
            quote = Quote(
                instrument=InstrumentId(code=code),
                venue=venue,
                last=decimal_of(values[2], Decimal(0)) or Decimal(0),
                change=decimal_of(values[4]),
                change_rate=decimal_of(values[5]),
                open=decimal_of(values[7]),
                high=decimal_of(values[8]),
                low=decimal_of(values[9]),
                ask=decimal_of(values[10]),
                bid=decimal_of(values[11]),
                volume=int_of(values[13]),
                source=source,
            )
            kind = StreamKind.QUOTE
            payload = quote.model_dump(mode="json")
        else:
            return None
        return StreamEvent(
            kind=kind,
            subscription_key=f"{kind.value}:{venue.value}:{code}",
            sequence=0,
            event_id=uuid4().hex,
            occurred_at=now_kst(),
            payload=payload,
        )
