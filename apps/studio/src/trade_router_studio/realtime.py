from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from trade_router import RouterError, RouterErrorCode, Venue
from trade_router.subscriptions import SubscriptionLease

from .connections import ConnectionRuntime
from .event_bus import StrategyEventBus


SEOUL = ZoneInfo("Asia/Seoul")
_QUOTE_TR_IDS = {
    Venue.KRX: "H0STCNT0",
    Venue.NXT: "H0NXCNT0",
    Venue.INTEGRATED: "H0UNCNT0",
}
_ORDERBOOK_TR_IDS = {
    Venue.KRX: "H0STASP0",
    Venue.NXT: "H0NXASP0",
    Venue.INTEGRATED: "H0UNASP0",
}
_NAMUH_QUOTE_TR_IDS = {
    Venue.KRX: "oc",
    Venue.NXT: "nc",
    Venue.INTEGRATED: "mc",
}
_NAMUH_ORDERBOOK_TR_IDS = {
    Venue.KRX: "ob",
    Venue.NXT: "nb",
    Venue.INTEGRATED: "mb",
}
_QUOTE_FIELD_COUNT = 47
_ORDERBOOK_FIELD_COUNTS = {"H0STASP0": 63, "H0NXASP0": 66, "H0UNASP0": 67}


@dataclass(slots=True)
class _Consumer:
    id: str
    strategy_name: str
    source_alias: str | None
    lease: SubscriptionLease
    account_alias: str | None = None


@dataclass(slots=True)
class _Desired:
    connection_id: str
    provider: str
    kind: str
    code: str
    venue: Venue
    tr_id: str
    consumers: dict[str, _Consumer]


