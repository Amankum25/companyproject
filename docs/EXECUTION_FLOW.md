# Execution Flow — Step-by-Step Walkthrough

This document traces the exact execution flow for a complete straddle lifecycle:
startup → instrument discovery → entry → risk monitoring → exit.

---

## Phase 1: Application Startup

```
python main.py --mode data
```

1. `main.py` → `print_banner()` — displays the welcome header.
2. `parse_args()` — reads `--mode data` from command line.
3. `load_config()` — reads `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET` from `.env`.
   - If missing or placeholder values → prints clear error and exits.
4. `run_data_mode(config)` → calls `build_trading_node(config, dry_run=True)`.

---

## Phase 2: TradingNode Initialization

Inside `exchange/bybit_client.py → build_trading_node()`:

1. Creates a `BybitDataClientConfig` with:
   - `product_types=[BybitProductType.OPTION]` — loads BTC options instruments.
   - `environment=BybitEnvironment.TESTNET` — hardcoded, no mainnet path.

2. Calls `LiveNode.builder(...).add_data_client(...).build()`.

3. Creates a `StraddleStrategy` with its config and adds it to the node.

4. Returns the node and strategy objects.

---

## Phase 3: Node Startup and Instrument Loading

When `node.run()` is called:

1. NautilusTrader starts its asyncio event loop.
2. `BybitDataClient.connect()` is called.
3. The client calls Bybit's REST API to load BTC OPTION instruments.
4. Each instrument definition is stored in the **Cache**.
5. For Bybit, this loads every BTC option contract available on Testnet:
   - Multiple expiries (weekly, monthly)
   - Many strikes per expiry
   - Both CALL and PUT for each strike
6. After loading, NautilusTrader calls `strategy.on_start()`.

---

## Phase 4: Strategy On-Start

Inside `StraddleStrategy.on_start()`:

1. `_safety_check()` — queries `self.cache.orders_open()` and `self.cache.positions_open()`.
   - If unexpected BTC option positions exist → logs error, sets ERROR state, returns.
   - If clean → proceeds.

2. Queries `self.cache.instruments()` → counts BTC options.

3. `_subscribe_option_chain()`:
   - Finds the nearest future expiry from the instrument cache.
   - Creates an `OptionSeriesId(venue, underlying, settlement, expiry_ns)`.
   - Creates a `StrikeRange.atm_relative(above=5, below=5)`.
   - Calls `self.subscribe_option_chain(series_id, strike_range, snapshot_interval_ms=5000)`.
   - NautilusTrader now has a standing subscription for BTC option chain data.

---

## Phase 5: First Option Chain Snapshot

5 seconds later (or sooner if data arrives), NautilusTrader delivers an `OptionChainSlice`:

```
StraddleStrategy.on_option_chain(slice) is called
```

1. Extracts `slice.atm_strike` → stores as `self._btc_reference_price`.

2. `_select_pair_from_chain(slice)`:
   - Calls `InstrumentSelector.select_atm_pair(instruments, reference_price, now_ns)`.
   - Selector filters to active, non-expired BTC options on BYBIT.
   - Groups by expiry → by strike.
   - Finds nearest expiry with both CALL and PUT.
   - Finds strike closest to reference price.
   - Returns `OptionPair(call_instrument, put_instrument, strike, expiry_ns)`.
   - Strategy stores the pair in `self._selected_pair`.
   - Updates `StraddleState` with instrument IDs.

3. `_update_ask_prices_from_chain(slice)`:
   - Looks up the Call at the selected strike: `slice.get_call(strike)`.
   - Extracts `call_data.quote.ask_price` → stores in `self._call_ask_price`.
   - Same for Put.

4. `_log_market_data()` → prints BTC price, Call ASK, Put ASK to log.

5. In DATA mode: phase=IDLE, dry_run=True → no entry, just display.

6. In EXECUTE mode (after user confirmation):
   - `_attempt_entry()` is called.
   - Validates prices are available.
   - Calls `_print_entry_confirmation_prompt()`.
   - (User confirmed in main.py before node started)
   - `submit_entry()` is called.

---

## Phase 6: Entry Order Submission

Inside `StraddleStrategy.submit_entry()`:

1. Checks `_entry_submitted` flag → if True, return immediately (duplicate prevention).

2. Gets pair from `self._selected_pair`.

3. Gets `qty = Quantity.from_str("0.01")`.

4. Rounds limit price to instrument's tick size using `_round_price_to_tick()`.

