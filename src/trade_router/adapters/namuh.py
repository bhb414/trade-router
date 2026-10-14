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
    CredentialScope,
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

_BASE = {
    ProviderEnvironment.LIVE: "https://api.nhplug.com:8443",
    ProviderEnvironment.PAPER: "https://moapi.nhplug.com:8443",
}
_AUTH = "https://api.nhplug.com:8443"
_WS = {
    ProviderEnvironment.LIVE: "wss://api.nhplug.com:7070/websocket",
    ProviderEnvironment.PAPER: "wss://moapi.nhplug.com:17070/websocket",
}
_MARKET = {
    MarketDataVenue.KRX: "KRX",
    MarketDataVenue.NXT: "NXT",
    MarketDataVenue.INTEGRATED: "UNT",
}
_ROUTE = {
    OrderRoute.KRX: "KRX",
    OrderRoute.NXT: "NXT",
    OrderRoute.SOR: "SOR",
    OrderRoute.AUTO: "SOR",
}
_ALLOWED_HOST_SUFFIXES = (".nhplug.com", ".n2plug.com")


def _guard_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"https", "wss"}
        or not any(host.endswith(suffix) for suffix in _ALLOWED_HOST_SUFFIXES)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RouterError(
            "PROVIDER_HOST_INVALID",
            "NAMUH credentials may only be sent to official NH PLUG hosts.",
        )
    return value.rstrip("/")


def _today_time(value: Any) -> datetime:
    raw = str(value or "").replace(":", "").ljust(6, "0")[:6]
    try:
        return datetime.combine(
            now_kst().date(), datetime.strptime(raw, "%H%M%S").time(), tzinfo=KST
        )
    except ValueError:
        return now_kst()


