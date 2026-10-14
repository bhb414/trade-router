# Trade Router

Trade Router is a local, versioned broker gateway and Python SDK for Korean equities.
It normalizes market data, account queries, cash orders, and real-time events across
Korea Investment & Securities (KIS), NH Investment & Securities NAMUH PLUG, and a
deterministic simulation adapter used for safe development.

The daemon owns credentials, OAuth tokens, rate limits, WebSocket connections,
idempotency, risk checks, and the durable order ledger. Strategy code only receives
registered aliases and normalized models.

## Safety defaults

- The API binds to `127.0.0.1` and requires an installation token.
- Secrets and cached broker tokens are encrypted with Windows DPAPI for the current user.
- Live orders require both the account and strategy to be armed.
- Every order requires a stable `client_order_id`.
- An uncertain submission is never retried automatically.
- Provider credentials and raw account numbers are redacted from logs and SQLite.

## Install for development

```powershell
cd H:\Hyunbin\snacks\trade-router
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
trade-router daemon start
trade-router daemon status
```

Use `TRADE_ROUTER_HOME` to move the runtime directory. By default it is stored in
the current Windows user's local application-data directory.

## Configure a safe simulation account

```powershell
trade-router profile add simulated-main --provider simulated --environment paper
trade-router account add paper-main --profile simulated-main --account-number PAPER-001
trade-router strategy register sample --account paper-main --market-data simulated-main
trade-router account arm paper-main
trade-router strategy arm sample
```

## Python SDK

```python
import asyncio
from trade_router import AsyncTradeRouter, StrategyConfig

STRATEGY = StrategyConfig(
    id="sample",
    trading_account="paper-main",
    market_data="simulated-main",
)

async def main() -> None:
    async with AsyncTradeRouter(strategy=STRATEGY) as router:
        quote = await router.market.quote(code="005930", venue="INTEGRATED")
        print(quote)

        result = await router.orders.market_buy(
            code="005930",
            quantity=1,
            client_order_id="sample-005930-20261014-130300-buy",
        )
        print(result)

asyncio.run(main())
```

`TradeRouter` provides the same surface for synchronous scripts. The sync wrapper
must not be invoked from an already-running asyncio event loop.

## Provider environments

KIS uses its official live and virtual-trading hosts. NAMUH PLUG uses the official
live host `api.nhplug.com:8443` and mock host `moapi.nhplug.com:8443`; OAuth tokens
are issued by the live authentication host for both environments. N2-branded keys
can be selected through profile settings without exposing custom credential hosts.
`BrokerCapabilities.credential_scope` reports whether credentials are absent,
environment-specific, or shared across a provider's supported environments. KIS
reports environment-specific credentials; NAMUH reports provider-wide credentials.

Actual live orders are never executed by the test suite.

The default suite uses simulated REST/WebSocket behavior. Optional read-only live checks
run when `TRADE_ROUTER_TEST_KIS_APP_KEY` / `_APP_SECRET` or
`TRADE_ROUTER_TEST_NAMUH_APP_KEY` / `_APP_SECRET` are set. They request only a
quote, order book, and daily bars; they never call an order endpoint.

## Configure KIS or NAMUH

The CLI prompts for secrets without echoing them. They are written only to the
current Windows user's DPAPI vault.

```powershell
trade-router profile add kis-main --provider kis --environment live
trade-router account add kis-account --profile kis-main

trade-router profile add nh-paper --provider namuh --environment paper
trade-router account add nh-paper-account --profile nh-paper
```

NAMUH's paper profile uses `moapi.nhplug.com` for API calls and the live OAuth host
for token issuance, as specified by the official SDK. KIS paper profiles use the KIS
virtual-trading host.

Before a live order can leave the daemon, arm both registrations explicitly:

```powershell
trade-router account arm kis-account
trade-router strategy arm sample
```

Disarming either registration blocks subsequent submissions. It does not liquidate
positions or silently cancel existing orders.

## Documentation

- [Architecture](docs/architecture.md)
- [Local HTTP and WebSocket API](docs/api.md)
- [Provider adapter contract](docs/provider-adapters.md)

The provider implementations follow the official [KIS Developers](https://apiportal.koreainvestment.com/docs)
and [NAMUH PLUG](https://www.nhplug.com/apiservice) interfaces. Live provider checks
are read-only; automated tests never submit a live order.
