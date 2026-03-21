# CLAUDE.md — Commarec Zápisy
> Tento soubor čti VŽDY jako první. Pak načti kód z GitHubu, projdi web živě a navrhni konkrétní posun.

---

## 🚀 Rychlý start pro novou session
```
1. Přečti tento soubor celý
2. Stáhni ZIP z GitHubu: https://github.com/CommarecMK/commarec-zapisy
3. Podívej se na živou aplikaci: https://web-production-76f2.up.railway.app
   - Přihlas se: admin@commarec.cz / admin123
   - Projdi: /prehled, /klient/1, /report/mesicni, /zapis/1
4. Navrhni TOP 3 konkrétní vylepšení na základě aktuálního stavu
5. Připrav ZIP k uploadu — vždy jen změněné soubory
6. Po každé změně aktualizuj CHANGELOG v tomto souboru
```

---

## 📍 Co je tento projekt
Interní Flask aplikace **Commarec s.r.o.** — konzultační firma zaměřená na optimalizaci skladů a logistiky.

**Hlavní use case:** Martin (a tým) ji používá po každé schůzce s klientem — nahraje přepis nebo poznámky, AI vygeneruje profesionální zápis, ten putuje do Freelea jako úkoly.

**Uživatelé:**
- Celý tým Commarec (konzultanti, Martin)
- Klienti uvidí části aplikace (veřejné zápisy, výhledově klientský portál)

**Klíčová priorita:** Stabilita a ostré použití. Aplikace se používá po každé schůzce — nesmí padat.

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
- Fonty: **Montserrat všude** (DrukCondensed byl odstraněn 22. 3. 2026)

---

## 📁 Struktura souborů
```
app.py                  — monolitický hlavní soubor (~3200 řádků) ⚠️
seed_extra.py           — demo data (5 klientů, různé fáze projektů)
CLAUDE.md               — tento soubor (VŽDY aktualizuj po změně)
requirements.txt        — Python závislosti
railway.toml            — Railway konfigurace

templates/
  base.html             — nav (lean: Přehled | +Nový zápis | AI Report | Správa), CSS variables
  prehled.html          — NOVÁ hlavní stránka /prehled (nahradila /home + /crm)
  klient_detail.html    — PŘEPRACOVANÝ detail klienta — vše na jednom místě + Freelo panel
  detail.html           — detail zápisu (AI obsah, Freelo úkoly, print CSS)
  novy.html             — formulář nového zápisu (3 šablony)
  nabidka_detail.html   — nabídka: editace položek, PDF (window.print)
  nabidka_nova.html     — nová nabídka
  progress_report.html  — report za období + Freelo splněné úkoly
  report_mesicni.html   — NOVÝ AI měsíční report generovaný Claudem
  admin.html            — správa uživatelů, šablon
  404.html, 500.html    — NOVÉ error stránky s brand stylem
  login.html, verejny.html, projekt_detail.html, klienti.html, dashboard.html

static/
  logo-dark.svg, logo-white.svg
```

---

## 🗄 Databázové modely
```python
Klient:
  nazev, slug, kontakt, email, telefon, adresa (provozní), sidlo (fakturační),
  ic, dic, logo_url, profil_json (AI extrakce), poznamka, is_active
  freelo_tasklist_id  ← NOVÉ: statické napojení klienta na Freelo tasklist

Projekt:
  nazev, klient_id, user_id, datum_od, datum_do, is_active,
  freelo_project_id, freelo_tasklist_id  ← legacy (nově se používá klient.freelo_tasklist_id)

Zapis:
  title, template (audit/operativa/obchod), input_text, output_json, output_text,
  tasks_json, notes_json, interni_prompt, freelo_sent, public_token, is_public,
  user_id, klient_id, projekt_id

Nabidka:
  cislo (NAB-YYYY-NNN auto), klient_id, projekt_id, user_id,
  nazev, poznamka, stav, platnost_do, mena

NabidkaPolozka:
  nabidka_id, poradi, nazev, popis, mnozstvi, jednotka, cena_ks, sleva_pct, dph_pct

User: email, name, role (superadmin/admin/konzultant), is_admin
TemplateConfig: template_key, name, system_prompt (editovatelný)
```

---

