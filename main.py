import os
import json
import logging
import asyncio
import aiohttp
import re
import signal
import statistics
import atexit
import urllib.request
import urllib.parse
import sys

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple, Any
from datetime import datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo
from collections import Counter
from html import unescape
from urllib.parse import urlencode, urlparse, quote

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from telegram import Update

# ============================================================
# AI CLIENT
# ============================================================

class AIClient:
    def __init__(self):
        self.groq_key = os.getenv("GROQ_API_KEY")
        self.openai_key = os.getenv("OPENAI_API_KEY")
        
        if self.groq_key:
            self.provider = "groq"
            self.api_url = "https://api.groq.com/openai/v1/chat/completions"
            self.model = "llama-3.1-70b-versatile"
            self.api_key = self.groq_key
            logger.info("🤖 AI: Groq (gratis)")
        elif self.openai_key:
            self.provider = "openai"
            self.api_url = "https://api.openai.com/v1/chat/completions"
            self.model = "gpt-4o-mini"
            self.api_key = self.openai_key
            logger.info("🤖 AI: OpenAI")
        else:
            self.provider = None
            logger.warning("⚠️ Geen AI - voeg GROQ_API_KEY toe")
    
    async def chat(self, user_message: str, system_prompt: Optional[str] = None, conversation_history: Optional[List[Dict]] = None) -> Optional[str]:
        if not self.provider:
            return "❌ AI niet beschikbaar - voeg GROQ_API_KEY toe aan .env\n\nMaak gratis op https://console.groq.com"
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if conversation_history:
            messages.extend(conversation_history[-6:])
        messages.append({"role": "user", "content": user_message})
        
        try:
            async with aiohttp.ClientSession() as session:
                payload = {"model": self.model, "messages": messages, "temperature": 0.7, "max_tokens": 600}
                headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
                
                async with session.post(self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"❌ AI error {response.status}: {error_text[:200]}")
                        
                        if response.status == 400:
                            return "❌ Ongeldige API key - maak nieuwe op https://console.groq.com/keys"
                        elif response.status == 401:
                            return "❌ Unauthorized - check API key"
                        elif response.status == 429:
                            return "⏱️ Rate limit - wacht even"
                        
                        return f"❌ AI HTTP {response.status}"
                    
                    data = await response.json()
                    if "choices" in data and data["choices"]:
                        return data["choices"][0]["message"]["content"].strip()
                    return "❌ Geen antwoord"
        
        except asyncio.TimeoutError:
            return "⏱️ Timeout - probeer opnieuw"
        except Exception as e:
            logger.exception(f"AI error: {e}")
            return f"❌ Fout: {str(e)[:100]}"


# ============================================================
# LOGGING & CONSTANTS
# ============================================================

SETTINGS_FILE = Path("runtime_settings.json")
MARKTPLAATS_API = "https://www.marktplaats.nl/lrp/api/search"
KENTEKEN_PATTERN = re.compile(r"\b([A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3})\b")

class ColoredFormatter(logging.Formatter):
    COLORS = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m", "ERROR": "\033[31m"}
    RESET = "\033[0m"
    def format(self, record):
        text = super().format(record)
        return f"{self.COLORS.get(record.levelname, self.RESET)}{text}{self.RESET}"

def setup_logging(level=logging.INFO):
    bot_logger = logging.getLogger("profit-bot")
    bot_logger.setLevel(level)
    bot_logger.handlers.clear()
    bot_logger.propagate = False
    
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColoredFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    bot_logger.addHandler(console)
    
    file_h = logging.FileHandler("bot.log", encoding="utf-8")
    file_h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    bot_logger.addHandler(file_h)
    
    return bot_logger

logger = setup_logging()

def extract_kenteken(text: str) -> Optional[str]:
    match = KENTEKEN_PATTERN.search(text)
    if not match:
        return None
    candidate = match.group(1).replace("-", "").replace(" ", "").upper()
    if 5 <= len(candidate) <= 6 and any(c.isdigit() for c in candidate) and any(c.isalpha() for c in candidate):
        return candidate
    return None


# ============================================================
# CONFIG
# ============================================================

@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    telegram_chat_id: str
    check_interval: int = 60
    max_concurrent_requests: int = 2
    request_timeout: int = 25
    max_retries: int = 3
    min_profit_margin: int = 500
    max_km: int = 300_000
    price_per_km_limit: float = 0.35
    seen_max_age_days: int = 3
    seen_file: Path = field(default_factory=lambda: Path("seen_links.json"))

    @classmethod
    def from_env(cls):
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise ValueError("TELEGRAM_TOKEN en TELEGRAM_CHAT_ID vereist")
        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            check_interval=int(os.getenv("CHECK_INTERVAL", "60")),
            min_profit_margin=int(os.getenv("MIN_PROFIT_MARGIN", "500")),
            max_km=int(os.getenv("MAX_KM", "300000")),
            price_per_km_limit=float(os.getenv("PRICE_PER_KM_LIMIT", "0.35"))
        )

