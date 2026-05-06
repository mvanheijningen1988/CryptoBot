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

    def test_first_call_at_lower_bound(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(100.0, state)
        assert state.level_index == 0
        assert s.on_price(100.0, state) == []  # no move, no signals

    def test_first_call_at_upper_bound(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(200.0, state)
        assert state.level_index == 10

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
        s.on_price(150.0, state)  # init at idx 5
        signals = s.on_price(140.0, state)  # idx 4 → 1 buy
        assert _sides(signals) == ["buy"]
        assert signals[0].quote_amount == 10.0

    def test_one_level_up_emits_sell(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # init at idx 5
        signals = s.on_price(160.0, state)  # idx 6 → 1 sell
        assert _sides(signals) == ["sell"]
        assert signals[0].quote_amount == 10.0

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
        s.on_price(160.0, state)
        assert state.level_index == 6


# ===================================================================
# 5. Multi-level movements
# ===================================================================

class TestMultiLevelMovement:
    """Price crosses multiple grid levels in one tick."""

    def test_two_levels_down_emits_two_buys(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(130.0, state)  # idx 5 → idx 3
        assert _sides(signals) == ["buy", "buy"]

    def test_three_levels_up_emits_three_sells(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(180.0, state)  # idx 5 → idx 8
        assert _sides(signals) == ["sell", "sell", "sell"]

    def test_full_grid_drop_bottom_to_top(self):
        """Price jumps from top to bottom: expect levels-1 buys."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(200.0, state)  # init at idx 10
        signals = s.on_price(100.0, state)
        assert _sides(signals) == ["buy"] * 10

    def test_full_grid_rise_bottom_to_top(self):
        """Price jumps from bottom to top: expect levels-1 sells."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(100.0, state)
        signals = s.on_price(200.0, state)
        assert _sides(signals) == ["sell"] * 10

    def test_five_levels_down(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(200.0, state)  # idx 10
        signals = s.on_price(150.0, state)  # idx 5
        assert len(signals) == 5
        assert all(sig.side == "buy" for sig in signals)


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
        # Stay close to 150
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
        """Once initialised below the grid, staying below produces no signals."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(50.0, state)  # idx 0
        assert s.on_price(30.0, state) == []

    def test_price_stays_above_grid(self):
        """Once initialised above the grid, staying above produces no signals."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(300.0, state)  # idx 10
        assert s.on_price(500.0, state) == []

    def test_drop_from_above_to_within(self):
        """Coming from above the grid into the grid should trigger buys."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(300.0, state)  # snaps to idx 10
        signals = s.on_price(170.0, state)  # idx 7 → 3 buys
        assert _sides(signals) == ["buy"] * 3

    def test_rise_from_below_to_within(self):
        """Coming from below the grid into the grid should trigger sells."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(50.0, state)  # snaps to idx 0
        signals = s.on_price(130.0, state)  # idx 3 → 3 sells
        assert _sides(signals) == ["sell"] * 3

    def test_from_below_to_above_entire_grid(self):
        """Price jumps from below grid to above grid → all levels crossed upward."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(10.0, state)  # idx 0
        signals = s.on_price(500.0, state)  # idx 10 → 10 sells
        assert _sides(signals) == ["sell"] * 10

    def test_from_above_to_below_entire_grid(self):
        """Price jumps from above grid to below grid → all levels crossed downward."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(500.0, state)  # idx 10
        signals = s.on_price(10.0, state)  # idx 0 → 10 buys
        assert _sides(signals) == ["buy"] * 10


# ===================================================================
# 8. Oscillation / zigzag patterns
# ===================================================================

class TestOscillation:
    """Price oscillating between levels produces alternating signals."""

    def test_simple_zigzag(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        assert _sides(s.on_price(140.0, state)) == ["buy"]
        assert _sides(s.on_price(150.0, state)) == ["sell"]
        assert _sides(s.on_price(140.0, state)) == ["buy"]
        assert _sides(s.on_price(150.0, state)) == ["sell"]

    def test_rapid_adjacent_oscillation(self):
        """Oscillating one level up and down repeatedly."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5

        for _ in range(20):
            buy_signals = s.on_price(140.0, state)
            assert len(buy_signals) == 1 and buy_signals[0].side == "buy"
            sell_signals = s.on_price(150.0, state)
            assert len(sell_signals) == 1 and sell_signals[0].side == "sell"

    def test_wider_oscillation(self):
        """Oscillating 3 levels each way."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5

        # Drop 3 levels
        signals = s.on_price(120.0, state)  # idx 2
        assert len(signals) == 3 and all(s.side == "buy" for s in signals)

        # Rise 3 levels
        signals = s.on_price(150.0, state)  # idx 5
        assert len(signals) == 3 and all(s.side == "sell" for s in signals)


# ===================================================================
# 9. State persistence and isolation
# ===================================================================

class TestStatePersistence:
    """State object is properly updated and strategies don't interfere."""

    def test_state_tracks_across_many_ticks(self):
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(100.0, state)  # idx 0

        s.on_price(130.0, state)  # idx 3 → 3 sells
        assert state.level_index == 3

        s.on_price(110.0, state)  # idx 1 → 2 buys
        assert state.level_index == 1

        s.on_price(190.0, state)  # idx 9 → 8 sells
        assert state.level_index == 9

    def test_separate_states_independent(self):
        """Two state objects used with the same strategy don't interfere."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state_a = StrategyState()
        state_b = StrategyState()

        s.on_price(150.0, state_a)  # init A at idx 5
        s.on_price(100.0, state_b)  # init B at idx 0

        signals_a = s.on_price(140.0, state_a)  # A: idx 5→4 → 1 buy
        signals_b = s.on_price(120.0, state_b)  # B: idx 0→2 → 2 sells

        assert _sides(signals_a) == ["buy"]
        assert _sides(signals_b) == ["sell", "sell"]
        assert state_a.level_index == 4
        assert state_b.level_index == 2

    def test_fresh_state_always_inits(self):
        """A fresh StrategyState starts with level_index = None."""
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
        assert all(sig.quote_amount == 25.0 for sig in signals)

    def test_sell_signal_amount(self):
        s = StaticGridStrategy(_grid(order_size=25.0))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(160.0, state)
        assert all(sig.quote_amount == 25.0 for sig in signals)

    def test_multi_level_all_same_amount(self):
        s = StaticGridStrategy(_grid(order_size=7.5))
        state = StrategyState()
        s.on_price(200.0, state)
        signals = s.on_price(100.0, state)  # 10 buys
        assert len(signals) == 10
        assert all(sig.quote_amount == 7.5 for sig in signals)

    def test_very_small_order_size(self):
        s = StaticGridStrategy(_grid(order_size=0.01))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(140.0, state)
        assert signals[0].quote_amount == 0.01

    def test_very_large_order_size(self):
        s = StaticGridStrategy(_grid(order_size=1_000_000.0))
        state = StrategyState()
        s.on_price(150.0, state)
        signals = s.on_price(140.0, state)
        assert signals[0].quote_amount == 1_000_000.0


# ===================================================================
# 11. Production-critical scenarios
# ===================================================================

class TestProductionScenarios:
    """Scenarios that could lead to money loss if handled incorrectly."""

    def test_flash_crash_and_recovery(self):
        """Sudden drop then recovery should produce buys then sells, net zero signals."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        crash_signals = s.on_price(100.0, state)  # 5 buys
        assert _sides(crash_signals) == ["buy"] * 5

        recovery_signals = s.on_price(150.0, state)  # 5 sells
        assert _sides(recovery_signals) == ["sell"] * 5

    def test_gradual_decline(self):
        """Step-by-step price decline produces one buy per step."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(200.0, state)

        total_buys = 0
        for price in [190, 180, 170, 160, 150, 140, 130, 120, 110, 100]:
            signals = s.on_price(float(price), state)
            assert len(signals) == 1
            assert signals[0].side == "buy"
            total_buys += 1

        assert total_buys == 10

    def test_gradual_rise(self):
        """Step-by-step price rise produces one sell per step."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(100.0, state)

        total_sells = 0
        for price in [110, 120, 130, 140, 150, 160, 170, 180, 190, 200]:
            signals = s.on_price(float(price), state)
            assert len(signals) == 1
            assert signals[0].side == "sell"
            total_sells += 1

        assert total_sells == 10

    def test_no_duplicate_signals_on_same_level(self):
        """Calling on_price repeatedly at the same level never generates signals."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)
        for _ in range(100):
            assert s.on_price(150.0, state) == []

    def test_symmetry_buy_then_sell(self):
        """For any round-trip the number of buy signals equals sell signals."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)

        down = s.on_price(110.0, state)  # 4 buys
        up = s.on_price(150.0, state)    # 4 sells
        assert len(down) == len(up)
        assert all(d.side == "buy" for d in down)
        assert all(u.side == "sell" for u in up)

    def test_price_at_exact_grid_boundaries_no_overshoot(self):
        """Exact boundary prices should not produce extra signals."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(100.0, state)  # idx 0

        # Move to exact last level
        signals = s.on_price(200.0, state)
        assert len(signals) == 10  # exactly levels - 1

        # Back to exact first level
        signals = s.on_price(100.0, state)
        assert len(signals) == 10  # exactly levels - 1

    def test_micro_grid_high_frequency_scenario(self):
        """Very tight grid with many levels simulating HFT-like conditions."""
        g = _grid(lower=99.0, upper=101.0, levels=201, order_size=1.0)
        s = StaticGridStrategy(g)
        state = StrategyState()

        # Step size = 0.01
        s.on_price(100.0, state)  # init at midpoint (~idx 100)
        signals = s.on_price(99.5, state)  # ~50 levels down
        # Should be ~50 buys
        assert all(sig.side == "buy" for sig in signals)
        assert len(signals) == 50

    def test_very_large_price_move_does_not_miss_levels(self):
        """A massive move should emit one signal per crossed level."""
        g = _grid(lower=10.0, upper=10000.0, levels=1000)
        s = StaticGridStrategy(g)
        state = StrategyState()
        s.on_price(10.0, state)  # idx 0
        signals = s.on_price(10000.0, state)  # idx 999
        assert len(signals) == 999
        assert all(sig.side == "sell" for sig in signals)

    def test_mixed_direction_sequence(self):
        """Complex up/down/up/down sequence tracks state correctly."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5

        # Down to idx 3 → 2 buys
        assert len(s.on_price(130.0, state)) == 2

        # Up to idx 7 → 4 sells
        assert len(s.on_price(170.0, state)) == 4

        # Down to idx 1 → 6 buys
        assert len(s.on_price(110.0, state)) == 6

        # Up to idx 9 → 8 sells
        assert len(s.on_price(190.0, state)) == 8

        assert state.level_index == 9

    def test_staircase_down_then_up(self):
        """Price goes down one level at a time then back up."""
        s = StaticGridStrategy(_grid(100, 200, 6))  # levels: 100,120,140,160,180,200
        state = StrategyState()
        s.on_price(160.0, state)  # idx 3

        # Down one at a time
        assert _sides(s.on_price(140.0, state)) == ["buy"]     # idx 2
        assert _sides(s.on_price(120.0, state)) == ["buy"]     # idx 1
        assert _sides(s.on_price(100.0, state)) == ["buy"]     # idx 0

        # Up one at a time
        assert _sides(s.on_price(120.0, state)) == ["sell"]    # idx 1
        assert _sides(s.on_price(140.0, state)) == ["sell"]    # idx 2
        assert _sides(s.on_price(160.0, state)) == ["sell"]    # idx 3

    def test_return_type_is_list_of_trade_signals(self):
        """Ensure all returned items are TradeSignal instances."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(200.0, state)
        signals = s.on_price(100.0, state)
        assert isinstance(signals, list)
        for sig in signals:
            assert isinstance(sig, TradeSignal)
            assert sig.side in ("buy", "sell")
            assert sig.quote_amount > 0


# ===================================================================
# 12. Edge case: price moves near midpoint boundary
# ===================================================================

class TestMidpointBoundary:
    """Prices near the exact midpoint between two levels."""

    def test_just_below_midpoint_stays(self):
        """Price just below midpoint should snap to lower level."""
        s = StaticGridStrategy(_grid(100, 200, 11))  # levels every 10
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5
        # 154.99 → closer to 150 (distance 4.99) than 160 (distance 5.01)
        signals = s.on_price(154.99, state)
        assert signals == []
        assert state.level_index == 5

    def test_just_above_midpoint_moves(self):
        """Price just above midpoint should snap to upper level → sell."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5
        # 155.01 → closer to 160 (distance 4.99) than 150 (distance 5.01)
        signals = s.on_price(155.01, state)
        assert _sides(signals) == ["sell"]
        assert state.level_index == 6


# ===================================================================
# 13. Cumulative signal counting over a session
# ===================================================================

class TestCumulativeSignals:
    """Verify total signal count over complex price paths."""

    def test_total_signals_round_trip(self):
        """A full down-and-up trip produces 2*(levels-1) signals total."""
        s = StaticGridStrategy(_grid(100, 200, 11))
        state = StrategyState()
        s.on_price(150.0, state)  # idx 5

        all_signals = []
        all_signals.extend(s.on_price(100.0, state))  # 5 buys
        all_signals.extend(s.on_price(200.0, state))  # 10 sells
        all_signals.extend(s.on_price(100.0, state))  # 10 buys
        all_signals.extend(s.on_price(150.0, state))  # 5 sells

        buys = sum(1 for sig in all_signals if sig.side == "buy")
        sells = sum(1 for sig in all_signals if sig.side == "sell")
        assert buys == 15
        assert sells == 15


# ===================================================================
# 14. Config edge cases
# ===================================================================

class TestConfigEdgeCases:
    """Unusual but valid grid configurations."""

    def test_very_small_price_range(self):
        g = _grid(lower=0.0001, upper=0.0002, levels=2)
        s = StaticGridStrategy(g)
        state = StrategyState()
        s.on_price(0.0001, state)
        signals = s.on_price(0.0002, state)
        assert _sides(signals) == ["sell"]

    def test_order_size_zero_rejected_by_config(self):
        """GridConfig rejects order_size_quote <= 0."""
        with pytest.raises(Exception):
            _grid(order_size=0.0)

    def test_negative_order_size_rejected(self):
        """GridConfig rejects negative order sizes."""
        with pytest.raises(Exception):
            _grid(order_size=-5.0)

    def test_extremely_many_levels(self):
        """Grid with 10,000 levels should still function correctly."""
        g = _grid(lower=1.0, upper=2.0, levels=10000)
        s = StaticGridStrategy(g)
        assert len(s.levels) == 10000
        state = StrategyState()
        s.on_price(1.0, state)
        signals = s.on_price(2.0, state)
        assert len(signals) == 9999
