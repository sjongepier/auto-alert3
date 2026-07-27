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
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "300"))
    seen_file: Path = Path("seen_links.json")
    filter_file: Path = Path("filters.json")

class ProfitScraper:
    def __init__(self, config: BotConfig):
        self.config = config
        self.seen_links = set()
        self.filters = self._load_filters()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9"
        }

    def _load_filters(self) -> Dict:
        if self.config.filter_file.exists():
            return json.loads(self.config.filter_file.read_text())
        return {"models": ["aygo"]}

    async def fetch_links(self, model: str) -> List[str]:
        # We proberen de 'Lijst' weergave, die is makkelijker te lezen voor bots
        url = f"https://www.marktplaats.nl/q/{quote(model)}/"
        links = []
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    html = await resp.text()
                    # Zoek naar alle advertentie-links
                    found = re.findall(r'href="((?:/v/|/a/)[^"]+m\d{8,}[^"]*)"', html)
                    for l in found:
                        full = f"https://www.marktplaats.nl{l}"
                        if full not in links: links.append(full)
        except Exception as e:
            logger.error(f"Fetch error: {e}")
        return links

    async def get_details(self, url: str) -> Optional[Dict]:
        # We negeren even de 'seen_links' voor de test, zodat we ALTIJD resultaat zien
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=10) as resp:
                    html = await resp.text()
                    json_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
                    if not json_match: return None
                    data = json.loads(unescape(json_match.group(1)))
                    ad = data['props']['pageProps']['ad']
                    
                    return {
                        "url": url,
                        "title": ad.get('title', 'Geen titel'),
                        "price": int(ad.get('priceInfo', {}).get('priceCents', 0) // 100),
                        "km": 0, "year": 0 # Voor test even simpel
                    }
        except: return None

class ProfitBot:
    def __init__(self):
        self.config = BotConfig()
        self.scraper = ProfitScraper(self.config)
        self.is_scanning = False

    async def perform_scan(self, context: ContextTypes.DEFAULT_TYPE, manual=False):
        if self.is_scanning: return
        self.is_scanning = True
        
        models = self.scraper.filters.get('models', ["aygo"])
        total_checked = 0
        total_sent = 0

        if manual: await context.bot.send_message(self.config.chat_id, f"🔍 Test-scan gestart voor: {', '.join(models)}")

        for model in models:
            links = await self.scraper.fetch_links(model)
            total_checked += len(links)
            
            for url in links[:3]: # Check er maar 3 per model voor de test
                details = await self.scraper.get_details(url)
                if details:
                    total_sent += 1
                    msg = (f"🧪 *TEST RESULTAAT*\n\n🚘 {details['title']}\n💰 €{details['price']}\n🔗 [Link]({details['url']})")
                    await context.bot.send_message(self.config.chat_id, msg, parse_mode=constants.ParseMode.MARKDOWN)
                    await asyncio.sleep(1)
            
        if manual:
            await context.bot.send_message(
                self.config.chat_id, 
                f"📊 *Scan Rapport:*\n- Links gevonden op Marktplaats: {total_checked}\n- Berichten gestuurd: {total_sent}\n\nAls 'Links gevonden' 0 is, blokkeert Marktplaats ons IP."
            )
        
        self.is_scanning = False

    async def scan_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.perform_scan(context, manual=True)

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Bot is online. Gebruik /scan voor de test.")

    def run(self):
        app = ApplicationBuilder().token(self.config.token).build()
        app.add_handler(CommandHandler("start", self.start_cmd))
        app.add_handler(CommandHandler("scan", self.scan_cmd))
        app.run_polling()

if __name__ == "__main__":
    ProfitBot().run()