"""
fetch_insiders.py
Fetches ALL recent Form 4 insider trades from SEC EDGAR.
No company whitelist — captures everything, filters by trade value.
Flags large trades ($1M+), cluster buys, and C-suite moves.
"""

import json, os, time, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request
import urllib.parse

OUTPUT_FILE  = Path(__file__).parent.parent / "data" / "insider_trades.json"
XAI_API_KEY  = os.environ.get("XAI_API_KEY", "")
HEADERS      = {"User-Agent": "BullTracker Personal Research veritasvoid2025@gmail.com"}

# Minimum trade value to include (filters out tiny token purchases)
MIN_TRADE_VALUE  = 100_000    # $100K minimum
FLAG_THRESHOLD   = 1_000_000  # $1M+ = unusual flag
DAYS_BACK        = 30         # Rolling 30-day window


# ── QUIVER QUANT — PRIMARY SOURCE ─────────────────────────────────────────────

def fetch_quiver_insiders():
    """
    Quiver Quant aggregates Form 4 data — works from GitHub Actions.
    Returns all trades, filtered by value threshold.
    """
    trades = []
    url = "https://api.quiverquant.com/beta/live/insiders"

    try:
        req = urllib.request.Request(url, headers={**HEADERS, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())

        print(f"[QuiverQuant] Raw records: {len(data)}")
        cutoff = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")

        for item in (data if isinstance(data, list) else []):
            tx_date = (item.get("Date") or "")[:10]
            if tx_date < cutoff:
                continue

            shares = float(item.get("Shares") or 0)
            price  = float(item.get("Price")  or 0)
            total  = shares * price

            if total < MIN_TRADE_VALUE:
                continue

            tx_raw   = (item.get("TransactionType") or "").lower()
            tx_type  = "buy" if any(x in tx_raw for x in ["purchase", "p -", "buy"]) else "sell"
            ticker   = (item.get("Ticker") or "").upper().strip()
            unusual  = total >= FLAG_THRESHOLD

            trades.append({
                "id":              f"qv_{ticker}_{item.get('Name','')}_{tx_date}_{tx_type}",
                "name":            item.get("Name") or "Unknown",
                "role":            item.get("Title") or "Corporate Insider",
                "ticker":          ticker or None,
                "company":         item.get("Company") or "",
                "type":            tx_type,
                "shares":          int(shares) if shares else None,
                "price_per_share": round(price, 2) if price else None,
                "amount_min":      total,
                "amount_max":      total,
                "amount_raw":      f"${total:,.0f}",
                "trade_date":      tx_date,
                "filed_date":      tx_date,
                "delay_days":      0,
                "sector":          None,
                "source":          "QuiverQuant / Form 4",
                "unusual":         unusual,
                "unusual_reason":  f"${total/1e6:.1f}M trade" if unusual else None,
                "summary":         None,
            })

    except Exception as e:
        print(f"[QuiverQuant] Error: {e}", file=sys.stderr)

    return trades


# ── EDGAR FULL-TEXT SEARCH — SECONDARY SOURCE ─────────────────────────────────

def fetch_edgar_form4():
    """
    Fetch Form 4 metadata from EDGAR full-text search.
    Parses XML only for trades above value threshold.
    """
    trades = []
    from_date = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?forms=4&dateRange=custom&startdt={from_date}"
        f"&enddt={datetime.now().strftime('%Y-%m-%d')}"
        "&hits.hits.total.value=true&hits.hits._source=period_of_report,entity_name,file_date"
        "&hits.hits.total=200"
    )

    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())

        hits = data.get("hits", {}).get("hits", [])
        print(f"[EDGAR] Form 4 hits: {len(hits)}")

        for hit in hits[:150]:
            src       = hit.get("_source", {})
            accession = (hit.get("_id") or "").replace("-", "")
            if not accession:
                continue

            cik_match = re.search(r'/data/(\d+)/', hit.get("_index", "") + accession)
            result = parse_form4(accession, src)
            trades.extend(result)
            time.sleep(0.11)  # SEC rate limit: max 10 req/sec

    except Exception as e:
        print(f"[EDGAR] Error: {e}", file=sys.stderr)

    return trades


