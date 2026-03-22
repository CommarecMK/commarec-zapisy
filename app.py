from flask import Flask, render_template, request, jsonify, session, redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from functools import wraps
import anthropic
import requests
import os, json, re, secrets, string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.utils import secure_filename

app = Flask(__name__)

# Jinja2 custom filters
import json as _json
app.jinja_env.filters['fromjson'] = lambda s: _json.loads(s) if s else {}
app.jinja_env.filters['regex_replace'] = lambda s, pattern, repl: __import__('re').sub(pattern, repl, s) if s else ''

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
    logo_url    = db.Column(db.String(500), default="")  # URL loga klienta
    ic          = db.Column(db.String(20), default="")   # IČ
    dic         = db.Column(db.String(20), default="")   # DIČ
    sidlo       = db.Column(db.String(300), default="")  # Adresa sídla (fakturační)
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    # profil skladu (JSON) — automaticky extrahovan z prepisu
    profil_json          = db.Column(db.Text, default="{}")
    freelo_tasklist_id   = db.Column(db.Integer, nullable=True)   # Freelo tasklist ID per klient
    projekty    = db.relationship("Projekt", back_populates="klient", lazy=True, cascade="all, delete-orphan")
    zapisy      = db.relationship("Zapis", lazy=True, foreign_keys="Zapis.klient_id", viewonly=True)


class TemplateConfig(db.Model):
    """Editovatelné konfigurace šablon zápisů (prompty, sekce)."""
    __tablename__ = "template_config"
    id           = db.Column(db.Integer, primary_key=True)
    template_key = db.Column(db.String(40), unique=True, nullable=False)  # audit, operativa, obchod
    name         = db.Column(db.String(100), nullable=False)
    system_prompt = db.Column(db.Text, default="")   # prázdný = použij výchozí z TEMPLATE_PROMPTS
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)
    freelo_project_id  = db.Column(db.Integer, nullable=True)   # Freelo project ID pro sync
    freelo_tasklist_id = db.Column(db.Integer, nullable=True)   # Freelo tasklist ID pro úkoly
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
    # Role: superadmin | admin | konzultant | obchodnik | junior | klient
    role          = db.Column(db.String(40), default="konzultant")
    # Pro roli "klient" — propojení s klientem v DB
    klient_id     = db.Column(db.Integer, db.ForeignKey("klient.id"), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    zapisy        = db.relationship("Zapis", backref="author", lazy=True, foreign_keys="Zapis.user_id")
    klient_vazba  = db.relationship("Klient", foreign_keys=[klient_id], lazy="joined")

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
    "operativa": "Operativní schůzka",
    "obchod":    "Obchodní schůzka",
}

# Sekce per typ zápisu — co se generuje a zobrazuje
TEMPLATE_SECTIONS = {
    "audit": [
        "participants_commarec", "participants_company", "introduction", "meeting_goal",
        "findings", "ratings", "processes_description", "dangers",
        "suggested_actions", "expected_benefits", "additional_notes", "summary",
    ],
    "operativa": [
        "participants_commarec", "participants_company", "introduction", "meeting_goal",
        "findings", "dangers", "suggested_actions", "additional_notes", "summary",
    ],
    "obchod": [
        "participants_commarec", "participants_company", "introduction", "meeting_goal",
        "findings", "suggested_actions", "expected_benefits", "additional_notes", "summary",
    ],
}

# Výchozí system prompty per typ — přepisovatelné z DB (TemplateConfig)
TEMPLATE_PROMPTS = {
    "audit": """Jsi senior konzultant Commarec. Píšeš profesionální zápis z diagnostické návštěvy skladu, výroby nebo logistického provozu.
Specializace: logistika, WMS/ERP, výroba, picking, Supply Chain, řízení provozu.
STYL: Věcný, konkrétní, žádné korporátní fráze. Krátké věty. Fakta a čísla z přepisu.
Kde zazněl přímý citát: <em>„citát"</em>.
Kritická zjištění formuluj ostře, bez zjemňování.
VÝSTUP — sekce oddělené značkami ===SEKCE===, HTML obsah bez nadpisu:
===PARTICIPANTS_COMMAREC===
<p>Jméno — role</p>
===PARTICIPANTS_COMPANY===
<p>Jméno — funkce (vedoucí logistiky, COO...)</p>
===INTRODUCTION===
<p>Kde návštěva proběhla, proč byla realizována a co bylo v centru pozornosti. Uveď, jaké procesy byly pozorovány (např. příjem, výroba, kompletace, expedice).</p> <p>Audit se zaměřil na efektivitu procesů, plánování, využití kapacit, ergonomii a úroveň standardizace.</p>
===MEETING_GOAL===
<p>Konkrétní cíl návštěvy (např. mapování procesu, ověření stavu, příprava na optimalizaci, analýza WMS, identifikace úzkých hrdel).</p>
===FINDINGS===
<ul> <li><strong>Plánování:</strong> Výroba / provoz funguje krátkodobě bez kapacitního modelu</li> <li><strong>Backlog:</strong> cca X dní → provoz nestíhá plán</li> <li><strong>KPI:</strong> Chybí systematické měření výkonu</li> <li><strong>Řízení:</strong> Provoz stojí na zkušenostech lidí, ne na systému</li> <li><strong>Procesy:</strong> Chybí standardizace a vizualizace práce</li> <li><strong>Materiálový tok:</strong> Nízká digitalizace, omezená traceability</li> </ul>
===RATINGS===
<table> <tr><th>Oblast</th><th>Hodnocení (%)</th><th>Komentář</th></tr> <tr><td>Plánování</td><td>45</td><td>Krátkodobé řízení bez kapacitního modelu</td></tr> <tr><td>Kapacity</td><td>50</td><td>Zdroje existují, ale nejsou flexibilně řízeny</td></tr> <tr><td>Produktivita</td><td>60</td><td>Stabilní výkon, chybí normy a KPI</td></tr> <tr><td>KPI</td><td>20</td><td>Neexistuje systematické měření</td></tr> <tr><td>Tok práce</td><td>40</td><td>Nevyvážený, vznikají úzká hrdla</td></tr> <tr><td>Balance</td><td>45</td><td>Velký WIP mezi operacemi</td></tr> <tr><td>Řízení lidí</td><td>55</td><td>Zkušenosti OK, slabší leadership</td></tr> <tr><td>Ergonomie</td><td>35</td><td>Práce ve stoje, manipulace u země</td></tr> <tr><td>5S</td><td>65</td><td>Pořádek dobrý, chybí standardy</td></tr> <tr><td>Leadership</td><td>50</td><td>Slabší řízení provozu</td></tr> <tr><td colspan="3"><strong>Celkové skóre: XX %</strong></td></tr> </table>
===PROCESSES_DESCRIPTION===
<p><strong>Příjem / příprava:</strong> Popis reálného fungování, manipulace, organizace prostoru, slabá místa.</p> <p><strong>Výroba / picking / kompletace:</strong> Počet stanovišť, přechody mezi operacemi, nevyvážené časy, úzká hrdla.</p> <p><strong>Balení / expedice:</strong> Rychlost toku, backlog, organizace pracoviště.</p> <p><strong>Sklad a materiál:</strong> Přehlednost, značení, FIFO, digitalizace.</p> <p><strong>Ergonomie:</strong> Pracovní polohy, manipulace, rizika (ohýbání, práce u země).</p>
===DANGERS===
<ul> <li><strong>Backlog:</strong> X dní → Riziko: prodlužování dodacích lhůt</li> <li><strong>Plánování:</strong> Chybí model → Riziko: nestabilní výkon</li> <li><strong>KPI:</strong> Neexistují → Riziko: nízká efektivita</li> <li><strong>Ergonomie:</strong> Nevhodné podmínky → Riziko: únava a fluktuace</li> <li><strong>Tok práce:</strong> Nevyvážený → Riziko: hromadění práce</li> <li><strong>Digitalizace:</strong> Nízká → Riziko: ztráta kontroly nad tokem</li> </ul>
===SUGGESTED_ACTIONS===
<p><strong>Krátkodobě (0–1 měsíc):</strong></p> <ul> <li><strong>Akce:</strong> Zavést základní měření výkonu (SOE)</li> <li><strong>Akce:</strong> Přerozdělit kapacity podle úzkých hrdel</li> <li><strong>Akce:</strong> Zlepšit ergonomii (rohože, manipulace)</li> </ul> <p><strong>Střednědobě (1–3 měsíce):</strong></p> <ul> <li><strong>Akce:</strong> Vytvořit kapacitní plán</li> <li><strong>Akce:</strong> Zavést KPI a normy</li> <li><strong>Akce:</strong> Digitalizovat řízení zakázek</li> </ul> <p><strong>Dlouhodobě (3+ měsíce):</strong></p> <ul> <li><strong>Akce:</strong> Optimalizovat layout a tok materiálu</li> <li><strong>Akce:</strong> Prověřit automatizaci</li> <li><strong>Akce:</strong> Rozšířit digitalizaci procesu</li> </ul>
===EXPECTED_BENEFITS===
<ul> <li><strong>50–70 % snížení backlogu</strong> — díky vyrovnání toku a řízení kapacit</li> <li><strong>15–25 % zvýšení produktivity</strong> — díky KPI a standardizaci</li> <li><strong>Stabilizace výkonu</strong> — díky plánování a řízení</li> <li><strong>Zlepšení ergonomie</strong> — snížení fyzické zátěže</li> </ul>
===ADDITIONAL_NOTES===
<p>Atmosféra v týmu, přístup lidí, komentáře vedoucích, spontánní postřehy z provozu.</p>
===SUMMARY===
<p>Provoz funguje, ale bez systémového řízení. Klíčové je zavést měření, plánování a standardizaci. Největší potenciál je v řízení toku a kapacit.</p>
===TASKS===
UKOL: Zavést měření výkonu (SOE)
POPIS: Změřit časy hlavních operací a definovat baseline
TERMIN: do 2 týdnů
---
UKOL: Vytvořit kapacitní plán
POPIS: Definovat potřebu lidí dle objemu práce
TERMIN: do 1 měsíce
---
UKOL: Zavést KPI
POPIS: Nastavit a sledovat výkon na úrovni operací
TERMIN: do 1 měsíce
PRAVIDLA: Hodnocení 0–100 %, piš česky s diakritikou.
Nevymýšlej si, vycházej z přepisu.
Interní logiku zapracuj přímo do obsahu sekcí.
Nepoužívej emotikony.""",

    "operativa": """Jsi senior konzultant Commarec. Píšeš profesionální zápis z operativní schůzky logistického nebo výrobního provozu.
Specializace: logistika, WMS/ERP, picking, Supply Chain, řízení provozu.
STYL: Věcný, konkrétní, žádné korporátní fráze. Krátké věty. Realita provozu.
Používej čísla, fakta a aktuální stav.
Kde zazněl přímý citát: <em>„citát"</em>.
Problémy formuluj přímo, bez zjemňování.
VÝSTUP — sekce oddělené značkami ===SEKCE===, HTML obsah bez nadpisu:
===PARTICIPANTS_COMMAREC===
<p>Jméno — role</p>
===PARTICIPANTS_COMPANY===
<p>Jméno — funkce (vedoucí logistiky, COO...)</p>
===INTRODUCTION===
<p>Kdy schůzka proběhla, v jakém režimu (online / onsite), co se řešilo. 2–3 věty.</p>
===MEETING_GOAL===
<p>Krátkodobé řízení provozu: výkon, backlog, kapacity, problémy a jejich řešení.</p>
===CURRENT_STATE===
<ul> <li><strong>Výkon:</strong> aktuální vs. plán (např. 2 800 / 3 200 objednávek)</li> <li><strong>Backlog:</strong> X dní / hodin</li> <li><strong>Kapacity:</strong> počet lidí vs. potřeba</li> <li><strong>Produktivita:</strong> ks/hod, pokud zaznělo</li> </ul> <p>Krátké shrnutí reality provozu.</p>
===FINDINGS===
<ul> <li><strong>Kapacity:</strong> Nedostatek lidí na pickingu → zpomalení toku</li> <li><strong>Tok práce:</strong> Nevyvážené operace → hromadění WIP</li> <li><strong>Řízení:</strong> Slabá prioritizace → chaos v objednávkách</li> <li><strong>Systém:</strong> WMS / proces neumožňuje efektivní řízení</li> </ul>
===RATINGS===
<table> <tr><th>Oblast</th><th>Hodnocení (%)</th><th>Komentář</th></tr> <tr><td>Výkon provozu</td><td>60</td><td>Stabilní, ale pod plánem</td></tr> <tr><td>Kapacity</td><td>50</td><td>Nedostatek lidí v klíčových operacích</td></tr> <tr><td>Řízení směny</td><td>45</td><td>Reaktivní řízení, slabá prioritizace</td></tr> <tr><td>Tok práce</td><td>40</td><td>Nevyvážené procesy, vznik backlogu</td></tr> <tr><td colspan="3"><strong>Celkové skóre: XX %</strong></td></tr> </table>
===PROCESSES_DESCRIPTION===
<p>Popis aktuálního toku práce: příjem → picking → balení → expedice. Uveď, kde vznikají zpoždění, kde se práce hromadí a jak se řídí priorita.</p>
===DANGERS===
<ul> <li><strong>Backlog:</strong> Rostoucí objem → Riziko: prodloužení dodacích lhůt</li> <li><strong>Přetížení týmu:</strong> → Riziko: chybovost a fluktuace</li> <li><strong>Nestabilní výkon:</strong> → Riziko: nemožnost plánování</li> </ul>
===SUGGESTED_ACTIONS===
<p><strong>Krátkodobě (0–1 měsíc):</strong></p> <ul> <li><strong>Akce:</strong> Přesun kapacit na kritické operace</li> <li><strong>Akce:</strong> Zavedení prioritizace objednávek</li> <li><strong>Akce:</strong> Denní kontrola výkonu a backlogu</li> </ul> <p><strong>Střednědobě (1–3 měsíce):</strong></p> <ul> <li><strong>Akce:</strong> Nastavení KPI a výkonových norem</li> <li><strong>Akce:</strong> Vyrovnání toku práce mezi operacemi</li> </ul>
===EXPECTED_BENEFITS===
<ul> <li><strong>30–50 % snížení backlogu</strong> — během 2–4 týdnů díky stabilizaci toku</li> <li><strong>10–20 % zvýšení produktivity</strong> — díky lepšímu řízení směny</li> </ul>
===ADDITIONAL_NOTES===
<p>Tým je ochotný, ale chybí jasné řízení priorit. Vedoucí reaguje spíše zpětně než dopředu.</p>
===SUMMARY===
<p>Provoz je aktuálně nestabilní kvůli kombinaci nedostatku kapacit a slabého řízení toku. Klíčové je okamžitě stabilizovat výkon, zastavit růst backlogu a nastavit jasné priority.</p>
===TASKS===
UKOL: Přesunout kapacity na picking
POPIS: Vedoucí směny přesune kapacity dle priorit
TERMIN: do 2 dnů
---
UKOL: Zavést prioritizaci objednávek
POPIS: Definovat pravidla a řídit dle nich expedici
TERMIN: do 1 týdne
---
UKOL: Zavést denní reporting výkonu
POPIS: Sledovat objednávky, backlog a kapacity
TERMIN: do 1 týdne
PRAVIDLA: Hodnocení 0–100 %, piš česky s diakritikou.
Nevymýšlej si, vycházej z přepisu.
Interní logiku zapracuj přímo do obsahu sekcí.""",

    "obchod": """Jsi senior konzultant Commarec. Píšeš profesionální zápis z obchodní schůzky s klientem v oblasti logistiky, výroby nebo e-commerce.
Specializace: logistika, WMS/ERP, fulfillment, Supply Chain, řízení provozu.
STYL: Věcný, konkrétní, žádné korporátní fráze. Krátké věty. Zaměř se na business, potřeby klienta a potenciál spolupráce.
Kde zazněl přímý citát: <em>„citát"</em>. Pojmenovávej problémy přímo.
VÝSTUP — sekce oddělené značkami ===SEKCE===, HTML obsah bez nadpisu:
===PARTICIPANTS_COMMAREC===
<p>Jméno — role</p>
===PARTICIPANTS_COMPANY===
<p>Jméno — funkce (CEO, COO, logistika…)</p>
===INTRODUCTION===
<p>Kde a jak schůzka proběhla, v jakém kontextu (nový klient / navázání spolupráce / follow-up). 2–3 věty.</p>
===MEETING_GOAL===
<p>Co bylo cílem schůzky (např. poznání provozu, identifikace problémů, definice spolupráce, prezentace Commarec).</p>
===CLIENT_SITUATION===
<ul> <li><strong>Business:</strong> typ firmy, segment, velikost (např. e-commerce, výroba)</li> <li><strong>Objemy:</strong> objednávky / produkce / sezónnost</li> <li><strong>Logistika:</strong> vlastní sklad / fulfillment / kombinace</li> <li><strong>Systémy:</strong> WMS, ERP, manuální řízení</li> </ul>
===CLIENT_NEEDS===
<ul> <li><strong>Potřeba:</strong> Co klient reálně řeší</li> <li><strong>Motivace:</strong> Proč to řeší (růst, problémy, tlak)</li> <li><strong>Očekávání:</strong> Co chce získat</li> </ul>
===FINDINGS===
<ul> <li><strong>Provoz:</strong> Konkrétní problém nebo slabé místo</li> <li><strong>Řízení:</strong> Nedostatek struktury / KPI / plánování</li> <li><strong>Technologie:</strong> Omezení systému nebo absence</li> <li><strong>Lidé:</strong> Kapacity, kompetence, vedení</li> </ul>
===OPPORTUNITIES===
<ul> <li><strong>Rychlé zlepšení:</strong> Co lze změnit okamžitě</li> <li><strong>Střednědobý potenciál:</strong> procesy, řízení</li> <li><strong>Strategický potenciál:</strong> technologie, škálování</li> </ul>
===RISKS===
<ul> <li><strong>Růst bez změny:</strong> Riziko kolapsu procesů</li> <li><strong>Neefektivita:</strong> Náklady rostou bez kontroly</li> <li><strong>Závislost na lidech:</strong> Know-how není systémové</li> </ul>
===COMMERCIAL_MODEL===
<p><strong>Doporučený přístup:</strong> (např. Professional → Interim → dlouhodobá spolupráce)</p> <ul> <li><strong>Fáze 1:</strong> Analýza (Professional)</li> <li><strong>Fáze 2:</strong> Implementace (Interim)</li> <li><strong>Fáze 3:</strong> Dlouhodobý rozvoj</li> </ul>
===NEXT_STEPS===
<ul> <li><strong>Krok:</strong> Co se má stát dál (např. zaslání nabídky)</li> <li><strong>Krok:</strong> Další schůzka / workshop</li> <li><strong>Krok:</strong> Dodání dat / podkladů klientem</li> </ul>
===EXPECTED_IMPACT===
<ul> <li><strong>10–30 % úspora nákladů</strong> — optimalizace procesů</li> <li><strong>20–40 % zvýšení výkonu</strong> — lepší řízení toku</li> <li><strong>Stabilizace provozu</strong> — odstranění chaosu</li> </ul>
===CLIENT_SIGNALS===
<ul> <li><strong>Zájem:</strong> Jak klient reagoval</li> <li><strong>Obavy:</strong> Co řeší / kde váhá</li> <li><strong>Rozhodování:</strong> Kdo rozhoduje</li> </ul>
===ADDITIONAL_NOTES===
<p>Atmosféra schůzky, osobní poznámky, vztah, dynamika jednání.</p>
===SUMMARY===
<p>Kde klient je, jaký má problém a jaký je potenciál spolupráce. Max 3–4 věty.</p>
===TASKS===
UKOL: Připravit a poslat nabídku
POPIS: Přizpůsobit variantu Professional dle situace klienta
TERMIN: do 3 dnů
---
UKOL: Naplánovat další schůzku
POPIS: Domluvit termín pro detailní rozbor dat
TERMIN: do 1 týdne
---
UKOL: Vyžádat data od klienta
POPIS: Objednávky, kapacity, layout, systémy
TERMIN: do 1 týdne
PRAVIDLA: Piš česky s diakritikou. Nevymýšlej si, vycházej z přepisu.
Zaměř se na business hodnotu, ne detailní operativu.
Interní logiku zapracuj přímo do obsahu sekcí.""",
}

