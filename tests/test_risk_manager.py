"""
tests/test_risk_manager.py — Unit tests for Stop-Loss and Take-Profit logic.

These tests verify the RiskManager's signal generation without any live connection.
We build StraddleState objects manually and pass them to the risk manager.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from models.straddle_state import LegState, LegStatus, StraddlePhase, StraddleState
from strategy.risk_manager import RiskManager, RiskSignal


NOW_NS = int(time.time() * 1e9)


def make_open_straddle(
    call_fill_price: float,
    call_qty: float,
    put_fill_price: float,
    put_qty: float,
) -> StraddleState:
    """
    Build a StraddleState in STRADDLE_OPEN phase with actual fills.

    This simulates the state after both entry orders have been fully filled.
    """
    state = StraddleState()
    state.phase = StraddlePhase.STRADDLE_OPEN

    state.call = LegState(instrument_id_str="BTC-CALL.BYBIT")
    state.call.status = LegStatus.FILLED
    state.call.filled_quantity = Decimal(str(call_qty))
    state.call.avg_fill_price = Decimal(str(call_fill_price))

    state.put = LegState(instrument_id_str="BTC-PUT.BYBIT")
    state.put.status = LegStatus.FILLED
    state.put.filled_quantity = Decimal(str(put_qty))
    state.put.avg_fill_price = Decimal(str(put_fill_price))

    # Compute the actual entry premium
    state.compute_entry_premium()

    return state


class TestRiskManagerSignals:
    """Test that the correct signals are generated based on buyback prices."""

    def setup_method(self):
        self.rm = RiskManager(
            stop_loss_pct=0.01,       # 1% SL
            take_profit_pct=0.02,     # 2% TP
            quote_staleness_secs=60.0,  # 60 seconds
        )
        # Quote timestamps: fresh (just now)
        self.fresh_ts = NOW_NS
        # Quote timestamps: stale (2 minutes ago)
        self.stale_ts = NOW_NS - int(120 * 1e9)

    def _evaluate(
        self,
        state: StraddleState,
        call_ask: float,
        put_ask: float,
        ts: int = None,
    ):
        ts = ts or self.fresh_ts
        return self.rm.evaluate(
            state=state,
            call_ask_price=Decimal(str(call_ask)),
            put_ask_price=Decimal(str(put_ask)),
            call_ask_ts_ns=ts,
            put_ask_ts_ns=ts,
            current_time_ns=NOW_NS,
        )

    # =========================================================================
    # Test: Entry premium calculation
    # =========================================================================

    def test_entry_premium_is_sum_of_legs(self):
        """entry_premium = call_fill_price × call_qty + put_fill_price × put_qty"""
        state = make_open_straddle(
            call_fill_price=5.0, call_qty=0.01,
            put_fill_price=5.0, put_qty=0.01,
        )
        # 5.0 × 0.01 + 5.0 × 0.01 = 0.05 + 0.05 = 0.10
        assert state.entry_premium == Decimal("0.10")

    def test_entry_premium_larger_quantities(self):
        """Check premium with quantity 1 — simpler numbers for verification."""
        state = make_open_straddle(
            call_fill_price=50.0, call_qty=1.0,
            put_fill_price=50.0, put_qty=1.0,
        )
        assert state.entry_premium == Decimal("100.0")

    # =========================================================================
    # Test: HOLD signal (no action needed)
    # =========================================================================

    def test_hold_when_buyback_between_thresholds(self):
        """
        Entry premium = 100 USDT
        SL threshold  = 101 USDT
        TP threshold  =  98 USDT

        Buyback = 99.5 USDT → should HOLD
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # Buyback: 49.75 + 49.75 = 99.5 (between 98 and 101)
        snapshot = self._evaluate(state, call_ask=49.75, put_ask=49.75)
        assert snapshot.signal == RiskSignal.HOLD

    # =========================================================================
    # Test: STOP_LOSS signal
    # =========================================================================

    def test_stop_loss_triggered_above_threshold(self):
        """
        Entry premium = 100, SL threshold = 101.
        Buyback = 102 → SL triggered.
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        snapshot = self._evaluate(state, call_ask=51.0, put_ask=51.0)
        assert snapshot.signal == RiskSignal.STOP_LOSS

    def test_stop_loss_triggered_exactly_at_threshold(self):
        """
        Entry premium = 100, SL threshold = 101.
        Buyback = 101 exactly → SL triggered (>= comparison).
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # Exactly 101: call_ask=50.5, put_ask=50.5
        snapshot = self._evaluate(state, call_ask=50.5, put_ask=50.5)
        assert snapshot.signal == RiskSignal.STOP_LOSS

    def test_stop_loss_not_triggered_just_below_threshold(self):
        """
        Entry premium = 100, SL threshold = 101.
        Buyback = 100.99 → should NOT trigger SL (< 101).
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # 50.49 + 50.50 = 100.99
        snapshot = self._evaluate(state, call_ask=50.49, put_ask=50.50)
        assert snapshot.signal == RiskSignal.HOLD

    # =========================================================================
    # Test: TAKE_PROFIT signal
    # =========================================================================

    def test_take_profit_triggered_below_threshold(self):
        """
        Entry premium = 100, TP threshold = 98.
        Buyback = 97 → TP triggered.
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        snapshot = self._evaluate(state, call_ask=48.5, put_ask=48.5)
        assert snapshot.signal == RiskSignal.TAKE_PROFIT

    def test_take_profit_triggered_exactly_at_threshold(self):
        """
        Entry premium = 100, TP threshold = 98.
        Buyback = 98 exactly → TP triggered (<= comparison).
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # Exactly 98: call_ask=49.0, put_ask=49.0
        snapshot = self._evaluate(state, call_ask=49.0, put_ask=49.0)
        assert snapshot.signal == RiskSignal.TAKE_PROFIT

    def test_take_profit_not_triggered_just_above_threshold(self):
        """
        Entry premium = 100, TP threshold = 98.
        Buyback = 98.01 → should NOT trigger TP.
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # 49.01 + 49.00 = 98.01
        snapshot = self._evaluate(state, call_ask=49.01, put_ask=49.00)
        assert snapshot.signal == RiskSignal.HOLD

    # =========================================================================
    # Test: Stale quotes
    # =========================================================================

    def test_stale_quotes_return_data_missing(self):
        """
        If quote timestamps are older than the staleness threshold,
        the signal must be DATA_MISSING, not SL or TP.
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # Buyback = 102 (would trigger SL) but quotes are stale
        snapshot = self.rm.evaluate(
            state=state,
            call_ask_price=Decimal("51.0"),
            put_ask_price=Decimal("51.0"),
            call_ask_ts_ns=self.stale_ts,  # 2 minutes old
            put_ask_ts_ns=self.stale_ts,
            current_time_ns=NOW_NS,
        )
        assert snapshot.signal == RiskSignal.DATA_MISSING

    def test_one_stale_quote_returns_data_missing(self):
        """If only ONE quote is stale, signal must still be DATA_MISSING."""
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        snapshot = self.rm.evaluate(
            state=state,
            call_ask_price=Decimal("51.0"),
            put_ask_price=Decimal("51.0"),
            call_ask_ts_ns=self.fresh_ts,   # Fresh
            put_ask_ts_ns=self.stale_ts,    # Stale
            current_time_ns=NOW_NS,
        )
        assert snapshot.signal == RiskSignal.DATA_MISSING

    def test_missing_ask_price_returns_data_missing(self):
        """If ASK price is None, signal must be DATA_MISSING."""
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        snapshot = self.rm.evaluate(
            state=state,
            call_ask_price=None,
            put_ask_price=Decimal("51.0"),
            call_ask_ts_ns=self.fresh_ts,
            put_ask_ts_ns=self.fresh_ts,
            current_time_ns=NOW_NS,
        )
        assert snapshot.signal == RiskSignal.DATA_MISSING

    # =========================================================================
    # Test: Wrong phase
    # =========================================================================

    def test_data_missing_when_phase_not_open(self):
        """Risk manager should not evaluate when straddle is not fully open."""
        state = StraddleState()
        state.phase = StraddlePhase.ENTRY_PENDING  # Not open yet

        snapshot = self._evaluate(state, call_ask=51.0, put_ask=51.0)
        assert snapshot.signal == RiskSignal.DATA_MISSING

    # =========================================================================
    # Test: Partial fill handling
    # =========================================================================

    def test_buyback_uses_remaining_open_quantity(self):
        """
        If part of the position has already been closed (partial exit),
        buyback should only count the REMAINING quantity.
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # Simulate partial exit: 0.5 contracts of call already closed
        state.call.exit_filled_quantity = Decimal("0.5")
        # Remaining call qty = 1.0 - 0.5 = 0.5

        # Buyback: call_ask × 0.5 + put_ask × 1.0 = 51 × 0.5 + 51 × 1.0 = 25.5 + 51 = 76.5
        # Entry premium = 100
        # SL threshold = 101 → NOT triggered (76.5 < 101)
        snapshot = self._evaluate(state, call_ask=51.0, put_ask=51.0)
        assert snapshot.call_qty == Decimal("0.5")
        assert snapshot.put_qty == Decimal("1.0")

    # =========================================================================
    # Test: Snapshot values
    # =========================================================================

    def test_snapshot_contains_correct_thresholds(self):
        """
        Verify that the snapshot's threshold values are correct.
        Entry = 100, SL = 101, TP = 98.
        """
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        snapshot = self._evaluate(state, call_ask=49.75, put_ask=49.75)

        assert snapshot.entry_premium == Decimal("100.0")
        assert snapshot.sl_threshold == Decimal("101.0")
        assert snapshot.tp_threshold == Decimal("98.0")

    def test_unrealized_pnl_positive_when_profitable(self):
        """When buyback < entry premium, unrealized P&L should be positive."""
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # Buyback = 97 (less than 100 entry) → profit = 3
        snapshot = self._evaluate(state, call_ask=48.5, put_ask=48.5)
        assert snapshot.unrealized_pnl > 0

    def test_unrealized_pnl_negative_when_losing(self):
        """When buyback > entry premium, unrealized P&L should be negative."""
        state = make_open_straddle(50.0, 1.0, 50.0, 1.0)
        # Buyback = 102 → loss = -2
        snapshot = self._evaluate(state, call_ask=51.0, put_ask=51.0)
        assert snapshot.unrealized_pnl < 0

