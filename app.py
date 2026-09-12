import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "change-this-secret-key"
)

DATABASE = "app.db"

LINK_MINUTES = {
    "3m": 3,
    "3h": 180,
}

LIVE_MINUTES = 30


# =========================
# DATABASE
# =========================

def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            duration TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            selected_option TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS location_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER UNIQUE NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            accuracy REAL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(link_id) REFERENCES links(id)
        )
    """)

    conn.commit()
    conn.close()


def refresh_expired():
    conn = db()
    current = now_utc()

    rows = conn.execute("""
        SELECT id, status, expires_at, used_at
        FROM links
        WHERE status IN ('ACTIVE', 'LIVE')
    """).fetchall()

    for row in rows:
        expires = parse_time(row["expires_at"])

        if expires and expires <= current:
            conn.execute("""
                UPDATE links
                SET status = 'EXPIRED'
                WHERE id = ?
            """, (row["id"],))
            continue

        if row["status"] == "LIVE" and row["used_at"]:
            started = parse_time(row["used_at"])

            if started and started + timedelta(minutes=LIVE_MINUTES) <= current:
                conn.execute("""
                    UPDATE links
                    SET status = 'LOCATION_RECEIVED'
                    WHERE id = ?
                """, (row["id"],))

    conn.commit()
    conn.close()


init_db()


# =========================
# ADMIN AUTH
# =========================

def admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return wrapper


# =========================
# SECURITY HEADERS
# =========================

@app.after_request
def security_headers(response):
    response.headers["Permissions-Policy"] = "geolocation=(self)"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


# =========================
# MAIN
# =========================

@app.get("/")
def home():
    return redirect(url_for("login"))


# =========================
# LOGIN
# =========================

@app.route("/login", methods=["GET", "POST"])
def login():

    error = None

    if request.method == "POST":

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        correct_username = os.environ.get("ADMIN_USERNAME", "admin")
        correct_password = os.environ.get("ADMIN_PASSWORD", "admin123")

        if username == correct_username and password == correct_password:
            session.clear()
            session["admin_logged_in"] = True

            return redirect(url_for("admin"))

        error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================
# ADMIN PAGE
# =========================

@app.get("/admin")
@admin_required
def admin():
    return render_template("admin.html")


# =========================
# CREATE LINK
# =========================

@app.post("/api/links")
@admin_required
def create_link():

    refresh_expired()

    data = request.get_json(silent=True) or {}

    duration = data.get("duration")

    if duration not in LINK_MINUTES:
        return jsonify({
            "success": False,
            "error": "Invalid duration"
        }), 400

    minutes = LINK_MINUTES[duration]

    token = secrets.token_urlsafe(32)

    created = now_utc()
    expires = created + timedelta(minutes=minutes)

    conn = db()

    conn.execute("""
        INSERT INTO links (
            token,
            duration,
            status,
            created_at,
            expires_at
        )
        VALUES (?, ?, 'ACTIVE', ?, ?)
    """, (
        token,
        duration,
        created.isoformat(),
        expires.isoformat()
    ))

    conn.commit()
    conn.close()

    base_url = request.host_url.rstrip("/")
    link = f"{base_url}/r/{token}"

    return jsonify({
        "success": True,
        "token": token,
        "url": link,
        "link": link,
        "duration": duration,
        "expires_at": expires.isoformat(),
        "status": "ACTIVE"
    })


# =========================
# ADMIN LINKS
# =========================

@app.get("/api/links")
@admin_required
def list_links():

    refresh_expired()

    conn = db()

    rows = conn.execute("""
        SELECT
            l.id,
            l.token,
            l.duration,
            l.status,
            l.created_at,
            l.expires_at,
            l.used_at,
            l.selected_option,
            ls.latitude,
            ls.longitude,
            ls.accuracy,
            ls.updated_at
        FROM links l
        LEFT JOIN location_shares ls
            ON ls.link_id = l.id
        ORDER BY l.id DESC
    """).fetchall()

    conn.close()

    base_url = request.host_url.rstrip("/")

    result = []

    for row in rows:

        item = {
            "id": row["id"],
            "token": row["token"],
            "duration": row["duration"],
            "status": row["status"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "used_at": row["used_at"],
            "selected_option": row["selected_option"],
            "url": f"{base_url}/r/{row['token']}",
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "accuracy": row["accuracy"],
            "updated_at": row["updated_at"],
        }

        result.append(item)

    return jsonify(result)


# =========================
# ADMIN STATS
# =========================

@app.get("/api/stats")
@admin_required
def stats():

    refresh_expired()

    conn = db()

    total = conn.execute("""
        SELECT COUNT(*) FROM links
    """).fetchone()[0]

    active = conn.execute("""
        SELECT COUNT(*) FROM links
        WHERE status = 'ACTIVE'
    """).fetchone()[0]

    live = conn.execute("""
        SELECT COUNT(*) FROM links
        WHERE status = 'LIVE'
    """).fetchone()[0]

    received = conn.execute("""
        SELECT COUNT(*) FROM links
        WHERE status IN ('LIVE', 'LOCATION_RECEIVED')
    """).fetchone()[0]

    declined = conn.execute("""
        SELECT COUNT(*) FROM links
        WHERE status = 'DECLINED'
    """).fetchone()[0]

    expired = conn.execute("""
        SELECT COUNT(*) FROM links
        WHERE status = 'EXPIRED'
    """).fetchone()[0]

    conn.close()

    return jsonify({
        "total": total,
        "active": active,
        "live": live,
        "received": received,
        "declined": declined,
        "expired": expired
    })


# =========================
# RECIPIENT PAGE
# =========================

@app.get("/r/<token>")
def recipient(token):

    refresh_expired()

    conn = db()

    link = conn.execute("""
        SELECT *
        FROM links
        WHERE token = ?
    """, (token,)).fetchone()

    conn.close()

    if not link:
        return render_template(
            "expired.html",
            message="This link does not exist."
        ), 404

    expires = parse_time(link["expires_at"])

    if expires and expires <= now_utc():
        return render_template(
            "expired.html",
            message="This link has expired."
        ), 410

    if link["status"] != "ACTIVE":
        return render_template(
            "expired.html",
            message="This link has already been used."
        ), 410

    return render_template(
        "recipient.html",
        token=token
    )


# =========================
# FIRST RESPONSE
# =========================

@app.post("/api/r/<token>/respond")
def respond(token):

    refresh_expired()

    data = request.get_json(silent=True) or {}

    choice = data.get("choice")

    if choice not in ("armenia", "yerevan", "decline"):
        return jsonify({
            "success": False,
            "error": "Invalid choice"
        }), 400

    conn = db()

    link = conn.execute("""
        SELECT *
        FROM links
        WHERE token = ?
    """, (token,)).fetchone()

    if not link:
        conn.close()

        return jsonify({
            "success": False,
            "error": "Link not found"
        }), 404

    if link["status"] != "ACTIVE":
        conn.close()

        return jsonify({
            "success": False,
            "error": "This link has already been used"
        }), 409

    expires = parse_time(link["expires_at"])

    if expires and expires <= now_utc():

        conn.execute("""
            UPDATE links
            SET status = 'EXPIRED'
            WHERE id = ?
        """, (link["id"],))

        conn.commit()
        conn.close()

        return jsonify({
            "success": False,
            "error": "Link expired"
        }), 410

    # =====================
    # DECLINE
    # =====================

    if choice == "decline":

        conn.execute("""
            UPDATE links
            SET
                status = 'DECLINED',
                selected_option = 'decline',
                used_at = ?
            WHERE id = ?
        """, (
            now_iso(),
            link["id"]
        ))

        conn.commit()
        conn.close()

        return jsonify({
            "success": True,
            "status": "DECLINED"
        })

    # =====================
    # LOCATION REQUIRED
    # =====================

    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))

        accuracy_value = data.get("accuracy")
        accuracy = float(accuracy_value) if accuracy_value is not None else None

    except (TypeError, ValueError):

        conn.close()

        return jsonify({
            "success": False,
            "error": "Valid location is required"
        }), 400

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):

        conn.close()

        return jsonify({
            "success": False,
            "error": "Invalid coordinates"
        }), 400

    started = now_utc().isoformat()

    conn.execute("""
        UPDATE links
        SET
            status = 'LIVE',
            selected_option = ?,
            used_at = ?
        WHERE id = ?
    """, (
        choice,
        started,
        link["id"]
    ))

    # One location row per link.
    # It will be updated during live tracking.
    conn.execute("""
        INSERT INTO location_shares (
            link_id,
            latitude,
            longitude,
            accuracy,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(link_id)
        DO UPDATE SET
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            accuracy = excluded.accuracy,
            updated_at = excluded.updated_at
    """, (
        link["id"],
        latitude,
        longitude,
        accuracy,
        started
    ))

    conn.commit()
    conn.close()

    # Tie live tracking to this browser session.
    session["live_token"] = token
    session["live_started_at"] = started

    if choice == "armenia":
        video = "video_armenia.mp4"
    else:
        video = "video_yerevan.mp4"

    return jsonify({
        "success": True,
        "status": "LIVE",
        "choice": choice,
        "video": video,
        "live_minutes": LIVE_MINUTES,
        "message": "Location sharing is active for up to 30 minutes."
    })


# =========================
# LIVE LOCATION
# =========================

@app.post("/api/r/<token>/location")
def live_location(token):

    refresh_expired()

    # Only the browser session that accepted the request
    # can continue sending live coordinates.
    if session.get("live_token") != token:
        return jsonify({
            "success": False,
            "error": "Live location session not found"
        }), 403

    conn = db()

    link = conn.execute("""
        SELECT *
        FROM links
        WHERE token = ?
    """, (token,)).fetchone()

    if not link:
        conn.close()

        return jsonify({
            "success": False,
            "error": "Link not found"
        }), 404

    # Original link expiration has priority.
    expires = parse_time(link["expires_at"])

    if expires and expires <= now_utc():

        conn.execute("""
            UPDATE links
            SET status = 'EXPIRED'
            WHERE id = ?
        """, (link["id"],))

        conn.commit()
        conn.close()

        session.pop("live_token", None)
        session.pop("live_started_at", None)

        return jsonify({
            "success": False,
            "error": "Link expired"
        }), 410

    if link["status"] != "LIVE":
        conn.close()

        session.pop("live_token", None)
        session.pop("live_started_at", None)

        return jsonify({
            "success": False,
            "error": "Live sharing is no longer active"
        }), 410

    # Maximum live sharing = 30 minutes.
    if link["used_at"]:

        started = parse_time(link["used_at"])

        if started and started + timedelta(minutes=LIVE_MINUTES) <= now_utc():

            conn.execute("""
                UPDATE links
                SET status = 'LOCATION_RECEIVED'
                WHERE id = ?
            """, (link["id"],))

            conn.commit()
            conn.close()

            session.pop("live_token", None)
            session.pop("live_started_at", None)

            return jsonify({
                "success": False,
                "error": "30 minute live sharing period ended"
            }), 410

    data = request.get_json(silent=True) or {}

    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))

        accuracy_value = data.get("accuracy")
        accuracy = float(accuracy_value) if accuracy_value is not None else None

    except (TypeError, ValueError):

        conn.close()

        return jsonify({
            "success": False,
            "error": "Invalid location data"
        }), 400

    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):

        conn.close()

        return jsonify({
            "success": False,
            "error": "Invalid coordinates"
        }), 400

    updated = now_iso()

    conn.execute("""
        UPDATE location_shares
        SET
            latitude = ?,
            longitude = ?,
            accuracy = ?,
            updated_at = ?
        WHERE link_id = ?
    """, (
        latitude,
        longitude,
        accuracy,
        updated,
        link["id"]
    ))

    conn.commit()
    conn.close()

    return jsonify({
        "success": True,
        "status": "LIVE",
        "updated_at": updated
    })


# =========================
# HEALTH CHECK
# =========================

@app.get("/api/healthz")
def healthz():
    return jsonify({
        "status": "ok"
    })


# =========================
# RUN LOCAL
# =========================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
