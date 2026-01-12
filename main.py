"""
POLYMASTER (Safe-Mode Alert Bot)
- Pulls Polymarket markets from Gamma API
- Pulls live orderbook summary from CLOB API
- Scores markets with configurable filters (liquidity, spread, momentum, time-to-resolve)
- Sends Telegram alerts (NO auto-trading, NO “guaranteed” buy/sell predictions)

Docs / Endpoints:
- Gamma Markets API: https://gamma-api.polymarket.com/markets   [oai_citation:0‡Polymarket Documentation](https://docs.polymarket.com/developers/gamma-markets-api/get-markets?utm_source=chatgpt.com)
- CLOB Orderbook Summary: https://clob.polymarket.com/book      [oai_citation:1‡Polymarket Documentation](https://docs.polymarket.com/api-reference/orderbook/get-order-book-summary?utm_source=chatgpt.com)

Run:
  pip install requests python-dateutil
  export TELEGRAM_BOT_TOKEN="..."
  export TELEGRAM_CHAT_ID="..."
  python polymaster.py

NOTE:
This is an ALERT bot. It helps you SEE opportunities; it does not guarantee profit.
"""

from __future__ import annotations

import os
import time
import math
import json
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from dateutil import parser as dateparser

# ----------------------------
# CONFIG (tune these)
# ----------------------------
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_SECONDS = 60  # how often to scan

# Filters
MIN_LIQUIDITY_USD = 2_500      # if too high, you get fewer alerts
MAX_SPREAD_CENTS  = 8          # tight markets are cheaper to enter/exit
MIN_TIME_TO_RESOLVE_HOURS = 2  # avoid ultra-chaotic last-minute stuff
MAX_TIME_TO_RESOLVE_DAYS  = 14 # avoid very long holds (optional)
MIN_MARKET_VOLUME_24H_USD = 500  # if too high, fewer alerts

# Scoring / alert threshold
ALERT_SCORE_MIN = 75  # loosen this if you want more alerts (e.g., 65-70)

# Momentum window (simple)
MOMENTUM_LOOKBACK_TRADES = 20  # for price trend check (if available)

# Safety limits
MAX_MARKETS_PER_SCAN = 200     # cap to avoid rate limits
REQUEST_TIMEOUT = 15
SESSION_RETRY = 2
SLEEP_ON_429_SECONDS = 10

# If you want only sports, put keywords here (leave empty for all)
INCLUDE_KEYWORDS = [
    # "NBA", "NFL", "MLB", "UFC", "Championship", "Lakers", "Chiefs"
]
EXCLUDE_KEYWORDS = [
    # "politics", "election"
]

# ----------------------------
# Helpers
# ----------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def cents(x: float) -> float:
    return x * 100.0

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def kw_match(text: str, keywords: List[str]) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)

def market_fingerprint(market: dict) -> str:
    """
    Stable fingerprint to dedupe alerts.
    Uses market id + lastUpdate or similar fields if present.
    """
    mid = str(market.get("id", market.get("slug", "")))
    lu  = str(market.get("updatedAt", market.get("lastUpdated", market.get("lastUpdate", ""))))
    return hashlib.sha256(f"{mid}|{lu}".encode("utf-8")).hexdigest()[:16]

# ----------------------------
# Data models
# ----------------------------
@dataclass
class BookSummary:
    token_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float

@dataclass
class Candidate:
    market_id: str
    slug: str
    question: str
    end_time: Optional[datetime]
    volume_24h: float
    liquidity: float
    outcomes: List[str]
    token_ids: List[str]  # clobTokenIds
    best_bid: float
    best_ask: float
    spread_cents: float
    score: int
    reason: str

