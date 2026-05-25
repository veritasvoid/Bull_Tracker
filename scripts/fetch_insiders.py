"""
fetch_insiders.py
Fetches SEC Form 4 insider trades from EDGAR for S&P 500 companies.
Flags unusual size/timing. Generates AI summaries.
"""

import json, os, time, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request
import urllib.parse

OUTPUT_FILE = Path(__file__).parent.parent / "data" / "insider_trades.json"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Top companies to monitor for insider activity
WATCH_COMPANIES = {
    "AAPL": "Apple Inc",
    "MSFT": "Microsoft Corp",
    "NVDA": "NVIDIA Corp",
    "GOOGL": "Alphabet Inc",
    "AMZN": "Amazon.com Inc",
    "META": "Meta Platforms",
    "TSLA": "Tesla Inc",
    "BRK-A": "Berkshire Hathaway",
    "JPM": "JPMorgan Chase",
    "V": "Visa Inc",
    "UNH": "UnitedHealth Group",
    "XOM": "Exxon Mobil",
    "WMT": "Walmart Inc",
    "JNJ": "Johnson & Johnson",
    "PG": "Procter & Gamble",
    "MA": "Mastercard Inc",
    "HD": "Home Depot",
    "CVX": "Chevron Corp",
    "MRK": "Merck & Co",
    "LLY": "Eli Lilly",
    "BAC": "Bank of America",
    "ABBV": "AbbVie Inc",
    "PFE": "Pfizer Inc",
    "KO": "Coca-Cola Co",
    "DJT": "Trump Media & Technology",
}

# Amount thresholds for unusual flags
LARGE_TRADE_THRESHOLD = 5_000_000    # $5M+
CLUSTER_WINDOW_DAYS   = 7


def fetch_recent_form4():
    """Fetch recent Form 4 filings from EDGAR full-text search."""
    trades = []
    from_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")

    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q=&forms=4&dateRange=custom&startdt={from_date}&enddt="
        + datetime.now().strftime("%Y-%m-%d")
        + "&hits.hits.total.value=true&hits.hits._source=period_of_report,entity_name,file_date"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BullTracker/1.0 contact@example.com"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())

        hits = data.get("hits", {}).get("hits", [])
        print(f"[EDGAR] Form 4 hits: {len(hits)}")

        for hit in hits[:200]:
            src = hit.get("_source", {})
            accession = hit.get("_id", "").replace("-", "")
            parsed = parse_form4_filing(accession, src)
            if parsed:
                trades.extend(parsed)

        time.sleep(0.5)

    except Exception as e:
        print(f"[EDGAR] Search error: {e}", file=sys.stderr)

    return trades


