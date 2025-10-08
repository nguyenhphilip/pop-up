from flask import Flask, request, jsonify, Response
from datetime import UTC
from datetime import datetime, timedelta
from threading import Lock
from twilio.rest import Client
import os
import json
import time
import uuid
from dotenv import load_dotenv

load_dotenv()

TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM")


app = Flask(__name__)


@app.route("/")
def serve_index():
    return app.send_static_file("index.html")

# ----------------------------
# TWILIO SETUP
# ----------------------------
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM")

if TWILIO_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM:
    twilio_client = Client(TWILIO_SID, TWILIO_AUTH_TOKEN)
else:
    twilio_client = None
    print("⚠️ Twilio not configured — SMS disabled.")

# ----------------------------
# GLOBAL STORAGE (in-memory)
# ----------------------------
broadcasts = []
subscribers = set()
listeners = []
lock = Lock()


# ----------------------------
# UTILITIES
# ----------------------------
def send_sms_to_all(message: str):
    if not twilio_client:
        print("SMS skipped (Twilio not configured).")
        return

    for phone in list(subscribers):
        try:
            twilio_client.messages.create(to=phone, from_=TWILIO_FROM, body=message)
            print(f"✅ Sent SMS to {phone}")
        except Exception as e:
            print(f"❌ Failed to send to {phone}: {e}")


def broadcast_event(event_name: str):
    """Push an SSE update to all connected browsers."""
    with lock:
        dead = []
        for q in listeners:
            try:
                q.put(event_name)
            except Exception:
                dead.append(q)
        for d in dead:
            listeners.remove(d)


# ----------------------------
# ROUTES
# ----------------------------

@app.route("/broadcasts", methods=["GET"])
def get_broadcasts():
    now = datetime.now(UTC)
    valid = [b for b in broadcasts if b["expires_at"] > now]
    return jsonify(valid)


@app.route("/broadcasts", methods=["POST"])
def post_broadcast():
    data = request.get_json()
    user = data.get("user")
    note = data.get("note")
    duration_hours = data.get("duration_hours")
    lat = data.get("lat")
    lon = data.get("lon")
    device_id = data.get("device_id")

    if not user or not note:
        return jsonify({"error": "Missing name or description."}), 400

    # Each device can only have one active broadcast
    existing = next((b for b in broadcasts if b.get("device_id") == device_id), None)
    if existing:
        return jsonify({"error": "You already have an active broadcast."}), 400

    hours = duration_hours if duration_hours else 12
    expires_at = datetime.now(UTC) + timedelta(hours=hours)
    delete_token = str(uuid.uuid4())

    broadcast = {
        "id": str(uuid.uuid4()),
        "user": user,
        "note": note,
        "lat": lat,
        "lon": lon,
        "duration_hours": duration_hours,
        "expires_at": expires_at,
        "device_id": device_id,
        "delete_token": delete_token,
    }

    broadcasts.append(broadcast)

    # Send SMS to all subscribers
    msg = f"📣 New pop-up from {user}: {note}"
    send_sms_to_all(msg)

    # Notify connected browsers via SSE
    broadcast_event("new_broadcast")

    return jsonify({"delete_token": delete_token, "expires_at": expires_at.isoformat()})


@app.route("/delete_broadcast", methods=["POST"])
def delete_broadcast():
    data = request.get_json()
    token = data.get("delete_token")
    if not token:
        return jsonify({"error": "Missing token"}), 400

    removed = False
    for b in list(broadcasts):
        if b["delete_token"] == token:
            broadcasts.remove(b)
            removed = True
            break

    if removed:
        broadcast_event("refresh")
        return jsonify({"message": "Broadcast removed."})
    else:
        return jsonify({"error": "Broadcast not found."}), 404


# ----------------------------
# SMS SUBSCRIPTION ROUTES
# ----------------------------
@app.route("/subscribe", methods=["POST"])
def subscribe():
    data = request.get_json()
    phone = data.get("phone")
    if not phone:
        return jsonify({"error": "Phone number required"}), 400
    subscribers.add(phone)
    return jsonify({"message": f"Subscribed {phone} for text alerts."})


@app.route("/unsubscribe", methods=["POST"])
def unsubscribe():
    data = request.get_json()
    phone = data.get("phone")
    if not phone:
        return jsonify({"error": "Phone number required"}), 400
    subscribers.discard(phone)
    return jsonify({"message": f"Unsubscribed {phone}."})


# ----------------------------
# SSE STREAM
# ----------------------------
@app.route("/stream")
def stream():
    def event_stream(q):
        while True:
            event = q.get()
            yield f"event: {event}\ndata: update\n\n"

    import queue
    q = queue.Queue()
    with lock:
        listeners.append(q)
    return Response(event_stream(q), mimetype="text/event-stream")


# ----------------------------
# AUTO CLEANUP TASK
# ----------------------------
@app.before_request
def cleanup():
    now = datetime.now(UTC)
    before = len(broadcasts)
    broadcasts[:] = [b for b in broadcasts if b["expires_at"] > now]
    if len(broadcasts) != before:
        broadcast_event("refresh")


# ----------------------------
# ENTRY POINT
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
