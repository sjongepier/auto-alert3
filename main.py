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

# ============================================================
# CONFIG
# ============================================================
@dataclass
class BotConfig:
    token: str = os.getenv("TELEGRAM_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    groq_key: str = os.getenv("GROQ_API_KEY", "")
    check_interval: int = 120 # 2 minuten is veiliger
    seen_file: Path = Path("seen_links.json")

# ============================================================
# SCRAPER LOGIC (Verbeterd)
# ============================================================
class ProfitScraper:
    def __init__(self, config: BotConfig):
        self.config = config
        self.seen_links = set()
        # Meer realistische browser headers
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    async def fetch_links(self, model: str) -> List[str]:
        # We zoeken specifiek in de categorie Auto's (c91) voor betere resultaten
        url = f"https://www.marktplaats.nl/q/{quote(model)}/"
        logger.info(f"🔎 Zoeken naar {model} op Marktplaats...")
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status != 200:
                        logger.error(f"❌ Marktplaats error {resp.status}")
                        return []
                    
                    html = await resp.text()
                    
                    # VERBETERDE REGEX: Zoekt naar zowel /v/ als /a/ links met een m-nummer
                    # Dit zijn de standaard patronen voor Marktplaats advertenties
                    links = re.findall(r'href="((?:/v/|/a/)[^"]+m\d{8,}[^"]*)"', html)
                    
                    full_links = []
                    for l in links:
                        full_url = f"https://www.marktplaats.nl{l}"
                        if full_url not in full_links:
                            full_links.append(full_url)
                    
                    logger.info(f"✅ {len(full_links)} advertenties gevonden voor {model}")
                    return full_links
        except Exception as e:
            logger.error(f"❌ Fetch error: {e}")
        return []

    async def get_details(self, url: str) -> Optional[Dict]:
        if url in self.seen_links:
            return None
        self.seen_links.add(url)
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    html = await resp.text()
                    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
                    if not match: return None
                    
                    data = json.loads(unescape(match.group(1)))
                    ad = data['props']['pageProps']['ad']
                    
                    # Prijs uit de JSON halen
                    price = 0
                    if 'priceInfo' in ad and ad['priceInfo'].get('priceCents'):
                        price = int(ad['priceInfo']['priceCents'] // 100)
                    
                    title = ad.get('title', 'Geen titel')
                    km, year = 0, 0
                    
                    # Attributen (KM en Bouwjaar)
                    for attr in ad.get('attributes', []):
                        tag = attr.get('tag')
                        val = str(attr.get('value', '0'))
                        if tag == 'mileage':
                            km = int(re.sub(r"\D", "", val))
                        elif tag == 'constructionYear':
                            year = int(re.sub(r"\D", "", val))
                    
                    return {"url": url, "title": title, "price": price, "km": km, "year": year}
        except:
            pass
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
        
        # Modellen laden uit filters.json
        models = ["Toyota Aygo", "Volkswagen Polo"]
        p = Path("filters.json")
        if p.exists():
            try:
                models = json.loads(p.read_text()).get("models", models)
            except: pass

        for model in models:
            links = await self.scraper.fetch_links(model)
            # Pak de eerste 5 resultaten per model om te testen
            for url in links[:5]:
                details = await self.scraper.get_details(url)
                if details:
                    # Bericht opbouwen
                    msg = (f"🚗 *Nieuwe advertentie gevonden!*\n\n"
                           f"📦 *{details['title']}*\n"
                           f"💰 Prijs: €{details['price']:,}\n"
                           f"📏 KM stand: {details['km']:,} km\n"
                           f"📅 Bouwjaar: {details['year']}\n\n"
                           f"🔗 [Bekijk op Marktplaats]({details['url']})")
                    
                    try:
                        await context.bot.send_message(
                            chat_id=self.config.chat_id,
                            text=msg,
                            parse_mode=constants.ParseMode.MARKDOWN,
                            disable_web_page_preview=False
                        )
                    except Exception as e:
                        logger.error(f"Telegram error: {e}")
                    
                    await asyncio.sleep(1) # Even wachten tussen berichten
            await asyncio.sleep(2) # Wacht tussen modellen

        self.is_scanning = False

    def run(self):
        app = ApplicationBuilder().token(self.config.token).build()
        
        # Start de herhalende taak (elke 2 minuten)
        app.job_queue.run_repeating(self.run_scan, interval=self.config.check_interval, first=1)
        
        logger.info("🚀 Bot is gestart en zoekt nu... Check je Telegram!")
        app.run_polling()

if __name__ == "__main__":
    ProfitBot().run()