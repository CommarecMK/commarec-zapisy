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
app.config["JSON_AS_ASCII"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

db = SQLAlchemy(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
FREELO_API_KEY = os.environ.get("FREELO_API_KEY", "")
FREELO_EMAIL   = os.environ.get("FREELO_EMAIL", "")
FREELO_PROJECT_ID = os.environ.get("FREELO_PROJECT_ID", "501350")

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

def build_system_prompt(template, client_info, blocks):
    """Builds dynamic system prompt based on selected blocks and client info."""
    
    client_name = client_info.get('client_name', '').strip()
    client_contact = client_info.get('client_contact', '').strip()
    commarec_rep = client_info.get('commarec_rep', '').strip()
    meeting_date = client_info.get('meeting_date', '').strip()
    meeting_place = client_info.get('meeting_place', '').strip()

    client_section = f"""
ZAČNI ZÁPIS PŘESNĚ TÍMTO BLOKEM (nic před tím):
ZÁPIS ZE SCHŮZKY: {meeting_date}
Zastoupení Commarec: {commarec_rep}
Zastoupení klienta: {client_contact} ({client_name})
Místo: {meeting_place}
---
"""

    block_map = {
        'uvod': """
ÚVOD A ÚČEL NÁVŠTĚVY
- Shrň proč se návštěva/schůzka konala a co bylo jejím cílem
- Uveď jaké procesy nebo oblasti byly pozorovány/diskutovány
- Zakončit větou: "Audit se zaměřil na efektivitu procesů, plánování, využití kapacit, ergonomii a úroveň standardizace." (nebo přizpůsobit kontextu)""",
        
        'zjisteni': """
SHRNUTÍ HLAVNÍCH ZJIŠTĚNÍ
- Použij odrážky (•) pro přehlednost
- Uveď nejdůležitější fakta ze schůzky
- Odděl POZITIVNÍ zjištění a RIZIKA/PROBLÉMY
- Buď konkrétní, používej čísla pokud zazněla""",
        
        'hodnoceni': """
HODNOCENÍ HLAVNÍCH OBLASTÍ
Vytvoř tabulku s hodnocením (0-100%) ve formátu:
Oblast | Hodnocení (%) | Komentář
- Vyber relevantní oblasti podle kontextu (logistika, výroba, IT, obchod...)
- Na konci uveď: Celkové skóre, Nejlepší oblasti, Nejkritičtější oblasti, Klíčová priorita""",
        
        'procesy': """
POPIS PROCESŮ A VIZUÁLNÍ POZOROVÁNÍ
- Rozděl na logické sekce podle toho co bylo pozorováno/diskutováno
- Popiš konkrétně co bylo vidět nebo slyšet
- Uveď silné stránky i slabiny každé oblasti
- Pokud zazněly citace klienta, dej je do uvozovek a kurzívy""",
        
        'rizika': """
KLÍČOVÉ PROBLÉMY A RIZIKA
- Použij krátké, silné body s dopadem
- Formát: Problém → důsledek/riziko
- Řaď od nejkritičtějšího""",
        
        'kroky': """
DOPORUČENÉ AKČNÍ KROKY
Krátkodobé (0–1 měsíc):
• [konkrétní krok 1]
• [konkrétní krok 2]
Střednědobé (1–3 měsíce):
• [krok 1]
Dlouhodobé (3+ měsíců):
• [krok 1]

POVINNÉ: Na úplný konec zápisu přidej tento blok PŘESNĚ v tomto formátu (každý úkol na nový řádek):
---ÚKOLY PRO FREELO---
ÚKOL: [název] | POPIS: [co udělat] | TERMÍN: [termín]
ÚKOL: [název] | POPIS: [co udělat] | TERMÍN: [termín]
ÚKOL: [název] | POPIS: [co udělat] | TERMÍN: [termín]

PRAVIDLA PRO ÚKOLY:
- Vycházej z Krátkodobých a Střednědobých kroků výše
- Týkají se VÝHRADNĚ práce Commarec: optimalizace skladu/logistiky/pickování/WMS/ERP/datová analýza/procesní audit
- min. 3, max. 8 úkolů
- NEZAPOMEŇ tento blok přidat — je povinný""",
        
        'prinosy': """
OČEKÁVANÉ PŘÍNOSY
- Uveď konkrétní kvantifikované přínosy (%)
- Ke každému přínosu přidej krátké vysvětlení proč
- Příklady: snížení backlogu, zvýšení produktivity, úspora času, stabilizace výkonu""",
        
        'poznamky': """
POZNÁMKY Z TERÉNU
- Volná sekce pro osobní postřehy
- Přístup lidí, atmosféra, komentáře vedoucích
- Spontánní nápady nebo překvapení""",
        
        'dalsi_krok': """
DALŠÍ KROK SPOLUPRÁCE
- Shrň co bylo dohodnuto jako next step
- Uveď termíny pokud zazněly
- Co Commarec připraví / pošle klientovi"""
    }
    
    selected_blocks = "\n".join([block_map[b] for b in ['uvod','zjisteni','hodnoceni','procesy','rizika','kroky','prinosy','poznamky','dalsi_krok'] if b in blocks])
    
    # ALWAYS append task extraction at the end - every record must have tasks
    if 'kroky' not in blocks:
        selected_blocks += """

DOPORUČENÉ AKČNÍ KROKY (zkrácená verze)
• Uveď 3-5 nejdůležitějších kroků které vyplývají ze schůzky

DŮLEŽITÉ: Na konci přidej tento blok (povinný vždy):
---ÚKOLY PRO FREELO---
(každý konkrétní úkol na nový řádek ve formátu:)
ÚKOL: [název úkolu] | POPIS: [stručný popis co udělat] | TERMÍN: [termín nebo "dle dohody"]
(min. 3, max. 8 úkolů — vždy z každé schůzky vzniknou úkoly)"""

    base = f"""Jsi expertní asistent společnosti Commarec pro tvorbu profesionálních zápisů z diagnostických návštěv, obchodních schůzek a porad.

Tvůj styl: odborný, ale lidský. Žádné korporátní fráze ani zbytečné omáčky. Konkrétní, strukturovaný, čitelný.

KRITICKÉ PRAVIDLO FORMÁTOVÁNÍ — ABSOLUTNÍ ZÁKAZ:
NIKDY NEPOUŽIVEJ HTML. Tedy žádné: <span>, <strong>, <b>, <div>, <p>, style=, font-weight:, color:#173767 ani žádné jiné HTML tagy nebo inline CSS. Pokud by ses pokusil napsat cokoliv začínající < nebo obsahující style=, font-weight, color:# — ZASTAV a napiš čistý text místo toho.

Výstup musí být 100% čistý prostý text bez jakéhokoli HTML nebo markdown.

FORMÁTOVÁNÍ - používej PŘESNĚ takto:
- Nadpisy sekcí: VELKÝMI PÍSMENY na samostatném řádku (např. HLAVNÍ ZJIŠTĚNÍ)
- Podnadpisy: Na samostatném řádku s dvojtečkou na konci (např. Pozitivní zjištění:)
- Odrážky: začínaj řádek znakem "• " (bullet + mezera)
- Tabulky: Oblast | Hodnocení | Komentář (oddělené svislítky |)
- Citace klienta: jen do "uvozovek"
- Oddělení sekcí: ---
- NIKDY: **hvězdičky**, _podtržítka_, #hashtag, emotikony, HTML tagy

{client_section}

Vytvoř zápis s těmito sekcemi (v tomto pořadí):
{selected_blocks}

Pokud data v přepisu nejsou úplná, doplň rozumné odhady na základě kontextu.
Pokud zazněla čísla (počty lidí, dny backlogu, rozměry, termíny), zahrň je do výsledku.
Nepiš žádný úvod, rovnou začni zápisem."""

    return base


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

def split_transcript(text, max_chars=8000):
    if len(text) <= max_chars:
        return [text]
    parts = []
    current = ""
    paragraphs = text.split("\n\n")
    for para in paragraphs:
        if len(current) + len(para) > max_chars and current:
            parts.append(current.strip())
            current = para
        else:
            current += "\n\n" + para if current else para
    if current.strip():
        parts.append(current.strip())
    return parts

@app.route("/api/generovat", methods=["POST"])
@login_required
def generovat():
    data = request.json
    template = data.get("template", "audit")
    input_text = data.get("text", "").strip()
    if not input_text:
        return jsonify({"error": "Prázdný text"}), 400

    client_info = data.get("client_info", {})
    blocks = set(client_info.get("blocks", ["uvod","zjisteni","hodnoceni","procesy","rizika","kroky","prinosy","poznamky","dalsi_krok"]))
    system_prompt = build_system_prompt(template, client_info, blocks)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    parts_text = split_transcript(input_text, max_chars=8000)

    try:
        if len(parts_text) == 1:
            message = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=3000,
                system=system_prompt,
                messages=[{"role": "user", "content": input_text}]
            )
            full_text = message.content[0].text
        else:
            summaries = []
            for i, part in enumerate(parts_text):
                part_msg = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=1500,
                    system=f"Jsi asistent pro analýzu přepisů schůzek. Toto je část {i+1} z {len(parts_text)} přepisu schůzky. Vyextrahuj VŠECHNY klíčové body, rozhodnutí, čísla, problémy, úkoly a důležité informace. Zachovej citace. Piš strukturovaně v češtině.",
                    messages=[{"role": "user", "content": part}]
                )
                summaries.append(f"=== ČÁST {i+1}/{len(parts_text)} ===\n{part_msg.content[0].text}")
            combined = "\n\n".join(summaries)
            final_prompt = f"Na základě těchto shrnutí jednotlivých částí schůzky vytvoř kompletní profesionální zápis:\n\n{combined}"
            message = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=3000,
                system=system_prompt,
                messages=[{"role": "user", "content": final_prompt}]
            )
            full_text = message.content[0].text
    except Exception as e:
        return jsonify({"error": f"Chyba API: {str(e)}"}), 500

    import re as re2

    # Step 1: Try to split on task marker
    ukoly_markers = [
        "---ÚKOLY PRO FREELO---", "--- ÚKOLY PRO FREELO ---",
        "---UKOLY PRO FREELO---", "ÚKOLY PRO FREELO:", "ÚKOLY PRO FREELO",
        "UKOLY PRO FREELO", "---ÚKOLY---",
    ]
    parts = None
    for marker in ukoly_markers:
        if marker in full_text:
            parts = full_text.split(marker, 1)
            break

    zapis_text = parts[0].strip() if parts else full_text.strip()
    tasks = []

    # Step 2: Parse tasks from marker block if present
    if parts and len(parts) > 1:
        for line in parts[1].strip().split("\n"):
            line = line.strip()
            if not line or len(line) < 4:
                continue
            if line.upper() == line and len(line) < 40 and "|" not in line:
                continue
            if "ÚKOL:" in line or "Úkol:" in line or "ukol:" in line.lower():
                ukol_m  = re2.search(r"[ÚúUu]kol:\s*([^|\n]+)",      line, re2.IGNORECASE)
                popis_m = re2.search(r"[Pp]opis:\s*([^|\n]+)",        line)
                term_m  = re2.search(r"[Tt]erm[ií]n:\s*([^|\n]+)",   line)
                name = ukol_m.group(1).strip() if ukol_m else line[:150]
                tasks.append({
                    "name": name[:200],
                    "desc": popis_m.group(1).strip() if popis_m else "",
                    "deadline": term_m.group(1).strip() if term_m else "dle dohody"
                })
            elif re2.match(r"^[•\-–\*\d]", line) and len(line) > 8:
                name = re2.sub(r"^[•\-–\*0-9\.]+\s*", "", line).strip()
                if name:
                    tasks.append({"name": name[:200], "desc": "", "deadline": "dle dohody"})

    # Step 3: If still no tasks — extract directly from DOPORUČENÉ AKČNÍ KROKY section
    if not tasks:
        # Find the action steps section in the zapis text
        akcni_match = re2.search(
            r'(DOPORUČENÉ AKČNÍ KROKY|AKČNÍ KROKY)(.*?)(?=\n[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ\s]{3,}\n|$)',
            zapis_text, re2.DOTALL | re2.IGNORECASE
        )
        if akcni_match:
            akcni_block = akcni_match.group(2)
            # Extract bullets — skip phase headers (Krátkodobé, Střednědobé, Dlouhodobé)
            phase_re = re2.compile(r'^(KRÁTKODOBÉ|STŘEDNĚDOBÉ|DLOUHODOBÉ|Krátkodobé|Střednědobé|Dlouhodobé)', re2.IGNORECASE)
            current_deadline = "dle dohody"
            for line in akcni_block.split('\n'):
                line = line.strip()
                if not line:
                    continue
                # Detect phase header for deadline hint
                if phase_re.match(line):
                    if '0' in line or '1 měs' in line.lower():
                        current_deadline = "do 1 měsíce"
                    elif '3' in line:
                        current_deadline = "do 3 měsíců"
                    else:
                        current_deadline = "dle dohody"
                    continue
                # Extract bullet points
                if re2.match(r'^[•\-–\*]\s+.{8,}', line):
                    name = re2.sub(r'^[•\-–\*]\s+', '', line).strip()
                    # Skip very long lines that are descriptions not tasks
                    if name and len(name) < 200:
                        tasks.append({"name": name[:200], "desc": "", "deadline": current_deadline})
                        if len(tasks) >= 8:
                            break

    # Step 4: If still nothing — dedicated AI JSON extraction
    if not tasks:
        try:
            task_prompt = f"""Z tohoto zápisu vytáhni konkrétní akční kroky pro tým Commarec.
Zaměř se na: optimalizaci skladu, logistiku, WMS/ERP implementaci, procesní audit, datovou analýzu.
Odpověz POUZE jako JSON pole, bez jakéhokoli dalšího textu:
[
  {{"name": "Název úkolu", "desc": "Stručný popis", "deadline": "do 1 měsíce"}},
  ...
]
Min. 3, max. 8 úkolů.

ZÁPIS:
{zapis_text[:5000]}"""
            task_msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=800,
                messages=[{"role": "user", "content": task_prompt}]
            )
            raw_json = task_msg.content[0].text.strip()
            raw_json = re2.sub(r"^```[\w]*\n?", "", raw_json)
            raw_json = re2.sub(r"\n?```$", "", raw_json).strip()
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                tasks = [{"name": str(t.get("name",""))[:200],
                          "desc": str(t.get("desc","")),
                          "deadline": str(t.get("deadline","dle dohody"))} for t in parsed if t.get("name")]
                app.logger.info(f"AI task extraction: {len(tasks)} tasks")
        except Exception as te:
            app.logger.error(f"Task extraction failed: {te}")

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

