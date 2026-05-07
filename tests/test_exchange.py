"""Unit tests for the exchange adapters.

Covers SimulatedExchange thoroughly (buy/sell execution, balance tracking,
insufficient funds, price behaviour, edge cases) and BitvavoExchange
message handling / price extraction (without requiring a live websocket).
"""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from common import Exchange, SimulatedExchange, BitvavoExchange, BudgetConfig, TradeSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _budget(quote: float = 1000.0, base: float = 0.0) -> BudgetConfig:
    return BudgetConfig(quote_budget=quote, base_budget=base)


def _buy(amount: float = 100.0) -> TradeSignal:
    return TradeSignal(side="buy", quote_amount=amount)


def _sell(amount: float = 100.0) -> TradeSignal:
    return TradeSignal(side="sell", quote_amount=amount)


# ===================================================================
# SimulatedExchange – initialisation
# ===================================================================

class TestSimulatedInit:

    def test_initial_balances(self):
        ex = SimulatedExchange(_budget(500.0, 2.0), fee_rate=0)
        assert ex.quote_balance == pytest.approx(500.0)
        assert ex.base_balance == pytest.approx(2.0)

    def test_initial_price(self):
        ex = SimulatedExchange(_budget(), start_price=42.0, fee_rate=0)
        assert ex.price == pytest.approx(42.0)

    def test_default_start_price(self):
        ex = SimulatedExchange(_budget(), fee_rate=0)
        assert ex.price == pytest.approx(100.0)

    def test_zero_budgets(self):
        ex = SimulatedExchange(_budget(0.0, 0.0), fee_rate=0)
        assert ex.quote_balance == pytest.approx(0.0)
        assert ex.base_balance == pytest.approx(0.0)

    def test_default_fee_rate(self):
        ex = SimulatedExchange(_budget())
        assert ex.fee_rate == pytest.approx(0.0025)


# ===================================================================
# SimulatedExchange – get_price
# ===================================================================

class TestSimulatedGetPrice:

    def test_price_is_float(self):
        ex = SimulatedExchange(_budget())
        assert isinstance(ex.get_price(), float)

    def test_price_changes_on_call(self):
        ex = SimulatedExchange(_budget(), start_price=100.0)
        prices = {ex.get_price() for _ in range(50)}
        # Extremely unlikely all 50 random walks produce the exact same price
        assert len(prices) > 1

    def test_price_stays_positive(self):
        ex = SimulatedExchange(_budget(), start_price=0.001)
        for _ in range(1000):
            p = ex.get_price()
            assert p >= 0.0001

    def test_price_minimum_floor(self):
        """Even from a very small price, the floor of 0.0001 holds."""
        ex = SimulatedExchange(_budget(), start_price=0.0001)
        for _ in range(200):
            assert ex.get_price() >= 0.0001


# ===================================================================
# SimulatedExchange – buy execution
# ===================================================================

