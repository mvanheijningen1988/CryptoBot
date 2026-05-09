"""Pre-trade profitability analysis for static grid configurations."""
from __future__ import annotations

from common import GridConfig


def build_static_grid_profit_preview(grid: GridConfig, fee_rate: float) -> dict:
    """
    Compute per-trade profit for each adjacent level pair in a grid.

    Returns a summary dict indicating whether every trade cycle is
    profitable after accounting for fee_rate (buy + sell fees), along
    with the full list of grid levels and per-trade details.

    :param grid: Grid configuration with price range, levels, and order size.
    :param fee_rate: The exchange fee rate per trade (e.g. 0.0025 for 0.25%).
    :return: Dict with profitability summary, step sizes, per-trade profit
             stats, and a ``trades`` list of individual level pairs.
    """
    step = (grid.upper_price - grid.lower_price) / (grid.levels - 1)
    levels = [grid.lower_price + i * step for i in range(grid.levels)]

    trades: list[dict] = []
    per_trade_profits: list[float] = []
    per_trade_fees: list[float] = []
    profitable_count = 0

    for i in range(len(levels) - 1):
        buy_price = levels[i]
        sell_price = levels[i + 1]

        quote_spent = grid.order_size_quote
        quote_received_before_fees = quote_spent * (sell_price / buy_price)

        buy_fee = quote_spent * fee_rate
        sell_fee = quote_received_before_fees * fee_rate
        total_fees = buy_fee + sell_fee
        net_profit = quote_received_before_fees - quote_spent - total_fees

        per_trade_profits.append(net_profit)
        per_trade_fees.append(total_fees)
        if net_profit > 0:
            profitable_count += 1

        trades.append({
            "level": i,
            "buy_price": round(buy_price, 6),
            "sell_price": round(sell_price, 6),
            "order_size_quote": round(quote_spent, 6),
            "buy_fee_quote": round(buy_fee, 6),
            "sell_fee_quote": round(sell_fee, 6),
            "total_fees_quote": round(total_fees, 6),
            "net_profit": round(net_profit, 6),
            "profitable": net_profit > 0,
        })

    min_profit = min(per_trade_profits)
    max_profit = max(per_trade_profits)
    avg_profit = sum(per_trade_profits) / len(per_trade_profits)
    min_fee = min(per_trade_fees)
    max_fee = max(per_trade_fees)
    avg_fee = sum(per_trade_fees) / len(per_trade_fees)
    total_fee_cost = sum(per_trade_fees)

    is_profitable = min_profit > 0

    return {
        "is_profitable": is_profitable,
        "step_size": step,
        "step_percent": (step / levels[0]) * 100,
        "profit_per_trade_quote_min": min_profit,
        "profit_per_trade_quote_avg": avg_profit,
        "profit_per_trade_quote_max": max_profit,
        "profitable_trades": profitable_count,
        "total_trade_paths": len(per_trade_profits),
        "fee_rate": fee_rate,
        "fee_cost_per_trade_quote_min": min_fee,
        "fee_cost_per_trade_quote_avg": avg_fee,
        "fee_cost_per_trade_quote_max": max_fee,
        "total_fee_cost_quote": total_fee_cost,
        "levels": [round(lv, 6) for lv in levels],
        "trades": trades,
    }
