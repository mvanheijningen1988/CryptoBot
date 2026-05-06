"""Static grid trading strategy.

Divides a price range into evenly spaced levels and emits buy signals
when the price drops through levels and sell signals when it rises.
The number of signals equals the number of levels crossed.
"""
from __future__ import annotations

from typing import List

from common.models import GridConfig, TradeSignal
from common.strategy.base import Strategy, StrategyState


class StaticGridStrategy(Strategy):
    """Grid strategy with fixed, evenly spaced price levels.

    On each price tick the strategy finds the nearest grid level.
    If the price moved *down* across one or more levels since the
    last tick a BUY signal is emitted for each crossed level;
    if it moved *up*, a SELL signal is emitted for each crossed level.
    """

    def __init__(self, config: GridConfig) -> None:
        """
        Initialise the grid from a configuration.

        Pre-computes the list of price levels so that ``on_price``
        only needs a cheap nearest-level lookup.

        :param config: Grid configuration defining price range, levels, and order size.
        """
        self.config = config
        step = (config.upper_price - config.lower_price) / (config.levels - 1)
        self.levels = [config.lower_price + i * step for i in range(config.levels)]

    def _nearest_level_index(self, price: float) -> int:
        """
        Return the index of the grid level closest to the given price.

        :param price: The market price to snap to the nearest grid level.
        :return: Index of the nearest grid level.
        """
        distances = [abs(price - level) for level in self.levels]
        return distances.index(min(distances))

    def on_price(self, price: float, state: StrategyState) -> List[TradeSignal]:
        """
        Process a new price tick and return trade signals.

        On the very first call the strategy records the nearest level
        and returns no signals.  On subsequent calls it compares the
        current nearest level to the previous one and emits one signal
        per crossed level.

        :param price: The latest market price.
        :param state: Mutable strategy state tracking the previous grid level.
        :return: A list of buy/sell signals for each crossed grid level.
        """
        signals: List[TradeSignal] = []
        current_idx = self._nearest_level_index(price)

        if state.level_index is None:
            state.level_index = current_idx
            return signals

        if current_idx < state.level_index:
            steps_down = state.level_index - current_idx
            for _ in range(steps_down):
                signals.append(TradeSignal(side="buy", quote_amount=self.config.order_size_quote))

        if current_idx > state.level_index:
            steps_up = current_idx - state.level_index
            for _ in range(steps_up):
                signals.append(TradeSignal(side="sell", quote_amount=self.config.order_size_quote))

        state.level_index = current_idx
        return signals
