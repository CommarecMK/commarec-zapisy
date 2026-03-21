# CLAUDE.md — Commarec Zápisy
> Tento soubor čti VŽDY jako první. Pak načti kód z GitHubu, projdi web živě a navrhni konkrétní posun.

---

## 🚀 Rychlý start pro novou session

```
1. Přečti tento soubor celý
2. Načti živý kód: https://github.com/CommarecMK/commarec-zapisy
   - Stáhni ZIP nebo projdi klíčové soubory (app.py, templates/)
3. Podívej se na živou aplikaci: https://web-production-76f2.up.railway.app
   - Přihlas se: admin@commarec.cz / admin123
   - Projdi: /home, /crm, /progress-report, /nabidka/1, /klient/1
4. Navrhni TOP 3 konkrétní vylepšení na základě aktuálního stavu
5. Začni s tím nejdůležitějším — připrav ZIP k uploadu
6. Po každé změně aktualizuj CHANGELOG v tomto souboru
```

---

## 📍 Co je tento projekt

Interní Flask aplikace **Commarec s.r.o.** — konzultační firma zaměřená na optimalizaci skladů a logistiky.

**Hlavní use case:** Martin (a tým) ji používá po každé schůzce s klientem — nahraje přepis nebo poznámky, AI vygeneruje profesionální zápis, ten putuje do Freelea jako úkoly.

**Uživatelé:**
- Celý tým Commarec (konzultanti, Martin)
- Klienti uvidí části aplikace (veřejné zápisy, výhledově klientský portál)

**Klíčová priorita:** Stabilita a opravy bugů. Aplikace se používá ostře po každé schůzce — nesmí padat.

**Live:** https://web-production-76f2.up.railway.app
**GitHub:** https://github.com/CommarecMK/commarec-zapisy
**Hosting:** Railway (auto-deploy z main branch, cca 2 min)
**Login:** admin@commarec.cz / admin123

---

## 🏗 Tech Stack

- Backend: Python Flask + SQLAlchemy
- Databáze: PostgreSQL (Railway)
- AI: Claude claude-sonnet-4-5 (Anthropic API)
- Frontend: Jinja2 + vanilla JS + custom CSS (žádný framework)
- Deploy: Gunicorn 4 workers (gthread)
- Fonty: DrukCondensed Super (jen display), Montserrat (vše ostatní)

---

## 📁 Struktura souborů

```
app.py                 — monolitický hlavní soubor (~2400 řádků) ⚠️
seed_extra.py          — demo data (5 klientů, různé fáze projektů)
CLAUDE.md              — tento soubor (VŽDY aktualizuj po změně)
requirements.txt       — Python závislosti
railway.toml           — Railway konfigurace
templates/
  base.html            — nav, CSS variables, globální styly
  dashboard_new.html   — nový home dashboard (rozcestník)
  dashboard.html       — starý přehled zápisů (zachován pro záložní)
  crm.html             — CRM přehled klientů s filtry
  klient_detail.html   — detail klienta (projekty, zápisy, tlačítka)
  klient_vyvoj.html    — vývoj klienta (timeline, Freelo live)
  klient_form.html     — formulář vytvoření/editace klienta
  nabidka_detail.html  — nabídka: editace položek, PDF (window.print)
  nabidka_nova.html    — nová nabídka
  progress_report.html — report za období (po Jinja2 opravě funguje)
  detail.html          — detail zápisu (AI obsah, print CSS)
  novy.html            — formulář nového zápisu (3 šablony)
  admin.html           — správa uživatelů, šablon
  login.html, verejny.html, projekt_detail.html, klienti.html
static/
  DrukCondensed-Super.woff2
  logo-dark.svg, logo-white.svg
```

---

## 🗄 Databázové modely

```python
Klient:     nazev, slug, kontakt, email, telefon,
            adresa (provozní), sidlo (fakturační), ic, dic,
            logo_url, profil_json (AI extrakce), is_active

Projekt:    nazev, klient_id, user_id, datum_od, datum_do,
            is_active, freelo_project_id, freelo_tasklist_id

Zapis:      title, template (audit/operativa/obchod),
            input_text, output_json, output_text, tasks_json,
            notes_json, interni_prompt, freelo_sent,
            public_token, is_public, user_id, klient_id, projekt_id

Nabidka:    cislo (NAB-YYYY-NNN auto), klient_id, projekt_id,
            user_id, nazev, poznamka, stav, platnost_do, mena

NabidkaPolozka: nabidka_id, poradi, nazev, popis, mnozstvi,
                jednotka (ks/m/m²/hod/paušál/...), cena_ks,
                sleva_pct, dph_pct (default 21, number input)

User:       email, name, role (superadmin/admin/konzultant), is_admin

TemplateConfig: template_key, name, system_prompt (editovatelný)
```

