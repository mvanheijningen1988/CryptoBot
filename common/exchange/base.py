"""Abstract base class for exchange adapters.

Defines the interface that both the simulated back-testing exchange and
the live Bitvavo adapter implement: price retrieval, balance queries,
and order execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from common.models import TradeSignal


class Exchange(ABC):
    """Base exchange interface.

    Subclasses must implement :meth:`execute`.  The other methods have
    sensible defaults that the simulated exchange overrides.
    """

    def start(self) -> None:
        """Open any connections or resources (no-op by default)."""
        return

    def stop(self) -> None:
        """Release connections or resources (no-op by default)."""
        return

    def get_price(self, fallback_price: float | None = None) -> float:
        """
        Return the latest market price.

        :param fallback_price: Price to return if no live price is available.
        :return: The current market price.
        """
        if fallback_price is None:
            raise NotImplementedError("Exchange did not provide a price")
        return fallback_price

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 5.0) -> float:
        """
        Block until a new price arrives or the timeout elapses.

        :param last_price: The previous price to compare against.
        :param timeout_seconds: Maximum seconds to wait for a new price.
        :return: The updated market price.
        """
        return self.get_price(last_price)

    def get_balances(self) -> tuple[float, float]:
        """Return ``(quote_balance, base_balance)``."""
        raise NotImplementedError

    @abstractmethod
    def execute(self, signal: TradeSignal, price: float | None = None) -> bool:
        """
        Execute a trade signal at the given price.

        :param signal: The trade signal describing side and amount.
        :param price: The execution price (may use internal price if None).
        :return: True if the trade executed successfully, False otherwise.
        """
        raise NotImplementedError
