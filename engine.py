"""PropDesk engine: account lifecycle, rule checks, risk watcher and payouts.

Lifecycle of one account
  pending_payment -> payment_review -> awaiting_setup -> active (phase1)
  -> passed -> active (phase2) -> passed -> active (funded)
  -> payout_pending -> active (funded) -> ...            any breach -> failed
"""
import threading
import time
import traceback
from datetime import datetime

import config
import db
from delta_client import DeltaClient, DeltaError
from paper_client import PaperClient

PHASE_LABEL = {"phase1": "Phase 1", "phase2": "Phase 2", "funded": "Funded"}
STATUS_LABEL = {
    "pending_payment": "Awaiting payment", "payment_review": "Payment under review",
    "awaiting_setup": "Being set up", "active": "Active", "paused_day": "Paused for today",
    "passed": "Passed", "payout_pending": "Payout in progress", "failed": "Failed", "closed": "Closed",
}
WATCHED = ("active", "paused_day", "passed", "payout_pending")
_clients = {}
_lock = threading.RLock()


class LifecycleError(Exception):
    pass


# ---------------------------------------------------------------- time
def now_ist():
    return datetime.now(config.TZ)


def today():
    return now_ist().strftime("%Y-%m-%d")


def day_start_ts(day):
    return datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=config.TZ).timestamp()


# ---------------------------------------------------------------- helpers
def client_for(acc):
    k = (acc["id"], acc["api_key"], acc["api_secret"], config.MODE)
    if k not in _clients:
        if config.MODE == "paper":
            _clients[k] = PaperClient(f"a{acc['id']}", acc["starting_balance"] or db.plan_of(acc)["account_size"])
        else:
            if not acc["api_key"] or not acc["api_secret"]:
                raise DeltaError("No API key on this account yet")
            _clients[k] = DeltaClient(acc["api_key"], acc["api_secret"])
    return _clients[k]


def next_phase(acc):
    phases = db.plan_of(acc)["phases"]
    if acc["phase"] == "phase1":
        return "phase2" if phases >= 2 else "funded"
    if acc["phase"] == "phase2":
        return "funded"
    return None


def first_phase(plan):
    return "funded" if plan["phases"] == 0 else "phase1"


def target_pct(acc):
    p = db.plan_of(acc)
    return {"phase1": p["p1_target_pct"], "phase2": p["p2_target_pct"]}.get(acc["phase"], 0) or 0


def period_start(acc):
    """Start of the current evaluation / payout period."""
    if acc["phase"] == "funded":
        return acc["last_payout_at"] or acc["funded_at"] or acc["phase_started_at"] or 0
    return acc["phase_started_at"] or 0


def trading_days(acc, since=None):
    since = period_start(acc) if since is None else since
    return db.q("SELECT COUNT(DISTINCT day) n FROM orders WHERE account_id=? AND kind='entry' AND status='sent' "
                "AND ts>=?", (acc["id"], since), one=True)["n"]


def within_hours(rules):
    h = (rules.get("trading_hours") or "").strip()
    if not h:
        return True
    try:
        start, end = [s.strip() for s in h.split("-")]
        cur = now_ist().strftime("%H:%M")
        return start <= cur < end if start < end else (cur >= start or cur < end)
    except ValueError:
        return True


