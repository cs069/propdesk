# PropDesk

Let a few friends trade your Delta Exchange capital under rules you set.
You add a friend by email with an amount in rupees. They log in with a 6-digit email code and trade from a dashboard.
The app blocks orders that break the rules and closes positions when a loss limit is hit.
No one can withdraw: each friend trades a separate Delta sub-account through a **Read + Trading** API key.

## Run it on your computer
Needs Python 3.10+.

```bash
git clone https://github.com/cs069/propdesk.git
cd propdesk
./run.sh            # Windows: double-click run.bat (or ./run.sh in Git Bash)
```
1. **First run:** it installs everything and creates `.env`. Set `PROPDESK_ADMIN_PASSWORD` there, then restart.
2. **Admin:** open http://localhost:8000/admin/login and log in as `admin`.
3. **Add a trader:** enter their email, capital (e.g. 100000) and rules.
4. **Your friend:** opens http://localhost:8000, enters their email, and types in the 6-digit code.
   - **No email set up:** the code appears on your admin dashboard (and in the terminal). Send it to them on WhatsApp.
   - **Email set up:** fill in the `SMTP_*` lines in `.env` (Gmail works with an App Password) and codes go out by email.

It starts in **paper mode**: simulated fills on live Delta prices, no keys and no money, so you can try everything safely.
To let friends on your Wi-Fi open it, add `HOST=0.0.0.0` to `.env` and share `http://<your-PC-IP>:8000`.

## What your friend sees
- **Equity in ₹:** a bar showing where they stand between the fail level, today's stop and their goal.
- **Today:** P&L, loss room left today and overall, and open P&L.
- **Equity chart, open positions and recent activity.**
- **Order panel:** buy/sell, contracts, leverage, stop-loss and take-profit. It shows the loss if the stop hits before they place the order.
- **Rules checklist** with live meters, plus their profit share.

## What you control (per friend)
Capital (₹), daily loss limit % (end the account or pause for the day), and max loss % (from starting capital or from highest equity).
Also: max leverage, max loss per trade %, stop-loss required, number of positions, trades per day, size limit, markets, trading hours, profit share % and an optional profit goal.

Buttons on each trader's page: **Close all positions**, **Turn off / on trading**, **Restart account** (fresh balance and limits), and **Edit rules**.
**Download daily results** gives a CSV for everyone.

## Going live with Delta
1. On Delta: Profile → Sub-Accounts → create one per friend → move their capital into it.
   The app uses ₹85/USD (`PROPDESK_USD_INR`), so ₹1,00,000 = 1,176.47 USD.
2. On **that sub-account** create an API key with **Read Data + Trading only** and whitelist your IP.
3. Set `PROPDESK_MODE=testnet` with demo keys first, then `live`. Paste the keys when you add the trader.
4. Set a long random `PROPDESK_SECRET_KEY` in `.env`.

> In a fast market, the price can move past a limit before positions close. The sub-account balance is the real cap.
> Everything on Delta is under your KYC, so you're responsible for all trades and tax.

## Files
`app.py` web app · `engine.py` rules, risk watcher, account lifecycle · `delta_client.py` Delta India API ·
`paper_client.py` simulator · `mailer.py` login codes · `db.py` SQLite · `config.py` settings · `templates/`, `static/` pages.

The database `propdesk.db` holds API secrets. It's git-ignored; keep it private and back it up.
