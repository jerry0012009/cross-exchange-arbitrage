"""Binance USDT-margined futures client (maker-side, minimal interface)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

import requests
import websockets

from .binance_okx_config import BINANCE_REST_BASE_URL, BINANCE_WS_BASE_URL, fetch_binance_precision


class BinanceError(Exception):
    """Base error for Binance client failures."""


class BinanceApiError(BinanceError):
    """Raised when Binance REST responds with an error payload."""

    def __init__(self, status_code: int, payload: Any):
        super().__init__(f"Binance API error {status_code}: {payload}")
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class BinanceBbo:
    bid: Decimal
    ask: Decimal
    ts: float


def _as_decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _round_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class BinanceClient:
    """Minimal Binance USDT futures REST/WS client for maker trading."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        ws_base_url: Optional[str] = None,
        recv_window: int = 5000,
        timeout: int = 10,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("BINANCE_API_KEY") or "").strip()
        self.api_secret = (api_secret or os.getenv("BINANCE_API_SECRET") or "").strip()
        if not self.api_key or not self.api_secret:
            raise BinanceError("Missing BINANCE_API_KEY/BINANCE_API_SECRET")

        self.base_url = (base_url or os.getenv("BINANCE_BASE_URL") or BINANCE_REST_BASE_URL).rstrip("/")
        self.ws_base_url = (ws_base_url or os.getenv("BINANCE_WS_BASE_URL") or BINANCE_WS_BASE_URL).rstrip("/")
        self.recv_window = int(recv_window)
        self.timeout = int(timeout)
        self.logger = logger or logging.getLogger(__name__)

        self.best_bid: Optional[Decimal] = None
        self.best_ask: Optional[Decimal] = None
        self._last_bbo_ts: float = 0.0

        self.listen_key: Optional[str] = None
        self.stop_flag = False

        self.on_order_update: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_bbo_update: Optional[Callable[[Decimal, Decimal], None]] = None

    def set_callbacks(
        self,
        *,
        on_order_update: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_bbo_update: Optional[Callable[[Decimal, Decimal], None]] = None,
    ) -> None:
        self.on_order_update = on_order_update
        self.on_bbo_update = on_bbo_update

    def _headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key}

    def _signed_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(params)
        payload.setdefault("timestamp", int(time.time() * 1000))
        payload.setdefault("recvWindow", self.recv_window)
        query = urlencode(payload, doseq=True)
        signature = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        payload["signature"] = signature
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        payload = dict(params or {})
        if signed:
            payload = self._signed_params(payload)
        response = requests.request(
            method=method,
            url=url,
            params=payload,
            headers=self._headers() if self.api_key else None,
            timeout=self.timeout,
        )
        try:
            data = response.json()
        except ValueError:
            raise BinanceApiError(response.status_code, response.text)
        if response.status_code >= 400 or isinstance(data, dict) and data.get("code", 0) not in (0, None):
            raise BinanceApiError(response.status_code, data)
        return data

    # ---------------------------
    # REST endpoints
    # ---------------------------
    def get_exchange_info(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        params = {"symbol": symbol.upper()} if symbol else None
        return self._request("GET", "/fapi/v1/exchangeInfo", params=params, signed=False)

    def get_book_ticker(self, symbol: str) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/ticker/bookTicker", params={"symbol": symbol.upper()}, signed=False)

    def get_position_risk(self, symbol: Optional[str] = None) -> Any:
        params = {"symbol": symbol.upper()} if symbol else {}
        return self._request("GET", "/fapi/v2/positionRisk", params=params, signed=True)

    def get_position_mode(self) -> bool:
        """Return True when account is in hedge (dual-side) mode."""
        data = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
        return bool(data.get("dualSidePosition"))

    def get_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise BinanceError("get_order requires order_id or client_order_id")
        return self._request("GET", "/fapi/v1/order", params=params, signed=True)

    def cancel_order(
        self,
        symbol: str,
        *,
        order_id: Optional[int] = None,
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"symbol": symbol.upper()}
        if order_id is not None:
            params["orderId"] = int(order_id)
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise BinanceError("cancel_order requires order_id or client_order_id")
        return self._request("DELETE", "/fapi/v1/order", params=params, signed=True)

    def place_post_only_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        *,
        client_order_id: Optional[str] = None,
        reduce_only: Optional[bool] = None,
        position_side: Optional[str] = None,
        price_tick: Optional[Decimal] = None,
        size_step: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        side_key = (side or "").upper()
        if side_key not in {"BUY", "SELL"}:
            raise BinanceError("side must be BUY or SELL")

        qty = Decimal(str(quantity))
        px = Decimal(str(price))
        if size_step:
            qty = _round_step(qty, size_step)
        if price_tick:
            px = _round_step(px, price_tick)

        params: Dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side_key,
            "type": "LIMIT",
            "timeInForce": "GTX",
            "quantity": str(qty),
            "price": str(px),
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        if reduce_only is not None:
            params["reduceOnly"] = "true" if reduce_only else "false"
        if position_side:
            params["positionSide"] = str(position_side).upper()
        return self._request("POST", "/fapi/v1/order", params=params, signed=True)

    def fetch_precision(self, symbol: str) -> Tuple[Decimal, Decimal, Decimal, Optional[Decimal]]:
        precision = fetch_binance_precision(symbol=symbol, base_url=self.base_url, timeout=self.timeout)
        return (
            precision.tick_size,
            precision.step_size,
            precision.min_qty,
            precision.min_notional,
        )

    def fetch_bbo_prices(
        self,
        symbol: str,
        *,
        ws_max_age: float = 2.0,
    ) -> BinanceBbo:
        """Return best bid/ask using WS if fresh, otherwise REST fallback."""
        now = time.time()
        if (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > 0
            and self.best_ask > 0
            and self.best_bid < self.best_ask
            and (now - self._last_bbo_ts) <= float(ws_max_age)
        ):
            return BinanceBbo(self.best_bid, self.best_ask, self._last_bbo_ts)

        data = self.get_book_ticker(symbol)
        bid = _as_decimal(data.get("bidPrice")) or Decimal("0")
        ask = _as_decimal(data.get("askPrice")) or Decimal("0")
        self._update_bbo(bid, ask)
        return BinanceBbo(bid, ask, self._last_bbo_ts)

    # ---------------------------
    # WebSocket helpers
    # ---------------------------
    def _update_bbo(self, bid: Decimal, ask: Decimal) -> None:
        self.best_bid = bid
        self.best_ask = ask
        self._last_bbo_ts = time.time()
        if self.on_bbo_update:
            self.on_bbo_update(bid, ask)

    async def _handle_public_ws(self, symbol: str) -> None:
        url = f"{self.ws_base_url}/ws/{symbol.lower()}@bookTicker"
        backoff = 1.0
        while not self.stop_flag:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self.logger.info("✅ Binance public WS connected")
                    backoff = 1.0
                    while not self.stop_flag:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        if data.get("e") != "bookTicker":
                            continue
                        bid = _as_decimal(data.get("b"))
                        ask = _as_decimal(data.get("a"))
                        if bid and ask and bid > 0 and ask > 0:
                            self._update_bbo(bid, ask)
            except Exception as exc:
                self.logger.warning(f"⚠️ Binance public WS error: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 1.5)

    def _normalize_order_update(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if data.get("e") != "ORDER_TRADE_UPDATE":
            return None
        order = data.get("o") or {}
        status_raw = str(order.get("X") or "").upper()
        status_map = {
            "NEW": "OPEN",
            "PARTIALLY_FILLED": "PARTIALLY_FILLED",
            "FILLED": "FILLED",
            "CANCELED": "CANCELED",
            "EXPIRED": "CANCELED",
            "REJECTED": "REJECTED",
        }
        status = status_map.get(status_raw, status_raw or "UNKNOWN")
        return {
            "event_type": "ORDER_TRADE_UPDATE",
            "symbol": str(order.get("s") or ""),
            "order_id": order.get("i"),
            "client_order_id": order.get("c"),
            "side": order.get("S"),
            "status": status,
            "order_type": order.get("o"),
            "time_in_force": order.get("f"),
            "price": _as_decimal(order.get("p")),
            "avg_price": _as_decimal(order.get("ap")),
            "orig_qty": _as_decimal(order.get("q")),
            "filled_qty": _as_decimal(order.get("z")),
            "last_filled_qty": _as_decimal(order.get("l")),
            "reduce_only": order.get("R"),
            "raw": order,
        }

    def create_listen_key(self) -> str:
        payload = self._request("POST", "/fapi/v1/listenKey", signed=False)
        listen_key = payload.get("listenKey")
        if not listen_key:
            raise BinanceError(f"Failed to create listenKey: {payload}")
        self.listen_key = str(listen_key)
        return self.listen_key

    def keepalive_listen_key(self) -> None:
        if not self.listen_key:
            raise BinanceError("listenKey missing; call create_listen_key first")
        self._request("PUT", "/fapi/v1/listenKey", params={"listenKey": self.listen_key}, signed=False)

    async def _handle_user_ws(self) -> None:
        if not self.listen_key:
            self.create_listen_key()
        backoff = 1.0
        keepalive_interval = 30 * 60
        next_keepalive = time.time() + keepalive_interval

        while not self.stop_flag:
            try:
                url = f"{self.ws_base_url}/ws/{self.listen_key}"
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self.logger.info("✅ Binance user WS connected")
                    backoff = 1.0
                    while not self.stop_flag:
                        now = time.time()
                        if now >= next_keepalive:
                            try:
                                self.keepalive_listen_key()
                                next_keepalive = now + keepalive_interval
                            except Exception as exc:
                                self.logger.warning(f"⚠️ listenKey keepalive failed: {exc}")

                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue

                        data = json.loads(msg)
                        update = self._normalize_order_update(data)
                        if update and self.on_order_update:
                            self.on_order_update(update)
            except Exception as exc:
                self.logger.warning(f"⚠️ Binance user WS error: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 1.5)

    def stop(self) -> None:
        self.stop_flag = True

    async def run_websockets(self, symbol: str) -> None:
        """Run public + private websocket streams concurrently."""
        self.stop_flag = False
        await asyncio.gather(
            self._handle_public_ws(symbol),
            self._handle_user_ws(),
        )
