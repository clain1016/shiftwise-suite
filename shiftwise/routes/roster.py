"""Employee roster and workforce configuration routes."""
import os
import smtplib
import sqlite3
import urllib.error
from datetime import date

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import generate_password_hash

from shiftwise.auth import login_required
from shiftwise.db import db, monday_of
from shiftwise.domain.rules import valid_email, valid_phone
from shiftwise.notify import notify, send_schedule_link
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
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    try:
        weekly_hours = int(request.form.get("weekly_hours", ""))
    except ValueError:
        weekly_hours = 0
    if not username or not name or len(password) < 12 or \
            employment_type not in ("full_time", "part_time") or \
            not 1 <= weekly_hours <= 168 or not valid_email(email) or \
            not valid_phone(phone):
        flash("Enter a name, username, password of at least 12 characters, "
              "employment type, valid weekly hours, email, and E.164 phone number (+country code).")
        return redirect(url_for("roster.roster"))
    if hired:
        try:
            date.fromisoformat(hired)
        except ValueError:
            flash("Enter a valid hire date.")
            return redirect(url_for("roster.roster"))
    conn = db()
    try:
        conn.execute(
            "INSERT INTO users (username, password, name, role, weekly_hours, "
            "employment_type, hired_on, email, phone) VALUES (?,?,?,?,?,?,?,?,?)",
            (username, generate_password_hash(password), name, "employee",
             weekly_hours, employment_type, hired or None, email or None, phone or None))
        conn.commit()
    except sqlite3.IntegrityError:
        flash("That username is already in use.")
    else:
        flash("Employee added.")
    finally:
        conn.close()
    return redirect(url_for("roster.roster"))


@roster_bp.route("/manager/roster/<int:user_id>/send-link", methods=["POST"])
@login_required(role="manager")
def send_employee_schedule_link(user_id):
    channel = request.form.get("channel", "")
    if channel not in ("email", "sms"):
        flash("Choose email or text delivery.")
        return redirect(url_for("roster.roster"))
    public_url = (current_app.config.get("SHIFTWISE_PUBLIC_URL") or
                  os.environ.get("SHIFTWISE_PUBLIC_URL", "")).strip().rstrip("/")
    if not public_url.startswith("https://"):
        flash("Set SHIFTWISE_PUBLIC_URL to the app's public HTTPS address before sending links.")
        return redirect(url_for("roster.roster"))
    conn = db()
    employee = conn.execute(
        "SELECT name, email, phone FROM users WHERE id=? AND role='employee'",
        (user_id,)).fetchone()
    conn.close()
    if not employee:
        abort(404)
    destination = employee["email"] if channel == "email" else employee["phone"]
    if not destination:
        flash(
            f"Add this employee's {'email address' if channel == 'email' else 'phone number'}"
            " first."
        )
        return redirect(url_for("roster.roster"))
    try:
        import sys
        appmod = sys.modules.get("app")
        sender = (
            getattr(appmod, "send_schedule_link", send_schedule_link)
            if appmod
            else send_schedule_link
        )
        sender(channel, destination, employee["name"],
               public_url + url_for("auth.login"))
    except (ValueError, OSError, smtplib.SMTPException, urllib.error.URLError) as exc:
        flash(str(exc) or "Message could not be sent. Check the delivery settings.")
    else:
        flash(
            f"Schedule link sent by {'email' if channel == 'email' else 'text'}"
            f" to {employee['name']}."
        )
    return redirect(url_for("roster.roster"))


@roster_bp.route("/manager/roster/<int:user_id>/password", methods=["POST"])
@login_required(role="manager")
def reset_employee_password(user_id):
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    if len(password) < 12:
        flash("Use a new password of at least 12 characters.")
        return redirect(url_for("roster.roster"))
    if password != confirm_password:
        flash("The passwords do not match.")
        return redirect(url_for("roster.roster"))
    conn = db()
    employee = conn.execute(
        "SELECT id FROM users WHERE id=? AND role='employee'", (user_id,)).fetchone()
    if not employee:
        conn.close()
        abort(404)
    conn.execute("UPDATE users SET password=? WHERE id=?",
                 (generate_password_hash(password), user_id))
    conn.commit()
    conn.close()
    flash("Employee password reset. Share the new password securely.")
    return redirect(url_for("roster.roster"))


