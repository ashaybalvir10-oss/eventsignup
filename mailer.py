"""Email notifications. Tries, in order:

1. Brevo HTTP API   (BREVO_API_KEY + MAIL_FROM)   <- works on free hosts that block SMTP
2. SMTP             (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, optional MAIL_FROM)
3. Console print    (nothing configured: fine for local demos)

Every email is also saved to the `outbox` table (admin can see it in the app).
Addresses ending in .invalid (used by demo users) are never actually sent.
"""
import json
import os
import smtplib
import threading
import urllib.request
from email.message import EmailMessage


def _brevo(to, subject, body):
    payload = {"sender": {"name": os.environ.get("MAIL_FROM_NAME", "Event Waitlist"),
                          "email": os.environ["MAIL_FROM"]},
               "to": [{"email": to}], "subject": subject, "textContent": body}
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode(),
        headers={"api-key": os.environ["BREVO_API_KEY"],
                 "Content-Type": "application/json", "accept": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()


def _smtp(to, subject, body):
    msg = EmailMessage()
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587)), timeout=15) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def _deliver(to, subject, body):
    try:
        if os.environ.get("BREVO_API_KEY") and os.environ.get("MAIL_FROM"):
            _brevo(to, subject, body)
        elif os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"):
            _smtp(to, subject, body)
        else:
            print(f"\n[EMAIL -> {to}] {subject}\n{body}\n")
    except Exception as e:  # a mail failure must never break signups
        print(f"[mail error] {e}")


def send_email(to, subject, body, db=None):
    """Log to the outbox, then send in a background thread so the API stays fast."""
    if db is not None:
        db.execute("INSERT INTO outbox(to_email, subject, body) VALUES (?,?,?)", (to, subject, body))
        db.commit()
    if not to.endswith(".invalid"):
        threading.Thread(target=_deliver, args=(to, subject, body), daemon=True).start()
