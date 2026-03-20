from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from functools import wraps
import anthropic
import requests
import os
import json
import re

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")

database_url = os.environ.get("DATABASE_URL", "sqlite:///zapisy.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["JSON_AS_ASCII"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

db = SQLAlchemy(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FREELO_API_KEY    = os.environ.get("FREELO_API_KEY", "")
FREELO_EMAIL      = os.environ.get("FREELO_EMAIL", "")
FREELO_PROJECT_ID = os.environ.get("FREELO_PROJECT_ID", "501350")

# ---------------------------------------------
# MODELS
# ---------------------------------------------

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    name          = db.Column(db.String(80),  nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin      = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    zapisy        = db.relationship("Zapis", backref="author", lazy=True)

class Zapis(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    template    = db.Column(db.String(50),  nullable=False)
    input_text  = db.Column(db.Text, nullable=False)
    # JSON string of the structured summary (12 sections from cmrc02)
    output_json = db.Column(db.Text, nullable=False, default="{}")
    # Rendered text for display / PDF (assembled from output_json)
    output_text = db.Column(db.Text, nullable=False, default="")
    tasks_json  = db.Column(db.Text, default="[]")
    freelo_sent = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

TEMPLATE_NAMES = {
    "audit":     "Audit / diagnostika",
    "operativa": "Operativn- sch-zka",
    "obchod":    "Obchodn- sch-zka",
}

# ---------------------------------------------
# SYSTEM PROMPT - inspired by cmrc02
# ---------------------------------------------

SYSTEM_PROMPT = """
Pom-h-- odborn-mu konzultantovi firmy Commarec sepsat profesion-ln- z-pis ze sch-zky s klientem.
Jsi specialista na diagnostiku logistiky, skladov-ho hospod--stv-, optimalizaci proces-, WMS/ERP a Supply Chain.

Tv-m -kolem je p-ev-st vstupn- p-epis a pozn-mky na strukturovan- JSON report podle pevn- dan-ho sch-matu.

### COMMAREC METODIKA
Commarec je logistick- poradensk- firma. Nav-t-vujeme sklady klient-, identifikujeme probl-my v procesech
a navrhujeme konkr-tn- zlep-en-. V-stupem je profesion-ln- z-pis s hodnocen-m a ak-n-mi kroky.

### PRAVIDLA
- V-e p--e- v -e-tin-, v-cn-, bez korpor-tn-ch fr-z- a om--ek.
- Konkr-tn- fakta, --sla, citace klienta - v-echno co zazn-lo, zahrni.
- Kde chyb- data, dopl- realistick- odhady na z-klad- kontextu logistiky.
- Ka-d- sekce = -ist- HTML (bez hlavn-ho nadpisu sekce - ten p-id- aplikace sama).
- Odr--ky v-dy jako <ul><li></li></ul>, tabulky jako <table>, d-le-it- v-ci <strong>.
- NIKDY nepou-i inline styly, --dn- style=, font-weight:, color: atributy.
- Kl--ov- fakta a citace klienta dej do <em> nebo uvozovek.

### JSON V-STUP - vra- POUZE tento JSON, nic jin-ho:
{
  "participants_commarec": "HTML - kdo byl za Commarec",
  "participants_company":  "HTML - kdo byl za klienta",
  "introduction":          "HTML - -vod, pro- se sch-zka konala, co bylo c-lem",
  "meeting_goal":          "HTML - konkr-tn- c-l n-v-t-vy",
  "findings":              "HTML - hlavn- zji-t-n- (pozitiva i rizika)",
  "ratings":               "HTML - tabulka hodnocen- oblast- 0-100%, celkov- sk-re",
  "processes_description": "HTML - popis proces- jak skute-n- funguj-",
  "dangers":               "HTML - kl--ov- probl-my a rizika s dopadem",
  "suggested_actions":     "HTML - ak-n- kroky (kr-tkodob-/st-edn-dob-/dlouhodob-)",
  "expected_benefits":     "HTML - kvantifikovan- p--nosy v % s vysv-tlen-m",
  "additional_notes":      "HTML - post-ehy z ter-nu, atmosf-ra, p-ekvapen-",
  "summary":               "HTML - stru-n- z-v-re-n- shrnut- s kl--ov-mi prioritami",
  "tasks": [
    {"name": "N-zev -kolu", "desc": "Co konkr-tn- ud-lat", "deadline": "YYYY-MM-DD nebo textov- term-n"}
  ]
}

### PRAVIDLA PRO TASKS
- Min. 3, max. 8 -kol-.
- -koly se t-kaj- V-HRADN- pr-ce Commarec: optimalizace skladu, logistika, picking, WMS/ERP, datov- anal-za, procesn- audit.
- Vych-zej z suggested_actions - kr-tkodob- a st-edn-dob- kroky.
- Ka-d- -kol mus- m-t konkr-tn- n-zev (max 100 znak-) a popis.
- deadline: pokud zazn-l konkr-tn- datum, pou-ij ho (YYYY-MM-DD), jinak textov- term-n jako "do 1 m-s-ce".

### RATINGS TABULKA - form-t:
<table>
  <tr><th>Oblast</th><th>Hodnocen- (%)</th><th>Koment--</th></tr>
  <tr><td>Procesn- dokumentace</td><td>45</td><td>Chyb- standardy...</td></tr>
  ...
  <tr><td colspan="3"><strong>Celkov- sk-re: 55%</strong> | Nejlep--: X | Nejkriti-t-j--: Y</td></tr>
</table>
"""

SECTION_TITLES = {
    "participants_commarec": "Zastoupen- Commarec",
    "participants_company":  "Zastoupen- klienta",
    "introduction":          "-vod",
    "meeting_goal":          "--el n-v-t-vy",
    "findings":              "Shrnut- hlavn-ch zji-t-n-",
    "ratings":               "Hodnocen- hlavn-ch oblast-",
    "processes_description": "Popis proces- a vizu-ln- pozorov-n-",
    "dangers":               "Kl--ov- probl-my a rizika",
    "suggested_actions":     "Doporu-en- ak-n- kroky",
    "expected_benefits":     "O-ek-van- p--nosy",
    "additional_notes":      "Pozn-mky z ter-nu",
    "summary":               "Shrnut-",
}

SECTION_ORDER = list(SECTION_TITLES.keys())

def build_header_html(client_info):
    """Build the client/meeting header block."""
    return f"""<div class="zapis-header-block">
<strong>Datum:</strong> {client_info.get('meeting_date','')}<br>
<strong>Zastoupen- Commarec:</strong> {client_info.get('commarec_rep','')}<br>
<strong>Zastoupen- klienta:</strong> {client_info.get('client_contact','')} ({client_info.get('client_name','')})<br>
<strong>M-sto:</strong> {client_info.get('meeting_place','')}
</div>"""

def assemble_output_text(client_info, summary_json, blocks):
    """Assemble full HTML output from structured JSON sections."""
    parts = []
    # Header
    parts.append(build_header_html(client_info))
    # Sections in order, only selected blocks
    block_to_section = {
        'uvod':      ['introduction', 'meeting_goal'],
        'zjisteni':  ['findings'],
        'hodnoceni': ['ratings'],
        'procesy':   ['processes_description'],
        'rizika':    ['dangers'],
        'kroky':     ['suggested_actions'],
        'prinosy':   ['expected_benefits'],
        'poznamky':  ['additional_notes'],
        'dalsi_krok':['summary'],
    }
    # Flatten selected sections in order
    selected_sections = []
    for block in ['uvod','zjisteni','hodnoceni','procesy','rizika','kroky','prinosy','poznamky','dalsi_krok']:
        if block in blocks:
            for sec in block_to_section.get(block, []):
                if sec not in selected_sections:
                    selected_sections.append(sec)
    for sec in selected_sections:
        content = summary_json.get(sec, "")
        if content:
            title = SECTION_TITLES.get(sec, sec.upper())
            parts.append(f'<section data-key="{sec}"><h2 class="section-title">{title.upper()}</h2>{content}</section>')
    return "\n".join(parts)

def condensed_transcript(client, transcript):
    """Shorten transcript if too long - mirrors cmrc02 createCondensedTranscript."""
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": f"""Zkondenzuj tento p-epis sch-zky. 
Zachovej V-ECHNY d-le-it- informace: jm-na, --sla, probl-my, -e-en-, citace, procesy.
Odstra- jen opakov-n- a zbyte-n- zdvo-ilostn- fr-ze.
V-sledek mus- b-t srozumiteln- a kompletn- - min. 5 stran.

P-EPIS:
{transcript}"""}]
    )
    return msg.content[0].text

# ---------------------------------------------
# AUTH
# ---------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or not user.is_admin:
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------
# ROUTES
# ---------------------------------------------

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_id"]   = user.id
            session["user_name"] = user.name
            session["is_admin"]  = user.is_admin
            return redirect(url_for("dashboard"))
        error = "Nespr-vn- e-mail nebo heslo."
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
    summary = json.loads(zapis.output_json or "{}")
    return render_template("detail.html", zapis=zapis, tasks=tasks,
                           summary=summary, section_titles=SECTION_TITLES,
                           template_names=TEMPLATE_NAMES)

# ---------------------------------------------
# API - GENERATE
# ---------------------------------------------

@app.route("/api/generovat", methods=["POST"])
@login_required
def generovat():
    data        = request.json
    template    = data.get("template", "audit")
    input_text  = data.get("text", "").strip()
    client_info = data.get("client_info", {})
    blocks      = set(client_info.get("blocks", [
        "uvod","zjisteni","hodnoceni","procesy","rizika","kroky","prinosy","poznamky","dalsi_krok"
    ]))

    if not input_text:
        return jsonify({"error": "Pr-zdn- text"}), 400

    ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Condense long transcripts (>12000 chars ~ 3000 tokens)
    transcript = input_text
    if len(input_text) > 12000:
        try:
            transcript = condensed_transcript(ai, input_text)
            app.logger.info(f"Transcript condensed: {len(input_text)} - {len(transcript)} chars")
        except Exception as e:
            app.logger.warning(f"Condensation failed, using original: {e}")
            transcript = input_text

    # Build context for the AI
    client_context = f"""
Klient: {client_info.get('client_name', '')}
Kontaktn- osoba klienta: {client_info.get('client_contact', '')}
Za Commarec: {client_info.get('commarec_rep', '')}
Datum sch-zky: {client_info.get('meeting_date', '')}
M-sto: {client_info.get('meeting_place', '')}
Typ sch-zky: {TEMPLATE_NAMES.get(template, template)}
"""

    user_message = f"""
INFORMACE O SCH-ZCE:
{client_context}

P-EPIS / POZN-MKY:
{transcript}

Vytvo- strukturovan- JSON z-pis podle sch-matu. Vra- POUZE validn- JSON, --dn- jin- text.
"""

    try:
        message = ai.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )
        raw = message.content[0].text.strip()
    except Exception as e:
        return jsonify({"error": f"Chyba API: {str(e)}"}), 500

    # Parse JSON - strip markdown fences if present
    raw = re.sub(r"^```[\w]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw).strip()

    try:
        summary_json = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: try to extract JSON from response
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                summary_json = json.loads(m.group())
            except:
                return jsonify({"error": "AI vr-tilo nevalidn- JSON. Zkus znovu."}), 500
        else:
            return jsonify({"error": "AI vr-tilo nevalidn- JSON. Zkus znovu."}), 500

    # Extract tasks from JSON (no more regex!)
    tasks = []
    raw_tasks = summary_json.pop("tasks", [])
    if isinstance(raw_tasks, list):
        for t in raw_tasks:
            if isinstance(t, dict) and t.get("name"):
                tasks.append({
                    "name":     str(t.get("name", ""))[:200],
                    "desc":     str(t.get("desc", "")),
                    "deadline": str(t.get("deadline", "dle dohody")),
                })

    # Assemble display text from structured JSON
    output_text = assemble_output_text(client_info, summary_json, blocks)

    # Build title from client + date
    client_name   = client_info.get("client_name", "").strip()
    meeting_date  = client_info.get("meeting_date", "").strip()
    title = f"{client_name} - {meeting_date}" if client_name else f"Z-pis {meeting_date}"

    # Save to DB
    zapis = Zapis(
        title=title,
        template=template,
        input_text=input_text,
        output_json=json.dumps(summary_json, ensure_ascii=False),
        output_text=output_text,
        tasks_json=json.dumps(tasks, ensure_ascii=False),
        user_id=session["user_id"]
    )
    db.session.add(zapis)
    db.session.commit()

    return jsonify({
        "zapis_id": zapis.id,
        "text":     output_text,
        "tasks":    tasks,
        "title":    title,
        "summary":  summary_json,
    })

# ---------------------------------------------
# FREELO HELPERS
# ---------------------------------------------

def freelo_auth():
    return (FREELO_EMAIL, FREELO_API_KEY)

def freelo_get(path):
    return requests.get(
        f"https://api.freelo.io/v1{path}",
        auth=freelo_auth(),
        headers={"Content-Type": "application/json"},
        timeout=15
    )

def freelo_post(path, payload):
    return requests.post(
        f"https://api.freelo.io/v1{path}",
        auth=freelo_auth(),
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=15
    )

# ---------------------------------------------
# FREELO API ENDPOINTS
# ---------------------------------------------

@app.route("/api/freelo/projects", methods=["GET"])
@login_required
def get_freelo_projects():
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"projects": [], "error": "Chyb- FREELO_API_KEY nebo FREELO_EMAIL"})
    try:
        resp = freelo_get("/projects")
        if resp.status_code != 200:
            return jsonify({"projects": [], "error": f"Freelo {resp.status_code}: {resp.text[:100]}"})
        raw = resp.json()
        projects = raw if isinstance(raw, list) else raw.get("data", [])
        result = [
            {
                "id": p["id"],
                "name": p.get("name", f"Projekt {p['id']}"),
                "tasklists": [
                    {"id": tl["id"], "name": tl.get("name", f"List {tl['id']}")}
                    for tl in p.get("tasklists", [])
                ]
            }
            for p in projects if isinstance(p, dict) and "id" in p
        ]
        return jsonify({"projects": result})
    except Exception as e:
        return jsonify({"projects": [], "error": str(e)})

@app.route("/api/freelo/members/<int:project_id>", methods=["GET"])
@login_required
def get_freelo_members(project_id):
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"members": []})
    try:
        resp = freelo_get(f"/project/{project_id}/workers")
        app.logger.info(f"Workers {project_id}: {resp.status_code} {resp.text[:200]}")
        members = []
        if resp.status_code == 200:
            workers = resp.json().get("data", {}).get("workers", [])
            for w in workers:
                if not isinstance(w, dict): continue
                fullname = w.get("fullname") or w.get("name") or ""
                if fullname:
                    members.append({"id": w["id"], "name": fullname, "email": w.get("email", "")})
        return jsonify({"members": members})
    except Exception as e:
        app.logger.error(f"Members error: {e}")
        return jsonify({"members": []})

@app.route("/api/freelo/create-tasklist", methods=["POST"])
@login_required
def create_freelo_tasklist():
    req   = request.json or {}
    name  = req.get("name", "").strip()
    pid   = str(req.get("project_id", FREELO_PROJECT_ID))
    if not name:
        return jsonify({"error": "Chyb- n-zev"}), 400
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"error": "Chyb- Freelo credentials"}), 500
    try:
        resp = freelo_post(f"/project/{pid}/tasklists", {"name": name})
        app.logger.info(f"Create tasklist: {resp.status_code} {resp.text[:200]}")
        if resp.status_code in (200, 201):
            data = resp.json()
            tl = data.get("data", data)
            if isinstance(tl, list): tl = tl[0]
            return jsonify({"id": tl["id"], "name": tl["name"]})
        return jsonify({"error": f"Freelo {resp.status_code}: {resp.text[:100]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/freelo/<int:zapis_id>", methods=["POST"])
@login_required
def odeslat_do_freela(zapis_id):
    zapis          = Zapis.query.get_or_404(zapis_id)
    data           = request.json or {}
    selected_tasks = data.get("tasks", [])
    tasklist_id    = data.get("tasklist_id")

    if not selected_tasks:
        return jsonify({"error": "--dn- -koly k odesl-n-"}), 400
    if not tasklist_id:
        return jsonify({"error": "Vyberte To-Do list"}), 400

    # Find project_id for this tasklist
    project_id_for_tasks = FREELO_PROJECT_ID
    try:
        resp_p = freelo_get("/projects")
        if resp_p.status_code == 200:
            for proj in resp_p.json():
                for tl in proj.get("tasklists", []):
                    if str(tl.get("id")) == str(tasklist_id):
                        project_id_for_tasks = proj["id"]
                        break
    except Exception:
        pass

    # Load members for worker_id resolution
    members_by_name = {}
    try:
        m_resp = freelo_get(f"/project/{project_id_for_tasks}/workers")
        if m_resp.status_code == 200:
            for w in m_resp.json().get("data", {}).get("workers", []):
                if w.get("fullname"):
                    members_by_name[w["fullname"].lower()] = w["id"]
    except Exception:
        pass

    created = []
    errors  = []

    for task in selected_tasks:
        name = task.get("name", "").strip()
        if not name:
            continue

        payload  = {"name": name}
        assignee = (task.get("assignee") or "").strip()
        deadline = (task.get("deadline") or "").strip()

        if task.get("desc"):
            payload["description"] = task["desc"]

        if assignee:
            worker_id = members_by_name.get(assignee.lower())
            if worker_id:
                payload["worker_id"] = worker_id

        if deadline and deadline.lower() not in ("dle dohody", ""):
            if re.match(r"\d{4}-\d{2}-\d{2}", deadline):
                payload["due_date"] = deadline
            elif re.match(r"\d{1,2}\.\d{1,2}\.\d{4}", deadline):
                p = deadline.replace(" ", "").split(".")
                payload["due_date"] = f"{p[2]}-{p[1].zfill(2)}-{p[0].zfill(2)}"

        try:
            resp = freelo_post(
                f"/project/{project_id_for_tasks}/tasklist/{tasklist_id}/tasks",
                payload
            )
            app.logger.info(f"Task '{name}': {resp.status_code} {resp.text[:200]}")

            if resp.status_code in (200, 201):
                created.append(name)
                task_data = resp.json()
                task_id   = (task_data.get("data") or task_data).get("id")
                if task_id:
                    desc = (task.get("desc") or "").strip()
                    if desc:
                        freelo_post(f"/task/{task_id}/description", {"description": desc})
                    if assignee and not members_by_name.get(assignee.lower()):
                        freelo_post(f"/task/{task_id}/comments",
                                    {"comment": f"Zodpov-dn- osoba: {assignee}"})
            else:
                errors.append(f"{name}: {resp.text[:100]}")
        except Exception as e:
            errors.append(f"{name}: {str(e)}")

    if created:
        zapis.freelo_sent = True
        db.session.commit()

    return jsonify({"created": created, "errors": errors})

@app.route("/api/freelo/debug", methods=["GET"])
@login_required
def freelo_debug():
    result = {
        "api_key_set":   bool(FREELO_API_KEY),
        "api_key_prefix": FREELO_API_KEY[:8] + "..." if FREELO_API_KEY else None,
        "email_set":     bool(FREELO_EMAIL),
        "email":         FREELO_EMAIL or "CHYB-",
    }
    if FREELO_API_KEY and FREELO_EMAIL:
        for ep in ["/projects", f"/project/{FREELO_PROJECT_ID}/tasklists"]:
            try:
                r = freelo_get(ep)
                result[f"test_{ep}"] = {"status": r.status_code, "body": r.text[:300]}
            except Exception as e:
                result[f"test_{ep}"] = {"error": str(e)}
    return jsonify(result)

# ---------------------------------------------
# ADMIN
# ---------------------------------------------

@app.route("/admin")
@admin_required
def admin():
    users = User.query.all()
    return render_template("admin.html", users=users)

@app.route("/admin/pridat-uzivatele", methods=["POST"])
@admin_required
def pridat_uzivatele():
    email    = request.form.get("email", "").strip().lower()
    name     = request.form.get("name", "").strip()
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

# ---------------------------------------------
# DB INIT
# ---------------------------------------------

with app.app_context():
    try:
        db.create_all()
        if not User.query.filter_by(is_admin=True).first():
            db.session.add(User(
                email="admin@commarec.cz",
                name="Admin",
                password_hash=generate_password_hash("admin123"),
                is_admin=True
            ))
            db.session.commit()
            print("Vytvo-en v-choz- admin: admin@commarec.cz / admin123")
    except Exception as e:
        print(f"DB init error: {e}")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