def freelo_auth():
    """Freelo Basic Auth: email as username, API key as password."""
    return (FREELO_EMAIL, FREELO_API_KEY)

def freelo_get(path):
    """Helper: GET from Freelo API"""
    return requests.get(
        f"https://api.freelo.io/v1{path}",
        auth=freelo_auth(),
        headers={"Content-Type": "application/json"},
        timeout=15
    )

def freelo_post(path, payload):
    """Helper: POST to Freelo API"""
    return requests.post(
        f"https://api.freelo.io/v1{path}",
        auth=freelo_auth(),
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=15
    )

def freelo_find_project_id():
    """Find correct project ID - first try config ID, then list all projects."""
    resp = freelo_get(f"/projects/{FREELO_PROJECT_ID}/tasklists")
    if resp.status_code == 200:
        return FREELO_PROJECT_ID, None

    # Config ID doesn't work - get real ID from projects list
    resp2 = freelo_get("/projects")
    if resp2.status_code != 200:
        return None, f"Nelze načíst projekty: {resp2.status_code}"
    projects = resp2.json()
    if isinstance(projects, list) and projects:
        pid = str(projects[0]["id"])
        app.logger.info(f"Using project id={pid} name={projects[0].get('name')}")
        return pid, None
    return None, "Žádné projekty nenalezeny"

