"""
fetch_insiders.py
Fetches large insider trades from OpenInsider (SEC Form 4 aggregator).
No company whitelist. Filters by minimum dollar value. Sorted by size.
"""

import json, os, time, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request
import urllib.parse

OUTPUT_FILE     = Path(__file__).parent.parent / "data" / "insider_trades.json"
XAI_API_KEY     = os.environ.get("XAI_API_KEY", "")
HEADERS         = {"User-Agent": "BullTracker Personal Research veritasvoid2025@gmail.com"}
MIN_VALUE       = 1_000_000   # $1M minimum — no noise
FLAG_VALUE      = 5_000_000   # $5M+ = unusual
DAYS_BACK       = 60


def fetch_openinsider():
    """
    Scrape OpenInsider screener — aggregates all SEC Form 4 filings.
    Filters: last 60 days, $1M+ trade value, buys AND sells.
    """
    trades = []

    # Two calls: one for buys (xp=1), one for sells (xs=1)
    for tx_flag, tx_type in [("xp=1&xs=0", "buy"), ("xp=0&xs=1", "sell")]:
        url = (
            f"https://openinsider.com/screener?"
            f"s=&o=&pl={MIN_VALUE}&ph=&ll=&lh="
            f"&fd={DAYS_BACK}&fdr=&td=0&tdr="
            f"&fdlyl=&fdlyh=&daysago="
            f"&{tx_flag}"
            f"&vl={MIN_VALUE}&vh="
            f"&ocl=&och=&sic1=-1&sicl=100&sich=9999"
            f"&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&ov="
            f"&tc=1&cnt=100&page=1"
        )

        try:
            req = urllib.request.Request(url, headers={
                **HEADERS,
                "Accept": "text/html",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", errors="replace")

            parsed = parse_openinsider_table(html, tx_type)
            print(f"[OpenInsider] {tx_type.upper()}S found: {len(parsed)}")
            trades.extend(parsed)
            time.sleep(1)

        except Exception as e:
            print(f"[OpenInsider] {tx_type} error: {e}", file=sys.stderr)

    return trades


def parse_openinsider_table(html, tx_type):
    """Parse OpenInsider HTML table rows into trade dicts."""
    trades = []

    # Find table body rows
    tbody = re.search(r'<tbody>(.*?)</tbody>', html, re.DOTALL | re.IGNORECASE)
    if not tbody:
        return trades

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tbody.group(1), re.DOTALL | re.IGNORECASE)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        # OpenInsider columns:
        # 0:X 1:Filing Date 2:Trade Date 3:Ticker 4:Company
        # 5:Insider Name 6:Title 7:Trade Type 8:Price 9:Qty
        # 10:Owned 11:Delta Own 12:Value
        if len(cells) < 13:
            continue

        try:
            filed_date = cells[1].strip()[:10] if cells[1] else None
            trade_date = cells[2].strip()[:10] if cells[2] else None
            ticker     = cells[3].strip().upper()
            company    = cells[4].strip()
            insider    = cells[5].strip()
            title      = cells[6].strip()
            price_s    = re.sub(r'[^\d.]', '', cells[8])
            qty_s      = re.sub(r'[^\d]',  '', cells[9])
            value_s    = re.sub(r'[^\d]',  '', cells[12])

            price = float(price_s) if price_s else 0
            qty   = int(qty_s)     if qty_s   else 0
            value = int(value_s)   if value_s else int(price * qty)

            if value < MIN_VALUE:
                continue

            unusual = value >= FLAG_VALUE
            delay   = compute_delay(trade_date, filed_date)

            trades.append({
                "id":              f"oi_{ticker}_{insider}_{trade_date}_{tx_type}",
                "name":            insider or "Unknown",
                "role":            title or "Corporate Insider",
                "ticker":          ticker or None,
                "company":         company,
                "type":            tx_type,
                "shares":          qty if qty else None,
                "price_per_share": round(price, 2) if price else None,
                "amount_min":      value,
                "amount_max":      value,
                "amount_raw":      f"${value:,}",
                "trade_date":      trade_date,
                "filed_date":      filed_date,
                "delay_days":      delay,
                "sector":          None,
                "source":          "OpenInsider / Form 4",
                "unusual":         unusual,
                "unusual_reason":  f"${value/1e6:.1f}M trade" if unusual else None,
                "summary":         None,
            })

        except Exception:
            continue

    return trades


def flag_clusters(trades):
    """Flag 2+ insiders buying same ticker in the window."""
    from collections import defaultdict
    groups = defaultdict(list)
    for t in trades:
        if t["ticker"] and t["type"] == "buy":
            groups[t["ticker"]].append(t)
    for ticker, group in groups.items():
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


def compute_delay(t, f):
    try:
        return max(0, (
            datetime.strptime(f[:10], "%Y-%m-%d") -
            datetime.strptime(t[:10], "%Y-%m-%d")
        ).days)
    except Exception:
        return 0


def generate_summary(trade):
    if not XAI_API_KEY:
        return None
    prompt = (
        f"{trade['name']} ({trade.get('role','insider')}) at {trade.get('company', trade.get('ticker','?'))} "
        f"{'purchased' if trade['type']=='buy' else 'sold'} {trade.get('shares','?'):,} shares "
        f"of {trade['ticker']} at ${trade.get('price_per_share','?')} "
        f"(total {trade.get('amount_raw','?')}) on {trade.get('trade_date','')}. "
        f"1-2 sentences plain English for a regular investor. Direct, no hedging."
    )
    try:
        payload = json.dumps({
            "model": "grok-3-mini",
            "max_tokens": 120,
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
        print(f"[AI] {e}", file=sys.stderr)
        return None


def main():
    print(f"=== Insider Trades — ${MIN_VALUE/1e6:.0f}M+ | Last {DAYS_BACK} days ===")

    trades = fetch_openinsider()
    trades = dedup(trades)
    flag_clusters(trades)

    # Sort biggest trades first
    trades.sort(key=lambda t: t.get("amount_max") or 0, reverse=True)
    print(f"Total trades: {len(trades)}")

    if XAI_API_KEY:
        print("Generating summaries for top 30...")
        for t in trades[:30]:
            if not t.get("summary"):
                t["summary"] = generate_summary(t)
                time.sleep(0.3)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "last_updated":   datetime.now(timezone.utc).isoformat(),
        "source":         "OpenInsider / SEC Form 4",
        "min_value":      MIN_VALUE,
        "flag_threshold": FLAG_VALUE,
        "days_back":      DAYS_BACK,
        "count":          len(trades),
        "trades":         trades,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"✓ Saved {len(trades)} trades → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