class NamuhAdapter(ProviderAdapterV1):
    provider_id = "namuh"
    adapter_version = "0.1.0"

    def __init__(self, context) -> None:
        super().__init__(context)
        self.base_url = _guard_url(
            str(context.settings.get("rest_url") or _BASE[context.profile.environment])
        )
        self.auth_url = _guard_url(str(context.settings.get("auth_url") or _AUTH))
        self.ws_url = _guard_url(
            str(context.settings.get("ws_url") or _WS[context.profile.environment])
        )
        if ("n2plug.com" in self.base_url) != ("n2plug.com" in self.auth_url):
            raise RouterError(
                "PROVIDER_BRAND_MISMATCH",
                "NAMUH/N2 REST and OAuth hosts must use the same brand.",
            )
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5))
        self._token_lock = asyncio.Lock()

    @classmethod
    def capabilities(cls) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider=cls.provider_id,
            adapter_version=cls.adapter_version,
            environments={ProviderEnvironment.LIVE, ProviderEnvironment.PAPER},
            credential_scope=CredentialScope.PROVIDER,
            features={
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
                "sell_price_type": {"type": "string", "default": "00"},
                "sor_split": {"type": "boolean", "default": True},
            },
        )

    async def close(self) -> None:
        await self.http.aclose()

    def _credentials(self) -> tuple[str, str]:
        key = str(self.context.secrets.get("app_key", ""))
        secret = str(self.context.secrets.get("app_secret", ""))
        if not key or not secret:
            raise RouterError(
                "PROFILE_SECRET_MISSING", "NAMUH App Key and App Secret are required."
            )
        return key, secret

    async def _limit(self, group: str, *, priority: int) -> None:
        if self.context.limiters is not None:
            calls = min(int(self.context.settings.get("rate_limit_per_second", 4)), 5)
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
                f"{self.auth_url}/oauth2/token",
                params={
                    "appkey": key,
                    "appsecretkey": secret,
                    "grant_type": "client_credentials",
                    "scope": "oob",
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            if response.status_code >= 400:
                raise self._http_error(
                    response, "NAMUH token issuance failed", key, secret
                )
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise ProviderError(
                    "PROVIDER_AUTH_FAILED",
                    "NAMUH did not return an access token.",
                    provider="namuh",
                )
            self.context.secrets["access_token"] = token
            self.context.secrets["token_expires_at"] = (
                now_kst() + timedelta(seconds=int(payload.get("expires_in", 86400)))
            ).isoformat()
            self.context.save_secrets()
            return str(token)

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
                payload.get("rsp_msg") or payload.get("error_description") or message,
                *sensitive,
            ),
            retryable=response.status_code in {408, 429, 500, 502, 503, 504},
            status_code=502,
            provider=self.provider_id,
            provider_code=str(
                payload.get("rsp_cd")
                or payload.get("error_code")
                or response.status_code
            ),
            correlation_id=response.headers.get("x-correlation-id"),
        )

    async def _call(
        self,
        path: str,
        input_0: dict[str, Any],
        *,
        group: str = "read",
        order: bool = False,
    ) -> dict[str, Any]:
        key, secret = self._credentials()
        attempts = 1 if order else 3
        for attempt in range(attempts):
            token = await self._token()
            await self._limit(group, priority=0 if order else 20)
            try:
                response = await self.http.post(
                    f"{self.base_url}{path}",
                    headers={
                        "x-client-id": key,
                        "x-client-secret": secret,
                        "authorization": f"Bearer {token}",
                        "content-type": "application/json; charset=UTF-8",
                    },
                    json={"Input_0": input_0},
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if order:
                    raise OrderUncertainError(
                        self.provider_id,
                        "NAMUH order response was not received; reconciliation is required.",
                        correlation_id=uuid4().hex,
                    ) from exc
                if attempt + 1 == attempts:
                    raise ProviderError(
                        "PROVIDER_UNAVAILABLE",
                        "NAMUH request failed.",
                        retryable=True,
                        status_code=502,
                        provider="namuh",
                    ) from exc
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            if response.status_code == 401 and attempt == 0 and not order:
                await self._token(force=True)
                continue
            if response.status_code >= 400:
                if order and response.status_code >= 500:
                    raise OrderUncertainError(
                        self.provider_id,
                        "NAMUH returned a server error after order submission; reconciliation is required.",
                        correlation_id=response.headers.get("x-correlation-id")
                        or uuid4().hex,
                    )
                raise self._http_error(
                    response, "NAMUH request failed", key, secret, input_0
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ProviderError(
                    "PROVIDER_DATA_INVALID",
                    "NAMUH returned an invalid response.",
                    provider="namuh",
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

    async def _quote_row(self, code: str, venue: MarketDataVenue) -> dict[str, Any]:
        payload = await self._call(
            "/krstock/quote/v1/currentPrice",
            {"iem_cd": code, "market_cd": _MARKET[venue]},
        )
        return payload.get("Output_0") or {}

    async def get_quote(self, code: str, venue: MarketDataVenue) -> Quote:
        row = await self._quote_row(code, venue)
        last = decimal_of(row.get("stck_prpr"))
        if last is None:
            raise ProviderError(
                "PROVIDER_DATA_INVALID",
                "NAMUH quote did not contain a price.",
                provider="namuh",
            )
        return Quote(
            instrument=InstrumentId(code=code),
            venue=venue,
            last=last,
            open=decimal_of(row.get("stck_oprc")),
            high=decimal_of(row.get("stck_hgpr")),
            low=decimal_of(row.get("stck_lwpr")),
            previous_close=decimal_of(row.get("stck_prdy_clpr")),
            change=decimal_of(row.get("prdy_vrss")),
            change_rate=decimal_of(row.get("prdy_ctrt")),
            bid=decimal_of(row.get("bidp")),
            ask=decimal_of(row.get("askp")),
            volume=int_of(row.get("acml_vol")),
            trading_status=str(row.get("market_status") or "") or None,
            source=source_metadata(
                provider="namuh",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
            ),
        )

    async def get_orderbook(self, code: str, venue: MarketDataVenue) -> OrderBook:
        row = await self._quote_row(code, venue)
        asks = [
            OrderBookLevel(price=p, quantity=int_of(row.get(f"askp_rsqn{i}"), 0) or 0)
            for i in range(1, 11)
            if (p := decimal_of(row.get(f"askp{i}"))) is not None
        ]
        bids = [
            OrderBookLevel(price=p, quantity=int_of(row.get(f"bidp_rsqn{i}"), 0) or 0)
            for i in range(1, 11)
            if (p := decimal_of(row.get(f"bidp{i}"))) is not None
        ]
        return OrderBook(
            instrument=InstrumentId(code=code),
            venue=venue,
            bids=bids,
            asks=asks,
            source=source_metadata(
                provider="namuh",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
                timestamp=_today_time(row.get("hoga_bsop_hour")),
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
            payload = await self._call(
                "/krstock/quote/v1/period",
                {
                    "market_cd": _MARKET[venue],
                    "iem_cd": code,
                    "mrkt_div_cls_code": "1",
                    "edate": (until or now_kst().date()).strftime("%Y%m%d"),
                    "array_cnt": str(count),
                    "maxavg": "",
                    "gubun": "4",
                    "xtick": str(minutes),
                    "today_cls_code": "0",
                    "fake_tick": "0",
                    "sur_flag": "0",
                    "sur_gb_day_cnt": "00",
                    "sur_bf_end_time": "",
                    "out1_scale_change": "0",
                    "out2_scale_change": "0",
                    "view_main_yn": "N" if venue == MarketDataVenue.INTEGRATED else "Y",
                },
            )
            rows = payload.get("Output_1") or []
            result: list[Bar] = []
            for row in reversed(rows[:count]):
                raw_date = str(
                    row.get("bsop_date")
                    or (until or now_kst().date()).strftime("%Y%m%d")
                )
                raw_time = (
                    str(row.get("bsop_time") or "000000")
                    .replace(":", "")
                    .ljust(6, "0")[:6]
                )
                started = datetime.strptime(
                    raw_date + raw_time, "%Y%m%d%H%M%S"
                ).replace(tzinfo=KST)
                result.append(
                    self._bar_from_row(
                        code,
                        venue,
                        interval,
                        started,
                        timedelta(minutes=minutes),
                        row,
                        close_key="stck_prpr",
                        volume_key="vol",
                    )
                )
            return result[-count:]
        payload = await self._call(
            "/krstock/quote/v1/currentDaily",
            {
                "market_cd": _MARKET[venue],
                "iem_cd": code,
                "array_cnt": str(min(count * (7 if interval == "1w" else 1), 1000)),
                "view_main_yn": "N" if venue == MarketDataVenue.INTEGRATED else "Y",
            },
        )
        daily: list[Bar] = []
        for row in reversed(payload.get("Output_0") or []):
            started = datetime.strptime(str(row.get("bsop_date")), "%Y%m%d").replace(
                tzinfo=KST
            )
            daily.append(
                self._bar_from_row(
                    code,
                    venue,
                    "1d",
                    started,
                    timedelta(days=1),
                    row,
                    close_key="stck_clpr",
                    volume_key="acml_vol",
                )
            )
        if interval == "1d":
            return daily[-count:]
        weeks: dict[tuple[int, int], list[Bar]] = {}
        for bar in daily:
            key = bar.started_at.isocalendar()[:2]
            weeks.setdefault(key, []).append(bar)
        result: list[Bar] = []
        for group in weeks.values():
            result.append(
                group[0].model_copy(
                    update={
                        "interval": "1w",
                        "ended_at": group[-1].ended_at,
                        "high": max(x.high for x in group),
                        "low": min(x.low for x in group),
                        "close": group[-1].close,
                        "volume": sum(x.volume for x in group),
                    }
                )
            )
        return result[-count:]

    def _bar_from_row(
        self,
        code: str,
        venue: MarketDataVenue,
        interval: str,
        started: datetime,
        step: timedelta,
        row: dict[str, Any],
        *,
        close_key: str,
        volume_key: str,
    ) -> Bar:
        return Bar(
            instrument=InstrumentId(code=code),
            venue=venue,
            interval=interval,
            started_at=started,
            ended_at=started + step,
            open=decimal_of(row.get("stck_oprc"), Decimal(0)) or Decimal(0),
            high=decimal_of(row.get("stck_hgpr"), Decimal(0)) or Decimal(0),
            low=decimal_of(row.get("stck_lwpr"), Decimal(0)) or Decimal(0),
            close=decimal_of(row.get(close_key), Decimal(0)) or Decimal(0),
            volume=int_of(row.get(volume_key), 0) or 0,
            complete=True,
            source=source_metadata(
                provider="namuh",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
                timestamp=started + step,
            ),
        )

    async def _balance_payload(self, account_number: str) -> dict[str, Any]:
        payload = await self._call(
            "/krstock/inquiry/v1/balance",
            {
                "act_no": account_number,
                "bnc_bse_cd": "5",
                "ltg_aot_dit_cd": "9",
                "aet_bse": "2",
                "qut_dit_cd": "UNT",
                "aly_qut_cd": "2",
            },
            group="account",
        )
        summary = payload.get("Output_0")
        if not isinstance(summary, dict) or not summary:
            raise ProviderError(
                "PROVIDER_DATA_INVALID",
                redact_text(
                    payload.get("rsp_msg") or "NAMUH balance response was incomplete.",
                    account_number,
                ),
                provider=self.provider_id,
                provider_code=str(payload.get("rsp_cd") or ""),
            )
        return payload

    async def get_balance(self, account_alias: str, account_number: str) -> Balance:
        payload = await self._balance_payload(account_number)
        row = payload.get("Output_0") or {}
        return Balance(
            account_alias=account_alias,
            cash=decimal_of(row.get("dca")),
            available_cash=decimal_of(row.get("orr_pbl_amt2")),
            buying_power=decimal_of(
                first(row, "orr_pbl_amt4", "orr_pbl_amt3", "orr_pbl_amt2")
            ),
            total_equity=decimal_of(first(row, "tot_aet_amt", "nas_amt")),
            market_value=decimal_of(row.get("tot_eal_amt")),
            unrealized_pnl=decimal_of(row.get("tot_eal_pls")),
            source=source_metadata(
                provider="namuh",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
            ),
        )

    async def get_positions(
        self, account_alias: str, account_number: str
    ) -> list[Position]:
        payload = await self._balance_payload(account_number)
        result: list[Position] = []
        for row in payload.get("Output_1") or []:
            quantity = int_of(row.get("itg_bnc_qty"), 0) or 0
            if quantity <= 0:
                continue
            result.append(
                Position(
                    account_alias=account_alias,
                    instrument=InstrumentId(code=str(row.get("iem_cd"))),
                    name=row.get("iem_nm"),
                    quantity=quantity,
                    available_quantity=int_of(first(row, "rsdl_qty", "itg_bnc_qty")),
                    average_price=decimal_of(row.get("phs_pr")),
                    market_price=decimal_of(row.get("now_pr")),
                    market_value=decimal_of(row.get("eal_amt")),
                    unrealized_pnl=decimal_of(row.get("eal_pls_amt")),
                    unrealized_pnl_rate=decimal_of(row.get("pft_rt")),
                    source=source_metadata(
                        provider="namuh",
                        profile=self.context.profile.id,
                        generation=self.context.connection_generation,
                    ),
                )
            )
        return result

    async def get_buying_power(
        self, account_alias: str, account_number: str, request: OrderRequest
    ) -> BuyingPower:
        inputs: dict[str, Any] = {
            "ost_dit_cd": "1",
            "act_no": account_number,
            "iem_cd": request.instrument.code,
            "nmn_pr_tp_cd": "05" if request.order_type == OrderType.MARKET else "01",
        }
        if request.price is not None:
            inputs["orr_pr"] = int(request.price)
        payload = await self._call(
            "/krstock/inquiry/v1/buyableQuantity", inputs, group="account"
        )
        row = payload.get("Output_0") or {}
        quote = await self._quote_row(
            request.instrument.code, MarketDataVenue.INTEGRATED
        )
        calculation = request.price or decimal_of(quote.get("stck_mxpr"))
        return BuyingPower(
            account_alias=account_alias,
            instrument=request.instrument,
            order_type=request.order_type,
            price=request.price,
            max_quantity=int_of(
                first(row, "csh_orr_pbl_qty", "orr_pbl_qty", "max_pbl_qty"), 0
            )
            or 0,
            available_amount=decimal_of(
                first(row, "csh_orr_pbl_amt", "orr_pbl_amt1", "max_pbl_amt")
            ),
            calculation_price=calculation,
            source=source_metadata(
                provider="namuh",
                profile=self.context.profile.id,
                generation=self.context.connection_generation,
            ),
        )

    async def preview_order(
        self, account_number: str, request: OrderRequest
    ) -> OrderPreview:
        if request.side == OrderSide.SELL:
            positions = await self.get_positions(request.account_alias, account_number)
            position = next(
                (p for p in positions if p.instrument.code == request.instrument.code),
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
            allowed = (available or 0) >= request.quantity
            return OrderPreview(
                request=request,
                allowed=allowed,
                estimated_price=price,
                estimated_notional=price * request.quantity,
                max_notional=price * request.quantity,
                available_quantity=available or 0,
                block_code=None if allowed else "INSUFFICIENT_POSITION",
                block_reason=None
                if allowed
                else "Sell quantity exceeds available position.",
            )
        buying = await self.get_buying_power(
            request.account_alias, account_number, request
        )
        conservative = request.price or buying.calculation_price
        allowed = buying.max_quantity >= request.quantity and conservative is not None
        return OrderPreview(
            request=request,
            allowed=allowed,
            estimated_price=conservative,
            estimated_notional=conservative * request.quantity
            if conservative
            else None,
            max_notional=conservative * request.quantity if conservative else None,
            available_quantity=buying.max_quantity,
            available_amount=buying.available_amount,
            block_code=None if allowed else "INSUFFICIENT_BUYING_POWER",
            block_reason=None
            if allowed
            else "NAMUH buying power or conservative market price is insufficient.",
        )

    @staticmethod
    def _condition(request: OrderRequest) -> str:
        return {
            TimeInForce.DAY: "00",
            TimeInForce.IOC: "01",
            TimeInForce.FOK: "02",
            TimeInForce.GTP: "05",
        }[request.time_in_force]

    async def submit_order(
        self, account_number: str, request: OrderRequest, *, router_order_id: str
    ) -> Order:
        route = _ROUTE[request.route]
        inputs: dict[str, Any] = {
            "act_no": account_number,
            "iem_cd": request.instrument.code,
            "orr_qty": request.quantity,
            "nmn_pr_tp_cd": "05" if request.order_type == OrderType.MARKET else "01",
            "orr_cnd_dit_cd": self._condition(request),
            "ssl_nmn_pr_dit_cd": str(
                request.provider_options.get("sell_price_type", "00")
            ),
            "rmt_mkt_cd": route,
            "sor_mkt_sli_yn": "Y"
            if route == "SOR" and bool(request.provider_options.get("sor_split", True))
            else "N",
        }
        if request.price is not None:
            inputs["orr_pr"] = int(request.price)
        path = (
            "/krstock/order/v1/cashBuy"
            if request.side == OrderSide.BUY
            else "/krstock/order/v1/cashSell"
        )
        payload = await self._call(path, inputs, group="order", order=True)
        row = payload.get("Output_0") or {}
        provider_id = str(row.get("mkt_orr_no") or "") or None
        if not provider_id:
            raise OrderUncertainError(
                "namuh",
                "NAMUH response did not contain an order number.",
                correlation_id=uuid4().hex,
            )
        now = now_kst()
        return Order(
            router_order_id=router_order_id,
            client_order_id=request.client_order_id,
            strategy_id=request.strategy_id,
            account_alias=request.account_alias,
            provider="namuh",
            provider_order_id=provider_id,
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
            message=str(payload.get("rsp_msg") or "NAMUH acknowledged the order."),
            correlation_id=uuid4().hex,
        )

    async def cancel_order(self, account_number: str, order: Order) -> Order:
        if not order.provider_order_id:
            raise RouterError(
                "PROVIDER_ORDER_ID_MISSING", "NAMUH order number is required."
            )
        await self._call(
            "/krstock/order/v1/cancel",
            {
                "act_no": account_number,
                "org_mkt_orr_no": int(order.provider_order_id),
                "all_pat_dit_cd": "1",
                "iem_cd": order.instrument.code,
            },
            group="order",
            order=True,
        )
        return order.model_copy(
            update={"status": OrderStatus.CANCEL_PENDING, "updated_at": now_kst()}
        )

    async def amend_order(
        self,
        account_number: str,
        order: Order,
        *,
        quantity: int | None,
        price: str | None,
    ) -> Order:
        if not order.provider_order_id:
            raise RouterError(
                "PROVIDER_ORDER_ID_MISSING", "NAMUH order number is required."
            )
        new_qty = quantity or max(order.quantity - order.filled_quantity, 0)
        new_price = Decimal(price) if price is not None else order.price
        if new_price is None:
            raise RouterError(
                "AMENDMENT_PRICE_REQUIRED", "NAMUH amendment requires a price."
            )
        route = _ROUTE[order.route]
        await self._call(
            "/krstock/order/v1/modify",
            {
                "act_no": account_number,
                "org_mkt_orr_no": int(order.provider_order_id),
                "all_pat_dit_cd": "2",
                "iem_cd": order.instrument.code,
                "cor_qty": new_qty,
                "cor_pr": int(new_price),
                "sop_cnd_pr": 0,
                "rmt_mkt_cd": route,
                "sor_mkt_sli_yn": "Y" if route == "SOR" else "N",
            },
            group="order",
            order=True,
        )
        return order.model_copy(
            update={
                "quantity": quantity or order.quantity,
                "price": new_price,
                "updated_at": now_kst(),
            }
        )

    async def get_orders(
        self, account_alias: str, account_number: str, *, open_only: bool = False
    ) -> list[Order]:
        today = now_kst().strftime("%Y%m%d")
        payload = await self._call(
            "/krstock/inquiry/v1/dailyOrderExecution",
            {
                "orr_dt": today,
                "act_no": account_number,
                "itg_orr_no": 0,
                "orr_mkt_cd": "00",
                "ost_cns_dit": "0",
            },
            group="account",
        )
        result: list[Order] = []
        for row in payload.get("Output_0") or []:
            provider_id = str(row.get("itg_orr_no") or "")
            if not provider_id:
                continue
            quantity = int_of(row.get("orr_qty"), 0) or 0
            filled = int_of(row.get("tot_cns_qty"), 0) or 0
            remaining = int_of(row.get("ny_cns_qty"), max(quantity - filled, 0)) or 0
            cancelled = (int_of(row.get("can_qty"), 0) or 0) > 0 and remaining == 0
            rejected = (
                bool(str(row.get("orr_rjt_rsn_cd_nm") or "").strip()) and filled == 0
            )
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
                if "매도" in str(row.get("sby_dit_cd_nm") or "")
                else OrderSide.BUY
            )
            type_name = str(row.get("nmn_pr_tp_cd_nm") or "")
            route_raw = str(row.get("rmt_mkt_cd") or "KRX").upper()
            route = (
                OrderRoute(route_raw)
                if route_raw in {item.value for item in OrderRoute}
                else OrderRoute.KRX
            )
            result.append(
                Order(
                    router_order_id=f"provider:namuh:{today}:{provider_id}",
                    client_order_id=f"provider:{provider_id}",
                    strategy_id="__provider__",
                    account_alias=account_alias,
                    provider="namuh",
                    provider_order_id=provider_id,
                    instrument=InstrumentId(code=str(row.get("iem_cd"))),
                    side=side,
                    order_type=OrderType.MARKET
                    if "시장" in type_name
                    else OrderType.LIMIT,
                    route=route,
                    time_in_force=TimeInForce.DAY,
                    quantity=quantity,
                    price=decimal_of(row.get("orr_pr")),
                    filled_quantity=filled,
                    average_fill_price=decimal_of(row.get("cns_avg_uit_pr")),
                    status=status,
                    submitted_at=now_kst().replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ),
                    updated_at=now_kst(),
                    message=str(row.get("orr_rjt_rsn_cd_nm") or "") or None,
                    correlation_id=uuid4().hex,
                )
            )
        return result

    async def stream(
        self, subscriptions: list[StreamSubscription]
    ) -> AsyncIterator[StreamEvent]:
        if len(subscriptions) > 30:
            raise RouterError(
                "STREAM_LIMIT_EXCEEDED",
                "NAMUH allows at most 30 registrations per session.",
            )
        if any(
            item.kind not in {StreamKind.QUOTE, StreamKind.ORDERBOOK, StreamKind.TRADE}
            or not item.code
            for item in subscriptions
        ):
            raise UnsupportedCapabilityError(self.provider_id, "requested stream")
        token = await self._token()
        async with websockets.connect(
            self.ws_url, ping_interval=20, ping_timeout=20
        ) as socket:
            sent: set[tuple[str, str]] = set()
            for index, item in enumerate(subscriptions):
                tr_cd = self._channel(item)
                registration = (tr_cd, str(item.code))
                if registration in sent:
                    continue
                sent.add(registration)
                await socket.send(
                    json.dumps(
                        {
                            "header": {"token": token, "tr_type": "1"},
                            "body": {"tr_cd": tr_cd, "tr_key": item.code},
                        }
                    )
                )
                if index and index % 9 == 0:
                    await asyncio.sleep(1)
            sequence = 0
            async for raw in socket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                message = json.loads(raw)
                header = message.get("header") or {}
                if header.get("rsp_cd") is not None:
                    if str(header.get("rsp_cd")) not in {"0", "00000"}:
                        raise ProviderError(
                            "PROVIDER_STREAM_REJECTED",
                            str(
                                header.get("rsp_msg")
                                or "NAMUH rejected a subscription."
                            ),
                            provider="namuh",
                            provider_code=str(header.get("rsp_cd")),
                        )
                    continue
                tr_cd = str(header.get("tr_cd") or "")
                code = str(header.get("tr_key") or "")
                body = message.get("body") or {}
                event = self._parse_stream(tr_cd, code, body)
                if event is None:
                    continue
                if event.kind == StreamKind.ORDERBOOK:
                    book = OrderBook.model_validate(event.payload)
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
                        quantity=int_of(first(body, "cntg_vol", "vol"), 0) or 0,
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
    def _channel(item: StreamSubscription) -> str:
        if item.kind == StreamKind.ORDERBOOK:
            return {
                MarketDataVenue.KRX: "ob",
                MarketDataVenue.NXT: "nb",
                MarketDataVenue.INTEGRATED: "mb",
            }[item.venue]
        return {
            MarketDataVenue.KRX: "oc",
            MarketDataVenue.NXT: "nc",
            MarketDataVenue.INTEGRATED: "mc",
        }[item.venue]

    def _parse_stream(
        self, channel: str, code: str, body: dict[str, Any]
    ) -> StreamEvent | None:
        venue = (
            MarketDataVenue.KRX
            if channel.startswith("o")
            else MarketDataVenue.NXT
            if channel.startswith("n")
            else MarketDataVenue.INTEGRATED
        )
        source = source_metadata(
            provider="namuh",
            profile=self.context.profile.id,
            generation=self.context.connection_generation,
            timestamp=_today_time(first(body, "bsop_hour", "hoga_bsop_hour")),
        )
        if channel.endswith("b"):
            payload = OrderBook(
                instrument=InstrumentId(code=code),
                venue=venue,
                asks=[
                    OrderBookLevel(
                        price=p, quantity=int_of(body.get(f"askp_rsqn{i}"), 0) or 0
                    )
                    for i in range(1, 11)
                    if (p := decimal_of(body.get(f"askp{i}"))) is not None
                ],
                bids=[
                    OrderBookLevel(
                        price=p, quantity=int_of(body.get(f"bidp_rsqn{i}"), 0) or 0
                    )
                    for i in range(1, 11)
                    if (p := decimal_of(body.get(f"bidp{i}"))) is not None
                ],
                source=source,
            ).model_dump(mode="json")
            kind = StreamKind.ORDERBOOK
        elif channel.endswith("c"):
            price = decimal_of(body.get("stck_prpr"))
            if price is None:
                return None
            payload = Quote(
                instrument=InstrumentId(code=code),
                venue=venue,
                last=price,
                open=decimal_of(body.get("stck_oprc")),
                high=decimal_of(body.get("stck_hgpr")),
                low=decimal_of(body.get("stck_lwpr")),
                change=decimal_of(body.get("prdy_vrss")),
                change_rate=decimal_of(body.get("prdy_ctrt")),
                bid=decimal_of(body.get("bidp")),
                ask=decimal_of(body.get("askp")),
                volume=int_of(body.get("acml_vol")),
                source=source,
            ).model_dump(mode="json")
            kind = StreamKind.QUOTE
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