class TestSimulatedBuy:

    def test_basic_buy(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=50.0, fee_rate=0)
        result = ex.execute(_buy(100.0), price=50.0)
        assert result is True
        assert ex.quote_balance == pytest.approx(900.0)
        assert ex.base_balance == pytest.approx(2.0)  # 100 / 50

    def test_buy_entire_balance(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        result = ex.execute(_buy(1000.0), price=100.0)
        assert result is True
        assert ex.quote_balance == pytest.approx(0.0)
        assert ex.base_balance == pytest.approx(10.0)

    def test_buy_beyond_balance_virtual_budget(self):
        """Simulation allows buying beyond available balance (virtual budget)."""
        ex = SimulatedExchange(_budget(50.0, 0.0), start_price=100.0, fee_rate=0)
        result = ex.execute(_buy(100.0), price=100.0)
        assert result is True
        assert ex.quote_balance == pytest.approx(-50.0)
        assert ex.base_balance == pytest.approx(1.0)

    def test_buy_exact_balance(self):
        """Buy exactly what's available should succeed."""
        ex = SimulatedExchange(_budget(100.0, 0.0), start_price=50.0, fee_rate=0)
        result = ex.execute(_buy(100.0), price=50.0)
        assert result is True
        assert ex.quote_balance == pytest.approx(0.0)

    def test_buy_uses_provided_price(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=999.0, fee_rate=0)
        ex.execute(_buy(200.0), price=25.0)
        assert ex.base_balance == pytest.approx(8.0)  # 200 / 25

    def test_buy_uses_internal_price_when_none(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=50.0, fee_rate=0)
        ex.execute(_buy(100.0), price=None)
        assert ex.base_balance == pytest.approx(2.0)

    def test_buy_very_small_amount(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        result = ex.execute(_buy(0.01), price=100.0)
        assert result is True
        assert ex.base_balance == pytest.approx(0.0001)

    def test_buy_at_very_high_price(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=1_000_000.0, fee_rate=0)
        result = ex.execute(_buy(1000.0), price=1_000_000.0)
        assert result is True
        assert ex.base_balance == pytest.approx(0.001)

    def test_multiple_buys(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.execute(_buy(200.0), price=100.0)
        ex.execute(_buy(300.0), price=100.0)
        assert ex.quote_balance == pytest.approx(500.0)
        assert ex.base_balance == pytest.approx(5.0)

    def test_buy_with_fee(self):
        """Fee reduces the base received."""
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0.01)
        ex.execute(_buy(100.0), price=100.0)
        assert ex.quote_balance == pytest.approx(900.0)
        assert ex.base_balance == pytest.approx(0.99)  # 1.0 * 0.99


# ===================================================================
# SimulatedExchange – sell execution
# ===================================================================

class TestSimulatedSell:

    def test_basic_sell(self):
        ex = SimulatedExchange(_budget(0.0, 10.0), start_price=50.0, fee_rate=0)
        result = ex.execute(_sell(100.0), price=50.0)
        assert result is True
        assert ex.base_balance == pytest.approx(8.0)
        assert ex.quote_balance == pytest.approx(100.0)

    def test_sell_all_base(self):
        ex = SimulatedExchange(_budget(0.0, 5.0), start_price=100.0, fee_rate=0)
        result = ex.execute(_sell(500.0), price=100.0)
        assert result is True
        assert ex.base_balance == pytest.approx(0.0)
        assert ex.quote_balance == pytest.approx(500.0)

    def test_sell_beyond_base_virtual_budget(self):
        """Simulation allows selling beyond available base (virtual budget)."""
        ex = SimulatedExchange(_budget(0.0, 1.0), start_price=100.0, fee_rate=0)
        result = ex.execute(_sell(200.0), price=100.0)  # needs 2 base, have 1
        assert result is True
        assert ex.base_balance == pytest.approx(-1.0)
        assert ex.quote_balance == pytest.approx(200.0)

    def test_sell_uses_provided_price(self):
        ex = SimulatedExchange(_budget(0.0, 10.0), start_price=999.0, fee_rate=0)
        ex.execute(_sell(50.0), price=25.0)
        assert ex.base_balance == pytest.approx(8.0)
        assert ex.quote_balance == pytest.approx(50.0)

    def test_sell_very_small_amount(self):
        ex = SimulatedExchange(_budget(0.0, 10.0), start_price=100.0, fee_rate=0)
        result = ex.execute(_sell(0.01), price=100.0)
        assert result is True
        assert ex.base_balance == pytest.approx(10.0 - 0.0001)

    def test_multiple_sells(self):
        ex = SimulatedExchange(_budget(0.0, 10.0), start_price=100.0, fee_rate=0)
        ex.execute(_sell(200.0), price=100.0)
        ex.execute(_sell(300.0), price=100.0)
        assert ex.base_balance == pytest.approx(5.0)
        assert ex.quote_balance == pytest.approx(500.0)

    def test_sell_with_fee(self):
        """Fee reduces the quote received."""
        ex = SimulatedExchange(_budget(0.0, 10.0), start_price=100.0, fee_rate=0.01)
        ex.execute(_sell(100.0), price=100.0)
        assert ex.base_balance == pytest.approx(9.0)
        assert ex.quote_balance == pytest.approx(99.0)  # 100 * 0.99


# ===================================================================
# SimulatedExchange – buy then sell round-trip
# ===================================================================

class TestSimulatedRoundTrip:

    def test_buy_and_sell_at_same_price(self):
        """Round-trip at same price returns to original balances (no fee)."""
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.execute(_buy(500.0), price=100.0)
        ex.execute(_sell(500.0), price=100.0)
        assert ex.quote_balance == pytest.approx(1000.0)
        assert ex.base_balance == pytest.approx(0.0)

    def test_buy_low_sell_high_profit(self):
        """Buy at low price, sell at higher price → quote increases."""
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.execute(_buy(500.0), price=50.0)    # buy 10 base
        ex.execute(_sell(1000.0), price=100.0)  # sell 10 base → 1000 quote
        assert ex.quote_balance == pytest.approx(1500.0)
        assert ex.base_balance == pytest.approx(0.0)

    def test_buy_high_sell_low_loss(self):
        """Buy at high price, sell at lower → negative balance (virtual budget)."""
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.execute(_buy(500.0), price=100.0)   # buy 5 base
        # sell at price 50: needs 500/50 = 10 base, have 5 → goes to -5 base
        result = ex.execute(_sell(500.0), price=50.0)
        assert result is True
        assert ex.base_balance == pytest.approx(-5.0)

    def test_round_trip_with_fees(self):
        """Round-trip with fees results in a small loss."""
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0.01)
        ex.execute(_buy(500.0), price=100.0)  # gets 4.95 base
        # sell 4.95 base at 100 → 495 * 0.99 = 490.05
        ex.execute(TradeSignal(side="sell", quote_amount=495.0), price=100.0)
        assert ex.quote_balance < 1000.0  # lost money to fees


# ===================================================================
# SimulatedExchange – get_balances
# ===================================================================

class TestSimulatedBalances:

    def test_get_balances_returns_tuple(self):
        ex = SimulatedExchange(_budget(100.0, 5.0), fee_rate=0)
        q, b = ex.get_balances()
        assert q == pytest.approx(100.0)
        assert b == pytest.approx(5.0)

    def test_get_balances_after_trade(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.execute(_buy(200.0), price=100.0)
        q, b = ex.get_balances()
        assert q == pytest.approx(800.0)
        assert b == pytest.approx(2.0)


# ===================================================================
# SimulatedExchange – start/stop (no-ops)
# ===================================================================

class TestSimulatedStartStop:

    def test_start_does_not_raise(self):
        ex = SimulatedExchange(_budget())
        ex.start()

    def test_stop_does_not_raise(self):
        ex = SimulatedExchange(_budget())
        ex.stop()


# ===================================================================
# SimulatedExchange – invalid signal side
# ===================================================================

class TestSimulatedInvalidSignal:

    def test_unknown_side_returns_false(self):
        ex = SimulatedExchange(_budget(1000.0, 10.0), fee_rate=0)
        signal = TradeSignal(side="buy", quote_amount=100.0)
        # Monkey-patch to test the fallthrough
        signal.side = "hold"  # type: ignore[assignment]
        result = ex.execute(signal, price=100.0)
        assert result is False


# ===================================================================
# SimulatedExchange – wait_for_price_update
# ===================================================================

class TestSimulatedWaitForPrice:

    def test_returns_float(self):
        ex = SimulatedExchange(_budget(), start_price=100.0)
        p = ex.wait_for_price_update(timeout_seconds=0.05)
        assert isinstance(p, float)
        assert p > 0


# ===================================================================
# BitvavoExchange – _extract_price
# ===================================================================

class TestBitvavoExtractPrice:
    """Test price extraction from various websocket message formats."""

    def _make_exchange(self) -> BitvavoExchange:
        return BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )

    def test_price_key(self):
        ex = self._make_exchange()
        assert ex._extract_price({"price": "50000.5"}) == pytest.approx(50000.5)

    def test_last_key(self):
        ex = self._make_exchange()
        assert ex._extract_price({"last": "42000"}) == pytest.approx(42000.0)

    def test_price_key_takes_precedence(self):
        ex = self._make_exchange()
        assert ex._extract_price({"price": "100", "last": "200"}) == pytest.approx(100.0)

    def test_bid_ask_midpoint(self):
        ex = self._make_exchange()
        result = ex._extract_price({"bestBid": "99", "bestAsk": "101"})
        assert result == pytest.approx(100.0)

    def test_bid_ask_without_price_or_last(self):
        ex = self._make_exchange()
        result = ex._extract_price({"bestBid": "50", "bestAsk": "60"})
        assert result == pytest.approx(55.0)

    def test_no_price_info_returns_none(self):
        ex = self._make_exchange()
        assert ex._extract_price({}) is None

    def test_invalid_price_value(self):
        ex = self._make_exchange()
        assert ex._extract_price({"price": "not_a_number"}) is None

    def test_invalid_last_value(self):
        ex = self._make_exchange()
        assert ex._extract_price({"last": None}) is None

    def test_invalid_bid_ask(self):
        ex = self._make_exchange()
        assert ex._extract_price({"bestBid": "abc", "bestAsk": "xyz"}) is None

    def test_partial_bid_ask_missing_ask(self):
        """Only bestBid without bestAsk should not compute midpoint."""
        ex = self._make_exchange()
        assert ex._extract_price({"bestBid": "100"}) is None

    def test_partial_bid_ask_missing_bid(self):
        ex = self._make_exchange()
        assert ex._extract_price({"bestAsk": "100"}) is None


# ===================================================================
# BitvavoExchange – _handle_message
# ===================================================================

class TestBitvavoHandleMessage:
    """Test websocket message dispatch without a live connection."""

    def _make_exchange(self) -> BitvavoExchange:
        return BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )

    def test_ticker_event_updates_price(self):
        ex = self._make_exchange()
        msg = json.dumps({"event": "ticker", "price": "55000"})
        ex._handle_message(msg)
        assert ex.latest_price == pytest.approx(55000.0)

    def test_trade_event_updates_price(self):
        ex = self._make_exchange()
        msg = json.dumps({"event": "trade", "price": "42000"})
        ex._handle_message(msg)
        assert ex.latest_price == pytest.approx(42000.0)

    def test_ticker24h_event_updates_price(self):
        ex = self._make_exchange()
        msg = json.dumps({"event": "ticker24h", "last": "30000"})
        ex._handle_message(msg)
        assert ex.latest_price == pytest.approx(30000.0)

    def test_market_message_updates_price(self):
        ex = self._make_exchange()
        msg = json.dumps({"market": "BTC-EUR", "bestBid": "48000", "bestAsk": "52000"})
        ex._handle_message(msg)
        assert ex.latest_price == pytest.approx(50000.0)

    def test_other_market_ignored(self):
        ex = self._make_exchange()
        msg = json.dumps({"market": "ETH-EUR", "price": "3000"})
        ex._handle_message(msg)
        assert ex.latest_price is None

    def test_request_response_dispatched(self):
        ex = self._make_exchange()
        event = threading.Event()
        ex.pending_events[42] = event
        msg = json.dumps({"requestId": 42, "response": {"status": "ok"}})
        ex._handle_message(msg)
        assert event.is_set()
        assert 42 in ex.pending_responses

    def test_invalid_json_ignored(self):
        ex = self._make_exchange()
        ex._handle_message("not json at all")
        assert ex.latest_price is None

    def test_non_dict_message_ignored(self):
        ex = self._make_exchange()
        ex._handle_message(json.dumps([1, 2, 3]))
        assert ex.latest_price is None

    def test_price_update_event_set(self):
        ex = self._make_exchange()
        ex.price_update_event.clear()
        msg = json.dumps({"event": "ticker", "price": "100"})
        ex._handle_message(msg)
        assert ex.price_update_event.is_set()

    def test_no_price_in_ticker_does_not_update(self):
        ex = self._make_exchange()
        msg = json.dumps({"event": "ticker"})
        ex._handle_message(msg)
        assert ex.latest_price is None