@dataclass
class RuntimeSettings:
    min_profit_margin: int
    max_km: int
    price_per_km_limit: float
    check_interval: int
    paused: bool = False

def load_runtime_settings(bot_config: BotConfig) -> RuntimeSettings:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return RuntimeSettings(
                int(data.get("min_profit_margin", bot_config.min_profit_margin)),
                int(data.get("max_km", bot_config.max_km)),
                float(data.get("price_per_km_limit", bot_config.price_per_km_limit)),
                int(data.get("check_interval", bot_config.check_interval)),
                bool(data.get("paused", False))
            )
        except Exception as e:
            logger.warning(f"⚠️ Settings error: {e}")
    return RuntimeSettings(bot_config.min_profit_margin, bot_config.max_km, bot_config.price_per_km_limit, bot_config.check_interval)

def save_runtime_settings(settings: RuntimeSettings):
    try:
        SETTINGS_FILE.write_text(json.dumps({
            "min_profit_margin": settings.min_profit_margin,
            "max_km": settings.max_km,
            "price_per_km_limit": settings.price_per_km_limit,
            "check_interval": settings.check_interval,
            "paused": settings.paused
        }, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"❌ Save error: {e}")

@dataclass(frozen=True)
class FilterConfig:
    models: List[str]
    motivation_words: List[str]
    dealer_words: List[str]
    red_flags: List[str]
    quality_indicators: List[str]
    model_aliases: Dict[str, List[str]] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path):
        if not path.exists():
            raise FileNotFoundError(f"❌ {path} niet gevonden")
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            data.get("models", []),
            data.get("motivation_words", []),
            data.get("dealer_words", []),
            data.get("red_flags", []),
            data.get("quality_indicators", []),
            data.get("model_aliases", {})
        )

_shutdown_notified = False

def notify_shutdown_sync(token: str, chat_id: str, reason: str = "Bot gestopt"):
    global _shutdown_notified
    if _shutdown_notified:
        return
    _shutdown_notified = True
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = f"🛑 {reason}\n📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')}"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        logger.error(f"❌ Shutdown failed: {e}")


# ============================================================
# HTTP CLIENT
# ============================================================

class SmartClient:
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
    ]

    def __init__(self, config: BotConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._ua_index = 0
        self._domain_delays: Dict[str, datetime] = {}
        self._domain_locks: Dict[str, asyncio.Lock] = {}
        self._stats = {"requests": 0, "errors": 0}

    def _rotate_ua(self) -> str:
        ua = self.USER_AGENTS[self._ua_index]
        self._ua_index = (self._ua_index + 1) % len(self.USER_AGENTS)
        return ua

    def _get_domain_lock(self, url: str) -> asyncio.Lock:
        domain = urlparse(url).netloc
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()
        return self._domain_locks[domain]

    async def _rate_limit(self, url: str):
        domain = urlparse(url).netloc
        async with self._get_domain_lock(url):
            prev = self._domain_delays.get(domain)
            if prev and (datetime.now() - prev).total_seconds() < 2.0:
                await asyncio.sleep(2.0)
            self._domain_delays[domain] = datetime.now()

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(limit=self.config.max_concurrent_requests, limit_per_host=2)
        self._session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=self.config.request_timeout))
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def get_text(self, url: str) -> Optional[str]:
        if not self._session:
            raise RuntimeError("Client not init")
        
        await self._rate_limit(url)
        
        async with self._semaphore:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    self._stats["requests"] += 1
                    headers = {"User-Agent": self._rotate_ua(), "Accept": "text/html,application/json"}
                    
                    async with self._session.get(url, headers=headers, ssl=False) as resp:
                        if resp.status == 200:
                            return await resp.text(errors="replace")
                        if resp.status in (400, 404):
                            return None
                        if resp.status in (403, 429, 500, 502, 503):
                            wait = min(2 ** attempt, 30)
                            await asyncio.sleep(wait)
                            continue
                        return None
                
                except Exception as e:
                    self._stats["errors"] += 1
                    if attempt < self.config.max_retries:
                        await asyncio.sleep(2 ** attempt)
        
        return None


