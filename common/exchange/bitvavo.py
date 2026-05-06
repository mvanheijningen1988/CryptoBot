from __future__ import annotations

from common.exchange.base import Exchange
from common.models import TradeSignal


class BitvavoExchange(Exchange):
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret

    def execute(self, signal: TradeSignal, price: float) -> bool:
        # Placeholder for real Bitvavo integration.
        # In a later phase, implement REST/WebSocket calls and proper order handling.
        raise NotImplementedError("Bitvavo live trading is not implemented yet.")
