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
