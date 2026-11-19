"""PropDesk — a small prop firm on top of Delta Exchange sub-accounts.

Run (dev):    python app.py
Run (server): gunicorn -w 1 --threads 8 -b 127.0.0.1:8000 app:app     # keep ONE worker
"""
import csv
import io
import json
import os
import re
import secrets
import time
from datetime import datetime
from functools import wraps
from urllib.parse import quote

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import engine
import mailer
from delta_client import DeltaError
from engine import LifecycleError

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# ================================================================ helpers
@app.template_filter("money")
def money(v):
    return "—" if v is None else f"{v:,.2f}"


@app.template_filter("inr")
def inr(v):
    return rupees(v)


def rupees(v, sign=False):
    """Indian digit grouping: 100000 -> ₹1,00,000"""
    if v is None:
        return "—"
    neg, n = v < 0, str(int(round(abs(v))))
    head, tail = n[:-3], n[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    body = (head + "," + tail) if head else tail
    return ("−" if neg else "+" if sign and v > 0 else "") + "₹" + body


@app.template_filter("rs")
def rs(v):
    return rupees(v)


@app.template_filter("rs_signed")
def rs_signed(v):
    return rupees(v, sign=True)


@app.template_filter("pct")
def pct(v):
    return "—" if v is None else f"{v:+.2f}%"


@app.template_filter("dt")
def dt(ts):
    return datetime.fromtimestamp(ts, config.TZ).strftime("%d %b %Y %H:%M") if ts else "—"


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals.update(
    csrf_token=csrf_token, MODE=config.MODE, ASSET=config.SETTLE_ASSET, FIRM=config.FIRM_NAME,
    PHASE=engine.PHASE_LABEL, STATUS=engine.STATUS_LABEL, USD_INR=config.USD_INR, plan_of=db.plan_of,
    rules_of=db.rules_of,
)


@app.before_request
def check_csrf():
    if request.method == "POST" and request.endpoint not in ("login", "register", "admin_login"):
        tok = request.form.get("csrf") or request.headers.get("X-CSRF")
        if not tok or tok != session.get("csrf"):
            abort(400, "Session expired — reload the page and try again")


def current_user():
    uid = session.get("uid")
    return db.q("SELECT * FROM users WHERE id=?", (uid,), one=True) if uid else None


def login_required(admin=False, approved=True):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            u = current_user()
            if not u or u["status"] == "disabled":
                session.clear()
                return redirect(url_for("login"))
            if admin and not u["is_admin"]:
                abort(403)
            if approved and not u["is_admin"] and u["status"] != "approved":
                return redirect(url_for("pending"))
            return f(u, *a, **kw)
        return wrapper
    return deco


def own_account(u, aid):
    a = db.account(aid)
    if not a or (a["user_id"] != u["id"] and not u["is_admin"]):
        abort(404)
    return a


def fnum(name, default=None):
    v = (request.form.get(name) or "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default


def back(default):
    return redirect(request.referrer or default)


# ================================================================ auth
@app.route("/")
def home():
    u = current_user()
    if not u:
        return redirect(url_for("login"))
    return redirect(url_for("admin_home" if u["is_admin"] else "dashboard"))


def _start_session(u):
    session.clear()
    session["uid"] = u["id"]
    db.x("UPDATE users SET last_login=? WHERE id=?", (time.time(), u["id"]))


@app.route("/login", methods=["GET", "POST"])
def login():
    """Traders log in with their email + a 6-digit code."""
    step = "email"
    email = session.get("otp_email", "")
    if request.method == "POST":
        act = request.form.get("action")
        if act == "send":
            email = (request.form.get("email") or "").strip().lower()
            u = db.q("SELECT * FROM users WHERE username=? AND is_admin=0", (email,), one=True)
            if not u or u["status"] != "approved":
                time.sleep(1)
                flash("This email doesn't have access. Ask the admin to add you.", "error")
            elif time.time() - (u["otp_sent_at"] or 0) < config.OTP_RESEND_SEC:
                flash("A code was just sent. Wait a few seconds before asking again.", "error")
                session["otp_email"], step = email, "code"
            else:
                code = f"{secrets.randbelow(10**6):06d}"
                db.x("UPDATE users SET otp_hash=?, otp_expires=?, otp_tries=0, otp_sent_at=? WHERE id=?",
                     (generate_password_hash(code), time.time() + config.OTP_TTL_SEC, time.time(), u["id"]))
                try:
                    mailer.send_code(email, u["full_name"], code)
                    session["otp_email"], step = email, "code"
                except Exception as e:  # SMTP problems
                    flash(f"Couldn't send the email: {e}", "error")
        elif act == "verify" and email:
            step = "code"
            code = re.sub(r"\D", "", request.form.get("code") or "")
            u = db.q("SELECT * FROM users WHERE username=? AND is_admin=0", (email,), one=True)
            if not u or not u["otp_hash"] or time.time() > (u["otp_expires"] or 0):
                flash("That code has expired. Send a new one.", "error")
                step = "email"
            elif u["otp_tries"] >= config.OTP_MAX_TRIES:
                flash("Too many wrong codes. Send a new one.", "error")
                step = "email"
            elif check_password_hash(u["otp_hash"], code):
                db.x("UPDATE users SET otp_hash='', otp_expires=0, otp_tries=0 WHERE id=?", (u["id"],))
                _start_session(u)
                return redirect(url_for("dashboard"))
            else:
                db.x("UPDATE users SET otp_tries=otp_tries+1 WHERE id=?", (u["id"],))
                time.sleep(1)
                flash("Wrong code. Check the latest email and try again.", "error")
        elif act == "change":
            session.pop("otp_email", None)
            email = ""
    elif email and request.args.get("step") == "code":
        step = "code"
    return render_template("login.html", user=None, step=step, email=email,
                           email_enabled=mailer.email_enabled())


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = db.q("SELECT * FROM users WHERE username=? AND is_admin=1",
                 (request.form.get("username", "").strip().lower(),), one=True)
        if u and check_password_hash(u["pw_hash"], request.form.get("password", "")):
            _start_session(u)
            return redirect(url_for("admin_home"))
        time.sleep(1)
        flash("Wrong username or password", "error")
    return render_template("admin_login.html", user=None)


@app.route("/register", methods=["GET", "POST"])
def register():
    if not config.REGISTRATION_OPEN:
        abort(404)
    f = request.form
    if request.method == "POST":
        username = (f.get("username") or "").strip().lower()
        errs = []
        if not re.fullmatch(r"[a-z0-9_.]{3,20}", username):
            errs.append("Username: 3–20 letters, numbers, _ or .")
        if db.q("SELECT 1 FROM users WHERE username=?", (username,), one=True):
            errs.append("That username is taken")
        if len(f.get("password", "")) < 8:
            errs.append("Password must be at least 8 characters")
        if f.get("password") != f.get("password2"):
            errs.append("Passwords don't match")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", f.get("email", "").strip()):
            errs.append("Enter a valid email")
        if not re.fullmatch(r"[0-9+ ]{10,15}", f.get("phone", "").strip()):
            errs.append("Enter a valid phone number")
        if not re.fullmatch(r"[\w.\-]{2,}@[\w]{2,}", f.get("upi_id", "").strip()):
            errs.append("Enter a valid UPI ID (e.g. name@okaxis) — payouts are sent here")
        if not f.get("agree"):
            errs.append("Please accept the rules")
        if errs:
            for e in errs:
                flash(e, "error")
            return render_template("register.html", user=None, f=f)
        status = "approved" if config.INVITE_CODE and f.get("invite", "").strip() == config.INVITE_CODE else "pending"
        uid = db.x("INSERT INTO users(username, full_name, email, phone, upi_id, pw_hash, status, created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (username, f.get("full_name", "").strip(), f["email"].strip(), f["phone"].strip(),
                    f["upi_id"].strip(), generate_password_hash(f["password"]), status, time.time()))
        session.clear()
        session["uid"] = uid
        return redirect(url_for("home"))
    return render_template("register.html", user=None, f={}, invite=bool(config.INVITE_CODE))


@app.route("/pending")
@login_required(approved=False)
def pending(u):
    if u["status"] == "approved" or u["is_admin"]:
        return redirect(url_for("home"))
    return render_template("pending.html", user=u)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required(approved=False)
def profile(u):
    if request.method == "POST":
        f = request.form
        if f.get("action") == "password":
            if not check_password_hash(u["pw_hash"], f.get("old", "")):
                flash("Current password is wrong", "error")
            elif len(f.get("new", "")) < 8:
                flash("New password must be at least 8 characters", "error")
            else:
                db.x("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(f["new"]), u["id"]))
                flash("Password changed", "ok")
        else:
            db.x("UPDATE users SET full_name=?, phone=?, upi_id=? WHERE id=?",
                 (f.get("full_name", "").strip(), f.get("phone", "").strip(), f.get("upi_id", "").strip(), u["id"]))
            flash("Profile saved", "ok")
        return redirect(url_for("profile"))
    return render_template("profile.html", user=u)


# ================================================================ trader
@app.route("/dashboard")
@login_required()
def dashboard(u):
    accs = db.q("SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC", (u["id"],))
    live = [a for a in accs if a["status"] not in ("pending_payment", "payment_review", "awaiting_setup")]
    if len(live) == 1 and len(accs) == 1:
        return redirect(url_for("trade", aid=live[0]["id"]))
    for a in accs:
        a["m"] = engine.metrics(a) if a["starting_balance"] else None
    pays = db.q("SELECT * FROM payouts WHERE user_id=? ORDER BY id DESC", (u["id"],))
    paid = sum(p["trader_share_inr"] for p in pays if p["status"] == "paid")
    return render_template("dashboard.html", user=u, accounts=accs, payouts=pays, paid_total=paid)


@app.route("/plans")
@login_required()
def plans(u):
    ps = db.q("SELECT * FROM plans WHERE active=1 ORDER BY account_size")
    for p in ps:
        p["rules"] = {**config.DEFAULT_RULES, **json.loads(p["rules_json"])}
    return render_template("plans.html", user=u, plans=ps)


@app.route("/plans/<int:pid>/start", methods=["POST"])
@login_required()
def start_plan(u, pid):
    if u["is_admin"]:
        abort(403)
    p = db.q("SELECT * FROM plans WHERE id=? AND active=1", (pid,), one=True) or abort(404)
    n_open = db.q("SELECT COUNT(*) n FROM accounts WHERE user_id=? AND status NOT IN ('failed','closed')",
                  (u["id"],), one=True)["n"]
    if n_open >= config.MAX_OPEN_ACCOUNTS_PER_USER:
        flash(f"You can have at most {config.MAX_OPEN_ACCOUNTS_PER_USER} open accounts", "error")
        return redirect(url_for("plans"))
    snap = {k: p[k] for k in ("name", "account_size", "fee_inr", "phases", "p1_target_pct", "p2_target_pct",
                              "min_trading_days", "max_days", "profit_split_pct", "payout_interval_days",
                              "min_payout_usd", "consistency_pct")}
    snap["rules"] = {**config.DEFAULT_RULES, **json.loads(p["rules_json"])}
    status = "pending_payment" if p["fee_inr"] > 0 else "awaiting_setup"
    aid = db.x("INSERT INTO accounts(user_id, plan_id, plan_json, phase, status, fee_inr, created_at) "
               "VALUES (?,?,?,?,?,?,?)", (u["id"], pid, json.dumps(snap), engine.first_phase(p), status,
                                          p["fee_inr"], time.time()))
    db.event(aid, "created", f"{p['name']} selected")
    if status == "pending_payment":
        return redirect(url_for("pay", aid=aid))
    flash("Challenge requested. You'll be able to trade as soon as the admin sets up your account.", "ok")
    return redirect(url_for("dashboard"))


@app.route("/account/<int:aid>/pay", methods=["GET", "POST"])
@login_required()
def pay(u, aid):
    a = own_account(u, aid)
    if request.method == "POST" and a["status"] == "pending_payment":
        ref = (request.form.get("utr") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{8,30}", ref):
            flash("Enter the UPI transaction ID / UTR (8–30 letters or digits)", "error")
        else:
            db.x("UPDATE accounts SET payment_ref=?, status='payment_review', status_reason='' WHERE id=?", (ref, aid))
            db.event(aid, "payment_submitted", f"UTR {ref}")
            flash("Thanks! The admin will confirm your payment shortly.", "ok")
            return redirect(url_for("dashboard"))
    note = f"{config.FIRM_NAME} A{aid}"
    upi = (f"upi://pay?pa={quote(config.ADMIN_UPI_ID)}&pn={quote(config.ADMIN_UPI_NAME)}"
           f"&am={a['fee_inr']:.2f}&cu=INR&tn={quote(note)}")
    return render_template("pay.html", user=u, a=a, upi=upi, note=note, upi_id=config.ADMIN_UPI_ID)


@app.route("/account/<int:aid>")
@login_required()
def trade(u, aid):
    a = own_account(u, aid)
    if a["status"] in ("pending_payment", "payment_review", "awaiting_setup"):
        return redirect(url_for("dashboard"))
    return render_template("account.html", **account_ctx(u, a, admin_view=False))


def account_ctx(u, a, admin_view):
    snaps = db.q("SELECT ts, equity FROM equity_snaps WHERE account_id=? ORDER BY ts", (a["id"],))
    if len(snaps) > 400:
        snaps = snaps[:: len(snaps) // 400 + 1]
    return dict(user=u, a=a, rules=db.rules_of(a), plan=db.plan_of(a), admin_view=admin_view, snaps=snaps,
                owner=db.q("SELECT * FROM users WHERE id=?", (a["user_id"],), one=True),
                orders=db.q("SELECT * FROM orders WHERE account_id=? ORDER BY ts DESC LIMIT 12", (a["id"],)),
                viol=db.q("SELECT * FROM violations WHERE account_id=? ORDER BY ts DESC LIMIT 5", (a["id"],)))


@app.route("/api/account/<int:aid>/state")
@login_required()
def api_state(u, aid):
    a = own_account(u, aid)
    rules = db.rules_of(a)
    out = {"status": a["status"], "status_label": engine.STATUS_LABEL.get(a["status"]), "reason": a["status_reason"],
           "phase": a["phase"], "last_error": a["last_error"], "positions": [], "prices": {}, "contract_values": {}}
    equity = None
    try:
        c = engine.client_for(a)
        equity, out["positions"] = c.account_state()
        for s in rules["allowed_symbols"]:
            out["prices"][s] = c.mark_price(s)
            out["contract_values"][s] = c.product(s)["contract_value"]
    except DeltaError as e:
        out["last_error"] = str(e)
    out["metrics"] = engine.metrics(a, equity)
    out["within_hours"] = engine.within_hours(rules)
    out["trades_today"] = db.q("SELECT COUNT(*) n FROM orders WHERE account_id=? AND day=? AND kind='entry' "
                               "AND status='sent'", (aid, engine.today()), one=True)["n"]
    return jsonify(out)


@app.route("/account/<int:aid>/order", methods=["POST"])
@login_required()
def order(u, aid):
    a = own_account(u, aid)
    if a["user_id"] != u["id"]:
        abort(403)
    ok, msg = engine.place_order(
        a, symbol=request.form.get("symbol", ""), side=request.form.get("side", ""),
        size=int(fnum("size", 0) or 0), leverage=int(fnum("leverage", 1) or 1),
        order_type="limit_order" if request.form.get("order_type") == "limit" else "market_order",
        limit_price=fnum("limit_price"), stop_loss=fnum("stop_loss"), take_profit=fnum("take_profit"))
    return jsonify(ok=ok, message=msg)


@app.route("/account/<int:aid>/close", methods=["POST"])
@login_required()
def close(u, aid):
    a = own_account(u, aid)
    sym = request.form.get("symbol", "")
    try:
        c = engine.client_for(a)
        if sym == "__all__":
            msg = "; ".join(c.flatten()) or "All positions closed, orders cancelled"
        else:
            c.close_position(sym)
            msg = f"Closed {sym}"
        engine.log_order(a, "sent", msg, symbol=sym, kind="close")
        return jsonify(ok=True, message=msg)
    except DeltaError as e:
        return jsonify(ok=False, message=str(e))


def build_report(a):
    m = engine.metrics(a) if a["starting_balance"] else None
    days = db.q("SELECT * FROM daily_stats WHERE account_id=? ORDER BY day DESC", (a["id"],))
    if a["day_date"] and m:
        start = a["day_start_equity"] or a["starting_balance"]
        n = db.q("SELECT COUNT(*) n FROM orders WHERE account_id=? AND day=? AND kind='entry' AND status='sent'",
                 (a["id"], a["day_date"]), one=True)["n"]
        days.insert(0, {"day": a["day_date"] + " (today)", "phase": a["phase"], "start_equity": start,
                        "end_equity": a["last_equity"], "pnl": (a["last_equity"] or start) - start, "trades": n,
                        "violations": None, "low_equity": None})
    pnls = [d["pnl"] for d in days if d["pnl"] is not None]
    green, red = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    snaps = db.q("SELECT ts, equity FROM equity_snaps WHERE account_id=? ORDER BY ts", (a["id"],))
    if len(snaps) > 600:
        snaps = snaps[:: len(snaps) // 600 + 1]
    summary = {
        "days_traded": len([d for d in days if (d["trades"] or 0) > 0]),
        "green": len(green), "red": len(red), "green_pct": len(green) / len(pnls) * 100 if pnls else None,
        "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None,
        "avg_green": sum(green) / len(green) if green else None, "avg_red": sum(red) / len(red) if red else None,
        "entries": db.q("SELECT COUNT(*) n FROM orders WHERE account_id=? AND kind='entry' AND status='sent'",
                        (a["id"],), one=True)["n"],
        "rejected": db.q("SELECT COUNT(*) n FROM orders WHERE account_id=? AND status='rejected'",
                         (a["id"],), one=True)["n"],
    }
    return dict(a=a, m=m, plan=db.plan_of(a), rules=db.rules_of(a), days=days, snaps=snaps, summary=summary,
                orders=db.q("SELECT * FROM orders WHERE account_id=? ORDER BY ts DESC LIMIT 200", (a["id"],)),
                viol=db.q("SELECT * FROM violations WHERE account_id=? ORDER BY ts DESC", (a["id"],)),
                events=db.q("SELECT * FROM events WHERE account_id=? ORDER BY ts DESC", (a["id"],)),
                payouts=db.q("SELECT * FROM payouts WHERE account_id=? ORDER BY id DESC", (a["id"],)),
                owner=db.q("SELECT * FROM users WHERE id=?", (a["user_id"],), one=True))


@app.route("/account/<int:aid>/report")
@login_required()
def report(u, aid):
    return render_template("report.html", user=u, **build_report(own_account(u, aid)))


@app.route("/account/<int:aid>/payout", methods=["GET", "POST"])
@login_required()
def payout(u, aid):
    a = own_account(u, aid)
    if request.method == "POST":
        if a["user_id"] != u["id"]:
            abort(403)
        if not u["upi_id"]:
            flash("Add your UPI ID in your profile first", "error")
            return redirect(url_for("profile"))
        try:
            engine.request_payout(a)
            flash("Payout requested. Trading on this account is paused until it's paid.", "ok")
            return redirect(url_for("payouts"))
        except LifecycleError as e:
            flash(str(e), "error")
            return redirect(url_for("payout", aid=aid))
    ok, why, info = engine.payout_check(a) if a["phase"] == "funded" else (False, ["Not funded yet"], {})
    return render_template("payout.html", user=u, a=a, ok=ok, why=why, info=info, m=engine.metrics(a),
                           plan=db.plan_of(a))


@app.route("/payouts")
@login_required()
def payouts(u):
    ps = db.q("SELECT p.*, a.plan_json FROM payouts p JOIN accounts a ON a.id=p.account_id WHERE p.user_id=? "
              "ORDER BY p.id DESC", (u["id"],))
    return render_template("payouts.html", user=u, payouts=ps)


# ================================================================ admin
@app.route("/admin")
@login_required(admin=True)
def admin_home(u):
    accs = db.q("SELECT a.*, u.username email, u.full_name, u.last_login FROM accounts a "
                "JOIN users u ON u.id=a.user_id ORDER BY a.id DESC")
    for a in accs:
        a["m"] = engine.metrics(a) if a["starting_balance"] else None
    live = [a for a in accs if a["status"] in ("active", "paused_day")]
    tot = {
        "capital": sum(a["m"]["starting_balance"] for a in accs if a["m"] and a["status"] not in ("closed",)),
        "equity": sum(a["m"]["equity"] for a in accs if a["m"] and a["status"] not in ("closed",)),
        "today": sum(a["m"]["daily_pnl"] for a in live if a["m"]),
        "live": len(live), "failed": len([a for a in accs if a["status"] == "failed"]),
    }
    return render_template("admin_home.html", user=u, accounts=accs, tot=tot,
                           dev_codes=list(mailer.dev_codes) if not mailer.email_enabled() else [])


TRADER_RULES = [("daily_loss_pct", float), ("max_drawdown_pct", float), ("max_leverage", int),
                ("max_risk_per_trade_pct", float), ("max_open_positions", int), ("max_trades_per_day", int),
                ("max_contracts_per_order", int)]


def trader_plan_from_form(f, old=None):
    rules = dict((old or {}).get("rules") or config.DEFAULT_RULES)
    for k, t in TRADER_RULES:
        v = (f.get(k) or "").strip()
        if v:
            rules[k] = t(float(v))
    rules.update(
        drawdown_type="trailing" if f.get("drawdown_type") == "trailing" else "static",
        daily_loss_action="pause" if f.get("daily_loss_action") == "pause" else "fail",
        allowed_symbols=[x.strip().upper() for x in (f.get("allowed_symbols") or "BTCUSD,ETHUSD").split(",") if x.strip()],
        require_stop_loss=bool(f.get("require_stop_loss")),
        trading_hours=(f.get("trading_hours") or "").strip(), flatten_outside_hours=False)
    capital_inr = float(f.get("capital_inr") or 0)
    if capital_inr <= 0:
        raise ValueError("Enter the capital in rupees")
    return {"name": f"{rupees(capital_inr)} account", "capital_inr": capital_inr,
            "account_size": round(capital_inr / config.USD_INR, 2), "fee_inr": 0, "phases": 0,
            "p1_target_pct": 0, "p2_target_pct": 0, "min_trading_days": 0, "max_days": 0,
            "profit_split_pct": float(f.get("profit_split_pct") or 0), "payout_interval_days": 14,
            "min_payout_usd": 0, "consistency_pct": 0, "goal_pct": float(f.get("goal_pct") or 0), "rules": rules}


def _plan_id():
    p = db.q("SELECT id FROM plans WHERE name='Direct (admin)'", one=True)
    if p:
        return p["id"]
    return db.x("INSERT INTO plans(name, account_size, phases, active, created_at) VALUES ('Direct (admin)', 0, 0, 0, ?)",
                (time.time(),))


@app.route("/admin/traders/new", methods=["GET", "POST"])
@login_required(admin=True)
def admin_trader_new(u):
    f = request.form
    if request.method == "POST":
        email = (f.get("email") or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email", "error")
            return render_template("trader_form.html", user=u, f=f, a=None, rules=config.DEFAULT_RULES)
        try:
            plan = trader_plan_from_form(f)
        except ValueError as e:
            flash(str(e) if "rupees" in str(e) else "Every number field must be a number", "error")
            return render_template("trader_form.html", user=u, f=f, a=None, rules=config.DEFAULT_RULES)
        t = db.q("SELECT * FROM users WHERE username=?", (email,), one=True)
        if t and t["is_admin"]:
            flash("That email belongs to the admin", "error")
            return redirect(request.url)
        if t:
            uid = t["id"]
            db.x("UPDATE users SET status='approved', full_name=COALESCE(NULLIF(?,''), full_name) WHERE id=?",
                 (f.get("full_name", "").strip(), uid))
        else:
            uid = db.x("INSERT INTO users(username, email, full_name, phone, pw_hash, status, created_at) "
                       "VALUES (?,?,?,?,?, 'approved', ?)",
                       (email, email, f.get("full_name", "").strip(), f.get("phone", "").strip(),
                        generate_password_hash(secrets.token_hex(16)), time.time()))
        aid = db.x("INSERT INTO accounts(user_id, plan_id, plan_json, phase, status, api_key, api_secret, "
                   "subaccount_label, created_at) VALUES (?,?,?, 'funded', 'awaiting_setup', ?,?,?,?)",
                   (uid, _plan_id(), json.dumps(plan), f.get("api_key", "").strip(), f.get("api_secret", "").strip(),
                    f.get("label", "").strip(), time.time()))
        db.event(aid, "created", f"{plan['name']} for {email}")
        try:
            engine.start_phase(db.account(aid), "funded", bool(f.get("force")))
            flash(f"{email} can log in now with an email code and trade {plan['name']}.", "ok")
        except (LifecycleError, DeltaError) as e:
            flash(f"Trader added, but trading isn't on yet: {e}", "error")
        return redirect(url_for("admin_account", aid=aid))
    return render_template("trader_form.html", user=u, f={}, a=None, rules=config.DEFAULT_RULES)


@app.route("/admin/accounts/<int:aid>/edit", methods=["GET", "POST"])
@login_required(admin=True)
def admin_trader_edit(u, aid):
    a = db.account(aid) or abort(404)
    owner = db.q("SELECT * FROM users WHERE id=?", (a["user_id"],), one=True)
    old = db.plan_of(a)
    f = request.form
    if request.method == "POST":
        try:
            plan = trader_plan_from_form(f, old)
        except ValueError:
            flash("Every number field must be a number", "error")
            return redirect(request.url)
        db.x("UPDATE accounts SET plan_json=?, subaccount_label=? WHERE id=?",
             (json.dumps(plan), f.get("label", "").strip(), aid))
        if f.get("api_key", "").strip():
            db.x("UPDATE accounts SET api_key=? WHERE id=?", (f["api_key"].strip(), aid))
        if f.get("api_secret", "").strip():
            db.x("UPDATE accounts SET api_secret=? WHERE id=?", (f["api_secret"].strip(), aid))
        db.x("UPDATE users SET full_name=?, phone=? WHERE id=?",
             (f.get("full_name", "").strip(), f.get("phone", "").strip(), owner["id"]))
        engine._clients.clear()
        msg = "Saved."
        if plan["account_size"] != old.get("account_size"):
            msg += " New capital applies when you click Restart account."
        db.event(aid, "rules_changed", "by admin")
        flash(msg, "ok")
        return redirect(url_for("admin_account", aid=aid))
    fdata = {"email": owner["username"], "full_name": owner["full_name"], "phone": owner["phone"],
             "capital_inr": old.get("capital_inr") or round(old["account_size"] * config.USD_INR),
             "profit_split_pct": old.get("profit_split_pct"), "goal_pct": old.get("goal_pct"),
             "label": a["subaccount_label"]}
    return render_template("trader_form.html", user=u, f=fdata, a=a, rules=db.rules_of(a))


@app.route("/admin/users")
@login_required(admin=True)
def admin_users(u):
    users = db.q("SELECT u.*, (SELECT COUNT(*) FROM accounts a WHERE a.user_id=u.id) n_acc FROM users u "
                 "WHERE is_admin=0 ORDER BY id DESC")
    return render_template("admin_users.html", user=u, users=users)


@app.route("/admin/users/<int:uid>/action", methods=["POST"])
@login_required(admin=True)
def admin_user_action(u, uid):
    t = db.q("SELECT * FROM users WHERE id=? AND is_admin=0", (uid,), one=True) or abort(404)
    act = request.form.get("action")
    if act in ("approve", "enable"):
        db.x("UPDATE users SET status='approved' WHERE id=?", (uid,))
        flash(f"{t['username']} approved", "ok")
    elif act == "disable":
        db.x("UPDATE users SET status='disabled' WHERE id=?", (uid,))
        for a in db.q("SELECT * FROM accounts WHERE user_id=? AND status IN ('active','paused_day')", (uid,)):
            try:
                engine.client_for(a).flatten()
            except DeltaError:
                pass
            engine.set_status(a, "closed", "User disabled by admin")
        flash(f"{t['username']} disabled and their accounts closed", "ok")
    elif act == "reset_password":
        pw = secrets.token_urlsafe(8)
        db.x("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), uid))
        flash(f"New password for {t['username']}: {pw}  (send it to them privately)", "ok")
    return back(url_for("admin_users"))


@app.route("/admin/plans")
@login_required(admin=True)
def admin_plans(u):
    ps = db.q("SELECT p.*, (SELECT COUNT(*) FROM accounts a WHERE a.plan_id=p.id) n FROM plans p ORDER BY active DESC, account_size")
    return render_template("admin_plans.html", user=u, plans=ps)


PLAN_FIELDS = [("account_size", float), ("fee_inr", float), ("phases", int), ("p1_target_pct", float),
               ("p2_target_pct", float), ("min_trading_days", int), ("max_days", int), ("profit_split_pct", float),
               ("payout_interval_days", int), ("min_payout_usd", float), ("consistency_pct", float)]
RULE_FIELDS = [("daily_loss_pct", float), ("max_drawdown_pct", float), ("max_leverage", int),
               ("max_risk_per_trade_pct", float), ("max_open_positions", int), ("max_contracts_per_order", int),
               ("max_trades_per_day", int)]


@app.route("/admin/plans/new", methods=["GET", "POST"])
@app.route("/admin/plans/<int:pid>", methods=["GET", "POST"])
@login_required(admin=True)
def admin_plan(u, pid=None):
    p = db.q("SELECT * FROM plans WHERE id=?", (pid,), one=True) if pid else None
    if pid and not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            vals = {k: t(float(f.get(k) or 0)) for k, t in PLAN_FIELDS}
            rules = {k: t(float(f.get(k) or 0)) for k, t in RULE_FIELDS}
        except ValueError:
            flash("Every number field must be a number", "error")
            return redirect(request.url)
        rules.update(
            drawdown_type="trailing" if f.get("drawdown_type") == "trailing" else "static",
            daily_loss_action="pause" if f.get("daily_loss_action") == "pause" else "fail",
            allowed_symbols=[s.strip().upper() for s in f.get("allowed_symbols", "").split(",") if s.strip()],
            require_stop_loss=bool(f.get("require_stop_loss")), flatten_outside_hours=bool(f.get("flatten_outside_hours")),
            trading_hours=f.get("trading_hours", "").strip())
        vals.update(name=f.get("name", "").strip() or "Plan", rules_json=json.dumps(rules),
                    active=1 if f.get("active") else 0)
        cols = list(vals)
        if p:
            db.x(f"UPDATE plans SET {', '.join(c + '=?' for c in cols)} WHERE id=?", [vals[c] for c in cols] + [pid])
            flash("Plan saved. Existing accounts keep the rules they started with.", "ok")
        else:
            pid = db.x(f"INSERT INTO plans({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(cols))}, ?)",
                       [vals[c] for c in cols] + [time.time()])
            flash("Plan created", "ok")
        return redirect(url_for("admin_plans"))
    rules = {**config.DEFAULT_RULES, **(json.loads(p["rules_json"]) if p else {})}
    return render_template("plan_form.html", user=u, p=p or config.SEED_PLANS[0] | {"active": 1, "name": ""},
                           rules=rules, is_new=not p)


@app.route("/admin/accounts")
@login_required(admin=True)
def admin_accounts(u):
    f = request.args.get("f", "live")
    where = {"live": "a.status IN ('active','paused_day','passed','payout_pending')",
             "funded": "a.phase='funded' AND a.status NOT IN ('failed','closed')",
             "failed": "a.status IN ('failed','closed')", "all": "1=1"}.get(f, "1=1")
    accs = db.q(f"SELECT a.*, u.username FROM accounts a JOIN users u ON u.id=a.user_id WHERE {where} ORDER BY a.id DESC")
    for a in accs:
        a["m"] = engine.metrics(a) if a["starting_balance"] else None
    accs.sort(key=lambda a: a["m"]["total_pnl_pct"] if a["m"] else -1e9, reverse=True)
    return render_template("admin_accounts.html", user=u, accounts=accs, f=f)


@app.route("/admin/accounts/<int:aid>")
@login_required(admin=True)
def admin_account(u, aid):
    a = db.account(aid) or abort(404)
    return render_template("account.html", **account_ctx(u, a, admin_view=True))


@app.route("/admin/accounts/<int:aid>/action", methods=["POST"])
@login_required(admin=True)
def admin_account_action(u, aid):
    a = db.account(aid) or abort(404)
    act, f = request.form.get("action"), request.form
    force = bool(f.get("force"))
    try:
        if act == "confirm_payment":
            engine.set_status(a, "awaiting_setup")
            db.event(aid, "payment_confirmed", f"₹{a['fee_inr']:,.0f}, UTR {a['payment_ref']}")
        elif act == "reject_payment":
            engine.set_status(a, "pending_payment", f.get("note") or "Payment not found — check the UTR")
            db.event(aid, "payment_rejected", f.get("note", ""))
        elif act == "save_keys":
            key, sec = f.get("api_key", "").strip(), f.get("api_secret", "").strip()
            if key:
                db.x("UPDATE accounts SET api_key=? WHERE id=?", (key, aid))
            if sec:
                db.x("UPDATE accounts SET api_secret=? WHERE id=?", (sec, aid))
            db.x("UPDATE accounts SET subaccount_label=? WHERE id=?", (f.get("label", "").strip(), aid))
            a = db.account(aid)
            try:
                eq, pos = engine.client_for(a).account_state()
                flash(f"Keys saved. Connected ✓ — equity {eq:.2f} {config.SETTLE_ASSET}, {len(pos)} open positions", "ok")
            except DeltaError as e:
                flash(f"Keys saved but connection failed: {e}", "error")
        elif act == "activate":
            if a["status"] != "awaiting_setup":
                raise LifecycleError("Account is not awaiting setup")
            engine.start_phase(a, a["phase"], force)
            flash(f"{engine.PHASE_LABEL[a['phase']]} started", "ok")
        elif act == "next_phase":
            if a["status"] != "passed":
                raise LifecycleError("Account has not passed")
            nxt = engine.next_phase(a)
            engine.start_phase(a, nxt, force)
            flash(f"{engine.PHASE_LABEL[nxt]} started", "ok")
        elif act == "restart":
            engine.start_phase(db.account(aid), "funded", force)
            flash("Account restarted with fresh limits.", "ok")
        elif act == "block":
            try:
                engine.client_for(a).flatten()
            except DeltaError:
                pass
            engine.set_status(a, "closed", f.get("note") or "Trading turned off by admin")
            db.event(aid, "blocked", "by admin")
        elif act == "fail":
            engine.fail(a, "admin", f.get("note") or "Failed by admin")
        elif act == "close":
            try:
                engine.client_for(a).flatten()
            except DeltaError:
                pass
            engine.set_status(a, "closed", f.get("note") or "Closed by admin")
            db.event(aid, "closed", f.get("note", ""))
        elif act == "flatten":
            flash("; ".join(engine.client_for(a).flatten()) or "All positions closed", "ok")
        elif act == "reactivate":
            engine.set_status(a, "active")
            db.event(aid, "reactivated", "by admin")
        elif act == "delete":
            for t in ("orders", "violations", "events", "equity_snaps", "daily_stats", "payouts"):
                db.x(f"DELETE FROM {t} WHERE account_id=?", (aid,))
            db.x("DELETE FROM accounts WHERE id=?", (aid,))
            flash("Account deleted", "ok")
            return redirect(url_for("admin_home"))
    except (LifecycleError, DeltaError) as e:
        flash(str(e), "error")
    return redirect(url_for("admin_account", aid=aid))


@app.route("/admin/payouts")
@login_required(admin=True)
def admin_payouts(u):
    ps = db.q("SELECT p.*, u.username, u.upi_id, u.full_name, a.plan_json, a.subaccount_label, a.starting_balance "
              "FROM payouts p JOIN users u ON u.id=p.user_id JOIN accounts a ON a.id=p.account_id ORDER BY p.id DESC")
    return render_template("admin_payouts.html", user=u, payouts=ps)


@app.route("/admin/payouts/<int:pid>/action", methods=["POST"])
@login_required(admin=True)
def admin_payout_action(u, pid):
    p = db.q("SELECT * FROM payouts WHERE id=?", (pid,), one=True) or abort(404)
    act, f = request.form.get("action"), request.form
    try:
        if act == "approve" and p["status"] == "requested":
            engine.payout_approve(p, f.get("note", ""))
        elif act == "reject" and p["status"] in ("requested", "approved"):
            engine.payout_reject(p, f.get("note") or "Rejected")
        elif act == "paid" and p["status"] == "approved":
            utr = f.get("utr", "").strip()
            if not utr:
                raise LifecycleError("Enter the UPI UTR of your payment to the trader")
            engine.payout_mark_paid(p, utr, bool(f.get("force")))
            flash("Payout marked paid. Account is trading again.", "ok")
    except (LifecycleError, DeltaError) as e:
        flash(str(e), "error")
    return redirect(url_for("admin_payouts"))


@app.route("/admin/export/<what>.csv")
@login_required(admin=True)
def export(u, what):
    queries = {
        "daily": "SELECT us.username, d.account_id, d.* FROM daily_stats d JOIN accounts a ON a.id=d.account_id "
                 "JOIN users us ON us.id=a.user_id ORDER BY us.username, d.day",
        "payouts": "SELECT us.username, us.upi_id, p.* FROM payouts p JOIN users us ON us.id=p.user_id ORDER BY p.id",
        "accounts": "SELECT us.username, a.id, a.phase, a.status, a.status_reason, a.starting_balance, a.last_equity, "
                    "a.fee_inr, a.payment_ref, a.created_at, a.funded_at FROM accounts a JOIN users us ON us.id=a.user_id",
    }
    rows = db.q(queries.get(what) or abort(404))
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=propdesk_{what}.csv"})


# ================================================================ boot
def bootstrap():
    db.init()
    try:
        os.chmod(config.DB_PATH, 0o600)
    except OSError:
        pass
    if not db.q("SELECT 1 FROM users WHERE is_admin=1", one=True):
        pw = config.ADMIN_PASSWORD or secrets.token_urlsafe(10)
        db.x("INSERT INTO users(username, pw_hash, is_admin, status, created_at) VALUES (?,?,1,'approved',?)",
             (config.ADMIN_USERNAME, generate_password_hash(pw), time.time()))
        print(f"\n*** Admin created: username={config.ADMIN_USERNAME} password={pw}\n")
    if not db.q("SELECT 1 FROM plans", one=True):
        for p in config.SEED_PLANS:
            cols = list(p)
            db.x(f"INSERT INTO plans({', '.join(cols)}, rules_json, created_at) VALUES ({', '.join('?' * len(cols))}, ?, ?)",
                 [p[c] for c in cols] + [json.dumps(config.DEFAULT_RULES), time.time()])
    if config.SECRET_KEY.startswith("change-this"):
        print("WARNING: set PROPDESK_SECRET_KEY before going live.")
    print(f"{config.FIRM_NAME} running in {config.MODE.upper()} mode -> {config.BASE_URL}")
    engine.start_watcher()


bootstrap()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")  # set HOST=0.0.0.0 to open it to your Wi-Fi/LAN
    print(f"Open http://{'localhost' if host == '127.0.0.1' else host}:{os.environ.get('PORT', 8000)}")
    app.run(host=host, port=int(os.environ.get("PORT", 8000)), threaded=True, use_reloader=False)
