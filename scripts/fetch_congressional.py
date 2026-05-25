"""
fetch_congressional.py
Fetches congressional trades from Senate eFDS and House disclosures,
filters for watchlist members, generates AI summaries via Anthropic API.
"""

import json, os, time, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request
import urllib.parse

# ── CONFIG ────────────────────────────────────────────────────────────────────

WATCHLIST = [
    # Pelosi family
    {"name": "Nancy Pelosi",  "chamber": "House", "party": "D", "state": "CA"},
    {"name": "Paul Pelosi",   "chamber": "Spouse","party": "D", "state": "CA"},
    # Trump family
    {"name": "Donald Trump",  "chamber": "Executive", "party": "R", "state": "FL"},
    # Top outperformers (based on public congressional trading data)
    {"name": "Dan Crenshaw",          "chamber": "House",  "party": "R", "state": "TX"},
    {"name": "Michael McCaul",        "chamber": "House",  "party": "R", "state": "TX"},
    {"name": "Josh Gottheimer",       "chamber": "House",  "party": "D", "state": "NJ"},
    {"name": "Ro Khanna",             "chamber": "House",  "party": "D", "state": "CA"},
    {"name": "Brian Mast",            "chamber": "House",  "party": "R", "state": "FL"},
    {"name": "Marjorie Taylor Greene","chamber": "House",  "party": "R", "state": "GA"},
    {"name": "Susie Lee",             "chamber": "House",  "party": "D", "state": "NV"},
    {"name": "David Rouzer",          "chamber": "House",  "party": "R", "state": "NC"},
    {"name": "Tommy Tuberville",      "chamber": "Senate", "party": "R", "state": "AL"},
    {"name": "Sheldon Whitehouse",    "chamber": "Senate", "party": "D", "state": "RI"},
]

WATCHLIST_NAMES = {m["name"].upper() for m in WATCHLIST}
WATCHLIST_LAST  = {m["name"].split()[-1].upper(): m for m in WATCHLIST}

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "congressional_trades.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── SENATE eFDS ───────────────────────────────────────────────────────────────