def parse_form4(accession, src):
    """Fetch and parse a single Form 4 XML from EDGAR Archives."""
    trades = []
    if len(accession) < 18:
        return trades

    cik      = accession[:10].lstrip("0")
    idx_url  = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{accession}-index.htm"

    try:
        req = urllib.request.Request(idx_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")

        xml_files = re.findall(r'href="(/Archives/[^"]+\.xml)"', html, re.IGNORECASE)
        if not xml_files:
            return trades

        xml_url = f"https://www.sec.gov{xml_files[0]}"
        req2    = urllib.request.Request(xml_url, headers=HEADERS)
        with urllib.request.urlopen(req2, timeout=10) as r2:
            xml = r2.read().decode("utf-8", errors="replace")

        trades = extract_transactions(xml, src)

    except Exception:
        pass

    return trades


def extract_transactions(xml, src):
    """Parse Form 4 XML — extract all non-derivative transactions."""
    trades = []

    def tag(t, content):
        m = re.search(rf'<{t}[^>]*>(.*?)</{t}>', content, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    ticker      = tag("issuerTradingSymbol", xml).upper()
    issuer_name = tag("issuerName", xml)
    filer_name  = tag("rptOwnerName", xml) or tag("reportingOwnerName", xml)
    role        = tag("officerTitle", xml) or "Corporate Insider"
    file_date   = src.get("file_date", "")

    tx_blocks = re.findall(
        r'<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>',
        xml, re.DOTALL | re.IGNORECASE
    )

    for block in tx_blocks:
        tx_code  = tag("transactionCode", block)
        if tx_code not in ("P", "S"):
            continue

        shares_s = tag("transactionShares", block)
        price_s  = tag("transactionPricePerShare", block)
        tx_date  = tag("transactionDate", block)

        try:
            shares = float(re.sub(r'[^\d.]', '', shares_s)) if shares_s else 0
            price  = float(re.sub(r'[^\d.]', '', price_s))  if price_s  else 0
            total  = shares * price
        except Exception:
            total = 0

        if total < MIN_TRADE_VALUE:
            continue

        tx_type = "buy" if tx_code == "P" else "sell"
        unusual = total >= FLAG_THRESHOLD

        trades.append({
            "id":              f"f4_{ticker}_{filer_name}_{tx_date}_{tx_code}",
            "name":            filer_name or "Unknown",
            "role":            role,
            "ticker":          ticker or None,
            "company":         issuer_name,
            "type":            tx_type,
            "shares":          int(shares) if shares else None,
            "price_per_share": round(price, 2) if price else None,
            "amount_min":      total,
            "amount_max":      total,
            "amount_raw":      f"${total:,.0f}",
            "trade_date":      tx_date[:10] if tx_date else None,
            "filed_date":      file_date[:10] if file_date else None,
            "delay_days":      compute_delay(tx_date, file_date),
            "sector":          None,
            "source":          "SEC EDGAR Form 4",
            "unusual":         unusual,
            "unusual_reason":  f"${total/1e6:.1f}M trade" if unusual else None,
            "summary":         None,
        })

    return trades


# ── HELPERS ───────────────────────────────────────────────────────────────────

def compute_delay(t, f):
    try:
        return max(0, (
            datetime.strptime(f[:10], "%Y-%m-%d") -
            datetime.strptime(t[:10], "%Y-%m-%d")
        ).days)
    except Exception:
        return 0


def flag_clusters(trades):
    """Flag when 2+ insiders buy same ticker within the window."""
    from collections import defaultdict
    by_ticker = defaultdict(list)
    for t in trades:
        if t["ticker"] and t["type"] == "buy":
            by_ticker[t["ticker"]].append(t)
    for ticker, group in by_ticker.items():
        if len(group) >= 2:
            for t in group:
                t["unusual"] = True
                t["unusual_reason"] = f"Cluster: {len(group)} insiders buying {ticker}"


def dedup(trades):
    seen, out = set(), []
    for t in trades:
        k = f"{t['name']}_{t.get('ticker')}_{t.get('trade_date')}_{t.get('type')}"
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


# ── AI SUMMARIES ─────────────────────────────────────────────────────────────

def generate_summary(trade):
    if not XAI_API_KEY:
        return None
    prompt = (
        f"{trade['name']} ({trade.get('role','insider')}) at {trade.get('company', trade.get('ticker','?'))} "
        f"{'purchased' if trade['type']=='buy' else 'sold'} {trade.get('shares','?')} shares "
        f"of {trade['ticker']} at ${trade.get('price_per_share','?')} per share "
        f"(total ~{trade.get('amount_raw','undisclosed')}) on {trade.get('trade_date','')}. "
        f"Write 1-2 sentences in plain English explaining what this insider move means for regular investors. "
        f"Be direct and factual."
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
            headers={**HEADERS, "Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[AI] Error: {e}", file=sys.stderr)
        return None


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=== Fetching Insider Trades (Form 4) — ALL companies ===")
    print(f"    Min value: ${MIN_TRADE_VALUE:,}  |  Flag threshold: ${FLAG_THRESHOLD:,}  |  Window: {DAYS_BACK} days")

    trades = []

    # Primary: QuiverQuant (reliable from GitHub Actions)
    t0 = fetch_quiver_insiders()
    print(f"[QuiverQuant] Above threshold: {len(t0)}")
    trades.extend(t0)

    # Secondary: EDGAR direct (if QuiverQuant is sparse)
    if len(trades) < 10:
        print("Supplementing with EDGAR...")
        t1 = fetch_edgar_form4()
        print(f"[EDGAR] Above threshold: {len(t1)}")
        trades.extend(t1)

    trades = dedup(trades)
    flag_clusters(trades)

    # Sort by trade value descending — biggest moves first
    trades.sort(key=lambda t: t.get("amount_max") or 0, reverse=True)
    print(f"Total trades above ${MIN_TRADE_VALUE:,}: {len(trades)}")

    if XAI_API_KEY:
        print("Generating AI summaries for top trades...")
        for t in trades[:30]:
            if not t.get("summary"):
                t["summary"] = generate_summary(t)
                time.sleep(0.3)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "last_updated":    datetime.now(timezone.utc).isoformat(),
        "source":          "QuiverQuant / SEC EDGAR Form 4",
        "min_trade_value": MIN_TRADE_VALUE,
        "flag_threshold":  FLAG_THRESHOLD,
        "days_back":       DAYS_BACK,
        "count":           len(trades),
        "trades":          trades,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"✓ Saved {len(trades)} insider trades → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
