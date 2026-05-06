from __future__ import annotations

from typing import List

from common.models import GridConfig, TradeSignal
from common.strategy.base import Strategy, StrategyState


class StaticGridStrategy(Strategy):
    def __init__(self, config: GridConfig):
        self.config = config
        step = (config.upper_price - config.lower_price) / (config.levels - 1)
        self.levels = [config.lower_price + i * step for i in range(config.levels)]

    def _nearest_level_index(self, price: float) -> int:
        distances = [abs(price - level) for level in self.levels]
        return distances.index(min(distances))

    def on_price(self, price: float, state: StrategyState) -> List[TradeSignal]:
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
