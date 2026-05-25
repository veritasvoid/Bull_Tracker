# 🐂 Bull Tracker

Personal trading intelligence dashboard. Aggregates congressional trades, corporate insider filings, and whale hedge fund moves — all from free public sources — into one clean dashboard.

**Live at:** `https://YOUR_USERNAME.github.io/Bull_Tracker`

---

## What It Tracks

| Feed | Source | Update Frequency |
|------|--------|-----------------|
| 🏛 Congressional trades | Senate eFDS, House Disclosures, QuiverQuant | Every 6 hours |
| 📋 Corporate insider trades | SEC EDGAR Form 4 | Every 6 hours |
| 🐋 Whale hedge fund moves | SEC EDGAR Form 13F | Quarterly |

### Watchlist — Politicians
Nancy Pelosi · Paul Pelosi · Donald Trump · Dan Crenshaw · Michael McCaul ·
Josh Gottheimer · Ro Khanna · Brian Mast · Marjorie Taylor Greene ·
Susie Lee · David Rouzer · Tommy Tuberville · Sheldon Whitehouse

### Watchlist — Whale Funds
Berkshire Hathaway (Buffett) · Pershing Square (Ackman) · Scion (Burry) ·
Bridgewater (Dalio) · Tiger Global (Coleman) · Duquesne (Druckenmiller) ·
Third Point (Loeb) · Appaloosa (Tepper)

---

## Setup (5 minutes)

### 1. Create the GitHub repo
- Go to github.com → New repository
- Name it `Bull_Tracker`
- Set to **Public** (required for free GitHub Pages)
- Don't initialize with README

### 2. Push this code
```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/Bull_Tracker.git
git push -u origin main
```

### 3. Add your Anthropic API key
- GitHub repo → Settings → Secrets and variables → Actions
- Click **New repository secret**
- Name: `ANTHROPIC_API_KEY`
- Value: your API key from console.anthropic.com

### 4. Enable GitHub Pages
- GitHub repo → Settings → Pages
- Source: **Deploy from a branch**
- Branch: `main` / `/ (root)`
- Save → your dashboard will be live in ~60 seconds

### 5. Run the first data fetch
- GitHub repo → Actions tab
- Click **Fetch Trading Data**
- Click **Run workflow**
- Wait ~2 minutes for data to populate

---

## How It Works

```
GitHub Actions (every 6h)
    ↓
scripts/fetch_congressional.py  →  data/congressional_trades.json
scripts/fetch_insiders.py       →  data/insider_trades.json
scripts/fetch_whales.py         →  data/whale_trades.json
    ↓
Commits JSON to repo
    ↓
index.html (GitHub Pages) loads JSON → renders dashboard
```

The dashboard is a single static HTML file. It fetches the JSON data files on load. Since the JSON files live in your repo, the data is identical on every device that opens your GitHub Pages URL.

---

## Unusual Activity Flags

A trade gets flagged ⚠ UNUSUAL when:
- Congressional trade filed within 5 days of the 45-day deadline
- Single trade > $1M (congressional) or > $5M (insider)
- Multiple insiders buying the same ticker in the same week
- Hedge fund opens a brand-new position
- Hedge fund holds > $100M in a single name

---

## Data Sources (all free, no auth required)

- **Senate eFDS**: `efts.senate.gov` — Senate financial disclosures
- **House Disclosures**: `disclosures.house.gov` — House financial disclosures
- **QuiverQuant**: `api.quiverquant.com` — Aggregated congressional trading
- **SEC EDGAR Form 4**: `efts.sec.gov` — Corporate insider trades
- **SEC EDGAR Form 13F**: `data.sec.gov` — Hedge fund quarterly holdings

---

## Customization

**Add a politician to the watchlist** — edit `scripts/fetch_congressional.py`:
```python
WATCHLIST = [
    ...
    {"name": "Your Person", "chamber": "House", "party": "D", "state": "CA"},
]
```

**Add a company to insider watch** — edit `scripts/fetch_insiders.py`:
```python
WATCH_COMPANIES = {
    ...
    "TICKER": "Company Name",
}
```

**Add a whale fund** — edit `scripts/fetch_whales.py`:
```python
WHALE_FUNDS = [
    ...
    {"name": "Fund Name", "cik": "0001234567", "manager": "Manager Name"},
]
```
Find CIK numbers at `sec.gov/cgi-bin/browse-edgar`.

---

*All data sourced from free public government filings. Not financial advice.*
