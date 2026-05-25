"""
fetch_whales.py
Fetches SEC Form 13F quarterly holdings for major hedge funds.
Detects new positions, large exits, and size changes.
"""

import json, os, re, time, sys
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.parse

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "whale_trades.json"
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")

# Major funds to track — CIK from SEC EDGAR
WHALE_FUNDS = [
    {"name": "Berkshire Hathaway",  "cik": "0001067983", "manager": "Warren Buffett"},
    {"name": "Pershing Square",      "cik": "0001336528", "manager": "Bill Ackman"},
    {"name": "Scion Asset Mgmt",     "cik": "0001649339", "manager": "Michael Burry"},
    {"name": "Bridgewater Assoc",    "cik": "0001350694", "manager": "Ray Dalio"},
    {"name": "Tiger Global Mgmt",    "cik": "0001167483", "manager": "Chase Coleman"},
    {"name": "Duquesne Family Off",  "cik": "0001536411", "manager": "Stanley Druckenmiller"},
    {"name": "Third Point LLC",      "cik": "0001040273", "manager": "Dan Loeb"},
    {"name": "Appaloosa Mgmt",       "cik": "0001418814", "manager": "David Tepper"},
]


def get_latest_13f(cik):
    """Get the most recent 13F-HR filing accession number for a CIK."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BullTracker Personal Research veritasvoid2025@gmail.com"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())

        filings = data.get("filings", {}).get("recent", {})
        forms   = filings.get("form", [])
        accnums = filings.get("accessionNumber", [])
        dates   = filings.get("filingDate", [])

        for i, form in enumerate(forms):
            if form in ("13F-HR", "13F-HR/A"):
                return {
                    "accession": accnums[i],
                    "date":      dates[i],
                    "form":      form,
                }
    except Exception as e:
        print(f"[{cik}] Submissions error: {e}", file=sys.stderr)

    return None


def fetch_13f_holdings(cik, accession, fund_meta):
    """Fetch and parse 13F holdings using EDGAR viewer API (avoids Archives 403)."""
    positions = []

    acc_dashed = accession  # already dashed from submissions API
    cik_clean  = cik.lstrip("0")

    HEADERS = {"User-Agent": "BullTracker Personal Research veritasvoid2025@gmail.com"}

    # Strategy 1: EDGAR filing index via data.sec.gov (not www.sec.gov/Archives)
    try:
        acc_nodash = acc_dashed.replace("-", "")
        idx_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        # We already have the accession — go straight to the document list
        doc_url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q=%22{acc_dashed}%22&forms=13F-HR"
        )
        req = urllib.request.Request(doc_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())

        hits = data.get("hits", {}).get("hits", [])
        for hit in hits[:1]:
            src = hit.get("_source", {})
            # Build archive URL with proper headers
            archive_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/"
                f"{acc_nodash}/{acc_nodash}-index.htm"
            )
            req2 = urllib.request.Request(archive_url, headers=HEADERS)
            try:
                with urllib.request.urlopen(req2, timeout=15) as r2:
                    html = r2.read().decode("utf-8", errors="replace")
                xml_links = re.findall(r'href="(/Archives/[^"]+infotable[^"]*\.xml)"', html, re.IGNORECASE)
                if not xml_links:
                    xml_links = re.findall(r'href="(/Archives/[^"]+\.xml)"', html, re.IGNORECASE)
                if xml_links:
                    xml_url = f"https://www.sec.gov{xml_links[0]}"
                    req3 = urllib.request.Request(xml_url, headers=HEADERS)
                    with urllib.request.urlopen(req3, timeout=15) as r3:
                        xml = r3.read().decode("utf-8", errors="replace")
                    positions = parse_infotable(xml, fund_meta, acc_dashed)
            except Exception as e2:
                print(f"[13F] {fund_meta['name']} archive: {e2}", file=sys.stderr)

    except Exception as e:
        print(f"[13F] {fund_meta['name']}: {e}", file=sys.stderr)

    # Strategy 2: Quiver Quant institutional endpoint as fallback
    if not positions:
        positions = fetch_13f_quiverquant(fund_meta)

    return positions


def fetch_13f_quiverquant(fund_meta):
    """Fallback: fetch institutional holdings from Quiver Quant."""
    positions = []
    # Map fund names to Quiver Quant institution names
    name_map = {
        "Berkshire Hathaway":  "BERKSHIRE HATHAWAY INC",
        "Pershing Square":     "PERSHING SQUARE CAPITAL MANAGEMENT",
        "Scion Asset Mgmt":    "SCION ASSET MANAGEMENT",
        "Bridgewater Assoc":   "BRIDGEWATER ASSOCIATES",
        "Tiger Global Mgmt":   "TIGER GLOBAL MANAGEMENT",
        "Duquesne Family Off": "DUQUESNE FAMILY OFFICE",
        "Third Point LLC":     "THIRD POINT LLC",
        "Appaloosa Mgmt":      "APPALOOSA MANAGEMENT",
    }
    institution = name_map.get(fund_meta["name"], "")
    if not institution:
        return positions

    url = f"https://api.quiverquant.com/beta/live/hedgefunds/{urllib.parse.quote(institution)}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "BullTracker Personal Research veritasvoid2025@gmail.com",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())

        for item in (data if isinstance(data, list) else [])[:50]:
            ticker  = (item.get("Ticker") or "").upper()
            shares  = item.get("Shares") or 0
            value   = item.get("Value") or 0  # in thousands
            val_usd = float(value) * 1000

            if val_usd < 1_000_000:
                continue

            positions.append({
                "id":             f"13f_qv_{fund_meta['name'].replace(' ','_')}_{ticker}",
                "name":           fund_meta["name"],
                "fund":           fund_meta["name"],
                "manager":        fund_meta["manager"],
                "ticker":         ticker or None,
                "company":        item.get("Security") or ticker,
                "type":           "hold",
                "shares":         int(shares),
                "amount_min":     val_usd,
                "amount_max":     val_usd,
                "amount_raw":     f"${val_usd/1e6:.1f}M",
                "trade_date":     item.get("Date"),
                "filed_date":     item.get("Date"),
                "delay_days":     0,
                "sector":         None,
                "source":         "QuiverQuant 13F",
                "unusual":        val_usd >= 100_000_000,
                "unusual_reason": f"${val_usd/1e6:.0f}M position" if val_usd >= 100_000_000 else None,
                "summary":        None,
            })
    except Exception as e:
        print(f"[QuiverQuant 13F] {fund_meta['name']}: {e}", file=sys.stderr)

    return positions


def parse_infotable(xml, fund_meta, accession):
    """Parse 13F infotable XML into position records."""
    positions = []

    entries = re.findall(r'<infoTable>(.*?)</infoTable>', xml, re.DOTALL | re.IGNORECASE)

    def tag(t, content):
        m = re.search(rf'<{t}[^>]*>(.*?)</{t}>', content, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    for entry in entries:
        name_co  = tag("nameOfIssuer", entry)
        ticker   = (tag("cusip", entry) or "").upper()  # CUSIP, not ticker
        value    = tag("value", entry)          # in thousands
        shares   = tag("sshPrnamt", entry)
        put_call = tag("putCall", entry)
        stype    = tag("sshPrnamtType", entry)

        # Skip puts/calls for simplicity
        if put_call and put_call.upper() in ("PUT", "CALL"):
            continue

        try:
            val_k    = int(re.sub(r'[^\d]', '', value)) if value else 0
            shares_n = int(re.sub(r'[^\d]', '', shares)) if shares else 0
            val_usd  = val_k * 1000
        except Exception:
            val_k, shares_n, val_usd = 0, 0, 0

        if val_usd < 1_000_000:  # Skip tiny positions < $1M
            continue

        unusual = val_usd >= 100_000_000  # Flag $100M+ positions

        positions.append({
            "id":             f"13f_{fund_meta['name'].replace(' ','_')}_{name_co}_{accession}",
            "name":           fund_meta["name"],
            "fund":           fund_meta["name"],
            "manager":        fund_meta["manager"],
            "ticker":         None,  # 13F uses CUSIP not ticker — resolved below
            "company":        name_co,
            "type":           "hold",     # 13F shows holdings, not transactions
            "shares":         shares_n,
            "amount_min":     val_usd,
            "amount_max":     val_usd,
            "amount_raw":     f"${val_usd/1e6:.1f}M",
            "trade_date":     None,
            "filed_date":     None,  # set by caller
            "delay_days":     0,
            "sector":         None,
            "source":         "SEC 13F",
            "unusual":        unusual,
            "unusual_reason": f"${val_usd/1e6:.0f}M position" if unusual else None,
            "summary":        None,
        })

    # Sort by value descending
    positions.sort(key=lambda p: p["amount_max"], reverse=True)
    return positions[:50]  # Top 50 per fund


def resolve_cusip_to_ticker(positions):
    """
    Best-effort CUSIP → ticker resolution using EDGAR's company search.
    Skips on failure — ticker stays None.
    """
    # Build a small known map for major holdings
    known = {
        "037833100": "AAPL", "594918104": "META", "023135106": "AMZN",
        "02079K305": "GOOGL","67066G104": "NVDA", "88160R101": "TSLA",
        "912828V57": "UST",  "46625H100": "JPM",  "747525103": "PG",
        "931142103": "WMT",  "30303M102": "META",  "079862105": "BRK",
        "191216100": "KO",   "166764100": "CVX",   "26875P101": "DJT",
    }

    for p in positions:
        # Try known map first
        cusip = (p.get("ticker") or "").replace("-", "")
        if cusip in known:
            p["ticker"] = known[cusip]
        else:
            # Try to match company name to ticker
            name = p.get("company", "").upper()
            ticker_guess = extract_ticker_from_name(name)
            if ticker_guess:
                p["ticker"] = ticker_guess

    return positions


def extract_ticker_from_name(name):
    """Heuristic company name → ticker."""
    mapping = {
        "APPLE": "AAPL", "MICROSOFT": "MSFT", "AMAZON": "AMZN",
        "ALPHABET": "GOOGL", "NVIDIA": "NVDA", "META PLATFORMS": "META",
        "TESLA": "TSLA", "BERKSHIRE": "BRK-B", "JPMORGAN": "JPM",
        "VISA": "V", "MASTERCARD": "MA", "UNITEDHEALTH": "UNH",
        "EXXON": "XOM", "CHEVRON": "CVX", "WALMART": "WMT",
        "JOHNSON": "JNJ", "PROCTER": "PG", "HOME DEPOT": "HD",
        "ELI LILLY": "LLY", "ABBVIE": "ABBV", "PFIZER": "PFE",
        "BANK OF AMERICA": "BAC", "COCA-COLA": "KO", "MERCK": "MRK",
        "TRUMP MEDIA": "DJT",
    }
    for key, ticker in mapping.items():
        if key in name:
            return ticker
    return None


def detect_new_positions(current, previous):
    """
    Compare current vs previous 13F to find new entries.
    Sets type='buy' for new positions, 'sell' for exits.
    """
    if not previous:
        return current

    prev_companies = {p["company"].upper() for p in previous}

    for pos in current:
        company_up = pos["company"].upper()
        if company_up not in prev_companies:
            pos["type"] = "buy"
            pos["unusual"] = True
            pos["unusual_reason"] = (pos.get("unusual_reason") or "") + " NEW POSITION"
        else:
            pos["type"] = "hold"

    return current


def generate_summary(trade):
    if not XAI_API_KEY:
        return None

    prompt = (
        f"{trade['manager']} ({trade['fund']}) holds ${trade.get('amount_raw','?')} "
        f"in {trade.get('ticker') or trade.get('company','?')} as of their latest 13F filing. "
        + ("This appears to be a NEW position. " if "NEW" in (trade.get("unusual_reason") or "") else "")
        + "Write 1-2 sentences in plain English about what this whale position signals to regular investors. "
        + "Be specific and direct."
    )

    try:
        payload = json.dumps({
            "model": "grok-3-mini",
            "max_tokens": 150,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.x.ai/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "content-type": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[AI] Whale summary error: {e}", file=sys.stderr)
        return None


def main():
    print("=== Fetching Whale 13F Holdings ===")

    all_positions = []

    for fund in WHALE_FUNDS:
        print(f"\n[{fund['name']}] Looking up latest 13F...")
        latest = get_latest_13f(fund["cik"])
        if not latest:
            print(f"  No 13F found for {fund['name']}")
            continue

        print(f"  Found: {latest['form']} filed {latest['date']}")
        positions = fetch_13f_holdings(fund["cik"], latest["accession"], fund)
        positions = resolve_cusip_to_ticker(positions)

        # Set filed date
        for p in positions:
            p["filed_date"] = latest["date"]
            p["trade_date"] = latest["date"]

        print(f"  Positions: {len(positions)}")
        all_positions.extend(positions)
        time.sleep(0.5)  # Be polite to EDGAR

    # Sort by value
    all_positions.sort(key=lambda p: p["amount_max"], reverse=True)
    print(f"\nTotal positions: {len(all_positions)}")

    # Generate AI summaries for unusual / new positions
    if XAI_API_KEY:
        print("Generating AI summaries for notable positions...")
        for pos in all_positions:
            if pos.get("unusual") and not pos.get("summary"):
                pos["summary"] = generate_summary(pos)
                time.sleep(0.3)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source":       "SEC EDGAR Form 13F",
        "funds_tracked": [f["name"] for f in WHALE_FUNDS],
        "count":        len(all_positions),
        "trades":       all_positions,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"✓ Saved {len(all_positions)} whale positions → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