class RealtimeHub:
    """Own broker sockets, re-subscribe desired streams, and fan out events."""

    def __init__(
        self,
        connections: ConnectionRuntime,
        events: StrategyEventBus,
        *,
        connector: Callable[..., Any] | None = None,
    ) -> None:
        self._connections = connections
        self._events = events
        self._connector = connector
        self._desired: dict[tuple[str, str, str], _Desired] = {}
        self._consumer_index: dict[str, tuple[tuple[str, str, str], _Consumer]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._changed: dict[str, asyncio.Event] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._account_event_handler: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None

    def set_account_event_handler(
        self,
        handler: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._account_event_handler = handler

    async def subscribe(
        self,
        *,
        strategy_name: str,
        source: str,
        source_alias: str | None,
        kind: str,
        code: str,
        venue: Venue,
    ) -> dict[str, Any]:
        normalized_kind = kind.strip().upper()
        instrument = code.strip().upper()
        if not instrument or len(instrument) > 24:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "실시간 구독 종목코드를 확인하세요.",
            )
        info = self._connections.realtime_connection(source)
        provider = str(info["provider"])
        if normalized_kind == "QUOTE":
            tr_id = (
                _QUOTE_TR_IDS[venue]
                if provider == "kis"
                else _NAMUH_QUOTE_TR_IDS[venue]
            )
        elif normalized_kind == "ORDERBOOK":
            tr_id = (
                _ORDERBOOK_TR_IDS[venue]
                if provider == "kis"
                else _NAMUH_ORDERBOOK_TR_IDS[venue]
            )
        else:
            raise RouterError(
                RouterErrorCode.INVALID_REQUEST,
                "실시간 종류는 QUOTE 또는 ORDERBOOK이어야 합니다.",
            )
        await self._connections.realtime_approval_key(source)
        # Profiles that use the same broker key and target share one socket,
        # matching the REST token/rate-limit scope.
        subscription_key = f"{tr_id}:{instrument}"
        consumer_id = f"{strategy_name}:{uuid4().hex}"
        lease = await self._connections.reserve_realtime_subscription(
            source,
            consumer_id=consumer_id,
            subscription_key=subscription_key,
        )
        connection_id = f"{info['rate_scope']}::{lease.session_id}"
        consumer = _Consumer(
            id=consumer_id,
            strategy_name=strategy_name,
            source_alias=source_alias,
            lease=lease,
        )
        key = (connection_id, tr_id, instrument)
        async with self._lock:
            desired = self._desired.get(key)
            if desired is None:
                desired = _Desired(
                    connection_id=connection_id,
                    provider=provider,
                    kind=normalized_kind,
                    code=instrument,
                    venue=venue,
                    tr_id=tr_id,
                    consumers={},
                )
                self._desired[key] = desired
            desired.consumers[consumer_id] = consumer
            self._consumer_index[consumer_id] = (key, consumer)
            changed = self._changed.setdefault(connection_id, asyncio.Event())
            changed.set()
            task = self._tasks.get(connection_id)
            if task is None or task.done():
                self._tasks[connection_id] = asyncio.create_task(
                    self._run_connection(source, connection_id),
                    name=f"trade-router-ws-{connection_id}",
                )
        return {
            "id": consumer_id,
            "kind": normalized_kind,
            "code": instrument,
            "venue": venue.value,
            "source": source,
            "source_alias": source_alias,
            "shared": not lease.created,
        }

    async def subscribe_account(
        self, *, strategy_name: str, account_reference: str
    ) -> dict[str, Any] | None:
        specs = self._connections.realtime_account_subscriptions(account_reference)
        if not specs:
            return None
        ids: list[str] = []
        try:
            for spec in specs:
                ids.append(
                    await self._subscribe_account_spec(
                        strategy_name=strategy_name,
                        spec=spec,
                    )
                )
        except Exception:
            for consumer_id in reversed(ids):
                await self.unsubscribe(consumer_id, strategy_name=strategy_name)
            raise
        return {
            "id": ids[0],
            "ids": ids,
            "kind": "ACCOUNT",
            "source": str(specs[0]["source"]),
        }

    async def _subscribe_account_spec(
        self,
        *,
        strategy_name: str,
        spec: dict[str, Any],
    ) -> str:
        source = str(spec["source"])
        info = self._connections.realtime_connection(source)
        await self._connections.realtime_approval_key(source)
        tr_id = str(spec["tr_id"])
        tr_key = str(spec["tr_key"])
        consumer_id = f"{strategy_name}:account:{uuid4().hex}"
        lease = await self._connections.reserve_realtime_subscription(
            source,
            consumer_id=consumer_id,
            subscription_key=f"{tr_id}:{tr_key}",
        )
        connection_id = f"{info['rate_scope']}::{lease.session_id}"
        consumer = _Consumer(
            id=consumer_id,
            strategy_name=strategy_name,
            source_alias=None,
            lease=lease,
            account_alias=str(spec["account_alias"]),
        )
        key = (connection_id, tr_id, tr_key)
        async with self._lock:
            desired = self._desired.get(key)
            if desired is None:
                desired = _Desired(
                    connection_id=connection_id,
                    provider=str(info["provider"]),
                    kind="ACCOUNT",
                    code=tr_key,
                    venue=Venue.KRX,
                    tr_id=tr_id,
                    consumers={},
                )
                self._desired[key] = desired
            desired.consumers[consumer_id] = consumer
            self._consumer_index[consumer_id] = (key, consumer)
            self._changed.setdefault(connection_id, asyncio.Event()).set()
            task = self._tasks.get(connection_id)
            if task is None or task.done():
                self._tasks[connection_id] = asyncio.create_task(
                    self._run_connection(source, connection_id),
                    name=f"trade-router-ws-{connection_id}",
                )
        return consumer_id

    async def unsubscribe(self, consumer_id: str, *, strategy_name: str) -> bool:
        async with self._lock:
            indexed = self._consumer_index.get(consumer_id)
            if indexed is None or indexed[1].strategy_name != strategy_name:
                return False
            key, consumer = indexed
        remove_broker = await self._connections.release_realtime_subscription(
            consumer.lease,
            consumer_id=consumer.id,
        )
        async with self._lock:
            self._consumer_index.pop(consumer_id, None)
            desired = self._desired.get(key)
            if desired is not None:
                desired.consumers.pop(consumer_id, None)
                if not desired.consumers:
                    self._desired.pop(key, None)
            if remove_broker:
                self._changed.setdefault(key[0], asyncio.Event()).set()
        return True

    async def unsubscribe_strategy(self, strategy_name: str) -> None:
        async with self._lock:
            ids = [
                consumer_id
                for consumer_id, (_, consumer) in self._consumer_index.items()
                if consumer.strategy_name == strategy_name
            ]
        for consumer_id in ids:
            await self.unsubscribe(consumer_id, strategy_name=strategy_name)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def snapshot(self) -> list[dict[str, Any]]:
        async with self._lock:
            counts: dict[str, int] = {}
            for connection_id, _, _ in self._desired:
                counts[connection_id] = counts.get(connection_id, 0) + 1
            connection_ids = set(self._states) | set(counts)
            return [
                {
                    **self._states.get(
                        connection_id,
                        {
                            "connection_id": connection_id,
                            "state": "waiting",
                            "generation": 0,
                            "last_received_at": None,
                            "message": "실시간 연결을 준비하고 있습니다.",
                        },
                    ),
                    "subscriptions": counts.get(connection_id, 0),
                }
                for connection_id in sorted(connection_ids)
            ]

    async def _run_connection(self, reference: str, connection_id: str) -> None:
        delay = 1
        generation = 0
        while not self._closed:
            async with self._lock:
                has_desired = any(key[0] == connection_id for key in self._desired)
            if not has_desired:
                self._states[connection_id] = {
                    "connection_id": connection_id,
                    "state": "idle",
                    "generation": generation,
                    "last_received_at": self._states.get(connection_id, {}).get("last_received_at"),
                    "message": "활성 실시간 구독이 없습니다.",
                }
                return
            try:
                approval_key = await self._connections.realtime_approval_key(reference)
                info = self._connections.realtime_connection(reference)
                provider = str(info["provider"])
                control_interval = float(info.get("control_interval") or 1.0)
                connector = self._connector
                if connector is None:
                    from websockets.asyncio.client import connect

                    connector = connect
                async with connector(str(info["url"]), ping_interval=None) as socket:
                    generation += 1
                    delay = 1
                    active: set[tuple[str, str]] = set()
                    pending_controls: list[tuple[str, str, bool]] = []
                    encryption: dict[str, tuple[str, str]] = {}
                    self._states[connection_id] = {
                        "connection_id": connection_id,
                        "state": "connected",
                        "generation": generation,
                        "last_received_at": None,
                        "message": "실시간 소켓이 연결되었습니다.",
                    }
                    while not self._closed:
                        changed = self._changed.setdefault(
                            connection_id, asyncio.Event()
                        )
                        # Clear before taking the desired-state snapshot. Any
                        # change after this point remains set and wakes the
                        # receiver loop; changes before it are already present
                        # in the snapshot below.
                        changed.clear()
                        desired = await self._desired_for(connection_id)
                        wanted = {(item.tr_id, item.code) for item in desired}
                        for tr_id, code in sorted(active - wanted):
                            await socket.send(
                                _control_frame(
                                    provider,
                                    approval_key,
                                    tr_id,
                                    code,
                                    subscribe=False,
                                )
                            )
                            pending_controls.append((tr_id, code, False))
                            active.discard((tr_id, code))
                            await asyncio.sleep(control_interval)
                        for tr_id, code in sorted(wanted - active):
                            await socket.send(
                                _control_frame(
                                    provider,
                                    approval_key,
                                    tr_id,
                                    code,
                                    subscribe=True,
                                )
                            )
                            pending_controls.append((tr_id, code, True))
                            active.add((tr_id, code))
                            await asyncio.sleep(control_interval)
                        if not wanted:
                            return
                        receive_task = asyncio.create_task(socket.recv())
                        changed_task = asyncio.create_task(changed.wait())
                        done, pending = await asyncio.wait(
                            {receive_task, changed_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        if changed_task in done:
                            continue
                        message = receive_task.result()
                        if isinstance(message, bytes):
                            message = message.decode("utf-8", errors="replace")
                        control_tr_id, control_code = _control_subscription_identity(
                            provider, message
                        )
                        try:
                            control = await _handle_control_message(
                                provider, socket.send, message
                            )
                        except RouterError as exc:
                            pending_control = _pop_pending_control(
                                pending_controls,
                                tr_id=control_tr_id,
                                code=control_code,
                            )
                            if pending_control is not None:
                                rejected_tr_id, rejected_code, subscribing = pending_control
                            else:
                                rejected_tr_id = control_tr_id
                                rejected_code = control_code
                                subscribing = False
                            quarantined = False
                            if (
                                subscribing
                                and rejected_code
                                and _is_nxt_ineligible_rejection(
                                    rejected_tr_id, exc
                                )
                            ):
                                quarantined = await self._quarantine_rejected_market_stream(
                                    connection_id,
                                    rejected_tr_id,
                                    rejected_code,
                                )
                            if not quarantined:
                                raise
                            active.discard((rejected_tr_id, rejected_code))
                            self._states[connection_id]["message"] = (
                                f"{rejected_tr_id}:{rejected_code} 실시간 구독이 거부되어 "
                                "해당 구독만 제외했습니다. 나머지 구독은 유지합니다."
                            )
                            continue
                        if control is not None:
                            _pop_pending_control(
                                pending_controls,
                                tr_id=control_tr_id,
                                code=control_code,
                            )
                            tr_id = str(control.get("tr_id") or "")
                            key = str(control.get("key") or "")
                            iv = str(control.get("iv") or "")
                            if tr_id and key and iv:
                                encryption[tr_id] = (key, iv)
                            continue
                        if provider == "kis":
                            events = _parse_kis_market_messages(
                                message,
                                generation=generation,
                                encryption=encryption,
                            )
                        else:
                            event = _parse_namuh_market_message(
                                message, generation=generation
                            )
                            events = [event] if event is not None else []
                        if not events:
                            continue
                        for event in events:
                            self._states[connection_id]["last_received_at"] = event[
                                "received_at"
                            ]
                            await self._dispatch(connection_id, event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._states[connection_id] = {
                    "connection_id": connection_id,
                    "state": "reconnecting",
                    "generation": generation,
                    "last_received_at": self._states.get(connection_id, {}).get("last_received_at"),
                    "message": f"실시간 연결이 끊겨 {delay}초 후 재연결합니다: {type(exc).__name__}",
                }
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _quarantine_rejected_market_stream(
        self,
        connection_id: str,
        tr_id: str,
        code: str,
    ) -> bool:
        key = (connection_id, tr_id, code)
        async with self._lock:
            desired = self._desired.get(key)
            if desired is None or desired.kind not in {"QUOTE", "ORDERBOOK"}:
                return False
            consumers = tuple(desired.consumers.values())
        for consumer in consumers:
            await self.unsubscribe(
                consumer.id,
                strategy_name=consumer.strategy_name,
            )
        return bool(consumers)

    async def _desired_for(self, connection_id: str) -> list[_Desired]:
        async with self._lock:
            return [item for item in self._desired.values() if item.connection_id == connection_id]

    async def _dispatch(self, connection_id: str, event: dict[str, Any]) -> None:
        tr_id = str(event.pop("tr_id"))
        key = (connection_id, tr_id, str(event["code"]))
        async with self._lock:
            if event.get("event_type") == "account_notice":
                consumers = tuple(
                    consumer
                    for desired_key, desired in self._desired.items()
                    if desired_key[0] == connection_id and desired_key[1] == tr_id
                    for consumer in desired.consumers.values()
                )
            else:
                desired = self._desired.get(key)
                consumers = tuple(desired.consumers.values()) if desired else ()
        if event.get("event_type") == "account_notice":
            if self._account_event_handler is not None:
                for account_alias in sorted(
                    {consumer.account_alias for consumer in consumers if consumer.account_alias}
                ):
                    await self._account_event_handler(
                        account_alias, dict(event["notice"])
                    )
            return
        for consumer in consumers:
            payload = dict(event)
            payload["source_alias"] = consumer.source_alias
            await self._events.publish(consumer.strategy_name, payload)


def _kis_control_frame(
    approval_key: str, tr_id: str, code: str, *, subscribe: bool
) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": code}},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _namuh_control_frame(
    token: str, tr_id: str, code: str, *, subscribe: bool
) -> str:
    return json.dumps(
        {
            "header": {"token": token, "tr_type": "1" if subscribe else "2"},
            "body": {"tr_cd": tr_id, "tr_key": code},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _control_frame(
    provider: str,
    credential: str,
    tr_id: str,
    code: str,
    *,
    subscribe: bool,
) -> str:
    if provider == "kis":
        return _kis_control_frame(
            credential, tr_id, code, subscribe=subscribe
        )
    return _namuh_control_frame(
        credential, tr_id, code, subscribe=subscribe
    )


def _control_subscription_identity(
    provider: str,
    message: str,
) -> tuple[str, str]:
    if not message.startswith("{"):
        return "", ""
    try:
        payload = json.loads(message)
    except ValueError:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    header = payload.get("header")
    body = payload.get("body")
    header = header if isinstance(header, dict) else {}
    body = body if isinstance(body, dict) else {}
    body_input = body.get("input")
    body_input = body_input if isinstance(body_input, dict) else {}
    if provider == "kis":
        return (
            str(header.get("tr_id") or ""),
            str(
                header.get("tr_key")
                or body_input.get("tr_key")
                or ""
            ),
        )
    return (
        str(header.get("tr_cd") or ""),
        str(
            header.get("tr_key")
            or body.get("tr_key")
            or body_input.get("tr_key")
            or ""
        ),
    )


def _pop_pending_control(
    pending: list[tuple[str, str, bool]],
    *,
    tr_id: str,
    code: str,
) -> tuple[str, str, bool] | None:
    if not tr_id:
        return None
    for index, item in enumerate(pending):
        if item[0] != tr_id:
            continue
        if code and item[1] != code:
            continue
        return pending.pop(index)
    return None


_NXT_MARKET_TR_IDS = frozenset({"H0NXCNT0", "H0NXASP0"})
_NXT_INELIGIBLE_MESSAGE_MARKERS = (
    "NXT거래대상이아닙니다",
    "NXT거래대상종목이아닙니다",
    "NXT거래종목이아닙니다",
    "NXT대상종목이아닙니다",
    "NXT미대상",
    "NXT미지원종목",
    "거래대상종목이아닙니다",
    "지원하지않는종목",
    "미지원종목",
    "NXTNOTELIGIBLE",
    "NOTELIGIBLEFORNXT",
    "NXTUNSUPPORTEDINSTRUMENT",
    "UNSUPPORTEDINSTRUMENT",
    "INSTRUMENTNOTSUPPORTED",
    "NOTSUPPORTEDONNXT",
    "NON-NXT",
)


def _is_nxt_ineligible_rejection(tr_id: str, exc: RouterError) -> bool:
    if (
        exc.code is not RouterErrorCode.INVALID_REQUEST
        or tr_id not in _NXT_MARKET_TR_IDS
    ):
        return False
    normalized = "".join(str(exc.message or "").upper().split())
    return any(marker in normalized for marker in _NXT_INELIGIBLE_MESSAGE_MARKERS)


async def _handle_control_message(
    provider: str,
    sender: Callable[[str], Awaitable[Any]],
    message: str,
) -> dict[str, Any] | None:
    if provider == "kis":
        return await _handle_kis_control_message(sender, message)
    if not message.startswith("{"):
        return None
    try:
        payload = json.loads(message)
    except ValueError:
        return {}
    header = payload.get("header") if isinstance(payload, dict) else None
    if not isinstance(header, dict) or "tr_type" not in header:
        return None
    code = str(header.get("rsp_cd") or "")
    if code and code != "00000":
        raise RouterError(
            RouterErrorCode.RATE_LIMITED if code == "WSS10010" else RouterErrorCode.INVALID_REQUEST,
            str(header.get("rsp_msg") or "NAMUH 실시간 구독이 거부되었습니다."),
            details={"provider_code": code},
        )
    return {}


async def _handle_kis_control_message(
    sender: Callable[[str], Awaitable[Any]], message: str
) -> dict[str, Any] | None:
    if not message.startswith("{"):
        return None
    try:
        payload = json.loads(message)
    except ValueError:
        return {}
    header = payload.get("header") if isinstance(payload, dict) else None
    if isinstance(header, dict) and header.get("tr_id") == "PINGPONG":
        await sender(message)
        return {}
    body = payload.get("body") if isinstance(payload, dict) else None
    if isinstance(body, dict):
        result_code = str(body.get("rt_cd") or "")
        if result_code not in {"", "0"}:
            provider_code = str(body.get("msg_cd") or "")
            message_text = str(
                body.get("msg1") or "KIS 실시간 구독이 거부되었습니다."
            )
            raise RouterError(
                (
                    RouterErrorCode.RATE_LIMITED
                    if any(
                        marker in f"{provider_code} {message_text}".upper()
                        for marker in ("LIMIT", "MAX", "초과", "한도")
                    )
                    else RouterErrorCode.INVALID_REQUEST
                ),
                message_text,
                details={"provider_code": provider_code},
            )
    output = body.get("output") if isinstance(body, dict) else None
    return {
        "tr_id": header.get("tr_id") if isinstance(header, dict) else None,
        "key": output.get("key") if isinstance(output, dict) else None,
        "iv": output.get("iv") if isinstance(output, dict) else None,
    }


def _parse_kis_market_message(
    message: str,
    *,
    generation: int,
    encryption: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Parse the first event in a KIS frame for backwards compatibility."""
    events = _parse_kis_market_messages(
        message,
        generation=generation,
        encryption=encryption,
    )
    return events[0] if events else None


def _parse_kis_market_messages(
    message: str,
    *,
    generation: int,
    encryption: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Parse every market event carried by one KIS WebSocket frame."""
    parts = message.split("|", 3)
    if len(parts) != 4 or parts[0] not in {"0", "1"}:
        return []
    tr_id = parts[1]
    try:
        count = max(1, int(parts[2]))
    except ValueError:
        count = 1
    values = parts[3].split("^")
    received_at = datetime.now(SEOUL)
    if tr_id in {"H0STCNI0", "H0STCNI9"}:
        secret = (encryption or {}).get(tr_id)
        if parts[0] != "1" or secret is None:
            return []
        decrypted = _aes_decrypt(parts[3], key=secret[0], iv=secret[1])
        row = decrypted.split("^")
        if len(row) < 26:
            return []
        return [
            {
                "event_type": "account_notice",
                "tr_id": tr_id,
                "code": row[8],
                "notice": {
                    "provider": "kis",
                    "provider_order_id": row[2],
                    "original_order_id": row[3],
                    "side_code": row[4],
                    "change_code": row[5],
                    "order_kind": row[6],
                    "code": row[8],
                    "quantity": row[9],
                    "price": row[10],
                    "time": row[11],
                    "rejected": row[12],
                    "execution_code": row[13],
                    "accepted": row[14],
                    "branch": row[15],
                    "ordered_quantity": row[16],
                    "route_code": row[19],
                },
                "received_at": received_at.isoformat(),
            },
        ]
    if tr_id in _QUOTE_TR_IDS.values():
        width = _QUOTE_FIELD_COUNT
        venue = _venue_for_tr(tr_id)
        events: list[dict[str, Any]] = []
        available = (len(values) + width - 1) // width
        for index in range(min(count, available)):
            row = values[index * width : (index + 1) * width]
            if len(row) < 3:
                continue
            price = _positive_decimal(row[2])
            if price is None:
                continue
            market_time = _market_datetime(row[1], received_at)
            events.append(
                {
                    "event_type": "quote",
                    "tr_id": tr_id,
                    "code": row[0],
                    "quote": {
                        "instrument": {
                            "code": row[0],
                            "country": "KR",
                            "asset": "EQUITY",
                        },
                        "price": str(price),
                        "venue": venue.value,
                        "provider": "kis",
                        "market_time": market_time.isoformat(),
                        "received_at": received_at.isoformat(),
                        "correlation_id": f"ws-{generation}-{uuid4().hex}",
                        "delayed": False,
                        "stale": False,
                        "connection_generation": generation,
                    },
                    "received_at": received_at.isoformat(),
                }
            )
        return events
    if tr_id in _ORDERBOOK_TR_IDS.values():
        width = _ORDERBOOK_FIELD_COUNTS[tr_id]
        events = []
        available = (len(values) + width - 1) // width
        for index in range(min(count, available)):
            row = values[index * width : (index + 1) * width]
            if len(row) < 43:
                continue
            asks = _levels(row[3:13], row[23:33])
            bids = _levels(row[13:23], row[33:43])
            events.append(
                {
                    "event_type": "orderbook",
                    "tr_id": tr_id,
                    "code": row[0],
                    "orderbook": {
                        "instrument": {
                            "code": row[0],
                            "country": "KR",
                            "asset": "EQUITY",
                        },
                        "asks": asks,
                        "bids": bids,
                        "venue": _venue_for_tr(tr_id).value,
                        "provider": "kis",
                        "market_time": _market_datetime(
                            row[1], received_at
                        ).isoformat(),
                        "received_at": received_at.isoformat(),
                        "correlation_id": f"ws-{generation}-{uuid4().hex}",
                        "stale": False,
                        "connection_generation": generation,
                    },
                    "received_at": received_at.isoformat(),
                }
            )
        return events
    return []


def _parse_namuh_market_message(
    message: str,
    *,
    generation: int,
) -> dict[str, Any] | None:
    try:
        payload = json.loads(message)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    header = payload.get("header")
    body = payload.get("body")
    if not isinstance(header, dict) or not isinstance(body, dict):
        return None
    tr_id = str(header.get("tr_cd") or "")
    if not tr_id or "tr_type" in header:
        return None
    received_at = datetime.now(SEOUL)
    if tr_id in _NAMUH_QUOTE_TR_IDS.values():
        code = str(body.get("code") or header.get("tr_key") or "").strip()
        price = _positive_decimal(body.get("price"))
        if not code or price is None:
            return None
        venue = _namuh_venue_for_tr(tr_id)
        return {
            "event_type": "quote",
            "tr_id": tr_id,
            "code": code,
            "quote": {
                "instrument": {"code": code, "country": "KR", "asset": "EQUITY"},
                "price": str(price),
                "venue": venue.value,
                "provider": "namuh",
                "market_time": _market_datetime(
                    str(body.get("time") or ""), received_at
                ).isoformat(),
                "received_at": received_at.isoformat(),
                "correlation_id": f"ws-{generation}-{uuid4().hex}",
                "delayed": False,
                "stale": False,
                "connection_generation": generation,
            },
            "received_at": received_at.isoformat(),
        }
    if tr_id in _NAMUH_ORDERBOOK_TR_IDS.values():
        code = str(body.get("code") or header.get("tr_key") or "").strip()
        if not code:
            return None
        ask_names = ["offer", "P_offer", "S_offer"] + [
            f"S{level}_offer" for level in range(4, 11)
        ]
        bid_names = ["bid", "P_bid", "S_bid"] + [
            f"S{level}_bid" for level in range(4, 11)
        ]
        asks = _named_levels(body, ask_names, [f"{name}rem" for name in ask_names])
        bids = _named_levels(body, bid_names, [f"{name}rem" for name in bid_names])
        return {
            "event_type": "orderbook",
            "tr_id": tr_id,
            "code": code,
            "orderbook": {
                "instrument": {"code": code, "country": "KR", "asset": "EQUITY"},
                "asks": asks,
                "bids": bids,
                "venue": _namuh_venue_for_tr(tr_id).value,
                "provider": "namuh",
                "market_time": _market_datetime(
                    str(body.get("hotime") or ""), received_at
                ).isoformat(),
                "received_at": received_at.isoformat(),
                "correlation_id": f"ws-{generation}-{uuid4().hex}",
                "stale": False,
                "connection_generation": generation,
            },
            "received_at": received_at.isoformat(),
        }
    if tr_id in {"d2", "d3"}:
        code = str(body.get("issuecd") or "").strip()
        execution = tr_id == "d2"
        return {
            "event_type": "account_notice",
            "tr_id": tr_id,
            "code": code,
            "notice": {
                "provider": "namuh",
                "provider_order_id": _normalize_namuh_order_id(body.get("orderno")),
                "original_order_id": _normalize_namuh_order_id(body.get("orgordno")),
                "side_code": str(body.get("slbygb") or ""),
                "change_code": str(body.get("ordercd") or ""),
                "order_kind": str(body.get("order_type") or ""),
                "code": code,
                "quantity": str(
                    (body.get("concgty") if execution else body.get("ordergty"))
                    or "0"
                ),
                "price": str(
                    (body.get("concprc") if execution else body.get("orderprc"))
                    or "0"
                ),
                "time": str(
                    (body.get("conctime") if execution else body.get("order_time"))
                    or ""
                ),
                "rejected": str(body.get("rejgb") or "0"),
                "execution_code": "2" if execution else "0",
                "accepted": "1",
                "route_code": str(body.get("rmt_mkt_cd") or ""),
            },
            "received_at": received_at.isoformat(),
        }
    return None


def _aes_decrypt(value: str, *, key: str, iv: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    decryptor = Cipher(
        algorithms.AES(key.encode("utf-8")),
        modes.CBC(iv.encode("utf-8")),
    ).decryptor()
    padded = decryptor.update(base64.b64decode(value)) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def _levels(prices: list[str], quantities: list[str]) -> list[dict[str, str]]:
    result = []
    for price_text, quantity_text in zip(prices, quantities, strict=False):
        price = _positive_decimal(price_text)
        quantity = _positive_decimal(quantity_text, allow_zero=True)
        if price is not None and quantity is not None:
            result.append({"price": str(price), "quantity": str(quantity)})
    return result


def _named_levels(
    body: dict[str, Any], prices: list[str], quantities: list[str]
) -> list[dict[str, str]]:
    return _levels(
        [str(body.get(name) or "") for name in prices],
        [str(body.get(name) or "") for name in quantities],
    )


def _positive_decimal(value: object, *, allow_zero: bool = False) -> Decimal | None:
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if result < 0 or (result == 0 and not allow_zero):
        return None
    return result


def _normalize_namuh_order_id(value: object) -> str:
    text = str(value or "").strip()
    return str(int(text)) if text.isdigit() else text


def _venue_for_tr(tr_id: str) -> Venue:
    if tr_id.startswith("H0NX"):
        return Venue.NXT
    if tr_id.startswith("H0UN"):
        return Venue.INTEGRATED
    return Venue.KRX


def _namuh_venue_for_tr(tr_id: str) -> Venue:
    if tr_id.startswith("n"):
        return Venue.NXT
    if tr_id.startswith("m"):
        return Venue.INTEGRATED
    return Venue.KRX


def _market_datetime(value: str, received_at: datetime) -> datetime:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) < 6:
        return received_at
    try:
        return received_at.replace(
            hour=int(digits[:2]),
            minute=int(digits[2:4]),
            second=int(digits[4:6]),
            microsecond=0,
        )
    except ValueError:
        return received_at