## 🛣 Klíčové routes
```
/                   → redirect na /prehled
/prehled            NOVÁ hlavní stránka — přehled klientů, filtry, skóre (nahradila /home + /crm)
/klient/<id>        PŘEPRACOVANÝ detail klienta (info + edit + Freelo + nabídky + timeline)
/klient/novy, /klient/<id>/upravit
/dashboard          starý přehled zápisů (záložní)
/progress-report    report za období + Freelo splněné úkoly
/report/mesicni     NOVÝ AI měsíční report
/nabidka/nova       nová nabídka (s klient_id param)
/nabidka/<id>       detail nabídky, editace, PDF
/nabidka/<id>/ulozit  AJAX save (JSON)
/nabidka/<id>/stav  změna stavu
/zapis/<id>         detail zápisu
/z/<token>          veřejný zápis (bez přihlášení)
/admin, /admin/templates

--- Freelo API endpoints (NOVÉ) ---
GET  /api/klient/<id>/freelo-ukoly          načte úkoly z tasklist klienta
POST /api/klient/<id>/freelo-nastavit       uloží tasklist_id ke klientovi
POST /api/klient/<id>/freelo-pridat-ukol    vytvoří nový úkol
GET  /api/klient/<id>/freelo-members        členové projektu pro přiřazení
POST /api/freelo/task/<id>/stav             finish/activate (POST /task/{id}/finish nebo /activate)
POST /api/freelo/task/<id>/edit             PUT /task/{id} + POST /task/{id}/description
POST /api/freelo/task/<id>/komentar         přidá komentář
GET  /api/freelo/task/<id>/komentare        načte komentáře
GET  /api/freelo/task/<id>/detail           detail úkolu vč. description
GET  /api/freelo/task/<id>/podukoly         podúkoly (GET /task/{id}/subtasks)
POST /api/klient/<id>/freelo-pridat-podukol vytvoří podúkol
POST /api/freelo/task/<id>/smazat           smaže úkol
GET  /api/freelo/tasklists-all              všechny tasklists pro výběr
GET  /api/freelo/projects                   projekty (fungující endpoint)
GET  /api/freelo/members/<project_id>       členové projektu (fungující endpoint)
```

---

## 🎨 Brand Guidelines
```
Navy:   #173767 (primary), #0E213E (dark), #050B15 (black)
Cyan:   #00AFF0 (primary), #008ABD (secondary)
Orange: #FF8D00 (nabídky, sekundární CTA)
Zelená: #34C759 (úspěch ≥70%)
Červená: #FF383C (danger, <40%)

MONTSERRAT — VŠE (DrukCondensed byl kompletně odstraněn 22. 3. 2026)
  Nadpisy stránek: 26px, font-weight: 800
  H2: 18px, font-weight: 700
  Tělo, labely, nav: 11–14px, font-weight: 500–700
```

---

## 🔗 Freelo integrace — KOMPLETNÍ PŘEHLED

### Architektura (nová)
- **Jeden Freelo projekt pro všechny klienty** (obvykle "Consulting-test" nebo "Sklad")
- **Každý klient = jeden tasklist** uložený v `klient.freelo_tasklist_id`
- Nastavuje se v detailu klienta: dropdown projekt → dropdown tasklist → Uložit
- Lze vytvořit nový tasklist přímo z aplikace

### Freelo API — ověřené správné endpointy
```
⚠️ KRITICKÉ POZNÁMKY PRO PŘÍŠTÍHO CLAUDA:

GET  /tasklist/{id}              → vrátí tasklist + tasks[] (BEZ /tasks na konci!)
GET  /task/{id}                  → detail úkolu, komentáře (description = comment.is_description=true)
GET  /task/{id}/subtasks         → podúkoly: {"data":{"subtasks":[...]}}
POST /task/{id}/finish           → označit jako hotový (NE PATCH, NE state:"done")
POST /task/{id}/activate         → znovu otevřít
POST /task/{id}/description      → uložit/přepsat popis
GET  /project/{id}/workers       → členové projektu
POST /project/{pid}/tasklist/{tlid}/tasks  → vytvořit úkol

Podúkoly mají DVĚ ID:
  - "id" = subtask record ID (nepoužitelné pro API volání!)
  - "task_id" = skutečné Freelo task ID → toto použij pro finish/activate/edit

Editace (PUT/POST /task/{id}) — stále se testuje správný verb
  Aktuálně: zkusí POST, při 404 fallback na PUT
  Popis: POST /task/{id}/description s {"content": "<div>text</div>"}

Auth: Basic (FREELO_EMAIL + FREELO_API_KEY)
Base URL: https://api.freelo.io/v1
```

### Stav Freelo integrace
```
Nastavení tasklist (dropdown)   ✅ Funguje
Načítání úkolů ze tasklist       ✅ Funguje (GET /tasklist/{id})
Označit hotový/otevřít           ✅ Funguje (POST /finish, /activate)
Přidat komentář                  ✅ Funguje
Načíst komentáře                 ✅ Funguje
Vytvořit nový úkol               ✅ Funguje
Smazat úkol                      ✅ Funguje
Přiřadit zodpovědnou osobu        ⚠️ Dropdown načten, uložení testuje se (PUT vs POST)
Editace názvu/deadline            ⚠️ Testuje se správný verb
Editace popisu (description)      ⚠️ POST /description funguje, ale lazy load fix probíhá
Podúkoly — zobrazení              ✅ Funguje
Podúkoly — označit hotový         ⚠️ Opraveno task_id vs id, testovat
Vytvořit podúkol                  ✅ Implementováno
Freelo data v progress reportu    ✅ Splněné úkoly za období
Freelo kontext v AI reportu       ✅ Předán Claudovi
```

