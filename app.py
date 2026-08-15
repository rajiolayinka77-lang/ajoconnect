
from flask import Flask, request, redirect, url_for, session, render_template_string, flash, abort
import sqlite3, os, json, urllib.request, urllib.error, uuid
from functools import wraps
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import escape

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-in-render")
DATABASE = os.environ.get("DATABASE_PATH", "ajoconnect.db")
PREMIUM_PRICE = 2000
FREE_GROUP_LIMIT = 1
FREE_MEMBER_LIMIT = 10

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def money(v):
    return f"₦{float(v or 0):,.2f}"

app.jinja_env.filters["money"] = money

def add_column_if_missing(conn, table, column, definition):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'admin',
        created_at TEXT NOT NULL,
        plan TEXT NOT NULL DEFAULT 'free',
        premium_until TEXT
    );
    CREATE TABLE IF NOT EXISTS groups(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contribution REAL NOT NULL DEFAULT 0,
        frequency TEXT NOT NULL DEFAULT 'monthly',
        created_at TEXT NOT NULL,
        user_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS members(
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
    CREATE TABLE IF NOT EXISTS contributions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'paid',
        note TEXT,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
        FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS payouts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        member_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payout_date TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        note TEXT,
        FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE,
        FOREIGN KEY(member_id) REFERENCES members(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS subscriptions(
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
    # Upgrade older AjoConnect databases safely.
    add_column_if_missing(conn, "users", "plan", "TEXT NOT NULL DEFAULT 'free'")
    add_column_if_missing(conn, "users", "premium_until", "TEXT")
    add_column_if_missing(conn, "groups", "user_id", "INTEGER")
    add_column_if_missing(conn, "contributions", "note", "TEXT")
    add_column_if_missing(conn, "payouts", "note", "TEXT")
    first = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if first:
        conn.execute("UPDATE groups SET user_id=? WHERE user_id IS NULL", (first["id"],))
    conn.commit()
    conn.close()

init_db()

def is_premium(uid):
    conn = get_db()
    u = conn.execute("SELECT plan,premium_until FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not u or u["plan"] != "premium" or not u["premium_until"]:
        return False
    try:
        return datetime.strptime(u["premium_until"], "%Y-%m-%d %H:%M:%S") > datetime.now()
    except ValueError:
        return False

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login"))
        session["premium"] = is_premium(session["user_id"])
        return fn(*a, **kw)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            return redirect(url_for("dashboard"))
        session["premium"] = is_premium(session["user_id"])
        return fn(*a, **kw)
    return wrapper

def owned_group(group_id):
    conn = get_db()
    g = conn.execute("SELECT * FROM groups WHERE id=? AND user_id=?", (group_id, session["user_id"])).fetchone()
    conn.close()
    return g

def current_user():
    if "user_id" not in session: return None
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    return u

def paystack(path, method="GET", payload=None):
    key = os.environ.get("PAYSTACK_SECRET_KEY")
    if not key: return None, "PAYSTACK_SECRET_KEY is not configured."
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        "https://api.paystack.co"+path, data=data,
        headers={"Authorization":"Bearer "+key, "Content-Type":"application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"Paystack error {e.code}"
    except Exception as e:
        return None, str(e)

BASE = """
<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }} - AjoConnect</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:#f4f7f6;color:#1f2937}
nav{background:#075e54;color:#fff;padding:14px 18px;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}
nav a{color:#fff;text-decoration:none;font-weight:bold;margin:4px 8px}.brand{font-size:21px}
.container{max-width:1100px;margin:24px auto;padding:0 14px}.hero{background:linear-gradient(135deg,#075e54,#128c7e);color:#fff;padding:28px;border-radius:16px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:15px}.card{background:#fff;padding:20px;border-radius:14px;box-shadow:0 2px 8px #00000012;margin-bottom:18px}
.stat{font-size:25px;font-weight:bold;color:#075e54;margin-top:8px}.btn{display:inline-block;background:#075e54;color:#fff;border:0;border-radius:8px;padding:10px 14px;text-decoration:none;font-weight:bold;cursor:pointer}
.btn-secondary{background:#555}.btn-warning{background:#d18b00}.btn-premium{background:#8a5a00}
.actions{display:flex;gap:8px;flex-wrap:wrap}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse}th,td{padding:12px;border-bottom:1px solid #eee;text-align:left;vertical-align:middle}th{background:#f0f4f3}
input,select,textarea{width:100%;padding:12px;margin:6px 0 15px;border:1px solid #ddd;border-radius:8px;font-size:15px}label{font-weight:bold}
.badge{display:inline-block;padding:5px 9px;border-radius:20px;font-size:12px;font-weight:bold}.paid,.success{background:#d7f5df;color:#176b35}.pending{background:#fff0c2;color:#7a5700}
.flash{padding:12px 15px;background:#fff3cd;border-radius:8px;margin-bottom:15px}.premium-box{border:2px solid #d8a63a;background:#fffaf0}
.member-row{cursor:pointer}.member-row:hover{background:#f7faf9}.member-link{color:inherit;text-decoration:none;display:block}.member-link strong{text-decoration:underline}.member-link small{display:block;color:#777;margin-top:3px}
footer{text-align:center;padding:30px;color:#777}@media(max-width:600px){nav{display:block}nav a{display:inline-block;margin:6px 5px}th,td{font-size:13px}.btn{padding:9px 10px}}
</style></head><body>
<nav><div class="brand">💰 AjoConnect</div><div>
{% if session.get('user_id') %}
<a href="{{url_for('dashboard')}}">Dashboard</a><a href="{{url_for('groups')}}">Groups</a><a href="{{url_for('subscription')}}">Premium</a>
{% if session.get('role')=='admin' %}<a href="{{url_for('admin_dashboard')}}">Admin</a>{% endif %}
<a href="{{url_for('logout')}}">Logout</a>
{% else %}<a href="{{url_for('login')}}">Login</a><a href="{{url_for('register')}}">Register</a>{% endif %}
</div></nav>
<div class="container">{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}{{content|safe}}</div>
<footer>AjoConnect © 2026 — Digital Ajo & Esusu Management</footer></body></html>
"""

def page(title, content):
    return render_template_string(BASE, title=title, content=content)

@app.route("/")
def home():
    if "user_id" in session: return redirect(url_for("dashboard"))
    return page("Home", """<div class="hero"><h1>💰 Welcome to AjoConnect</h1><p>Manage Ajo / Esusu groups, members, contributions and rotation.</p><a class="btn" href="/register">Create Free Account</a> <a class="btn btn-secondary" href="/login">Login</a></div>
    <div class="grid"><div class="card"><h3>👥 Members</h3><p>Manage members and rotation positions.</p></div><div class="card"><h3>💵 Contributions</h3><p>Record and view payment history.</p></div><div class="card"><h3>🔄 Rotation</h3><p>Track beneficiaries and payouts.</p></div><div class="card premium-box"><h3>⭐ Premium</h3><p>Unlimited groups and members for ₦2,000/month.</p></div></div>""")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method=="POST":
        name=request.form.get("name","").strip(); email=request.form.get("email","").strip().lower(); pw=request.form.get("password","")
        if not name or not email or len(pw)<6: flash("Enter your name, a valid email and a password of at least 6 characters."); return redirect(url_for("register"))
        conn=get_db()
        try:
            conn.execute("INSERT INTO users(name,email,password,role,plan,created_at) VALUES(?,?,?,'admin','free',?)",(name,email,generate_password_hash(pw),now()))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close(); flash("An account with that email already exists."); return redirect(url_for("login"))
        conn.close(); flash("Account created successfully. Please login."); return redirect(url_for("login"))
    return page("Register", """<div class="card"><h2>📝 Create AjoConnect Account</h2><form method="POST"><label>Full Name</label><input name="name" required><label>Email</label><input type="email" name="email" required><label>Password</label><input type="password" name="password" minlength="6" required><button class="btn">Create Account</button></form></div>""")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        email=request.form.get("email","").strip().lower(); pw=request.form.get("password","")
        conn=get_db(); u=conn.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone(); conn.close()
        if u and check_password_hash(u["password"],pw):
            session.clear(); session.update(user_id=u["id"],name=u["name"],role=u["role"],premium=is_premium(u["id"]))
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.")
    return page("Login", """<div class="card"><h2>🔐 Login</h2><form method="POST"><label>Email</label><input type="email" name="email" required><label>Password</label><input type="password" name="password" required><button class="btn">Login</button></form></div>""")

@app.route("/logout")
def logout():
    session.clear(); flash("You have been logged out."); return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    conn=get_db()
    gs=conn.execute("SELECT * FROM groups WHERE user_id=? ORDER BY id DESC",(session["user_id"],)).fetchall()
    members=conn.execute("SELECT COUNT(*) n FROM members JOIN groups ON groups.id=members.group_id WHERE groups.user_id=?",(session["user_id"],)).fetchone()["n"]
    contrib=conn.execute("SELECT COALESCE(SUM(c.amount),0) n FROM contributions c JOIN groups g ON g.id=c.group_id WHERE g.user_id=? AND lower(c.status)='paid'",(session["user_id"],)).fetchone()["n"]
    payout=conn.execute("SELECT COALESCE(SUM(p.amount),0) n FROM payouts p JOIN groups g ON g.id=p.group_id WHERE g.user_id=? AND lower(p.status)='paid'",(session["user_id"],)).fetchone()["n"]
    conn.close()
    rows="".join(f"<tr><td>{escape(g['name'])}</td><td>{money(g['contribution'])}</td><td>{escape(g['frequency'].title())}</td><td><a class='btn' href='/group/{g['id']}'>Open</a></td></tr>" for g in gs)
    return page("Dashboard",f"""<div class="hero"><h1>👋 Welcome, {escape(session.get('name',''))}</h1><p>Plan: <strong>{'⭐ PREMIUM' if session.get('premium') else 'FREE'}</strong></p></div>
    <div class="grid"><div class="card"><h3>👥 Members</h3><div class="stat">{members}</div></div><div class="card"><h3>💰 Contributions</h3><div class="stat">{money(contrib)}</div></div><div class="card"><h3>💸 Payouts</h3><div class="stat">{money(payout)}</div></div></div>
    <div class="card"><div class="actions"><a class="btn" href="/groups/new">➕ Create Ajo Group</a><a class="btn btn-secondary" href="/groups">📋 View Groups</a></div></div>
    <div class="card"><h2>Your Ajo Groups</h2><div class="table-wrap"><table><tr><th>Group</th><th>Contribution</th><th>Frequency</th><th>Action</th></tr>{rows or '<tr><td colspan="4">No groups yet. Create your first Ajo group.</td></tr>'}</table></div></div>""")

@app.route("/groups")
@login_required
def groups():
    conn=get_db(); gs=conn.execute("SELECT * FROM groups WHERE user_id=? ORDER BY id DESC",(session["user_id"],)).fetchall(); conn.close()
    cards="".join(f"<div class='card'><h2>{escape(g['name'])}</h2><p>Contribution: <strong>{money(g['contribution'])}</strong></p><p>Frequency: <strong>{escape(g['frequency'].title())}</strong></p><a class='btn' href='/group/{g['id']}'>Open Group</a></div>" for g in gs)
    return page("Groups",f"<div class='card'><div class='actions'><h2 style='margin-right:auto'>📋 Ajo Groups</h2><a class='btn' href='/groups/new'>➕ New Group</a></div></div><div class='grid'>{cards or '<div class=\"card\">No groups yet.</div>'}</div>")

@app.route("/groups/new", methods=["GET","POST"])
@login_required
def create_group():
    conn=get_db(); count=conn.execute("SELECT COUNT(*) n FROM groups WHERE user_id=?",(session["user_id"],)).fetchone()["n"]; conn.close()
    if not session.get("premium") and count>=FREE_GROUP_LIMIT:
        flash("Free plan allows 1 Ajo group. Upgrade to Premium for unlimited groups."); return redirect(url_for("subscription"))
    if request.method=="POST":
        name=request.form.get("name","").strip(); freq=request.form.get("frequency","monthly")
        try: amount=float(request.form.get("contribution","0"))
        except: amount=-1
        if not name or amount<=0: flash("Enter a group name and a valid contribution amount."); return redirect(url_for("create_group"))
        if freq not in ("weekly","biweekly","monthly"): freq="monthly"
        conn=get_db(); cur=conn.execute("INSERT INTO groups(name,contribution,frequency,created_at,user_id) VALUES(?,?,?,?,?)",(name,amount,freq,now(),session["user_id"])); conn.commit(); gid=cur.lastrowid; conn.close()
        flash("Ajo group created successfully."); return redirect(url_for("group_detail",group_id=gid))
    return page("Create Group","""<div class="card"><h2>➕ Create Ajo Group</h2><form method="POST"><label>Group Name</label><input name="name" placeholder="Mama Raji" required><label>Contribution Amount</label><input type="number" name="contribution" min="1" step="0.01" placeholder="10000" required><label>Frequency</label><select name="frequency"><option value="weekly">Weekly</option><option value="biweekly">Bi-weekly</option><option value="monthly">Monthly</option></select><button class="btn">Create Group</button></form></div>""")

@app.route("/group/<int:group_id>")
@login_required
def group_detail(group_id):
    g=owned_group(group_id)
    if not g: abort(404)
    conn=get_db()
    ms=conn.execute("SELECT * FROM members WHERE group_id=? ORDER BY position,id",(group_id,)).fetchall()
    paid=conn.execute("SELECT COALESCE(SUM(amount),0) n FROM contributions WHERE group_id=? AND lower(status)='paid'",(group_id,)).fetchone()["n"]
    payout=conn.execute("SELECT COALESCE(SUM(amount),0) n FROM payouts WHERE group_id=? AND lower(status)='paid'",(group_id,)).fetchone()["n"]
    conn.close()
    expected=g["contribution"]*len(ms)
    current=next((m for m in ms if m["status"]=="active"),None)
    rows=""
    for m in ms:
        rows+=f"""<tr class="member-row" onclick="if(!event.target.closest('a')) location.href='/group/{group_id}/member/{m['id']}'">
        <td>{m['position']}</td><td><a class="member-link" href="/group/{group_id}/member/{m['id']}"><strong>{escape(m['name'])}</strong><small>Tap to view details</small></a></td>
        <td>{escape(m['phone'] or '-')}</td><td><span class="badge success">{escape(m['status'].title())}</span></td><td>{money(expected)}</td>
        <td><div class="actions"><a class="btn" href="/group/{group_id}/member/{m['id']}">View Details</a><a class="btn" href="/group/{group_id}/member/{m['id']}/contribute">Payment</a><a class="btn btn-warning" href="/group/{group_id}/member/{m['id']}/payout">Payout</a></div></td></tr>"""
    if not rows: rows="<tr><td colspan='6'>No members yet. Add the first member.</td></tr>"
    return page(g["name"],f"""<div class="hero"><h1>🔄 {escape(g['name'])}</h1><p>Contribution: <strong>{money(g['contribution'])}</strong> · Members: <strong>{len(ms)}</strong></p><p>Expected payout: <strong>{money(expected)}</strong></p></div>
    <div class="grid"><div class="card"><h3>Total Contributions</h3><div class="stat">{money(paid)}</div></div><div class="card"><h3>Total Payouts</h3><div class="stat">{money(payout)}</div></div><div class="card"><h3>Current Beneficiary</h3><div class="stat">{escape(current['name']) if current else 'Completed'}</div></div></div>
    <div class="card"><div class="actions"><a class="btn" href="/group/{group_id}/member/add">➕ Add Member</a><a class="btn btn-secondary" href="/group/{group_id}/contributions">💰 Contributions</a><a class="btn btn-secondary" href="/group/{group_id}/payouts">💸 Payouts</a></div></div>
    <div class="card"><h2>📅 Rotation Schedule</h2><div class="table-wrap"><table><tr><th>Position</th><th>Member</th><th>Phone</th><th>Status</th><th>Expected Payout</th><th>Action</th></tr>{rows}</table></div></div>""")

@app.route("/group/<int:group_id>/member/add", methods=["GET","POST"])
@login_required
def add_member(group_id):
    g=owned_group(group_id)
    if not g: abort(404)
    conn=get_db(); count=conn.execute("SELECT COUNT(*) n FROM members WHERE group_id=?",(group_id,)).fetchone()["n"]
    if not session.get("premium") and count>=FREE_MEMBER_LIMIT:
        conn.close(); flash("Free plan allows 10 members per group. Upgrade to Premium."); return redirect(url_for("subscription"))
    if request.method=="POST":
        name=request.form.get("name","").strip(); phone=request.form.get("phone","").strip(); email=request.form.get("email","").strip()
        if not name: conn.close(); flash("Member name is required."); return redirect(url_for("add_member",group_id=group_id))
        pos=conn.execute("SELECT COALESCE(MAX(position),0)+1 n FROM members WHERE group_id=?",(group_id,)).fetchone()["n"]
        conn.execute("INSERT INTO members(group_id,name,phone,email,position,status,created_at) VALUES(?,?,?,?,?,'active',?)",(group_id,name,phone,email,pos,now())); conn.commit(); conn.close()
        flash(f"{name} has been added."); return redirect(url_for("group_detail",group_id=group_id))
    conn.close()
    return page("Add Member",f"""<div class="card"><h2>➕ Add Member</h2><p>Group: <strong>{escape(g['name'])}</strong></p><form method="POST"><label>Member Name</label><input name="name" required><label>Phone</label><input name="phone"><label>Email</label><input type="email" name="email"><button class="btn">Add Member</button></form></div>""")

def get_member(group_id, member_id):
    conn=get_db(); m=conn.execute("SELECT * FROM members WHERE id=? AND group_id=?",(member_id,group_id)).fetchone(); conn.close(); return m

@app.route("/group/<int:group_id>/member/<int:member_id>")
@login_required
def member_detail(group_id,member_id):
    g=owned_group(group_id)
    if not g: abort(404)
    m=get_member(group_id,member_id)
    if not m: abort(404)
    conn=get_db()
    cs=conn.execute("SELECT * FROM contributions WHERE group_id=? AND member_id=? ORDER BY payment_date DESC,id DESC",(group_id,member_id)).fetchall()
    ps=conn.execute("SELECT * FROM payouts WHERE group_id=? AND member_id=? ORDER BY payout_date DESC,id DESC",(group_id,member_id)).fetchall()
    active=conn.execute("SELECT COUNT(*) n FROM members WHERE group_id=? AND status='active'",(group_id,)).fetchone()["n"]; conn.close()
    ctotal=sum(float(x["amount"] or 0) for x in cs); ptotal=sum(float(x["amount"] or 0) for x in ps if x["status"].lower()=="paid"); expected=g["contribution"]*active
    cr="".join(f"<tr><td>{money(x['amount'])}</td><td>{escape(x['payment_date'])}</td><td><span class='badge paid'>{escape(x['status'].title())}</span></td><td>{escape(x['note'] or '-')}</td></tr>" for x in cs) or "<tr><td colspan='4'>No contribution recorded yet.</td></tr>"
    pr="".join(f"<tr><td>{money(x['amount'])}</td><td>{escape(x['payout_date'] or '-')}</td><td><span class='badge {'paid' if x['status'].lower()=='paid' else 'pending'}'>{escape(x['status'].title())}</span></td><td>{escape(x['note'] or '-')}</td></tr>" for x in ps) or "<tr><td colspan='4'>No payout recorded yet.</td></tr>"
    return page(m["name"],f"""<div class="hero"><h1>👤 {escape(m['name'])}</h1><p>Member details for <strong>{escape(g['name'])}</strong></p></div>
    <div class="grid"><div class="card"><h3>Position</h3><div class="stat">#{m['position']}</div></div><div class="card"><h3>Phone</h3><div class="stat">{escape(m['phone'] or '-')}</div></div><div class="card"><h3>Status</h3><div class="stat">{escape(m['status'].title())}</div></div><div class="card"><h3>Total Contributions</h3><div class="stat">{money(ctotal)}</div></div><div class="card"><h3>Total Payout Received</h3><div class="stat">{money(ptotal)}</div></div><div class="card"><h3>Expected Payout</h3><div class="stat">{money(expected)}</div></div></div>
    <div class="card"><h2>📋 Member Information</h2><p><strong>Name:</strong> {escape(m['name'])}</p><p><strong>Phone:</strong> {escape(m['phone'] or 'Not provided')}</p><p><strong>Email:</strong> {escape(m['email'] or 'Not provided')}</p><p><strong>Rotation Position:</strong> {m['position']}</p><p><strong>Status:</strong> {escape(m['status'].title())}</p><p><strong>Joined:</strong> {escape(m['created_at'])}</p><div class="actions"><a class="btn" href="/group/{group_id}/member/{member_id}/contribute">💰 Record Contribution</a><a class="btn btn-warning" href="/group/{group_id}/member/{member_id}/payout">💸 Record Payout</a><a class="btn btn-secondary" href="/group/{group_id}">← Back</a></div></div>
    <div class="card"><h2>💰 Contribution History</h2><div class="table-wrap"><table><tr><th>Amount</th><th>Date</th><th>Status</th><th>Note</th></tr>{cr}</table></div></div>
    <div class="card"><h2>💸 Payout History</h2><div class="table-wrap"><table><tr><th>Amount</th><th>Date</th><th>Status</th><th>Note</th></tr>{pr}</table></div></div>""")

@app.route("/group/<int:group_id>/member/<int:member_id>/contribute",methods=["GET","POST"])
@login_required
def record_contribution(group_id,member_id):
    g=owned_group(group_id); m=get_member(group_id,member_id)
    if not g: abort(404)
    if not m: abort(404)
    if request.method=="POST":
        try: amount=float(request.form.get("amount",g["contribution"]))
        except: amount=-1
        date=request.form.get("payment_date") or datetime.now().strftime("%Y-%m-%d"); note=request.form.get("note","").strip()
        if amount<=0: flash("Enter a valid amount."); return redirect(url_for("record_contribution",group_id=group_id,member_id=member_id))
        conn=get_db(); conn.execute("INSERT INTO contributions(group_id,member_id,amount,payment_date,status,note) VALUES(?,?,?,?, 'paid',?)",(group_id,member_id,amount,date,note)); conn.commit(); conn.close()
        flash(f"Payment of {money(amount)} recorded for {m['name']}."); return redirect(url_for("member_detail",group_id=group_id,member_id=member_id))
    return page("Record Contribution",f"""<div class="card"><h2>💰 Record Contribution</h2><p>Member: <strong>{escape(m['name'])}</strong></p><form method="POST"><label>Amount Paid</label><input type="number" name="amount" value="{g['contribution']}" min="0.01" step="0.01" required><label>Payment Date</label><input type="date" name="payment_date" value="{datetime.now().strftime('%Y-%m-%d')}" required><label>Note</label><textarea name="note"></textarea><button class="btn">Record Payment</button></form></div>""")

@app.route("/group/<int:group_id>/contributions")
@login_required
def contributions(group_id):
    g=owned_group(group_id)
    if not g: abort(404)
    conn=get_db(); rows=conn.execute("""SELECT c.*,m.name member_name FROM contributions c JOIN members m ON m.id=c.member_id WHERE c.group_id=? ORDER BY c.payment_date DESC,c.id DESC""",(group_id,)).fetchall(); conn.close()
    body="".join(f"<tr><td><a class='member-link' href='/group/{group_id}/member/{r['member_id']}'><strong>{escape(r['member_name'])}</strong></a></td><td>{money(r['amount'])}</td><td>{escape(r['payment_date'])}</td><td><span class='badge paid'>{escape(r['status'].title())}</span></td><td>{escape(r['note'] or '-')}</td></tr>" for r in rows)
    if not body: body="<tr><td colspan='5'>No contribution has been recorded yet.</td></tr>"
    return page("Contributions",f"""<div class="card"><h2>💰 Contribution History</h2><p>Group: <strong>{escape(g['name'])}</strong></p><div class="table-wrap"><table><tr><th>Member</th><th>Amount</th><th>Date</th><th>Status</th><th>Note</th></tr>{body}</table></div><br><a class="btn btn-secondary" href="/group/{group_id}">← Back to Group</a></div>""")

@app.route("/group/<int:group_id>/member/<int:member_id>/payout",methods=["GET","POST"])
@login_required
def record_payout(group_id,member_id):
    g=owned_group(group_id); m=get_member(group_id,member_id)
    if not g: abort(404)
    if not m: abort(404)
    conn=get_db(); n=conn.execute("SELECT COUNT(*) n FROM members WHERE group_id=? AND status='active'",(group_id,)).fetchone()["n"]; conn.close()
    expected=g["contribution"]*n
    if request.method=="POST":
        try: amount=float(request.form.get("amount",expected))
        except: amount=-1
        date=request.form.get("payout_date") or datetime.now().strftime("%Y-%m-%d"); note=request.form.get("note","").strip()
        if amount<=0: flash("Enter a valid payout amount."); return redirect(url_for("record_payout",group_id=group_id,member_id=member_id))
        conn=get_db(); conn.execute("INSERT INTO payouts(group_id,member_id,amount,payout_date,status,note) VALUES(?,?,?,?, 'paid',?)",(group_id,member_id,amount,date,note)); conn.commit(); conn.close()
        flash(f"Payout of {money(amount)} recorded for {m['name']}."); return redirect(url_for("member_detail",group_id=group_id,member_id=member_id))
    return page("Record Payout",f"""<div class="card"><h2>💸 Record Payout</h2><p>Beneficiary: <strong>{escape(m['name'])}</strong></p><p>Expected payout: <strong>{money(expected)}</strong></p><form method="POST"><label>Payout Amount</label><input type="number" name="amount" value="{expected}" min="0.01" step="0.01" required><label>Payout Date</label><input type="date" name="payout_date" value="{datetime.now().strftime('%Y-%m-%d')}" required><label>Note</label><textarea name="note"></textarea><button class="btn btn-warning">Confirm Payout</button></form></div>""")

@app.route("/group/<int:group_id>/payouts")
@login_required
def payouts(group_id):
    g=owned_group(group_id)
    if not g: abort(404)
    conn=get_db(); rows=conn.execute("SELECT p.*,m.name member_name FROM payouts p JOIN members m ON m.id=p.member_id WHERE p.group_id=? ORDER BY p.payout_date DESC,p.id DESC",(group_id,)).fetchall(); conn.close()
    body="".join(f"<tr><td><a class='member-link' href='/group/{group_id}/member/{r['member_id']}'><strong>{escape(r['member_name'])}</strong></a></td><td>{money(r['amount'])}</td><td>{escape(r['payout_date'] or '-')}</td><td><span class='badge {'paid' if r['status'].lower()=='paid' else 'pending'}'>{escape(r['status'].title())}</span></td><td>{escape(r['note'] or '-')}</td></tr>" for r in rows) or "<tr><td colspan='5'>No payout has been recorded yet.</td></tr>"
    return page("Payouts",f"""<div class="card"><h2>💸 Payout History</h2><p>Group: <strong>{escape(g['name'])}</strong></p><div class="table-wrap"><table><tr><th>Beneficiary</th><th>Amount</th><th>Date</th><th>Status</th><th>Note</th></tr>{body}</table></div><br><a class="btn btn-secondary" href="/group/{group_id}">← Back to Group</a></div>""")

@app.route("/subscription")
@login_required
def subscription():
    u=current_user()
    if is_premium(u["id"]):
        return page("Premium",f"<div class='hero'><h1>⭐ AjoConnect Premium</h1><p>Your Premium membership is active.</p></div><div class='card premium-box'><h2>Premium Active ✅</h2><div class='stat'>{escape(u['premium_until'])}</div></div>")
    return page("Premium",f"""<div class="hero"><h1>⭐ Upgrade AjoConnect</h1><p>More space and tools for your Ajo business.</p></div><div class="card premium-box"><h2>Premium — ₦{PREMIUM_PRICE:,.0f}/month</h2><ul><li>Unlimited groups</li><li>Unlimited members</li><li>Contribution tracking</li><li>Payout records</li></ul><a class="btn btn-premium" href="/pay/premium">Upgrade for ₦{PREMIUM_PRICE:,.0f}</a></div><div class="card"><h3>Free Plan</h3><p>1 group and up to 10 members.</p></div>""")

@app.route("/pay/premium")
@login_required
def pay_premium():
    u=current_user(); ref="AJOCONNECT-"+uuid.uuid4().hex.upper()
    conn=get_db(); conn.execute("INSERT INTO subscriptions(user_id,plan,amount,reference,status,created_at) VALUES(?,'premium',?,?,'pending',?)",(u["id"],PREMIUM_PRICE,ref,now())); conn.commit(); conn.close()
    payload={"email":u["email"],"amount":PREMIUM_PRICE*100,"currency":"NGN","reference":ref,"callback_url":request.host_url.rstrip("/")+"/paystack/callback","metadata":{"user_id":u["id"],"product":"AjoConnect Premium"}}
    result,error=paystack("/transaction/initialize","POST",payload)
    if error or not result or not result.get("status"): flash("Payment could not be started. Check PAYSTACK_SECRET_KEY in Render."); return redirect(url_for("subscription"))
    return redirect(result["data"]["authorization_url"])

@app.route("/paystack/callback")
@login_required
def paystack_callback():
    ref=request.args.get("reference","").strip(); u=current_user()
    if not ref: flash("No payment reference received."); return redirect(url_for("subscription"))
    result,error=paystack("/transaction/verify/"+ref)
    if error or not result or not result.get("status"): flash("Payment could not be verified."); return redirect(url_for("subscription"))
    tx=result.get("data",{})
    if tx.get("status")=="success" and tx.get("currency")=="NGN" and tx.get("amount")==PREMIUM_PRICE*100:
        conn=get_db(); sub=conn.execute("SELECT id FROM subscriptions WHERE reference=? AND user_id=?",(ref,u["id"])).fetchone()
        if sub:
            expiry=datetime.now()+timedelta(days=30)
            conn.execute("UPDATE subscriptions SET status='paid',started_at=?,expires_at=? WHERE id=?",(now(),expiry.strftime("%Y-%m-%d %H:%M:%S"),sub["id"]))
            conn.execute("UPDATE users SET plan='premium',premium_until=? WHERE id=?",(expiry.strftime("%Y-%m-%d %H:%M:%S"),u["id"])); conn.commit()
        conn.close(); session["premium"]=True; flash("Payment successful. Premium is active for 30 days."); return redirect(url_for("subscription"))
    flash("Payment was not completed successfully."); return redirect(url_for("subscription"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    conn=get_db()
    users=conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]; groups=conn.execute("SELECT COUNT(*) n FROM groups").fetchone()["n"]; members=conn.execute("SELECT COUNT(*) n FROM members").fetchone()["n"]
    contributions=conn.execute("SELECT COALESCE(SUM(amount),0) n FROM contributions WHERE lower(status)='paid'").fetchone()["n"]; payouts=conn.execute("SELECT COALESCE(SUM(amount),0) n FROM payouts WHERE lower(status)='paid'").fetchone()["n"]
    premium_users=conn.execute("SELECT COUNT(*) n FROM users WHERE plan='premium' AND premium_until>?",(now(),)).fetchone()["n"]; revenue=conn.execute("SELECT COALESCE(SUM(amount),0) n FROM subscriptions WHERE status='paid'").fetchone()["n"]; conn.close()
    return page("Admin",f"""<div class="hero"><h1>⚙️ AjoConnect Admin</h1><p>Platform overview.</p></div><div class="grid"><div class="card"><h3>Users</h3><div class="stat">{users}</div></div><div class="card"><h3>Groups</h3><div class="stat">{groups}</div></div><div class="card"><h3>Members</h3><div class="stat">{members}</div></div><div class="card"><h3>Premium Users</h3><div class="stat">{premium_users}</div></div><div class="card"><h3>Contributions</h3><div class="stat">{money(contributions)}</div></div><div class="card"><h3>Payouts</h3><div class="stat">{money(payouts)}</div></div><div class="card premium-box"><h3>Premium Revenue</h3><div class="stat">{money(revenue)}</div></div></div>""")

@app.route("/health")
def health(): return {"status":"ok","app":"AjoConnect"}

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)),debug=False)
