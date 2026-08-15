from flask import Flask, request, redirect, url_for, session, render_template_string, flash
import sqlite3
import os
from functools import wraps
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "ajoconnect-development-secret-change-this"
)

DATABASE = os.environ.get("DATABASE_PATH", "ajoconnect.db")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    """)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))

        if session.get("role") != "admin":
            return redirect(url_for("dashboard"))

        return view(*args, **kwargs)

    return wrapped


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def money(value):
    return f"₦{float(value or 0):,.2f}"


app.jinja_env.filters["money"] = money


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
        * {
            box-sizing: border-box;
        }

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

        .brand {
            font-size: 22px;
            font-weight: bold;
        }

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

        .hero h1 {
            margin-top: 0;
        }

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

        .btn:hover {
            opacity: .9;
        }

        .btn-danger {
            background: #c62828;
        }

        .btn-warning {
            background: #e09f00;
        }

        .btn-secondary {
            background: #555;
        }

        input, select, textarea {
            width: 100%;
            padding: 12px;
            margin-top: 6px;
            margin-bottom: 15px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 15px;
        }

        label {
            font-weight: bold;
        }

        table {
            width: 100%;
            border-collapse: collapse;
        }

        th, td {
            padding: 12px;
            border-bottom: 1px solid #eee;
            text-align: left;
        }

        th {
            background: #f0f4f3;
        }

        .table-wrap {
            overflow-x: auto;
        }

        .badge {
            display: inline-block;
            padding: 5px 9px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: bold;
        }

        .paid {
            background: #d7f5df;
            color: #176b35;
        }

        .pending {
            background: #fff0c2;
            color: #7a5700;
        }

        .danger {
            background: #ffdede;
            color: #9b111e;
        }

        .success {
            background: #d7f5df;
            color: #176b35;
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

        footer {
            text-align: center;
            padding: 30px;
            color: #777;
        }

        @media(max-width: 600px) {
            nav {
                display: block;
            }

            nav a {
                display: inline-block;
                margin: 8px 8px 0 0;
            }

            th, td {
                font-size: 13px;
            }
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

<footer>
    AjoConnect © 2026 — Digital Ajo & Esusu Management
</footer>

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
        <p>
            Manage your Ajo / Esusu savings group,
            contributions and rotation schedule in one place.
        </p>

        <a class="btn" href="/register">Create Account</a>
        <a class="btn btn-secondary" href="/login">Login</a>
    </div>

    <div class="grid">

        <div class="card">
            <h3>👥 Manage Members</h3>
            <p>Add members and organize their rotation positions.</p>
        </div>

        <div class="card">
            <h3>💵 Track Contributions</h3>
            <p>Record payments and see who has paid.</p>
        </div>

        <div class="card">
            <h3>🔄 Rotation</h3>
            <p>Know who receives the Ajo payout next.</p>
        </div>

        <div class="card">
            <h3>📊 Dashboard</h3>
            <p>See your group's financial activity.</p>
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
            (name, email, password, role, created_at)
            VALUES (?, ?, ?, 'admin', ?)
            """,
            (
                name,
                email,
                generate_password_hash(password),
                now()
            )
        )

        conn.commit()
        conn.close()

        flash("Account created successfully. Please login.")
        return redirect(url_for("login"))

    content = """
    <div class="card">
        <h2>📝 Create AjoConnect Account</h2>

        <form method="POST">

            <label>Full Name</label>
            <input type="text" name="name" required>

            <label>Email</label>
            <input type="email" name="email" required>

            <label>Password</label>
            <input type="password" name="password" required>

            <button class="btn" type="submit">
                Create Account
            </button>

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

            <button class="btn" type="submit">
                Login
            </button>

        </form>
    </div>
    """

    return page("Login", content)


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    flash("You have been logged out.")

    return redirect(url_for("login"))


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():

    conn = get_db()

    groups = conn.execute(
        "SELECT * FROM groups ORDER BY id DESC"
    ).fetchall()

    total_members = conn.execute(
        "SELECT COUNT(*) AS total FROM members"
    ).fetchone()["total"]

    total_contributions = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS total
        FROM contributions
        WHERE status = 'paid'
        """
    ).fetchone()["total"]

    total_payouts = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS total
        FROM payouts
        WHERE status = 'paid'
        """
    ).fetchone()["total"]

    conn.close()

    content = f"""
    <div class="hero">
        <h1>👋 Welcome, {session.get("name")}</h1>
        <p>Manage your Ajo groups and savings activities.</p>
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
            <a class="btn" href="/groups/new">
                ➕ Create Ajo Group
            </a>

            <a class="btn btn-secondary" href="/groups">
                📋 View Groups
            </a>
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
                <td>
                    <a class="btn"
                       href="/group/{group["id"]}">
                       Open
                    </a>
                </td>
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

    conn.close()

    content = f"""
    <div class="hero">
        <h1>⚙️ Admin Dashboard</h1>
        <p>Overview of your AjoConnect platform.</p>
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
            <h3>Contributions</h3>
            <div class="stat">{money(contributions)}</div>
        </div>

        <div class="card">
            <h3>Payouts</h3>
            <div class="stat">{money(payouts)}</div>
        </div>

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
        "SELECT * FROM groups ORDER BY id DESC"
    ).fetchall()

    conn.close()

    content = """
    <div class="card">
        <div class="actions">
            <h2 style="margin-right:auto;">📋 Ajo Groups</h2>

            <a class="btn" href="/groups/new">
                ➕ New Group
            </a>
        </div>
    </div>

    <div class="grid">
    """

    for group in all_groups:

        content += f"""
        <div class="card">

            <h2>{group["name"]}</h2>

            <p>
                Contribution:
                <strong>{money(group["contribution"])}</strong>
            </p>

            <p>
                Frequency:
                <strong>{group["frequency"].title()}</strong>
            </p>

            <a class="btn" href="/group/{group["id"]}">
                Open Group
            </a>

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

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        contribution = request.form.get("contribution", "0")
        frequency = request.form.get("frequency", "monthly")

        try:
            contribution = float(contribution)
        except ValueError:
            flash("Contribution must be a valid amount.")
            return redirect(url_for("create_group"))

        if not name:
            flash("Group name is required.")
            return redirect(url_for("create_group"))

        conn = get_db()

        conn.execute(
            """
            INSERT INTO groups
            (name, contribution, frequency, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                name,
                contribution,
                frequency,
                now()
            )
        )

        conn.commit()

        group_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        conn.close()

        flash("Ajo group created successfully.")

        return redirect(url_for("group_detail", group_id=group_id))

    content = """
    <div class="card">

        <h2>➕ Create Ajo Group</h2>

        <form method="POST">

            <label>Group Name</label>
            <input
                type="text"
                name="name"
                placeholder="e.g. Mama Raji"
                required
            >

            <label>Contribution Amount</label>
            <input
                type="number"
                name="contribution"
                min="0"
                step="0.01"
                placeholder="10000"
                required
            >

            <label>Contribution Frequency</label>

            <select name="frequency">
                <option value="weekly">Weekly</option>
                <option value="biweekly">Bi-weekly</option>
                <option value="monthly" selected>Monthly</option>
            </select>

            <button class="btn" type="submit">
                Create Group
            </button>

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

    conn = get_db()

    group = conn.execute(
        "SELECT * FROM groups WHERE id = ?",
        (group_id,)
    ).fetchone()

    if not group:
        conn.close()
        return "Group not found", 404

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
        WHERE group_id = ?
        AND status = 'paid'
        """,
        (group_id,)
    ).fetchone()["total"]

    payout_total = conn.execute(
        """
        SELECT COALESCE(SUM(amount),0) AS total
        FROM payouts
        WHERE group_id = ?
        AND status = 'paid'
        """,
        (group_id,)
    ).fetchone()["total"]

    current_member = None

    for member in members:

        payout = conn.execute(
            """
            SELECT id
            FROM payouts
            WHERE group_id = ?
            AND member_id = ?
            AND status = 'paid'
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

        <p>
            Contribution:
            <strong>{money(group["contribution"])}</strong>
        </p>

        <p>
            Members:
            <strong>{len(members)}</strong>
        </p>

        <p>
            Expected payout each round:
            <strong>{money(expected_payout)}</strong>
        </p>

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
            <div class="stat">
                {current_member["name"] if current_member else "Completed"}
            </div>
        </div>

    </div>

    <div class="card">

        <div class="actions">

            <a class="btn"
               href="/group/{group_id}/member/add">
               ➕ Add Member
            </a>

            <a class="btn btn-secondary"
               href="/group/{group_id}/contributions">
               💰 Contributions
            </a>

            <a class="btn btn-secondary"
               href="/group/{group_id}/payouts">
               💸 Payouts
            </a>

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
        <tr>

            <td>{member["position"]}</td>

            <td>
                <strong>{member["name"]}</strong>
            </td>

            <td>{member["phone"] or "-"}</td>

            <td>
                <span class="badge success">
                    {member["status"].title()}
                </span>
            </td>

            <td>{money(expected_payout)}</td>

            <td>

                <div class="actions">

                    <a class="btn"
                       href="/group/{group_id}/member/{member["id"]}/contribute">
                       Payment
                    </a>

                    <a class="btn btn-warning"
                       href="/group/{group_id}/member/{member["id"]}/payout">
                       Payout
                    </a>

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
# ADD MEMBER
# ============================================================

@app.route("/group/<int:group_id>/member/add", methods=["GET", "POST"])
@login_required
def add_member(group_id):

    conn = get_db()

    group = conn.execute(
        "SELECT * FROM groups WHERE id = ?",
        (group_id,)
    ).fetchone()

    if not group:
        conn.close()
        return "Group not found", 404

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
            (
                group_id,
                name,
                phone,
                email,
                next_position,
                now()
            )
        )

        conn.commit()
        conn.close()

        flash(f"{name} has been added to the Ajo group.")

        return redirect(
            url_for("group_detail", group_id=group_id)
        )

    conn.close()

    content = f"""
    <div class="card">

        <h2>➕ Add Member</h2>

        <p>
            Group:
            <strong>{group["name"]}</strong>
        </p>

        <form method="POST">

            <label>Member Name</label>
            <input
                type="text"
                name="name"
                placeholder="Full name"
                required
            >

            <label>Phone Number</label>
            <input
                type="text"
                name="phone"
                placeholder="080..."
            >

            <label>Email</label>
            <input
                type="email"
                name="email"
                placeholder="member@email.com"
            >

            <button class="btn" type="submit">
                Add Member
            </button>

        </form>

    </div>
    """

    return page("Add Member", content)


# ============================================================
# RECORD CONTRIBUTION
# ============================================================

@app.route(
    "/group/<int:group_id>/member/<int:member_id>/contribute",
    methods=["GET", "POST"]
)
@login_required
def record_contribution(group_id, member_id):

    conn = get_db()

    group = conn.execute(
        "SELECT * FROM groups WHERE id = ?",
        (group_id,)
    ).fetchone()

    member = conn.execute(
        """
        SELECT *
        FROM members
        WHERE id = ?
        AND group_id = ?
        """,
        (member_id, group_id)
    ).fetchone()

    if not group or not member:
        conn.close()
        return "Member or group not found", 404

    if request.method == "POST":

        amount = request.form.get(
            "amount",
            str(group["contribution"])
        )

        payment_date = request.form.get(
            "payment_date",
            datetime.now().strftime("%Y-%m-%d")
        )

        note = request.form.get("note", "").strip()

        try:
            amount = float(amount)
        except ValueError:
            conn.close()
            flash("Invalid contribution amount.")
            return redirect(
                url_for(
                    "record_contribution",
                    group_id=group_id,
                    member_id=member_id
                )
            )

        conn.execute(
            """
            INSERT INTO contributions
            (group_id, member_id, amount, payment_date, status, note)
            VALUES (?, ?, ?, ?, 'paid', ?)
            """,
            (
                group_id,
                member_id,
                amount,
                payment_date,
                note
            )
        )

        conn.commit()
        conn.close()

        flash(
            f"Payment of {money(amount)} recorded for {member['name']}."
        )

        return redirect(
            url_for(
                "group_detail",
                group_id=group_id
            )
        )

    conn.close()

    content = f"""
    <div class="card">

        <h2>💰 Record Contribution</h2>

        <p>
            Member:
            <strong>{member["name"]}</strong>
        </p>

        <p>
            Expected contribution:
            <strong>{money(group["contribution"])}</strong>
        </p>

        <form method="POST">

            <label>Amount Paid</label>
            <input
                type="number"
                name="amount"
                value="{group["contribution"]}"
                min="0"
                step="0.01"
                required
            >

            <label>Payment Date</label>
            <input
                type="date"
                name="payment_date"
                value="{datetime.now().strftime("%Y-%m-%d")}"
                required
            >

            <label>Note</label>
            <textarea
                name="note"
                placeholder="Optional note"
            ></textarea>

            <button class="btn" type="submit">
                Record Payment
            </button>

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

    conn = get_db()

    group = conn.execute(
        "SELECT * FROM groups WHERE id = ?",
        (group_id,)
    ).fetchone()

    records = conn.execute(
        """
        SELECT
            contributions.*,
            members.name AS member_name
        FROM contributions
        JOIN members
            ON members.id = contributions.member_id
        WHERE contributions.group_id = ?
        ORDER BY contributions.payment_date DESC,
                 contributions.id DESC
        """,
        (group_id,)
    ).fetchall()

    conn.close()

    content = f"""
    <div class="card">

        <h2>💰 Contribution History</h2>

        <p>
            Group:
            <strong>{group["name"]}</strong>
        </p>

        <div class="table-wrap">

        <table>

            <tr>
                <th>Member</th>
                <th>Amount</th>
                <th>Date</th>
                <th>Status</th>
                <th>Note</th>
            </tr>
    """

    for row in records:

        content += f"""
        <tr>

            <td>{row["member_name"]}</td>

            <td>
                <strong>{money(row["amount"])}</strong>
            </td>

            <td>{row["payment_date"]}</td>

            <td>
                <span class="badge paid">
                    {row["status"].title()}
                </span>
            </td>

            <td>{row["note"] or "-"}</td>

        </tr>
        """

    content += """
        </table>

        </div>

    </div>
    """

    return page("Contributions", content)


# ============================================================
# RECORD PAYOUT
# ============================================================

@app.route(
    "/group/<int:group_id>/member/<int:member_id>/payout",
    methods=["GET", "POST"]
)
@login_required
def record_payout(group_id, member_id):

    conn = get_db()

    group = conn.execute(
        "SELECT * FROM groups WHERE id = ?",
        (group_id,)
    ).fetchone()

    member = conn.execute(
        """
        SELECT *
        FROM members
        WHERE id = ?
        AND group_id = ?
        """,
        (member_id, group_id)
    ).fetchone()

    if not group or not member:
        conn.close()
        return "Member or group not found", 404

    member_count = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM members
        WHERE group_id = ?
        AND status = 'active'
        """,
        (group_id,)
    ).fetchone()["total"]

    expected_payout = group["contribution"] * member_count

    if request.method == "POST":

        amount = request.form.get(
            "amount",
            str(expected_payout)
        )

        payout_date = request.form.get(
            "payout_date",
            datetime.now().strftime("%Y-%m-%d")
        )

        note = request.form.get("note", "").strip()

        try:
            amount = float(amount)
        except ValueError:
            conn.close()
            flash("Invalid payout amount.")
            return redirect(
                url_for(
                    "record_payout",
                    group_id=group_id,
                    member_id=member_id
                )
            )

        conn.execute(
            """
            INSERT INTO payouts
            (group_id, member_id, amount, payout_date, status, note)
            VALUES (?, ?, ?, ?, 'paid', ?)
            """,
            (
                group_id,
                member_id,
                amount,
                payout_date,
                note
            )
        )

        conn.commit()
        conn.close()

        flash(
            f"Payout of {money(amount)} recorded for {member['name']}."
        )

        return redirect(
            url_for(
                "group_detail",
                group_id=group_id
            )
        )

    conn.close()

    content = f"""
    <div class="card">

        <h2>💸 Record Payout</h2>

        <p>
            Beneficiary:
            <strong>{member["name"]}</strong>
        </p>

        <p>
            Expected payout:
            <strong>{money(expected_payout)}</strong>
        </p>

        <form method="POST">

            <label>Payout Amount</label>
            <input
                type="number"
                name="amount"
                value="{expected_payout}"
                min="0"
                step="0.01"
                required
            >

            <label>Payout Date</label>
            <input
                type="date"
                name="payout_date"
                value="{datetime.now().strftime("%Y-%m-%d")}"
                required
            >

            <label>Note</label>
            <textarea
                name="note"
                placeholder="Optional payout note"
            ></textarea>

            <button class="btn btn-warning" type="submit">
                Confirm Payout
            </button>

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

    conn = get_db()

    group = conn.execute(
        "SELECT * FROM groups WHERE id = ?",
        (group_id,)
    ).fetchone()

    records = conn.execute(
        """
        SELECT
            payouts.*,
            members.name AS member_name
        FROM payouts
        JOIN members
            ON members.id = payouts.member_id
        WHERE payouts.group_id = ?
        ORDER BY payouts.id DESC
        """,
        (group_id,)
    ).fetchall()

    conn.close()

    content = f"""
    <div class="card">

        <h2>💸 Payout History</h2>

        <p>
            Group:
            <strong>{group["name"]}</strong>
        </p>

        <div class="table-wrap">

        <table>

            <tr>
                <th>Beneficiary</th>
                <th>Amount</th>
                <th>Date</th>
                <th>Status</th>
                <th>Note</th>
            </tr>
    """

    for row in records:

        content += f"""
        <tr>

            <td>{row["member_name"]}</td>

            <td>
                <strong>{money(row["amount"])}</strong>
            </td>

            <td>{row["payout_date"] or "-"}</td>

            <td>
                <span class="badge success">
                    {row["status"].title()}
                </span>
            </td>

            <td>{row["note"] or "-"}</td>

        </tr>
        """

    content += """
        </table>

        </div>

    </div>
    """

    return page("Payouts", content)


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():
    return {
        "status": "ok",
        "app": "AjoConnect"
    }


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
