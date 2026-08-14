from flask import Flask, request, redirect, url_for, session, render_template_string
import sqlite3
import os
from functools import wraps
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key-in-render"
)

DATABASE = "ajoconnect.db"


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    # USERS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # GROUPS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contribution REAL NOT NULL,
            frequency TEXT NOT NULL,
            creator_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # MEMBERS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            joined_at TEXT NOT NULL
        )
    """)

    # CONTRIBUTIONS
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            member_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# LOGIN PROTECTION
# ============================================================

def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return wrapper


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def user_is_group_member(conn, group_id, user_id):
    member = conn.execute("""
        SELECT id
        FROM members
        WHERE group_id=? AND user_id=?
    """, (group_id, user_id)).fetchone()

    return member is not None


def user_is_group_owner(conn, group_id, user_id):
    group = conn.execute("""
        SELECT id
        FROM groups
        WHERE id=? AND creator_id=?
    """, (group_id, user_id)).fetchone()

    return group is not None


def get_rotation_schedule(group, members):
    """
    Creates an automatic rotation schedule.

    Position 1 receives first.
    Position 2 receives second.
    Position 3 receives third, etc.

    The amount collected each round is:

        contribution amount × number of members
    """

    schedule = []

    if not members:
        return schedule

    try:
        start_date = datetime.fromisoformat(
            group["created_at"]
        )
    except Exception:
        start_date = datetime.now()

    total_members = len(members)

    payout_amount = group["contribution"] * total_members

    for index, member in enumerate(members):

        if group["frequency"].lower() == "monthly":
            due_date = start_date + timedelta(days=30 * index)
        else:
            due_date = start_date + timedelta(days=7 * index)

        schedule.append({
            "round": index + 1,
            "member_name": member["name"],
            "phone": member["phone"],
            "position": member["position"],
            "date": due_date.strftime("%Y-%m-%d"),
            "amount": payout_amount
        })

    return schedule


# ============================================================
# HTML STYLE
# ============================================================

STYLE = """
<style>

* {
    box-sizing: border-box;
}

body {
    font-family: Arial, sans-serif;
    background: #f4f7f6;
    margin: 0;
    color: #222;
}

nav {
    background: #087f5b;
    padding: 15px;
    color: white;
}

nav a {
    color: white;
    text-decoration: none;
    margin-right: 18px;
    font-weight: bold;
}

.container {
    max-width: 950px;
    margin: 30px auto;
    padding: 20px;
}

.card {
    background: white;
    padding: 25px;
    margin-bottom: 20px;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,.08);
}

.hero {
    text-align: center;
    padding: 60px 20px;
}

.hero h1 {
    color: #087f5b;
    font-size: 45px;
}

input,
select {
    width: 100%;
    padding: 12px;
    margin: 8px 0 15px;
    border: 1px solid #ccc;
    border-radius: 7px;
}

button,
.btn {
    background: #087f5b;
    color: white;
    border: none;
    padding: 12px 18px;
    border-radius: 7px;
    cursor: pointer;
    text-decoration: none;
    display: inline-block;
}

button:hover,
.btn:hover {
    background: #056044;
}

.btn-secondary {
    background: #495057;
}

.btn-danger {
    background: #c92a2a;
}

.stat {
    display: inline-block;
    width: 30%;
    margin: 1%;
    background: #e9f7f2;
    padding: 20px;
    border-radius: 10px;
    text-align: center;
}

.error {
    background: #ffe3e3;
    padding: 12px;
    border-radius: 7px;
    color: #a00;
    margin-bottom: 15px;
}

.success {
    background: #d3f9d8;
    padding: 12px;
    border-radius: 7px;
    color: #176b24;
    margin-bottom: 15px;
}

.info {
    background: #e7f5ff;
    padding: 12px;
    border-radius: 7px;
    color: #1864ab;
    margin-bottom: 15px;
}