# ============================================================
# PARSERS
# ============================================================

class ListingParser:
    @staticmethod
    def parse_price(raw: Any, is_cents: bool = False) -> Optional[int]:
        if raw is None:
            return None
        try:
            if isinstance(raw, (int, float)):
                val = float(raw)
            else:
                text = re.sub(r"[^\d.,]", "", str(raw).strip())
                if not text:
                    return None
                if "." in text and "," in text:
                    text = text.replace(".", "").replace(",", ".")
                elif "," in text:
                    text = text.replace(",", ".") if len(text.split(",")[-1]) == 2 else text.replace(",", "")
                val = float(text)
            
            if is_cents:
                val = val / 100
            elif val > 100_000:
                val = val / 100
            
            price = int(round(val))
            return price if price > 0 else None
        except:
            return None

    @staticmethod
    def _find_listing(obj: Any) -> Optional[dict]:
        if isinstance(obj, dict):
            if obj.get("title") and any(k in obj for k in ("priceInfo", "price", "priceCents")):
                return obj
            for v in obj.values():
                found = ListingParser._find_listing(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = ListingParser._find_listing(item)
                if found:
                    return found
        return None

    @staticmethod
    def parse_marktplaats_json(html: str) -> Tuple[Optional[str], Optional[int], str]:
        match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.I | re.S)
        if match:
            try:
                data = json.loads(unescape(match.group(1)))
                listing = ListingParser._find_listing(data)
                if listing:
                    title = str(listing.get("title", "")).strip()
                    desc = str(listing.get("description", "")).strip()
                    price_info = listing.get("priceInfo", {})
                    price = None
                    
                    if isinstance(price_info, dict):
                        if price_info.get("priceCents"):
                            price = ListingParser.parse_price(price_info["priceCents"], is_cents=True)
                        elif price_info.get("price"):
                            price = ListingParser.parse_price(price_info["price"])
                    
                    if not price:
                        price = ListingParser.parse_price(listing.get("priceCents"), is_cents=True) or ListingParser.parse_price(listing.get("price"))
                    
                    if title and price:
                        return title, price, desc
            except:
                pass
        
        # Fallback
        title = None
        price = None
        desc = ""
        
        og = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', html, re.I)
        if og:
            title = unescape(og.group(1)).strip()
        
        pc = re.search(r'"priceCents"\s*:\s*"?([\d.,]+)"?', html, re.I)
        if pc:
            price = ListingParser.parse_price(pc.group(1), is_cents=True)
        
        if not price:
            pm = re.search(r'"price"\s*:\s*"?([\d.,]+)"?', html, re.I)
            if pm:
                price = ListingParser.parse_price(pm.group(1))
        
        if title and price:
            return title, price, desc
        
        return None, None, ""

    @staticmethod
    def extract_km(text: str) -> Optional[int]:
        for pattern in [r"(\d{1,3}(?:[.,\s]\d{3})+)\s*km\b", r"\b(\d{4,6})\s*km\b"]:
            m = re.search(pattern, text, re.I)
            if m:
                raw = re.sub(r"[.,\s]", "", m.group(1))
                try:
                    km = int(raw)
                    if 50 < km < 500_000:
                        return km
                except:
                    pass
        return None

    @staticmethod
    def extract_year(text: str) -> Optional[int]:
        current = datetime.now().year
        patterns = [r"bouwjaar\s*[:\-]?\s*(\d{4})", r"\b(19\d{2}|20\d{2})\b"]
        
        for p in patterns:
            m = re.search(p, text, re.I)
            if m:
                try:
                    yr = int(m.group(1))
                    if 1990 <= yr <= current:
                        return yr
                except:
                    pass
        return None


# ============================================================
# DEAL QUALITY
# ============================================================

class DealQuality(Enum):
    GODLIKE = "💎 GODLIKE"
    EXCELLENT = "🔥 EXCELLENT"
    GOOD = "✅ GOOD"
    AVERAGE = "👍 AVERAGE"
    WATCHLIST = "👀 WATCHLIST"
    POOR = "❌ POOR"