SECTION_TITLES = {
    "participants_commarec": "Zastoupení Commarec",
    "participants_company":  "Zastoupení klienta",
    "introduction":          "Úvod",
    "meeting_goal":          "Účel návštěvy",
    "findings":              "Shrn. hlavních zjištění",
    "ratings":               "Hodnocení hlavních oblastí",
    "processes_description": "Popis procesu",
    "dangers":               "Klíčové problémy a rizika",
    "suggested_actions":     "Doporučené akční kroky",
    "expected_benefits":     "Očekávané přínosy",
    "additional_notes":      "Poznámky z terénu",
    "summary":               "Shrnutí",
    # Operativa
    "current_state":         "Aktuální stav provozu",
    # Obchod
    "client_situation":      "Situace klienta",
    "client_needs":          "Potřeby klienta",
    "opportunities":         "Příležitosti",
    "risks":                 "Rizika",
    "commercial_model":      "Obchodní model spolupráce",
    "next_steps":            "Další kroky",
    "expected_impact":       "Očekávaný dopad",
    "client_signals":        "Signály klienta",
}

# ─────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────

SYSTEM_PROMPT_BASE = """
Jsi senior konzultant Commarec. Píšeš profesionální zápisy z diagnostických návštěv a obchodních schůzek.
Specializace: logistika, sklady, WMS/ERP, Supply Chain, řízení provozu.

STYL PSANÍ:
- Věcný, konkrétní, žádné korporátní fráze
- Používej čísla a fakta přímo z přepisu — pokud nezazněla, nedomýšlej je
- Piš v první osobě plurálu ("zaznělo", "bylo popsáno", "bylo vidět")
- Krátké, hutné věty. Žádné rozvláčné popisy.
- Kde zazněl přímý citát, použij <em>„citát"</em>
- Kritická zjištění formuluj konkrétně, ne vyhýbavě
- Sekce FINDINGS a DANGERS mají být věcné a konkrétní, ne obecné

VÝSTUP: Vrať zápis jako jednotlivé sekce oddělené značkami ===SEKCE===.
Každá sekce obsahuje HTML obsah (bez nadpisu — ten přidáme sami).
HTML: <ul><li>, <strong>, <table> — žádné inline styly.

Použij PŘESNĚ tuto strukturu:

===PARTICIPANTS_COMMAREC===
<p>Jméno — role (např. senior konzultant pro logistiku)</p>
===PARTICIPANTS_COMPANY===
<p>Jméno — funkce (např. vedoucí logistiky)</p>
===INTRODUCTION===
<p>Kontext návštěvy: kde, proč, co bylo v centru pozornosti. 2-3 věty.</p>
===MEETING_GOAL===
<p>Konkrétní cíl schůzky — co jsme chtěli zjistit nebo vyřešit.</p>
===FINDINGS===
<ul>
<li><strong>Oblast:</strong> Konkrétní zjištění s čísly z přepisu</li>
<li><strong>Oblast:</strong> Pozitivní nebo negativní nález — věcně</li>
</ul>
===RATINGS===
<table><tr><th>Oblast</th><th>Hodnocení (%)</th><th>Komentář</th></tr>
<tr><td>Název oblasti</td><td>65</td><td>Konkrétní zdůvodnění hodnocení</td></tr>
<tr><td colspan="3"><strong>Celkové skóre: XX %</strong> | Nejlepší: Oblast | Nejkritičtější: Oblast</td></tr>
</table>
===PROCESSES_DESCRIPTION===
<p>Jak procesy skutečně fungují — příjem, skladování, pick, expedice, doprava. Co funguje, co ne.</p>
===DANGERS===
<ul>
<li><strong>Problém</strong>: Popis problému → Riziko: konkrétní dopad nebo hrozba</li>
</ul>
===SUGGESTED_ACTIONS===
<p><strong>Krátkodobě (0–1 měsíc):</strong></p>
<ul><li><strong>Akce:</strong> Co konkrétně udělat a proč</li></ul>
<p><strong>Střednědobě (1–3 měsíce):</strong></p>
<ul><li><strong>Akce:</strong> Co konkrétně udělat</li></ul>
===EXPECTED_BENEFITS===
<ul>
<li><strong>XX % úspora / zlepšení oblasti</strong> — Jak toho dosáhnout a za jak dlouho</li>
</ul>
===ADDITIONAL_NOTES===
<p>Atmosféra, překvapení, zajímavé momenty z návštěvy. Co nezaznělo v číslech ale bylo cítit.</p>
===SUMMARY===
<p>Shrnutí v max. 3-4 větách: kde klient stojí, co jsou TOP 3 priority a jaký je potenciál.</p>
===TASKS===
UKOL: Název úkolu (max 80 znaků, konkrétní akce)
POPIS: Co přesně udělat, kdo to udělá, jaký je výstup
TERMIN: do X týdnů/měsíců
---
UKOL: Další úkol
POPIS: Popis
TERMIN: do X měsíců

PRAVIDLA:
- Sekce RATINGS: hodnocení 0–100 %, poslední řádek = celkové skóre
- Sekce TASKS: 3–8 úkolů, pouze práce Commarec (audit, analýza, optimalizace, workshop)
- Piš v češtině s diakritikou
- Nedomýšlej informace které nezazněly — piš jen to co je v přepisu
- Pokud byl zadán interní prompt, zapracuj ho do obsahu sekcí (ne jako samostatnou sekci)
"""

def get_template_prompt(template_key):
    """Vrátí system prompt pro šablonu — z DB nebo výchozí."""
    try:
        cfg = TemplateConfig.query.filter_by(template_key=template_key).first()
        if cfg and cfg.system_prompt and cfg.system_prompt.strip():
            return cfg.system_prompt.strip()
    except Exception:
        pass
    return TEMPLATE_PROMPTS.get(template_key, TEMPLATE_PROMPTS["audit"])


# Fixní instrukce formátu — VŽDY přidána na konec, nelze přepsat vlastním promptem
FORMAT_INSTRUCTIONS = """

=== POVINNÝ FORMÁT VÝSTUPU ===
Výstup MUSÍ používat přesně tyto markery pro sekce (nic jiného!):
===PARTICIPANTS_COMMAREC===
obsah sekce jako HTML (<p>, <ul><li>, <strong>)
===PARTICIPANTS_COMPANY===
obsah...
===INTRODUCTION===
obsah...
===MEETING_GOAL===
obsah...
===FINDINGS===
obsah...
===RATINGS===
<table>...</table>
===PROCESSES_DESCRIPTION===
obsah...
===DANGERS===
obsah...
===SUGGESTED_ACTIONS===
obsah...
===EXPECTED_BENEFITS===
obsah...
===ADDITIONAL_NOTES===
obsah...
===SUMMARY===
obsah...
===TASKS===
UKOL: název
POPIS: popis
TERMIN: termín
---

KRITICKÉ: Výstup nesmí začínat žádným úvodem, JSON, nebo markdown. Pouze ===SEKCE=== markery.
Nepoužívej emotikony. Obsah sekcí je HTML (ne markdown). Piš česky s diakritikou.
"""


def build_system_prompt(interni_prompt="", klient_profil=None, template="audit"):
    prompt = get_template_prompt(template)
    if klient_profil:
        profil_str = ", ".join(f"{k}: {v}" for k, v in klient_profil.items() if v)
        if profil_str:
            prompt += f"\n\n### PROFIL KLIENTA: {profil_str}"
    if interni_prompt and interni_prompt.strip():
        prompt += f"\n\n### INTERNÍ INSTRUKCE (splnit na 100 %): {interni_prompt.strip()}"
    # Vždy přidej fixní instrukce formátu — i při vlastním promptu ze správy šablon
    prompt += FORMAT_INSTRUCTIONS
    return prompt

def build_header_html(client_info):
    return f"""<div class="zapis-header-block">
<strong>Datum:</strong> {client_info.get('meeting_date','')}<br>
<strong>Zastoupení Commarec:</strong> {client_info.get('commarec_rep','')}<br>
<strong>Zastoupení klienta:</strong> {client_info.get('client_contact','')} ({client_info.get('client_name','')})<br>
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
    """Smart truncation bez API call — zachová začátek, střed a konec přepisu.
    Pro přepisy > 60k znaků (cca 2h+) zachová 50k nejdůležitějších znaků.
    """
    MAX_CHARS = 50000  # ~14k tokenů — dost pro kvalitní výstup, rychlé zpracování
    if len(transcript) <= MAX_CHARS:
        return transcript

    # Zachovej začátek (30%), střed (40%), konec (30%) — nejdůležitější části
    part = MAX_CHARS // 3
    start = transcript[:part]
    mid_start = (len(transcript) - part) // 2
    middle = transcript[mid_start:mid_start + part]
    end = transcript[-part:]

    # Ořízni na celé věty/odstavce
    separator = "\n\n[... část přepisu vynechána pro rychlost zpracování ...]\n\n"
    condensed = start + separator + middle + separator + end

    app.logger.info(f"Smart truncation: {len(transcript)} -> {len(condensed)} chars (no API call)")
    return condensed

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

class Nabidka(db.Model):
    __tablename__ = "nabidka"
    id          = db.Column(db.Integer, primary_key=True)
    cislo       = db.Column(db.String(50), unique=True, nullable=False)  # napr. NAB-2026-001
    klient_id   = db.Column(db.Integer, db.ForeignKey("klient.id"), nullable=False)
    projekt_id  = db.Column(db.Integer, db.ForeignKey("projekt.id"), nullable=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    nazev       = db.Column(db.String(300), nullable=False)
    poznamka    = db.Column(db.Text, default="")
    platnost_do = db.Column(db.Date, nullable=True)
    stav        = db.Column(db.String(30), default="draft")  # draft, odeslana, prijata, zamitnuta
    mena        = db.Column(db.String(10), default="CZK")
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    klient      = db.relationship("Klient", backref="nabidky")
    projekt     = db.relationship("Projekt", backref="nabidky")
    konzultant  = db.relationship("User", backref="nabidky")
    polozky     = db.relationship("NabidkaPolozka", backref="nabidka",
                                   cascade="all, delete-orphan", order_by="NabidkaPolozka.poradi")

    @property
    def celkova_cena(self):
        return sum(p.celkem_bez_dph for p in self.polozky)

    @property
    def celkova_dph(self):
        return sum(p.dph_castka for p in self.polozky)

    @property
    def celkova_cena_s_dph(self):
        return self.celkova_cena + self.celkova_dph

class NabidkaPolozka(db.Model):
    __tablename__ = "nabidka_polozka"
    id          = db.Column(db.Integer, primary_key=True)
    nabidka_id  = db.Column(db.Integer, db.ForeignKey("nabidka.id"), nullable=False)
    poradi      = db.Column(db.Integer, default=0)
    nazev       = db.Column(db.String(300), nullable=False)
    popis       = db.Column(db.Text, default="")
    mnozstvi    = db.Column(db.Numeric(10, 2), default=1)
    jednotka    = db.Column(db.String(30), default="ks")  # ks, m, m2, hod, paušál
    cena_ks     = db.Column(db.Numeric(12, 2), default=0)
    sleva_pct   = db.Column(db.Numeric(5, 2), default=0)
    dph_pct     = db.Column(db.Numeric(5, 2), default=0)  # 0 = bez DPH, 21 = 21%, atd.

    @property
    def celkem_bez_dph(self):
        zaklad = float(self.mnozstvi) * float(self.cena_ks)
        return zaklad * (1 - float(self.sleva_pct) / 100)

    @property
    def celkem(self):
        return self.celkem_bez_dph

    @property
    def dph_castka(self):
        return self.celkem_bez_dph * float(self.dph_pct) / 100

    @property
    def celkem_s_dph(self):
        return self.celkem_bez_dph + self.dph_castka


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        # Klient má vlastní portál
        if session.get("user_role") == "klient":
            return redirect(url_for("klient_portal"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    """Pouze superadmin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or user.role != "superadmin":
            return abort(403)
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    """Povolí přístup jen uživatelům s jednou z uvedených rolí."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            user = User.query.get(session["user_id"])
            if not user or user.role not in roles:
                return abort(403)
            return f(*args, **kwargs)
        return decorated
    return decorator

def get_current_user():
    """Vrátí aktuálně přihlášeného uživatele nebo None."""
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)

# ─────────────────────────────────────────────
# OPRÁVNĚNÍ — co smí která role
# ─────────────────────────────────────────────
ROLE_PERMISSIONS = {
    # superadmin: vše (kontroluje se zvlášť)
    "admin": {
        "edit_zapis_any", "delete_zapis", "manage_klient", "freelo_setup",
        "nabidky", "nabidky_any", "send_freelo", "view_all",
        "create_zapis", "edit_zapis_own",
    },
    "konzultant": {
        "create_zapis", "edit_zapis_own", "send_freelo", "view_all",
    },
    "obchodnik": {
        "nabidky", "nabidky_any", "view_all",
    },
    "junior": {
        "create_zapis", "edit_zapis_own", "view_assigned",
    },
    "klient": {
        "portal_only",
    },
}

def can(action, obj=None):
    """Kontrola zda má aktuální uživatel dané oprávnění."""
    u = get_current_user()
    if not u:
        return False
    if u.role == "superadmin":
        return True
    perms = ROLE_PERMISSIONS.get(u.role, set())
    if action in perms:
        # edit_zapis_own — jen vlastní zápis
        if action == "edit_zapis_own" and obj and hasattr(obj, "user_id"):
            return obj.user_id == u.id
        return True
    # edit_zapis — obecná kontrola (any nebo own)
    if action == "edit_zapis":
        if "edit_zapis_any" in perms:
            return True
        if "edit_zapis_own" in perms:
            if obj and hasattr(obj, "user_id"):
                return obj.user_id == u.id
            return True
    return False

# ─────────────────────────────────────────────
# ROUTES — AUTH
# ─────────────────────────────────────────────


@app.route("/home")
@login_required  
def home():
    """Nový dashboard — status overview rozcestník."""
    now = datetime.utcnow()
    cutoff_60 = now - timedelta(days=60)
    cutoff_30 = now - timedelta(days=30)

    klienti_all = Klient.query.filter_by(is_active=True).all()
    
    stats = {
        "klienti_aktivni": len(klienti_all),
        "projekty_aktivni": Projekt.query.filter_by(is_active=True).count(),
        "zapisy_celkem": Zapis.query.count(),
        "zapisy_30d": Zapis.query.filter(Zapis.created_at >= cutoff_30).count(),
        "bez_aktivity": 0,
        "nabidky_otevrene": Nabidka.query.filter(
            Nabidka.stav.in_(["draft", "odeslana"])
        ).count(),
    }

    pozor_klienti = []
    for k in klienti_all:
        posledni = Zapis.query.filter_by(klient_id=k.id)            .order_by(Zapis.created_at.desc()).first()
        if not posledni or posledni.created_at < cutoff_60:
            dni = (now - posledni.created_at).days if posledni else 999
            if dni > 60:
                pozor_klienti.append({"klient": k, "posledni": posledni, "dni": min(dni, 999)})
    stats["bez_aktivity"] = len(pozor_klienti)
    pozor_klienti = sorted(pozor_klienti, key=lambda x: -x["dni"])[:5]

    aktivita = []
    for z in Zapis.query.order_by(Zapis.created_at.desc()).limit(12).all():
        aktivita.append({
            "typ": z.template or "audit",
            "typ_label": {"audit": "Audit", "operativa": "Operativa", "obchod": "Obchod"}.get(z.template, "Zápis"),
            "title": z.title or (z.projekt.nazev if z.projekt else k.nazev if z.klient else "Zápis"),
            "klient": z.klient.nazev if z.klient else "",
            "projekt": z.projekt.nazev if z.projekt else "",
            "datum": z.created_at,
            "url": url_for("detail_zapisu", zapis_id=z.id),
        })
    for n in Nabidka.query.order_by(Nabidka.created_at.desc()).limit(5).all():
        aktivita.append({
            "typ": "nabidka",
            "typ_label": "Nabídka",
            "title": f"{n.cislo} — {n.nazev}",
            "klient": n.klient.nazev if n.klient else "",
            "projekt": n.projekt.nazev if n.projekt else "",
            "datum": n.created_at,
            "url": url_for("nabidka_detail", nabidka_id=n.id),
        })
    aktivita.sort(key=lambda x: x["datum"], reverse=True)
    aktivita = aktivita[:15]

    aktivni_projekty = Projekt.query.filter_by(is_active=True)        .order_by(db.case((Projekt.datum_do == None, 1), else_=0), Projekt.datum_do.asc())        .limit(8).all()

    current_user = User.query.get(session["user_id"])

    return render_template("dashboard_new.html",
                           stats=stats, aktivita=aktivita,
                           pozor_klienti=pozor_klienti,
                           aktivni_projekty=aktivni_projekty,
                           now=now,
                           current_user=current_user)

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("prehled"))
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
    zapisy  = Zápisy_query()
    klienti = Klient.query.filter_by(is_active=True).order_by(Klient.nazev).all()
    stats = {
        "celkem":  Zapis.query.count(),
        "freelo":  Zapis.query.filter_by(freelo_sent=True).count(),
        "klienti": Klient.query.filter_by(is_active=True).count(),
        "projekty": Projekt.query.filter_by(is_active=True).count(),
    }
    return render_template("dashboard.html", zapisy=zapisy, klienti=klienti,
                           stats=stats, template_names=TEMPLATE_NAMES)

def Zápisy_query():
    return Zapis.query.order_by(Zapis.created_at.desc()).limit(30).all()

# ─────────────────────────────────────────────
# ROUTES — KLIENTI
# ─────────────────────────────────────────────


# ─── LOGO UPLOAD HELPER ─────────────────────────────────────────────
ALLOWED_LOGO_EXT = {'png', 'jpg', 'jpeg', 'svg', 'webp'}
MAX_LOGO_BYTES   = 2 * 1024 * 1024  # 2 MB

def save_klient_logo(file_obj, klient_id):
    """Uloží logo klienta do static/logos/, vrátí URL nebo None."""
    if not file_obj or not file_obj.filename:
        return None
    ext = file_obj.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_LOGO_EXT:
        return None
    file_obj.seek(0, 2)
    size = file_obj.tell()
    file_obj.seek(0)
    if size > MAX_LOGO_BYTES:
        return None
    filename = secure_filename(f"klient_{klient_id}_{secrets.token_hex(6)}.{ext}")
    upload_dir = os.path.join(app.root_path, 'static', 'logos')
    os.makedirs(upload_dir, exist_ok=True)
    file_obj.save(os.path.join(upload_dir, filename))
    return f"/static/logos/{filename}"
# ────────────────────────────────────────────────────────────────────


def send_welcome_email(to_email, to_name, password):
    """Odešle uvítací email novému uživateli s přihlašovacími údaji."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)

    if not smtp_host or not smtp_user:
        app.logger.warning("SMTP not configured — welcome email not sent")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Přístup do Commarec Zápisy"
        msg["From"]    = f"Commarec Zápisy <{smtp_from}>"
        msg["To"]      = to_email

        app_url = os.environ.get("APP_URL", "https://web-production-76f2.up.railway.app")

        html = f"""
        <div style="font-family:Arial,sans-serif;max-width:500px;margin:0 auto;padding:32px;">
          <img src="{app_url}/static/logo-dark.svg" alt="Commarec" style="height:32px;margin-bottom:24px;">
          <h2 style="color:#173767;font-size:22px;margin-bottom:8px;">Vítejte, {to_name}</h2>
          <p style="color:#4A6080;margin-bottom:24px;">Byl vám vytvořen přístup do aplikace Commarec Zápisy.</p>
          <table style="background:#f7f9fb;border-radius:8px;padding:20px;width:100%;border-collapse:collapse;">
            <tr><td style="padding:8px 12px;color:#4A6080;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;">Přihlašovací URL</td>
                <td style="padding:8px 12px;"><a href="{app_url}" style="color:#173767;font-weight:700;">{app_url}</a></td></tr>
            <tr><td style="padding:8px 12px;color:#4A6080;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;">Email</td>
                <td style="padding:8px 12px;font-weight:600;">{to_email}</td></tr>
            <tr><td style="padding:8px 12px;color:#4A6080;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;">Heslo</td>
                <td style="padding:8px 12px;font-weight:700;font-size:18px;letter-spacing:0.1em;color:#173767;">{password}</td></tr>
          </table>
          <p style="color:#8aa0b8;font-size:12px;margin-top:24px;">Po prvním přihlášení si heslo změňte. Tento email byl vygenerován automaticky.</p>
          <p style="color:#8aa0b8;font-size:12px;margin-top:4px;">Commarec s.r.o. · Varšavská 715/36, Praha 2</p>
        </div>"""

        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, to_email, msg.as_string())

        app.logger.info(f"Welcome email sent to {to_email}")
        return True
    except Exception as e:
        app.logger.warning(f"Email send failed: {e}")
        return False


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
            return render_template("klient_form.html", klient=None, error="Název je povinný")
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
        db.session.flush()  # získáme k.id
        logo_url = save_klient_logo(request.files.get('logo'), k.id)
        if logo_url:
            k.logo_url = logo_url
        db.session.commit()
        return redirect(url_for("klient_detail", klient_id=k.id))
    return render_template("klient_form.html", klient=None)



