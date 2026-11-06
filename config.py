"""
config.py — Application configuration.

Supports Deribit Testnet (default) and Bybit Testnet.
Loads all settings from environment variables (via .env file).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv

# The local .env is the credential source for this workspace. In particular,
# blank fields there must not silently fall back to inherited shell secrets.
load_dotenv(override=True)


@dataclass
class StraddleConfig:
    """
    All configuration for the BTC Short Straddle strategy.
    """
    venue: str = "DERIBIT"            # "DERIBIT" or "BYBIT"
    api_key: str = ""
    api_secret: str = ""
    underlying: str = "BTC"
    order_quantity: str = "0.1"        # Deribit minimum BTC option contract size is 0.1
    stop_loss_pct: float = 0.01        # 1%
    take_profit_pct: float = 0.02      # 2%
    quote_staleness_secs: float = 30.0
    strikes_above_atm: int = 5
    strikes_below_atm: int = 5
    snapshot_interval_ms: int = 2_000

    @property
    def stop_loss_multiplier(self) -> float:
        return 1.0 + self.stop_loss_pct

    @property
    def take_profit_multiplier(self) -> float:
        return 1.0 - self.take_profit_pct


def load_config() -> StraddleConfig:
    """
    Load and validate configuration from environment variables.
    """
    # Detect venue
    venue = os.getenv("EXCHANGE_VENUE", "DERIBIT").strip().upper() or "DERIBIT"
    if venue not in {"DERIBIT", "BYBIT"}:
        raise ValueError("EXCHANGE_VENUE must be DERIBIT or BYBIT.")
    
    # Deribit credentials
    deribit_key = os.getenv("DERIBIT_TESTNET_CLIENT_ID", "").strip()
    deribit_secret = os.getenv("DERIBIT_TESTNET_CLIENT_SECRET", "").strip()

    # Bybit credentials
    bybit_key = os.getenv("BYBIT_TESTNET_API_KEY", "").strip()
    bybit_secret = os.getenv("BYBIT_TESTNET_API_SECRET", "").strip()

    if venue == "DERIBIT":
        api_key = deribit_key
        api_secret = deribit_secret
        order_qty = os.getenv("ORDER_QUANTITY", "0.1")
    else:
        api_key = bybit_key
        api_secret = bybit_secret
        order_qty = os.getenv("ORDER_QUANTITY", "0.01")

    # Filter placeholder values
    placeholders = (
        "your_deribit_client_id_here",
        "your_deribit_client_secret_here",
        "your_testnet_api_key_here",
        "your_testnet_api_secret_here",
    )
    if api_key in placeholders:
        api_key = ""
    if api_secret in placeholders:
        api_secret = ""

    return StraddleConfig(
        venue=venue,
        api_key=api_key,
        api_secret=api_secret,
        order_quantity=order_qty,
    )
