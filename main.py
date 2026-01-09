import os
import time
import json
import requests
from datetime import datetime, timezone

# =========================
# ENV VARS (REQUIRED)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Missing BOT_TOKEN or CHAT_ID")

# =========================
# CORE SETTINGS (TUNED)
# =========================
ALLOWED_LEAGUES = {"NFL", "NBA", "NHL", "CBB", "CFB"}

MIN_LIQ = 1500
MAX_SPREAD = 0.06

MIN_EV = 0.04          # minimum +EV (4%)
TARGET_EV = 0.08       # sell when edge collapses to this
STOP_EV = 0.01         # emergency exit

MAX_PRICE = 0.90       # don't buy overpriced favorites
MIN_PRICE = 0.15       # avoid lottery trash

SLEEP_SECONDS = 120

# =========================
# API
# =========================
GAMMA = "https://gamma-api.polymarket.com/markets"

# =========================
# TELEGRAM
# =========================
def send(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True
    }, timeout=20)

send("🧠 PollyMaster BEAST MODE online")

# =========================
# STATE (dedupe)
# =========================
STATE_FILE = "/tmp/state.json"

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

state = load_state()

# =========================
# HELPERS
# =========================
def today_utc():
    return datetime.now(timezone.utc).date()

def iso_date(s):
    try:
        return datetime.fromisoformat(s.replace("Z","+00:00")).date()
    except:
        return None

def league_from_slug(slug):
    return slug.split("-")[0].upper() if slug else None

def normalize(m):
    try:
        outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
        prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"]
        if len(outcomes) != 2:
            return None
        return [
            {"name": outcomes[0], "price": float(prices[0])},
            {"name": outcomes[1], "price": float(prices[1])},
        ]
    except:
        return None

def implied_prob(price):
    return price

def expected_value(true_p, market_p):
    return true_p - market_p

def fair_price(true_p):
    return round(true_p, 2)

# =========================
# CORE SCAN
# =========================
def scan():
    r = requests.get(GAMMA, params={
        "active": "true",
        "closed": "false",
        "limit": "300",
        "order": "volume",
        "ascending": "false"
    }, timeout=25)
    r.raise_for_status()
    markets = r.json()

    for m in markets:
        slug = m.get("slug","")
        league = league_from_slug(slug)
        if league not in ALLOWED_LEAGUES:
            continue

        date = iso_date(m.get("startTime",""))
        if date != today_utc():
            continue

        liq = float(m.get("liquidity",0))
        if liq < MIN_LIQ:
            continue

        outs = normalize(m)
        if not outs:
            continue

        p1, p2 = outs[0]["price"], outs[1]["price"]
        spread = abs(p1 - p2)
        if spread > MAX_SPREAD:
            continue

        # choose higher probability side
        pick = outs[0] if p1 > p2 else outs[1]
        opp = outs[1] if pick == outs[0] else outs[0]

        price = pick["price"]
        if not (MIN_PRICE <= price <= MAX_PRICE):
            continue

        # ---- EDGE MODEL ----
        # soft correction toward market mean + liquidity confidence
        confidence = min(0.15, liq / 100000)
        true_p = price + confidence

        ev = expected_value(true_p, price)
        if ev < MIN_EV:
            continue

        buy = round(price, 2)
        sell = round(min(fair_price(true_p), 0.98), 2)

        key = f"{slug}|{pick['name']}"
        if key in state:
            continue

        state[key] = time.time()
        save_state(state)

        msg = (
            f"🔥 EV PLAY ({league})\n"
            f"BUY: {pick['name']} @ {buy}\n"
            f"SELL TARGET: {sell}\n"
            f"EV ≈ +{round(ev*100,1)}%\n"
            f"liq={int(liq)} | spread={round(spread,3)}\n"
            f"{m.get('question','')}\n"
            f"https://polymarket.com/market/{slug}"
        )
        send(msg)

# =========================
# LOOP
# =========================
while True:
    try:
        scan()
        time.sleep(SLEEP_SECONDS)
    except Exception as e:
        send(f"⚠️ PollyMaster error: {e}")
        time.sleep(30)