def metrics(acc, equity=None):
    plan, r = db.plan_of(acc), db.rules_of(acc)
    sb = acc["starting_balance"] or plan["account_size"]
    eq = acc["last_equity"] if equity is None else equity
    eq = sb if eq is None else eq
    day_start = acc["day_start_equity"] if acc["day_start_equity"] is not None else sb
    peak = max(acc["peak_equity"] or sb, eq)
    dd_ref = peak if r["drawdown_type"] == "trailing" else sb
    dd_limit = sb * r["max_drawdown_pct"] / 100
    daily_limit = sb * r["daily_loss_pct"] / 100
    tp = target_pct(acc)
    tdays = trading_days(acc)
    elapsed = (time.time() - acc["phase_started_at"]) / 86400 if acc["phase_started_at"] else 0
    out = {
        "equity": eq, "starting_balance": sb, "peak_equity": peak,
        "total_pnl": eq - sb, "total_pnl_pct": (eq - sb) / sb * 100,
        "daily_pnl": eq - day_start, "daily_pnl_pct": (eq - day_start) / sb * 100,
        "daily_loss_limit": daily_limit, "daily_loss_left": daily_limit + min(eq - day_start, 0),
        "dd_floor": dd_ref - dd_limit, "dd_left": eq - (dd_ref - dd_limit),
        "drawdown_pct": max(0.0, (dd_ref - eq) / sb * 100),
        "target_pct": tp, "target_equity": sb * (1 + tp / 100) if tp else None,
        "target_progress_pct": ((eq - sb) / (sb * tp / 100) * 100) if tp else None,
        "trading_days": tdays, "min_trading_days": plan["min_trading_days"],
        "days_elapsed": elapsed,
        "days_left": (plan["max_days"] - elapsed) if plan["max_days"] and acc["phase"] != "funded" else None,
    }
    goal = plan.get("goal_pct") or 0
    out["goal_pct"] = goal
    out["goal_equity"] = sb * (1 + goal / 100) if goal else None
    out["goal_progress_pct"] = ((eq - sb) / (sb * goal / 100) * 100) if goal else None
    out["usd_inr"] = config.USD_INR
    if acc["phase"] == "funded":
        out["profit_split_pct"] = plan["profit_split_pct"]
        out["trader_share_usd"] = max(0.0, eq - sb) * plan["profit_split_pct"] / 100
        out["next_payout_at"] = period_start(acc) + plan["payout_interval_days"] * 86400
    return out


def log_order(acc, row_status, message, **o):
    db.x("INSERT INTO orders(account_id, ts, day, phase, symbol, side, size, order_type, limit_price, stop_loss, "
         "take_profit, leverage, kind, status, exchange_order_id, message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (acc["id"], time.time(), today(), acc["phase"], o.get("symbol"), o.get("side"), o.get("size"),
          o.get("order_type"), o.get("limit_price"), o.get("stop_loss"), o.get("take_profit"),
          o.get("leverage"), o.get("kind", "entry"), row_status, o.get("exchange_order_id"), message))


def set_status(acc, status, reason=""):
    db.x("UPDATE accounts SET status=?, status_reason=? WHERE id=?", (status, reason, acc["id"]))


# ------------------------------------------------------------- pre-trade
def check_order(acc, symbol, side, size, leverage, order_type, limit_price, stop_loss, take_profit):
    r = db.rules_of(acc)
    if acc["status"] != "active":
        return False, f"Account is {STATUS_LABEL.get(acc['status'], acc['status'])}. {acc['status_reason']}"
    if symbol not in r["allowed_symbols"]:
        return False, f"{symbol} is not allowed. Allowed: {', '.join(r['allowed_symbols'])}"
    if side not in ("buy", "sell"):
        return False, "Side must be buy or sell"
    if not within_hours(r):
        return False, f"Outside trading hours ({r['trading_hours']} IST)"
    if size <= 0 or size > r["max_contracts_per_order"]:
        return False, f"Size must be 1–{r['max_contracts_per_order']} contracts"
    if leverage < 1 or leverage > r["max_leverage"]:
        return False, f"Leverage must be 1–{r['max_leverage']}x"
    n = db.q("SELECT COUNT(*) n FROM orders WHERE account_id=? AND day=? AND kind='entry' AND status='sent'",
             (acc["id"], today()), one=True)["n"]
    if n >= r["max_trades_per_day"]:
        return False, f"Max {r['max_trades_per_day']} trades per day reached"
    if order_type == "limit_order" and not limit_price:
        return False, "Limit price required for limit orders"
    c = client_for(acc)
    try:
        positions = c.positions()
        mark = c.mark_price(symbol)
        cv = c.product(symbol)["contract_value"]
    except DeltaError as e:
        return False, f"Exchange error: {e}"
    open_syms = {p["symbol"] for p in positions}
    if symbol not in open_syms and len(open_syms) >= r["max_open_positions"]:
        return False, f"Max {r['max_open_positions']} open positions"
    entry = float(limit_price) if order_type == "limit_order" else mark
    if r["require_stop_loss"] and not stop_loss:
        return False, "Stop-loss is required on every trade"
    if stop_loss and ((side == "buy" and stop_loss >= entry) or (side == "sell" and stop_loss <= entry)):
        return False, f"For a {side}, stop-loss must be {'below' if side == 'buy' else 'above'} entry price"
    if take_profit and ((side == "buy" and take_profit <= entry) or (side == "sell" and take_profit >= entry)):
        return False, f"For a {side}, take-profit must be {'above' if side == 'buy' else 'below'} entry price"
    if stop_loss:
        m = metrics(acc)
        risk = size * cv * abs(entry - stop_loss)
        max_risk = m["starting_balance"] * r["max_risk_per_trade_pct"] / 100
        if risk > max_risk:
            return False, f"Risk at stop-loss is {risk:.2f}; max allowed is {max_risk:.2f}. Reduce size or tighten stop."
        if risk > m["daily_loss_left"]:
            return False, f"Risk {risk:.2f} is more than your remaining daily loss room {m['daily_loss_left']:.2f}"
        if risk > m["dd_left"]:
            return False, f"Risk {risk:.2f} is more than your remaining drawdown room {m['dd_left']:.2f}"
    return True, "ok"