### Diagnostické endpointy (pro ladění)
```
/api/freelo/test-ukoly/<tasklist_id>   → testuje URL formáty
/api/freelo/debug-task/<task_id>       → plná struktura úkolu
/api/freelo/debug-tasklist/<id>        → plná struktura tasklist
/api/freelo/debug-state/<task_id>      → testuje PATCH formáty
/api/freelo/debug-state2/<task_id>     → testuje POST formáty
/api/freelo/debug-edit/<task_id>       → testuje edit endpointy
```

---

## 🤖 AI funkce

### Generování zápisů
- Model: claude-sonnet-4-5
- 3 šablony: audit / operativa / obchod
- System prompty editovatelné v Správě → Šablony
- Sekce se vybírají checkboxy před generováním
- Každá sekce editovatelná inline + AI úprava

### AI měsíční report (/report/mesicni)
- Klient + období → Claude shrne všechny zápisy z období
- Strukturovaný output: executive summary, zjištění, pokrok, rizika, next steps
- Integruje Freelo data: splněné úkoly za období, otevřené úkoly
- Tisknutelné do PDF (Ctrl+P)
- ⚠️ Opravit: chybí timedelta v šabloně → předáváme od_default, do_default stringly

---

## ⚙️ Railway env vars
```
DATABASE_URL     PostgreSQL connection string (Railway poskytuje auto)
SECRET_KEY       Flask session secret
ANTHROPIC_API_KEY Claude API key
FREELO_API_KEY   Freelo API key
FREELO_EMAIL     Freelo přihlašovací email
FREELO_PROJECT_ID 501350 (legacy, nově se používá dynamicky)
```

---

## 🧠 Hodnocení kódu (upřímné — pro dalšího Clauda)

### Funguje dobře ✅
- Flask architektura správná, SQLAlchemy modely dobře navrženy
- Brand CSS systém (variables) konzistentní
- AI prompty dobře strukturované, sekce fungují
- Freelo push nových úkolů ze zápisů funguje
- Error stránky 404/500 existují s brand stylem
- Přehled klientů (prehled.html) je přehledný a funkční
- Detail klienta — vše na jednom místě, inline edit, Freelo panel

### Otevřené problémy ⚠️
- **app.py MONOLITH (~3200 řádků)** — každá změna je riziková
- **Freelo edit úkolů** — stále se hledá správný HTTP verb (POST vs PUT)
- **Popis úkolu** — lazy load opravován, testovat
- **Podúkoly finish** — opraveno task_id, ale neotestováno
- **Zodpovědná osoba** — dropdown funguje, uložení testovat
- **Email zápisů** — chybí, bylo odstraněno
- **Mobilní verze** — CSS není responsivní
- **PDF** — window.print() funguje ale uživatel ukládá ručně

### Technický dluh 🔴 (prioritizovaný)
1. Freelo edit/finish/zodpovědná osoba — dodokončit a otestovat
2. Email zápisů klientům (Microsoft 365 SMTP)
3. Responsivní CSS
4. app.py blueprinty — rozdělit na moduly
5. Server-side PDF (WeasyPrint)

---

## 📊 Aktuální stav funkcí
```
Generování zápisů (AI)           ✅ audit/operativa/obchod
Hlavní přehled (/prehled)        ✅ NOVÝ — klienti, filtry, skóre, delta
Detail klienta                   ✅ PŘEPRACOVANÝ — vše na jednom místě
Inline editace klienta           ✅ Jméno, kontakt, IČ, DIČ, poznámky, profil skladu
Přidání projektu z detailu       ✅ Funguje
Progress Report                  ✅ + Freelo splněné úkoly za období
AI Měsíční report                ✅ Funguje, Freelo data integrována
Nabídky — editace                ✅ DPH number input, step=1, default 21%
Nabídky — PDF                    ⚠️ window.print() — funguje, layout OK
Freelo panel v detailu klienta   ⚠️ Základní funkce OK, edit/finish se ladí
Error stránky (404/500)          ✅ Přidány s brand stylem
Veřejný zápis (/z/token)         ✅ Existuje
Email zápisů                     ❌ Chybí
Mobilní verze                    ❌ CSS není responsivní
Klientský portál                 ❌ Plánováno
```

---

## 🗺 Roadmapa (aktualizovaná)

### Ihned (nedodělané z dnešní session)
1. **Freelo edit** — ověřit správný HTTP verb z `/api/freelo/debug-edit/28782591`
2. **Freelo popis** — lazy load + POST /description
3. **Freelo podúkoly finish** — ověřit task_id vs id

