from flask import Flask, render_template, request, jsonify, session, redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from functools import wraps
import anthropic
import requests
import os, json, re, secrets, string

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

# ─────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────

class Klient(db.Model):
    __tablename__ = "klient"
    id          = db.Column(db.Integer, primary_key=True)
    nazev       = db.Column(db.String(200), nullable=False)
    slug        = db.Column(db.String(200), unique=True, nullable=False)
    kontakt     = db.Column(db.String(200), default="")   # hlavni kontaktni osoba
    email       = db.Column(db.String(200), default="")
    telefon     = db.Column(db.String(60),  default="")
    adresa      = db.Column(db.String(300), default="")
    poznamka    = db.Column(db.Text, default="")
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    # profil skladu (JSON) — automaticky extrahovan z prepisu
    profil_json = db.Column(db.Text, default="{}")
    projekty    = db.relationship("Projekt", back_populates="klient", lazy=True, cascade="all, delete-orphan")
    zapisy      = db.relationship("Zapis", lazy=True, foreign_keys="Zapis.klient_id", viewonly=True)

class Projekt(db.Model):
    __tablename__ = "projekt"
    id          = db.Column(db.Integer, primary_key=True)
    nazev       = db.Column(db.String(200), nullable=False)
    popis       = db.Column(db.Text, default="")
    klient_id   = db.Column(db.Integer, db.ForeignKey("klient.id"), nullable=False)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)  # prirazeny konzultant
    datum_od    = db.Column(db.Date, nullable=True)
    datum_do    = db.Column(db.Date, nullable=True)
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    konzultant  = db.relationship("User", backref="user_projekty", foreign_keys=[user_id])
    klient      = db.relationship("Klient", foreign_keys=[klient_id], back_populates="projekty", lazy="joined")
    zapisy      = db.relationship("Zapis", lazy=True, foreign_keys="Zapis.projekt_id", viewonly=True)

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    name          = db.Column(db.String(80),  nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin      = db.Column(db.Boolean, default=False)
    is_active     = db.Column(db.Boolean, default=True)
    role          = db.Column(db.String(40), default="konzultant")  # superadmin | admin | konzultant
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    zapisy        = db.relationship("Zapis", backref="author", lazy=True, foreign_keys="Zapis.user_id")

class Zapis(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    title           = db.Column(db.String(200), nullable=False)
    template        = db.Column(db.String(50),  nullable=False)
    input_text      = db.Column(db.Text, nullable=False)
    output_json     = db.Column(db.Text, nullable=True,  default="{}")
    output_text     = db.Column(db.Text, nullable=False, default="")
    tasks_json      = db.Column(db.Text, default="[]")
    # Notes — structured field notes before generating (JSON list of {title, text})
    notes_json      = db.Column(db.Text, default="[]")
    # Internal prompt — special AI instructions (highest priority)
    interni_prompt  = db.Column(db.Text, default="")
    freelo_sent     = db.Column(db.Boolean, default=False)
    # Public link
    public_token    = db.Column(db.String(40), nullable=True, unique=True)
    is_public       = db.Column(db.Boolean, default=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    user_id         = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    klient_id       = db.Column(db.Integer, db.ForeignKey("klient.id"), nullable=True)
    projekt_id      = db.Column(db.Integer, db.ForeignKey("projekt.id"), nullable=True)
    klient          = db.relationship("Klient", foreign_keys=[klient_id], lazy="joined", overlaps="zapisy,klient_ref")
    projekt         = db.relationship("Projekt", foreign_keys=[projekt_id], lazy="joined", overlaps="zapisy,klient")

TEMPLATE_NAMES = {
    "audit":     "Audit / diagnostika",
    "operativa": "Operativni schuzka",
    "obchod":    "Obchodni schuzka",
}

SECTION_TITLES = {
    "participants_commarec": "Zastoupeni Commarec",
    "participants_company":  "Zastoupeni klienta",
    "introduction":          "Uvod",
    "meeting_goal":          "Ucel navstevy",
    "findings":              "Shrn. hlavnich zjisteni",
    "ratings":               "Hodnoceni hlavnich oblasti",
    "processes_description": "Popis procesu",
    "dangers":               "Klicove problemy a rizika",
    "suggested_actions":     "Doporucene akcni kroky",
    "expected_benefits":     "Ocekavane prinosy",
    "additional_notes":      "Poznamky z terenu",
    "summary":               "Shrnuti",
}

# ─────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────

SYSTEM_PROMPT_BASE = """
Pomahes odbornemu konzultantovi firmy Commarec sepsat profesionalni zapisy ze schuzky s klientem.
Jsi specialista na diagnostiku logistiky, skladoveho hospodarstvi, optimalizaci procesu, WMS/ERP a Supply Chain.

Tvym ukolem je prevest vstupni prepis a poznamky na strukturovany JSON report.

### COMMAREC METODIKA
Commarec je logisticka poradenska firma. Navstevujeme sklady klientu, identifikujeme problemy v procesech
a navrhujeme konkretni zlepseni. Vystupem je profesionalni zapis s hodnocenim a akcnimi kroky.

### PRAVIDLA
- Vse pises v cestine, vecne, bez korporatnich frazi.
- Konkretni fakta, cisla, citace klienta — vse co zaznelo, zahrni.
- Kde chybi data, dopln realisticke odhady na zaklade kontextu logistiky.
- Kazda sekce = ciste HTML bez hlavniho nadpisu sekce.
- Odrazky vzdy jako <ul><li></li></ul>, tabulky jako <table>, dulezite veci <strong>.
- NIKDY nepouzi inline styly, zadne style=, font-weight:, color: atributy.

### JSON VYSTUP — vrat POUZE tento JSON, nic jineho:
{
  "participants_commarec": "HTML",
  "participants_company":  "HTML",
  "introduction":          "HTML",
  "meeting_goal":          "HTML",
  "findings":              "HTML",
  "ratings":               "HTML — tabulka hodnoceni oblasti 0-100%",
  "processes_description": "HTML",
  "dangers":               "HTML",
  "suggested_actions":     "HTML — akcni kroky kratko/stredne/dlouhodobe",
  "expected_benefits":     "HTML — kvantifikovane prinosy v %",
  "additional_notes":      "HTML",
  "summary":               "HTML — stucne zavrecne shrnuti",
  "tasks": [
    {"name": "Nazev ukolu max 100 znaku", "desc": "Co konkretne udelat", "deadline": "YYYY-MM-DD nebo textovy termin"}
  ]
}

### PRAVIDLA PRO TASKS
- Min. 3, max. 8 ukolu.
- Ukoly se tykaji VYHRADNE prace Commarec: optimalizace skladu, logistika, picking, WMS/ERP, datova analyza, procesni audit.
- Vycházej z suggested_actions — kratkodobe a stredodobe kroky.
- deadline: pokud zaznelo konkretni datum, pouzij YYYY-MM-DD, jinak textovy termin.

### RATINGS TABULKA — format:
<table>
  <tr><th>Oblast</th><th>Hodnoceni (%)</th><th>Komentar</th></tr>
  <tr><td>Procesni dokumentace</td><td>45</td><td>Chybi standardy...</td></tr>
  <tr><td colspan="3"><strong>Celkove skore: 55%</strong> | Nejlepsi: X | Nejkriticketjsi: Y</td></tr>
</table>
"""

def build_system_prompt(interni_prompt="", klient_profil=None):
    prompt = SYSTEM_PROMPT_BASE
    if klient_profil:
        prompt += f"\n\n### PROFIL KLIENTA (kontext pro zapis):\n{json.dumps(klient_profil, ensure_ascii=False)}"
    if interni_prompt and interni_prompt.strip():
        prompt += f"\n\n### INTERNI PROMPT (nejvyssi priorita — splnit na 100%):\n{interni_prompt.strip()}"
    return prompt

def build_header_html(client_info):
    return f"""<div class="zapis-header-block">
<strong>Datum:</strong> {client_info.get('meeting_date','')}<br>
<strong>Zastoupeni Commarec:</strong> {client_info.get('commarec_rep','')}<br>
<strong>Zastoupeni klienta:</strong> {client_info.get('client_contact','')} ({client_info.get('client_name','')})<br>
<strong>Misto:</strong> {client_info.get('meeting_place','')}
</div>"""

def assemble_output_text(client_info, summary_json, blocks):
    parts = [build_header_html(client_info)]
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
    selected = []
    for block in ['uvod','zjisteni','hodnoceni','procesy','rizika','kroky','prinosy','poznamky','dalsi_krok']:
        if block in blocks:
            for sec in block_to_section.get(block, []):
                if sec not in selected:
                    selected.append(sec)
    for sec in selected:
        content = summary_json.get(sec, "")
        if content:
            title = SECTION_TITLES.get(sec, sec.upper())
            parts.append(f'<section data-key="{sec}"><h2 class="section-title">{title.upper()}</h2>{content}</section>')
    return "\n".join(parts)

def condensed_transcript(ai_client, transcript):
    msg = ai_client.messages.create(
        model="claude-sonnet-4-5", max_tokens=4000,
        messages=[{"role": "user", "content": f"""Zkondenzuj tento prepis schuzky.
Zachovej VSECHNY dulezite informace: jmena, cisla, problemy, reseni, citace, procesy.
Odstran jen opakovani a zbytecne zdvorilostni fraze.
Vysledek musi byt srozumitelny a kompletni.

PREPIS:
{transcript}"""}])
    return msg.content[0].text

def extract_klient_profil(ai_client, text, existing=None):
    """Extract/update client profile data from transcript."""
    current = json.dumps(existing or {}, ensure_ascii=False)
    msg = ai_client.messages.create(
        model="claude-sonnet-4-5", max_tokens=1000,
        messages=[{"role": "user", "content": f"""Z tohoto prepisu schuzky vytahni NOVE informace o klientovi.
Vrat POUZE JSON s novymi nebo zmenenenymi hodnotami. Pokud informaci nemas, vrat null pro to pole.

AKTUALNI DATA: {current}

DOSTUPNA POLE:
- typ_skladu: typ skladu (distribuci, vyrobni, komisionalni...)
- pocet_sku: pocet SKU (cislo)
- metody_pickingu: metody kompletace (batch, single, zone...)
- pocet_zamestnanci: pocet lidi ve skladu
- pocet_smen: 1, 2 nebo 3
- wms_system: nazev WMS pokud pouzivaji
- prumerna_denni_expedice: kusy/objednavky za den
- hlavni_problemy: hlavni problemy klienta (string)
- specialni_pozadavky: specificke pozadavky klienta

TEXT:
{text[:5000]}

Vrat jen JSON, zadny jiny text."""}])
    raw = msg.content[0].text.strip()
    raw = re.sub(r'^```[\w]*\n?', '', raw)
    raw = re.sub(r'\n?```$', '', raw).strip()
    try:
        new_data = json.loads(raw)
        merged = dict(existing or {})
        for k, v in new_data.items():
            if v is not None:
                merged[k] = v
        return merged
    except Exception:
        return existing or {}

def slug_from_name(name):
    name = name.lower()
    replacements = {'a':'a','b':'b','c':'c','d':'d','e':'e','f':'f','g':'g','h':'h',
                    'i':'i','j':'j','k':'k','l':'l','m':'m','n':'n','o':'o','p':'p',
                    'q':'q','r':'r','s':'s','t':'t','u':'u','v':'v','w':'w','x':'x',
                    'y':'y','z':'z',
                    'a':'a','e':'e','i':'i','o':'o','u':'u',
                    'c':'c','d':'d','e':'e','n':'n','r':'r','s':'s','t':'t','u':'u','y':'y','z':'z'}
    result = ""
    for ch in name:
        if ch.isalnum():
            result += ch
        elif ch in (' ', '-', '_'):
            result += '-'
    result = re.sub(r'-+', '-', result).strip('-')
    return result or "klient"

# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

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

# ─────────────────────────────────────────────
# ROUTES — AUTH
# ─────────────────────────────────────────────

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
        if user and user.is_active and check_password_hash(user.password_hash, password):
            session["user_id"]   = user.id
            session["user_name"] = user.name
            session["is_admin"]  = user.is_admin
            session["user_role"] = user.role
            return redirect(url_for("dashboard"))
        error = "Nespravny e-mail nebo heslo."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─────────────────────────────────────────────
# ROUTES — DASHBOARD
# ─────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    zapisy  = Zapisy_query()
    klienti = Klient.query.filter_by(is_active=True).order_by(Klient.nazev).all()
    stats = {
        "celkem":  Zapis.query.count(),
        "freelo":  Zapis.query.filter_by(freelo_sent=True).count(),
        "klienti": Klient.query.filter_by(is_active=True).count(),
        "projekty": Projekt.query.filter_by(is_active=True).count(),
    }
    return render_template("dashboard.html", zapisy=zapisy, klienti=klienti,
                           stats=stats, template_names=TEMPLATE_NAMES)

def Zapisy_query():
    return Zapis.query.order_by(Zapis.created_at.desc()).limit(30).all()

# ─────────────────────────────────────────────
# ROUTES — KLIENTI
# ─────────────────────────────────────────────

@app.route("/klienti")
@login_required
def klienti_list():
    klienti = Klient.query.order_by(Klient.nazev).all()
    return render_template("klienti.html", klienti=klienti)

@app.route("/klient/novy", methods=["GET", "POST"])
@login_required
def klient_novy():
    if request.method == "POST":
        nazev = request.form.get("nazev","").strip()
        if not nazev:
            return render_template("klient_form.html", klient=None, error="Nazev je povinny")
        slug  = slug_from_name(nazev)
        # ensure unique slug
        base, i = slug, 1
        while Klient.query.filter_by(slug=slug).first():
            slug = f"{base}-{i}"; i += 1
        k = Klient(
            nazev=nazev, slug=slug,
            kontakt=request.form.get("kontakt",""),
            email=request.form.get("email",""),
            telefon=request.form.get("telefon",""),
            adresa=request.form.get("adresa",""),
            poznamka=request.form.get("poznamka",""),
        )
        db.session.add(k)
        db.session.commit()
        return redirect(url_for("klient_detail", klient_id=k.id))
    return render_template("klient_form.html", klient=None)

@app.route("/klient/<int:klient_id>")
@login_required
def klient_detail(klient_id):
    k = Klient.query.get_or_404(klient_id)
    projekty = Projekt.query.filter_by(klient_id=klient_id).order_by(Projekt.created_at.desc()).all()
    zapisy   = Zapis.query.filter_by(klient_id=klient_id).order_by(Zapis.created_at.desc()).all()
    konzultanti = User.query.filter_by(is_active=True).all()
    try:
        profil = json.loads(k.profil_json or "{}")
    except Exception:
        profil = {}
    return render_template("klient_detail.html", k=k, projekty=projekty,
                           zapisy=zapisy, profil=profil,
                           konzultanti=konzultanti, template_names=TEMPLATE_NAMES)

@app.route("/klient/<int:klient_id>/upravit", methods=["GET", "POST"])
@login_required
def klient_upravit(klient_id):
    k = Klient.query.get_or_404(klient_id)
    if request.method == "POST":
        k.nazev   = request.form.get("nazev", k.nazev).strip()
        k.kontakt = request.form.get("kontakt","")
        k.email   = request.form.get("email","")
        k.telefon = request.form.get("telefon","")
        k.adresa  = request.form.get("adresa","")
        k.poznamka= request.form.get("poznamka","")
        k.is_active = request.form.get("is_active") == "1"
        db.session.commit()
        return redirect(url_for("klient_detail", klient_id=k.id))
    return render_template("klient_form.html", klient=k)

@app.route("/api/klient/<int:klient_id>/profil", methods=["POST"])
@login_required
def klient_profil_update(klient_id):
    k = Klient.query.get_or_404(klient_id)
    data = request.json or {}
    try:
        profil = json.loads(k.profil_json or "{}")
    except Exception:
        profil = {}
    for key, val in data.items():
        if val is not None and val != "":
            profil[key] = val
        elif key in profil and (val is None or val == ""):
            del profil[key]
    k.profil_json = json.dumps(profil, ensure_ascii=False)
    db.session.commit()
    return jsonify({"ok": True, "profil": profil})

# ─────────────────────────────────────────────
# ROUTES — PROJEKTY
# ─────────────────────────────────────────────

@app.route("/projekt/novy", methods=["POST"])
@login_required
def projekt_novy():
    data      = request.form
    klient_id = data.get("klient_id")
    nazev     = data.get("nazev","").strip()
    if not nazev or not klient_id:
        return redirect(url_for("klienti_list"))
    datum_od = None
    datum_do = None
    try:
        if data.get("datum_od"): datum_od = datetime.strptime(data["datum_od"], "%Y-%m-%d").date()
        if data.get("datum_do"): datum_do = datetime.strptime(data["datum_do"], "%Y-%m-%d").date()
    except ValueError:
        pass
    p = Projekt(
        nazev=nazev,
        popis=data.get("popis",""),
        klient_id=int(klient_id),
        user_id=int(data["user_id"]) if data.get("user_id") else None,
        datum_od=datum_od,
        datum_do=datum_do,
    )
    db.session.add(p)
    db.session.commit()
    return redirect(url_for("klient_detail", klient_id=klient_id))

@app.route("/projekt/<int:projekt_id>/upravit", methods=["POST"])
@login_required
def projekt_upravit(projekt_id):
    p    = Projekt.query.get_or_404(projekt_id)
    data = request.form
    p.nazev   = data.get("nazev", p.nazev).strip()
    p.popis   = data.get("popis", "")
    p.user_id = int(data["user_id"]) if data.get("user_id") else None
    p.is_active = data.get("is_active") == "1"
    try:
        if data.get("datum_od"): p.datum_od = datetime.strptime(data["datum_od"], "%Y-%m-%d").date()
        if data.get("datum_do"): p.datum_do = datetime.strptime(data["datum_do"], "%Y-%m-%d").date()
    except ValueError:
        pass
    db.session.commit()
    return redirect(url_for("klient_detail", klient_id=p.klient_id))

@app.route("/projekt/<int:projekt_id>")
@login_required
def projekt_detail(projekt_id):
    p      = Projekt.query.get_or_404(projekt_id)
    zapisy = Zapis.query.filter_by(projekt_id=projekt_id).order_by(Zapis.created_at.desc()).all()
    konzultanti = User.query.filter_by(is_active=True).all()
    return render_template("projekt_detail.html", p=p, zapisy=zapisy,
                           konzultanti=konzultanti, template_names=TEMPLATE_NAMES)

# ─────────────────────────────────────────────
# ROUTES — ZAPISY
# ─────────────────────────────────────────────

@app.route("/novy")
@login_required
def novy_zapis():
    klienti     = Klient.query.filter_by(is_active=True).order_by(Klient.nazev).all()
    konzultanti = User.query.filter_by(is_active=True).all()
    return render_template("novy.html", klienti=klienti,
                           konzultanti=konzultanti, template_names=TEMPLATE_NAMES)

@app.route("/novy/projekty/<int:klient_id>")
@login_required
def get_projekty_for_klient(klient_id):
    projekty = Projekt.query.filter_by(klient_id=klient_id, is_active=True).all()
    return jsonify([{"id": p.id, "nazev": p.nazev} for p in projekty])

@app.route("/zapis/<int:zapis_id>")
@login_required
def detail_zapisu(zapis_id):
    zapis = Zapis.query.get_or_404(zapis_id)
    tasks = json.loads(zapis.tasks_json or "[]")
    notes = json.loads(zapis.notes_json or "[]")
    try:
        summary = json.loads(zapis.output_json or "{}")
    except Exception:
        summary = {}
    return render_template("detail.html", zapis=zapis, tasks=tasks, notes=notes,
                           summary=summary, section_titles=SECTION_TITLES,
                           template_names=TEMPLATE_NAMES)

@app.route("/zapis/verejny/<token>")
def zapis_verejny(token):
    zapis = Zapis.query.filter_by(public_token=token, is_public=True).first_or_404()
    try:
        summary = json.loads(zapis.output_json or "{}")
    except Exception:
        summary = {}
    return render_template("verejny.html", zapis=zapis, summary=summary,
                           section_titles=SECTION_TITLES, template_names=TEMPLATE_NAMES)

@app.route("/api/zapis/<int:zapis_id>/publikovat", methods=["POST"])
@login_required
def zapis_publikovat(zapis_id):
    zapis = Zapis.query.get_or_404(zapis_id)
    data  = request.json or {}
    publish = data.get("publish", True)
    if publish and not zapis.public_token:
        zapis.public_token = secrets.token_urlsafe(20)
    zapis.is_public = bool(publish)
    db.session.commit()
    url = url_for("zapis_verejny", token=zapis.public_token, _external=True) if zapis.is_public else None
    return jsonify({"ok": True, "is_public": zapis.is_public, "url": url, "token": zapis.public_token})

# ─────────────────────────────────────────────
# API — GENERATE
# ─────────────────────────────────────────────

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
    notes_raw      = data.get("notes", [])   # [{title, text}, ...]
    interni_prompt = data.get("interni_prompt", "").strip()
    klient_id      = data.get("klient_id")
    projekt_id     = data.get("projekt_id")

    if not input_text:
        return jsonify({"error": "Prazdny text"}), 400

    ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Load client profile for context
    klient_profil = None
    if klient_id:
        k = Klient.query.get(klient_id)
        if k:
            try:
                klient_profil = json.loads(k.profil_json or "{}")
            except Exception:
                pass

    # Condense long transcripts
    transcript = input_text
    if len(input_text) > 12000:
        try:
            transcript = condensed_transcript(ai, input_text)
        except Exception as e:
            app.logger.warning(f"Condensation failed: {e}")

    # Combine notes with transcript
    notes_text = ""
    if notes_raw:
        notes_parts = []
        for n in notes_raw:
            if n.get("text","").strip():
                title = n.get("title","Poznamka")
                notes_parts.append(f"[{title}]\n{n['text'].strip()}")
        if notes_parts:
            notes_text = "\n\n".join(notes_parts)

    client_context = f"""
Klient: {client_info.get('client_name', '')}
Kontaktni osoba klienta: {client_info.get('client_contact', '')}
Za Commarec: {client_info.get('commarec_rep', '')}
Datum schuzky: {client_info.get('meeting_date', '')}
Misto: {client_info.get('meeting_place', '')}
Typ schuzky: {TEMPLATE_NAMES.get(template, template)}
"""

    user_message = f"""INFORMACE O SCHUZCE:
{client_context}
"""
    if notes_text:
        user_message += f"\nPOZNAMKY Z TERENU (auditora):\n{notes_text}\n"

    user_message += f"\nPREPIS / POZNAMKY ZE SCHUZKY:\n{transcript}\n\nVytvor strukturovany JSON zapis. Vrat POUZE validni JSON, zadny jiny text."

    system = build_system_prompt(interni_prompt, klient_profil)

    try:
        message = ai.messages.create(
            model="claude-sonnet-4-5", max_tokens=8000,
            system=system,
            messages=[
                {"role": "user",      "content": user_message},
                {"role": "assistant", "content": "{"},  # prefill forces JSON output
            ]
        )
        # Prepend the prefill character we started with
        raw = "{" + message.content[0].text.strip()
        app.logger.info(f"AI response length: {len(raw)} chars, stop_reason: {message.stop_reason}")
    except Exception as e:
        return jsonify({"error": f"Chyba API: {str(e)}"}), 500

    # Robust JSON extraction — handles markdown fences, preamble text, partial responses
    app.logger.info(f"Raw AI response (first 300): {raw[:300]}")

    def repair_truncated_json(text):
        """Pokusi se uzavrit oriznuty JSON pridanim chybejicich zaviracich znaku."""
        # Spocitej nezavrene { a "
        depth = 0
        in_string = False
        escape_next = False
        for ch in text:
            if escape_next:
                escape_next = False
                continue
            if ch == '\\':
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
            if not in_string:
                if ch == '{': depth += 1
                elif ch == '}': depth -= 1
        # Uzavri nezavrene retezce a objekty
        if in_string:
            text += '"'
        # Uzavri vsechny otevrene objekty
        text += '}' * max(0, depth)
        return text

    def extract_json(text):
        """Try multiple strategies to extract valid JSON from AI response."""
        # Strategy 1: strip markdown fences and parse directly
        cleaned = re.sub(r'^```[\w]*\n?', '', text.strip())
        cleaned = re.sub(r'\n?```$', '', cleaned).strip()
        try:
            return json.loads(cleaned)
        except Exception:
            pass

        # Strategy 2: find the outermost { } block
        start = text.find('{')
        if start != -1:
            # Walk forward counting braces to find matching close
            depth = 0
            for i, ch in enumerate(text[start:], start):
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i+1])
                        except Exception:
                            break

        # Strategy 3: regex fallback
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass

        return None

    # Pokud byl JSON oriznut (max_tokens), zkus ho uzavrit
    if message.stop_reason == "max_tokens":
        app.logger.warning("Response hit max_tokens — attempting JSON repair")
        raw = repair_truncated_json(raw)

    summary_json = extract_json(raw)
    if summary_json is None:
        app.logger.error(f"JSON parse failed. Raw response: {raw[:500]}")
        return jsonify({"error": f"AI vratilo nevalidni JSON. Zkus znovu. (zacatek odpovedi: {raw[:200]})"}), 500

    tasks = []
    raw_tasks = summary_json.pop("tasks", [])
    if isinstance(raw_tasks, list):
        for t in raw_tasks:
            if isinstance(t, dict) and t.get("name"):
                tasks.append({
                    "name":     str(t.get("name",""))[:200],
                    "desc":     str(t.get("desc","")),
                    "deadline": str(t.get("deadline","dle dohody")),
                })

    output_text = assemble_output_text(client_info, summary_json, blocks)

    # Build title
    client_name  = client_info.get("client_name","").strip()
    meeting_date = client_info.get("meeting_date","").strip()
    title = f"{client_name} - {meeting_date}" if client_name else f"Zapis {meeting_date}"

    # Auto-update client profile from transcript
    if klient_id:
        k = Klient.query.get(klient_id)
        if k:
            try:
                existing_profil = json.loads(k.profil_json or "{}")
                new_profil = extract_klient_profil(ai, input_text, existing_profil)
                k.profil_json = json.dumps(new_profil, ensure_ascii=False)
            except Exception as e:
                app.logger.warning(f"Profile extraction failed: {e}")

    zapis = Zapis(
        title=title, template=template,
        input_text=input_text,
        output_json=json.dumps(summary_json, ensure_ascii=False),
        output_text=output_text,
        tasks_json=json.dumps(tasks, ensure_ascii=False),
        notes_json=json.dumps(notes_raw, ensure_ascii=False),
        interni_prompt=interni_prompt,
        user_id=session["user_id"],
        klient_id=int(klient_id) if klient_id else None,
        projekt_id=int(projekt_id) if projekt_id else None,
    )
    db.session.add(zapis)
    db.session.commit()

    return jsonify({"zapis_id": zapis.id, "text": output_text,
                    "tasks": tasks, "title": title, "summary": summary_json})

# ─────────────────────────────────────────────
# API — EDIT SECTION
# ─────────────────────────────────────────────

@app.route("/api/zapis/<int:zapis_id>/sekce", methods=["POST"])
@login_required
def ulozit_sekci(zapis_id):
    zapis = Zapis.query.get_or_404(zapis_id)
    data  = request.json or {}
    key   = data.get("key","")
    html  = data.get("html","")
    if key not in SECTION_TITLES:
        return jsonify({"error": "Neznama sekce"}), 400
    try:
        summary = json.loads(zapis.output_json or "{}")
    except Exception:
        summary = {}
    summary[key] = html
    zapis.output_json = json.dumps(summary, ensure_ascii=False)
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/zapis/<int:zapis_id>/ai-sekce", methods=["POST"])
@login_required
def ai_upravit_sekci(zapis_id):
    zapis = Zapis.query.get_or_404(zapis_id)
    data  = request.json or {}
    key          = data.get("key","")
    user_prompt  = data.get("prompt","").strip()
    current_html = data.get("html","")
    if not user_prompt:
        return jsonify({"error": "Chybi instrukce"}), 400
    section_title = SECTION_TITLES.get(key, key)
    system = f"""Uprav tuto sekci zapisu ze schuzky podle instrukce uzivatele.
Sekce: {section_title}
Zachovej styl, strukturu a HTML formatting pokud instrukce nerika jinak.
Vrat POUZE upravene HTML bez komentaru, vysvetleni nebo markdown znacek."""
    try:
        ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = ai.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": f"ORIGINAL HTML:\n{current_html}\n\nINSTRUKCE:\n{user_prompt}"}]
        )
        new_html = msg.content[0].text.strip()
        new_html = re.sub(r'^```[\w]*\n?', '', new_html)
        new_html = re.sub(r'\n?```$', '', new_html).strip()
        return jsonify({"ok": True, "html": new_html})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/zapis/<int:zapis_id>/notes", methods=["POST"])