def place_order(acc, **o):
    ok, msg = check_order(acc, o["symbol"], o["side"], o["size"], o["leverage"], o["order_type"],
                          o.get("limit_price"), o.get("stop_loss"), o.get("take_profit"))
    if not ok:
        log_order(acc, "rejected", msg, **o)
        return False, msg
    try:
        c = client_for(acc)
        c.set_leverage(o["symbol"], o["leverage"])
        oid = c.place_order(o["symbol"], o["side"], o["size"], o["order_type"], o.get("limit_price"),
                            o.get("stop_loss"), o.get("take_profit"))
        log_order(acc, "sent", "accepted", exchange_order_id=oid, **o)
        return True, f"Order sent: {o['side']} {o['size']} {o['symbol']}"
    except DeltaError as e:
        log_order(acc, "error", str(e), **o)
        return False, f"Exchange error: {e}"


# -------------------------------------------------------------- lifecycle
def _balance_ok(c, expected, force):
    eq, pos = c.account_state()
    if pos:
        raise LifecycleError("Sub-account still has open positions. Close them first.")
    if not force and abs(eq - expected) > expected * config.BALANCE_TOLERANCE:
        raise LifecycleError(f"Sub-account equity is {eq:.2f} {config.SETTLE_ASSET} but should be {expected:.2f}. "
                             f"Move funds on Delta first (or tick 'force').")
    return eq


def start_phase(acc, phase, force=False):
    """Activate the account in `phase` with a fresh balance of plan size."""
    with _lock:
        size = db.plan_of(acc)["account_size"]
        c = client_for(acc)
        if config.MODE == "paper":
            c.reset_balance(size)
        try:
            eq = _balance_ok(c, size, force)
        except DeltaError as e:
            raise LifecycleError(f"Could not read the sub-account: {e}")
        now = time.time()
        db.x("UPDATE accounts SET phase=?, status='active', status_reason='', starting_balance=?, phase_started_at=?, "
             "day_date=?, day_start_equity=?, peak_equity=?, last_equity=?, last_error='', "
             "funded_at=CASE WHEN ?='funded' THEN ? ELSE funded_at END WHERE id=?",
             (phase, size, now, today(), eq, eq, eq, phase, now, acc["id"]))
        db.event(acc["id"], "phase_started", f"{PHASE_LABEL[phase]} started with {eq:.2f} {config.SETTLE_ASSET}")


def fail(acc, rule, detail):
    c = client_for(acc)
    errs = c.flatten()
    set_status(acc, "failed", f"{rule}: {detail}")
    log_order(acc, "sent" if not errs else "error", "; ".join(errs) or f"Auto-close: {rule}", kind="flatten")
    db.violation(acc["id"], rule, detail, "positions closed, account failed")
    db.event(acc["id"], "failed", f"{rule}: {detail}")


def pause_day(acc, rule, detail):
    client_for(acc).flatten()
    set_status(acc, "paused_day", f"{rule}: {detail}")
    log_order(acc, "sent", f"Auto-close: {rule}", kind="flatten")
    db.violation(acc["id"], rule, detail, "positions closed, paused till midnight IST")
    db.event(acc["id"], "paused", f"{rule}: {detail}")


def pass_phase(acc, m):
    client_for(acc).flatten()
    nxt = next_phase(acc)
    set_status(acc, "passed", f"{PHASE_LABEL[acc['phase']]} passed. Next: {PHASE_LABEL[nxt]} (being set up)")
    db.event(acc["id"], "passed", f"{PHASE_LABEL[acc['phase']]} passed with equity {m['equity']:.2f} "
                                  f"({m['total_pnl_pct']:+.2f}%) in {m['trading_days']} trading days")