@app.route("/api/freelo/projects", methods=["GET"])
@login_required
def get_freelo_projects():
    """Returns all Freelo projects with their embedded tasklists."""
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"projects": [], "error": "Chybí FREELO_API_KEY nebo FREELO_EMAIL"})
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
        app.logger.info(f"Freelo projects: {len(result)}")
        return jsonify({"projects": result})
    except Exception as e:
        app.logger.error(f"Freelo projects error: {e}")
        return jsonify({"projects": [], "error": str(e)})

@app.route("/api/freelo/tasklists", methods=["GET"])
@login_required
def get_freelo_tasklists():
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"tasklists": [], "error": "Chybí FREELO_API_KEY nebo FREELO_EMAIL v Railway Variables"})
    try:
        # Get projects — tasklists are embedded directly in response
        resp = freelo_get("/projects")
        if resp.status_code != 200:
            return jsonify({"tasklists": [], "error": f"Freelo {resp.status_code}: {resp.text[:100]}"})

        projects = resp.json()
        if not isinstance(projects, list) or not projects:
            return jsonify({"tasklists": [], "error": "Žádné projekty v účtu"})

        # Find matching project or use first one
        project = next((p for p in projects if str(p.get("id")) == str(FREELO_PROJECT_ID)), projects[0])
        tasklists = [{"id": tl["id"], "name": tl["name"]}
                     for tl in project.get("tasklists", []) if "id" in tl]

        # If no tasklists in embedded data, call dedicated endpoint
        if not tasklists:
            resp2 = freelo_get(f"/projects/{project['id']}/tasklists")
            if resp2.status_code == 200:
                data = resp2.json()
                items = data.get("data", data) if isinstance(data, dict) else data
                tasklists = [{"id": tl["id"], "name": tl["name"]} for tl in items if "id" in tl]

        app.logger.info(f"Freelo project={project['id']} name={project.get('name')} tasklists={len(tasklists)}")
        return jsonify({"tasklists": tasklists})
    except Exception as e:
        app.logger.error(f"Freelo tasklists error: {e}")
        return jsonify({"tasklists": [], "error": str(e)})




