import os
import json
import logging
import asyncio
import aiohttp
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any, Set
from datetime import datetime
from zoneinfo import ZoneInfo
from html import unescape
from urllib.parse import quote

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
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
    min_score: int = 40  # Iets lager gezet zodat je sneller deals krijgt
    seen_file: Path = Path("seen_links.json")

    def __post_init__(self):
        if not self.token or not self.chat_id:
            logger.error("⚠️ WAARSCHUWING: TELEGRAM_TOKEN of TELEGRAM_CHAT_ID ontbreekt!")

class SettingsManager:
    def __init__(self, config: BotConfig):
        self.config = config
        self.file = Path("runtime_settings.json")
        self.data = self._load()

    def _load(self):
        if self.file.exists():
            try:
                return json.loads(self.file.read_text())
            except: pass
        return {"paused": False, "min_score": self.config.min_score}

    def save(self):
        self.file.write_text(json.dumps(self.data))

# ============================================================
# AI CLIENT
# ============================================================
class AIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = "https://api.groq.com/openai/v1/chat/completions"
        self.model = "llama-3.1-70b-versatile"

    async def analyze_deal(self, title: str, price: int, km: int, year: int) -> str:
        if not self.api_key: return "AI niet geconfigureerd."
        prompt = (f"Auto: {title}, Prijs: €{price}, KM: {km}, Jaar: {year}. "
                  f"Is dit een goede handelsdeal voor snelle verkoop? "
                  f"Geef een oordeel in 1 korte krachtige zin.")
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
                payload = {
                    "model": self.model, 
                    "messages": [{"role": "system", "content": "Je bent een auto-inkoper expert."},
                                 {"role": "user", "content": prompt}],
                    "temperature": 0.5,
                    "max_tokens": 100
                }
                async with session.post(self.url, headers=headers, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data['choices'][0]['message']['content'].strip()
        except: pass
        return "Geen AI analyse mogelijk."

# ============================================================
# SCRAPER LOGIC
# ============================================================
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
        stars = "⭐" * (min(5, max(1, self.score // 20)))
        return (
            f"🚀 *NIEUWE DEAL FOUND* {stars}\n\n"
            f"🚘 *{self.title}*\n"
            f"💰 Prijs: €{self.price:,}\n"
            f"📏 KM: {self.km:,} km\n"
            f"📅 Jaar: {self.year}\n"
            f"📈 Deal Score: {self.score}/100\n"
            f"💡 _{self.reason}_\n\n"
            f"🤖 *AI Oordeel:* {ai_text}\n\n"
            f"🔗 [Bekijk op Marktplaats]({self.url})"
        )

class ProfitScraper:
    def __init__(self, config: BotConfig, settings: SettingsManager):
        self.config = config
        self.settings = settings
        self.seen_links = self._load_seen()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        }

    def _load_seen(self) -> Set[str]:
        if self.config.seen_file.exists():
            try:
                return set(json.loads(self.config.seen_file.read_text()))
            except: pass
        return set()

    def _save_seen(self):
        list_seen = list(self.seen_links)[-2000:] # Bewaar laatste 2000 links
        self.config.seen_file.write_text(json.dumps(list_seen))

    def calculate_score(self, price: int, km: int, year: int) -> Tuple[int, str]:
        if price < 250: return 0, ""
        score = 20 # Basis score
        reasons = []

        # KM score
        if 0 < km < 150000:
            score += 30
            reasons.append("Lage kilometerstand")
        elif km < 220000:
            score += 15

        # Jaar score
        current_year = datetime.now().year
        if year >= current_year - 8:
            score += 25
            reasons.append("Relatief nieuw")
        
        # Prijs check
        if price < 1500:
            score += 20
            reasons.append("Budget koopje")
        
        return min(score, 100), ", ".join(reasons) if reasons else "Normale prijs"

    async def fetch_listings(self, model: str) -> List[str]:
        url = f"https://www.marktplaats.nl/q/{quote(model)}/"
        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status != 200: return []
                    html = await resp.text()
                    # Zoek alle /a/ advertentie links
                    links = re.findall(r'href="(/a/[^"]+/m\d+[^"]*)"', html)
                    return list(dict.fromkeys([f"https://www.marktplaats.nl{l}" for l in links]))
        except Exception as e:
            logger.error(f"Fout bij ophalen {model}: {e}")
        return []

    async def process_url(self, url: str) -> Optional[CarListing]:
        if url in self.seen_links: return None
        self.seen_links.add(url)

        try:
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.get(url, timeout=15) as resp:
                    if resp.status != 200: return None
                    html = await resp.text()
                    
                    # NEXT_DATA extractie
                    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
                    if not match: return None
                    
                    data = json.loads(unescape(match.group(1)))
                    ad = data['props']['pageProps']['ad']
                    
                    title = ad.get('title', 'Onbekend')
                    price_cents = ad.get('priceInfo', {}).get('priceCents', 0)
                    price = int(price_cents // 100) if price_cents else 0
                    
                    if price == 0: return None # Geen prijs = overslaan

                    km = 0
                    year = 0
                    for attr in ad.get('attributes', []):
                        if attr.get('tag') == 'mileage':
                            km = int(re.sub(r"\D", "", str(attr.get('value', '0'))))
                        if attr.get('tag') == 'constructionYear':
                            year = int(re.sub(r"\D", "", str(attr.get('value', '0'))))

                    score, reason = self.calculate_score(price, km, year)
                    
                    if score >= self.settings.data['min_score']:
                        return CarListing(url, title, price, km, year, score, reason)
        except: pass
        return None

# ============================================================
# BOT APPLICATION
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
            try:
                data = json.loads(p.read_text())
                return data.get("models", [])
            except: pass
        return ["Toyota Aygo", "Peugeot 107", "Citroen C1", "Volkswagen Polo"]

    async def run_scan(self, context: ContextTypes.DEFAULT_TYPE):
        if self.settings.data['paused']: return

        logger.info(f"Checking {len(self.models)} models for deals...")
        for model in self.models:
            urls = await self.scraper.fetch_listings(model)
            for url in urls[:12]: # Check de nieuwste 12 per model
                listing = await self.scraper.process_url(url)
                if listing:
                    ai_resp = await self.ai.analyze_deal(listing.title, listing.price, listing.km, listing.year)
                    await context.bot.send_message(
                        chat_id=self.config.chat_id,
                        text=listing.format_message(ai_resp),
                        parse_mode=constants.ParseMode.MARKDOWN
                    )
                    await asyncio.sleep(1) # Delay tegen spam
            await asyncio.sleep(2)
        
        self.scraper._save_seen()

    # COMMAND HANDLERS
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💰 *Profit Bot is Online!*\n\nIk scan nu Marktplaats op koopjes.\n/stats - Bekijk status\n/pause - Stop met scannen\n/resume - Start met scannen", parse_mode=constants.ParseMode.MARKDOWN)

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status = "PAUZE ⏸️" if self.settings.data['paused'] else "RUNNING ▶️"
        txt = (f"📊 *Bot Status*\n\n"
               f"Toestand: {status}\n"
               f"Modellen: {len(self.models)}\n"
               f"Min Score: {self.settings.data['min_score']}\n"
               f"Bekende links: {len(self.scraper.seen_links)}")
        await update.message.reply_text(txt, parse_mode=constants.ParseMode.MARKDOWN)

    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.settings.data['paused'] = True
        self.settings.save()
        await update.message.reply_text("Bot is gepauzeerd ⏸️")

    async def resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.settings.data['paused'] = False
        self.settings.save()
        await update.message.reply_text("Bot is hervat ▶️")

    def run(self):
        if not self.config.token:
            logger.error("Geen Telegram Token gevonden!")
            return

        app = ApplicationBuilder().token(self.config.token).build()
        
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("stats", self.stats))
        app.add_handler(CommandHandler("pause", self.pause))
        app.add_handler(CommandHandler("resume", self.resume))

        # Scan loop instellen
        job_queue = app.job_queue
        job_queue.run_repeating(self.run_scan, interval=self.config.check_interval, first=5)

        logger.info("Bot gestart. Druk op Ctrl+C om te stoppen.")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    ProfitBot().run()