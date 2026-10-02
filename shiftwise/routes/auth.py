"""Authentication routes: login, logout, and password management."""

import math

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from shiftwise.auth import (
    clear_login_failures,
    client_ip,
    lockout_remaining,
    login_required,
    note_login_failure,
    verify_credentials,
)
from shiftwise.db import db

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        ip = client_ip()
        conn = db()
        try:
            remaining = lockout_remaining(conn, username, ip)
            if remaining > 0:
                flash(
                    "Too many failed sign-in attempts — try again in "
                    f"{math.ceil(remaining / 60)} minute(s)."
                )
                return render_template("login.html")
            user = verify_credentials(conn, username, password)
            if user:
                clear_login_failures(conn, username, ip)
                conn.commit()
                # a fresh session id after sign-in, so a pre-login cookie
                # cannot be reused
                session.clear()
                session.update(uid=user["id"], role=user["role"], name=user["name"])
                return redirect(url_for("dashboard"))
            note_login_failure(conn, username, ip)
            conn.commit()
            flash("Wrong username or password")
        finally:
            conn.close()
    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@auth_bp.route("/account/password", methods=["GET", "POST"])
@login_required()
def change_password():
    if request.method == "POST":
        old_password = request.form.get("old_password", "")
        new_password = request.form.get("new_password", "")
        if len(new_password) < 12:
            flash("Use a new password of at least 12 characters.")
            return redirect(url_for("change_password"))
        conn = db()
        user = conn.execute("SELECT password FROM users WHERE id=?", (session["uid"],)).fetchone()
        if not user or not check_password_hash(user["password"], old_password):
            conn.close()
            flash("Current password is incorrect.")
            return redirect(url_for("change_password"))
        conn.execute(
            "UPDATE users SET password=? WHERE id=?",
            (generate_password_hash(new_password), session["uid"]),
        )
        conn.commit()
        conn.close()
        session.clear()
        flash("Password changed. Sign in again.")
        return redirect(url_for("login"))
    return render_template("password.html")
