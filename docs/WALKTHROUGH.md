# Technical Walkthrough — BTC Short Straddle

> This document prepares you to explain the project to an engineering team in 5–10 minutes. It covers what to say, the reasoning behind each decision, and how to answer follow-up questions.

---

## 5-Minute Demo Script

**Opening sentence:**
> "I built a BTC ATM Short Straddle strategy using NautilusTrader 2.0 and Bybit Testnet. It's an event-driven trading system that automatically selects the nearest ATM options, sells both a Call and a Put, and manages the position with Stop-Loss and Take-Profit thresholds."

**Show the project structure:**
```
bitbybit/
├── main.py           ← Entry point, two modes: data and execute
├── config.py         ← Credentials from env vars, never hardcoded
├── strategy/
│   ├── straddle_strategy.py   ← NautilusTrader Strategy class
│   ├── instrument_selector.py ← ATM selection logic (testable)
│   └── risk_manager.py        ← SL/TP evaluation (testable)
├── models/
│   └── straddle_state.py      ← Explicit state machine
└── tests/            ← 30+ unit tests, no live connection needed
```

**Run the tests:**
```bash
source ~/.venvs/btc-straddle/bin/activate
cd /mnt/c/Users/amank/Downloads/bitbybit
pytest tests/ -v
```

**Run data mode:**
```bash
python main.py --mode data
```

**Explain what you see in the logs.**

---

## Interview Q&A

### Q1: How does NautilusTrader start and initialize?

**Answer:**
NautilusTrader uses a `LiveNode` as the central engine. You configure it with:
- A `BybitDataClientConfig` (loads instruments, subscribes to market data)
- A `BybitExecutionClientConfig` (submits orders, tracks fills)
- A `Strategy` (your trading logic)

When `node.run()` is called, it starts an asyncio event loop. The data client connects to Bybit's WebSocket and REST API. It loads all BTC OPTION instruments into a `Cache`. Then it calls `strategy.on_start()`.

The key insight: **NautilusTrader is fully async and event-driven. You never poll. You implement handler methods, and the engine calls them when events arrive.**

---

### Q2: How did you configure Bybit?

**Answer:**
Using NautilusTrader's official Bybit adapter in `exchange/bybit_client.py`:

```python
BybitDataClientConfig(
    api_key=config.api_key,
    api_secret=config.api_secret,
    product_types=[BybitProductType.OPTION],
    environment=BybitEnvironment.TESTNET,  # Hardcoded — no mainnet path
)
```

Credentials come from environment variables only, loaded via `python-dotenv` from a `.env` file that is never committed to git. The `.env.example` file shows the format without real values.

**Testnet safety**: The environment is hardcoded as `BybitEnvironment.TESTNET`. There is no Mainnet fallback. If someone accidentally modifies this, the connection goes to the wrong environment and fails credentials validation — it's fail-safe.

---

### Q3: How does market data reach the strategy?

**Answer:**
When we subscribe to the option chain:
```python
self.subscribe_option_chain(series_id, strike_range, snapshot_interval_ms=5000)
```

NautilusTrader's DataEngine creates a standing WebSocket subscription with Bybit. Every 5 seconds, it delivers an `OptionChainSlice` to `strategy.on_option_chain()`.

