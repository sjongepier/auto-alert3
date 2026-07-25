import os
import asyncio
import aiohttp
import json
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

print("BOT STARTING...", flush=True)

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