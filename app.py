from flask import Flask, request, redirect, url_for, session, render_template_string, flash, jsonify, send_file
import sqlite3
import os
import json
import urllib.request
import urllib.error
import urllib.parse
import uuid
import io
import csv
import html
from functools import wraps
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE-ME-IN-RENDER")

DATABASE = os.environ.get("DATABASE_PATH", "ajoconnect.db")
PREMIUM_PRICE = 2000
FREE_GROUP_LIMIT = 1
FREE_MEMBER_LIMIT = 10
MAX_RECEIPT_SIZE = 2 * 1024 * 1024  # 2 MB

PAYSTACK_PUBLIC_KEY = (os.environ.get("PAYSTACK_PUBLIC_KEY") or "").strip()
PAYSTACK_SECRET_KEY = (os.environ.get("PAYSTACK_SECRET_KEY") or "").strip()


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def add_column_if_missing(conn, table, column, definition):
    columns = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    conn = get_db()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        plan TEXT NOT NULL DEFAULT 'free',
        premium_until TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contribution REAL NOT NULL DEFAULT 0,
        frequency TEXT NOT NULL DEFAULT 'monthly',
        user_id INTEGER,
        created_at TEXT NOT NULL,
        invite_token TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        position INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS contributions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'paid',
        note TEXT,
        cycle_number INTEGER NOT NULL DEFAULT 1,
        receipt_data BLOB,
        receipt_filename TEXT,
        receipt_mime TEXT,
        approved_at TEXT,
        approved_by INTEGER,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
        FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payout_date TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        note TEXT,
        cycle_number INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
        FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan TEXT NOT NULL DEFAULT 'premium',
        amount REAL NOT NULL DEFAULT 2000,
        reference TEXT UNIQUE,
        status TEXT NOT NULL DEFAULT 'pending',
        started_at TEXT,
        expires_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Safe migrations for existing AjoConnect databases.
    migrations = [
        ("users", "role", "TEXT NOT NULL DEFAULT 'user'"),
        ("users", "plan", "TEXT NOT NULL DEFAULT 'free'"),
        ("users", "premium_until", "TEXT"),
        ("users", "created_at", "TEXT"),
        ("groups", "user_id", "INTEGER"),
        ("groups", "created_at", "TEXT"),
        ("groups", "invite_token", "TEXT"),
        ("members", "email", "TEXT"),
        ("members", "status", "TEXT NOT NULL DEFAULT 'active'"),
        ("members", "created_at", "TEXT"),
        ("contributions", "status", "TEXT NOT NULL DEFAULT 'paid'"),
        ("contributions", "note", "TEXT"),
        ("contributions", "cycle_number", "INTEGER NOT NULL DEFAULT 1"),
        ("contributions", "receipt_data", "BLOB"),
        ("contributions", "receipt_filename", "TEXT"),
        ("contributions", "receipt_mime", "TEXT"),
        ("contributions", "approved_at", "TEXT"),
        ("contributions", "approved_by", "INTEGER"),
        ("payouts", "status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("payouts", "note", "TEXT"),
        ("payouts", "cycle_number", "INTEGER NOT NULL DEFAULT 1"),
        ("subscriptions", "started_at", "TEXT"),
        ("subscriptions", "expires_at", "TEXT"),
    ]

    for table, column, definition in migrations:
        add_column_if_missing(conn, table, column, definition)

    conn.execute("UPDATE users SET plan='free' WHERE plan IS NULL OR plan=''")
    conn.execute("UPDATE users SET role='user' WHERE role IS NULL OR role=''")
    conn.execute("UPDATE users SET created_at=? WHERE created_at IS NULL OR created_at=''", (now(),))
    conn.execute("UPDATE groups SET created_at=? WHERE created_at IS NULL OR created_at=''", (now(),))
    conn.execute("UPDATE members SET status='active' WHERE status IS NULL OR status=''")
    conn.execute("UPDATE members SET created_at=? WHERE created_at IS NULL OR created_at=''", (now(),))
    conn.execute("UPDATE contributions SET cycle_number=1 WHERE cycle_number IS NULL OR cycle_number<1")
    conn.execute("UPDATE payouts SET cycle_number=1 WHERE cycle_number IS NULL OR cycle_number<1")

    first_user = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if first_user:
        conn.execute("UPDATE groups SET user_id=? WHERE user_id IS NULL", (first_user["id"],))

    # Give old groups invite tokens.
    old_groups = conn.execute(
        "SELECT id FROM groups WHERE invite_token IS NULL OR invite_token=''"
    ).fetchall()
    for group in old_groups:
        conn.execute(
            "UPDATE groups SET invite_token=? WHERE id=?",
            (uuid.uuid4().hex, group["id"])
        )

    admin_email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    if admin_email:
        conn.execute(
            "UPDATE users SET role='user' WHERE role='admin' AND lower(email)<>?",
            (admin_email,)
        )
        conn.execute("UPDATE users SET role='admin' WHERE lower(email)=?", (admin_email,))
    elif first_user:
        old_admin = conn.execute(
            "SELECT id FROM users WHERE role='admin' LIMIT 1"
        ).fetchone()
        if not old_admin:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (first_user["id"],))

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def money(value):
    return f"₦{float(value or 0):,.2f}"


def safe(value):
    return html.escape(str(value or ""))


app.jinja_env.filters["money"] = money


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    try:
        return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()


def is_premium_user(user_id):
    conn = get_db()
    user = conn.execute(
        "SELECT plan,premium_until FROM users WHERE id=?",
        (user_id,)
    ).fetchone()
    conn.close()

    if not user or user["plan"] != "premium" or not user["premium_until"]:
        return False

    try:
        return datetime.strptime(
            user["premium_until"], "%Y-%m-%d %H:%M:%S"
        ) > datetime.now()
    except (ValueError, TypeError):
        return False


def sync_session():
    user = current_user()
    if not user:
        return None
    session["user_id"] = user["id"]
    session["name"] = user["name"]
    session["role"] = user["role"]
    session["premium"] = is_premium_user(user["id"])
    return user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not sync_session():
            session.clear()
            flash("Please log in to continue.")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = sync_session()
        if not user:
            session.clear()
            flash("Please log in to continue.")
            return redirect(url_for("login"))
        if user["role"] != "admin":
            flash("Admin access is restricted.")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


def group_belongs_to_user(group_id, user_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM groups WHERE id=? AND user_id=?",
            (group_id, user_id)
        ).fetchone()
    finally:
        conn.close()


def get_active_member_count(conn, group_id):
    return conn.execute(
        "SELECT COUNT(*) total FROM members WHERE group_id=? AND status='active'",
        (group_id,)
    ).fetchone()["total"]


def frequency_days(frequency):
    return {
        "weekly": 7,
        "biweekly": 14,
        "monthly": 30,
    }.get(frequency, 30)


