from flask import Flask, request, jsonify, Response
from datetime import datetime, timedelta, timezone
import sqlite3, json, queue, threading, time
from secrets import token_hex
import os

app = Flask(__name__, static_folder="static", static_url_path="")
DB_PATH = os.getenv("DB_PATH", "broadcasts.db")

# ---------- DATABASE ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
        """)
        conn.commit()

with app.app_context():
    init_db()

# ---------- SSE CHANNEL ----------
listeners = []

def publish_event(event: str, data: dict):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    for q in listeners:
        q.put(msg)

@app.route("/stream")
def stream():
    def event_stream(q):
        try:
            while True:
                msg = q.get()
                yield msg
        except GeneratorExit:
            pass

    q = queue.Queue()
    listeners.append(q)
    return Response(event_stream(q), mimetype="text/event-stream")

# ---------- ROUTES ----------
@app.route("/broadcasts", methods=["POST"])
def create_broadcast():
    data = request.get_json(silent=True) or {}
    user = (data.get("user") or "").strip()
    note = (data.get("note") or "").strip()
    if not user or not note:
        return jsonify({"error": "Missing user or note"}), 400

    duration = data.get("duration_hours")
    duration_hours = None if duration is None else float(duration)
    hours = 12 if duration_hours is None else duration_hours
    
    expires_at_dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    expires_at = expires_at_dt.strftime('%Y-%m-%dT%H:%M:%SZ')

    delete_token = token_hex(16)

    with get_db() as conn:
        conn.execute("""
            INSERT INTO broadcasts (user, note, lat, lon, expires_at, delete_token, duration_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user, note, data.get("lat"), data.get("lon"), expires_at, delete_token, duration_hours))
        conn.commit()

    publish_event("new_broadcast", {"user": user, "note": note, "expires_at": expires_at})
    return jsonify({"status": "ok", "delete_token": delete_token}), 201

@app.route("/broadcasts", methods=["GET"])
def list_broadcasts():
    now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM broadcasts
            WHERE expires_at > ?
            ORDER BY expires_at
        """, (now_utc,)).fetchall()
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

    publish_event("refresh", {"action": "deleted"})
    return jsonify({"status": "deleted"})

@app.route("/")
def serve_index():
    return app.send_static_file("index.html")

# ---------- AUTO-CLEANUP JOB ----------
def cleanup_expired_broadcasts(interval_hours=1):
    while True:
        time.sleep(interval_hours * 3600)
        try:
            cutoff = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            with get_db() as conn:
                conn.execute("DELETE FROM broadcasts WHERE expires_at < ?", (cutoff,))
                conn.commit()
            print(f"[CLEANUP] Expired broadcasts removed at {cutoff}")
        except Exception as e:
            print("[CLEANUP ERROR]", e)

# Start background cleanup thread
cleanup_thread = threading.Thread(target=cleanup_expired_broadcasts, daemon=True)
cleanup_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