@dataclass
class Listing:
    url: str
    title: str
    price: int
    platform: str
    search_term: str = ""
    km: Optional[int] = None
    year: Optional[int] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    description: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    
    deal_quality: DealQuality = field(default=DealQuality.POOR, init=False)
    
    @property
    def is_good_deal(self) -> bool:
        return self.deal_quality != DealQuality.POOR
    
    def format_message(self) -> str:
        result = f"{self.deal_quality.value}\n{'━'*40}\n{self.title}\n\n"
        result += f"💰 €{self.price:,}\n"
        result += f"📅 {self.year or '?'} | 📏 {f'{self.km:,}' if self.km else '?'} km\n"
        result += f"{'━'*40}\n🔗 {self.url}"
        return result


# ============================================================
# SEEN LINKS
# ============================================================

class SeenLinksManager:
    def __init__(self, path: Path, max_age_days: int):
        self._path = path
        self._data: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._load()
    
    def _load(self):
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._data = {k: datetime.fromisoformat(v) for k, v in raw.items()}
            except:
                pass
    
    def _save(self):
        try:
            self._path.write_text(json.dumps({k: v.isoformat() for k, v in self._data.items()}, indent=2), encoding="utf-8")
        except:
            pass
    
    async def contains(self, url: str) -> bool:
        async with self._lock:
            return url in self._data
    
    async def add(self, url: str):
        async with self._lock:
            self._data[url] = datetime.now()
    
    async def cleanup_and_save(self):
        async with self._lock:
            self._save()


# ============================================================
# MARKTPLAATS MONITOR
# ============================================================

class MarktplaatsMonitor:
    def __init__(self):
        self._seen_items: Set[str] = set()
        self._last_reset = datetime.now()
    
    def _maybe_reset(self):
        if datetime.now() - self._last_reset > timedelta(hours=6):
            old = len(self._seen_items)
            self._seen_items.clear()
            self._last_reset = datetime.now()
            logger.info(f"🔄 Reset seen ({old} cleared)")
    
    async def scan_search_page(self, model: str, client: SmartClient) -> List[str]:
        self._maybe_reset()
        
        search_url = f"https://www.marktplaats.nl/q/{quote(model)}/"
        html = await client.get_text(search_url)
        
        if not html:
            return []
        
        patterns = [
            r'href="(/a/[^"]+/m\d+[^"]*)"',
            r'"vipUrl"\s*:\s*"(/a/[^"]+/m\d+[^"]*)"'
        ]
        
        all_urls = []
        for pattern in patterns:
            all_urls.extend(re.findall(pattern, html, re.I))
        
        full_urls = [f"https://www.marktplaats.nl{u}" if not u.startswith("http") else u for u in all_urls]
        unique_urls = list(dict.fromkeys(full_urls))
        
        new_urls = []
        for url in unique_urls[:50]:
            if url not in self._seen_items:
                self._seen_items.add(url)
                new_urls.append(url)
        
        logger.info(f"🔎 {model}: {len(unique_urls)} found, {len(new_urls)} new")
        return new_urls


# ============================================================
# SCRAPER
# ============================================================