5. Creates CALL SELL LIMIT order:
   ```python
   call_order = self.order_factory.limit(
       instrument_id=call_instrument_id,
       order_side=OrderSide.SELL,
       quantity=Quantity.from_str("0.01"),
       price=call_limit_price,
       time_in_force=TimeInForce.GTC,
   )
   ```

6. Stores `call_order.client_order_id.value` in `state.call.entry_order_id`.

7. Same for PUT SELL LIMIT order.

8. Calls `self.submit_order(call_order)` → routes to ExecutionEngine → BybitExecClient → REST API.

9. Calls `self.submit_order(put_order)`.

10. Sets `_entry_submitted = True` and `state.phase = ENTRY_PENDING`.

---

## Phase 7: Order Events

**OrderSubmitted event** (NautilusTrader internal):
```
on_order_submitted(event) → logs "Order submitted to engine"
```
*Note: This means NautilusTrader accepted it internally, NOT that Bybit received it.*

**OrderAccepted event** (from Bybit):
```
on_order_accepted(event) → logs "Order accepted by exchange"
state.call.status = ORDER_ACCEPTED
```
*This means Bybit received and accepted the order.*

**OrderFilled event** (from Bybit WebSocket fill stream):
```
on_order_filled(event) → 
  fill_qty = event.last_qty
  fill_price = event.last_px
  state.call.update_entry_fill(fill_qty, fill_price)
  state.call.status = FILLED
  _update_phase_after_entry_fill()
```
*This triggers when both legs fill:*
```
  state.phase = STRADDLE_OPEN
  state.compute_entry_premium()
  → logs "STRADDLE OPEN — BOTH LEGS FILLED"
  → logs "Entry Premium: X USDT"
```

---

## Phase 8: Risk Monitoring

On every subsequent `on_option_chain()` call (every 5 seconds):

```
state.phase == STRADDLE_OPEN → _evaluate_risk() is called
```

Inside `RiskManager.evaluate()`:

1. Checks stale quotes: `current_time_ns - ask_ts_ns > staleness_ns` → DATA_MISSING.

2. Gets remaining quantities:
   ```
   call_qty = state.call.remaining_open_quantity  # = filled - exit_filled
   put_qty = state.put.remaining_open_quantity
   ```

3. Computes buyback cost (using ASK prices):
   ```
   current_buyback = (call_ask × call_qty) + (put_ask × put_qty)
   ```

4. Computes thresholds:
   ```
   sl_threshold = entry_premium × 1.01
   tp_threshold = entry_premium × 0.98
   ```

5. Returns signal:
   - `current_buyback >= sl_threshold` → STOP_LOSS
   - `current_buyback <= tp_threshold` → TAKE_PROFIT
   - Otherwise → HOLD

---

## Phase 9: Exit Trigger

When STOP_LOSS or TAKE_PROFIT signal is returned:

1. `state.is_exit_in_progress` checked → if already exiting, return (duplicate prevention).

2. `state.phase = EXIT_TRIGGERED` — set BEFORE submitting orders.

3. `_submit_exit_orders()`:
   - Gets `call_qty = state.call.remaining_open_quantity`.
   - Gets `put_qty = state.put.remaining_open_quantity`.
   - For each leg: submits a BUY LIMIT order at current ASK price.
   - Stores exit order IDs.
   - Sets `state.phase = EXIT_PENDING`.

---

## Phase 10: Exit Fills

When exit BUY orders fill:

```
on_order_filled(event):
  if order_id == state.call.exit_order_id:
    state.call.update_exit_fill(fill_qty)
    if state.call.remaining_open_quantity == 0:
      state.call.status = CLOSED
      _update_phase_after_exit_fill()
```

When both legs are CLOSED:
```
state.phase = CLOSED
→ logs "STRADDLE CLOSED — BOTH LEGS EXITED"
→ logs "Exit reason: STOP_LOSS / TAKE_PROFIT"
```

---

## Recovery: One-Leg Failure

If CALL fills but PUT is REJECTED:

1. `on_order_rejected()` is called for the PUT order.
2. `state.put.status = REJECTED`.
3. `_handle_entry_leg_failed("PUT")` is called.
4. Checks `state.call.filled_quantity > 0` → yes.
5. `_submit_recovery_buy("CALL", quantity=state.call.filled_quantity)`.
6. Submits a BUY IOC LIMIT order for the CALL at ASK + 10% (to ensure fill).
7. `state.phase = RECOVERY_REQUIRED`.
8. When recovery BUY fills: `state.call.status = CLOSED`.
9. Strategy is flat (no position remains).

