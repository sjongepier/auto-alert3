import os
import asyncio
import aiohttp
import json
import re
from datetime import datetime, timedelta
from telegram import Bot

# ================= CONFIG =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 180

MAX_PRICE = 6500
MAX_KM = 180000
MIN_MARGIN = 700
MIN_PERCENT_BELOW_MARKET = 0.15

MODELS = [
    "aygo", "c1", "107", "up", "polo",
    "fiesta", "clio", "yaris",
    "208", "micra", "ibiza"
]

MOTIVATION_WORDS = [
    "moet weg", "ivm", "verhuizing",
    "geen tijd", "spoed", "overcompleet",
    "wegens nieuwe auto", "tweede auto"
]

DEALER_WORDS = [
    "garantie", "inruil", "btw",
    "bedrijf", "dealer", "showroom"
]

BAD_WORDS = [
    "motor kapot", "versnellingsbak kapot",
    "loopt niet", "schade", "export"
]

SEEN_FILE = "seen.json"

bot = Bot(token=TELEGRAM_TOKEN)

# ================= STORAGE =================

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# ================= HELPERS =================

async def send_telegram(message):
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

async def get_html(url, session):
    headers = {"User-Agent": "Mozilla/5.0"}
    async with session.get(url, headers=headers, timeout=15) as response:
        return await response.text()

def contains_any(text, word_list):
    text = text.lower()
    return any(word in text for word in word_list)

# ================= MARKET AVERAGE =================

async def get_market_average(session, model_keyword):
    url = f"https://www.marktplaats.nl/l/auto-s/q/{model_keyword}/"
    html = await get_html(url, session)

    prices = re.findall(r'"price":\s*"(\d+)"', html)
    prices = [int(p) for p in prices if int(p) < 20000]

    if len(prices) < 5:
        return None

    prices = prices[:20]
    return sum(prices) / len(prices)

# ================= SCRAPERS =================

async def scrape_marktplaats(session):
    url = "https://www.marktplaats.nl/l/auto-s/"
    html = await get_html(url, session)

    links = re.findall(r'href="(/v/auto[^"]+)"', html)
    return list(set([
        "https://www.marktplaats.nl" + link
        for link in links
    ]))[:30]

def parse_marktplaats(html):
    title_match = re.search(r'<title>(.*?)</title>', html)
    title = title_match.group(1) if title_match else ""

    price_match = re.search(r'"price":\s*"(\d+)"', html)
    price = int(price_match.group(1)) if price_match else None

    km_match = re.search(r'"mileageFromOdometer":\s*{\s*"value":\s*(\d+)', html)
    km = int(km_match.group(1)) if km_match else None

    return title, price, km

# ================= DEAL SCORING =================

def calculate_score(price, market_avg, km, title):
    score = 0

    margin = market_avg - price
    percent_below = margin / market_avg

    # winst
    if margin >= 1000:
        score += 30
    elif margin >= 700:
        score += 20

    # percentage onder markt
    if percent_below >= 0.25:
        score += 25
    elif percent_below >= 0.15:
        score += 15

    # lage km bonus
    if km and km < 120000:
        score += 10

    # motivatie woorden
    if contains_any(title, MOTIVATION_WORDS):
        score += 15

    return score

# ================= ENGINE =================

async def process_link(link, seen_links, market_cache, session):

    if link in seen_links:
        return

    html = await get_html(link, session)

    title, price, km = parse_marktplaats(html)

    if not title or not price or not km:
        return

    if price > MAX_PRICE or km > MAX_KM:
        return

    if contains_any(title, DEALER_WORDS):
        return

    if contains_any(title, BAD_WORDS):
        return

    model_keyword = next((m for m in MODELS if m in title.lower()), None)
    if not model_keyword:
        return

    market_avg = market_cache.get(model_keyword)
    if not market_avg:
        return

    margin = market_avg - price
    percent_below = margin / market_avg

    if margin < MIN_MARGIN:
        return

    if percent_below < MIN_PERCENT_BELOW_MARKET:
        return

    price_per_km = price / km
    if price_per_km > 0.05:
        return

    score = calculate_score(price, market_avg, km, title)

    if score >= 70:
        tag = "🔥 HOT DEAL"
    elif score >= 50:
        tag = "🟡 GOEDE DEAL"
    else:
        return

    message = (
        f"{tag} (Score: {score}/100)\n\n"
        f"🚗 {title}\n"
        f"💰 Vraagprijs: €{price}\n"
        f"📈 Marktwaarde: €{int(market_avg)}\n"
        f"💸 Winst: €{int(margin)} ({int(percent_below*100)}% onder markt)\n"
        f"📏 {km} km\n"
        f"💲 €{round(price_per_km,3)} per km\n"
        f"🔗 {link}"
    )

    await send_telegram(message)
    print("✅ DEAL:", link)

    seen_links.add(link)

# ================= MAIN LOOP =================

async def run():
    print("🚗 PRO SNIPER V6 - DEALER MODE")

    seen_links = load_seen()

    while True:
        try:
            async with aiohttp.ClientSession() as session:

                # market cache 1x per scan
                market_cache = {}
                for model in MODELS:
                    avg = await get_market_average(session, model)
                    if avg:
                        market_cache[model] = avg

                links = await scrape_marktplaats(session)

                tasks = [
                    process_link(link, seen_links, market_cache, session)
                    for link in links
                ]

                await asyncio.gather(*tasks)

            save_seen(seen_links)

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("Error:", e)
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(run())