"""Currency news for the Pro plan: central-bank feeds + GDELT headlines.

DISPLAY ONLY. Nothing in here feeds the forecast, confidence score or recommendation.

Sources
  * Central bank RSS/Atom feeds (official, free).      -> FEEDS below
  * GDELT DOC 2.0 API (free, no key). GDELT requires a citation + link to
    https://www.gdeltproject.org/ wherever its data is used (the UI shows it).
    GDELT also asks for at most one request every 5 seconds, enforced here.

We only keep headline, link, source and date, never article text.

Design notes
  * Results are cached per *currency* (not per pair, not per user) in a process-wide cache,
    so API traffic grows with the number of currencies, not with the number of visitors.
  * A failed fetch is cached for a short time so reruns of the page don't hammer the sources.
  * Every URL we call is fixed in this file (nothing user-supplied), and every link we show
    is validated to be http(s).
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse

import feedparser
import requests

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MIN_INTERVAL = 5.5  # seconds between GDELT calls (their limit is 1 per 5 s)
USER_AGENT = "FXForecaster/1.0 (news panel)"
MAX_BYTES = 3_000_000
TTL_OK = 30 * 60
TTL_FAIL = 2 * 60
MAX_ITEMS = 10  # per currency shown
MAX_PER_DOMAIN = 2
TITLE_MAX = 200

# currency -> search phrases for GDELT (hand-tuned; edit freely to improve relevance)
TERMS: dict[str, list[str]] = {
    "USD": ["Federal Reserve", "US dollar", "US inflation", "US interest rates"],
    "EUR": ["European Central Bank", "eurozone inflation", "eurozone economy", "euro exchange rate"],
    "GBP": ["Bank of England", "British pound", "UK inflation", "UK interest rates"],
    "JPY": ["Bank of Japan", "Japanese yen", "Japan inflation", "Japan interest rates"],
    "CHF": ["Swiss National Bank", "Swiss franc", "Swiss inflation"],
    "AUD": ["Reserve Bank of Australia", "Australian dollar", "Australia inflation", "Australia interest rates"],
    "CAD": ["Bank of Canada", "Canadian dollar", "Canada inflation", "Canada interest rates"],
    "NZD": ["Reserve Bank of New Zealand", "New Zealand dollar", "New Zealand inflation"],
    "CNY": ["People's Bank of China", "Chinese yuan", "China inflation", "China economy"],
    "HKD": ["Hong Kong dollar", "Hong Kong Monetary Authority", "Hong Kong economy"],
    "SGD": ["Singapore dollar", "Monetary Authority of Singapore", "Singapore inflation"],
    "THB": ["Thai baht", "Bank of Thailand", "Thailand inflation", "Thailand interest rates"],
    "IDR": ["Indonesian rupiah", "Bank Indonesia", "Indonesia inflation"],
    "MYR": ["Malaysian ringgit", "Bank Negara Malaysia", "Malaysia inflation"],
    "PHP": ["Philippine peso", "Bangko Sentral ng Pilipinas", "Philippines inflation"],
    "VND": ["Vietnamese dong", "State Bank of Vietnam", "Vietnam inflation"],
    "INR": ["Indian rupee", "Reserve Bank of India", "India inflation", "India interest rates"],
    "KRW": ["South Korean won", "Bank of Korea", "South Korea inflation"],
    "TWD": ["Taiwan dollar", "Taiwan central bank", "Taiwan inflation"],
    "AED": ["UAE dirham", "UAE economy", "UAE central bank"],
    "SAR": ["Saudi riyal", "Saudi central bank", "Saudi Arabia inflation"],
    "ZAR": ["South African rand", "South African Reserve Bank", "South Africa inflation"],
    "TRY": ["Turkish lira", "Turkey central bank", "Turkey inflation", "Turkey interest rates"],
    "MXN": ["Mexican peso", "Banxico", "Mexico inflation", "Bank of Mexico"],
    "BRL": ["Brazilian real", "Central Bank of Brazil", "Brazil inflation", "Brazil interest rates"],
    "SEK": ["Swedish krona", "Riksbank", "Sweden inflation"],
    "NOK": ["Norwegian krone", "Norges Bank", "Norway inflation"],
    "DKK": ["Danish krone", "Danmarks Nationalbank", "Denmark inflation"],
    "PLN": ["Polish zloty", "National Bank of Poland", "Poland inflation"],
    "CZK": ["Czech koruna", "Czech National Bank", "Czech inflation"],
}

# currency -> [(display name, feed URL)].
# NOTE: these URLs were written from memory and could NOT be verified from the build
# environment. If one is wrong or moves, the app just skips it and the "Source status"
# panel shows the error. Fix the URL here (and add more banks the same way).
FEEDS: dict[str, list[tuple[str, str]]] = {
    "USD": [("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml")],
    "EUR": [("European Central Bank", "https://www.ecb.europa.eu/rss/press.xml")],
    "GBP": [("Bank of England", "https://www.bankofengland.co.uk/rss/news")],
    "JPY": [("Bank of Japan", "https://www.boj.or.jp/en/rss/whatsnew.xml")],
    "AUD": [("Reserve Bank of Australia", "https://www.rba.gov.au/rss/rss-cb-media-releases.xml")],
    "CAD": [("Bank of Canada", "https://www.bankofcanada.ca/content_type/press-releases/feed/")],
}

# A GDELT headline is kept if it mentions one of the currency's own phrases or a generic
# macro/FX word. This is a light noise filter, not a relevance model.
ECON_WORDS = (
    "inflation", "interest rate", "rate cut", "rate hike", "rates", "central bank", "currency",
    "exchange rate", "forex", "bond yield", "yields", "gdp", "tariff", "trade", "economy",
    "economic", "recession", "monetary", "dollar", "euro", "yen", "pound", "sterling", "baht",
    "peso", "rupee", "yuan", "franc", "krona", "krone", "won", "rand", "lira", "ringgit",
    "rupiah", "dong", "stimulus", "jobs report", "unemployment", "devalu", "budget deficit",
)


class NewsError(Exception):
    pass


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    published: datetime | None
    kind: str  # "central_bank" | "news"
    code: str


@dataclass
class SourceStatus:
    name: str
    ok: bool
    count: int = 0
    error: str = ""


@dataclass
class NewsResult:
    code: str
    items: list[NewsItem] = field(default_factory=list)
    status: list[SourceStatus] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def failed(self) -> list[SourceStatus]:
        return [s for s in self.status if not s.ok]


# --------------------------------------------------------------------------
# HTTP (single choke point; tests replace this)
# --------------------------------------------------------------------------
def _http_get(url: str, params: dict | None = None) -> bytes:
    try:
        with requests.get(url, params=params, headers={"User-Agent": USER_AGENT},
                          timeout=(5, 15), stream=True) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > MAX_BYTES:
                    raise NewsError("response too large")
            return bytes(buf)
    except requests.RequestException as e:
        raise NewsError(f"{type(e).__name__}: {str(e)[:120]}") from e


# --------------------------------------------------------------------------
# Cleaning helpers
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_title(s: str | None) -> str:
    s = unescape(_TAG_RE.sub(" ", s or ""))
    s = _WS_RE.sub(" ", s).strip()
    return s if len(s) <= TITLE_MAX else s[: TITLE_MAX - 1].rstrip() + "…"


def safe_url(u: str | None) -> str | None:
    """Only absolute http(s) links; anything else (javascript:, data:, ...) is dropped."""
    if not u:
        return None
    u = u.strip()
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return u


def md_link(title: str, url: str) -> str:
    """A Markdown link that cannot be broken out of by odd characters in title or url."""
    t = re.sub(r"([\\`*_{}\[\]()#+!|<>~$])", r"\\\1", title)
    u = url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")
    return f"[{t}]({u})"


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    if not dt:
        return "date unknown"
    secs = max(0, int(((now or datetime.now(timezone.utc)) - dt).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def _norm_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:90]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_feed(content: bytes, source: str, code: str, limit: int = 30) -> list[NewsItem]:
    """RSS 2.0, RSS 1.0 (RDF) and Atom, via feedparser."""
    d = feedparser.parse(content)
    if not d.entries:
        raise NewsError("no entries (not a valid feed, or the feed is empty)")
    out = []
    for e in d.entries[:limit]:
        title, url = clean_title(e.get("title")), safe_url(e.get("link"))
        if not title or not url:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime(*ts[:6], tzinfo=timezone.utc) if ts else None
        out.append(NewsItem(title, url, source, pub, "central_bank", code))
    if not out:
        raise NewsError("feed had entries but none had a title and link")
    return out


def parse_gdelt(content: bytes, code: str) -> list[NewsItem]:
    try:
        data = json.loads(content)
    except ValueError:
        # GDELT answers with plain text when throttled or when the query is rejected.
        raise NewsError("GDELT said: " + content[:120].decode("utf-8", "replace").strip()) from None
    out = []
    for a in data.get("articles", []) if isinstance(data, dict) else []:
        title, url = clean_title(a.get("title")), safe_url(a.get("url"))
        if not title or not url:
            continue
        try:
            pub = datetime.strptime(a.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pub = None
        out.append(NewsItem(title, url, clean_title(a.get("domain")) or "news", pub, "news", code))
    return out


def _relevant(item: NewsItem, terms: list[str]) -> bool:
    t = item.title.lower()
    return any(x.lower() in t for x in terms) or any(w in t for w in ECON_WORDS)


def _select(items: list[NewsItem], terms: list[str]) -> list[NewsItem]:
    """Filter noise, dedupe, cap per domain, keep central bank items, newest first."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    items = sorted(items, key=lambda i: i.published or floor, reverse=True)
    seen_t, seen_u, per_domain, out = set(), set(), {}, []
    for it in items:
        if it.kind == "news" and not _relevant(it, terms):
            continue
        k = _norm_key(it.title)
        if k in seen_t or it.url in seen_u:
            continue
        dom = it.source.lower()
        if it.kind == "news" and per_domain.get(dom, 0) >= MAX_PER_DOMAIN:
            continue
        seen_t.add(k)
        seen_u.add(it.url)
        per_domain[dom] = per_domain.get(dom, 0) + 1
        out.append(it)
    cb = [i for i in out if i.kind == "central_bank"][:4]
    news = [i for i in out if i.kind == "news"][: MAX_ITEMS - len(cb)]
    return sorted(cb + news, key=lambda i: i.published or floor, reverse=True)


