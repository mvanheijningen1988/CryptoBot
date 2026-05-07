"""Base classes for trading strategies.

Every concrete strategy inherits from ``Strategy`` and implements
``on_price`` which receives the latest market price together with a
mutable ``StrategyState`` and returns zero or more trade signals.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Literal

from common.models import TradeSignal


@dataclass
class StrategyState:
    """Mutable state carried between successive ``Strategy.on_price`` calls.

    :param level_index: The grid level the price was nearest to on the
        previous tick.  ``None`` until the first price is observed.
    :param open_orders: Dict mapping level index → ``"buy"`` or ``"sell"``
        for every pending order on the grid.
    :param filled_buys: Set of level indices where a buy was filled and
        a corresponding sell is now pending one level above.
    """

    level_index: int | None = None
    open_orders: dict[int, Literal["buy", "sell"]] = field(default_factory=dict)
    filled_buys: set[int] = field(default_factory=set)
    filled_amounts: dict[int, float] = field(default_factory=dict)


class Strategy(ABC):
    """Abstract base for all trading strategies."""

    @abstractmethod
    def on_price(self, price: float, state: StrategyState) -> List[TradeSignal]:
        """
        React to a new market price and return trade signals.

        Implementations must update *state* in place so that the next
        call can determine how the price moved relative to the grid.

        :param price: The latest market price.
        :param state: Mutable strategy state tracking the previous grid level.
        :return: A list of trade signals to execute (may be empty).
        """
        raise NotImplementedError
