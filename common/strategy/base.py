from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List

from common.models import TradeSignal


@dataclass
class StrategyState:
    level_index: int | None = None


class Strategy(ABC):
    @abstractmethod
    def on_price(self, price: float, state: StrategyState) -> List[TradeSignal]:
        raise NotImplementedError
