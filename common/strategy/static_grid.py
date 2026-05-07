"""Static grid trading strategy.

Implements a proper spot grid bot.  On startup the grid places a
single pending buy order at the level directly below the current
price.  Order lifecycle is two-phase:

1. **on_price** detects which open orders have been hit and returns
   ``TradeSignal`` objects, but does **not** yet place consequent
   orders (sells after buys, cascade buys).
2. **confirm_fill** is called by the runner *after the exchange has
   executed the trade*.  Only then are the follow-up orders placed.

This ensures sells are never placed without the bot actually holding
the base currency from a confirmed buy.
"""
from __future__ import annotations

from typing import List

from common.models import GridConfig, TradeSignal
from common.strategy.base import Strategy, StrategyState


class StaticGridStrategy(Strategy):
    """Grid strategy with fixed, evenly spaced price levels.

    On initialisation the grid places a single pending buy at the
    level directly below the current price.  Everything else
    is placed via :meth:`confirm_fill` after exchange confirmation.
    """

    def __init__(self, config: GridConfig) -> None:
        self.config = config
        step = (config.upper_price - config.lower_price) / (config.levels - 1)
        self.levels = [config.lower_price + i * step for i in range(config.levels)]

    def _nearest_level_index(self, price: float) -> int:
        """Return the index of the grid level closest to *price*."""
        distances = [abs(price - level) for level in self.levels]
        return distances.index(min(distances))

    # ── core logic ────────────────────────────────────────────

    def on_price(self, price: float, state: StrategyState) -> List[TradeSignal]:
        """Detect fills and return trade signals.

        **First call** — initialises the grid:

        * Sets ``level_index`` to the nearest grid level.
        * Places a single pending buy at the level directly below.

        **Subsequent calls** — scans open orders for fills:

        * Buy orders fill when price ≤ the order's level price.
        * Sell orders fill when price ≥ the order's level price.
        * Filled orders are removed from ``state.open_orders``.
        * **No follow-up orders are placed** — that happens in
          :meth:`confirm_fill` after exchange confirmation.

        Cascading: when a fill creates a new order that would also
        be immediately hit at the current price, that order is also
        returned as a signal (via the internal loop).  But the
        follow-up orders for *those* cascade fills are likewise
        deferred to ``confirm_fill``.
        """
        signals: List[TradeSignal] = []
        current_idx = self._nearest_level_index(price)

        # ── First tick: place one buy below current price ──────
        if state.level_index is None:
            state.level_index = current_idx
            buy_idx = current_idx - 1
            if buy_idx >= 0:
                state.open_orders[buy_idx] = "buy"
            return signals

        # ── Detect filled orders ───────────────────────────────
        filled: list[tuple[int, str]] = []
        for idx, side in list(state.open_orders.items()):
            level_price = self.levels[idx]
            if side == "buy" and price <= level_price:
                filled.append((idx, "buy"))
            elif side == "sell" and price >= level_price:
                filled.append((idx, "sell"))

        # Sort: process buys low→high, sells high→low for determinism
        filled.sort(key=lambda x: (x[1], x[0] if x[1] == "buy" else -x[0]))

        for idx, side in filled:
            del state.open_orders[idx]
            signals.append(
                TradeSignal(side=side, quote_amount=self.config.order_size_quote, level_index=idx)
            )

        state.level_index = current_idx
        return signals

    def confirm_fill(self, signal: TradeSignal, state: StrategyState) -> None:
        """Place follow-up orders after the exchange confirms a fill.

        Called by the runner for each signal that was successfully
        executed on the exchange.  This is where sells, cascade buys,
        and bookkeeping happen — never speculatively.

        :param signal: The confirmed trade signal (must have level_index set).
        :param state: Current strategy state to mutate.
        """
        idx = signal.level_index
        if idx is None:
            return

        if signal.side == "buy":
            state.filled_buys.add(idx)
            # Place a sell order one level above
            sell_idx = idx + 1
            if sell_idx < len(self.levels) and sell_idx not in state.open_orders:
                state.open_orders[sell_idx] = "sell"
            # Cascade: place a buy one level below
            next_buy = idx - 1
            if next_buy >= 0 and next_buy not in state.open_orders:
                state.open_orders[next_buy] = "buy"
        else:  # sell confirmed
            buy_origin = idx - 1
            if buy_origin >= 0:
                state.filled_buys.discard(buy_origin)
            # Place a buy order one level below
            buy_idx = idx - 1
            if buy_idx >= 0 and buy_idx not in state.open_orders:
                state.open_orders[buy_idx] = "buy"

    def get_open_orders(self, state: StrategyState) -> list[dict]:
        """Return a list of open orders for display purposes.

        :param state: Current strategy state.
        :return: List of dicts with level, price, side, quote_amount, and filled_quote.
        """
        orders = []
        for idx, side in sorted(state.open_orders.items()):
            filled = state.filled_amounts.get(idx, 0.0) if hasattr(state, "filled_amounts") else 0.0
            orders.append({
                "level": idx,
                "price": round(self.levels[idx], 6),
                "side": side,
                "quote_amount": self.config.order_size_quote,
                "filled_quote": round(filled, 6),
            })
        return orders
