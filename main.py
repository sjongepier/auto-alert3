import os
import asyncio
import aiohttp
import json
import re
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ================== CONFIG ==================

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CONFIG_FILE = "config.json"
SEEN_FILE = "seen.json"

DEFAULT_CONFIG = {
    "max_price": 8000,
    "min_margin": 500,
    "min_percent": 0.08,
    "models": ["aygo", "c1", "107", "up", "polo"],
    "active": True
}

CHECK_INTERVAL = 180

# ================== STORAGE ==================

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except:
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

config = load_config()
seen_links = load_seen()

# ================== SCRAPING ==================

async def get_html(url, session):
    try:
        async with session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as response:
            if response.status != 200:
                return None
            return await response.text()
    except:
        return None

async def get_market_average(session, model):
    url = f"https://www.marktplaats.nl/l/auto-s/q/{model}/"
    html = await get_html(url, session)
    if not html:
        return None

    prices = re.findall(r'"price":\s*"(\d+)"', html)
    prices = [int(p) for p in prices if int(p) < 25000]

    if len(prices) < 5:
        return None

    prices = prices[:20]
    return sum(prices) / len(prices)

async def scrape_model(session, model):
    url = f"https://www.marktplaats.nl/l/auto-s/q/{model}/?sortBy=SORT_INDEX&sortOrder=DECREASING"
    html = await get_html(url, session)
    if not html:
        return []

    links = re.findall(r'href="(/v/auto[^"]+)"', html)

    return list(set([
        "https://www.marktplaats.nl" + l
        for l in links
    ]))[:15]

def parse_listing(html):
    if not html:
        return None, None

    title_match = re.search(r'<title>(.*?)</title>', html)
    title = title_match.group(1) if title_match else None

    price_match = re.search(r'"price":\s*"(\d+)"', html)
    price = int(price_match.group(1)) if price_match else None

    return title, price

# ================== DEAL ENGINE ==================

async def deal_loop(application):

    await application.bot.send_message(
        chat_id=CHAT_ID,
        text="✅ Dealer Bot actief."
    )

    async with aiohttp.ClientSession() as session:

        while True:

            if not config["active"]:
                await asyncio.sleep(5)
                continue

            market_cache = {}

            for model in config["models"]:
                avg = await get_market_average(session, model)
                if avg:
                    market_cache[model] = avg

            for model in config["models"]:

                links = await scrape_model(session, model)

                for link in links:

                    if link in seen_links:
                        continue

                    html = await get_html(link, session)
                    title, price = parse_listing(html)

                    if not title or not price:
                        continue