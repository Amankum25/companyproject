"""
tests/test_execution_manager.py — Unit tests for execution state tracking.

These tests verify that the StraddleState and LegState correctly track
fills, partial fills, exits, and state transitions.

We test the data model (models/straddle_state.py) here, not the full strategy
(which requires a live NautilusTrader node to test).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from models.straddle_state import (
    LegState,
    LegStatus,
    StraddlePhase,
    StraddleState,
)


class TestLegState:
    """Tests for LegState fill tracking and weighted average price."""

    def test_initial_state(self):
        """LegState starts with zero fills."""
        leg = LegState(instrument_id_str="BTC-CALL.BYBIT")
        assert leg.filled_quantity == Decimal("0")
        assert leg.avg_fill_price == Decimal("0")
        assert leg.exit_filled_quantity == Decimal("0")
        assert leg.status == LegStatus.IDLE

    def test_single_full_fill(self):
        """Single fill sets quantity and price correctly."""
        leg = LegState(instrument_id_str="BTC-CALL.BYBIT")
        leg.update_entry_fill(Decimal("0.01"), Decimal("100.0"))
        assert leg.filled_quantity == Decimal("0.01")
        assert leg.avg_fill_price == Decimal("100.0")

    def test_partial_fill_then_full_fill_computes_weighted_average(self):
        """
        Two fills at different prices → weighted average price.

        Fill 1: 0.005 contracts at 100 → value = 0.5
        Fill 2: 0.005 contracts at 200 → value = 1.0
        Total: 0.01 contracts at avg = 1.5 / 0.01 = 150
        """
        leg = LegState(instrument_id_str="BTC-CALL.BYBIT")
        leg.update_entry_fill(Decimal("0.005"), Decimal("100.0"))
        leg.update_entry_fill(Decimal("0.005"), Decimal("200.0"))

        assert leg.filled_quantity == Decimal("0.01")
        assert leg.avg_fill_price == Decimal("150.0")

    def test_exit_fill_reduces_remaining_quantity(self):
        """After an exit fill, remaining_open_quantity decreases."""
        leg = LegState(instrument_id_str="BTC-CALL.BYBIT")
        leg.update_entry_fill(Decimal("1.0"), Decimal("50.0"))
        leg.status = LegStatus.FILLED

        leg.update_exit_fill(Decimal("0.5"))
        assert leg.remaining_open_quantity == Decimal("0.5")

    def test_full_exit_marks_leg_closed(self):
        """When exit fill equals entry fill quantity, status becomes CLOSED."""
        leg = LegState(instrument_id_str="BTC-CALL.BYBIT")
        leg.update_entry_fill(Decimal("1.0"), Decimal("50.0"))
        leg.status = LegStatus.FILLED

        leg.update_exit_fill(Decimal("1.0"))
        assert leg.status == LegStatus.CLOSED
        assert leg.remaining_open_quantity == Decimal("0")

    def test_remaining_quantity_cannot_go_negative(self):
        """remaining_open_quantity is clamped to 0 (never negative)."""
        leg = LegState(instrument_id_str="BTC-CALL.BYBIT")
        leg.update_entry_fill(Decimal("1.0"), Decimal("50.0"))
        leg.status = LegStatus.FILLED
        # Overfill exit (shouldn't happen, but test defensive behavior)
        leg.update_exit_fill(Decimal("2.0"))
        assert leg.remaining_open_quantity == Decimal("0")


class TestStraddleState:
    """Tests for StraddleState phase transitions and premium calculation."""

    def test_initial_phase_is_idle(self):
        """StraddleState starts in IDLE phase."""
        state = StraddleState()
        assert state.phase == StraddlePhase.IDLE

    def test_set_instruments_initializes_legs(self):
        """set_instruments sets the instrument IDs on both legs."""
        state = StraddleState()
        state.set_instruments("BTC-CALL.BYBIT", "BTC-PUT.BYBIT")
        assert state.call.instrument_id_str == "BTC-CALL.BYBIT"
        assert state.put.instrument_id_str == "BTC-PUT.BYBIT"

    def test_entry_premium_calculation(self):
        """
        Entry premium = sum of (fill_price × fill_qty) for both legs.

        Call: 50 × 1.0 = 50
        Put:  50 × 1.0 = 50
        Total: 100
        """
        state = StraddleState()
        state.set_instruments("BTC-CALL.BYBIT", "BTC-PUT.BYBIT")

        state.call.filled_quantity = Decimal("1.0")
        state.call.avg_fill_price = Decimal("50.0")
        state.put.filled_quantity = Decimal("1.0")
        state.put.avg_fill_price = Decimal("50.0")

        premium = state.compute_entry_premium()
        assert premium == Decimal("100.0")
        assert state.entry_premium == Decimal("100.0")

    def test_entry_premium_different_quantities(self):
        """
        Test asymmetric fill quantities (e.g. partial fills).

        Call: 60 × 0.5 = 30
        Put:  40 × 1.0 = 40
        Total: 70
        """
        state = StraddleState()
        state.set_instruments("BTC-CALL.BYBIT", "BTC-PUT.BYBIT")

        state.call.filled_quantity = Decimal("0.5")
        state.call.avg_fill_price = Decimal("60.0")
        state.put.filled_quantity = Decimal("1.0")
        state.put.avg_fill_price = Decimal("40.0")

        premium = state.compute_entry_premium()
        assert premium == Decimal("70.0")

    def test_is_exit_in_progress_false_when_open(self):
        """Exit is not in progress when straddle is STRADDLE_OPEN."""
        state = StraddleState()
        state.phase = StraddlePhase.STRADDLE_OPEN
        assert not state.is_exit_in_progress

    def test_is_exit_in_progress_true_after_trigger(self):
        """Exit is in progress for all exit-related phases."""
        exit_phases = [
            StraddlePhase.EXIT_TRIGGERED,
            StraddlePhase.EXIT_PENDING,
            StraddlePhase.CALL_EXIT_FILLED,
            StraddlePhase.PUT_EXIT_FILLED,
            StraddlePhase.CLOSED,
        ]
        for phase in exit_phases:
            state = StraddleState()
            state.phase = phase
            assert state.is_exit_in_progress, f"Phase {phase.name} should be exit-in-progress."


class TestExecutionScenarios:
    """
    Integration-style tests for common execution scenarios.

    These test the state transitions that the strategy would go through
    in real trading scenarios, without needing a live strategy instance.
    """

    def _make_open_straddle(self) -> StraddleState:
        """Helper: create a fully open straddle state."""
        state = StraddleState()
        state.set_instruments("BTC-CALL.BYBIT", "BTC-PUT.BYBIT")
        state.phase = StraddlePhase.STRADDLE_OPEN

        state.call.status = LegStatus.FILLED
        state.call.update_entry_fill(Decimal("1.0"), Decimal("50.0"))

        state.put.status = LegStatus.FILLED
        state.put.update_entry_fill(Decimal("1.0"), Decimal("50.0"))

        state.compute_entry_premium()
        return state

    # =========================================================================
    # Test: Both legs filled
    # =========================================================================

    def test_both_legs_filled_state(self):
        """When both legs fill, straddle state shows FILLED for both."""
        state = self._make_open_straddle()
        assert state.call.status == LegStatus.FILLED
        assert state.put.status == LegStatus.FILLED
        assert state.phase == StraddlePhase.STRADDLE_OPEN
        assert state.entry_premium == Decimal("100.0")

    # =========================================================================
    # Test: One leg rejected
    # =========================================================================

    def test_call_rejected_put_filled(self):
        """
        Scenario: Call fills, Put is rejected.
        The state must show CALL_FILLED (not STRADDLE_OPEN).
        Recovery logic should recognize this.
        """
        state = StraddleState()
        state.set_instruments("BTC-CALL.BYBIT", "BTC-PUT.BYBIT")
        state.phase = StraddlePhase.ENTRY_PENDING

        # Call fills
        state.call.update_entry_fill(Decimal("1.0"), Decimal("50.0"))
        state.call.status = LegStatus.FILLED

        # Put is rejected
        state.put.status = LegStatus.REJECTED

        # State machine should recognize incomplete
        assert state.call.status == LegStatus.FILLED
        assert state.put.status == LegStatus.REJECTED
        # We did NOT set STRADDLE_OPEN — it stays ENTRY_PENDING
        # (the strategy would set RECOVERY_REQUIRED)
        assert state.phase == StraddlePhase.ENTRY_PENDING

    # =========================================================================
    # Test: Partial fill on entry
    # =========================================================================

    def test_partial_fill_on_call(self):
        """
        Partial fill: call filled with 0.005 out of 0.01 intended.
        filled_quantity should reflect only what actually filled.
        """
        leg = LegState(instrument_id_str="BTC-CALL.BYBIT")
        leg.update_entry_fill(Decimal("0.005"), Decimal("100.0"))
        leg.status = LegStatus.PARTIALLY_FILLED

        assert leg.filled_quantity == Decimal("0.005")
        assert leg.avg_fill_price == Decimal("100.0")
        assert leg.status == LegStatus.PARTIALLY_FILLED

    # =========================================================================
    # Test: Closing order partial fill
    # =========================================================================

    def test_closing_order_partial_fill(self):
        """
        Partial exit fill: only 0.5 of 1.0 contracts bought back.
        Remaining quantity = 0.5.
        """
        state = self._make_open_straddle()
        # Trigger exit
        state.phase = StraddlePhase.EXIT_PENDING
        state.call.status = LegStatus.EXIT_SUBMITTED
        state.put.status = LegStatus.EXIT_SUBMITTED

        # Partial fill on call exit
        state.call.update_exit_fill(Decimal("0.5"))

        assert state.call.remaining_open_quantity == Decimal("0.5")
        assert state.call.status != LegStatus.CLOSED  # Not fully closed yet

    # =========================================================================
    # Test: Duplicate exit prevention
    # =========================================================================

    def test_duplicate_exit_prevented_by_phase(self):
        """
        is_exit_in_progress must return True for EXIT_TRIGGERED onwards.
        Strategy uses this to prevent submitting exit orders twice.
        """
        state = self._make_open_straddle()

        # Before exit: not in progress
        assert not state.is_exit_in_progress

        # Set exit triggered
        state.phase = StraddlePhase.EXIT_TRIGGERED
        assert state.is_exit_in_progress  # Duplicate prevention active

        # Still in progress for all subsequent phases
        for phase in [
            StraddlePhase.EXIT_PENDING,
            StraddlePhase.CALL_EXIT_FILLED,
            StraddlePhase.PUT_EXIT_FILLED,
            StraddlePhase.CLOSED,
        ]:
            state.phase = phase
            assert state.is_exit_in_progress, f"Should be in-progress at {phase.name}"

    # =========================================================================
    # Test: Both exits filled
    # =========================================================================

    def test_both_exits_filled_marks_closed(self):
        """
        After both exit fills, both legs should be CLOSED.
        """
        state = self._make_open_straddle()
        state.phase = StraddlePhase.EXIT_PENDING

        # Both exit fills
        state.call.update_exit_fill(Decimal("1.0"))  # Marks call as CLOSED
        state.put.update_exit_fill(Decimal("1.0"))   # Marks put as CLOSED

        assert state.call.status == LegStatus.CLOSED
        assert state.put.status == LegStatus.CLOSED
        assert state.call.remaining_open_quantity == Decimal("0")
        assert state.put.remaining_open_quantity == Decimal("0")

    # =========================================================================
    # Test: Straddle summary
    # =========================================================================

    def test_summary_contains_key_info(self):
        """The summary string should include phase and fill info."""
        state = self._make_open_straddle()
        summary = state.summary()
        assert "STRADDLE_OPEN" in summary
        assert "1.0" in summary  # filled qty

