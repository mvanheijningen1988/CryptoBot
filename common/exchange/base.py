"""Abstract base class for exchange adapters.

Defines the interface that both the simulated back-testing exchange and
the live Bitvavo adapter implement: price retrieval, balance queries,
and order execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from common.models import TradeSignal


class Exchange(ABC):
    """Base exchange interface.

    Subclasses must implement :meth:`execute`.  The other methods have
    sensible defaults that the simulated exchange overrides.
    """

    def start(self) -> None:
        """Open any connections or resources (no-op by default)."""

    def stop(self) -> None:
        """Release connections or resources (no-op by default)."""

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
        Execute a trade signal at the given price (market order).

        :param signal: The trade signal describing side and amount.
        :param price: The execution price (may use internal price if None).
        :return: True if the trade executed successfully, False otherwise.
        """

    # ── Limit order interface ─────────────────────────────────

    def place_limit_order(
        self,
        order_id: str,
        side: str,
        quote_amount: float,
        limit_price: float,
        level_index: int | None = None,
    ) -> bool:
        """Place a limit order at *limit_price*.

        :param order_id: Unique identifier for this order.
        :param side: ``"buy"`` or ``"sell"``.
        :param quote_amount: Order size in quote currency.
        :param limit_price: Price at which the order should fill.
        :param level_index: Optional grid level index for tracking.
        :return: True if the order was accepted.
        """
        raise NotImplementedError

    def get_filled_orders(self) -> list[dict[str, Any]]:
        """Return orders filled since the last call.

        Each dict contains at least: ``order_id``, ``side``,
        ``quote_amount``, ``fill_price``, ``level_index``.
        """
        return []

    def cancel_all_orders(self) -> None:
        """Cancel all pending limit orders."""
        raise NotImplementedError
