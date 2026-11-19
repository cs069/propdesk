"""Public market data from Delta Exchange (candles, order book, trades, ticker), with a short cache
so several traders watching the same market don't hit Delta's rate limits."""
import threading
import time

import requests

import config
from delta_client import DeltaClient, DeltaError

_pub = DeltaClient("", "", config.BASE_URLS["testnet" if config.MODE == "testnet" else "live"])
_cache, _lock = {}, threading.Lock()
RESOLUTIONS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def ticker(symbol):
    def get():
        t = _pub._req("GET", f"/v2/tickers/{symbol}", auth=False) or {}
        last, opn = _f(t.get("close")), _f(t.get("open"))
        q = t.get("quotes") or {}
        return {
            "symbol": symbol,
            "last": last, "mark": _f(t.get("mark_price"), last), "index": _f(t.get("spot_price")),
            "open": opn, "high": _f(t.get("high")), "low": _f(t.get("low")),
            "change_pct": ((last - opn) / opn * 100) if last and opn else _f(t.get("mark_change_24h")),
            "volume": _f(t.get("volume")), "turnover_usd": _f(t.get("turnover_usd") or t.get("turnover")),
            "oi": _f(t.get("oi")), "funding_rate": _f(t.get("funding_rate")),
            "best_bid": _f(q.get("best_bid")), "best_ask": _f(q.get("best_ask")),
        }
    return _cached(("t", symbol), 1.5, get)


def orderbook(symbol, depth=15):
    def get():
        b = _pub._req("GET", f"/v2/l2orderbook/{symbol}", query={"depth": depth}, auth=False) or {}
        side = lambda rows: [{"price": _f(r.get("price")), "size": _f(r.get("size"), 0)} for r in rows or []]
        return {"bids": side(b.get("buy")), "asks": side(b.get("sell")), "ts": b.get("last_updated_at")}
    return _cached(("b", symbol, depth), 1.0, get)


def trades(symbol):
    def get():
        r = _pub._req("GET", f"/v2/trades/{symbol}", auth=False) or {}
        rows = r.get("trades", r) if isinstance(r, dict) else r
        out = []
        for t in (rows or [])[:40]:
            ts = t.get("timestamp") or 0
            out.append({"side": t.get("side"), "size": _f(t.get("size"), 0),
                        "price": _f(t.get("price")), "ts": ts / 1e6 if ts > 1e12 else ts})
        return out
    return _cached(("tr", symbol), 1.5, get)


def candles(symbol, res="5m", count=500):
    step = RESOLUTIONS.get(res, 300)

    def get():
        end = int(time.time())
        rows = _pub._req("GET", "/v2/history/candles", auth=False,
                         query={"resolution": res, "symbol": symbol, "start": end - step * count, "end": end}) or []
        out = [{"time": int(r["time"]), "open": _f(r["open"]), "high": _f(r["high"]), "low": _f(r["low"]),
                "close": _f(r["close"]), "volume": _f(r.get("volume"), 0)} for r in rows if r.get("time")]
        return sorted(out, key=lambda c: c["time"])
    return _cached(("c", symbol, res), 4 if step <= 300 else 15, get)


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except DeltaError as e:
        return None, str(e)


def movers(limit=14):
    """Most traded perpetuals with 24h change, for the ticker strip."""
    def get():
        rows = _pub._req("GET", "/v2/tickers", query={"contract_types": "perpetual_futures"}, auth=False) or []
        out = []
        for t in rows:
            last, opn = _f(t.get("close")), _f(t.get("open"))
            if not last or not opn:
                continue
            out.append({"symbol": t.get("symbol"), "last": last, "change_pct": (last - opn) / opn * 100,
                        "turnover": _f(t.get("turnover_usd") or t.get("turnover"), 0)})
        return sorted(out, key=lambda r: r["turnover"], reverse=True)[:limit]
    return _cached(("movers",), 15, get)


BINANCE_FAPI = "https://fapi.binance.com/fapi/v1/klines"


def binance_symbol(delta_symbol):
    """Delta perpetual -> Binance USD-M perpetual: BTCUSD -> BTCUSDT."""
    s = delta_symbol.upper()
    return s + "T" if s.endswith("USD") else s


def binance_candles(symbol, res="15m", limit=500):
    """Server-side copy of Binance futures candles (used if the browser can't reach Binance directly)."""
    def get():
        r = requests.get(BINANCE_FAPI, params={"symbol": binance_symbol(symbol), "interval": res, "limit": limit},
                         timeout=10, headers={"User-Agent": "propdesk"})
        rows = r.json()
        if not isinstance(rows, list):
            raise DeltaError(f"Binance: {rows}")
        return [{"time": int(k[0] // 1000), "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                 "close": float(k[4]), "volume": float(k[5])} for k in rows]
    try:
        return _cached(("bc", symbol, res), 4, get)
    except (requests.RequestException, ValueError) as e:
        raise DeltaError(f"Binance: {e}")
