"""
tests/test_instrument_selector.py — Unit tests for ATM selection logic.

These tests run entirely without a live connection. We create mock instrument
objects with the same attributes that NautilusTrader uses.

WHY THESE TESTS MATTER:
  The ATM selection is the most important pre-trade step. Getting the wrong
  strike or mixing Call and Put from different expiries would create a broken
  straddle. These tests verify correctness in edge cases.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional
from unittest.mock import MagicMock

import pytest

from strategy.instrument_selector import InstrumentSelector, OptionPair


# =============================================================================
# Mock Infrastructure
# We create lightweight fake instruments that have the same attributes
# NautilusTrader exposes, without requiring the actual library.
# =============================================================================

class MockOptionKind(Enum):
    CALL = "CALL"
    PUT = "PUT"


class MockCurrency:
    def __init__(self, code: str):
        self._code = code
    def __str__(self) -> str:
        return self._code


class MockInstrumentId:
    def __init__(self, symbol: str, venue: str):
        self.symbol = symbol
        self.venue = MockVenue(venue)
    def __str__(self) -> str:
        return f"{self.symbol}.{self.venue}"


class MockVenue:
    def __init__(self, name: str):
        self._name = name
    def __str__(self) -> str:
        return self._name


class MockPrice:
    def __init__(self, value: float):
        self._value = float(value)
    def __float__(self) -> float:
        return float(self._value)
    def __str__(self) -> str:
        return str(self._value)


@dataclass
class MockOption:
    """Fake NautilusTrader option instrument for testing."""
    symbol: str
    venue: str
    option_kind: MockOptionKind
    strike: float
    expiry_ns: int
    settlement: str = "USDT"
    active: bool = True
    multiplier: float = 1.0
    size_increment: float = 0.01
    min_quantity: float = 0.01
    price_increment_val: float = 0.01

    @property
    def id(self) -> MockInstrumentId:
        return MockInstrumentId(self.symbol, self.venue)

    @property
    def strike_price(self) -> MockPrice:
        return MockPrice(self.strike)

    @property
    def expiration_ns(self) -> int:
        return self.expiry_ns

    @property
    def settlement_currency(self) -> MockCurrency:
        return MockCurrency(self.settlement)

    @property
    def price_increment(self) -> MockPrice:
        return MockPrice(self.price_increment_val)

    def is_active(self) -> bool:
        return self.active


def make_option(
    kind: str,
    strike: float,
    expiry_ns: int,
    venue: str = "BYBIT",
    settlement: str = "USDT",
    active: bool = True,
) -> MockOption:
    """Helper to create a mock option instrument."""
    kind_enum = MockOptionKind.CALL if kind == "C" else MockOptionKind.PUT
    symbol = f"BTC-EXP{expiry_ns}-{int(strike)}-{kind}-{settlement}-OPTION"
    return MockOption(
        symbol=symbol,
        venue=venue,
        option_kind=kind_enum,
        strike=strike,
        expiry_ns=expiry_ns,
        settlement=settlement,
        active=active,
    )


NOW_NS = int(time.time() * 1e9)
EXPIRY_1 = NOW_NS + 1 * 24 * 3600 * int(1e9)   # 1 day from now
EXPIRY_2 = NOW_NS + 7 * 24 * 3600 * int(1e9)   # 7 days from now
EXPIRY_3 = NOW_NS + 30 * 24 * 3600 * int(1e9)  # 30 days from now


# =============================================================================
# Tests
# =============================================================================

class TestInstrumentSelectorATM:
    """Tests for ATM strike selection."""

    def setup_method(self):
        self.selector = InstrumentSelector(underlying="BTC", venue="BYBIT")

    def test_selects_closest_strike_to_reference_price(self):
        """
        Given BTC at 50000, with strikes at 49000 50000 51000,
        the ATM should be 50000.
        """
        instruments = [
            make_option("C", 49000, EXPIRY_1),
            make_option("P", 49000, EXPIRY_1),
            make_option("C", 50000, EXPIRY_1),
            make_option("P", 50000, EXPIRY_1),
            make_option("C", 51000, EXPIRY_1),
            make_option("P", 51000, EXPIRY_1),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is not None
        assert pair.strike == Decimal("50000")

    def test_selects_nearest_strike_when_not_exact(self):
        """
        Given BTC at 50100, with strikes at 49000 50000 51000,
        ATM should be 50000 (distance 100) vs 51000 (distance 900).
        """
        instruments = [
            make_option("C", 49000, EXPIRY_1),
            make_option("P", 49000, EXPIRY_1),
            make_option("C", 50000, EXPIRY_1),
            make_option("P", 50000, EXPIRY_1),
            make_option("C", 51000, EXPIRY_1),
            make_option("P", 51000, EXPIRY_1),
        ]
        pair = self.selector.select_atm_pair(instruments, 50100.0, NOW_NS)
        assert pair is not None
        assert pair.strike == Decimal("50000")

    def test_tie_breaking_selects_lower_strike(self):
        """
        Given BTC at 50500 with strikes at 50000 and 51000 (both 500 away),
        the lower strike (50000) should be selected per the tie-breaking rule.
        """
        instruments = [
            make_option("C", 50000, EXPIRY_1),
            make_option("P", 50000, EXPIRY_1),
            make_option("C", 51000, EXPIRY_1),
            make_option("P", 51000, EXPIRY_1),
        ]
        pair = self.selector.select_atm_pair(instruments, 50500.0, NOW_NS)
        assert pair is not None
        assert pair.strike == Decimal("50000"), (
            "Tie-breaking rule: lower strike should be selected when two strikes are equidistant."
        )

    def test_matches_call_and_put_at_same_strike(self):
        """The returned pair must have both Call and Put at the same strike."""
        instruments = [
            make_option("C", 50000, EXPIRY_1),
            make_option("P", 50000, EXPIRY_1),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is not None
        assert "CALL" in str(pair.call_instrument.option_kind).upper()
        assert "PUT" in str(pair.put_instrument.option_kind).upper()
        assert pair.call_instrument.strike == pair.put_instrument.strike

    def test_different_expiries_do_not_mix(self):
        """Call from EXPIRY_1 and Put from EXPIRY_2 must NOT form a pair."""
        instruments = [
            make_option("C", 50000, EXPIRY_1),  # Call at expiry 1
            make_option("P", 50000, EXPIRY_2),  # Put at expiry 2 (different!)
            # No complete pair at EXPIRY_1 (no Put)
            # No complete pair at EXPIRY_2 (no Call)
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is None, "Should not form a pair from different expiries."

    def test_returns_none_when_call_missing(self):
        """If only Put exists at all strikes, no pair should be found."""
        instruments = [
            make_option("P", 50000, EXPIRY_1),
            make_option("P", 51000, EXPIRY_1),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is None

    def test_returns_none_when_put_missing(self):
        """If only Call exists at all strikes, no pair should be found."""
        instruments = [
            make_option("C", 50000, EXPIRY_1),
            make_option("C", 51000, EXPIRY_1),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is None

    def test_returns_none_when_no_instruments(self):
        """Empty instrument list returns None."""
        pair = self.selector.select_atm_pair([], 50000.0, NOW_NS)
        assert pair is None

    def test_returns_none_when_all_expired(self):
        """Expired options (expiry in the past) should be filtered out."""
        past_expiry = NOW_NS - 1  # Already expired
        instruments = [
            make_option("C", 50000, past_expiry),
            make_option("P", 50000, past_expiry),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is None

    def test_selects_nearest_expiry(self):
        """
        When multiple expiries exist, should select the nearest future one
        that has both Call and Put.
        """
        instruments = [
            make_option("C", 50000, EXPIRY_1),
            make_option("P", 50000, EXPIRY_1),
            make_option("C", 50000, EXPIRY_2),
            make_option("P", 50000, EXPIRY_2),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is not None
        assert pair.expiry_ns == EXPIRY_1, "Should select the NEAREST expiry."

    def test_skips_incomplete_expiry_and_uses_next(self):
        """
        If the nearest expiry has only Call but no Put,
        should move to the next expiry.
        """
        instruments = [
            make_option("C", 50000, EXPIRY_1),  # Only call at EXPIRY_1
            make_option("C", 50000, EXPIRY_2),
            make_option("P", 50000, EXPIRY_2),  # Both at EXPIRY_2
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is not None
        assert pair.expiry_ns == EXPIRY_2, (
            "Should fall back to EXPIRY_2 since EXPIRY_1 has no Put."
        )

    def test_ignores_inactive_instruments(self):
        """Inactive (delisted) instruments should not be selected."""
        instruments = [
            make_option("C", 50000, EXPIRY_1, active=False),
            make_option("P", 50000, EXPIRY_1, active=False),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is None

    def test_ignores_non_bybit_instruments(self):
        """Instruments from a different venue should be ignored."""
        instruments = [
            make_option("C", 50000, EXPIRY_1, venue="BINANCE"),
            make_option("P", 50000, EXPIRY_1, venue="BINANCE"),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is None

    def test_settlement_currency_in_result(self):
        """The returned pair should include the settlement currency."""
        instruments = [
            make_option("C", 50000, EXPIRY_1, settlement="USDT"),
            make_option("P", 50000, EXPIRY_1, settlement="USDT"),
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is not None
        assert pair.settlement_currency == "USDT"

    def test_rejects_mismatched_settlement_currency(self):
        """
        If Call and Put have different settlement currencies,
        the pair must be rejected.
        """
        instruments = [
            make_option("C", 50000, EXPIRY_1, settlement="USDT"),
            make_option("P", 50000, EXPIRY_1, settlement="BTC"),  # Different!
        ]
        pair = self.selector.select_atm_pair(instruments, 50000.0, NOW_NS)
        assert pair is None, "Cannot form a straddle with incompatible settlement currencies."

    def test_multiple_strikes_with_complete_pairs(self):
        """
        Given multiple complete pairs at different strikes,
        select the one closest to reference price.
        """
        instruments = [
            make_option("C", 48000, EXPIRY_1),
            make_option("P", 48000, EXPIRY_1),
            make_option("C", 50000, EXPIRY_1),
            make_option("P", 50000, EXPIRY_1),
            make_option("C", 52000, EXPIRY_1),
            make_option("P", 52000, EXPIRY_1),
        ]
        # Reference price 49800 — closest to 50000
        pair = self.selector.select_atm_pair(instruments, 49800.0, NOW_NS)
        assert pair is not None
        assert pair.strike == Decimal("50000")

