import os
import time
import json
import requests
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Missing BOT_TOKEN or CHAT_ID env vars")

TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ---- Alert thresholds (tweak anytime) ----
ULTRA_SAFE_PROB_MIN = 0.75
ULTRA_SAFE_SPREAD_MAX = 0.03
ULTRA_SAFE_LIQ_MIN = 5000

BALANCED_PROB_MIN = 0.55
BALANCED_PROB_MAX = 0.75
BALANCED_SPREAD_MAX = 0.05
BALANCED_LIQ_MIN = 2000

# Avoid spamming same market repeatedly
SEEN_FILE = "seen.json"
MAX_SEEN = 5000

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen):
    try:
        data = list(seen)[-MAX_SEEN:]
        with open(SEEN_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def tg_send(text: str):
    r = requests.post(TELEGRAM_URL, json={
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=20)
    r.raise_for_status()

def safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def parse_prices(m):
    """
    Gamma returns outcomes/outcomePrices often as strings.
    Example: outcomes: '["YES","NO"]', outcomePrices: '["0.62","0.38"]'
    """
    outcomes_raw = m.get("outcomes")
    prices_raw = m.get("outcomePrices")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if not outcomes or not prices or len(outcomes) != len(prices):
            return None, None
        # convert prices to floats
        prices = [safe_float(p) for p in prices]
        if any(p is None for p in prices):
            return None, None
        return outcomes, prices
    except Exception:
        return None, None

def get_sports_tag_ids():
    # /sports returns objects with "tags" as comma-separated string of IDs
    sports = requests.get(f"{GAMMA}/sports", timeout=20).json()
    tag_ids = set()
    for s in sports:
        tags = s.get("tags")
        if not tags:
            continue
        for t in str(tags).split(","):
            t = t.strip()
            if t.isdigit():
                tag_ids.add(int(t))
    return sorted(tag_ids)

def fetch_markets_by_tag(tag_id: int, limit=100, max_pages=10):
    allm = []
    offset = 0
    for _ in range(max_pages):
        params = {
            "tag_id": tag_id,
            "closed": "false",
            "limit": str(limit),
            "offset": str(offset),
        }
        r = requests.get(f"{GAMMA}/markets", params=params, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        allm.extend(batch)
        offset += limit
    return allm

def classify_market(m):
    # Pull core fields
    q = (m.get("question") or "").strip()
    slug = m.get("slug")
    url = f"https://polymarket.com/market/{slug}" if slug else None

    liq = safe_float(m.get("liquidity"), 0) or 0
    best_bid = safe_float(m.get("bestBid"), None)
    best_ask = safe_float(m.get("bestAsk"), None)
    last = safe_float(m.get("lastTradePrice"), None)

    outcomes, prices = parse_prices(m)
    if not outcomes:
        return None

    # For YES/NO markets, we treat "YES" price as implied prob (fallback: max price)
    prob = None
    if "YES" in outcomes:
        prob = prices[outcomes.index("YES")]
    else:
        prob = max(prices)

    # Spread estimate
    spread = None
    if best_bid is not None and best_ask is not None:
        spread = max(0.0, best_ask - best_bid)

    # Skip illiquid / missing
    if spread is None or prob is None:
        return None

    label = None
    if (prob >= ULTRA_SAFE_PROB_MIN and spread <= ULTRA_SAFE_SPREAD_MAX and liq >= ULTRA_SAFE_LIQ_MIN):
        label = "ULTRA_SAFE"
    elif (BALANCED_PROB_MIN <= prob < BALANCED_PROB_MAX and spread <= BALANCED_SPREAD_MAX and liq >= BALANCED_LIQ_MIN):
        label = "BALANCED"

    if not label:
        return None

    return {
        "label": label,
        "question": q,
        "prob": prob,
        "spread": spread,
        "liq": liq,
        "last": last,
        "url": url,
        "id": str(m.get("id") or slug or q)
    }

def main():
    seen = load_seen()

    # Startup ping so you KNOW it can message you
    tg_send("✅ Polymaster is online (sports alerts). I’ll ping you when ULTRA_SAFE or BALANCED markets match.")

    # Get all sports tag IDs
    sports_tag_ids = get_sports_tag_ids()

    # Loop forever
    while True:
        try:
            alerts = []

            # Pull markets across all sports tags (no max — we send everything that fits)
            for tag_id in sports_tag_ids:
                markets = fetch_markets_by_tag(tag_id, limit=100, max_pages=5)
                for m in markets:
                    if not m.get("active", True):
                        continue
                    if m.get("closed", False):
                        continue

                    pick = classify_market(m)
                    if not pick:
                        continue

                    # Dedup
                    key = pick["id"]
                    if key in seen:
                        continue
                    seen.add(key)
                    alerts.append(pick)

            if alerts:
                # Send in chunks so Telegram doesn’t reject huge messages
                alerts.sort(key=lambda x: (x["label"], -x["liq"], -x["prob"]))

                chunk = []
                for a in alerts:
                    line = (
                        f"{a['label']} | p={a['prob']:.2f} | spread={a['spread']:.3f} | liq={a['liq']:.0f}\n"
                        f"{a['question']}\n"
                        f"{a['url'] or ''}\n"
                        "—"
                    )
                    chunk.append(line)
                    if len("\n".join(chunk)) > 3000:
                        tg_send("\n".join(chunk))
                        chunk = []
                if chunk:
                    tg_send("\n".join(chunk))

                save_seen(seen)

        except Exception as e:
            # Don’t crash; just report once in a while
            try:
                tg_send(f"⚠️ Polymaster warning: {type(e).__name__}: {e}")
            except Exception:
                pass

        time.sleep(60)  # scan every 60s

if __name__ == "__main__":
    main()
