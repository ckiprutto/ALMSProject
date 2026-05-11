from flask import Blueprint, render_template, request, session, redirect, url_for, flash
from functools import wraps
from datetime import datetime, date, timedelta
import mysql.connector
import os

reports_bp = Blueprint("reports", __name__, template_folder="templates")

# =============================================================================
# DB HELPER
# =============================================================================
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "0000"),
    "database": os.getenv("DB_NAME", "alms_db"),
}

def get_db():
    return mysql.connector.connect(**DB_CONFIG)

# =============================================================================
# AUTH GUARD
# =============================================================================
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        if (session.get("role") or "").lower() != "admin":
            flash("Admin access required.", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper

# =============================================================================
# HELPERS
# =============================================================================
def _date_range_from_request():
    """
    Read ?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD from query string.
    Defaults: last 30 days.
    """
    today = date.today()
    raw_from = request.args.get("from_date", "")
    raw_to   = request.args.get("to_date", "")
    try:
        from_date = datetime.strptime(raw_from, "%Y-%m-%d").date() if raw_from else today - timedelta(days=30)
    except ValueError:
        from_date = today - timedelta(days=30)
    try:
        to_date = datetime.strptime(raw_to, "%Y-%m-%d").date() if raw_to else today
    except ValueError:
        to_date = today
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    return from_date, to_date

# =============================================================================
# ROUTES
# =============================================================================

# ---------- Reports Index ----------
@reports_bp.route("/")
@admin_required
def reports_index():
    return render_template("reports/index.html")


# ---------- Bookings Summary ----------
@reports_bp.route("/bookings")
@admin_required
def bookings_report():
    from_date, to_date = _date_range_from_request()
    db = cur = None
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)

        # Overall counts by status
        cur.execute("""
            SELECT status, COUNT(*) AS total
            FROM bookings
            WHERE check_in BETWEEN %s AND %s
            GROUP BY status
        """, (from_date, to_date))
        status_counts = cur.fetchall()

        # Daily bookings trend
        cur.execute("""
            SELECT DATE(check_in) AS day, COUNT(*) AS bookings
            FROM bookings
            WHERE check_in BETWEEN %s AND %s
            GROUP BY DATE(check_in)
            ORDER BY day
        """, (from_date, to_date))
        daily_trend = cur.fetchall()

        # Top booked room types
        cur.execute("""
            SELECT r.room_type, COUNT(*) AS bookings
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            WHERE b.check_in BETWEEN %s AND %s
            GROUP BY r.room_type
            ORDER BY bookings DESC
        """, (from_date, to_date))
        room_type_counts = cur.fetchall()

        # Recent bookings list (fixed: removed broken LIMIT text)
        cur.execute("""
            SELECT b.booking_id, b.check_in, b.check_out, b.guests, b.status,
                   r.room_number, r.room_type, r.price_per_night,
                   u.username, u.email,
                   DATEDIFF(b.check_out, b.check_in) AS nights,
                   (DATEDIFF(b.check_out, b.check_in) * r.price_per_night) AS total_amount
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            JOIN users u ON u.user_id = b.user_id
            WHERE b.check_in BETWEEN %s AND %s
            ORDER BY b.booking_id DESC
            LIMIT 50
        """, (from_date, to_date))
        bookings = cur.fetchall()

        total_bookings = sum(s["total"] for s in status_counts)

        return render_template(
            "reports/bookings.html",
            from_date=from_date, to_date=to_date,
            status_counts=status_counts,
            daily_trend=daily_trend,
            room_type_counts=room_type_counts,
            bookings=bookings,
            total_bookings=total_bookings,
        )
    finally:
        if cur: cur.close()
        if db:  db.close()


