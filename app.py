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
    profil_json = db.Column(db.Text, default="{}")
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
        return redirect(url_for("home"))
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
        resp = freelo_get(f"/tasklists/{p.freelo_tasklist_id}/tasks")
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

            klient_data["projekty"].append({
                "projekt": p,
                "zapisy_v_obdobi": zapisy_v_obdobi,
                "vsechny_zapisy_count": len(vsechny_zapisy),
                "ukoly_splnene": ukoly_splnene[:10],
                "ukoly_otevrene": ukoly_otevrene[:15],
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
    konzultanti = User.query.filter_by(is_active=True).all()
    try:
        profil = json.loads(k.profil_json or "{}")
    except Exception:
        profil = {}
    return render_template("klient_detail.html", k=k, projekty=projekty,
                           zapisy=zapisy, profil=profil,
                           konzultanti=konzultanti, template_names=TEMPLATE_NAMES)


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
    email    = request.form.get("email","").strip().lower()
    name     = request.form.get("name","").strip()
    is_admin = bool(request.form.get("is_admin"))
    role     = request.form.get("role","konzultant")
    if not email or not name:
        return redirect(url_for("admin"))
    if User.query.filter_by(email=email).first():
        return redirect(url_for("admin"))

    # Generuj bezpečné heslo: 3 slova + čísla (snadno zapamatovatelné)
    words = ["Sklad", "Logistika", "Komárec", "Picking", "Trasa", "Expres", "Projekt"]
    import random
    password = random.choice(words) + str(random.randint(10,99)) + random.choice(words) + "!"

    u = User(email=email, name=name, role=role,
             password_hash=generate_password_hash(password), is_admin=is_admin)
    db.session.add(u)
    db.session.commit()

    # Flash zpráva s heslem — zobraz vždy (email zatím neposíláme)
    session["admin_flash"] = f"Uživatel {name} vytvořen. Heslo: {password}"

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

@app.route("/admin/templates", methods=["GET"])
@login_required
def admin_templates():
    if not session.get("is_admin"):
        return redirect(url_for("dashboard"))
    configs = {}
    for key in TEMPLATE_PROMPTS:
        cfg = TemplateConfig.query.filter_by(template_key=key).first()
        configs[key] = cfg
    return render_template("admin_templates.html",
        configs=configs, template_names=TEMPLATE_NAMES,
        default_prompts=TEMPLATE_PROMPTS, template_sections=TEMPLATE_SECTIONS)


@app.route("/admin/templates/<template_key>", methods=["POST"])
@login_required
def admin_template_save(template_key):
    if not session.get("is_admin"):
        return jsonify({"error": "Unauthorized"}), 403
    if template_key not in TEMPLATE_PROMPTS:
        return jsonify({"error": "Neznámá šablona"}), 404
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
    return jsonify({"ok": True, "msg": "Šablona uložena"})


@app.route("/admin/templates/<template_key>/reset", methods=["POST"])
@login_required
def admin_template_reset(template_key):
    if not session.get("is_admin"):
        return jsonify({"error": "Unauthorized"}), 403
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
    if Klient.query.first():
        return
    import time, random
    time.sleep(random.uniform(0, 0.3))
    if Klient.query.first():
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
            ("klient", "logo_url",       "ALTER TABLE klient ADD COLUMN logo_url VARCHAR(500) DEFAULT ''"),
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

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
