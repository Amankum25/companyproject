# Architecture — BTC Short Straddle System

## Overview

This system is built on top of **NautilusTrader 2.0**, an open-source algorithmic trading framework that uses an **event-driven architecture**. Understanding this architecture is key to understanding the codebase.

---

## Event-Driven Architecture

Traditional synchronous programs ask: "What is happening right now?"

Event-driven programs say: "Tell me when something happens."

NautilusTrader uses the event-driven model. Your strategy does **not** poll the exchange. Instead:

1. The **DataEngine** receives market data from the exchange.
2. It routes events to **subscribers** (your strategy).
3. Your strategy's handlers are called: `on_quote_tick()`, `on_option_chain()`, etc.
4. When you submit an order, the **ExecutionEngine** routes it to the exchange.
5. Fill events come back and call `on_order_filled()`.

You never wait. You just react.

---

## Component Diagram

```
                    ┌─────────────────────────────────────────────┐
                    │              Bybit WebSocket                 │
                    │   (market data + order status feeds)         │
                    └───────────┬─────────────────┬───────────────┘
                                │                 │
                   Market Data  │                 │  Order Fills/Status
                                ▼                 ▼
┌───────────────────────────────────────────────────────────────────┐
│                        LiveNode (TradingNode)                     │
│                                                                   │
│  ┌─────────────────┐    ┌─────────────────┐  ┌─────────────────┐ │
│  │  BybitDataClient│    │ BybitExecClient  │  │  RiskEngine     │ │
│  │  - Load options │    │ - Submit orders  │  │  - Bypass=True  │ │
│  │  - Subscribe    │    │ - Cancel orders  │  │    for options  │ │
│  │    option chain │    │ - Reconcile      │  └─────────────────┘ │
│  └───────┬─────────┘    └───────┬──────────┘                     │
│          │                      │                                 │
│          ▼                      ▼                                 │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                      DataEngine / ExecEngine               │  │
│  │  - Routes events to subscribed strategies                  │  │
│  │  - Routes orders to the correct execution client           │  │
│  └────────────────────────┬───────────────────────────────────┘  │
│                            │                                      │
│                            ▼                                      │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                       MessageBus                            │  │
│  │  - Internal pub/sub for all events                         │  │
│  │  - Strategies subscribe to specific event types            │  │
│  └────────────────────────┬───────────────────────────────────┘  │
│                            │                                      │
│          ┌─────────────────┼─────────────────────┐               │
│          │                 │                     │               │
│          ▼                 ▼                     ▼               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │    Cache     │  │  Portfolio   │  │  StraddleStrategy    │   │
│  │  - Quotes    │  │  - Positions │  │  - on_option_chain() │   │
│  │  - Orders    │  │  - P&L       │  │  - on_order_filled() │   │
│  │  - Positions │  │  - Account   │  │  - submit_entry()    │   │
│  │  - Instruments│ └──────────────┘  │  - _evaluate_risk()  │   │
│  └──────────────┘                    └──────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
```

---

## Module Responsibilities

| File | Responsibility |
|------|----------------|
| `main.py` | Entry point. Parses CLI args, loads config, starts node. |
| `config.py` | Loads and validates env vars. Single source of truth for config. |
| `exchange/bybit_client.py` | Builds the LiveNode with Bybit data/exec clients. |
| `strategy/straddle_strategy.py` | The NautilusTrader Strategy class. All trading logic. |
| `strategy/instrument_selector.py` | Pure-function ATM selection. No live dependencies. |
| `strategy/risk_manager.py` | Pure-function SL/TP evaluation. No live dependencies. |
| `models/straddle_state.py` | Data classes for tracking order and position state. |
| `utils/logger.py` | Python logging setup (console + file). |
| `tests/` | pytest unit tests (no live connection required). |

---

## Key NautilusTrader Concepts

### InstrumentId
Every tradeable instrument has a unique ID: `symbol.venue`
Example: `BTC-27MAR26-70000-C-USDT-OPTION.BYBIT`

### Cache
NautilusTrader maintains an in-memory cache of:
- All loaded instruments
- All active orders
- All open positions
- Recent quotes

The strategy reads from the cache to get instrument specs and check positions.

### MessageBus
NautilusTrader uses an internal event bus. When an order fills:
1. BybitExecClient receives the WebSocket fill event.
2. It publishes `OrderFilled` to the MessageBus.
3. The ExecutionEngine routes it to the strategy's `on_order_filled()` handler.

### OptionChainSlice
A snapshot of option data at a given expiry, centered around ATM.
Contains: bid/ask quotes for each strike, the ATM strike, Greeks (IV, delta, gamma...).
The strategy receives these via `on_option_chain()`.

### OptionSeriesId
Identifies a specific option series: `venue + underlying + settlement + expiry`.
Used to subscribe to an option chain.

### StrikeRange.atm_relative(above=N, below=M)
Tells NautilusTrader to include N strikes above and M strikes below the ATM
in the option chain subscription.

---

## State Machine

The strategy uses an explicit state machine to prevent invalid transitions:

```
IDLE
 │
 ├─ [entry submitted] ──► ENTRY_PENDING
 │                              │
 │            ┌─────────────────┼─────────────────┐
 │            ▼                 ▼                 ▼
 │       CALL_FILLED        PUT_FILLED      (both filled)
 │            │                 │                 │
 │            └─────────────────┴────────────────►│
 │                                                 ▼
 │                                          STRADDLE_OPEN
 │                                                 │
 │                              ┌──────────────────┤
 │                              │                  │
 │                    [SL or TP hit]      [one leg failed]
 │                              │                  │
 │                              ▼                  ▼
 │                       EXIT_TRIGGERED    RECOVERY_REQUIRED
 │                              │
 │                              ▼
 │                        EXIT_PENDING
 │                              │
 │            ┌─────────────────┼─────────────────┐
 │            ▼                 ▼                 ▼
 │      CALL_EXIT         PUT_EXIT           (both closed)
 │       _FILLED           _FILLED                │
 │            │                 │                 ▼
 │            └─────────────────┴──────────── CLOSED
 │
 └─ ERROR (at any point, unrecoverable)
```