---

## 🛣 Klíčové routes

```
/                              → redirect na /home
/home                          nový dashboard (status + aktivita)
/dashboard                     starý přehled zápisů
/crm                           CRM přehled klientů + filtry
/klient/<id>                   detail klienta
/klient/<id>/vyvoj             vývoj klienta (timeline, Freelo)
/klient/novy, /klient/<id>/upravit
/progress-report               progress report za období
/nabidka/nova                  nová nabídka (s klient_id param)
/nabidka/<id>                  detail nabídky, editace, PDF
/nabidka/<id>/ulozit           AJAX save (JSON)
/nabidka/<id>/stav             změna stavu
/api/freelo/projekt/<id>/ukoly live Freelo úkoly (JSON)
/projekt/<id>/nastavit-freelo  nastavení Freelo ID k projektu
/z/<token>                     veřejný zápis (bez přihlášení)
/admin, /admin/templates
```

---

## 🎨 Brand Guidelines

```
Navy:    #173767 (primary), #0E213E (dark), #050B15 (black)
Cyan:    #00AFF0 (primary), #008ABD (secondary)
Orange:  #FF8D00 (nabídky, sekundární CTA)
Zelená:  #34C759 (úspěch ≥70%)
Červená: #FF383C (danger, <40%)

DRUK CONDENSED — POUZE tyto prvky:
  h1 stránky (36px), hero tituly (52px+),
  velká čísla na kartách (48px+), celková cena nabídky (60px)

MONTSERRAT — VŠE OSTATNÍ:
  h2, h3, tlačítka, nav, labely, badge, tabulky, formy,
  popisky, meta texty
```

---

## 🔗 Integrace

### Freelo API
- Auth: Basic (FREELO_EMAIL + FREELO_API_KEY)
- Base URL: https://api.freelo.io/v1
- Výchozí projekt ID: 501350
- Klíčové endpointy:
  - GET /tasklists/{id}/tasks — úkoly tasklist
  - POST /task/{id}/description — zápis popisu úkolu
  - GET /project/{id}/workers — členové projektu
- **Napojení:** Každý Projekt v DB má freelo_tasklist_id — zadává se ručně v detailu projektu. Bez tohoto ID live úkoly nefungují.

### Anthropic API
- Model: claude-sonnet-4-5
- Generování zápisů: audit / operativa / obchod šablony
- System prompty jsou editovatelné v Správě → Šablony zápisů

---

## ⚙️ Railway env vars

```
DATABASE_URL        PostgreSQL connection string (Railway poskytuje auto)
SECRET_KEY          Flask session secret
ANTHROPIC_API_KEY   Claude API key
FREELO_API_KEY      Freelo API key
FREELO_EMAIL        Freelo přihlašovací email
FREELO_PROJECT_ID   501350
```

---

## 🧠 Hodnocení kódu (upřímné — pro dalšího Clauda)

### Funguje dobře ✅
- Flask architektura je správná, SQLAlchemy modely jsou dobře navrženy
- Brand CSS systém (variables) je konzistentní a hezký
- Seed data jsou kvalitní — realistické scénáře pro demo
- AI prompty jsou dobře strukturované, sekce fungují
- Freelo push úkolů funguje

### Kritické problémy ⚠️
- **app.py MONOLITH (~2400 řádků)** — vše v jednom souboru. Každá změna je riziková. Nutno rozdělit na blueprinty: routes/zapisy.py, routes/klienti.py, routes/nabidky.py, routes/admin.py
- **Inline styly všude** — stovky `style="..."` v templates. Brutálně těžko udržovatelné. Potřeba jeden main.css.
- **Žádné testy** — ani jeden test. Každá deploy je risk.
- **PDF** — window.print() funguje ale uživatel musí ručně ukládat. Ideál: server-side PDF (WeasyPrint).
- **Seed race condition** — 4 Gunicorn workery seedují najednou při startu. Opraveno rollback() ale není eleganté.
- **Žádná error stránka** — 500 error = bílá stránka.
- **Žádné logy** — z Flask aplikace nejsou žádné strukturované logy.

### Technický dluh 🔴
Pořadí oprav podle priority:
1. Error stránky (404, 500) — 30 minut práce, velký dopad
2. Responsivní CSS — Martin i tým používají mobil
3. Email odesílání zápisů — kritické pro workflow s klienty
4. app.py blueprinty — nutné pro budoucí rozvoj
5. main.css — konsolidace inline stylů

---

## 📊 Aktuální stav funkcí

