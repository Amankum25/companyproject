# BTC Options Short Straddle — Deribit Testnet (default)

A BTC ATM Short Straddle system built with NautilusTrader 2.0. Deribit Testnet is the default venue; Bybit Testnet remains an optional venue. The project includes a read-only Deribit market dashboard and a separate testnet strategy runner.

---

## What is a Short Straddle? (Simple Explanation)

Imagine you think Bitcoin's price will stay roughly the same for the next few days — it won't go up dramatically or crash.

A **Short Straddle** profits from that prediction:

1. You **sell** a **Call option** at the current BTC price (the ATM strike).
   - A Call option gives someone else the *right to buy* BTC at the strike price.
   - You collect a **premium** (cash) for selling it.

2. You **sell** a **Put option** at the same price (same ATM strike).
   - A Put option gives someone else the *right to sell* BTC at the strike price.
   - You collect another **premium** for selling it.

You now have two premiums in hand. If BTC stays flat, both options expire worthless and you keep all the premium. If BTC moves a lot either way, you lose money buying back the options at a higher price.

**Key terms:**
- **Call option** — The right to *buy* at the strike price
- **Put option** — The right to *sell* at the strike price
- **ATM (At The Money)** — The strike price closest to the current BTC market price
- **Premium** — The price you receive for selling an option
- **Short** — Means you are the *seller* of the option

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        main.py                              │
│   Entry point: selects DATA_MODE or EXECUTION_MODE          │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                   TradingNode (NautilusTrader)              │
│  ┌─────────────────┐    ┌──────────────────────────────┐   │
│  │  Bybit Data      │    │  Bybit Execution Client      │   │
│  │  Client          │    │  (TESTNET only)              │   │
│  │  - Instruments   │    │  - Order submission          │   │
│  │  - Quotes        │    │  - Fill tracking             │   │
│  │  - Option Chain  │    │  - Position management       │   │
│  └────────┬─────────┘    └──────────┬───────────────────┘   │
│           │                         │                        │
│           ▼                         ▼                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │              StraddleStrategy (Strategy)               │  │
│  │  ┌───────────────────┐  ┌──────────────────────────┐  │  │
│  │  │ InstrumentSelector│  │  RiskManager             │  │  │
│  │  │ - Find ATM strike │  │  - Entry premium calc    │  │  │
│  │  │ - Match Call+Put  │  │  - SL/TP monitoring      │  │  │
│  │  │ - Validate specs  │  │  - Exit order management │  │  │
│  │  └───────────────────┘  └──────────────────────────┘  │  │
│  │  ┌───────────────────────────────────────────────────┐ │  │
│  │  │               StraddleState                       │ │  │
│  │  │  - Order IDs, fill prices, quantities, positions  │ │  │
│  │  └───────────────────────────────────────────────────┘ │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.13+ |
| Trading Engine | NautilusTrader 2.0.0rc8 |
| Exchange | Deribit (default) or Bybit (via official adapter) |
| Environment | Testnet only |
| Config | python-dotenv |
| Testing | pytest |
| Runtime | WSL2 Ubuntu 24.04 |

---

## Environment Setup

### Prerequisites

You need:
- Windows 10/11 with WSL2 enabled (Ubuntu 24.04)
- Deribit Testnet account for the default venue, or Bybit Testnet if explicitly selected
- Python 3.13 or newer

### 1. Install NautilusTrader in WSL2

Open a **WSL2 Ubuntu terminal** and run:

```bash
bash /mnt/c/Users/<yourname>/Downloads/bitbybit/scripts/install_wsl.sh
```

This installs Rust, uv, creates a virtual environment, and installs NautilusTrader 2.0.0rc8.

### 2. Verify installation

```bash
source ~/.venvs/btc-straddle/bin/activate
python -c "import nautilus_trader; print(nautilus_trader.__version__)"
```

### 3. Configure API credentials

