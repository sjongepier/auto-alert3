import os
import json
import logging
import asyncio
import aiohttp
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from datetime import datetime
from zoneinfo import ZoneInfo
from html import unescape
from urllib.parse import quote, urlparse

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from telegram import Update, constants

# ============================================================
# LOGGING SETUP
# ============================================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("ProfitBot")

# ============================================================
# CONFIG & SETTINGS
# ============================================================
@dataclass
class BotConfig:
    token: str = os.getenv("TELEGRAM_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    groq_key: str = os.getenv("GROQ_API_KEY", "")
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "120"))
    min_score: int = 50  # Minimale score voor een deal
    seen_file: Path = Path("seen_links.json")

    def __post_init__(self):
        if not self.token or not self.chat_id:
            logger.error("❌ TELEGRAM_TOKEN of TELEGRAM_CHAT_ID mist in env!")

class SettingsManager:
    def __init__(self, config: BotConfig):
        self.config = config
        self.file = Path("runtime_settings.json")
        self.data = self._load()

    def _load(self):
        if self.file.exists():
            return json.loads(self.file.read_text())
        return {"paused": False, "min_score": self.config.min_score}

    def save(self):
        self.file.write_text(json.dumps(self.data))

# ============================================================
# AI CLIENT (Groq/OpenAI)
# ============================================================
class AIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = "https://api.groq.com/openai/v1/chat/completions"
        self.model = "llama-3.1-70b-versatile"

    async def analyze_deal(self, title: str, price: int, km: int, year: int) -> str:
        if not self.api_key: return "AI niet geconfigureerd."
        prompt = f"Auto: {title}, Prijs: €{price}, KM: {km}, Jaar: {year}. Is dit een goede handelsdeal voor wederverkoop in Nederland? Antwoord kort in 2 zinnen."
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
                payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.5}
                async with session.post(self.url, headers=headers, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content']
        except Exception as e:
            return f"AI Error: {str(e)}"
        return "Geen AI analyse beschikbaar."

# ============================================================
# PARSERS & SCRAPER
# ============================================================
class ListingParser:
    @staticmethod
    def extract_json(html: str) -> Optional[Dict]:
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        if match:
            try:
                return json.loads(unescape(match.group(1)))
            except: pass
        return None

    @staticmethod
    def clean_int(text: Any) -> int:
        if not text: return 0
        return int(re.sub(r"[^\d]", "", str(text)))

@dataclass
class CarListing:
    url: str
    title: str
    price: int
    km: int = 0
    year: int = 0
    score: int = 0
    reason: str = ""

    def format_message(self, ai_text: str = "") -> str:
        stars = "⭐" * (min(5, self.score // 20))
        return (
            f"🚀 *NIEUWE DEAL FOUND* {stars}\n\n"
            f"🚘 *{self.title}*\n"
            f"💰 Prijs: €{self.price:,}\n"
            f"📏 KM: {self.km:,} km\n"
            f"📅 Jaar: {self.year}\n"
            f"📈 Score: {self.score}/100\n"
            f"💡 {self.reason}\n\n"
            f"🤖 *AI:* _{ai_text}_\n\n"
            f"🔗 [Bekijk op Marktplaats]({self.url})"
        )

class ProfitScraper:
    def __init__(self, config: BotConfig, settings: SettingsManager):
        self.config = config
        self.settings = settings
        self.seen_links = self._load_seen()
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"}

    def _load_seen(self) -> Set:
        if self.config.seen_file.exists():
            return set(json.loads(self.config.seen_file.read_text()))
        return set()

    def _save_seen(self):
        # Keep file small
        list_seen = list(self.seen_links)[-1000:]
        self.config.seen_file.write_text(json.dumps(list_seen))

    def calculate_score(self, price: int, km: int, year: int) -> Tuple[int, str]:
        if price < 100 or price > 25000: return 0, ""
        score = 0
        reasons = []

        # Basis prijs vs KM
        if km > 0:
            ratio = price / km
            if ratio < 0.10: score += 40; reasons.append("Zeer lage km/prijs ratio")
            elif ratio < 0.15: score += 25; reasons.append("Goede km/prijs ratio")

        # Jaar bonus
        current_year = datetime.now().year
        if year >= current_year - 5: score += 30; reasons.append("Jonge auto")
        elif year >= current_year - 10: score += 15; reasons.append("Redelijke leeftijd")

        # Koopjes-factor
        if price < 2000: score += 20; reasons.append("Budget topper")
        
        return score, ", ".join(reasons)

    async def fetch_listings(self, model: str) -> List[str]:
        url = f"https://www.marktplaats.nl/q/{quote(model)}/"
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status != 200: return []
                    html = await resp.text()
                    links = re.findall(r'href="(/a/[^"]+/m\d+[^"]*)"', html)
                    return [f"https://www.marktplaats.nl{l}" for l in links]
        except: return []

    async def process_url(self, url: str) -> Optional[CarListing]:
        if url in self.seen_links: return None
        self.seen_links.add(url)

        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    html = await resp.text()
                    data = ListingParser.extract_json(html)
                    
                    # Probeer data te vinden in de geneste JSON van Marktplaats
                    try:
                        vip = data['props']['pageProps']['ad']
                        title = vip['title']
                        price = ListingParser.clean_int(vip['priceInfo']['priceCents']) // 100
                        
                        km = 0
                        year = 0
                        for attr in vip.get('attributes', []):
                            if attr['tag'] == 'mileage': km = ListingParser.clean_int(attr['value'])
                            if attr['tag'] == 'constructionYear': year = ListingParser.clean_int(attr['value'])
                        
                        score, reason = self.calculate_score(price, km, year)
                        
                        if score >= self.settings.data['min_score']:
                            return CarListing(url, title, price, km, year, score, reason)
                    except: return None
        except: return None
        return None

# ============================================================
# MAIN BOT ENGINE
# ============================================================
class ProfitBot:
    def __init__(self):
        self.config = BotConfig()
        self.settings = SettingsManager(self.config)
        self.scraper = ProfitScraper(self.config, self.settings)
        self.ai = AIClient(self.config.groq_key)
        self.models = self._load_models()

    def _load_models(self) -> List[str]:
        p = Path("filters.json")
        if p.exists():
            data = json.loads(p.read_text())
            return data.get("models", ["Aygo", "Polo", "Fiat 500"])
        return ["Aygo"]

    async def run_scan(self, context):
        if self.settings.data['paused']: return

        logger.info("🔍 Scan gestart...")
        for model in self.models:
            urls = await self.scraper.fetch_listings(model)
            for url in urls[:10]: # Check top 10 per model
                listing = await self.scraper.process_url(url)
                if listing:
                    ai_resp = await self.ai.analyze_deal(listing.title, listing.price, listing.km, listing.year)
                    await context.bot.send_message(
                        chat_id=self.config.chat_id,
                        text=listing.format_message(ai_resp),
                        parse_mode=constants.ParseMode.MARKDOWN
                    )
                    await asyncio.sleep(2) # Anti-spam
            await asyncio.sleep(5)
        
        self.scraper._save_seen()
        logger.info("✅ Scan voltooid.")

    # Commands
    async def start(self, update: Update, context):
        await update.message.reply_text("💰 Profit Bot Actief!\n/stats voor overzicht\n/pause om te stoppen")

    async def stats(self, update: Update, context):
        status = "PAUZE ⏸️" if self.settings.data['paused'] else "RUNNING ▶️"
        await update.message.reply_text(f"📊 *Status:* {status}\n🔍 *Modellen:* {len(self.models)}\n🎯 *Min Score:* {self.settings.data['min_score']}")

    async def toggle(self, update: Update, context):
        self.settings.data['paused'] = not self.settings.data['paused']
        self.settings.save()
        status = "gepauzeerd" if self.settings.data['paused'] else "hervat"
        await update.message.reply_text(f"Bot is nu {status}.")

    def run(self):
        app = ApplicationBuilder().token(self.config.token).build()
        
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("stats", self.stats))
        app.add_handler(CommandHandler("pause", self.toggle))
        app.add_handler(CommandHandler("resume", self.toggle))

        job_queue = app.job_queue
        job_queue.run_repeating(self.run_scan, interval=self.config.check_interval, first=10)

        logger.info("🚀 Bot start polling...")
        app.run_polling()

if __name__ == "__main__":
    ProfitBot().run()