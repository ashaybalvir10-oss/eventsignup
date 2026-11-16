import os
import secrets
from datetime import timedelta
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from ds import RateLimiter
from service import EventError, EventService

app = Flask(__name__, static_folder="static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # correct client IP / https behind a host's proxy
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),  # set SECRET_KEY in production!
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PRODUCTION") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7))

svc = EventService()
login_limiter = RateLimiter(limit=5, window_seconds=300)   # 5 failed logins / 5 min


def body():
    # JSON only: a cross-site HTML form can't send this, which blocks CSRF on our POSTs
    return request.get_json(silent=True) or {}


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        u = svc.get_user(session.get("uid")) if session.get("uid") else None
        if not u:
            return jsonify(error="Please sign in"), 401
        return f(u, *a, **k)
    return w


def admin_required(f):
    @wraps(f)
    @login_required
    def w(u, *a, **k):
        if not u["is_admin"]:
            return jsonify(error="Admins only"), 403
        return f(u, *a, **k)
    return w


@app.errorhandler(EventError)
def handle_err(e):
    return jsonify(error=str(e)), 400


# ---------- pages ----------
@app.get("/")
def home():
    return send_from_directory("static", "index.html")


@app.get("/visualizer")
def visualizer():
    return send_from_directory("static", "visualizer.html")


@app.get("/healthz")
def health():
    return "ok"


@app.get("/api/email-status")
def email_status():
    # Report readiness without ever exposing a key or sender credential.
    if os.environ.get("BREVO_API_KEY") and os.environ.get("MAIL_FROM"):
        return jsonify(configured=True, provider="Brevo")
    if (os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER")
            and os.environ.get("SMTP_PASS")):
        return jsonify(configured=True, provider="SMTP")
    return jsonify(configured=False, provider="console")


# ---------- auth ----------
@app.post("/api/register")
def register():
    d = body()
    user = svc.register(d.get("name"), d.get("email"), d.get("password"))
    session.clear(); session["uid"] = user["id"]; session.permanent = True
    return jsonify(user=user), 201


@app.post("/api/login")
def login():
    d = body()
    key = f"{request.remote_addr}:{(d.get('email') or '').lower()}"
    if not login_limiter.allow(key):
        return jsonify(error="Too many failed attempts. Try again in a few minutes."), 429
    try:
        user = svc.authenticate(d.get("email"), d.get("password"))
    except EventError:
        login_limiter.record(key)
        raise
    login_limiter.clear(key)
    session.clear(); session["uid"] = user["id"]; session.permanent = True
    return jsonify(user=user)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    uid = session.get("uid")
    return jsonify(user=svc.get_user(uid) if uid else None)


# ---------- events ----------
@app.get("/api/events")
def events():
    return jsonify(svc.list_events(session.get("uid")))


@app.post("/api/events")
@admin_required
def create_event(u):
    d = body()
    eid = svc.create_event(d.get("name"), d.get("description"), d.get("location"),
                           d.get("starts_at"), d.get("capacity"), d.get("category"))
    return jsonify(id=eid), 201


@app.post("/api/events/<int:eid>/signup")
@login_required
def signup(u, eid):
    return jsonify(svc.signup(u["id"], eid)), 201


@app.post("/api/events/<int:eid>/cancel")
@login_required
def cancel(u, eid):
    return jsonify(svc.cancel(u["id"], eid))


# ---------- admin ----------
@app.get("/api/events/<int:eid>/structure")
@admin_required
def structure(u, eid):
    return jsonify(svc.event_structure(eid))


@app.post("/api/events/<int:eid>/remove")
@admin_required
def remove(u, eid):
    return jsonify(svc.remove_by_email(eid, body().get("email")))


@app.post("/api/events/<int:eid>/demo")
@admin_required
def demo(u, eid):
    return jsonify(added=svc.add_demo(eid))


@app.get("/api/outbox")
@admin_required
def outbox(u):
    return jsonify(svc.outbox())


if __name__ == "__main__":
    app.run(debug=True, port=5000)

