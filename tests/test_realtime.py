from __future__ import annotations

import asyncio
import base64
import json

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from trade_router import Venue
from trade_router.errors import RouterError, RouterErrorCode
from trade_router.subscriptions import SubscriptionLease
from trade_router_studio.event_bus import StrategyEventBus
from trade_router_studio.realtime import (
    RealtimeHub,
    _handle_kis_control_message,
    _is_nxt_ineligible_rejection,
    _parse_kis_market_message,
    _parse_kis_market_messages,
    _parse_namuh_market_message,
)


def test_kis_control_error_is_not_treated_as_a_subscription_success() -> None:
    async def run() -> None:
        async def sender(_: str) -> None:
            raise AssertionError("An error response must not be echoed.")

        try:
            await _handle_kis_control_message(
                sender,
                json.dumps(
                    {
                        "header": {"tr_id": "H0STCNT0"},
                        "body": {
                            "rt_cd": "1",
                            "msg_cd": "OPSQ9999",
                            "msg1": "subscription rejected",
                        },
                    }
                ),
            )
        except RouterError as exc:
            assert exc.code is RouterErrorCode.INVALID_REQUEST
            assert exc.details == {"provider_code": "OPSQ9999"}
        else:
            raise AssertionError("KIS WebSocket rejection was ignored.")

    asyncio.run(run())


def test_only_explicit_nxt_ineligible_errors_are_quarantinable() -> None:
    unsupported = RouterError(
        RouterErrorCode.INVALID_REQUEST,
        "해당 종목은 NXT 거래 대상 종목이 아닙니다.",
    )
    temporary = RouterError(
        RouterErrorCode.INVALID_REQUEST,
        "temporary subscription error",
    )
    assert _is_nxt_ineligible_rejection("H0NXCNT0", unsupported) is True
    assert _is_nxt_ineligible_rejection("H0STCNT0", unsupported) is False
    assert _is_nxt_ineligible_rejection("H0NXCNT0", temporary) is False


