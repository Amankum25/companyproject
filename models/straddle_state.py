"""
models/straddle_state.py — Data classes for tracking the straddle lifecycle.

WHY THIS FILE EXISTS:
  A short straddle has two independent legs (Call and Put), each with its own
  order state, fill state, and position state. We need to track them
  independently because NautilusTrader does NOT guarantee that two orders
  submitted together will fill together.

  The StraddleState is the single source of truth for:
  - Which orders have been submitted
  - Which orders have been filled (and at what price/quantity)
  - What the current position quantities are
  - What phase of the straddle lifecycle we are in

  Using an explicit state machine (StraddlePhase enum) prevents bugs like
  submitting duplicate closing orders or missing a failed leg.

STATE MACHINE DIAGRAM:
  IDLE
    └─► ENTRY_PENDING  (orders submitted)
          ├─► CALL_FILLED  (call filled, put still pending)
          ├─► PUT_FILLED   (put filled, call still pending)
          └─► STRADDLE_OPEN  (both filled)
                ├─► RECOVERY_REQUIRED  (one leg failed mid-straddle)
                └─► EXIT_TRIGGERED
                      └─► EXIT_PENDING
                            ├─► CALL_EXIT_FILLED
                            ├─► PUT_EXIT_FILLED
                            └─► CLOSED
  ERROR  (at any point if something unexpected happens)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Optional


class StraddlePhase(Enum):
    """
    The lifecycle phase of the straddle position.

    Think of this as a traffic light that tells the strategy what it is
    currently doing and what actions are permitted.
    """
    IDLE = auto()              # No position, no pending orders
    ENTRY_PENDING = auto()     # Both SELL orders submitted, waiting for fills
    CALL_FILLED = auto()       # Call filled, Put still pending
    PUT_FILLED = auto()        # Put filled, Call still pending
    STRADDLE_OPEN = auto()     # Both legs filled — straddle is live
    RECOVERY_REQUIRED = auto() # One leg filled, other rejected — need to close the filled leg
    EXIT_TRIGGERED = auto()    # SL or TP hit — exit has been initiated
    EXIT_PENDING = auto()      # Both closing BUY orders submitted
    CALL_EXIT_FILLED = auto()  # Call closed, waiting for Put to close
    PUT_EXIT_FILLED = auto()   # Put closed, waiting for Call to close
    CLOSED = auto()            # Both legs closed — straddle complete
    ERROR = auto()             # Unrecoverable error state


class LegStatus(Enum):
    """Status of a single option leg (Call or Put)."""
    IDLE = auto()
    ORDER_SUBMITTED = auto()
    ORDER_ACCEPTED = auto()
    PARTIALLY_FILLED = auto()
    FILLED = auto()
    REJECTED = auto()
    CANCELED = auto()
    EXIT_SUBMITTED = auto()
    EXIT_FILLED = auto()
    CLOSED = auto()


@dataclass
class LegState:
    """
    State for one leg of the straddle (either the Call or the Put).

    This tracks the complete lifecycle of one option contract from order
    submission through fill through exit.

    Attributes
    ----------
    instrument_id_str : str
        String representation of the instrument ID (e.g. "BTC-27MAR26-70000-C-USDT-OPTION.BYBIT").
    entry_order_id : str | None
        The NautilusTrader client order ID for the entry SELL order.
    exit_order_id : str | None
        The NautilusTrader client order ID for the exit BUY order.
    filled_quantity : Decimal
        How many contracts have actually been filled (confirmed by exchange).
    avg_fill_price : Decimal
        Weighted average price at which the leg was filled (in USDT).
    exit_filled_quantity : Decimal
        How many contracts have been bought back (exit fills).
    status : LegStatus
        Current status of this leg.
    """
    instrument_id_str: str
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    exit_filled_quantity: Decimal = Decimal("0")
    status: LegStatus = LegStatus.IDLE

    @property
    def is_fully_filled(self) -> bool:
        """True if entry fill is complete (not just submitted)."""
        return self.status == LegStatus.FILLED

    @property
    def is_closed(self) -> bool:
        """True if exit is fully filled."""
        return self.status == LegStatus.CLOSED

    @property
    def remaining_open_quantity(self) -> Decimal:
        """How many contracts are still short (not yet bought back)."""
        return max(Decimal("0"), self.filled_quantity - self.exit_filled_quantity)

    def update_entry_fill(self, fill_qty: Decimal, fill_price: Decimal) -> None:
        """
        Update the leg with a new entry fill event.

        NautilusTrader can send multiple fill events for a single order
        (partial fills), so we accumulate the total and compute a weighted
        average price.

        Parameters
        ----------
        fill_qty : Decimal
            Quantity filled in this event.
        fill_price : Decimal
            Price of this fill event.
        """
        # Weighted average: (old_total_value + new_fill_value) / new_total_qty
        old_value = self.avg_fill_price * self.filled_quantity
        new_value = fill_price * fill_qty
        self.filled_quantity += fill_qty
        if self.filled_quantity > 0:
            self.avg_fill_price = (old_value + new_value) / self.filled_quantity

    def update_exit_fill(self, fill_qty: Decimal) -> None:
        """
        Update the leg with a new exit fill event.

        Parameters
        ----------
        fill_qty : Decimal
            Quantity bought back in this exit fill event.
        """
        self.exit_filled_quantity += fill_qty
        if self.exit_filled_quantity >= self.filled_quantity:
            self.status = LegStatus.CLOSED


@dataclass
class StraddleState:
    """
    Complete state of the short straddle position.

    This is the single source of truth that the strategy, risk manager,
    and execution manager all read and update.

    Attributes
    ----------
    phase : StraddlePhase
        Current lifecycle phase.
    call : LegState
        State of the Call option leg.
    put : LegState
        State of the Put option leg.
    expiry_ns : int
        Expiry timestamp in nanoseconds (from instrument metadata).
    strike : Decimal
        Selected ATM strike price.
    entry_premium : Decimal
        Combined premium received at entry (call_fill_value + put_fill_value).
    exit_reason : str
        Human-readable reason for exit ("STOP_LOSS", "TAKE_PROFIT", "MANUAL").
    """
    phase: StraddlePhase = StraddlePhase.IDLE
    call: LegState = field(default_factory=lambda: LegState(instrument_id_str=""))
    put: LegState = field(default_factory=lambda: LegState(instrument_id_str=""))
    expiry_ns: int = 0
    strike: Decimal = Decimal("0")
    entry_premium: Decimal = Decimal("0")
    exit_reason: str = ""

    def set_instruments(self, call_id: str, put_id: str) -> None:
        """Set the instrument IDs for both legs."""
        self.call = LegState(instrument_id_str=call_id)
        self.put = LegState(instrument_id_str=put_id)

    def compute_entry_premium(self) -> Decimal:
        """
        Calculate the total premium received at entry.

        For Bybit linear BTC options, the premium is in USDT.
        No multiplier adjustment needed for Bybit options (qty is in contracts,
        price is in USDT per contract).

        Returns
        -------
        Decimal
            Total USDT premium received.
        """
        call_value = self.call.avg_fill_price * self.call.filled_quantity
        put_value = self.put.avg_fill_price * self.put.filled_quantity
        self.entry_premium = call_value + put_value
        return self.entry_premium

    @property
    def is_exit_in_progress(self) -> bool:
        """
        True if we have already triggered an exit.

        This prevents duplicate exit submissions — once an exit is triggered,
        this flag blocks any further exit attempts.
        """
        return self.phase in (
            StraddlePhase.EXIT_TRIGGERED,
            StraddlePhase.EXIT_PENDING,
            StraddlePhase.CALL_EXIT_FILLED,
            StraddlePhase.PUT_EXIT_FILLED,
            StraddlePhase.CLOSED,
        )

    def summary(self) -> str:
        """Return a human-readable summary for logging."""
        return (
            f"StraddleState | Phase={self.phase.name} | Strike={self.strike} | "
            f"CALL: qty={self.call.filled_quantity} avg={self.call.avg_fill_price} status={self.call.status.name} | "
            f"PUT: qty={self.put.filled_quantity} avg={self.put.avg_fill_price} status={self.put.status.name} | "
            f"EntryPremium={self.entry_premium}"
        )

