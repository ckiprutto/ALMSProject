# =============================================================================
# app.py — ALMS Hotel Booking System
# Fully debugged | Stripe + M-PESA STK Push | MySQL
# =============================================================================

# --- Load .env FIRST before any os.getenv() calls ---
from dotenv import load_dotenv
load_dotenv()

import os
import json
import base64
import requests
import warnings
from datetime import datetime, timedelta
import re

from flask import (
    Flask, render_template, request, redirect,
    session, url_for, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
import mysql.connector
from functools import wraps
import stripe

# =============================================================================
# FLASK CONFIG
# =============================================================================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "alms_super_secret_2024_change_in_prod")

from reports import reports_bp
app.register_blueprint(reports_bp, url_prefix="/admin/reports")

# =============================================================================
# STRIPE CONFIG
# =============================================================================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")   # Add your Stripe key here or in .env

# =============================================================================
# M-PESA (Safaricom Daraja) CONFIG
# Priority: .env file > hardcoded defaults below
# =============================================================================
MPESA_ENV             = os.getenv("MPESA_ENV",             "sandbox")
MPESA_CONSUMER_KEY    = os.getenv("MPESA_CONSUMER_KEY",    "X0KIhhGAO1rq6mJB60WFeYjiIJrA7zOGxiKkmTh56FLaUmWt")
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET", "lNetNLWDTdgGK2Bii5WhanIrZQ1tI9A4v5m1PYadHKGBxqx1VqxeBq9JY0FxxkEj")
MPESA_SHORTCODE       = os.getenv("MPESA_SHORTCODE",       "174379")
MPESA_PASSKEY         = os.getenv("MPESA_PASSKEY",         "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919")
MPESA_CALLBACK_URL    = os.getenv("MPESA_CALLBACK_URL",    "https://calorifically-clypeal-triston.ngrok-free.dev/mpesa/callback")
# ^^^ IMPORTANT: Replace YOUR_NGROK_URL with your live ngrok URL before testing.
#     Run: ngrok http 5000  — then paste the https URL above.

MPESA_BASE_URL = (
    "https://sandbox.safaricom.co.ke"
    if MPESA_ENV == "sandbox"
    else "https://api.safaricom.co.ke"
)
MPESA_TOKEN_URL = f"{MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials"
MPESA_STK_URL   = f"{MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest"

# In-memory token cache (refreshed automatically before expiry)
_TOKEN_CACHE = {"token": None, "expires": None}

# =============================================================================
# DATABASE CONFIG
# =============================================================================
DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "user":     os.getenv("DB_USER",     "root"),
    "password": os.getenv("DB_PASSWORD", "0000"),
    "database": os.getenv("DB_NAME",     "alms_db"),
}

def get_db():
    """Open and return a new MySQL connection."""
    return mysql.connector.connect(**DB_CONFIG)

# =============================================================================
# STARTUP CHECKS
# =============================================================================
def _startup_checks():
    missing = []
    if not MPESA_CONSUMER_KEY:    missing.append("MPESA_CONSUMER_KEY")
    if not MPESA_CONSUMER_SECRET: missing.append("MPESA_CONSUMER_SECRET")
    if not MPESA_PASSKEY:         missing.append("MPESA_PASSKEY")
    if "YOUR_NGROK_URL" in MPESA_CALLBACK_URL:
        warnings.warn(
            "\n[MPESA] MPESA_CALLBACK_URL still contains placeholder 'YOUR_NGROK_URL'.\n"
            "Run `ngrok http 5000` and set MPESA_CALLBACK_URL in your .env or directly in app.py.\n"
            "STK pushes will be sent but payment confirmation callbacks WILL FAIL until this is set.",
            stacklevel=2
        )
    if missing:
        warnings.warn(
            f"\n[MPESA] Missing credentials: {', '.join(missing)}.\n"
            "M-PESA payments will not work.",
            stacklevel=2
        )

_startup_checks()

# =============================================================================
# AUTH DECORATORS
# =============================================================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def role_required(role):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in first.", "warning")
                return redirect(url_for("login"))
            if (session.get("role") or "").lower() != role.lower():
                flash("Access denied.", "danger")
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return wrapper
    return decorator


def current_role():
    return (session.get("role") or "").lower()

