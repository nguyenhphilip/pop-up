from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import sqlite3, os, json, logging
from pywebpush import webpush, WebPushException
from dotenv import load_dotenv
from secrets import token_hex
from flask import Response
import queue


load_dotenv()

# ---------- CONFIG ----------
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_CLAIM_EMAIL = os.getenv("VAPID_CLAIM_EMAIL", "mailto:admin@local")
DB_PATH = os.getenv("DB_PATH", "broadcasts.db")

app = Flask(__name__, static_folder="static", static_url_path="")


# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("popup")

# ---------- DATABASE ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database if not exists."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT,
                note TEXT,
                lat REAL,
                lon REAL,
                expires_at TEXT,
                delete_token TEXT,
                duration_hours REAL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT UNIQUE,
                keys TEXT
            );
        """)
        conn.commit()
    log.info("DB initialized (or already present)")

# Initialize DB once at startup
with app.app_context():
    init_db()

@app.after_request
def add_header(response):
    # Avoid caching API responses in edge/CDN which can stale the list
    response.headers["Cache-Control"] = "no-store"
    return response



# Ensure tables exist both locally and under Gunicorn/Render

# ---------- UTIL ----------
def _push_to_all(payload: dict):
    """Send a push message to every subscription; drop dead ones."""
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        log.error("VAPID keys not configured; aborting push.")
        return

    with get_db() as conn:
        subs = conn.execute("SELECT id, endpoint, keys FROM subscriptions").fetchall()

    dead_ids = []
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": json.loads(s["keys"]),
                },
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL},
            )
        except WebPushException as e:
            # Capture common "gone" statuses and clean them up
            status = getattr(e, "response", None)
            code = getattr(status, "status_code", None)
            log.warning("Push failed for id=%s endpoint=%s code=%s error=%r",
                        s["id"], s["endpoint"], code, e)
            if code in (404, 410):
                dead_ids.append(s["id"])
        except Exception as e:
            log.exception("Unexpected push exception: %r", e)

    if dead_ids:
        with get_db() as conn:
            conn.executemany("DELETE FROM subscriptions WHERE id = ?", [(i,) for i in dead_ids])
            conn.commit()
        log.info("Cleaned up %d expired subscriptions", len(dead_ids))


# ---------- SSE BROADCAST CHANNEL ----------
listeners = []

def publish_event(event: str, data: dict):
    """Send an event to all connected SSE listeners."""
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    for q in listeners:
        q.put(msg)

@app.route("/stream")
def stream():
    """Server-Sent Events endpoint for real-time updates."""
    def event_stream(q):
        try:
            while True:
                msg = q.get()
                yield msg
        except GeneratorExit:
            # Client disconnected
            pass

    q = queue.Queue()
    listeners.append(q)
    log.info("Client connected (total: %d)", len(listeners))
    return Response(event_stream(q), mimetype="text/event-stream")



# ---------- ROUTES ----------
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "has_vapid_public": bool(VAPID_PUBLIC_KEY),
        "has_vapid_private": bool(VAPID_PRIVATE_KEY),
    })

@app.route("/vapid_public_key")
def vapid_public_key():
    """Return the public VAPID key to clients for subscription."""
    if not VAPID_PUBLIC_KEY:
        return jsonify({"error": "VAPID_PUBLIC_KEY is not configured"}), 500
    return jsonify({"key": VAPID_PUBLIC_KEY})

@app.route("/subscribe", methods=["POST"])
def subscribe():
    """Store push subscription information."""
    sub = request.get_json(silent=True) or {}
    endpoint = sub.get("endpoint")
    keys = sub.get("keys")

    if not endpoint or not keys:
        return jsonify({"error": "Malformed subscription"}), 400

    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (endpoint, keys) VALUES (?, ?)",
            (endpoint, json.dumps(keys)),
        )
        conn.commit()

    log.info("Subscription stored: %s", endpoint)
    return jsonify({"status": "subscribed"})

@app.route("/broadcasts", methods=["POST"])
def create_broadcast():
    """Create a new broadcast and send notifications."""
    data = request.get_json(silent=True) or {}

    user = (data.get("user") or "").strip()
    note = (data.get("note") or "").strip()
    if not user or not note:
        return jsonify({"error": "Missing user or note"}), 400

    duration = data.get("duration_hours")
    duration_hours = None if duration is None else float(duration)

    # For unspecified, we still set an internal expiry (12h)
    hours = 12 if duration_hours is None else duration_hours
    expires_at = (datetime.utcnow() + timedelta(hours=hours)).isoformat()

    delete_token = token_hex(16)

    with get_db() as conn:
        conn.execute("""
            INSERT INTO broadcasts (user, note, lat, lon, expires_at, delete_token, duration_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user,
            note,
            data.get("lat"),
            data.get("lon"),
            expires_at,
            delete_token,
            duration_hours
        ))
        conn.commit()

    # Fire the push
    _push_to_all({
        "title": "Someone’s out!",
        "body": f"{user} — {note}"
    })

    publish_event("new_broadcast", {"user": user, "note": note, "expires_at": expires_at})

    return jsonify({"status": "ok", "delete_token": delete_token}), 201

@app.route("/broadcasts", methods=["GET"])
def list_broadcasts():
    """Return all current non-expired broadcasts."""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM broadcasts
            WHERE expires_at > ?
            ORDER BY expires_at
        """, (now,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/delete_broadcast", methods=["POST"])
def delete_broadcast():
    token = (request.get_json(silent=True) or {}).get("delete_token")
    if not token:
        return jsonify({"error": "Missing delete_token"}), 400

    with get_db() as conn:
        cur = conn.execute("DELETE FROM broadcasts WHERE delete_token = ?", (token,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Invalid or expired token"}), 404

    return jsonify({"status": "deleted"})

# --- Push diagnostics: server-side trigger ---
@app.route("/test_push")
def test_push():
    _push_to_all({
        "title": "Push test",
        "body": "If you see this, push works end-to-end.",
    })
    return jsonify({"status": "sent"})

@app.route("/")
def serve_index():
    # index.html is served from /static/index.html
    return app.send_static_file("index.html")

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