# ----------------------------
# HTTP
# ----------------------------
class Http:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "polymaster-alert-bot/1.0"
        })

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        for attempt in range(SESSION_RETRY + 1):
            r = self.s.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                time.sleep(SLEEP_ON_429_SECONDS)
                continue
            return r
        return r

    def post(self, url: str, json_body: dict) -> requests.Response:
        for attempt in range(SESSION_RETRY + 1):
            r = self.s.post(url, json=json_body, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                time.sleep(SLEEP_ON_429_SECONDS)
                continue
            return r
        return r

http = Http()

# ----------------------------
# Telegram
# ----------------------------
def telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # If not set, just print.
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        r = http.post(url, payload)
        if r.status_code >= 300:
            print("Telegram error:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram exception:", e)

# ----------------------------
# Polymarket Fetchers
# ----------------------------
def fetch_markets(limit: int = 100, offset: int = 0) -> List[dict]:
    """
    Gamma Get Markets endpoint.
    Docs show: GET /markets  [oai_citation:2‡Polymarket Documentation](https://docs.polymarket.com/developers/gamma-markets-api/get-markets?utm_source=chatgpt.com)
    """
    url = f"{GAMMA_BASE}/markets"
    params = {
        "limit": limit,
        "offset": offset,
        # You can add filters here if you want:
        # "active": True,
        # "closed": False,
        # "sort": "volume24hr",
        # "order": "desc",
    }
    r = http.get(url, params=params)
    if r.status_code >= 300:
        raise RuntimeError(f"Gamma markets fetch failed: {r.status_code} {r.text[:200]}")
    return r.json()

def fetch_book_summary(token_id: str) -> Optional[BookSummary]:
    """
    CLOB orderbook summary endpoint.
    Docs show: GET /book  [oai_citation:3‡Polymarket Documentation](https://docs.polymarket.com/api-reference/orderbook/get-order-book-summary?utm_source=chatgpt.com)
    token_id should be passed as query param: token_id=<id>
    """
    url = f"{CLOB_BASE}/book"
    params = {"token_id": token_id}
    r = http.get(url, params=params)
    if r.status_code == 404:
        return None
    if r.status_code >= 300:
        # Sometimes CLOB returns 400 if token is malformed
        return None

    data = r.json() if r.text else {}
    # Typical fields: bids/asks arrays; use top-of-book
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        return None

    # Each entry often like {"price":"0.57","size":"123.4"} (strings)
    best_bid = safe_float(bids[0].get("price"))
    bid_size = safe_float(bids[0].get("size"))
    best_ask = safe_float(asks[0].get("price"))
    ask_size = safe_float(asks[0].get("size"))

    return BookSummary(
        token_id=str(token_id),
        best_bid=best_bid,
        best_ask=best_ask,
        bid_size=bid_size,
        ask_size=ask_size,
    )

def parse_end_time(m: dict) -> Optional[datetime]:
    # Gamma markets may use "endDate" / "endTime" / "resolutionTime"
    for key in ["endDate", "endTime", "resolutionTime", "resolveDate", "closeDate"]:
        if m.get(key):
            try:
                dt = dateparser.parse(m[key])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    return None

def get_market_text(m: dict) -> str:
    return (m.get("question") or m.get("title") or m.get("slug") or "").strip()

def get_clob_token_ids(m: dict) -> List[str]:
    # Docs mention `clobTokenIds` in market details.  [oai_citation:4‡Polymarket Documentation](https://docs.polymarket.com/quickstart/fetching-data?utm_source=chatgpt.com)
    ids = m.get("clobTokenIds")
    if isinstance(ids, list):
        return [str(x) for x in ids if str(x)]
    # Some markets might nest this differently
    return []

def estimate_liquidity_usd(book: BookSummary) -> float:
    """
    Simple liquidity proxy: top-of-book notional on both sides.
    (This is not total depth, but a consistent quick filter.)
    """
    bid_notional = book.best_bid * book.bid_size
    ask_notional = book.best_ask * book.ask_size
    return float(bid_notional + ask_notional)

def compute_score(
    liquidity: float,
    spread_cents: float,
    volume_24h: float,
    hours_to_resolve: float
) -> Tuple[int, str]:
    """
    Score 0-100. Higher is better.
    We reward:
      - higher liquidity
      - tighter spread
      - decent 24h volume
      - not too close / not too far to resolve
    """
    # Liquidity score (0..40)
    liq_score = 40.0 * clamp(math.log10(max(liquidity, 1.0)) / math.log10(10_000.0), 0.0, 1.0)

    # Spread score (0..25) where 0 cents = best
    spr_score = 25.0 * clamp((MAX_SPREAD_CENTS - spread_cents) / MAX_SPREAD_CENTS, 0.0, 1.0)

    # Volume score (0..20)
    vol_score = 20.0 * clamp(math.log10(max(volume_24h, 1.0)) / math.log10(50_000.0), 0.0, 1.0)

    # Time score (0..15) best in the middle of our window
    min_h = MIN_TIME_TO_RESOLVE_HOURS
    max_h = MAX_TIME_TO_RESOLVE_DAYS * 24.0
    if hours_to_resolve < min_h or hours_to_resolve > max_h:
        time_score = 0.0
    else:
        # Prefer 12h - 7d-ish, penalize extremes
        ideal = 72.0  # 3 days
        dist = abs(hours_to_resolve - ideal)
        time_score = 15.0 * clamp(1.0 - (dist / ideal), 0.0, 1.0)

    raw = liq_score + spr_score + vol_score + time_score
    score = int(round(clamp(raw, 0.0, 100.0)))

    reason = f"liq={liquidity:.0f}, spr={spread_cents:.1f}c, vol24h={volume_24h:.0f}, ttr={hours_to_resolve:.1f}h"
    return score, reason

# ----------------------------
# Core scan
# ----------------------------
def build_candidates(markets: List[dict]) -> List[Candidate]:
    out: List[Candidate] = []

    for m in markets:
        txt = get_market_text(m)
        if not txt:
            continue

        # Keyword include/exclude
        if INCLUDE_KEYWORDS and not kw_match(txt, INCLUDE_KEYWORDS):
            continue
        if EXCLUDE_KEYWORDS and kw_match(txt, EXCLUDE_KEYWORDS):
            continue

        # Basic fields
        market_id = str(m.get("id", ""))
        slug = str(m.get("slug", "")) or market_id
        question = txt

        volume_24h = safe_float(m.get("volume24hr") or m.get("volume24h") or 0.0)
        liquidity  = safe_float(m.get("liquidity") or m.get("liquidityUsd") or 0.0)

        # If Gamma liquidity missing, we will estimate from book.
        token_ids = get_clob_token_ids(m)
        if not token_ids:
            continue  # cannot price without token ids

        end_time = parse_end_time(m)
        if end_time:
            hours_to_resolve = (end_time - now_utc()).total_seconds() / 3600.0
        else:
            # If no end time, treat as far away (avoid spamming)
            hours_to_resolve = 9999.0

        # time-to-resolve filter
        if hours_to_resolve < MIN_TIME_TO_RESOLVE_HOURS:
            continue
        if hours_to_resolve > MAX_TIME_TO_RESOLVE_DAYS * 24.0:
            continue

        # volume filter
        if volume_24h < MIN_MARKET_VOLUME_24H_USD:
            continue

        # Pull book for the *YES* token (often token_ids[0]) and score it.
        # Many markets have two token ids (YES/NO). We can check both and pick better book.
        best_pick: Optional[Tuple[BookSummary, float, float]] = None  # (book, spread_cents, liq_est)

        for tid in token_ids[:2]:
            book = fetch_book_summary(tid)
            if not book:
                continue
            if book.best_bid <= 0 or book.best_ask <= 0:
                continue
            spr = cents(book.best_ask - book.best_bid)
            liq_est = estimate_liquidity_usd(book)

            # quick spread filter
            if spr > MAX_SPREAD_CENTS:
                continue

            # pick the most liquid
            if best_pick is None or liq_est > best_pick[2]:
                best_pick = (book, spr, liq_est)

        if not best_pick:
            continue

        book, spr, liq_est = best_pick
        eff_liq = liquidity if liquidity > 0 else liq_est

        # liquidity filter
        if eff_liq < MIN_LIQUIDITY_USD:
            continue

        score, reason = compute_score(
            liquidity=eff_liq,
            spread_cents=spr,
            volume_24h=volume_24h,
            hours_to_resolve=hours_to_resolve
        )

        outcomes = m.get("outcomes") if isinstance(m.get("outcomes"), list) else []
        out.append(Candidate(
            market_id=market_id,
            slug=slug,
            question=question,
            end_time=end_time,
            volume_24h=volume_24h,
            liquidity=eff_liq,
            outcomes=[str(x) for x in outcomes],
            token_ids=token_ids,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            spread_cents=spr,
            score=score,
            reason=reason
        ))

    # highest score first
    out.sort(key=lambda c: c.score, reverse=True)
    return out

def format_alert(c: Candidate) -> str:
    end_str = c.end_time.isoformat().replace("+00:00", "Z") if c.end_time else "unknown"
    url = f"https://polymarket.com/market/{c.slug}"

    # Suggested “entry/exit” here is NOT a prediction—just a structure:
    # - Entry near best_bid if you want to get filled cheaper (may not fill)
    # - Exit near best_ask (may not fill), or tighten based on spread
    entry = c.best_bid
    exit_  = c.best_ask

    return (
        f"🔥 POLYMASTER ALERT (Score {c.score})\n"
        f"{c.question}\n"
        f"Link: {url}\n"
        f"End: {end_str}\n"
        f"Book: bid={entry:.3f} ask={exit_:.3f} (spread {c.spread_cents:.1f}c)\n"
        f"Liquidity≈${c.liquidity:,.0f} | Vol24h≈${c.volume_24h:,.0f}\n"
        f"Why: {c.reason}\n"
        f"TokenIds: {', '.join(c.token_ids[:2])}\n"
        f"\n"
        f"Test-mode idea:\n"
        f"- Paper entry near bid ({entry:.3f})\n"
        f"- Paper exit near ask ({exit_:.3f})\n"
        f"- Watch if price trends + liquidity stays healthy"
    )

# ----------------------------
# Main loop with dedupe
# ----------------------------
def main():
    seen: Dict[str, float] = {}  # fingerprint -> timestamp
    telegram_send("✅ POLYMASTER online. Scanning Polymarket markets...")

    offset = 0
    limit = 100

    while True:
        try:
            # Pull first N markets (you can paginate if you want more)
            markets: List[dict] = []
            offset = 0
            while len(markets) < MAX_MARKETS_PER_SCAN:
                batch = fetch_markets(limit=limit, offset=offset)
                if not batch:
                    break
                markets.extend(batch)
                offset += limit
                if len(batch) < limit:
                    break
                if offset >= MAX_MARKETS_PER_SCAN:
                    break

            cands = build_candidates(markets)
            fired = 0

            for c in cands:
                if c.score < ALERT_SCORE_MIN:
                    break

                # dedupe by fingerprint based on market update
                # (to avoid repeating the same alert every minute)
                fp = hashlib.sha256(f"{c.market_id}|{c.best_bid:.4f}|{c.best_ask:.4f}|{int(c.volume_24h)}|{int(c.liquidity)}".encode()).hexdigest()[:16]
                if fp in seen and (time.time() - seen[fp]) < 6 * 3600:
                    continue

                telegram_send(format_alert(c))
                seen[fp] = time.time()
                fired += 1

                # avoid spamming
                if fired >= 3:
                    break

            # Clean old dedupe entries
            if len(seen) > 500:
                cutoff = time.time() - 24 * 3600
                seen = {k: v for k, v in seen.items() if v >= cutoff}

            if fired == 0:
                print(f"[{now_utc().isoformat()}] no alerts (top score: {cands[0].score if cands else 'n/a'})")

        except Exception as e:
            print("Scan error:", e)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
