from flask import Flask, request, redirect, url_for, session, render_template_string
import sqlite3
import os
from functools import wraps
from datetime import datetime

app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "ajoconnect-secret-key")

DATABASE = "ajoconnect.db"


# -----------------------------
# DATABASE
# -----------------------------

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            joined_at TEXT NOT NULL
        )
    """)

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


# -----------------------------
# LOGIN PROTECTION
# -----------------------------

def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return function(*args, **kwargs)

    return wrapper


# -----------------------------
# HTML STYLE
# -----------------------------

STYLE = """
<style>
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
    max-width: 900px;
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

input, select {
    width: 100%;
    padding: 12px;
    margin: 8px 0 15px;
    border: 1px solid #ccc;
    border-radius: 7px;
    box-sizing: border-box;
}

button, .btn {
    background: #087f5b;
    color: white;
    border: none;
    padding: 12px 18px;
    border-radius: 7px;
    cursor: pointer;
    text-decoration: none;
    display: inline-block;
}

button:hover, .btn:hover {
    background: #056044;
}

.hero {
    text-align: center;
    padding: 50px 20px;
}

.hero h1 {
    color: #087f5b;
    font-size: 42px;
}

.stat {
    display: inline-block;
    width: 28%;
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
}

.success {
    background: #d3f9d8;
    padding: 12px;
    border-radius: 7px;
    color: #176b24;
}

table {
    width: 100%;
    border-collapse: collapse;
}

th, td {
    padding: 10px;
    border-bottom: 1px solid #ddd;
    text-align: left;
}

@media(max-width:600px) {
    .stat {
        width: 90%;
        margin: 5px;
    }
}
</style>
"""


# -----------------------------
# HOME
# -----------------------------

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
        track contributions and organize your rotation schedule.
        </p>

        <br>

        <a class="btn" href="/register">Create Account</a>
        <a class="btn" href="/login">Login</a>
    </div>
    """)


# -----------------------------
# REGISTER
# -----------------------------

@app.route("/register", methods=["GET", "POST"])
def register():

    error = ""

    if request.method == "POST":

        name = request.form["name"].strip()
        phone = request.form["phone"].strip()
        password = request.form["password"]

        if not name or not phone or not password:
            error = "Please complete all fields."

        else:

            conn = get_db()

            try:

                conn.execute("""
                    INSERT INTO users
                    (name, phone, password, created_at)
                    VALUES (?, ?, ?, ?)
                """, (
                    name,
                    phone,
                    password,
                    datetime.now().isoformat()
                ))

                conn.commit()

                user = conn.execute(
                    "SELECT id FROM users WHERE phone=?",
                    (phone,)
                ).fetchone()

                session["user_id"] = user["id"]
                session["user_name"] = name

                conn.close()

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
                <input name="name" required>

                <label>Phone Number</label>
                <input name="phone" required>

                <label>Password</label>
                <input type="password" name="password" required>

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


# -----------------------------
# LOGIN
# -----------------------------

@app.route("/login", methods=["GET", "POST"])
def login():

    error = ""

    if request.method == "POST":

        phone = request.form["phone"]
        password = request.form["password"]

        conn = get_db()

        user = conn.execute("""
            SELECT * FROM users
            WHERE phone=? AND password=?
        """, (phone, password)).fetchone()

        conn.close()

        if user:

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
                <input name="phone" required>

                <label>Password</label>
                <input type="password" name="password" required>

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


# -----------------------------
# LOGOUT
# -----------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# -----------------------------
# DASHBOARD
# -----------------------------

@app.route("/dashboard")
@login_required
def dashboard():

    user_id = session["user_id"]

    conn = get_db()

    groups = conn.execute("""
        SELECT * FROM groups
        WHERE creator_id=?
        ORDER BY id DESC
    """, (user_id,)).fetchall()

    member_count = conn.execute("""
        SELECT COUNT(*) AS count
        FROM members
        WHERE user_id=?
    """, (user_id,)).fetchone()["count"]

    conn.close()

    return render_template_string(STYLE + """
    <nav>
        <a href="/dashboard">AjoConnect</a>
        <a href="/create-group">Create Group</a>
        <a href="/logout">Logout</a>
    </nav>

    <div class="container">

        <h1>Welcome, {{ name }} 👋</h1>

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

                    <h3>{{ group["name"] }}</h3>

                    <p>
                    Contribution:
                    ₦{{ "{:,.2f}".format(group["contribution"]) }}
                    </p>

                    <p>
                    Frequency: {{ group["frequency"] }}
                    </p>

                    <a class="btn"
                       href="/group/{{ group['id'] }}">
                       Open Group
                    </a>

                </div>

                {% endfor %}

            {% else %}

                <p>You haven't created an Ajo group yet.</p>

                <a class="btn" href="/create-group">
                    Create Your First Group
                </a>

            {% endif %}

        </div>

    </div>
    """,
    name=session["user_name"],
    groups=groups,
    member_count=member_count)