@app.route("/api/freelo/debug-tasklist/<tasklist_id>", methods=["GET"])
@login_required  
def debug_tasklist(tasklist_id):
    """Debug: test task creation endpoint"""
    # Find project_id for this tasklist
    project_id_found = None
    resp_p = freelo_get("/projects")
    if resp_p.status_code == 200:
        for proj in resp_p.json():
            for tl in proj.get("tasklists", []):
                if str(tl.get("id")) == str(tasklist_id):
                    project_id_found = proj["id"]
                    break
    resp2 = freelo_get(f"/tasklist/{tasklist_id}")
    return jsonify({
        "tasklist_id": tasklist_id,
        "project_id_found": project_id_found,
        "correct_create_endpoint": f"POST /projects/{project_id_found}/tasklists/{tasklist_id}/tasks",
        "GET_tasklist_status": resp2.status_code,
        "GET_tasklist_body": resp2.text[:300],
    })

@app.route("/api/freelo/debug", methods=["GET"])
@login_required
def freelo_debug():
    """Debug endpoint - tests Freelo API authentication"""
    result = {
        "api_key_set": bool(FREELO_API_KEY),
        "api_key_prefix": FREELO_API_KEY[:8] + "..." if FREELO_API_KEY else None,
        "email_set": bool(FREELO_EMAIL),
        "email": FREELO_EMAIL if FREELO_EMAIL else "CHYBÍ — přidej FREELO_EMAIL do Railway Variables",
        "project_id_in_config": FREELO_PROJECT_ID,
        "auth_method": "email + api_key (Basic Auth)",
    }
    if not FREELO_API_KEY or not FREELO_EMAIL:
        result["problem"] = "Chybí FREELO_EMAIL nebo FREELO_API_KEY v Railway Variables"
        return jsonify(result)
    for ep in ["/projects", f"/project/{FREELO_PROJECT_ID}/tasklists"]:
        try:
            resp = freelo_get(ep)
            result[f"test_{ep}"] = {"status": resp.status_code, "body": resp.text[:400]}
        except Exception as e:
            result[f"test_{ep}"] = {"error": str(e)}
    return jsonify(result)