@login_required
def ulozit_notes(zapis_id):
    zapis = Zapis.query.get_or_404(zapis_id)
    notes = request.json or []
    zapis.notes_json = json.dumps(notes, ensure_ascii=False)
    db.session.commit()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# FREELO HELPERS
# ─────────────────────────────────────────────

def freelo_auth():
    return (FREELO_EMAIL, FREELO_API_KEY)

def freelo_get(path):
    return requests.get(f"https://api.freelo.io/v1{path}",
                        auth=freelo_auth(), headers={"Content-Type":"application/json"}, timeout=15)

def freelo_post(path, payload):
    return requests.post(f"https://api.freelo.io/v1{path}",
                         auth=freelo_auth(), headers={"Content-Type":"application/json"},
                         json=payload, timeout=15)

# ─────────────────────────────────────────────
# FREELO API ENDPOINTS
# ─────────────────────────────────────────────

@app.route("/api/freelo/projects", methods=["GET"])
@login_required
def get_freelo_projects():
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"projects":[], "error":"Chybi FREELO credentials"})
    try:
        resp = freelo_get("/projects")
        if resp.status_code != 200:
            return jsonify({"projects":[], "error":f"Freelo {resp.status_code}"})
        raw = resp.json()
        projects = raw if isinstance(raw, list) else raw.get("data",[])
        result = [{"id":p["id"],"name":p.get("name",""),
                   "tasklists":[{"id":tl["id"],"name":tl.get("name","")} for tl in p.get("tasklists",[])]}
                  for p in projects if isinstance(p, dict) and "id" in p]
        return jsonify({"projects": result})
    except Exception as e:
        return jsonify({"projects":[], "error":str(e)})

