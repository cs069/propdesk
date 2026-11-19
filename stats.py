"""Turns stored fills into closed round-trip trades and trader statistics."""
import time
from datetime import datetime, timezone

import config
import db

SESSIONS = [("Asia", 0, 8), ("London", 7, 16), ("New York", 13, 21)]   # UTC hours, overlaps allowed


def parse_ts(x):
    """Delta timestamps come as microseconds, milliseconds, seconds or ISO strings."""
    if x is None:
        return time.time()
    if isinstance(x, (int, float)):
        return x / 1e6 if x > 1e14 else x / 1e3 if x > 1e11 else float(x)
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return parse_ts(float(x))
        except ValueError:
            return time.time()


def store_fills(acc, fills):
    n = 0
    for f in fills or []:
        fid = str(f.get("id") or f"{f.get('created_at')}-{f.get('price')}-{f.get('size')}")
        try:
            db.x("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?)",
                 (acc["id"], fid, parse_ts(f.get("created_at")), f.get("symbol"), f.get("side"),
                  float(f.get("size") or 0), float(f.get("price") or 0), float(f.get("fee") or 0)))
            n += 1
        except (TypeError, ValueError):
            continue
    return n


def closed_trades(acc, contract_values, since=None):
    since = (acc.get("phase_started_at") or 0) if since is None else since
    rows = db.q("SELECT * FROM fills WHERE account_id=? AND ts>=? ORDER BY ts, fill_id", (acc["id"], since))
    books, out = {}, []
    for f in rows:
        sym, cv = f["symbol"], contract_values.get(f["symbol"], 1.0)
        b = books.setdefault(sym, {"pos": 0.0, "avg": 0.0, "t": None})
        qty, px = f["size"], f["price"]
        signed = qty if f["side"] == "buy" else -qty
        while qty > 1e-12:
            if b["pos"] == 0:
                b["t"] = {"symbol": sym, "side": "long" if signed > 0 else "short", "open_ts": f["ts"], "open_qty": 0.0,
                          "open_val": 0.0, "close_qty": 0.0, "close_val": 0.0, "fees": 0.0, "gross": 0.0, "max": 0.0}
            t = b["t"]
            if b["pos"] == 0 or (b["pos"] > 0) == (signed > 0):          # opening / adding
                b["avg"] = (b["avg"] * abs(b["pos"]) + px * qty) / (abs(b["pos"]) + qty)
                b["pos"] += qty if signed > 0 else -qty
                t["open_qty"] += qty
                t["open_val"] += px * qty
                t["fees"] += f["fee"] * (qty / f["size"] if f["size"] else 1)
                t["max"] = max(t["max"], abs(b["pos"]))
                qty = 0
            else:                                                       # reducing / closing
                c = min(abs(b["pos"]), qty)
                share = c / f["size"] if f["size"] else 1
                t["gross"] += c * cv * (px - b["avg"]) * (1 if b["pos"] > 0 else -1)
                t["close_qty"] += c
                t["close_val"] += px * c
                t["fees"] += f["fee"] * share
                b["pos"] += c if b["pos"] < 0 else -c
                qty -= c
                if abs(b["pos"]) < 1e-12:
                    b["pos"], b["avg"] = 0.0, 0.0
                    out.append({
                        "symbol": sym, "side": t["side"], "size": t["max"],
                        "open_price": t["open_val"] / t["open_qty"], "close_price": t["close_val"] / t["close_qty"],
                        "fees": t["fees"], "gross": t["gross"], "net": t["gross"] - t["fees"],
                        "open_ts": t["open_ts"], "close_ts": f["ts"],
                    })
                    b["t"] = None
    return sorted(out, key=lambda t: t["close_ts"], reverse=True)


def summary(acc, trades, metrics):
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gw, gl = sum(t["net"] for t in wins), -sum(t["net"] for t in losses)
    longs = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]
    n = len(trades)

    sess = []
    for name, a, b in SESSIONS:
        ts = [t for t in trades if a <= datetime.fromtimestamp(t["open_ts"], timezone.utc).hour < b]
        w = len([t for t in ts if t["net"] > 0])
        sess.append({"name": name, "start": a, "end": b, "trades": len(ts), "wins": w,
                     "rate": w / len(ts) * 100 if ts else 0, "pnl": sum(t["net"] for t in ts)})

    since = acc.get("phase_started_at") or 0
    entries = db.q("SELECT stop_loss, take_profit FROM orders WHERE account_id=? AND kind='entry' AND status='sent' "
                   "AND ts>=?", (acc["id"], since))
    sl_use = (len([e for e in entries if e["stop_loss"]]) / len(entries) * 100) if entries else 0
    viol = db.q("SELECT COUNT(*) n FROM violations WHERE account_id=? AND ts>=?", (acc["id"], since), one=True)["n"]
    rej = db.q("SELECT COUNT(*) n FROM orders WHERE account_id=? AND status='rejected' AND ts>=?",
               (acc["id"], since), one=True)["n"]
    days = db.q("SELECT pnl FROM daily_stats WHERE account_id=? AND day>=?",
                (acc["id"], datetime.fromtimestamp(since, config.TZ).strftime("%Y-%m-%d")))
    green = len([d for d in days if (d["pnl"] or 0) > 0])
    avg_w = gw / len(wins) if wins else 0
    avg_l = gl / len(losses) if losses else 0
    rr = (avg_w / avg_l) if avg_l else (2.0 if avg_w else 0)
    hold = sum(t["close_ts"] - t["open_ts"] for t in trades) / n if n else 0

    radar = {  # 0-100 scores
        "Win rate": len(wins) / n * 100 if n else 0,
        "Risk:reward": min(100, rr * 50),
        "Stop-loss use": sl_use,
        "Consistency": green / len(days) * 100 if days else 0,
        "Discipline": max(0, 100 - viol * 25 - rej * 3) if (n or entries) else 0,
    }
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / n * 100 if n else 0,
        "net": sum(t["net"] for t in trades), "fees": sum(t["fees"] for t in trades),
        "avg_trade": sum(t["net"] for t in trades) / n if n else 0,
        "avg_win": avg_w, "avg_loss": avg_l, "rr": rr,
        "profit_factor": (gw / gl) if gl else (None if not gw else float("inf")),
        "best": max((t["net"] for t in trades), default=0), "worst": min((t["net"] for t in trades), default=0),
        "hold_sec": hold,
        "long": {"n": len(longs), "pnl": sum(t["net"] for t in longs),
                 "pct": len(longs) / n * 100 if n else 0, "wins": len([t for t in longs if t["net"] > 0])},
        "short": {"n": len(shorts), "pnl": sum(t["net"] for t in shorts),
                  "pct": len(shorts) / n * 100 if n else 0, "wins": len([t for t in shorts if t["net"] > 0])},
        "sessions": sess, "sl_use": sl_use, "violations": viol, "rejected": rej, "radar": radar,
        "share": metrics.get("trader_share_usd") or 0,
    }
