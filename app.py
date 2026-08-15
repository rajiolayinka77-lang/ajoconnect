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

    # Upgrade existing databases created by the earlier AjoConnect version.
    add_column_if_missing(conn, "groups", "user_id", "INTEGER")
    add_column_if_missing(conn, "users", "plan", "TEXT NOT NULL DEFAULT 'free'")
    add_column_if_missing(conn, "users", "premium_until", "TEXT")

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
        if "user_id" not in session:
            return redirect(url_for("login"))
        refresh_session_plan()
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            return redirect(url_for("dashboard"))
        refresh_session_plan()
        return view(*args, **kwargs)
    return wrapped


def current_user():
    if "user_id" not in session:
        return None
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE id = ?",
        (session["user_id"],)
    ).fetchone()
    conn.close()
    return user


def group_belongs_to_user(group_id, user_id):
    conn = get_db()
    group = conn.execute(
        "SELECT * FROM groups WHERE id = ? AND user_id = ?",
        (group_id, user_id)
    ).fetchone()
    conn.close()
    return group


def paystack_request(path, method="GET", payload=None):
    secret = os.environ.get("PAYSTACK_SECRET_KEY")

    if not secret:
        return None, "PAYSTACK_SECRET_KEY is not configured in Render."

    url = "https://api.paystack.co" + path

    headers = {
        "Authorization": "Bearer " + secret,
        "Content-Type": "application/json",
    }

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = str(e)
        return None, f"Paystack HTTP error: {e.code} {body}"
    except Exception as e:
        return None, f"Payment connection error: {e}"


