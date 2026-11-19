"""
PropDesk configuration. Most things can be set with environment variables.
Plans (account sizes, targets, splits, rules) are managed in the admin panel;
SEED_PLANS below are only created on the very first start.
"""
import os
from datetime import timezone, timedelta


def _load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Read KEY=VALUE lines from .env (real environment variables win)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()
env = os.environ.get

# ---- Mode -------------------------------------------------------------
# paper   : simulated fills on real Delta prices, no keys needed (for testing)
# testnet : Delta demo account keys (demo.delta.exchange)
# live    : real money (delta.exchange)
MODE = env("PROPDESK_MODE", "paper").lower()
BASE_URLS = {
    "live": "https://api.india.delta.exchange",
    "testnet": "https://cdn-ind.testnet.deltaex.org",
    "paper": "https://api.india.delta.exchange",
}
BASE_URL = BASE_URLS.get(MODE, BASE_URLS["paper"])

# ---- App --------------------------------------------------------------
FIRM_NAME = env("PROPDESK_NAME", "PropDesk")
SECRET_KEY = env("PROPDESK_SECRET_KEY", "change-this-to-a-long-random-string")
DB_PATH = env("PROPDESK_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "propdesk.db"))
ADMIN_USERNAME = env("PROPDESK_ADMIN_USER", "admin")
ADMIN_PASSWORD = env("PROPDESK_ADMIN_PASSWORD")       # used only on first start

# ---- Registration -----------------------------------------------------
REGISTRATION_OPEN = env("PROPDESK_REGISTRATION_OPEN", "1") == "1"
# If set, people who enter this code are approved instantly; others wait for admin approval.
INVITE_CODE = env("PROPDESK_INVITE_CODE", "")
MAX_OPEN_ACCOUNTS_PER_USER = int(env("PROPDESK_MAX_ACCOUNTS", "2"))

# ---- Money ------------------------------------------------------------
SETTLE_ASSET = env("PROPDESK_SETTLE_ASSET", "USD")    # Delta India wallets are USD-denominated
USD_INR = float(env("PROPDESK_USD_INR", "85"))        # rate used to show payouts in ₹
ADMIN_UPI_ID = env("PROPDESK_UPI_ID", "yourname@upi")  # where challenge fees are paid
ADMIN_UPI_NAME = env("PROPDESK_UPI_NAME", FIRM_NAME)
# How close (fraction) a sub-account balance must be to the plan size when you activate it
BALANCE_TOLERANCE = 0.02

# ---- Engine -----------------------------------------------------------
TZ = timezone(timedelta(hours=5, minutes=30))          # trading day = midnight to midnight IST
CHECK_INTERVAL_SEC = 5
SNAPSHOT_INTERVAL_SEC = 60

# Risk-rule defaults (each plan stores its own copy; edit plans in admin)
DEFAULT_RULES = {
    "daily_loss_pct": 5.0,          # % of account size
    "daily_loss_action": "fail",    # "fail" (prop-firm style) or "pause" (locked till midnight)
    "max_drawdown_pct": 10.0,
    "drawdown_type": "static",      # "static" from start, or "trailing" from peak equity
    "max_leverage": 10,
    "allowed_symbols": ["BTCUSD", "ETHUSD"],
    "require_stop_loss": True,
    "max_risk_per_trade_pct": 1.0,
    "max_open_positions": 2,
    "max_contracts_per_order": 1000,
    "max_trades_per_day": 20,
    "trading_hours": "",            # e.g. "09:00-23:00" IST, empty = 24/7
    "flatten_outside_hours": False,
}

SEED_PLANS = [
    # phases: 2 = Phase 1 -> Phase 2 -> Funded, 1 = Phase 1 -> Funded, 0 = instant funded
    dict(name="Starter $100", account_size=100, fee_inr=0, phases=2, p1_target_pct=8, p2_target_pct=5,
         min_trading_days=3, max_days=30, profit_split_pct=80, payout_interval_days=14,
         min_payout_usd=5, consistency_pct=40),
    dict(name="Pro $250", account_size=250, fee_inr=0, phases=2, p1_target_pct=8, p2_target_pct=5,
         min_trading_days=3, max_days=30, profit_split_pct=80, payout_interval_days=14,
         min_payout_usd=10, consistency_pct=40),
    dict(name="One-step $500", account_size=500, fee_inr=0, phases=1, p1_target_pct=10, p2_target_pct=0,
         min_trading_days=5, max_days=0, profit_split_pct=70, payout_interval_days=14,
         min_payout_usd=20, consistency_pct=0),
]
