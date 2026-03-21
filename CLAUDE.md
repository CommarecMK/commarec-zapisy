# Commarec Zápisy — Project Brief pro Claude

## Co je tento projekt
Interní Flask webová aplikace Commarec s.r.o. pro generování zápisů ze schůzek pomocí AI (Claude API), správu klientů, projektů, nabídek a progress reportů.

**Live URL:** https://web-production-76f2.up.railway.app  
**GitHub:** https://github.com/CommarecMK/commarec-zapisy  
**Hosting:** Railway (auto-deploy z main branch)  
**Login:** admin@commarec.cz / admin123  

---

## Tech Stack
- **Backend:** Python Flask + SQLAlchemy + PostgreSQL (Railway)
- **AI:** Claude claude-sonnet-4-5 přes Anthropic API
- **Frontend:** Jinja2 templates, vanilla JS, custom CSS
- **Deploy:** Gunicorn 4 workers (gthread), Railway
- **Fonty:** DrukCondensed Super (display), Montserrat (vše ostatní)

---

## Struktura souborů
```
app.py              — hlavní soubor, ~2400 řádků (modely + routes + helpers)
seed_extra.py       — demo data (3 klienti × různé fáze projektu)
templates/          — Jinja2 HTML templates
static/             — DrukCondensed-Super.woff2, logo-dark.svg, logo-white.svg
CLAUDE.md           — tento soubor
```

---

## Klíčové modely (DB)
- **Klient** — název, slug, kontakt, email, telefon, adresa, sidlo, ic, dic, logo_url, profil_json
- **Projekt** — klient_id, user_id, datum_od/do, is_active, freelo_project_id, freelo_tasklist_id
- **Zapis** — title, template (audit/operativa/obchod), input_text, output_json, output_text, tasks_json
- **Nabidka** — cislo (NAB-YYYY-NNN), klient_id, stav (draft/odeslana/prijata/zamitnuta), mena
- **NabidkaPolozka** — nazev, popis, mnozstvi, jednotka, cena_ks, sleva_pct, dph_pct (default 21)
- **User** — email, name, role (superadmin/admin/konzultant), is_admin
- **TemplateConfig** — editovatelné AI prompty per template typ

---

## Klíčové routes
| Route | Funkce |
|-------|--------|
| `/` | index → redirect na home |
| `/home` | nový dashboard (status overview) |
| `/dashboard` | starý dashboard (seznam zápisů) |
| `/crm` | CRM přehled všech klientů |
| `/progress-report` | progress report za období |
| `/klient/<id>/vyvoj` | vývoj klienta (timeline, Freelo úkoly) |
| `/nabidka/nova` | nová nabídka |
| `/nabidka/<id>` | detail nabídky s editací položek |
| `/api/freelo/projekt/<id>/ukoly` | live Freelo úkoly |

---

## Brand Guidelines
- **Navy:** #173767 (primary), #0E213E (dark), #050B15 (black)
- **Cyan:** #00AFF0 (primary), #008ABD (secondary)
- **Orange:** #FF8D00 (nabídky, CTA)
- **DrukCondensed:** POUZE na h1, hero tituly, velká display čísla (48px+)
- **Montserrat:** vše ostatní — tlačítka, nadpisy, labely, nav

---

## Integrace
- **Freelo API:** Basic auth (FREELO_EMAIL + FREELO_API_KEY), projekt ID 501350
  - `GET /tasklists/{id}/tasks` — načtení úkolů projektu
  - `POST /task/{id}/description` — zápis popisu
- **Anthropic API:** claude-sonnet-4-5, generování zápisů

---

## Railway env vars (potřebné)
```
DATABASE_URL        — PostgreSQL connection string
SECRET_KEY          — Flask secret
ANTHROPIC_API_KEY   — Claude API
FREELO_API_KEY      — Freelo
FREELO_EMAIL        — Freelo
FREELO_PROJECT_ID   — 501350
```

---

## Jak deployovat
1. Oprav soubory lokálně nebo v pracovní kopii
2. Nahraj na GitHub (Upload files nebo git push)
3. Railway nasadí automaticky do ~2 minut

---

## Aktuální TODO / Known Issues
- [ ] `app.py` je monolitický (~2400 řádků) — plánujeme rozdělit na blueprinty
- [ ] nabidka_nova.html — DPH input (step=1, default 21%) 
- [ ] nabidka_detail.html — PDF generování (html2pdf.js, separátní #pdfTemplate div)
- [ ] seed_extra.py — race condition fix při multi-worker startu

---

## Jak začít novou session s Claudem
1. Pošli odkaz na tento soubor nebo obsah
2. Řekni co chceš změnit
3. Claude opraví soubory a připraví ZIP k uploadu na GitHub

