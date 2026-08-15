from flask import Flask, request, redirect, url_for, session, render_template_string, flash
import sqlite3
import os
import json
import urllib.request
import urllib.error
import uuid
from functools import wraps
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "ajoconnect-development-secret-change-this"
)

DATABASE = os.environ.get("DATABASE_PATH", "ajoconnect.db")
PREMIUM_PRICE = 2000
FREE_GROUP_LIMIT = 1
FREE_MEMBER_LIMIT = 10


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def add_column_if_missing(conn, table, column, definition):
    columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
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
        role TEXT NOT NULL DEFAULT 'admin',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contribution REAL NOT NULL DEFAULT 0,
        frequency TEXT NOT NULL DEFAULT 'monthly',
        created_at TEXT NOT NULL
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
        FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS contributions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'paid',
        note TEXT,
        FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
        FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payout_date TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        note TEXT,
        FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
        FOREIGN KEY (member_id) REFERENCES members(id) ON DELETE CASCADE
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
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)

    # Upgrade existing databases created by earlier AjoConnect versions.
    # These migrations are deliberately additive so existing groups, members,
    # contributions and payouts are not deleted.
    add_column_if_missing(conn, "users", "email", "TEXT")
    add_column_if_missing(conn, "users", "role", "TEXT NOT NULL DEFAULT 'admin'")
    add_column_if_missing(conn, "users", "created_at", "TEXT")
    add_column_if_missing(conn, "users", "plan", "TEXT NOT NULL DEFAULT 'free'")
    add_column_if_missing(conn, "users", "premium_until", "TEXT")
    add_column_if_missing(conn, "groups", "user_id", "INTEGER")
    add_column_if_missing(conn, "groups", "created_at", "TEXT")
    add_column_if_missing(conn, "members", "email", "TEXT")
    add_column_if_missing(conn, "members", "status", "TEXT NOT NULL DEFAULT 'active'")
    add_column_if_missing(conn, "members", "created_at", "TEXT")
    add_column_if_missing(conn, "contributions", "status", "TEXT NOT NULL DEFAULT 'paid'")
    add_column_if_missing(conn, "contributions", "note", "TEXT")
    add_column_if_missing(conn, "payouts", "status", "TEXT NOT NULL DEFAULT 'pending'")
    add_column_if_missing(conn, "payouts", "note", "TEXT")
    add_column_if_missing(conn, "subscriptions", "started_at", "TEXT")
    add_column_if_missing(conn, "subscriptions", "expires_at", "TEXT")

    # Give migrated rows sensible defaults where older schemas did not have them.
    conn.execute("UPDATE users SET role = 'admin' WHERE role IS NULL OR role = ''")
    conn.execute("UPDATE users SET plan = 'free' WHERE plan IS NULL OR plan = ''")
    conn.execute("UPDATE users SET created_at = ? WHERE created_at IS NULL OR created_at = ''", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    conn.execute("UPDATE groups SET created_at = ? WHERE created_at IS NULL OR created_at = ''", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    conn.execute("UPDATE members SET status = 'active' WHERE status IS NULL OR status = ''")
    conn.execute("UPDATE members SET created_at = ? WHERE created_at IS NULL OR created_at = ''", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    conn.execute("UPDATE contributions SET status = 'paid' WHERE status IS NULL OR status = ''")
    conn.execute("UPDATE payouts SET status = 'pending' WHERE status IS NULL OR status = ''")

    # Existing groups from the prototype are assigned to the first admin.
    first_user = conn.execute(
        "SELECT id FROM users ORDER BY id LIMIT 1"
    ).fetchone()

    if first_user:
        conn.execute(
            "UPDATE groups SET user_id = ? WHERE user_id IS NULL",
            (first_user["id"],)
        )

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def money(value):
    return f"₦{float(value or 0):,.2f}"


app.jinja_env.filters["money"] = money


def is_premium_user(user_id):
    conn = get_db()
    user = conn.execute(
        "SELECT plan, premium_until FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    conn.close()

    if not user:
        return False

    if user["plan"] != "premium":
        return False

    if not user["premium_until"]:
        return False

    try:
        return datetime.strptime(
            user["premium_until"], "%Y-%m-%d %H:%M:%S"
        ) > datetime.now()
    except ValueError:
        return False


def refresh_session_plan():
    if "user_id" in session:
        session["premium"] = is_premium_user(session["user_id"])


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            # Prevent stale/deleted sessions from causing 500 errors.
            session.clear()
            flash("Your session has expired. Please log in again.")
            return redirect(url_for("login"))
        session["user_id"] = user["id"]
        session["name"] = user["name"]
        session["role"] = user["role"]
        refresh_session_plan()
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            session.clear()
            flash("Your session has expired. Please log in again.")
            return redirect(url_for("login"))
        if user["role"] != "admin":
            return redirect(url_for("dashboard"))
        session["user_id"] = user["id"]
        session["name"] = user["name"]
        session["role"] = user["role"]
        refresh_session_plan()
        return view(*args, **kwargs)
    return wrapped


def current_user():
    """Return the logged-in user, or None if the session is stale."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    try:
        user = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        return user
    finally:
        conn.close()


def group_belongs_to_user(group_id, user_id):
    conn = get_db()
    group = conn.execute(
        "SELECT * FROM groups WHERE id = ? AND user_id = ?",
        (group_id, user_id)
    ).fetchone()
    conn.close()
    return group


def paystack_request(path, method="GET", payload=None):
    """Call Paystack safely and return (result, error)."""
    secret = (os.environ.get("PAYSTACK_SECRET_KEY") or "").strip()
    if not secret:
        return None, "PAYSTACK_SECRET_KEY is not configured in Render."
    if not secret.startswith(("sk_live_", "sk_test_")):
        return None, "PAYSTACK_SECRET_KEY must start with sk_live_ or sk_test_."

    url = "https://api.paystack.co" + path
    headers = {
        "Authorization": "Bearer " + secret,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            result = json.loads(body)
            if not result.get("status"):
                return result, result.get("message", "Paystack rejected the request.")
            return result, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
            parsed = json.loads(body)
            message = parsed.get("message") or body
        except Exception:
            message = str(e)
        return None, f"Paystack error ({e.code}): {message}"
    except urllib.error.URLError as e:
        return None, f"Could not connect to Paystack: {e.reason}"
    except Exception as e:
        return None, f"Payment connection error: {e}"


def activate_premium(user_id, reference):
    """Idempotently mark a verified Premium transaction as paid."""
    conn = get_db()
    try:
        subscription = conn.execute(
            "SELECT * FROM subscriptions WHERE reference = ? AND user_id = ?",
            (reference, user_id)
        ).fetchone()
        if not subscription:
            return False
        if subscription["status"] == "paid":
            return True

        user = conn.execute(
            "SELECT premium_until FROM users WHERE id = ?",
            (user_id,)
        ).fetchone()
        if not user:
            return False

        start = datetime.now()
        if user["premium_until"]:
            try:
                old_expiry = datetime.strptime(user["premium_until"], "%Y-%m-%d %H:%M:%S")
                if old_expiry > start:
                    start = old_expiry
            except (ValueError, TypeError):
                pass

        expiry = start + timedelta(days=30)
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        expires = expiry.strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """
            UPDATE subscriptions
            SET status = 'paid', started_at = ?, expires_at = ?
            WHERE reference = ? AND user_id = ?
            """,
            (started, expires, reference, user_id)
        )
        conn.execute(
            "UPDATE users SET plan = 'premium', premium_until = ? WHERE id = ?",
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
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title or "AjoConnect" }}</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f4f7f6;
            color: #1f2937;
        }
        nav {
            background: #075e54;
            color: white;
            padding: 15px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
        }
        nav a {
            color: white;
            text-decoration: none;
            margin-left: 12px;
            font-weight: bold;
        }
        .brand { font-size: 22px; font-weight: bold; }
        .container {
            max-width: 1100px;
            margin: 25px auto;
            padding: 0 15px;
        }
        .hero {
            background: linear-gradient(135deg, #075e54, #128c7e);
            color: white;
            padding: 30px;
            border-radius: 16px;
            margin-bottom: 25px;
        }
        .hero h1 { margin-top: 0; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
            gap: 15px;
            margin-bottom: 25px;
        }
        .card {
            background: white;
            padding: 20px;
            border-radius: 14px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            margin-bottom: 20px;
        }
        .stat {
            font-size: 27px;
            font-weight: bold;
            color: #075e54;
            margin-top: 8px;
        }
        .btn {
            display: inline-block;
            border: none;
            background: #075e54;
            color: white;
            padding: 11px 16px;
            border-radius: 8px;
            text-decoration: none;
            cursor: pointer;
            font-weight: bold;
        }
        .btn:hover { opacity: .9; }
        .btn-warning { background: #e09f00; }
        .btn-secondary { background: #555; }
        .btn-premium { background: #8a5a00; }
        input, select, textarea {
            width: 100%;
            padding: 12px;
            margin-top: 6px;
            margin-bottom: 15px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 15px;
        }
        label { font-weight: bold; }
        table { width: 100%; border-collapse: collapse; }
        th, td {
            padding: 12px;
            border-bottom: 1px solid #eee;
            text-align: left;
        }
        th { background: #f0f4f3; }
        .table-wrap { overflow-x: auto; }
        .badge {
            display: inline-block;
            padding: 5px 9px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
        }
        .paid, .success {
            background: #d7f5df;
            color: #176b35;
        }
        .pending {
            background: #fff0c2;
            color: #7a5700;
        }
        .flash {
            padding: 12px 15px;
            background: #fff3cd;
            border-radius: 8px;
            margin-bottom: 15px;
        }
        .actions {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }
        .member-row {
            cursor: pointer;
            transition: background 0.15s ease;
        }
        .member-row:hover {
            background: #f7faf9;
        }
        .member-row td {
            vertical-align: middle;
        }
        .member-link {
            color: inherit;
            text-decoration: none;
            display: block;
        }
        .member-link strong {
            text-decoration: underline;
            text-decoration-thickness: 1px;
            text-underline-offset: 3px;
        }
        .view-member {
            white-space: nowrap;
        }
        .premium-box {
            border: 2px solid #d8a63a;
            background: #fffaf0;
        }
        footer {
            text-align: center;
            padding: 30px;
            color: #777;
        }
        .member-link small {
            display: block;
            margin-top: 3px;
            color: #777;
            font-size: 11px;
        }
        @media(max-width: 600px) {
            nav { display: block; }
            nav a {
                display: inline-block;
                margin: 8px 8px 0 0;
            }
            th, td { font-size: 13px; }
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
            {% if session.get("role") == "admin" %}
                <a href="{{ url_for('admin_dashboard') }}">Admin</a>
            {% endif %}
            <a href="{{ url_for('logout') }}">Logout</a>
        {% else %}
            <a href="{{ url_for('login') }}">Login</a>
            <a href="{{ url_for('register') }}">Register</a>
        {% endif %}
    </div>
</nav>
<div class="container">
    {% with messages = get_flashed_messages() %}
        {% for message in messages %}
            <div class="flash">{{ message }}</div>
        {% endfor %}
    {% endwith %}
    {{ content|safe }}
</div>
<footer>AjoConnect © 2026 — Digital Ajo & Esusu Management</footer>
</body>
</html>
"""


def page(title, content):
    return render_template_string(
        BASE_HTML,
        title=title,
        content=content
    )


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    content = """
    <div class="hero">
        <h1>💰 Welcome to AjoConnect</h1>
        <p>Manage your Ajo / Esusu savings group, contributions and rotation schedule.</p>
        <a class="btn" href="/register">Create Free Account</a>
        <a class="btn btn-secondary" href="/login">Login</a>
    </div>

    <div class="grid">
        <div class="card">
            <h3>👥 Manage Members</h3>
            <p>Organize members and their rotation positions.</p>
        </div>
        <div class="card">
            <h3>💵 Track Contributions</h3>
            <p>Record payments and contribution history.</p>
        </div>
        <div class="card">
            <h3>🔄 Rotation</h3>
            <p>Know who receives the Ajo payout next.</p>
        </div>
        <div class="card premium-box">
            <h3>⭐ AjoConnect Premium</h3>
            <p>Unlock unlimited groups, members and advanced features for ₦2,000/month.</p>
        </div>
    </div>
    """
    return page("Home", content)


# ============================================================
# REGISTER
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Please complete all fields.")
            return redirect(url_for("register"))

        conn = get_db()
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if existing:
            conn.close()
            flash("An account with this email already exists.")
            return redirect(url_for("login"))

        conn.execute(
            """
            INSERT INTO users
            (name, email, password, role, plan, created_at)
            VALUES (?, ?, ?, 'admin', 'free', ?)
            """,
            (name, email, generate_password_hash(password), now())
        )
        conn.commit()
        conn.close()

        flash("Account created successfully. You can now login.")
        return redirect(url_for("login"))

    content = """
    <div class="card">
        <h2>📝 Create AjoConnect Account</h2>
        <p>Start free. Upgrade later when you need more features.</p>
        <form method="POST">
            <label>Full Name</label>
            <input type="text" name="name" required>
            <label>Email</label>
            <input type="email" name="email" required>
            <label>Password</label>
            <input type="password" name="password" minlength="6" required>
            <button class="btn" type="submit">Create Free Account</button>
        </form>
    </div>
    """
    return page("Register", content)


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ?",
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

    content = """
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
    """
    return page("Login", content)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


# ============================================================
# SUBSCRIPTION / PAYSTACK
# ============================================================

@app.route("/subscription")
@login_required
def subscription():
    refresh_session_plan()
    user = current_user()
    if user is None:
        session.clear()
        flash("Your session has expired. Please log in again.")
        return redirect(url_for("login"))

    if is_premium_user(user["id"]):
        expiry = user["premium_until"]
        content = f"""
        <div class="hero">
            <h1>⭐ AjoConnect Premium</h1>
            <p>Your Premium membership is active.</p>
        </div>
        <div class="card premium-box">
            <h2>Premium Active ✅</h2>
            <p>Premium access until:</p>
            <div class="stat">{expiry}</div>
            <p>Thank you for supporting AjoConnect.</p>
        </div>
        """
        return page("Premium", content)

    content = f"""
    <div class="hero">
        <h1>⭐ Upgrade AjoConnect</h1>
        <p>Get more space and tools for your Ajo business.</p>
    </div>

    <div class="card premium-box">
        <h2>Premium — ₦{PREMIUM_PRICE:,.0f}/month</h2>
        <ul>
            <li>Unlimited Ajo groups</li>
            <li>Unlimited members</li>
            <li>Contribution tracking</li>
            <li>Payout records</li>
            <li>Advanced Ajo management</li>
            <li>30 days of Premium access</li>
        </ul>
        <br>
        <a class="btn btn-premium" href="/pay/premium">
            Upgrade for ₦{PREMIUM_PRICE:,.0f}
        </a>
    </div>

    <div class="card">
        <h3>Free Plan</h3>
        <p>1 Ajo group and up to 10 members.</p>
    </div>
    """
    return page("Premium", content)


@app.route("/pay/premium")
@login_required
def pay_premium():
    user = current_user()
    if user is None:
        session.clear()
        flash("Your session has expired. Please log in again.")
        return redirect(url_for("login"))

    if is_premium_user(user["id"]):
        flash("Your Premium membership is already active.")
        return redirect(url_for("subscription"))

    email = (user["email"] or "").strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        flash("Please update your account with a valid email address before paying.")
        return redirect(url_for("subscription"))

    reference = "AJOCONNECT-" + uuid.uuid4().hex.upper()
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO subscriptions
            (user_id, plan, amount, reference, status, created_at)
            VALUES (?, 'premium', ?, ?, 'pending', ?)
            """,
            (user["id"], PREMIUM_PRICE, reference, now())
        )
        conn.commit()
    finally:
        conn.close()

    payload = {
        "email": email,
        "amount": int(PREMIUM_PRICE * 100),
        "currency": "NGN",
        "reference": reference,
        "callback_url": request.host_url.rstrip("/") + url_for("paystack_callback"),
        "metadata": {
            "user_id": user["id"],
            "product": "AjoConnect Premium"
        }
    }

    result, error = paystack_request("/transaction/initialize", "POST", payload)
    if error or not result or not result.get("status"):
        conn = get_db()
        try:
            conn.execute("UPDATE subscriptions SET status = 'failed' WHERE reference = ?", (reference,))
            conn.commit()
        finally:
            conn.close()
        flash("Payment could not be started: " + (error or "Paystack returned an invalid response."))
        return redirect(url_for("subscription"))

    authorization_url = (result.get("data") or {}).get("authorization_url")
    if not authorization_url:
        flash("Payment could not be started because Paystack returned no checkout link.")
        return redirect(url_for("subscription"))

    return redirect(authorization_url)


@app.route("/paystack/callback")
def paystack_callback():
    """Verify Paystack payment without requiring an existing browser session."""
    reference = request.args.get("reference", "").strip()
    if not reference:
        flash("No payment reference was received.")
        return redirect(url_for("login"))

    result, error = paystack_request("/transaction/verify/" + reference)
    if error or not result or not result.get("status"):
        flash("Payment could not be verified: " + (error or "Paystack returned an invalid response."))
        return redirect(url_for("subscription")) if current_user() else redirect(url_for("login"))

    transaction = result.get("data") or {}
    conn = get_db()
    try:
        subscription = conn.execute(
            "SELECT * FROM subscriptions WHERE reference = ?",
            (reference,)
        ).fetchone()
    finally:
        conn.close()

    if not subscription:
        flash("Payment record not found.")
        return redirect(url_for("login"))

    expected_amount = int(PREMIUM_PRICE * 100)
    if (
        transaction.get("status") == "success"
        and transaction.get("currency") == "NGN"
        and int(transaction.get("amount") or 0) == expected_amount
    ):
        if activate_premium(subscription["user_id"], reference):
            session.clear()
            session["user_id"] = subscription["user_id"]
            user = current_user()
            if user:
                session["name"] = user["name"]
                session["role"] = user["role"]
                session["premium"] = True
            flash("Payment successful! Your Premium account is now active for 30 days.")
            return redirect(url_for("subscription"))

    flash("Payment was not completed successfully.")
    return redirect(url_for("subscription")) if current_user() else redirect(url_for("login"))


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():
    refresh_session_plan()
    conn = get_db()

    groups = conn.execute(
        """
        SELECT *
        FROM groups
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    total_members = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM members
        JOIN groups ON groups.id = members.group_id
        WHERE groups.user_id = ?
        """,
        (session["user_id"],)
    ).fetchone()["total"]

    total_contributions = conn.execute(
        """
        SELECT COALESCE(SUM(contributions.amount),0) AS total
        FROM contributions
        JOIN groups ON groups.id = contributions.group_id
        WHERE groups.user_id = ?
        AND contributions.status = 'paid'
        """,
        (session["user_id"],)
    ).fetchone()["total"]

    total_payouts = conn.execute(
        """
        SELECT COALESCE(SUM(payouts.amount),0) AS total
        FROM payouts
        JOIN groups ON groups.id = payouts.group_id
        WHERE groups.user_id = ?
        AND payouts.status = 'paid'
        """,
        (session["user_id"],)
    ).fetchone()["total"]

    conn.close()

    plan_text = "⭐ PREMIUM" if session.get("premium") else "FREE"

    content = f"""
    <div class="hero">
        <h1>👋 Welcome, {session.get("name")}</h1>
        <p>Plan: <strong>{plan_text}</strong></p>
    </div>

    <div class="grid">
        <div class="card">
            <h3>👥 Members</h3>
            <div class="stat">{total_members}</div>
        </div>
        <div class="card">
            <h3>💰 Contributions</h3>
            <div class="stat">{money(total_contributions)}</div>
        </div>
        <div class="card">
            <h3>💸 Payouts</h3>
            <div class="stat">{money(total_payouts)}</div>
        </div>
    </div>

    <div class="card">
        <div class="actions">
            <a class="btn" href="/groups/new">➕ Create Ajo Group</a>
            <a class="btn btn-secondary" href="/groups">📋 View Groups</a>
            {"<a class='btn btn-premium' href='/subscription'>⭐ Premium</a>" if not session.get("premium") else ""}
        </div>
    </div>

    <div class="card">
        <h2>Your Ajo Groups</h2>
        <div class="table-wrap">
        <table>
            <tr>
                <th>Group</th>
                <th>Contribution</th>
                <th>Frequency</th>
                <th>Action</th>
            </tr>
    """

    for group in groups:
        content += f"""
        <tr>
            <td>{group["name"]}</td>
            <td>{money(group["contribution"])}</td>
            <td>{group["frequency"].title()}</td>
            <td><a class="btn" href="/group/{group["id"]}">Open</a></td>
        </tr>
        """

    content += """
        </table>
        </div>
    </div>
    """

    return page("Dashboard", content)


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()

    users = conn.execute(
        "SELECT COUNT(*) AS total FROM users"
    ).fetchone()["total"]

    groups = conn.execute(
        "SELECT COUNT(*) AS total FROM groups"
    ).fetchone()["total"]

    members = conn.execute(
        "SELECT COUNT(*) AS total FROM members"
    ).fetchone()["total"]

    contributions = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS total
        FROM contributions
        WHERE status = 'paid'
        """
    ).fetchone()["total"]

    payouts = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS total
        FROM payouts
        WHERE status = 'paid'
        """
    ).fetchone()["total"]

    premium_users = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM users
        WHERE plan = 'premium'
        AND premium_until > ?
        """,
        (now(),)
    ).fetchone()["total"]

    premium_revenue = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS total
        FROM subscriptions
        WHERE status = 'paid'
        """
    ).fetchone()["total"]

    conn.close()

    content = f"""
    <div class="hero">
        <h1>⚙️ AjoConnect Admin</h1>
        <p>Platform overview and Premium revenue.</p>
    </div>

    <div class="grid">
        <div class="card">
            <h3>Users</h3>
            <div class="stat">{users}</div>
        </div>
        <div class="card">
            <h3>Ajo Groups</h3>
            <div class="stat">{groups}</div>
        </div>
        <div class="card">
            <h3>Members</h3>
            <div class="stat">{members}</div>
        </div>
        <div class="card">
            <h3>Premium Users</h3>
            <div class="stat">{premium_users}</div>
        </div>
        <div class="card premium-box">
            <h3>Premium Revenue</h3>
            <div class="stat">{money(premium_revenue)}</div>
        </div>
        <div class="card">
            <h3>Ajo Contributions</h3>
            <div class="stat">{money(contributions)}</div>
        </div>
        <div class="card">
            <h3>Ajo Payouts</h3>
            <div class="stat">{money(payouts)}</div>
        </div>
    </div>

    <div class="card">
        <h2>💡 Monetization</h2>
        <p>Premium price: <strong>{money(PREMIUM_PRICE)}/30 days</strong></p>
        <p>Premium payments are verified through Paystack.</p>
    </div>
    """

    return page("Admin Dashboard", content)


# ============================================================
# GROUPS
# ============================================================

@app.route("/groups")
@login_required
def groups():
    conn = get_db()
    all_groups = conn.execute(
        """
        SELECT *
        FROM groups
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],)
    ).fetchall()
    conn.close()

    content = """
    <div class="card">
        <div class="actions">
            <h2 style="margin-right:auto;">📋 Ajo Groups</h2>
            <a class="btn" href="/groups/new">➕ New Group</a>
        </div>
    </div>
    <div class="grid">
    """

    for group in all_groups:
        content += f"""
        <div class="card">
            <h2>{group["name"]}</h2>
            <p>Contribution: <strong>{money(group["contribution"])}</strong></p>
            <p>Frequency: <strong>{group["frequency"].title()}</strong></p>
            <a class="btn" href="/group/{group["id"]}">Open Group</a>
        </div>
        """

    content += "</div>"
    return page("Groups", content)