# ─────────────────────────────────────────────
# FREELO ÚKOLY — FÁZE 2
# ─────────────────────────────────────────────

@app.route("/api/freelo/projekt/<int:projekt_id>/ukoly")
@login_required
def freelo_projekt_ukoly(projekt_id):
    """Načte úkoly z Freelo pro daný projekt (přes uložený tasklist_id)."""
    p = Projekt.query.get_or_404(projekt_id)
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"ukoly": [], "error": "Freelo credentials chybí"})
    if not p.freelo_tasklist_id:
        return jsonify({"ukoly": [], "error": "Projekt nemá propojený Freelo tasklist"})
    try:
        resp = freelo_get(f"/tasklist/{p.freelo_tasklist_id}")
        if resp.status_code != 200:
            return jsonify({"ukoly": [], "error": f"Freelo API {resp.status_code}"})
        tasks_raw = resp.json().get("data", [])
        ukoly = []
        for t in tasks_raw:
            if not isinstance(t, dict):
                continue
            assignees = t.get("assigned_users") or []
            ukoly.append({
                "id": t.get("id"),
                "name": t.get("name", ""),
                "is_done": t.get("is_done", False),
                "due_date": t.get("due_date"),
                "assignee": assignees[0].get("fullname", "") if assignees else "",
                "url": f"https://app.freelo.io/task/{t.get('id')}",
                "created_at": t.get("created_at"),
                "finished_at": t.get("finished_at"),
            })
        done = sum(1 for u in ukoly if u["is_done"])
        return jsonify({"ukoly": ukoly, "done": done, "total": len(ukoly)})
    except Exception as e:
        return jsonify({"ukoly": [], "error": str(e)})


@app.route("/projekt/<int:projekt_id>/nastavit-freelo", methods=["POST"])
@login_required
def projekt_nastavit_freelo(projekt_id):
    """Uloží Freelo project_id a tasklist_id k projektu."""
    p = Projekt.query.get_or_404(projekt_id)
    p.freelo_project_id = request.form.get("freelo_project_id", type=int) or None
    p.freelo_tasklist_id = request.form.get("freelo_tasklist_id", type=int) or None
    db.session.commit()
    return redirect(request.referrer or url_for("projekt_detail", projekt_id=p.id))


# ─────────────────────────────────────────────
# PROGRESS REPORT — FÁZE 3
# ─────────────────────────────────────────────

@app.route("/progress-report")
@login_required
def progress_report():
    """Progress report za zvolené období — per klient, per projekt."""
    od_str = request.args.get("od")
    do_str = request.args.get("do")

    # Defaultně: poslední 30 dní
    do_dt = datetime.utcnow()
    od_dt = do_dt - timedelta(days=30)
    if od_str:
        try: od_dt = datetime.strptime(od_str, "%Y-%m-%d")
        except: pass
    if do_str:
        try: do_dt = datetime.strptime(do_str, "%Y-%m-%d")
        except: pass

    klienti = Klient.query.filter_by(is_active=True).order_by(Klient.nazev).all()
    report_data = []

    for k in klienti:
        projekty = Projekt.query.filter_by(klient_id=k.id, is_active=True).all()
        if not projekty:
            continue

        klient_data = {"klient": k, "projekty": []}

        for p in projekty:
            # Zápisy v období
            zapisy_v_obdobi = Zapis.query.filter(
                Zapis.projekt_id == p.id,
                Zapis.created_at >= od_dt,
                Zapis.created_at <= do_dt,
            ).order_by(Zapis.created_at.desc()).all()

            # Všechny zápisy projektu pro kontext
            vsechny_zapisy = Zapis.query.filter_by(projekt_id=p.id)                .order_by(Zapis.created_at.desc()).all()

            # Úkoly ze zápisů (tasks_json)
            ukoly_splnene = []
            ukoly_otevrene = []
            for z in vsechny_zapisy:
                try:
                    tasks = json.loads(z.tasks_json or "[]")
                    for t in tasks:
                        if isinstance(t, dict) and t.get("name"):
                            # Přidej timestamp zápisu
                            t["zapis_datum"] = z.created_at.strftime("%d. %m. %Y")
                            t["zapis_id"] = z.id
                            if t.get("done"):
                                ukoly_splnene.append(t)
                            else:
                                ukoly_otevrene.append(t)
                except: pass

            # Skóre z auditů
            skore_list = []
            for z in vsechny_zapisy:
                if z.template == "audit" and z.output_json:
                    try:
                        import re as _re
                        data = json.loads(z.output_json)
                        ratings = data.get("ratings", "")
                        m = _re.search(r"Celkov[eé][^0-9]*([0-9]+) *%", ratings)
                        if m:
                            skore_list.append({
                                "skore": int(m.group(1)),
                                "datum": z.created_at.strftime("%d. %m. %Y"),
                                "zapis_id": z.id,
                            })
                    except: pass

            # Freelo live hotové úkoly v období
            freelo_splnene = []
            if k.freelo_tasklist_id and FREELO_API_KEY and FREELO_EMAIL:
                try:
                    fr = freelo_get(f"/tasklist/{k.freelo_tasklist_id}")
                    if fr.status_code == 200:
                        raw_fr = fr.json()
                        if isinstance(raw_fr, list):
                            tasks_raw = raw_fr
                        elif isinstance(raw_fr, dict):
                            tasks_raw = raw_fr.get("tasks", raw_fr.get("data", []))
                        else:
                            tasks_raw = []
                        for t in tasks_raw:
                            if not isinstance(t, dict):
                                continue
                            if t.get("state") == "done":
                                finished = t.get("finished_at", "")
                                if finished:
                                    try:
                                        fin_dt = datetime.strptime(finished[:10], "%Y-%m-%d")
                                        if od_dt <= fin_dt <= do_dt + timedelta(days=1):
                                            freelo_splnene.append({
                                                "name": t.get("name", ""),
                                                "finished_at": finished[:10],
                                                "assignee": t.get("worker", {}).get("fullname", "") if t.get("worker") else "",
                                                "url": f"https://app.freelo.io/task/{t.get('id')}",
                                            })
                                    except Exception:
                                        pass
                except Exception:
                    pass

            klient_data["projekty"].append({
                "projekt": p,
                "zapisy_v_obdobi": zapisy_v_obdobi,
                "vsechny_zapisy_count": len(vsechny_zapisy),
                "ukoly_splnene": ukoly_splnene[:10],
                "ukoly_otevrene": ukoly_otevrene[:15],
                "freelo_splnene": freelo_splnene,
                "skore_list": skore_list,
                "posledni_skore": skore_list[0]["skore"] if skore_list else None,
                "prvni_skore": skore_list[-1]["skore"] if len(skore_list) > 1 else None,
            })

        if any(pd["zapisy_v_obdobi"] or pd["skore_list"] for pd in klient_data["projekty"]):
            report_data.append(klient_data)

    return render_template("progress_report.html",
                           report_data=report_data,
                           od=od_dt, do=do_dt,
                           od_str=od_dt.strftime("%Y-%m-%d"),
                           do_str=do_dt.strftime("%Y-%m-%d"),
                           now=datetime.utcnow())

# ─────────────────────────────────────────────
# HLAVNÍ PŘEHLED (nová hlavní stránka)
# ─────────────────────────────────────────────

@app.route("/prehled")
@login_required
def prehled():
    """Hlavní stránka — přehled všech klientů s filtry, skóre a poslední aktivitou."""
    now = datetime.utcnow()
    filtr = request.args.get("filtr", "vse")
    hledat = request.args.get("q", "").strip()

    klienti_all = Klient.query.filter_by(is_active=True).order_by(Klient.nazev).all()
    cutoff_60 = now - timedelta(days=60)
    cutoff_30 = now - timedelta(days=30)

    prehled_data = []
    for k in klienti_all:
        if hledat and hledat.lower() not in k.nazev.lower() and hledat.lower() not in (k.kontakt or "").lower():
            continue
        zapisy = Zapis.query.filter_by(klient_id=k.id).order_by(Zapis.created_at.desc()).all()
        projekty = Projekt.query.filter_by(klient_id=k.id, is_active=True).all()
        nabidky = Nabidka.query.filter_by(klient_id=k.id).order_by(Nabidka.created_at.desc()).limit(3).all()
        posledni_zapis = zapisy[0] if zapisy else None

        # Filtry
        if filtr == "aktivni" and not projekty:
            continue
        if filtr == "bez_aktivity":
            if posledni_zapis and posledni_zapis.created_at > cutoff_60:
                continue
        if filtr == "tento_mesic":
            if not posledni_zapis or posledni_zapis.created_at < cutoff_30:
                continue

        # Skóre z auditů — vezmi první i poslední pro delta
        skore_list = []
        for z in zapisy:
            if z.template == "audit" and z.output_json and z.output_json != "{}":
                try:
                    import re as _re
                    data = json.loads(z.output_json)
                    ratings = data.get("ratings", "") or data.get("hodnoceni", "")
                    m = _re.search(r"Celkov[eé][^0-9]*([0-9]+)\s*%", ratings)
                    if m:
                        skore_list.append({"skore": int(m.group(1)), "datum": z.created_at})
                except Exception:
                    pass

        posledni_skore = skore_list[0]["skore"] if skore_list else None
        prvni_skore = skore_list[-1]["skore"] if len(skore_list) > 1 else None
        delta = (posledni_skore - prvni_skore) if (posledni_skore is not None and prvni_skore is not None) else None

        # Otevřené úkoly
        ukoly_otevrene = 0
        for z in zapisy[:5]:
            try:
                tasks = json.loads(z.tasks_json or "[]")
                ukoly_otevrene += sum(1 for t in tasks if isinstance(t, dict) and t.get("name") and not t.get("done"))
            except Exception:
                pass

        prehled_data.append({
            "klient": k,
            "zapisy_count": len(zapisy),
            "projekty": projekty,
            "posledni_zapis": posledni_zapis,
            "nabidky": nabidky,
            "skore": posledni_skore,
            "delta": delta,
            "ukoly_otevrene": ukoly_otevrene,
        })

    stats = {
        "klienti": len(prehled_data),
        "zapisy_30d": Zapis.query.filter(Zapis.created_at >= cutoff_30).count(),
        "nabidky_otevrene": Nabidka.query.filter(Nabidka.stav.in_(["draft", "odeslana"])).count(),
        "projekty": Projekt.query.filter_by(is_active=True).count(),
    }

    return render_template("prehled.html",
                           prehled_data=prehled_data, filtr=filtr, hledat=hledat,
                           stats=stats, template_names=TEMPLATE_NAMES, now=now)


# ─────────────────────────────────────────────
# CRM PŘEHLED
# ─────────────────────────────────────────────

@app.route("/crm")
@login_required
def crm_prehled():
    klienti = Klient.query.filter_by(is_active=True).order_by(Klient.nazev).all()
    filtr = request.args.get("filtr", "vse")
    hledat = request.args.get("q", "").strip()

    # Sestav data per klient
    crm_data = []
    for k in klienti:
        if hledat and hledat.lower() not in k.nazev.lower():
            continue
        zapisy = Zapis.query.filter_by(klient_id=k.id).order_by(Zapis.created_at.desc()).all()
        projekty = Projekt.query.filter_by(klient_id=k.id, is_active=True).all()
        posledni_zapis = zapisy[0] if zapisy else None
        nabidky = Nabidka.query.filter_by(klient_id=k.id).order_by(Nabidka.created_at.desc()).limit(3).all()

        # Filtr
        if filtr == "aktivni" and not projekty:
            continue
        if filtr == "bez_aktivity" and posledni_zapis:
            if posledni_zapis.created_at > datetime.utcnow() - timedelta(days=60):
                continue
        if filtr == "tento_mesic" and posledni_zapis:
            if posledni_zapis.created_at < datetime.utcnow() - timedelta(days=30):
                continue

        # Poslední skóre z auditního zápisu
        posledni_skore = None
        for z in zapisy:
            if z.template == "audit" and z.output_json and z.output_json != "{}":
                try:
                    data = json.loads(z.output_json)
                    ratings = data.get("ratings", "")
                    import re
                    m = re.search(r"Celkov[eé][^\\d]*(\\d+)\\s*%", ratings)
                    if m:
                        posledni_skore = int(m.group(1))
                        break
                except Exception:
                    pass

        crm_data.append({
            "klient": k,
            "zapisy_count": len(zapisy),
            "projekty": projekty,
            "posledni_zapis": posledni_zapis,
            "nabidky": nabidky,
            "skore": posledni_skore,
        })

    return render_template("crm.html", crm_data=crm_data, filtr=filtr, hledat=hledat,
                           template_names=TEMPLATE_NAMES, now=datetime.utcnow())


# ─────────────────────────────────────────────
# NABÍDKY
# ─────────────────────────────────────────────

@app.route("/nabidka/nova", methods=["GET", "POST"])
@login_required
def nabidka_nova():
    klienti = Klient.query.filter_by(is_active=True).order_by(Klient.nazev).all()
    klient_id = request.args.get("klient_id", type=int)
    projekt_id = request.args.get("projekt_id", type=int)

    if request.method == "POST":
        klient_id = request.form.get("klient_id", type=int)
        # Generuj číslo nabídky
        rok = datetime.utcnow().year
        pocet = Nabidka.query.filter(
            db.func.extract("year", Nabidka.created_at) == rok
        ).count()
        cislo = f"NAB-{rok}-{(pocet+1):03d}"

        n = Nabidka(
            cislo=cislo,
            klient_id=klient_id,
            projekt_id=request.form.get("projekt_id", type=int) or None,
            user_id=session["user_id"],
            nazev=request.form.get("nazev", "").strip(),
            poznamka=request.form.get("poznamka", "").strip(),
            stav="draft",
            mena=request.form.get("mena", "CZK"),
        )
        if request.form.get("platnost_do"):
            from datetime import date as date_type
            n.platnost_do = datetime.strptime(request.form["platnost_do"], "%Y-%m-%d").date()
        db.session.add(n)
        db.session.flush()

        # Položky
        nazvy = request.form.getlist("pol_nazev")
        popisy = request.form.getlist("pol_popis")
        mnozstvi = request.form.getlist("pol_mnozstvi")
        jednotky = request.form.getlist("pol_jednotka")
        ceny = request.form.getlist("pol_cena")
        slevy = request.form.getlist("pol_sleva")

        for i, nazev in enumerate(nazvy):
            if not nazev.strip():
                continue
            p = NabidkaPolozka(
                nabidka_id=n.id,
                poradi=i,
                nazev=nazev.strip(),
                popis=popisy[i] if i < len(popisy) else "",
                mnozstvi=float(mnozstvi[i]) if i < len(mnozstvi) and mnozstvi[i] else 1,
                jednotka=jednotky[i] if i < len(jednotky) else "ks",
                cena_ks=float(ceny[i]) if i < len(ceny) and ceny[i] else 0,
                sleva_pct=float(slevy[i]) if i < len(slevy) and slevy[i] else 0,
                dph_pct=float(request.form.getlist("pol_dph")[i]) if i < len(request.form.getlist("pol_dph")) and request.form.getlist("pol_dph")[i] else 0,
            )
            db.session.add(p)

        db.session.commit()
        return redirect(url_for("nabidka_detail", nabidka_id=n.id))

    k = Klient.query.get(klient_id) if klient_id else None
    projekty = Projekt.query.filter_by(klient_id=klient_id).all() if klient_id else []
    return render_template("nabidka_nova.html", klienti=klienti, klient=k,
                           projekty=projekty, klient_id=klient_id, projekt_id=projekt_id)


@app.route("/nabidka/<int:nabidka_id>")
@login_required
def nabidka_detail(nabidka_id):
    n = Nabidka.query.get_or_404(nabidka_id)
    return render_template("nabidka_detail.html", n=n)


@app.route("/nabidka/<int:nabidka_id>/polozka/pridat", methods=["POST"])
@login_required
def nabidka_polozka_pridat(nabidka_id):
    n = Nabidka.query.get_or_404(nabidka_id)
    p = NabidkaPolozka(
        nabidka_id=n.id,
        poradi=len(n.polozky),
        nazev=request.form.get("nazev", "Nová položka"),
        mnozstvi=1, cena_ks=0, jednotka="ks",
    )
    db.session.add(p)
    db.session.commit()
    return redirect(url_for("nabidka_detail", nabidka_id=n.id))


@app.route("/nabidka/<int:nabidka_id>/polozka/<int:pol_id>/smazat", methods=["POST"])
@login_required
def nabidka_polozka_smazat(nabidka_id, pol_id):
    p = NabidkaPolozka.query.get_or_404(pol_id)
    db.session.delete(p)
    db.session.commit()
    return ("", 204)


