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
# LOGGING
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
    groq_key: str = os.getenv("GROQ_API_KEY", "")
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "300"))
    filter_file: Path = Path("filters.json")
    seen_file: Path = Path("seen_links.json")

# ============================================================
# AI EXPERT (GROQ)
# ============================================================
class ProfitAI:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = "https://api.groq.com/openai/v1/chat/completions"

    async def evaluate_deal(self, title: str, price: int, desc: str) -> str:
        if not self.api_key: return "AI niet ingesteld."
        
        prompt = (
            f"Ik ben een autohandelaar met een eigen monteur en oprijwagen. "
            f"Ik zoek auto's met winstpotentie (fix & flip). "
            f"Auto: {title}\nPrijs: €{price}\nBeschrijving: {desc[:500]}\n\n"
            f"Analyseer dit: Is dit een goede deal voor iemand die zelf kan repareren? "
            f"Schat de potentiële winst en geef 2 korte tips. Antwoord in het Nederlands."
        )
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
                payload = {
                    "model": "llama-3.1-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.5,
                    "max_tokens": 200
                }
                async with session.post(self.url, json=payload, headers=headers, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content'].strip()
        except: pass
        return "Geen AI oordeel beschikbaar."

# ============================================================
# SCRAPER
# ============================================================
class ProfitScraper:
    def __init__(self, config: BotConfig):
        self.config = config
        self.filters = self._load_filters()
        self.seen_links = self._load_seen()
        self.headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"}

    def _load_filters(self):
        if self.config.filter_file.exists():
            return json.loads(self.config.filter_file.read_text())
        return {"models": ["aygo"], "opportunity_keywords": [], "exclude_keywords": []}

    def _load_seen(self) -> Set[str]:
        if self.config.seen_file.exists():
            try: return set(json.loads(self.config.seen_file.read_text()))
            except: pass
        return set()

    def _save_seen(self):
        links = list(self.seen_links)[-1500:]
        self.config.seen_file.write_text(json.dumps(links))

    async def fetch_links(self, model: str) -> List[str]:
        url = f"https://www.marktplaats.nl/q/{quote(model)}/"
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    html = await resp.text()
                    return [f"https://www.marktplaats.nl{l}" for l in re.findall(r'href="((?:/v/|/a/)[^"]+m\d{8,}[^"]*)"', html)]
        except: return []

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
                    
                    title = ad.get('title', '')
                    desc = ad.get('description', '').lower()
                    price = int(ad.get('priceInfo', {}).get('priceCents', 0) // 100)
                    
                    if price < 250: return None
                    
                    # KRITISCHE FILTERING
                    text_blob = (title + " " + desc).lower()
                    
                    # 1. Uitsluiten (bv Watershade)
                    if any(x in text_blob for x in self.filters['exclude_keywords']):
                        return None
                    
                    # 2. Score berekenen op basis van kansen
                    score = 0
                    found_ops = []
                    for op in self.filters['opportunity_keywords']:
                        if op in text_blob:
                            score += 25
                            found_ops.append(op)
                    
                    # Als het een goedkope auto is met een technisch probleem = Kassa!
                    if score >= 25 or (price < 1500 and "moet weg" in text_blob):
                        return {
                            "url": url, "title": title, "price": price, 
                            "desc": desc, "ops": found_ops, "score": score
                        }
        except: pass
        return None

# ============================================================
# BOT
# ============================================================
class ProfitBot:
    def __init__(self):
        self.config = BotConfig()
        self.scraper = ProfitScraper(self.config)
        self.ai = ProfitAI(self.config.groq_key)
        self.is_scanning = False

    async def perform_scan(self, context: ContextTypes.DEFAULT_TYPE, manual=False):
        if self.is_scanning: return
        self.is_scanning = True
        
        models = self.scraper.filters['models']
        if manual: await context.bot.send_message(self.config.chat_id, "🔧 Handelaar-scan gestart. Ik zoek naar projecten met marge...")

        for model in models:
            links = await self.scraper.fetch_links(model)
            for url in links[:12]:
                details = await self.scraper.get_details(url)
                if details:
                    # AI Analyse
                    ai_advice = await self.ai.evaluate_deal(details['title'], details['price'], details['desc'])
                    
                    opps = ", ".join(details['ops'])
                    msg = (f"🛠️ *PROJECT GEVONDEN* 🛠️\n\n"
                           f"🚘 *{details['title']}*\n"
                           f"💰 Prijs: €{details['price']:,}\n"
                           f"⚠️ Gevonden issues: `{opps}`\n\n"
                           f"🤖 *AI ADVIES:*\n{ai_advice}\n\n"
                           f"🔗 [Sla je slag op Marktplaats]({details['url']})")
                    
                    await context.bot.send_message(self.config.chat_id, msg, parse_mode=constants.ParseMode.MARKDOWN)
                    await asyncio.sleep(2)
            await asyncio.sleep(2)

        self.scraper._save_seen()
        self.is_scanning = False

    def run(self):
        app = ApplicationBuilder().token(self.config.token).build()
        app.add_handler(CommandHandler("scan", lambda u, c: self.perform_scan(c, True)))
        app.job_queue.run_repeating(lambda ctx: self.perform_scan(ctx), interval=self.config.check_interval, first=10)
        app.run_polling()

if __name__ == "__main__":
    ProfitBot().run()