"""Simple back-test runner for evaluating strategy performance."""
from __future__ import annotations

import random

from common import BotConfig, SimulatedExchange, StrategyState, StaticGridStrategy, TradeSignal


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

    # Prime the strategy with the first price (sets up initial orders)
    strategy.on_price(prices[0], state)

    # Place initial limit orders on the simulated exchange
    for idx, side in state.open_orders.items():
        limit_price = strategy.levels[idx]
        exchange.place_limit_order(
            order_id=f"lvl_{idx}",
            side=side,
            quote_amount=config.grid.order_size_quote,
            limit_price=limit_price,
            level_index=idx,
        )

    for price in prices[1:]:
        exchange.price = price

        # Process fills until stable (handles cascading)
        while True:
            fills = exchange.get_filled_orders()
            if not fills:
                break
            for fill in fills:
                idx = fill["level_index"]
                state.open_orders.pop(idx, None)
                signal = TradeSignal(
                    side=fill["side"],
                    quote_amount=fill["quote_amount"],
                    level_index=idx,
                )
                orders_before = set(state.open_orders.keys())
                strategy.confirm_fill(signal, state)
                for new_idx in state.open_orders:
                    if new_idx not in orders_before:
                        exchange.place_limit_order(
                            order_id=f"lvl_{new_idx}",
                            side=state.open_orders[new_idx],
                            quote_amount=config.grid.order_size_quote,
                            limit_price=strategy.levels[new_idx],
                            level_index=new_idx,
                        )
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