def fetch_senate_trades(days_back=90):
    """Fetch PTR (Periodic Transaction Reports) from Senate eFDS."""
    trades = []
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")

    url = (
        "https://efts.senate.gov/LATEST/search-results"
        f"?query=&dateRange=custom&fromDate={from_date}&toDate={to_date}"
        "&type=ptr&pageSize=100"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BullTracker/1.0 research"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())

        results = data.get("results", [])
        print(f"[Senate] Got {len(results)} raw results")

        for item in results:
            filer = item.get("filerName", "") or ""
            last  = filer.split(",")[0].strip().upper() if "," in filer else filer.split()[-1].upper()

            meta = WATCHLIST_LAST.get(last)
            if not meta:
                continue  # not on watchlist

            transactions = item.get("transactions", [])
            if not transactions:
                # Try to parse from reportLine
                transactions = parse_senate_report_line(item)

            for tx in transactions:
                trade = build_congress_trade(tx, meta, item, source="senate")
                if trade:
                    trades.append(trade)

    except Exception as e:
        print(f"[Senate] Error: {e}", file=sys.stderr)

    return trades


def parse_senate_report_line(item):
    """Best-effort parse when transaction detail is absent."""
    return []  # Senate API sometimes returns summary only; PDF parse needed


# ── HOUSE DISCLOSURES ─────────────────────────────────────────────────────────

def fetch_house_trades(days_back=90):
    """Fetch PTR data from House disclosures JSON endpoint."""
    trades = []

    # House provides a Zip of PDF filings — we use their search endpoint instead
    url = "https://disclosures.house.gov/FinancialDisclosure/ViewMemberSearchResult"
    params = urllib.parse.urlencode({
        "LastName": "",
        "FirstName": "",
        "FilingYear": datetime.now().year,
        "State": "",
        "District": "",
        "FilingType": "P",  # P = Periodic Transaction Report
    })

    try:
        req = urllib.request.Request(
            f"{url}?{params}",
            headers={"User-Agent": "BullTracker/1.0 research"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", errors="replace")

        # Parse HTML table rows
        rows = re.findall(
            r'<tr[^>]*>.*?</tr>',
            raw,
            re.DOTALL | re.IGNORECASE
        )

        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            if len(cells) < 3:
                continue

            name_raw = cells[0].strip()
            last     = name_raw.split(",")[0].strip().upper() if "," in name_raw else name_raw.split()[-1].upper()
            meta     = WATCHLIST_LAST.get(last)
            if not meta:
                continue

            # Extract PDF link for deep parse
            pdf_match = re.search(r'href="(/[^"]+\.pdf)"', row, re.IGNORECASE)
            pdf_url   = f"https://disclosures.house.gov{pdf_match.group(1)}" if pdf_match else None

            trade = {
                "name":        meta["name"],
                "chamber":     meta["chamber"],
                "party":       meta["party"],
                "state":       meta["state"],
                "type":        "unknown",
                "ticker":      "—",
                "company":     "See PDF filing",
                "amount_min":  None,
                "amount_max":  None,
                "trade_date":  None,
                "filed_date":  cells[2] if len(cells) > 2 else None,
                "delay_days":  None,
                "sector":      None,
                "source":      "house",
                "pdf_url":     pdf_url,
                "unusual":     False,
                "summary":     f"House PTR filing — see PDF for transaction details: {pdf_url or 'N/A'}",
            }
            trades.append(trade)

    except Exception as e:
        print(f"[House] Error: {e}", file=sys.stderr)

    return trades


# ── QUIVER QUANT (fallback / supplement) ─────────────────────────────────────

def fetch_quiver_congress():
    """
    Quiver Quantitative provides aggregated congressional trading data.
    Free tier: recent trades available at beta endpoint.
    """
    trades = []
    url = "https://api.quiverquant.com/beta/live/congresstrading"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "BullTracker/1.0 research",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())

        print(f"[QuiverQuant] Got {len(data)} records")

        for item in data:
            name = item.get("Representative", "")
            last = name.split()[-1].upper() if name else ""
            meta = WATCHLIST_LAST.get(last)
            if not meta:
                continue

            t_date = item.get("TransactionDate", "") or ""
            f_date = item.get("DisclosureDate", "") or ""

            delay = compute_delay(t_date, f_date)

            trade_type_raw = (item.get("Transaction", "") or "").lower()
            trade_type = "buy" if "purchase" in trade_type_raw or "buy" in trade_type_raw else "sell"

            amount_range = item.get("Range", "$1,001 - $15,000")
            min_v, max_v = parse_amount_range(amount_range)

            trade = {
                "id":           f"qv_{name}_{item.get('Ticker','')}_{t_date}",
                "name":         meta["name"],
                "chamber":      meta["chamber"],
                "party":        meta["party"],
                "state":        meta["state"],
                "role":         "Congress Member",
                "ticker":       (item.get("Ticker") or "").upper() or None,
                "company":      item.get("Company") or "",
                "type":         trade_type,
                "amount_min":   min_v,
                "amount_max":   max_v,
                "amount_raw":   amount_range,
                "trade_date":   t_date,
                "filed_date":   f_date,
                "delay_days":   delay,
                "sector":       item.get("Sector") or None,
                "source":       "quiverquant",
                "unusual":      False,
                "summary":      None,
            }

            trades.append(trade)

    except Exception as e:
        print(f"[QuiverQuant] Error: {e}", file=sys.stderr)

    return trades


# ── HELPERS ───────────────────────────────────────────────────────────────────

def build_congress_trade(tx, meta, item, source):
    t_date = tx.get("transactionDate") or item.get("dateOfTransaction") or ""
    f_date = item.get("dateReceivedOrFiled") or ""
    ticker = (tx.get("ticker") or tx.get("assetName") or "").upper().strip()
    ticker = ticker if re.match(r'^[A-Z]{1,5}$', ticker) else None

    trade_type_raw = (tx.get("type") or tx.get("transactionType") or "").lower()
    trade_type = "buy" if any(x in trade_type_raw for x in ["purchase", "buy", "acquisit"]) else "sell"

    amount_raw = tx.get("amount") or ""
    min_v, max_v = parse_amount_range(amount_raw)
    delay = compute_delay(t_date, f_date)

    return {
        "id":          f"senate_{meta['name'].replace(' ','_')}_{ticker}_{t_date}",
        "name":        meta["name"],
        "chamber":     meta["chamber"],
        "party":       meta["party"],
        "state":       meta["state"],
        "role":        "Senator" if meta["chamber"] == "Senate" else "Representative",
        "ticker":      ticker,
        "company":     tx.get("assetName") or "",
        "type":        trade_type,
        "amount_min":  min_v,
        "amount_max":  max_v,
        "amount_raw":  amount_raw,
        "trade_date":  t_date,
        "filed_date":  f_date,
        "delay_days":  delay,
        "sector":      None,
        "source":      source,
        "unusual":     delay > 40 or (max_v and max_v >= 1_000_000),
        "unusual_reason": "Filed near 45-day deadline" if delay > 40 else ("$1M+ trade" if max_v and max_v >= 1_000_000 else None),
        "summary":     None,
    }


def parse_amount_range(raw):
    """Parse '$1,001 - $15,000' → (1001, 15000)"""
    nums = re.findall(r'[\d,]+', str(raw))
    nums = [int(n.replace(",", "")) for n in nums if n.replace(",", "").isdigit()]
    if len(nums) >= 2: return nums[0], nums[1]
    if len(nums) == 1: return nums[0], nums[0]
    return None, None


def compute_delay(trade_date_str, filed_date_str):
    try:
        td = datetime.strptime(trade_date_str[:10], "%Y-%m-%d")
        fd = datetime.strptime(filed_date_str[:10], "%Y-%m-%d")
        return max(0, (fd - td).days)
    except Exception:
        return 0


def dedup(trades):
    seen, out = set(), []
    for t in trades:
        k = f"{t['name']}_{t.get('ticker')}_{t.get('trade_date')}_{t.get('type')}"
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


# ── AI SUMMARIES ──────────────────────────────────────────────────────────────

def generate_summary(trade):
    if not ANTHROPIC_API_KEY:
        return None
    if not trade.get("ticker") or trade["ticker"] == "—":
        return None

    prompt = (
        f"{trade['name']} ({trade.get('party','')}, {trade.get('chamber','')}) "
        f"{'purchased' if trade['type']=='buy' else 'sold'} "
        f"{trade.get('amount_raw') or 'an undisclosed amount of'} in {trade['ticker']} "
        f"({trade.get('company','')}) on {trade.get('trade_date','')}, "
        f"filed {trade.get('delay_days',0)} days later. "
        f"Write a 1-2 sentence plain-English summary of what this means for a regular investor. "
        f"Be factual, concise, and mention any notable context like the politician's committee assignments "
        f"or recent market activity if relevant. No hedging language."
    )

    try:
        payload = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 150,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
        return resp["content"][0]["text"].strip()
    except Exception as e:
        print(f"[AI] Summary error for {trade['name']}: {e}", file=sys.stderr)
        return None


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Fetching Congressional Trades ===")

    all_trades = []

    # 1. Try QuiverQuant (best aggregated source)
    qv = fetch_quiver_congress()
    print(f"[QuiverQuant] Watchlist hits: {len(qv)}")
    all_trades.extend(qv)

    # 2. Senate eFDS (raw source)
    senate = fetch_senate_trades()
    print(f"[Senate] Watchlist hits: {len(senate)}")
    all_trades.extend(senate)

    # 3. House (limited without PDF parse)
    house = fetch_house_trades()
    print(f"[House] Watchlist hits: {len(house)}")
    all_trades.extend(house)

    # Dedup + sort newest first
    trades = dedup(all_trades)
    trades.sort(key=lambda t: t.get("filed_date") or "", reverse=True)
    print(f"Total unique trades: {len(trades)}")

    # Generate AI summaries
    if ANTHROPIC_API_KEY:
        print("Generating AI summaries...")
        for i, t in enumerate(trades[:50]):  # summarize top 50
            if not t.get("summary"):
                t["summary"] = generate_summary(t)
                time.sleep(0.3)  # rate limit
    else:
        print("[AI] No API key — skipping summaries")

    # Write output
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source": "SEC EDGAR / Senate eFDS / House Disclosures / QuiverQuant",
        "watchlist": [m["name"] for m in WATCHLIST],
        "count": len(trades),
        "trades": trades
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"✓ Saved {len(trades)} trades → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
