"""One grouping key per traded underlying, independent of how a source spelled it.

Historical seeds say ``ETH``; PropW says ``ETHUSDT``; a USDC-margined contract
says ``BTCUSDCP``. Statistics that group or count by instrument use
``base_asset`` so one underlying is one bucket. The recorded instrument string
is never rewritten.

Only stablecoin quote suffixes are removed. A bare ``USD`` quote is kept, so a
forex symbol such as ``EURUSD`` is never collapsed to ``EUR``. Contract-size
prefixes such as ``1000SHIB`` are kept: they are a different contract unit.
"""
from __future__ import annotations

import re
from typing import Any

_STABLECOIN_QUOTES = ("USDCP", "USDT", "USDC", "BUSD")
_SEPARATORS = re.compile(r"[\s/_:\-]")
# Longest first: "BTC/USDT Perpetual", "BTCUSDT.PERP", "BTCUSDT.P", "SOLUSDTPERP".
_PERPETUAL_SUFFIXES = (".PERPETUAL", "PERPETUAL", ".PERP", "PERP", ".P")


def base_asset(instrument: Any) -> str:
    """Return the underlying grouping key, or ``unknown`` when none is recorded."""
    if not isinstance(instrument, str) or not instrument.strip():
        return "unknown"
    symbol = _SEPARATORS.sub("", instrument.strip().upper())
    for suffix in _PERPETUAL_SUFFIXES:
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            symbol = symbol[: -len(suffix)]
            break
    for quote in _STABLECOIN_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol
