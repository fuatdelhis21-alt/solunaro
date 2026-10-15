"""Market data adapters. Every value that reaches an agent comes from here.

Crypto (CEX spot): Binance public market data (no key needed).
Solana DEX pools: GeckoTerminal OHLCV + DexScreener pool stats (no key needed).
News: public RSS feeds. Sentiment: alternative.me Fear & Greed + order-flow stats.

If a source fails, the market gets a `missing` entry instead of guessed values.
"""
from __future__ import annotations

import email.utils
import html
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

from .indicators import Candle

UA = {"User-Agent": "solunaro-paper-desk/1.0"}
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
BINANCE_INTERVAL_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
GT_INTERVAL_SEC = {"minute": 60, "hour": 3600, "day": 86400}


class DataError(Exception):
    pass


RETRY_STATUS = {429, 500, 502, 503, 504}


def _get(url: str, params: dict | None = None, timeout: int = 15, retries: int = 2):
    """GET with a bounded retry on 429/5xx/network errors (backoff 1s, 2s; Retry-After honoured
    up to 10s). Never loops forever; the caller sees DataError and the market is skipped."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
        except requests.RequestException as e:
            if attempt == retries:
                raise DataError(f"{url} -> {type(e).__name__}") from e
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            break
        if r.status_code not in RETRY_STATUS or attempt == retries:
            raise DataError(f"{url} -> HTTP {r.status_code}")
        try:
            wait = float(r.headers.get("Retry-After", 0))
        except (TypeError, ValueError, AttributeError):
            wait = 0
        time.sleep(min(max(wait, 2 ** attempt), 10))
    return r


@dataclass
class MarketSnapshot:
    market_id: str                 # e.g. "crypto:SOLUSDT" or "dex:solana:<pool>"
    kind: str                      # "crypto" | "dex"
    label: str
    candles: list[Candle]
    interval_sec: int
    fetched_at: int
    fee_bps: float
    slippage_bps: float = 0
    keywords: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)     # pool stats, 24h ticker, etc.
    missing: list[str] = field(default_factory=list)

    @property
    def last_price(self) -> float:
        return self.candles[-1].close

    def staleness_minutes(self, now: int | None = None) -> float:
        now = now or int(time.time())
        last_close_time = self.candles[-1].ts + self.interval_sec
        return max(0.0, (now - last_close_time) / 60)


def clean_candles(candles: list[Candle], source: str) -> list[Candle]:
    """Sort, drop exact duplicate timestamps (keep the last), and FAIL CLOSED on any candle with
    a non-finite/non-positive price, negative volume or impossible OHLC (high < max(open, close)
    or low > min(open, close)). One bad candle makes the whole series untrusted."""
    by_ts: dict[int, Candle] = {}
    for c in candles:
        prices = (c.open, c.high, c.low, c.close)
        if not all(math.isfinite(x) and x > 0 for x in prices):
            raise DataError(f"{source}: invalid price in candle {c.ts}")
        if not math.isfinite(c.volume) or c.volume < 0:
            raise DataError(f"{source}: invalid volume in candle {c.ts}")
        if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close, c.high):
            raise DataError(f"{source}: inconsistent OHLC in candle {c.ts}")
        by_ts[c.ts] = c
    return [by_ts[k] for k in sorted(by_ts)]


def gap_count(candles: list[Candle], interval_sec: int) -> int:
    """Missing intervals between consecutive candles (DEX pools omit hours without trades)."""
    return sum(max(0, (b.ts - a.ts) // interval_sec - 1) for a, b in zip(candles, candles[1:]))


# ---------------------------------------------------------------- Binance
def binance_klines(symbol: str, interval: str, limit: int) -> list[Candle]:
    last_err = None
    for host in BINANCE_HOSTS:
        try:
            rows = _get(f"{host}/api/v3/klines",
                        {"symbol": symbol, "interval": interval, "limit": limit}).json()
            break
        except (DataError, requests.RequestException) as e:
            last_err = e
    else:
        raise DataError(f"binance klines {symbol}: {last_err}")
    now_ms = int(time.time() * 1000)
    out = []
    for r in rows:
        if int(r[6]) > now_ms:          # drop the still-open candle
            continue
        out.append(Candle(ts=int(r[0]) // 1000, open=float(r[1]), high=float(r[2]),
                          low=float(r[3]), close=float(r[4]), volume=float(r[5]),
                          taker_buy_volume=float(r[9])))
    return clean_candles(out, f"binance {symbol}")


def binance_24h(symbol: str) -> dict:
    for host in BINANCE_HOSTS:
        try:
            d = _get(f"{host}/api/v3/ticker/24hr", {"symbol": symbol}).json()
            return {k: d.get(k) for k in
                    ("priceChangePercent", "quoteVolume", "count", "highPrice", "lowPrice", "lastPrice")}
        except (DataError, requests.RequestException):
            continue
    raise DataError(f"binance 24h {symbol}")


def crypto_snapshot(sym_cfg: dict, mcfg: dict) -> MarketSnapshot:
    symbol = sym_cfg["symbol"]
    snap = MarketSnapshot(market_id=f"crypto:{symbol}", kind="crypto", label=symbol,
                          candles=[], interval_sec=BINANCE_INTERVAL_SEC[mcfg["interval"]],
                          fetched_at=int(time.time()), fee_bps=mcfg.get("fee_bps", 10),
                          keywords=sym_cfg.get("keywords", []))
    snap.candles = binance_klines(symbol, mcfg["interval"], mcfg["lookback"])
    if (g := gap_count(snap.candles, snap.interval_sec)):
        snap.missing.append(f"{g} missing candle(s) in series")
    try:
        snap.extra["ticker_24h"] = binance_24h(symbol)
    except DataError as e:
        snap.missing.append(f"24h ticker unavailable: {e}")
    return snap


# ---------------------------------------------------------------- Solana DEX
def geckoterminal_ohlcv(chain: str, pool: str, timeframe: str, limit: int) -> list[Candle]:
    url = f"https://api.geckoterminal.com/api/v2/networks/{chain}/pools/{pool}/ohlcv/{timeframe}"
    d = _get(url, {"aggregate": 1, "limit": min(limit, 1000), "currency": "usd"}).json()
    rows = d["data"]["attributes"]["ohlcv_list"]           # newest first
    step = GT_INTERVAL_SEC[timeframe]
    now = int(time.time())
    out = [Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                  close=float(r[4]), volume=float(r[5]))
           for r in sorted(rows, key=lambda r: r[0]) if int(r[0]) + step <= now]
    return clean_candles(out, f"geckoterminal {pool}")


def dexscreener_pool(chain: str, pool: str) -> dict:
    d = _get(f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pool}").json()
    pair = d.get("pair") or (d.get("pairs") or [None])[0]
    if not pair:
        raise DataError(f"dexscreener: pool {pool} not found")
    # Never attach stats of a different pool/chain to this market.
    if pair.get("chainId") != chain or str(pair.get("pairAddress", "")).lower() != pool.lower():
        raise DataError(f"dexscreener: returned {pair.get('chainId')}/{pair.get('pairAddress')}, "
                        f"expected {chain}/{pool}")
    return {
        "dex": pair.get("dexId"),
        "base": (pair.get("baseToken") or {}).get("symbol"),
        "base_name": (pair.get("baseToken") or {}).get("name"),
        "quote": (pair.get("quoteToken") or {}).get("symbol"),
        "price_usd": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
        "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
        "volume": pair.get("volume"),
        "txns": pair.get("txns"),
        "price_change_pct": pair.get("priceChange"),
        "pair_created_at_ms": pair.get("pairCreatedAt"),
        "url": pair.get("url"),
    }


def dex_snapshot(pool_cfg: dict, mcfg: dict) -> MarketSnapshot:
    chain, pool = mcfg["chain"], pool_cfg["address"]
    snap = MarketSnapshot(market_id=f"dex:{chain}:{pool}", kind="dex", label=pool[:8],
                          candles=[], interval_sec=GT_INTERVAL_SEC[mcfg["interval"]],
                          fetched_at=int(time.time()), fee_bps=mcfg.get("fee_bps", 30),
                          slippage_bps=mcfg.get("slippage_bps", 50),
                          keywords=list(pool_cfg.get("keywords", [])))
    snap.candles = geckoterminal_ohlcv(chain, pool, mcfg["interval"], mcfg["lookback"])
    if (g := gap_count(snap.candles, snap.interval_sec)):
        snap.missing.append(f"{g} missing candle(s) in series (hours without trades)")
    try:
        stats = dexscreener_pool(chain, pool)
        snap.extra["pool"] = stats
        if stats.get("base"):
            snap.label = f"{stats['base']}/{stats.get('quote')} ({stats.get('dex')})"
            # padded: news_for() matches substrings, so a bare "ray" would hit "array"
            snap.keywords += [f" {stats['base'].lower()} "]
            if stats.get("base_name"):
                snap.keywords.append(stats["base_name"].lower())
    except (DataError, requests.RequestException) as e:
        snap.missing.append(f"pool stats unavailable: {e}")
    return snap


# ---------------------------------------------------------------- News (RSS)
def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch_news(feeds: list[str], max_age_hours: float) -> tuple[list[dict], list[str]]:
    items, errors = [], []
    cutoff = time.time() - max_age_hours * 3600
    for url in feeds:
        try:
            root = ET.fromstring(_get(url).content)
        except Exception as e:  # noqa: BLE001 — a bad feed must not kill the cycle
            errors.append(f"{url}: {e}")
            continue
        for it in root.iter("item"):
            title = _strip(it.findtext("title"))
            link = (it.findtext("link") or "").strip()
            pub = it.findtext("pubDate")
            try:
                ts = email.utils.parsedate_to_datetime(pub).timestamp() if pub else None
            except (TypeError, ValueError):
                ts = None
            if ts is None or ts < cutoff:
                continue
            items.append({"title": title, "link": link, "published_ts": int(ts),
                          "source": re.sub(r"https?://(www\.)?([^/]+).*", r"\2", url),
                          "summary": _strip(it.findtext("description"))[:400]})
    # de-duplicate by title
    seen, uniq = set(), []
    for i in sorted(items, key=lambda x: -x["published_ts"]):
        key = i["title"].lower()
        if key not in seen:
            seen.add(key)
            uniq.append(i)
    return uniq, errors


def news_for(keywords: list[str], news: list[dict], limit: int = 8) -> list[dict]:
    kws = [k.lower() for k in keywords if k.strip()]
    out = []
    for n in news:
        text = f" {n['title']} {n['summary']} ".lower()
        if any(k in text for k in kws):
            out.append(n)
    return out[:limit]


# ---------------------------------------------------------------- Sentiment
def fear_greed() -> dict:
    d = _get("https://api.alternative.me/fng/", {"limit": 7}).json()["data"]
    return {"latest": {"value": int(d[0]["value"]), "label": d[0]["value_classification"],
                       "ts": int(d[0]["timestamp"])},
            "last_7_values": [int(x["value"]) for x in d]}


# ---------------------------------------------------------------- Futures positioning
FAPI = "https://fapi.binance.com"


def binance_futures_positioning(symbol: str) -> dict:
    """Funding rate + open interest change for the USDT-M perpetual of `symbol`.
    Public endpoints, no key. Note: geo-restricted in some regions (e.g. US IPs)."""
    p = _get(f"{FAPI}/fapi/v1/premiumIndex", {"symbol": symbol}).json()
    hist = _get(f"{FAPI}/futures/data/openInterestHist",
                {"symbol": symbol, "period": "1h", "limit": 25}).json()
    out = {"funding_rate_last": float(p["lastFundingRate"]),
           "funding_rate_last_pct": round(float(p["lastFundingRate"]) * 100, 4),
           "mark_price": float(p["markPrice"]), "index_price": float(p["indexPrice"]),
           "next_funding_ts": int(p["nextFundingTime"]) // 1000}
    if isinstance(hist, list) and len(hist) >= 2:
        first, last = float(hist[0]["sumOpenInterestValue"]), float(hist[-1]["sumOpenInterestValue"])
        out["open_interest_usd"] = round(last, 0)
        out["open_interest_change_24h_pct"] = round((last / first - 1) * 100, 3) if first else None
    return out


# ---------------------------------------------------------------- CryptoPanic (optional)
def cryptopanic_news(url: str, token: str, currencies: list[str], max_age_hours: float) -> list[dict]:
    """Optional aggregated news with community votes. Needs CRYPTOPANIC_TOKEN.
    Field names are read defensively because the API has had several versions."""
    if not token or not currencies:
        return []
    d = _get(url, {"auth_token": token, "currencies": ",".join(currencies),
                   "public": "true", "kind": "news"}).json()
    cutoff = time.time() - max_age_hours * 3600
    out = []
    for r in d.get("results", []):
        ts_raw = r.get("published_at") or r.get("created_at")
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            continue
        if ts < cutoff:
            continue
        src = r.get("source") or {}
        out.append({"title": _strip(r.get("title", "")),
                    "link": r.get("original_url") or r.get("url") or "",
                    "published_ts": int(ts),
                    "source": f"cryptopanic/{src.get('domain') or src.get('title') or '?'}",
                    "summary": _strip(r.get("description") or "")[:400],
                    "votes": r.get("votes"),
                    "currencies": [c.get("code") for c in (r.get("currencies") or [])
                                   if isinstance(c, dict)]})
    return out


# ---------------------------------------------------------------- History (backtest only)
def binance_history(symbol: str, interval: str, total: int) -> list[Candle]:
    """Up to `total` closed candles, paginated backwards with endTime (1000 per request)."""
    out: dict[int, Candle] = {}
    end_ms = None
    while len(out) < total:
        params = {"symbol": symbol, "interval": interval, "limit": min(1000, total - len(out))}
        if end_ms:
            params["endTime"] = end_ms
        rows = None
        for host in BINANCE_HOSTS:
            try:
                rows = _get(f"{host}/api/v3/klines", params).json()
                break
            except (DataError, requests.RequestException):
                continue
        if rows is None:
            raise DataError(f"binance history {symbol} failed")
        if not rows:
            break
        now_ms = int(time.time() * 1000)
        for r in rows:
            if int(r[6]) <= now_ms:
                out[int(r[0]) // 1000] = Candle(int(r[0]) // 1000, float(r[1]), float(r[2]),
                                                float(r[3]), float(r[4]), float(r[5]), float(r[9]))
        new_end = int(rows[0][0]) - 1
        if end_ms is not None and new_end >= end_ms:
            break
        end_ms = new_end
        time.sleep(0.2)
    return clean_candles(list(out.values()), f"binance history {symbol}")[-total:]


def geckoterminal_history(chain: str, pool: str, timeframe: str, total: int) -> list[Candle]:
    url = f"https://api.geckoterminal.com/api/v2/networks/{chain}/pools/{pool}/ohlcv/{timeframe}"
    step, now = GT_INTERVAL_SEC[timeframe], int(time.time())
    out: dict[int, Candle] = {}
    before = None
    while len(out) < total:
        params = {"aggregate": 1, "limit": min(1000, total - len(out)), "currency": "usd"}
        if before:
            params["before_timestamp"] = before
        rows = _get(url, params).json()["data"]["attributes"]["ohlcv_list"]
        if not rows:
            break
        for r in rows:
            if int(r[0]) + step <= now:
                out[int(r[0])] = Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                                        float(r[4]), float(r[5]))
        oldest = min(int(r[0]) for r in rows)
        if before is not None and oldest >= before:
            break
        before = oldest
        time.sleep(2.5)                      # GeckoTerminal free tier is ~30 req/min
    return clean_candles(list(out.values()), f"geckoterminal history {pool}")[-total:]
