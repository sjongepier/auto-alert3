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
    check_interval: int = 300 
    seen_file: Path = Path("seen_links.json")

# ============================================================
# DE ULTIEME SCRAPER
# ============================================================
class ProfitScraper:
    def __init__(self, config: BotConfig):
        self.config = config
        self.seen_links = set()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9",
            "Referer": "https://www.google.com/",
        }

    async def fetch_links(self, model: str) -> Tuple[List[str], str]:
        # Zoeken in categorie 91 (Auto's) met sortering op 'Nieuwste'
        url = f"https://www.marktplaats.nl/l/auto-s/q/{quote(model)}/p/1/#Category:91|sortBy:OPTIMIZED|sortOrder:DECREASING"
        
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=20) as resp:
                    if resp.status != 200:
                        return [], f"Fout: Marktplaats gaf status {resp.status}"
                    
                    html = await resp.text()
                    
                    # Zoek naar de JSON data die Marktplaats altijd meestuurt
                    json_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
                    
                    found_urls = []
                    if json_match:
                        try:
                            data = json.loads(unescape(json_match.group(1)))
                            # We proberen op verschillende plekken in de JSON te kijken naar de advertenties
                            page_props = data.get('props', {}).get('pageProps', {})
                            listings = page_props.get('searchListingData', {}).get('listings', [])
                            
                            for item in listings:
                                # Pak alleen echte advertenties, geen 'gesponsorde'
                                if item.get('isVip', True) and 'vipUrl' in item:
                                    found_urls.append(f"https://www.marktplaats.nl{item['vipUrl']}")
                        except:
                            pass

                    # Fallback Regex als JSON faalt
                    if not found_urls:
                        links = re.findall(r'href="((?:/v/|/a/)[^"]+m\d{8,}[^"]*)"', html)
                        for l in links:
                            full = f"https://www.marktplaats.nl{l}"
                            if full not in found_urls: found_urls.append(full)

                    return list(dict.fromkeys(found_urls)), "Succes"
        except Exception as e:
            return [], f"Fout: {str(e)}"

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
                    title = ad.get('title', 'Auto')
                    
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

    async def perform_scan(self, context: ContextTypes.DEFAULT_TYPE, manual=False):
        if self.is_scanning:
            if manual: await context.bot.send_message(chat_id=self.config.chat_id, text="⚠️ Er is al een scan bezig...")
            return
        
        self.is_scanning = True
        models = ["Aygo", "Polo", "107", "C1", "Picanto", "Fiat 500"]
        
        p = Path("filters.json")
        if p.exists():
            try: models = json.loads(p.read_text()).get("models", models)
            except: pass

        if manual: await context.bot.send_message(chat_id=self.config.chat_id, text=f"🔎 Handmatige scan gestart voor {len(models)} modellen...")

        total_found = 0
        for model in models:
            links, status_msg = await self.scraper.fetch_links(model)
            if manual and not links:
                logger.info(f"Niks gevonden voor {model}: {status_msg}")

            for url in links[:8]:
                details = await self.scraper.get_details(url)
                if details:
                    total_found += 1
                    msg = (f"🚗 *Nieuwe Auto Gevonden!*\n\n"
                           f"📦 *{details['title']}*\n"
                           f"💰 Prijs: €{details['price']:,}\n"
                           f"📏 KM: {details['km']:,} km\n"
                           f"📅 Jaar: {details['year']}\n\n"
                           f"🔗 [Bekijk op Marktplaats]({details['url']})")
                    
                    await context.bot.send_message(chat_id=self.config.chat_id, text=msg, parse_mode=constants.ParseMode.MARKDOWN)
                    await asyncio.sleep(1)
            await asyncio.sleep(2)

        if manual:
            await context.bot.send_message(chat_id=self.config.chat_id, text=f"✅ Scan klaar! Totaal {total_found} nieuwe auto's gestuurd.")
        
        self.is_scanning = False

    async def scan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.perform_scan(context, manual=True)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💰 ProfitBot Online! Gebruik /scan voor een directe check.")

    def run(self):
        app = ApplicationBuilder().token(self.config.token).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CommandHandler("scan", self.scan_command))
        
        # Automatische scan
        app.job_queue.run_repeating(lambda ctx: self.perform_scan(ctx), interval=self.config.check_interval, first=10)
        
        logger.info("🤖 Bot draait. Gebruik /scan in Telegram.")
        app.run_polling()

if __name__ == "__main__":
    ProfitBot().run()