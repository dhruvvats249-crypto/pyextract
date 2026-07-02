from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import sqlite3, os, smtplib, time, random, threading, uuid, csv, io, requests, re
import dns.resolver
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from groq import Groq
from cryptography.fernet import Fernet

# ── CONFIG ──────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

SECRET_KEY     = os.environ.get("FLASK_SECRET_KEY", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
BASE_URL       = os.environ.get("BASE_URL", "http://localhost:5050")
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

if not SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY is not set. Add it to your .env file or host's environment variables.")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY is not set. Generate one with:\n"
        "  python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
        "and add it to your .env file / host's environment variables."
    )
fernet = Fernet(ENCRYPTION_KEY.encode())

def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return fernet.encrypt(plaintext.encode()).decode()

def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return fernet.decrypt(ciphertext.encode()).decode()
# ────────────────────────────────────────────────────

app            = Flask(__name__)
app.secret_key = SECRET_KEY
bcrypt         = Bcrypt(app)
login_manager  = LoginManager(app)
login_manager.login_view    = "login"
login_manager.login_message = "Please log in to access this page."

os.makedirs("data", exist_ok=True)
campaign_status = {}

@app.context_processor
def inject_user():
    try:
        return dict(current_user=current_user)
    except Exception:
        return dict(current_user=None)

