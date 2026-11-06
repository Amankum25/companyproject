"""
main.py — Entry point for the BTC Short Straddle application.

Supports Deribit Testnet and Bybit Testnet.

USAGE:
  # Data/Discovery mode (SAFE — no orders):
  python main.py --mode data

  # Testnet execution mode (requires explicit confirmation):
  python main.py --mode execute
"""

from __future__ import annotations

import argparse
import sys
import time

from config import load_config
from exchange.bybit_client import build_trading_node as build_bybit_node
from exchange.deribit_client import build_deribit_trading_node
from utils.logger import setup_logger

logger = setup_logger(__name__)

CONFIRMATION_STRING = "SUBMIT TESTNET STRADDLE"


def print_banner(venue: str) -> None:
    """Print the application startup banner."""
    print()
    print("=" * 60)
    print(f"  BTC OPTIONS SHORT STRADDLE")
    print(f"  NautilusTrader 2.0 + {venue} Testnet")
    print("=" * 60)
    print()


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="BTC ATM Short Straddle — NautilusTrader + Deribit/Bybit Testnet"
    )
    parser.add_argument(
        "--mode",
        choices=["data", "execute"],
        default="data",
        help=(
            "'data' (default): Connect, discover instruments, display quotes. "
            "No orders submitted. "
            "'execute': Same as data, plus submits SELL orders after explicit confirmation."
        ),
    )
    return parser.parse_args()


def confirm_execution(venue: str, order_quantity: str) -> bool:
    """
    Ask the operator to type a confirmation string before executing orders.
    """
    print()
    print("=" * 60)
    print(f"  *** EXECUTION MODE — {venue} TESTNET ORDER SUBMISSION ***")
    print("=" * 60)
    print()
    print(f"  This will connect to {venue} TESTNET and submit:")
    print(f"  - 1 BTC ATM CALL SELL LIMIT order, quantity {order_quantity}")
    print(f"  - 1 BTC ATM PUT SELL LIMIT order, quantity {order_quantity}")
    print("  The prices are selected from the first valid option-chain snapshot.")
    print("  Orders use GTC and may remain open until filled or cancelled.")
    print()
    print("  These are TESTNET orders (no real money).")
    print()
    print(f"  To confirm, type exactly: {CONFIRMATION_STRING}")
    print("  To cancel, type anything else or press Ctrl+C.")
    print()

    try:
        user_input = input("  > ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled by user.")
        return False

    if user_input == CONFIRMATION_STRING:
        print()
        print("  Confirmed. Proceeding with TESTNET execution.")
        print()
        return True
    else:
        print()
        print(f"  Input '{user_input}' did not match expected confirmation.")
        print("  Exiting without submitting orders.")
        print()
        return False


def get_node_builder(venue: str):
    if venue == "DERIBIT":
        return build_deribit_trading_node
    return build_bybit_node


def run_data_mode(config) -> None:
    """
    Run in DATA mode: connect, discover, display — no orders.
    """
    logger.info(f"Starting in DATA mode on {config.venue} TESTNET (no orders will be submitted).")

    builder = get_node_builder(config.venue)
    node, strategy = builder(
        config=config,
        execution_mode="DATA",
        dry_run=True,
    )

    print()
    print("=" * 60)
    print("  DATA MODE ACTIVE")
    print(f"  Connecting to {config.venue} Testnet...")
    print("  Press Ctrl+C to exit.")
    print("=" * 60)
    print()

    try:
        node.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down...")
    finally:
        logger.info("Node stopped.")
        logger.info("Straddle state: %s", strategy._state.summary())


def run_execute_mode(config) -> None:
    """
    Run in EXECUTE mode: connect, discover, then ask for confirmation before ordering.
    """
    confirmed = confirm_execution(config.venue, config.order_quantity)
    if not confirmed:
        sys.exit(0)

    logger.info(f"Starting in EXECUTE mode on {config.venue} TESTNET.")
    logger.warning("This session WILL submit SELL orders to Testnet.")

    builder = get_node_builder(config.venue)
    node, strategy = builder(
        config=config,
        execution_mode="EXECUTE",
        dry_run=False,
    )

    strategy._user_confirmed = True

    print()
    print("=" * 60)
    print(f"  EXECUTE MODE ACTIVE — {config.venue} TESTNET")
    print("  Connecting and waiting for first option chain snapshot...")
    print("  Press Ctrl+C to exit.")
    print("=" * 60)
    print()

    try:
        node.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down...")
    finally:
        logger.info("Node stopped.")
        logger.info("Final straddle state: %s", strategy._state.summary())


def main() -> None:
    """Main entry point."""
    args = parse_args()
    config = load_config()

    print_banner(config.venue)

    logger.info(f"Configuration loaded. Mode: {args.mode.upper()}")
    logger.info(f"Environment: {config.venue} TESTNET")
    logger.info(f"Underlying: {config.underlying}")
    logger.info(f"Order quantity: {config.order_quantity}")

    if args.mode == "data":
        run_data_mode(config)
    elif args.mode == "execute":
        run_execute_mode(config)
    else:
        logger.error(f"Unknown mode: {args.mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