# =============================================================================
# UTILITY HELPERS
# =============================================================================
def parse_date(s: str):
    """Parse YYYY-MM-DD string → date object, or None on failure."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def normalize_phone_ke(raw: str) -> str:
    """
    Normalize any Kenyan phone number to 12-digit format: 2547XXXXXXXX
    Accepts: 0713003689 | 713003689 | +254713003689 | 254713003689
    Raises ValueError on unrecognised format.
    """
    phone = raw.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+254"):
        phone = phone[1:]                         # +254... → 254...
    elif phone.startswith("0") and len(phone) == 10:
        phone = "254" + phone[1:]                 # 07... → 2547...
    elif phone.startswith("7") and len(phone) == 9:
        phone = "254" + phone                     # 7... → 2547...
    elif phone.startswith("254") and len(phone) == 12:
        pass                                      # already correct
    else:
        raise ValueError(
            f"Unrecognised phone number '{raw}'. "
            "Use format: 07XXXXXXXX, +2547XXXXXXXX, or 2547XXXXXXXX."
        )
    return phone


def compute_booking_total(cursor, booking_id: int, user_id: int):
    """
    Fetch booking details and compute total cost.
    Returns (booking_dict, nights, total_kes) or (None, None, None).
    """
    cursor.execute("""
        SELECT b.booking_id, b.room_id, b.check_in, b.check_out,
               b.guests, b.status,
               r.room_number, r.room_type, r.price_per_night,
               r.status AS room_status
        FROM bookings b
        JOIN rooms r ON r.room_id = b.room_id
        WHERE b.booking_id = %s AND b.user_id = %s
        LIMIT 1
    """, (booking_id, user_id))
    booking = cursor.fetchone()
    if not booking:
        return None, None, None

    ci = booking["check_in"]
    co = booking["check_out"]
    if isinstance(ci, datetime): ci = ci.date()
    if isinstance(co, datetime): co = co.date()

    nights = max((co - ci).days, 1)
    total  = round(float(booking["price_per_night"]) * nights, 2)
    return booking, nights, total

# =============================================================================
# M-PESA TOKEN (with caching)
# =============================================================================
def mpesa_token() -> str:
    """
    Return a valid M-PESA OAuth bearer token.
    Caches the token and refreshes 60 s before expiry.
    Raises ValueError with a descriptive message on failure.
    """
    global _TOKEN_CACHE
    now = datetime.now()

    # Return cached token if still valid (with 60-second buffer)
    if (
        _TOKEN_CACHE["token"]
        and _TOKEN_CACHE["expires"]
        and _TOKEN_CACHE["expires"] > now + timedelta(seconds=60)
    ):
        return _TOKEN_CACHE["token"]

    if not MPESA_CONSUMER_KEY or not MPESA_CONSUMER_SECRET:
        raise ValueError(
            "M-PESA credentials are missing. "
            "Set MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET."
        )

    b64 = base64.b64encode(
        f"{MPESA_CONSUMER_KEY}:{MPESA_CONSUMER_SECRET}".encode()
    ).decode()

    print(f"[MPESA] Fetching OAuth token from {MPESA_TOKEN_URL} ...")
    try:
        resp = requests.get(
            MPESA_TOKEN_URL,
            headers={"Authorization": f"Basic {b64}"},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise ValueError(f"Network error reaching Safaricom: {exc}") from exc

    if not resp.ok:
        print(f"[MPESA] Token error {resp.status_code}: {resp.text}")
        raise ValueError(
            f"Safaricom rejected credentials (HTTP {resp.status_code}). "
            f"Check MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET. "
            f"Details: {resp.text}"
        )

    data = resp.json()
    _TOKEN_CACHE["token"]   = data["access_token"]
    _TOKEN_CACHE["expires"] = now + timedelta(seconds=int(data.get("expires_in", 3599)))
    print("[MPESA] Token acquired successfully.")
    return _TOKEN_CACHE["token"]

# =============================================================================
# ROUTES — CORE
# =============================================================================
@app.route("/")
def home():
    return redirect(url_for("login"))


# ---------- LOGIN / LOGOUT ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password   = request.form.get("password",   "").strip()

        if not identifier or not password:
            flash("Please enter your username/email and password.", "warning")
            return render_template("login.html")

        db = cur = None
        try:
            db  = get_db()
            cur = db.cursor(dictionary=True)
            cur.execute(
                "SELECT * FROM users WHERE username=%s OR email=%s",
                (identifier, identifier)
            )
            user = cur.fetchone()
        except mysql.connector.Error as exc:
            flash(f"Database error: {exc}", "danger")
            return render_template("login.html")
        finally:
            if cur: cur.close()
            if db:  db.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"]  = user["user_id"]
            session["username"] = user["username"]
            session["role"]     = user["role"]
            return redirect(url_for(
                "admin_dashboard" if current_role() == "admin" else "customer_dashboard"
            ))

        flash("Invalid login credentials.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return redirect(url_for(
        "admin_dashboard" if current_role() == "admin" else "customer_dashboard"
    ))


# ---------- REGISTER ----------
@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("username", "").strip()
    email    = request.form.get("email",    "").strip().lower()
    pw       = request.form.get("password",         "")
    pw2      = request.form.get("confirm_password", "")

    if not username or not email or not pw:
        flash("All fields are required.", "danger")
        return redirect(url_for("login"))
    if pw != pw2:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("login"))
    if len(pw) < 6:
        flash("Password must be at least 6 characters.", "warning")
        return redirect(url_for("login"))

    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT user_id FROM users WHERE username=%s OR email=%s",
            (username, email)
        )
        if cur.fetchone():
            flash("Username or email already exists.", "danger")
            return redirect(url_for("login"))

        cur.execute(
            "INSERT INTO users (username, email, password_hash, role) VALUES (%s,%s,%s,'customer')",
            (username, email, generate_password_hash(pw))
        )
        db.commit()
        flash("Account created. Please log in.", "success")
    except mysql.connector.Error as exc:
        if db: db.rollback()
        flash(f"Registration failed: {exc}", "danger")
    finally:
        if cur: cur.close()
        if db:  db.close()

    return redirect(url_for("login"))


# ---------- PASSWORD RESET (TODO) ----------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    return "Forgot password — TODO", 200


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    return "Reset password — TODO", 200

# =============================================================================
# ROUTES — DASHBOARDS
# =============================================================================
# =============================================================================
# Replace your existing admin_dashboard route in app.py with this version.
# It passes live stats (rooms, bookings, revenue) to the template.
# =============================================================================

@app.route("/admin")
@login_required
@role_required("admin")
def admin_dashboard():
    db = cur = None
    stats = {
        "total_rooms": 0,
        "pending_bookings": 0,
        "confirmed_bookings": 0,
        "total_revenue": 0,
    }
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)

        cur.execute("SELECT COUNT(*) AS n FROM rooms")
        stats["total_rooms"] = cur.fetchone()["n"]

        cur.execute("SELECT COUNT(*) AS n FROM bookings WHERE status='pending'")
        stats["pending_bookings"] = cur.fetchone()["n"]

        cur.execute("SELECT COUNT(*) AS n FROM bookings WHERE status='confirmed'")
        stats["confirmed_bookings"] = cur.fetchone()["n"]

        cur.execute("""
            SELECT COALESCE(SUM(DATEDIFF(b.check_out, b.check_in) * r.price_per_night), 0) AS rev
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            WHERE b.status IN ('confirmed', 'checked_out')
        """)
        stats["total_revenue"] = float(cur.fetchone()["rev"] or 0)

    except mysql.connector.Error as exc:
        flash(f"Dashboard stats error: {exc}", "warning")
    finally:
        if cur: cur.close()
        if db:  db.close()

    return render_template("admin_dashboard.html", stats=stats)


@app.route("/customer")
@login_required
@role_required("customer")
def customer_dashboard():
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("""
            SELECT b.booking_id, b.check_in, b.check_out, b.guests, b.status,
                   r.room_number, r.room_type, r.price_per_night
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            WHERE b.user_id = %s
            ORDER BY b.booking_id DESC LIMIT 10
        """, (session["user_id"],))
        return render_template("customer_dashboard.html", my_bookings=cur.fetchall())
    finally:
        if cur: cur.close()
        if db:  db.close()

# =============================================================================
# ROUTES — CUSTOMER: ROOMS & BOOKINGS
# =============================================================================
@app.route("/customer/rooms")
@login_required
@role_required("customer")
def customer_rooms():
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM rooms WHERE status='available' ORDER BY room_id DESC")
        return render_template("customer_rooms.html", rooms=cur.fetchall())
    except mysql.connector.Error as exc:
        flash(f"Database error: {exc}", "danger")
        return redirect(url_for("customer_dashboard"))
    finally:
        if cur: cur.close()
        if db:  db.close()


@app.route("/customer/book/<int:room_id>", methods=["GET", "POST"])
@login_required
@role_required("customer")
def customer_book_room(room_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM rooms WHERE room_id=%s", (room_id,))
        room = cur.fetchone()

        if not room:
            flash("Room not found.", "danger")
            return redirect(url_for("customer_rooms"))
        if (room.get("status") or "").lower() != "available":
            flash("Room is not available.", "warning")
            return redirect(url_for("customer_rooms"))

        if request.method == "POST":
            ci_str = request.form.get("check_in",  "")
            co_str = request.form.get("check_out", "")
            g_str  = request.form.get("guests",    "1")

            ci = parse_date(ci_str)
            co = parse_date(co_str)

            if not ci or not co or co <= ci:
                flash("Please enter valid check-in and check-out dates.", "danger")
                return render_template("customer_book_room.html", room=room)

            try:
                guests = int(g_str)
            except ValueError:
                flash("Guests must be a whole number.", "danger")
                return render_template("customer_book_room.html", room=room)

            capacity = int(room.get("capacity", 1))
            if not (1 <= guests <= capacity):
                flash(f"Guests must be between 1 and {capacity}.", "warning")
                return render_template("customer_book_room.html", room=room)

            cur2 = db.cursor()
            try:
                cur2.execute("""
                    INSERT INTO bookings (user_id, room_id, check_in, check_out, guests, status)
                    VALUES (%s,%s,%s,%s,%s,'pending')
                """, (session["user_id"], room_id, ci, co, guests))
                cur2.execute(
                    "UPDATE rooms SET status='reserved' WHERE room_id=%s", (room_id,)
                )
                db.commit()
                flash("Booking submitted — status: pending.", "success")
                return redirect(url_for("customer_dashboard"))
            except mysql.connector.Error as exc:
                db.rollback()
                flash(f"DB error: {exc}", "danger")
                return render_template("customer_book_room.html", room=room)
            finally:
                cur2.close()

        return render_template("customer_book_room.html", room=room)

    except mysql.connector.Error as exc:
        flash(f"Database error: {exc}", "danger")
        return redirect(url_for("customer_rooms"))
    finally:
        if cur: cur.close()
        if db:  db.close()


@app.route("/customer/my-bookings")
@login_required
@role_required("customer")
def customer_my_bookings():
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("""
            SELECT b.*, r.room_number, r.room_type, r.price_per_night
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            WHERE b.user_id = %s
            ORDER BY b.booking_id DESC
        """, (session["user_id"],))
        return render_template("customer_my_bookings.html", bookings=cur.fetchall())
    except mysql.connector.Error as exc:
        flash(f"Database error: {exc}", "danger")
        return redirect(url_for("customer_dashboard"))
    finally:
        if cur: cur.close()
        if db:  db.close()


@app.route("/customer/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
@role_required("customer")
def customer_cancel_booking(booking_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("""
            SELECT booking_id, status, room_id
            FROM bookings WHERE booking_id=%s AND user_id=%s
        """, (booking_id, session["user_id"]))
        b = cur.fetchone()

        if not b:
            flash("Booking not found.", "danger")
            return redirect(url_for("customer_my_bookings"))
        if (b["status"] or "").lower() != "pending":
            flash("Only pending bookings can be cancelled.", "warning")
            return redirect(url_for("customer_my_bookings"))

        cur2 = db.cursor()
        cur2.execute(
            "UPDATE bookings SET status='cancelled' WHERE booking_id=%s", (booking_id,)
        )
        cur2.execute(
            "UPDATE rooms SET status='available' WHERE room_id=%s", (b["room_id"],)
        )
        db.commit()
        cur2.close()
        flash("Booking cancelled. Room is now available.", "success")

    except mysql.connector.Error as exc:
        if db: db.rollback()
        flash(f"DB error: {exc}", "danger")
    finally:
        if cur: cur.close()
        if db:  db.close()

    return redirect(url_for("customer_my_bookings"))

# =============================================================================
# ROUTES — ADMIN: ROOMS
# =============================================================================
@app.route("/admin/rooms")
@login_required
@role_required("admin")
def admin_rooms():
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM rooms ORDER BY room_id DESC")
        return render_template("admin_rooms.html", rooms=cur.fetchall())
    except mysql.connector.Error as exc:
        flash(f"Database error: {exc}", "danger")
        return render_template("admin_rooms.html", rooms=[])
    finally:
        if cur: cur.close()
        if db:  db.close()


@app.route("/admin/rooms/new", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_rooms_new():
    if request.method == "POST":
        room_number = request.form.get("room_number", "").strip()
        room_type   = request.form.get("room_type",   "").strip()
        capacity    = request.form.get("capacity",    "1").strip()
        price       = request.form.get("price_per_night", "").strip()
        status      = request.form.get("status",      "available").strip()
        lat_raw     = request.form.get("latitude",    "").strip()
        lng_raw     = request.form.get("longitude",   "").strip()

        if not room_number or not room_type or not price:
            flash("Room number, type, and price are required.", "danger")
            return render_template("admin_rooms_new.html")

        db = cur = None
        try:
            latitude  = float(lat_raw) if lat_raw else None
            longitude = float(lng_raw) if lng_raw else None
            db  = get_db()
            cur = db.cursor()
            cur.execute("""
                INSERT INTO rooms
                    (room_number, room_type, capacity, price_per_night, status, latitude, longitude)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (room_number, room_type, int(capacity), float(price), status, latitude, longitude))
            db.commit()
            flash("Room added.", "success")
            return redirect(url_for("admin_rooms"))
        except ValueError:
            if db: db.rollback()
            flash("Capacity must be an integer; price and coordinates must be numbers.", "danger")
            return render_template("admin_rooms_new.html")
        except mysql.connector.Error as exc:
            if db: db.rollback()
            flash(f"DB error: {exc}", "danger")
            return render_template("admin_rooms_new.html")
        finally:
            if cur: cur.close()
            if db:  db.close()

    return render_template("admin_rooms_new.html")