# ══════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect("data/sent_log.db")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        plan TEXT DEFAULT 'free',
        emails_sent INTEGER DEFAULT 0,
        sender_email TEXT DEFAULT '',
        sender_app_password_enc TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    # Backfill columns for databases created before this feature existed
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "sender_email" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN sender_email TEXT DEFAULT ''")
    if "sender_app_password_enc" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN sender_app_password_enc TEXT DEFAULT ''")
    conn.execute("""CREATE TABLE IF NOT EXISTS sent_emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        email TEXT, name TEXT, company TEXT,
        subject TEXT, status TEXT,
        sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS opens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        email TEXT, token TEXT,
        opened_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn

# ══════════════════════════════════════════════════════
#  USER MODEL
# ══════════════════════════════════════════════════════
class User(UserMixin):
    def __init__(self, id, username, email, plan, emails_sent,
                 sender_email="", sender_app_password_enc=""):
        self.id                       = id
        self.username                 = username
        self.email                    = email
        self.plan                     = plan
        self.emails_sent              = emails_sent
        self.sender_email             = sender_email
        self.sender_app_password_enc  = sender_app_password_enc

    def has_email_configured(self):
        return bool(self.sender_email and self.sender_app_password_enc)

    def sender_app_password(self):
        return decrypt_secret(self.sender_app_password_enc)

    def email_limit(self):
        return {"free": 100, "starter": 500, "pro": 5000, "agency": 999999}.get(self.plan, 100)

    def can_send(self):
        return self.emails_sent < self.email_limit()

    def emails_left(self):
        return max(0, self.email_limit() - self.emails_sent)

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row  = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["username"], row["email"], row["plan"], row["emails_sent"],
                     row["sender_email"], row["sender_app_password_enc"])
    return None

# ══════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════
def log_result(conn, row, subject, status, user_id):
    conn.execute(
        "INSERT INTO sent_emails (user_id,email,name,company,subject,status) VALUES(?,?,?,?,?,?)",
        (user_id, row.get("email",""),
         (str(row.get("first_name",""))+" "+str(row.get("last_name",""))).strip(),
         row.get("company",""), subject, status)
    )
    if status == "sent":
        conn.execute("UPDATE users SET emails_sent = emails_sent + 1 WHERE id = ?", (user_id,))
    conn.commit()

def send_email_smtp(to, subject, body_html, sender_email, sender_password):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender_email
    msg["To"]      = to
    msg.attach(MIMEText(body_html, "html"))
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(sender_email, sender_password)
        s.send_message(msg)

def add_tracking(html_body, email, user_id):
    token = uuid.uuid4().hex
    pixel = f'<img src="{BASE_URL}/track?t={token}&e={email}&u={user_id}" width="1" height="1" style="display:none"/>'
    return html_body + pixel

def verify_email(email):
    email  = email.strip().lower()
    result = {"email":email,"valid_format":False,"has_mx":False,"deliverable":False,"score":0,"reason":""}
    if not re.match(r'^[\w.+-]+@[\w-]+\.[\w.]+$', email):
        result["reason"] = "Invalid format"
        return result
    result["valid_format"] = True
    domain = email.split("@")[1]
    try:
        mx      = dns.resolver.resolve(domain, 'MX')
        mx_host = str(sorted(mx, key=lambda r: r.preference)[0].exchange).rstrip('.')
        result["has_mx"] = True
    except:
        result["reason"] = "No MX record"
        result["score"]  = 20
        return result
    try:
        smtp = smtplib.SMTP(timeout=10)
        smtp.connect(mx_host, 25)
        smtp.helo('check.local')
        smtp.mail('verify@check.local')
        code, _ = smtp.rcpt(email)
        smtp.quit()
        if code == 250:
            result["deliverable"] = True
            result["reason"]      = "Mailbox exists"
            result["score"]       = 100
        else:
            result["reason"] = f"Mailbox rejected ({code})"
            result["score"]  = 40
    except smtplib.SMTPRecipientsRefused:
        result["reason"] = "Mailbox does not exist"
        result["score"]  = 10
    except Exception:
        result["reason"] = "Could not verify via SMTP"
        result["score"]  = 55
    return result

def run_campaign(leads, subject, body, delay_min, delay_max, user_id, sender_email, sender_password):
    global campaign_status
    campaign_status[user_id] = {"running":True,"log":[],"progress":0,"total":len(leads)}
    conn = get_db()
    for i, row in enumerate(leads):
        email = row.get("email","").strip()
        if not email or "@" not in email:
            log_result(conn, row, subject, "failed: invalid email", user_id)
            campaign_status[user_id]["log"].append({"status":"fail","text":f"{email or 'blank'} — invalid, skipped"})
            campaign_status[user_id]["progress"] = i + 1
            continue
        try:
            ps = subject.replace("{company}",str(row.get("company","")))\
                        .replace("{first_name}",str(row.get("first_name","")))\
                        .replace("{title}",str(row.get("title","")))
            pb = body.replace("{company}",str(row.get("company","")))\
                     .replace("{first_name}",str(row.get("first_name","")))\
                     .replace("{title}",str(row.get("title","")))
            html = add_tracking(pb.replace("\n","<br>"), email, user_id)
            send_email_smtp(email, ps, html, sender_email, sender_password)
            log_result(conn, row, ps, "sent", user_id)
            campaign_status[user_id]["log"].append({"status":"ok","text":f"{row.get('first_name','')} <{email}> — delivered"})
        except Exception as e:
            log_result(conn, row, subject, f"failed: {e}", user_id)
            campaign_status[user_id]["log"].append({"status":"fail","text":f"{email} — {str(e)[:80]}"})
        campaign_status[user_id]["progress"] = i + 1
        if i < len(leads) - 1:
            time.sleep(random.uniform(delay_min, delay_max))
    conn.close()
    campaign_status[user_id]["running"] = False

def ai_write_email(first_name, company, title, tone, goal):
    client   = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""Write a short cold outreach email:
- Recipient: {first_name}
- Company: {company}
- Job title: {title}
- Tone: {tone}
- Goal: {goal}

Rules:
- Max 100 words
- No subject line
- Sound human not AI
- End with clear call to action
- Do NOT start with I hope this email finds you well

Return only the email body, nothing else."""
        }]
    )
    return response.choices[0].message.content

# ══════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════
@app.route("/register", methods=["GET","POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username","").strip()
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        confirm  = request.form.get("confirm","")
        if not username or not email or not password:
            flash("All fields are required.", "error")
            return render_template("register.html")
        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return render_template("register.html")
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        hashed = bcrypt.generate_password_hash(password).decode("utf-8")
        try:
            conn = get_db()
            conn.execute("INSERT INTO users (username, email, password) VALUES (?,?,?)",
                         (username, email, hashed))
            conn.commit()
            row  = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            conn.close()
            user = User(row["id"], row["username"], row["email"], row["plan"], row["emails_sent"])
            login_user(user)
            flash(f"Welcome, {username}! Your account is ready.", "success")
            return redirect(url_for("dashboard"))
        except sqlite3.IntegrityError:
            flash("Username or email already exists.", "error")
            return render_template("register.html")
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        remember = request.form.get("remember") == "on"
        conn = get_db()
        row  = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        if row and bcrypt.check_password_hash(row["password"], password):
            user = User(row["id"], row["username"], row["email"], row["plan"], row["emails_sent"])
            login_user(user, remember=remember)
            next_page = request.args.get("next")
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(next_page or url_for("dashboard"))
        else:
            flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))