The project `.env` is ready for Deribit Testnet credentials. Set `DERIBIT_TESTNET_CLIENT_ID` and `DERIBIT_TESTNET_CLIENT_SECRET` there. Keep `EXCHANGE_VENUE=DERIBIT` (or omit it; Deribit is the default). The dashboard's market data is public; credentials are only needed for account data and authenticated testnet execution.

```bash
cp .env.example .env
# Edit .env with your actual keys
```

**Never commit `.env` to git.**

---

## API Credential Configuration

Create a `.env` file (never commit this):

```
BYBIT_TESTNET_API_KEY=your_key_here
BYBIT_TESTNET_API_SECRET=your_secret_here
```

Get your keys from: https://testnet.bybit.com → Account → API Management

**Requirements for Deribit Testnet API credentials:**
- Create the client ID and client secret in your account at [Deribit Testnet](https://test.deribit.com).
- Grant only the permissions needed for your use, such as account read access for the dashboard.
- Keep the secret in the ignored local `.env`; never paste it into chat or commit it.

Bybit Testnet is still available as an optional venue by setting `EXCHANGE_VENUE=BYBIT` and configuring its separate variables.

**Requirements for Bybit Testnet API key:**
- Enable **Unified Trading Account (UTA)**
- Enable **Options trading** permission
- Note: Testnet options liquidity is limited — the bot handles empty order books gracefully

---

## Running the Application

### Live dashboard and confirmed orders (Deribit Testnet)

The browser dashboard is served by a local Python backend. It polls Deribit Testnet for the BTC index and nearest matching BTC call/put quotes every three seconds. With valid Testnet credentials, it displays the account summary and lets you choose the call, put, or both for a limit-sell order. The browser first shows the exact contracts, amount, and limit prices; submission requires typing the confirmation phrase. The API secret stays on the server and is never sent to the browser.

Activate the project environment, install the dashboard dependencies if needed, and start it from the project root:

```bash
source ~/.venvs/btc-straddle/bin/activate
python -m pip install fastapi 'uvicorn[standard]' httpx
python -m uvicorn web.server:app --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000** in your browser. Keep `EXCHANGE_VENUE=DERIBIT` and use Deribit Testnet credentials in `.env`. Market data is public. Account details need account-read permission; placing orders also needs trade permission on the Testnet API key. If private authentication fails, order controls stay disabled while the dashboard reports the error.

On Windows PowerShell, run these commands from the project folder instead:

```powershell
python -m pip install fastapi "uvicorn[standard]" httpx python-dotenv
python -m uvicorn web.server:app --host 127.0.0.1 --port 8000
```

This dashboard connects to Deribit Testnet only. Order buttons submit GTC limit orders to Testnet after review and explicit typed confirmation; manage and cancel open orders in your Deribit Testnet account.

**All commands must be run in WSL2** with the virtual environment activated:

```bash
source ~/.venvs/btc-straddle/bin/activate
cd /mnt/c/Users/amank/Downloads/bitbybit
```

### Mode 1: Data / Discovery Mode (Safe — no orders)

```bash
python main.py --mode data
```

This:
- Connects to the configured testnet venue (Deribit by default)
- Loads all BTC option instruments
- Finds the nearest ATM strike
- Displays Call and Put contracts with their quotes
- Calculates hypothetical entry/exit values
- **Never submits any orders**

### Mode 2: Testnet Execution Mode

```bash
python main.py --mode execute
```

This requires you to type `SUBMIT TESTNET STRADDLE` to confirm before any order is sent.

---

## How Level 1 Works (Naked Short Straddle)

1. Strategy starts → queries the NautilusTrader cache for all BTC option instruments
2. Finds the **nearest future expiry** where both a Call and Put exist at the same strike
3. Selects the strike **closest to the current BTC mark price** (ATM)
4. Subscribes to live option chain quotes for both contracts
5. Waits for valid, fresh quotes on both legs
6. After operator confirmation → submits **SELL LIMIT** orders for both contracts
7. Monitors order events: `OrderAccepted`, `OrderFilled`, `OrderRejected`
8. If one leg fills but the other fails → automatically closes the filled leg (recovery)
9. When both legs are filled → state = `STRADDLE_OPEN`

---

## How Level 2 Works (Risk Management)

### Entry Premium

```
entry_premium = (call_avg_fill_price × call_qty) + (put_avg_fill_price × put_qty)
```

(Both in USDT, as Bybit BTC options are USDT-settled linear contracts)

### Stop-Loss Trigger

```
sl_threshold = entry_premium × 1.01   (1% above entry)

if current_buyback_cost >= sl_threshold:
    → Close both positions immediately
```

### Take-Profit Trigger

```
tp_threshold = entry_premium × 0.98   (2% below entry)

if current_buyback_cost <= tp_threshold:
    → Close both positions immediately
```

### Buyback Cost (uses ASK prices — the cost to close short positions)

```
buyback_cost = (call_ask_price × call_remaining_qty) + (put_ask_price × put_remaining_qty)
```

---

## How Order Events Are Handled

NautilusTrader delivers all order events through the strategy's event handlers:

| Event | Handler | Action |
|-------|---------|--------|
| `OrderSubmitted` | `on_order_submitted` | Record order ID, update state to PENDING |
| `OrderAccepted` | `on_order_accepted` | Log exchange acknowledgement |
| `OrderRejected` | `on_order_rejected` | Trigger recovery for the other leg |
| `OrderFilled` | `on_order_filled` | Record fill price and qty, update position state |
| `OrderPartiallyFilled` | `on_order_filled` | (same handler, NautilusTrader uses fill events) |
| `OrderCanceled` | `on_order_canceled` | Update state, trigger recovery if needed |

---

## How Partial Fills Are Handled

### Entry partial fill

If a leg is partially filled and the remaining quantity is cancelled:
- The strategy records the **actual filled quantity only**
- It computes risk thresholds based on actual fill quantities
- It submits closing orders using the actual remaining open quantities

### Exit partial fill

If a closing order is partially filled:
- The strategy does **not** retry immediately
- It waits for the remaining quantity to fill or cancel
- It reconciles positions before submitting any retry

---

## How to Run Tests

```bash
source ~/.venvs/btc-straddle/bin/activate
cd /mnt/c/Users/amank/Downloads/bitbybit
pytest tests/ -v
```

To run a specific test file:

```bash
pytest tests/test_instrument_selector.py -v
pytest tests/test_risk_manager.py -v
pytest tests/test_execution_manager.py -v
```

---

## Common Errors and Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `ModuleNotFoundError: nautilus_trader` | Not in virtual env | Run `source ~/.venvs/btc-straddle/bin/activate` |
| `No BTC options found` | Testnet instruments not loaded | Wait 30s after start; Testnet may have limited options |
| `OrderRejected: Insufficient margin` | Testnet account has no funds | Add testnet funds using the selected exchange's testnet funding process |
| `401 Unauthorized` | Wrong API key | Check `.env` file, ensure Testnet keys are used |
| `retCode: 10001` | Invalid parameter | Check instrument ID format and quantity precision |
| `Connection refused` | Testnet service unavailable | Check the selected exchange's testnet status |

---

## Limitations

1. **Testnet liquidity**: Testnet options may have limited live quotes. The bot uses mark prices as a fallback for ATM selection but requires actual quotes for order submission.
2. **Options availability**: Testnet may have fewer expiries and strikes than Mainnet.
3. **No Greeks**: This implementation does not use delta, gamma, or vega for position sizing.
4. **Fixed quantity**: Default order quantity is 0.1 BTC on Deribit and 0.01 on Bybit.
5. **No portfolio hedging**: This is a demonstration system, not a production trading system.

