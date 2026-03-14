# Commarec Zápisy

Webová aplikace pro generování zápisů ze schůzek pomocí AI a odesílání úkolů do Freela.

## Funkce
- Přihlašování pro tým (5–10 uživatelů)
- 3 šablony zápisů (Audit, Operativní schůzka, Obchodní schůzka)
- Generování zápisů pomocí Claude AI
- Automatické odeslání úkolů do Freela
- Archiv všech zápisů

## Nasazení na Railway (doporučeno)

### 1. Nahrajte kód na GitHub
1. Jděte na github.com a vytvořte nový repozitář (např. `commarec-zapisy`)
2. Nahrajte všechny soubory z této složky

### 2. Nasaďte na Railway
1. Jděte na railway.app a přihlaste se přes GitHub
2. Klikněte "New Project" → "Deploy from GitHub repo"
3. Vyberte váš repozitář
4. Railway automaticky detekuje Python aplikaci

### 3. Nastavte environment variables
V Railway → váš projekt → Variables přidejte:

```
ANTHROPIC_API_KEY=váš-anthropic-klíč
FREELO_API_KEY=váš-freelo-klíč
FREELO_PROJECT_ID=582553
SECRET_KEY=náhodný-dlouhý-řetězec-znaků
DATABASE_URL=automaticky-doplní-railway
```

Pro DATABASE_URL: v Railway přidejte PostgreSQL databázi přes "New" → "Database" → "PostgreSQL", pak zkopírujte connection string.

### 4. Přidejte databázi
1. V Railway projektu klikněte "+ New"
2. Vyberte "Database" → "PostgreSQL"
3. Railway automaticky propojí DATABASE_URL s vaší aplikací

### 5. První přihlášení
Po nasazení se přihlaste s výchozím adminem:
- E-mail: `admin@commarec.cz`
- Heslo: `admin123`

**Ihned po přihlášení si změňte heslo** (nebo smazejte účet a vytvořte nový přes správu).

### 6. Přidejte kolegy
Jděte na Správa → zadejte jméno, e-mail a heslo každého kolegy.

## Lokální spuštění (pro testování)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=váš-klíč
export FREELO_API_KEY=váš-freelo-klíč
export SECRET_KEY=cokoliv-pro-lokální-testování
python app.py
```

Otevřete http://localhost:5000