# ---------- Revenue & Payments ----------
@reports_bp.route("/revenue")
@admin_required
def revenue_report():
    from_date, to_date = _date_range_from_request()
    db = cur = None
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)

        # Total revenue in period (confirmed/checked_out bookings)
        cur.execute("""
            SELECT
                COALESCE(SUM(DATEDIFF(b.check_out, b.check_in) * r.price_per_night), 0) AS total_revenue,
                COUNT(*) AS paid_bookings
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            WHERE b.status IN ('confirmed', 'checked_out')
              AND b.check_in BETWEEN %s AND %s
        """, (from_date, to_date))
        revenue_summary = cur.fetchone()

        # Monthly revenue breakdown
        cur.execute("""
            SELECT
                DATE_FORMAT(b.check_in, '%%Y-%%m') AS month,
                COALESCE(SUM(DATEDIFF(b.check_out, b.check_in) * r.price_per_night), 0) AS revenue,
                COUNT(*) AS bookings
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            WHERE b.status IN ('confirmed', 'checked_out')
              AND b.check_in BETWEEN %s AND %s
            GROUP BY month
            ORDER BY month
        """, (from_date, to_date))
        monthly_revenue = cur.fetchall()

        # Revenue by room type
        cur.execute("""
            SELECT
                r.room_type,
                COALESCE(SUM(DATEDIFF(b.check_out, b.check_in) * r.price_per_night), 0) AS revenue,
                COUNT(*) AS bookings
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            WHERE b.status IN ('confirmed', 'checked_out')
              AND b.check_in BETWEEN %s AND %s
            GROUP BY r.room_type
            ORDER BY revenue DESC
        """, (from_date, to_date))
        revenue_by_type = cur.fetchall()

        # Recent paid bookings
        cur.execute("""
            SELECT b.booking_id, b.check_in, b.check_out, b.status,
                   r.room_number, r.room_type, r.price_per_night,
                   u.username,
                   DATEDIFF(b.check_out, b.check_in) AS nights,
                   (DATEDIFF(b.check_out, b.check_in) * r.price_per_night) AS amount
            FROM bookings b
            JOIN rooms r ON r.room_id = b.room_id
            JOIN users u ON u.user_id = b.user_id
            WHERE b.status IN ('confirmed', 'checked_out')
              AND b.check_in BETWEEN %s AND %s
            ORDER BY b.booking_id DESC
            LIMIT 50
        """, (from_date, to_date))
        paid_bookings = cur.fetchall()

        return render_template(
            "reports/revenue.html",
            from_date=from_date, to_date=to_date,
            revenue_summary=revenue_summary,
            monthly_revenue=monthly_revenue,
            revenue_by_type=revenue_by_type,
            paid_bookings=paid_bookings,
        )
    finally:
        if cur: cur.close()
        if db:  db.close()


# ---------- Room Occupancy ----------
@reports_bp.route("/occupancy")
@admin_required
def occupancy_report():
    from_date, to_date = _date_range_from_request()
    db = cur = None
    try:
        db = get_db()
        cur = db.cursor(dictionary=True)

        # Total rooms
        cur.execute("SELECT COUNT(*) AS total FROM rooms")
        total_rooms = cur.fetchone()["total"]

        # Currently occupied
        cur.execute("SELECT COUNT(*) AS occupied FROM rooms WHERE status = 'occupied'")
        currently_occupied = cur.fetchone()["occupied"]

        # Occupancy per room (how many times booked in period)
        cur.execute("""
            SELECT r.room_number, r.room_type, r.price_per_night, r.status,
                   COUNT(b.booking_id) AS times_booked,
                   COALESCE(SUM(DATEDIFF(b.check_out, b.check_in)), 0) AS total_nights
            FROM rooms r
            LEFT JOIN bookings b
                ON b.room_id = r.room_id
               AND b.check_in BETWEEN %s AND %s
               AND b.status IN ('confirmed', 'checked_in', 'checked_out')
            GROUP BY r.room_id
            ORDER BY times_booked DESC
        """, (from_date, to_date))
        room_stats = cur.fetchall()

        # Occupancy rate by room type
        cur.execute("""
            SELECT r.room_type,
                   COUNT(DISTINCT r.room_id) AS total_rooms,
                   COUNT(DISTINCT b.booking_id) AS bookings,
                   COALESCE(SUM(DATEDIFF(b.check_out, b.check_in)), 0) AS nights_sold
            FROM rooms r
            LEFT JOIN bookings b
                ON b.room_id = r.room_id
               AND b.check_in BETWEEN %s AND %s
               AND b.status IN ('confirmed', 'checked_in', 'checked_out')
            GROUP BY r.room_type
            ORDER BY bookings DESC
        """, (from_date, to_date))
        type_occupancy = cur.fetchall()

        # Period length for occupancy % calculation
        period_days = max((to_date - from_date).days, 1)

        return render_template(
            "reports/occupancy.html",
            from_date=from_date, to_date=to_date,
            total_rooms=total_rooms,
            currently_occupied=currently_occupied,
            room_stats=room_stats,
            type_occupancy=type_occupancy,
            period_days=period_days,
        )
    finally:
        if cur: cur.close()
        if db:  db.close()