@roster_bp.route("/manager/roster/<int:user_id>/delete", methods=["POST"])
@login_required(role="manager")
def delete_employee(user_id):
    if request.form.get("confirm") != "yes":
        flash("Confirm employee deletion before continuing.")
        return redirect(url_for("roster.roster"))
    conn = db()
    employee = conn.execute(
        "SELECT id FROM users WHERE id=? AND role='employee'", (user_id,)).fetchone()
    if not employee:
        conn.close()
        abort(404)
    # These tables have no cascading foreign keys; remove the employee's
    # history before removing the account to avoid dangling user references.
    pending_invites = conn.execute(
        "SELECT user_id FROM requests WHERE target_user_id=? "
        "AND kind='swap' AND status='approved'", (user_id,)).fetchall()
    for invite in pending_invites:
        notify(conn, invite["user_id"], "conflict",
               "Your shift swap request was cancelled because the invited employee was removed.")
    conn.execute("DELETE FROM requests WHERE target_user_id=?", (user_id,))
    for table in ("assignments", "picks", "coverage_preferences", "requests", "notifications"):
        conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=? AND role='employee'", (user_id,))
    conn.commit()
    conn.close()
    run_scheduler(monday_of(date.today()).isoformat())
    flash("Employee deleted; the current week's schedule was rebuilt.")
    return redirect(url_for("roster.roster"))


@roster_bp.route("/manager/roster", methods=["GET", "POST"])
@login_required(role="manager")
def roster():
    conn = db()
    if request.method == "POST":
        updates = []
        for row in conn.execute(
                "SELECT id, employment_type, hired_on, weekly_hours, station, email, phone"
                " FROM users WHERE role='employee'"):
            uid = row["id"]
            # Fields absent from the post keep their current values; fields
            # present are validated strictly before anything is written.
            et = request.form.get(f"type_{uid}", row["employment_type"])
            hired = request.form.get(f"hired_{uid}", row["hired_on"] or "").strip()
            cap = request.form.get(f"cap_{uid}", str(row["weekly_hours"] or "")).strip()
            station = request.form.get(f"station_{uid}", row["station"] or "front")
            email = request.form.get(f"email_{uid}", row["email"] or "").strip()
            phone = request.form.get(f"phone_{uid}", row["phone"] or "").strip()
            try:
                if hired:
                    date.fromisoformat(hired)
                hours_cap = int(cap)
            except ValueError:
                conn.close()
                flash("Enter a valid hire date and weekly hours cap.")
                return redirect(url_for("roster.roster"))
            if et not in ("full_time", "part_time") or not 1 <= hours_cap <= 168:
                conn.close()
                flash("Choose an employment type and a cap from 1 to 168 hours.")
                return redirect(url_for("roster.roster"))
            if station not in ("front", "back"):
                conn.close()
                flash("Choose a valid station (front/back).")
                return redirect(url_for("roster.roster"))
            if not valid_email(email) or not valid_phone(phone):
                conn.close()
                flash(
                    "Enter a valid email address and phone in international "
                    "format (+country code)."
                )
                return redirect(url_for("roster.roster"))
            updates.append((et, hired or None, hours_cap, station,
                            email or None, phone or None, uid))
        conn.executemany(
            "UPDATE users SET employment_type=?, hired_on=?, weekly_hours=?,"
            " station=?, email=?, phone=? WHERE id=?",
            updates)
        conn.commit()
        flash("Roster updated.")
        conn.close()
        # auto-rebuild: priority lineup may have changed -> re-run
        run_scheduler(monday_of(date.today()).isoformat())
        return redirect(url_for("roster.roster"))
    employees = conn.execute(
        "SELECT * FROM users WHERE role='employee' "
        "ORDER BY employment_type='full_time' DESC, hired_on ISNULL, hired_on").fetchall()
    conn.close()
    return render_template("roster.html", employees=employees)
