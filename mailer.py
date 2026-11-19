"""Sends 6-digit login codes. Without SMTP settings the code is printed and kept for the admin dashboard."""
import smtplib
import time
from collections import deque
from email.message import EmailMessage

import config

# Recent codes when email is not configured (shown to admin only)
dev_codes = deque(maxlen=20)


def email_enabled():
    return bool(config.SMTP_HOST)


def send_code(email, name, code):
    if not email_enabled():
        dev_codes.appendleft({"email": email, "code": code, "ts": time.time()})
        print(f"\n  >>> Login code for {email}: {code}  (valid {config.OTP_TTL_SEC // 60} min)\n", flush=True)
        return
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your {config.FIRM_NAME} login code"
    msg["From"] = config.SMTP_FROM
    msg["To"] = email
    msg.set_content(f"Hi {name or ''},\n\nYour {config.FIRM_NAME} login code is {code}.\n"
                    f"It works for {config.OTP_TTL_SEC // 60} minutes. If you didn't ask for it, ignore this email.\n")
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
        s.starttls()
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASS)
        s.send_message(msg)
