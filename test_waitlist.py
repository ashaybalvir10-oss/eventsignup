import contextlib, io, os, tempfile, unittest
from unittest.mock import patch

DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = DB
os.environ.pop("ADMIN_EMAILS", None)

from ds import LinkedQueue, RateLimiter
from service import EventService, EventError


def setUpModule():  # keep console email prints out of test output
    global _quiet
    _quiet = contextlib.redirect_stdout(io.StringIO()); _quiet.__enter__()


def tearDownModule():
    _quiet.__exit__(None, None, None)
    os.path.exists(DB) and os.remove(DB)


class TestDataStructures(unittest.TestCase):
    def test_queue_fifo_remove(self):
        q = LinkedQueue()
        for k in "abc":
            q.enqueue(k, k.upper())
        self.assertEqual(q.position("c"), 3)
        q.remove("b")
        self.assertEqual(q.dequeue(), ("a", "A"))
        self.assertEqual(q.dequeue(), ("c", "C"))
        self.assertIsNone(q.dequeue())

    def test_queue_duplicate(self):
        q = LinkedQueue(); q.enqueue("x")
        with self.assertRaises(ValueError):
            q.enqueue("x")

    def test_snapshot(self):
        q = LinkedQueue()
        for k in "abc":
            q.enqueue(k, k)
        q.remove("b")
        s = q.snapshot()
        self.assertEqual((s["head"], s["tail"], s["size"]), ("a", "c", 2))
        self.assertEqual([h["op"] for h in s["history"]], ["enqueue"] * 3 + ["remove"])

    def test_rate_limiter_window(self):
        r = RateLimiter(3, 60)
        for t in (0, 1, 2):
            r.record("k", now=t)
        self.assertFalse(r.allow("k", now=10))
        self.assertTrue(r.allow("k", now=70))   # old hits expired
        r.record("k", now=70); r.clear("k")
        self.assertTrue(r.allow("k", now=71))