@app.route("/api/freelo/members/<int:project_id>", methods=["GET"])
@login_required
def get_freelo_members(project_id):
    """Returns project members for assignee dropdown."""
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"members": []})
    try:
        members = []
        # Correct plural endpoint per official PHP SDK
        for path in [f"/projects/{project_id}/workers",
                     f"/projects/{project_id}/users",
                     f"/project/{project_id}/workers"]:
            resp = freelo_get(path)
            app.logger.info(f"Members {path}: {resp.status_code} {resp.text[:300]}")
            if resp.status_code == 200:
                data = resp.json()
                workers = data if isinstance(data, list) else data.get("data", [])
                for w in workers:
                    if not isinstance(w, dict): continue
                    fullname = (w.get("fullname") or w.get("name") or
                                f"{w.get('firstname','')} {w.get('lastname','')}".strip() or
                                w.get("username",""))
                    if fullname:
                        members.append({"id": w.get("id"), "name": fullname, "email": w.get("email","")})
                if members:
                    break

        # Fallback: get current user from /users/me
        if not members:
            me = freelo_get("/users/me")
            app.logger.info(f"Users/me: {me.status_code} {me.text[:200]}")
            if me.status_code == 200:
                u = me.json()
                if isinstance(u, dict):
                    fullname = u.get("fullname") or u.get("name") or FREELO_EMAIL
                    members.append({"id": u.get("id"), "name": fullname, "email": u.get("email", FREELO_EMAIL)})

        # Always ensure current user is in the list
        if FREELO_EMAIL and not any(m.get("email") == FREELO_EMAIL for m in members):
            members.insert(0, {"id": None, "name": FREELO_EMAIL.split("@")[0].replace(".", " ").title(), "email": FREELO_EMAIL})

        app.logger.info(f"Members found: {len(members)}")
        return jsonify({"members": members})
    except Exception as e:
        app.logger.error(f"Members error: {e}")
        # Return at least the current user
        return jsonify({"members": [{"id": None, "name": FREELO_EMAIL.split("@")[0].replace(".", " ").title(), "email": FREELO_EMAIL}]})