.badge {
    display: inline-block;
    background: #087f5b;
    color: white;
    padding: 5px 9px;
    border-radius: 20px;
    font-size: 12px;
}

table {
    width: 100%;
    border-collapse: collapse;
}

th,
td {
    padding: 10px;
    border-bottom: 1px solid #ddd;
    text-align: left;
}

.small {
    color: #666;
    font-size: 14px;
}

@media(max-width:600px) {

    .container {
        margin: 10px auto;
        padding: 12px;
    }

    .stat {
        width: 90%;
        margin: 5px;
    }

    table {
        font-size: 13px;
    }

    th,
    td {
        padding: 7px;
    }

    .hero h1 {
        font-size: 35px;
    }

    nav a {
        display: inline-block;
        margin-bottom: 8px;
    }
}

</style>
"""


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    if "user_id" in session:
        return redirect(url_for("dashboard"))

    return render_template_string(STYLE + """

    <div class="hero">

        <h1>💰 AjoConnect</h1>

        <h2>Digital Ajo & Esusu Savings</h2>

        <p>
            Create savings groups, manage members,
            track contributions and organize your
            automatic rotation schedule.
        </p>

        <br>

        <a class="btn" href="/register">
            Create Account
        </a>

        <a class="btn" href="/login">
            Login
        </a>

    </div>

    """)


# ============================================================
# REGISTER
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    error = ""

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        if not name or not phone or not password:

            error = "Please complete all fields."

        elif len(password) < 6:

            error = "Password must be at least 6 characters."

        else:

            conn = get_db()

            try:

                hashed_password = generate_password_hash(password)

                conn.execute("""
                    INSERT INTO users
                    (name, phone, password, created_at)
                    VALUES (?, ?, ?, ?)
                """, (
                    name,
                    phone,
                    hashed_password,
                    datetime.now().isoformat()
                ))

                conn.commit()

                user = conn.execute("""
                    SELECT id
                    FROM users
                    WHERE phone=?
                """, (phone,)).fetchone()

                conn.close()

                session["user_id"] = user["id"]
                session["user_name"] = name

                return redirect(url_for("dashboard"))

            except sqlite3.IntegrityError:

                conn.close()

                error = "This phone number is already registered."

    return render_template_string(STYLE + """

    <div class="container">

        <div class="card">

            <h2>👤 Create AjoConnect Account</h2>

            {% if error %}
                <div class="error">{{ error }}</div>
            {% endif %}

            <form method="POST">

                <label>Full Name</label>

                <input
                    name="name"
                    placeholder="Your full name"
                    required
                >

                <label>Phone Number</label>

                <input
                    name="phone"
                    placeholder="08012345678"
                    required
                >

                <label>Password</label>

                <input
                    type="password"
                    name="password"
                    minlength="6"
                    required
                >

                <button type="submit">
                    Create Account
                </button>

            </form>

            <p>
                Already have an account?
                <a href="/login">Login</a>
            </p>

        </div>

    </div>

    """, error=error)


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    error = ""

    if request.method == "POST":

        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        conn = get_db()

        user = conn.execute("""
            SELECT *
            FROM users
            WHERE phone=?
        """, (phone,)).fetchone()

        conn.close()

        if user:

            password_valid = False

            try:
                password_valid = check_password_hash(
                    user["password"],
                    password
                )
            except Exception:
                password_valid = False

            # Temporary compatibility with accounts
            # created by the old version.
            if not password_valid and user["password"] == password:

                password_valid = True

                conn = get_db()

                new_hash = generate_password_hash(password)

                conn.execute("""
                    UPDATE users
                    SET password=?
                    WHERE id=?
                """, (new_hash, user["id"]))

                conn.commit()
                conn.close()

            if password_valid:

                session["user_id"] = user["id"]
                session["user_name"] = user["name"]

                return redirect(url_for("dashboard"))

        error = "Invalid phone number or password."

    return render_template_string(STYLE + """

    <div class="container">

        <div class="card">

            <h2>🔐 Login</h2>

            {% if error %}
                <div class="error">{{ error }}</div>
            {% endif %}

            <form method="POST">

                <label>Phone Number</label>

                <input
                    name="phone"
                    required
                >

                <label>Password</label>

                <input
                    type="password"
                    name="password"
                    required
                >

                <button type="submit">
                    Login
                </button>

            </form>

            <p>
                Don't have an account?
                <a href="/register">Register</a>
            </p>

        </div>

    </div>

    """, error=error)


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# ============================================================
# MAIN DASHBOARD
# ============================================================

@app.route("/dashboard")
@login_required
def dashboard():

    user_id = session["user_id"]

    conn = get_db()

    # Groups created by the user
    created_groups = conn.execute("""
        SELECT *
        FROM groups
        WHERE creator_id=?
        ORDER BY id DESC
    """, (user_id,)).fetchall()

    # Groups where user is a member
    joined_groups = conn.execute("""
        SELECT
            groups.*
        FROM groups
        JOIN members
            ON members.group_id = groups.id
        WHERE members.user_id=?
        ORDER BY groups.id DESC
    """, (user_id,)).fetchall()

    member_count = conn.execute("""
        SELECT COUNT(*) AS count
        FROM members
        WHERE user_id=?
    """, (user_id,)).fetchone()["count"]

    conn.close()

    # Remove duplicate groups
    all_groups = {}

    for group in created_groups:
        all_groups[group["id"]] = group

    for group in joined_groups:
        all_groups[group["id"]] = group

    groups = list(all_groups.values())

    return render_template_string(STYLE + """

    <nav>

        <a href="/dashboard">🏠 AjoConnect</a>

        <a href="/create-group">
            ➕ Create Group
        </a>

        <a href="/logout">
            Logout
        </a>

    </nav>

    <div class="container">

        <h1>
            Welcome, {{ name }} 👋
        </h1>

        <div class="stat">
            <h2>{{ groups|length }}</h2>
            <p>Your Groups</p>
        </div>

        <div class="stat">
            <h2>{{ member_count }}</h2>
            <p>Your Memberships</p>
        </div>

        <br><br>

        <div class="card">

            <h2>👥 Your Ajo Groups</h2>

            {% if groups %}

                {% for group in groups %}

                    <div class="card">

                        <h3>
                            {{ group["name"] }}
                        </h3>

                        <p>
                            Contribution:
                            <strong>
                            ₦{{ "{:,.2f}".format(
                                group["contribution"]
                            ) }}
                            </strong>
                        </p>

                        <p>
                            Frequency:
                            {{ group["frequency"] }}
                        </p>

                        <a
                            class="btn"
                            href="/group/{{ group['id'] }}"
                        >
                            Open Group
                        </a>

                        <a
                            class="btn btn-secondary"
                            href="/group/{{ group['id'] }}/rotation"
                        >
                            🔄 Rotation
                        </a>

                    </div>

                {% endfor %}

            {% else %}

                <p>
                    You haven't joined an Ajo group yet.
                </p>

                <a
                    class="btn"
                    href="/create-group"
                >
                    Create Your First Group
                </a>

            {% endif %}

        </div>

    </div>

    """,
    name=session["user_name"],
    groups=groups,
    member_count=member_count)


# ============================================================
# CREATE GROUP
# ============================================================

@app.route("/create-group", methods=["GET", "POST"])
@login_required
def create_group():

    error = ""

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        contribution_text = request.form.get(
            "contribution",
            ""
        ).strip()

        frequency = request.form.get(
            "frequency",
            "Weekly"
        )

        if not name or not contribution_text:

            error = "Please complete all fields."

        else:

            try:
                contribution = float(contribution_text)

                if contribution <= 0:
                    raise ValueError

            except ValueError:

                error = "Enter a valid contribution amount."

            if not error:

                conn = get_db()

                cursor = conn.execute("""
                    INSERT INTO groups
                    (
                        name,
                        contribution,
                        frequency,
                        creator_id,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    name,
                    contribution,
                    frequency,
                    session["user_id"],
                    datetime.now().isoformat()
                ))

                group_id = cursor.lastrowid

                # Creator automatically becomes position 1
                conn.execute("""
                    INSERT INTO members
                    (
                        group_id,
                        user_id,
                        position,
                        joined_at
                    )
                    VALUES (?, ?, ?, ?)
                """, (
                    group_id,
                    session["user_id"],
                    1,
                    datetime.now().isoformat()
                ))

                conn.commit()
                conn.close()

                return redirect(
                    url_for(
                        "group",
                        group_id=group_id
                    )
                )

    return render_template_string(STYLE + """

    <nav>

        <a href="/dashboard">
            Dashboard
        </a>

        <a href="/logout">
            Logout
        </a>

    </nav>

    <div class="container">

        <div class="card">

            <h2>➕ Create Ajo Group</h2>

            {% if error %}
                <div class="error">
                    {{ error }}
                </div>
            {% endif %}

            <form method="POST">

                <label>Group Name</label>

                <input
                    name="name"
                    placeholder="e.g. Family Ajo"
                    required
                >

                <label>
                    Contribution Amount (₦)
                </label>

                <input
                    type="number"
                    name="contribution"
                    min="1"
                    step="0.01"
                    placeholder="10000"
                    required
                >

                <label>
                    Contribution Frequency
                </label>

                <select name="frequency">

                    <option value="Weekly">
                        Weekly
                    </option>

                    <option value="Monthly">
                        Monthly
                    </option>

                </select>

                <button type="submit">
                    Create Ajo Group
                </button>

            </form>

        </div>

    </div>

    """, error=error)


