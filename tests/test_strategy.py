"""Thorough unit tests for the static grid trading strategy.

Covers grid level computation, signal generation across every edge case,
state management, boundary conditions, and production-critical scenarios
designed to prevent money loss.
"""
from __future__ import annotations

import pytest

from common import GridConfig, TradeSignal, StrategyState, StaticGridStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grid(lower: float = 100.0, upper: float = 200.0, levels: int = 11,
          order_size: float = 10.0) -> GridConfig:
    """Build a GridConfig with sensible defaults."""
    return GridConfig(
        lower_price=lower, upper_price=upper,
        levels=levels, order_size_quote=order_size,
    )


def _sides(signals: list[TradeSignal]) -> list[str]:
    """Extract sides from a list of signals for easy assertion."""
    return [s.side for s in signals]


def _tick(s: StaticGridStrategy, price: float, state: StrategyState) -> list[TradeSignal]:
    """Detect fills at *price* and confirm them all (places follow-up orders).

    This simulates one exchange round-trip: detect → execute → confirm.
    """
    signals = s.on_price(price, state)
    for sig in signals:
        s.confirm_fill(sig, state)
    return signals


def _process(s: StaticGridStrategy, price: float, state: StrategyState) -> list[TradeSignal]:
    """Full cascade: detect fills, confirm, re-check until stable.

    Used when the price jumps multiple levels and cascade orders also
    fill at the same price.
    """
    all_signals: list[TradeSignal] = []
    while True:
        signals = s.on_price(price, state)
        if not signals:
            break
        for sig in signals:
            s.confirm_fill(sig, state)
        all_signals.extend(signals)
    return all_signals


# ===================================================================
# 1. Grid level computation
# ===================================================================

