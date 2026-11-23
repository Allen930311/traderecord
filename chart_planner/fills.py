"""Read-only BingX fills for the chart planner (Allen, 2026-11-23).

Safety:
- the API key/secret are read ONLY from the environment (BINGX_API_KEY, BINGX_API_SECRET) at call
  time; they are never written to disk, logged, returned to the browser or put in an error message;
- only GET on one history endpoint; no order, transfer or account-change call exists here;
- use a key with READ-ONLY permission.
No key set -> `fills()` returns {"configured": False, ...} instead of failing, so the app still works.

Endpoint: GET /openApi/swap/v2/trade/allFillOrders (signed). Field names are mapped defensively
because this was written without live data: UNVERIFIED against a real account (see README).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

BASE = "https://open-api.bingx.com"
PATH = "/openApi/swap/v2/trade/allFillOrders"
MAX_SPAN_MS = 7 * 24 * 3600 * 1000  # query in windows of at most 7 days


def sign(secret: str, params: dict[str, Any]) -> str:
    canonical = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _http(url: str, key: str) -> dict:
    req = urllib.request.Request(url, headers={"X-BX-APIKEY": key})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _num(row: dict, *names: str) -> float | None:
    for n in names:
        if row.get(n) not in (None, ""):
            try:
                return float(row[n])
            except (TypeError, ValueError):
                return None
    return None


def normalize(row: dict) -> dict:
    ts = row.get("filledTm") or row.get("filledTime") or row.get("time") or row.get("updateTime")
    if isinstance(ts, str) and not ts.isdigit():
        try:
            from datetime import datetime
            ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            ts = None
    return {
        "symbol": row.get("symbol"), "order_id": str(row.get("orderId", "")),
        "side": str(row.get("side", "")).lower(), "position_side": str(row.get("positionSide", "")).lower(),
        "price": _num(row, "price", "avgPrice"), "qty": _num(row, "qty", "volume", "filledQty"),
        "fee": _num(row, "commission", "fee"), "pnl": _num(row, "realisedPnl", "realizedPnl", "pnl", "profit"),
        "filled_at_ms": int(ts) if ts not in (None, "") else None,
    }


def fills(start_ms: int, end_ms: int, symbol: str | None = None,
          http: Callable[[str, str], dict] = _http, now: Callable[[], float] = time.time) -> dict:
    key, secret = os.environ.get("BINGX_API_KEY", ""), os.environ.get("BINGX_API_SECRET", "")
    if not key or not secret:
        return {"configured": False, "fills": [],
                "hint": "Set BINGX_API_KEY and BINGX_API_SECRET (read-only key) in the environment that starts the server."}
    if end_ms <= start_ms:
        raise ValueError("end must be after start")
    out: list[dict] = []
    t = start_ms
    while t < end_ms:
        hi = min(t + MAX_SPAN_MS, end_ms)
        params: dict[str, Any] = {"startTs": t, "endTs": hi, "tradingUnit": "COIN",
                                  "timestamp": int(now() * 1000)}
        if symbol:
            params["symbol"] = symbol
        qs = urllib.parse.urlencode(sorted(params.items()))
        url = f"{BASE}{PATH}?{qs}&signature={sign(secret, params)}"
        try:
            body = http(url, key)
        except Exception as e:  # never echo the URL: it carries the signature
            raise RuntimeError(f"BingX request failed ({type(e).__name__})") from None
        if body.get("code") not in (0, "0", None):
            raise RuntimeError(f"BingX error code {body.get('code')}: {body.get('msg', '')}")
        data = body.get("data") or {}
        rows = data.get("fill_orders") or data.get("fillOrders") or (data if isinstance(data, list) else [])
        out += [normalize(r) for r in rows]
        t = hi
    out.sort(key=lambda r: r["filled_at_ms"] or 0)
    return {"configured": True, "fills": out}
