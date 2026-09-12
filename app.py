import os
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    redirect,
    url_for,
    session,
)

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-key")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

DB_PATH = os.environ.get("DB_PATH", "app.db")

LINK_MINUTES = {
    "3m": 3,
    "3h": 180,
}

LIVE_MINUTES = 30


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            link_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            selected_option TEXT,
            used_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS location_shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id INTEGER NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            accuracy REAL,
            location_timestamp TEXT,
            received_at TEXT NOT NULL,
            FOREIGN KEY(link_id) REFERENCES links(id)
        )
        """
    )

    conn.commit()
    conn.close()




def refresh_expired():
    conn = get_db()
    current = now_utc()

    rows = conn.execute(
        """
        SELECT id, expires_at, status, used_at
        FROM links
        WHERE status IN ('ACTIVE', 'LIVE')
        """
    ).fetchall()

    for row in rows:
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except Exception:
            continue

        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)

        if current >= expires:
            conn.execute(
                """
                UPDATE links
                SET status = 'EXPIRED'
                WHERE id = ?
                """,
                (row["id"],),
            )
            continue

        if row["status"] == "LIVE" and row["used_at"]:
            try:
                used_at = datetime.fromisoformat(row["used_at"])
                if used_at.tzinfo is None:
                    used_at = used_at.replace(tzinfo=timezone.utc)

                if current >= used_at + timedelta(minutes=LIVE_MINUTES):
                    conn.execute(
                        """
                        UPDATE links
                        SET status = 'LOCATION_RECEIVED'
                        WHERE id = ?
                        """,
                        (row["id"],),
                    )
            except Exception:
                pass

    conn.commit()
    conn.close()


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


@app.get("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))

        error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/admin")
@admin_required
def admin():
    refresh_expired()
    return render_template("admin.html")


@app.post("/api/links")
@admin_required
def create_link():
    data = request.get_json(silent=True) or {}

    link_type = data.get("type") or data.get("duration")

    if link_type not in LINK_MINUTES:
        return jsonify({
            "success": False,
            "error": "Invalid duration"
        }), 400

    minutes = LINK_MINUTES[link_type]

    token = secrets.token_urlsafe(32)

    created = now_utc()
    expires = created + timedelta(minutes=minutes)

    conn = get_db()

    conn.execute(
        """
        INSERT INTO links (
            token,
            link_type,
            created_at,
            expires_at,
            status
        )
        VALUES (?, ?, ?, ?, 'ACTIVE')
        """,
        (
            token,
            link_type,
            created.isoformat(),
            expires.isoformat(),
        ),
    )

    conn.commit()
    conn.close()

    base_url = request.host_url.rstrip("/")
    link_url = f"{base_url}/r/{token}"

    return jsonify({
        "success": True,
        "url": link_url,
        "expires_at": expires.isoformat(),
    })


@app.get("/api/links")
@admin_required
def list_links():
    refresh_expired()

    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            l.id,
            l.token,
            l.link_type,
            l.created_at,
            l.expires_at,
            l.status,
            l.selected_option,
            l.used_at,
            ls.latitude,
            ls.longitude,
            ls.accuracy,
            ls.location_timestamp,
            ls.received_at
        FROM links l
        LEFT JOIN location_shares ls
            ON ls.id = (
                SELECT MAX(ls2.id)
                FROM location_shares ls2
                WHERE ls2.link_id = l.id
            )
        ORDER BY l.id DESC
        """
    ).fetchall()

    conn.close()

    items = []

    for row in rows:
        items.append({
            "id": row["id"],
            "token": row["token"],
            "link_type": row["link_type"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "status": row["status"],
            "selected_option": row["selected_option"],
            "used_at": row["used_at"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "accuracy": row["accuracy"],
            "location_timestamp": row["location_timestamp"],
            "received_at": row["received_at"],
        })

    return jsonify({
        "items": items
    })


@app.get("/api/stats")
@admin_required
def stats():
    refresh_expired()

    conn = get_db()

    active = conn.execute(
        "SELECT COUNT(*) FROM links WHERE status = 'ACTIVE'"
    ).fetchone()[0]

    received = conn.execute(
        """
        SELECT COUNT(*)
        FROM links
        WHERE status IN ('LIVE', 'LOCATION_RECEIVED')
        """
    ).fetchone()[0]

    declined = conn.execute(
        "SELECT COUNT(*) FROM links WHERE status = 'DECLINED'"
    ).fetchone()[0]

    expired = conn.execute(
        "SELECT COUNT(*) FROM links WHERE status = 'EXPIRED'"
    ).fetchone()[0]

    conn.close()

    return jsonify({
        "active": active,
        "received": received,
        "declined": declined,
        "expired": expired,
    })


@app.get("/r/<token>")
def recipient(token):
    refresh_expired()

    conn = get_db()

    link = conn.execute(
        """
        SELECT *
        FROM links
        WHERE token = ?
        """,
        (token,),
    ).fetchone()

    conn.close()

    if not link:
        return render_template("expired.html")

    if link["status"] != "ACTIVE":
        return render_template("expired.html")

    try:
        expires = datetime.fromisoformat(link["expires_at"])

        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)

        if now_utc() >= expires:
            return render_template("expired.html")
    except Exception:
        return render_template("expired.html")

    return render_template(
        "recipient.html",
        token=token,
        expires_at=link["expires_at"],
    )


@app.post("/api/r/<token>/respond")
def respond(token):
    data = request.get_json(silent=True) or {}

    choice = data.get("choice")

    if choice not in ("armenia", "yerevan", "declined", "decline"):
        return jsonify({
            "success": False,
            "error": "Invalid choice"
        }), 400

    refresh_expired()

    conn = get_db()

    link = conn.execute(
        """
        SELECT *
        FROM links
        WHERE token = ?
        """,
        (token,),
    ).fetchone()

    if not link:
        conn.close()
        return jsonify({
            "success": False,
            "error": "Invalid link"
        }), 404

    if link["status"] != "ACTIVE":
        conn.close()
        return jsonify({
            "success": False,
            "error": "Link expired or already used"
        }), 410

    if choice in ("declined", "decline"):
        conn.execute(
            """
            UPDATE links
            SET status = 'DECLINED',
                selected_option = 'declined',
                used_at = ?
            WHERE id = ?
            """,
            (now_iso(), link["id"]),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "success": True,
            "video": None,
            "live": False,
        })

    latitude = data.get("latitude")
    longitude = data.get("longitude")
    accuracy = data.get("accuracy")
    location_timestamp = data.get("location_timestamp")

    if latitude is None or longitude is None:
        conn.close()
        return jsonify({
            "success": False,
            "error": "Location was not provided"
        }), 400

    conn.execute(
        """
        INSERT INTO location_shares (
            link_id,
            latitude,
            longitude,
            accuracy,
            location_timestamp,
            received_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            link["id"],
            float(latitude),
            float(longitude),
            float(accuracy) if accuracy is not None else None,
            location_timestamp,
            now_iso(),
        ),
    )

    conn.execute(
        """
        UPDATE links
        SET status = 'LIVE',
            selected_option = ?,
            used_at = ?
        WHERE id = ?
        """,
        (
            choice,
            now_iso(),
            link["id"],
        ),
    )

    conn.commit()
    conn.close()

    video = (
        "video_armenia.mp4"
        if choice == "armenia"
        else "video_yerevan.mp4"
    )

    session["live_token"] = token

    return jsonify({
        "success": True,
        "video": video,
        "live": True,
    })


@app.post("/api/r/<token>/location")
def live_location(token):
    data = request.get_json(silent=True) or {}

    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if latitude is None or longitude is None:
        return jsonify({
            "success": False,
            "error": "Location was not provided"
        }), 400

    refresh_expired()

    conn = get_db()

    link = conn.execute(
        """
        SELECT *
        FROM links
        WHERE token = ?
        """,
        (token,),
    ).fetchone()

    if not link:
        conn.close()
        return jsonify({
            "success": False,
            "error": "Invalid link"
        }), 404

    if link["status"] not in ("LIVE", "LOCATION_RECEIVED"):
        conn.close()
        return jsonify({
            "success": False,
            "error": "Live location is not active"
        }), 410

    if link["used_at"]:
        try:
            used_at = datetime.fromisoformat(link["used_at"])

            if used_at.tzinfo is None:
                used_at = used_at.replace(tzinfo=timezone.utc)

            if now_utc() >= used_at + timedelta(minutes=LIVE_MINUTES):
                conn.execute(
                    """
                    UPDATE links
                    SET status = 'LOCATION_RECEIVED'
                    WHERE id = ?
                    """,
                    (link["id"],),
                )
                conn.commit()
                conn.close()

                return jsonify({
                    "success": False,
                    "error": "Live location period ended"
                }), 410
        except Exception:
            pass

    latest = conn.execute(
        """
        SELECT id
        FROM location_shares
        WHERE link_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (link["id"],),
    ).fetchone()

    if latest:
        conn.execute(
            """
            UPDATE location_shares
            SET latitude = ?,
                longitude = ?,
                accuracy = ?,
                location_timestamp = ?,
                received_at = ?
            WHERE id = ?
            """,
            (
                float(latitude),
                float(longitude),
                float(data["accuracy"]) if data.get("accuracy") is not None else None,
                data.get("location_timestamp"),
                now_iso(),
                latest["id"],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO location_shares (
                link_id,
                latitude,
                longitude,
                accuracy,
                location_timestamp,
                received_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                link["id"],
                float(latitude),
                float(longitude),
                float(data["accuracy"]) if data.get("accuracy") is not None else None,
                data.get("location_timestamp"),
                now_iso(),
            ),
        )

    conn.commit()
    conn.close()

    return jsonify({
        "success": True
    })


@app.get("/healthz")
def healthz():
    return jsonify({
        "ok": True
    })


init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )
