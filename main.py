import os
import json
import logging
import asyncio
import aiohttp
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any, Set
from datetime import datetime
from html import unescape
from urllib.parse import quote

from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram import Update, constants

# ============================================================
# LOGGING SETUP (Heel uitgebreid voor testen)
# ============================================================
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TestBot")

# ============================================================
# CONFIG
# ============================================================
@dataclass
class BotConfig:
    token: str = os.getenv("TELEGRAM_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    groq_key: str = os.getenv("GROQ_API_KEY", "")
    # Test interval: 60 seconden
    check_interval: int = 60 
    seen_file: Path = Path("seen_links.json")

# ============================================================
# SCRAPER LOGIC
# ============================================================
class ProfitScraper:
    def __init__(self, config: BotConfig):
        self.config = config
        self.seen_links = set()
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"}

    async def fetch_links(self, model: str) -> List[str]:
        url = f"https://www.marktplaats.nl/q/{quote(model)}/"
        logger.info(f"🔎 Zoeken naar: {model}...")
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status != 200:
                        logger.error(f"❌ Marktplaats fout: {resp.status}")
                        return []
                    html = await resp.text()
                    links = re.findall(r'href="(/a/[^"]+/m\d+[^"]*)"', html)
                    full_links = list(dict.fromkeys([f"https://www.marktplaats.nl{l}" for l in links]))
                    logger.info(f"✅ {len(full_links)} links gevonden voor {model}")
                    return full_links
        except Exception as e:
            logger.error(f"❌ Fetch error: {e}")
        return []

    async def get_details(self, url: str) -> Optional[Dict]:
        if url in self.seen_links:
            return None
        
        self.seen_links.add(url)
        logger.info(f"📄 Advertentie checken: {url}")
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    html = await resp.text()
                    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
                    if not match:
                        logger.warning("⚠️ Geen data gevonden in advertentie (NEXT_DATA missing)")
                        return None
                    
                    data = json.loads(unescape(match.group(1)))
                    ad = data['props']['pageProps']['ad']
                    
                    price = int(ad.get('priceInfo', {}).get('priceCents', 0) // 100)
                    title = ad.get('title', 'Geen titel')
                    
                    km, year = 0, 0
                    for attr in ad.get('attributes', []):
                        if attr.get('tag') == 'mileage': km = int(re.sub(r"\D", "", str(attr.get('value', '0'))))
                        if attr.get('tag') == 'constructionYear': year = int(re.sub(r"\D", "", str(attr.get('value', '0'))))
                    
                    return {"url": url, "title": title, "price": price, "km": km, "year": year}
        except Exception as e:
            logger.error(f"❌ Detail error op {url}: {e}")
        return None

# ============================================================
# BOT ENGINE
# ============================================================
class ProfitBot:
    def __init__(self):
        self.config = BotConfig()
        self.scraper = ProfitScraper(self.config)
        self.is_scanning = False 

    async def run_scan(self, context: ContextTypes.DEFAULT_TYPE):
        if self.is_scanning: return
        self.is_scanning = True
        
        logger.info("🚀 TEST SCAN START...")
        # Fallback modellen als filters.json niet werkt
        models = ["Toyota Aygo", "Volkswagen Polo"]
        
        # Probeer filters.json te laden
        if Path("filters.json").exists():
            try:
                models = json.loads(Path("filters.json").read_text()).get("models", models)
            except: pass

        for model in models:
            links = await self.scraper.fetch_links(model)
            for url in links[:5]: # Test alleen de eerste 5
                details = await self.scraper.get_details(url)
                if details:
                    logger.info(f"✨ Deal gevonden! Verzenden naar Telegram...")
                    msg = (f"🧪 *TEST RESULTAAT*\n\n"
                           f"🚘 *{details['title']}*\n"
                           f"💰 Prijs: €{details['price']:,}\n"
                           f"📏 KM: {details['km']:,}\n"
                           f"📅 Jaar: {details['year']}\n\n"
                           f"🔗 [Link]({details['url']})")
                    
                    try:
                        await context.bot.send_message(
                            chat_id=self.config.chat_id, 
                            text=msg, 
                            parse_mode=constants.ParseMode.MARKDOWN
                        )
                        logger.info("✉️ Bericht succesvol verzonden!")
                    except Exception as e:
                        logger.error(f"❌ Telegram verzendfout: {e}")
                    
                    await asyncio.sleep(2)
            await asyncio.sleep(3)

        self.is_scanning = False
        logger.info("✅ TEST SCAN KLAAR. Wachten op volgende ronde...")

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Bot leeft! De test-scan draait elke minuut.")

    def run(self):
        if not self.config.token or not self.config.chat_id:
            logger.critical("❌ TOKEN of CHAT_ID mist in Railway Variables!")
            return
            
        app = ApplicationBuilder().token(self.config.token).build()
        app.add_handler(CommandHandler("start", self.start_cmd))

        # Start elke 60 seconden, eerste keer na 1 seconde
        app.job_queue.run_repeating(self.run_scan, interval=60, first=1)

        logger.info("🤖 Bot is opgestart en gaat nu scannen...")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    ProfitBot().run()