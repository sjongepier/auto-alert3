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
            logger.warning("⚠️ Geen AI - voeg GROQ_API_KEY toe aan .env voor gratis AI!")
    
    async def chat(self, user_message: str, system_prompt: Optional[str] = None, conversation_history: Optional[List[Dict]] = None) -> Optional[str]:
        if not self.provider:
            return "❌ AI niet beschikbaar - voeg GROQ_API_KEY toe aan .env\n\nMaak gratis account op https://console.groq.com"
        
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
                        error = await response.text()
                        logger.error(f"❌ AI error {response.status}: {error[:200]}")
                        return f"❌ AI fout: HTTP {response.status}"
                    
                    data = await response.json()
                    if "choices" in data and data["choices"]:
                        return data["choices"][0]["message"]["content"].strip()
                    return "❌ Geen antwoord van AI"
        
        except asyncio.TimeoutError:
            return "⏱️ AI timeout - probeer opnieuw"
        except Exception as e:
            logger.exception(f"AI error: {e}")
            return f"❌ AI fout: {str(e)[:100]}"


# ============================================================
# LOGGING & CONSTANTS
# ============================================================

SETTINGS_FILE = Path("runtime_settings.json")
MARKTPLAATS_API = "https://www.marktplaats.nl/lrp/api/search"
MARKTPLAATS_API_LIMIT = 10
MIN_COMPARISON_SAMPLES = 1
KENTEKEN_PATTERN = re.compile(r"\b([A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3})\b")

class ColoredFormatter(logging.Formatter):
    COLORS = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[35m"}
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

def build_marktplaats_api_url(query: str, limit: int = MARKTPLAATS_API_LIMIT) -> str:
    return f"{MARKTPLAATS_API}?{urlencode({'query': query, 'limit': str(limit)})}"

def make_marktplaats_url(value: str) -> str:
    if not value:
        return ""
    value = str(value).strip()
    if value.startswith(("http://", "https://")):
        return value
    return f"https://www.marktplaats.nl{'/' if not value.startswith('/') else ''}{value}"

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
    market_value_samples: int = 50
    market_pool_ttl_hours: int = 1
    seen_file: Path = field(default_factory=lambda: Path("seen_links.json"))

    @classmethod
    def from_env(cls):
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise ValueError("TELEGRAM_TOKEN en TELEGRAM_CHAT_ID vereist in .env")
        return cls(
            telegram_token=token, telegram_chat_id=chat_id,
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
            logger.warning(f"⚠️ Settings load error: {e}")
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
        logger.error(f"❌ Save settings error: {e}")

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
        logger.info("📤 Shutdown notification sent")
    except Exception as e:
        logger.error(f"❌ Shutdown notification failed: {e}")


# ============================================================
# HTTP CLIENT
# ============================================================

class SmartClient:
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
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
            if prev:
                elapsed = (datetime.now() - prev).total_seconds()
                if elapsed < 1.5:
                    await asyncio.sleep(1.5 - elapsed)
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
            raise RuntimeError("SmartClient not initialized")
        
        await self._rate_limit(url)
        
        async with self._semaphore:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    self._stats["requests"] += 1
                    headers = {"User-Agent": self._rotate_ua(), "Accept": "text/html,application/json", "Accept-Language": "nl-NL,nl;q=0.9"}
                    
                    async with self._session.get(url, headers=headers, ssl=False) as resp:
                        if resp.status == 200:
                            return await resp.text(errors="replace")
                        if resp.status == 400:
                            logger.warning(f"⚠️ HTTP 400: {url}")
                            return None
                        if resp.status in (403, 429, 500, 502, 503):
                            wait = min(2 ** attempt, 30)
                            logger.warning(f"⚠️ HTTP {resp.status} - wait {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        return None
                
                except asyncio.CancelledError:
                    raise
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
        # Next.js data
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
                        if price_info.get("priceCents") is not None:
                            price = ListingParser.parse_price(price_info["priceCents"], is_cents=True)
                        elif price_info.get("price") is not None:
                            price = ListingParser.parse_price(price_info["price"])
                    
                    if price is None:
                        price = ListingParser.parse_price(listing.get("priceCents"), is_cents=True) or ListingParser.parse_price(listing.get("price"))
                    
                    if title and price:
                        return title, price, desc
            except:
                pass
        
        # Fallback: meta tags
        title = None
        price = None
        desc = ""
        
        og = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.S)
        if og:
            title = unescape(og.group(1)).strip()
        
        if not title:
            tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            if tm:
                title = unescape(tm.group(1)).strip()
        
        pc = re.search(r'"priceCents"\s*:\s*"?([\d.,]+)"?', html, re.I)
        if pc:
            price = ListingParser.parse_price(pc.group(1), is_cents=True)
        
        if not price:
            pm = re.search(r'"price"\s*:\s*"?([\d.,]+)"?', html, re.I)
            if pm:
                price = ListingParser.parse_price(pm.group(1))
        
        if title and price:
            dm = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.S)
            if dm:
                desc = unescape(dm.group(1)).strip()
            return title, price, desc
        
        return None, None, ""

    @staticmethod
    def extract_km(text: str) -> Optional[int]:
        for pattern in [r"(\d{1,3}(?:[.,\s]\d{3})+)\s*km\b", r"\b(\d{4,6})\s*km\b", r"km\s*[:\-]?\s*(\d{3,6})\b"]:
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
        patterns = [r"bouwjaar\s*[:\-]?\s*(\d{4})", r"\bjaar\s*[:\-]?\s*(\d{4})", r"\b(\d{4})[- ]model\b", r"\bbj\.?\s*(\d{4})", r"\b(19\d{2}|20\d{2})\b"]
        
        for p in patterns:
            m = re.search(p, text, re.I)
            if m:
                try:
                    yr = int(m.group(1))
                    if 1990 <= yr <= current:
                        return yr
                except:
                    pass
        
        years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text) if 1990 <= int(y) <= current]
        if years:
            return Counter(years).most_common(1)[0][0]
        
        return None


