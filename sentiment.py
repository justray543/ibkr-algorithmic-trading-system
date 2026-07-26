"""
Public news headline sentiment -- filter only, never a trade trigger on its own.

Pulls headlines from a few free, no-key RSS feeds, matches them against each
instrument's keywords, and scores matches with a simple keyword heuristic.
This is intentionally crude (no ML, no paid API) -- it exists to veto an entry
the technicals already signaled when the news is loudly one-sided, not to
predict anything on its own.

Results are cached per label for SENTIMENT_CACHE_SECONDS since these feeds
don't update fast enough to justify refetching every 1-minute trading cycle.
"""

import time
import xml.etree.ElementTree as ET

import requests

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "https://www.investing.com/rss/news.rss",
]

# Which keywords count as "about" each instrument label.
KEYWORDS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth "],
    "SOL": ["solana"],
    "XRP": ["ripple", "xrp"],
    "LTC": ["litecoin"],
    "BCH": ["bitcoin cash"],
    "BNB": ["binance coin", "bnb"],
    "NASDAQ100": ["nasdaq"],
    "SP500": ["s&p 500", "s&p500", "wall street"],
    "NIKKEI225": ["nikkei"],
    "OIL": ["crude oil", "oil price", "opec"],
    "GOLD": ["gold price", "gold "],
}

POSITIVE_WORDS = [
    "surge", "surges", "rally", "rallies", "soar", "soars", "jump", "jumps",
    "bullish", "gain", "gains", "record high", "breakout", "upgrade", "rebound",
    "boom", "climb", "climbs", "outperform", "optimis",
]
NEGATIVE_WORDS = [
    "crash", "crashes", "plunge", "plunges", "slump", "slumps", "bearish",
    "sell-off", "selloff", "tumble", "tumbles", "downgrade", "warns", "warning",
    "fear", "fears", "ban", "bans", "hack", "hacked", "lawsuit", "crackdown",
    "collapse", "recession", "default",
]

SENTIMENT_CACHE_SECONDS = 900  # 15 min; RSS doesn't move fast enough to justify more
_headlines_cache = (0.0, [])  # (timestamp, headlines) -- shared across all labels/instruments


def _fetch_headlines():
    headlines = []
    for url in FEEDS:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                title = item.findtext("title")
                if title:
                    headlines.append(title)
        except Exception:
            continue  # one dead feed shouldn't block the others
    return headlines


def _get_headlines():
    """One shared fetch per SENTIMENT_CACHE_SECONDS window, reused by every
    instrument's lookup -- avoids re-hitting the feeds once per instrument."""
    global _headlines_cache
    now = time.time()
    ts, headlines = _headlines_cache
    if now - ts >= SENTIMENT_CACHE_SECONDS:
        headlines = _fetch_headlines()
        _headlines_cache = (now, headlines)
    return headlines


def _score_headline(title):
    t = title.lower()
    score = 0
    for w in POSITIVE_WORDS:
        if w in t:
            score += 1
    for w in NEGATIVE_WORDS:
        if w in t:
            score -= 1
    return score


def get_sentiment(label):
    """Returns (score, matched_headlines). score > 0 leans positive, < 0 leans
    negative, 0 means neutral or no matching headlines found."""
    keywords = KEYWORDS.get(label, [])
    if not keywords:
        return 0, []

    headlines = _get_headlines()
    matched = [h for h in headlines if any(k in h.lower() for k in keywords)]
    score = sum(_score_headline(h) for h in matched)
    return score, matched
