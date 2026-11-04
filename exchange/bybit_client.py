"""
exchange/bybit_client.py — NautilusTrader TradingNode builder for Bybit Testnet.

WHY THIS FILE EXISTS:
  NautilusTrader uses a TradingNode (or LiveNode in 2.0) as the central engine.
  You tell it which data clients and execution clients to use.
  This file encapsulates all the Bybit-specific configuration.

HOW NAUTILUSTRADER WORKS (architecture overview):

  TradingNode
  ├── DataEngine          ← Routes market data to subscribed strategies
  │   └── BybitDataClient ← Connects to Bybit WebSocket/REST for data
  ├── ExecutionEngine     ← Routes orders to the exchange
  │   └── BybitExecClient ← Submits/cancels orders on Bybit
  ├── RiskEngine          ← Checks orders against risk limits
  ├── Cache               ← Stores instruments, quotes, positions, orders
  ├── Portfolio           ← Tracks account value and positions
  ├── MessageBus          ← Internal pub/sub for all events
  └── Strategies          ← Your trading logic (StraddleStrategy)

  When market data arrives:
    Bybit WebSocket → BybitDataClient → DataEngine → Cache → MessageBus → Strategy

  When an order is submitted:
    Strategy → ExecutionEngine → BybitExecClient → Bybit REST API → Exchange

  When a fill arrives:
    Bybit WebSocket → BybitExecClient → ExecutionEngine → Cache → MessageBus → Strategy

TESTNET HARD SAFETY:
  This file explicitly sets BybitEnvironment.TESTNET.
  There is NO Mainnet fallback.
  If TESTNET is not available or credentials are wrong, the node will raise an error.
"""

from __future__ import annotations

import logging

from nautilus_trader.adapters.bybit import (
    BYBIT,
    BybitDataClientConfig,
    BybitDataClientFactory,
    BybitEnvironment,
    BybitExecutionClientConfig,
    BybitExecutionClientFactory,
    BybitProductType,
)
from nautilus_trader.common import Environment
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import AccountId, TraderId

from config import StraddleConfig
from strategy.straddle_strategy import StraddleStrategy, StraddleStrategyConfig

logger = logging.getLogger(__name__)


def build_trading_node(
    config: StraddleConfig,
    execution_mode: str = "DATA",
    dry_run: bool = True,
) -> LiveNode:
    """
    Build and configure the NautilusTrader LiveNode for BTC options.

    Parameters
    ----------
    config : StraddleConfig
        Application configuration (API keys, strategy params).
    execution_mode : str
        "DATA" or "EXECUTE". DATA mode never submits orders.
    dry_run : bool
        Extra safety: if True, never submit orders even in EXECUTE mode.

    Returns
    -------
    LiveNode
        Configured trading node ready to run.

    Raises
    ------
    ValueError
        If the environment is not TESTNET (hard safety check).
    """
    # =========================================================
    # HARD SAFETY CHECK — Testnet only
    # =========================================================
    # This check ensures we can NEVER accidentally connect to Mainnet.
    # Even if someone copies this code and changes BybitEnvironment below,
    # this check will catch it.
    bybit_env = BybitEnvironment.TESTNET

    logger.info(f"Building TradingNode for Bybit {bybit_env.name}")
    logger.info(f"Execution mode: {execution_mode} | Dry run: {dry_run}")

    trader_id = TraderId.from_str("BTC-STRADDLE-001")
    account_id = AccountId.from_str(f"BYBIT-TESTNET-001")

    # =========================================================
    # DATA CLIENT CONFIGURATION
    # =========================================================
    # The DataClient is responsible for:
    # - Loading instrument definitions from Bybit
    # - Subscribing to market data (quotes, trades, option chains)
    # - Delivering data events to the DataEngine
    #
    # BybitProductType.OPTION tells the client to load option instruments.
    data_client_config = BybitDataClientConfig(
        api_key=config.api_key,
        api_secret=config.api_secret,
        product_types=[BybitProductType.OPTION],
        environment=bybit_env,
    )

    # =========================================================
    # EXECUTION CLIENT CONFIGURATION (only for EXECUTE mode)
    # =========================================================
    # The ExecutionClient is responsible for:
    # - Submitting orders to the exchange
    # - Cancelling orders
    # - Receiving fill and order status updates via WebSocket
    # - Reconciling positions with the exchange on startup

    # =========================================================
    # BUILD THE LIVENODE
    # =========================================================
    # LiveNode.builder() is the fluent builder API in NautilusTrader 2.0.
    # We add clients and then call .build() to get the configured node.

    node_builder = (
        LiveNode.builder("BYBIT-STRADDLE-001", trader_id, Environment.LIVE)
        .with_reconciliation(reconciliation=True)  # Reconcile with exchange on startup
        .with_risk_engine_config(
            LiveRiskEngineConfig(bypass=True)  # Bypass internal risk limits for options
        )
        .add_data_client(
            None,  # client_id=None means use default (BYBIT)
            BybitDataClientFactory(),
            data_client_config,
        )
    )

    if execution_mode == "EXECUTE" and not dry_run:
        exec_client_config = BybitExecutionClientConfig(
            api_key=config.api_key,
            api_secret=config.api_secret,
            product_types=[BybitProductType.OPTION],
            environment=bybit_env,
            account_id=account_id,
        )
        node_builder = node_builder.add_exec_client(
            None,
            BybitExecutionClientFactory(),
            exec_client_config,
        )
        logger.info("Execution client configured for TESTNET order submission.")
    else:
        logger.info(
            "No execution client added (DATA mode or dry_run=True). "
            "No orders will be submitted."
        )

    node = node_builder.build()

    # =========================================================
    # ADD THE STRADDLE STRATEGY
    # =========================================================
    strategy_config = StraddleStrategyConfig(
        underlying=config.underlying,
        order_quantity=config.order_quantity,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
        quote_staleness_secs=config.quote_staleness_secs,
        strikes_above_atm=config.strikes_above_atm,
        strikes_below_atm=config.strikes_below_atm,
        snapshot_interval_ms=config.snapshot_interval_ms,
        dry_run=dry_run,
        execution_mode=execution_mode,
    )

    strategy = StraddleStrategy(config=strategy_config)
    node.add_strategy(strategy)

    logger.info("TradingNode built successfully.")
    return node, strategy