# ══════════════════════════════════════════════════════
#  PAGE ROUTES
# ══════════════════════════════════════════════════════
@app.route("/")
@login_required
def dashboard():
    conn      = get_db()
    rows      = conn.execute("SELECT * FROM sent_emails WHERE user_id=? ORDER BY sent_at DESC", (current_user.id,)).fetchall()
    opens     = conn.execute("SELECT COUNT(*) FROM opens WHERE user_id=?", (current_user.id,)).fetchone()[0]
    conn.close()
    total     = len(rows)
    delivered = len([r for r in rows if r["status"] == "sent"])
    failed    = len([r for r in rows if str(r["status"]).startswith("failed")])
    companies = len(set(r["company"] for r in rows if r["company"]))
    rate      = f"{(delivered/total*100):.0f}%" if total else "—"
    recent    = rows[:6]
    return render_template("dashboard.html", total=total, delivered=delivered,
        failed=failed, companies=companies, rate=rate, recent=recent,
        opens=opens, page="dashboard")

@app.route("/send-emails")
@login_required
def send_emails_page():
    return render_template("send_emails.html", page="send")

@app.route("/scraper")
@login_required
def scraper_page():
    return render_template("scraper.html", page="scraper")

@app.route("/verifier")
@login_required
def verifier_page():
    return render_template("verifier.html", page="verifier")

@app.route("/sent-log")
@login_required
def sent_log_page():
    status_filter = request.args.get("status","All")
    search        = request.args.get("search","")
    conn   = get_db()
    query  = "SELECT * FROM sent_emails WHERE user_id=?"
    params = [current_user.id]
    if status_filter != "All":
        query += " AND status=?"
        params.append(status_filter)
    if search:
        query += " AND (email LIKE ? OR company LIKE ?)"
        params.extend([f"%{search}%",f"%{search}%"])
    query += " ORDER BY sent_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template("sent_log.html", rows=rows, page="log",
        status_filter=status_filter, search=search)

@app.route("/opens")
@login_required
def opens_page():
    conn = get_db()
    rows = conn.execute("SELECT * FROM opens WHERE user_id=? ORDER BY opened_at DESC", (current_user.id,)).fetchall()
    conn.close()
    return render_template("opens.html", rows=rows, page="opens")

@app.route("/account")
@login_required
def account_page():
    return render_template("account.html", page="account",
        sender_email=current_user.sender_email,
        has_email_configured=current_user.has_email_configured())

@app.route("/api/save-email-config", methods=["POST"])
@login_required
def save_email_config():
    data            = request.get_json()
    sender_email    = data.get("sender_email","").strip().lower()
    sender_password = data.get("sender_app_password","").strip()
    if not sender_email or not re.match(r'^[\w.+-]+@[\w-]+\.[\w.]+$', sender_email):
        return jsonify({"error":"Enter a valid email address."}), 400
    if not sender_password or len(sender_password.replace(" ","")) < 16:
        return jsonify({"error":"That doesn't look like a valid Gmail App Password. Generate one at https://myaccount.google.com/apppasswords"}), 400
    encrypted = encrypt_secret(sender_password)
    conn = get_db()
    conn.execute("UPDATE users SET sender_email=?, sender_app_password_enc=? WHERE id=?",
                 (sender_email, encrypted, current_user.id))
    conn.commit()
    conn.close()
    return jsonify({"success":True})

@app.route("/api/remove-email-config", methods=["POST"])
@login_required
def remove_email_config():
    conn = get_db()
    conn.execute("UPDATE users SET sender_email='', sender_app_password_enc='' WHERE id=?",
                 (current_user.id,))
    conn.commit()
    conn.close()
    return jsonify({"success":True})


# ══════════════════════════════════════════════════════
#  API ROUTES
# ══════════════════════════════════════════════════════
@app.route("/api/upload-leads", methods=["POST"])
@login_required
def upload_leads():
    file = request.files.get("file")
    if not file:
        return jsonify({"error":"No file"}), 400
    stream = io.StringIO(file.stream.read().decode("utf-8"))
    leads  = list(csv.DictReader(stream))
    return jsonify({"leads":leads,"count":len(leads)})

