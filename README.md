# PropDesk — a private prop firm on Delta Exchange

Friends register, pick a challenge, pass Phase 1 → Phase 2, get funded, and request payouts.
They trade **only through this website**. Each account runs on its own Delta sub-account with a
**Read + Trading** API key, so nobody can ever withdraw from Delta.

---

## 1. The full journey (A → Z)

```
TRADER                                   ADMIN (you)                         SYSTEM (automatic)
──────                                   ───────────                         ──────────────────
1  Register (name, email, phone, UPI)
                                         2  Approve registration
3  Pick a plan
4  Pay fee by UPI, enter UTR             5  Confirm payment            (skipped if the fee is ₹0)
                                         6  Create Delta sub-account,
                                            fund it with plan size,
                                            paste API key → Start Phase 1
7  Trade Phase 1 on the dashboard                                          checks every order before sending it
                                                                           watches equity every 5 s
                                                                           target + min days hit → PASSED (positions closed)
                                                                           daily loss / drawdown / time limit → FAILED
                                         8  Reset sub-account to plan
                                            size → Start Phase 2
9  Trade Phase 2                                                           same checks → PASSED / FAILED
                                         10 Reset balance → Start Funded
11 Trade funded account                                                    same risk rules, no target
12 Request payout (after 14 days,                                          checks window, min days, min profit,
   min days, consistency rule)                                             consistency, no open positions;
                                                                           pauses trading
                                         13 Review → Approve
                                         14 Move profit out of sub-account
                                            to your main, pay trader's share
                                            by UPI, enter UTR → Mark paid
15 Sees ₹ paid + UTR                                                       account restarts at plan size → back to 11
```

### Account statuses
`pending_payment → payment_review → awaiting_setup → active → passed → active (next phase) … → funded active ⇄ payout_pending`
Any breach → `failed`. Admin can also `close` (no fault), `pause/unpause`, `reactivate`.

### Default plans (edit in Admin → Plans)
| Plan | Size | Steps | Targets | Min days | Time limit | Split | Payout every |
|---|---|---|---|---|---|---|---|
| Starter | $100 | 2-step | 8% / 5% | 3 | 30 d/phase | 80% | 14 d |
| Pro | $250 | 2-step | 8% / 5% | 3 | 30 d/phase | 80% | 14 d |
| One-step | $500 | 1-step | 10% | 5 | none | 70% | 14 d |

The default risk rules on every plan are: daily loss 5% (the account fails) and max drawdown 10% (static).
Leverage is capped at 10x, and the loss at the stop-loss can't be more than 1% of the account. A stop-loss is required on every trade.
Only BTCUSD and ETHUSD are allowed, with at most 2 positions open and 20 trades a day.
For payouts, the best day can't be more than 40% of the profit (consistency rule).
Fees are ₹0 by default. All % figures are % of the account size.
**A plan's rules are copied into each account when it starts**, so editing a plan never changes rules under someone mid-challenge.

### Payout math
`profit = equity − account size` → `trader share = profit × split%` → shown in ₹ at `PROPDESK_USD_INR` (default 85).
You keep the rest. After you pay, the sub-account goes back to the account size and the trader continues.

---

## 2. Safety model
| Layer | Protects against |
|---|---|
| One Delta sub-account per trader account | A trader can only lose their sub-account balance — never your main balance or other traders' |
| API key with Read + Trading only + IP whitelist | Withdrawals are impossible; a stolen key is useless from another IP |
| Pre-trade checks | Oversized trades, missing stop-loss, wrong coins, too much leverage, trading while paused |
| Risk watcher | Closes positions and fails/pauses the account when limits are hit |
| Admin checks the sub-account balance before starting a phase or marking a payout paid | Starting with the wrong balance, forgetting to move profit out |

> In a fast market, the price can move past the limit before positions close (slippage). Fund sub-accounts only with what you're OK losing.

---

## 3. Run locally (paper mode: no keys, no money)
Needs Python 3.10+.

```bash
git clone https://github.com/<you>/propdesk.git
cd propdesk
./run.sh          # macOS / Linux
run.bat           # Windows (double-click or run in cmd)
```
On the first run this creates `.venv/`, installs the requirements and copies `.env.example` → `.env`.
Edit `.env` (admin password, UPI ID), stop with Ctrl+C, then run it again.