def roll_day(acc, equity):
    d = acc["day_date"]
    if d:
        start = acc["day_start_equity"] if acc["day_start_equity"] is not None else equity
        last = acc["last_equity"] if acc["last_equity"] is not None else equity
        t0 = day_start_ts(d)
        low = db.q("SELECT MIN(equity) m FROM equity_snaps WHERE account_id=? AND ts>=? AND ts<?",
                   (acc["id"], t0, t0 + 86400), one=True)["m"]
        trades = db.q("SELECT COUNT(*) n FROM orders WHERE account_id=? AND day=? AND kind='entry' AND status='sent'",
                      (acc["id"], d), one=True)["n"]
        viol = db.q("SELECT COUNT(*) n FROM violations WHERE account_id=? AND ts>=? AND ts<?",
                    (acc["id"], t0, t0 + 86400), one=True)["n"]
        db.x("INSERT OR REPLACE INTO daily_stats VALUES (?,?,?,?,?,?,?,?,?)",
             (acc["id"], d, acc["phase"], start, last, last - start, low if low is not None else min(start, last),
              trades, viol))
    db.x("UPDATE accounts SET day_date=?, day_start_equity=? WHERE id=?", (today(), equity, acc["id"]))
    if acc["status"] == "paused_day":
        set_status(acc, "active")
        db.event(acc["id"], "resumed", "New trading day")


_last_snap = {}


def check_account(acc):
    c = client_for(acc)
    equity, positions = c.account_state()
    with _lock:
        acc = db.account(acc["id"])           # re-read: admin may have changed it meanwhile
        if acc["status"] not in WATCHED:
            return
        if acc["day_date"] != today():
            roll_day(acc, equity)
            acc = db.account(acc["id"])
        peak = max(acc["peak_equity"] or equity, equity)
        db.x("UPDATE accounts SET last_equity=?, peak_equity=?, last_check=?, last_error='' WHERE id=?",
             (equity, peak, time.time(), acc["id"]))
        acc.update(last_equity=equity, peak_equity=peak)
        if time.time() - _last_snap.get(acc["id"], 0) >= config.SNAPSHOT_INTERVAL_SEC:
            db.x("INSERT INTO equity_snaps VALUES (?,?,?)", (acc["id"], time.time(), equity))
            _last_snap[acc["id"]] = time.time()

        if acc["status"] != "active":        # paused / passed / payout: must stay flat
            if positions:
                c.flatten()
            return

        r, plan, m = db.rules_of(acc), db.plan_of(acc), metrics(acc, equity)
        if m["dd_left"] <= 0:
            return fail(acc, "max_drawdown", f"equity {equity:.2f} reached floor {m['dd_floor']:.2f} "
                                             f"({r['max_drawdown_pct']}% {r['drawdown_type']})")
        if -m["daily_pnl"] >= m["daily_loss_limit"]:
            detail = f"day P&L {m['daily_pnl']:.2f} (limit -{m['daily_loss_limit']:.2f})"
            return fail(acc, "daily_loss", detail) if r["daily_loss_action"] == "fail" \
                else pause_day(acc, "daily_loss", detail)
        if m["days_left"] is not None and m["days_left"] <= 0:
            return fail(acc, "time_limit", f"target not reached within {plan['max_days']} days")
        if m["target_equity"] and equity >= m["target_equity"] and m["trading_days"] >= plan["min_trading_days"]:
            return pass_phase(acc, m)
        if positions and r.get("flatten_outside_hours") and not within_hours(r):
            c.flatten()
            db.violation(acc["id"], "trading_hours", "positions open after hours", "positions closed")


# ----------------------------------------------------------------- payouts
def period_daily_pnls(acc):
    since_day = datetime.fromtimestamp(period_start(acc), config.TZ).strftime("%Y-%m-%d")
    rows = db.q("SELECT pnl FROM daily_stats WHERE account_id=? AND day>=? AND phase='funded'", (acc["id"], since_day))
    pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
    m = metrics(acc)
    pnls.append(m["daily_pnl"])
    return pnls


