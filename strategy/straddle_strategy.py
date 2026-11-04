"""
strategy/straddle_strategy.py — The main NautilusTrader Strategy for the BTC Short Straddle.

WHY THIS IS A STRATEGY (not an Actor):
  NautilusTrader has two types of event-driven components:
  - Actor: Can subscribe to data, but cannot submit orders.
  - Strategy: Can subscribe to data AND submit orders.

  Since we need to submit SELL orders for the Call and PUT, we use Strategy.

HOW NAUTILUSTRADER DELIVERS EVENTS TO THE STRATEGY:
  The TradingNode runs an event loop. When market data arrives (quotes, trades),
  NautilusTrader's DataEngine routes it to subscribed strategies by calling
  their on_quote_tick(), on_trade_tick(), etc. methods.

  When an order is acknowledged, filled, or rejected, the ExecutionEngine
  routes those events by calling on_order_accepted(), on_order_filled(), etc.

  You NEVER poll — you just implement these handler methods and trust the engine.

  Important: These handlers are called synchronously from the event loop.
  Do NOT perform blocking I/O (network calls, file I/O) inside handlers.
  Do NOT sleep inside handlers.
  Do NOT do expensive computations that take seconds inside handlers.

EXECUTION FLOW (simplified):
  1. on_start() → query cache → find ATM pair → subscribe to option chain
  2. on_option_chain() → check if data valid → (DATA mode: display, EXECUTE mode: prompt+submit)
  3. on_order_filled() → update LegState → if both filled: compute entry premium
  4. on_option_chain() (recurring) → evaluate risk → if SL/TP: submit exit orders
  5. on_order_filled() (exit) → update exit qty → if both legs closed: CLOSED state

STATE MACHINE:
  IDLE → ENTRY_PENDING → CALL_FILLED / PUT_FILLED → STRADDLE_OPEN →
  EXIT_TRIGGERED → EXIT_PENDING → (CALL_EXIT_FILLED / PUT_EXIT_FILLED) → CLOSED

  At any point: → RECOVERY_REQUIRED (if one leg filled, other rejected)
  At any point: → ERROR (if something unexpected happens)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

# NautilusTrader 2.0 imports
# These are based on the actual bybit_option_chain.py and exec_tester.py examples
from nautilus_trader.adapters.bybit import BYBIT
from nautilus_trader.trading import Strategy, StrategyConfig
from nautilus_trader.model import (
    ClientId,
    ClientOrderId,
    InstrumentId,
    OptionChainSlice,
    OptionSeriesId,
    OrderAccepted,
    OrderCanceled,
    OrderFilled,
    OrderRejected,
    OrderSide,
    OrderSubmitted,
    OrderType,
    Price,
    Quantity,
    QuoteTick,
    StrikeRange,
    TimeInForce,
)

from config import StraddleConfig
from models.straddle_state import (
    LegStatus,
    StraddlePhase,
    StraddleState,
)
from strategy.instrument_selector import InstrumentSelector, OptionPair
from strategy.risk_manager import RiskManager, RiskSignal

logger = logging.getLogger(__name__)


class StraddleStrategyConfig(StrategyConfig):
    """
    Configuration for StraddleStrategy.
    """
    venue: str = "DERIBIT"
    underlying: str = "BTC"
    order_quantity: str = "0.1"
    stop_loss_pct: float = 0.01
    take_profit_pct: float = 0.02
    quote_staleness_secs: float = 30.0
    strikes_above_atm: int = 5
    strikes_below_atm: int = 5
    snapshot_interval_ms: int = 2_000
    dry_run: bool = True
    execution_mode: str = "DATA"


class StraddleStrategy(Strategy):
    """
    BTC ATM Short Straddle Strategy.

    This strategy implements both Level 1 (naked straddle execution) and
    Level 2 (risk management with SL/TP).

    Lifecycle:
    1. on_start(): Query instruments, subscribe to option chain.
    2. on_option_chain(): Receive market data snapshots, trigger entry/risk checks.
    3. on_order_submitted/accepted/filled/rejected/canceled(): Handle order events.
    4. on_stop(): Clean up subscriptions.

    Parameters
    ----------
    config : StraddleStrategyConfig
        Strategy configuration.
    """

    def __init__(self, config: StraddleStrategyConfig) -> None:
        super().__init__(config)

        self._cfg = config
        self._venue = config.venue
        self._underlying = config.underlying
        self._dry_run = config.dry_run
        self._execution_mode = config.execution_mode

        # Core components
        self._instrument_selector = InstrumentSelector(
            underlying=config.underlying,
            venue=self._venue,
        )
        self._risk_manager = RiskManager(
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            quote_staleness_secs=config.quote_staleness_secs,
        )

        # Strategy state
        self._state = StraddleState()
        self._selected_pair: Optional[OptionPair] = None
        self._series_id: Optional[OptionSeriesId] = None

        # Market data state: ASK prices for risk monitoring
        self._call_ask_price: Optional[Decimal] = None
        self._put_ask_price: Optional[Decimal] = None
        self._call_ask_ts_ns: Optional[int] = None
        self._put_ask_ts_ns: Optional[int] = None

        # ATM reference price from the option chain ticker
        self._btc_reference_price: Optional[float] = None

        # Flag to prevent submitting orders more than once
        self._entry_submitted = False

    # =========================================================================
    # LIFECYCLE METHODS
    # =========================================================================

    def on_start(self) -> None:
        """
        Called by NautilusTrader when the strategy starts.

        This is where we:
        1. Check for existing orders/positions (safety check)
        2. Query the instrument cache for BTC options
        3. Subscribe to the option chain for ATM +/- N strikes
        """
        self.log.info("=" * 60)
        self.log.info("  BTC STRADDLE STRATEGY STARTING")
        self.log.info(f"  Mode: {self._execution_mode}")
        self.log.info(f"  Dry Run: {self._dry_run}")
        self.log.info(f"  Underlying: {self._underlying}")
        self.log.info("=" * 60)

        # Safety check: abort if there are unexpected existing positions or orders
        if not self._safety_check():
            self.log.error("Safety check failed. Strategy will not proceed.")
            self._state.phase = StraddlePhase.ERROR
            return

        # Query all instruments from the cache
        instruments = self.cache.instruments()
        self.log.info(f"Total instruments in cache: {len(instruments)}")

        # Count BTC options specifically
        btc_opts = [
            i for i in instruments
            if str(i.id.venue) == self._venue and hasattr(i, "option_kind")
        ]
        self.log.info(f"BTC option instruments available on {self._venue}: {len(btc_opts)}")

        if not btc_opts:
            self.log.warning(
                f"No BTC options found in cache for {self._venue}. The instrument provider may not "
                "have loaded them yet, or Testnet has no active options."
            )
            # We'll try again when the option chain subscription delivers data
            # For now, subscribe with a broad range and use the ATM from the chain
            self._subscribe_option_chain(reference_price=None)
            return

        # Find ATM pair (we'll refine it once we have a reference price from the chain)
        # Use None for now — we'll select the pair when the first chain snapshot arrives
        self._subscribe_option_chain(reference_price=None)

        self.log.info("Subscribed to BTC option chain. Waiting for first snapshot...")

    def on_stop(self) -> None:
        """Called when the strategy stops. Unsubscribe from market data."""
        if self._series_id is not None:
            try:
                self.unsubscribe_option_chain(
                    series_id=self._series_id,
                    client_id=ClientId(self._venue),
                )
                self.log.info(f"Unsubscribed from option chain: {self._series_id}")
            except Exception as e:
                self.log.error(f"Error unsubscribing from option chain: {e}")

    def on_reset(self) -> None:
        """Called when the strategy is reset. Clear internal state."""
        self._state = StraddleState()
        self._selected_pair = None
        self._series_id = None
        self._call_ask_price = None
        self._put_ask_price = None
        self._call_ask_ts_ns = None
        self._put_ask_ts_ns = None
        self._btc_reference_price = None
        self._entry_submitted = False
        self.log.info("Strategy state reset.")

    # =========================================================================
    # OPTION CHAIN SUBSCRIPTION AND DATA HANDLING
    # =========================================================================

    def _subscribe_option_chain(self, reference_price: Optional[float]) -> None:
        """
        Subscribe to the BTC option chain.

        NautilusTrader delivers option chain data as OptionChainSlice objects
        via on_option_chain(). We subscribe once here and receive periodic
        snapshots as configured by snapshot_interval_ms.

        The OptionSeriesId identifies WHICH option chain we want:
        venue + underlying + settlement_currency + expiry.

        Since we don't know the exact expiry yet (we'll pick nearest once we have
        data), we subscribe with a broad ATM range and let the first snapshot
        tell us the reference price and available strikes.
        """
        # Find the nearest expiry from the instrument cache
        instruments = self.cache.instruments()
        now_ns = self.clock.timestamp_ns()

        btc_opts = [
            i for i in instruments
            if str(i.id.venue) == self._venue
            and hasattr(i, "option_kind")
            and hasattr(i, "expiration_ns")
            and i.expiration_ns > now_ns
        ]

        if not btc_opts:
            self.log.warning(
                f"Cannot subscribe to option chain: no {self._underlying} options in cache for {self._venue}. "
                "The instrument provider may need more time to load instruments."
            )
            return

        # Find nearest expiry (minimum future expiry timestamp)
        nearest_expiry = min(i.expiration_ns for i in btc_opts)

        # Detect settlement currency from available options for this expiry
        options_at_expiry = [i for i in btc_opts if i.expiration_ns == nearest_expiry]
        settlements = [str(i.settlement_currency) for i in options_at_expiry if hasattr(i, "settlement_currency")]
        
        if self._venue == "DERIBIT":
            settlement = "BTC" if "BTC" in settlements else (settlements[0] if settlements else "BTC")
        else:
            settlement = "USDT" if "USDT" in settlements else (settlements[0] if settlements else "USDT")

        series_id = OptionSeriesId(
            self._venue,
            self._underlying,
            settlement,
            nearest_expiry,
        )
        self._series_id = series_id

        strike_range = StrikeRange.atm_relative(
            strikes_above=self._cfg.strikes_above_atm,
            strikes_below=self._cfg.strikes_below_atm,
        )

        self.log.info(
            f"Subscribing to option chain: {series_id} | "
            f"strikes: ATM-{self._cfg.strikes_below_atm} to ATM+{self._cfg.strikes_above_atm}"
        )

        self.subscribe_option_chain(
            series_id=series_id,
            strike_range=strike_range,
            snapshot_interval_ms=self._cfg.snapshot_interval_ms,
            client_id=ClientId(self._venue),
        )

    def on_option_chain(self, slice: OptionChainSlice) -> None:
        """
        Called by NautilusTrader when a new option chain snapshot arrives.

        This is the main data handler. It:
        1. Extracts the ATM reference price from the chain.
        2. Selects the ATM Call+Put pair if not already selected.
        3. Updates our ASK price state for both legs.
        4. If in EXECUTE mode: triggers entry (once, after confirmation).
        5. If straddle is open: evaluates risk and potentially triggers exit.

        Parameters
        ----------
        slice : OptionChainSlice
            A snapshot of the option chain around ATM.
            Contains quotes for each strike, the ATM strike, and Greeks.
        """
        # Extract the reference price from the chain's ATM marker
        atm_strike = slice.atm_strike
        if atm_strike is not None:
            self._btc_reference_price = float(atm_strike)

        # Log chain summary
        self.log.info(
            f"OPTION_CHAIN | {slice.series_id} | "
            f"atm={atm_strike} | "
            f"calls={slice.call_count()} puts={slice.put_count()} | "
            f"straddle_state={self._state.phase.name}"
        )

        # --- Step 1: Select the ATM pair if we haven't yet ---
        if self._selected_pair is None and self._btc_reference_price is not None:
            self._select_pair_from_chain(slice)

        if self._selected_pair is None:
            # Still no pair — log and wait for next snapshot
            self.log.warning("ATM pair not yet selected. Waiting for next snapshot...")
            return

        # --- Step 2: Update ASK prices from the chain slice ---
        self._update_ask_prices_from_chain(slice)

        # --- Step 3: Display market data ---
        self._log_market_data()

        # --- Step 4: Entry logic (only once, only if EXECUTE mode) ---
        if (
            self._state.phase == StraddlePhase.IDLE
            and not self._entry_submitted
            and self._execution_mode == "EXECUTE"
            and not self._dry_run
        ):
            self._attempt_entry()

        # --- Step 5: Risk monitoring (only when straddle is open) ---
        elif self._state.phase == StraddlePhase.STRADDLE_OPEN:
            self._evaluate_risk()

        # --- Step 6: Show position status ---
        self._log_position_status()

    def _select_pair_from_chain(self, slice: OptionChainSlice) -> None:
        """
        Select the ATM Call+Put pair from the instruments in cache.

        We use the instrument selector with the reference price from the chain.
        """
        instruments = self.cache.instruments()
        now_ns = self.clock.timestamp_ns()

        pair = self._instrument_selector.select_atm_pair(
            instruments=instruments,
            reference_price=self._btc_reference_price,
            current_time_ns=now_ns,
        )

        if pair is None:
            self.log.warning("Could not select ATM pair from cache.")
            return

        self._selected_pair = pair
        self._state.set_instruments(pair.call_id, pair.put_id)
        self._state.strike = pair.strike
        self._state.expiry_ns = pair.expiry_ns

        self.log.info(f"ATM pair selected: CALL={pair.call_id} | PUT={pair.put_id}")

    def _update_ask_prices_from_chain(self, slice: OptionChainSlice) -> None:
        """
        Update our cached ASK prices from the latest chain snapshot.

        We specifically look at the ATM strike's Call and Put ASK prices.
        These are used for:
        1. Display (DATA mode)
        2. Risk monitoring (EXECUTE mode, when straddle is open)
        """
        if self._selected_pair is None:
            return

        strike_val = float(self._selected_pair.strike)
        target_strike = None
        for s in slice.strikes():
            if abs(float(s) - strike_val) < 1e-4:
                target_strike = s
                break

        if target_strike is None:
            try:
                target_strike = Price(strike_val, precision=0)
            except Exception:
                target_strike = Price.from_str(str(int(strike_val)))

        now_ns = self.clock.timestamp_ns()

        # Get the Call at our selected strike
        call_data = slice.get_call(target_strike)
        if call_data is not None and call_data.quote is not None:
            q = call_data.quote
            if q.ask_price is not None:
                self._call_ask_price = Decimal(str(float(q.ask_price)))
                self._call_ask_ts_ns = now_ns

        # Get the Put at our selected strike
        put_data = slice.get_put(target_strike)
        if put_data is not None and put_data.quote is not None:
            q = put_data.quote
            if q.ask_price is not None:
                self._put_ask_price = Decimal(str(float(q.ask_price)))
                self._put_ask_ts_ns = now_ns

    # =========================================================================
    # ENTRY LOGIC
    # =========================================================================

    def _attempt_entry(self) -> None:
        """
        Attempt to submit the entry SELL orders.

        This is called only once (guarded by _entry_submitted flag).
        In EXECUTE mode, it validates data and submits both SELL LIMIT orders.
        """
        if self._entry_submitted:
            return  # Duplicate prevention

        if self._selected_pair is None:
            self.log.warning("Cannot enter: no ATM pair selected.")
            return

        # Validate we have ask prices for both legs
        if self._call_ask_price is None or self._put_ask_price is None:
            self.log.warning(
                "Cannot enter: ASK prices not available for one or both legs. "
                "Waiting for valid market data..."
            )
            return

        # Print the pre-trade summary and obtain confirmation
        self._print_entry_confirmation_prompt()

    def _print_entry_confirmation_prompt(self) -> None:
        """
        Display the trade details and explain that manual confirmation is needed.

        In a real implementation, this would wait for the user to type
        "SUBMIT TESTNET STRADDLE". Since NautilusTrader runs asynchronously,
        we can't block waiting for input here. Instead, in main.py we handle
        this before the node starts.

        For the purpose of the strategy, we set a flag that main.py sets
        after the confirmation is received.
        """
        pair = self._selected_pair
        self.log.info("=" * 60)
        self.log.info("  *** ENTRY CONFIRMATION REQUIRED ***")
        self.log.info("=" * 60)
        self.log.info(f"  Environment      : {self._venue} TESTNET")
        self.log.info(f"  Strategy         : SHORT STRADDLE")
        self.log.info(f"  Underlying Price : {self._btc_reference_price}")
        self.log.info(f"  Selected Strike  : {pair.strike}")
        self.log.info(f"  CALL Instrument  : {pair.call_id}")
        self.log.info(f"  PUT  Instrument  : {pair.put_id}")
        self.log.info(f"  CALL ASK (Bid)   : {self._call_ask_price}")
        self.log.info(f"  PUT  ASK (Bid)   : {self._put_ask_price}")
        self.log.info(f"  Order Quantity   : {self._cfg.order_quantity}")
        self.log.info("=" * 60)
        self.log.info("  To execute: Call strategy.submit_entry() after confirmation.")
        self.log.info("=" * 60)

    def submit_entry(self) -> None:
        """
        Submit the entry SELL orders for both legs.

        This is called by main.py AFTER the user types "SUBMIT TESTNET STRADDLE".
        It is NOT called automatically.

        Order type: LIMIT at the current BID price (conservative — we're selling,
        so we try to get the best fill price, not cross the spread aggressively).

        For option SELL orders on Bybit:
        - We set the price at the current BID (or slightly below if adjusting for fill probability).
        - TimeInForce.GTC — the order stays open until filled or cancelled.
        """
        if self._entry_submitted:
            self.log.warning("Entry already submitted. Ignoring duplicate call.")
            return

        if self._selected_pair is None:
            self.log.error("Cannot submit entry: no pair selected.")
            return

        if self._dry_run:
            self.log.info("[DRY RUN] Would submit SELL orders but dry_run=True. Skipping.")
            self._entry_submitted = True
            return

        pair = self._selected_pair
        qty = Quantity.from_str(self._cfg.order_quantity)

        # For the SELL orders, use the current ASK as a reference for limit price.
        # Using the BID price of the option as the limit for a sell means we're
        # willing to sell at the current market bid — a reasonable aggressive-but-not-
        # market-order approach.
        # In practice for Testnet, we use ASK as the limit to maximize fill probability
        # (we're the seller, so quoting at ASK means we post at the best ask).
        call_limit_price = self._call_ask_price
        put_limit_price = self._put_ask_price

        if call_limit_price is None or put_limit_price is None:
            self.log.error("Cannot submit entry: price data unavailable.")
            return

        # Validate price precision against the instrument's tick size
        call_inst = pair.call_instrument
        put_inst = pair.put_instrument

        try:
            call_price_obj = self._round_price_to_tick(
                call_limit_price, call_inst.price_increment
            )
            put_price_obj = self._round_price_to_tick(
                put_limit_price, put_inst.price_increment
            )
        except Exception as e:
            self.log.error(f"Price rounding error: {e}")
            return

        self.log.info("Submitting CALL SELL LIMIT order...")
        self.log.info(f"  Instrument: {pair.call_id}")
        self.log.info(f"  Quantity:   {qty}")
        self.log.info(f"  Price:      {call_price_obj}")

        # Submit CALL SELL order
        call_order = self.order_factory.limit(
            instrument_id=InstrumentId.from_str(pair.call_id),
            order_side=OrderSide.SELL,
            quantity=qty,
            price=call_price_obj,
            time_in_force=TimeInForce.GTC,
        )
        self._state.call.entry_order_id = call_order.client_order_id.value
        self._state.call.status = LegStatus.ORDER_SUBMITTED

        self.log.info("Submitting PUT SELL LIMIT order...")
        self.log.info(f"  Instrument: {pair.put_id}")
        self.log.info(f"  Quantity:   {qty}")
        self.log.info(f"  Price:      {put_price_obj}")

        # Submit PUT SELL order
        put_order = self.order_factory.limit(
            instrument_id=InstrumentId.from_str(pair.put_id),
            order_side=OrderSide.SELL,
            quantity=qty,
            price=put_price_obj,
            time_in_force=TimeInForce.GTC,
        )
        self._state.put.entry_order_id = put_order.client_order_id.value
        self._state.put.status = LegStatus.ORDER_SUBMITTED

        self.submit_order(call_order)
        self.submit_order(put_order)

        self._state.phase = StraddlePhase.ENTRY_PENDING
        self._entry_submitted = True

        self.log.info("Both SELL LIMIT orders submitted. Awaiting fills...")
        self.log.info(f"  CALL order ID: {self._state.call.entry_order_id}")
        self.log.info(f"  PUT  order ID: {self._state.put.entry_order_id}")

    # =========================================================================
    # ORDER EVENT HANDLERS
    # =========================================================================

    def on_order_submitted(self, event: OrderSubmitted) -> None:
        """
        Called when an order has been submitted to the execution engine.

        Note: This does NOT mean the exchange received the order yet.
        It means NautilusTrader's internal engine has accepted it for routing.
        """
        order_id = event.client_order_id.value
        self.log.info(f"Order submitted to engine: {order_id}")

    def on_order_accepted(self, event: OrderAccepted) -> None:
        """
        Called when the exchange has acknowledged the order.

        This means the exchange received and accepted the order.
        It does NOT mean it was filled — just acknowledged.

        NautilusTrader delivers this event when the exchange responds
        with a confirmation (e.g. Bybit's "retCode: 0").
        """
        order_id = event.client_order_id.value
        self.log.info(f"Order accepted by exchange: {order_id}")

        if order_id == self._state.call.entry_order_id:
            self._state.call.status = LegStatus.ORDER_ACCEPTED
        elif order_id == self._state.put.entry_order_id:
            self._state.put.status = LegStatus.ORDER_ACCEPTED
        else:
            self.log.warning(f"Accepted order {order_id} not recognized as call or put entry.")

    def on_order_rejected(self, event: OrderRejected) -> None:
        """
        Called when the exchange rejected an order.

        Common causes:
        - Insufficient margin
        - Invalid price or quantity
        - Instrument not available for trading
        - Account restrictions

        We must handle the case where one leg was rejected after the
        other leg was already filled (the asymmetric fill problem).
        """
        order_id = event.client_order_id.value
        reason = str(event.reason) if hasattr(event, "reason") else "Unknown"
        self.log.error(f"Order REJECTED: {order_id} | Reason: {reason}")

        if order_id == self._state.call.entry_order_id:
            self._state.call.status = LegStatus.REJECTED
            self._handle_entry_leg_failed("CALL")
        elif order_id == self._state.put.entry_order_id:
            self._state.put.status = LegStatus.REJECTED
            self._handle_entry_leg_failed("PUT")
        elif order_id == self._state.call.exit_order_id:
            self.log.error(
                f"CALL exit order rejected: {order_id}. "
                "Manual intervention may be needed to close the CALL position."
            )
        elif order_id == self._state.put.exit_order_id:
            self.log.error(
                f"PUT exit order rejected: {order_id}. "
                "Manual intervention may be needed to close the PUT position."
            )

    def on_order_filled(self, event: OrderFilled) -> None:
        """
        Called when an order has been fully or partially filled.

        NautilusTrader uses this same event for both full and partial fills.
        The event contains:
        - last_qty: how much was filled in THIS event (not cumulative)
        - last_px: price at which this fill happened
        - cumulative_qty: total filled so far for this order

        We update our LegState's weighted average fill price.
        """
        order_id = event.client_order_id.value
        fill_qty = Decimal(str(float(event.last_qty)))
        fill_price = Decimal(str(float(event.last_px)))

        self.log.info(
            f"Order fill: {order_id} | qty={fill_qty} | price={fill_price}"
        )

        # --- Entry fills ---
        if order_id == self._state.call.entry_order_id:
            self._state.call.update_entry_fill(fill_qty, fill_price)
            # Check if fully filled (compare with intended quantity)
            intended_qty = Decimal(self._cfg.order_quantity)
            if self._state.call.filled_quantity >= intended_qty:
                self._state.call.status = LegStatus.FILLED
                self.log.info(
                    f"CALL entry FULLY FILLED | "
                    f"qty={self._state.call.filled_quantity} | "
                    f"avg_price={self._state.call.avg_fill_price}"
                )
                self._update_phase_after_entry_fill()
            else:
                self._state.call.status = LegStatus.PARTIALLY_FILLED
                self.log.info(
                    f"CALL entry PARTIAL FILL | "
                    f"filled={self._state.call.filled_quantity}/{intended_qty}"
                )

        elif order_id == self._state.put.entry_order_id:
            self._state.put.update_entry_fill(fill_qty, fill_price)
            intended_qty = Decimal(self._cfg.order_quantity)
            if self._state.put.filled_quantity >= intended_qty:
                self._state.put.status = LegStatus.FILLED
                self.log.info(
                    f"PUT entry FULLY FILLED | "
                    f"qty={self._state.put.filled_quantity} | "
                    f"avg_price={self._state.put.avg_fill_price}"
                )
                self._update_phase_after_entry_fill()
            else:
                self._state.put.status = LegStatus.PARTIALLY_FILLED
                self.log.info(
                    f"PUT entry PARTIAL FILL | "
                    f"filled={self._state.put.filled_quantity}/{intended_qty}"
                )

        # --- Exit fills ---
        elif order_id == self._state.call.exit_order_id:
            self._state.call.update_exit_fill(fill_qty)
            self.log.info(
                f"CALL exit fill: qty={fill_qty} | "
                f"remaining={self._state.call.remaining_open_quantity}"
            )
            self._update_phase_after_exit_fill()

        elif order_id == self._state.put.exit_order_id:
            self._state.put.update_exit_fill(fill_qty)
            self.log.info(
                f"PUT exit fill: qty={fill_qty} | "
                f"remaining={self._state.put.remaining_open_quantity}"
            )
            self._update_phase_after_exit_fill()

        else:
            self.log.warning(f"Fill for untracked order ID: {order_id}")

    def on_order_canceled(self, event: OrderCanceled) -> None:
        """
        Called when an order has been cancelled.

        This can happen if:
        - The strategy cancelled the order itself
        - The exchange cancelled the order (e.g. expired GTC at end of session)
        - The order was partially filled, then cancelled for the remaining qty
        """
        order_id = event.client_order_id.value
        self.log.info(f"Order canceled: {order_id}")

        if order_id == self._state.call.entry_order_id:
            self._state.call.status = LegStatus.CANCELED
            # If some quantity was filled before cancellation, we may have a partial position
            if self._state.call.filled_quantity > 0:
                self.log.warning(
                    f"CALL entry partially filled before cancel: "
                    f"qty={self._state.call.filled_quantity}. Recovery needed."
                )
                self._handle_entry_leg_failed("CALL")
        elif order_id == self._state.put.entry_order_id:
            self._state.put.status = LegStatus.CANCELED
            if self._state.put.filled_quantity > 0:
                self.log.warning(
                    f"PUT entry partially filled before cancel: "
                    f"qty={self._state.put.filled_quantity}. Recovery needed."
                )
                self._handle_entry_leg_failed("PUT")

    # =========================================================================
    # STATE TRANSITION HELPERS
    # =========================================================================

    def _update_phase_after_entry_fill(self) -> None:
        """
        Update the straddle phase based on which legs have filled.

        Call this after every entry fill event to keep the state machine current.
        """
        call_filled = self._state.call.status == LegStatus.FILLED
        put_filled = self._state.put.status == LegStatus.FILLED

        if call_filled and put_filled:
            self._state.phase = StraddlePhase.STRADDLE_OPEN
            premium = self._state.compute_entry_premium()
            self.log.info("=" * 60)
            self.log.info("  *** STRADDLE OPEN — BOTH LEGS FILLED ***")
            self.log.info(f"  CALL avg fill: {self._state.call.avg_fill_price}")
            self.log.info(f"  PUT  avg fill: {self._state.put.avg_fill_price}")
            self.log.info(f"  Entry Premium: {premium:.4f} USDT")
            self.log.info("=" * 60)

        elif call_filled and not put_filled:
            if self._state.phase != StraddlePhase.PUT_FILLED:
                self._state.phase = StraddlePhase.CALL_FILLED
                self.log.info("Phase: CALL_FILLED — awaiting PUT fill")

        elif put_filled and not call_filled:
            if self._state.phase != StraddlePhase.CALL_FILLED:
                self._state.phase = StraddlePhase.PUT_FILLED
                self.log.info("Phase: PUT_FILLED — awaiting CALL fill")

    def _update_phase_after_exit_fill(self) -> None:
        """Update phase after an exit fill."""
        call_closed = self._state.call.status == LegStatus.CLOSED
        put_closed = self._state.put.status == LegStatus.CLOSED

        if call_closed and put_closed:
            self._state.phase = StraddlePhase.CLOSED
            self.log.info("=" * 60)
            self.log.info("  *** STRADDLE CLOSED — BOTH LEGS EXITED ***")
            self.log.info(f"  Exit reason: {self._state.exit_reason}")
            self.log.info("=" * 60)
        elif call_closed:
            self._state.phase = StraddlePhase.CALL_EXIT_FILLED
            self.log.info("CALL exit filled. Awaiting PUT exit...")
        elif put_closed:
            self._state.phase = StraddlePhase.PUT_EXIT_FILLED
            self.log.info("PUT exit filled. Awaiting CALL exit...")

    def _handle_entry_leg_failed(self, leg: str) -> None:
        """
        Handle the case where one entry leg failed (rejected or cancelled).

        This is the asymmetric fill recovery scenario described in the assignment:
        - If CALL filled but PUT failed → we have an unwanted CALL short position
        - If PUT filled but CALL failed → we have an unwanted PUT short position

        Recovery: Buy back the filled quantity of the successful leg.
        """
        self.log.error(
            f"{leg} entry leg failed. Checking if other leg needs recovery..."
        )

        # Determine which leg has a position that needs to be closed
        if leg == "CALL":
            # Call failed — did Put fill?
            if self._state.put.filled_quantity > 0:
                self.log.warning(
                    f"PUT is partially/fully filled ({self._state.put.filled_quantity} contracts) "
                    "but CALL failed. Initiating recovery: buying back PUT..."
                )
                self._state.phase = StraddlePhase.RECOVERY_REQUIRED
                self._submit_recovery_buy("PUT", self._state.put.filled_quantity)
        else:
            # Put failed — did Call fill?
            if self._state.call.filled_quantity > 0:
                self.log.warning(
                    f"CALL is partially/fully filled ({self._state.call.filled_quantity} contracts) "
                    "but PUT failed. Initiating recovery: buying back CALL..."
                )
                self._state.phase = StraddlePhase.RECOVERY_REQUIRED
                self._submit_recovery_buy("CALL", self._state.call.filled_quantity)

    def _submit_recovery_buy(self, leg: str, quantity: Decimal) -> None:
        """
        Submit a recovery BUY order to close an accidentally opened single-leg position.

        Parameters
        ----------
        leg : str
            "CALL" or "PUT"
        quantity : Decimal
            The quantity to buy back (equals the filled quantity of that leg).
        """
        if self._dry_run:
            self.log.info(f"[DRY RUN] Would submit recovery BUY for {leg} qty={quantity}")
            return

        pair = self._selected_pair
        if pair is None:
            self.log.error("Cannot submit recovery: no pair selected.")
            return

        if leg == "CALL":
            instrument_id = InstrumentId.from_str(pair.call_id)
            inst = pair.call_instrument
        else:
            instrument_id = InstrumentId.from_str(pair.put_id)
            inst = pair.put_instrument

        qty = Quantity.from_str(str(quantity))

        self.log.info(
            f"Submitting RECOVERY BUY for {leg}: "
            f"instrument={instrument_id} qty={qty}"
        )

        # Use a market order for recovery to ensure we close the position
        # immediately rather than risking a limit order not filling.
        # Note: Some exchanges don't support market orders for options.
        # For Bybit options, we use IOC limit at a wide price to simulate market.
        ask_price = self._call_ask_price if leg == "CALL" else self._put_ask_price
        if ask_price is None:
            self.log.error(
                f"Cannot submit recovery BUY for {leg}: no ASK price available."
            )
            return

        # Add a 10% premium to the ask to ensure the order fills
        recovery_price = ask_price * Decimal("1.10")
        try:
            recovery_price_obj = self._round_price_to_tick(recovery_price, inst.price_increment)
        except Exception as e:
            self.log.error(f"Price rounding error for recovery order: {e}")
            return

        recovery_order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=qty,
            price=recovery_price_obj,
            time_in_force=TimeInForce.IOC,  # Immediate or Cancel
        )

        if leg == "CALL":
            self._state.call.exit_order_id = recovery_order.client_order_id.value
        else:
            self._state.put.exit_order_id = recovery_order.client_order_id.value

        self.submit_order(recovery_order)
        self.log.info(f"Recovery BUY submitted for {leg}: {recovery_order.client_order_id}")

    # =========================================================================
    # RISK MANAGEMENT AND EXIT
    # =========================================================================

    def _evaluate_risk(self) -> None:
        """
        Run the risk manager and trigger an exit if SL or TP is hit.

        Called from on_option_chain() every time new data arrives.
        """
        if self._state.is_exit_in_progress:
            return  # Already exiting — don't trigger again

        snapshot = self._risk_manager.evaluate(
            state=self._state,
            call_ask_price=self._call_ask_price,
            put_ask_price=self._put_ask_price,
            call_ask_ts_ns=self._call_ask_ts_ns,
            put_ask_ts_ns=self._put_ask_ts_ns,
            current_time_ns=self.clock.timestamp_ns(),
        )

        snapshot.log_summary()

        if snapshot.signal == RiskSignal.STOP_LOSS:
            self.log.warning("STOP-LOSS TRIGGERED! Initiating exit...")
            self._state.exit_reason = "STOP_LOSS"
            self._submit_exit_orders()

        elif snapshot.signal == RiskSignal.TAKE_PROFIT:
            self.log.info("TAKE-PROFIT TRIGGERED! Initiating exit...")
            self._state.exit_reason = "TAKE_PROFIT"
            self._submit_exit_orders()

    def _submit_exit_orders(self) -> None:
        """
        Submit BUY orders to close both short positions.

        Called when SL or TP triggers. This sets the exit-in-progress state
        FIRST to prevent duplicate exits, then submits the orders.

        Uses ASK prices (cost to buy back = cost to close short options).
        """
        # Set exit state BEFORE submitting orders to prevent duplicate triggers
        self._state.phase = StraddlePhase.EXIT_TRIGGERED

        if self._dry_run:
            self.log.info("[DRY RUN] Would submit exit BUY orders but dry_run=True. Marking CLOSED.")
            self._state.phase = StraddlePhase.CLOSED
            return

        pair = self._selected_pair
        if pair is None:
            self.log.error("Cannot exit: no pair selected.")
            return

        # Use actual remaining quantities from the position state
        call_qty = self._state.call.remaining_open_quantity
        put_qty = self._state.put.remaining_open_quantity

        if call_qty <= 0 and put_qty <= 0:
            self.log.info("No remaining quantity to close. Marking CLOSED.")
            self._state.phase = StraddlePhase.CLOSED
            return

        self.log.info("=" * 60)
        self.log.info(f"  SUBMITTING EXIT ORDERS ({self._state.exit_reason})")
        self.log.info(f"  CALL qty to close: {call_qty}")
        self.log.info(f"  PUT  qty to close: {put_qty}")
        self.log.info("=" * 60)

        if call_qty > 0:
            self._submit_exit_leg("CALL", call_qty)
        if put_qty > 0:
            self._submit_exit_leg("PUT", put_qty)

        self._state.phase = StraddlePhase.EXIT_PENDING

    def _submit_exit_leg(self, leg: str, quantity: Decimal) -> None:
        """Submit a BUY order to close one leg of the straddle."""
        pair = self._selected_pair
        if leg == "CALL":
            instrument_id = InstrumentId.from_str(pair.call_id)
            inst = pair.call_instrument
            ask_price = self._call_ask_price
        else:
            instrument_id = InstrumentId.from_str(pair.put_id)
            inst = pair.put_instrument
            ask_price = self._put_ask_price

        if ask_price is None:
            self.log.error(f"Cannot submit {leg} exit: no ASK price.")
            return

        qty = Quantity.from_str(str(quantity))

        try:
            exit_price_obj = self._round_price_to_tick(ask_price, inst.price_increment)
        except Exception as e:
            self.log.error(f"Price rounding error for exit {leg}: {e}")
            return

        exit_order = self.order_factory.limit(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=qty,
            price=exit_price_obj,
            time_in_force=TimeInForce.GTC,
        )

        if leg == "CALL":
            self._state.call.exit_order_id = exit_order.client_order_id.value
            self._state.call.status = LegStatus.EXIT_SUBMITTED
        else:
            self._state.put.exit_order_id = exit_order.client_order_id.value
            self._state.put.status = LegStatus.EXIT_SUBMITTED

        self.submit_order(exit_order)
        self.log.info(
            f"Exit BUY submitted for {leg}: "
            f"order_id={exit_order.client_order_id} qty={qty} price={exit_price_obj}"
        )

    # =========================================================================
    # UTILITY METHODS
    # =========================================================================

    def _safety_check(self) -> bool:
        """
        Check for unexpected existing orders or positions before starting.

        If the account already has open BTC option positions or orders,
        we stop — we won't interfere with other strategies or positions.

        Returns
        -------
        bool
            True if it's safe to proceed, False if we should stop.
        """
        # Check for open orders
        open_orders = self.cache.orders_open()
        if open_orders:
            btc_option_orders = [
                o for o in open_orders
                if "OPTION" in str(o.instrument_id)
            ]
            if btc_option_orders:
                self.log.error(
                    f"Found {len(btc_option_orders)} existing open BTC option orders. "
                    "Cannot start straddle on a dirty account. "
                    "Please cancel existing orders before running this strategy."
                )
                for o in btc_option_orders:
                    self.log.error(f"  Existing order: {o.instrument_id} {o.side} {o.quantity}")
                return False

        # Check for open positions
        positions = self.cache.positions_open()
        if positions:
            btc_option_positions = [
                p for p in positions
                if "OPTION" in str(p.instrument_id)
            ]
            if btc_option_positions:
                self.log.error(
                    f"Found {len(btc_option_positions)} existing BTC option positions. "
                    "Cannot start straddle on a dirty account. "
                    "Please close existing positions before running this strategy."
                )
                for p in btc_option_positions:
                    self.log.error(
                        f"  Existing position: {p.instrument_id} "
                        f"qty={p.quantity} side={p.side}"
                    )
                return False

        self.log.info("Safety check passed: no existing BTC option orders or positions.")
        return True

    def _round_price_to_tick(self, price: Decimal, price_increment: object) -> Price:
        """
        Round a price to the instrument's tick size and return a Price object.

        Parameters
        ----------
        price : Decimal
            The raw price to round.
        price_increment : object
            NautilusTrader Price object representing the tick size.

        Returns
        -------
        Price
            A NautilusTrader Price object at the correct precision.
        """
        tick = Decimal(str(float(price_increment)))
        if tick <= 0:
            tick = Decimal("0.01")  # Fallback for safety

        # Round to nearest tick
        rounded = (price / tick).quantize(Decimal("1")) * tick

        # Build the Price with the correct number of decimal places from the increment
        # e.g. if tick=0.01, we need 2 decimal places
        tick_str = str(tick)
        if "." in tick_str:
            precision = len(tick_str.split(".")[1])
        else:
            precision = 0

        return Price(float(rounded), precision=precision)

    def _log_market_data(self) -> None:
        """Log the current market data state."""
        if self._selected_pair is None:
            return
        self.log.info("=" * 60)
        self.log.info("  MARKET DATA")
        self.log.info(f"  BTC Reference Price : {self._btc_reference_price}")
        self.log.info(f"  CALL ASK            : {self._call_ask_price}")
        self.log.info(f"  PUT  ASK            : {self._put_ask_price}")
        self.log.info("=" * 60)

    def _log_position_status(self) -> None:
        """Log the current position and straddle status."""
        self.log.info("=" * 60)
        self.log.info("  POSITION STATUS")
        self.log.info(f"  Straddle Phase : {self._state.phase.name}")
        self.log.info(f"  CALL Status    : {self._state.call.status.name}")
        self.log.info(f"  PUT  Status    : {self._state.put.status.name}")
        if self._state.call.filled_quantity > 0:
            self.log.info(f"  CALL Position  : SHORT {self._state.call.remaining_open_quantity}")
        if self._state.put.filled_quantity > 0:
            self.log.info(f"  PUT  Position  : SHORT {self._state.put.remaining_open_quantity}")
        self.log.info("=" * 60)

