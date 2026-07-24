import os
import asyncio
import aiohttp
import json
import re
import random
from telegram import Bot

# ================= CONFIG =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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

# ================= 