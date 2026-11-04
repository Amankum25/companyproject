"""
strategy/risk_manager.py — Stop-Loss and Take-Profit monitoring for the straddle.

WHY THIS FILE EXISTS:
  Risk management is separated from the strategy to keep concerns clean.
  The strategy handles the trading lifecycle (connecting, subscribing, ordering).
  The risk manager handles the financial logic (when to exit based on P&L).

HOW STOP-LOSS WORKS (simple explanation):
  When we sell both options, we receive a combined premium (e.g. 100 USDT).
  We set a Stop-Loss: if buying back both options now would cost more than
  101 USDT (1% above entry), we exit — we're losing too much.

  Stop-Loss threshold = entry_premium × 1.01

HOW TAKE-PROFIT WORKS:
  If buying back both options now would cost less than 98 USDT (2% below entry),
  we exit — we've made our profit.

  Take-Profit threshold = entry_premium × 0.98

WHY WE USE ASK PRICES FOR BUYBACK:
  To close a short option position, we must BUY the option back.
  When you buy something, you pay the ASK price (the seller's price).
  Using BID prices would underestimate the actual cost of closing.

STALE QUOTE PROTECTION:
  We refuse to trigger an exit based on stale quotes.
  If a quote is older than quote_staleness_secs, we skip that evaluation.
  A stale quote might show a dangerously low or high price that isn't real.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto
from typing import Optional

from models.straddle_state import StraddleState, StraddlePhase

logger = logging.getLogger(__name__)


class RiskSignal(Enum):
    """What the risk manager recommends doing right now."""
    HOLD = auto()          # No action needed
    STOP_LOSS = auto()     # Exit: cost of buyback exceeded SL threshold
    TAKE_PROFIT = auto()   # Exit: cost of buyback fell below TP threshold
    DATA_MISSING = auto()  # Cannot evaluate: quotes are stale or absent


@dataclass
class RiskSnapshot:
    """
    A snapshot of the current risk metrics.

    This is logged every evaluation cycle so you can see the numbers moving.
    """
    entry_premium: Decimal
    current_buyback: Decimal
    sl_threshold: Decimal
    tp_threshold: Decimal
    signal: RiskSignal
    call_ask: Optional[Decimal]
    put_ask: Optional[Decimal]
    call_qty: Decimal
    put_qty: Decimal
    unrealized_pnl: Decimal  # Positive means profit (buyback < entry)

    def log_summary(self) -> None:
        """Print a formatted risk summary to the logger."""
        logger.info("=" * 60)
        logger.info("  RISK MONITORING SNAPSHOT")
        logger.info("=" * 60)
        logger.info(f"  Entry Premium         : {self.entry_premium:.4f} USDT")
        logger.info(f"  Current Buyback Cost  : {self.current_buyback:.4f} USDT")
        logger.info(f"  Stop-Loss Threshold   : {self.sl_threshold:.4f} USDT (at +1%)")
        logger.info(f"  Take-Profit Threshold : {self.tp_threshold:.4f} USDT (at -2%)")
        logger.info(f"  Unrealized P&L Est.   : {self.unrealized_pnl:.4f} USDT")
        logger.info(f"  CALL Ask              : {self.call_ask}")
        logger.info(f"  PUT Ask               : {self.put_ask}")
        logger.info(f"  Risk Signal           : {self.signal.name}")
        logger.info("=" * 60)


class RiskManager:
    """
    Monitors the straddle position and signals Stop-Loss or Take-Profit exits.

    This class is stateless with respect to the trading system — it reads from
    StraddleState but does not modify it. Only the strategy should modify state.

    Parameters
    ----------
    stop_loss_pct : float
        Exit if buyback_cost >= entry_premium * (1 + stop_loss_pct).
    take_profit_pct : float
        Exit if buyback_cost <= entry_premium * (1 - take_profit_pct).
    quote_staleness_secs : float
        Reject quotes older than this many seconds.
    """

    def __init__(
        self,
        stop_loss_pct: float = 0.01,
        take_profit_pct: float = 0.02,
        quote_staleness_secs: float = 30.0,
    ) -> None:
        self._sl_multiplier = Decimal(str(1.0 + stop_loss_pct))
        self._tp_multiplier = Decimal(str(1.0 - take_profit_pct))
        self._staleness_ns = int(quote_staleness_secs * 1e9)

        logger.info(
            f"RiskManager initialized: SL={stop_loss_pct*100:.1f}% "
            f"(multiplier={self._sl_multiplier}), "
            f"TP={take_profit_pct*100:.1f}% "
            f"(multiplier={self._tp_multiplier})"
        )

    def evaluate(
        self,
        state: StraddleState,
        call_ask_price: Optional[Decimal],
        put_ask_price: Optional[Decimal],
        call_ask_ts_ns: Optional[int],
        put_ask_ts_ns: Optional[int],
        current_time_ns: int,
    ) -> RiskSnapshot:
        """
        Evaluate the current risk and return a RiskSnapshot with a signal.

        Parameters
        ----------
        state : StraddleState
            Current straddle state (must be STRADDLE_OPEN to evaluate).
        call_ask_price : Decimal | None
            Best ASK price for the Call option.
        put_ask_price : Decimal | None
            Best ASK price for the Put option.
        call_ask_ts_ns : int | None
            Timestamp of the Call ASK quote in nanoseconds.
        put_ask_ts_ns : int | None
            Timestamp of the Put ASK quote in nanoseconds.
        current_time_ns : int
            Current time in nanoseconds.

        Returns
        -------
        RiskSnapshot
            Contains the signal and all metrics for logging.
        """
        # Can only evaluate when the straddle is fully open
        if state.phase != StraddlePhase.STRADDLE_OPEN:
            return RiskSnapshot(
                entry_premium=state.entry_premium,
                current_buyback=Decimal("0"),
                sl_threshold=Decimal("0"),
                tp_threshold=Decimal("0"),
                signal=RiskSignal.DATA_MISSING,
                call_ask=None,
                put_ask=None,
                call_qty=Decimal("0"),
                put_qty=Decimal("0"),
                unrealized_pnl=Decimal("0"),
            )

        # Check for missing quotes
        if call_ask_price is None or put_ask_price is None:
            logger.warning("Risk evaluation skipped: ASK price missing for one or both legs.")
            return self._missing_data_snapshot(state)

        # Check for stale quotes
        if not self._are_quotes_fresh(call_ask_ts_ns, put_ask_ts_ns, current_time_ns):
            logger.warning("Risk evaluation skipped: ASK quotes are stale.")
            return self._missing_data_snapshot(state)

        # Compute buyback cost using remaining open quantities
        call_qty = state.call.remaining_open_quantity
        put_qty = state.put.remaining_open_quantity

        if call_qty <= 0 and put_qty <= 0:
            logger.warning("Risk evaluation: both legs have zero remaining quantity.")
            return self._missing_data_snapshot(state)

        # Combined buyback cost (what it costs to close right now)
        # Bybit BTC options are USDT-settled linear contracts.
        # price is in USDT per contract, qty is in contracts.
        # No additional multiplier needed for Bybit linear options.
        current_buyback = (call_ask_price * call_qty) + (put_ask_price * put_qty)

        # Thresholds
        entry_premium = state.entry_premium
        sl_threshold = entry_premium * self._sl_multiplier
        tp_threshold = entry_premium * self._tp_multiplier

        # P&L estimate (positive = we're profitable, buyback is cheaper than entry)
        unrealized_pnl = entry_premium - current_buyback

        # Determine signal
        if current_buyback >= sl_threshold:
            signal = RiskSignal.STOP_LOSS
        elif current_buyback <= tp_threshold:
            signal = RiskSignal.TAKE_PROFIT
        else:
            signal = RiskSignal.HOLD

        snapshot = RiskSnapshot(
            entry_premium=entry_premium,
            current_buyback=current_buyback,
            sl_threshold=sl_threshold,
            tp_threshold=tp_threshold,
            signal=signal,
            call_ask=call_ask_price,
            put_ask=put_ask_price,
            call_qty=call_qty,
            put_qty=put_qty,
            unrealized_pnl=unrealized_pnl,
        )

        return snapshot

    def _are_quotes_fresh(
        self,
        call_ts_ns: Optional[int],
        put_ts_ns: Optional[int],
        current_time_ns: int,
    ) -> bool:
        """
        Check that both quotes are within the staleness window.

        Parameters
        ----------
        call_ts_ns : int | None
            Timestamp of the Call quote in nanoseconds.
        put_ts_ns : int | None
            Timestamp of the Put quote in nanoseconds.
        current_time_ns : int
            Current time in nanoseconds.

        Returns
        -------
        bool
            True if both quotes are fresh.
        """
        if call_ts_ns is None or put_ts_ns is None:
            return False

        call_age = current_time_ns - call_ts_ns
        put_age = current_time_ns - put_ts_ns

        if call_age > self._staleness_ns:
            logger.debug(
                f"Call ASK quote is stale: age={call_age/1e9:.1f}s > "
                f"threshold={self._staleness_ns/1e9:.1f}s"
            )
            return False

        if put_age > self._staleness_ns:
            logger.debug(
                f"Put ASK quote is stale: age={put_age/1e9:.1f}s > "
                f"threshold={self._staleness_ns/1e9:.1f}s"
            )
            return False

        return True

    def _missing_data_snapshot(self, state: StraddleState) -> RiskSnapshot:
        """Return a DATA_MISSING snapshot for when we can't evaluate."""
        return RiskSnapshot(
            entry_premium=state.entry_premium,
            current_buyback=Decimal("0"),
            sl_threshold=state.entry_premium * self._sl_multiplier,
            tp_threshold=state.entry_premium * self._tp_multiplier,
            signal=RiskSignal.DATA_MISSING,
            call_ask=None,
            put_ask=None,
            call_qty=state.call.remaining_open_quantity,
            put_qty=state.put.remaining_open_quantity,
            unrealized_pnl=Decimal("0"),
        )

