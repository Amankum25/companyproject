"""
strategy/instrument_selector.py — ATM option instrument selection logic.

WHY THIS FILE EXISTS:
  Finding the right Call and Put contracts from hundreds of listed BTC options
  is non-trivial. This module encapsulates that logic cleanly so it can be
  tested independently of the live trading node.

HOW IT WORKS:
  1. We receive a list of Instrument objects from NautilusTrader's cache.
  2. We filter them: must be BTC options, must be on BYBIT, must be active,
     must not be expired.
  3. We group them by expiry, then by strike within each expiry.
  4. We pick the nearest future expiry that has BOTH a Call and a Put at the
     same strike.
  5. Within that expiry, we find the strike closest to the BTC reference price.
  6. If two strikes are equidistant, we pick the LOWER one (deterministic
     tie-breaking rule — documented below).

IMPORTANT — NautilusTrader instrument attributes for Bybit options:
  inst.id              → InstrumentId, e.g. "BTC-27MAR26-70000-C-USDT-OPTION.BYBIT"
  inst.option_kind     → OptionKind.CALL or OptionKind.PUT
  inst.strike_price    → Price object (use float(inst.strike_price) for math)
  inst.expiration_ns   → int nanoseconds since epoch
  inst.settlement_currency → Currency object (should be USDT for Bybit linear options)
  inst.is_active()     → bool
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class OptionPair:
    """
    A matched Call and Put contract at the same strike and expiry.

    This is what the strategy ultimately needs: two instruments that form
    a valid straddle — same strike, same expiry, same settlement currency.

    Attributes
    ----------
    call_instrument : Any
        NautilusTrader Instrument object for the Call option.
    put_instrument : Any
        NautilusTrader Instrument object for the Put option.
    strike : Decimal
        The ATM strike price (in USDT).
    expiry_ns : int
        Expiry timestamp in nanoseconds.
    settlement_currency : str
        Settlement currency (expected: "USDT" for Bybit linear BTC options).
    """
    call_instrument: Any
    put_instrument: Any
    strike: Decimal
    expiry_ns: int
    settlement_currency: str

    @property
    def call_id(self) -> str:
        return str(self.call_instrument.id)

    @property
    def put_id(self) -> str:
        return str(self.put_instrument.id)


class InstrumentSelector:
    """
    Selects the ATM Call and Put pair from available BTC option instruments.

    This class contains NO NautilusTrader-specific live calls. It operates
    purely on the instrument list passed to it, making it fully unit-testable
    without a live connection.

    Parameters
    ----------
    underlying : str
        The underlying asset symbol, e.g. "BTC".
    venue : str
        The exchange venue string, e.g. "BYBIT".
    """

    def __init__(self, underlying: str = "BTC", venue: str = "BYBIT") -> None:
        self._underlying = underlying
        self._venue = venue

    def select_atm_pair(
        self,
        instruments: List[Any],
        reference_price: float,
        current_time_ns: int,
    ) -> Optional[OptionPair]:
        """
        Select the nearest-expiry ATM Call+Put pair from the instrument list.

        Parameters
        ----------
        instruments : List[Any]
            All instruments from NautilusTrader's cache.
        reference_price : float
            The current BTC mark/index price used to find the ATM strike.
        current_time_ns : int
            Current time in nanoseconds (used to filter expired contracts).

        Returns
        -------
        OptionPair | None
            The matched pair, or None if no valid pair can be found.
        """
        # Step 1: Filter to active, non-expired BTC option instruments on BYBIT
        btc_options = self._filter_btc_options(instruments, current_time_ns)

        if not btc_options:
            logger.warning(
                f"No active BTC options found on {self._venue}. "
                "Testnet may have limited instrument availability."
            )
            return None

        logger.info(f"Found {len(btc_options)} active BTC options in cache.")

        # Step 2: Group by expiry, then by strike
        # Structure: {expiry_ns: {strike_decimal: {"C": inst, "P": inst}}}
        grouped = self._group_by_expiry_and_strike(btc_options)

        if not grouped:
            logger.warning("Could not group options by expiry and strike.")
            return None

        # Step 3: Iterate expiries from nearest to farthest
        sorted_expiries = sorted(grouped.keys())

        for expiry_ns in sorted_expiries:
            strikes_at_expiry = grouped[expiry_ns]

            # Step 4: Find strikes that have BOTH Call and Put
            complete_strikes = {
                strike: contracts
                for strike, contracts in strikes_at_expiry.items()
                if "C" in contracts and "P" in contracts
            }

            if not complete_strikes:
                logger.debug(
                    f"Expiry {expiry_ns} has no complete Call+Put pairs. Trying next expiry."
                )
                continue

            # Step 5: Find the ATM strike (closest to reference price)
            atm_strike = self._find_atm_strike(
                strikes=list(complete_strikes.keys()),
                reference_price=reference_price,
            )

            if atm_strike is None:
                continue

            contracts = complete_strikes[atm_strike]
            call_inst = contracts["C"]
            put_inst = contracts["P"]

            # Step 6: Validate settlement currency compatibility
            call_ccy = str(call_inst.settlement_currency)
            put_ccy = str(put_inst.settlement_currency)

            if call_ccy != put_ccy:
                logger.error(
                    f"Settlement currency mismatch: Call={call_ccy}, Put={put_ccy}. "
                    "Cannot build a straddle with incompatible settlement currencies."
                )
                continue

            pair = OptionPair(
                call_instrument=call_inst,
                put_instrument=put_inst,
                strike=atm_strike,
                expiry_ns=expiry_ns,
                settlement_currency=call_ccy,
            )

            # Log the selection details
            self._log_selection(pair, reference_price)

            return pair

        logger.error(
            "Could not find a valid ATM Call+Put pair in any available expiry. "
            "Check that BTC options are available on the connected exchange/testnet."
        )
        return None

    def _filter_btc_options(
        self, instruments: List[Any], current_time_ns: int
    ) -> List[Any]:
        """
        Filter instruments to active, non-expired BTC options on the target venue.

        We check:
        1. The venue matches (e.g. "BYBIT")
        2. The instrument has an option_kind attribute (is an option)
        3. The instrument is not expired
        4. The instrument is active

        Parameters
        ----------
        instruments : List[Any]
            All instruments from cache.
        current_time_ns : int
            Current time in nanoseconds.

        Returns
        -------
        List[Any]
            Filtered list of valid option instruments.
        """
        result = []
        for inst in instruments:
            # Check venue
            if str(inst.id.venue) != self._venue:
                continue

            # Check it's an option (has option_kind attribute)
            if not hasattr(inst, "option_kind"):
                continue

            # Check expiry
            expiry = getattr(inst, "expiration_ns", None)
            if expiry is None:
                continue
            if expiry <= current_time_ns:
                continue  # Already expired

            # Check active status
            # NautilusTrader uses is_active() as a method in some versions,
            # or status attribute. We try both.
            try:
                if not inst.is_active():
                    continue
            except (AttributeError, TypeError):
                # If is_active() not available, check status attribute
                status = getattr(inst, "status", None)
                if status is not None and str(status) not in ("ACTIVE", "Active", "1"):
                    continue

            result.append(inst)

        return result

    def _group_by_expiry_and_strike(
        self, options: List[Any]
    ) -> Dict[int, Dict[Decimal, Dict[str, Any]]]:
        """
        Organize options into a nested dictionary: expiry → strike → {"C": inst, "P": inst}.

        Parameters
        ----------
        options : List[Any]
            Filtered option instruments.

        Returns
        -------
        Dict[int, Dict[Decimal, Dict[str, Any]]]
            Grouped structure.
        """
        grouped: Dict[int, Dict[Decimal, Dict[str, Any]]] = {}

        for inst in options:
            expiry = inst.expiration_ns
            # strike_price is a NautilusTrader Price object; convert to Decimal for exact math
            try:
                strike = Decimal(str(inst.strike_price))
            except (AttributeError, ValueError, TypeError):
                try:
                    strike = Decimal(str(float(inst.strike_price)))
                except (AttributeError, ValueError, TypeError) as e:
                    logger.debug(f"Could not parse strike price for {inst.id}: {e}")
                    continue

            # option_kind is a NautilusTrader OptionKind enum
            # Its string representation is "CALL" or "PUT"
            kind_str = str(inst.option_kind).upper()
            if "CALL" in kind_str:
                kind = "C"
            elif "PUT" in kind_str:
                kind = "P"
            else:
                logger.debug(f"Unknown option kind '{kind_str}' for {inst.id}")
                continue

            if expiry not in grouped:
                grouped[expiry] = {}
            if strike not in grouped[expiry]:
                grouped[expiry][strike] = {}

            grouped[expiry][strike][kind] = inst

        return grouped

    def _find_atm_strike(
        self, strikes: List[Decimal], reference_price: float
    ) -> Optional[Decimal]:
        """
        Find the strike closest to the reference price.

        TIE-BREAKING RULE:
        If two strikes are equidistant from the reference price (e.g. price=100,
        strikes at 95 and 105 — both 5 away), we select the LOWER strike.
        This is a deterministic, documented rule. The choice of lower vs. higher
        is arbitrary, but it must be consistent and documented so the system
        behaves predictably.

        Parameters
        ----------
        strikes : List[Decimal]
            Available strikes at a given expiry.
        reference_price : float
            BTC mark or index price.

        Returns
        -------
        Decimal | None
            The ATM strike.
        """
        if not strikes:
            return None

        ref = Decimal(str(reference_price))

        # Sort to ensure deterministic tie-breaking (lower strike wins)
        sorted_strikes = sorted(strikes)

        atm = min(sorted_strikes, key=lambda s: (abs(s - ref), s))
        distance = abs(atm - ref)

        logger.info(
            f"ATM selection: reference_price={reference_price:.2f}, "
            f"selected_strike={atm}, distance={distance:.2f}"
        )

        return atm

    def _log_selection(self, pair: OptionPair, reference_price: float) -> None:
        """Log the selected instrument pair in a readable format."""
        call_inst = pair.call_instrument
        put_inst = pair.put_instrument

        logger.info("=" * 60)
        logger.info("  SELECTED ATM OPTION PAIR")
        logger.info("=" * 60)
        logger.info(f"  BTC Reference Price   : {reference_price:.2f}")
        logger.info(f"  Selected Strike       : {pair.strike}")
        logger.info(f"  Expiry (ns)           : {pair.expiry_ns}")
        logger.info(f"  Settlement Currency   : {pair.settlement_currency}")
        logger.info(f"  CALL Instrument ID    : {pair.call_id}")
        logger.info(f"  PUT  Instrument ID    : {pair.put_id}")

        # Log contract specs if available
        for label, inst in [("CALL", call_inst), ("PUT", put_inst)]:
            try:
                logger.info(
                    f"  {label} specs: "
                    f"multiplier={inst.multiplier}, "
                    f"size_increment={inst.size_increment}, "
                    f"min_quantity={inst.min_quantity}, "
                    f"price_increment={inst.price_increment}"
                )
            except AttributeError:
                logger.info(f"  {label} specs: (some attributes unavailable)")

        logger.info("=" * 60)