# ============================================================
# DEAL QUALITY
# ============================================================

class DealQuality(Enum):
    GODLIKE = "GODLIKE"
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    AVERAGE = "AVERAGE"
    WATCHLIST = "WATCHLIST"
    POOR = "POOR"

@dataclass
class MarketAnalysis:
    asking_price: int
    market_value: Optional[float]
    profit_potential: Optional[int]
    profit_percentage: Optional[float]
    is_profitable: bool
    is_estimated: bool = False

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
    
    motivated_seller: bool = field(default=False, init=False)
    is_dealer: bool = field(default=False, init=False)
    market_analysis: Optional[MarketAnalysis] = field(default=None, init=False)
    deal_quality: DealQuality = field(default=DealQuality.POOR, init=False)
    
    @property
    def is_good_deal(self) -> bool:
        return self.deal_quality in {DealQuality.GODLIKE, DealQuality.EXCELLENT, DealQuality.GOOD, DealQuality.AVERAGE, DealQuality.WATCHLIST}
    
    def format_message(self) -> str:
        emoji = {"GODLIKE": "💎💎💎", "EXCELLENT": "🔥🔥", "GOOD": "🔥", "AVERAGE": "✅", "WATCHLIST": "👀", "POOR": "❌"}[self.deal_quality.value]
        
        result = f"{emoji} {self.deal_quality.value}\n{'━'*40}\n{self.title}\n\n"
        result += f"{self.brand or '?'} {self.model or '?'} | {self.year or '?'} | {f'{self.km:,}' if self.km else '?'} km\n"
        
        if self.market_analysis and self.market_analysis.market_value:
            profit = self.market_analysis.profit_potential or 0
            pct = self.market_analysis.profit_percentage or 0
            est = "\n⚠️ Schatting" if self.market_analysis.is_estimated else ""
            result += f"\n💰 WINST:\nVraag: €{self.price:,}\nMarkt: €{self.market_analysis.market_value:,.0f}\nWinst: €{profit:,} ({pct:.0f}%){est}\n"
        
        result += f"{'━'*40}\n🔗 {self.url}"
        return result


# ============================================================
# SEEN LINKS MANAGER (simplified)
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
# SCRAPER (simplified for demo - voeg volledige logica toe indien nodig)
# ============================================================

class ProfitScraper:
    def __init__(self, filter_config, seen_manager, settings, *args):
        self.filter_config = filter_config
        self.seen_manager = seen_manager
        self.settings = settings
        self._stats = {"scans": 0, "listings_checked": 0, "deals_found": 0, "watchlist_deals": 0}
        self.found_deals: List[Listing] = []
    
    async def scan_all(self, client: SmartClient) -> List[Listing]:
        self._stats["scans"] += 1
        logger.info(f"🚀 SCAN #{self._stats['scans']}")
        
        # Simplified: return empty voor nu (voeg volledige scraper logica toe)
        return []
    
    def get_stats(self) -> Dict:
        return self._stats.copy()
    
    def get_top_deals(self, n: int = 5) -> List[Listing]:
        return sorted(self.found_deals, key=lambda x: 0, reverse=True)[:n]


# ============================================================
# TELEGRAM NOTIFIER
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
            logger.info(f"✅ Message sent (ID: {result.message_id})")
            return True
        except Exception as e:
            logger.error(f"❌ Telegram error: {e}")
            return False
    
    async def send_listing(self, listing: Listing) -> bool:
        return await self.send_message(listing.format_message())
    
    async def send_startup(self, min_profit: int) -> bool:
        return await self.send_message(f"💰 BOT ACTIEF\n📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%H:%M:%S')}\n🎯 Min €{min_profit}\n🤖 AI enabled")


