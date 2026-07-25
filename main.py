import os
import json
import logging
import asyncio
import aiohttp
import re
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime
from telegram.ext import ApplicationBuilder
import hashlib

# ============================================
# MULTI-PLATFORM BOT - ANTI-DETECTIE
# ============================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Roterende User Agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

@dataclass
class Config:
    telegram_token: str
    telegram_chat_id: str
    check_interval: int = 180  # 3 min tussen scans
    max_price: int = 6000
    max_km: int = 200000
    request_delay: float = 3.0  # Seconds tussen requests
    
    @classmethod
    def from_env(cls):
        return cls(
            telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "")
        )

@dataclass
class Deal:
    title: str
    price: int
    url: str
    platform: str
    km: Optional[int] = None
    year: Optional[int] = None
    
    def format_message(self) -> str:
        icons = {"marktplaats": "🟠", "autoscout24": "🟢", "gaspedaal": "🔵"}
        icon = icons.get(self.platform, "⚪")
        km_str = f"{self.km:,} km" if self.km else "?"
        year_str = f"{self.year}" if self.year else "?"
        
        return (
            f"🔥 DEAL GEVONDEN!\n"
            f"{icon} {self.platform.upper()}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📋 {self.title[:80]}\n\n"
            f"💶 Prijs: €{self.price:,}\n"
            f"🚗 KM: {km_str}\n"
            f"📅 Jaar: {year_str}\n"
            f"🔗 {self.url}\n\n"
            f"⚡ Direct bellen!"
        )