def activate_premium(user_id, reference):
    conn = get_db()

    existing = conn.execute(
        "SELECT id FROM subscriptions WHERE reference = ?",
        (reference,)
    ).fetchone()

    if existing:
        conn.close()
        return False

    user = conn.execute(
        "SELECT premium_until FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    start = datetime.now()

    if user and user["premium_until"]:
        try:
            old_expiry = datetime.strptime(
                user["premium_until"], "%Y-%m-%d %H:%M:%S"
            )
            if old_expiry > start:
                start = old_expiry
        except ValueError:
            pass

    expiry = start + timedelta(days=30)

    conn.execute(
        """
        INSERT INTO subscriptions
        (user_id, plan, amount, reference, status, started_at, expires_at, created_at)
        VALUES (?, 'premium', ?, ?, 'paid', ?, ?, ?)
        """,
        (
            user_id,
            PREMIUM_PRICE,
            reference,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            expiry.strftime("%Y-%m-%d %H:%M:%S"),
            now()
        )
    )

    conn.execute(
        """
        UPDATE users
        SET plan = 'premium', premium_until = ?
        WHERE id = ?
        """,
        (expiry.strftime("%Y-%m-%d %H:%M:%S"), user_id)
    )

    conn.commit()
    conn.close()

    return True


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
    <meta name="description" content="AjoConnect is a simple digital Ajo and Esusu management platform for groups, contributions, members, rotations and payouts.">
    <meta name="keywords" content="Ajo, Esusu, savings group, contribution tracker, Ajo management, Nigeria, AjoConnect">
    <meta name="robots" content="index, follow">
    <meta name="theme-color" content="#075e54">
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
        .public-hero {
            display: grid; grid-template-columns: 1.35fr .65fr; gap: 30px; align-items: center;
            background: linear-gradient(135deg, #064e45, #128c7e); color: white; padding: 48px; border-radius: 24px; margin-bottom: 35px; overflow: hidden;
        }
        .hero-copy h1 { font-size: clamp(36px, 6vw, 62px); line-height: 1.05; margin: 12px 0 18px; }
        .hero-copy h1 span { color: #ffe08a; }
        .hero-text { font-size: 19px; line-height: 1.6; max-width: 650px; opacity: .95; }
        .eyebrow { font-size: 12px; letter-spacing: 2px; font-weight: 800; text-transform: uppercase; opacity: .85; }
        .eyebrow.dark { color: #075e54; }
        .hero-actions { display: flex; gap: 12px; flex-wrap: wrap; margin: 25px 0 12px; }
        .btn-large { padding: 14px 22px !important; font-size: 16px !important; }
        .trust-text { font-size: 13px; opacity: .75; }
        .hero-card { background: white; color: #1f2937; border-radius: 18px; padding: 22px; box-shadow: 0 18px 50px rgba(0,0,0,.18); }
        .mini-header { font-weight: 800; color: #075e54; margin-bottom: 18px; }
        .mini-stat { background: #f0f8f6; padding: 16px; border-radius: 12px; margin-bottom: 12px; }
        .mini-stat strong { display: block; font-size: 27px; color: #075e54; }
        .mini-stat span { color: #6b7280; font-size: 12px; }
        .mini-row { display: flex; justify-content: space-between; padding: 13px 0; border-bottom: 1px solid #edf1f0; font-size: 13px; }
        .section { padding: 25px 0 45px; }
        .section-heading { text-align: center; margin-bottom: 28px; }
        .section-heading h2, .premium-cta h2 { font-size: 32px; margin: 8px 0; color: #12332f; }
        .section-heading p, .premium-cta p { color: #66736f; }
        .feature-grid, .steps-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 18px; }
        .feature-card { background: white; border: 1px solid #e5ecea; border-radius: 16px; padding: 24px; box-shadow: 0 5px 20px rgba(0,0,0,.04); }
        .feature-icon { font-size: 30px; }
        .feature-card h3 { margin-bottom: 8px; }
        .feature-card p, .step p { color: #66736f; line-height: 1.55; }
        .steps-section { background: #075e54; color: white; padding: 45px 25px; border-radius: 22px; margin-bottom: 35px; }
        .light-heading h2 { color: white; }
        .steps-grid { max-width: 1000px; margin: auto; }
        .step { text-align: center; padding: 10px; }
        .step-number { width: 48px; height: 48px; margin: auto; border-radius: 50%; display: grid; place-items: center; background: #ffe08a; color: #075e54; font-size: 20px; font-weight: 800; }
        .step p { color: rgba(255,255,255,.78); }
        .premium-cta { display: flex; justify-content: space-between; align-items: center; gap: 20px; background: #fff8e7; border: 2px solid #f0d88b; padding: 30px; border-radius: 18px; }
        @media(max-width: 760px) { .public-hero { grid-template-columns: 1fr; padding: 30px 22px; } .premium-cta { flex-direction: column; align-items: flex-start; } }
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


# ============================================================
# PUBLIC AJOConnect WEBSITE
# ============================================================

@app.route("/")
def public_home():
    content = """
    <style>
      .site-hero {
        text-align:center;
        padding:42px 20px 34px;
        border-radius:24px;
        background:linear-gradient(135deg,#0f172a,#14532d);
        color:white;
        margin-bottom:24px;
      }
      .site-hero h1 {font-size:42px;margin:0 0 12px;}
      .site-hero p {font-size:18px;max-width:720px;margin:0 auto 22px;line-height:1.6;}
      .site-btn {
        display:inline-block;padding:13px 20px;border-radius:12px;
        text-decoration:none;font-weight:700;margin:5px;
      }
      .site-btn-primary {background:white;color:#14532d;}
      .site-btn-secondary {border:1px solid rgba(255,255,255,.5);color:white;}
      .site-feature h3 {margin-bottom:8px;}
      .site-feature p {line-height:1.6;color:#475569;}
      .site-section {margin:28px 0;}
      .site-section h2 {text-align:center;margin-bottom:18px;}
      .site-steps {counter-reset:step;}
      .site-step {position:relative;padding-left:54px;}
      .site-step:before {
        counter-increment:step;content:counter(step);
        position:absolute;left:0;top:0;width:36px;height:36px;
        border-radius:50%;display:grid;place-items:center;
        background:#14532d;color:white;font-weight:800;
      }
      .site-cta {text-align:center;padding:28px;border-radius:20px;background:#f1f5f9;}
      .site-footer {text-align:center;margin-top:28px;color:#64748b;font-size:14px;}
    </style>

    <div class="site-hero">
      <div style="font-size:46px;">💰</div>
      <h1>AjoConnect</h1>
      <p><strong>Digital Ajo & Esusu Management</strong><br>
      Manage your savings groups, members, contributions, rotation schedules and payouts in one simple platform.</p>
      <a class="site-btn site-btn-primary" href="/register">🚀 Get Started</a>
      <a class="site-btn site-btn-secondary" href="/login">🔐 Login</a>
    </div>

    <div class="site-section">
      <h2>Everything You Need to Manage Your Ajo</h2>
      <div class="grid">
        <div class="card site-feature">
          <h3>👥 Member Management</h3>
          <p>Add members, assign rotation positions and keep their details organized.</p>
        </div>
        <div class="card site-feature">
          <h3>💰 Contribution Tracking</h3>
          <p>Record contributions and keep a clear history of who has paid.</p>
        </div>
        <div class="card site-feature">
          <h3>🔄 Rotation Schedule</h3>
          <p>Follow the correct order for beneficiaries and see the expected payout.</p>
        </div>
        <div class="card site-feature">
          <h3>💸 Payout Records</h3>
          <p>Record payouts and prevent duplicate or incorrect payout entries.</p>
        </div>
        <div class="card site-feature">
          <h3>📊 Group Dashboard</h3>
          <p>See members, contributions, payouts and group progress at a glance.</p>
        </div>
        <div class="card site-feature">
          <h3>🔐 Secure Accounts</h3>
          <p>Members use individual accounts with protected login access.</p>
        </div>
      </div>
    </div>

    <div class="site-section">
      <h2>How AjoConnect Works</h2>
      <div class="card site-steps">
        <div class="site-step">
          <h3>Create an Ajo Group</h3>
          <p>Set your contribution amount and payment frequency.</p>
        </div>
        <div class="site-step">
          <h3>Add Your Members</h3>
          <p>Add members and assign their positions in the rotation.</p>
        </div>
        <div class="site-step">
          <h3>Record Contributions</h3>
          <p>Record each member's payment as it is received.</p>
        </div>
        <div class="site-step">
          <h3>Pay the Beneficiary</h3>
          <p>Follow the rotation order and record the correct payout.</p>
        </div>
      </div>
    </div>

    <div class="site-cta">
      <h2>Ready to manage your Ajo digitally?</h2>
      <p>Start organizing your Ajo group with AjoConnect.</p>
      <a class="btn" href="/register">Create Your Account</a>
      <a class="btn btn-secondary" href="/login">Login to AjoConnect</a>
    </div>

    <div class="site-footer">
      AjoConnect © 2026 — Digital Ajo & Esusu Management
    </div>
    """
    return page("AjoConnect – Digital Ajo & Esusu Management", content)


@app.route("/robots.txt")
def robots_txt():
    return """User-agent: *
Allow: /
Sitemap: /sitemap.xml
""", 200, {"Content-Type": "text/plain"}


@app.route("/sitemap.xml")
def sitemap_xml():
    base = request.url_root.rstrip("/")
    urls = ["/", "/register", "/login"]
    xml = '<?xml version="1.0" encoding="UTF-8"?>'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    for path in urls:
        xml += f"<url><loc>{base}{path}</loc></url>"
    xml += "</urlset>"
    return xml, 200, {"Content-Type": "application/xml"}

@app.route("/dashboard") if "user_id" in session:
        return redirect(url_for("dashboard"))

    content = """
    <section class="public-hero">
        <div class="hero-copy">
            <div class="eyebrow">🇳🇬 Built for Ajo & Esusu groups</div>
            <h1>Manage your Ajo with <span>AjoConnect</span></h1>
            <p class="hero-text">Keep members, contributions, rotation schedules and payouts organized in one simple digital platform.</p>
            <div class="hero-actions">
                <a class="btn btn-large" href="/register">Create Free Account</a>
                <a class="btn btn-secondary btn-large" href="/login">Login</a>
            </div>
            <p class="trust-text">Simple • Organized • Easy to track</p>
        </div>
        <div class="hero-card">
            <div class="mini-header">AjoConnect Dashboard</div>
            <div class="mini-stat"><strong>₦20,000</strong><span>Expected payout</span></div>
            <div class="mini-row"><span>👥 Members</span><b>10</b></div>
            <div class="mini-row"><span>💰 Contributions</span><b>₦100,000</b></div>
            <div class="mini-row"><span>🔄 Next payout</span><b>Position #2</b></div>
        </div>
    </section>

    <section class="section">
        <div class="section-heading">
            <div class="eyebrow dark">WHY AJOCONNECT</div>
            <h2>Everything your Ajo group needs</h2>
            <p>Stop relying on notebooks, scattered WhatsApp messages and manual calculations.</p>
        </div>
        <div class="feature-grid">
            <div class="feature-card"><div class="feature-icon">👥</div><h3>Manage Members</h3><p>Keep member names, phone numbers, positions and status organized.</p></div>
            <div class="feature-card"><div class="feature-icon">💰</div><h3>Track Contributions</h3><p>Record payments and see contribution history for every member.</p></div>
            <div class="feature-card"><div class="feature-icon">🔄</div><h3>Rotation Schedule</h3><p>Know who is due to receive the Ajo payout and when.</p></div>
            <div class="feature-card"><div class="feature-icon">💸</div><h3>Record Payouts</h3><p>Record payout amounts and dates so your group records stay clear.</p></div>
            <div class="feature-card"><div class="feature-icon">📊</div><h3>Group Dashboard</h3><p>See important group information at a glance from one dashboard.</p></div>
            <div class="feature-card"><div class="feature-icon">🔐</div><h3>Secure Accounts</h3><p>Members and administrators use their own accounts to manage records.</p></div>
        </div>
    </section>

    <section class="steps-section">
        <div class="section-heading light-heading">
            <div class="eyebrow">HOW IT WORKS</div>
            <h2>Start managing your Ajo in minutes</h2>
        </div>
        <div class="steps-grid">
            <div class="step"><div class="step-number">1</div><h3>Create your account</h3><p>Register your AjoConnect account.</p></div>
            <div class="step"><div class="step-number">2</div><h3>Create a group</h3><p>Set your contribution amount and frequency.</p></div>
            <div class="step"><div class="step-number">3</div><h3>Add members</h3><p>Enter the members and their rotation positions.</p></div>
            <div class="step"><div class="step-number">4</div><h3>Track & manage</h3><p>Record contributions and payouts as the Ajo runs.</p></div>
        </div>
    </section>

    <section class="premium-cta">
        <div><div class="eyebrow dark">AJOCONNECT PREMIUM</div><h2>Ready to take your Ajo management further?</h2><p>Upgrade when you need more groups, members and advanced features.</p></div>
        <a class="btn btn-large" href="/register">Get Started</a>
    </section>
    """
    return page("AjoConnect — Digital Ajo & Esusu Management", content)


@app.route("/robots.txt")
def robots_txt():
    base = request.host_url.rstrip("/")
    return f"User-agent: *\nAllow: /\nDisallow: /dashboard\nDisallow: /admin\nDisallow: /logout\nSitemap: {base}/sitemap.xml\n", 200, {"Content-Type": "text/plain"}


@app.route("/sitemap.xml")
def sitemap_xml():
    base = request.host_url.rstrip("/")
    urls = ["/", "/register", "/login"]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path in urls:
        xml.append(f"<url><loc>{base}{path}</loc></url>")
    xml.append('</urlset>')
    return "\n".join(xml), 200, {"Content-Type": "application/xml"}


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

    if is_premium_user(user["id"]):
        flash("Your Premium membership is already active.")
        return redirect(url_for("subscription"))

    reference = "AJOCONNECT-" + uuid.uuid4().hex.upper()

    conn = get_db()
    conn.execute(
        """
        INSERT INTO subscriptions
        (user_id, plan, amount, reference, status, created_at)
        VALUES (?, 'premium', ?, ?, 'pending', ?)
        """,
        (user["id"], PREMIUM_PRICE, reference, now())
    )
    conn.commit()
    conn.close()

    # Paystack expects NGN amount in kobo, so ₦2,000 = 200,000.
    payload = {
        "email": user["email"],
        "amount": PREMIUM_PRICE * 100,
        "currency": "NGN",
        "reference": reference,
        "callback_url": request.host_url.rstrip("/") + url_for("paystack_callback"),
        "metadata": json.dumps({
            "user_id": user["id"],
            "product": "AjoConnect Premium"
        })
    }

    result, error = paystack_request(
        "/transaction/initialize",
        method="POST",
        payload=payload
    )

    if error or not result or not result.get("status"):
        conn = get_db()
        conn.execute(
            "UPDATE subscriptions SET status = 'failed' WHERE reference = ?",
            (reference,)
        )
        conn.commit()
        conn.close()

        flash("We could not start the payment. Please try again.")
        return redirect(url_for("subscription"))

    authorization_url = result["data"]["authorization_url"]
    return redirect(authorization_url)


@app.route("/paystack/callback")
@login_required
def paystack_callback():
    reference = request.args.get("reference", "").strip()

    if not reference:
        flash("No payment reference was received.")
        return redirect(url_for("subscription"))

    user = current_user()

    conn = get_db()
    subscription = conn.execute(
        """
        SELECT *
        FROM subscriptions
        WHERE reference = ?
        AND user_id = ?
        """,
        (reference, user["id"])
    ).fetchone()
    conn.close()

    if not subscription:
        flash("Payment record not found.")
        return redirect(url_for("subscription"))

    result, error = paystack_request(
        "/transaction/verify/" + reference
    )

    if error or not result or not result.get("status"):
        flash("Payment could not be verified yet. Please try again.")
        return redirect(url_for("subscription"))

    transaction = result.get("data", {})

    paid_amount = transaction.get("amount")
    currency = transaction.get("currency")
    payment_status = transaction.get("status")

    expected_amount = PREMIUM_PRICE * 100

    if (
        payment_status == "success"
        and currency == "NGN"
        and paid_amount == expected_amount
    ):
        activate_premium(user["id"], reference)
        session["premium"] = True
        flash("Payment successful! Your Premium account is now active for 30 days.")
        return redirect(url_for("subscription"))

    flash("Payment was not completed successfully.")
    return redirect(url_for("subscription"))


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
            flash("Contribution amount must be greater than ₦0.00.")
            return redirect(url_for("create_group"))

        if frequency not in ("weekly", "biweekly", "monthly"):
            conn.close()
            flash("Invalid contribution frequency.")
            return redirect(url_for("create_group"))

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

    expected_payout = group["contribution"] * sum(1 for m in members if m["status"] == "active")

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

        if email and "@" not in email:
            conn.close()
            flash("Please enter a valid email address or leave it blank.")
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
        except (ValueError, TypeError):
            conn.close()
            flash("Invalid contribution amount.")
            return redirect(url_for(
                "record_contribution",
                group_id=group_id,
                member_id=member_id
            ))

        expected_contribution = float(group["contribution"] or 0)
        if amount <= 0:
            conn.close()
            flash("Contribution amount must be greater than ₦0.00.")
            return redirect(url_for(
                "record_contribution",
                group_id=group_id,
                member_id=member_id
            ))

        if abs(amount - expected_contribution) > 0.01:
            conn.close()
            flash(f"Contribution must be exactly {money(expected_contribution)} for this group.")
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

    current_member_id = conn.execute(
        """
        SELECT m.id
        FROM members m
        WHERE m.group_id = ? AND m.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM payouts p
              WHERE p.group_id = m.group_id
                AND p.member_id = m.id
                AND p.status = 'paid'
          )
        ORDER BY m.position ASC
        LIMIT 1
        """,
        (group_id,)
    ).fetchone()

    if request.method == "POST":
        if not current_member_id or current_member_id["id"] != member_id:
            conn.close()
            flash("This member is not the current payout beneficiary. Follow the rotation order.")
            return redirect(url_for("group_detail", group_id=group_id))
        amount = request.form.get("amount", str(expected_payout))
        payout_date = request.form.get(
            "payout_date", datetime.now().strftime("%Y-%m-%d")
        )
        note = request.form.get("note", "").strip()

        try:
            amount = float(amount)
        except (ValueError, TypeError):
            conn.close()
            flash("Invalid payout amount.")
            return redirect(url_for(
                "record_payout",
                group_id=group_id,
                member_id=member_id
            ))

        if amount <= 0:
            conn.close()
            flash("Payout amount must be greater than ₦0.00.")
            return redirect(url_for(
                "record_payout",
                group_id=group_id,
                member_id=member_id
            ))

        if abs(amount - float(expected_payout)) > 0.01:
            conn.close()
            flash(f"Payout must be exactly {money(expected_payout)} for this group.")
            return redirect(url_for(
                "record_payout",
                group_id=group_id,
                member_id=member_id
            ))

        existing_payout = conn.execute(
            """
            SELECT id
            FROM payouts
            WHERE group_id = ? AND member_id = ? AND status = 'paid'
            LIMIT 1
            """,
            (group_id, member_id)
        ).fetchone()

        if existing_payout:
            conn.close()
            flash(f"{member['name']} has already received a payout for this Ajo round.")
            return redirect(url_for("group_detail", group_id=group_id))

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
