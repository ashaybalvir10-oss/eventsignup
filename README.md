# Event Signup with Waitlist

Python + Flask + SQLite. Accounts (sign up / sign in), events, automatic waitlist,
and an email when a spot opens. Data structures are written in Python (`ds.py`).

## Data structures (`ds.py`)
- **Singly linked list as a FIFO queue**: the waitlist. O(1) join (tail) and O(1) promote (head).
- **Hash map**: email -> node, so "already waiting?" is O(1).
- **Mid-queue removal**: leaving the waitlist = pointer walk, O(n).
- **Sliding-window rate limiter** (deque of timestamps): locks password guessing (5 failures / 5 min).

## How it works
1. Admin creates an event with a category (music, sports, academics, arts, technology, community, or other). Existing database events are kept and assigned the `other` category during migration.
2. Users create an account, sign in, filter events by category, and click Join.
3. Spots 1..N are confirmed. After that users join the back of the queue and get a "you are #k" email.
4. When a confirmed user cancels, the head of the queue is dequeued, confirmed and emailed.
5. SQLite stores everything; queues are rebuilt on restart. A lock prevents double-booking.
6. Admin page `/visualizer` shows the queue live as a linked list.

Security: passwords hashed (werkzeug), HttpOnly + SameSite session cookies, JSON-only POSTs
(blocks CSRF form posts), login rate limit, admin-only endpoints.

## Run locally
    pip install -r requirements.txt
    python test_waitlist.py
    python app.py            # http://localhost:5000

With no email settings, emails print in the console. With no `ADMIN_EMAILS`, the FIRST account
registered becomes admin.

## Environment variables
| Name | Purpose |
|---|---|
| `SECRET_KEY` | Signs login cookies. Required in production (long random string). |
| `PRODUCTION` | Set `1` online so cookies are HTTPS-only. |
| `ADMIN_EMAILS` | Comma-separated emails that become admins when they register. |
| `BREVO_API_KEY`, `MAIL_FROM` | Email via Brevo's HTTP API (works on hosts that block SMTP). `MAIL_FROM` must be a sender verified in Brevo. |
| `SMTP_HOST/PORT/USER/PASS` | Alternative: normal SMTP (e.g. Gmail app password). |
| `DB_PATH` | SQLite file location (default `events.db`). |

## Put it online (Render)
1. Push this folder to a GitHub repo.
2. Render -> New -> Blueprint (reads `render.yaml`) or New Web Service (build `pip install -r requirements.txt`,
   start `gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT`).
3. Set `ADMIN_EMAILS` (your email), `MAIL_FROM`, `BREVO_API_KEY`. `MAIL_FROM` must match a verified sender in Brevo. Render generates `SECRET_KEY`.
4. You get a free domain like `https://event-waitlist.onrender.com` with HTTPS.
5. Register with your admin email, create an event, test with a second account.

### Email setup (Brevo, free tier)
Create a Brevo account -> add and verify a sender email -> create an API key -> put both in the env vars. The app's organizer panel shows whether the required email settings are present; `/api/email-status` reports only the provider/readiness and never returns credentials. Welcome, confirmation, waitlist, and promotion messages are already sent through Brevo when both settings are present. Without them, the app records messages in its outbox and prints them to the server log for development; it cannot deliver real email until an email provider is configured.

### Things to know
- **Render's free plan blocks SMTP ports**, which is why the app uses Brevo's HTTP API.
- **Free plan sleeps** after ~15 idle minutes (first visit then takes up to a minute).
- **Free plan disk is not persistent**: the SQLite file resets on redeploy/restart. Fine for a demo; for real use
  attach a persistent disk (paid) and set `DB_PATH` to it, or host on PythonAnywhere (files persist).
- **Keep ONE worker.** Queues live in memory; several workers would each hold their own copy.

### Own domain name (e.g. myevents.in)
Buy one from a registrar, then Render -> your service -> Settings -> Custom Domains -> add it and create the
DNS record Render shows (usually a CNAME). HTTPS is issued automatically. A free `onrender.com` address
already counts as an online domain for the demo.

### Alternative: PythonAnywhere
Free accounts can send mail only through Gmail SMTP (use an app password with the `SMTP_*` variables).
Create a Flask web app, point its WSGI file to `from app import app as application`, set env vars in the WSGI file.
Custom domains there need a paid plan.