To do it by hand instead: `python -m venv .venv`, activate it, `pip install -r requirements.txt`, `cp .env.example .env`, then `python app.py`.

Open http://localhost:8000 and log in as `admin` with the password from `.env`. Then:
1. Register a test trader in a private window.
2. As `admin`, approve them.
3. As the trader, pick a plan.
4. As admin, click **Start** under Overview → step 3.
5. Trade.

Paper mode simulates fills on live Delta prices. Admin "Start" and "Mark paid" reset or withdraw the paper balance automatically.

## 4. Settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `PROPDESK_MODE` | `paper` | `paper`, `testnet` (demo.delta.exchange keys) or `live` |
| `PROPDESK_SECRET_KEY` | — | **Required live.** Long random string |
| `PROPDESK_ADMIN_PASSWORD` | random (printed) | First-start admin password |
| `PROPDESK_NAME` | PropDesk | Firm name shown everywhere |
| `PROPDESK_UPI_ID` / `PROPDESK_UPI_NAME` | — | Where challenge fees are paid |
| `PROPDESK_INVITE_CODE` | empty | People who enter it skip approval |
| `PROPDESK_REGISTRATION_OPEN` | 1 | 0 = no new signups |
| `PROPDESK_MAX_ACCOUNTS` | 2 | Open accounts per trader |
| `PROPDESK_USD_INR` | 85 | Rate for showing payouts in ₹ |

## 5. Go live
1. **Server:** a VPS with a fixed IP (Lightsail / DigitalOcean / Hetzner, ~₹400–800/month), plus a domain name.
2. Copy this folder to `/opt/propdesk`, then run `pip install -r requirements.txt`.
3. Set it up as a systemd service, `/etc/systemd/system/propdesk.service`:
```ini
[Unit]
Description=PropDesk
After=network.target
[Service]
WorkingDirectory=/opt/propdesk
Environment=PROPDESK_MODE=live
Environment=PROPDESK_SECRET_KEY=paste-64-random-chars
Environment=PROPDESK_UPI_ID=you@okaxis
Environment=PROPDESK_NAME=YourDesk
ExecStart=/usr/bin/gunicorn -w 1 --threads 8 -b 127.0.0.1:8000 app:app
Restart=always
[Install]
WantedBy=multi-user.target
```
   `systemctl enable --now propdesk`, then `journalctl -u propdesk` shows the first admin password.
   **Keep `-w 1`**, because the risk watcher runs inside the app.
4. **HTTPS:** use Caddy (`/etc/caddy/Caddyfile`: `yourdomain.com { reverse_proxy 127.0.0.1:8000 }`).
5. **Backups:** copy `propdesk.db` daily (`sqlite3 propdesk.db ".backup backup.db"`). It holds API secrets, so keep it private.
6. Run a full round on `PROPDESK_MODE=testnet` with demo keys before going live.

### Per account, on Delta (admin, step 6)
Profile → Sub-Accounts → create `pd-<id>-<name>` → transfer exactly the plan size →
on that sub-account create an API key with **Read Data + Trading only**, whitelisted to your server IP →
paste it in Admin → account → **Save & test connection** → **Start**.
Reuse a sub-account for a new challenge after the old one fails. Keep one sub-account per *running* account.

---

## 6. Files
| File | What |
|---|---|
| `app.py` | Web app: registration, trader pages, admin panel |
| `engine.py` | Lifecycle, pre-trade checks, risk watcher, payouts |
| `delta_client.py` | Delta India REST API (signed requests) |
| `paper_client.py` | Simulator with the same interface |
| `db.py` | SQLite schema + helpers |
| `config.py` | Settings, default rules, starter plans |
| `templates/`, `static/` | Pages and styling |

## 7. Notes
- The admin moves money on Delta by hand (funding sub-accounts and taking profit out). The app checks the balance before it continues.
- Fees and payouts go by UPI by hand, with the UTR recorded. The app has no payment gateway.
- No emails or SMS are sent. Traders see everything on their dashboard.
- Everything on Delta is under your KYC: you're responsible for all trades and tax. Taking fees and paying
  profit shares can make this a business, so get proper advice on tax and legality before you charge money.
