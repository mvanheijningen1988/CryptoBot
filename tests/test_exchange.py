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
        assert ex.fee_rate == pytest.approx(0.0)


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


class TestBitvavoCallActionRetry:

    def _make_exchange(self) -> BitvavoExchange:
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.ws = MagicMock()
        return ex

    def test_retries_after_transient_send_error_with_reconnect(self):
        ex = self._make_exchange()
        calls = {"n": 0}

        def _send(payload: dict):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("[SSL: BAD_LENGTH] bad length")
            req_id = payload["requestId"]
            ex.pending_responses[req_id] = {"requestId": req_id, "response": {"ok": True}}
            ex.pending_events[req_id].set()

        with patch.object(ex, "_send_json", side_effect=_send), patch.object(ex, "_reconnect_transport") as mock_reconnect:
            response = ex._call_action("privateGetBalance", {}, timeout=0.2)

        assert response["response"]["ok"] is True
        assert calls["n"] == 2
        mock_reconnect.assert_called_once()

    def test_raises_when_send_error_persists_after_retries(self):
        ex = self._make_exchange()
        ex._action_send_retry_attempts = 2

        with patch.object(ex, "_send_json", side_effect=OSError("[SSL: BAD_LENGTH] bad length")), patch.object(
            ex,
            "_reconnect_transport",
            return_value=None,
        ) as mock_reconnect:
            with pytest.raises(RuntimeError, match="Bitvavo websocket send failed"):
                ex._call_action("privateGetBalance", {}, timeout=0.1)

        assert mock_reconnect.call_count == 1


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

    def test_balance_error_code_raises_runtime_error(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = True
        response = {"errorCode": 201, "error": "unauthorized"}
        with patch.object(ex, "_call_action", return_value=response):
            with pytest.raises(RuntimeError, match="privateGetBalance failed: unauthorized"):
                ex._refresh_balances()

    def test_balance_timeout_is_non_fatal(self):
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = True

        with patch.object(ex, "_call_action", side_effect=TimeoutError("timed out")):
            ex._refresh_balances()

        assert ex.quote_balance == pytest.approx(0.0)
        assert ex.base_balance == pytest.approx(0.0)


# ===================================================================
# BitvavoExchange – tracked order polling
# ===================================================================

class TestBitvavoTrackedOrders:

    def _make_exchange(self) -> BitvavoExchange:
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = True
        return ex

    def test_get_filled_orders_uses_order_api_fee(self):
        ex = self._make_exchange()
        ex._limit_orders["our-1"] = {
            "side": "buy",
            "quote_amount": 100.0,
            "limit_price": 100.0,
            "level_index": 2,
            "exchange_order_id": "ex-1",
        }
        ex._exchange_order_map["ex-1"] = "our-1"

        order_response = {
            "response": {
                "orderId": "ex-1",
                "status": "filled",
                "filledAmount": "1.0",
                "filledAmountQuote": "100.0",
                "feePaid": "0.15",
                "feeCurrency": "EUR",
                "fills": [
                    {
                        "amount": "1.0",
                        "price": "100.0",
                        "fee": "0.15",
                        "feeCurrency": "EUR",
                        "taker": False,
                        "settled": True,
                    }
                ],
            }
        }

        with patch.object(ex, "_call_action", return_value=order_response), patch.object(ex, "_refresh_balances"):
            fills = ex.get_filled_orders()

        assert len(fills) == 1
        assert fills[0]["order_id"] == "our-1"
        assert fills[0]["quote_amount"] == pytest.approx(100.0)
        assert fills[0]["fill_count"] == 1
        assert fills[0]["fee_paid_quote"] == pytest.approx(0.15)
        assert fills[0]["fee_rate"] == pytest.approx(0.0015)
        assert "our-1" not in ex._limit_orders

    def test_partially_filled_order_stays_tracked_until_filled(self):
        ex = self._make_exchange()
        ex._limit_orders["our-2"] = {
            "side": "sell",
            "quote_amount": 120.0,
            "limit_price": 120.0,
            "level_index": 4,
            "exchange_order_id": "ex-2",
        }
        ex._exchange_order_map["ex-2"] = "our-2"

        partial_response = {
            "response": {
                "orderId": "ex-2",
                "status": "partiallyFilled",
                "filledAmount": "0.5",
                "filledAmountQuote": "60.0",
            }
        }
        final_response = {
            "response": {
                "orderId": "ex-2",
                "status": "filled",
                "filledAmount": "1.0",
                "filledAmountQuote": "120.0",
                "feePaid": "0.18",
                "feeCurrency": "EUR",
                "fills": [
                    {
                        "amount": "0.5",
                        "price": "120.0",
                        "fee": "0.09",
                        "feeCurrency": "EUR",
                        "taker": False,
                        "settled": True,
                    },
                    {
                        "amount": "0.5",
                        "price": "120.0",
                        "fee": "0.09",
                        "feeCurrency": "EUR",
                        "taker": False,
                        "settled": True,
                    },
                ],
            }
        }

        with patch.object(ex, "_get_open_orders", return_value=[]), patch.object(
            ex, "_call_action", side_effect=[partial_response, final_response]
        ), patch.object(ex, "_refresh_balances"):
            first = ex.get_filled_orders()
            assert first == []
            assert "our-2" in ex._limit_orders

            second = ex.get_filled_orders()

        assert len(second) == 1
        assert second[0]["order_id"] == "our-2"
        assert second[0]["fill_count"] == 2
        assert second[0]["fee_paid_quote"] == pytest.approx(0.18)
        assert "our-2" not in ex._limit_orders

    def test_get_filled_orders_reconciles_with_open_orders_endpoint(self):
        ex = self._make_exchange()
        ex._open_order_refresh_interval_seconds = 0.0
        ex._limit_orders["our-open"] = {
            "side": "buy",
            "quote_amount": 100.0,
            "limit_price": 100.0,
            "level_index": 0,
            "client_order_id": "cid-open",
            "exchange_order_id": "ex-open",
        }
        ex._limit_orders["our-closed"] = {
            "side": "sell",
            "quote_amount": 120.0,
            "limit_price": 120.0,
            "level_index": 1,
            "client_order_id": "cid-closed",
            "exchange_order_id": "ex-closed",
        }
        ex._exchange_order_map["ex-open"] = "our-open"
        ex._exchange_order_map["ex-closed"] = "our-closed"

        open_orders = [
            {
                "orderId": "ex-open",
                "operatorId": None,
                "side": "buy",
                "price": "100.0",
                "status": "new",
                "clientOrderId": "cid-open",
            }
        ]

        def _call(action, body, timeout=6.0):  # noqa: ARG001
            assert action == "privateGetOrder"
            assert body.get("orderId") == "ex-closed"
            return {
                "response": {
                    "orderId": "ex-closed",
                    "status": "filled",
                    "filledAmount": "1.0",
                    "filledAmountQuote": "120.0",
                    "feePaid": "0.12",
                    "feeCurrency": "EUR",
                    "fills": [
                        {
                            "amount": "0.4",
                            "price": "120.0",
                            "fee": "0.048",
                            "feeCurrency": "EUR",
                        },
                        {
                            "amount": "0.6",
                            "price": "120.0",
                            "fee": "0.072",
                            "feeCurrency": "EUR",
                        },
                    ],
                }
            }

        with patch.object(ex, "_get_open_orders", return_value=open_orders), patch.object(
            ex, "_call_action", side_effect=_call
        ), patch.object(ex, "_refresh_balances"):
            fills = ex.get_filled_orders()

        assert len(fills) == 1
        assert fills[0]["order_id"] == "our-closed"
        assert fills[0]["quote_amount"] == pytest.approx(120.0)
        assert "our-open" in ex._limit_orders
        assert "our-closed" not in ex._limit_orders

    def test_reconcile_planned_level_orders_emits_fill_for_filled_non_open_order(self):
        ex = self._make_exchange()
        ex._planned_level_reconcile_interval_seconds = 0.0

        level_index = 1
        expected_client_order_id = ex._client_order_id(f"sync-{level_index}", f"level-{level_index}")

        order_response = {
            "response": {
                "orderId": "ex-reconcile-1",
                "clientOrderId": expected_client_order_id,
                "status": "filled",
                "side": "buy",
                "price": "100.0",
                "filledAmount": "1.0",
                "filledAmountQuote": "100.0",
                "feePaid": "0.10",
                "feeCurrency": "EUR",
                "fills": [
                    {
                        "amount": "0.4",
                        "price": "100.0",
                        "fee": "0.04",
                        "feeCurrency": "EUR",
                    },
                    {
                        "amount": "0.6",
                        "price": "100.0",
                        "fee": "0.06",
                        "feeCurrency": "EUR",
                    },
                ],
            }
        }

        def _call(action, body, timeout=6.0):  # noqa: ARG001
            assert action == "privateGetOrder"
            assert body.get("clientOrderId") == expected_client_order_id
            return order_response

        with patch.object(ex, "_get_open_orders", return_value=[]), patch.object(ex, "_call_action", side_effect=_call):
            fills = ex.reconcile_planned_level_orders(
                planned_open_orders={level_index: "buy"},
                level_prices=[90.0, 100.0, 110.0],
                quote_amount=100.0,
            )

        assert len(fills) == 1
        assert fills[0]["level_index"] == level_index
        assert fills[0]["quote_amount"] == pytest.approx(100.0)
        assert fills[0]["fill_count"] == 2
        assert fills[0]["fee_paid_quote"] == pytest.approx(0.10)

    def test_reconcile_planned_level_orders_skips_still_open_orders(self):
        ex = self._make_exchange()
        ex._planned_level_reconcile_interval_seconds = 0.0

        level_index = 0
        expected_client_order_id = ex._client_order_id(f"sync-{level_index}", f"level-{level_index}")
        open_orders = [{"orderId": "ex-open-1", "clientOrderId": expected_client_order_id, "status": "new", "side": "buy", "price": "90.0", "operatorId": 42}]

        with patch.object(ex, "_get_open_orders", return_value=open_orders), patch.object(ex, "_call_action") as mock_call:
            fills = ex.reconcile_planned_level_orders(
                planned_open_orders={level_index: "buy"},
                level_prices=[90.0, 100.0],
                quote_amount=100.0,
            )

        assert fills == []
        mock_call.assert_not_called()


class TestBitvavoPlaceLimitOrder:

    def _make_exchange(self) -> BitvavoExchange:
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )
        ex.authenticated = True
        return ex

    def test_timeout_falls_back_to_get_order_by_client_order_id(self):
        ex = self._make_exchange()
        expected_client_order_id = ex._client_order_id("our-timeout", "level-1")

        def _call(action, body, timeout=6.0):  # noqa: ARG001
            if action == "privateCreateOrder":
                assert body.get("clientOrderId") == expected_client_order_id
                raise TimeoutError("create timeout")
            if action == "privateGetOrder":
                assert body.get("clientOrderId") == expected_client_order_id
                return {"response": {"orderId": "ex-timeout", "status": "new"}}
            raise AssertionError(f"Unexpected action: {action}")

        with patch.object(ex, "_call_action", side_effect=_call):
            ok = ex.place_limit_order("our-timeout", "buy", 100.0, 50000.0, level_index=1)

        assert ok is True
        assert "our-timeout" in ex._limit_orders
        assert ex._limit_orders["our-timeout"]["client_order_id"] == expected_client_order_id
        assert ex._limit_orders["our-timeout"]["exchange_order_id"] == "ex-timeout"
        assert ex._exchange_order_map["ex-timeout"] == "our-timeout"

    def test_error_code_raises_runtime_error(self):
        ex = self._make_exchange()

        with patch.object(ex, "_call_action", return_value={"errorCode": 105, "error": "insufficient balance"}):
            with pytest.raises(RuntimeError, match="privateCreateOrder failed: insufficient balance"):
                ex.place_limit_order("our-fail", "buy", 100.0, 50000.0, level_index=1)

    def test_limit_order_payload_uses_max_6_decimals_round_down(self):
        ex = self._make_exchange()

        calls: list[tuple[str, dict]] = []

        def _call(action, body, timeout=6.0):  # noqa: ARG001
            calls.append((action, body))
            return {"response": {"orderId": "ex-precise", "status": "new"}}

        with patch.object(ex, "_call_action", side_effect=_call):
            ok = ex.place_limit_order("our-precise", "buy", 12.3456789123, 123.4567899876, level_index=0)

        assert ok is True
        create_call = next((payload for action, payload in calls if action == "privateCreateOrder"), None)
        assert create_call is not None
        assert create_call["price"] == "123.456789"
        assert create_call["amount"] == "0.099999"

    @patch("common.exchange.bitvavo.requests.get")
    def test_load_market_precision_from_markets_metadata(self, mock_get):
        ex = self._make_exchange()

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [{
            "market": "BTC-EUR",
            "pricePrecision": 5,
            "amountPrecision": 4,
            "quotePrecision": 2,
        }]
        mock_get.return_value = mock_response

        ex._load_market_precision()

        assert ex._price_decimals == 5
        assert ex._amount_decimals == 4
        assert ex._quote_decimals == 2

    @patch("common.exchange.bitvavo.requests.get")
    def test_load_market_precision_caps_to_6_decimals(self, mock_get):
        ex = self._make_exchange()

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [{
            "market": "BTC-EUR",
            "pricePrecision": 8,
            "amountPrecision": 8,
            "quotePrecision": 8,
        }]
        mock_get.return_value = mock_response

        ex._load_market_precision()

        assert ex._price_decimals == 6
        assert ex._amount_decimals == 6
        assert ex._quote_decimals == 6

    def test_limit_order_payload_uses_market_precision_when_loaded(self):
        ex = self._make_exchange()
        ex._price_decimals = 5
        ex._amount_decimals = 4

        calls: list[tuple[str, dict]] = []

        def _call(action, body, timeout=6.0):  # noqa: ARG001
            calls.append((action, body))
            return {"response": {"orderId": "ex-precise-market", "status": "new"}}

        with patch.object(ex, "_call_action", side_effect=_call):
            ok = ex.place_limit_order("our-precise-market", "buy", 12.3456789123, 123.4567891234, level_index=0)

        assert ok is True
        create_call = next((payload for action, payload in calls if action == "privateCreateOrder"), None)
        assert create_call is not None
        assert create_call["price"] == "123.45678"
        assert create_call["amount"] == "0.0999"

    def test_limit_order_uses_stable_level_client_reference_for_client_order_id(self):
        ex = self._make_exchange()
        ex.operator_id = 42

        calls: list[tuple[str, dict]] = []

        def _call(action, body, timeout=6.0):  # noqa: ARG001
            calls.append((action, body))
            return {"response": {"orderId": "ex-level-ref", "status": "new"}}

        with patch.object(ex, "_call_action", side_effect=_call):
            ok = ex.place_limit_order(
                "our-dynamic-id",
                "buy",
                100.0,
                50000.0,
                level_index=0,
                client_reference="level-0",
            )

        assert ok is True
        create_call = next((payload for action, payload in calls if action == "privateCreateOrder"), None)
        assert create_call is not None
        assert create_call["clientOrderId"] == ex._client_order_id("our-dynamic-id", "level-0")
        assert ex._limit_orders["our-dynamic-id"]["client_reference"] == "level-0"


