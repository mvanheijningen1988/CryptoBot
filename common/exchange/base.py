from __future__ import annotations

from abc import ABC, abstractmethod

from common.models import TradeSignal


class Exchange(ABC):
    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def get_price(self, fallback_price: float | None = None) -> float:
        if fallback_price is None:
            raise NotImplementedError("Exchange did not provide a price")
        return fallback_price

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 5.0) -> float:
        return self.get_price(last_price)

    def get_balances(self) -> tuple[float, float]:
        raise NotImplementedError

    @abstractmethod
    def execute(self, signal: TradeSignal, price: float | None = None) -> bool:
        raise NotImplementedError