@app.route("/api/freelo/create-tasklist", methods=["POST"])
@login_required
def create_freelo_tasklist():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "Chybí název"}), 400
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"error": "Chybí FREELO_API_KEY nebo FREELO_EMAIL"}), 500
    try:
        # Accept project_id from request body, fall back to config
        req_project_id = (request.json or {}).get("project_id")
        if req_project_id:
            project_id = str(req_project_id)
        else:
            project_id, err = freelo_find_project_id()
            if err or not project_id:
                return jsonify({"error": err or "Projekt nenalezen"}), 400
        resp = freelo_post(f"/projects/{project_id}/tasklists", {"name": name})
        app.logger.info(f"Create tasklist status={resp.status_code} body={resp.text[:200]}")
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
    zapis = Zapis.query.get_or_404(zapis_id)
    data = request.json or {}
    selected_tasks = data.get("tasks", [])
    tasklist_id = data.get("tasklist_id")

    if not selected_tasks:
        return jsonify({"error": "Zadne ukoly k odeslani"}), 400

    if not tasklist_id:
        return jsonify({"error": "Vyberte nebo vytvořte To-Do list ve Freelu"}), 400

    import re as re3

    # Get project_id for the tasklist — needed for correct endpoint
    # First try to find project from our cached projects list
    project_id_for_tasks = None
    try:
        resp_projects = freelo_get("/projects")
        if resp_projects.status_code == 200:
            for proj in resp_projects.json():
                for tl in proj.get("tasklists", []):
                    if str(tl.get("id")) == str(tasklist_id):
                        project_id_for_tasks = proj["id"]
                        break
                if project_id_for_tasks:
                    break
    except Exception:
        pass

    if not project_id_for_tasks:
        project_id_for_tasks = FREELO_PROJECT_ID

    created = []
    errors = []
    for task in selected_tasks:
        name = task.get("name", "")
        if not name:
            continue
        payload = {"name": name}

        # Description as note
        if task.get("desc"):
            payload["note"] = task["desc"]

        # Deadline: convert to YYYY-MM-DD
        deadline = (task.get("deadline") or "").strip()
        if deadline and deadline.lower() != "dle dohody":
            if re3.match(r"\d{4}-\d{2}-\d{2}", deadline):
                payload["due_date"] = deadline
            elif re3.match(r"\d{1,2}\.\d{1,2}\.\d{4}", deadline):
                parts2 = deadline.replace(" ", "").split(".")
                payload["due_date"] = f"{parts2[2]}-{parts2[1].zfill(2)}-{parts2[0].zfill(2)}"

        try:
            # Correct Freelo endpoint per official PHP SDK:
            # POST /projects/{projectId}/tasklists/{tasklistId}/tasks
            resp = freelo_post(
                f"/projects/{project_id_for_tasks}/tasklists/{tasklist_id}/tasks",
                payload
            )
            app.logger.info(f"Task '{name}': status={resp.status_code} body={resp.text[:200]}")

            if resp.status_code in (200, 201):
                created.append(name)
                # Add assignee as comment
                assignee = (task.get("assignee") or "").strip()
                if assignee:
                    task_data = resp.json()
                    task_id = (task_data.get("data") or task_data).get("id")
                    if task_id:
                        freelo_post(f"/task/{task_id}/comments",
                                    {"comment": f"Zodpovědná osoba: {assignee}"})
            else:
                errors.append(f"{name}: {resp.text[:100]}")
        except Exception as e:
            errors.append(f"{name}: {str(e)}")

    if created:
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

# --- Leaderboard ---

@app.route("/api/leaderboard", methods=["GET"])
@login_required
def get_leaderboard():
    import json as json_mod
    try:
        scores = json_mod.loads(open("/tmp/leaderboard.json").read()) if __import__("os").path.exists("/tmp/leaderboard.json") else []
    except:
        scores = []
    return jsonify(scores)

@app.route("/api/leaderboard", methods=["POST"])
@login_required
def post_leaderboard():
    import json as json_mod, os
    data = request.json
    score = int(data.get("score", 0))
    name = session.get("user_name", "?")
    try:
        scores = json_mod.loads(open("/tmp/leaderboard.json").read()) if os.path.exists("/tmp/leaderboard.json") else []
    except:
        scores = []
    # Update or insert
    existing = next((s for s in scores if s["name"] == name), None)
    if existing:
        if score > existing["score"]:
            existing["score"] = score
    else:
        scores.append({"name": name, "score": score})
    scores.sort(key=lambda x: x["score"], reverse=True)
    scores = scores[:10]
    with open("/tmp/leaderboard.json", "w") as f:
        json_mod.dump(scores, f)
    return jsonify({"ok": True})

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