def test_kis_quote_and_orderbook_frames_are_normalized() -> None:
    quote_values = ["005930", "093001", "73000"] + ["0"] * 44
    quote = _parse_kis_market_message(
        "0|H0STCNT0|1|" + "^".join(quote_values), generation=3
    )
    assert quote is not None
    assert quote["event_type"] == "quote"
    assert quote["quote"]["price"] == "73000"
    assert quote["quote"]["connection_generation"] == 3

    orderbook_values = ["005930", "093002", "0"]
    orderbook_values += [str(73100 + value * 100) for value in range(10)]
    orderbook_values += [str(72900 - value * 100) for value in range(10)]
    orderbook_values += [str(10 + value) for value in range(10)]
    orderbook_values += [str(20 + value) for value in range(10)]
    orderbook_values += ["0"] * (63 - len(orderbook_values))
    orderbook = _parse_kis_market_message(
        "0|H0STASP0|1|" + "^".join(orderbook_values), generation=4
    )
    assert orderbook is not None
    assert orderbook["event_type"] == "orderbook"
    assert len(orderbook["orderbook"]["asks"]) == 10
    assert orderbook["orderbook"]["bids"][0] == {
        "price": "72900",
        "quantity": "20",
    }

    key = "0123456789abcdef0123456789abcdef"
    iv = "0123456789abcdef"
    notice_values = ["user", "acct", "broker-1", "", "02", "", "", "", "005930", "1", "73000", "093003", "0", "2", "1", "", "1", "name", "", "KRX", "", "", "", "", "삼성전자", "73000"]
    padder = PKCS7(128).padder()
    padded = padder.update("^".join(notice_values).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    encoded = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()
    notice = _parse_kis_market_message(
        f"1|H0STCNI0|1|{encoded}",
        generation=5,
        encryption={"H0STCNI0": (key, iv)},
    )
    assert notice is not None
    assert notice["event_type"] == "account_notice"
    assert notice["notice"]["provider_order_id"] == "broker-1"
    assert notice["notice"]["quantity"] == "1"


def test_kis_multi_record_quote_and_orderbook_frames_are_all_normalized() -> None:
    first_quote = ["005930", "093001", "73000"] + ["0"] * 44
    second_quote = ["000660", "093002", "180000"] + ["0"] * 44
    quotes = _parse_kis_market_messages(
        "0|H0STCNT0|2|" + "^".join(first_quote + second_quote),
        generation=9,
    )
    assert [(event["code"], event["quote"]["price"]) for event in quotes] == [
        ("005930", "73000"),
        ("000660", "180000"),
    ]

    def orderbook_row(code: str, base: int) -> list[str]:
        values = [code, "093003", "0"]
        values += [str(base + 100 + value * 100) for value in range(10)]
        values += [str(base - 100 - value * 100) for value in range(10)]
        values += [str(10 + value) for value in range(10)]
        values += [str(20 + value) for value in range(10)]
        return values + ["0"] * (63 - len(values))

    orderbooks = _parse_kis_market_messages(
        "0|H0STASP0|2|"
        + "^".join(
            orderbook_row("005930", 73000)
            + orderbook_row("000660", 180000)
        ),
        generation=10,
    )
    assert [event["code"] for event in orderbooks] == ["005930", "000660"]
    assert orderbooks[1]["orderbook"]["bids"][0] == {
        "price": "179900",
        "quantity": "20",
    }


def test_strategy_event_bus_isolated_queues_and_bounded_drop() -> None:
    async def run() -> None:
        bus = StrategyEventBus(queue_size=2)
        await bus.publish("a.py", {"event_type": "quote", "sequence": 1})
        await bus.publish("a.py", {"event_type": "quote", "sequence": 2})
        await bus.publish("a.py", {"event_type": "quote", "sequence": 3})
        await bus.publish("b.py", {"event_type": "fill", "sequence": 7})
        assert (await bus.next_event("a.py", timeout=0.1))["sequence"] == 2
        assert (await bus.next_event("a.py", timeout=0.1))["sequence"] == 3
        assert (await bus.next_event("b.py", timeout=0.1))["sequence"] == 7
        assert await bus.next_event("a.py", timeout=0.01) is None

    asyncio.run(run())


def test_namuh_quote_orderbook_and_fill_frames_are_normalized() -> None:
    quote = _parse_namuh_market_message(
        json.dumps(
            {
                "header": {"tr_cd": "mc", "tr_key": "005930"},
                "body": {"code": "005930", "time": "09:30:01", "price": "73000"},
            }
        ),
        generation=6,
    )
    assert quote is not None
    assert quote["quote"]["provider"] == "namuh"
    assert quote["quote"]["venue"] == "INTEGRATED"

    body = {"code": "005930", "hotime": "09:30:02"}
    ask_names = ["offer", "P_offer", "S_offer"] + [
        f"S{level}_offer" for level in range(4, 11)
    ]
    bid_names = ["bid", "P_bid", "S_bid"] + [
        f"S{level}_bid" for level in range(4, 11)
    ]
    for index, name in enumerate(ask_names):
        body[name] = str(73100 + index * 100)
        body[f"{name}rem"] = str(index + 1)
    for index, name in enumerate(bid_names):
        body[name] = str(72900 - index * 100)
        body[f"{name}rem"] = str(index + 11)
    orderbook = _parse_namuh_market_message(
        json.dumps({"header": {"tr_cd": "mb"}, "body": body}),
        generation=7,
    )
    assert orderbook is not None
    assert len(orderbook["orderbook"]["asks"]) == 10
    assert orderbook["orderbook"]["bids"][0] == {
        "price": "72900",
        "quantity": "11",
    }

    fill = _parse_namuh_market_message(
        json.dumps(
            {
                "header": {"tr_cd": "d2"},
                "body": {
                    "orderno": "0000000030",
                    "issuecd": "005930",
                    "concgty": "1",
                    "concprc": "73000",
                    "conctime": "093003",
                    "rejgb": "0",
                },
            }
        ),
        generation=8,
    )
    assert fill is not None
    assert fill["event_type"] == "account_notice"
    assert fill["notice"]["execution_code"] == "2"
    assert fill["notice"]["provider_order_id"] == "30"


def test_realtime_hub_reconnects_resubscribes_and_fans_out() -> None:
    class FakeRuntime:
        def __init__(self) -> None:
            self.consumers: dict[str, set[str]] = {}

        def realtime_connection(self, _: str) -> dict[str, object]:
            return {
                "id": "connection-a",
                "provider": "kis",
                "rate_scope": "shared-key-live",
                "url": "ws://fake",
                "control_interval": 0.01,
            }

        async def realtime_approval_key(self, _: str) -> str:
            return "approval"

        async def reserve_realtime_subscription(
            self, _: str, *, consumer_id: str, subscription_key: str
        ) -> SubscriptionLease:
            consumers = self.consumers.setdefault(subscription_key, set())
            created = not consumers
            consumers.add(consumer_id)
            return SubscriptionLease(
                "shared-key-live", "session-1", subscription_key, created
            )

        async def release_realtime_subscription(
            self, lease: SubscriptionLease, *, consumer_id: str
        ) -> bool:
            consumers = self.consumers[lease.subscription_key]
            consumers.remove(consumer_id)
            return not consumers

    first_quote_values = ["005930", "093001", "73000"] + ["0"] * 44
    second_quote_values = ["005930", "093002", "73100"] + ["0"] * 44
    quote_frame = "0|H0STCNT0|2|" + "^".join(
        first_quote_values + second_quote_values
    )

    class FakeSocket:
        def __init__(self, *, disconnect_after_quote: bool) -> None:
            self.disconnect_after_quote = disconnect_after_quote
            self.sent: list[str] = []
            self.delivered = False
            self.wait_forever = asyncio.Event()

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def recv(self) -> str:
            if not self.delivered:
                self.delivered = True
                return quote_frame
            if self.disconnect_after_quote:
                # A KRX INVALID_REQUEST is not proof that the instrument is
                # permanently unsupported. It must keep the normal reconnect
                # and retry behavior instead of quarantining the stream.
                return json.dumps(
                    {
                        "header": {
                            "tr_id": "H0STCNT0",
                            "tr_key": "005930",
                        },
                        "body": {
                            "rt_cd": "1",
                            "msg_cd": "OPSQ9999",
                            "msg1": "temporary subscription error",
                        },
                    }
                )
            await self.wait_forever.wait()
            raise AssertionError("unreachable")

    class FakeContext:
        def __init__(self, socket: FakeSocket) -> None:
            self.socket = socket

        async def __aenter__(self) -> FakeSocket:
            return self.socket

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeConnector:
        def __init__(self) -> None:
            self.sockets: list[FakeSocket] = []

        def __call__(self, *_: object, **__: object) -> FakeContext:
            socket = FakeSocket(disconnect_after_quote=not self.sockets)
            self.sockets.append(socket)
            return FakeContext(socket)

    async def run() -> None:
        runtime = FakeRuntime()
        connector = FakeConnector()
        bus = StrategyEventBus()
        hub = RealtimeHub(runtime, bus, connector=connector)  # type: ignore[arg-type]
        first = await hub.subscribe(
            strategy_name="first.py",
            source="source-a",
            source_alias="primary",
            kind="QUOTE",
            code="005930",
            venue=Venue.KRX,
        )
        second = await hub.subscribe(
            strategy_name="second.py",
            source="source-b",
            source_alias="secondary",
            kind="QUOTE",
            code="005930",
            venue=Venue.KRX,
        )
        try:
            assert first["shared"] is False
            assert second["shared"] is True
            first_event = await bus.next_event("first.py", timeout=4)
            second_event = await bus.next_event("second.py", timeout=4)
            assert first_event is not None and second_event is not None
            assert first_event["quote"]["connection_generation"] == 1
            assert second_event["source_alias"] == "secondary"

            first_same_frame = await bus.next_event("first.py", timeout=1)
            second_same_frame = await bus.next_event("second.py", timeout=1)
            assert first_same_frame is not None and second_same_frame is not None
            assert first_same_frame["quote"]["price"] == "73100"
            assert first_same_frame["quote"]["connection_generation"] == 1
            assert second_same_frame["quote"]["price"] == "73100"

            reconnected = await bus.next_event("first.py", timeout=5)
            assert reconnected is not None
            assert reconnected["quote"]["connection_generation"] == 2
            assert len(connector.sockets) == 2
            for socket in connector.sockets:
                controls = [json.loads(item) for item in socket.sent]
                subscribes = [
                    item
                    for item in controls
                    if item["header"].get("tr_type") == "1"
                ]
                assert len(subscribes) == 1
                assert subscribes[0]["body"]["input"] == {
                    "tr_id": "H0STCNT0",
                    "tr_key": "005930",
                }
        finally:
            await hub.close()

    asyncio.run(run())


def test_realtime_hub_quarantines_rejected_nxt_leg_and_keeps_krx() -> None:
    class FakeRuntime:
        def __init__(self) -> None:
            self.consumers: dict[str, set[str]] = {}

        def realtime_connection(self, _: str) -> dict[str, object]:
            return {
                "id": "connection-a",
                "provider": "kis",
                "rate_scope": "shared-key-live",
                "url": "ws://fake",
                "control_interval": 0.001,
            }

        async def realtime_approval_key(self, _: str) -> str:
            return "approval"

        async def reserve_realtime_subscription(
            self, _: str, *, consumer_id: str, subscription_key: str
        ) -> SubscriptionLease:
            consumers = self.consumers.setdefault(subscription_key, set())
            created = not consumers
            consumers.add(consumer_id)
            return SubscriptionLease(
                "shared-key-live", "session-1", subscription_key, created
            )

        async def release_realtime_subscription(
            self, lease: SubscriptionLease, *, consumer_id: str
        ) -> bool:
            consumers = self.consumers[lease.subscription_key]
            consumers.remove(consumer_id)
            return not consumers

    quote_values = ["005930", "093001", "73000"] + ["0"] * 44
    quote_frame = "0|H0STCNT0|1|" + "^".join(quote_values)

    class FakeSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.controls_changed = asyncio.Event()
            self.responses = 0
            self.wait_forever = asyncio.Event()

        async def send(self, message: str) -> None:
            self.sent.append(message)
            self.controls_changed.set()

        def subscribed_tr_ids(self) -> set[str]:
            return {
                str(item["body"]["input"]["tr_id"])
                for raw in self.sent
                for item in (json.loads(raw),)
                if item["header"].get("tr_type") == "1"
            }

        async def recv(self) -> str:
            while not {"H0STCNT0", "H0NXCNT0"} <= self.subscribed_tr_ids():
                await self.controls_changed.wait()
                self.controls_changed.clear()
            if self.responses == 0:
                self.responses += 1
                return json.dumps(
                    {
                        "header": {
                            "tr_id": "H0NXCNT0",
                            "tr_key": "005930",
                        },
                        "body": {
                            "rt_cd": "1",
                            "msg_cd": "OPSQ9999",
                            "msg1": "NXT unsupported instrument",
                        },
                    }
                )
            if self.responses == 1:
                self.responses += 1
                return quote_frame
            await self.wait_forever.wait()
            raise AssertionError("unreachable")

    class FakeContext:
        def __init__(self, socket: FakeSocket) -> None:
            self.socket = socket

        async def __aenter__(self) -> FakeSocket:
            return self.socket

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeConnector:
        def __init__(self) -> None:
            self.sockets: list[FakeSocket] = []

        def __call__(self, *_: object, **__: object) -> FakeContext:
            socket = FakeSocket()
            self.sockets.append(socket)
            return FakeContext(socket)

    async def run() -> None:
        runtime = FakeRuntime()
        connector = FakeConnector()
        bus = StrategyEventBus()
        hub = RealtimeHub(runtime, bus, connector=connector)  # type: ignore[arg-type]
        krx = await hub.subscribe(
            strategy_name="strategy.py",
            source="source-a",
            source_alias="primary",
            kind="QUOTE",
            code="005930",
            venue=Venue.KRX,
        )
        nxt = await hub.subscribe(
            strategy_name="strategy.py",
            source="source-a",
            source_alias="primary",
            kind="QUOTE",
            code="005930",
            venue=Venue.NXT,
        )
        try:
            event = await bus.next_event("strategy.py", timeout=4)
            assert event is not None
            assert event["quote"]["venue"] == "KRX"
            assert event["quote"]["price"] == "73000"
            assert len(connector.sockets) == 1

            snapshot = await hub.snapshot()
            assert snapshot[0]["state"] == "connected"
            assert snapshot[0]["subscriptions"] == 1
            assert "해당 구독만 제외" in snapshot[0]["message"]
            assert await hub.unsubscribe(
                nxt["id"], strategy_name="strategy.py"
            ) is False
            assert await hub.unsubscribe(
                krx["id"], strategy_name="strategy.py"
            ) is True
        finally:
            await hub.close()

    asyncio.run(run())
