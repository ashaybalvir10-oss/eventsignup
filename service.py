"""Business logic: users, events, signups, waitlist promotion.

SQLite is the source of truth. One LinkedQueue per event lives in memory for
fast queue operations and is rebuilt from the DB on startup.
"""
import functools
import os
import secrets
import sqlite3
import threading
from werkzeug.security import check_password_hash, generate_password_hash
from ds import LinkedQueue
from mailer import send_email

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, description TEXT DEFAULT '', location TEXT DEFAULT '',
  starts_at TEXT DEFAULT '', capacity INTEGER NOT NULL CHECK(capacity > 0),
  category TEXT NOT NULL DEFAULT 'other');
CREATE TABLE IF NOT EXISTS signups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK(status IN ('confirmed','waitlisted')),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(event_id, user_id));
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  to_email TEXT, subject TEXT, body TEXT, sent_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


class EventError(Exception):
    pass


def locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self.lock:
            return fn(self, *a, **k)
    return wrapper


class EventService:
    def __init__(self, path=None):
        self.db = sqlite3.connect(path or os.environ.get("DB_PATH", "events.db"),
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Existing Render databases predate categories; add the column in place.
        event_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(events)")}
        if "category" not in event_columns:
            self.db.execute("ALTER TABLE events ADD COLUMN category TEXT NOT NULL DEFAULT 'other'")
            self.db.commit()
        self.lock = threading.RLock()  # one operation at a time -> no double-booking
        self.queues = {}               # event_id -> LinkedQueue
        for r in self.db.execute(
                """SELECT s.event_id, u.email, u.name FROM signups s
                   JOIN users u ON u.id = s.user_id
                   WHERE s.status='waitlisted' ORDER BY s.id"""):
            self.queues.setdefault(r["event_id"], LinkedQueue()).enqueue(r["email"], r["name"])

    def close(self):
        """Close the SQLite connection (primarily useful for clean shutdowns and tests)."""
        with self.lock:
            self.db.close()

    # ---------- users ----------
    def _user(self, uid):
        r = self.db.execute("SELECT id,name,email,is_admin FROM users WHERE id=?", (uid,)).fetchone()
        return {**dict(r), "is_admin": bool(r["is_admin"])} if r else None

    get_user = locked(_user)

    @locked
    def register(self, name, email, password):
        name, email = (name or "").strip(), (email or "").strip().lower()
        if not name or "@" not in email or "." not in email.split("@")[-1]:
            raise EventError("Enter a valid name and email")
        if len(password or "") < 8:
            raise EventError("Password must be at least 8 characters")
        if self.db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise EventError("An account with this email already exists")
        admins = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
        first = self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        is_admin = int(email in admins or (not admins and first))  # no ADMIN_EMAILS -> first user
        cur = self.db.execute("INSERT INTO users(name,email,pw_hash,is_admin) VALUES (?,?,?,?)",
                              (name, email, generate_password_hash(password), is_admin))
        self.db.commit()
        send_email(email, "Welcome to Event Waitlist",
                   f"Hi {name},\n\nYour account is ready. Browse events and join one - "
                   f"if it's full, you'll be added to the waitlist automatically.", self.db)
        return self._user(cur.lastrowid)

    @locked
    def authenticate(self, email, password):
        r = self.db.execute("SELECT id,pw_hash FROM users WHERE email=?",
                            ((email or "").strip().lower(),)).fetchone()
        if not r or not check_password_hash(r["pw_hash"], password or ""):
            raise EventError("Incorrect email or password")
        return self._user(r["id"])

    # ---------- events ----------
    @locked
    def create_event(self, name, description, location, starts_at, capacity, category="other"):
        try:
            capacity = int(capacity)
        except (TypeError, ValueError):
            capacity = 0
        if not (name or "").strip() or capacity < 1:
            raise EventError("Event needs a name and a capacity of at least 1")
        category = (category or "other").strip().lower()
        if category not in {"music", "sports", "academics", "arts", "technology", "community", "other"}:
            raise EventError("Choose a valid event category")
        cur = self.db.execute(
            "INSERT INTO events(name,description,location,starts_at,capacity,category) VALUES (?,?,?,?,?,?)",
            (name.strip(), (description or "").strip(), (location or "").strip(),
             (starts_at or "").strip(), capacity, category))
        self.db.commit()
        self.queues[cur.lastrowid] = LinkedQueue()
        return cur.lastrowid

    def _event(self, eid):
        ev = self.db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not ev:
            raise EventError("Event not found")
        return ev

    def _confirmed(self, eid):
        return self.db.execute("SELECT COUNT(*) FROM signups WHERE event_id=? AND status='confirmed'",
                               (eid,)).fetchone()[0]

    @locked
    def event_status(self, eid):
        ev, confirmed = self._event(eid), self._confirmed(eid)
        q = self.queues.setdefault(eid, LinkedQueue())
        return {"id": eid, "name": ev["name"], "description": ev["description"],
                "location": ev["location"], "starts_at": ev["starts_at"],
                "category": ev["category"], "capacity": ev["capacity"], "confirmed": confirmed,
                "spots_left": ev["capacity"] - confirmed, "waitlist_length": len(q)}

    @locked
    def list_events(self, user_id=None):
        email = self._user(user_id)["email"] if user_id else None
        out = []
        for r in self.db.execute("SELECT id FROM events ORDER BY id DESC").fetchall():
            s = self.event_status(r["id"])
            s["my_status"] = s["my_position"] = None
            if user_id:
                row = self.db.execute("SELECT status FROM signups WHERE event_id=? AND user_id=?",
                                      (r["id"], user_id)).fetchone()
                if row:
                    s["my_status"] = row["status"]
                    if row["status"] == "waitlisted":
                        s["my_position"] = self.queues[r["id"]].position(email)
            out.append(s)
        return out

    # ---------- signup / cancel ----------
    @locked
    def signup(self, user_id, eid):
        user, ev = self._user(user_id), self._event(eid)
        q = self.queues.setdefault(eid, LinkedQueue())
        if self.db.execute("SELECT 1 FROM signups WHERE event_id=? AND user_id=?", (eid, user_id)).fetchone():
            raise EventError("You are already registered for this event")
        where = f"{ev['name']}" + (f" ({ev['starts_at']}, {ev['location']})" if ev["starts_at"] or ev["location"] else "")
        if self._confirmed(eid) < ev["capacity"]:
            status = "confirmed"
        else:
            status = "waitlisted"
            q.enqueue(user["email"], user["name"])       # join the back of the line
        self.db.execute("INSERT INTO signups(event_id,user_id,status) VALUES (?,?,?)", (eid, user_id, status))
        self.db.commit()
        if status == "confirmed":
            send_email(user["email"], f"You're in: {ev['name']}",
                       f"Hi {user['name']},\n\nYour spot is confirmed for {where}.", self.db)
            return {"status": "confirmed"}
        pos = q.position(user["email"])
        send_email(user["email"], f"Waitlisted: {ev['name']}",
                   f"Hi {user['name']},\n\n{where} is full. You are #{pos} on the waitlist. "
                   f"We'll email you the moment a spot opens.", self.db)
        return {"status": "waitlisted", "position": pos}

    @locked
    def cancel(self, user_id, eid):
        user, ev = self._user(user_id), self._event(eid)
        q = self.queues.setdefault(eid, LinkedQueue())
        row = self.db.execute("SELECT status FROM signups WHERE event_id=? AND user_id=?",
                              (eid, user_id)).fetchone()
        if not row:
            raise EventError("You are not registered for this event")
        self.db.execute("DELETE FROM signups WHERE event_id=? AND user_id=?", (eid, user_id))
        promoted = None
        if row["status"] == "waitlisted":
            q.remove(user["email"])                      # leave the line from the middle
        else:
            promoted = self._promote_next(ev, q)
        self.db.commit()
        return {"cancelled": user["email"], "promoted": promoted}

    @locked
    def remove_by_email(self, eid, email):
        r = self.db.execute("SELECT id FROM users WHERE email=?", ((email or "").strip().lower(),)).fetchone()
        if not r:
            raise EventError("No user with that email")
        return self.cancel(r["id"], eid)

    def _promote_next(self, ev, q):
        """A spot opened: dequeue the longest-waiting person, confirm them, email them."""
        nxt = q.dequeue()                                # FIFO: first come, first served
        if not nxt:
            return None
        email, name = nxt
        self.db.execute("""UPDATE signups SET status='confirmed' WHERE event_id=? AND user_id=
                           (SELECT id FROM users WHERE email=?)""", (ev["id"], email))
        send_email(email, f"A spot opened up: {ev['name']}!",
                   f"Hi {name},\n\nGood news - a spot opened and you are now CONFIRMED for "
                   f"{ev['name']}. See you there!", self.db)
        return email

    # ---------- admin helpers ----------
    @locked
    def event_structure(self, eid):
        s = self.event_status(eid)
        confirmed = [dict(r) for r in self.db.execute(
            """SELECT u.name,u.email FROM signups s JOIN users u ON u.id=s.user_id
               WHERE s.event_id=? AND s.status='confirmed' ORDER BY s.id""", (eid,))]
        return {"event": {k: s[k] for k in ("id", "name", "capacity", "confirmed", "spots_left")},
                "confirmed": confirmed, "queue": self.queues.setdefault(eid, LinkedQueue()).snapshot()}

    @locked
    def add_demo(self, eid, n=5):
        self._event(eid)
        names = ["Asha", "Ravi", "Meera", "Karan", "Isha", "Neel", "Tara", "Dev"]
        for _ in range(n):
            nm = secrets.choice(names)
            u = self.register(nm, f"{nm.lower()}{secrets.randbelow(9000) + 1000}@demo.invalid",
                              secrets.token_urlsafe(12))
            self.signup(u["id"], eid)
        return n

    @locked
    def outbox(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 50")]