# ============================================================
# BOT COMMANDS MET AI
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
        await update.message.reply_text(
            "🚀 *Auto Profit Bot + AI*\n\n"
            "✅ Automatisch deals scannen\n"
            "🤖 AI assistent (Groq)\n\n"
            "*Wat je kunt doen:*\n"
            "💬 Stel een vraag aan AI\n"
            "🔗 Stuur Marktplaats link\n"
            "🪪 Stuur kenteken\n\n"
            "/help voor alle commando's"
        )
    
    async def help(self, update, context):
        if not self._is_authorized(update):
            return
        await update.message.reply_text(
            "❓ *Commando's:*\n\n"
            "/stats - Statistieken\n"
            "/top - Top deals\n"
            "/pause - Pauzeren\n"
            "/resume - Hervatten\n"
            "/ai [vraag] - AI vraag\n\n"
            "*Auto features:*\n"
            "🔗 Link → Analyse\n"
            "🪪 Kenteken → RDW\n"
            "💬 Tekst → AI chat"
        )
    
    async def stats(self, update, context):
        if not self._is_authorized(update):
            return
        stats = self.scraper.get_stats()
        status = "⏸️ PAUSED" if self.settings.paused else "▶️ ACTIVE"
        await update.message.reply_text(
            f"📊 *Stats*\n\n{status}\n"
            f"Scans: {stats['scans']}\n"
            f"Checked: {stats['listings_checked']}\n"
            f"Deals: {stats['deals_found']}"
        )
    
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
            lines.append(f"{i}. {d.title[:50]}")
        await update.message.reply_text("\n".join(lines))
    
    async def ai_command(self, update: Update, context):
        if not self._is_authorized(update):
            return
        if not context.args:
            await update.message.reply_text("💬 Gebruik: /ai [vraag]\n\nBv: /ai Is €4500 voor Aygo 2015 met 80k km een goede deal?")
            return
        await self._ai_chat(update, " ".join(context.args))
    
    async def handle_text(self, update: Update, context):
        if not self._is_authorized(update):
            return
        text = update.message.text.strip()
        
        # Marktplaats link
        if "marktplaats.nl" in text.lower():
            await self._analyze_link(update, text)
            return
        
        # Kenteken
        kenteken = extract_kenteken(text)
        if kenteken:
            await self._lookup_rdw(update, kenteken)
            return
        
        # AI chat
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
            
            ai_prompt = f"Analyseer in 3 zinnen:\n{title}\nPrijs: €{price:,}\nJaar: {year or '?'}\nKM: {f'{km:,}' if km else '?'}\n\nGoede deal? Realistische prijs?"
            ai_resp = await self.ai.chat(ai_prompt, "Je bent auto-expert. Kort advies in 3 zinnen.")
            
            msg = f"🚗 *{title}*\n\n💰 €{price:,}\n📅 {year or '?'} | 📏 {f'{km:,} km' if km else '?'}\n\n🤖 *Advies:*\n{ai_resp or 'AI niet beschikbaar'}\n\n🔗 {url}"
            await update.message.reply_text(msg[:4000])
        
        except Exception as e:
            logger.exception(f"Link error: {e}")
            await update.message.reply_text(f"❌ Fout: {str(e)[:100]}")
    
    async def _lookup_rdw(self, update: Update, kenteken: str):
        await update.message.reply_text(f"🔍 {kenteken}...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://opendata.rdw.nl/resource/m9d7-ebf2.json?kenteken={kenteken}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        await update.message.reply_text("❌ RDW fout")
                        return
                    data = await resp.json()
            
            if not data:
                await update.message.reply_text(f"❌ {kenteken} niet gevonden")
                return
            
            info = data[0]
            msg = f"🪪 *{kenteken}*\n\n🚗 {info.get('merk', '?')} {info.get('handelsbenaming', '?')}\n📅 {info.get('datum_eerste_toelating', '?')}\n⛽ {info.get('brandstof_omschrijving', '?')}\n🎨 {info.get('eerste_kleur', '?')}"
            await update.message.reply_text(msg)
        
        except Exception as e:
            logger.exception(f"RDW error: {e}")
            await update.message.reply_text(f"❌ {str(e)[:100]}")
    
    async def _ai_chat(self, update: Update, question: str):
        user_id = update.effective_chat.id
        if user_id not in self.conversations:
            self.conversations[user_id] = []
        
        thinking = await update.message.reply_text("🤔 ...")
        
        try:
            response = await self.ai.chat(
                question,
                "Je bent Nederlandse auto-expert. Kort advies (max 4 zinnen). Gebruik emoji.",
                self.conversations[user_id]
            )
            
            if not response:
                response = "❌ Geen antwoord"
            
            self.conversations[user_id].append({"role": "user", "content": question})
            self.conversations[user_id].append({"role": "assistant", "content": response})
            self.conversations[user_id] = self.conversations[user_id][-10:]
            
            await thinking.edit_text(response[:4000])
        
        except Exception as e:
            logger.exception(f"AI chat error: {e}")
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
        logger.info("✅ Ready with AI!")
    
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