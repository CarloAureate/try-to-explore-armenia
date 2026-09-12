import os, secrets, sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, abort

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
DB = os.environ.get("DATABASE_PATH", "app.db")
LINK_MINUTES = {"3m": 3, "3h": 180}

def now():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.isoformat()

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS links (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token TEXT UNIQUE NOT NULL,
      link_type TEXT NOT NULL,
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'ACTIVE',
      selected_option TEXT,
      used_at TEXT
    );
    CREATE TABLE IF NOT EXISTS location_shares (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      link_id INTEGER NOT NULL,
      latitude REAL NOT NULL,
      longitude REAL NOT NULL,
      accuracy REAL,
      location_timestamp TEXT NOT NULL,
      received_at TEXT NOT NULL,
      FOREIGN KEY(link_id) REFERENCES links(id)
    );
    """)
    con.commit(); con.close()

def refresh_expired():
    con = db()
    con.execute("UPDATE links SET status='EXPIRED' WHERE status='ACTIVE' AND expires_at <= ?", (iso(now()),))
    con.commit(); con.close()

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self)"
    return resp

@app.route("/")
def home():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        user = request.form.get("username","")
        pw = request.form.get("password","")
        if secrets.compare_digest(user, os.environ.get("ADMIN_USERNAME","admin")) and secrets.compare_digest(pw, os.environ.get("ADMIN_PASSWORD","")):
            session.clear(); session["admin"] = True
            return redirect(url_for("admin"))
        error = "Access denied. Check the admin credentials and try again."
    return render_template("login.html", error=error)



    data = request.get_json(silent=True) or {}

    choice = data.get("choice")

    if choice not in ("armenia", "yerevan", "declined"):
        return jsonify(error="Invalid choice"), 400

    con = db()

    row = con.execute(
        "SELECT * FROM links WHERE token=?",
        (token,)
    ).fetchone()

    if not row:
        con.close()
        return jsonify(error="Invalid link"), 404

    if row["status"] != "ACTIVE":
        con.close()
        return jsonify(
            error="Link expired or already used"
        ), 410

    if datetime.fromisoformat(row["expires_at"]) <= now():
        con.execute(
            "UPDATE links SET status='EXPIRED' WHERE id=?",
            (row["id"],)
        )
        con.commit()
        con.close()

        return jsonify(error="Link expired"), 410

    # Մերժում
    if choice == "declined":

        con.execute(
            """
            UPDATE links
            SET status='DECLINED',
                selected_option=?,
                used_at=?
            WHERE id=? AND status='ACTIVE'
            """,
            (
                choice,
                iso(now()),
                row["id"]
            )
        )

        con.commit()
        con.close()

        return jsonify(
            ok=True,
            status="DECLINED",
            video=None
        )

    # Առաջին location
    lat = data.get("latitude")
    lon = data.get("longitude")
    acc = data.get("accuracy")

    if not isinstance(lat, (int, float)) or \
       not isinstance(lon, (int, float)):

        con.close()

        return jsonify(
            error="A real location was not received."
        ), 422

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):

        con.close()

        return jsonify(
            error="Invalid coordinates"
        ), 400

    started = now()

    # LIVE կարգավիճակ
    cur = con.execute(
        """
        UPDATE links
        SET status='LIVE',
            selected_option=?,
            used_at=?
        WHERE id=? AND status='ACTIVE'
        """,
        (
            choice,
            iso(started),
            row["id"]
        )
    )

    if cur.rowcount != 1:

        con.rollback()
        con.close()

        return jsonify(
            error="Link already completed"
        ), 409

    con.execute(
        """
        INSERT INTO location_shares
        (
            link_id,
            latitude,
            longitude,
            accuracy,
            location_timestamp,
            received_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            row["id"],
            lat,
            lon,
            acc if isinstance(acc, (int, float)) else None,
            data.get("location_timestamp") or iso(started),
            iso(started)
        )
    )

    con.commit()
    con.close()

    # Այս բրաուզերն է live session-ի տերը
    session["live_token"] = token
    session["live_started_at"] = iso(started)

    return jsonify(
        ok=True,
        status="LIVE",
        video=
            "video_armenia.mp4"
            if choice == "armenia"
            else "video_yerevan.mp4"
    )


