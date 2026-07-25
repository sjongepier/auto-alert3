import os
import asyncio
import aiohttp
import json
import re
import random
from telegram import Bot

print("🚀 SCRIPT STARTED", flush=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("❌ TELEGRAM ENV VARS MISSING!", flush=True)

CHECK_INTERVAL = 180
MAX_PRICE = 8000
MAX_KM = 200000
MIN_MARGIN = 400
MIN_PERCENT_BELOW_MARKET = 0.08

MODELS = [
    "aygo", "c1", "107", "up", "polo",
    "fiesta", "clio", "yaris",
    "208", "micra", "ibiza"
]

SEEN_FILE = "seen.json"

bot = Bot(token=TELEGRAM_TOKEN)
semaphore = asyncio.Semaphore(5)

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

async def get_html(url, session):
    try:
        async with semaphore:
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as response:

                if response.status != 200:
                    print("⚠️ Bad status:", response.status, flush=True)
                    return None

                return await response.text()

    except Exception as e:
        print("❌ Request error:", e, flush=True)
        return None

async def get_market_average(session, model):
    print("📊 Getting market for:", model, flush=True)
    url = f"https://www.marktplaats.nl/l/auto-s/q/{model}/"
    html = await get_html(url, session)

    if not html:
        return None

    prices = re.findall(r'"price":\s*"(\d+)"', html)
    prices = [int(p) for p in prices if int(p) < 25000]

    if len(prices) < 5:
        return None

    return sum(prices[:20]) / len(prices[:20])

async def scrape_marktplaats(session):
    print("🔎 Scraping marktplaats...", flush=True)
    url = "https://www.marktplaats.nl/l/auto-s/"
    html = await get_html(url, session)

    if not html:
        return []

    links = re.findall(r'href="(/v/auto[^"]+)"', html)

    links = list(set([
        "https://www.marktplaats.nl" + link
        for link in links
    ]))[:30]

    print("✅ Found links:", len(links), flush=True)
    return links

def parse_listing(html):
    if not html:
        return None, None, None

    title_match = re.search(r'<title>(.*?)</title>', html)
    title = title_match.group(1) if title_match else None

    price_match = re.search(r'"price":\s*"(\d+)"', html)
    price = int(price_match.group(1)) if price_match else None

    km_match = re.search(r'"mileageFromOdometer":\s*{\s*"value":\s*(\d+)', html)
    km = int(km_match.group(1)) if km_match else None

    return title, price, km

async def process_link(link, seen_links, market_cache, session):
    if link in seen_links:
        return

    html = await get_html(link, session)
    title, price, km = parse_listing(html)

    if not title or not price or not km:
        return

    model = next((m for m in MODELS if m in title.lower()), None)
    if not model:
        return

    market_avg = market_cache.get(model)
    if not market_avg:
        return

    margin = market_avg - price
    percent = margin / market_avg

    if margin < MIN_MARGIN or percent < MIN_PERCENT_BELOW_MARKET:
        return

    print("🔥 DEAL FOUND:", title, flush=True)

    message = (
        f"🚗 {title}\n"
        f"💰 €{price}\n"
        f"📈 Markt €{int(market_avg)}\n"
        f"💸 Winst €{int(margin)}\n"
        f"🔗 {link}"
    )

    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
    seen_links.add(link)

async def run():
    print("🚗 BOT RUNNING", flush=True)

    seen_links = load_seen()

    async with aiohttp.ClientSession() as session:
        while True:
            print("🔄 New scan cycle", flush=True)

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

if __name__ == "__main__":
    asyncio.run(run())