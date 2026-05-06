from __future__ import annotations

from abc import ABC, abstractmethod

from common.models import TradeSignal


class Exchange(ABC):
    @abstractmethod
    def execute(self, signal: TradeSignal, price: float) -> bool:
        raise NotImplementedError