@app.route("/api/freelo/members/<int:project_id>", methods=["GET"])
@login_required
def get_freelo_members(project_id):
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"members":[]})
    try:
        resp = freelo_get(f"/project/{project_id}/workers")
        members = []
        if resp.status_code == 200:
            workers = resp.json().get("data",{}).get("workers",[])
            for w in workers:
                if isinstance(w, dict) and w.get("fullname"):
                    members.append({"id":w["id"],"name":w["fullname"],"email":w.get("email","")})
        return jsonify({"members": members})
    except Exception as e:
        return jsonify({"members":[]})

@app.route("/api/freelo/create-tasklist", methods=["POST"])
@login_required
def create_freelo_tasklist():
    req  = request.json or {}
    name = req.get("name","").strip()
    pid  = str(req.get("project_id", FREELO_PROJECT_ID))
    if not name: return jsonify({"error":"Chybi nazev"}), 400
    try:
        resp = freelo_post(f"/project/{pid}/tasklists", {"name": name})
        if resp.status_code in (200,201):
            data = resp.json()
            tl = data.get("data", data)
            if isinstance(tl, list): tl = tl[0]
            return jsonify({"id":tl["id"],"name":tl["name"]})
        return jsonify({"error":f"Freelo {resp.status_code}: {resp.text[:100]}"}), 400
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/freelo/<int:zapis_id>", methods=["POST"])
@login_required
def odeslat_do_freela(zapis_id):
    zapis          = Zapis.query.get_or_404(zapis_id)
    data           = request.json or {}
    selected_tasks = data.get("tasks",[])
    tasklist_id    = data.get("tasklist_id")
    if not selected_tasks: return jsonify({"error":"Zadne ukoly"}), 400
    if not tasklist_id:    return jsonify({"error":"Vyberte To-Do list"}), 400

    project_id_for_tasks = FREELO_PROJECT_ID
    try:
        resp_p = freelo_get("/projects")
        if resp_p.status_code == 200:
            for proj in resp_p.json():
                for tl in proj.get("tasklists",[]):
                    if str(tl.get("id")) == str(tasklist_id):
                        project_id_for_tasks = proj["id"]; break
    except Exception:
        pass

    members_by_name = {}
    try:
        mr = freelo_get(f"/project/{project_id_for_tasks}/workers")
        if mr.status_code == 200:
            for w in mr.json().get("data",{}).get("workers",[]):
                if w.get("fullname"):
                    members_by_name[w["fullname"].lower()] = w["id"]
    except Exception:
        pass

    created, errors = [], []
    for task in selected_tasks:
        name = task.get("name","").strip()
        if not name: continue
        payload  = {"name": name}
        assignee = (task.get("assignee") or "").strip()
        deadline = (task.get("deadline") or "").strip()
        if task.get("desc"): payload["description"] = task["desc"]
        if assignee:
            wid = members_by_name.get(assignee.lower())
            if wid: payload["worker_id"] = wid
        if deadline and deadline.lower() not in ("dle dohody",""):
            if re.match(r"\d{4}-\d{2}-\d{2}", deadline):
                payload["due_date"] = deadline
            elif re.match(r"\d{1,2}\.\d{1,2}\.\d{4}", deadline):
                p = deadline.replace(" ","").split(".")
                payload["due_date"] = f"{p[2]}-{p[1].zfill(2)}-{p[0].zfill(2)}"
        try:
            resp = freelo_post(f"/project/{project_id_for_tasks}/tasklist/{tasklist_id}/tasks", payload)
            app.logger.info(f"Task '{name}': {resp.status_code} {resp.text[:150]}")
            if resp.status_code in (200,201):
                created.append(name)
                task_data = resp.json()
                task_id   = (task_data.get("data") or task_data).get("id")
                if task_id:
                    if task.get("desc"):
                        freelo_post(f"/task/{task_id}/description", {"description": task["desc"]})
                    if assignee and not members_by_name.get(assignee.lower()):
                        freelo_post(f"/task/{task_id}/comments", {"comment": f"Zodpovedna osoba: {assignee}"})
            else:
                errors.append(f"{name}: {resp.text[:100]}")
        except Exception as e:
            errors.append(f"{name}: {str(e)}")

    if created:
        zapis.freelo_sent = True
        db.session.commit()
    return jsonify({"created": created, "errors": errors})