class TestBitvavoSyncOpenOrders:

    def _make_exchange(self) -> BitvavoExchange:
        ex = BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR", operator_id=42,
        )
        ex.authenticated = True
        return ex

    def test_sync_open_orders_tracks_matching_operator_and_level(self):
        ex = self._make_exchange()

        open_orders_response = {
            "response": [
                {
                    "orderId": "ex-match",
                    "operatorId": 42,
                    "side": "buy",
                    "price": "100.00000000",
                    "status": "new",
                    "clientOrderId": "cid-match",
                },
                {
                    "orderId": "ex-other-operator",
                    "operatorId": 99,
                    "side": "buy",
                    "price": "95.00000000",
                    "status": "new",
                    "clientOrderId": "cid-other",
                },
            ]
        }

        with patch.object(ex, "_call_action", return_value=open_orders_response) as mock_call:
            matched = ex.sync_open_orders_for_levels(
                planned_open_orders={0: "buy", 1: "buy"},
                level_prices=[100.0, 95.0],
                quote_amount=100.0,
            )

        assert matched == {0}
        assert ex.has_tracked_level_order(0, "buy", 100.0) is True
        assert ex.has_tracked_level_order(1, "buy", 95.0) is False
        assert mock_call.call_args_list[0].args[0] == "privateGetOrdersOpen"

    def test_sync_open_orders_raises_exchange_error(self):
        ex = self._make_exchange()

        with patch.object(ex, "_call_action", return_value={"errorCode": 201, "error": "unauthorized"}):
            with pytest.raises(RuntimeError, match="privateGetOrdersOpen failed: unauthorized") as exc_info:
                ex.sync_open_orders_for_levels(
                    planned_open_orders={0: "buy"},
                    level_prices=[100.0],
                    quote_amount=100.0,
                )
        assert "request_action=privateGetOrdersOpen" in str(exc_info.value)
        assert '"market": "BTC-EUR"' in str(exc_info.value)

    def test_sync_open_orders_falls_back_to_get_orders_on_invalid_action(self):
        ex = self._make_exchange()

        fallback_open_orders = {
            "response": [
                {
                    "orderId": "ex-fallback",
                    "operatorId": 42,
                    "side": "buy",
                    "price": "100.00000000",
                    "status": "new",
                    "clientOrderId": "cid-fallback",
                }
            ]
        }

        calls: list[str] = []

        def _call(action, body, timeout=6.0):  # noqa: ARG001
            calls.append(action)
            if action == "privateGetOrdersOpen":
                return {"errorCode": 110, "error": "Invalid action. Please check the request."}
            if action == "privateGetOrders":
                return fallback_open_orders
            raise AssertionError(f"Unexpected action: {action}")

        with patch.object(ex, "_call_action", side_effect=_call):
            matched = ex.sync_open_orders_for_levels(
                planned_open_orders={0: "buy"},
                level_prices=[100.0],
                quote_amount=100.0,
            )

        assert calls == ["privateGetOrdersOpen", "privateGetOrders"]
        assert matched == {0}

    def test_sync_open_orders_matches_stable_level_client_order_id_before_price(self):
        ex = self._make_exchange()
        expected_client_order_id = ex._client_order_id("sync-0", "level-0")

        open_orders_response = {
            "response": [
                {
                    "orderId": "ex-level-0",
                    "operatorId": 42,
                    "side": "buy",
                    "price": "101.00000000",
                    "status": "new",
                    "clientOrderId": expected_client_order_id,
                }
            ]
        }

        with patch.object(ex, "_call_action", return_value=open_orders_response):
            matched = ex.sync_open_orders_for_levels(
                planned_open_orders={0: "buy"},
                level_prices=[100.0],
                quote_amount=100.0,
            )

        assert matched == {0}
        assert ex.has_tracked_level_order(0, "buy", 100.0) is True
        tracked = next(iter(ex._limit_orders.values()))
        assert tracked["client_reference"] == "level-0"
        assert tracked["client_order_id"] == expected_client_order_id
        assert tracked["exchange_order_id"] == "ex-level-0"

        matches = ex.get_last_open_order_sync_matches()
        assert len(matches) == 1
        assert matches[0]["level_index"] == 0
        assert matches[0]["match_method"] == "client_reference"
        assert matches[0]["client_reference"] == "level-0"
        assert matches[0]["client_order_id"] == expected_client_order_id


