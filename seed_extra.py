"""Extra seed data — 3 noví klienti s projekty po 1, 3 a 6 měsících."""
import json
from datetime import datetime, timedelta

def seed_extra_data(db, Klient, Projekt, Zapis, User, TEMPLATE_SECTIONS, assemble_output_text, generate_password_hash):
    """Přidá demo data pokud ještě neexistují."""
    try:
        if Klient.query.filter_by(slug="nabytek-centrum").first():
            return  # Už naseedováno
    except Exception:
        db.session.rollback()
        return

    print("Seeduji extra demo data (3 klienti × různé fáze projektu)...")

    admin  = User.query.filter_by(email="admin@commarec.cz").first()
    martin = User.query.filter_by(email="martin@commarec.cz").first()
    uid_m  = martin.id if martin else (admin.id if admin else 1)
    uid_a  = admin.id if admin else 1

    # ─── KLIENT 3: Nábytek Centrum — 6 měsíců ───
    k3 = Klient(
        nazev="Nábytek Centrum s.r.o.", slug="nabytek-centrum",
        kontakt="Tomáš Veselý", email="vesely@nabytekcentrum.cz",
        telefon="+420 603 456 789", adresa="Skandinávská 12, Praha 5, 155 00",
        poznamka="E-commerce nábytkář s vlastním skladem 8 000 m². Projekt 6 měsíců — velká transformace procesů i systémů.",
        profil_json=json.dumps({
            "typ_skladu": "e-commerce fulfillment", "pocet_sku": "12 400",
            "metody_pickingu": "batch picking, wave picking (zavedeno v projektu)",
            "pocet_zamestnanci": "67", "pocet_smen": "2",
            "wms_system": "Helios → Logiwa WMS (implementace dokončena)",
            "prumerna_denni_expedice": "1 240",
        }, ensure_ascii=False)
    )
    db.session.add(k3); db.session.flush()

    # ─── KLIENT 4: Pharma Distribution — 1 měsíc ───
    k4 = Klient(
        nazev="Pharma Distribution k.s.", slug="pharma-distribution",
        kontakt="Ing. Lucie Marková", email="markova@pharmadist.cz",
        telefon="+420 734 567 890", adresa="Nupaky 120, Nupaky 251 01",
        poznamka="Distributor farmaceutik. GDP regulované prostředí, FEFO, teplotní zóny. Projekt teprve začíná.",
        profil_json=json.dumps({
            "typ_skladu": "farmaceutický / regulovaný", "pocet_sku": "3 800",
            "metody_pickingu": "single order (FEFO povinný)",
            "pocet_zamestnanci": "31", "pocet_smen": "1",
            "wms_system": "SAP WM (stará verze)", "prumerna_denni_expedice": "320",
        }, ensure_ascii=False)
    )
    db.session.add(k4); db.session.flush()

    # ─── KLIENT 5: Auto Parts CZ — 3 měsíce ───
    k5 = Klient(
        nazev="Auto Parts CZ a.s.", slug="auto-parts-cz",
        kontakt="Radek Šimánek", email="simanek@autopartscz.cz",
        telefon="+420 608 234 567", adresa="Průmyslová zóna Mladá Boleslav, 293 01",
        poznamka="Dodavatel náhradních dílů. 28 500 SKU, projekt po 3 měsících — RF picking nasazen, výsledky viditelné.",
        profil_json=json.dumps({
            "typ_skladu": "automotive díly / B2B fulfillment", "pocet_sku": "28 500",
            "metody_pickingu": "RF picking zone A+B (79% objemu), papír zóna C",
            "pocet_zamestnanci": "45", "pocet_smen": "2",
            "wms_system": "vlastní SW + RF Zebra TC52", "prumerna_denni_expedice": "680",
        }, ensure_ascii=False)
    )
    db.session.add(k5); db.session.flush()

    td = timedelta

    # ─── PROJEKTY ───
    p3 = Projekt(
        nazev="WMS transformace + procesní optimalizace",
        popis="Kompletní transformace: výběr a implementace Logiwa WMS, redesign layoutu, kapacitní plánování pro sezónu Q4.",
        klient_id=k3.id, user_id=uid_m, is_active=True,
        datum_od=(datetime.utcnow() - td(days=185)).date(),
        datum_do=(datetime.utcnow() + td(days=5)).date(),
    )
    p4 = Projekt(
        nazev="GDP audit a digitalizace procesů",
        popis="Audit souladu s GDP, digitalizace pick-listů, příprava na certifikaci SÚKL. Fáze 1 z 3.",
        klient_id=k4.id, user_id=uid_a, is_active=True,
        datum_od=(datetime.utcnow() - td(days=32)).date(),
        datum_do=(datetime.utcnow() + td(days=60)).date(),
    )
    p5 = Projekt(
        nazev="Procesní optimalizace a RF terminály",
        popis="ABC analýza 28k SKU, přechod na RF picking, redesign zónování, snížení chybovosti expedice z 2,1% na <0,3%.",
        klient_id=k5.id, user_id=uid_m, is_active=True,
        datum_od=(datetime.utcnow() - td(days=92)).date(),
        datum_do=(datetime.utcnow() + td(days=88)).date(),
    )
    db.session.add_all([p3, p4, p5]); db.session.flush()

    all_bl = list(TEMPLATE_SECTIONS.get("audit", []))
    op_bl  = list(TEMPLATE_SECTIONS.get("operativa", []))
    ob_bl  = list(TEMPLATE_SECTIONS.get("obchod", []))

    def ci(d, rep, contact, name, place):
        return {"meeting_date": str(d.date()), "commarec_rep": rep,
                "client_contact": contact, "client_name": name, "meeting_place": place}

    def zapis(title, tmpl, summary, sections, uid, kid, pid, days_ago, sent=False):
        z = Zapis(
            title=title, template=tmpl, input_text="[seed demo data]",
            output_json=json.dumps(summary, ensure_ascii=False),
            output_text="", interni_prompt="", freelo_sent=sent,
            user_id=uid, klient_id=kid, projekt_id=pid,
            created_at=datetime.utcnow() - td(days=days_ago),
        )
        date_obj = datetime.utcnow() - td(days=days_ago)
        client_info = ci(date_obj, "Martin Komárek" if uid == uid_m else "Admin",
                         "Kontakt", title.split("—")[0].strip(), "On-site")
        z.output_text = assemble_output_text(client_info, summary, sections)
        return z

    # ═══ NÁBYTEK CENTRUM — 4 ZÁPISY (audit + 2 operativa + audit závěrečný) ═══

    s_nc1 = {
        "participants_commarec": "<p>Martin Komárek — vedoucí konzultant; Jana Horáčková — analytik</p>",
        "participants_company": "<p>Tomáš Veselý (CEO), Miroslav Krejčí (ředitel logistiky), Eva Nováková (IT manažer)</p>",
        "introduction": "<p>Úvodní diagnostická návštěva e-commerce skladu Nábytek Centrum, Praha 5. Sklad 8 000 m², 67 pracovníků, expedice 1 200 obj./den v low-season. Klient čelí dramatickému poklesu výkonu v Q4 (až 3 600 obj./den) a stávající systém (Helios + Excel) nestačí.</p>",
        "meeting_goal": "<p>Zmapovat aktuální stav procesů, identifikovat kritická úzká hrdla před sezónou a navrhnout roadmapu transformace.</p>",
        "findings": "<ul><li><strong>WMS:</strong> Helios bez wave-planningu, Excel pro rozpis práce — manuální, pomalé, chybné</li><li><strong>Layout:</strong> Fast-movers pohromadě s pomalými SKU, pick-trasy přes celý sklad (avg. 620 m/obj.)</li><li><strong>Sezónnost:</strong> Q4 nárůst 300% — sklad na to není připravený</li><li><strong>Chybovost:</strong> 1,8% expedičních chyb, NPS -12</li></ul>",
        "ratings": "<table><tr><th>Oblast</th><th>%</th><th>Komentář</th></tr><tr><td>WMS / systém</td><td>25</td><td>Helios bez wave planningu, Excel workaroundy</td></tr><tr><td>Layout a zónování</td><td>30</td><td>ABC analýza chybí, pick-trasy 620 m/obj.</td></tr><tr><td>Kapacitní plánování</td><td>20</td><td>Q4 plánování na Excel, bez predikcí</td></tr><tr><td>Produktivita</td><td>45</td><td>72 řádků/hod, potenciál 130+</td></tr><tr><td colspan=3><strong>Celkové skóre: 31% — kritický stav před sezónou</strong></td></tr></table>",
        "processes_description": "<p><strong>Příjem:</strong> Manuální bez RF, chyby v naskladnění. <strong>Picking:</strong> Single-order, papírové pick-listy, tiskárny na každém patře. <strong>Balení:</strong> 4 linky, úzké hrdlo Q4. <strong>Expedice:</strong> 3 dopravci, manifesting v Helios — funkční.</p>",
        "dangers": "<ul><li><strong>Q4 kolaps:</strong> Bez změny systému hrozí 2–3 denní delay expedice v listopadu</li><li><strong>Závislost na klíčových lidech:</strong> 2 lidi znají celý sklad — při výpovědi chaos</li><li><strong>NPS trend:</strong> -12 a klesá — vliv chybovosti expedice</li></ul>",
        "suggested_actions": "<p><strong>Urgentní (do 2 týdnů):</strong></p><ul><li>ABC analýza sortimentu — přesun top 500 SKU do přední zóny</li><li>Wave picking v Helios (omezená verze)</li></ul><p><strong>Střednědobě (1–3 měsíce):</strong></p><ul><li>Výběr a nákup nového WMS (Logiwa / Mintsoft)</li><li>RF terminály — pilotní nasazení příjem</li></ul><p><strong>Dlouhodobě (3–6 měsíců):</strong></p><ul><li>Go-live nového WMS před Q4 2025</li><li>Redesign layoutu druhého patra</li></ul>",
        "expected_benefits": "<ul><li><strong>Snížení pick-tras o 45%</strong> po ABC reorganizaci → +30% produktivita</li><li><strong>Chybovost pod 0,3%</strong> po RF nasazení → NPS z -12 na +20</li><li><strong>Q4 kapacita 3 800 obj./den</strong> s novým WMS</li></ul>",
        "additional_notes": "<p>Vedení je velmi motivované — Tomáš Veselý byl přítomen celý den. Eva Nováková (IT) má zkušenosti s WMS implementacemi — klíčová osoba pro projekt.</p>",
        "summary": "<p>Nábytek Centrum je v kritickém bodě. Stávající systémy nestačí ani na dnešní objem, natož na Q4. Priorita č. 1: ABC analýza a přesun fast-movers. Máme 6 měsíců do Q4 — je to reálné, ale musíme začít hned.</p>",
        "tasks": "UKOL: ABC analýza a reorganizace zóny A\nPOPIS: Analyzovat pohyb za 12 měsíců, přesunout top 500 SKU\nTERMIN: do 2 týdnů\n---\nUKOL: RFP pro nový WMS\nPOPIS: Zpracovat požadavky, oslovit 4 dodavatele (Logiwa, Mintsoft, Deposco, Katana)\nTERMIN: do 1 měsíce",
    }
    db.session.add(zapis("Nábytek Centrum — úvodní audit", "audit", s_nc1, all_bl, uid_m, k3.id, p3.id, 182))

    s_nc2 = {
        "participants_commarec": "<p>Martin Komárek — vedoucí konzultant</p>",
        "participants_company": "<p>Miroslav Krejčí (ředitel logistiky), Eva Nováková (IT), Pavel Dvořák (vedoucí směny)</p>",
        "introduction": "<p>Kontrolní schůzka po 2 měsících. ABC analýza a reorganizace zóny A dokončeny. Řešíme výsledky a výběr WMS.</p>",
        "meeting_goal": "<p>Vyhodnotit výsledky ABC reorganizace, rozhodnout o WMS dodavateli, naplánovat RF pilot.</p>",
        "current_state": "<ul><li><strong>ABC reorganizace:</strong> Dokončena — top 480 SKU přesunuto do zóny A</li><li><strong>Pick-trasy:</strong> z 620 m na 380 m (-39%) ✓</li><li><strong>Produktivita:</strong> z 72 na 94 řádků/hod (+31%) ✓</li><li><strong>Chybovost:</strong> 1,8% → 1,2% (cíl pod 0,3% — dosáhneme RF)</li><li><strong>WMS výběr:</strong> Finalisté Logiwa vs. Mintsoft</li></ul>",
        "findings": "<ul><li><strong>Pozitivní:</strong> ABC překonala očekávání, tým nadšený z rychlých výsledků</li><li><strong>Problém:</strong> Wave picking v Helios nestabilní — 2x crash za měsíc</li><li><strong>WMS:</strong> Logiwa vede — lepší API integrace s e-shopem, nižší TCO</li><li><strong>Q4 timeline:</strong> Go-live musí být do 15. 9.</li></ul>",
        "suggested_actions": "<p><strong>Ihned:</strong></p><ul><li>Podpis smlouvy s Logiwa — do konce týdne</li><li>Kick-off implementace — příští týden</li></ul><p><strong>Do konce měsíce:</strong></p><ul><li>RF terminály — nákup 8 ks Symbol TC52</li><li>Datová migrace 12 400 SKU do Logiwa</li></ul>",
        "summary": "<p>Výsledky ABC reorganizace výrazně lepší než plán. Projekt on-track. Kritická cesta: podpis Logiwa → implementace → go-live 15. 9.</p>",
        "tasks": "UKOL: Podepsat smlouvu Logiwa\nPOPIS: Odsouhlasit SLA a implementation scope\nTERMIN: do 5 dnů\n---\nUKOL: Nákup RF terminálů\nPOPIS: Objednat 8x Symbol TC52 + holstry + nabíječky\nTERMIN: do 10 dnů",
    }
    db.session.add(zapis("Nábytek Centrum — kontrola, výsledky ABC", "operativa", s_nc2, op_bl, uid_m, k3.id, p3.id, 122))

    s_nc3 = {
        "participants_commarec": "<p>Martin Komárek — vedoucí konzultant; Petra Houšková — WMS specialistka</p>",
        "participants_company": "<p>Eva Nováková (IT), Pavel Dvořák (vedoucí směny), Lenka Procházková (Logiwa PM)</p>",
        "introduction": "<p>Přípravná schůzka 3 týdny před go-live Logiwa WMS. Řešíme open itemy, zátěžové testování a Q4 contingency plán.</p>",
        "meeting_goal": "<p>Ověřit připravenost na go-live, projít open issues, finalizovat Q4 kapacitní plán.</p>",
        "current_state": "<ul><li><strong>WMS implementace:</strong> 94% hotovo — zbývá konfigurace 3 EDI napojení</li><li><strong>RF terminály:</strong> 8 ks v provozu na příjmu — funguje bez problémů</li><li><strong>Zátěžový test:</strong> 2 200 obj./den simulace — WMS zvládl, ale EDI bot měl timeout</li><li><strong>Produktivita:</strong> 108 řádků/hod (cíl 130 do Q4)</li></ul>",
        "findings": "<ul><li><strong>Kritické:</strong> EDI bot timeout při >2k obj./den — musí být opraveno před go-live</li><li><strong>OK:</strong> RF příjem eliminoval chyby naskladnění (z 12/týden na 0)</li><li><strong>Q4 plán:</strong> 3 směny v listopadu, 18 brigádníků od 1. 10.</li></ul>",
        "suggested_actions": "<p><strong>Do go-live (15. 9.):</strong></p><ul><li>Logiwa — fix EDI bot timeout (blocker)</li><li>Cutover plán — sobotní přestávka 36h</li></ul><p><strong>Po go-live:</strong></p><ul><li>Hypercare 2 týdny — Petra on-site denně</li><li>Onboarding brigádníků v Logiwa — 1. 10.</li></ul>",
        "summary": "<p>Jsme 3 týdny od go-live s jedním kritickým blokerem (EDI timeout). Pokud Logiwa dodá fix do 10. 9., jdeme dle plánu. Q4 kapacitní plán připraven — 3 směny + brigádníci → 3 800 obj./den.</p>",
        "tasks": "UKOL: Logiwa — fix EDI timeout\nPOPIS: P1 priorita, SLA 72h, eskalace na CTO\nTERMIN: do 10. 9.\n---\nUKOL: Finalizovat cutover plán\nPOPIS: Postup migrace dat, rollback scénář, komunikační plán\nTERMIN: do 12. 9.",
    }
    db.session.add(zapis("Nábytek Centrum — go-live příprava Logiwa", "operativa", s_nc3, op_bl, uid_m, k3.id, p3.id, 21, sent=True))

    s_nc4 = {
        "participants_commarec": "<p>Martin Komárek — vedoucí konzultant; Petra Houšková — WMS specialistka</p>",
        "participants_company": "<p>Tomáš Veselý (CEO), Miroslav Krejčí (ředitel logistiky), Eva Nováková (IT)</p>",
        "introduction": "<p>Závěrečný audit 6 měsíců od zahájení projektu. Logiwa WMS 2 týdny v ostrém provozu, RF terminály na všech operacích. Tým prošel školením, Q4 příprava finalizována.</p>",
        "meeting_goal": "<p>Vyhodnotit dosažené výsledky vs. původní KPI a navrhnout long-term partnership.</p>",
        "findings": "<ul><li><strong>WMS:</strong> Logiwa v provozu 14 dní, stabilní, wave picking automatický</li><li><strong>Produktivita:</strong> 124 řádků/hod (cíl 130, na 95%)</li><li><strong>Chybovost:</strong> 0,21% (cíl pod 0,3%) ✓ překonáno</li><li><strong>Pick-trasy:</strong> 218 m/obj. (z původních 620 m, -65%)</li><li><strong>NPS:</strong> +31 (z původních -12) ✓</li></ul>",
        "ratings": "<table><tr><th>Oblast</th><th>%</th><th>Komentář</th></tr><tr><td>WMS / systém</td><td>88</td><td>Logiwa plně funkční, wave automatizace</td></tr><tr><td>Layout a zónování</td><td>82</td><td>ABC zóny fungují, fine-tuning 2. patro zbývá</td></tr><tr><td>Kapacitní plánování</td><td>85</td><td>Q4 plán připraven, brigádníci onboarded</td></tr><tr><td>Procesní dokumentace</td><td>78</td><td>SOP v Notion, potřeba lokalizace CS</td></tr><tr><td>Produktivita</td><td>90</td><td>124/130 ř/hod — Q4 bude 130+</td></tr><tr><td colspan=3><strong>Celkové skóre: 85% — dramatické zlepšení z původních 31%</strong></td></tr></table>",
        "processes_description": "<p>Wave picking automaticky plánuje skupiny 20 objednávek dle zóny a dopravce. RF terminály eliminovaly papír na příjmu, vyskladnění i balení. Brigádníci zvládnou základní provoz po 4h zaškolení (bylo 3 týdny).</p>",
        "expected_benefits": "<ul><li><strong>+72% produktivita pickování</strong> (72 → 124 řádků/hod)</li><li><strong>-88% chybovost expedice</strong> (1,8% → 0,21%)</li><li><strong>NPS +43 bodů</strong> (-12 → +31)</li><li><strong>Q4 kapacita +60%</strong> (2 400 → 3 850 obj./den)</li><li><strong>ROI projektu:</strong> odhadováno 14 měsíců payback period</li></ul>",
        "summary": "<p>Transformace Nábytek Centrum je ukázkovým příkladem úspěšné logistické optimalizace. Za 6 měsíců jsme posunuli sklad z kritického stavu (31%) na high-performance operaci (85%). Klíčem byl správný sled kroků: rychlé výsledky (ABC) → technologie (WMS+RF) → fine-tuning.</p>",
        "tasks": "UKOL: Fine-tuning 2. patra\nPOPIS: ABC analýza specificky pro nábytek na paletách\nTERMIN: do 4 týdnů\n---\nUKOL: SOP lokalizace do češtiny\nPOPIS: Přeložit a aktualizovat procesní dokumenty\nTERMIN: do 3 týdnů",
    }
    db.session.add(zapis("Nábytek Centrum — závěrečný audit (6 měsíců)", "audit", s_nc4, all_bl, uid_m, k3.id, p3.id, 3, sent=True))

    # ═══ PHARMA DISTRIBUTION — 2 ZÁPISY (1 měsíc) ═══

    s_ph1 = {
        "participants_commarec": "<p>Admin — vedoucí konzultant; Martin Komárek — GDP specialista</p>",
        "participants_company": "<p>Ing. Lucie Marková (COO), PharmDr. Ondřej Blaha (Quality Manager), Simona Richterová (vedoucí skladu)</p>",
        "introduction": "<p>Úvodní GDP audit distribuční společnosti Pharma Distribution, Nupaky. Sklad 2 200 m², teplotní zóny (ambient, +2–8°C, +15–25°C). Klient připravuje obnovu GDP certifikátu.</p>",
        "meeting_goal": "<p>Identifikovat neshody s GDP požadavky, zmapovat FEFO kontroly, navrhnout plán digitalizace papírové dokumentace.</p>",
        "findings": "<ul><li><strong>FEFO:</strong> Papírová evidence, pracovníci FEFO kontrolují vizuálně — chyby 3–5x týdně</li><li><strong>Dokumentace:</strong> 100% papírová — batch records, pick-listy, teplotní záznamy</li><li><strong>Teplotní monitoring:</strong> Dataloggery jsou, ale data stahována ručně 1x týdně</li><li><strong>Personál:</strong> GDP školení 1x ročně — nedostatečné pro nové pracovníky</li></ul>",
        "ratings": "<table><tr><th>Oblast</th><th>%</th><th>Komentář</th></tr><tr><td>FEFO kontrola</td><td>30</td><td>Manuální, chybovost 3–5x/týden</td></tr><tr><td>Dokumentace</td><td>25</td><td>100% papírová, riziko ztráty/chyby</td></tr><tr><td>Teplotní monitoring</td><td>50</td><td>Dataloggery fungují, manuální stahování</td></tr><tr><td>Auditní připravenost</td><td>35</td><td>Systémové mezery v auditní stopě</td></tr><tr><td colspan=3><strong>Celkové skóre: 37% — kritické pro recertifikaci</strong></td></tr></table>",
        "processes_description": "<p>Příjem: papírový, kontrola COA manuálně. Picking: papírové pick-listy s FEFO polem — pracovníci vidí expiraci, ale SAP WM nehlídá. Expedice: GDP dokumenty přikládány ručně.</p>",
        "dangers": "<ul><li><strong>Recertifikace GDP:</strong> Audit SÚKL za 4 měsíce — stávající stav = riziko neúspěchu</li><li><strong>FEFO chyby:</strong> Vydání prošlého zboží → recall, pokuta, reputace</li><li><strong>Teplotní mezery:</strong> Nepokrytý víkend → auditní neshoda</li></ul>",
        "suggested_actions": "<p><strong>Priorita 1 — do 2 týdnů:</strong></p><ul><li>Kontinuální teplotní monitoring (cloud) — nahradit manuální stahování</li><li>FEFO školení všech pracovníků + checklist</li></ul><p><strong>Priorita 2 — do 6 týdnů:</strong></p><ul><li>Digitalizace pick-listů (tablet app s FEFO kontrolou)</li></ul>",
        "expected_benefits": "<ul><li><strong>FEFO chyby na 0</strong> po digitalizaci a školení</li><li><strong>Kontinuální teplotní dohled</strong> — auditní stopa bez mezer</li><li><strong>Úspěšná GDP recertifikace</strong> — základní obchodní podmínka</li></ul>",
        "summary": "<p>Pharma Distribution má funkční GDP základ, ale kritické mezery v digitalizaci a FEFO kontrole. Bez okamžitých opatření je recertifikace SÚKL riziková. Projekt má jasný scope — do certifikace zvládneme fázi 1.</p>",
        "tasks": "UKOL: Kontinuální teplotní monitoring\nPOPIS: Vybrat a nasadit cloud řešení (navrhujeme Elpro Libero)\nTERMIN: do 2 týdnů\n---\nUKOL: FEFO školení\nPOPIS: Workshop pro 31 pracovníků, aktualizovat SOP\nTERMIN: do 10 dnů",
    }
    db.session.add(zapis("Pharma Distribution — GDP audit fáze 1", "audit", s_ph1, all_bl, uid_a, k4.id, p4.id, 30))

    s_ph2 = {
        "participants_commarec": "<p>Admin — vedoucí konzultant</p>",
        "participants_company": "<p>Ing. Lucie Marková (COO), Simona Richterová (vedoucí skladu)</p>",
        "introduction": "<p>Měsíční follow-up po úvodním auditu. Sledujeme plnění prioritních akcí — teplotní monitoring a FEFO školení.</p>",
        "meeting_goal": "<p>Ověřit dokončení prioritních akcí, přejít na fázi 2 — digitalizace pick-listů.</p>",
        "current_state": "<ul><li><strong>Teplotní monitoring:</strong> Elpro Libero nasazen před 2 týdny — kontinuální, cloud, alarmy OK ✓</li><li><strong>FEFO školení:</strong> Hotovo, 31 pracovníků certifikovaných ✓</li><li><strong>FEFO chyby:</strong> z 3–5/týden na 0 za poslední 2 týdny ✓</li><li><strong>Tablet app:</strong> Výběr probíhá — 2 kandidáti: Soti MobiControl nebo TraceLink</li></ul>",
        "findings": "<ul><li><strong>Výborný pokrok</strong> — fáze 1 dokončena před termínem</li><li><strong>Teplotní data:</strong> Jeden alarm za 2 týdny — dveře mrazáku — správně zaznamenáno a vyřešeno</li><li><strong>Doporučení tablet:</strong> TraceLink — lépe integrovatelný s SAP</li></ul>",
        "suggested_actions": "<p><strong>Fáze 2 — tablet digitalizace:</strong></p><ul><li>Schválit TraceLink — podpis smlouvy</li><li>Datová migrace SAP → TraceLink mapování</li><li>Pilotní provoz příjem (2 tablety) — 3 týdny validace</li></ul>",
        "summary": "<p>Fáze 1 splněna na 100% — nulové FEFO chyby, kontinuální teplotní monitoring. Jsme měsíc do projektu a výsledky jsou viditelné. Fáze 2 digitalizace startuje příští týden.</p>",
        "tasks": "UKOL: Podpis smlouvy TraceLink\nPOPIS: Schválit commercial terms, podepsat\nTERMIN: do 5 dnů\n---\nUKOL: Pilotní provoz TraceLink příjem\nPOPIS: 2 tablety, příjmový proces, 3 týdny validace\nTERMIN: do 4 týdnů",
    }
    db.session.add(zapis("Pharma Distribution — měsíční follow-up", "operativa", s_ph2, op_bl, uid_a, k4.id, p4.id, 3))

    # ═══ AUTO PARTS CZ — 3 ZÁPISY (3 měsíce) ═══

    s_ap1 = {
        "participants_commarec": "<p>Martin Komárek — vedoucí konzultant; Jan Kovář — procesní analytik</p>",
        "participants_company": "<p>Radek Šimánek (ředitel logistiky), Ing. Barbora Kučerová (IT), Michal Vlček (vedoucí skladu)</p>",
        "introduction": "<p>Úvodní diagnostický audit skladu Auto Parts CZ, Mladá Boleslav. Sklad 14 000 m² s 28 500 SKU automotive dílů — od šroubků po náhradní karoserie. Klient reportuje rostoucí chybovost expedice a extrémně dlouhé vychystávání.</p>",
        "meeting_goal": "<p>Zmapovat aktuální picking procesy, identifikovat příčiny chybovosti 2,1% a navrhnout plán zavedení RF terminálů a ABC zónování pro 28k SKU.</p>",
        "findings": "<ul><li><strong>Pick-trasy:</strong> Bez ABC analýzy — picks přes celý sklad, avg. 890 m/obj.</li><li><strong>Systém:</strong> Vlastní SW + Excel — bez lokace, pracovníci znají sklad nazpaměť</li><li><strong>Chybovost:</strong> 2,1% — záměna podobných dílů (různá výrobní čísla)</li><li><strong>Dokumentace:</strong> Papírové pick-listy, zcela závislé na zkušenosti lidí</li></ul>",
        "ratings": "<table><tr><th>Oblast</th><th>%</th><th>Komentář</th></tr><tr><td>Systém / WMS</td><td>20</td><td>Vlastní SW bez lokací, Excel backup</td></tr><tr><td>ABC zónování</td><td>15</td><td>Žádná analýza, SKU umístěny historicky</td></tr><tr><td>Pick přesnost</td><td>35</td><td>2,1% chybovost, záměny podobných dílů</td></tr><tr><td>Produktivita</td><td>30</td><td>48 řádků/hod, potenciál 110+</td></tr><tr><td colspan=3><strong>Celkové skóre: 28% — kritický stav</strong></td></tr></table>",
        "processes_description": "<p>Single-order picking, papírový pick-list. Záměny dílů: podobná čísla (1K0-998-262 vs 1K0-998-262A) — bez barcode ověření. Balení a expedice funguje dobře — problém výhradně v pickingu.</p>",
        "dangers": "<ul><li><strong>Záměna dílů:</strong> 2,1% → reklamace, bezpečnostní riziko (brzdové součástky!)</li><li><strong>Znalostní závislost:</strong> 3 pickeři znají sklad nazpaměť — co při odchodu?</li><li><strong>Sezóna:</strong> Bez změn systému bude jaro 2026 opět krizové</li></ul>",
        "suggested_actions": "<p><strong>Fáze 1 (0–6 týdnů):</strong></p><ul><li>ABC analýza 28 500 SKU — prioritizace top 3 000</li><li>Lokační systém — doplnit do vlastního SW</li></ul><p><strong>Fáze 2 (6–12 týdnů):</strong></p><ul><li>RF terminály — Zebra TC52, scan-to-confirm picking</li><li>Fyzická reorganizace top 3k SKU do přední zóny</li></ul>",
        "expected_benefits": "<ul><li><strong>Chybovost pod 0,2%</strong> po zavedení RF picking s barcode kontrolou</li><li><strong>+100% produktivita</strong> po ABC reorganizaci a RF (48 → 110 ř/hod)</li><li><strong>Pick-trasy -60%</strong> po ABC reorganizaci</li></ul>",
        "summary": "<p>Auto Parts čelí kombinaci nejhorších logistických problémů: žádný systém lokací, žádná ABC analýza, papírový picking bez ověření. S 28k SKU a automotive díly je chybovost 2,1% nepřijatelná. Plán je jasný — musíme jít rychle.</p>",
        "tasks": "UKOL: ABC analýza 28 500 SKU\nPOPIS: Exportovat data pohybu za 24 měsíců, klasifikovat A/B/C\nTERMIN: do 3 týdnů\n---\nUKOL: Lokační systém v SW\nPOPIS: Definovat lokační schéma, programátor doplní\nTERMIN: do 4 týdnů",
    }
    db.session.add(zapis("Auto Parts CZ — úvodní audit (28k SKU)", "audit", s_ap1, all_bl, uid_m, k5.id, p5.id, 90))

    s_ap2 = {
        "participants_commarec": "<p>Martin Komárek — vedoucí konzultant</p>",
        "participants_company": "<p>Radek Šimánek (ředitel logistiky), Michal Vlček (vedoucí skladu)</p>",
        "introduction": "<p>Kontrolní schůzka po 6 týdnech. ABC analýza dokončena, lokační systém nasazen. Řešíme výsledky a RF terminál pilot.</p>",
        "meeting_goal": "<p>Vyhodnotit ABC reorganizaci a lokační systém, zahájit RF picking pilot na zóně A.</p>",
        "current_state": "<ul><li><strong>ABC analýza:</strong> Dokončena — top 2 800 SKU identifikováno (12% SKU = 78% picků)</li><li><strong>Fyzická reorganizace:</strong> 60% hotová — 1 680 SKU přesunuto</li><li><strong>Lokační systém:</strong> V provozu 2 týdny, 22 000 SKU nalokováno</li><li><strong>Chybovost:</strong> 2,1% → 1,4% (pokles díky lokacím)</li><li><strong>RF terminály:</strong> 6x Zebra TC52 objednáno, doručení za 2 týdny</li></ul>",
        "findings": "<ul><li><strong>ABC efekt:</strong> Pick-trasy z 890 m na 540 m (-39%) jen z částečné reorganizace</li><li><strong>Tým:</strong> První týden odpor, teď oceňují lokace</li><li><strong>Chybovost:</strong> Pokles na 1,4% — záměny stále, RF to vyřeší</li></ul>",
        "suggested_actions": "<p><strong>Po doručení RF terminálů:</strong></p><ul><li>RF pilot — zóna A, 3 terminály, 2 pickeři, 2 týdny</li><li>Dokončit barcode labeling zbývajících lokací</li></ul>",
        "summary": "<p>Výsledky po 6 týdnech slibné — pick-trasy -39%, chybovost -33%. Skutečný zlom přijde s RF picking. Za 4 týdny budeme mít první RF data.</p>",
        "tasks": "UKOL: RF pilot — zóna A\nPOPIS: 3 terminály, 2 pickeři, sběr dat přesnost a rychlost\nTERMIN: do 2 týdnů od doručení RF\n---\nUKOL: Dokončit barcode labeling\nPOPIS: Zbývá 22% lokací — tisknout a lepit štítky\nTERMIN: do 3 týdnů",
    }
    db.session.add(zapis("Auto Parts CZ — kontrola po 6 týdnech", "operativa", s_ap2, op_bl, uid_m, k5.id, p5.id, 48))

    s_ap3 = {
        "participants_commarec": "<p>Martin Komárek — vedoucí konzultant</p>",
        "participants_company": "<p>Radek Šimánek (ředitel logistiky), Michal Vlček (vedoucí skladu), Barbora Kučerová (IT)</p>",
        "introduction": "<p>Tříměsíční review projektu. RF picking pilot dokončen a expandován na celou zónu A + B. Vyhodnocujeme výsledky a plánujeme fázi 3 — full RF na celém skladu.</p>",
        "meeting_goal": "<p>Vyhodnotit 3 měsíce projektu, prezentovat ROI, rozhodnout o full RF rollout.</p>",
        "current_state": "<ul><li><strong>RF picking:</strong> Zóna A+B v provozu (78% objemu picků), 8 terminálů</li><li><strong>Chybovost v RF zónách:</strong> 2,1% → 0,18% ✓ dramatické zlepšení</li><li><strong>Pick-trasy:</strong> z 890 m na 290 m (-67%) po dokončení ABC reorganizace</li><li><strong>Produktivita:</strong> z 48 na 89 řádků/hod (+85%)</li><li><strong>Zóna C (22% SKU):</strong> stále papír, chybovost 3,2%</li></ul>",
        "findings": "<ul><li><strong>RF pilot — úspěch:</strong> záměny dílů v RF zónách nulové</li><li><strong>ROI:</strong> Investice RF (350k Kč) vs. úspora reklamací (180k Kč/měsíc) → payback 2 měsíce</li><li><strong>Tým:</strong> Pickeři si RF oblíbili — méně stresu, jasné instrukce</li></ul>",
        "suggested_actions": "<p><strong>Full RF rollout (fáze 3):</strong></p><ul><li>4 další terminály pro zónu C</li><li>RF příjem a expedice — dokončit celý tok</li></ul><p><strong>Do konce projektu (6 týdnů):</strong></p><ul><li>Sezónní kapacitní plán na jaro 2026</li><li>Předat systém internímu SOP</li></ul>",
        "summary": "<p>Po 3 měsících: chybovost z 2,1% na 0,18% v RF zónách, produktivita +85%, pick-trasy -67%. ROI 2 měsíce payback — excelentní výsledek. Zbývá rollout na zónu C a formalizace.</p>",
        "tasks": "UKOL: Objednat 4 RF terminály pro zónu C\nPOPIS: Zebra TC52 — stejný typ jako stávající\nTERMIN: do 1 týdne\n---\nUKOL: Kapacitní plán jaro 2026\nPOPIS: Modelovat peak sezónu — počty lidí, terminálů, směn\nTERMIN: do 3 týdnů\n---\nUKOL: SOP dokumentace RF picking\nPOPIS: Instruktážní video + PDF — předání internímu týmu\nTERMIN: do 4 týdnů",
    }
    db.session.add(zapis("Auto Parts CZ — 3 měsíce, RF výsledky", "operativa", s_ap3, op_bl, uid_m, k5.id, p5.id, 5, sent=True))

    # Obchodní zápis — Demo Expres (navazující)
    s_ob1 = {
        "participants_commarec": "<p>Admin — obchodní manažer</p>",
        "participants_company": "<p>Jana Horáčková (COO), Karel Dušek (CFO)</p>",
        "introduction": "<p>Follow-up obchodní schůzka po úspěšné WMS implementaci. Klient zvažuje rozšíření spolupráce na optimalizaci cross-docking procesů.</p>",
        "meeting_goal": "<p>Prezentovat nabídku pro rozšíření fáze 2 — cross-docking optimalizace a zákaznický portál pro sledování zásilek.</p>",
        "client_situation": "<ul><li><strong>WMS:</strong> Logiwa v provozu 3 měsíce — spokojeni</li><li><strong>Nová potřeba:</strong> Cross-docking pro 3 klíčové zákazníky — chybí proces</li><li><strong>Peak:</strong> Q3 jejich peak — 2x denní objem</li></ul>",
        "client_needs": "<ul><li>Optimalizace cross-docking toku (příjem → expedice do 4h)</li><li>Zákaznický portál — viditelnost zásilek pro B2B zákazníky</li><li>Kapacitní plánování pro Q3 2025</li></ul>",
        "opportunities": "<ul><li><strong>Cross-docking projekt:</strong> 8–12 týdnů, 280k Kč</li><li><strong>Zákaznický portál:</strong> SaaS řešení, 45k Kč/rok</li><li><strong>Long-term retainer:</strong> 25k Kč/měsíc od měsíce 3</li></ul>",
        "commercial_model": "<p><strong>Návrh: Professional + Retainer</strong></p><ul><li>Fáze 2 cross-docking: 280k Kč (fixní cena, 10 týdnů)</li><li>Retainer od měsíce 3: 25k Kč/měsíc</li><li>Zákaznický portál: 45k Kč/rok</li></ul>",
        "next_steps": "<ul><li>Zaslat detailní nabídku fáze 2 — do 5 dnů</li><li>Prezentace CFO — příští týden online</li><li>Cíl podpisu: do 14 dnů</li></ul>",
        "client_signals": "<ul><li><strong>Zájem:</strong> Jana nadšená, Karel opatrný — chce ROI kalkulaci</li><li><strong>Rozhodovatel:</strong> Finální slovo má CFO Karel Dušek</li><li><strong>Timeline:</strong> Start do konce října</li></ul>",
        "summary": "<p>Demo Expres je spokojený klient připravený na rozšíření. Key: přesvědčit CFO ROI kalkulací. Potenciál relationship 350k+ Kč/rok 2026.</p>",
        "tasks": "UKOL: Zaslat nabídku fáze 2\nPOPIS: Detailní scope, timeline, ROI kalkulace pro CFO\nTERMIN: do 5 dnů\n---\nUKOL: Připravit ROI prezentaci\nPOPIS: Porovnání cross-docking before/after, finanční model\nTERMIN: do 1 týdne",
    }
    db.session.add(zapis("Demo Expres — obchodní schůzka, rozšíření fáze 2", "obchod", s_ob1, ob_bl, uid_a, k4.id, p4.id, 7))

    db.session.commit()
    print("Extra seed: 3 klienti, 3 projekty, 10 zápisů (Nábytek 6M, Pharma 1M, AutoParts 3M + obchod)")
