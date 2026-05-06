"""Trading strategies for CryptoBot."""

from common.strategy.base import Strategy, StrategyState
from common.strategy.static_grid import StaticGridStrategy

__all__ = ["Strategy", "StrategyState", "StaticGridStrategy"]
