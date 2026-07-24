import os
import asyncio
import aiohttp
import json
import re
from telegram import Bot

# ================= CONFIG =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 180
MAX_PRICE = 5000
MAX_KM = 150000
MIN_MARGIN = 1000   # ✅ Minimale winst

MODELS = ["aygo", "c1", "107", "up", "polo"]
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

def is_target_model(title):
    title = title.lower()
    for model in MODELS:
        if re.search(rf"\b{model}\b", title):
            return True
    return False

async def send_telegram(message):
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)

async def get_html(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=15) as response:
            return await response.text()

# ================= MARKET AVERAGE (CACHED) =================