def payout_check(acc):
    """Returns (eligible, [reasons not met], info)."""
    plan, m = db.plan_of(acc), metrics(acc)
    profit = m["total_pnl"]
    info = {"profit": profit, "split": plan["profit_split_pct"],
            "share_usd": max(profit, 0) * plan["profit_split_pct"] / 100}
    info["share_inr"] = info["share_usd"] * config.USD_INR
    why = []
    if acc["phase"] != "funded":
        why.append("Payouts are only for funded accounts")
    if acc["status"] != "active":
        why.append(f"Account status is {STATUS_LABEL.get(acc['status'])}")
    wait = m.get("next_payout_at", 0) - time.time()
    if wait > 0:
        why.append(f"Next payout window opens in {wait / 86400:.1f} days")
    if m["trading_days"] < plan["min_trading_days"]:
        why.append(f"Need {plan['min_trading_days']} trading days this period (have {m['trading_days']})")
    if profit < max(plan["min_payout_usd"], 0.01):
        why.append(f"Profit {profit:.2f} is below the minimum payout {plan['min_payout_usd']:.2f}")
    if plan["consistency_pct"] and profit > 0:
        best = max(period_daily_pnls(acc) or [0])
        share = best / profit * 100
        info["best_day_share"] = share
        if share > plan["consistency_pct"]:
            why.append(f"Consistency: best day is {share:.0f}% of profit (max {plan['consistency_pct']:.0f}%). "
                       f"Keep trading to spread profits.")
    try:
        if client_for(acc).positions():
            why.append("Close all open positions first")
    except DeltaError as e:
        why.append(f"Exchange error: {e}")
    return (not why), why, info


def request_payout(acc):
    with _lock:
        acc = db.account(acc["id"])
        ok, why, info = payout_check(acc)
        if not ok:
            raise LifecycleError(" · ".join(why))
        client_for(acc).cancel_all()
        set_status(acc, "payout_pending", "Payout requested — trading paused until it is paid")
        pid = db.x("INSERT INTO payouts(account_id, user_id, requested_at, profit_usd, split_pct, trader_share_usd, "
                   "trader_share_inr, usd_inr, status) VALUES (?,?,?,?,?,?,?,?, 'requested')",
                   (acc["id"], acc["user_id"], time.time(), info["profit"], info["split"], info["share_usd"],
                    info["share_inr"], config.USD_INR))
        db.event(acc["id"], "payout_requested", f"Profit {info['profit']:.2f}, trader share {info['share_usd']:.2f} "
                                                f"{config.SETTLE_ASSET} (₹{info['share_inr']:,.0f})")
        return pid


def payout_reject(payout, note):
    acc = db.account(payout["account_id"])
    db.x("UPDATE payouts SET status='rejected', admin_note=? WHERE id=?", (note, payout["id"]))
    set_status(acc, "active")
    db.event(acc["id"], "payout_rejected", note)


def payout_approve(payout, note=""):
    db.x("UPDATE payouts SET status='approved', admin_note=? WHERE id=?", (note, payout["id"]))
    db.event(payout["account_id"], "payout_approved", note)


def payout_mark_paid(payout, utr, force=False):
    """Admin has (1) moved the profit out of the sub-account and (2) paid the trader by UPI."""
    with _lock:
        acc = db.account(payout["account_id"])
        c = client_for(acc)
        if config.MODE == "paper":
            c.withdraw(payout["profit_usd"])
        eq, pos = c.account_state()
        sb = acc["starting_balance"]
        if not force and eq > sb * (1 + config.BALANCE_TOLERANCE):
            raise LifecycleError(f"Sub-account still holds {eq:.2f} (start {sb:.2f}). Transfer the profit "
                                 f"{payout['profit_usd']:.2f} to your main account on Delta first, or tick 'force'.")
        now = time.time()
        db.x("UPDATE payouts SET status='paid', utr=?, paid_at=? WHERE id=?", (utr, now, payout["id"]))
        db.x("UPDATE accounts SET status='active', status_reason='', last_payout_at=?, day_start_equity=?, "
             "peak_equity=?, last_equity=? WHERE id=?", (now, eq, eq, eq, acc["id"]))
        db.event(acc["id"], "payout_paid", f"₹{payout['trader_share_inr']:,.0f} paid, UTR {utr}")


# ----------------------------------------------------------------- watcher
def watcher_loop(stop):
    while not stop.is_set():
        for a in db.q(f"SELECT * FROM accounts WHERE status IN ({','.join('?' * len(WATCHED))})", WATCHED):
            try:
                check_account(a)
            except Exception as e:
                db.x("UPDATE accounts SET last_error=? WHERE id=?", (str(e)[:300], a["id"]))
                if not isinstance(e, DeltaError):
                    traceback.print_exc()
        stop.wait(config.CHECK_INTERVAL_SEC)


_started = False


def start_watcher():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=watcher_loop, args=(threading.Event(),), daemon=True, name="risk-watcher").start()
