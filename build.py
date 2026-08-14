#!/usr/bin/env python3
"""
Market Pulse AI — data refresh.

Reads index.html, replaces ONLY the `const DATA = {...};` block with freshly
fetched values, writes index.html back. All HTML/CSS/JS structure is preserved
by construction: nothing outside that one block is touched.

Robustness contract:
  * Every source is fetched independently. A failure never blanks a panel —
    the previous run's value is reused and the failure is reported.
  * Percentage signs are only ever taken verbatim from signed JSON fields.
    If a direction cannot be confirmed, the value is set to null and the
    dashboard renders "—". Signs are never inferred or guessed.
"""

import json
import re
import sys
import html as htmllib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

INDEX = "index.html"
UA = {"User-Agent": "market-pulse-refresh/1.0 (+github-actions)"}
TIMEOUT = 45

# Feeds are tried in order; the first that parses wins.
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://cryptobriefing.com/feed/",
    "https://decrypt.co/feed",
]

problems = []


def get(url, tries=3):
    """Fetch a URL with retries. Transparently handles gzip."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:  # noqa: BLE001 - report, then retry
            last = e
    raise last


def get_json(url, tries=3):
    return json.loads(get(url, tries).decode("utf-8", "replace"))


def num(v):
    """Coerce to float, or None. None means 'unknown' -> renders as em dash."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # reject NaN


