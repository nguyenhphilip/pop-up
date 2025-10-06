from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime, timedelta
import sqlite3, os, json
from pywebpush import webpush, WebPushException
from dotenv import load_dotenv
from secrets import token_hex

# Load .env
load_dotenv()


app = Flask(__name__, static_folder="static", static_url_path="")


# ---------- CONFIG ----------
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_CLAIM_EMAIL = os.getenv("VAPID_CLAIM_EMAIL", "mailto:admin@local")
DB_PATH = "broadcasts.db"


@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-store"
    return response


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

# ---------- ROUTES ----------

@app.route("/vapid_public_key")
def vapid_public_key():
    """Return the public VAPID key to clients for subscription."""
    return jsonify({"key": VAPID_PUBLIC_KEY})


@app.route("/subscribe", methods=["POST"])
def subscribe():
    """Store push subscription information."""
    sub = request.json
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (endpoint, keys) VALUES (?, ?)",
            (sub["endpoint"], json.dumps(sub["keys"]))
        )
        conn.commit()
    return jsonify({"status": "subscribed"})


@app.route("/broadcasts", methods=["POST"])
def create_broadcast():
    """Create a new broadcast and send notifications."""
    data = request.json

    # Handle duration (may be None for "unspecified")
    duration = data.get("duration_hours")
    duration_hours = None if duration is None else float(duration)

    # Even if unspecified, set an internal expiry (12 hours)
    hours = 12 if duration_hours is None else duration_hours
    expires_at = (datetime.utcnow() + timedelta(hours=hours)).isoformat()

    # Create a unique token for later deletion
    delete_token = token_hex(16)

    # Save broadcast
    with get_db() as conn:
        conn.execute("""
            INSERT INTO broadcasts (user, note, lat, lon, expires_at, delete_token, duration_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            data["user"],
            data.get("note", ""),
            data.get("lat"),
            data.get("lon"),
            expires_at,
            delete_token,
            duration_hours
        ))
        conn.commit()

    # Send push notifications
    payload = json.dumps({
        "title": "Someone’s out!",
        "body": f"{data['user']} — {data.get('note', '')}"
    })

    with get_db() as conn:
        subs = conn.execute("SELECT endpoint, keys FROM subscriptions").fetchall()

    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s["endpoint"],
                    "keys": json.loads(s["keys"])
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIM_EMAIL}
            )
        except WebPushException as e:
            print("Push failed:", repr(e))

    # Return token to client
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
    """Delete a broadcast using the user's unique token."""
    token = request.json.get("delete_token")
    if not token:
        return jsonify({"error": "Missing delete_token"}), 400

    with get_db() as conn:
        cur = conn.execute("DELETE FROM broadcasts WHERE delete_token = ?", (token,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Invalid or expired token"}), 404

    return jsonify({"status": "deleted"}), 200


@app.route("/")
def serve_index():
    return app.send_static_file("index.html")

# ---------- MAIN ----------
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