# ============================================================
# CREATE GROUP
# ============================================================

@app.route("/groups/new", methods=["GET", "POST"])
@login_required
def create_group():
    conn = get_db()

    group_count = conn.execute(
        "SELECT COUNT(*) AS total FROM groups WHERE user_id = ?",
        (session["user_id"],)
    ).fetchone()["total"]

    premium = is_premium_user(session["user_id"])

    if not premium and group_count >= FREE_GROUP_LIMIT:
        conn.close()
        flash("The Free plan allows 1 Ajo group. Upgrade to Premium for unlimited groups.")
        return redirect(url_for("subscription"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        contribution = request.form.get("contribution", "0")
        frequency = request.form.get("frequency", "monthly")

        try:
            contribution = float(contribution)
        except ValueError:
            conn.close()
            flash("Contribution must be a valid amount.")
            return redirect(url_for("create_group"))

        if not name:
            conn.close()
            flash("Group name is required.")
            return redirect(url_for("create_group"))

        if contribution <= 0:
            conn.close()
            flash("Contribution must be greater than zero.")
            return redirect(url_for("create_group"))

        if frequency not in {"weekly", "biweekly", "monthly"}:
            frequency = "monthly"

        conn.execute(
            """
            INSERT INTO groups
            (name, contribution, frequency, created_at, user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                contribution,
                frequency,
                now(),
                session["user_id"]
            )
        )

        conn.commit()
        group_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]
        conn.close()

        flash("Ajo group created successfully.")
        return redirect(url_for("group_detail", group_id=group_id))

    conn.close()

    content = """
    <div class="card">
        <h2>➕ Create Ajo Group</h2>
        <form method="POST">
            <label>Group Name</label>
            <input type="text" name="name" placeholder="e.g. Mama Raji" required>

            <label>Contribution Amount</label>
            <input type="number" name="contribution" min="0" step="0.01"
                   placeholder="10000" required>

            <label>Contribution Frequency</label>
            <select name="frequency">
                <option value="weekly">Weekly</option>
                <option value="biweekly">Bi-weekly</option>
                <option value="monthly" selected>Monthly</option>
            </select>

            <button class="btn" type="submit">Create Group</button>
        </form>
    </div>
    """
    return page("Create Group", content)


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
        """
        SELECT *
        FROM members
        WHERE group_id = ?
        ORDER BY position ASC
        """,
        (group_id,)
    ).fetchall()

    paid_total = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS total
        FROM contributions
        WHERE group_id = ? AND status = 'paid'
        """,
        (group_id,)
    ).fetchone()["total"]

    payout_total = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS total
        FROM payouts
        WHERE group_id = ? AND status = 'paid'
        """,
        (group_id,)
    ).fetchone()["total"]

    current_member = None

    for member in members:
        payout = conn.execute(
            """
            SELECT id
            FROM payouts
            WHERE group_id = ? AND member_id = ? AND status = 'paid'
            """,
            (group_id, member["id"])
        ).fetchone()

        if not payout:
            current_member = member
            break

    conn.close()

    expected_payout = group["contribution"] * len(members)

    content = f"""
    <div class="hero">
        <h1>🔄 {group["name"]}</h1>
        <p>Contribution: <strong>{money(group["contribution"])}</strong></p>
        <p>Members: <strong>{len(members)}</strong></p>
        <p>Expected payout each round: <strong>{money(expected_payout)}</strong></p>
    </div>

    <div class="grid">
        <div class="card">
            <h3>Total Contributions</h3>
            <div class="stat">{money(paid_total)}</div>
        </div>
        <div class="card">
            <h3>Total Payouts</h3>
            <div class="stat">{money(payout_total)}</div>
        </div>
        <div class="card">
            <h3>Current Beneficiary</h3>
            <div class="stat">{current_member["name"] if current_member else "Completed"}</div>
        </div>
    </div>

    <div class="card">
        <div class="actions">
            <a class="btn" href="/group/{group_id}/member/add">➕ Add Member</a>
            <a class="btn btn-secondary" href="/group/{group_id}/contributions">💰 Contributions</a>
            <a class="btn btn-secondary" href="/group/{group_id}/payouts">💸 Payouts</a>
        </div>
    </div>

    <div class="card">
        <h2>📅 Rotation Schedule</h2>
        <div class="table-wrap">
        <table>
            <tr>
                <th>Position</th>
                <th>Member</th>
                <th>Phone</th>
                <th>Status</th>
                <th>Expected Payout</th>
                <th>Action</th>
            </tr>
    """

    for member in members:
        content += f"""
        <tr class="member-row" onclick="window.location.href='/group/{group_id}/member/{member["id"]}'">
            <td>{member["position"]}</td>
            <td>
                <a class="member-link" href="/group/{group_id}/member/{member["id"]}">
                    <strong>{member["name"]}</strong>
                    <small>Tap to view details</small>
                </a>
            </td>
            <td>{member["phone"] or "-"}</td>
            <td><span class="badge success">{member["status"].title()}</span></td>
            <td>{money(expected_payout)}</td>
            <td>
                <div class="actions">
                    <a class="btn view-member" href="/group/{group_id}/member/{member["id"]}">View Details</a>
                    <a class="btn" href="/group/{group_id}/member/{member["id"]}/contribute">Payment</a>
                    <a class="btn btn-warning" href="/group/{group_id}/member/{member["id"]}/payout">Payout</a>
                </div>
            </td>
        </tr>
        """

    content += """
        </table>
        </div>
    </div>
    """

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
        "SELECT * FROM members WHERE id = ? AND group_id = ?",
        (member_id, group_id)
    ).fetchone()

    if not member:
        conn.close()
        return "Member not found", 404

    contribution_rows = conn.execute(
        """
        SELECT amount, payment_date, status, note
        FROM contributions
        WHERE group_id = ? AND member_id = ?
        ORDER BY payment_date DESC, id DESC
        """,
        (group_id, member_id)
    ).fetchall()

    payout_rows = conn.execute(
        """
        SELECT amount, payout_date, status, note
        FROM payouts
        WHERE group_id = ? AND member_id = ?
        ORDER BY payout_date DESC, id DESC
        """,
        (group_id, member_id)
    ).fetchall()

    contribution_total = sum(float(row["amount"] or 0) for row in contribution_rows)
    payout_total = sum(float(row["amount"] or 0) for row in payout_rows if row["status"] == "paid")
    expected_payout = group["contribution"] * conn.execute(
        "SELECT COUNT(*) FROM members WHERE group_id = ? AND status = 'active'",
        (group_id,)
    ).fetchone()[0]
    conn.close()

    content = f"""
    <div class="hero">
        <h1>👤 {member["name"]}</h1>
        <p>Member details for <strong>{group["name"]}</strong></p>
    </div>

    <div class="grid">
        <div class="card">
            <h3>Position</h3>
            <div class="stat">#{member["position"]}</div>
        </div>
        <div class="card">
            <h3>Phone</h3>
            <div class="stat">{member["phone"] or "-"}</div>
        </div>
        <div class="card">
            <h3>Status</h3>
            <div class="stat">{member["status"].title()}</div>
        </div>
        <div class="card">
            <h3>Total Contributions</h3>
            <div class="stat">{money(contribution_total)}</div>
        </div>
        <div class="card">
            <h3>Total Payout Received</h3>
            <div class="stat">{money(payout_total)}</div>
        </div>
        <div class="card">
            <h3>Expected Payout</h3>
            <div class="stat">{money(expected_payout)}</div>
        </div>
    </div>

    <div class="card">
        <h2>📋 Member Information</h2>
        <p><strong>Name:</strong> {member["name"]}</p>
        <p><strong>Phone:</strong> {member["phone"] or "Not provided"}</p>
        <p><strong>Email:</strong> {member["email"] or "Not provided"}</p>
        <p><strong>Rotation Position:</strong> {member["position"]}</p>
        <p><strong>Status:</strong> <span class="badge success">{member["status"].title()}</span></p>
        <p><strong>Joined:</strong> {member["created_at"]}</p>
        <div class="actions">
            <a class="btn" href="/group/{group_id}/member/{member_id}/contribute">💰 Record Contribution</a>
            <a class="btn btn-warning" href="/group/{group_id}/member/{member_id}/payout">💸 Record Payout</a>
            <a class="btn btn-secondary" href="/group/{group_id}">← Back to Rotation</a>
        </div>
    </div>

    <div class="card">
        <h2>💰 Contribution History</h2>
        <div class="table-wrap">
        <table>
            <tr><th>Amount</th><th>Date</th><th>Status</th><th>Note</th></tr>
    """

    if contribution_rows:
        for row in contribution_rows:
            content += f"""
            <tr>
                <td><strong>{money(row["amount"])}</strong></td>
                <td>{row["payment_date"]}</td>
                <td><span class="badge paid">{row["status"].title()}</span></td>
                <td>{row["note"] or "-"}</td>
            </tr>
            """
    else:
        content += '<tr><td colspan="4">No contribution recorded yet.</td></tr>'

    content += """
        </table>
        </div>
    </div>

    <div class="card">
        <h2>💸 Payout History</h2>
        <div class="table-wrap">
        <table>
            <tr><th>Amount</th><th>Date</th><th>Status</th><th>Note</th></tr>
    """

    if payout_rows:
        for row in payout_rows:
            badge_class = "paid" if row["status"] == "paid" else "pending"
            content += f"""
            <tr>
                <td><strong>{money(row["amount"])}</strong></td>
                <td>{row["payout_date"] or "-"}</td>
                <td><span class="badge {badge_class}">{row["status"].title()}</span></td>
                <td>{row["note"] or "-"}</td>
            </tr>
            """
    else:
        content += '<tr><td colspan="4">No payout recorded yet.</td></tr>'

    content += """
        </table>
        </div>
    </div>
    """

    return page(member["name"], content)


# ============================================================
# ADD MEMBER

# ============================================================

@app.route("/group/<int:group_id>/member/add", methods=["GET", "POST"])
@login_required
def add_member(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])

    if not group:
        return "Group not found", 404

    conn = get_db()

    member_count = conn.execute(
        "SELECT COUNT(*) AS total FROM members WHERE group_id = ?",
        (group_id,)
    ).fetchone()["total"]

    premium = is_premium_user(session["user_id"])

    if not premium and member_count >= FREE_MEMBER_LIMIT:
        conn.close()
        flash("The Free plan allows 10 members per group. Upgrade to Premium for unlimited members.")
        return redirect(url_for("subscription"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()

        if not name:
            conn.close()
            flash("Member name is required.")
            return redirect(url_for("add_member", group_id=group_id))

        next_position = conn.execute(
            """
            SELECT COALESCE(MAX(position),0) + 1 AS next_position
            FROM members
            WHERE group_id = ?
            """,
            (group_id,)
        ).fetchone()["next_position"]

        conn.execute(
            """
            INSERT INTO members
            (group_id, name, phone, email, position, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
            """,
            (group_id, name, phone, email, next_position, now())
        )

        conn.commit()
        conn.close()

        flash(f"{name} has been added to the Ajo group.")
        return redirect(url_for("group_detail", group_id=group_id))

    conn.close()

    content = f"""
    <div class="card">
        <h2>➕ Add Member</h2>
        <p>Group: <strong>{group["name"]}</strong></p>
        <form method="POST">
            <label>Member Name</label>
            <input type="text" name="name" placeholder="Full name" required>
            <label>Phone Number</label>
            <input type="text" name="phone" placeholder="080...">
            <label>Email</label>
            <input type="email" name="email" placeholder="member@email.com">
            <button class="btn" type="submit">Add Member</button>
        </form>
    </div>
    """
    return page("Add Member", content)


# ============================================================
# CONTRIBUTION
# ============================================================

@app.route("/group/<int:group_id>/member/<int:member_id>/contribute", methods=["GET", "POST"])
@login_required
def record_contribution(group_id, member_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    member = conn.execute(
        "SELECT * FROM members WHERE id = ? AND group_id = ?",
        (member_id, group_id)
    ).fetchone()

    if not member:
        conn.close()
        return "Member not found", 404

    if request.method == "POST":
        amount = request.form.get("amount", str(group["contribution"]))
        payment_date = request.form.get(
            "payment_date", datetime.now().strftime("%Y-%m-%d")
        )
        note = request.form.get("note", "").strip()

        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except ValueError:
            conn.close()
            flash("Contribution amount must be greater than zero.")
            return redirect(url_for(
                "record_contribution",
                group_id=group_id,
                member_id=member_id
            ))

        conn.execute(
            """
            INSERT INTO contributions
            (group_id, member_id, amount, payment_date, status, note)
            VALUES (?, ?, ?, ?, 'paid', ?)
            """,
            (group_id, member_id, amount, payment_date, note)
        )
        conn.commit()
        conn.close()

        flash(f"Payment of {money(amount)} recorded for {member['name']}.")
        return redirect(url_for("group_detail", group_id=group_id))

    conn.close()

    content = f"""
    <div class="card">
        <h2>💰 Record Contribution</h2>
        <p>Member: <strong>{member["name"]}</strong></p>
        <p>Expected contribution: <strong>{money(group["contribution"])}</strong></p>
        <form method="POST">
            <label>Amount Paid</label>
            <input type="number" name="amount" value="{group["contribution"]}"
                   min="0" step="0.01" required>
            <label>Payment Date</label>
            <input type="date" name="payment_date"
                   value="{datetime.now().strftime("%Y-%m-%d")}" required>
            <label>Note</label>
            <textarea name="note" placeholder="Optional note"></textarea>
            <button class="btn" type="submit">Record Payment</button>
        </form>
    </div>
    """
    return page("Record Contribution", content)


# ============================================================
# CONTRIBUTIONS
# ============================================================

@app.route("/group/<int:group_id>/contributions")
@login_required
def contributions(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    records = conn.execute(
        """
        SELECT contributions.*, members.name AS member_name
        FROM contributions
        JOIN members ON members.id = contributions.member_id
        WHERE contributions.group_id = ?
        ORDER BY contributions.payment_date DESC, contributions.id DESC
        """,
        (group_id,)
    ).fetchall()
    conn.close()

    content = f"""
    <div class="card">
        <h2>💰 Contribution History</h2>
        <p>Group: <strong>{group["name"]}</strong></p>
        <div class="table-wrap">
        <table>
            <tr>
                <th>Member</th><th>Amount</th><th>Date</th><th>Status</th><th>Note</th>
            </tr>
    """

    for row in records:
        content += f"""
        <tr>
            <td>{row["member_name"]}</td>
            <td><strong>{money(row["amount"])}</strong></td>
            <td>{row["payment_date"]}</td>
            <td><span class="badge paid">{row["status"].title()}</span></td>
            <td>{row["note"] or "-"}</td>
        </tr>
        """

    content += "</table></div></div>"
    return page("Contributions", content)


# ============================================================
# PAYOUT
# ============================================================

@app.route("/group/<int:group_id>/member/<int:member_id>/payout", methods=["GET", "POST"])
@login_required
def record_payout(group_id, member_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()

    member = conn.execute(
        "SELECT * FROM members WHERE id = ? AND group_id = ?",
        (member_id, group_id)
    ).fetchone()

    if not member:
        conn.close()
        return "Member not found", 404

    member_count = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM members
        WHERE group_id = ? AND status = 'active'
        """,
        (group_id,)
    ).fetchone()["total"]

    expected_payout = group["contribution"] * member_count

    already_paid = conn.execute(
        "SELECT id FROM payouts WHERE group_id = ? AND member_id = ? AND status = 'paid' LIMIT 1",
        (group_id, member_id)
    ).fetchone()

    if already_paid:
        conn.close()
        flash("This member has already received a payout for this Ajo cycle.")
        return redirect(url_for("member_detail", group_id=group_id, member_id=member_id))

    if request.method == "POST":
        amount = request.form.get("amount", str(expected_payout))
        payout_date = request.form.get(
            "payout_date", datetime.now().strftime("%Y-%m-%d")
        )
        note = request.form.get("note", "").strip()

        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except ValueError:
            conn.close()
            flash("Payout amount must be greater than zero.")
            return redirect(url_for(
                "record_payout",
                group_id=group_id,
                member_id=member_id
            ))

        conn.execute(
            """
            INSERT INTO payouts
            (group_id, member_id, amount, payout_date, status, note)
            VALUES (?, ?, ?, ?, 'paid', ?)
            """,
            (group_id, member_id, amount, payout_date, note)
        )
        conn.commit()
        conn.close()

        flash(f"Payout of {money(amount)} recorded for {member['name']}.")
        return redirect(url_for("group_detail", group_id=group_id))

    conn.close()

    content = f"""
    <div class="card">
        <h2>💸 Record Payout</h2>
        <p>Beneficiary: <strong>{member["name"]}</strong></p>
        <p>Expected payout: <strong>{money(expected_payout)}</strong></p>
        <form method="POST">
            <label>Payout Amount</label>
            <input type="number" name="amount" value="{expected_payout}"
                   min="0" step="0.01" required>
            <label>Payout Date</label>
            <input type="date" name="payout_date"
                   value="{datetime.now().strftime("%Y-%m-%d")}" required>
            <label>Note</label>
            <textarea name="note" placeholder="Optional payout note"></textarea>
            <button class="btn btn-warning" type="submit">Confirm Payout</button>
        </form>
    </div>
    """
    return page("Record Payout", content)


# ============================================================
# PAYOUT HISTORY
# ============================================================

@app.route("/group/<int:group_id>/payouts")
@login_required
def payouts(group_id):
    group = group_belongs_to_user(group_id, session["user_id"])
    if not group:
        return "Group not found", 404

    conn = get_db()
    records = conn.execute(
        """
        SELECT payouts.*, members.name AS member_name
        FROM payouts
        JOIN members ON members.id = payouts.member_id
        WHERE payouts.group_id = ?
        ORDER BY payouts.id DESC
        """,
        (group_id,)
    ).fetchall()
    conn.close()

    content = f"""
    <div class="card">
        <h2>💸 Payout History</h2>
        <p>Group: <strong>{group["name"]}</strong></p>
        <div class="table-wrap">
        <table>
            <tr>
                <th>Beneficiary</th><th>Amount</th><th>Date</th><th>Status</th><th>Note</th>
            </tr>
    """

    for row in records:
        content += f"""
        <tr>
            <td>{row["member_name"]}</td>
            <td><strong>{money(row["amount"])}</strong></td>
            <td>{row["payout_date"] or "-"}</td>
            <td><span class="badge success">{row["status"].title()}</span></td>
            <td>{row["note"] or "-"}</td>
        </tr>
        """

    content += "</table></div></div>"
    return page("Payouts", content)


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():
    return {"status": "ok", "app": "AjoConnect"}


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