class SmartHTTPClient:
    """HTTP client met anti-detectie"""
    
    def __init__(self, delay: float = 3.0):
        self.delay = delay
        self.session: Optional[aiohttp.ClientSession] = None
        self._last_request = {}
    
    async def __aenter__(self):
        # Realistische browser headers
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False, limit=5),
            timeout=aiohttp.ClientTimeout(total=20),
        )
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    def _get_headers(self, url: str) -> Dict[str, str]:
        """Generate realistische headers per domain"""
        domain = re.search(r'https?://(?:www\.)?([^/]+)', url).group(1)
        
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
        }
        
        # Domain-specific headers
        if "marktplaats" in domain:
            headers["Referer"] = "https://www.google.nl/"
        elif "autoscout24" in domain:
            headers["Referer"] = "https://www.autoscout24.nl/"
        elif "gaspedaal" in domain:
            headers["Referer"] = "https://www.gaspedaal.nl/"
        
        return headers
    
    async def get(self, url: str, retry: int = 3) -> Optional[str]:
        """GET request met rate limiting en retry"""
        domain = re.search(r'https?://(?:www\.)?([^/]+)', url).group(1)
        
        # Rate limiting per domain
        now = asyncio.get_event_loop().time()
        last = self._last_request.get(domain, 0)
        wait = self.delay - (now - last)
        
        if wait > 0:
            await asyncio.sleep(wait)
        
        self._last_request[domain] = asyncio.get_event_loop().time()
        
        for attempt in range(retry):
            try:
                headers = self._get_headers(url)
                
                async with self.session.get(url, headers=headers, allow_redirects=True) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    
                    elif resp.status == 403:
                        logger.warning(f"🚫 403 op {domain} - probeer langzamere requests")
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    
                    elif resp.status == 429:
                        wait = 10 * (attempt + 1)
                        logger.warning(f"⏸ Rate limit - wacht {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    
                    else:
                        logger.debug(f"HTTP {resp.status} voor {url[:50]}")
                        return None
            
            except asyncio.TimeoutError:
                logger.warning(f"⏱ Timeout op {url[:50]}")
                await asyncio.sleep(2)
            
            except Exception as e:
                logger.error(f"Request fout: {e}")
                return None
        
        return None

class Parser:
    """Universele parser voor alle platforms"""
    
    @staticmethod
    def extract_price(text: str) -> Optional[int]:
        patterns = [
            r'€\s*([0-9]{1,3}(?:[.,][0-9]{3})*)',
            r'"price"[:\s]+([0-9]+)',
            r'prijs[^0-9]+([0-9.,]+)',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text, re.I)
            for match in matches:
                try:
                    price = int(re.sub(r'[.,]', '', match))
                    if 500 < price < 50000:
                        return price
                except:
                    continue
        return None
    
    @staticmethod
    def extract_km(text: str) -> Optional[int]:
        match = re.search(r'(\d{1,3}(?:[.,\s]\d{3})+|\d+)\s*km', text.lower())
        if match:
            try:
                km = int(re.sub(r'[.,\s]', '', match.group(1)))
                if 100 < km < 500000:
                    return km
            except:
                pass
        return None
    
    @staticmethod
    def extract_year(text: str) -> Optional[int]:
        matches = re.findall(r'\b(19[89]\d|20[0-2]\d)\b', text)
        if matches:
            years = [int(y) for y in matches if 1995 <= int(y) <= 2024]
            return max(years) if years else None
        return None
    
    @staticmethod
    def extract_title(html: str) -> Optional[str]:
        patterns = [
            r'<h1[^>]*>([^<]+)</h1>',
            r'<title>([^<]+)</title>',
            r'og:title["\s]+content="([^"]+)"',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html, re.I | re.DOTALL)
            if match:
                title = re.sub(r'<[^>]+>', '', match.group(1)).strip()
                title = re.sub(r'\s+', ' ', title)
                if len(title) > 10:
                    return title[:150]
        return None

class PlatformScraper:
    """Scraper per platform"""
    
    PLATFORMS = {
        "marktplaats": {
            "search": "https://www.marktplaats.nl/q/{model}/",
            "link_pattern": r'href="(/a/[^"]+|/v/auto[^"]+)"',
        },
        "autoscout24": {
            "search": "https://www.autoscout24.nl/lst?sort=age&desc=1&ustate=N%2CU&size=20&cy=NL&atype=C&kmto=200000&priceto=6000&search_id={model}",
            "link_pattern": r'href="(/aanbod/[^"]+)"',
        },
        "gaspedaal": {
            "search": "https://www.gaspedaal.nl/occasions/aanbod?search={model}&sort=date_desc",
            "link_pattern": r'href="(/occasions/[0-9]+[^"]*)"',
        },
    }
    
    def __init__(self, config: Config):
        self.config = config
        self.seen_file = Path("seen_deals.json")
        self.seen_urls: set = self._load_seen()
    
    def _load_seen(self) -> set:
        if self.seen_file.exists():
            try:
                return set(json.loads(self.seen_file.read_text()))
            except:
                return set()
        return set()
    
    def _save_seen(self):
        self.seen_file.write_text(json.dumps(list(self.seen_urls)[-5000:], indent=2))
    
    def _url_hash(self, url: str) -> str:
        """Maak hash om dubbele listings te detecteren"""
        clean = re.sub(r'[?#].*', '', url)
        return hashlib.md5(clean.encode()).hexdigest()
    
    async def scrape_platform(self, platform: str, model: str, client: SmartHTTPClient) -> List[Deal]:
        """Scrape één platform voor één model"""
        
        if platform not in self.PLATFORMS:
            return []
        
        config = self.PLATFORMS[platform]
        search_url = config["search"].format(model=model.replace(' ', '+'))
        
        logger.info(f"🔍 {platform}: {model}")
        
        html = await client.get(search_url)
        if not html:
            return []
        
        # Extract listing URLs
        links = re.findall(config["link_pattern"], html)
        unique_links = list(dict.fromkeys(links))[:15]  # Max 15 per search
        
        deals = []
        
        for link in unique_links:
            # Maak absolute URL
            if link.startswith('http'):
                url = link
            elif platform == "marktplaats":
                url = f"https://www.marktplaats.nl{link}"
            elif platform == "autoscout24":
                url = f"https://www.autoscout24.nl{link}"
            elif platform == "gaspedaal":
                url = f"https://www.gaspedaal.nl{link}"
            else:
                continue
            
            url_id = self._url_hash(url)
            
            if url_id in self.seen_urls:
                continue
            
            # Haal listing op
            listing_html = await client.get(url)
            
            if not listing_html:
                self.seen_urls.add(url_id)
                continue
            
            # Parse
            title = Parser.extract_title(listing_html)
            price = Parser.extract_price(listing_html)
            
            if not title or not price:
                self.seen_urls.add(url_id)
                continue
            
            # Filter
            if price > self.config.max_price:
                self.seen_urls.add(url_id)
                continue
            
            km = Parser.extract_km(listing_html)
            if km and km > self.config.max_km:
                self.seen_urls.add(url_id)
                continue
            
            year = Parser.extract_year(listing_html)
            
            # DEAL!
            deal = Deal(
                title=title,
                price=price,
                url=url,
                platform=platform,
                km=km,
                year=year
            )
            
            deals.append(deal)
            self.seen_urls.add(url_id)
            
            logger.info(f"✅ Deal: {platform} - €{price} - {title[:40]}")
        
        return deals
    
    async def scan_all(self, models: List[str]) -> List[Deal]:
        """Scan alle platforms voor alle modellen"""
        all_deals = []
        
        async with SmartHTTPClient(delay=self.config.request_delay) as client:
            
            for platform in self.PLATFORMS.keys():
                for model in models:
                    deals = await self.scrape_platform(platform, model, client)
                    all_deals.extend(deals)
                    
                    if deals:
                        logger.info(f"🔥 {platform}: {len(deals)} deals voor {model}")
                    
                    # Respectvolle delay tussen models
                    await asyncio.sleep(2)
        
        self._save_seen()
        return all_deals

class TelegramBot:
    """Telegram notificaties"""
    
    def __init__(self, config: Config):
        self.config = config
        self.app = None
    
    async def send(self, message: str):
        try:
            await self.app.bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=message,
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.error(f"Telegram error: {e}")
    
    async def run_loop(self):
        """Hoofd scan loop"""
        
        models = ["toyota aygo", "volkswagen up", "peugeot 107", "kia picanto", "hyundai i10"]
        
        scraper = PlatformScraper(self.config)
        
        await self.send(
            f"🤖 Multi-Platform Bot ACTIEF\n\n"
            f"🌐 Platforms: Marktplaats, Autoscout24, Gaspedaal\n"
            f"🚗 Modellen: {len(models)}\n"
            f"💶 Max: €{self.config.max_price:,}\n"
            f"📏 Max: {self.config.max_km:,} km\n"
            f"⏱ Interval: {self.config.check_interval}s"
        )
        
        while True:
            try:
                logger.info("=" * 50)
                logger.info(f"🔄 Nieuwe scan - {datetime.now().strftime('%H:%M:%S')}")
                
                deals = await scraper.scan_all(models)
                
                logger.info(f"✅ Scan klaar: {len(deals)} nieuwe deals")
                
                for deal in deals:
                    await self.send(deal.format_message())
                    await asyncio.sleep(3)  # Telegram rate limit
                
            except Exception as e:
                logger.exception("Scan error")
            
            await asyncio.sleep(self.config.check_interval)
    
    async def post_init(self, app):
        self.app = app
        asyncio.create_task(self.run_loop())
    
    def start(self):
        app = (
            ApplicationBuilder()
            .token(self.config.telegram_token)
            .post_init(self.post_init)
            .build()
        )
        
        logger.info("🚀 Bot starting...")
        app.run_polling(allowed_updates=[], drop_pending_updates=True)

def main():
    config = Config.from_env()
    
    if not config.telegram_token or not config.telegram_chat_id:
        print("❌ Zet TELEGRAM_TOKEN en TELEGRAM_CHAT_ID environment variables")
        return
    
    bot = TelegramBot(config)
    bot.start()

if __name__ == "__main__":
    main()