@app.post("/api/r/<token>/location")
def live_location(token):

    # Միայն սկզբնական համաձայնություն տված browser-ը
    if session.get("live_token") != token:

        return jsonify(
            error="Live location session not authorized."
        ), 403

    con = db()

    row = con.execute(
        "SELECT * FROM links WHERE token=?",
        (token,)
    ).fetchone()

    if not row:

        con.close()

        return jsonify(
            error="Invalid link"
        ), 404

    if row["status"] != "LIVE":

        con.close()

        return jsonify(
            error="Live session is no longer active."
        ), 410

    started = datetime.fromisoformat(
        row["used_at"]
    )

    current = now()

    # Live location-ի առավելագույն ժամանակը՝ 30 րոպե
    if current >= started + timedelta(minutes=30):

        con.execute(
            """
            UPDATE links
            SET status='LOCATION_RECEIVED'
            WHERE id=? AND status='LIVE'
            """,
            (row["id"],)
        )

        con.commit()
        con.close()

        session.pop("live_token", None)
        session.pop("live_started_at", None)

        return jsonify(
            error="Live location session finished."
        ), 410

    lat = data = request.get_json(silent=True) or {}

    latitude = data.get("latitude")
    longitude = data.get("longitude")
    accuracy = data.get("accuracy")

    if not isinstance(latitude, (int, float)) or \
       not isinstance(longitude, (int, float)):

        con.close()

        return jsonify(
            error="Invalid location."
        ), 422

    if not (-90 <= latitude <= 90 and
            -180 <= longitude <= 180):

        con.close()

        return jsonify(
            error="Invalid coordinates."
        ), 400

    received = now()

    # Պահում ենք վերջին դիրքը
    con.execute(
        """
        UPDATE location_shares
        SET latitude=?,
            longitude=?,
            accuracy=?,
            location_timestamp=?,
            received_at=?
        WHERE link_id=?
        """,
        (
            latitude,
            longitude,
            accuracy if isinstance(accuracy, (int, float)) else None,
            data.get("location_timestamp") or iso(received),
            iso(received),
            row["id"]
        )
    )

    con.commit()
    con.close()

    return jsonify(
        ok=True,
        status="LIVE"
    )
@admin_required
def stats():
    refresh_expired()
    con = db()
    out = {}
    out["active"] = con.execute("SELECT COUNT(*) FROM links WHERE status='ACTIVE'").fetchone()[0]
    out["received"] = con.execute("SELECT COUNT(*) FROM links WHERE status='LOCATION_RECEIVED'").fetchone()[0]
    out["declined"] = con.execute("SELECT COUNT(*) FROM links WHERE status='DECLINED'").fetchone()[0]
    out["expired"] = con.execute("SELECT COUNT(*) FROM links WHERE status='EXPIRED'").fetchone()[0]
    out["failed"] = con.execute("SELECT COUNT(*) FROM links WHERE status='LOCATION_FAILED'").fetchone()[0]
    con.close()
    return jsonify(out)

@app.route("/r/<token>", methods=["GET"])
def recipient(token):
    refresh_expired()
    con = db()
    row = con.execute("SELECT * FROM links WHERE token=?", (token,)).fetchone()
    con.close()
    if not row:
        return render_template("expired.html", title="LINK NOT FOUND", message="This exploration link is not valid."), 404
    if row["status"] == "EXPIRED":
        return render_template("expired.html", title="LINK EXPIRED", message="This exploration link is no longer active."), 410
    if row["status"] != "ACTIVE":
        return render_template("expired.html", title="LINK ALREADY USED", message="This link has already been completed."), 410
    return render_template("recipient.html", token=token, expires_at=row["expires_at"])

@app.post("/api/r/<token>/respond")
def respond(token):
    data = request.get_json(silent=True) or {}
    choice = data.get("choice")
    if choice not in ("armenia","yerevan","declined"):
        return jsonify(error="Invalid choice"), 400
    con = db()
    row = con.execute("SELECT * FROM links WHERE token=?", (token,)).fetchone()
    if not row:
        con.close(); return jsonify(error="Invalid link"), 404
    if row["status"] != "ACTIVE" or datetime.fromisoformat(row["expires_at"]) <= now():
        con.close(); return jsonify(error="Link expired or already used"), 410

    if choice == "declined":
        con.execute("UPDATE links SET status='DECLINED', selected_option=?, used_at=? WHERE id=? AND status='ACTIVE'",
                    (choice, iso(now()), row["id"]))
        con.commit(); con.close()
        return jsonify(ok=True, status="DECLINED", video=None)

    lat = data.get("latitude"); lon = data.get("longitude"); acc = data.get("accuracy")
    if not isinstance(lat,(int,float)) or not isinstance(lon,(int,float)):
        con.execute("UPDATE links SET status='LOCATION_FAILED', selected_option=?, used_at=? WHERE id=? AND status='ACTIVE'",
                    (choice, choice, iso(now()), row["id"]))
        con.commit(); con.close()
        return jsonify(error="A real location was not received."), 422
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        con.close(); return jsonify(error="Invalid coordinates"), 400

    received = now()
    cur = con.execute("UPDATE links SET status='LOCATION_RECEIVED', selected_option=?, used_at=? WHERE id=? AND status='ACTIVE'",
                      (choice, iso(received), row["id"]))
    if cur.rowcount != 1:
        con.rollback(); con.close(); return jsonify(error="Link already completed"), 409
    con.execute("""INSERT INTO location_shares(link_id,latitude,longitude,accuracy,location_timestamp,received_at)
                   VALUES(?,?,?,?,?,?)""",
                (row["id"], lat, lon, acc if isinstance(acc,(int,float)) else None,
                 data.get("location_timestamp") or iso(received), iso(received)))
    con.commit(); con.close()
    return jsonify(ok=True, status="LOCATION_RECEIVED",
                   video="video_armenia.mp4" if choice=="armenia" else "video_yerevan.mp4")

@app.get("/api/healthz")
def healthz():
    return jsonify(ok=True)

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","5000")), debug=False)