def group_cycle_number(group):
    """Cycle 1 starts on the group's creation date."""
    try:
        created = datetime.strptime(group["created_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        created = datetime.now()

    elapsed = max(0, (datetime.now() - created).days)
    return max(1, (elapsed // frequency_days(group["frequency"])) + 1)


def cycle_start(group, cycle_number):
    try:
        created = datetime.strptime(group["created_at"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        created = datetime.now()
    return created + timedelta(days=frequency_days(group["frequency"]) * max(0, cycle_number - 1))


def cycle_due_date(group, cycle_number):
    return cycle_start(group, cycle_number).strftime("%Y-%m-%d")


def contribution_status_for_member(conn, group, member_id, cycle_number):
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) total, MAX(status) last_status
        FROM contributions
        WHERE group_id=? AND member_id=? AND cycle_number=?
        """,
        (group["id"], member_id, cycle_number)
    ).fetchone()

    total = float(row["total"] or 0)
    expected = float(group["contribution"] or 0)

    if total >= expected and expected > 0:
        return "paid"
    if datetime.strptime(today(), "%Y-%m-%d") > datetime.strptime(
        cycle_due_date(group, cycle_number), "%Y-%m-%d"
    ):
        return "late"
    if total > 0:
        return "partial"
    return "pending"


def current_beneficiary(conn, group):
    members = conn.execute(
        "SELECT * FROM members WHERE group_id=? AND status='active' ORDER BY position",
        (group["id"],)
    ).fetchall()

    if not members:
        return None

    cycle = group_cycle_number(group)

    for member in members:
        row = conn.execute(
            """
            SELECT id FROM payouts
            WHERE group_id=? AND member_id=? AND cycle_number=? AND status='paid'
            LIMIT 1
            """,
            (group["id"], member["id"], cycle)
        ).fetchone()
        if not row:
            return member

    # If the current cycle has been completed, use the next position.
    next_position = ((cycle - 1) % len(members)) + 1
    return next((m for m in members if m["position"] == next_position), members[0])


def trust_score(conn, group_id, member_id):
    rows = conn.execute(
        """
        SELECT amount,status,cycle_number FROM contributions
        WHERE group_id=? AND member_id=?
        """,
        (group_id, member_id)
    ).fetchall()

    if not rows:
        return 100

    expected = conn.execute(
        "SELECT contribution FROM groups WHERE id=?",
        (group_id,)
    ).fetchone()["contribution"]

    score = 100
    for row in rows:
        if row["status"] == "missed":
            score -= 15
        elif row["status"] == "late":
            score -= 7
        elif float(row["amount"] or 0) < float(expected or 0):
            score -= 3

    return max(0, min(100, score))


def whatsapp_url(phone, message):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if digits.startswith("0"):
        digits = "234" + digits[1:]
    return "https://wa.me/" + digits + "?text=" + urllib.parse.quote(message) if digits else "#"


# ============================================================
# PAYSTACK
# ============================================================

def paystack_request(path, method="GET", payload=None):
    secret = PAYSTACK_SECRET_KEY

    if not secret:
        return None, "PAYSTACK_SECRET_KEY is not configured in Render."

    if not secret.startswith(("sk_live_", "sk_test_")):
        return None, "PAYSTACK_SECRET_KEY is invalid."

    req_url = "https://api.paystack.co" + path
    headers = {
        "Authorization": "Bearer " + secret,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "AjoConnect/1.1",
    }

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        req_url, data=data, headers=headers, method=method.upper()
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
            if not result.get("status"):
                return result, result.get("message", "Paystack rejected the request.")
            return result, None
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode("utf-8"))
            detail = parsed.get("detail") or parsed.get("message") or str(e)
        except Exception:
            detail = str(e)
        return None, f"Paystack error ({e.code}): {detail}"
    except urllib.error.URLError as e:
        return None, f"Could not connect to Paystack: {e.reason}"
    except Exception as e:
        return None, f"Payment verification error: {e}"


def create_pending_subscription(user_id):
    reference = "AJOCONNECT-" + uuid.uuid4().hex.upper()
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO subscriptions
            (user_id,plan,amount,reference,status,created_at)
            VALUES (?,'premium',?,?,'pending',?)
            """,
            (user_id, PREMIUM_PRICE, reference, now())
        )
        conn.commit()
        return reference
    finally:
        conn.close()


def activate_premium(user_id, reference):
    conn = get_db()
    try:
        subscription = conn.execute(
            "SELECT * FROM subscriptions WHERE reference=? AND user_id=?",
            (reference, user_id)
        ).fetchone()

        if not subscription:
            return False
        if subscription["status"] == "paid":
            return True

        user = conn.execute(
            "SELECT premium_until FROM users WHERE id=?",
            (user_id,)
        ).fetchone()
        if not user:
            return False

        start = datetime.now()
        if user["premium_until"]:
            try:
                old_expiry = datetime.strptime(
                    user["premium_until"], "%Y-%m-%d %H:%M:%S"
                )
                if old_expiry > start:
                    start = old_expiry
            except Exception:
                pass

        expiry = start + timedelta(days=30)
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        expires = expiry.strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            UPDATE subscriptions
            SET status='paid',started_at=?,expires_at=?
            WHERE reference=? AND user_id=?
            """,
            (started, expires, reference, user_id)
        )
        conn.execute(
            "UPDATE users SET plan='premium',premium_until=? WHERE id=?",
            (expires, user_id)
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# DESIGN
# ============================================================

BASE_HTML = """
<!doctype html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title or "AjoConnect" }}</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:Arial,sans-serif;background:#f4f7f6;color:#1f2937}
nav{background:#075e54;color:#fff;padding:15px 20px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
nav a{color:#fff;text-decoration:none;margin-left:12px;font-weight:bold}
.brand{font-size:22px;font-weight:bold}
.container{max-width:1150px;margin:25px auto;padding:0 15px}
.hero{background:linear-gradient(135deg,#075e54,#128c7e);color:#fff;padding:30px;border-radius:16px;margin-bottom:25px}
.hero h1{margin-top:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:15px;margin-bottom:25px}
.card{background:#fff;padding:20px;border-radius:14px;box-shadow:0 2px 8px rgba(0,0,0,.08);margin-bottom:20px}
.stat{font-size:25px;font-weight:bold;color:#075e54;margin-top:8px}
.btn{display:inline-block;border:0;background:#075e54;color:#fff;padding:11px 16px;border-radius:8px;text-decoration:none;cursor:pointer;font-weight:bold}
.btn:hover{opacity:.9}
.btn-warning{background:#e09f00}.btn-secondary{background:#555}.btn-premium{background:#8a5a00}
.btn-danger{background:#b42318}
input,select,textarea{width:100%;padding:12px;margin-top:6px;margin-bottom:15px;border:1px solid #ddd;border-radius:8px;font-size:15px}
label{font-weight:bold}
table{width:100%;border-collapse:collapse}
th,td{padding:11px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
th{background:#f0f4f3}
.table-wrap{overflow-x:auto}
.badge{display:inline-block;padding:5px 9px;border-radius:20px;font-size:12px;font-weight:bold}
.paid,.success{background:#d7f5df;color:#176b35}
.pending{background:#fff0c2;color:#7a5700}
.late{background:#ffe0e0;color:#9b1c1c}
.partial{background:#e4edff;color:#174ea6}
.flash{padding:12px 15px;background:#fff3cd;border-radius:8px;margin-bottom:15px}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.premium-box{border:2px solid #d8a63a;background:#fffaf0}
.member-link{color:inherit;text-decoration:none;display:block}
.member-link strong{text-decoration:underline}
.member-link small{display:block;margin-top:3px;color:#777;font-size:11px}
footer{text-align:center;padding:30px;color:#777}
.progress{background:#e5e7eb;border-radius:20px;height:13px;overflow:hidden}
.progress div{background:#075e54;height:100%}
.kpi{font-size:14px;color:#667085}
.reminder{background:#f0fff8;border-left:5px solid #075e54;padding:15px;border-radius:8px}
img.receipt{max-width:180px;max-height:180px;border-radius:8px;border:1px solid #ddd}
@media(max-width:600px){
nav{display:block}nav a{display:inline-block;margin:8px 8px 0 0}
th,td{font-size:13px}.hero{padding:22px}
}
</style>
</head>
<body>
<nav>
<div class="brand">💰 AjoConnect</div>
<div>
{% if session.get("user_id") %}
<a href="{{ url_for('dashboard') }}">Dashboard</a>
<a href="{{ url_for('groups') }}">Groups</a>
<a href="{{ url_for('subscription') }}">Premium</a>
{% if session.get("role")=="admin" %}<a href="{{ url_for('admin_dashboard') }}">Admin</a>{% endif %}
<a href="{{ url_for('logout') }}">Logout</a>
{% else %}
<a href="{{ url_for('login') }}">Login</a>
<a href="{{ url_for('register') }}">Register</a>
{% endif %}
</div>
</nav>
<div class="container">
{% with messages=get_flashed_messages() %}
{% for message in messages %}<div class="flash">{{ message }}</div>{% endfor %}
{% endwith %}
{{ content|safe }}
</div>
<footer>AjoConnect © 2026 — Digital Ajo & Esusu Management</footer>
</body>
</html>
"""


def page(title, content):
    return render_template_string(BASE_HTML, title=title, content=content)


# ============================================================
# HOME / AUTH
# ============================================================

@app.route("/")
def home():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    return page("Home", """
    <div class="hero">
      <h1>💰 Welcome to AjoConnect</h1>
      <p>Manage Ajo / Esusu groups, members, contributions, receipts, reminders and payouts in one place.</p>
      <a class="btn" href="/register">Create Free Account</a>
      <a class="btn btn-secondary" href="/login">Login</a>
    </div>
    <div class="grid">
      <div class="card"><h3>👥 Manage Members</h3><p>Organize members and rotation positions.</p></div>
      <div class="card"><h3>💵 Track Contributions</h3><p>Record payments, receipts and payment status.</p></div>
      <div class="card"><h3>🔔 Reminders</h3><p>Send WhatsApp contribution reminders in one tap.</p></div>
      <div class="card"><h3>🔄 Rotation</h3><p>See who receives the Ajo payout for each cycle.</p></div>
      <div class="card"><h3>🧾 Reports</h3><p>Download your group's contribution and payout records.</p></div>
      <div class="card premium-box"><h3>⭐ Premium</h3><p>Unlimited groups and members for ₦2,000/month.</p></div>
    </div>
    """)


@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip().lower()
        password = request.form.get("password","")

        if not name or not email or not password:
            flash("Please complete all fields.")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("Password must be at least 6 characters.")
            return redirect(url_for("register"))

        conn = get_db()
        try:
            existing = conn.execute(
                "SELECT id FROM users WHERE lower(email)=?",
                (email,)
            ).fetchone()
            if existing:
                flash("An account with this email already exists.")
                return redirect(url_for("login"))

            role = "admin" if (
                os.environ.get("ADMIN_EMAIL","").strip().lower() == email
            ) else "user"

            conn.execute(
                """
                INSERT INTO users
                (name,email,password,role,plan,premium_until,created_at)
                VALUES (?,?,?,?,'free',NULL,?)
                """,
                (name,email,generate_password_hash(password),role,now())
            )
            conn.commit()
        finally:
            conn.close()

        flash("Account created successfully. Please log in.")
        return redirect(url_for("login"))

    return page("Register", """
    <div class="card">
      <h2>📝 Create AjoConnect Account</h2>
      <p>Start free and upgrade whenever you need more space.</p>
      <form method="POST">
        <label>Full Name</label>
        <input name="name" required>
        <label>Email</label>
        <input type="email" name="email" required>
        <label>Password</label>
        <input type="password" name="password" minlength="6" required>
        <button class="btn" type="submit">Create Free Account</button>
      </form>
    </div>
    """)


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        password = request.form.get("password","")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE lower(email)=?",
            (email,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            session["premium"] = is_premium_user(user["id"])
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.")

    return page("Login", """
    <div class="card">
      <h2>🔐 Login</h2>
      <form method="POST">
        <label>Email</label>
        <input type="email" name="email" required>
        <label>Password</label>
        <input type="password" name="password" required>
        <button class="btn" type="submit">Login</button>
      </form>
    </div>
    """)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


# ============================================================
# PREMIUM / PAYSTACK
# ============================================================

@app.route("/subscription")
@login_required
def subscription():
    user = current_user()
    if not user:
        session.clear()
        return redirect(url_for("login"))

    if is_premium_user(user["id"]):
        return page("Premium", f"""
        <div class="hero"><h1>⭐ AjoConnect Premium</h1>
        <p>Your Premium membership is active.</p></div>
        <div class="card premium-box">
          <h2>Premium Active ✅</h2>
          <p>Premium access until:</p>
          <div class="stat">{safe(user["premium_until"])}</div>
          <p>Thank you for supporting AjoConnect.</p>
        </div>
        """)

    if not PAYSTACK_PUBLIC_KEY:
        pay_button = """<div class="flash">Premium payments are temporarily unavailable because PAYSTACK_PUBLIC_KEY has not been added to Render.</div>"""
    else:
        pay_button = f"""<a class="btn btn-premium" href="/pay/premium">⭐ Upgrade for ₦{PREMIUM_PRICE:,.0f}</a>"""

    return page("Premium", f"""
    <div class="hero"><h1>⭐ Upgrade AjoConnect</h1><p>Get more space and tools for your Ajo business.</p></div>
    <div class="card premium-box">
      <h2>Premium — ₦{PREMIUM_PRICE:,.0f}/month</h2>
      <ul>
        <li>Unlimited Ajo groups</li>
        <li>Unlimited members</li>
        <li>Receipt and payment records</li>
        <li>WhatsApp reminders</li>
        <li>Advanced Ajo management</li>
        <li>Reports and group statistics</li>
        <li>30 days of Premium access</li>
      </ul>
      <br>{pay_button}
    </div>
    <div class="card"><h3>Free Plan</h3><p>1 Ajo group and up to 10 members.</p></div>
    """)


@app.route("/pay/premium")
@login_required
def pay_premium():
    user = current_user()
    if not user:
        session.clear()
        return redirect(url_for("login"))

    if is_premium_user(user["id"]):
        flash("Your Premium membership is already active.")
        return redirect(url_for("subscription"))

    if not PAYSTACK_PUBLIC_KEY:
        flash("Payment is not configured yet. Add PAYSTACK_PUBLIC_KEY to Render.")
        return redirect(url_for("subscription"))

    if not PAYSTACK_SECRET_KEY:
        flash("Payment verification is not configured. Add PAYSTACK_SECRET_KEY to Render.")
        return redirect(url_for("subscription"))

    email = (user["email"] or "").strip().lower()
    if "@" not in email or "." not in email.rsplit("@",1)[-1]:
        flash("Please use a valid email address before paying.")
        return redirect(url_for("subscription"))

    reference = create_pending_subscription(user["id"])

    return page("Pay Premium", f"""
    <div class="card" style="text-align:center;padding:25px">
      <h2>⭐ AjoConnect Premium</h2>
      <p>Amount: <strong>₦{PREMIUM_PRICE:,.0f}</strong></p>
      <p>Email: <strong>{safe(email)}</strong></p>
      <button id="payButton" class="btn btn-premium">Pay ₦{PREMIUM_PRICE:,.0f}</button>
      <p id="paymentStatus"></p>
    </div>
    <script src="https://js.paystack.co/v2/inline.js"></script>
    <script>
    const payButton = document.getElementById("payButton");
    const statusBox = document.getElementById("paymentStatus");
    payButton.addEventListener("click", function() {{
      payButton.disabled = true;
      payButton.innerText = "Opening Paystack...";
      try {{
        const popup = new PaystackPop();
        popup.newTransaction({{
          key: {json.dumps(PAYSTACK_PUBLIC_KEY)},
          email: {json.dumps(email)},
          amount: {int(PREMIUM_PRICE * 100)},
          currency: "NGN",
          reference: {json.dumps(reference)},
          onSuccess: function(transaction) {{
            statusBox.innerText = "Payment received. Verifying...";
            window.location.href = "/paystack/complete?reference=" +
              encodeURIComponent(transaction.reference || {json.dumps(reference)});
          }},
          onCancel: function() {{
            payButton.disabled = false;
            payButton.innerText = "Pay ₦{PREMIUM_PRICE:,.0f}";
            statusBox.innerText = "Payment cancelled.";
          }},
          onError: function(error) {{
            payButton.disabled = false;
            payButton.innerText = "Pay ₦{PREMIUM_PRICE:,.0f}";
            statusBox.innerText = "Paystack could not open the payment window.";
            console.error(error);
          }}
        }});
      }} catch(error) {{
        payButton.disabled = false;
        payButton.innerText = "Pay ₦{PREMIUM_PRICE:,.0f}";
        statusBox.innerText = "Could not open Paystack. Please try again.";
        console.error(error);
      }}
    }});
    </script>
    """)


@app.route("/paystack/complete")
@login_required
def paystack_complete():
    user = current_user()
    reference = request.args.get("reference","").strip()

    if not user:
        session.clear()
        return redirect(url_for("login"))
    if not reference:
        flash("No payment reference was received.")
        return redirect(url_for("subscription"))

    conn = get_db()
    subscription = conn.execute(
        "SELECT * FROM subscriptions WHERE reference=? AND user_id=?",
        (reference, user["id"])
    ).fetchone()
    conn.close()

    if not subscription:
        flash("Payment record was not found.")
        return redirect(url_for("subscription"))

    result, error = paystack_request("/transaction/verify/" + reference, "GET")

    if error or not result or not result.get("status"):
        flash("Payment was received by Paystack, but AjoConnect could not complete verification automatically. Please try again.")
        return redirect(url_for("subscription"))

    transaction = result.get("data") or {}
    try:
        amount = int(transaction.get("amount") or 0)
    except (ValueError, TypeError):
        amount = 0

    if (
        transaction.get("status") == "success"
        and transaction.get("currency") == "NGN"
        and amount == int(PREMIUM_PRICE * 100)
        and transaction.get("reference") == reference
    ):
        if activate_premium(user["id"], reference):
            sync_session()
            flash("Payment successful! Premium is active for 30 days.")
            return redirect(url_for("subscription"))

    flash("The payment could not be verified as a valid ₦2,000 Premium payment.")
    return redirect(url_for("subscription"))


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()

    groups = conn.execute(
        "SELECT * FROM groups WHERE user_id=? ORDER BY id DESC",
        (session["user_id"],)
    ).fetchall()

    total_members = conn.execute(
        """
        SELECT COUNT(*) total FROM members JOIN groups ON groups.id=members.group_id
        WHERE groups.user_id=?
        """,
        (session["user_id"],)
    ).fetchone()["total"]

    total_contributions = conn.execute(
        """
        SELECT COALESCE(SUM(contributions.amount),0) total
        FROM contributions JOIN groups ON groups.id=contributions.group_id
        WHERE groups.user_id=? AND contributions.status='paid'
        """,
        (session["user_id"],)
    ).fetchone()["total"]

    total_payouts = conn.execute(
        """
        SELECT COALESCE(SUM(payouts.amount),0) total
        FROM payouts JOIN groups ON groups.id=payouts.group_id
        WHERE groups.user_id=? AND payouts.status='paid'
        """,
        (session["user_id"],)
    ).fetchone()["total"]

    pending_count = conn.execute(
        """
        SELECT COUNT(*) total FROM contributions c
        JOIN groups g ON g.id=c.group_id
        WHERE g.user_id=? AND c.status='pending'
        """,
        (session["user_id"],)
    ).fetchone()["total"]

    conn.close()

    content = f"""
    <div class="hero">
      <h1>👋 Welcome, {safe(session.get("name"))}</h1>
      <p>Plan: <strong>{"⭐ PREMIUM" if session.get("premium") else "FREE"}</strong></p>
    </div>

    <div class="grid">
      <div class="card"><h3>👥 Members</h3><div class="stat">{total_members}</div></div>
      <div class="card"><h3>💰 Contributions</h3><div class="stat">{money(total_contributions)}</div></div>
      <div class="card"><h3>💸 Payouts</h3><div class="stat">{money(total_payouts)}</div></div>
      <div class="card"><h3>⏳ Pending</h3><div class="stat">{pending_count}</div></div>
    </div>

    <div class="card"><div class="actions">
      <a class="btn" href="/groups/new">➕ Create Ajo Group</a>
      <a class="btn btn-secondary" href="/groups">📋 View Groups</a>
      {"<a class='btn btn-premium' href='/subscription'>⭐ Premium</a>" if not session.get("premium") else ""}
    </div></div>

    <div class="card">
      <h2>Your Ajo Groups</h2>
      <div class="table-wrap"><table>
      <tr><th>Group</th><th>Contribution</th><th>Frequency</th><th>Cycle</th><th>Action</th></tr>
    """

    for group in groups:
        content += f"""
        <tr>
          <td>{safe(group["name"])}</td>
          <td>{money(group["contribution"])}</td>
          <td>{safe(group["frequency"]).title()}</td>
          <td>{group_cycle_number(group)}</td>
          <td><a class="btn" href="/group/{group["id"]}">Open</a></td>
        </tr>
        """

    content += "</table></div></div>"
    return page("Dashboard", content)


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()

    users = conn.execute("SELECT COUNT(*) total FROM users").fetchone()["total"]
    groups_count = conn.execute("SELECT COUNT(*) total FROM groups").fetchone()["total"]
    members = conn.execute("SELECT COUNT(*) total FROM members").fetchone()["total"]
    contributions = conn.execute(
        "SELECT COALESCE(SUM(amount),0) total FROM contributions WHERE status='paid'"
    ).fetchone()["total"]
    payouts = conn.execute(
        "SELECT COALESCE(SUM(amount),0) total FROM payouts WHERE status='paid'"
    ).fetchone()["total"]
    premium_users = conn.execute(
        "SELECT COUNT(*) total FROM users WHERE plan='premium' AND premium_until>?",
        (now(),)
    ).fetchone()["total"]
    premium_revenue = conn.execute(
        "SELECT COALESCE(SUM(amount),0) total FROM subscriptions WHERE status='paid'"
    ).fetchone()["total"]

    recent_users = conn.execute(
        "SELECT name,email,plan,created_at FROM users ORDER BY id DESC LIMIT 15"
    ).fetchall()

    conn.close()

    content = f"""
    <div class="hero"><h1>⚙️ AjoConnect Admin</h1><p>Platform overview and Premium revenue.</p></div>
    <div class="grid">
      <div class="card"><h3>Users</h3><div class="stat">{users}</div></div>
      <div class="card"><h3>Ajo Groups</h3><div class="stat">{groups_count}</div></div>
      <div class="card"><h3>Members</h3><div class="stat">{members}</div></div>
      <div class="card"><h3>Premium Users</h3><div class="stat">{premium_users}</div></div>
      <div class="card premium-box"><h3>Premium Revenue</h3><div class="stat">{money(premium_revenue)}</div></div>
      <div class="card"><h3>Ajo Contributions</h3><div class="stat">{money(contributions)}</div></div>
      <div class="card"><h3>Ajo Payouts</h3><div class="stat">{money(payouts)}</div></div>
    </div>
    <div class="card"><h2>👥 Recent Users</h2><div class="table-wrap"><table>
      <tr><th>Name</th><th>Email</th><th>Plan</th><th>Joined</th></tr>
    """
    for u in recent_users:
        content += f"<tr><td>{safe(u['name'])}</td><td>{safe(u['email'])}</td><td>{safe(u['plan']).title()}</td><td>{safe(u['created_at'])}</td></tr>"
    content += """</table></div></div>
    <div class="card"><h2>💡 AjoConnect model</h2>
    <p>AjoConnect does not hold or control members' Ajo funds. It provides management, records, reminders, reports and transparency tools.</p></div>
    """
    return page("Admin Dashboard", content)


# ============================================================
# GROUPS
# ============================================================

@app.route("/groups")
@login_required
def groups():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM groups WHERE user_id=? ORDER BY id DESC",
        (session["user_id"],)
    ).fetchall()
    conn.close()

    content = """
    <div class="card"><div class="actions">
      <h2 style="margin-right:auto">📋 Ajo Groups</h2>
      <a class="btn" href="/groups/new">➕ New Group</a>
    </div></div><div class="grid">
    """

    for group in rows:
        content += f"""
        <div class="card">
          <h2>{safe(group["name"])}</h2>
          <p>Contribution: <strong>{money(group["contribution"])}</strong></p>
          <p>Frequency: <strong>{safe(group["frequency"]).title()}</strong></p>
          <p>Current cycle: <strong>{group_cycle_number(group)}</strong></p>
          <a class="btn" href="/group/{group["id"]}">Open Group</a>
        </div>
        """

    return page("Groups", content + "</div>")


@app.route("/groups/new", methods=["GET","POST"])
@login_required
def create_group():
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) total FROM groups WHERE user_id=?",
        (session["user_id"],)
    ).fetchone()["total"]
    conn.close()

    if not is_premium_user(session["user_id"]) and count >= FREE_GROUP_LIMIT:
        flash("The Free plan allows 1 Ajo group. Upgrade to Premium for unlimited groups.")
        return redirect(url_for("subscription"))

    if request.method == "POST":
        name = request.form.get("name","").strip()
        frequency = request.form.get("frequency","monthly")
        try:
            contribution = float(request.form.get("contribution","0"))
        except ValueError:
            contribution = 0

        if not name:
            flash("Group name is required.")
            return redirect(url_for("create_group"))
        if contribution <= 0:
            flash("Contribution must be greater than zero.")
            return redirect(url_for("create_group"))
        if frequency not in {"weekly","biweekly","monthly"}:
            frequency = "monthly"

        invite_token = uuid.uuid4().hex

        conn = get_db()
        cur = conn.execute(
            """
            INSERT INTO groups(name,contribution,frequency,user_id,created_at,invite_token)
            VALUES(?,?,?,?,?,?)
            """,
            (name,contribution,frequency,session["user_id"],now(),invite_token)
        )
        conn.commit()
        group_id = cur.lastrowid
        conn.close()

        flash("Ajo group created successfully.")
        return redirect(url_for("group_detail",group_id=group_id))

    return page("Create Group", """
    <div class="card">
      <h2>➕ Create Ajo Group</h2>
      <form method="POST">
        <label>Group Name</label>
        <input name="name" placeholder="e.g. Mama Raji" required>
        <label>Contribution Amount</label>
        <input type="number" name="contribution" min="1" step="0.01" placeholder="10000" required>
        <label>Contribution Frequency</label>
        <select name="frequency">
          <option value="weekly">Weekly</option>
          <option value="biweekly">Bi-weekly</option>
          <option value="monthly" selected>Monthly</option>
        </select>
        <button class="btn" type="submit">Create Group</button>
      </form>
    </div>
    """)


# ============================================================
# GROUP DETAIL
# ============================================================

@app.route("/group/<int:group_id>")
@login_required
def group_detail(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    members = conn.execute(
        "SELECT * FROM members WHERE group_id=? ORDER BY position",
        (group_id,)
    ).fetchall()

    paid_total = conn.execute(
        "SELECT COALESCE(SUM(amount),0) total FROM contributions WHERE group_id=? AND status='paid'",
        (group_id,)
    ).fetchone()["total"]

    payout_total = conn.execute(
        "SELECT COALESCE(SUM(amount),0) total FROM payouts WHERE group_id=? AND status='paid'",
        (group_id,)
    ).fetchone()["total"]

    cycle = group_cycle_number(group)
    current = current_beneficiary(conn, group)
    active_count = get_active_member_count(conn, group_id)
    expected = group["contribution"] * active_count

    paid_this_cycle = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) total FROM contributions
        WHERE group_id=? AND cycle_number=? AND status='paid'
        """,
        (group_id, cycle)
    ).fetchone()["total"]

    conn.close()

    due = cycle_due_date(group, cycle)
    try:
        days_left = (datetime.strptime(due, "%Y-%m-%d") - datetime.strptime(today(), "%Y-%m-%d")).days
    except Exception:
        days_left = 0

    share_url = request.url_root.rstrip("/") + url_for("join_group", token=group["invite_token"])

    content = f"""
    <div class="hero"><h1>🔄 {safe(group["name"])}</h1>
      <p>Contribution: <strong>{money(group["contribution"])}</strong></p>
      <p>Members: <strong>{active_count}</strong></p>
      <p>Current cycle: <strong>{cycle}</strong> · Due: <strong>{due}</strong></p>
    </div>

    <div class="grid">
      <div class="card"><h3>Cycle Contributions</h3><div class="stat">{money(paid_this_cycle)}</div></div>
      <div class="card"><h3>Expected This Cycle</h3><div class="stat">{money(expected)}</div></div>
      <div class="card"><h3>Current Beneficiary</h3><div class="stat">{safe(current["name"]) if current else "No members"}</div></div>
      <div class="card"><h3>Days to Due Date</h3><div class="stat">{max(0, days_left)}</div></div>
    </div>

    <div class="card"><div class="actions">
      <a class="btn" href="/group/{group_id}/member/add">➕ Add Member</a>
      <a class="btn btn-secondary" href="/group/{group_id}/contributions">💰 Contributions</a>
      <a class="btn btn-secondary" href="/group/{group_id}/payouts">💸 Payouts</a>
      <a class="btn btn-secondary" href="/group/{group_id}/reports">📊 Reports</a>
      <a class="btn" href="/group/{group_id}/reminders">🔔 Reminders</a>
    </div></div>

    <div class="card reminder">
      <h3>🔗 Invite Members</h3>
      <p>Share this link with members so they can see the group invitation page:</p>
      <input readonly value="{safe(share_url)}" onclick="this.select()">
      <a class="btn" href="https://wa.me/?text={urllib.parse.quote('Join my AjoConnect group: ' + share_url)}" target="_blank">Share on WhatsApp</a>
    </div>

    <div class="card">
      <h2>📅 Rotation & Payment Status — Cycle {cycle}</h2>
      <div class="table-wrap"><table>
      <tr><th>Position</th><th>Member</th><th>Phone</th><th>Payment</th><th>Trust</th><th>Payout</th><th>Action</th></tr>
    """

    conn = get_db()
    members = conn.execute(
        "SELECT * FROM members WHERE group_id=? ORDER BY position",
        (group_id,)
    ).fetchall()

    for member in members:
        status = contribution_status_for_member(conn, group, member["id"], cycle)
        score = trust_score(conn, group_id, member["id"])

        payout = conn.execute(
            "SELECT id,status,amount FROM payouts WHERE group_id=? AND member_id=? AND cycle_number=? ORDER BY id DESC LIMIT 1",
            (group_id, member["id"], cycle)
        ).fetchone()

        payout_text = "Not paid"
        if payout and payout["status"] == "paid":
            payout_text = "✅ " + money(payout["amount"])

        content += f"""
        <tr>
          <td>{member["position"]}</td>
          <td><a class="member-link" href="/group/{group_id}/member/{member["id"]}">
            <strong>{safe(member["name"])}</strong><small>Tap for details</small></a></td>
          <td>{safe(member["phone"]) or "-"}</td>
          <td><span class="badge {status}">{status.title()}</span></td>
          <td><strong>{score}/100</strong></td>
          <td>{payout_text}</td>
          <td><div class="actions">
            <a class="btn" href="/group/{group_id}/member/{member["id"]}">Details</a>
            <a class="btn" href="/group/{group_id}/member/{member["id"]}/contribute">Payment</a>
            <a class="btn btn-warning" href="/group/{group_id}/member/{member["id"]}/payout">Payout</a>
          </div></td>
        </tr>
        """

    conn.close()

    content += "</table></div></div>"
    return page(group["name"], content)


# ============================================================
# MEMBER DETAILS
# ============================================================

@app.route("/group/<int:group_id>/member/<int:member_id>")
@login_required
def member_detail(group_id, member_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    member = conn.execute(
        "SELECT * FROM members WHERE id=? AND group_id=?",
        (member_id, group_id)
    ).fetchone()

    if not member:
        conn.close()
        return "Member not found", 404

    contributions = conn.execute(
        """
        SELECT id,amount,payment_date,status,note,cycle_number,receipt_filename,receipt_mime
        FROM contributions
        WHERE group_id=? AND member_id=?
        ORDER BY payment_date DESC,id DESC
        """,
        (group_id, member_id)
    ).fetchall()

    payouts = conn.execute(
        """
        SELECT amount,payout_date,status,note,cycle_number
        FROM payouts WHERE group_id=? AND member_id=?
        ORDER BY payout_date DESC,id DESC
        """,
        (group_id, member_id)
    ).fetchall()

    active_count = get_active_member_count(conn, group_id)
    score = trust_score(conn, group_id, member_id)
    conn.close()

    contribution_total = sum(float(x["amount"] or 0) for x in contributions)
    payout_total = sum(float(x["amount"] or 0) for x in payouts if x["status"] == "paid")
    expected = group["contribution"] * active_count

    content = f"""
    <div class="hero"><h1>👤 {safe(member["name"])}</h1>
      <p>Member details for <strong>{safe(group["name"])}</strong></p>
    </div>

    <div class="grid">
      <div class="card"><h3>Position</h3><div class="stat">#{member["position"]}</div></div>
      <div class="card"><h3>Trust Score</h3><div class="stat">{score}/100</div></div>
      <div class="card"><h3>Total Contributions</h3><div class="stat">{money(contribution_total)}</div></div>
      <div class="card"><h3>Total Payout Received</h3><div class="stat">{money(payout_total)}</div></div>
      <div class="card"><h3>Expected Payout</h3><div class="stat">{money(expected)}</div></div>
    </div>

    <div class="card"><h2>📋 Member Information</h2>
      <p><strong>Name:</strong> {safe(member["name"])}</p>
      <p><strong>Phone:</strong> {safe(member["phone"]) or "Not provided"}</p>
      <p><strong>Email:</strong> {safe(member["email"]) or "Not provided"}</p>
      <p><strong>Rotation Position:</strong> {member["position"]}</p>
      <p><strong>Status:</strong> {safe(member["status"]).title()}</p>
      <div class="actions">
        <a class="btn" href="/group/{group_id}/member/{member_id}/contribute">💰 Record Contribution</a>
        <a class="btn btn-warning" href="/group/{group_id}/member/{member_id}/payout">💸 Record Payout</a>
        <a class="btn btn-secondary" href="/group/{group_id}">← Back</a>
      </div>
    </div>

    <div class="card"><h2>💰 Contribution History</h2><div class="table-wrap"><table>
      <tr><th>Cycle</th><th>Amount</th><th>Date</th><th>Status</th><th>Receipt</th><th>Note</th></tr>
    """

    if contributions:
        for row in contributions:
            receipt = ""
            if row["receipt_filename"]:
                receipt = f'<a href="/receipt/{row["id"]}" target="_blank">📎 View</a>'
            content += f"""
            <tr><td>{row["cycle_number"]}</td>
            <td><strong>{money(row["amount"])}</strong></td>
            <td>{safe(row["payment_date"])}</td>
            <td><span class="badge {safe(row["status"])}">{safe(row["status"]).title()}</span></td>
            <td>{receipt or "-"}</td>
            <td>{safe(row["note"]) or "-"}</td></tr>
            """
    else:
        content += '<tr><td colspan="6">No contribution recorded yet.</td></tr>'

    content += "</table></div></div><div class='card'><h2>💸 Payout History</h2><div class='table-wrap'><table>"
    content += "<tr><th>Cycle</th><th>Amount</th><th>Date</th><th>Status</th><th>Note</th></tr>"

    if payouts:
        for row in payouts:
            content += f"""
            <tr><td>{row["cycle_number"]}</td><td><strong>{money(row["amount"])}</strong></td>
            <td>{safe(row["payout_date"]) or "-"}</td><td>{safe(row["status"]).title()}</td><td>{safe(row["note"]) or "-"}</td></tr>
            """
    else:
        content += '<tr><td colspan="5">No payout recorded yet.</td></tr>'

    content += "</table></div></div>"
    return page(safe(member["name"]), content)


# ============================================================
# ADD MEMBER / INVITE
# ============================================================

@app.route("/group/<int:group_id>/member/add", methods=["GET","POST"])
@login_required
def add_member(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) total FROM members WHERE group_id=?",
        (group_id,)
    ).fetchone()["total"]
    conn.close()

    if not is_premium_user(session["user_id"]) and count >= FREE_MEMBER_LIMIT:
        flash("The Free plan allows 10 members per group. Upgrade to Premium for unlimited members.")
        return redirect(url_for("subscription"))

    if request.method == "POST":
        name = request.form.get("name","").strip()
        phone = request.form.get("phone","").strip()
        email = request.form.get("email","").strip().lower()

        if not name:
            flash("Member name is required.")
            return redirect(url_for("add_member", group_id=group_id))

        conn = get_db()
        position = conn.execute(
            "SELECT COALESCE(MAX(position),0)+1 next_position FROM members WHERE group_id=?",
            (group_id,)
        ).fetchone()["next_position"]

        conn.execute(
            """
            INSERT INTO members(group_id,name,phone,email,position,status,created_at)
            VALUES(?,?,?,?,?,'active',?)
            """,
            (group_id,name,phone,email,position,now())
        )
        conn.commit()
        conn.close()

        flash(f"{name} has been added to the Ajo group.")
        return redirect(url_for("group_detail", group_id=group_id))

    return page("Add Member", f"""
    <div class="card"><h2>➕ Add Member</h2><p>Group: <strong>{safe(group["name"])}</strong></p>
    <form method="POST">
      <label>Member Name</label><input name="name" required>
      <label>Phone Number</label><input name="phone" placeholder="080...">
      <label>Email</label><input type="email" name="email" placeholder="member@email.com">
      <button class="btn" type="submit">Add Member</button>
    </form></div>
    """)


@app.route("/join/<token>")
def join_group(token):
    conn = get_db()
    group = conn.execute(
        "SELECT * FROM groups WHERE invite_token=?",
        (token,)
    ).fetchone()
    conn.close()

    if not group:
        return page("Invalid Invite", """
        <div class="card"><h2>❌ Invalid invitation</h2><p>This Ajo group invitation is no longer valid.</p></div>
        """), 404

    return page("Join Ajo", f"""
    <div class="hero">
      <h1>🤝 Join AjoConnect</h1>
      <p>You have been invited to <strong>{safe(group["name"])}</strong>.</p>
    </div>
    <div class="card">
      <h2>Group Details</h2>
      <p>Contribution: <strong>{money(group["contribution"])}</strong></p>
      <p>Frequency: <strong>{safe(group["frequency"]).title()}</strong></p>
      <p>To join, contact the group administrator and ask them to add your name and phone number to the group.</p>
      <a class="btn" href="/register">Create AjoConnect Account</a>
      <a class="btn btn-secondary" href="/login">Login</a>
    </div>
    """)


# ============================================================
# CONTRIBUTIONS
# ============================================================

@app.route("/group/<int:group_id>/member/<int:member_id>/contribute", methods=["GET","POST"])
@login_required
def record_contribution(group_id, member_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    member = conn.execute(
        "SELECT * FROM members WHERE id=? AND group_id=?",
        (member_id, group_id)
    ).fetchone()
    conn.close()

    if not member:
        return "Member not found", 404

    cycle = group_cycle_number(group)

    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", group["contribution"]))
        except ValueError:
            amount = 0

        if amount <= 0:
            flash("Contribution amount must be greater than zero.")
            return redirect(url_for("record_contribution", group_id=group_id, member_id=member_id))

        payment_date = request.form.get("payment_date") or today()
        note = request.form.get("note","").strip()

        receipt = request.files.get("receipt")
        receipt_data = None
        receipt_filename = None
        receipt_mime = None

        if receipt and receipt.filename:
            receipt.seek(0, os.SEEK_END)
            size = receipt.tell()
            receipt.seek(0)

            if size > MAX_RECEIPT_SIZE:
                flash("Receipt is too large. Maximum size is 2 MB.")
                return redirect(url_for("record_contribution", group_id=group_id, member_id=member_id))

            allowed = {
                "image/jpeg", "image/png", "image/webp",
                "application/pdf"
            }
            if receipt.mimetype not in allowed:
                flash("Receipt must be JPG, PNG, WEBP or PDF.")
                return redirect(url_for("record_contribution", group_id=group_id, member_id=member_id))

            receipt_data = receipt.read()
            receipt_filename = secure_filename(receipt.filename)[:150]
            receipt_mime = receipt.mimetype

        # Admin-recorded payments are immediately approved.
        conn = get_db()
        conn.execute(
            """
            INSERT INTO contributions
            (group_id,member_id,amount,payment_date,status,note,cycle_number,
             receipt_data,receipt_filename,receipt_mime,approved_at,approved_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                group_id, member_id, amount, payment_date, "paid", note, cycle,
                receipt_data, receipt_filename, receipt_mime, now(), session["user_id"]
            )
        )
        conn.commit()
        conn.close()

        flash(f"Payment of {money(amount)} recorded for {member['name']}. Cycle {cycle}.")
        return redirect(url_for("group_detail", group_id=group_id))

    return page("Record Contribution", f"""
    <div class="card"><h2>💰 Record Contribution</h2>
      <p>Member: <strong>{safe(member["name"])}</strong></p>
      <p>Current Ajo cycle: <strong>{cycle}</strong></p>
      <p>Expected contribution: <strong>{money(group["contribution"])}</strong></p>
      <form method="POST" enctype="multipart/form-data">
        <label>Amount Paid</label>
        <input type="number" name="amount" value="{group["contribution"]}" min="0.01" step="0.01" required>
        <label>Payment Date</label>
        <input type="date" name="payment_date" value="{today()}" required>
        <label>Payment Receipt (optional, max 2 MB)</label>
        <input type="file" name="receipt" accept=".jpg,.jpeg,.png,.webp,.pdf">
        <label>Note</label><textarea name="note" placeholder="Transfer reference or note"></textarea>
        <button class="btn" type="submit">✅ Record Payment</button>
      </form>
    </div>
    """)


@app.route("/receipt/<int:contribution_id>")
@login_required
def receipt(contribution_id):
    conn = get_db()
    row = conn.execute(
        """
        SELECT c.receipt_data,c.receipt_mime,c.receipt_filename,g.user_id
        FROM contributions c JOIN groups g ON g.id=c.group_id
        WHERE c.id=?
        """,
        (contribution_id,)
    ).fetchone()
    conn.close()

    if not row or row["user_id"] != session["user_id"] or not row["receipt_data"]:
        return "Receipt not found", 404

    return send_file(
        io.BytesIO(row["receipt_data"]),
        mimetype=row["receipt_mime"] or "application/octet-stream",
        download_name=row["receipt_filename"] or "payment-receipt",
        as_attachment=False
    )


@app.route("/group/<int:group_id>/contributions")
@login_required
def contributions(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    cycle = request.args.get("cycle", type=int) or group_cycle_number(group)
    rows = conn.execute(
        """
        SELECT contributions.*,members.name member_name
        FROM contributions JOIN members ON members.id=contributions.member_id
        WHERE contributions.group_id=? AND contributions.cycle_number=?
        ORDER BY contributions.payment_date DESC,contributions.id DESC
        """,
        (group_id, cycle)
    ).fetchall()

    totals = conn.execute(
        """
        SELECT
        COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END),0) paid,
        COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END),0) pending
        FROM contributions
        WHERE group_id=? AND cycle_number=?
        """,
        (group_id, cycle)
    ).fetchone()
    conn.close()

    content = f"""
    <div class="card"><h2>💰 Contribution Ledger</h2>
      <p>Group: <strong>{safe(group["name"])}</strong> · Cycle <strong>{cycle}</strong></p>
      <div class="grid">
        <div><span class="kpi">Paid</span><div class="stat">{money(totals["paid"])}</div></div>
        <div><span class="kpi">Pending</span><div class="stat">{money(totals["pending"])}</div></div>
      </div>
      <div class="actions">
        <a class="btn" href="/group/{group_id}/contributions?cycle={max(1,cycle-1)}">← Previous Cycle</a>
        <a class="btn" href="/group/{group_id}/contributions?cycle={cycle+1}">Next Cycle →</a>
      </div>
      <br>
      <div class="table-wrap"><table>
      <tr><th>Member</th><th>Amount</th><th>Date</th><th>Status</th><th>Receipt</th><th>Note</th></tr>
    """

    for row in rows:
        receipt_link = f'<a href="/receipt/{row["id"]}" target="_blank">📎 View</a>' if row["receipt_filename"] else "-"
        content += f"""
        <tr><td>{safe(row["member_name"])}</td><td><strong>{money(row["amount"])}</strong></td>
        <td>{safe(row["payment_date"])}</td>
        <td><span class="badge {safe(row["status"])}">{safe(row["status"]).title()}</span></td>
        <td>{receipt_link}</td><td>{safe(row["note"]) or "-"}</td></tr>
        """

    content += "</table></div></div>"
    return page("Contributions", content)


# ============================================================
# PAYOUTS
# ============================================================

@app.route("/group/<int:group_id>/member/<int:member_id>/payout", methods=["GET","POST"])
@login_required
def record_payout(group_id, member_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    member = conn.execute(
        "SELECT * FROM members WHERE id=? AND group_id=?",
        (member_id, group_id)
    ).fetchone()

    if not member:
        conn.close()
        return "Member not found", 404

    count = get_active_member_count(conn, group_id)
    expected = group["contribution"] * count
    cycle = group_cycle_number(group)

    already = conn.execute(
        """
        SELECT id FROM payouts
        WHERE group_id=? AND member_id=? AND cycle_number=? AND status='paid'
        LIMIT 1
        """,
        (group_id, member_id, cycle)
    ).fetchone()
    conn.close()

    if already:
        flash("This member has already received a payout for the current Ajo cycle.")
        return redirect(url_for("member_detail", group_id=group_id, member_id=member_id))

    if request.method == "POST":
        try:
            amount = float(request.form.get("amount", expected))
        except ValueError:
            amount = 0

        if amount <= 0:
            flash("Payout amount must be greater than zero.")
            return redirect(url_for("record_payout", group_id=group_id, member_id=member_id))

        payout_date = request.form.get("payout_date") or today()
        note = request.form.get("note","").strip()

        conn = get_db()
        conn.execute(
            """
            INSERT INTO payouts(group_id,member_id,amount,payout_date,status,note,cycle_number)
            VALUES(?,?,?,?, 'paid', ?, ?)
            """,
            (group_id, member_id, amount, payout_date, note, cycle)
        )
        conn.commit()
        conn.close()

        flash(f"Payout of {money(amount)} recorded for {member['name']} — Cycle {cycle}.")
        return redirect(url_for("group_detail", group_id=group_id))

    return page("Record Payout", f"""
    <div class="card"><h2>💸 Record Payout</h2>
      <p>Beneficiary: <strong>{safe(member["name"])}</strong></p>
      <p>Current cycle: <strong>{cycle}</strong></p>
      <p>Expected payout: <strong>{money(expected)}</strong></p>
      <form method="POST">
        <label>Payout Amount</label>
        <input type="number" name="amount" value="{expected}" min="0.01" step="0.01" required>
        <label>Payout Date</label>
        <input type="date" name="payout_date" value="{today()}" required>
        <label>Note</label><textarea name="note"></textarea>
        <button class="btn btn-warning" type="submit">Confirm Payout</button>
      </form>
    </div>
    """)


@app.route("/group/<int:group_id>/payouts")
@login_required
def payouts(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    cycle = request.args.get("cycle", type=int) or group_cycle_number(group)
    rows = conn.execute(
        """
        SELECT payouts.*,members.name member_name
        FROM payouts JOIN members ON members.id=payouts.member_id
        WHERE payouts.group_id=? AND payouts.cycle_number=?
        ORDER BY payouts.id DESC
        """,
        (group_id, cycle)
    ).fetchall()
    conn.close()

    content = f"""
    <div class="card"><h2>💸 Payout History</h2>
      <p>Group: <strong>{safe(group["name"])}</strong> · Cycle <strong>{cycle}</strong></p>
      <div class="actions">
        <a class="btn" href="/group/{group_id}/payouts?cycle={max(1,cycle-1)}">← Previous Cycle</a>
        <a class="btn" href="/group/{group_id}/payouts?cycle={cycle+1}">Next Cycle →</a>
      </div><br>
      <div class="table-wrap"><table>
      <tr><th>Beneficiary</th><th>Amount</th><th>Date</th><th>Status</th><th>Note</th></tr>
    """

    for row in rows:
        content += f"""
        <tr><td>{safe(row["member_name"])}</td><td><strong>{money(row["amount"])}</strong></td>
        <td>{safe(row["payout_date"]) or "-"}</td><td>{safe(row["status"]).title()}</td><td>{safe(row["note"]) or "-"}</td></tr>
        """

    content += "</table></div></div>"
    return page("Payouts", content)


# ============================================================
# REMINDERS / WHATSAPP
# ============================================================

@app.route("/group/<int:group_id>/reminders")
@login_required
def reminders(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    cycle = group_cycle_number(group)
    members = conn.execute(
        "SELECT * FROM members WHERE group_id=? AND status='active' ORDER BY position",
        (group_id,)
    ).fetchall()

    rows = []
    for member in members:
        status = contribution_status_for_member(conn, group, member["id"], cycle)
        if status != "paid":
            message = (
                f"Hello {member['name']}, this is a reminder for your "
                f"AjoConnect contribution of {money(group['contribution'])} "
                f"for {group['name']}, cycle {cycle}. "
                f"Please make your payment by {cycle_due_date(group, cycle)}. Thank you."
            )
            url = "https://wa.me/?text=" + urllib.parse.quote(message)
            if member["phone"]:
                digits = "".join(ch for ch in member["phone"] if ch.isdigit())
                if digits.startswith("0"):
                    digits = "234" + digits[1:]
                url = f"https://wa.me/{digits}?text=" + urllib.parse.quote(message)
            rows.append((member, status, url))
    conn.close()

    content = f"""
    <div class="hero"><h1>🔔 Contribution Reminders</h1>
      <p>{safe(group["name"])} · Cycle {cycle} · Due {cycle_due_date(group, cycle)}</p>
    </div>
    <div class="card">
      <p>Members below have not been recorded as fully paid for this cycle.</p>
      <div class="table-wrap"><table>
      <tr><th>Member</th><th>Phone</th><th>Status</th><th>Action</th></tr>
    """

    if rows:
        for member, status, url in rows:
            content += f"""
            <tr><td>{safe(member["name"])}</td><td>{safe(member["phone"]) or "-"}</td>
            <td><span class="badge {status}">{status.title()}</span></td>
            <td><a class="btn" href="{url}" target="_blank">📱 WhatsApp Reminder</a></td></tr>
            """
    else:
        content += '<tr><td colspan="4">🎉 Everyone is recorded as fully paid for this cycle.</td></tr>'

    content += "</table></div></div>"
    return page("Reminders", content)


# ============================================================
# REPORTS / CSV / PRINTABLE
# ============================================================

@app.route("/group/<int:group_id>/reports")
@login_required
def reports(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    cycle = request.args.get("cycle", type=int) or group_cycle_number(group)

    return page("Reports", f"""
    <div class="hero"><h1>📊 Ajo Reports</h1>
      <p>{safe(group["name"])} · Cycle {cycle}</p></div>
    <div class="grid">
      <div class="card"><h3>📥 Contribution CSV</h3><p>Download the full contribution ledger for this cycle.</p>
        <a class="btn" href="/group/{group_id}/reports/contributions.csv?cycle={cycle}">Download CSV</a></div>
      <div class="card"><h3>📥 Payout CSV</h3><p>Download the payout records for this cycle.</p>
        <a class="btn" href="/group/{group_id}/reports/payouts.csv?cycle={cycle}">Download CSV</a></div>
      <div class="card"><h3>🖨️ Printable Report</h3><p>Open a clean report that can be printed or saved as PDF from your browser.</p>
        <a class="btn" href="/group/{group_id}/reports/print?cycle={cycle}" target="_blank">Open Report</a></div>
    </div>
    """)


@app.route("/group/<int:group_id>/reports/contributions.csv")
@login_required
def contribution_csv(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    cycle = request.args.get("cycle", type=int) or group_cycle_number(group)
    conn = get_db()
    rows = conn.execute(
        """
        SELECT m.name,c.amount,c.payment_date,c.status,c.note,c.cycle_number
        FROM contributions c JOIN members m ON m.id=c.member_id
        WHERE c.group_id=? AND c.cycle_number=?
        ORDER BY m.position,c.id
        """,
        (group_id, cycle)
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["AjoConnect Contribution Report"])
    writer.writerow(["Group", group["name"]])
    writer.writerow(["Cycle", cycle])
    writer.writerow([])
    writer.writerow(["Member","Amount","Payment Date","Status","Note","Cycle"])
    for r in rows:
        writer.writerow([r["name"], r["amount"], r["payment_date"], r["status"], r["note"] or "", r["cycle_number"]])

    return (
        output.getvalue(),
        200,
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="ajoconnect_contributions_cycle_{cycle}.csv"'
        }
    )


@app.route("/group/<int:group_id>/reports/payouts.csv")
@login_required
def payout_csv(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    cycle = request.args.get("cycle", type=int) or group_cycle_number(group)
    conn = get_db()
    rows = conn.execute(
        """
        SELECT m.name,p.amount,p.payout_date,p.status,p.note,p.cycle_number
        FROM payouts p JOIN members m ON m.id=p.member_id
        WHERE p.group_id=? AND p.cycle_number=?
        ORDER BY m.position,p.id
        """,
        (group_id, cycle)
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["AjoConnect Payout Report"])
    writer.writerow(["Group", group["name"]])
    writer.writerow(["Cycle", cycle])
    writer.writerow([])
    writer.writerow(["Beneficiary","Amount","Payout Date","Status","Note","Cycle"])
    for r in rows:
        writer.writerow([r["name"], r["amount"], r["payout_date"] or "", r["status"], r["note"] or "", r["cycle_number"]])

    return (
        output.getvalue(),
        200,
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="ajoconnect_payouts_cycle_{cycle}.csv"'
        }
    )


@app.route("/group/<int:group_id>/reports/print")
@login_required
def print_report(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    cycle = request.args.get("cycle", type=int) or group_cycle_number(group)
    conn = get_db()
    members = conn.execute(
        "SELECT * FROM members WHERE group_id=? ORDER BY position",
        (group_id,)
    ).fetchall()
    conn.close()

    rows = ""
    for member in members:
        conn = get_db()
        status = contribution_status_for_member(conn, group, member["id"], cycle)
        score = trust_score(conn, group_id, member["id"])
        total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM contributions WHERE group_id=? AND member_id=? AND cycle_number=? AND status='paid'",
            (group_id, member["id"], cycle)
        ).fetchone()["total"]
        payout = conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM payouts WHERE group_id=? AND member_id=? AND cycle_number=? AND status='paid'",
            (group_id, member["id"], cycle)
        ).fetchone()["total"]
        conn.close()

        rows += f"""
        <tr>
          <td>{member["position"]}</td><td>{safe(member["name"])}</td>
          <td>{money(total)}</td><td>{safe(status).title()}</td>
          <td>{money(payout)}</td><td>{score}/100</td>
        </tr>
        """

    return render_template_string("""
    <!doctype html><html><head><meta charset="UTF-8"><title>AjoConnect Report</title>
    <style>body{font-family:Arial;padding:30px}table{width:100%;border-collapse:collapse}th,td{border:1px solid #ddd;padding:8px}th{background:#f2f2f2}.print{padding:10px 15px;margin-bottom:20px}@media print{.print{display:none}}</style>
    </head><body>
    <button class="print" onclick="window.print()">🖨️ Print / Save as PDF</button>
    <h1>💰 AjoConnect</h1>
    <h2>{{ group["name"] }}</h2>
    <p>Cycle {{ cycle }} · Contribution {{ group["contribution"]|money }} · Frequency {{ group["frequency"].title() }}</p>
    <table><tr><th>Position</th><th>Member</th><th>Contribution</th><th>Status</th><th>Payout</th><th>Trust</th></tr>
    {{ rows|safe }}
    </table>
    <p style="margin-top:30px">Generated by AjoConnect — Digital Ajo & Esusu Management</p>
    </body></html>
    """, group=group, cycle=cycle, rows=rows)


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():
    return jsonify({"status":"ok","app":"AjoConnect","version":"1.1","time":now()})


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