class ProfitScraper:
    def __init__(self, filter_config, seen_manager, settings):
        self.filter_config = filter_config
        self.seen_manager = seen_manager
        self.settings = settings
        self.monitor = MarktplaatsMonitor()
        
        self._stats = {"scans": 0, "listings_checked": 0, "deals_found": 0, "watchlist_deals": 0}
        self.found_deals: List[Listing] = []
    
    async def process_listing(self, url: str, search_term: str, client: SmartClient) -> Optional[Listing]:
        if await self.seen_manager.contains(url):
            return None
        
        html = await client.get_text(url)
        if not html:
            return None
        
        title, price, desc = ListingParser.parse_marktplaats_json(html)
        if not title or not price:
            await self.seen_manager.add(url)
            return None
        
        km = ListingParser.extract_km(f"{title} {desc}") or ListingParser.extract_km(html)
        year = ListingParser.extract_year(f"{title} {desc}") or ListingParser.extract_year(html)
        
        listing = Listing(url=url, title=title, price=price, platform="marktplaats", search_term=search_term, km=km, year=year, description=desc)
        
        self._stats["listings_checked"] += 1
        
        # SIMPELE DEAL LOGICA
        is_deal = False
        
        if price < 1500:
            listing.deal_quality = DealQuality.WATCHLIST
            is_deal = True
        
        if km and price / km < 0.20:
            listing.deal_quality = DealQuality.GOOD
            is_deal = True
        
        if year and year >= 2015 and price < 4500:
            listing.deal_quality = DealQuality.EXCELLENT
            is_deal = True
        
        if year and year >= 2010 and price < 2500:
            listing.deal_quality = DealQuality.GOOD
            is_deal = True
        
        if not is_deal:
            await self.seen_manager.add(url)
            return None
        
        self._stats["deals_found"] += 1
        if listing.deal_quality == DealQuality.WATCHLIST:
            self._stats["watchlist_deals"] += 1
        
        await self.seen_manager.add(url)
        self.found_deals.append(listing)
        self.found_deals = self.found_deals[-100:]
        
        logger.info(f"🎉 DEAL: {listing.deal_quality.value} - {title[:50]}")
        return listing
    
    async def scan_model(self, model: str, client: SmartClient) -> List[Listing]:
        new_urls = await self.monitor.scan_search_page(model, client)
        if not new_urls:
            return []
        
        logger.info(f"🔍 {model}: Processing {len(new_urls)} URLs...")
        
        tasks = [self.process_listing(url, model, client) for url in new_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        deals = [r for r in results if isinstance(r, Listing)]
        
        if deals:
            logger.info(f"✅ {model}: {len(deals)} deals!")
        
        return deals
    
    async def scan_all(self, client: SmartClient) -> List[Listing]:
        self._stats["scans"] += 1
        scan_num = self._stats["scans"]
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🚀 SCAN #{scan_num}")
        logger.info(f"{'='*60}")
        
        terms = []
        for model in self.filter_config.models:
            terms.append(model)
            terms.extend(self.filter_config.model_aliases.get(model, []))
        
        terms = list(dict.fromkeys(terms))
        logger.info(f"🔎 Scanning {len(terms)} terms...")
        
        before = self._stats["listings_checked"]
        
        tasks = [self.scan_model(term, client) for term in terms]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_deals = []
        for result in results:
            if isinstance(result, list):
                all_deals.extend(result)
        
        checked = self._stats["listings_checked"] - before
        
        logger.info(f"{'='*60}")
        logger.info(f"📊 SCAN #{scan_num} RESULTS")
        logger.info(f"  Checked: {checked}")
        logger.info(f"  Deals: {len(all_deals)}")
        logger.info(f"  Total: {len(self.found_deals)}")
        
        if all_deals:
            logger.info(f"✅ {len(all_deals)} DEALS:")
            for d in all_deals:
                logger.info(f"  {d.deal_quality.value}: {d.title[:60]}")
        else:
            logger.info("❌ No deals")
        
        logger.info(f"{'='*60}\n")
        
        return all_deals
    
    def get_stats(self) -> Dict:
        return self._stats.copy()
    
    def get_top_deals(self, n: int = 5) -> List[Listing]:
        return sorted(self.found_deals, key=lambda x: x.price)[:n]


# ============================================================
# TELEGRAM
# ============================================================

class TelegramNotifier:
    def __init__(self, app, chat_id: str):
        self.app = app
        self.chat_id = chat_id
        self._stats = {"sent": 0}
    
    async def send_message(self, text: str) -> bool:
        try:
            if len(text) > 4000:
                text = text[:4000]
            result = await self.app.bot.send_message(chat_id=self.chat_id, text=text, disable_web_page_preview=True)
            self._stats["sent"] += 1
            logger.info(f"✅ Sent (ID: {result.message_id})")
            return True
        except Exception as e:
            logger.error(f"❌ Telegram: {e}")
            return False
    
    async def send_listing(self, listing: Listing) -> bool:
        return await self.send_message(listing.format_message())
    
    async def send_startup(self, min_profit: int) -> bool:
        return await self.send_message(f"💰 BOT ACTIEF\n📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%H:%M')}\n🎯 Min €{min_profit}\n🤖 AI ready")


# ============================================================
# BOT COMMANDS
# ============================================================

class BotCommands:
    def __init__(self, notifier, scraper, settings, bot_config, ai_client):
        self.notifier = notifier
        self.scraper = scraper
        self.settings = settings
        self.bot_config = bot_config
        self.ai = ai_client
        self.conversations: Dict[int, List[Dict]] = {}
    
    def _is_authorized(self, update) -> bool:
        return update.effective_chat and str(update.effective_chat.id) == str(self.bot_config.telegram_chat_id)
    
    async def start(self, update, context):
        if not self._is_authorized(update):
            return
        await update.message.reply_text("🚀 *Profit Bot + AI*\n\n✅ Auto-scan actief\n🤖 AI via Groq\n\n/help voor commando's")
    
    async def help(self, update, context):
        if not self._is_authorized(update):
            return
        await update.message.reply_text("/stats - Stats\n/top - Top deals\n/pause - Pause\n/resume - Resume\n/ai [vraag] - AI\n\nOf stuur:\n🔗 Marktplaats link\n🪪 Kenteken")
    
    async def stats(self, update, context):
        if not self._is_authorized(update):
            return
        stats = self.scraper.get_stats()
        status = "⏸️ PAUSED" if self.settings.paused else "▶️ ACTIVE"
        await update.message.reply_text(f"📊 *Stats*\n\n{status}\nScans: {stats['scans']}\nChecked: {stats['listings_checked']}\nDeals: {stats['deals_found']}")
    
    async def pause(self, update, context):
        if not self._is_authorized(update):
            return
        self.settings.paused = True
        save_runtime_settings(self.settings)
        await update.message.reply_text("⏸️ Paused")
    
    async def resume(self, update, context):
        if not self._is_authorized(update):
            return
        self.settings.paused = False
        save_runtime_settings(self.settings)
        await update.message.reply_text("▶️ Resumed")
    
    async def top(self, update, context):
        if not self._is_authorized(update):
            return
        deals = self.scraper.get_top_deals(5)
        if not deals:
            await update.message.reply_text("📭 Geen deals")
            return
        lines = ["🏆 *Top 5:*"]
        for i, d in enumerate(deals, 1):
            lines.append(f"{i}. €{d.price:,} - {d.title[:40]}")
        await update.message.reply_text("\n".join(lines))
    
    async def ai_command(self, update: Update, context):
        if not self._is_authorized(update):
            return
        if not context.args:
            await update.message.reply_text("💬 /ai [vraag]\n\nBv: /ai Is €4000 voor Aygo 2015 goed?")
            return
        await self._ai_chat(update, " ".join(context.args))
    
    async def handle_text(self, update: Update, context):
        if not self._is_authorized(update):
            return
        text = update.message.text.strip()
        
        if "marktplaats.nl" in text.lower():
            await self._analyze_link(update, text)
            return
        
        kenteken = extract_kenteken(text)
        if kenteken:
            await self._lookup_rdw(update, kenteken)
            return
        
        await self._ai_chat(update, text)
    
    async def _analyze_link(self, update: Update, url: str):
        await update.message.reply_text("🔍 Analyseren...")
        
        if not url.startswith("http"):
            url = f"https://www.marktplaats.nl{url}"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        await update.message.reply_text(f"❌ HTTP {resp.status}")
                        return
                    html = await resp.text()
            
            title, price, desc = ListingParser.parse_marktplaats_json(html)
            if not title:
                await update.message.reply_text("❌ Kon niet lezen")
                return
            
            km = ListingParser.extract_km(f"{title} {desc}") or ListingParser.extract_km(html)
            year = ListingParser.extract_year(f"{title} {desc}") or ListingParser.extract_year(html)
            
            ai_prompt = f"{title} voor €{price:,}. Jaar: {year or '?'}, KM: {f'{km:,}' if km else '?'}. Goede deal?"
            ai_resp = await self.ai.chat(ai_prompt, "Auto-expert. 3 zinnen.")
            
            msg = f"🚗 *{title}*\n\n💰 €{price:,}\n📅 {year or '?'} | 📏 {f'{km:,} km' if km else '?'}\n\n🤖 {ai_resp or 'AI n/a'}\n\n🔗 {url}"
            await update.message.reply_text(msg[:4000])
        
        except Exception as e:
            await update.message.reply_text(f"❌ {str(e)[:100]}")
    
    async def _lookup_rdw(self, update: Update, kenteken: str):
        await update.message.reply_text(f"🔍 {kenteken}...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://opendata.rdw.nl/resource/m9d7-ebf2.json?kenteken={kenteken}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        await update.message.reply_text("❌ RDW error")
                        return
                    data = await resp.json()
            
            if not data:
                await update.message.reply_text(f"❌ {kenteken} n/a")
                return
            
            info = data[0]
            msg = f"🪪 *{kenteken}*\n\n🚗 {info.get('merk', '?')} {info.get('handelsbenaming', '?')}\n📅 {info.get('datum_eerste_toelating', '?')}\n⛽ {info.get('brandstof_omschrijving', '?')}"
            await update.message.reply_text(msg)
        
        except Exception as e:
            await update.message.reply_text(f"❌ {str(e)[:100]}")
    
    async def _ai_chat(self, update: Update, question: str):
        user_id = update.effective_chat.id
        if user_id not in self.conversations:
            self.conversations[user_id] = []
        
        thinking = await update.message.reply_text("🤔 ...")
        
        try:
            response = await self.ai.chat(question, "Auto-expert. Max 4 zinnen.", self.conversations[user_id])
            
            if not response:
                response = "❌ Geen antwoord"
            
            self.conversations[user_id].append({"role": "user", "content": question})
            self.conversations[user_id].append({"role": "assistant", "content": response})
            self.conversations[user_id] = self.conversations[user_id][-10:]
            
            await thinking.edit_text(response[:4000])
        
        except Exception as e:
            await thinking.edit_text(f"❌ {str(e)[:100]}")


# ============================================================
# MAIN BOT
# ============================================================

class ProfitBot:
    def __init__(self, bot_config: BotConfig, filter_config: FilterConfig):
        self.bot_config = bot_config
        self.filter_config = filter_config
        self.settings = load_runtime_settings(bot_config)
        self.seen_manager = SeenLinksManager(bot_config.seen_file, bot_config.seen_max_age_days)
        self.ai = AIClient()
        self.scraper = None
        self.notifier = None
        self._shutdown = asyncio.Event()
        self._setup_signals()
    
    def _setup_signals(self):
        def handle(s, f):
            logger.info("🛑 Shutdown")
            self._shutdown.set()
        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)
    
    async def _scan_loop(self):
        for _ in range(300):
            if self.notifier:
                break
            await asyncio.sleep(0.1)
        
        if not self.notifier:
            return
        
        await self.notifier.send_startup(self.settings.min_profit_margin)
        
        async with SmartClient(self.bot_config) as client:
            while not self._shutdown.is_set():
                if not self.settings.paused:
                    try:
                        deals = await self.scraper.scan_all(client)
                        if deals:
                            for d in deals:
                                await self.notifier.send_listing(d)
                                await asyncio.sleep(1)
                        await self.seen_manager.cleanup_and_save()
                    except Exception as e:
                        logger.exception(f"Scan error: {e}")
                        await asyncio.sleep(5)
                
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=self.settings.check_interval)
                    break
                except asyncio.TimeoutError:
                    pass
    
    async def _post_init(self, app):
        logger.info("🔧 Init...")
        self.notifier = TelegramNotifier(app, self.bot_config.telegram_chat_id)
        self.scraper = ProfitScraper(self.filter_config, self.seen_manager, self.settings)
        
        cmd = BotCommands(self.notifier, self.scraper, self.settings, self.bot_config, self.ai)
        
        app.add_handler(CommandHandler("start", cmd.start))
        app.add_handler(CommandHandler("help", cmd.help))
        app.add_handler(CommandHandler("stats", cmd.stats))
        app.add_handler(CommandHandler("top", cmd.top))
        app.add_handler(CommandHandler("pause", cmd.pause))
        app.add_handler(CommandHandler("resume", cmd.resume))
        app.add_handler(CommandHandler("ai", cmd.ai_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd.handle_text))
        
        asyncio.create_task(self._scan_loop())
        logger.info("✅ Ready!")
    
    async def _post_shutdown(self, app):
        await self.seen_manager.cleanup_and_save()
    
    def run(self):
        logger.info("💰 PROFIT BOT + AI")
        logger.info(f"🎯 Min €{self.settings.min_profit_margin}")
        
        app = ApplicationBuilder().token(self.bot_config.telegram_token).post_init(self._post_init).post_shutdown(self._post_shutdown).build()
        app.run_polling(drop_pending_updates=True)


def main():
    try:
        bot_config = BotConfig.from_env()
        filter_config = FilterConfig.from_file(Path("filters.json"))
        logger.info(f"✅ {len(filter_config.models)} modellen")
        atexit.register(lambda: notify_shutdown_sync(bot_config.telegram_token, bot_config.telegram_chat_id))
        ProfitBot(bot_config, filter_config).run()
    except Exception as e:
        logger.exception(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()