# =============================================================================
# Replace your existing admin_rooms_delete route in app.py with this version.
# It safely deletes payments and bookings linked to the room first,
# then deletes the room itself.
# =============================================================================

@app.route("/admin/rooms/<int:room_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def admin_rooms_delete(room_id):
    db = cur = None
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)

        # Check the room exists
        cur.execute("SELECT room_id, room_number, status FROM rooms WHERE room_id=%s", (room_id,))
        room = cur.fetchone()
        if not room:
            flash("Room not found.", "danger")
            return redirect(url_for("admin_rooms"))

        # Block deletion if room is currently occupied
        if (room["status"] or "").lower() == "occupied":
            flash("Cannot delete an occupied room. Check out the guest first.", "danger")
            return redirect(url_for("admin_rooms"))

        cur2 = db.cursor()

        # 1. Delete payments linked to bookings of this room
        cur2.execute("""
            DELETE FROM payments
            WHERE booking_id IN (
                SELECT booking_id FROM bookings WHERE room_id = %s
            )
        """, (room_id,))

        # 2. Delete bookings linked to this room
        cur2.execute("DELETE FROM bookings WHERE room_id = %s", (room_id,))

        # 3. Now safe to delete the room
        cur2.execute("DELETE FROM rooms WHERE room_id = %s", (room_id,))

        db.commit()
        cur2.close()
        flash(f"Room #{room['room_number']} and all its booking history deleted.", "success")

    except mysql.connector.Error as exc:
        if db: db.rollback()
        flash(f"Database error: {exc}", "danger")
    finally:
        if cur: cur.close()
        if db:  db.close()

    return redirect(url_for("admin_rooms"))


