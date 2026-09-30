"""Employee roster and workforce configuration routes."""
import sqlite3
from datetime import date

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import generate_password_hash

from shiftwise.auth import login_required
from shiftwise.db import db, monday_of
from shiftwise.scheduler.engine import run_scheduler

roster_bp = Blueprint("roster", __name__)


@roster_bp.route("/manager/roster/add", methods=["POST"])
@login_required(role="manager")
def add_employee():
    username = request.form.get("username", "").strip()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    employment_type = request.form.get("employment_type", "")
    hired = request.form.get("hired_on", "").strip()
    try:
        weekly_hours = int(request.form.get("weekly_hours", ""))
    except ValueError:
        weekly_hours = 0
    if not username or not name or len(password) < 12 or \
            employment_type not in ("full_time", "part_time") or \
            not 1 <= weekly_hours <= 168:
        flash("Enter a name, username, password of at least 12 characters, "
              "employment type, and valid weekly hours.")
        return redirect(url_for("roster"))
    if hired:
        try:
            date.fromisoformat(hired)
        except ValueError:
            flash("Enter a valid hire date.")
            return redirect(url_for("roster"))
    conn = db()
    try:
        conn.execute(
            "INSERT INTO users (username, password, name, role, weekly_hours, "
            "employment_type, hired_on) VALUES (?,?,?,?,?,?,?)",
            (username, generate_password_hash(password), name, "employee",
             weekly_hours, employment_type, hired or None))
        conn.commit()
    except sqlite3.IntegrityError:
        flash("That username is already in use.")
    else:
        flash("Employee added.")
    finally:
        conn.close()
    return redirect(url_for("roster"))


@roster_bp.route("/manager/roster", methods=["GET", "POST"])
@login_required(role="manager")
def roster():
    conn = db()
    if request.method == "POST":
        updates = []
        for row in conn.execute(
                "SELECT id, employment_type, hired_on, weekly_hours, station"
                " FROM users WHERE role='employee'"):
            uid = row["id"]
            # Fields absent from the post keep their current values; fields
            # present are validated strictly before anything is written.
            et = request.form.get(f"type_{uid}", row["employment_type"])
            hired = request.form.get(f"hired_{uid}", row["hired_on"] or "").strip()
            cap = request.form.get(f"cap_{uid}", str(row["weekly_hours"] or "")).strip()
            station = request.form.get(f"station_{uid}", row["station"] or "front")
            try:
                if hired:
                    date.fromisoformat(hired)
                hours_cap = int(cap)
            except ValueError:
                conn.close()
                flash("Enter a valid hire date and weekly hours cap.")
                return redirect(url_for("roster"))
            if et not in ("full_time", "part_time") or not 1 <= hours_cap <= 168:
                conn.close()
                flash("Choose an employment type and a cap from 1 to 168 hours.")
                return redirect(url_for("roster"))
            if station not in ("front", "back"):
                conn.close()
                flash("Choose a valid station (front/back).")
                return redirect(url_for("roster"))
            updates.append((et, hired or None, hours_cap, station, uid))
        conn.executemany(
            "UPDATE users SET employment_type=?, hired_on=?, weekly_hours=?,"
            " station=? WHERE id=?",
            updates)
        conn.commit()
        flash("Roster updated.")
        conn.close()
        run_scheduler(monday_of(date.today()).isoformat())
        return redirect(url_for("roster"))
    employees = conn.execute(
        "SELECT * FROM users WHERE role='employee' "
        "ORDER BY employment_type='full_time' DESC, hired_on ISNULL, hired_on").fetchall()
    conn.close()
    return render_template("roster.html", employees=employees)
