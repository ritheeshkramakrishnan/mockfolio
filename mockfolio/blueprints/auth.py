"""Authentication: register/login/logout, and password reset."""
import hashlib
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from mockfolio.db import get_db

bp = Blueprint("auth", __name__)


def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()


@bp.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("trading.dashboard"))
    return render_template("index.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json() or request.form
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (email, hash_pw(password))
        ).fetchone()
        if user:
            session["user_id"] = user["id"]
            return jsonify({"ok": True, "redirect": "/dashboard"})
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401
    return render_template("login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        data = request.get_json() or request.form
        username = data.get("username", "").strip()
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        if not username or not email or not password:
            return jsonify({"ok": False, "error": "All fields required"}), 400
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, email, password) VALUES (?,?,?)",
                (username, email, hash_pw(password))
            )
            db.commit()
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            session["user_id"] = user["id"]
            # record starting portfolio value
            from mockfolio.portfolio import record_snapshot
            record_snapshot(user["id"], db)
            db.commit()
            return jsonify({"ok": True, "redirect": "/dashboard"})
        except Exception:
            db.rollback()
            return jsonify({"ok": False, "error": "Username or email already taken"}), 409
    return render_template("register.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.index"))


# ── password reset ────────────────────────────────────────────────────────────
def _send_reset_email(to_email: str, reset_url: str):
    """Send reset email via SMTP if env vars are set, otherwise silently skip."""
    import os
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    if not (smtp_host and smtp_user and smtp_pass):
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        body = f"""Hi,

You requested a password reset for your MockFolio account.

Click the link below to set a new password (expires in 1 hour):
{reset_url}

If you didn't request this, ignore this email.

— MockFolio
"""
        msg = MIMEText(body)
        msg["Subject"] = "MockFolio — Reset your password"
        msg["From"] = smtp_user
        msg["To"] = to_email
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email] send failed: {e}")
        return False


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    data = request.get_json() or request.form
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "Email is required"}), 400

    db = get_db()
    user = db.execute("SELECT id, email FROM users WHERE email=?", (email,)).fetchone()

    # Always return ok=True to not leak whether email exists
    if not user:
        return jsonify({"ok": True, "emailed": False, "message": "If that email is registered you'll see the reset link below."})

    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    # clear any existing tokens for this user
    db.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (user["id"],))
    db.execute(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?,?,?)",
        (user["id"], token, expires)
    )
    db.commit()

    base_url = request.host_url.rstrip("/")
    reset_url = f"{base_url}/reset-password/{token}"
    emailed = _send_reset_email(email, reset_url)

    return jsonify({
        "ok": True,
        "emailed": emailed,
        "reset_url": reset_url,   # shown on-screen when no email configured
        "message": "Reset link sent to your email." if emailed else "Email not configured — use the link below."
    })


@bp.route("/reset-password/<token>", methods=["GET"])
def reset_password_page(token):
    return render_template("reset_password.html", token=token)


@bp.route("/api/reset-password", methods=["POST"])
def api_reset_password():
    data = request.get_json() or {}
    token = data.get("token", "")
    password = data.get("password", "")

    if not token or not password or len(password) < 6:
        return jsonify({"ok": False, "error": "Invalid request — password must be at least 6 characters"}), 400

    db = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > ?",
        (token, now)
    ).fetchone()

    if not row:
        return jsonify({"ok": False, "error": "This reset link has expired or already been used. Request a new one."}), 400

    db.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(password), row["user_id"]))
    db.execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", (row["id"],))
    db.commit()
    return jsonify({"ok": True})
