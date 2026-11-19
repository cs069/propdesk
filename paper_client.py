"""Paper-trading client with the same interface as DeltaClient.

Uses real Delta India prices when reachable, otherwise a random walk,
so you can test the whole dashboard and rules without risking money.
State is kept in paper_state.json next to the database.
"""
import json
import os
import random
import threading
import time

import config
from delta_client import DeltaClient, DeltaError

FEE = 0.0005  # 0.05% taker
CONTRACT_VALUES = {"BTCUSD": 0.001, "ETHUSD": 0.01, "SOLUSD": 1.0, "XRPUSD": 1.0}
_FALLBACK = {"BTCUSD": 90000.0, "ETHUSD": 3000.0, "SOLUSD": 150.0, "XRPUSD": 0.6}
_lock = threading.RLock()
_prices = {}          # symbol -> (price, ts)
_public = DeltaClient("", "", config.BASE_URLS["live"])
_STATE_FILE = os.path.join(os.path.dirname(config.DB_PATH) or ".", "paper_state.json")
_state = None


def _load():
    global _state
    if _state is None:
        try:
            with open(_STATE_FILE) as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
    return _state


def _save():
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_state, f)
    os.replace(tmp, _STATE_FILE)


def price(symbol):
    with _lock:
        p, ts = _prices.get(symbol, (None, 0))
        if p is not None and time.time() - ts < 2:
            return p
        try:
            p = _public.mark_price(symbol)
        except Exception:
            base = p or _FALLBACK.get(symbol, 100.0)
            p = round(base * (1 + random.gauss(0, 0.0015)), 2)
        _prices[symbol] = (p, time.time())
        return p