# ─────────────────────────────────────────────
# ROUTES — ADMIN (users)
# ─────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin():
    users   = User.query.order_by(User.name).all()
    klienti = Klient.query.order_by(Klient.nazev).all()
    return render_template("admin.html", users=users, klienti=klienti)

@app.route("/admin/pridat-uzivatele", methods=["POST"])
@admin_required
def pridat_uzivatele():
    email    = request.form.get("email","").strip().lower()
    name     = request.form.get("name","").strip()
    password = request.form.get("password","")
    is_admin = bool(request.form.get("is_admin"))
    role     = request.form.get("role","konzultant")
    if User.query.filter_by(email=email).first():
        return redirect(url_for("admin"))
    db.session.add(User(email=email, name=name, role=role,
                        password_hash=generate_password_hash(password), is_admin=is_admin))
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/admin/upravit-uzivatele/<int:user_id>", methods=["POST"])
@admin_required
def upravit_uzivatele(user_id):
    user = User.query.get_or_404(user_id)
    user.name      = request.form.get("name", user.name).strip()
    user.is_admin  = bool(request.form.get("is_admin"))
    user.is_active = bool(request.form.get("is_active"))
    user.role      = request.form.get("role", user.role)
    if request.form.get("password"):
        user.password_hash = generate_password_hash(request.form["password"])
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/admin/smazat-uzivatele/<int:user_id>", methods=["POST"])
@admin_required
def smazat_uzivatele(user_id):
    if user_id == session["user_id"]: return redirect(url_for("admin"))
    user = User.query.get_or_404(user_id)
    user.is_active = False
    db.session.commit()
    return redirect(url_for("admin"))