The slice contains:
- The ATM strike (from the exchange's reference price)
- Bid/Ask quotes for each strike
- Greeks (IV, delta, gamma...)

**The pattern**: subscribe once, receive callbacks repeatedly. Never poll.

---

### Q4: How are BTC options instruments identified?

**Answer:**
Bybit option instruments have IDs like: `BTC-27MAR26-70000-C-USDT-OPTION.BYBIT`

In NautilusTrader, each instrument has:
- `inst.option_kind` → `OptionKind.CALL` or `OptionKind.PUT`
- `inst.strike_price` → the strike as a Price object
- `inst.expiration_ns` → expiry as nanoseconds since epoch
- `inst.settlement_currency` → `USDT` for linear contracts

My `InstrumentSelector` queries the Cache for all instruments, filters to BTC options on BYBIT that are active and not expired, then groups them by expiry and strike.

---

### Q5: How does the bot select the ATM strike?

**Answer:**
```python
distance = abs(strike - reference_price)
atm = min(strikes, key=lambda s: (distance, s))  # tie-break: lower strike
```

The reference price comes from `slice.atm_strike` — the exchange's own ATM marker from the first option chain snapshot.

The selector iterates expiries from nearest to farthest. For each expiry, it finds all strikes where BOTH a Call AND a Put exist. Among those, it picks the one minimizing `abs(strike - reference_price)`.

**Tie-breaking rule**: If two strikes are equidistant (e.g. ref=50500, strikes at 50000 and 51000), I always pick the **lower strike**. This is arbitrary but documented and deterministic.

---

### Q6: How are Sell orders submitted?

**Answer:**
```python
order = self.order_factory.limit(
    instrument_id=call_instrument_id,
    order_side=OrderSide.SELL,
    quantity=Quantity.from_str("0.01"),
    price=call_limit_price_at_ask,
    time_in_force=TimeInForce.GTC,
)
self.submit_order(order)
```

I use **LIMIT orders** (not market orders) for three reasons:
1. Options can have wide spreads — market orders get poor execution
2. LIMIT orders let us set a price floor for the premium we receive
3. The exchange may not support market orders for options

The price is set at the current ASK (we're selling at the best ask = visible market price).

**Before any order is submitted in EXECUTE mode**, the user must type `SUBMIT TESTNET STRADDLE` as an explicit confirmation. The strategy checks `self._dry_run` and `self._user_confirmed` flags.

---

### Q7: How do we know whether an order was accepted or filled?

**Answer:**
NautilusTrader delivers these as separate events with separate handler methods:

| Event | Meaning |
|-------|---------|
| `on_order_submitted` | NautilusTrader's internal engine accepted the order |
| `on_order_accepted` | Bybit exchange acknowledged the order |
| `on_order_rejected` | Bybit rejected the order (margin, permission, invalid params) |
| `on_order_filled` | The order (or part of it) was matched with a buyer |

**A critical distinction**: `accepted ≠ filled`. An accepted order is resting on the order book. A fill event means actual execution happened.

My strategy only records a position after `on_order_filled`. `on_order_accepted` just logs.

---

### Q8: How are positions tracked?

**Answer:**
I use my own `StraddleState` data class (not NautilusTrader's Portfolio) because:
1. I need to track two independent legs with weighted average fill prices
2. I need to track exit fills separately from entry fills
3. The state machine gives me explicit control over what actions are permitted

The `LegState.update_entry_fill()` method computes a **weighted average price** as fills come in:
```
new_avg = (old_avg × old_qty + fill_price × fill_qty) / new_total_qty
```

The `remaining_open_quantity` property computes: `filled_qty - exit_filled_qty`.

---

### Q9: How does Stop-Loss and Take-Profit work?

**Answer:**
The `RiskManager.evaluate()` method is called every time a new option chain snapshot arrives (every 5 seconds).

```
entry_premium = call_fill × call_qty + put_fill × put_qty

sl_threshold = entry_premium × 1.01   (1% above entry)
tp_threshold = entry_premium × 0.98   (2% below entry)

buyback_cost = call_ask × call_remaining + put_ask × put_remaining

if buyback_cost >= sl_threshold: → STOP_LOSS signal
if buyback_cost <= tp_threshold: → TAKE_PROFIT signal
```

**Why ASK prices for buyback?**
To close a short option, we BUY it back. When you buy, you pay the ASK price. Using BID would underestimate the actual cost.

**Stale quote protection**: If a quote is older than 30 seconds, the risk manager returns `DATA_MISSING` instead of a signal. This prevents triggering a premature exit based on stale data.

**Duplicate prevention**: Once `EXIT_TRIGGERED` phase is set, `state.is_exit_in_progress` returns True, blocking any further exit submissions.

---

### Q10: What happens if one leg executes but the other does not?

**Answer:**
This is handled in `_handle_entry_leg_failed()`:

1. `on_order_rejected(event)` or `on_order_canceled(event)` is called for the failed leg.
2. The strategy checks: "Does the OTHER leg have a filled quantity > 0?"
3. If yes: sets `state.phase = RECOVERY_REQUIRED`.
4. Calls `_submit_recovery_buy(leg, quantity=state.other_leg.filled_quantity)`.
5. Submits a BUY IOC LIMIT order at ASK + 10% to ensure it fills.
6. When the recovery BUY fills: the accidental position is closed.
7. The strategy logs the recovery result and can be restarted.

**Why IOC (Immediate or Cancel) for recovery?**
We need to close the position urgently. IOC means: fill as much as possible immediately, cancel the rest. For the minimum quantity (0.01 BTC), this should fill completely or fail immediately — no lingering open order.

---

## Design Decision Q&A

### "Why NautilusTrader and not a simple REST polling loop?"

Polling is fragile: if you poll every second, you miss fills that happen between polls. NautilusTrader uses WebSocket streams for all events — fills arrive in milliseconds, not seconds. The event-driven architecture also separates concerns cleanly: data layer, execution layer, and strategy layer are independent.

### "Why LIMIT orders instead of MARKET orders for options?"

Options often have very wide bid-ask spreads. A market sell on a wide-spread option could execute at a price far below the fair value. Using a limit at the current ASK gives us price control while still being immediately fillable in a liquid market.

### "Why separate the InstrumentSelector and RiskManager from the Strategy?"

Separation of concerns + testability. The strategy requires a live NautilusTrader node to test. The InstrumentSelector and RiskManager are pure functions — they take inputs and return outputs. We can test them with 30+ unit tests without any network connection.

### "What happens if Bybit Testnet goes down during an open position?"

NautilusTrader will reconnect and reconcile. The `with_reconciliation(reconciliation=True)` setting tells NautilusTrader to reconcile its internal state with the exchange state on reconnect. The strategy's `on_start()` safety check will detect any existing positions from before the disconnect and report them.

### "Why not use Bybit's native TP/SL for options?"

The assignment requires a combined two-leg exit threshold: we monitor `call_buyback + put_buyback` against the total entry premium. Bybit's native TP/SL is per-order, not per-straddle. We need a custom combined threshold that the strategy monitors internally.

