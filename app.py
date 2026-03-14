from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import anthropic
import requests
import os
import json
import re

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")

# Fix Railway PostgreSQL URL (postgres:// -> postgresql://)
database_url = os.environ.get("DATABASE_URL", "sqlite:///zapisy.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

db = SQLAlchemy(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FREELO_API_KEY = os.environ.get("FREELO_API_KEY", "")
FREELO_PROJECT_ID = os.environ.get("FREELO_PROJECT_ID", "582553")

# --- Models ---

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    zapisy = db.relationship("Zapis", backref="author", lazy=True)

class Zapis(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    template = db.Column(db.String(50), nullable=False)
    input_text = db.Column(db.Text, nullable=False)
    output_text = db.Column(db.Text, nullable=False)
    tasks_json = db.Column(db.Text, default="[]")
    freelo_sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

# --- System prompts ---

SYSTEM_PROMPTS = {
    "audit": """Jsi asistent pro tvorbu zápisů z konzultačních a auditorských schůzek firmy Commarec. Na základě přepisu nebo poznámek vytvoř strukturovaný profesionální zápis v češtině s těmito sekcemi:

ZÁPIS ZE SCHŮZKY – [název projektu/klienta]
Datum: [datum pokud je uvedeno, jinak neuvedeno]
Účastníci: [pokud jsou uvedeni]

ÚVOD A ÚČEL
[stručný popis účelu schůzky, 2-3 věty]

HLAVNÍ ZJIŠTĚNÍ
[klíčové poznatky jako odrážky s tučným názvem a popisem]

DOPORUČENÉ KROKY
Krátkodobé (0–1 měsíc):
1. [úkol] | Odpovědný: [osoba nebo "neurčeno"] | Termín: [termín nebo "dle dohody"]

Střednědobé (1–3 měsíce):
1. [úkol]

ZÁVĚR
[stručné shrnutí, 2-3 věty]

Na konci přidej sekci oddělenou řádkem ---ÚKOLY PRO FREELO---:
ÚKOL: [název úkolu] | POPIS: [stručný popis] | TERMÍN: [termín]
(jeden řádek na úkol, pouze krátkodobé/konkrétní akční kroky)

Piš profesionálně, věcně a stručně. Odpovídej pouze zápisem bez komentářů.""",

    "operativa": """Jsi asistent pro tvorbu zápisů z operativních porad. Na základě přepisu vytvoř stručný přehledný zápis v češtině:

ZÁPIS Z OPERATIVNÍ PORADY
Datum: [datum]
Účastníci: [účastníci]

PROBRANÉ BODY
1. [bod jednání a jeho výsledek]

ROZHODNUTÍ
- [přijaté rozhodnutí]

ÚKOLY
- [úkol] | Odpovědný: [osoba] | Termín: [termín]

PŘÍŠTÍ SCHŮZKA
[termín pokud je uveden]

---ÚKOLY PRO FREELO---
ÚKOL: [název] | POPIS: [detail] | TERMÍN: [termín]

Piš stručně a věcně. Odpovídej pouze zápisem.""",

    "obchod": """Jsi asistent pro tvorbu zápisů z obchodních schůzek. Na základě přepisu vytvoř profesionální obchodní zápis v češtině:

ZÁPIS Z OBCHODNÍ SCHŮZKY
Datum: [datum]
Klient: [název klienta]
Účastníci: [účastníci]

KONTEXT A CÍL SCHŮZKY
[shrnutí situace a cíle, 2-3 věty]

ZÁVĚRY Z JEDNÁNÍ
- [klíčový závěr nebo informace]

NEXT STEPS
1. [akce] | Odpovědný: [osoba] | Termín: [termín]

FOLLOW-UP
[co je potřeba sledovat nebo připravit]

---ÚKOLY PRO FREELO---
ÚKOL: [název] | POPIS: [detail] | TERMÍN: [termín]

Piš profesionálně. Odpovídej pouze zápisem."""
}

TEMPLATE_NAMES = {
    "audit": "Audit / diagnostika",
    "operativa": "Operativní schůzka",
    "obchod": "Obchodní schůzka"
}

# --- Auth helpers ---

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or not user.is_admin:
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

# --- Routes ---

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            session["user_name"] = user.name
            session["is_admin"] = user.is_admin
            return redirect(url_for("dashboard"))
        error = "Nesprávný e-mail nebo heslo."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    zapisy = Zapis.query.order_by(Zapis.created_at.desc()).limit(20).all()
    return render_template("dashboard.html", zapisy=zapisy, template_names=TEMPLATE_NAMES)

@app.route("/novy")
@login_required
def novy_zapis():
    return render_template("novy.html", template_names=TEMPLATE_NAMES)

@app.route("/zapis/<int:zapis_id>")
@login_required
def detail_zapisu(zapis_id):
    zapis = Zapis.query.get_or_404(zapis_id)
    tasks = json.loads(zapis.tasks_json or "[]")
    return render_template("detail.html", zapis=zapis, tasks=tasks, template_names=TEMPLATE_NAMES)

@app.route("/api/generovat", methods=["POST"])
@login_required
def generovat():
    data = request.json
    template = data.get("template", "audit")
    input_text = data.get("text", "").strip()
    if not input_text:
        return jsonify({"error": "Prázdný text"}), 400

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            system=SYSTEM_PROMPTS.get(template, SYSTEM_PROMPTS["audit"]),
            messages=[{"role": "user", "content": input_text}]
        )
        full_text = message.content[0].text
    except Exception as e:
        return jsonify({"error": f"Chyba API: {str(e)}"}), 500

    parts = full_text.split("---ÚKOLY PRO FREELO---")
    zapis_text = parts[0].strip()
    tasks = []
    if len(parts) > 1:
        for line in parts[1].strip().split("\n"):
            if "ÚKOL:" in line:
                ukol_m = re.search(r"ÚKOL:\s*([^|]+)", line)
                popis_m = re.search(r"POPIS:\s*([^|]+)", line)
                termin_m = re.search(r"TERMÍN:\s*(.+)", line)
                tasks.append({
                    "name": ukol_m.group(1).strip() if ukol_m else line,
                    "desc": popis_m.group(1).strip() if popis_m else "",
                    "deadline": termin_m.group(1).strip() if termin_m else "dle dohody"
                })

    first_line = zapis_text.split("\n")[0].replace("ZÁPIS ZE SCHŮZKY –", "").replace("ZÁPIS ZE SCHŮZKY -", "").strip()
    title = first_line[:100] if first_line else "Zápis ze schůzky"

    zapis = Zapis(
        title=title,
        template=template,
        input_text=input_text,
        output_text=zapis_text,
        tasks_json=json.dumps(tasks, ensure_ascii=False),
        user_id=session["user_id"]
    )
    db.session.add(zapis)
    db.session.commit()

    return jsonify({"zapis_id": zapis.id, "text": zapis_text, "tasks": tasks, "title": title})

@app.route("/api/freelo/<int:zapis_id>", methods=["POST"])
@login_required
def odeslat_do_freela(zapis_id):
    zapis = Zapis.query.get_or_404(zapis_id)
    tasks = json.loads(zapis.tasks_json or "[]")
    if not tasks:
        return jsonify({"error": "Žádné úkoly k odeslání"}), 400

    headers = {"Content-Type": "application/json"}
    auth = ("apikey", FREELO_API_KEY)

    try:
        tl_resp = requests.get(
            f"https://api.freelo.io/v1/project/{FREELO_PROJECT_ID}/tasklists",
            auth=auth, headers=headers, timeout=10
        )
        tl_data = tl_resp.json()
        tasklist_id = tl_data["data"][0]["id"] if tl_data.get("data") else None
    except Exception as e:
        return jsonify({"error": f"Chyba Freelo API: {str(e)}"}), 500

    if not tasklist_id:
        return jsonify({"error": "Nenalezen žádný seznam úkolů ve Freelo projektu"}), 400

    created = []
    errors = []
    for task in tasks:
        payload = {"name": task["name"], "comment": task.get("desc", "")}
        try:
            resp = requests.post(
                f"https://api.freelo.io/v1/tasklist/{tasklist_id}/tasks",
                auth=auth, headers=headers, json=payload, timeout=10
            )
            if resp.status_code in (200, 201):
                created.append(task["name"])
            else:
                errors.append(f"{task['name']}: {resp.text[:100]}")
        except Exception as e:
            errors.append(f"{task['name']}: {str(e)}")

    zapis.freelo_sent = True
    db.session.commit()
    return jsonify({"created": created, "errors": errors})

@app.route("/admin")
@admin_required
def admin():
    users = User.query.all()
    return render_template("admin.html", users=users)

@app.route("/admin/pridat-uzivatele", methods=["POST"])
@admin_required
def pridat_uzivatele():
    email = request.form.get("email", "").strip().lower()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    is_admin = bool(request.form.get("is_admin"))
    if User.query.filter_by(email=email).first():
        return redirect(url_for("admin"))
    user = User(
        email=email, name=name,
        password_hash=generate_password_hash(password),
        is_admin=is_admin
    )
    db.session.add(user)
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/admin/smazat-uzivatele/<int:user_id>", methods=["POST"])
@admin_required
def smazat_uzivatele(user_id):
    if user_id == session["user_id"]:
        return redirect(url_for("admin"))
    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for("admin"))

# --- DB Init (runs on every startup) ---

with app.app_context():
    try:
        db.create_all()
        if not User.query.filter_by(is_admin=True).first():
            admin_user = User(
                email="admin@commarec.cz",
                name="Admin",
                password_hash=generate_password_hash("admin123"),
                is_admin=True
            )
            db.session.add(admin_user)
            db.session.commit()
            print("Vytvořen výchozí admin: admin@commarec.cz / admin123")
    except Exception as e:
        print(f"DB init error: {e}")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