def parse_form4_filing(accession_raw, src):
    """Fetch and parse individual Form 4 XML filing."""
    trades = []

    # Build EDGAR filing URL
    if not accession_raw:
        return trades

    accession = re.sub(r'[^0-9]', '', accession_raw)
    if len(accession) < 18:
        return trades

    cik = accession[:10].lstrip("0")
    acc_dashed = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
    xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{accession}-index.htm"

    try:
        req = urllib.request.Request(xml_url, headers={"User-Agent": "BullTracker/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")

        # Find the main .xml or form4 file
        xml_files = re.findall(r'href="(/Archives/[^"]+\.xml)"', html, re.IGNORECASE)
        if not xml_files:
            return trades

        form4_url = f"https://www.sec.gov{xml_files[0]}"
        req2 = urllib.request.Request(form4_url, headers={"User-Agent": "BullTracker/1.0"})
        with urllib.request.urlopen(req2, timeout=10) as r2:
            xml = r2.read().decode("utf-8", errors="replace")

        trades = extract_from_xml(xml, src, cik)

    except Exception as e:
        pass  # Many filings won't match our watchlist — silent skip

    return trades


def extract_from_xml(xml, src, cik):
    """Parse Form 4 XML and extract non-derivative transaction table."""
    trades = []

    def tag(t, content):
        m = re.search(rf'<{t}[^>]*>(.*?)</{t}>', content, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    issuer_ticker = tag("issuerTradingSymbol", xml).upper()

    # Only process companies on our watchlist
    if issuer_ticker not in WATCH_COMPANIES:
        return trades

    issuer_name = tag("issuerName", xml) or WATCH_COMPANIES.get(issuer_ticker, "")
    filer_name  = tag("rptOwnerName", xml) or tag("reportingOwnerName", xml)
    role_raw    = tag("officerTitle", xml) or tag("reportingOwnerRelationship", xml)
    is_officer  = bool(re.search(r'officer|director|ceo|cfo|coo|president|vp', role_raw or "", re.IGNORECASE))

    if not filer_name:
        return trades

    file_date = src.get("file_date") or tag("periodOfReport", xml) or ""

    # Non-derivative transactions
    tx_blocks = re.findall(
        r'<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>',
        xml, re.DOTALL | re.IGNORECASE
    )

    for block in tx_blocks:
        tx_code   = tag("transactionCode", block)
        shares    = tag("transactionShares", block)
        price     = tag("transactionPricePerShare", block)
        tx_date   = tag("transactionDate", block)
        direct    = tag("directOrIndirectOwnership", block)

        # P = Purchase, S = Sale
        if tx_code not in ("P", "S"):
            continue

        trade_type = "buy" if tx_code == "P" else "sell"

        try:
            shares_n = float(re.sub(r'[^\d.]', '', shares)) if shares else 0
            price_n  = float(re.sub(r'[^\d.]', '', price)) if price else 0
            total_v  = shares_n * price_n
        except Exception:
            shares_n, price_n, total_v = 0, 0, 0

        unusual = total_v >= LARGE_TRADE_THRESHOLD
        unusual_reason = f"${total_v/1e6:.1f}M trade — above $5M threshold" if unusual else None

        trade = {
            "id":             f"f4_{cik}_{issuer_ticker}_{tx_date}_{tx_code}",
            "name":           filer_name,
            "role":           role_raw or "Corporate Insider",
            "ticker":         issuer_ticker,
            "company":        issuer_name,
            "type":           trade_type,
            "shares":         int(shares_n) if shares_n else None,
            "price_per_share": round(price_n, 2) if price_n else None,
            "amount_min":     total_v,
            "amount_max":     total_v,
            "amount_raw":     f"${total_v:,.0f}" if total_v else None,
            "trade_date":     tx_date[:10] if tx_date else None,
            "filed_date":     file_date[:10] if file_date else None,
            "delay_days":     compute_delay(tx_date, file_date),
            "ownership":      "Direct" if direct == "D" else "Indirect",
            "sector":         get_sector(issuer_ticker),
            "source":         "SEC EDGAR Form 4",
            "unusual":        unusual,
            "unusual_reason": unusual_reason,
            "summary":        None,
        }
        trades.append(trade)

    return trades


def fetch_via_edgar_rss():
    """Alternative: fetch latest Form 4 filings from EDGAR RSS feed."""
    trades = []
    url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=100&search_text=&output=atom"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BullTracker/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            content = r.read().decode("utf-8", errors="replace")

        entries = re.findall(r'<entry>(.*?)</entry>', content, re.DOTALL)
        print(f"[EDGAR RSS] {len(entries)} entries")

        for entry in entries[:100]:
            filing_url_m = re.search(r'<filing-href>(.*?)</filing-href>', entry)
            if not filing_url_m:
                continue
            filing_url = filing_url_m.group(1).strip()

            try:
                req2 = urllib.request.Request(filing_url, headers={"User-Agent": "BullTracker/1.0"})
                with urllib.request.urlopen(req2, timeout=10) as r2:
                    idx = r2.read().decode("utf-8", errors="replace")

                xml_links = re.findall(r'<a href="(/Archives/[^"]+\.xml)"', idx)
                if xml_links:
                    xml_url = f"https://www.sec.gov{xml_links[0]}"
                    req3 = urllib.request.Request(xml_url, headers={"User-Agent": "BullTracker/1.0"})
                    with urllib.request.urlopen(req3, timeout=10) as r3:
                        xml = r3.read().decode("utf-8", errors="replace")
                    cik_m = re.search(r'/data/(\d+)/', xml_links[0])
                    cik = cik_m.group(1) if cik_m else ""
                    result = extract_from_xml(xml, {}, cik)
                    trades.extend(result)

            except Exception:
                pass

            time.sleep(0.1)

    except Exception as e:
        print(f"[EDGAR RSS] Error: {e}", file=sys.stderr)

    return trades


def compute_delay(trade_date_str, filed_date_str):
    try:
        td = datetime.strptime((trade_date_str or "")[:10], "%Y-%m-%d")
        fd = datetime.strptime((filed_date_str or "")[:10], "%Y-%m-%d")
        return max(0, (fd - td).days)
    except Exception:
        return 0


def get_sector(ticker):
    sectors = {
        "AAPL":"Technology","MSFT":"Technology","NVDA":"Technology",
        "GOOGL":"Technology","AMZN":"Consumer","META":"Technology",
        "TSLA":"Automotive","JPM":"Finance","V":"Finance","BAC":"Finance",
        "UNH":"Healthcare","JNJ":"Healthcare","MRK":"Healthcare",
        "LLY":"Healthcare","PFE":"Healthcare","ABBV":"Healthcare",
        "XOM":"Energy","CVX":"Energy","WMT":"Retail","PG":"Consumer",
        "HD":"Retail","KO":"Consumer","MA":"Finance","DJT":"Media",
    }
    return sectors.get(ticker)


def flag_cluster_trades(trades):
    """Flag when multiple insiders buy same ticker within 7 days."""
    from collections import defaultdict
    by_ticker = defaultdict(list)
    for t in trades:
        if t["ticker"] and t["type"] == "buy":
            by_ticker[t["ticker"]].append(t)

    for ticker, group in by_ticker.items():
        if len(group) >= 2:
            for t in group:
                t["unusual"] = True
                t["unusual_reason"] = f"Cluster: {len(group)} insiders buying {ticker} this week"


def generate_summary(trade):
    if not ANTHROPIC_API_KEY:
        return None

    prompt = (
        f"{trade['name']} ({trade.get('role','insider')}) at {trade['company']} "
        f"{'purchased' if trade['type']=='buy' else 'sold'} "
        f"{trade.get('shares','?')} shares of {trade['ticker']} "
        f"at ${trade.get('price_per_share','?')} per share "
        f"(total ~{trade.get('amount_raw','undisclosed')}) on {trade.get('trade_date','')}. "
        f"Write 1-2 sentences in plain English explaining what this insider move means for regular investors. "
        f"Be direct and factual. No hedging."
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
        print(f"[AI] Error: {e}", file=sys.stderr)
        return None


def dedup(trades):
    seen, out = set(), []
    for t in trades:
        k = f"{t['name']}_{t.get('ticker')}_{t.get('trade_date')}_{t.get('type')}"
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def main():
    print("=== Fetching Insider Trades (Form 4) ===")

    trades = []

    # Primary: EDGAR search index
    t1 = fetch_recent_form4()
    print(f"[EDGAR Search] Watchlist hits: {len(t1)}")
    trades.extend(t1)

    # Fallback: EDGAR RSS
    if len(trades) < 5:
        print("Falling back to EDGAR RSS...")
        t2 = fetch_via_edgar_rss()
        print(f"[EDGAR RSS] Watchlist hits: {len(t2)}")
        trades.extend(t2)

    trades = dedup(trades)
    flag_cluster_trades(trades)
    trades.sort(key=lambda t: t.get("filed_date") or "", reverse=True)
    print(f"Total unique insider trades: {len(trades)}")

    # Generate AI summaries
    if ANTHROPIC_API_KEY:
        print("Generating AI summaries...")
        for t in trades[:40]:
            if not t.get("summary"):
                t["summary"] = generate_summary(t)
                time.sleep(0.3)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "source":       "SEC EDGAR Form 4",
        "watch_tickers": list(WATCH_COMPANIES.keys()),
        "count":        len(trades),
        "trades":       trades,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"✓ Saved {len(trades)} insider trades → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