class TestService(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.s = EventService(self.path)
        self.admin = self.s.register("Admin", "admin@x.com", "password123")
        self.eid = self.s.create_event("Hackathon", "", "Pune", "2026-12-01 10:00", 2)
        self.u = {n: self.s.register(n, f"{n}@x.com", "password123") for n in "abcd"}

    def tearDown(self):
        self.s.close()
        os.path.exists(self.path) and os.remove(self.path)

    def test_first_user_is_admin_only(self):
        self.assertTrue(self.admin["is_admin"])
        self.assertFalse(self.u["a"]["is_admin"])

    def test_auth(self):
        self.assertEqual(self.s.authenticate("A@x.com", "password123")["id"], self.u["a"]["id"])
        with self.assertRaises(EventError):
            self.s.authenticate("a@x.com", "wrong-password")
        with self.assertRaises(EventError):
            self.s.register("Dup", "a@x.com", "password123")
        with self.assertRaises(EventError):
            self.s.register("Short", "z@x.com", "short")
        stored = self.s.db.execute("SELECT pw_hash FROM users WHERE email='a@x.com'").fetchone()[0]
        self.assertNotIn("password123", stored)       # hashed, not plaintext

    def test_waitlist_and_promotion(self):
        ids = {n: self.u[n]["id"] for n in "abcd"}
        self.assertEqual(self.s.signup(ids["a"], self.eid)["status"], "confirmed")
        self.assertEqual(self.s.signup(ids["b"], self.eid)["status"], "confirmed")
        self.assertEqual(self.s.signup(ids["c"], self.eid)["position"], 1)
        self.assertEqual(self.s.signup(ids["d"], self.eid)["position"], 2)
        self.assertEqual(self.s.cancel(ids["a"], self.eid)["promoted"], "c@x.com")
        ev = self.s.list_events(ids["d"])[0]
        self.assertEqual((ev["confirmed"], ev["waitlist_length"], ev["my_position"]), (2, 1, 1))
        self.assertTrue(any("spot opened" in m["subject"] for m in self.s.outbox()))

    def test_event_category_is_saved_and_validated(self):
        music_id = self.s.create_event("Open Mic", "", "Hall", "", 20, "music")
        event = next(e for e in self.s.list_events() if e["id"] == music_id)
        self.assertEqual(event["category"], "music")
        with self.assertRaises(EventError):
            self.s.create_event("Mystery", "", "", "", 5, "not-a-category")

    def test_existing_database_gets_category_column(self):
        legacy_path = tempfile.mktemp(suffix=".db")
        import sqlite3
        db = sqlite3.connect(legacy_path)
        db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,description TEXT DEFAULT '',location TEXT DEFAULT '',starts_at TEXT DEFAULT '',capacity INTEGER NOT NULL CHECK(capacity > 0))")
        db.execute("INSERT INTO events(name,capacity) VALUES('Old event',10)")
        db.commit(); db.close()
        migrated = EventService(legacy_path)
        event = migrated.list_events()[0]
        self.assertEqual(event["category"], "other")
        migrated.db.close()
        os.remove(legacy_path)

    def test_leave_waitlist_and_duplicate(self):
        ids = [self.u[n]["id"] for n in "abc"]
        for i in ids:
            self.s.signup(i, self.eid)
        self.assertIsNone(self.s.cancel(ids[2], self.eid)["promoted"])
        with self.assertRaises(EventError):
            self.s.signup(ids[0], self.eid)

    def test_queue_survives_restart(self):
        for n in "abcd":
            self.s.signup(self.u[n]["id"], self.eid)
        s2 = EventService(self.path)
        self.assertEqual([x[0] for x in s2.queues[self.eid].to_list()], ["c@x.com", "d@x.com"])
        s2.close()


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["ADMIN_EMAILS"] = "boss@x.com"      # also tests the ADMIN_EMAILS setting
        import app as appmod
        cls.appmod = appmod
        cls.app = appmod.app

    @classmethod
    def tearDownClass(cls):
        cls.appmod.svc.close()
        os.environ.pop("ADMIN_EMAILS", None)

    def client(self):
        return self.app.test_client()

    def test_permissions_and_flow(self):
        admin, user, anon = self.client(), self.client(), self.client()
        r = admin.post("/api/register", json={"name": "Boss", "email": "boss@x.com", "password": "password123"})
        self.assertEqual(r.status_code, 201)
        self.assertTrue(r.get_json()["user"]["is_admin"])
        created = admin.post("/api/events", json={"name": "Meetup", "capacity": 1, "category": "sports"})
        eid = created.get_json()["id"]
        self.assertEqual(admin.get("/api/events").get_json()[0]["category"], "sports")
        self.assertEqual(anon.get("/api/email-status").status_code, 200)

        user.post("/api/register", json={"name": "Una", "email": "una@x.com", "password": "password123"})
        self.assertEqual(user.post("/api/events", json={"name": "Hack", "capacity": 5}).status_code, 403)
        self.assertEqual(anon.post(f"/api/events/{eid}/signup", json={}).status_code, 401)
        self.assertEqual(anon.get("/api/events").status_code, 200)           # browsing is public

        self.assertEqual(admin.post(f"/api/events/{eid}/signup", json={}).get_json()["status"], "confirmed")
        r = user.post(f"/api/events/{eid}/signup", json={}).get_json()
        self.assertEqual((r["status"], r["position"]), ("waitlisted", 1))
        self.assertEqual(user.get(f"/api/events/{eid}/structure").status_code, 403)
        self.assertEqual(admin.get(f"/api/events/{eid}/structure").status_code, 200)
        self.assertEqual(admin.post(f"/api/events/{eid}/cancel", json={}).get_json()["promoted"], "una@x.com")

    def test_csrf_style_form_post_rejected(self):
        c = self.client()
        r = c.post("/api/login", data={"email": "x@x.com", "password": "password123"})  # not JSON
        self.assertEqual(r.status_code, 400)

    def test_email_status_reports_provider_without_credentials(self):
        with patch.dict(os.environ, {"BREVO_API_KEY": "test-secret", "MAIL_FROM": "sender@example.test"}):
            response = self.client().get("/api/email-status")
        self.assertEqual(response.get_json(), {"configured": True, "provider": "Brevo"})

    def test_login_lockout(self):
        c = self.client()
        c.post("/api/register", json={"name": "Lee", "email": "lee@x.com", "password": "password123"})
        codes = [c.post("/api/login", json={"email": "lee@x.com", "password": "nope-nope"}).status_code
                 for _ in range(6)]
        self.assertEqual(codes[:5], [400] * 5)
        self.assertEqual(codes[5], 429)
        self.assertEqual(c.post("/api/login", json={"email": "lee@x.com", "password": "password123"}).status_code, 429)


if __name__ == "__main__":
    unittest.main()

