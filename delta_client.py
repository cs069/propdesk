"""Minimal Delta Exchange India REST client (Read + Trading permissions only)."""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import requests

import config


class DeltaError(Exception):
    pass


_product_cache = {}


class DeltaClient:
    def __init__(self, api_key, api_secret, base_url=None):
        self.key = api_key
        self.secret = (api_secret or "").encode()
        self.base = (base_url or config.BASE_URL).rstrip("/")
        self.s = requests.Session()

    # ---------------------------------------------------------------- core
    def _req(self, method, path, query=None, body=None, auth=True):
        qs = ("?" + urlencode(query)) if query else ""
        body_s = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"User-Agent": "propdesk-python", "Content-Type": "application/json"}
        if auth:
            ts = str(int(time.time()))
            prehash = method + ts + path + qs + body_s
            sig = hmac.new(self.secret, prehash.encode(), hashlib.sha256).hexdigest()
            headers.update({"api-key": self.key, "timestamp": ts, "signature": sig})
        try:
            r = self.s.request(method, self.base + path + qs, data=body_s or None,
                               headers=headers, timeout=10)
            j = r.json()
        except Exception as e:  # network / bad JSON
            raise DeltaError(f"{method} {path}: {e}")
        if not j.get("success", False):
            raise DeltaError(f"{method} {path}: {j.get('error') or j}")
        return j.get("result")

    # -------------------------------------------------------------- public
    def product(self, symbol):
        if symbol not in _product_cache:
            p = self._req("GET", f"/v2/products/{symbol}", auth=False)
            _product_cache[symbol] = {"id": p["id"], "symbol": p["symbol"],
                                      "contract_value": float(p["contract_value"])}
        return _product_cache[symbol]

    def mark_price(self, symbol):
        t = self._req("GET", f"/v2/tickers/{symbol}", auth=False)
        return float(t.get("mark_price") or t.get("close"))

    # ------------------------------------------------------------- private
    def positions(self):
        """List of dicts: symbol, size (+long/-short), entry_price, unrealized_pnl."""
        out = []
        for p in self._req("GET", "/v2/positions/margined") or []:
            size = int(float(p.get("size") or 0))
            if size == 0:
                continue
            out.append({
                "symbol": p.get("product_symbol"),
                "product_id": p.get("product_id"),
                "size": size,
                "entry_price": float(p.get("entry_price") or 0),
                "mark_price": float(p.get("mark_price") or 0),
                "unrealized_pnl": float(p.get("unrealized_pnl") or 0),
                "liquidation_price": p.get("liquidation_price"),
            })
        return out

    def wallet_balance(self):
        for b in self._req("GET", "/v2/wallet/balances") or []:
            if b.get("asset_symbol") == config.SETTLE_ASSET:
                return float(b.get("balance") or 0)
        raise DeltaError(f"No {config.SETTLE_ASSET} wallet found on this account")

    def account_state(self):
        """(equity, positions). Equity = wallet balance + unrealized PnL."""
        bal = self.wallet_balance()
        pos = self.positions()
        return bal + sum(p["unrealized_pnl"] for p in pos), pos

    def set_leverage(self, symbol, leverage):
        pid = self.product(symbol)["id"]
        return self._req("POST", f"/v2/products/{pid}/orders/leverage",
                         body={"leverage": str(int(leverage))})

    def place_order(self, symbol, side, size, order_type="market_order", limit_price=None,
                    stop_loss=None, take_profit=None, reduce_only=False):
        body = {"product_symbol": symbol, "side": side, "size": int(size),
                "order_type": order_type}
        if order_type == "limit_order":
            body["limit_price"] = str(limit_price)
            body["time_in_force"] = "gtc"
        if stop_loss:
            body["bracket_stop_loss_price"] = str(stop_loss)
            body["bracket_stop_trigger_method"] = "mark_price"
        if take_profit:
            body["bracket_take_profit_price"] = str(take_profit)
        if reduce_only:
            body["reduce_only"] = True
        r = self._req("POST", "/v2/orders", body=body)
        return str(r.get("id")) if isinstance(r, dict) else ""

    def open_orders(self):
        out = []
        for o in self._req("GET", "/v2/orders", query={"states": "open,pending"}) or []:
            out.append({"id": o.get("id"), "product_id": o.get("product_id"),
                        "symbol": o.get("product_symbol"), "side": o.get("side"),
                        "size": o.get("unfilled_size") or o.get("size"),
                        "limit": o.get("limit_price"), "stop": o.get("stop_price"),
                        "type": o.get("stop_order_type") or o.get("order_type"),
                        "reduce_only": o.get("reduce_only"), "created_at": o.get("created_at")})
        return out

    def cancel_order(self, order_id, product_id):
        return self._req("DELETE", "/v2/orders", body={"id": int(order_id), "product_id": int(product_id)})

    def fills(self, limit=50):
        out = []
        for f in self._req("GET", "/v2/fills", query={"page_size": limit}) or []:
            meta = (f.get("meta_data") or {}).get("new_position") or {}
            out.append({"id": f.get("id"), "symbol": f.get("product_symbol") or (f.get("product") or {}).get("symbol"),
                        "side": f.get("side"), "size": f.get("size"), "price": f.get("price"),
                        "fee": f.get("commission"), "role": f.get("role"), "realized_pnl": meta.get("realized_pnl"),
                        "created_at": f.get("created_at")})
        return out

    def cancel_all(self):
        return self._req("DELETE", "/v2/orders/all")

    def close_position(self, symbol):
        for p in self.positions():
            if p["symbol"] == symbol:
                side = "sell" if p["size"] > 0 else "buy"
                return self.place_order(symbol, side, abs(p["size"]), reduce_only=True)
        return None

    def flatten(self):
        """Cancel every order and close every position with reduce-only market orders."""
        errors = []
        try:
            self.cancel_all()
        except DeltaError as e:
            errors.append(str(e))
        for p in self.positions():
            try:
                side = "sell" if p["size"] > 0 else "buy"
                self.place_order(p["symbol"], side, abs(p["size"]), reduce_only=True)
            except DeltaError as e:
                errors.append(str(e))
        return errors
