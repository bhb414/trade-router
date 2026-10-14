from trade_router import StrategyConfig, TradeRouter

strategy = StrategyConfig(
    id="sample",
    trading_account="paper-main",
    market_data="simulated-main",
)

with TradeRouter(strategy=strategy) as router:
    print(router.market.quote(code="005930", venue="INTEGRATED"))