class PaperClient:
    def __init__(self, account_id, starting_balance):
        self.id = str(account_id)
        with _lock:
            st = _load()
            if self.id not in st:
                st[self.id] = {"cash": float(starting_balance), "positions": {}, "orders": []}
                _save()

    @property
    def acct(self):
        return _load()[self.id]

    def product(self, symbol):
        return {"id": symbol, "symbol": symbol, "contract_value": CONTRACT_VALUES.get(symbol, 1.0)}

    def mark_price(self, symbol):
        return price(symbol)

    # ----------------------------------------------------------- internals
    def _fill(self, symbol, side, size, px):
        a = self.acct
        cv = self.product(symbol)["contract_value"]
        signed = size if side == "buy" else -size
        fee = abs(size) * cv * px * FEE
        a["cash"] -= fee
        cash_before = a["cash"]
        a.setdefault("fills", []).insert(0, {"id": int(time.time() * 1e6), "symbol": symbol, "side": side, "size": size,
                                             "price": px, "fee": round(fee, 6), "role": "taker",
                                             "created_at": time.time(), "realized_pnl": 0})
        del a["fills"][200:]
        self._finish_fill(a, symbol, signed, px, cv)
        a["fills"][0]["realized_pnl"] = round(a["cash"] - cash_before, 6)

    def _finish_fill(self, a, symbol, signed, px, cv):
        pos = a["positions"].get(symbol)
        if not pos:
            a["positions"][symbol] = {"size": signed, "entry": px, "sl": None, "tp": None}
            return
        old = pos["size"]
        new = old + signed
        if old * signed > 0:  # adding
            pos["entry"] = (pos["entry"] * abs(old) + px * abs(signed)) / abs(new)
            pos["size"] = new
            return
        closed = min(abs(old), abs(signed))
        a["cash"] += closed * cv * (px - pos["entry"]) * (1 if old > 0 else -1)
        if new == 0:
            del a["positions"][symbol]
        elif old * new < 0:  # flipped
            a["positions"][symbol] = {"size": new, "entry": px, "sl": None, "tp": None}
        else:
            pos["size"] = new

    def _tick(self):
        a = self.acct
        for sym, pos in list(a["positions"].items()):
            px = price(sym)
            long = pos["size"] > 0
            hit_sl = pos["sl"] and ((long and px <= pos["sl"]) or (not long and px >= pos["sl"]))
            hit_tp = pos["tp"] and ((long and px >= pos["tp"]) or (not long and px <= pos["tp"]))
            if hit_sl or hit_tp:
                self._fill(sym, "sell" if long else "buy", abs(pos["size"]), px)
        for o in list(a["orders"]):
            px = price(o["symbol"])
            if (o["side"] == "buy" and px <= o["limit"]) or (o["side"] == "sell" and px >= o["limit"]):
                a["orders"].remove(o)
                self._fill(o["symbol"], o["side"], o["size"], o["limit"])
                p = a["positions"].get(o["symbol"])
                if p:
                    p["sl"], p["tp"] = o["sl"], o["tp"]

    # ------------------------------------------------------------ interface
    def positions(self):
        with _lock:
            self._tick()
            _save()
            out = []
            for sym, p in self.acct["positions"].items():
                px = price(sym)
                cv = self.product(sym)["contract_value"]
                out.append({"symbol": sym, "product_id": sym, "size": p["size"],
                            "entry_price": round(p["entry"], 2), "mark_price": px,
                            "unrealized_pnl": round(p["size"] * cv * (px - p["entry"]), 4),
                            "liquidation_price": None, "stop_loss": p["sl"], "take_profit": p["tp"]})
            return out

    def wallet_balance(self):
        return self.acct["cash"]

    def account_state(self):
        pos = self.positions()
        return self.acct["cash"] + sum(p["unrealized_pnl"] for p in pos), pos

    def set_leverage(self, symbol, leverage):
        return {"leverage": leverage}

    def place_order(self, symbol, side, size, order_type="market_order", limit_price=None,
                    stop_loss=None, take_profit=None, reduce_only=False):
        with _lock:
            a = self.acct
            if reduce_only:
                pos = a["positions"].get(symbol)
                if not pos:
                    raise DeltaError("reduce_only: no position")
                size = min(size, abs(pos["size"]))
            if order_type == "limit_order":
                a["orders"].append({"id": int(time.time() * 1e6), "product_id": symbol, "symbol": symbol,
                                    "side": side, "size": size, "limit": float(limit_price), "stop": None,
                                    "type": "limit_order", "sl": stop_loss, "tp": take_profit,
                                    "created_at": time.time()})
            else:
                self._fill(symbol, side, size, price(symbol))
                p = a["positions"].get(symbol)
                if p and not reduce_only:
                    p["sl"], p["tp"] = stop_loss, take_profit
            _save()
            return f"paper-{int(time.time() * 1000)}"

    def cancel_all(self):
        with _lock:
            self.acct["orders"] = []
            _save()

    def close_position(self, symbol):
        with _lock:
            pos = self.acct["positions"].get(symbol)
            if not pos:
                return None
            return self.place_order(symbol, "sell" if pos["size"] > 0 else "buy",
                                    abs(pos["size"]), reduce_only=True)

    def flatten(self):
        with _lock:
            self.cancel_all()
            for sym in list(self.acct["positions"]):
                self.close_position(sym)
        return []

    def open_orders(self):
        out = list(self.acct["orders"])
        for sym, pos in self.acct["positions"].items():  # show brackets like Delta's stop orders
            close = "sell" if pos["size"] > 0 else "buy"
            if pos.get("sl"):
                out.append({"id": f"sl-{sym}", "product_id": sym, "symbol": sym, "side": close, "size": abs(pos["size"]),
                            "limit": None, "stop": pos["sl"], "type": "stop_loss_order", "reduce_only": True})
            if pos.get("tp"):
                out.append({"id": f"tp-{sym}", "product_id": sym, "symbol": sym, "side": close, "size": abs(pos["size"]),
                            "limit": None, "stop": pos["tp"], "type": "take_profit_order", "reduce_only": True})
        return out

    def cancel_order(self, order_id, product_id):
        with _lock:
            a = self.acct
            oid = str(order_id)
            if oid.startswith(("sl-", "tp-")):
                pos = a["positions"].get(oid[3:])
                if pos:
                    pos["sl" if oid.startswith("sl-") else "tp"] = None
            else:
                a["orders"] = [o for o in a["orders"] if str(o.get("id")) != oid]
            _save()

    def fills(self, limit=50):
        return list(self.acct.get("fills", []))[:limit]

    # --- paper-only helpers used by the admin lifecycle (stand-ins for Delta transfers)
    def reset_balance(self, amount):
        """Like moving funds so the sub-account holds exactly `amount` and is flat."""
        with _lock:
            _load()[self.id] = {"cash": float(amount), "positions": {}, "orders": []}
            _save()

    def withdraw(self, amount):
        """Like transferring profit out of the sub-account to your main account."""
        with _lock:
            self.acct["cash"] -= float(amount)
            _save()