# ===================================================================
# BitvavoExchange – wait_for_price_update fallback
# ===================================================================

class TestBitvavoWaitForPriceUpdate:

    def _make_exchange(self) -> BitvavoExchange:
        return BitvavoExchange(
            api_key="test", api_secret="secret",
            market="BTC-EUR", base_currency="BTC", quote_currency="EUR",
        )

    @patch("common.exchange.bitvavo.requests.get")
    def test_fetches_price_from_exchange_when_ws_timeout(self, mock_get):
        ex = self._make_exchange()
        ex.latest_price = None

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"market": "BTC-EUR", "price": "61500.12"}
        mock_get.return_value = mock_response

        price = ex.wait_for_price_update(last_price=None, timeout_seconds=0.01)

        assert price == pytest.approx(61500.12)
        assert ex.latest_price == pytest.approx(61500.12)

    @patch("common.exchange.bitvavo.requests.get")
    def test_prefers_ws_price_when_available(self, mock_get):
        ex = self._make_exchange()
        ex.latest_price = 42000.0

        price = ex.wait_for_price_update(last_price=None, timeout_seconds=0.01)

        assert price == pytest.approx(42000.0)
        mock_get.assert_not_called()


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


# ===================================================================
# SimulatedExchange – Limit orders
# ===================================================================