# ---------------------------------------------------------------- previous
def load_previous(src):
    """
    Parse the existing DATA block so failed sources can fall back to it.
    Run 1 sees a JS object literal with unquoted keys and will fail here;
    from run 2 onward DATA is emitted as strict JSON and parses cleanly.
    """
    m = re.search(r"const DATA = (\{.*?\n\});", src, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def keep(prev, key, label):
    """Reuse a previous value and record why."""
    problems.append(label)
    return prev.get(key)


# ---------------------------------------------------------------- sources
def fetch_global_and_coins(prev):
    """Global stats, top-15 coins, gainers, ETH dominance. Shares one fetch."""
    out = {}
    try:
        g = get_json("https://api.coinpaprika.com/v1/global")
        total_mcap = num(g.get("market_cap_usd"))
        out["global"] = {
            "marketCap": total_mcap,
            "marketCapChange24h": num(g.get("market_cap_change_24h")),
            "volume24h": num(g.get("volume_24h_usd")),
            "btcDominance": round(num(g.get("bitcoin_dominance_percentage")) or 0, 1),
            "ethDominance": None,
        }
    except Exception as e:  # noqa: BLE001
        problems.append(f"global stats ({type(e).__name__})")
        out["global"] = prev.get("global", {})
        total_mcap = num(out["global"].get("marketCap"))

    try:
        tickers = get_json("https://api.coinpaprika.com/v1/tickers?quotes=USD")
    except Exception as e:  # noqa: BLE001
        problems.append(f"coin list ({type(e).__name__})")
        out["coins"] = prev.get("coins", [])
        out["gainers"] = prev.get("gainers", [])
        return out, []

    def usd(t, field):
        return num((t.get("quotes") or {}).get("USD", {}).get(field))

    ranked = [t for t in tickers if isinstance(t.get("rank"), int) and t["rank"] > 0]
    ranked.sort(key=lambda t: t["rank"])

    out["coins"] = [
        {
            "rank": t["rank"],
            "name": t.get("name"),
            "sym": t.get("symbol"),
            "price": usd(t, "price"),
            "h1": usd(t, "percent_change_1h"),
            "h24": usd(t, "percent_change_24h"),
            "d7": usd(t, "percent_change_7d"),
            "mcap": usd(t, "market_cap"),
        }
        for t in ranked[:15]
    ]

    # ETH dominance from the same snapshot, so numerator and denominator agree.
    eth = next((t for t in ranked if (t.get("symbol") or "").upper() == "ETH"), None)
    if eth and total_mcap:
        eth_mcap = usd(eth, "market_cap")
        if eth_mcap:
            out["global"]["ethDominance"] = round(eth_mcap / total_mcap * 100, 1)
    if out["global"].get("ethDominance") is None:
        out["global"]["ethDominance"] = (prev.get("global") or {}).get("ethDominance")

    top500 = [t for t in ranked if t["rank"] <= 500 and usd(t, "percent_change_24h") is not None]
    top500.sort(key=lambda t: usd(t, "percent_change_24h"), reverse=True)
    out["gainers"] = [
        {"name": t.get("name"), "sym": t.get("symbol"), "change": usd(t, "percent_change_24h")}
        for t in top500[:10]
    ]
    return out, ranked


def fetch_trending(prev, ranked):
    """
    Trending names from CoinGecko. The 24h change is taken from CoinPaprika's
    signed field where the symbol matches (best rank wins on duplicates), and
    only falls back to CoinGecko's own signed JSON field. Never inferred.
    """
    try:
        d = get_json("https://api.coingecko.com/api/v3/search/trending")
    except Exception as e:  # noqa: BLE001
        problems.append(f"trending ({type(e).__name__})")
        return prev.get("trending", [])

    by_sym = {}
    for t in ranked:  # ranked is rank-ascending, so first write is best rank
        by_sym.setdefault((t.get("symbol") or "").upper(), t)

    trending = []
    for entry in (d.get("coins") or [])[:10]:
        item = entry.get("item") or {}
        name, sym = item.get("name"), (item.get("symbol") or "").upper()
        change = None
        match = by_sym.get(sym)
        if match:
            change = num((match.get("quotes") or {}).get("USD", {}).get("percent_change_24h"))
        if change is None:
            change = num(((item.get("data") or {}).get("price_change_percentage_24h") or {}).get("usd"))
        trending.append({"name": name, "sym": sym, "change": change})

    return trending or prev.get("trending", [])


def fetch_fng(prev):
    try:
        d = get_json("https://api.alternative.me/fng/?limit=30")["data"]
    except Exception as e:  # noqa: BLE001
        problems.append(f"fear & greed ({type(e).__name__})")
        return prev.get("fearGreed", {})

    def at(i):
        try:
            return int(d[i]["value"]), d[i]["value_classification"]
        except (IndexError, KeyError, ValueError, TypeError):
            return None, None

    now, now_l = at(0)
    y, y_l = at(1)
    w, w_l = at(7)
    m, m_l = at(29)
    if now is None:
        return prev.get("fearGreed", {})
    return {
        "now": now, "nowLabel": now_l,
        "yesterday": y, "yesterdayLabel": y_l,
        "lastWeek": w, "lastWeekLabel": w_l,
        "lastMonth": m, "lastMonthLabel": m_l,
    }


def relative_time(dt, now):
    secs = max(0, (now - dt).total_seconds())
    if secs < 3600:
        return f"about {max(1, int(secs // 60))} minutes ago"
    if secs < 86400:
        h = int(secs // 3600)
        return f"about {h} hour{'s' if h != 1 else ''} ago"
    dys = int(secs // 86400)
    return f"about {dys} day{'s' if dys != 1 else ''} ago"


def clean(text, limit=260):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = htmllib.unescape(text).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        text = cut[: cut.rfind(" ")] + "…" if " " in cut else cut + "…"
    return text


def fetch_news(prev):
    now = datetime.now(timezone.utc)
    for url in NEWS_FEEDS:
        try:
            root = ET.fromstring(get(url, tries=2))
            src_name = clean((root.findtext(".//channel/title") or "News"), 40)
            items = []
            for it in root.findall(".//channel/item")[:3]:
                title = clean(it.findtext("title") or "", 200)
                desc = clean(it.findtext("description") or "", 260)
                try:
                    when = relative_time(parsedate_to_datetime(it.findtext("pubDate")), now)
                except Exception:  # noqa: BLE001 - missing/odd date is not fatal
                    when = ""
                body = f"{title} — {desc}" if desc and desc.lower() not in title.lower() else title
                if title:
                    items.append({"src": src_name, "time": when, "text": body})
            if len(items) == 3:
                return items
        except Exception:  # noqa: BLE001 - try the next feed
            continue
    problems.append("news (all feeds unavailable)")
    return prev.get("news", [])


# ---------------------------------------------------------------- main
def main():
    with open(INDEX, encoding="utf-8") as f:
        src = f.read()

    prev = load_previous(src)
    if not prev:
        print("note: no parseable previous DATA (expected on first run)")

    core, ranked = fetch_global_and_coins(prev)
    data = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "global": core["global"],
        "fearGreed": fetch_fng(prev),
        "coins": core["coins"],
        "trending": fetch_trending(prev, ranked),
        "gainers": core["gainers"],
        "news": fetch_news(prev),
    }

    # Refuse to publish a hollowed-out page.
    if not data["coins"]:
        print("FATAL: no coin data and no previous values to fall back on", file=sys.stderr)
        return 1

    block = "const DATA = " + json.dumps(data, indent=2, ensure_ascii=False) + ";"
    new_src, n = re.subn(r"const DATA = \{.*?\n\};", lambda _: block, src, count=1, flags=re.S)
    if n != 1:
        print("FATAL: could not locate the DATA block in index.html", file=sys.stderr)
        return 1

    with open(INDEX, "w", encoding="utf-8") as f:
        f.write(new_src)

    print(f"refreshed {data['generatedAt']} — {len(data['coins'])} coins, "
          f"{len(data['trending'])} trending, {len(data['gainers'])} gainers, "
          f"{len(data['news'])} news")
    if problems:
        print("degraded (previous values kept): " + "; ".join(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())