### Fáze A — Stabilizace
4. **Email zápisů** — zaslat zápis klientovi (Microsoft 365 SMTP)
5. **Responsivní CSS** — základní mobile breakpoints

### Fáze B — Klientský portál
6. **Klientský portál** — klient vidí své zápisy, nabídky, stav projektů
7. **Sdílení nabídky** — link bez přihlášení (jako veřejný zápis)

### Fáze C — Optimalizace kódu
8. **app.py blueprinty**
9. **Testy** — aspoň smoke testy

### Fáze D — Rozšíření
10. **Analytika** — grafy vývoje skóre přes čas
11. **Datové analýzy** — upload Excel od klienta, Claude shrne čísla do reportu

---

## 📝 CHANGELOG

### 2026-03-22 — Velká session (celý den)

**Navigace & UX přestavba:**
- Navigace zjednodušena ze 7 položek na 3: Přehled | +Nový zápis | AI Report | Správa
- Nová hlavní stránka `/prehled` — spojuje CRM + dashboard, filtry, skóre s deltou, otevřené úkoly
- `/home` a `/crm` přesměrovány na `/prehled`

**Detail klienta — kompletní přestavba:**
- Vše na jednom místě: info + inline edit + poznámky (autosave) + profil skladu + projekty + skóre history + otevřené úkoly + chronologie zápisů + nabídky
- Inline editace: jméno, kontakt, IČ, DIČ, sídlo, poznámky, profil skladu
- Přidání nového projektu z detailu bez opuštění stránky
- API: `/api/klient/<id>/upravit`, `/api/klient/<id>/poznamky`, `/api/klient/<id>/profil`

**Error stránky:**
- 404.html + 500.html s brand stylem (Navy/Cyan)
- Flask handlery: `@app.errorhandler(404)`, `@app.errorhandler(500)` + db.session.rollback()

**Fonty:**
- DrukCondensed kompletně odstraněn ze všech 10 šablon
- Montserrat všude, velikosti sníženy (nadpisy: 26px místo 36px+)

**AI měsíční report (/report/mesicni):**
- Nová stránka pro generování měsíčního reportu z zápisů
- Claude dostane všechny zápisy za období + Freelo splněné/otevřené úkoly
- Structured JSON output: executive summary, zjištění, pokrok, rizika, next steps, skóre vizualizace
- Opravena chyba: `timedelta is undefined` → předáváme `od_default`, `do_default`

**Freelo integrace — kompletní přestavba:**
- Nová architektura: klient → tasklist (statické napojení přes `klient.freelo_tasklist_id`)
- DB migrace: nový sloupec `klient.freelo_tasklist_id`
- Kaskádový výběr: Freelo projekt → tasklist → Uložit + možnost vytvořit nový tasklist
- Opraveno: Freelo API vrací `/tasklist/{id}` (bez /tasks), ne `/tasklists/{id}/tasks`
- Freelo panel v detailu klienta: zobrazení úkolů (otevřené/hotové/vše), tabs
- Akce: označit hotový (POST /finish, /activate), přidat komentář, vytvořit úkol, smazat
- Editace úkolu: název, popis (POST /description), deadline, zodpovědná osoba (dropdown z workers)
- Podúkoly: zobrazení (GET /subtasks), vytvoření, finish — opraveno task_id vs subtask.id
- Zodpovědná osoba: dropdown načtený z `/api/freelo/members/{project_id}`
- Freelo splněné úkoly v progress reportu (za dané období)
- Freelo data v AI měsíčním reportu (splněné + otevřené úkoly)
- Diagnostické endpointy pro ladění API

**Opraveno:**
- Freelo API parsování: `raw.json()` může být list nebo dict — robustní handling všude
- `'list' object has no attribute 'get'` — opraveno na 5 místech
- Stav podúkolů: `date_finished != null` = hotový (ne string state)
- Lazy loading description úkolu při otevření editace

### 2026-03-21 — Velká session
- Celý projekt od základů (viz původní CLAUDE.md)

---

## ❓ Zodpovězené otázky
- **Uživatelé:** celý tým Commarec, desktop only (mobil nízká priorita)
- **Freelo workflow:** jeden projekt pro všechny klienty, každý klient = jeden tasklist
- **Email zápisů:** dnes ručně z Outlooku → chceme tlačítko přímo z aplikace
- **Měsíční report:** dnes Figma/Canva/PPT → cíl: AI report přímo z aplikace
- **Nabídky:** dnes Excel → v aplikaci základ funguje, PDF chybí server-side
- **Datové analýzy:** Excel tabulky → výhledově upload + AI shrnutí do reportů

---

*Poslední aktualizace: 22. 03. 2026*
*Verze aplikace: ~1.5 (ostré použití, Freelo integrace se ladí)*