# --------------------------------------------------------------------------
# Fetching with shared cache
# --------------------------------------------------------------------------
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, NewsResult]] = {}
_last_gdelt = 0.0


def _gdelt_search(code: str, terms: list[str]) -> list[NewsItem]:
    global _last_gdelt
    wait = GDELT_MIN_INTERVAL - (time.monotonic() - _last_gdelt)
    if wait > 0:
        time.sleep(wait)
    _last_gdelt = time.monotonic()
    query = "(" + " OR ".join(f'"{t}"' for t in terms) + ") sourcelang:english"
    raw = _http_get(GDELT_URL, {"query": query, "mode": "artlist", "format": "json",
                                "maxrecords": 50, "timespan": "7d", "sort": "datedesc"})
    return parse_gdelt(raw, code)


def _fetch(code: str) -> NewsResult:
    res, items = NewsResult(code), []
    terms = TERMS.get(code, [])
    for name, url in FEEDS.get(code, []):
        try:
            got = parse_feed(_http_get(url), name, code)
            items += got
            res.status.append(SourceStatus(name, True, len(got)))
        except Exception as e:  # one bad source must never break the page
            res.status.append(SourceStatus(name, False, error=str(e)[:160]))
    if terms:
        try:
            got = _gdelt_search(code, terms)
            items += got
            res.status.append(SourceStatus("GDELT headlines", True, len(got)))
        except Exception as e:
            res.status.append(SourceStatus("GDELT headlines", False, error=str(e)[:160]))
    if not res.status:
        res.status.append(SourceStatus("no sources configured", False, error=f"nothing set up for {code}"))
    res.items = _select(items, terms)
    return res


def get_news(code: str) -> NewsResult:
    now = time.monotonic()
    hit = _CACHE.get(code)
    if hit and hit[0] > now:
        return hit[1]
    with _LOCK:  # one fetch at a time: also keeps GDELT calls spaced out across visitors
        hit = _CACHE.get(code)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        res = _fetch(code)
        ok = bool(res.items) or not res.failed
        _CACHE[code] = (time.monotonic() + (TTL_OK if ok else TTL_FAIL), res)
        return res


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()