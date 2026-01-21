"""Central config for Binance-OKX SENT arbitrage."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional

import requests


# Exchange identifiers
MAKER_EXCHANGE = "binance"
TAKER_EXCHANGE = "okx"

# Symbol mapping (SENT only)
BINANCE_SYMBOL = "SENTUSDT"
OKX_INST_ID = "SENT-USDT-SWAP"
OKX_INST_TYPE = "SWAP"
QUOTE_ASSET = "USDT"
CONTRACT_TYPE = "perpetual"

# REST/WS endpoints (production only; no testnet/proxy)
BINANCE_REST_BASE_URL = "https://fapi.binance.com"
BINANCE_WS_BASE_URL = "wss://fstream.binance.com"
OKX_REST_BASE_URL = "https://www.okx.com"
OKX_WS_PUBLIC_URL = "wss://ws.okx.com:8443/ws/v5/public"
OKX_WS_PRIVATE_URL = "wss://ws.okx.com:8443/ws/v5/private"

# Public market config endpoints
BINANCE_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
OKX_INSTRUMENTS_PATH = "/api/v5/public/instruments"


@dataclass(frozen=True)
class MarketPrecision:
    """Normalized market precision/limits for a symbol."""

    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Optional[Decimal] = None


def _as_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(value))


def fetch_binance_precision(
    symbol: str = BINANCE_SYMBOL,
    base_url: str = BINANCE_REST_BASE_URL,
    timeout: int = 10,
) -> MarketPrecision:
    """Fetch Binance futures precision/limits via public exchangeInfo."""
    url = f"{base_url}{BINANCE_EXCHANGE_INFO_PATH}"
    response = requests.get(url, params={"symbol": symbol}, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    symbols = data.get("symbols", [])
    if not symbols:
        raise ValueError("Binance exchangeInfo response missing symbols")

    entry = next((item for item in symbols if item.get("symbol") == symbol), None)
    if not entry:
        raise ValueError(f"Binance symbol not found: {symbol}")

    filters = {f["filterType"]: f for f in entry.get("filters", [])}

    price_filter = filters.get("PRICE_FILTER", {})
    lot_filter = filters.get("LOT_SIZE", {})
    notional_filter = filters.get("MIN_NOTIONAL", {})

    tick_size = _as_decimal(price_filter.get("tickSize"))
    step_size = _as_decimal(lot_filter.get("stepSize"))
    min_qty = _as_decimal(lot_filter.get("minQty"))
    min_notional = _as_decimal(notional_filter.get("notional"))

    if tick_size is None or step_size is None or min_qty is None:
        raise ValueError(f"Missing Binance precision fields for {symbol}")

    return MarketPrecision(
        tick_size=tick_size,
        step_size=step_size,
        min_qty=min_qty,
        min_notional=min_notional,
    )


def fetch_okx_precision(
    inst_id: str = OKX_INST_ID,
    inst_type: str = OKX_INST_TYPE,
    base_url: str = OKX_REST_BASE_URL,
    timeout: int = 10,
) -> MarketPrecision:
    """Fetch OKX instrument precision/limits via public instruments."""
    url = f"{base_url}{OKX_INSTRUMENTS_PATH}"
    params = {"instType": inst_type, "instId": inst_id}
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()

    instruments = data.get("data", [])
    if not instruments:
        raise ValueError(f"OKX instrument not found: {inst_id}")

    inst = instruments[0]
    tick_size = _as_decimal(inst.get("tickSz"))
    step_size = _as_decimal(inst.get("lotSz"))
    min_qty = _as_decimal(inst.get("minSz"))

    if tick_size is None or step_size is None or min_qty is None:
        raise ValueError(f"Missing OKX precision fields for {inst_id}")

    return MarketPrecision(
        tick_size=tick_size,
        step_size=step_size,
        min_qty=min_qty,
    )


def fetch_precision_map() -> Dict[str, MarketPrecision]:
    """Convenience helper to fetch both sides' precision."""
    return {
        MAKER_EXCHANGE: fetch_binance_precision(),
        TAKER_EXCHANGE: fetch_okx_precision(),
    }