# ===================================================================
# BitvavoExchange – _create_signature
# ===================================================================

class TestBitvavoSignature:

    def test_signature_is_hex_string(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        sig = ex._create_signature(1700000000000)
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA256 hex

    def test_different_timestamps_different_sigs(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        sig1 = ex._create_signature(1000)
        sig2 = ex._create_signature(2000)
        assert sig1 != sig2


# ===================================================================
# BitvavoExchange – get_price
# ===================================================================

class TestBitvavoGetPrice:

    def _make_exchange(self) -> BitvavoExchange:
        return BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )

    def test_returns_latest_price(self):
        ex = self._make_exchange()
        ex.latest_price = 42000.0
        assert ex.get_price() == pytest.approx(42000.0)

    def test_returns_fallback_when_no_price(self):
        ex = self._make_exchange()
        assert ex.get_price(fallback_price=99.0) == pytest.approx(99.0)

    def test_raises_when_no_price_no_fallback(self):
        ex = self._make_exchange()
        with pytest.raises(RuntimeError, match="not available"):
            ex.get_price()


# ===================================================================
# BitvavoExchange – request ID
# ===================================================================

class TestBitvavoRequestId:

    def test_increments(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        id1 = ex._next_request_id()
        id2 = ex._next_request_id()
        assert id2 == id1 + 1


# ===================================================================
# BitvavoExchange – execute without auth
# ===================================================================

class TestBitvavoExecuteNoAuth:

    def test_raises_without_auth(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        with pytest.raises(RuntimeError, match="not authenticated"):
            ex.execute(TradeSignal(side="buy", quote_amount=100.0))


# ===================================================================
# BitvavoExchange – _refresh_balances
# ===================================================================

class TestBitvavoRefreshBalances:

    def test_not_authenticated_skips(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = False
        ex._refresh_balances()
        assert ex.quote_balance == pytest.approx(0.0)
        assert ex.base_balance == pytest.approx(0.0)

    def test_parses_response_list(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = True
        response = {
            "response": [
                {"symbol": "EUR", "available": "500.00"},
                {"symbol": "BTC", "available": "1.5"},
            ]
        }
        with patch.object(ex, "_call_action", return_value=response):
            ex._refresh_balances()
        assert ex.quote_balance == pytest.approx(500.0)
        assert ex.base_balance == pytest.approx(1.5)

    def test_parses_balances_key(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = True
        response = {
            "balances": [
                {"symbol": "EUR", "available": "200"},
                {"symbol": "BTC", "available": "0.3"},
            ]
        }
        with patch.object(ex, "_call_action", return_value=response):
            ex._refresh_balances()
        assert ex.quote_balance == pytest.approx(200.0)
        assert ex.base_balance == pytest.approx(0.3)

    def test_invalid_available_skipped(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = True
        response = {
            "response": [
                {"symbol": "EUR", "available": "not_a_number"},
                {"symbol": "BTC", "available": "1.0"},
            ]
        }
        with patch.object(ex, "_call_action", return_value=response):
            ex._refresh_balances()
        assert ex.quote_balance == pytest.approx(0.0)  # skipped due to ValueError
        assert ex.base_balance == pytest.approx(1.0)

    def test_non_list_response_ignored(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = True
        with patch.object(ex, "_call_action", return_value={"error": "something"}):
            ex._refresh_balances()
        assert ex.quote_balance == pytest.approx(0.0)
        assert ex.base_balance == pytest.approx(0.0)


# ===================================================================
# BitvavoExchange – stop
# ===================================================================

class TestBitvavoStop:

    def test_stop_without_start(self):
        """Stop should be safe to call even if never started."""
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.stop()
        assert ex.running is False
        assert ex.authenticated is False


# ===================================================================
# Exchange ABC
# ===================================================================

class TestExchangeABC:
    """Verify the abstract base class interface."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            Exchange()  # type: ignore[abstract]

    def test_simulated_is_exchange(self):
        ex = SimulatedExchange(_budget())
        assert isinstance(ex, Exchange)