# ─────────────────────────────────────────────
# DB INIT + AUTO-MIGRATE
# ─────────────────────────────────────────────


def seed_test_data():
    """Vytvor testovaci data pokud jeste neexistuji."""
    # Jen pokud je prazdna DB (zadny klient)
    if Klient.query.first():
        return

    print("Seeduji testovaci data...")

    # Test users
    martin = User.query.filter_by(email="martin@commarec.cz").first()
    if not martin:
        martin = User(
            email="martin@commarec.cz", name="Martin Komarek",
            role="konzultant", is_admin=False, is_active=True,
            password_hash=generate_password_hash("test123")
        )
        db.session.add(martin)
        db.session.flush()

    # Klient 1
    k1 = Klient(
        nazev="Testovaci Logistika s.r.o.",
        slug="testovaci-logistika",
        kontakt="Petr Novotny",
        email="novotny@testlogistika.cz",
        telefon="+420 777 123 456",
        adresa="Brno, Jihomoravský kraj",
        poznamka="Distribuční sklad, klient od 2023. Zaměřujeme se na optimalizaci pickování.",
        profil_json=json.dumps({
            "typ_skladu": "distribucni",
            "pocet_sku": "4200",
            "metody_pickingu": "batch picking, zone picking",
            "pocet_zamestnanci": "28",
            "pocet_smen": "2",
            "wms_system": "Helios Orange",
            "prumerna_denni_expedice": "850",
            "hlavni_problemy": "Vysoký backlog, chybovost při pickování B2B objednávek",
        }, ensure_ascii=False)
    )
    db.session.add(k1)
    db.session.flush()

    # Klient 2
    k2 = Klient(
        nazev="Demo Expres a.s.",
        slug="demo-expres",
        kontakt="Jana Horakova",
        email="horakova@demoexpres.cz",
        adresa="Praha 9, Letňany",
        poznamka="Výrobní a expediční sklad. Implementace WMS v řešení.",
    )
    db.session.add(k2)
    db.session.flush()

    # Admin user (already exists, just get ref)
    admin = User.query.filter_by(email="admin@commarec.cz").first()

    # Projekt 1 -- Testovaci Logistika
    p1 = Projekt(
        nazev="Optimalizace skladu 2025",
        popis="Procesní audit a návrh optimalizace pickování a layoutu skladu.",
        klient_id=k1.id,
        user_id=admin.id if admin else None,
        datum_od=datetime(2025, 1, 15).date(),
        datum_do=datetime(2025, 12, 31).date(),
        is_active=True,
    )
    db.session.add(p1)
    db.session.flush()

    # Projekt 2 -- Demo Expres
    p2 = Projekt(
        nazev="WMS implementace",
        popis="Výběr a implementace WMS systému.",
        klient_id=k2.id,
        user_id=admin.id if admin else None,
        datum_od=datetime(2025, 3, 1).date(),
        is_active=True,
    )
    db.session.add(p2)
    db.session.flush()

    # Zapis 1 -- audit Testovaci Logistika
    summary1 = {
        "participants_commarec": "<p>Martin Komárek -- vedoucí konzultant</p>",
        "participants_company": "<p>Petr Novotny (ředitel logistiky), Pavel Benes (vedoucí skladu)</p>",
        "introduction": "<p>Diagnostická návštěva zaměřená na identifikaci příčin rostoucího backlogu a chybovosti při expedici B2B objednávek.</p>",
        "meeting_goal": "<p>Zmapovat aktuální stav pickování, změřit výkonnost a navrhnout konkrétní opatření.</p>",
        "findings": "<ul><li><strong>Pozitivní:</strong> Motivovaný tým, dobrá znalost sortimentu, zavedené ranní porady</li><li><strong>Rizika:</strong> Chybovost pickování 4,2 % (standard je pod 0,5 %), backlog 3 dny, WMS bez wave-planningu</li></ul>",
        "ratings": "<table><tr><th>Oblast</th><th>Hodnocení (%)</th><th>Komentář</th></tr><tr><td>Procesní dokumentace</td><td>35</td><td>Chybí standardy pro B2B picking</td></tr><tr><td>WMS utilizace</td><td>45</td><td>Nevyužívají wave planning ani ABC analýzu</td></tr><tr><td>Layout skladu</td><td>60</td><td>Základní zónování, reserve locations OK</td></tr><tr><td>Produktivita pickování</td><td>40</td><td>58 řádků/hod, potenciál 90+</td></tr><tr><td colspan='3'><strong>Celkové skóre: 45 %</strong> | Nejlepší: Layout | Nejkritičtější: Chybovost</td></tr></table>",
        "processes_description": "<p>Picking probiha single-order metodou bez batch zpracovani. Pracovnici chodi pro kazde objednavku zvlast, prumerna vzdalenost 340 m/objednavka. <em>Mame pocit ze chodime porad dokola</em> (Pavel Benes). ABC analyza nebyla nikdy provedena -- fast-movers jsou rozmisteny nahodne po celem skladu.</p>",
        "dangers": "<ul><li><strong>Chybovost 4,2 %</strong> → reklamace, ztráta zákazníků, přepracování</li><li><strong>Backlog 3 dny</strong> → nesplněné SLA, penalty od odběratelů</li><li><strong>Odchod klíčových lidí</strong> → frustrace z chaosu, 2 výpovědi za Q4 2024</li></ul>",
        "suggested_actions": "<p><strong>Krátkodobé (0-1 měsíc):</strong></p><ul><li>ABC analýza sortimentu -- přesunout top 200 SKU do pick zóny A</li><li>Zavedení batch pickingu pro B2C objednávky (skupiny po 8-12 obj.)</li></ul><p><strong>Střednědobé (1-3 měsíce):</strong></p><ul><li>Konfigurace wave planningu v Helios Orange</li><li>Tvorba standardů a SOP pro picking B2B</li></ul>",
        "expected_benefits": "<ul><li><strong>Snížení chybovosti</strong> z 4,2 % na pod 0,8 % -- úspora 280 tis. Kč/rok na reklamacích</li><li><strong>Zvýšení produktivity</strong> o 35-45 % po zavedení batch pickingu</li><li><strong>Odbourání backlogu</strong> do 2 týdnů od implementace ABC zónování</li></ul>",
        "additional_notes": "<p>Velmi pozitivní přístup vedení -- okamžitě souhlasili s navrhovanými změnami. Pavel Benes je silný interní champion. Sklad je čistý a dobře organizovaný co se týče fyzického uspořádání -- problém je v procesech, ne v prostoru.</p>",
        "summary": "<p>Sklad Testovaci Logistika má solidní základy ale trpí procesními neduhy typickými pro organicky rostoucí e-commerce/B2B operaci. Priorita #1: ABC analýza a přesun fast-movers. Priorita #2: batch picking. Očekáváme rychlé výsledky -- tým je motivovaný a vedení plně podporuje změny.</p>",
    }

    z1 = Zapis(
        title="Testovaci Logistika s.r.o. - 2025-02-14",
        template="audit",
        input_text="[Testovací zápis -- vygenerováno jako seed data]",
        output_json=json.dumps(summary1, ensure_ascii=False),
        output_text="",
        tasks_json=json.dumps([
            {"name": "ABC analyza sortimentu", "desc": "Provest analyzu pohyblivosti SKU a navrhnout rozmisteni fast-movers do zony A", "deadline": "do 1 mesice"},
            {"name": "Navrh batch picking procesu", "desc": "Zpracovat navrh wave planu pro B2C objednavky, skupiny 8-12 obj.", "deadline": "do 3 tydnu"},
            {"name": "Konfigurace wave planningu v Helios", "desc": "Spoluprace s IT na nastaveni wave planning modulu v Helios Orange", "deadline": "do 2 mesicu"},
        ], ensure_ascii=False),
        interni_prompt="",
        freelo_sent=False,
        user_id=admin.id if admin else 1,
        klient_id=k1.id,
        projekt_id=p1.id,
        created_at=datetime(2025, 2, 14, 10, 30),
    )
    # Assemble output_text from sections
    client_info = {"meeting_date": "2025-02-14", "commarec_rep": "Martin Komarek",
                   "client_contact": "Petr Novotny", "client_name": "Testovaci Logistika s.r.o.", "meeting_place": "Sídlo klienta, Brno"}
    all_blocks = set(["uvod","zjisteni","hodnoceni","procesy","rizika","kroky","prinosy","poznamky","dalsi_krok"])
    z1.output_text = assemble_output_text(client_info, summary1, all_blocks)
    db.session.add(z1)

    # Zapis 2 -- operativni schuzka Demo Expres
    summary2 = {
        "participants_commarec": "<p>Martin Komárek</p>",
        "participants_company": "<p>Jana Horakova (COO), Tomas Kral (IT ředitel)</p>",
        "introduction": "<p>Kick-off meeting k výběru WMS systému. Diskuse požadavků a harmonogramu.</p>",
        "meeting_goal": "<p>Definovat klíčové požadavky na WMS, odsouhlasit shortlist dodavatelů a nastavit harmonogram výběrového řízení.</p>",
        "findings": "<ul><li>Aktuálně používají Excel + papírové průvodky -- žádný WMS</li><li>Denní expedice 1 200 ks, 3 směny, 45 zaměstnanců</li><li>Požadavek na go-live do září 2025</li></ul>",
        "suggested_actions": "<p><strong>Krátkodobé:</strong></p><ul><li>Commarec připraví RFP dokument do 28. 2.</li><li>Demo Expres dodá kompletní seznam SKU a procesní mapu do 7. 3.</li></ul><p><strong>Střednědobé:</strong></p><ul><li>Demo prezentace 3 dodavatelů -- duben 2025</li><li>Výběr dodavatele -- květen 2025</li></ul>",
        "summary": "<p>Kick-off proběhl konstruktivně. Obě strany shodnuty na harmonogramu. Hlavní riziko: krátký timeline na go-live (6 měsíců). Commarec doporučuje zvážit fázovaný rollout.</p>",
    }
    z2 = Zapis(
        title="Demo Expres a.s. - 2025-03-05",
        template="operativa",
        input_text="[Testovací zápis -- vygenerováno jako seed data]",
        output_json=json.dumps(summary2, ensure_ascii=False),
        output_text="",
        tasks_json=json.dumps([
            {"name": "Pripravit RFP dokument", "desc": "Zpracovat pozadavky na WMS pro Demo Expres Czech", "deadline": "2025-02-28"},
            {"name": "Demo prezentace WMS dodavatelu", "desc": "Organizace demo dni pro 3 vybrane dodavatele", "deadline": "2025-04-15"},
        ], ensure_ascii=False),
        interni_prompt="",
        freelo_sent=False,
        user_id=admin.id if admin else 1,
        klient_id=k2.id,
        projekt_id=p2.id,
        created_at=datetime(2025, 3, 5, 14, 0),
    )
    client_info2 = {"meeting_date": "2025-03-05", "commarec_rep": "Martin Komarek",
                    "client_contact": "Jana Horakova", "client_name": "Demo Expres a.s.", "meeting_place": "Praha 9, Letňany"}
    z2.output_text = assemble_output_text(client_info2, summary2, all_blocks)
    db.session.add(z2)

    db.session.commit()
    print("Seed data vytvorena: 2 klienti, 2 projekty, 2 zapisy")