@app.route("/api/ai-write-email", methods=["POST"])
@login_required
def ai_write_email_route():
    data       = request.get_json()
    first_name = data.get("first_name","there")
    company    = data.get("company","your company")
    title      = data.get("title","professional")
    tone       = data.get("tone","professional")
    goal       = data.get("goal","explore a collaboration opportunity")
    try:
        body = ai_write_email(first_name, company, title, tone, goal)
        return jsonify({"body": body})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/verify-single", methods=["POST"])
@login_required
def api_verify_single():
    data  = request.get_json()
    email = data.get("email","")
    return jsonify(verify_email(email))

@app.route("/api/verify-emails", methods=["POST"])
@login_required
def api_verify_emails():
    data    = request.get_json()
    emails  = data.get("emails",[])
    results = [verify_email(e) for e in emails]
    return jsonify({"results":results})

@app.route("/api/launch-campaign", methods=["POST"])
@login_required
def launch_campaign():
    if not current_user.can_send():
        return jsonify({"error":"Email limit reached. Upgrade your plan."}), 403
    if not current_user.has_email_configured():
        return jsonify({"error":"Connect your Gmail account in Account settings before sending campaigns."}), 400
    data      = request.get_json()
    leads     = data.get("leads",[])
    subject   = data.get("subject","")
    body      = data.get("body","")
    delay_min = float(data.get("delay_min",3))
    delay_max = float(data.get("delay_max",8))
    sender_email    = current_user.sender_email
    sender_password = current_user.sender_app_password()
    threading.Thread(target=run_campaign,
        args=(leads,subject,body,delay_min,delay_max,current_user.id,sender_email,sender_password)).start()
    return jsonify({"started":True})

@app.route("/api/campaign-status")
@login_required
def get_campaign_status():
    status = campaign_status.get(current_user.id,{"running":False,"log":[],"progress":0,"total":0})
    return jsonify(status)

@app.route("/api/scrape", methods=["POST"])
@login_required
def scrape():
    from bs4 import BeautifulSoup
    data    = request.get_json()
    urls    = data.get("urls",[])
    results = []
    log     = []
    for url in urls:
        try:
            res   = requests.get(url,timeout=10,headers={"User-Agent":"Mozilla/5.0"})
            soup  = BeautifulSoup(res.text,"html.parser")
            emails = list(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+",soup.get_text())))
            clean  = [e for e in emails if not any(x in e.lower() for x in
                      ['example','test','png','jpg','gif','css','js','svg','woff'])]
            for email in clean:
                results.append({"first_name":"Team","last_name":"","email":email,
                    "company":url.split("//")[-1].split("/")[0].replace("www.",""),"title":"Manager"})
            log.append({"status":"ok","text":f"{url} — {len(clean)} found"})
        except Exception as e:
            log.append({"status":"fail","text":f"{url} — {e}"})
    return jsonify({"results":results,"log":log})

@app.route("/api/download-csv", methods=["POST"])
@login_required
def download_csv():
    data = request.get_json()
    rows = data.get("rows",[])
    if not rows:
        return jsonify({"error":"No data"}), 400
    output = io.StringIO()
    writer = csv.DictWriter(output,fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem,mimetype="text/csv",as_attachment=True,download_name="leads.csv")

@app.route("/sent-log/export")
@login_required
def export_sent_log():
    conn = get_db()
    rows = conn.execute("SELECT * FROM sent_emails WHERE user_id=? ORDER BY sent_at DESC",
                        (current_user.id,)).fetchall()
    conn.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","email","name","company","subject","status","sent_at"])
    writer.writerows(rows)
    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)
    return send_file(mem,mimetype="text/csv",as_attachment=True,download_name="sent_log.csv")

@app.route("/track")
def track():
    token   = request.args.get("t","")
    email   = request.args.get("e","")
    user_id = request.args.get("u","")
    conn    = get_db()
    conn.execute("INSERT INTO opens (user_id,email,token) VALUES(?,?,?)",(user_id,email,token))
    conn.commit()
    conn.close()
    pixel = (b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff'
              b'\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00\x2c\x00\x00\x00\x00'
              b'\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b')
    return send_file(io.BytesIO(pixel),mimetype="image/gif")

if __name__ == "__main__":
    get_db()
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port       = int(os.environ.get("PORT", 5050))
    print(f"🚀 PyExtract running at {BASE_URL}")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)