```
Generování zápisů (AI)     ✅ Funguje — audit/operativa/obchod
Dashboard home             ✅ Funguje — aktivita, status karty, projekty
CRM přehled                ✅ Funguje — filtry, tlačítka (Vývoj/Nabídka/Zápis)
Vývoj klienta              ✅ Funguje — timeline zápisů
Progress Report            ✅ Funguje (opravena Jinja2 chyba sum filter)
Nabídky — editace          ✅ Funguje — DPH number input, step=1, default 21%
Nabídky — PDF              ⚠️ window.print() — funguje, layout OK
Freelo push úkolů          ✅ Funguje — ze zápisů do Freelea
Freelo live úkoly          ⚠️ Závisí na nastavení freelo_tasklist_id v projektu
Veřejný zápis (/z/token)   ✅ Existuje
Email zápisů               ❌ Chybí — bylo odstraněno, nutno přidat zpět
Mobilní verze              ❌ CSS není responsivní
Error stránky (404/500)    ❌ Chybí — jen bílá stránka
Klientský portál           ❌ Plánováno — klient vidí své zápisy/nabídky
Notifikace na úkoly        ❌ Neplánováno zatím
```

---

## 🗺 Doporučená roadmapa

### Fáze A — Stabilizace (PRIORITA — aplikace se používá ostře)
1. **Error stránky** — 404.html + 500.html s brand stylem
2. **Email zápisů** — zaslat zápis klientovi (Microsoft 365 SMTP)
3. **Responsivní CSS** — základní mobile breakpoints (nav, karty, tabulky)
4. **Lepší PDF** — server-side WeasyPrint nebo výrazně lepší print CSS

### Fáze B — Klientský portál (vysoká hodnota)
5. **Klientský portál** — klient se přihlásí a vidí: své zápisy, nabídky, stav projektů
6. **Email notifikace** — "byl vygenerován nový zápis pro váš projekt"
7. **Sdílení nabídky** — link pro klienta bez přihlášení (jako veřejný zápis)

### Fáze C — Optimalizace kódu
8. **app.py blueprinty** — rozdělit na moduly
9. **main.css** — konsolidace inline stylů
10. **Testy** — aspoň smoke testy klíčových routes

### Fáze D — Rozšíření
11. **Analytika** — grafy vývoje skóre klienta přes čas
12. **Kalendář** — plánování dalších schůzek
13. **Multi-tenant** — white-label pro jiné firmy (pokud bude zájem)

---

## ❓ Otevřené otázky (zodpovězeno + zbývá)

### Zodpovězeno ✅
- Uživatelé: celý tým Commarec + klienti uvidí části
- Priorita: stabilita a opravy bugů
- Použití: po každé schůzce (generování zápisů)

### Zbývá doplnit 📝
- [ ] Plánujete sdílet nabídky klientům přímo z aplikace? (ovlivní PDF prioritu)
- [ ] Máte nastaveno Freelo? Jaký je workflow tasklist_id per projekt?
- [ ] Jak důležitý je email — posíláte zápisy klientům dnes jinak?
- [ ] Je mobilní přístup potřeba (tablet na schůzce, nebo jen desktop)?
- [ ] Kolik konzultantů bude mít přístup? (User management priorita)

---

## 📝 CHANGELOG

### 2026-03-21 — Velká session (celý den)
**Vytvořeno:**
- Celý projekt od základů — Flask app, modely, routes, templates
- Modely: Klient (IČ/DIČ/sídlo), Projekt, Zapis, User, Nabidka, NabidkaPolozka, TemplateConfig
- Routes: home dashboard, CRM, progress-report, klient_vyvoj, nabidky CRUD, Freelo API
- Brand systém: DrukCondensed (display only), Montserrat (vše ostatní)
- Freelo integrace: live úkoly per projekt, push ze zápisů
- Seed data: 5 klientů s realistickými scénáři (1M, 3M, 6M projekty)

**Opraveno:**
- timedelta import chyběl → crash /progress-report
- dashboard_old → dashboard (crash při loginu)
- Jinja2 sum filter chain → namespace loop
- Seed race condition (4 Gunicorn workery) → rollback()
- html2pdf.js → window.print() (rozbíjel layout)
- inline SVG logo místo img tagu (funguje v print)

**Změněno:**
- Druk omezen jen na display prvky
- DPH: select → number input, default 21%, step=1
- Nav: aktivní položka zvýrazněna cyan podtržením
- CRM tlačítka: Nabídka oranžová, výraznější písmo

**Přidáno:**
- CLAUDE.md pro kontinuitu sessions

---

*Poslední aktualizace: 21. 03. 2026*
*Verze aplikace: ~1.0 (pre-production, aktivní ostré použití)*
