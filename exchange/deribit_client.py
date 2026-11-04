"""
exchange/deribit_client.py — NautilusTrader TradingNode builder for Deribit Testnet.

Deribit is the global standard for crypto options trading.
This client integrates NautilusTrader 2.0 with Deribit Testnet (test.deribit.com).
"""

from __future__ import annotations

import logging

from nautilus_trader.adapters.deribit import (
    DERIBIT,
    DeribitDataClientConfig,
    DeribitDataClientFactory,
    DeribitEnvironment,
    DeribitExecutionClientConfig,
    DeribitExecutionClientFactory,
    DeribitProductType,
)
from nautilus_trader.common import Environment
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import AccountId, TraderId

from config import StraddleConfig
from strategy.straddle_strategy import StraddleStrategy, StraddleStrategyConfig

logger = logging.getLogger(__name__)


def build_deribit_trading_node(
    config: StraddleConfig,
    execution_mode: str = "DATA",
    dry_run: bool = True,
) -> tuple[LiveNode, StraddleStrategy]:
    """
    Build and configure the NautilusTrader LiveNode for Deribit Testnet options.
    """
    # Hard safety check: ensure Testnet
    deribit_env = DeribitEnvironment.TESTNET

    logger.info(f"Building TradingNode for Deribit {deribit_env.name.upper()}")
    logger.info(f"Execution mode: {execution_mode} | Dry run: {dry_run}")

    trader_id = TraderId.from_str("DERIBIT-STRADDLE-001")
    account_id = AccountId.from_str("DERIBIT-TESTNET-001")

    # Data Client Configuration
    # Options on Deribit: DeribitProductType.OPTION
    data_client_config = DeribitDataClientConfig(
        api_key=config.api_key if config.api_key else None,
        api_secret=config.api_secret if config.api_secret else None,
        product_types=[DeribitProductType.OPTION],
        environment=deribit_env,
    )

    node_builder = (
        LiveNode.builder("DERIBIT-STRADDLE-001", trader_id, Environment.LIVE)
        .with_reconciliation(reconciliation=True)
        .with_risk_engine_config(LiveRiskEngineConfig(bypass=True))
        .add_data_client(
            None,
            DeribitDataClientFactory(),
            data_client_config,
        )
    )

    if execution_mode == "EXECUTE" and not dry_run:
        exec_client_config = DeribitExecutionClientConfig(
            account_id=account_id,
            api_key=config.api_key,
            api_secret=config.api_secret,
            product_types=[DeribitProductType.OPTION],
            environment=deribit_env,
        )
        node_builder = node_builder.add_exec_client(
            None,
            DeribitExecutionClientFactory(),
            exec_client_config,
        )
        logger.info("Deribit Execution Client added for TESTNET order submission.")
    else:
        logger.info("Running in DATA / DRY RUN mode (no orders submitted).")

    node = node_builder.build()

    strategy_config = StraddleStrategyConfig(
        venue="DERIBIT",
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

    logger.info("Deribit TradingNode built successfully.")
    return node, strategy