with app.app_context():
    try:
        db.create_all()  # skips existing tables — safe to run repeatedly
        # Auto-migrate new columns
        migrations = [
            ("zapis", "output_json",    "ALTER TABLE zapis ADD COLUMN output_json TEXT DEFAULT '{}'"),
            ("zapis", "notes_json",     "ALTER TABLE zapis ADD COLUMN notes_json TEXT DEFAULT '[]'"),
            ("zapis", "interni_prompt", "ALTER TABLE zapis ADD COLUMN interni_prompt TEXT DEFAULT ''"),
            ("zapis", "public_token",   "ALTER TABLE zapis ADD COLUMN public_token VARCHAR(40)"),
            ("zapis", "is_public",      "ALTER TABLE zapis ADD COLUMN is_public BOOLEAN DEFAULT FALSE"),
            ("zapis", "klient_id",      "ALTER TABLE zapis ADD COLUMN klient_id INTEGER"),
            ("zapis", "projekt_id",     "ALTER TABLE zapis ADD COLUMN projekt_id INTEGER"),
            ("user",  "is_active",      "ALTER TABLE user ADD COLUMN is_active BOOLEAN DEFAULT TRUE"),
            ("user",  "role",           "ALTER TABLE user ADD COLUMN role VARCHAR(40) DEFAULT 'konzultant'"),
        ]
        with db.engine.connect() as conn:
            for table, col, sql in migrations:
                try:
                    conn.execute(db.text(sql))
                    conn.commit()
                    print(f"Migrated: {table}.{col}")
                except Exception:
                    pass
        if not User.query.filter_by(email="admin@commarec.cz").first():
            try:
                db.session.add(User(
                    email="admin@commarec.cz", name="Admin", role="superadmin",
                    password_hash=generate_password_hash("admin123"), is_admin=True
                ))
                db.session.commit()
                print("Vytvoren vychozi admin: admin@commarec.cz / admin123")
            except Exception:
                db.session.rollback()  # another worker beat us to it — fine
        # Seed test data (only if DB is empty)
        try:
            seed_test_data()
        except Exception as e:
            print(f"Seed error: {e}")
    except Exception as e:
        print(f"DB init error: {e}")

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