class TestGridLevelComputation:
    """Verify the grid levels array is constructed correctly."""

    def test_levels_count(self):
        s = StaticGridStrategy(_grid(levels=11))
        assert len(s.levels) == 11

    def test_levels_endpoints(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        assert s.levels[0] == pytest.approx(100.0)
        assert s.levels[-1] == pytest.approx(200.0)

    def test_levels_evenly_spaced(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        expected_step = 10.0
        for i in range(1, len(s.levels)):
            assert s.levels[i] - s.levels[i - 1] == pytest.approx(expected_step)

    def test_two_levels_minimum(self):
        s = StaticGridStrategy(_grid(50, 150, 2))
        assert s.levels == [pytest.approx(50.0), pytest.approx(150.0)]

    def test_three_levels(self):
        s = StaticGridStrategy(_grid(100, 200, 3))
        assert s.levels == [pytest.approx(100.0), pytest.approx(150.0), pytest.approx(200.0)]

    def test_large_number_of_levels(self):
        s = StaticGridStrategy(_grid(0.01, 100.0, 1000))
        assert len(s.levels) == 1000
        assert s.levels[0] == pytest.approx(0.01)
        assert s.levels[-1] == pytest.approx(100.0)

    def test_very_narrow_grid(self):
        s = StaticGridStrategy(_grid(100.0, 100.1, 11))
        assert len(s.levels) == 11
        assert s.levels[-1] == pytest.approx(100.1)

    def test_fractional_prices(self):
        s = StaticGridStrategy(_grid(0.001, 0.010, 10))
        assert s.levels[0] == pytest.approx(0.001)
        assert s.levels[-1] == pytest.approx(0.010)

    def test_wide_grid(self):
        s = StaticGridStrategy(_grid(1.0, 1_000_000.0, 101))
        assert s.levels[0] == pytest.approx(1.0)
        assert s.levels[-1] == pytest.approx(1_000_000.0)
        step = (1_000_000.0 - 1.0) / 100
        for i in range(1, len(s.levels)):
            assert s.levels[i] - s.levels[i - 1] == pytest.approx(step)


# ===================================================================
# 2. Nearest level index
# ===================================================================

class TestNearestLevelIndex:
    """Verify _nearest_level_index returns the correct grid level."""

    def test_exact_match_lower(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        assert s._nearest_level_index(100.0) == 0

    def test_exact_match_upper(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        assert s._nearest_level_index(200.0) == 10

    def test_exact_match_middle(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        assert s._nearest_level_index(150.0) == 5

    def test_price_between_levels_rounds_down(self):
        """Price slightly above a level should still snap to that level."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        # 110.1 is closer to 110 (idx 1) than to 120 (idx 2)
        assert s._nearest_level_index(110.1) == 1

    def test_price_between_levels_rounds_up(self):
        """Price closer to next level should snap up."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        # 119.9 is closer to 120 (idx 2) than to 110 (idx 1)
        assert s._nearest_level_index(119.9) == 2

    def test_price_exactly_midpoint(self):
        """Midpoint between two levels should snap to the lower one (first min)."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        # Midpoint between 100 (idx 0) and 110 (idx 1) is 105
        # Both have distance 5.0; index(min) returns the first occurrence → idx 0
        assert s._nearest_level_index(105.0) == 0

    def test_price_below_grid(self):
        """Price below the lowest level snaps to index 0."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        assert s._nearest_level_index(50.0) == 0

    def test_price_above_grid(self):
        """Price above the highest level snaps to the last index."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        assert s._nearest_level_index(300.0) == 10

    def test_price_far_below_grid(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        assert s._nearest_level_index(0.001) == 0

    def test_price_far_above_grid(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        assert s._nearest_level_index(1_000_000.0) == 10

    def test_all_exact_levels(self):
        """Every exact grid level maps to its own index."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        for i, level in enumerate(s.levels):
            assert s._nearest_level_index(level) == i

    def test_two_level_grid_midpoint(self):
        s = StaticGridStrategy(_grid(50, 150, 2))
        # Midpoint 100 is equidistant; expect lower index (0)
        assert s._nearest_level_index(100.0) == 0

    def test_two_level_grid_closer_to_upper(self):
        s = StaticGridStrategy(_grid(50, 150, 2))
        assert s._nearest_level_index(120.0) == 1


# ===================================================================
# 3. First price initialisation
# ===================================================================

class TestFirstPriceInit:
    """First on_price call initialises state and returns no signals."""

    def test_first_call_returns_empty(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        signals = s.on_price(150.0, state)
        assert signals == []

    def test_first_call_sets_level_index(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        assert state.level_index == 5

    def test_first_call_places_buys_below(self):
        """Init should place buy orders at all levels below current price."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5
        assert state.open_orders == {0: "buy", 1: "buy", 2: "buy", 3: "buy", 4: "buy"}

    def test_first_call_no_sells(self):
        """Init should NOT place any sell orders (no base currency held)."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        assert all(side == "buy" for side in state.open_orders.values())

    def test_first_call_at_lower_bound(self):
        """At the lowest level there is no level below — no orders placed."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(100.0, state)
        assert state.level_index == 0
        assert state.open_orders == {}

    def test_first_call_at_upper_bound(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(200.0, state)
        assert state.level_index == 10
        assert len(state.open_orders) == 10
        assert all(side == "buy" for side in state.open_orders.values())

    def test_first_call_below_grid(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(50.0, state)
        assert state.level_index == 0

    def test_first_call_above_grid(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(300.0, state)
        assert state.level_index == 10


# ===================================================================
# 4. Single-level movements (basic buy/sell)
# ===================================================================

class TestSingleLevelMovement:
    """Price crosses exactly one grid level."""

    def test_one_level_down_emits_buy(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # init at idx 5, buys at 0-4
        signals = s.on_price(140.0, state)  # fills buy at idx 4
        assert _sides(signals) == ["buy"]
        assert signals[0].quote_amount == pytest.approx(10.0)

    def test_buy_then_sell(self):
        """After a confirmed buy, sell is placed one level above — price rising fills it."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # init, buys at 0-4
        _tick(s, 140.0, state)    # fills buy at 4, confirm places sell at 5
        signals = s.on_price(150.0, state)  # fills sell at 5
        assert _sides(signals) == ["sell"]
        assert signals[0].quote_amount == pytest.approx(10.0)

    def test_sell_without_prior_buy_impossible(self):
        """Price rising from init should not produce sell (no base currency)."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # init, buys at 0-4
        signals = s.on_price(160.0, state)  # no sell order there
        assert signals == []

    def test_state_updated_after_buy(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        s.on_price(140.0, state)
        assert state.level_index == 4

    def test_state_updated_after_sell(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        _tick(s, 140.0, state)    # buy at 4 confirmed → sell at 5
        s.on_price(150.0, state)  # sell at 5 fills
        assert state.level_index == 5


# ===================================================================
# 5. Multi-level movements (cascading buys)
# ===================================================================

class TestMultiLevelMovement:
    """Price crosses multiple grid levels — buys cascade downward."""

    def test_cascade_two_buys(self):
        """Drop 2 levels: buy at 4 fills, confirm places sell at 5, buy at 3 already exists and fills."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # init, buys at 0-4
        signals = _process(s, 130.0, state)
        buy_signals = [sig for sig in signals if sig.side == "buy"]
        assert len(buy_signals) >= 2

    def test_cascade_fills_sell_and_buy(self):
        """After cascading buys confirmed, sells are placed above each filled buy."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        _process(s, 130.0, state)  # buys at 3 and 4 fill
        assert state.open_orders.get(5) == "sell"
        assert state.open_orders.get(4) == "sell"

    def test_sell_requires_prior_buy(self):
        """Rising price from init with no filled buys → no sells."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(100.0, state)  # idx 0, no level below → no buy order
        signals = s.on_price(200.0, state)  # no sell orders exist
        assert signals == []

    def test_gradual_decline_one_at_a_time(self):
        """Step-by-step decline: each step fills one buy."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5, buys at 0-4

        total_buys = 0
        for price in [140, 130, 120, 110, 100]:
            signals = _tick(s, float(price), state)
            buy_signals = [sig for sig in signals if sig.side == "buy"]
            assert len(buy_signals) == 1
            total_buys += len(buy_signals)
        assert total_buys == 5


# ===================================================================
# 6. No movement (same level)
# ===================================================================

class TestNoMovement:
    """Price stays on same level → no signals."""

    def test_same_exact_price(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        assert s.on_price(150.0, state) == []

    def test_small_fluctuation_within_level(self):
        """Price wiggles around a level without crossing the midpoint."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5
        assert s.on_price(151.0, state) == []
        assert s.on_price(149.0, state) == []
        assert s.on_price(150.5, state) == []

    def test_repeated_same_level(self):
        """Many calls at the same level produce no signals."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(120.0, state)
        for _ in range(50):
            assert s.on_price(120.0, state) == []


# ===================================================================
# 7. Boundary / edge prices
# ===================================================================

class TestBoundaryPrices:
    """Price at or beyond grid boundaries."""

    def test_price_stays_below_grid(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(50.0, state)
        assert s.on_price(30.0, state) == []

    def test_price_stays_above_grid(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(300.0, state)  # idx 10, buy at 9
        assert s.on_price(500.0, state) == []

    def test_drop_from_above_fills_cascading_buys(self):
        """Price dropping from above should fill the initial buy and cascade."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(200.0, state)  # idx 10, buy at 9 (price 190)
        signals = _process(s, 170.0, state)  # fills 9 → 8 → 7
        assert len(signals) == 3
        assert all(sig.side == "buy" for sig in signals)

    def test_rise_from_below_no_sells(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(50.0, state)
        signals = s.on_price(130.0, state)
        assert signals == []


# ===================================================================
# 8. Oscillation / zigzag patterns
# ===================================================================

class TestOscillation:
    """Price oscillating between levels produces alternating buy/sell."""

    def test_simple_zigzag(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        assert _sides(_tick(s, 140.0, state)) == ["buy"]
        assert _sides(_tick(s, 150.0, state)) == ["sell"]
        assert _sides(_tick(s, 140.0, state)) == ["buy"]
        assert _sides(_tick(s, 150.0, state)) == ["sell"]

    def test_rapid_adjacent_oscillation(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        for _ in range(20):
            buy_signals = _tick(s, 140.0, state)
            assert len(buy_signals) == 1 and buy_signals[0].side == "buy"
            sell_signals = _tick(s, 150.0, state)
            assert len(sell_signals) == 1 and sell_signals[0].side == "sell"

    def test_wider_oscillation_cascade(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        signals = _process(s, 130.0, state)
        assert len(signals) == 2 and all(sig.side == "buy" for sig in signals)

        signals = _process(s, 150.0, state)
        assert len(signals) == 2 and all(sig.side == "sell" for sig in signals)


# ===================================================================
# 9. State persistence and isolation
# ===================================================================

class TestStatePersistence:
    """State object is properly updated and strategies don't interfere."""

    def test_state_tracks_across_many_ticks(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        _tick(s, 140.0, state)
        assert state.level_index == 4

        _tick(s, 150.0, state)
        assert state.level_index == 5

    def test_separate_states_independent(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state_a = StrategyState()
        state_b = StrategyState()

        s.on_price(150.0, state_a)
        s.on_price(140.0, state_b)

        signals_a = s.on_price(140.0, state_a)
        signals_b = s.on_price(130.0, state_b)

        assert _sides(signals_a) == ["buy"]
        assert _sides(signals_b) == ["buy"]
        assert state_a.level_index == 4
        assert state_b.level_index == 3

    def test_fresh_state_always_inits(self):
        state = StrategyState()
        assert state.level_index is None


# ===================================================================
# 10. Order size correctness
# ===================================================================

class TestOrderSize:
    """Every signal uses the configured order_size_quote."""

    def test_buy_signal_amount(self):
        s = StaticGridStrategy(_grid(order_size=25.0))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(140.0, state)
        assert all(sig.quote_amount == pytest.approx(25.0) for sig in signals)

    def test_sell_signal_amount(self):
        s = StaticGridStrategy(_grid(order_size=25.0))
        state = StrategyState()
        s.on_price(150.0, state)
        _tick(s, 140.0, state)  # buy confirmed → sell at 5
        signals = s.on_price(150.0, state)
        assert all(sig.quote_amount == pytest.approx(25.0) for sig in signals)

    def test_cascade_all_same_amount(self):
        s = StaticGridStrategy(_grid(order_size=7.5))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = _process(s, 130.0, state)
        assert len(signals) == 2
        assert all(sig.quote_amount == pytest.approx(7.5) for sig in signals)

    def test_very_small_order_size(self):
        s = StaticGridStrategy(_grid(order_size=0.01))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(140.0, state)
        assert signals[0].quote_amount == pytest.approx(0.01)

    def test_very_large_order_size(self):
        s = StaticGridStrategy(_grid(order_size=1_000_000.0))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(140.0, state)
        assert signals[0].quote_amount == pytest.approx(1_000_000.0)


# ===================================================================
# 11. Production-critical scenarios
# ===================================================================

class TestProductionScenarios:
    """Scenarios that could lead to money loss if handled incorrectly."""

    def test_flash_crash_and_recovery(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        crash_signals = _process(s, 100.0, state)
        assert all(sig.side == "buy" for sig in crash_signals)
        assert len(crash_signals) == 5

        recovery_signals = _process(s, 150.0, state)
        assert all(sig.side == "sell" for sig in recovery_signals)
        assert len(recovery_signals) == 5

    def test_gradual_decline_step_by_step(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        total_buys = 0
        for price in [140, 130, 120, 110, 100]:
            signals = _tick(s, float(price), state)
            assert len(signals) == 1
            assert signals[0].side == "buy"
            total_buys += 1
        assert total_buys == 5

    def test_gradual_rise_after_buys(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        _tick(s, 140.0, state)
        _tick(s, 130.0, state)
        _tick(s, 120.0, state)

        total_sells = 0
        for price in [130, 140, 150]:
            signals = _tick(s, float(price), state)
            assert len(signals) == 1
            assert signals[0].side == "sell"
            total_sells += 1
        assert total_sells == 3

    def test_no_duplicate_signals_on_same_level(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        for _ in range(100):
            assert s.on_price(150.0, state) == []

    def test_symmetry_buy_then_sell(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        down = _tick(s, 140.0, state)
        up = _tick(s, 150.0, state)
        assert len(down) == len(up)
        assert all(d.side == "buy" for d in down)
        assert all(u.side == "sell" for u in up)

    def test_no_sell_without_base(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(100.0, state)
        for price in [110, 120, 130, 140, 150]:
            assert s.on_price(float(price), state) == []

    def test_staircase_down_then_up(self):
        s = StaticGridStrategy(_grid(100, 200, 6))
        state = StrategyState()
        s.on_price(160.0, state)

        assert _sides(_tick(s, 140.0, state)) == ["buy"]
        assert _sides(_tick(s, 120.0, state)) == ["buy"]
        assert _sides(_tick(s, 100.0, state)) == ["buy"]

        assert _sides(_tick(s, 120.0, state)) == ["sell"]
        assert _sides(_tick(s, 140.0, state)) == ["sell"]
        assert _sides(_tick(s, 160.0, state)) == ["sell"]

    def test_return_type_is_list_of_trade_signals(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(140.0, state)
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, TradeSignal)
            assert sig.side in ("buy", "sell")
            assert sig.quote_amount > 0

    def test_unconfirmed_buy_does_not_place_sell(self):
        """If a buy is detected but NOT confirmed, no sell order appears."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # buys at 0-4
        signals = s.on_price(140.0, state)  # detects buy at 4, removes it
        assert len(signals) == 1
        # Do NOT confirm — no sell should exist
        assert "sell" not in state.open_orders.values()
        # Buy at 4 is gone (detected but not confirmed), buys 0-3 remain
        assert 4 not in state.open_orders
        assert state.open_orders.get(3) == "buy"


# ===================================================================
# 12. Edge case: price moves near midpoint boundary
# ===================================================================

class TestMidpointBoundary:
    """Prices near the exact midpoint between two levels."""

    def test_just_below_midpoint_stays(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(154.99, state)
        assert signals == []
        assert state.level_index == 5

    def test_just_above_midpoint_moves(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(155.01, state)
        assert signals == []
        assert state.level_index == 6


# ===================================================================
# 13. Cumulative signal counting over a session
# ===================================================================

class TestCumulativeSignals:
    """Verify total signal count over complex price paths."""

    def test_total_signals_round_trip(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        all_signals = []
        all_signals.extend(_process(s, 100.0, state))
        all_signals.extend(_process(s, 150.0, state))

        buys = sum(1 for sig in all_signals if sig.side == "buy")
        sells = sum(1 for sig in all_signals if sig.side == "sell")
        assert buys == sells


# ===================================================================
# 14. Config edge cases
# ===================================================================

class TestConfigEdgeCases:
    """Unusual but valid grid configurations."""

    def test_very_small_price_range(self):
        g = _grid(lower=0.0001, upper=0.0002, levels=2)
        s = StaticGridStrategy(g)
        state = StrategyState()
        s.on_price(0.0002, state)
        signals = s.on_price(0.0001, state)
        assert _sides(signals) == ["buy"]

    def test_order_size_zero_rejected_by_config(self):
        with pytest.raises(Exception):
            _grid(order_size=0.0)

    def test_negative_order_size_rejected(self):
        with pytest.raises(Exception):
            _grid(order_size=-5.0)

    def test_extremely_many_levels(self):
        g = _grid(lower=1.0, upper=2.0, levels=10000)
        s = StaticGridStrategy(g)
        assert len(s.levels) == 10000
        state = StrategyState()
        s.on_price(1.5, state)
        # All levels with price strictly below 1.5 get buy orders
        expected = sum(1 for lv in s.levels if lv < 1.5)
        assert len(state.open_orders) == expected
        assert all(side == "buy" for side in state.open_orders.values())