# -----------------------------
# CREATE GROUP
# -----------------------------

@app.route("/create-group", methods=["GET", "POST"])
@login_required
def create_group():

    if request.method == "POST":

        name = request.form["name"]
        contribution = float(request.form["contribution"])
        frequency = request.form["frequency"]

        conn = get_db()

        cursor = conn.execute("""
            INSERT INTO groups
            (name, contribution, frequency, creator_id, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            name,
            contribution,
            frequency,
            session["user_id"],
            datetime.now().isoformat()
        ))

        group_id = cursor.lastrowid

        conn.execute("""
            INSERT INTO members
            (group_id, user_id, position, joined_at)
            VALUES (?, ?, ?, ?)
        """, (
            group_id,
            session["user_id"],
            1,
            datetime.now().isoformat()
        ))

        conn.commit()
        conn.close()

        return redirect(url_for("group", group_id=group_id))

    return render_template_string(STYLE + """
    <nav>
        <a href="/dashboard">AjoConnect</a>
        <a href="/logout">Logout</a>
    </nav>

    <div class="container">

        <div class="card">

            <h2>➕ Create Ajo Group</h2>

            <form method="POST">

                <label>Group Name</label>
                <input
                    name="name"
                    placeholder="e.g. Family Ajo"
                    required
                >

                <label>Contribution Amount (₦)</label>
                <input
                    type="number"
                    name="contribution"
                    min="1"
                    required
                >

                <label>Contribution Frequency</label>

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
    """)


# -----------------------------
# GROUP DASHBOARD
# -----------------------------

@app.route("/group/<int:group_id>")
@login_required
def group(group_id):

    conn = get_db()

    group_data = conn.execute("""
        SELECT * FROM groups
        WHERE id=?
    """, (group_id,)).fetchone()

    if not group_data:
        conn.close()
        return "Group not found", 404

    members = conn.execute("""
        SELECT
            members.id AS member_id,
            members.position,
            users.name,
            users.phone
        FROM members
        JOIN users ON users.id = members.user_id
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

    conn.close()

    return render_template_string(STYLE + """
    <nav>
        <a href="/dashboard">Dashboard</a>
        <a href="/logout">Logout</a>
    </nav>

    <div class="container">

        <div class="card">

            <h1>👥 {{ group["name"] }}</h1>

            <p>
                Contribution:
                <strong>
                ₦{{ "{:,.2f}".format(group["contribution"]) }}
                </strong>
            </p>

            <p>
                Frequency:
                <strong>{{ group["frequency"] }}</strong>
            </p>

        </div>


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

                    <td>{{ member["position"] }}</td>

                    <td>{{ member["name"] }}</td>

                    <td>{{ member["phone"] }}</td>

                </tr>

                {% endfor %}

            </table>

        </div>


        <div class="card">

            <h2>💰 Contributions</h2>

            <table>

                <tr>
                    <th>Member</th>
                    <th>Amount</th>
                    <th>Date</th>
                    <th>Status</th>
                </tr>

                {% for payment in contributions %}

                <tr>

                    <td>{{ payment["name"] }}</td>

                    <td>
                    ₦{{ "{:,.2f}".format(payment["amount"]) }}
                    </td>

                    <td>{{ payment["date"] }}</td>

                    <td>{{ payment["status"] }}</td>

                </tr>

                {% endfor %}

            </table>

        </div>


        <div class="card">

            <h2>➕ Record Contribution</h2>

            <form method="POST"
                  action="/group/{{ group['id'] }}/contribution">

                <label>Member</label>

                <select name="member_id">

                    {% for member in members %}

                    <option value="{{ member['member_id'] }}">
                        {{ member["name"] }}
                    </option>

                    {% endfor %}

                </select>

                <label>Amount</label>

                <input
                    type="number"
                    name="amount"
                    value="{{ group['contribution'] }}"
                    required
                >

                <button type="submit">
                    Record Payment
                </button>

            </form>

        </div>

    </div>
    """,
    group=group_data,
    members=members,
    contributions=contributions)


# -----------------------------
# RECORD CONTRIBUTION
# -----------------------------

@app.route("/group/<int:group_id>/contribution", methods=["POST"])
@login_required
def contribution(group_id):

    member_id = request.form["member_id"]
    amount = float(request.form["amount"])

    conn = get_db()

    conn.execute("""
        INSERT INTO contributions
        (group_id, member_id, amount, date, status)
        VALUES (?, ?, ?, ?, ?)
    """, (
        group_id,
        member_id,
        amount,
        datetime.now().strftime("%Y-%m-%d"),
        "Paid"
    ))

    conn.commit()
    conn.close()

    return redirect(url_for("group", group_id=group_id))


# -----------------------------
# HEALTH CHECK
# -----------------------------

@app.route("/health")
def health():

    return "AjoConnect is running!"


# -----------------------------
# START APPLICATION
# -----------------------------

init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