class TestSimulatedLimitOrders:

    def test_place_limit_buy(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ok = ex.place_limit_order("o1", "buy", 50.0, 95.0, level_index=3)
        assert ok is True
        assert "o1" in ex._pending_orders

    def test_buy_not_filled_above_limit(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.place_limit_order("o1", "buy", 50.0, 95.0, level_index=3)
        fills = ex.get_filled_orders()
        assert fills == []
        assert ex.quote_balance == pytest.approx(1000.0)

    def test_buy_filled_at_limit(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.place_limit_order("o1", "buy", 50.0, 95.0, level_index=3)
        ex.price = 95.0
        fills = ex.get_filled_orders()
        assert len(fills) == 1
        assert fills[0]["order_id"] == "o1"
        assert fills[0]["side"] == "buy"
        assert fills[0]["fill_price"] == pytest.approx(95.0)
        assert fills[0]["level_index"] == 3
        assert ex.quote_balance == pytest.approx(950.0)
        assert ex.base_balance == pytest.approx(50.0 / 95.0)

    def test_buy_filled_below_limit(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.place_limit_order("o1", "buy", 50.0, 95.0, level_index=3)
        ex.price = 90.0  # below limit
        fills = ex.get_filled_orders()
        assert len(fills) == 1
        # Executes at limit price, not market price
        assert fills[0]["fill_price"] == pytest.approx(95.0)
        assert ex.quote_balance == pytest.approx(950.0)

    def test_sell_not_filled_below_limit(self):
        ex = SimulatedExchange(_budget(0.0, 1.0), start_price=100.0, fee_rate=0)
        ex.place_limit_order("o1", "sell", 50.0, 105.0, level_index=5)
        fills = ex.get_filled_orders()
        assert fills == []

    def test_sell_filled_at_limit(self):
        ex = SimulatedExchange(_budget(0.0, 1.0), start_price=100.0, fee_rate=0)
        ex.place_limit_order("o1", "sell", 50.0, 105.0, level_index=5)
        ex.price = 105.0
        fills = ex.get_filled_orders()
        assert len(fills) == 1
        assert fills[0]["side"] == "sell"
        assert fills[0]["fill_price"] == pytest.approx(105.0)

    def test_limit_order_with_fee(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0.01)
        ex.place_limit_order("o1", "buy", 100.0, 50.0, level_index=0)
        ex.price = 50.0
        fills = ex.get_filled_orders()
        assert len(fills) == 1
        # 100 / 50 = 2.0 base, minus 1% fee = 1.98
        assert ex.base_balance == pytest.approx(1.98)
        assert ex.quote_balance == pytest.approx(900.0)

    def test_multiple_orders_partial_fill(self):
        """Only orders whose price condition is met should fill."""
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.place_limit_order("buy_90", "buy", 50.0, 90.0, level_index=0)
        ex.place_limit_order("buy_95", "buy", 50.0, 95.0, level_index=1)
        ex.price = 93.0  # only buy_95 should fill (93 <= 95), buy_90 should not (93 > 90)
        fills = ex.get_filled_orders()
        assert len(fills) == 1
        assert fills[0]["order_id"] == "buy_95"
        # buy_90 still pending
        assert "buy_90" in ex._pending_orders

    def test_filled_orders_cleared_after_get(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=50.0, fee_rate=0)
        ex.place_limit_order("o1", "buy", 50.0, 50.0, level_index=0)
        fills = ex.get_filled_orders()
        assert len(fills) == 1
        fills2 = ex.get_filled_orders()
        assert fills2 == []

    def test_cancel_all_orders(self):
        ex = SimulatedExchange(_budget(1000.0, 0.0), start_price=100.0, fee_rate=0)
        ex.place_limit_order("o1", "buy", 50.0, 95.0, level_index=0)
        ex.place_limit_order("o2", "buy", 50.0, 90.0, level_index=1)
        ex.cancel_all_orders()
        assert len(ex._pending_orders) == 0
        ex.price = 80.0
        fills = ex.get_filled_orders()
        assert fills == []
