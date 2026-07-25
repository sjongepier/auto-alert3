import os
import asyncio
import aiohttp
import json
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

print("🚀 BOT STARTING...", flush=True)

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

# ================= STORAGE =================

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

# ================= SCRAPING =================

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

    return sum(prices[:20]) / len(prices[:20])

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

# ================= DEAL LOOP =================

async def deal_loop(application):
    print("✅ DEAL LOOP STARTED", flush=True)

    await application.bot.send_message(
        chat_id=CHAT_ID,
        text="✅ Dealer Bot actief."
    )

    async with aiohttp.ClientSession() as session:

        while True:

            if not config["active"]:
                await asyncio.sleep(5)
                continue

            print("🔎 Nieuwe scan...", flush=True)

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

                    if price > config["max_price"]:
                        continue

                    market_avg = market_cache.get(model)
                    if not market_avg:
                        continue

                    margin = market_avg - price
                    percent = margin / market_avg

                    if margin < config["min_margin"]:
                        continue

                    if percent < config["min_percent"]:
                        continue

                    tag = "🔥 SNIPER" if margin >= 1000 else "💰 FLIP"

                    message = (
                        f"{tag}\n\n"
                        f"🚗 {title}\n"
                        f"💰 €{price}\n"
                        f"📈 Markt €{int(market_avg)}\n"
                        f"💸 Winst €{int(margin)}\n"
                        f"🔗 {link}"
                    )

                    await application.bot.send_message(
                        chat_id=CHAT_ID,
                        text=message
                    )

                    seen_links.add(link)
                    save_seen(seen_links)

            await asyncio.sleep(CHECK_INTERVAL)

# ================= COMMANDS =================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"📊 INSTELLINGEN\n\n"
        f"Max prijs: €{config['max_price']}\n"
        f"Min marge: €{config['min_margin']}\n"
        f"Min %: {int(config['min_percent']*100)}%\n"
        f"Modellen: {', '.join(config['models'])}\n"
        f"Status: {'Actief' if config['active'] else 'Gepauzeerd'}"
    )
    await update.message.reply_text(msg)

async def margin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(context.args[0])
        config["min_margin"] = value
        save_config(config)
        await update.message.reply_text(f"✅ 