@app.route("/admin/rooms/<int:room_id>/available", methods=["POST"])
@login_required
@role_required("admin")
def admin_room_available(room_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT status FROM rooms WHERE room_id=%s", (room_id,))
        row = cur.fetchone()
        if not row:
            flash("Room not found.", "danger")
            return redirect(url_for("admin_rooms"))
        if (row["status"] or "").lower() == "available":
            flash("Room is already available.", "info")
            return redirect(url_for("admin_rooms"))
        cur2 = db.cursor()
        cur2.execute("UPDATE rooms SET status='available' WHERE room_id=%s", (room_id,))
        db.commit()
        cur2.close()
        flash("Room marked as available.", "success")
    except mysql.connector.Error as exc:
        if db: db.rollback()
        flash(f"Database error: {exc}", "danger")
    finally:
        if cur: cur.close()
        if db:  db.close()
    return redirect(url_for("admin_rooms"))


@app.route("/admin/rooms/<int:room_id>/occupied", methods=["POST"])
@login_required
@role_required("admin")
def admin_room_occupied(room_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor()
        cur.execute("UPDATE rooms SET status='occupied' WHERE room_id=%s", (room_id,))
        db.commit()
        flash("Room marked as occupied.", "success")
    except mysql.connector.Error as exc:
        if db: db.rollback()
        flash(f"Database error: {exc}", "danger")
    finally:
        if cur: cur.close()
        if db:  db.close()
    return redirect(url_for("admin_rooms"))

# =============================================================================
# ROUTES — ADMIN: BOOKINGS
# =============================================================================
@app.route("/admin/bookings")
@login_required
@role_required("admin")
def admin_bookings():
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("""
            SELECT b.booking_id, b.check_in, b.check_out, b.guests,
                   b.status, b.created_at,
                   u.username, u.email,
                   r.room_id, r.room_number, r.room_type,
                   r.price_per_night, r.status AS room_status
            FROM bookings b
            JOIN users u ON u.user_id = b.user_id
            JOIN rooms r ON r.room_id = b.room_id
            ORDER BY b.booking_id DESC
        """)
        return render_template("admin_bookings.html", bookings=cur.fetchall())
    finally:
        if cur: cur.close()
        if db:  db.close()


@app.route("/admin/bookings/<int:booking_id>/confirm", methods=["POST"])
@login_required
@role_required("admin")
def admin_confirm_booking(booking_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("""
            SELECT b.booking_id, b.status, b.room_id,
                   p.status AS payment_status
            FROM bookings b
            LEFT JOIN payments p ON p.booking_id = b.booking_id
            WHERE b.booking_id = %s
        """, (booking_id,))
        booking = cur.fetchone()

        if not booking:
            flash("Booking not found.", "danger")
            return redirect(url_for("admin_bookings"))
        if (booking["status"] or "").lower() != "pending":
            flash("Only pending bookings can be confirmed.", "warning")
            return redirect(url_for("admin_bookings"))
        if (booking.get("payment_status") or "").lower() != "paid":
            flash("Payment must be received before confirming.", "warning")
            return redirect(url_for("admin_bookings"))

        cur2 = db.cursor()
        cur2.execute(
            "UPDATE bookings SET status='confirmed' WHERE booking_id=%s", (booking_id,)
        )
        cur2.execute(
            "UPDATE rooms SET status='occupied' WHERE room_id=%s", (booking["room_id"],)
        )
        db.commit()
        cur2.close()
        flash("Booking confirmed.", "success")

    except mysql.connector.Error as exc:
        if db: db.rollback()
        flash(f"DB error: {exc}", "danger")
    finally:
        if cur: cur.close()
        if db:  db.close()
    return redirect(url_for("admin_bookings"))


@app.route("/admin/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
@role_required("admin")
def admin_cancel_booking(booking_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT * FROM bookings WHERE booking_id=%s", (booking_id,))
        booking = cur.fetchone()
        if not booking:
            flash("Booking not found.", "danger")
            return redirect(url_for("admin_bookings"))

        cur2 = db.cursor()
        cur2.execute(
            "UPDATE bookings SET status='cancelled' WHERE booking_id=%s", (booking_id,)
        )
        cur2.execute(
            "UPDATE rooms SET status='available' WHERE room_id=%s", (booking["room_id"],)
        )
        db.commit()
        cur2.close()
        flash("Booking cancelled.", "success")

    except mysql.connector.Error as exc:
        if db: db.rollback()
        flash(f"DB error: {exc}", "danger")
    finally:
        if cur: cur.close()
        if db:  db.close()
    return redirect(url_for("admin_bookings"))


@app.route("/admin/bookings/<int:booking_id>/checkout", methods=["POST"])
@login_required
@role_required("admin")
def admin_checkout_booking(booking_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute(
            "SELECT booking_id, status, room_id FROM bookings WHERE booking_id=%s",
            (booking_id,)
        )
        booking = cur.fetchone()
        if not booking:
            flash("Booking not found.", "danger")
            return redirect(url_for("admin_bookings"))
        if (booking["status"] or "").lower() != "confirmed":
            flash("Only confirmed bookings can be checked out.", "warning")
            return redirect(url_for("admin_bookings"))

        cur2 = db.cursor()
        cur2.execute(
            "UPDATE bookings SET status='checked_out' WHERE booking_id=%s", (booking_id,)
        )
        cur2.execute(
            "UPDATE rooms SET status='available' WHERE room_id=%s", (booking["room_id"],)
        )
        db.commit()
        cur2.close()
        flash("Guest checked out. Room is now available.", "success")

    except mysql.connector.Error as exc:
        if db: db.rollback()
        flash(f"DB error: {exc}", "danger")
    finally:
        if cur: cur.close()
        if db:  db.close()
    return redirect(url_for("admin_bookings"))

# =============================================================================
# ROUTES — ADMIN: PAYMENTS
# =============================================================================
@app.route("/admin/payments")
@login_required
@role_required("admin")
def admin_payments():
    db = cur = None
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)
        # FIX: added check_in, check_out, email, room_type, payment_id to SELECT
        cur.execute("""
            SELECT b.booking_id, b.check_in, b.check_out,
                   u.username, u.email,
                   r.room_number, r.room_type, r.price_per_night,
                   p.payment_id, p.amount, p.method, p.status AS payment_status,
                   p.paid_at
            FROM bookings b
            JOIN users u ON u.user_id = b.user_id
            JOIN rooms r ON r.room_id = b.room_id
            LEFT JOIN payments p ON p.booking_id = b.booking_id
            ORDER BY b.booking_id DESC
        """)
        return render_template("admin_payments.html", rows=cur.fetchall())
    finally:
        if cur: cur.close()
        if db:  db.close()


@app.route("/admin/payments/new/<int:booking_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_payments_new(booking_id):
    db = cur = None
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)

        # FIX: fetch full booking info including dates, room type, and the
        #      customer's user_id so payment is recorded against the customer
        cur.execute("""
            SELECT b.booking_id, b.user_id AS customer_user_id,
                   b.check_in, b.check_out,
                   u.username, u.email,
                   r.room_number, r.room_type, r.price_per_night
            FROM bookings b
            JOIN users u ON u.user_id = b.user_id
            JOIN rooms r ON r.room_id = b.room_id
            WHERE b.booking_id = %s
        """, (booking_id,))
        booking = cur.fetchone()

        if not booking:
            flash("Booking not found.", "danger")
            return redirect(url_for("admin_payments"))

        # Pre-calculate suggested amount from dates
        suggested_amount = None
        nights = None
        if booking["check_in"] and booking["check_out"]:
            ci = booking["check_in"]
            co = booking["check_out"]
            if hasattr(ci, "date"): ci = ci.date()
            if hasattr(co, "date"): co = co.date()
            nights = max((co - ci).days, 1)
            suggested_amount = round(float(booking["price_per_night"]) * nights, 2)

        if request.method == "POST":
            raw_amount = request.form.get("amount", "").strip()
            method = request.form.get("method", "")

            if not raw_amount or not method:
                flash("Amount and payment method are required.", "danger")
                return render_template(
                    "admin_payments_new.html",
                    booking=booking, nights=nights, suggested_amount=suggested_amount
                )

            try:
                amount = float(raw_amount)
            except ValueError:
                flash("Amount must be a number.", "danger")
                return render_template(
                    "admin_payments_new.html",
                    booking=booking, nights=nights, suggested_amount=suggested_amount
                )

            cur2 = db.cursor()
            try:
                # FIX: use customer_user_id (not admin's session user_id)
                cur2.execute("""
                    INSERT INTO payments (booking_id, user_id, amount, method, status, paid_at)
                    VALUES (%s, %s, %s, %s, 'paid', NOW())
                """, (booking_id, booking["customer_user_id"], amount, method))

                # Auto-confirm the booking and mark room occupied
                cur2.execute(
                    "UPDATE bookings SET status='confirmed' WHERE booking_id=%s",
                    (booking_id,)
                )
                cur2.execute("""
                    UPDATE rooms SET status='occupied'
                    WHERE room_id = (SELECT room_id FROM bookings WHERE booking_id=%s)
                """, (booking_id,))

                db.commit()
                flash("Payment recorded and booking confirmed.", "success")
                return redirect(url_for("admin_payments"))
            except mysql.connector.Error as exc:
                db.rollback()
                flash(f"DB error: {exc}", "danger")
            finally:
                cur2.close()

        return render_template(
            "admin_payments_new.html",
            booking=booking, nights=nights, suggested_amount=suggested_amount
        )

    except mysql.connector.Error as exc:
        flash(f"DB error: {exc}", "danger")
        return redirect(url_for("admin_payments"))
    finally:
        if cur: cur.close()
        if db:  db.close()


# =============================================================================
# ROUTES — CUSTOMER: PAYMENTS
# =============================================================================
@app.route("/customer/pay/<int:booking_id>")
@login_required
@role_required("customer")
def customer_pay_booking(booking_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        booking, nights, total = compute_booking_total(cur, booking_id, session["user_id"])

        if not booking:
            flash("Booking not found.", "warning")
            return redirect(url_for("customer_my_bookings"))
        if (booking.get("status") or "").lower() != "pending":
            flash("Only pending bookings can be paid.", "warning")
            return redirect(url_for("customer_my_bookings"))

        return render_template(
            "customer_pay_now.html",
            booking=booking, nights=nights, total=total
        )
    finally:
        if cur: cur.close()
        if db:  db.close()


# ---------- STRIPE ----------
@app.route("/customer/pay/card/<int:booking_id>", methods=["POST"])
@login_required
@role_required("customer")
def pay_with_card(booking_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        booking, nights, total = compute_booking_total(cur, booking_id, session["user_id"])

        if not booking:
            flash("Invalid booking.", "danger")
            return redirect(url_for("customer_my_bookings"))
        if (booking.get("status") or "").lower() != "pending":
            flash("Only pending bookings can be paid.", "warning")
            return redirect(url_for("customer_my_bookings"))
        if not stripe.api_key:
            flash("Card payments are not configured. Please use M-PESA.", "danger")
            return redirect(url_for("customer_pay_booking", booking_id=booking_id))

        # Record initiated payment
        cur2 = db.cursor()
        cur2.execute("""
            INSERT INTO payments (booking_id, user_id, amount, method, status)
            VALUES (%s,%s,%s,'card','initiated')
        """, (booking_id, session["user_id"], total))
        db.commit()
        cur2.close()

        # Create Stripe checkout session
        stripe_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "kes",
                    "product_data": {"name": f"Room Booking #{booking_id}"},
                    "unit_amount": int(round(total * 100)),
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=url_for("stripe_success", booking_id=booking_id, _external=True),
            cancel_url=url_for("customer_pay_booking", booking_id=booking_id, _external=True),
        )
        return redirect(stripe_session.url)

    except stripe.error.StripeError as exc:
        flash(f"Stripe error: {exc.user_message}", "danger")
        return redirect(url_for("customer_pay_booking", booking_id=booking_id))
    finally:
        if cur: cur.close()
        if db:  db.close()


@app.route("/stripe/success/<int:booking_id>")
@login_required
@role_required("customer")
def stripe_success(booking_id):
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        booking, _, _ = compute_booking_total(cur, booking_id, session["user_id"])
        if not booking:
            flash("Booking not found.", "warning")
            return redirect(url_for("customer_my_bookings"))

        cur2 = db.cursor()
        cur2.execute("""
            UPDATE payments SET status='paid', paid_at=NOW()
            WHERE booking_id=%s AND user_id=%s AND method='card'
              AND status IN ('initiated','pending')
            ORDER BY payment_id DESC LIMIT 1
        """, (booking_id, session["user_id"]))
        cur2.execute(
            "UPDATE bookings SET status='confirmed' WHERE booking_id=%s AND user_id=%s",
            (booking_id, session["user_id"])
        )
        cur2.execute(
            "UPDATE rooms SET status='occupied' WHERE room_id=%s", (booking["room_id"],)
        )
        db.commit()
        cur2.close()
        flash("Payment successful. Booking confirmed!", "success")
        return redirect(url_for("customer_my_bookings"))
    finally:
        if cur: cur.close()
        if db:  db.close()


# ---------- M-PESA STK PUSH ----------
@app.route("/customer/pay/mpesa/<int:booking_id>", methods=["POST"])
@login_required
@role_required("customer")
def pay_with_mpesa(booking_id):
    # --- 1. Normalise phone ---
    phone_raw = request.form.get("phone", "").strip()
    print(f"[MPESA] Raw phone input: {repr(phone_raw)}")
    try:
        phone = normalize_phone_ke(phone_raw)
        print(f"[MPESA] Normalised phone: {phone}")
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("customer_pay_booking", booking_id=booking_id))

    db = cur = None
    try:
        # --- 2. Fetch booking ---
        db  = get_db()
        cur = db.cursor(dictionary=True)
        booking, nights, total = compute_booking_total(cur, booking_id, session["user_id"])

        if not booking:
            flash("Booking not found.", "danger")
            return redirect(url_for("customer_my_bookings"))
        if (booking.get("status") or "").lower() != "pending":
            flash("Only pending bookings can be paid.", "warning")
            return redirect(url_for("customer_my_bookings"))

        amount    = max(int(round(total)), 1)   # Safaricom minimum is KES 1
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password  = base64.b64encode(
            f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()
        ).decode()

        # --- 3. Get OAuth token ---
        try:
            token = mpesa_token()
        except ValueError as exc:
            flash(f"M-PESA setup error: {exc}", "danger")
            return redirect(url_for("customer_pay_booking", booking_id=booking_id))

        # --- 4. Send STK push ---
        stk_payload = {
            "BusinessShortCode": MPESA_SHORTCODE,
            "Password":          password,
            "Timestamp":         timestamp,
            "TransactionType":   "CustomerPayBillOnline",
            "Amount":            amount,
            "PartyA":            phone,
            "PartyB":            MPESA_SHORTCODE,
            "PhoneNumber":       phone,
            "CallBackURL":       MPESA_CALLBACK_URL,
            "AccountReference":  f"ALMS-{booking_id}",
            "TransactionDesc":   f"Room Booking #{booking_id}",
        }
        print(f"[MPESA] STK payload: {stk_payload}")

        try:
            stk_resp = requests.post(
                MPESA_STK_URL,
                json=stk_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
                timeout=30,
            )
            print(f"[MPESA] STK response [{stk_resp.status_code}]: {stk_resp.text}")
            stk_data = stk_resp.json()
        except requests.exceptions.RequestException as exc:
            flash(f"Network error sending STK push: {exc}", "danger")
            return redirect(url_for("customer_pay_booking", booking_id=booking_id))

        if str(stk_data.get("ResponseCode")) != "0":
            flash(
                f"STK push failed: {stk_data.get('ResponseDescription', 'Unknown error')}",
                "danger",
            )
            return redirect(url_for("customer_pay_booking", booking_id=booking_id))

        # --- 5. Save initiated payment record ---
        cur2 = db.cursor()
        cur2.execute("""
            INSERT INTO payments
                (booking_id, user_id, amount, method, status,
                 phone, merchant_request_id, checkout_request_id)
            VALUES (%s,%s,%s,'mpesa','initiated',%s,%s,%s)
        """, (
            booking_id,
            session["user_id"],
            amount,
            phone,
            stk_data.get("MerchantRequestID"),
            stk_data.get("CheckoutRequestID"),
        ))
        db.commit()
        cur2.close()

        flash(
            "STK push sent! Check your phone and enter your M-PESA PIN to complete payment.",
            "success",
        )
        return redirect(url_for("customer_my_bookings"))

    finally:
        if cur: cur.close()
        if db:  db.close()


# ---------- M-PESA CALLBACK (called by Safaricom servers) ----------
@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    payload = request.get_json(force=True, silent=True) or {}
    print(f"[MPESA] Callback received:\n{json.dumps(payload, indent=2)}")

    try:
        stk         = payload["Body"]["stkCallback"]
        checkout_id = stk.get("CheckoutRequestID")
        result_code = int(stk.get("ResultCode", 999))
        result_desc = stk.get("ResultDesc", "")
        receipt     = None

        if result_code == 0:
            items   = stk.get("CallbackMetadata", {}).get("Item", [])
            meta    = {i.get("Name"): i.get("Value") for i in items if "Name" in i}
            receipt = meta.get("MpesaReceiptNumber")
            print(f"[MPESA] Payment successful. Receipt: {receipt}")
        else:
            print(f"[MPESA] Payment failed: {result_desc}")

        db = cur = None
        try:
            db  = get_db()
            cur = db.cursor(dictionary=True)
            cur.execute("""
                SELECT payment_id, booking_id, status
                FROM payments
                WHERE checkout_request_id = %s
                LIMIT 1
            """, (checkout_id,))
            pay = cur.fetchone()

            if not pay:
                print(f"[MPESA] No payment record for checkout_id={checkout_id}")
                return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

            # Idempotency — skip if already finalised
            if (pay.get("status") or "").lower() in ("paid", "failed"):
                return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

            cur2 = db.cursor()
            if result_code == 0:
                cur2.execute("""
                    UPDATE payments
                    SET status='paid', paid_at=NOW(), mpesa_receipt=%s
                    WHERE payment_id=%s
                """, (receipt, pay["payment_id"]))
                cur2.execute(
                    "UPDATE bookings SET status='confirmed' WHERE booking_id=%s",
                    (pay["booking_id"],)
                )
                cur2.execute("""
                    UPDATE rooms SET status='occupied'
                    WHERE room_id = (SELECT room_id FROM bookings WHERE booking_id=%s)
                """, (pay["booking_id"],))
                print(f"[MPESA] Booking {pay['booking_id']} confirmed.")
            else:
                cur2.execute(
                    "UPDATE payments SET status='failed' WHERE payment_id=%s",
                    (pay["payment_id"],)
                )

            db.commit()
            cur2.close()

        finally:
            if cur: cur.close()
            if db:  db.close()

        # Safaricom expects this exact response format
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

    except Exception as exc:
        print(f"[MPESA] Callback error: {exc}")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"}), 200
        # Always return 200 + ResultCode 0 — Safaricom will retry on any other response

# =============================================================================
# ROUTES — CHAT BOT
# =============================================================================
@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    msg_lower = msg.lower()
    role = (session.get("role") or "").lower()

    if not msg_lower:
        return jsonify({"reply": "Please type a message."})

    # =========================================================================
    # HELPER: format room rows into readable text
    # =========================================================================
    def room_rows_to_text(rows, header):
        if not rows:
            return None
        lines = [header]
        for r in rows:
            lines.append(
                f"  • Room #{r['room_number']} — {r['room_type']}, "
                f"capacity {r['capacity']}, KES {r['price_per_night']}/night"
            )
        return "\n".join(lines)

    # =========================================================================
    # HELPER: run a simple DB query and return all rows
    # =========================================================================
    def db_query(sql, params=()):
        db = cur = None
        try:
            db = get_db()
            cur = db.cursor(dictionary=True)
            cur.execute(sql, params)
            return cur.fetchall()
        except Exception:
            return []
        finally:
            if cur: cur.close()
            if db:  db.close()

    def db_one(sql, params=()):
        rows = db_query(sql, params)
        return rows[0] if rows else {}

    # =========================================================================
    # GREETINGS
    # =========================================================================
    greetings = ["hello", "hi", "hey", "good morning", "good afternoon",
                 "good evening", "howdy", "hujambo", "habari"]
    if any(g in msg_lower for g in greetings):
        name = session.get("username", "there")
        if role == "admin":
            return jsonify({"reply": (
                f"Hello {name}! 👋 I'm your ALMS admin assistant.\n\n"
                "I can help you with:\n"
                "  • pending bookings\n"
                "  • unpaid bookings\n"
                "  • room status summary\n"
                "  • today's check-ins / check-outs\n"
                "  • revenue summary\n"
                "  • total rooms\n"
                "Type 'help' for the full list."
            )})
        else:
            return jsonify({"reply": (
                f"Hello {name}! 👋 Welcome to ALMS.\n\n"
                "I can help you with:\n"
                "  • available rooms\n"
                "  • rooms under a budget\n"
                "  • room types (single, double, suite…)\n"
                "  • your bookings\n"
                "  • how to book or cancel\n"
                "  • payment options\n"
                "Type 'help' for the full list."
            )})

    # =========================================================================
    # HELP
    # =========================================================================
    if "help" in msg_lower:
        if role == "admin":
            return jsonify({"reply": (
                "📋 Admin commands:\n\n"
                "  • 'pending bookings' — list unconfirmed bookings\n"
                "  • 'unpaid bookings' — bookings with no payment\n"
                "  • 'confirmed bookings' — today's confirmed list\n"
                "  • 'check-ins today' — guests arriving today\n"
                "  • 'check-outs today' — guests leaving today\n"
                "  • 'room summary' — count by status\n"
                "  • 'available rooms' — list available rooms\n"
                "  • 'occupied rooms' — list occupied rooms\n"
                "  • 'revenue' — total revenue from confirmed stays\n"
                "  • 'total rooms' — how many rooms exist\n"
                "  • 'total customers' — registered customer count\n"
                "  • 'recent payments' — last 5 payments received"
            )})
        else:
            return jsonify({"reply": (
                "📋 What I can help with:\n\n"
                "  • 'available rooms' — show all available rooms\n"
                "  • 'rooms under 5000' — budget filter\n"
                "  • 'single rooms' / 'double rooms' / 'suite' etc.\n"
                "  • 'my bookings' — see your reservations\n"
                "  • 'how to book' — step-by-step booking guide\n"
                "  • 'how to cancel' — cancellation instructions\n"
                "  • 'payment' — payment methods accepted\n"
                "  • 'check-in' / 'check-out' — policy info\n"
                "  • 'prices' — show all room prices\n"
                "  • 'most expensive room' / 'cheapest room'"
            )})

    # =========================================================================
    # ── ADMIN-ONLY INTENTS ────────────────────────────────────────────────────
    # =========================================================================
    if role == "admin":

        # Pending bookings
        if "pending" in msg_lower and "book" in msg_lower:
            rows = db_query("""
                SELECT b.booking_id, u.username, r.room_number,
                       b.check_in, b.check_out, b.status
                FROM bookings b
                JOIN users u ON u.user_id = b.user_id
                JOIN rooms r ON r.room_id = b.room_id
                WHERE b.status = 'pending'
                ORDER BY b.booking_id DESC LIMIT 8
            """)
            if not rows:
                return jsonify({"reply": "✅ No pending bookings right now."})
            lines = [f"⏳ Pending bookings ({len(rows)} shown):"]
            for b in rows:
                lines.append(
                    f"  • #{b['booking_id']} — {b['username']}, Room {b['room_number']}, {b['check_in']} → {b['check_out']}")
            return jsonify({"reply": "\n".join(lines)})

        # Unpaid bookings
        if "unpaid" in msg_lower or ("payment" in msg_lower and "no" in msg_lower):
            rows = db_query("""
                SELECT b.booking_id, u.username, r.room_number, b.check_in, b.check_out
                FROM bookings b
                JOIN users u ON u.user_id = b.user_id
                JOIN rooms r ON r.room_id = b.room_id
                LEFT JOIN payments p ON p.booking_id = b.booking_id
                WHERE b.status = 'pending'
                  AND (p.payment_id IS NULL OR p.status != 'paid')
                ORDER BY b.booking_id DESC LIMIT 8
            """)
            if not rows:
                return jsonify({"reply": "✅ No unpaid bookings found."})
            lines = [f"💳 Unpaid bookings ({len(rows)} shown):"]
            for b in rows:
                lines.append(
                    f"  • #{b['booking_id']} — {b['username']}, Room {b['room_number']}, {b['check_in']} → {b['check_out']}")
            return jsonify({"reply": "\n".join(lines)})

        # Confirmed bookings
        if "confirmed" in msg_lower and "book" in msg_lower:
            rows = db_query("""
                SELECT b.booking_id, u.username, r.room_number, b.check_in, b.check_out
                FROM bookings b
                JOIN users u ON u.user_id = b.user_id
                JOIN rooms r ON r.room_id = b.room_id
                WHERE b.status = 'confirmed'
                ORDER BY b.check_in ASC LIMIT 8
            """)
            if not rows:
                return jsonify({"reply": "No confirmed bookings found."})
            lines = [f"✅ Confirmed bookings ({len(rows)} shown):"]
            for b in rows:
                lines.append(
                    f"  • #{b['booking_id']} — {b['username']}, Room {b['room_number']}, {b['check_in']} → {b['check_out']}")
            return jsonify({"reply": "\n".join(lines)})

        # Check-ins today
        if "check" in msg_lower and "in" in msg_lower and "today" in msg_lower:
            rows = db_query("""
                SELECT b.booking_id, u.username, r.room_number, b.guests
                FROM bookings b
                JOIN users u ON u.user_id = b.user_id
                JOIN rooms r ON r.room_id = b.room_id
                WHERE DATE(b.check_in) = CURDATE()
                  AND b.status IN ('confirmed','pending')
                ORDER BY b.booking_id
            """)
            if not rows:
                return jsonify({"reply": "📅 No check-ins scheduled for today."})
            lines = [f"📅 Today's check-ins ({len(rows)}):"]
            for b in rows:
                lines.append(
                    f"  • #{b['booking_id']} — {b['username']}, Room {b['room_number']}, {b['guests']} guest(s)")
            return jsonify({"reply": "\n".join(lines)})

        # Check-outs today
        if "check" in msg_lower and "out" in msg_lower and "today" in msg_lower:
            rows = db_query("""
                SELECT b.booking_id, u.username, r.room_number
                FROM bookings b
                JOIN users u ON u.user_id = b.user_id
                JOIN rooms r ON r.room_id = b.room_id
                WHERE DATE(b.check_out) = CURDATE()
                  AND b.status = 'confirmed'
                ORDER BY b.booking_id
            """)
            if not rows:
                return jsonify({"reply": "📅 No check-outs scheduled for today."})
            lines = [f"📅 Today's check-outs ({len(rows)}):"]
            for b in rows:
                lines.append(f"  • #{b['booking_id']} — {b['username']}, Room {b['room_number']}")
            return jsonify({"reply": "\n".join(lines)})

        # Room summary / status
        if "room" in msg_lower and any(w in msg_lower for w in ["summary", "status", "overview", "count", "how many"]):
            row = db_one("""
                SELECT
                    COUNT(*) AS total,
                    SUM(status='available') AS available,
                    SUM(status='occupied')  AS occupied,
                    SUM(status='reserved')  AS reserved,
                    SUM(status='maintenance') AS maintenance
                FROM rooms
            """)
            return jsonify({"reply": (
                f"🏨 Room Summary:\n"
                f"  • Total:       {row.get('total', 0)}\n"
                f"  • Available:   {row.get('available', 0)}\n"
                f"  • Occupied:    {row.get('occupied', 0)}\n"
                f"  • Reserved:    {row.get('reserved', 0)}\n"
                f"  • Maintenance: {row.get('maintenance', 0)}"
            )})

        # Total rooms
        if "total" in msg_lower and "room" in msg_lower:
            row = db_one("SELECT COUNT(*) AS n FROM rooms")
            return jsonify({"reply": f"🏨 There are {row.get('n', 0)} rooms in the system."})

        # Total customers
        if "customer" in msg_lower or "guest" in msg_lower and "total" in msg_lower:
            row = db_one("SELECT COUNT(*) AS n FROM users WHERE role='customer'")
            return jsonify({"reply": f"👤 There are {row.get('n', 0)} registered customers."})

        # Occupied rooms
        if "occupied" in msg_lower and "room" in msg_lower:
            rows = db_query("""
                SELECT r.room_number, r.room_type, r.price_per_night
                FROM rooms r WHERE r.status = 'occupied'
                ORDER BY r.room_number
            """)
            if not rows:
                return jsonify({"reply": "No rooms are currently occupied."})
            lines = [f"🔴 Occupied rooms ({len(rows)}):"]
            for r in rows:
                lines.append(f"  • Room #{r['room_number']} — {r['room_type']}, KES {r['price_per_night']}/night")
            return jsonify({"reply": "\n".join(lines)})

        # Revenue
        if "revenue" in msg_lower or "earning" in msg_lower or "income" in msg_lower:
            row = db_one("""
                SELECT
                    COALESCE(SUM(DATEDIFF(b.check_out, b.check_in) * r.price_per_night), 0) AS total,
                    COUNT(*) AS bookings
                FROM bookings b
                JOIN rooms r ON r.room_id = b.room_id
                WHERE b.status IN ('confirmed','checked_out')
            """)
            total = float(row.get("total", 0))
            count = row.get("bookings", 0)
            return jsonify({"reply": (
                f"💰 Revenue Summary:\n"
                f"  • Total revenue:    KES {total:,.0f}\n"
                f"  • Paid bookings:    {count}\n"
                f"  • Avg per booking:  KES {(total / count):,.0f}" if count else
                f"💰 Total revenue: KES {total:,.0f} from {count} booking(s)."
            )})

        # Recent payments
        if "recent payment" in msg_lower or ("payment" in msg_lower and "recent" in msg_lower):
            rows = db_query("""
                SELECT p.amount, p.method, p.paid_at, u.username, r.room_number
                FROM payments p
                JOIN bookings b ON b.booking_id = p.booking_id
                JOIN users u ON u.user_id = b.user_id
                JOIN rooms r ON r.room_id = b.room_id
                WHERE p.status = 'paid'
                ORDER BY p.paid_at DESC LIMIT 5
            """)
            if not rows:
                return jsonify({"reply": "No payments recorded yet."})
            lines = ["💳 Recent payments:"]
            for p in rows:
                lines.append(
                    f"  • {p['username']} — Room {p['room_number']}, KES {p['amount']} via {p['method']} on {p['paid_at']}")
            return jsonify({"reply": "\n".join(lines)})

        # Available rooms (admin)
        if "available" in msg_lower and "room" in msg_lower:
            rows = db_query("""
                SELECT room_number, room_type, capacity, price_per_night
                FROM rooms WHERE status='available'
                ORDER BY price_per_night ASC LIMIT 8
            """)
            text = room_rows_to_text(rows, "🟢 Available rooms:")
            return jsonify({"reply": text or "No available rooms at the moment."})

        # Fallback for admin
        return jsonify({"reply": (
            "I didn't quite understand that. Try:\n"
            "  • 'pending bookings'\n"
            "  • 'unpaid bookings'\n"
            "  • 'check-ins today'\n"
            "  • 'check-outs today'\n"
            "  • 'room summary'\n"
            "  • 'revenue'\n"
            "  • 'total customers'\n"
            "  • 'recent payments'\n"
            "Or type 'help' for the full list."
        )})

    # =========================================================================
    # ── CUSTOMER INTENTS ──────────────────────────────────────────────────────
    # =========================================================================

    # How to book
    if any(p in msg_lower for p in ["how to book", "how do i book", "booking steps", "make a booking", "book a room"]):
        return jsonify({"reply": (
            "📋 How to book a room:\n\n"
            "  1. Go to Available Rooms\n"
            "  2. Browse and click Book on a room\n"
            "  3. Choose your check-in and check-out dates\n"
            "  4. Enter number of guests\n"
            "  5. Click Submit Booking\n"
            "  6. Go to My Bookings → Pay Now to complete payment\n\n"
            "Your booking is confirmed automatically after payment! ✅"
        )})

    # How to cancel
    if any(p in msg_lower for p in ["how to cancel", "how do i cancel", "cancel booking", "cancel my booking"]):
        return jsonify({"reply": (
            "❌ How to cancel a booking:\n\n"
            "  1. Go to My Bookings\n"
            "  2. Find the booking you want to cancel\n"
            "  3. Click the Cancel button\n\n"
            "⚠️ Note: Only bookings with status Pending can be cancelled.\n"
            "Once confirmed, contact the front desk for assistance."
        )})

    # Payment info
    if any(p in msg_lower for p in ["payment", "how to pay", "pay", "mpesa", "m-pesa", "card", "stripe"]):
        return jsonify({"reply": (
            "💳 Payment Methods:\n\n"
            "  • M-PESA STK Push — enter your phone number and confirm on your phone\n"
            "  • Card (Stripe) — redirected to secure Stripe checkout\n\n"
            "After successful payment, your booking is automatically confirmed. ✅\n"
            "Go to My Bookings → Pay Now to pay for a pending booking."
        )})

    # Check-in / check-out policy
    if "check" in msg_lower and "in" in msg_lower:
        return jsonify({
                           "reply": "🕐 Check-in time is from 2:00 PM. Early check-in may be available on request — contact the front desk."})
    if "check" in msg_lower and "out" in msg_lower:
        return jsonify(
            {"reply": "🕐 Check-out time is by 11:00 AM. Late check-out may be available — contact the front desk."})

    # Prices / all rooms
    if any(p in msg_lower for p in ["price", "prices", "how much", "cost", "rate", "rates", "tariff"]):
        rows = db_query("""
            SELECT room_number, room_type, capacity, price_per_night
            FROM rooms WHERE status='available'
            ORDER BY price_per_night ASC LIMIT 10
        """)
        text = room_rows_to_text(rows, "💰 Room prices (available rooms):")
        return jsonify({"reply": text or "No available rooms at the moment."})

    # Cheapest room
    if any(p in msg_lower for p in ["cheapest", "lowest price", "most affordable", "budget"]):
        row = db_one("""
            SELECT room_number, room_type, capacity, price_per_night
            FROM rooms WHERE status='available'
            ORDER BY price_per_night ASC LIMIT 1
        """)
        if row:
            return jsonify({
                               "reply": f"💚 Most affordable room:\n  • Room #{row['room_number']} — {row['room_type']}, capacity {row['capacity']}, KES {row['price_per_night']}/night"})
        return jsonify({"reply": "No available rooms at the moment."})

    # Most expensive room
    if any(p in msg_lower for p in ["expensive", "luxury", "most expensive", "premium", "highest price"]):
        row = db_one("""
            SELECT room_number, room_type, capacity, price_per_night
            FROM rooms WHERE status='available'
            ORDER BY price_per_night DESC LIMIT 1
        """)
        if row:
            return jsonify({
                               "reply": f"⭐ Most premium room:\n  • Room #{row['room_number']} — {row['room_type']}, capacity {row['capacity']}, KES {row['price_per_night']}/night"})
        return jsonify({"reply": "No available rooms at the moment."})

    # Available rooms
    if "available" in msg_lower and "room" in msg_lower or "available room" in msg_lower:
        rows = db_query("""
            SELECT room_number, room_type, capacity, price_per_night
            FROM rooms WHERE status='available'
            ORDER BY price_per_night ASC LIMIT 8
        """)
        text = room_rows_to_text(rows, "🟢 Available rooms:")
        return jsonify({"reply": text or "No available rooms at the moment."})

    # Budget search — "rooms under 5000"
    import re as _re
    m = _re.search(r"(under|below|less than|cheaper than|max)\s*(?:kes)?\s*(\d+)", msg_lower)
    if m:
        limit = float(m.group(2))
        rows = db_query("""
            SELECT room_number, room_type, capacity, price_per_night
            FROM rooms WHERE status='available' AND price_per_night <= %s
            ORDER BY price_per_night ASC LIMIT 8
        """, (limit,))
        text = room_rows_to_text(rows, f"💚 Rooms under KES {int(limit)}:")
        return jsonify({"reply": text or f"No rooms available under KES {int(limit)}."})

    # Room type search
    for rtype in ("single", "double", "twin", "triple", "deluxe", "suite",
                  "standard", "executive", "family", "penthouse", "studio"):
        if rtype in msg_lower:
            rows = db_query("""
                SELECT room_number, room_type, capacity, price_per_night
                FROM rooms WHERE status='available' AND LOWER(room_type) LIKE %s
                ORDER BY price_per_night ASC LIMIT 8
            """, (f"%{rtype}%",))
            text = room_rows_to_text(rows, f"🛏️ Available {rtype} rooms:")
            return jsonify({"reply": text or f"No {rtype} rooms available right now."})

    # My bookings
    if "my booking" in msg_lower or "my reservation" in msg_lower:
        rows = db_query("""
            SELECT b.booking_id, b.check_in, b.check_out, b.status,
                   r.room_number, r.room_type,
                   p.status AS payment_status
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            LEFT JOIN payments p ON p.booking_id = b.booking_id
            WHERE b.user_id = %s
            ORDER BY b.booking_id DESC LIMIT 5
        """, (session["user_id"],))
        if not rows:
            return jsonify({"reply": "You have no bookings yet. Browse available rooms to get started!"})
        lines = ["📋 Your latest bookings:"]
        for b in rows:
            pay = b.get("payment_status") or "unpaid"
            lines.append(
                f"  • #{b['booking_id']} — Room {b['room_number']} ({b['room_type']})\n"
                f"    {b['check_in']} → {b['check_out']} | Status: {b['status']} | Payment: {pay}"
            )
        return jsonify({"reply": "\n".join(lines)})

    # Booking status for specific ID
    m2 = _re.search(r"booking\s*#?(\d+)", msg_lower)
    if m2:
        bid = int(m2.group(1))
        row = db_one("""
            SELECT b.booking_id, b.check_in, b.check_out, b.status, b.guests,
                   r.room_number, r.room_type, r.price_per_night,
                   p.status AS payment_status, p.method
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            LEFT JOIN payments p ON p.booking_id = b.booking_id
            WHERE b.booking_id = %s AND b.user_id = %s
        """, (bid, session["user_id"]))
        if not row:
            return jsonify({"reply": f"Booking #{bid} not found in your account."})
        return jsonify({"reply": (
                f"📋 Booking #{row['booking_id']}:\n"
                f"  • Room:    #{row['room_number']} ({row['room_type']})\n"
                f"  • Dates:   {row['check_in']} → {row['check_out']}\n"
                f"  • Guests:  {row['guests']}\n"
                f"  • Status:  {row['status']}\n"
                f"  • Payment: {row.get('payment_status') or 'unpaid'}"
                + (f" via {row['method']}" if row.get('method') else "")
        )})

    # Wifi / amenities
    if any(p in msg_lower for p in ["wifi", "wi-fi", "internet", "amenity", "amenities", "facilities"]):
        return jsonify({
                           "reply": "📶 Amenities vary by room type. Common facilities include free Wi-Fi, parking, and breakfast options. Contact the front desk for specific room amenities."})

    # Contact
    if any(p in msg_lower for p in ["contact", "phone", "call", "email", "reach", "front desk"]):
        return jsonify({
                           "reply": "📞 To reach the front desk, please visit reception or contact the hotel directly. You can also reach us via the contact information on the main website."})

    # Thank you
    if any(p in msg_lower for p in ["thank", "thanks", "asante", "appreciated"]):
        return jsonify({"reply": "You're welcome! 😊 Is there anything else I can help you with?"})

    # =========================================================================
    # FALLBACK
    # =========================================================================
    return jsonify({"reply": (
        "I'm not sure I understood that. Here's what I can help with:\n\n"
        "  • 'available rooms'\n"
        "  • 'rooms under 3000'\n"
        "  • 'suite rooms'\n"
        "  • 'my bookings'\n"
        "  • 'how to book'\n"
        "  • 'how to cancel'\n"
        "  • 'payment options'\n"
        "  • 'cheapest room'\n\n"
        "Type 'help' for the full list."
    )})


# =============================================================================
# ROUTES — MAP (MySQL, not SQLite)
# =============================================================================
@app.route("/map")
@login_required
def map_view():
    db = cur = None
    try:
        db  = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("""
            SELECT room_number, latitude, longitude, price_per_night
            FROM rooms
            WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        """)
        return render_template("map.html", rooms=cur.fetchall())
    except mysql.connector.Error as exc:
        flash(f"Database error: {exc}", "danger")
        return redirect(url_for("customer_dashboard"))
    finally:
        if cur: cur.close()
        if db:  db.close()

# =============================================================================
# DEBUG ROUTE — remove before going live
# =============================================================================
@app.route("/debug-config")
def debug_config():
    """
    Visit /debug-config to verify all credentials loaded correctly.
    DELETE THIS ROUTE before deploying to production.
    """
    return jsonify({
        "mpesa_env":                 MPESA_ENV,
        "mpesa_consumer_key_set":    bool(MPESA_CONSUMER_KEY),
        "mpesa_consumer_secret_set": bool(MPESA_CONSUMER_SECRET),
        "mpesa_passkey_set":         bool(MPESA_PASSKEY),
        "mpesa_shortcode":           MPESA_SHORTCODE,
        "mpesa_callback_url":        MPESA_CALLBACK_URL,
        "callback_url_configured":   "YOUR_NGROK_URL" not in MPESA_CALLBACK_URL,
        "stripe_key_set":            bool(stripe.api_key),
        "flask_secret_set":          bool(app.secret_key),
    })
# =============================================================================
# 1. ADD this new route to app.py  (customer booking history / report)
# =============================================================================

@app.route("/customer/report")
@login_required
@role_required("customer")
def customer_report():
    db = cur = None
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)

        # All bookings with payment info joined
        cur.execute("""
            SELECT b.booking_id, b.check_in, b.check_out, b.guests, b.status,
                   r.room_number, r.room_type, r.price_per_night,
                   p.status AS payment_status, p.method AS payment_method, p.paid_at
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            LEFT JOIN payments p ON p.booking_id = b.booking_id
            WHERE b.user_id = %s
            ORDER BY b.booking_id DESC
        """, (session["user_id"],))
        bookings = cur.fetchall()

        # Summary stats
        summary = {
            "total": len(bookings),
            "confirmed": sum(1 for b in bookings if (b["status"] or "").lower() == "confirmed"),
            "pending":   sum(1 for b in bookings if (b["status"] or "").lower() == "pending"),
            "total_spent": 0.0,
        }

        for b in bookings:
            if (b["status"] or "").lower() in ("confirmed", "checked_out"):
                if b["check_in"] and b["check_out"]:
                    ci = b["check_in"]
                    co = b["check_out"]
                    if hasattr(ci, "date"): ci = ci.date()
                    if hasattr(co, "date"): co = co.date()
                    nights = max((co - ci).days, 1)
                    summary["total_spent"] += float(b["price_per_night"] or 0) * nights

        return render_template("customer_report.html", bookings=bookings, summary=summary)

    except mysql.connector.Error as exc:
        flash(f"Database error: {exc}", "danger")
        return redirect(url_for("customer_dashboard"))
    finally:
        if cur: cur.close()
        if db:  db.close()


# =============================================================================
# 2. UPDATE your customer_dashboard route to pass my_bookings AND show report link
#    (no change needed to the route logic — just update customer_dashboard.html below)
# =============================================================================


# =============================================================================
# ENTRY POINT
# =============================================================================
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

