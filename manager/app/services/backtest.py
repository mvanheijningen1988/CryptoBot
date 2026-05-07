"""Simple back-test runner for evaluating strategy performance."""
from __future__ import annotations

import random

from common import BotConfig, SimulatedExchange, StrategyState, StaticGridStrategy


def run_backtest(config: BotConfig, prices: list[float] | None = None) -> dict:
    """
    Run a back-test using a bot configuration over a list of prices.

    If prices is omitted, 500 random-walk prices are generated from
    ``config.start_price``.

    :param config: Full bot configuration (grid, budget, market settings).
    :param prices: Optional list of historical prices to replay.
    :return: Dict with initial_equity_quote, final_equity_quote,
        total_pnl_quote, and trades_executed.
    """
    if not prices:
        prices = []
        p = config.start_price or (config.grid.lower_price + config.grid.upper_price) / 2
        for _ in range(500):
            p = max(0.0001, p * (1 + random.uniform(-0.01, 0.01)))
            prices.append(p)

    exchange = SimulatedExchange(config.budget)
    strategy = StaticGridStrategy(config.grid)
    state = StrategyState()

    if not prices:
        raise ValueError("No prices available for backtest.")

    initial_price = prices[0]
    initial_equity = exchange.quote_balance + exchange.base_balance * initial_price
    trades = 0

    for price in prices:
        signals = strategy.on_price(price, state)
        for signal in signals:
            success = exchange.execute(signal, price)
            if success:
                trades += 1

    final_price = prices[-1]
    final_equity = exchange.quote_balance + exchange.base_balance * final_price
    total_pnl = final_equity - initial_equity

    return {
        "initial_equity_quote": initial_equity,
        "final_equity_quote": final_equity,
        "total_pnl_quote": total_pnl,
        "trades_executed": trades,
    }