@app.route("/nabidka/<int:nabidka_id>/ulozit", methods=["POST"])
@login_required
def nabidka_ulozit(nabidka_id):
    """Uloží všechny položky z AJAX POST (JSON)."""
    n = Nabidka.query.get_or_404(nabidka_id)
    data = request.get_json()
    if not data:
        return jsonify(ok=False), 400

    # Update hlavičky
    if "nazev" in data: n.nazev = data["nazev"]
    if "poznamka" in data: n.poznamka = data["poznamka"]
    if "stav" in data: n.stav = data["stav"]

    # Update položek
    for pol_data in data.get("polozky", []):
        pol_id = pol_data.get("id")
        if pol_id:
            p = NabidkaPolozka.query.get(pol_id)
            if p and p.nabidka_id == n.id:
                p.nazev = pol_data.get("nazev", p.nazev)
                p.popis = pol_data.get("popis", p.popis)
                p.mnozstvi = float(pol_data.get("mnozstvi", p.mnozstvi))
                p.jednotka = pol_data.get("jednotka", p.jednotka)
                p.cena_ks = float(pol_data.get("cena_ks", p.cena_ks))
                p.sleva_pct = float(pol_data.get("sleva_pct", p.sleva_pct or 0))
                p.dph_pct = float(pol_data.get("dph_pct", p.dph_pct or 0))
        else:
            # Nová položka
            p = NabidkaPolozka(
                nabidka_id=n.id,
                poradi=pol_data.get("poradi", 99),
                nazev=pol_data.get("nazev", ""),
                popis=pol_data.get("popis", ""),
                mnozstvi=float(pol_data.get("mnozstvi", 1)),
                jednotka=pol_data.get("jednotka", "ks"),
                cena_ks=float(pol_data.get("cena_ks", 0)),
                sleva_pct=float(pol_data.get("sleva_pct", 0)),
                dph_pct=float(pol_data.get("dph_pct", 0)),
            )
            db.session.add(p)

    db.session.commit()
    return jsonify(ok=True, celkem=float(n.celkova_cena), dph=float(n.celkova_dph), celkem_s_dph=float(n.celkova_cena_s_dph), cislo=n.cislo)


@app.route("/nabidka/<int:nabidka_id>/stav", methods=["POST"])
@login_required
def nabidka_stav(nabidka_id):
    n = Nabidka.query.get_or_404(nabidka_id)
    n.stav = request.form.get("stav", n.stav)
    db.session.commit()
    return redirect(url_for("nabidka_detail", nabidka_id=n.id))

@app.route("/klient/<int:klient_id>")
@login_required
def klient_detail(klient_id):
    k = Klient.query.get_or_404(klient_id)
    projekty = Projekt.query.filter_by(klient_id=klient_id).order_by(Projekt.created_at.desc()).all()
    zapisy   = Zapis.query.filter_by(klient_id=klient_id).order_by(Zapis.created_at.desc()).all()
    nabidky  = Nabidka.query.filter_by(klient_id=klient_id).order_by(Nabidka.created_at.desc()).all()
    konzultanti = User.query.filter_by(is_active=True).all()
    try:
        profil = json.loads(k.profil_json or "{}")
    except Exception:
        profil = {}

    # Skóre history
    import re as _re
    skore_list = []
    for z in zapisy:
        if z.template == "audit" and z.output_json and z.output_json != "{}":
            try:
                data = json.loads(z.output_json)
                ratings = data.get("ratings", "") or data.get("hodnoceni", "")
                m = _re.search(r"Celkov[eé][^0-9]*([0-9]+)\s*%", ratings)
                if m:
                    skore_list.append({"skore": int(m.group(1)), "datum": z.created_at, "zapis_id": z.id})
            except Exception:
                pass

    # Otevřené úkoly napříč zápisy
    ukoly_otevrene = []
    for z in zapisy:
        try:
            tasks = json.loads(z.tasks_json or "[]")
            for t in tasks:
                if isinstance(t, dict) and t.get("name") and not t.get("done"):
                    t["zapis_id"] = z.id
                    t["zapis_title"] = z.title
                    ukoly_otevrene.append(t)
        except Exception:
            pass

    return render_template("klient_detail.html", k=k, projekty=projekty,
                           zapisy=zapisy, nabidky=nabidky, profil=profil,
                           skore_list=skore_list, ukoly_otevrene=ukoly_otevrene,
                           konzultanti=konzultanti, template_names=TEMPLATE_NAMES,
                           now=datetime.utcnow())


@app.route("/klient/<int:klient_id>/vyvoj")
@login_required
def klient_vyvoj(klient_id):
    k = Klient.query.get_or_404(klient_id)
    projekty = Projekt.query.filter_by(klient_id=klient_id).order_by(Projekt.created_at.desc()).all()
    zapisy   = Zapis.query.filter_by(klient_id=klient_id).order_by(Zapis.created_at.desc()).all()

    # Freelo úkoly — zatím prázdné, napojíme přes Freelo project ID na projektu
    freelo_tasks = {}

    try:
        profil = json.loads(k.profil_json or "{}") if hasattr(k, 'profil_json') else {}
    except Exception:
        profil = {}

    return render_template("klient_vyvoj.html",
                           k=k, projekty=projekty, zapisy=zapisy,
                           freelo_tasks=freelo_tasks,
                           profil=profil,
                           template_names=TEMPLATE_NAMES)

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
        logo_url = save_klient_logo(request.files.get('logo'), klient_id)
        if logo_url:
            k.logo_url = logo_url
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

@app.route("/api/klient/<int:klient_id>/poznamky", methods=["POST"])
@login_required
def api_klient_poznamky(klient_id):
    """Uloží interní poznámky ke klientovi."""
    k = Klient.query.get_or_404(klient_id)
    data = request.get_json()
    k.poznamka = data.get("poznamka", "")
    try:
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/klient/<int:klient_id>/upravit", methods=["POST"])
@login_required
def api_klient_upravit(klient_id):
    """Inline editace klienta přes JSON API."""
    k = Klient.query.get_or_404(klient_id)
    data = request.get_json()
    k.nazev   = data.get("nazev", k.nazev).strip()
    k.kontakt = data.get("kontakt", k.kontakt or "").strip()
    k.email   = data.get("email", k.email or "").strip()
    k.telefon = data.get("telefon", k.telefon or "").strip()
    k.adresa  = data.get("adresa", k.adresa or "").strip()
    k.sidlo   = data.get("sidlo", k.sidlo or "").strip()
    k.ic      = data.get("ic", k.ic or "").strip()
    k.dic     = data.get("dic", k.dic or "").strip()
    try:
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


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

def sanitize_summary(summary):
    """Oprav časté problémy v AI výstupu uloženém v DB."""
    if not isinstance(summary, dict):
        return {}
    cleaned = {}
    for key, val in summary.items():
        if not val:
            cleaned[key] = val
            continue
        val = str(val).strip()
        # JSON array ["x","y"] → <p>x, y</p>
        if val.startswith('[') and val.endswith(']'):
            try:
                items = json.loads(val)
                if isinstance(items, list):
                    val = "<p>" + ", ".join(str(i).strip('"') for i in items) + "</p>"
            except Exception:
                pass
        # Markdown bold **text** → <strong>text</strong>
        import re
        val = re.sub(r'[*][*](.+?)[*][*]', r'<strong></strong>', val)
        # Markdown bullet • nebo - na začátku řádku → <li>
        if '\n' in val and not val.strip().startswith('<'):
            lines = val.split('\n')
            html_lines = []
            in_ul = False
            for line in lines:
                line = line.strip()
                if not line:
                    if in_ul:
                        html_lines.append('</ul>')
                        in_ul = False
                    continue
                if line.startswith(('• ', '- ', '* ')):
                    if not in_ul:
                        html_lines.append('<ul>')
                        in_ul = True
                    html_lines.append(f'<li>{line[2:]}</li>')
                else:
                    if in_ul:
                        html_lines.append('</ul>')
                        in_ul = False
                    html_lines.append(f'<p>{line}</p>')
            if in_ul:
                html_lines.append('</ul>')
            val = '\n'.join(html_lines)
        cleaned[key] = val
    return cleaned


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
    # Sanitizuj hodnoty — oprav JSON arrays (["x","y"]) → HTML text
    summary = sanitize_summary(summary)

    # Tasklist klienta — pokud je nastaven, zápis ho použije automaticky (bez dropdownu)
    klient_tasklist_id = None
    klient_tasklist_name = None
    klient_project_name = None
    if zapis.klient and zapis.klient.freelo_tasklist_id:
        klient_tasklist_id = zapis.klient.freelo_tasklist_id
        # Pokus se načíst název tasklist z Freelo
        try:
            r = freelo_get(f"/tasklist/{klient_tasklist_id}")
            if r.status_code == 200:
                d = r.json()
                klient_tasklist_name = d.get("name", str(klient_tasklist_id))
        except Exception:
            klient_tasklist_name = str(klient_tasklist_id)

    return render_template("detail.html", zapis=zapis, tasks=tasks, notes=notes,
                           summary=summary, section_titles=SECTION_TITLES,
                           template_names=TEMPLATE_NAMES,
                           klient_tasklist_id=klient_tasklist_id,
                           klient_tasklist_name=klient_tasklist_name,
                           klient_project_name=klient_project_name)

@app.route("/zapis/verejny/<token>")
def zapis_verejny(token):
    zapis = Zapis.query.filter_by(public_token=token, is_public=True).first_or_404()
    try:
        summary = json.loads(zapis.output_json or "{}")
    except Exception:
        summary = {}
    summary = sanitize_summary(summary)
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

    # Zkondenzuj dlouhe prepisy — aby vystup AI nepresahl limit tokenu
    # Limit: 6000 znaku ~ 1700 tokenu vstupu, vystup pak snadno vejde do 8000 tokenu
    transcript = input_text
    if len(input_text) > 50000:  # Zkracuj jen opravdu dlouhé přepisy (>50k znaků = cca 2h+)
        try:
            app.logger.info(f"Condensing transcript: {len(input_text)} chars")
            transcript = condensed_transcript(ai, input_text)
            app.logger.info(f"Condensed to: {len(transcript)} chars")
        except Exception as e:
            app.logger.warning(f"Condensation failed, using original: {e}")

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

    system = build_system_prompt(interni_prompt, klient_profil, template)

    try:
        message = ai.messages.create(
            model="claude-sonnet-4-5", max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user_message}]
        )
        raw = message.content[0].text.strip()
        app.logger.info(f"AI response: {len(raw)} chars, stop={message.stop_reason}")
    except Exception as e:
        return jsonify({"error": f"Chyba API: {str(e)}"}), 500

    # Parse section markers ===SEKCE===
    SECTION_KEYS = [
        # Standardní sekce (všechny typy)
        "participants_commarec", "participants_company", "introduction", "meeting_goal",
        "findings", "ratings", "processes_description", "dangers", "suggested_actions",
        "expected_benefits", "additional_notes", "summary", "tasks",
        # Operativa
        "current_state",
        # Obchod
        "client_situation", "client_needs", "opportunities", "risks",
        "commercial_model", "next_steps", "expected_impact", "client_signals",
    ]

    def parse_sections(text):
        """Parsuje sekce z AI odpovědi. Zvládá různé formáty markerů.
        Také opravuje časté chyby: JSON pole místo HTML, raw text bez markerů.
        """
        result = {}
        current_key = None
        current_lines = []

        # Normalizuj alternativní markery na standard ===KEY===
        import re as _re
        # Zvládne: ## PARTICIPANTS_COMMAREC, # PARTICIPANTS_COMMAREC:, **PARTICIPANTS_COMMAREC**
        alt_marker = _re.compile(
            r'^(?:#+\s*|[*]{2})?([A-Z_]{3,30})(?:[:\s*]*)?$'
        )

        for line in text.split("\n"):
            stripped = line.strip()

            # Hlavní formát: ===KEY===
            if stripped.startswith("===") and stripped.endswith("==="):
                if current_key:
                    result[current_key] = "\n".join(current_lines).strip()
                inner = stripped.strip("=").strip()
                if inner.upper().startswith("SEKCE:"):
                    inner = inner[6:].strip()
                marker = inner.lower().replace(" ", "_").replace("-", "_")
                if marker in SECTION_KEYS:
                    current_key = marker
                    current_lines = []
                else:
                    current_key = None
                    current_lines = []

            # Fallback: alternativní markery (## PARTICIPANTS_COMMAREC)
            elif not current_key or not current_lines:
                m = alt_marker.match(stripped)
                if m:
                    candidate = m.group(1).lower()
                    if candidate in SECTION_KEYS:
                        if current_key:
                            result[current_key] = "\n".join(current_lines).strip()
                        current_key = candidate
                        current_lines = []
                        continue
                if current_key:
                    current_lines.append(line)
            else:
                current_lines.append(line)

        if current_key:
            result[current_key] = "\n".join(current_lines).strip()

        # Oprav hodnoty: JSON array ["x","y"] → <p>x, y</p>
        for k, v in result.items():
            if v and v.strip().startswith('[') and v.strip().endswith(']'):
                try:
                    import json as _json
                    items = _json.loads(v.strip())
                    if isinstance(items, list):
                        result[k] = "<p>" + ", ".join(str(i) for i in items) + "</p>"
                except Exception:
                    pass

        return result

    def parse_tasks(tasks_text):
        """Parsuje UKOL/POPIS/TERMIN bloky ze sekce TASKS."""
        tasks = []
        if not tasks_text:
            return tasks
        current = {}
        for line in tasks_text.split("\n"):
            line = line.strip()
            if line.startswith("UKOL:"):
                if current.get("name"):
                    tasks.append(current)
                current = {"name": line[5:].strip()[:200], "desc": "", "deadline": "dle dohody"}
            elif line.startswith("POPIS:") and current:
                current["desc"] = line[6:].strip()
            elif line.startswith("TERMIN:") and current:
                current["deadline"] = line[7:].strip()
            elif line == "---" and current.get("name"):
                tasks.append(current)
                current = {}
        if current.get("name"):
            tasks.append(current)
        return tasks[:8]

    summary_json = parse_sections(raw)
    app.logger.info(f"Parsed sections: {list(summary_json.keys())}")

    # Pokud parser nic nenasel — AI ignorovalo format, zkus znovu s pripomentim
    if not summary_json:
        app.logger.warning(f"No sections found, retrying. Raw start: {raw[:200]}")
        retry_msg = user_message + """

DULEZITE: Tvuj vystup MUSI zacinat presne takto (bez jakehokoliv uvodni textu):
===PARTICIPANTS_COMMAREC===
...obsah...
===PARTICIPANTS_COMPANY===
...obsah...
atd.

Pouzij PRESNE tyto markery, jinak aplikace zapis nezobrazi."""
        try:
            retry = ai.messages.create(
                model="claude-sonnet-4-5", max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": retry_msg}]
            )
            raw = retry.content[0].text.strip()
            summary_json = parse_sections(raw)
            app.logger.info(f"Retry parsed sections: {list(summary_json.keys())}")
        except Exception as e:
            app.logger.error(f"Retry failed: {e}")

    if not summary_json:
        app.logger.error(f"Both attempts failed. Raw: {raw[:400]}")
        return jsonify({"error": f"AI nevrátilo ocekávany format ani po opakování. Začátek odpovědi: {raw[:150]}"}), 500

    tasks = parse_tasks(summary_json.pop("tasks", ""))

    output_text = assemble_output_text(client_info, summary_json, blocks)

    # Build title
    client_name  = client_info.get("client_name","").strip()
    meeting_date = client_info.get("meeting_date","").strip()
    title = f"{client_name} - {meeting_date}" if client_name else f"Zapis {meeting_date}"

    # Auto-update client profile v pozadí (neblokuje odpověď)
    if klient_id:
        import threading
        def update_profil_bg(app_ctx, kid, text):
            with app_ctx:
                try:
                    k = Klient.query.get(kid)
                    if k:
                        ai_bg = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                        existing = json.loads(k.profil_json or "{}")
                        new_profil = extract_klient_profil(ai_bg, text[:10000], existing)
                        k.profil_json = json.dumps(new_profil, ensure_ascii=False)
                        db.session.commit()
                except Exception as e:
                    app.logger.warning(f"BG profile extraction failed: {e}")
        t = threading.Thread(target=update_profil_bg, args=(app.app_context(), int(klient_id), input_text), daemon=True)
        t.start()

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

def freelo_patch(path, payload):
    """PATCH request na Freelo API."""
    return requests.patch(f"https://api.freelo.io/v1{path}",
                          auth=freelo_auth(), headers={"Content-Type": "application/json"},
                          json=payload, timeout=15)

def freelo_delete(path):
    """DELETE request na Freelo API."""
    return requests.delete(f"https://api.freelo.io/v1{path}",
                           auth=freelo_auth(), headers={"Content-Type": "application/json"},
                           timeout=15)

# ─────────────────────────────────────────────
# FREELO — KLIENT NAPOJENÍ A PLNÁ SPRÁVA
# ─────────────────────────────────────────────

@app.route("/api/freelo/tasklists-all", methods=["GET"])
@login_required
def get_freelo_tasklists_all():
    """Načte všechny tasklists ze všech Freelo projektů."""
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"tasklists": [], "error": "Chybí FREELO credentials"})
    try:
        resp = freelo_get("/projects")
        if resp.status_code != 200:
            return jsonify({"tasklists": [], "error": f"Freelo {resp.status_code}"})
        raw = resp.json()
        projects = raw if isinstance(raw, list) else raw.get("data", [])
        tasklists = []
        for p in projects:
            for tl in p.get("tasklists", []):
                tasklists.append({
                    "id": tl.get("id"),
                    "name": tl.get("name"),
                    "project_name": p.get("name"),
                    "project_id": p.get("id"),
                })
        return jsonify({"tasklists": tasklists})
    except Exception as e:
        return jsonify({"tasklists": [], "error": str(e)})


@app.route("/api/klient/<int:klient_id>/freelo-nastavit", methods=["POST"])
@login_required
def api_klient_freelo_nastavit(klient_id):
    """Nastaví tasklist ID pro klienta."""
    k = Klient.query.get_or_404(klient_id)
    data = request.get_json()
    tasklist_id = data.get("tasklist_id")
    k.freelo_tasklist_id = int(tasklist_id) if tasklist_id else None
    try:
        db.session.commit()
        return jsonify({"ok": True, "tasklist_id": k.freelo_tasklist_id})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/klient/<int:klient_id>/freelo-ukoly", methods=["GET"])
@login_required
def api_klient_freelo_ukoly(klient_id):
    """Načte úkoly z Freelo tasklist klienta."""
    k = Klient.query.get_or_404(klient_id)
    if not k.freelo_tasklist_id:
        return jsonify({"ukoly": [], "not_configured": True})
    if not FREELO_API_KEY or not FREELO_EMAIL:
        return jsonify({"ukoly": [], "error": "Chybí FREELO credentials"})
    try:
        resp = freelo_get(f"/tasklist/{k.freelo_tasklist_id}")
        if resp.status_code != 200:
            return jsonify({"ukoly": [], "error": f"Freelo {resp.status_code}: {resp.text[:200]}"})
        raw2 = resp.json()
        # Freelo: GET /tasklist/{id} vrací {"id":..., "tasks":[...]}
        if isinstance(raw2, list):
            tasks_raw = raw2
        elif isinstance(raw2, dict):
            tasks_raw = raw2.get("tasks", raw2.get("data", []))
        else:
            tasks_raw = []

        # Zjisti project_id pro tento tasklist (potřebné pro editaci)
        tasklist_project_id = None
        try:
            resp_p = freelo_get("/projects")
            if resp_p.status_code == 200:
                raw_p = resp_p.json()
                projects_list = raw_p if isinstance(raw_p, list) else raw_p.get("data", raw_p.get("projects", []))
                for p in projects_list:
                    if not isinstance(p, dict): continue
                    for tl in p.get("tasklists", []):
                        if tl.get("id") == k.freelo_tasklist_id:
                            tasklist_project_id = p.get("id")
                            break
                    if tasklist_project_id:
                        break
        except Exception:
            pass

        ukoly = []
        for t in tasks_raw:
            if not isinstance(t, dict):
                continue
            # Freelo tasklist endpoint vrací zkrácená data — state je v GET /task/{id}
            # V tasklist: parent_task_id != null = podúkol
            state_raw = t.get("state", {})
            if isinstance(state_raw, dict):
                is_done = state_raw.get("state") in ("finished", "done") or state_raw.get("id", 1) > 1
            else:
                is_done = str(state_raw).lower() in ("finished", "done", "2", "3")
            ukoly.append({
                "id": t.get("id"),
                "name": t.get("name", ""),
                "state": "done" if (is_done or t.get("date_finished")) else "open",
                "deadline": (t.get("due_date") or t.get("due_date_end") or ""),
                "assignee": t.get("worker", {}).get("fullname", "") if t.get("worker") else "",
                "assignee_id": t.get("worker", {}).get("id") if t.get("worker") else None,
                "comments_count": t.get("comments_count", 0),
                "count_subtasks": t.get("count_subtasks", 0),
                "description": "",
                "url": f"https://app.freelo.io/task/{t.get('id')}",
                "finished_at": t.get("date_finished", ""),
                "created_at": t.get("date_add", ""),
                "is_subtask": bool(t.get("parent_task_id")),
                "parent_task_id": t.get("parent_task_id"),
                "project_id": tasklist_project_id,
                "tasklist_id": k.freelo_tasklist_id,
            })
        ukoly.sort(key=lambda x: (0 if x["state"] == "open" else 1, x.get("deadline") or "9999"))
        return jsonify({"ukoly": ukoly, "tasklist_id": k.freelo_tasklist_id})
    except Exception as e:
        return jsonify({"ukoly": [], "error": str(e)})


