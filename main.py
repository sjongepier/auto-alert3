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
    filter_file: Path = Path("filters.json")

class ProfitScraper:
    def __init__(self, config: BotConfig):
        self.config = config
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9"
        }

    async def fetch_links(self, model: str) -> List[str]:
        url = f"https://www.marktplaats.nl/q/{quote(model)}/"
        links = []
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    html = await resp.text()
                    # We zoeken naar advertentie links
                    found = re.findall(r'href="((?:/v/|/a/)[^"]+m\d{8,}[^"]*)"', html)
                    for l in found:
                        full = f"https://www.marktplaats.nl{l}"
                        if full not in links: links.append(full)
        except: pass
        return links

    async def get_details(self, url: str) -> Optional[Dict]:
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200: return None
                    html = await resp.text()
                    
                    # We gebruiken Meta Tags (die Marktplaats voor Google/Facebook gebruikt)
                    # Deze zijn veel stabieler dan de interne JSON
                    title_match = re.search(r'property="og:title" content="(.*?)"', html)
                    price_match = re.search(r'property="product:price:amount" content="(.*?)"', html)
                    
                    if not title_match: # Fallback voor titel
                        title_match = re.search(r'<title>(.*?)</title>', html)

                    title = unescape(title_match.group(1)) if title_match else "Auto"
                    
                    # Prijs extractor
                    price = 0
                    if price_match:
                        price = int(float(price_match.group(1)))
                    else:
                        # Zoek naar prijs in de tekst als de meta tag mist
                        p_match = re.search(r'€\s?([0-9.,]+)', html)
                        if p_match:
                            price = int(p_match.group(1).replace('.', '').replace(',', ''))

                    return {
                        "url": url,
                        "title": title[:60],
                        "price": price
                    }
        except Exception as e:
            logger.error(f"Fout bij {url}: {e}")
            return None

class ProfitBot:
    def __init__(self):
        self.config = BotConfig()
        self.scraper = ProfitScraper(self.config)
        self.is_scanning = False

    async def perform_scan(self, context: ContextTypes.DEFAULT_TYPE, manual=False):
        if self.is_scanning: return
        self.is_scanning = True
        
        # Modellen uit filters laden
        models = ["aygo"]
        if self.config.filter_file.exists():
            models = json.loads(self.config.filter_file.read_text()).get("models", models)

        if manual: await context.bot.send_message(self.config.chat_id, "🔍 Scan gestart...")

        total_links = 0
        sent_count = 0

        for model in models:
            links = await self.scraper.fetch_links(model)
            total_links += len(links)
            
            # Check de eerste 3 van elk model
            for url in links[:3]:
                details = await self.scraper.get_details(url)
                if details and details['price'] > 100:
                    sent_count += 1
                    msg = (f"🚗 *AUTO GEVONDEN*\n\n"
                           f"📦 {details['title']}\n"
                           f"💰 Prijs: €{details['price']:,}\n\n"
                           f"🔗 [Bekijk op Marktplaats]({details['url']})")
                    await context.bot.send_message(self.config.chat_id, msg, parse_mode=constants.ParseMode.MARKDOWN)
                    await asyncio.sleep(1.5)
            await asyncio.sleep(2)

        if manual:
            await context.bot.send_message(
                self.config.chat_id, 
                f"✅ Scan voltooid!\n- Links gevonden: {total_links}\n- Berichten gestuurd: {sent_count}"
            )
        self.is_scanning = False

    async def scan_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.perform_scan(context, manual=True)

    def run(self):
        app = ApplicationBuilder().token(self.config.token).build()
        app.add_handler(CommandHandler("scan", self.scan_cmd))
        app.job_queue.run_repeating(lambda ctx: self.perform_scan(ctx), interval=self.config.check_interval, first=5)
        app.run_polling()

if __name__ == "__main__":
    ProfitBot().run()