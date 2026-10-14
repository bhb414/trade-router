import asyncio

from trade_router import AsyncTradeRouter, StrategyConfig

STRATEGY = StrategyConfig(
    id="sample",
    trading_account="paper-main",
    market_data="fake-main",
)


async def main() -> None:
    async with AsyncTradeRouter(strategy=STRATEGY) as router:
        quote = await router.market.quote(code="005930", venue="INTEGRATED")
        print(quote)

        result = await router.orders.market_buy(
            code="005930",
            quantity=1,
            client_order_id="sample-005930-unique-buy-key",
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