@app.route("/api/klient/<int:klient_id>/freelo-pridat-ukol", methods=["POST"])
@login_required
def api_klient_freelo_pridat_ukol(klient_id):
    """Vytvoří nový úkol v Freelo tasklist klienta."""
    k = Klient.query.get_or_404(klient_id)
    if not k.freelo_tasklist_id:
        return jsonify({"error": "Klient nemá nastavený tasklist"}), 400
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Název je povinný"}), 400
    try:
        # Najdi project_id podle tasklist_id
        resp_p = freelo_get("/projects")
        project_id = str(FREELO_PROJECT_ID)
        if resp_p.status_code == 200:
            raw_p = resp_p.json()
            projects_list = raw_p if isinstance(raw_p, list) else raw_p.get("data", raw_p.get("projects", []))
            for p in projects_list:
                if not isinstance(p, dict):
                    continue
                for tl in p.get("tasklists", []):
                    if tl.get("id") == k.freelo_tasklist_id:
                        project_id = str(p.get("id"))
                        break

        # Resolve assignee jméno → worker_id (stejně jako odeslat_do_freela)
        worker_id = None
        assignee_name = (data.get("assignee") or "").strip()
        if assignee_name:
            try:
                mr = freelo_get(f"/project/{project_id}/workers")
                if mr.status_code == 200:
                    for w in mr.json().get("data", {}).get("workers", []):
                        if w.get("fullname", "").lower() == assignee_name.lower():
                            worker_id = w["id"]
                            break
            except Exception:
                pass

        payload = {"name": name}
        if worker_id:
            payload["worker_id"] = worker_id
        if data.get("deadline"):
            payload["due_date"] = data["deadline"]

        resp = freelo_post(f"/project/{project_id}/tasklist/{k.freelo_tasklist_id}/tasks", payload)
        if resp.status_code in (200, 201):
            task_data = resp.json()
            task = task_data.get("data", task_data)
            task_id = task.get("id")

            # Přidej popis zvlášť (Freelo ho ignoruje při vytvoření)
            desc = (data.get("description") or "").strip()
            if task_id and desc:
                freelo_post(f"/task/{task_id}/description", {"content": f"<div>{desc}</div>"})

            return jsonify({"ok": True, "task": {
                "id": task_id,
                "name": name,
                "state": "open",
                "deadline": data.get("deadline", ""),
                "assignee": assignee_name,
                "assignee_id": worker_id,
                "comments_count": 0,
                "description": desc,
                "url": f"https://app.freelo.io/task/{task_id}",
                "project_id": int(project_id) if project_id else None,
                "tasklist_id": k.freelo_tasklist_id,
            }})
        return jsonify({"error": f"Freelo {resp.status_code}: {resp.text[:300]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/freelo/task/<int:task_id>/stav", methods=["POST"])
@login_required
def api_freelo_task_stav(task_id):
    """Přepne stav úkolu - POST /finish nebo /activate dle Freelo API."""
    data = request.get_json()
    done = data.get("done", False)
    try:
        endpoint = f"/task/{task_id}/finish" if done else f"/task/{task_id}/activate"
        resp = freelo_post(endpoint, {})
        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True})
        return jsonify({"error": f"Freelo {resp.status_code}: {resp.text[:200]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/freelo/task/<int:task_id>/edit", methods=["POST"])
@login_required
def api_freelo_task_edit(task_id):
    """Edituje úkol — POST /task/{id} (ověřeno že funguje) + POST /task/{id}/description."""
    data = request.get_json()
    errors = []

    project_id  = data.get("project_id")
    tasklist_id = data.get("tasklist_id")

    # Resolve assignee jméno → worker_id
    worker_id = None
    assignee_name = (data.get("assignee") or "").strip()
    if assignee_name and project_id:
        try:
            mr = freelo_get(f"/project/{project_id}/workers")
            if mr.status_code == 200:
                for w in mr.json().get("data", {}).get("workers", []):
                    if w.get("fullname", "").lower() == assignee_name.lower():
                        worker_id = w["id"]
                        break
        except Exception:
            pass

    # POST /task/{id} — funguje pro name, due_date, worker_id
    post_payload = {}
    if "name" in data and data["name"]:
        post_payload["name"] = data["name"]
    if "deadline" in data:
        post_payload["due_date"] = data["deadline"] or None
    if worker_id:
        post_payload["worker_id"] = worker_id

    if post_payload:
        try:
            resp = freelo_post(f"/task/{task_id}", post_payload)
            if resp.status_code not in (200, 201, 204):
                errors.append(f"Úkol: {resp.status_code} {resp.text[:150]}")
        except Exception as e:
            errors.append(f"Úkol error: {str(e)}")

    # POST /task/{id}/description — jen pokud popis není prázdný
    desc = (data.get("description") or "").strip()
    if desc:
        try:
            if not desc.startswith("<"):
                desc = f"<div>{desc}</div>"
            resp2 = freelo_post(f"/task/{task_id}/description", {"content": desc})
            if resp2.status_code not in (200, 201, 204):
                errors.append(f"Popis: {resp2.status_code} {resp2.text[:150]}")
        except Exception as e:
            errors.append(f"Popis error: {str(e)}")

    if errors:
        return jsonify({"error": " | ".join(errors)}), 400
    return jsonify({"ok": True})


@app.route("/api/freelo/task/<int:task_id>/komentar", methods=["POST"])
@login_required
def api_freelo_task_komentar(task_id):
    """Přidá komentář k úkolu."""
    data = request.get_json()
    text = data.get("content", "").strip()
    if not text:
        return jsonify({"error": "Prázdný komentář"}), 400
    try:
        resp = freelo_post(f"/task/{task_id}/comments", {"content": text})
        if resp.status_code in (200, 201):
            return jsonify({"ok": True})
        return jsonify({"error": f"Freelo {resp.status_code}: {resp.text[:200]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/freelo/task/<int:task_id>/komentare", methods=["GET"])
@login_required
def api_freelo_task_komentare(task_id):
    """Načte komentáře k úkolu."""
    try:
        resp = freelo_get(f"/task/{task_id}/comments")
        if resp.status_code == 200:
            data = resp.json()
            comments = data if isinstance(data, list) else data.get("data", [])
            return jsonify({"ok": True, "comments": [{
                "id": c.get("id"),
                "content": c.get("content", ""),
                "author": c.get("author", {}).get("fullname", "") if c.get("author") else "",
                "created_at": c.get("created_at", ""),
            } for c in comments]})
        return jsonify({"ok": False, "comments": []})
    except Exception as e:
        return jsonify({"ok": False, "comments": [], "error": str(e)})



@app.route("/api/freelo/task/<int:task_id>/podukoly", methods=["GET"])
@login_required
def api_freelo_task_podukoly(task_id):
    """Načte podúkoly úkolu - GET /task/{id}/subtasks."""
    try:
        resp = freelo_get(f"/task/{task_id}/subtasks")
        if resp.status_code == 200:
            raw_sub = resp.json()
            # Freelo: {"data":{"subtasks":[...]}} nebo list
            if isinstance(raw_sub, dict):
                subtasks = raw_sub.get("data", {}).get("subtasks", [])
            elif isinstance(raw_sub, list):
                subtasks = raw_sub
            else:
                subtasks = []
            result = []
            for t in subtasks:
                if not isinstance(t, dict): continue
                # Stav: date_finished != null = hotový; nebo state.id > 1
                state_raw = t.get("state", {})
                if isinstance(state_raw, dict):
                    is_done = state_raw.get("id", 1) > 1 or state_raw.get("state","active") not in ("active","open")
                else:
                    is_done = False
                is_done = is_done or bool(t.get("date_finished"))
                # Podúkol má "id" (subtask record ID) a "task_id" (skutečné Freelo task ID)
                # Pro finish/activate/edit musíme použít task_id
                actual_task_id = t.get("task_id") or t.get("id")
                result.append({
                    "id": actual_task_id,          # Používáme task_id pro API volání
                    "subtask_record_id": t.get("id"),  # Původní subtask id
                    "name": t.get("name", ""),
                    "state": "done" if is_done else "open",
                    "deadline": t.get("due_date", "") or "",
                    "assignee": t.get("worker", {}).get("fullname", "") if t.get("worker") else "",
                    "assignee_id": t.get("worker", {}).get("id") if t.get("worker") else None,
                    "comments_count": t.get("count_comments", 0),
                    "count_subtasks": t.get("count_subtasks", 0),
                    "description": "",
                    "url": f"https://app.freelo.io/task/{actual_task_id}",
                    "finished_at": t.get("date_finished", ""),
                    "is_subtask": True,
                    "parent_task_id": task_id,
                })
            return jsonify({"ok": True, "subtasks": result})
        return jsonify({"ok": False, "subtasks": [], "error": f"Freelo {resp.status_code}"})
    except Exception as e:
        return jsonify({"ok": False, "subtasks": [], "error": str(e)})


@app.route("/api/klient/<int:klient_id>/freelo-pridat-podukol", methods=["POST"])
@login_required
def api_freelo_pridat_podukol(klient_id):
    """Vytvoří podúkol k existujícímu úkolu - POST /task/{parent_id}/subtasks."""
    data = request.get_json()
    parent_id = data.get("parent_id")
    name = data.get("name", "").strip()
    if not parent_id or not name:
        return jsonify({"error": "parent_id a name jsou povinné"}), 400
    try:
        payload = {"name": name}
        if data.get("deadline"):
            payload["due_date"] = data["deadline"]
        resp = freelo_post(f"/task/{parent_id}/subtasks", payload)
        if resp.status_code in (200, 201):
            t = resp.json().get("data", resp.json())
            return jsonify({"ok": True, "subtask": {
                "id": t.get("id"),
                "name": name,
                "state": "open",
                "deadline": data.get("deadline", ""),
                "assignee": "",
                "parent_task_id": parent_id,
            }})
        return jsonify({"error": f"Freelo {resp.status_code}: {resp.text[:200]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/freelo/task/<int:task_id>/smazat", methods=["POST"])
@login_required
def api_freelo_task_smazat(task_id):
    """Smaže úkol ve Freelo - DELETE /task/{id}."""
    try:
        resp = requests.delete(
            f"https://api.freelo.io/v1/task/{task_id}",
            auth=freelo_auth(),
            headers={"Content-Type": "application/json"},
            timeout=15
        )
        if resp.status_code in (200, 201, 204):
            return jsonify({"ok": True})
        return jsonify({"error": f"Freelo {resp.status_code}: {resp.text[:200]}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    if not selected_tasks: return jsonify({"error":"Žádné úkoly"}), 400
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
        # Posli popis primo pri vytvareni — zkus vsechna pole ktera Freelo muze prijimat
        desc = (task.get("desc") or "").strip()
        # Pozn: "content" pri vytvoreni ukolu Freelo ignoruje — popis se posilá zvlášt přes /description
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
                    desc = (task.get("desc") or "").strip()
        # Pozn: "content" pri vytvoreni ukolu Freelo ignoruje — popis se posilá zvlášt přes /description
                    if desc:
                        # Freelo vyzaduje pole "content" pro popis ukolu
                        dr = freelo_post(f"/task/{task_id}/description", {"content": desc})
                        app.logger.info(f"  description: {dr.status_code} {dr.text[:100]}")
                    if assignee and not members_by_name.get(assignee.lower()):
                        freelo_post(f"/task/{task_id}/comments", {"content": f"Zodpovedna osoba: {assignee}"})
            else:
                errors.append(f"{name}: {resp.text[:100]}")
        except Exception as e:
            errors.append(f"{name}: {str(e)}")

    if created:
        zapis.freelo_sent = True
        db.session.commit()
    return jsonify({"created": created, "errors": errors})


@app.route("/api/freelo/projekt/<int:projekt_id>", methods=["POST"])
@login_required
def odeslat_do_freela_projekt(projekt_id):
    """Odešle úkoly do Freela z kontextu projektu (bez vazby na konkrétní zápis)."""
    data           = request.json or {}
    selected_tasks = data.get("tasks", [])
    tasklist_id    = data.get("tasklist_id")
    if not selected_tasks: return jsonify({"error": "Žádné úkoly"}), 400
    if not tasklist_id:    return jsonify({"error": "Vyberte To-Do list"}), 400

    project_id_for_tasks = FREELO_PROJECT_ID
    try:
        resp_p = freelo_get("/projects")
        if resp_p.status_code == 200:
            for proj in resp_p.json():
                for tl in proj.get("tasklists", []):
                    if str(tl.get("id")) == str(tasklist_id):
                        project_id_for_tasks = proj["id"]; break
    except Exception:
        pass

    members_by_name = {}
    try:
        mr = freelo_get(f"/project/{project_id_for_tasks}/workers")
        if mr.status_code == 200:
            for w in mr.json().get("data", {}).get("workers", []):
                if w.get("fullname"):
                    members_by_name[w["fullname"].lower()] = w["id"]
    except Exception:
        pass

    created, errors = [], []
    for task in selected_tasks:
        name = task.get("name", "").strip()
        if not name: continue
        payload  = {"name": name}
        assignee = (task.get("assignee") or "").strip()
        deadline = (task.get("deadline") or "").strip()
        desc     = (task.get("desc") or "").strip()
        if assignee:
            wid = members_by_name.get(assignee.lower())
            if wid: payload["worker_id"] = wid
        if deadline and deadline.lower() not in ("dle dohody", ""):
            if re.match(r"\d{4}-\d{2}-\d{2}", deadline):
                payload["due_date"] = deadline
            elif re.match(r"\d{1,2}\.\d{1,2}\.\d{4}", deadline):
                p = deadline.replace(" ", "").split(".")
                payload["due_date"] = f"{p[2]}-{p[1].zfill(2)}-{p[0].zfill(2)}"
        try:
            resp = freelo_post(f"/project/{project_id_for_tasks}/tasklist/{tasklist_id}/tasks", payload)
            if resp.status_code in (200, 201):
                created.append(name)
                task_data = resp.json()
                task_id   = (task_data.get("data") or task_data).get("id")
                if task_id and desc:
                    freelo_post(f"/task/{task_id}/description", {"content": desc})
                if assignee and not members_by_name.get(assignee.lower()):
                    freelo_post(f"/task/{task_id}/comments", {"content": f"Zodpovedna osoba: {assignee}"})
            else:
                errors.append(f"{name}: {resp.text[:100]}")
        except Exception as e:
            errors.append(f"{name}: {str(e)}")

    return jsonify({"created": created, "errors": errors})


@app.route("/api/freelo/test-kompletni")
@login_required
def test_freelo_kompletni():
    """Vytvori v projektu 582553 testovaci list, ukol s popisem a komentar.
    Vrati co fungovalo a co ne."""
    PROJECT_ID = 582553
    log = []

    # 1. Vytvor todo list
    r = freelo_post(f"/project/{PROJECT_ID}/tasklists", {"name": "TEST API - SMAZAT"})
    log.append({"krok": "1. Vytvor tasklist", "status": r.status_code, "odpoved": r.text[:300]})
    if r.status_code not in (200, 201):
        return jsonify({"chyba": "Nepodarilo se vytvorit tasklist", "log": log})
    
    tl_data = r.json()
    tl = tl_data.get("data") or tl_data
    if isinstance(tl, list): tl = tl[0]
    tasklist_id = tl.get("id")
    log.append({"krok": "1b. Tasklist ID", "id": tasklist_id})

    # 2. Vytvor ukol — zkus "content" primo pri vytvoreni
    task_payload = {
        "name": "Test ukol s popisem",
        "content": "Popis pres pole CONTENT pri vytvoreni ukolu",
    }
    r2 = freelo_post(f"/project/{PROJECT_ID}/tasklist/{tasklist_id}/tasks", task_payload)
    log.append({"krok": "2. Vytvor ukol s content", "status": r2.status_code, "odpoved": r2.text[:400]})
    if r2.status_code not in (200, 201):
        return jsonify({"chyba": "Nepodarilo se vytvorit ukol", "log": log})

    t_data = r2.json()
    task = t_data.get("data") or t_data
    if isinstance(task, list): task = task[0]
    task_id = task.get("id")
    log.append({"krok": "2b. Task ID", "id": task_id})

    # 3. GET description - co je aktualne ulozeno
    r3 = requests.get(f"https://api.freelo.io/v1/task/{task_id}/description",
        auth=freelo_auth(), headers={"Content-Type": "application/json"}, timeout=15)
    log.append({"krok": "3. GET /description", "status": r3.status_code, "odpoved": r3.text[:300]})

    # 4. POST /description s "content"
    r4 = freelo_post(f"/task/{task_id}/description", {"content": "TEST CONTENT POLE"})
    log.append({"krok": "4. POST /description content", "status": r4.status_code, "odpoved": r4.text[:300]})

    # 5. GET description znovu - zmenilo se neco?
    r5 = requests.get(f"https://api.freelo.io/v1/task/{task_id}/description",
        auth=freelo_auth(), headers={"Content-Type": "application/json"}, timeout=15)
    log.append({"krok": "5. GET /description po POST", "status": r5.status_code, "odpoved": r5.text[:300]})

    # 6. Komentar s "content"
    r6 = freelo_post(f"/task/{task_id}/comments", {"content": "Testovaci KOMENTAR s polem content"})
    log.append({"krok": "6. Komentar content", "status": r6.status_code, "odpoved": r6.text[:300]})

    # 7. Precti vysledny ukol — co se skutecne ulozilo
    r7 = requests.get(f"https://api.freelo.io/v1/task/{task_id}",
        auth=freelo_auth(), headers={"Content-Type": "application/json"}, timeout=15)
    log.append({"krok": "7. GET task - finalni stav", "status": r7.status_code, "odpoved": r7.text[:600]})

    return jsonify({
        "vysledek": "Hotovo! Zkontroluj projekt 582553 v Freelu.",
        "tasklist_id": tasklist_id,
        "task_id": task_id,
        "log": log
    })

@app.route("/api/freelo/test-description", methods=["GET"])
@login_required
def test_freelo_description():
    """Vytvori testovaci ukol a zkusi vsechny zpusoby nastaveni popisu."""
    results = {}
    try:
        r = freelo_get("/projects")
        data = r.json()
        projects = data if isinstance(data, list) else data.get("data", [])
        project = next((p for p in projects if p.get("tasklists")), None)
        if not project:
            return jsonify({"error": "Zadny projekt s tasklists", "raw": str(data)[:300]})
        tasklist_id = project["tasklists"][0]["id"]
        project_id  = project["id"]
        results["using"] = f"projekt={project['name']}, tasklist={project['tasklists'][0]['name']}"
    except Exception as e:
        return jsonify({"error": f"Nemohu nacist projekty: {e}"})

    try:
        r = freelo_post(f"/project/{project_id}/tasklist/{tasklist_id}/tasks", {"name": "[TEST POPISU - SMAZAT]"})
        task_data = r.json()
        task = task_data.get("data") or task_data
        if isinstance(task, list): task = task[0]
        task_id = task.get("id")
        if not task_id:
            return jsonify({"error": f"Nepodarilo se vytvorit ukol: {r.text[:200]}"})
        results["task_id"] = task_id
    except Exception as e:
        return jsonify({"error": f"Chyba vytvareni: {e}"})

    import requests as req
    tests = [
        ("POST_description", lambda: freelo_post(f"/task/{task_id}/description", {"description": "POPIS 1"})),
        ("POST_note",        lambda: freelo_post(f"/task/{task_id}/description", {"note": "POPIS 2"})),
        ("PATCH_note",       lambda: req.patch(f"https://api.freelo.io/v1/task/{task_id}", auth=freelo_auth(), headers={"Content-Type":"application/json"}, json={"note": "POPIS 3"}, timeout=10)),
        ("PATCH_description",lambda: req.patch(f"https://api.freelo.io/v1/task/{task_id}", auth=freelo_auth(), headers={"Content-Type":"application/json"}, json={"description": "POPIS 4"}, timeout=10)),
    ]
    for name, fn in tests:
        try:
            r = fn()
            results[name] = {"status": r.status_code, "body": r.text[:200]}
        except Exception as e:
            results[name] = {"error": str(e)}

    try:
        r = req.get(f"https://api.freelo.io/v1/task/{task_id}", auth=freelo_auth(), headers={"Content-Type":"application/json"}, timeout=10)
        results["final_task"] = r.text[:600]
    except Exception as e:
        results["final_task"] = str(e)

    return jsonify(results)


# ─────────────────────────────────────────────
# ROUTES — ADMIN (users)
# ─────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin():
    users   = User.query.order_by(User.name).all()
    klienti = Klient.query.order_by(Klient.nazev).all()
    flash   = session.pop("admin_flash", None)
    # Data šablon — pro inline blok
    tmpl_configs = {}
    for key in TEMPLATE_PROMPTS:
        cfg = TemplateConfig.query.filter_by(template_key=key).first()
        tmpl_configs[key] = cfg
    return render_template("admin.html", users=users, klienti=klienti, admin_flash=flash,
                           template_names=TEMPLATE_NAMES, tmpl_configs=tmpl_configs,
                           tmpl_sections=TEMPLATE_SECTIONS, tmpl_default_prompts=TEMPLATE_PROMPTS)

@app.route("/admin/pridat-uzivatele", methods=["POST"])
@admin_required
def pridat_uzivatele():
    email     = request.form.get("email","").strip().lower()
    name      = request.form.get("name","").strip()
    role      = request.form.get("role","konzultant")
    klient_id = request.form.get("klient_id", type=int) or None
    is_admin  = role in ("superadmin", "admin")
    if not email or not name:
        return redirect(url_for("admin"))
    if User.query.filter_by(email=email).first():
        session["admin_flash"] = f"Email {email} už existuje."
        return redirect(url_for("admin"))

    import random
    words = ["Sklad", "Logistika", "Picking", "Trasa", "Expres", "Projekt", "Audit"]
    password = random.choice(words) + str(random.randint(10,99)) + random.choice(words) + "!"

    u = User(email=email, name=name, role=role, is_admin=is_admin,
             klient_id=klient_id if role == "klient" else None,
             password_hash=generate_password_hash(password))
    db.session.add(u)
    db.session.commit()
    session["admin_flash"] = f"Uživatel {name} vytvořen. Heslo: {password}"
    return redirect(url_for("admin"))

@app.route("/admin/upravit-uzivatele/<int:user_id>", methods=["POST"])
@admin_required
def upravit_uzivatele(user_id):
    user = User.query.get_or_404(user_id)
    user.name      = request.form.get("name", user.name).strip()
    user.role      = request.form.get("role", user.role)
    user.is_admin  = user.role in ("superadmin", "admin")
    user.is_active = bool(request.form.get("is_active"))
    klient_id      = request.form.get("klient_id", type=int) or None
    user.klient_id = klient_id if user.role == "klient" else None
    if request.form.get("password"):
        user.password_hash = generate_password_hash(request.form["password"])
    db.session.commit()
    return redirect(url_for("admin"))

@app.route("/admin/templates", methods=["GET"])
@admin_required
def admin_templates():
    configs = {}
    for key in TEMPLATE_PROMPTS:
        cfg = TemplateConfig.query.filter_by(template_key=key).first()
        configs[key] = cfg
    return render_template("admin_templates.html",
        configs=configs, template_names=TEMPLATE_NAMES,
        default_prompts=TEMPLATE_PROMPTS, template_sections=TEMPLATE_SECTIONS)


@app.route("/admin/templates/<template_key>", methods=["POST"])
@admin_required
def admin_template_save(template_key):
    if template_key not in TEMPLATE_PROMPTS:
        return redirect(url_for("admin"))
    prompt = request.form.get("system_prompt", "").strip()
    cfg = TemplateConfig.query.filter_by(template_key=template_key).first()
    if not cfg:
        cfg = TemplateConfig(
            template_key=template_key,
            name=TEMPLATE_NAMES.get(template_key, template_key)
        )
        db.session.add(cfg)
    cfg.system_prompt = prompt
    db.session.commit()
    session["admin_flash"] = f"Šablona '{TEMPLATE_NAMES.get(template_key, template_key)}' uložena."
    return redirect(url_for("admin"))


@app.route("/admin/templates/<template_key>/reset", methods=["POST"])
@admin_required
def admin_template_reset(template_key):
    cfg = TemplateConfig.query.filter_by(template_key=template_key).first()
    if cfg:
        cfg.system_prompt = ""
        db.session.commit()
    return jsonify({"ok": True, "msg": "Resetováno na výchozí"})


@app.route("/admin/smazat-uzivatele/<int:user_id>", methods=["POST"])
@admin_required
def smazat_uzivatele(user_id):
    if user_id == session["user_id"]:
        return redirect(url_for("admin"))  # nelze smazat sám sebe
    user = User.query.get_or_404(user_id)
    # Nelze smazat superadmina
    if user.role == "superadmin":
        return redirect(url_for("admin"))
    # Přeřaď zápisy na admina před smazáním
    admin_user = User.query.filter_by(role="superadmin").first()
    if admin_user:
        Zapis.query.filter_by(user_id=user_id).update({"user_id": admin_user.id})
        db.session.flush()
    db.session.delete(user)
    db.session.commit()
    session["admin_flash"] = f"Uživatel {user.name} byl smazán."
    return redirect(url_for("admin"))

# ─────────────────────────────────────────────
# DB INIT + AUTO-MIGRATE
# ─────────────────────────────────────────────


def seed_test_data():
    """Vytvoř testovací data s českou diakritikou."""
    try:
        if Klient.query.first():
            return
    except Exception:
        db.session.rollback()
        return
    import time, random
    time.sleep(random.uniform(0, 0.5))
    try:
        if Klient.query.first():
            return
    except Exception:
        db.session.rollback()
        return

    print("Seeduji testovací data...")

    admin = User.query.filter_by(email="admin@commarec.cz").first()

    # Konzultant Martin Komárek
    martin = User.query.filter_by(email="martin@commarec.cz").first()
    if not martin:
        try:
            martin = User(
                email="martin@commarec.cz", name="Martin Komárek",
                role="konzultant", is_admin=False, is_active=True,
                password_hash=generate_password_hash("test123")
            )
            db.session.add(martin)
            db.session.flush()
        except Exception:
            db.session.rollback()
            martin = User.query.filter_by(email="martin@commarec.cz").first()

    # Klient 1
    k1 = Klient(
        nazev="Testovací Logistika s.r.o.",
        slug="testovaci-logistika",
        kontakt="Petr Novotný",
        email="novotny@testlogistika.cz",
        telefon="+420 777 123 456",
        adresa="Průmyslová 14, Brno 615 00",
        poznamka="Distribuční sklad, klient od roku 2023. Zaměřujeme se na optimalizaci pickování a procesů expedice.",
        profil_json=json.dumps({
            "typ_skladu": "distribuční",
            "pocet_sku": "4 200",
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
        kontakt="Jana Horáčková",
        email="horacekova@demoexpres.cz",
        adresa="Letňanská 8, Praha 9, 190 00",
        poznamka="Výrobní a expediční sklad. Implementace WMS v řešení.",
    )
    db.session.add(k2)
    db.session.flush()

    # Projekt 1
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

    # Projekt 2
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

    # Zápis 1  -  audit Testovací Logistika
    summary1 = {
        "participants_commarec": "<p>Martin Komárek  -  vedoucí konzultant</p>",
        "participants_company": "<p>Petr Novotný (ředitel logistiky), Pavel Beneš (vedoucí skladu)</p>",
        "introduction": "<p>Diagnostická návštěva zaměřená na identifikaci příčin rostoucího backlogu a chybovosti při expedici B2B objednávek.</p>",
        "meeting_goal": "<p>Zmapovat aktuální stav pickování, změřit výkonnost a navrhnout konkrétní opatření.</p>",
        "findings": "<ul><li><strong>Pozitivní:</strong> Motivovaný tým, dobrá znalost sortimentu, zavedené ranní porady</li><li><strong>Rizika:</strong> Chybovost pickování 4,2 % (standard je pod 0,5 %), backlog 3 dny, WMS bez wave-planningu</li></ul>",
        "ratings": "<table><tr><th>Oblast</th><th>Hodnocení (%)</th><th>Komentář</th></tr><tr><td>Procesní dokumentace</td><td>35</td><td>Chybí standardy pro B2B picking</td></tr><tr><td>WMS utilizace</td><td>45</td><td>Nevyužívají wave planning ani ABC analýzu</td></tr><tr><td>Layout skladu</td><td>60</td><td>Základní zónování, reserve locations OK</td></tr><tr><td>Produktivita pickování</td><td>40</td><td>58 řádků/hod, potenciál 90+</td></tr><tr><td colspan='3'><strong>Celkové skóre: 45 %</strong> | Nejlepší: Layout | Nejkritičtější: Chybovost</td></tr></table>",
        "processes_description": "<p>Picking probíhá single-order metodou bez batch zpracování. Pracovníci chodí pro každou objednávku zvlášť, průměrná vzdálenost 340 m/objednávka. ABC analýza nebyla nikdy provedena - fast-movers jsou rozmísteny náhodně po celém skladu.</p>",
        "dangers": "<ul><li><strong>Chybovost 4,2 %</strong> → reklamace, ztráta zákazníků, přepracování</li><li><strong>Backlog 3 dny</strong> → nesplněné SLA, pokuty od odběratelů</li><li><strong>Odchod klíčových lidí</strong> → frustrace z chaosu, 2 výpovědi za Q4 2024</li></ul>",
        "suggested_actions": "<p><strong>Krátkodobé (0 - 1 měsíc):</strong></p><ul><li>ABC analýza sortimentu  -  přesunout top 200 SKU do pick zóny A</li><li>Zavedení batch pickingu pro B2C objednávky (skupiny po 8 - 12 obj.)</li></ul><p><strong>Střednědobé (1 - 3 měsíce):</strong></p><ul><li>Konfigurace wave planningu v Helios Orange</li><li>Tvorba standardů a SOP pro picking B2B</li></ul>",
        "expected_benefits": "<ul><li><strong>Snížení chybovosti</strong> z 4,2 % na pod 0,8 %  -  úspora 280 tis. Kč/rok na reklamacích</li><li><strong>Zvýšení produktivity</strong> o 35 - 45 % po zavedení batch pickingu</li><li><strong>Odbourání backlogu</strong> do 2 týdnů od implementace ABC zónování</li></ul>",
        "additional_notes": "<p>Velmi pozitivní přístup vedení  -  okamžitě souhlasili s navrhovanými změnami. Pavel Beneš je silný interní champion. Sklad je čistý a dobře organizovaný co se týče fyzického uspořádání  -  problém je v procesech, ne v prostoru.</p>",
        "summary": "<p>Sklad Testovací Logistika má solidní základy, ale trpí procesními neduhy typickými pro organicky rostoucí e-commerce/B2B operaci. Priorita č. 1: ABC analýza a přesun fast-movers. Priorita č. 2: batch picking. Očekáváme rychlé výsledky  -  tým je motivovaný a vedení plně podporuje změny.</p>",
    }

    z1 = Zapis(
        title="Testovací Logistika s.r.o.  -  Audit skladu",
        template="audit",
        input_text="[Testovací zápis  -  vygenerováno jako seed data]",
        output_json=json.dumps(summary1, ensure_ascii=False),
        output_text="",
        tasks_json=json.dumps([
            {"name": "ABC analýza sortimentu", "desc": "Provést analýzu pohyblivosti SKU a navrhnout rozmístění fast-movers do zóny A", "deadline": "do 1 měsíce"},
            {"name": "Návrh batch picking procesu", "desc": "Zpracovat návrh wave plánu pro B2C objednávky, skupiny 8 - 12 obj.", "deadline": "do 3 týdnů"},
            {"name": "Konfigurace wave planningu v Helios", "desc": "Spolupráce s IT na nastavení wave planning modulu v Helios Orange", "deadline": "do 2 měsíců"},
        ], ensure_ascii=False),
        interni_prompt="",
        freelo_sent=False,
        user_id=admin.id if admin else 1,
        klient_id=k1.id,
        projekt_id=p1.id,
        created_at=datetime(2025, 2, 14, 10, 30),
    )
    client_info = {"meeting_date": "2025-02-14", "commarec_rep": "Martin Komárek",
                   "client_contact": "Petr Novotný", "client_name": "Testovací Logistika s.r.o.", "meeting_place": "Sídlo klienta, Brno"}
    all_blocks = set(["uvod","zjisteni","hodnoceni","procesy","rizika","kroky","prinosy","poznamky","dalsi_krok"])
    z1.output_text = assemble_output_text(client_info, summary1, all_blocks)
    db.session.add(z1)

    # Zápis 2  -  kick-off Demo Expres
    summary2 = {
        "participants_commarec": "<p>Martin Komárek</p>",
        "participants_company": "<p>Jana Horáčková (COO), Tomáš Král (IT ředitel)</p>",
        "introduction": "<p>Kick-off meeting k výběru WMS systému. Diskuse požadavků a harmonogramu implementace.</p>",
        "meeting_goal": "<p>Definovat klíčové požadavky na WMS, odsouhlasit shortlist dodavatelů a nastavit harmonogram výběrového řízení.</p>",
        "findings": "<ul><li>Aktuálně používají Excel + papírové průvodky  -  žádný WMS</li><li>Denní expedice 1 200 ks, 3 směny, 45 zaměstnanců</li><li>Požadavek na go-live do září 2025</li></ul>",
        "suggested_actions": "<p><strong>Krátkodobé:</strong></p><ul><li>Commarec připraví RFP dokument do 28. 2.</li><li>Demo Expres dodá kompletní seznam SKU a procesní mapu do 7. 3.</li></ul><p><strong>Střednědobé:</strong></p><ul><li>Demo prezentace 3 dodavatelů  -  duben 2025</li><li>Výběr dodavatele  -  květen 2025</li></ul>",
        "summary": "<p>Kick-off proběhl konstruktivně. Obě strany shodnuty na harmonogramu. Hlavní riziko: krátký timeline na go-live (6 měsíců). Commarec doporučuje zvážit fázovaný rollout.</p>",
    }
    z2 = Zapis(
        title="Demo Expres a.s.  -  WMS Kick-off",
        template="operativa",
        input_text="[Testovací zápis  -  vygenerováno jako seed data]",
        output_json=json.dumps(summary2, ensure_ascii=False),
        output_text="",
        tasks_json=json.dumps([
            {"name": "Připravit RFP dokument", "desc": "Zpracovat požadavky na WMS pro Demo Expres", "deadline": "2025-02-28"},
            {"name": "Demo prezentace WMS dodavatelů", "desc": "Organizace demo dnů pro 3 vybrané dodavatele", "deadline": "2025-04-15"},
        ], ensure_ascii=False),
        interni_prompt="",
        freelo_sent=False,
        user_id=admin.id if admin else 1,
        klient_id=k2.id,
        projekt_id=p2.id,
        created_at=datetime(2025, 3, 5, 14, 0),
    )
    client_info2 = {"meeting_date": "2025-03-05", "commarec_rep": "Martin Komárek",
                    "client_contact": "Jana Horáčková", "client_name": "Demo Expres a.s.", "meeting_place": "Praha 9, Letňany"}
    z2.output_text = assemble_output_text(client_info2, summary2, all_blocks)
    db.session.add(z2)



    db.session.commit()
    print("Seed data vytvořena: 5 klientů, 5 projektů, 10+ zápisů")

with app.app_context():
    try:
        db.create_all()  # skips existing tables — safe to run repeatedly (vytvoří i template_config)
        # Auto-migrate new columns
        migrations = [
            ("klient", "ic",      "ALTER TABLE klient ADD COLUMN IF NOT EXISTS ic VARCHAR(20) DEFAULT ''"),
            ("klient", "dic",     "ALTER TABLE klient ADD COLUMN IF NOT EXISTS dic VARCHAR(20) DEFAULT ''"),
            ("klient", "sidlo",   "ALTER TABLE klient ADD COLUMN IF NOT EXISTS sidlo VARCHAR(300) DEFAULT ''"),
            ("nabidka_polozka", "dph_pct", "ALTER TABLE nabidka_polozka ADD COLUMN IF NOT EXISTS dph_pct NUMERIC(5,2) DEFAULT 0"),
            ("projekt", "freelo_project_id",  "ALTER TABLE projekt ADD COLUMN IF NOT EXISTS freelo_project_id INTEGER"),
            ("projekt", "freelo_tasklist_id", "ALTER TABLE projekt ADD COLUMN IF NOT EXISTS freelo_tasklist_id INTEGER"),
            ("zapis", "output_json",    "ALTER TABLE zapis ADD COLUMN output_json TEXT DEFAULT '{}'"),
            ("zapis", "notes_json",     "ALTER TABLE zapis ADD COLUMN notes_json TEXT DEFAULT '[]'"),
            ("zapis", "interni_prompt", "ALTER TABLE zapis ADD COLUMN interni_prompt TEXT DEFAULT ''"),
            ("zapis", "public_token",   "ALTER TABLE zapis ADD COLUMN public_token VARCHAR(40)"),
            ("zapis", "is_public",      "ALTER TABLE zapis ADD COLUMN is_public BOOLEAN DEFAULT FALSE"),
            ("zapis", "klient_id",      "ALTER TABLE zapis ADD COLUMN klient_id INTEGER"),
            ("zapis", "projekt_id",     "ALTER TABLE zapis ADD COLUMN projekt_id INTEGER"),
            ("user",  "is_active",      "ALTER TABLE user ADD COLUMN is_active BOOLEAN DEFAULT TRUE"),
            ("user",  "role",           "ALTER TABLE user ADD COLUMN role VARCHAR(40) DEFAULT 'konzultant'"),
            ("user",  "klient_id",      "ALTER TABLE user ADD COLUMN IF NOT EXISTS klient_id INTEGER REFERENCES klient(id)"),
            ("klient", "logo_url",       "ALTER TABLE klient ADD COLUMN logo_url VARCHAR(500) DEFAULT ''"),
            ("klient", "poznamka",     "ALTER TABLE klient ADD COLUMN IF NOT EXISTS poznamka TEXT DEFAULT ''"),
            ("klient", "freelo_tasklist_id", "ALTER TABLE klient ADD COLUMN IF NOT EXISTS freelo_tasklist_id INTEGER"),
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
        # Extra demo data (3 klienti — 1M, 3M, 6M projekty)
        try:
            import importlib.util, os as _os
            _spec = importlib.util.spec_from_file_location("seed_extra",
                _os.path.join(_os.path.dirname(__file__), "seed_extra.py"))
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _mod.seed_extra_data(db, Klient, Projekt, Zapis, User,
                TEMPLATE_SECTIONS, assemble_output_text, generate_password_hash)
        except Exception as e:
            print(f"Extra seed error: {e}")
    except Exception as e:
        print(f"DB init error: {e}")

# ─────────────────────────────────────────────
# MĚSÍČNÍ AI REPORT
# ─────────────────────────────────────────────

@app.route("/report/mesicni")
@login_required
def report_mesicni():
    """Stránka pro výběr klienta a generování měsíčního AI reportu."""
    klienti = Klient.query.filter_by(is_active=True).order_by(Klient.nazev).all()
    now = datetime.utcnow()
    od_default = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    do_default = now.strftime("%Y-%m-%d")
    return render_template("report_mesicni.html", klienti=klienti, now=now,
                           od_default=od_default, do_default=do_default)


@app.route("/api/report/generovat", methods=["POST"])
@login_required
def api_report_generovat():
    """Generuje měsíční AI report pro klienta z jeho zápisů."""
    data = request.get_json()
    klient_id = data.get("klient_id")
    od_str = data.get("od")
    do_str = data.get("do")

    if not klient_id or not od_str or not do_str:
        return jsonify({"error": "Chybí parametry"}), 400

    try:
        od_dt = datetime.strptime(od_str, "%Y-%m-%d")
        do_dt = datetime.strptime(do_str, "%Y-%m-%d")
    except:
        return jsonify({"error": "Neplatné datum"}), 400

    klient = Klient.query.get_or_404(klient_id)
    projekty = Projekt.query.filter_by(klient_id=klient_id, is_active=True).all()

    # Sbírání dat ze zápisů
    zapisy_data = []
    ukoly_otevrene = []
    ukoly_splnene = []
    skore_history = []
    vsechny_zapisy_v_obdobi = []

    for p in projekty:
        zapisy_v_obdobi = Zapis.query.filter(
            Zapis.projekt_id == p.id,
            Zapis.created_at >= od_dt,
            Zapis.created_at <= do_dt + timedelta(days=1),
        ).order_by(Zapis.created_at.asc()).all()

        vsechny_zapisy_v_obdobi.extend(zapisy_v_obdobi)

        for z in zapisy_v_obdobi:
            output = {}
            try:
                output = json.loads(z.output_json or "{}")
            except:
                pass

            # Sestavení obsahu zápisu pro AI
            zapis_text = f"--- ZÁPIS: {z.title} ({z.created_at.strftime('%d. %m. %Y')}) | Typ: {z.template} ---\n"
            for key in ["uvod", "zjisteni", "hodnoceni", "procesy", "rizika", "kroky", "prinosy", "poznamky", "dalsi_krok"]:
                val = output.get(key, "")
                if val and len(val.strip()) > 10:
                    # Zbav se HTML tagů pro čistý text
                    import re as _re
                    clean = _re.sub(r"<[^>]+>", " ", val).strip()
                    if clean:
                        zapis_text += f"[{key.upper()}]: {clean}\n"

            zapisy_data.append(zapis_text)

            # Úkoly
            try:
                tasks = json.loads(z.tasks_json or "[]")
                for t in tasks:
                    if isinstance(t, dict) and t.get("name"):
                        t["zapis_nazev"] = z.title
                        t["zapis_datum"] = z.created_at.strftime("%d. %m. %Y")
                        if t.get("done"):
                            ukoly_splnene.append(t)
                        else:
                            ukoly_otevrene.append(t)
            except:
                pass

            # Skóre z auditů
            if z.template == "audit" and z.output_json:
                try:
                    import re as _re
                    ratings = output.get("hodnoceni", "") or output.get("ratings", "")
                    m = _re.search(r"Celkov[eé][^0-9]*([0-9]+)\s*%", ratings)
                    if m:
                        skore_history.append({
                            "skore": int(m.group(1)),
                            "datum": z.created_at.strftime("%d. %m. %Y"),
                        })
                except:
                    pass

    if not zapisy_data:
        return jsonify({"error": "V zadaném období nejsou žádné zápisy pro tohoto klienta."}), 400

    # Načti Freelo hotové úkoly za období
    freelo_splnene_ai = []
    freelo_otevrene_ai = []
    if klient.freelo_tasklist_id and FREELO_API_KEY and FREELO_EMAIL:
        try:
            fr = freelo_get(f"/tasklist/{klient.freelo_tasklist_id}")
            if fr.status_code == 200:
                raw_ai = fr.json()
                if isinstance(raw_ai, list):
                    tasks_raw = raw_ai
                elif isinstance(raw_ai, dict):
                    tasks_raw = raw_ai.get("tasks", raw_ai.get("data", []))
                else:
                    tasks_raw = []
                for t in tasks_raw:
                    if not isinstance(t, dict):
                        continue
                    if t.get("state") == "done":
                        finished = t.get("finished_at", "")
                        if finished:
                            try:
                                fin_dt = datetime.strptime(finished[:10], "%Y-%m-%d")
                                if od_dt <= fin_dt <= do_dt + timedelta(days=1):
                                    freelo_splnene_ai.append(t.get("name", ""))
                            except Exception:
                                pass
                    elif t.get("state") == "open":
                        freelo_otevrene_ai.append(t.get("name", ""))
        except Exception:
            pass

    # Sestavení promptu pro Claude
    zapisy_blok = "\n\n".join(zapisy_data)
    skore_blok = ""
    if skore_history:
        skore_blok = "\n".join([f"- {s['datum']}: {s['skore']} %" for s in skore_history])

    freelo_blok = ""
    if freelo_splnene_ai:
        freelo_blok += f"\nSPLNĚNÉ ÚKOLY Z FREELA V OBDOBÍ ({len(freelo_splnene_ai)}):\n"
        freelo_blok += "\n".join([f"- {u}" for u in freelo_splnene_ai[:20]])
    if freelo_otevrene_ai:
        freelo_blok += f"\n\nOTEVŘENÉ ÚKOLY VE FREELU ({len(freelo_otevrene_ai)}):\n"
        freelo_blok += "\n".join([f"- {u}" for u in freelo_otevrene_ai[:10]])

    prompt = f"""Jsi konzultant Commarec s.r.o., který píše měsíční report pro klienta.

KLIENT: {klient.nazev}
OBDOBÍ: {od_dt.strftime('%d. %m. %Y')} — {do_dt.strftime('%d. %m. %Y')}
POČET ZÁPISŮ V OBDOBÍ: {len(zapisy_data)}
{'VÝVOJ SKÓRE SKLADU:\n' + skore_blok if skore_blok else ''}
{freelo_blok if freelo_blok else ''}

ZÁPISY Z OBDOBÍ:
{zapisy_blok}

Na základě výše uvedených zápisů vytvoř strukturovaný měsíční report pro klienta.
Report piš profesionálně, v první osobě množného čísla (my, naše doporučení), v češtině.
Buď konkrétní — cituj čísla, termíny a fakta ze zápisů.

Vrať POUZE JSON (bez markdown backticks) v tomto formátu:
{{
  "executive_summary": "2-3 věty shrnující co se v období hlavně dělo a jaký je celkový trend",
  "klic_zjisteni": ["zjištění 1", "zjištění 2", "zjištění 3"],
  "pokrok": "Odstavec o konkrétním pokroku — co se zlepšilo, jaká čísla, co bylo dokončeno",
  "rizika": ["riziko nebo otevřená otázka 1", "riziko 2"],
  "next_steps": ["doporučený krok 1 na příští období", "doporučený krok 2", "doporučený krok 3"],
  "nadpis_reportu": "Stručný výstižný nadpis reportu (max 8 slov)"
}}"""

    try:
        ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = ai.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # Zbav se případných markdown backticks
        import re as _re
        raw = _re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw, flags=_re.MULTILINE).strip()
        ai_data = json.loads(raw)
    except json.JSONDecodeError as e:
        return jsonify({"error": f"AI vrátila neplatný JSON: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Chyba AI: {str(e)}"}), 500

    return jsonify({
        "ok": True,
        "klient_nazev": klient.nazev,
        "od": od_dt.strftime("%d. %m. %Y"),
        "do": do_dt.strftime("%d. %m. %Y"),
        "pocet_zapisu": len(zapisy_data),
        "ukoly_otevrene": ukoly_otevrene[:20],
        "ukoly_splnene": ukoly_splnene[:20],
        "skore_history": skore_history,
        "ai": ai_data,
    })


@app.route("/api/freelo/test-ukoly/<int:tasklist_id>")
@login_required  
def test_freelo_ukoly(tasklist_id):
    """Otestuje různé URL formáty pro načtení úkolů z Freelo tasklist."""
    results = {}
    urls = [
        f"/tasklists/{tasklist_id}/tasks",
        f"/tasklist/{tasklist_id}/tasks", 
        f"/tasklist/{tasklist_id}",
        f"/tasklists/{tasklist_id}",
    ]
    for url in urls:
        try:
            r = freelo_get(url)
            results[url] = {"status": r.status_code, "preview": r.text[:200]}
        except Exception as e:
            results[url] = {"error": str(e)}
    return jsonify(results)


@app.route("/api/freelo/debug-task/<int:task_id>")
@login_required
def debug_freelo_task(task_id):
    """Zobrazí plnou strukturu úkolu a otestuje různé PATCH formáty."""
    import requests as req
    results = {}
    
    # 1. Načti detail úkolu
    r = freelo_get(f"/task/{task_id}")
    results["GET_task"] = {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:300]}
    
    # 2. Otestuj různé způsoby označení jako hotový
    patch_variants = [
        ("state_done", {"state": "done"}),
        ("state_1", {"state": 1}),
        ("finished_true", {"finished": True}),
        ("is_done_true", {"is_done": True}),
        ("status_done", {"status": "done"}),
    ]
    # Nezapišeme — jen ukážeme co by šlo
    results["task_fields"] = list(results["GET_task"].get("body", {}).keys()) if isinstance(results["GET_task"].get("body"), dict) else []
    
    # 3. Načti subúkoly
    r2 = freelo_get(f"/task/{task_id}/subtasks")
    results["GET_subtasks"] = {"status": r2.status_code, "body": r2.text[:400]}
    
    r3 = freelo_get(f"/task/{task_id}")
    if r3.status_code == 200:
        data = r3.json()
        results["subtasks_in_task"] = data.get("subtasks", data.get("sub_tasks", data.get("children", "NOT_FOUND")))
        results["state_field"] = data.get("state", "NOT_FOUND")
        results["finished_field"] = data.get("finished", "NOT_FOUND") 
        results["is_done_field"] = data.get("is_done", "NOT_FOUND")
        results["status_field"] = data.get("status", "NOT_FOUND")
    
    return jsonify(results)


@app.route("/api/freelo/debug-tasklist/<int:tasklist_id>")  
@login_required
def debug_freelo_tasklist(tasklist_id):
    """Zobrazí plnou raw odpověď tasklist včetně podúkolů."""
    r = freelo_get(f"/tasklist/{tasklist_id}")
    if r.status_code != 200:
        return jsonify({"error": r.text, "status": r.status_code})
    data = r.json()
    # Vrať plná data prvních 2 úkolů pro analýzu struktury
    tasks = data.get("tasks", [])
    return jsonify({
        "tasklist_name": data.get("name"),
        "tasks_count": len(tasks),
        "first_tasks_full": tasks[:2],  # plná struktura
        "task_keys": list(tasks[0].keys()) if tasks else [],
    })


@app.route("/api/freelo/debug-state/<int:task_id>", methods=["GET"])
@login_required
def debug_freelo_state(task_id):
    """Otestuje různé PATCH formáty pro označení úkolu jako hotového."""
    import requests as req
    results = {}
    
    # Načti aktuální stav
    r = freelo_get(f"/task/{task_id}")
    if r.status_code == 200:
        t = r.json()
        results["current_state"] = t.get("state")
        results["date_finished"] = t.get("date_finished")
        # Najdi description comment
        for c in t.get("comments", []):
            if c.get("is_description"):
                results["description_comment_id"] = c.get("id")
                results["description_content"] = c.get("content", "")[:200]
                break
    
    # Test PATCH formátů (nedestruktivní — vrátíme zpět)
    patch_tests = [
        ("state_obj_2", {"state": {"id": 2}}),
        ("state_int_2", {"state": 2}),
        ("state_str_done", {"state": "done"}),
        ("finished_true", {"finished": True}),
        ("date_finished_now", {"date_finished": "2026-03-22"}),
    ]
    
    for name, payload in patch_tests:
        try:
            r2 = freelo_patch(f"/task/{task_id}", payload)
            results[f"PATCH_{name}"] = {
                "status": r2.status_code,
                "response": r2.text[:200]
            }
            # Pokud uspělo, vrať zpět na active
            if r2.status_code in (200, 201, 204):
                freelo_patch(f"/task/{task_id}", {"state": {"id": 1}})
                break  # Našli jsme správný formát
        except Exception as e:
            results[f"PATCH_{name}"] = {"error": str(e)}
    
    return jsonify(results)

@app.route("/api/freelo/debug-state2/<int:task_id>")
@login_required
def debug_freelo_state2(task_id):
    """Testuje POST endpointy pro označení hotového + editaci."""
    import requests as req
    results = {}

    # Test POST endpointů pro finish/done
    post_tests = [
        ("POST_finish",         f"/task/{task_id}/finish",       {}),
        ("POST_done",           f"/task/{task_id}/done",          {}),
        ("POST_complete",       f"/task/{task_id}/complete",      {}),
        ("POST_state_done",     f"/task/{task_id}/state",         {"state": "done"}),
        ("POST_state_id2",      f"/task/{task_id}/state",         {"id": 2}),
        ("POST_state_finished", f"/task/{task_id}/state",         {"state": "finished"}),
    ]
    for name, url, payload in post_tests:
        try:
            r = freelo_post(url, payload)
            results[name] = {"status": r.status_code, "body": r.text[:200]}
            if r.status_code in (200, 201, 204):
                results[name]["SUCCESS"] = True
                # Vrátit zpět
                freelo_post(f"/task/{task_id}/state", {"state": "active"})
                break
        except Exception as e:
            results[name] = {"error": str(e)}

    # Test editace description komentáře (id=31443745 z předchozího testu)
    # Zkus PATCH /comment/{id}
    desc_comment_id = None
    r2 = freelo_get(f"/task/{task_id}")
    if r2.status_code == 200:
        for c in r2.json().get("comments", []):
            if c.get("is_description"):
                desc_comment_id = c.get("id")
                break
    
    if desc_comment_id:
        patch_comment_tests = [
            ("PATCH_comment_content",  f"/comment/{desc_comment_id}", {"content": "<div>TEST EDIT - IGNORUJ</div>"}),
            ("PUT_comment_content",    f"/comment/{desc_comment_id}", None),  # PUT
            ("POST_comment_edit",      f"/comment/{desc_comment_id}/edit", {"content": "TEST"}),
        ]
        for name, url, payload in patch_comment_tests:
            if payload is None:  # PUT
                try:
                    r3 = req.put(f"https://api.freelo.io/v1{url}",
                                auth=freelo_auth(), headers={"Content-Type":"application/json"},
                                json={"content": "<div>TEST EDIT</div>"}, timeout=10)
                    results[f"PUT_{url}"] = {"status": r3.status_code, "body": r3.text[:200]}
                except Exception as e:
                    results[f"PUT_{url}"] = {"error": str(e)}
            else:
                try:
                    r3 = freelo_patch(url, payload)
                    results[name] = {"status": r3.status_code, "body": r3.text[:200]}
                    if r3.status_code in (200, 201, 204):
                        results[name]["SUCCESS"] = True
                except Exception as e:
                    results[name] = {"error": str(e)}

    # Test GET pro dostupné endpointy na úkolu
    get_tests = [
        f"/task/{task_id}/subtasks",
        f"/task/{task_id}/comments",
        f"/task/{task_id}/activity",
    ]
    for url in get_tests:
        r = freelo_get(url)
        results[f"GET{url}"] = {"status": r.status_code, "preview": r.text[:150]}

    return jsonify(results)

@app.route("/api/klient/<int:klient_id>/freelo-members", methods=["GET"])
@login_required
def api_klient_freelo_members(klient_id):
    """Načte členy projektu pro přiřazení k úkolům."""
    k = Klient.query.get_or_404(klient_id)
    if not k.freelo_tasklist_id:
        return jsonify({"members": []})
    try:
        # Zjisti projekt z tasklist
        resp_p = freelo_get("/projects")
        project_id = str(FREELO_PROJECT_ID)
        if resp_p.status_code == 200:
            raw_p = resp_p.json()
            projects_list = raw_p if isinstance(raw_p, list) else raw_p.get("data", raw_p.get("projects", []))
            for p in projects_list:
                if not isinstance(p, dict): continue
                for tl in p.get("tasklists", []):
                    if tl.get("id") == k.freelo_tasklist_id:
                        project_id = str(p.get("id"))
                        break
        resp = freelo_get(f"/project/{project_id}/workers")
        members = []
        if resp.status_code == 200:
            data = resp.json()
            workers = data.get("data", {}).get("workers", []) if isinstance(data, dict) else []
            for w in workers:
                if isinstance(w, dict) and w.get("fullname"):
                    members.append({"id": w["id"], "name": w["fullname"], "email": w.get("email","")})
        return jsonify({"members": members})
    except Exception as e:
        return jsonify({"members": [], "error": str(e)})


@app.route("/api/freelo/debug-edit/<int:task_id>")
@login_required
def debug_freelo_edit(task_id):
    """Testuje různé endpointy pro editaci úkolu."""
    results = {}
    test_name = "TEST_EDIT_SMAZAT"
    
    methods = [
        ("POST_task", lambda: freelo_post(f"/task/{task_id}", {"name": test_name})),
        ("PATCH_task", lambda: freelo_patch(f"/task/{task_id}", {"name": test_name})),
        ("PUT_task", lambda: requests.put(f"https://api.freelo.io/v1/task/{task_id}",
            auth=freelo_auth(), headers={"Content-Type":"application/json"},
            json={"name": test_name}, timeout=10)),
        ("POST_task_edit", lambda: freelo_post(f"/task/{task_id}/edit", {"name": test_name})),
    ]
    for name, fn in methods:
        try:
            r = fn()
            results[name] = {"status": r.status_code, "body": r.text[:200]}
            if r.status_code in (200, 201, 204):
                results[name]["SUCCESS"] = True
                # Vrať zpět původní název
                freelo_post(f"/task/{task_id}", {"name": "Navrh batch picking procesu"})
                break
        except Exception as e:
            results[name] = {"error": str(e)}
    return jsonify(results)


@app.route("/api/freelo/task/<int:task_id>/detail", methods=["GET"])
@login_required
def api_freelo_task_detail(task_id):
    """Načte detail úkolu včetně description (z komentáře is_description=true)."""
    try:
        resp = freelo_get(f"/task/{task_id}")
        if resp.status_code != 200:
            return jsonify({"ok": False, "error": f"Freelo {resp.status_code}"})
        t = resp.json()
        # Description je komentář s is_description=true
        description = ""
        for c in t.get("comments", []):
            if c.get("is_description"):
                # Zbav se HTML tagů
                import re as _re
                description = _re.sub(r'<[^>]+>', '', c.get("content", "")).strip()
                break
        return jsonify({
            "ok": True,
            "description": description,
            "worker_id": t.get("worker", {}).get("id") if t.get("worker") else None,
            "worker_name": t.get("worker", {}).get("fullname", "") if t.get("worker") else "",
            "deadline": t.get("due_date", "") or "",
            "state": "done" if t.get("date_finished") else "open",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ─────────────────────────────────────────────
# ERROR HANDLERS
# ─────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


# ─────────────────────────────────────────────
# KLIENTSKÝ PORTÁL — role klient
# ─────────────────────────────────────────────

@app.route("/portal")
def klient_portal():
    """Portál pro klienta — vidí jen své zápisy, nabídky, Freelo úkoly."""
    if "user_id" not in session:
        return redirect(url_for("login"))
    u = User.query.get(session["user_id"])
    if not u or u.role != "klient":
        return redirect(url_for("prehled"))
    
    if not u.klient_id:
        return render_template("portal.html", klient=None, zapisy=[], nabidky=[], ukoly=[])
    
    k = Klient.query.get(u.klient_id)
    zapisy = Zapis.query.filter_by(klient_id=k.id).order_by(Zapis.created_at.desc()).all()
    nabidky = Nabidka.query.filter_by(klient_id=k.id).order_by(Nabidka.created_at.desc()).all()
    
    # Freelo úkoly
    ukoly = []
    if k.freelo_tasklist_id and FREELO_API_KEY and FREELO_EMAIL:
        try:
            resp = freelo_get(f"/tasklist/{k.freelo_tasklist_id}")
            if resp.status_code == 200:
                raw = resp.json()
                tasks_raw = raw.get("tasks", raw.get("data", []))
                for t in tasks_raw:
                    if not isinstance(t, dict): continue
                    is_done = bool(t.get("date_finished"))
                    ukoly.append({
                        "name": t.get("name", ""),
                        "state": "done" if is_done else "open",
                        "assignee": t.get("worker", {}).get("fullname", "") if t.get("worker") else "",
                        "deadline": (t.get("due_date") or "")[:10],
                    })
        except Exception:
            pass
    
    return render_template("portal.html", klient=k, zapisy=zapisy, nabidky=nabidky, ukoly=ukoly)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


# ─────────────────────────────────────────────
# TEST ENDPOINT — ZODPOVĚDNÁ OSOBA (worker_id)
# Otevři: /api/freelo/test-worker/<project_id>/<tasklist_id>
# Příklad: /api/freelo/test-worker/582553/1810216
# ─────────────────────────────────────────────
@app.route("/api/freelo/test-worker/<int:project_id>/<int:tasklist_id>")
@login_required
def test_freelo_worker(project_id, tasklist_id):
    """
    Kompletní test zodpovědné osoby:
    1. Načte members projektu
    2. Vytvoří testovací úkol BEZ worker_id
    3. Vytvoří testovací úkol S worker_id (první člen)
    4. Upraví úkol přes POST /task/{id} s worker_id
    5. Zobrazí výsledky — uvidíš přesně kde se worker ztrácí
    """
    log = []

    # KROK 1: Načti members
    log.append({"krok": "1. GET members", "url": f"/project/{project_id}/workers"})
    members = []
    try:
        r = freelo_get(f"/project/{project_id}/workers")
        log.append({"status": r.status_code, "body": r.text[:500]})
        if r.status_code == 200:
            members = r.json().get("data", {}).get("workers", [])
            log.append({"members_nalezeno": len(members), "members": [{"id": w["id"], "jmeno": w.get("fullname")} for w in members]})
        else:
            log.append({"CHYBA": "Members se nenačetly!", "status": r.status_code})
    except Exception as e:
        log.append({"EXCEPTION": str(e)})

    if not members:
        return jsonify({"PROBLEM": "Žádní members! Zkontroluj project_id.", "log": log})

    first_member = members[0]
    worker_id = first_member["id"]
    worker_name = first_member.get("fullname", "?")
    log.append({"testovaci_worker": {"id": worker_id, "jmeno": worker_name}})

    # KROK 2: Vytvoř úkol BEZ worker_id
    log.append({"krok": "2. Vytvoř úkol BEZ worker_id"})
    try:
        r2 = freelo_post(f"/project/{project_id}/tasklist/{tasklist_id}/tasks", {
            "name": "[TEST-WORKER] bez prirazeni - SMAZ"
        })
        log.append({"status": r2.status_code, "body": r2.text[:300]})
        if r2.status_code in (200, 201):
            task_id_bez = (r2.json().get("data") or r2.json()).get("id")
            log.append({"task_bez_worker_id": task_id_bez})
        else:
            log.append({"CHYBA": "Nepodařilo se vytvořit úkol"})
            task_id_bez = None
    except Exception as e:
        log.append({"EXCEPTION": str(e)})
        task_id_bez = None

    # KROK 3: Vytvoř úkol S worker_id
    log.append({"krok": f"3. Vytvoř úkol S worker_id={worker_id} ({worker_name})"})
    try:
        r3 = freelo_post(f"/project/{project_id}/tasklist/{tasklist_id}/tasks", {
            "name": f"[TEST-WORKER] s worker_id={worker_id} ({worker_name}) - SMAZ",
            "worker_id": worker_id
        })
        log.append({"status": r3.status_code, "body": r3.text[:300]})
        if r3.status_code in (200, 201):
            task_data = r3.json().get("data") or r3.json()
            task_id_s = task_data.get("id")
            worker_v_odpovedi = task_data.get("worker")
            log.append({
                "task_s_worker_id": task_id_s,
                "worker_v_odpovedi_freelo": worker_v_odpovedi,
                "VYSLEDEK": "✅ WORKER SE ULOZIL" if worker_v_odpovedi else "❌ WORKER CHYBI V ODPOVEDI"
            })
        else:
            log.append({"CHYBA": "Nepodařilo se vytvořit úkol s worker_id"})
            task_id_s = None
    except Exception as e:
        log.append({"EXCEPTION": str(e)})
        task_id_s = None

    # KROK 4: Uprav existující úkol přes POST /task/{id}
    if task_id_bez:
        log.append({"krok": f"4. Uprav úkol {task_id_bez} přes POST /task/{{id}} s worker_id={worker_id}"})
        try:
            r4 = freelo_post(f"/task/{task_id_bez}", {"worker_id": worker_id})
            log.append({"status": r4.status_code, "body": r4.text[:300]})
            if r4.status_code in (200, 201, 204):
                task_data4 = r4.json() if r4.text else {}
                worker_po_editu = task_data4.get("worker")
                log.append({
                    "VYSLEDEK_EDITU": "✅ WORKER SE ULOZIL PO EDITU" if worker_po_editu else "⚠️ EDIT PROSLO ALE WORKER V ODPOVEDI CHYBI",
                    "worker_v_odpovedi": worker_po_editu
                })
            else:
                log.append({"CHYBA_EDITU": f"Status {r4.status_code}"})
        except Exception as e:
            log.append({"EXCEPTION_EDIT": str(e)})

    # KROK 5: Ověř přes GET /task/{id} jestli worker je uložený
    if task_id_s:
        log.append({"krok": f"5. Ověř GET /task/{task_id_s} — je worker uložený?"})
        try:
            r5 = freelo_get(f"/task/{task_id_s}")
            log.append({"status": r5.status_code})
            if r5.status_code == 200:
                task_check = r5.json()
                worker_check = task_check.get("worker")
                log.append({
                    "KONECNY_VYSLEDEK": "✅ WORKER JE V TASKU" if worker_check else "❌ WORKER NENI V TASKU",
                    "worker": worker_check
                })
        except Exception as e:
            log.append({"EXCEPTION_CHECK": str(e)})

    return jsonify({
        "navod": f"Otevři Freelo → Consulting-test → tasklist ID {tasklist_id} → najdi [TEST-WORKER] úkoly a zkontroluj kdo je přiřazen",
        "testovaci_worker": {"id": worker_id, "jmeno": worker_name},
        "log": log
    })


# ─────────────────────────────────────────────
# TEST ENDPOINT — SIMULACE ULOŽIT BUTTON
# Otevři: /api/freelo/test-edit-worker/<task_id>/<jmeno>
# Příklad: /api/freelo/test-edit-worker/28798538/Martin%20Kom%C3%A1rek
# ─────────────────────────────────────────────
@app.route("/api/freelo/test-edit-worker/<int:task_id>/<path:jmeno>")
@login_required
def test_freelo_edit_worker(task_id, jmeno):
    """
    Simuluje přesně co dělá tlačítko ULOŽIT v editaci úkolu.
    Ukáže: payload který se posílá, worker_id který se našel, odpověď Freela.
    """
    log = []

    # Stejný kód jako api_freelo_task_edit
    project_id = 582553  # Consulting-test

    # KROK 1: Resolve jméno → worker_id
    log.append({"krok": f"1. Hledám worker_id pro jméno='{jmeno}' v project {project_id}"})
    worker_id = None
    try:
        mr = freelo_get(f"/project/{project_id}/workers")
        log.append({"members_status": mr.status_code})
        if mr.status_code == 200:
            workers = mr.json().get("data", {}).get("workers", [])
            log.append({"vsichni_workers": [{"id": w["id"], "jmeno": w.get("fullname")} for w in workers]})
            for w in workers:
                if w.get("fullname", "").lower() == jmeno.lower():
                    worker_id = w["id"]
                    break
            log.append({"hledane_jmeno": jmeno, "nalezeny_worker_id": worker_id})
        else:
            log.append({"CHYBA": "Members API selhalo"})
    except Exception as e:
        log.append({"EXCEPTION": str(e)})

    # KROK 2: Sestaví payload (přesně jako edit endpoint)
    post_payload = {"worker_id": worker_id} if worker_id else {}
    log.append({"krok": "2. Payload pro POST /task/{id}", "payload": post_payload})

    if not worker_id:
        return jsonify({
            "PROBLEM": f"Worker '{jmeno}' nenalezen v projektu {project_id}",
            "RESENI": "Zkontroluj jméno — musí přesně souhlasit (case-insensitive)",
            "log": log
        })

    # KROK 3: POST /task/{id}
    log.append({"krok": f"3. POST /task/{task_id}", "payload": post_payload})
    try:
        resp = freelo_post(f"/task/{task_id}", post_payload)
        log.append({"status": resp.status_code, "odpoved": resp.text[:400]})
        if resp.status_code in (200, 201, 204):
            resp_data = resp.json() if resp.text else {}
            log.append({
                "VYSLEDEK": "✅ EDIT PROSLO",
                "worker_v_odpovedi": resp_data.get("worker"),
                "cely_task": {k: v for k, v in resp_data.items() if k in ["id", "name", "worker"]}
            })
        else:
            log.append({"VYSLEDEK": f"❌ EDIT SELHALO — status {resp.status_code}"})
    except Exception as e:
        log.append({"EXCEPTION": str(e)})

    # KROK 4: GET task pro ověření
    log.append({"krok": f"4. Ověření: GET /task/{task_id}"})
    try:
        r_check = freelo_get(f"/task/{task_id}")
        if r_check.status_code == 200:
            d = r_check.json()
            log.append({
                "KONECNY_STAV_WORKERA": d.get("worker"),
                "USPECH": "✅ WORKER JE PRIRAZEN" if d.get("worker") else "❌ WORKER NENI PRIRAZEN"
            })
    except Exception as e:
        log.append({"EXCEPTION_CHECK": str(e)})

    return jsonify({"log": log})


# ─────────────────────────────────────────────
# TEST STRÁNKA — ZODPOVĚDNÁ OSOBA
# Otevři: /freelo-test
# ─────────────────────────────────────────────
@app.route("/freelo-test")
@login_required
def freelo_test_stranka():
    """HTML testovací stránka — načte úkoly a vygeneruje klikatelné test odkazy."""
    # Načti všechny klienty s tasklist_id
    klienti = Klient.query.filter(Klient.freelo_tasklist_id != None).all()

    # Načti members z projektu 582553
    members = []
    try:
        mr = freelo_get("/project/582553/workers")
        if mr.status_code == 200:
            members = mr.json().get("data", {}).get("workers", [])
    except Exception:
        pass

    # Pro každého klienta načti první úkol
    klienti_data = []
    for k in klienti[:5]:  # max 5 klientů
        ukoly = []
        try:
            r = freelo_get(f"/tasklist/{k.freelo_tasklist_id}")
            if r.status_code == 200:
                raw = r.json()
                tasks_raw = raw.get("tasks", raw.get("data", []))
                for t in tasks_raw[:3]:
                    ukoly.append({"id": t.get("id"), "name": t.get("name", "?"), "worker": t.get("worker")})
        except Exception:
            pass
        klienti_data.append({"klient": k.nazev, "tasklist_id": k.freelo_tasklist_id, "ukoly": ukoly})

    html = f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<title>Freelo Test — Zodpovědná osoba</title>
<style>
body {{ font-family: 'Montserrat', sans-serif; background: #f0f3f7; padding: 2rem; color: #173767; }}
h1 {{ font-size: 22px; font-weight: 900; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.5rem; }}
h2 {{ font-size: 14px; font-weight: 700; text-transform: uppercase; color: #4A6080; margin: 1.5rem 0 0.5rem; }}
.card {{ background: white; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
.btn {{ display: inline-block; background: #173767; color: white; padding: 8px 16px; border-radius: 5px; text-decoration: none; font-size: 12px; font-weight: 700; margin: 4px 4px 4px 0; text-transform: uppercase; letter-spacing: 0.04em; }}
.btn:hover {{ background: #00AFF0; color: white; }}
.btn.green {{ background: #0A7A5A; }}
.btn.red {{ background: #C0392B; }}
.tag {{ display: inline-block; background: #f0f3f7; border-radius: 4px; padding: 2px 8px; font-size: 11px; font-weight: 600; color: #4A6080; margin-right: 6px; }}
.ok {{ color: #0A7A5A; font-weight: 700; }}
.err {{ color: #C0392B; font-weight: 700; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }}
th {{ background: #f0f3f7; padding: 8px 12px; text-align: left; font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #4A6080; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #e8eef4; }}
tr:last-child td {{ border-bottom: none; }}
#result {{ background: #0A7A5A; color: white; padding: 1rem 1.5rem; border-radius: 8px; margin-top: 1rem; font-size: 13px; white-space: pre-wrap; display: none; }}
#result.err {{ background: #C0392B; }}
</style>
</head>
<body>
<h1>🔧 Freelo Test — Zodpovědná osoba</h1>
<p style="color:#4A6080;font-size:13px;margin-bottom:1.5rem;">Testovací stránka pro debugging přiřazení zodpovědné osoby ve Freelu.</p>

<div class="card">
<h2>👥 Members projektu 582553</h2>
<table>
<tr><th>ID</th><th>Jméno</th><th>Test odkaz</th></tr>
"""
    for m in members:
        jmeno_enc = m.get('fullname','').replace(' ', '%20')
        html += f"<tr><td><code>{m.get('id')}</code></td><td><strong>{m.get('fullname','?')}</strong></td>"
        html += f"<td><span class='tag'>Použij s úkolem níže</span></td></tr>"

    html += "</table></div>"

    if not members:
        html += "<div class='card'><span class='err'>❌ Members se nenačetly! Zkontroluj FREELO_EMAIL a FREELO_API_KEY.</span></div>"

    # Úkoly per klient
    html += "<div class='card'><h2>📋 Úkoly klientů — klikni pro test</h2>"
    for kd in klienti_data:
        html += f"<h2 style='margin-top:1rem;'>{kd['klient']} <span class='tag'>tasklist {kd['tasklist_id']}</span></h2>"
        if not kd['ukoly']:
            html += "<p style='color:#C0392B;font-size:13px;'>Žádné úkoly nebo chyba načítání.</p>"
            continue
        html += "<table><tr><th>ID úkolu</th><th>Název</th><th>Aktuální worker</th><th>Test akce</th></tr>"
        for u in kd['ukoly']:
            worker_info = u['worker'].get('fullname','?') if u['worker'] else '<span class="err">nepřiřazeno</span>'
            for m in members[:2]:  # Nabídni 2 osoby k otestování
                jmeno_enc = m.get('fullname','').replace(' ', '%20')
                html += f"""<tr>
<td><code>{u['id']}</code></td>
<td>{u['name'][:40]}</td>
<td>{worker_info}</td>
<td>
  <a class="btn green" href="/api/freelo/test-edit-worker/{u['id']}/{jmeno_enc}" target="_blank">
    Přiraď: {m.get('fullname','?').split()[0]}
  </a>
</td>
</tr>"""
            break  # jen první úkol per klient
        html += "</table>"

    html += "</div>"

    # Manuální test
    html += f"""
<div class="card">
<h2>🔬 Manuální test</h2>
<p style="font-size:13px;color:#4A6080;margin-bottom:1rem;">Zadej ručně ID úkolu a jméno — otestuje celý flow edit endpointu:</p>
<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;">
  <div>
    <label style="font-size:10px;font-weight:700;color:#4A6080;display:block;margin-bottom:4px;">ID ÚKOLU</label>
    <input id="ukol_id" type="text" placeholder="např. 28798538" style="padding:8px 12px;border:1.5px solid #D0DAE8;border-radius:5px;font-size:13px;width:160px;">
  </div>
  <div>
    <label style="font-size:10px;font-weight:700;color:#4A6080;display:block;margin-bottom:4px;">JMÉNO</label>
    <select id="jmeno_sel" style="padding:8px 12px;border:1.5px solid #D0DAE8;border-radius:5px;font-size:13px;">
"""
    for m in members:
        html += f"<option value=\"{m.get('fullname','')}\">{m.get('fullname','?')}</option>"

    html += f"""
    </select>
  </div>
  <button onclick="spustTest()" style="background:#173767;color:white;border:none;padding:9px 18px;border-radius:5px;font-size:13px;font-weight:700;cursor:pointer;">▶ Spustit test</button>
  <a id="test_odkaz" href="#" target="_blank" style="display:none;font-size:12px;color:#00AFF0;">Otevřít raw JSON →</a>
</div>
<div id="result"></div>
</div>

<script>
async function spustTest() {{
  const id = document.getElementById('ukol_id').value.trim();
  const jmeno = document.getElementById('jmeno_sel').value;
  if (!id) {{ alert('Zadej ID úkolu'); return; }}
  const url = `/api/freelo/test-edit-worker/${{id}}/${{encodeURIComponent(jmeno)}}`;
  document.getElementById('test_odkaz').href = url;
  document.getElementById('test_odkaz').style.display = 'inline';
  const res = document.getElementById('result');
  res.style.display = 'block';
  res.className = '';
  res.textContent = 'Testuji...';
  try {{
    const r = await fetch(url);
    const d = await r.json();
    const success = JSON.stringify(d).includes('WORKER JE PRIRAZEN') || JSON.stringify(d).includes('ULOZIL');
    res.className = success ? '' : 'err';
    res.textContent = JSON.stringify(d, null, 2);
  }} catch(e) {{
    res.className = 'err';
    res.textContent = 'Chyba: ' + e.message;
  }}
}}
</script>

</body>
</html>"""

    return html
