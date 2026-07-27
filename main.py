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
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ProfitBot")

@dataclass
class BotConfig:
    token: str = os.getenv("TELEGRAM_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    check_interval: int = 300 # 5 minuten is beter voor stabiliteit
    seen_file: Path = Path("seen_links.json")

# ============================================================
# DE NIEUWE SCRAPER (ZEER ROBUUST)
# ============================================================
class ProfitScraper:
    def __init__(self, config: BotConfig):
        self.config = config
        self.seen_links = set()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9",
        }

    async def fetch_links(self, model: str) -> List[str]:
        # We zoeken specifiek in de categorie Auto's (c91)
        url = f"https://www.marktplaats.nl/q/{quote(model)}/#Category:91"
        logger.info(f"🔎 Scannen van Marktplaats voor: {model}")
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=20) as resp:
                    if resp.status != 200:
                        return []
                    html = await resp.text()
                    
                    # Methode 1: Zoek in de JSON data (het meest betrouwbaar)
                    json_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
                    found_urls = []
                    
                    if json_match:
                        try:
                            data = json.loads(unescape(json_match.group(1)))
                            # Duik diep in de JSON structuur van de zoekresultaten
                            listings = data.get('props', {}).get('pageProps', {}).get('searchListingData', {}).get('listings', [])
                            for item in listings:
                                if 'vipUrl' in item:
                                    found_urls.append(f"https://www.marktplaats.nl{item['vipUrl']}")
                        except Exception as e:
                            logger.error(f"JSON Parse error: {e}")

                    # Methode 2 Fallback: Regex voor /v/ of /a/ links
                    if not found_urls:
                        regex_links = re.findall(r'href="((?:/v/|/a/)[^"]+m\d{8,}[^"]*)"', html)
                        for l in regex_links:
                            url = f"https://www.marktplaats.nl{l}"
                            if url not in found_urls:
                                found_urls.append(url)

                    return list(dict.fromkeys(found_urls)) # Verwijder dubbele
        except Exception as e:
            logger.error(f"Netwerkfout: {e}")
            return []

    async def get_details(self, url: str) -> Optional[Dict]:
        if url in self.seen_links: return None
        self.seen_links.add(url)
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    html = await resp.text()
                    json_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
                    if not json_match: return None
                    
                    data = json.loads(unescape(json_match.group(1)))
                    ad = data['props']['pageProps']['ad']
                    
                    price = int(ad.get('priceInfo', {}).get('priceCents', 0) // 100)
                    title = ad.get('title', 'Onbekende auto')
                    
                    km, year = 0, 0
                    for attr in ad.get('attributes', []):
                        if attr.get('tag') == 'mileage':
                            km = int(re.sub(r"\D", "", str(attr.get('value', '0'))))
                        if attr.get('tag') == 'constructionYear':
                            year = int(re.sub(r"\D", "", str(attr.get('value', '0'))))
                    
                    return {"url": url, "title": title, "price": price, "km": km, "year": year}
        except:
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
        
        # Testbericht: zo weet je dat hij begint
        # await context.bot.send_message(chat_id=self.config.chat_id, text="🔄 Scan gestart...")

        models = ["Toyota Aygo", "Volkswagen Polo", "Peugeot 107", "Citroen C1"]
        p = Path("filters.json")
        if p.exists():
            try:
                models = json.loads(p.read_text()).get("models", models)
            except: pass

        found_anything = False
        for model in models:
            links = await self.scraper.fetch_links(model)
            logger.info(f"Resultaat voor {model}: {len(links)} advertenties.")
            
            for url in links[:10]:
                details = await self.scraper.get_details(url)
                if details:
                    found_anything = True
                    msg = (f"🚗 *Nieuwe Auto Gevonden!*\n\n"
                           f"📦 *{details['title']}*\n"
                           f"💰 Prijs: €{details['price']:,}\n"
                           f"📏 KM: {details['km']:,} km\n"
                           f"📅 Jaar: {details['year']}\n\n"
                           f"🔗 [Bekijk op Marktplaats]({details['url']})")
                    
                    await context.bot.send_message(chat_id=self.config.chat_id, text=msg, parse_mode=constants.ParseMode.MARKDOWN)
                    await asyncio.sleep(1)
            await asyncio.sleep(2)

        self.is_scanning = False

    async def test_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Verbinding met Telegram is OK! Wacht op de volgende scan.")

    def run(self):
        if not self.config.token or not self.config.chat_id:
            logger.error("TOKEN of CHAT_ID ontbreekt in Railway!")
            return

        app = ApplicationBuilder().token(self.config.token).build()
        app.add_handler(CommandHandler("test", self.test_cmd))
        
        # Scan elke X seconden (volgens config)
        app.job_queue.run_repeating(self.run_scan, interval=self.config.check_interval, first=5)
        
        logger.info("🚀 Bot draait. Gebruik /test in Telegram om te checken.")
        app.run_polling()

if __name__ == "__main__":
    ProfitBot().run()