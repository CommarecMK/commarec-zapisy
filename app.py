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

def build_system_prompt(template, client_info, blocks):
    """Builds dynamic system prompt based on selected blocks and client info."""
    
    client_section = ""
    if any([client_info.get('client_name'), client_info.get('meeting_date')]):
        client_section = f"""
Na začátku zápisu uveď hlavičku:
Zápis ze schůzky: {client_info.get('meeting_date', 'neuvedeno')}
Zastoupení Commarec: {client_info.get('commarec_rep', 'neuvedeno')}
Zastoupení klienta: {client_info.get('client_contact', 'neuvedeno')} ({client_info.get('client_name', 'neuvedeno')})
Místo: {client_info.get('meeting_place', 'neuvedeno')}
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
Rozděl do tří fází:
Krátkodobé (0–1 měsíc): konkrétní, okamžitě realizovatelné kroky
Střednědobé (1–3 měsíce): systémové změny, implementace
Dlouhodobé (3+ měsíců): strategické kroky, digitalizace, automatizace

DŮLEŽITÉ: Za touto sekcí přidej oddělený blok:
---ÚKOLY PRO FREELO---
(každý konkrétní úkol na nový řádek ve formátu:)
ÚKOL: [název úkolu] | POPIS: [stručný popis co udělat] | TERMÍN: [termín nebo "dle dohody"]
(vypiš pouze krátkodobé a střednědobé úkoly, max 8 úkolů)""",
        
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
    
    if not selected_blocks and 'kroky' not in blocks:
        # Always need to extract tasks even if block not shown
        selected_blocks += """
---ÚKOLY PRO FREELO---
ÚKOL: [název] | POPIS: [detail] | TERMÍN: [termín]"""

    base = f"""Jsi expertní asistent společnosti Commarec pro tvorbu profesionálních zápisů z diagnostických návštěv, obchodních schůzek a porad.

Tvůj styl: odborný, ale lidský. Žádné korporátní fráze ani zbytečné omáčky. Konkrétní, strukturovaný, čitelný.
FORMÁTOVÁNÍ - používej PŘESNĚ takto:
- Nadpisy sekcí pište VELKÝMI PÍSMENY bez speciálních znaků (např. HLAVNÍ ZJIŠTĚNÍ)
- Podnadpisy uváděj na samostatném řádku s dvojtečkou na konci (např. Profil firmy:)
- Odrážky: začínaj řádek znakem "• " (bullet + mezera)
- Pro tabulky použij formát: Oblast | Hodnocení | Komentář (oddělené svislítky)
- NIKDY nepoužívej **hvězdičky**, _podtržítka_, #hashtag nebo jiné markdown znaky
- NIKDY nepoužívej emotikony
- Citace klienta dej do "uvozovek"

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