# ============================================================
# GROUP DASHBOARD
# ============================================================

@app.route("/group/<int:group_id>")
@login_required
def group(group_id):

    user_id = session["user_id"]

    conn = get_db()

    group_data = conn.execute("""
        SELECT *
        FROM groups
        WHERE id=?
    """, (group_id,)).fetchone()

    if not group_data:

        conn.close()

        return "Group not found", 404

    # Only group members or owner can see group
    allowed = user_is_group_member(
        conn,
        group_id,
        user_id
    )

    if not allowed and group_data["creator_id"] != user_id:

        conn.close()

        return "You are not a member of this group.", 403

    members = conn.execute("""
        SELECT
            members.id AS member_id,
            members.position,
            users.name,
            users.phone
        FROM members
        JOIN users
            ON users.id = members.user_id
        WHERE members.group_id=?
        ORDER BY members.position
    """, (group_id,)).fetchall()

    contributions = conn.execute("""
        SELECT
            contributions.amount,
            contributions.date,
            contributions.status,
            users.name
        FROM contributions
        JOIN members
            ON members.id = contributions.member_id
        JOIN users
            ON users.id = members.user_id
        WHERE contributions.group_id=?
        ORDER BY contributions.id DESC
    """, (group_id,)).fetchall()

    is_owner = (
        group_data["creator_id"] == user_id
    )

    conn.close()

    return render_template_string(STYLE + """

    <nav>

        <a href="/dashboard">
            Dashboard
        </a>

        <a href="/group/{{ group['id'] }}/rotation">
            🔄 Rotation
        </a>

        <a href="/logout">
            Logout
        </a>

    </nav>

    <div class="container">

        <div class="card">

            <h1>
                👥 {{ group["name"] }}
            </h1>

            <p>
                Contribution:
                <strong>
                    ₦{{ "{:,.2f}".format(
                        group["contribution"]
                    ) }}
                </strong>
            </p>

            <p>
                Frequency:
                <strong>
                    {{ group["frequency"] }}
                </strong>
            </p>

            {% if is_owner %}
                <span class="badge">
                    GROUP ADMIN
                </span>
            {% else %}
                <span class="badge">
                    MEMBER
                </span>
            {% endif %}

        </div>


        {% if is_owner %}

        <div class="card">

            <h2>➕ Add Member</h2>

            <div class="info">

                The person must already have an
                AjoConnect account.

                Enter the phone number they used
                during registration.

            </div>

            <form method="POST"
                  action="/group/{{ group['id'] }}/add-member">

                <label>
                    Member Phone Number
                </label>

                <input
                    type="tel"
                    name="phone"
                    placeholder="08012345678"
                    required
                >

                <button type="submit">
                    Add Member
                </button>

            </form>

        </div>

        {% endif %}


        <div class="card">

            <h2>👤 Members</h2>

            <table>

                <tr>
                    <th>Position</th>
                    <th>Name</th>
                    <th>Phone</th>
                </tr>

                {% for member in members %}

                <tr>

                    <td>
                        {{ member["position"] }}
                    </td>

                    <td>
                        {{ member["name"] }}
                    </td>

                    <td>
                        {{ member["phone"] }}
                    </td>

                </tr>

                {% endfor %}

            </table>

        </div>


        <div class="card">

            <h2>🔄 Rotation</h2>

            <p>
                The rotation order is based on member position.
            </p>

            <a
                class="btn"
                href="/group/{{ group['id'] }}/rotation"
            >
                View Rotation Schedule
            </a>

        </div>


        {% if is_owner %}

        <div class="card">

            <h2>💰 Record Contribution</h2>

            <form method="POST"
                  action="/group/{{ group['id'] }}/contribution">

                <label>Member</label>

                <select name="member_id">

                    {% for member in members %}

                    <option
                        value="{{ member['member_id'] }}"
                    >
                        {{ member["name"] }}
                    </option>

                    {% endfor %}

                </select>

                <label>Amount</label>

                <input
                    type="number"
                    name="amount"
                    value="{{ group['contribution'] }}"
                    min="0"
                    step="0.01"
                    required
                >

                <button type="submit">
                    Record Payment
                </button>

            </form>

        </div>

        {% endif %}


        <div class="card">

            <h2>💰 Contributions</h2>

            {% if contributions %}

            <table>

                <tr>
                    <th>Member</th>
                    <th>Amount</th>
                    <th>Date</th>
                    <th>Status</th>
                </tr>

                {% for payment in contributions %}

                <tr>

                    <td>
                        {{ payment["name"] }}
                    </td>

                    <td>
                        ₦{{ "{:,.2f}".format(
                            payment["amount"]
                        ) }}
                    </td>

                    <td>
                        {{ payment["date"] }}
                    </td>

                    <td>
                        {{ payment["status"] }}
                    </td>

                </tr>

                {% endfor %}

            </table>

            {% else %}

            <p>
                No contributions recorded yet.
            </p>

            {% endif %}

        </div>

    </div>

    """,
    group=group_data,
    members=members,
    contributions=contributions,
    is_owner=is_owner)


# ============================================================
# ADD MEMBER
# ============================================================

@app.route(
    "/group/<int:group_id>/add-member",
    methods=["POST"]
)
@login_required
def add_member(group_id):

    phone = request.form.get(
        "phone",
        ""
    ).strip()

    conn = get_db()

    # Only group owner can add members
    group_data = conn.execute("""
        SELECT *
        FROM groups
        WHERE id=? AND creator_id=?
    """, (
        group_id,
        session["user_id"]
    )).fetchone()

    if not group_data:

        conn.close()

        return (
            "Only the group administrator can "
            "add members.",
            403
        )

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE phone=?
    """, (phone,)).fetchone()

    if not user:

        conn.close()

        return render_template_string(
            STYLE + """

            <div class="container">

                <div class="card">

                    <h2>❌ Member Not Found</h2>

                    <div class="error">

                        No AjoConnect account was
                        found with this phone number.

                    </div>

                    <p>
                        Ask the person to register
                        on AjoConnect first.
                    </p>

                    <a
                        class="btn"
                       
