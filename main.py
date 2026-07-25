import os
import json
import logging
import asyncio
import aiohttp
import re
import signal
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple
from datetime import datetime, timedelta
from enum import Enum
from telegram.ext import ApplicationBuilder
import sys

# ---------------------------------------------------------
# CONSTANTS & ENUMS
# ---------------------------------------------------------

class DealQuality(Enum):
    """Kwaliteit van de deal."""
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    AVERAGE = "AVERAGE"
    POOR = "POOR"

class Platform(Enum):
    """Ondersteunde platforms."""
    MARKTPLAATS = "marktplaats"
    AUTOSCOUT24 = "autoscout24"
    GASPEDAAL = "gaspedaal"
    AUTOTRADER = "autotrader"
    SCHADEAUTOS = "schadeautos"

PRICE_CENT_THRESHOLD = 100_000

# ---------------------------------------------------------
# CONFIG MANAGEMENT
# ---------------------------------------------------------

@dataclass(frozen=True)
class BotConfig:
    """Immutable configuratie voor de bot."""
    
    telegram_token: str
    telegram_chat_id: str
    check_interval: int = 5  # RSS check elke 5 sec
    max_concurrent_requests: int = 10  # Meer voor multi-platform
    request_timeout: int = 15
    max_retries: int = 3
    max_links_per_model: int = 15
    
    max_km: int = 220_000
    price_per_km_limit: float = 0.035
    seen_max_age_days: int = 30
    low_km_threshold: int = 120_000
    high_km_threshold: int = 180_000
    low_km_bonus: int = 300
    high_km_penalty: int = 400
    
    seen_file: Path = field(default_factory=lambda: Path("seen_links.json"))
    
    @classmethod
    def from_env(cls) -> 'BotConfig':
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        if not token:
            raise ValueError("TELEGRAM_TOKEN environment variable is vereist")
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID environment variable is vereist")
        
        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            check_interval=int(os.getenv("CHECK_INTERVAL", 5)),
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", 10)),
        )

@dataclass(frozen=True)
class FilterConfig:
    """Configuratie voor filtering."""
    models: List[str]
    motivation_words: List[str]
    dealer_words: List[str]
    red_flags: List[str]
    quality_indicators: List[str]
    buy_limits: List[Dict[str, int]]
    
    @classmethod
    def from_file(cls, path: Path) -> 'FilterConfig':
        if not path.exists():
            raise FileNotFoundError(f"filters.json niet gevonden")
        
        data = json.loads(path.read_text())
        return cls(**data)

@dataclass
class PlatformConfig:
    """Config voor een platform."""
    name: str
    enabled: bool
    priority: int
    base_url: str
    search_template: str
    listing_pattern: str
    supports_rss: bool = False
    rss_template: Optional[str] = None

class PlatformsConfig:
    """Manager voor alle platforms."""
    
    def __init__(self, config_file: Path = Path("platforms.json")):
        self.config_file = config_file
        self.platforms: Dict[str, PlatformConfig] = {}
        self._load()
    
    def _load(self):
        if not self.config_file.exists():
            self._create_default()
        
        data = json.loads(self.config_file.read_text())
        
        for name, config in data.items():
            self.platforms[name] = PlatformConfig(
                name=name,
                **config
            )
    
    def _create_default(self):
        """Maak default platforms.json."""
        default = {
            "marktplaats": {
                "enabled": True,
                "priority": 1,
                "base_url": "https://www.marktplaats.nl",
                "search_template": "/lrp/api/search?query={model}&limit=25",
                "listing_pattern": r'href="(/v/auto[^"#?]+)"',
                "supports_rss": True,
                "rss_template": "/lrp/api/search?query={model}&searchInTitleAndDescription=true&limit=25"
            },
            "autoscout24": {
                "enabled": True,
                "priority": 2,
                "base_url": "https://www.autoscout24.nl",
                "search_template": "/lst?sort=age&desc=1&search_id=&ustate=N%2CU&size=20&page=1&cy=NL&atype=C&make=&model=&keyw={model}",
                "listing_pattern": r'href="(/aanbod/[^"]+)"',
                "supports_rss": False
            },
            "gaspedaal": {
                "enabled": True,
                "priority": 3,
                "base_url": "https://www.gaspedaal.nl",
                "search_template": "/occasions/search?q={model}&sort=created_desc",
                "listing_pattern": r'href="(/occasions/[^"]+)"',
                "supports_rss": False
            },
            "autotrader": {
                "enabled": True,
                "priority": 4,
                "base_url": "https://www.autotrader.nl",
                "search_template": "/auto/occasions?search={model}&sortorder=4",
                "listing_pattern": r'href="(/auto/[^"]+)"',
                "supports_rss": False
            },
            "schadeautos": {
                "enabled": False,
                "priority": 5,
                "base_url": "https://www.schadeautos.nl",
                "search_template": "/zoeken?search={model}&sort=date_desc",
                "listing_pattern": r'href="(/auto/[^"]+)"',
                "supports_rss": False
            }
        }
        
        self.config_file.write_text(json.dumps(default, indent=2))
    
    def get_enabled(self) -> List[PlatformConfig]:
        """Krijg enabled platforms gesorteerd op priority."""
        enabled = [p for p in self.platforms.values() if p.enabled]
        return sorted(enabled, key=lambda p: p.priority)

# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[36m',
        'INFO': '\033[32m',
        'WARNING': '\033[33m',
        'ERROR': '\033[31m',
        'CRITICAL': '\033[35m',
    }
    RESET = '\033[0m'
    
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        return super().format(record)

def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("multi-platform-bot")
    logger.setLevel(level)
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        ColoredFormatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(console_handler)
    
    file_handler = logging.FileHandler("bot.log")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(file_handler)
    
    return logger

logger = setup_logging()

# ---------------------------------------------------------
# DATACLASSES
# ---------------------------------------------------------

@dataclass
class Listing:
    """Representatie van een listing."""
    url: str
    title: str
    price: int
    platform: str
    km: Optional[int] = None
    year: Optional[int] = None
    description: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    
    buy_limit: Optional[int] = field(default=None, init=False, repr=False)
    motivated_seller: bool = field(default=False, init=False, repr=False)
    is_dealer: bool = field(default=False, init=False, repr=False)
    has_red_flags: bool = field(default=False, init=False, repr=False)
    quality_score: int = field(default=0, init=False, repr=False)
    deal_quality: DealQuality = field(default=DealQuality.POOR, init=False, repr=False)
    
    def __post_init__(self):
        if self.price < 0:
            raise ValueError(f"Prijs kan niet negatief zijn: {self.price}")
        if self.km is not None and self.km < 0:
            raise ValueError(f"KM kan niet negatief zijn: {self.km}")
        if self.year is not None and (self.year < 1960 or self.year > datetime.now().year + 1):
            raise ValueError(f"Ongeldig bouwjaar: {self.year}")
    
    def analyze(self, filter_config: FilterConfig, bot_config: BotConfig) -> None:
        """Analyseer de listing."""
        combined_text = f"{self.title} {self.description}".lower()
        
        self.is_dealer = any(word in combined_text for word in filter_config.dealer_words)
        self.motivated_seller = any(word in combined_text for word in filter_config.motivation_words)
        self.has_red_flags = any(flag in combined_text for flag in filter_config.red_flags)
        
        self.quality_score = sum(
            1 for indicator in filter_config.quality_indicators 
            if indicator in combined_text
        )
        
        self.buy_limit = self._calculate_buy_limit(filter_config, bot_config)
        self.deal_quality = self._calculate_deal_quality(bot_config, filter_config)
    
    def _calculate_buy_limit(self, filter_config: FilterConfig, bot_config: BotConfig) -> Optional[int]:
        if not self.year or not self.km:
            return None
        if self.km > bot_config.max_km:
            return None
        
        base = next(
            (limit["base"] for limit in filter_config.buy_limits 
             if self.year >= limit["min_year"]),
            filter_config.buy_limits[-1]["base"],
        )
        
        if self.km < bot_config.low_km_threshold:
            base += bot_config.low_km_bonus
        elif self.km > bot_config.high_km_threshold:
            base -= bot_config.high_km_penalty
        
        return base
    
    def _calculate_deal_quality(self, bot_config: BotConfig, filter_config: FilterConfig) -> DealQuality:
        if self.is_dealer or self.has_red_flags or not self.buy_limit:
            return DealQuality.POOR
        
        if self.price > self.buy_limit:
            return DealQuality.POOR
        
        if self.km and self.price / self.km > bot_config.price_per_km_limit:
            return DealQuality.POOR
        
        discount_pct = ((self.buy_limit - self.price) / self.buy_limit) * 100
        
        score = 0
        
        if discount_pct >= 30:
            score += 50
        elif discount_pct >= 20:
            score += 40
        elif discount_pct >= 10:
            score += 30
        else:
            score += 20
        
        if self.motivated_seller:
            score += 20
        
        score += min(20, self.quality_score * 4)
        
        if self.km:
            if self.km < 100_000:
                score += 10
            elif self.km < 150_000:
                score += 5
        
        if score >= 80:
            return DealQuality.EXCELLENT
        elif score >= 60:
            return DealQuality.GOOD
        elif score >= 40:
            return DealQuality.AVERAGE
        else:
            return DealQuality.POOR
    
    @property
    def is_good_deal(self) -> bool:
        return self.deal_quality in (DealQuality.EXCELLENT, DealQuality.GOOD)
    
    def format_message(self) -> str:
        """Premium notificatie format."""
        quality_emoji = {
            DealQuality.EXCELLENT: "🔥🔥🔥",
            DealQuality.GOOD: "🔥",
            DealQuality.AVERAGE: "✅",
            DealQuality.POOR: "❌",
        }
        
        platform_emoji = {
            "marktplaats": "🟠",
            "autoscout24": "🟢",
            "gaspedaal": "🔵",
            "autotrader": "🟡",
            "schadeautos": "🔴",
        }
        
        emoji = quality_emoji[self.deal_quality]
        platform_em = platform_emoji.get(self.platform, "⚪")
        
        discount = ""
        if self.buy_limit:
            discount_pct = ((self.buy_limit - self.price) / self.buy_limit) * 100
            discount = f" 💰 -{discount_pct:.0f}%"
        
        urgency = ""
        if self.motivated_seller:
            urgency = "\n🚨 GEMOTIVEERDE VERKOPER - REAGEER NU!"
        
        quality_info = ""
        if self.quality_score > 0:
            quality_info = f"\n⭐ Kwaliteit: {self.quality_score}/10"
        
        time_posted = self.timestamp.strftime("%H:%M:%S")
        km_str = f"{self.km:,}" if self.km else "onbekend"
        year_str = str(self.year) if self.year else "onbekend"
        
        return (
            f"{emoji} {self.deal_quality.value} DEAL{discount}\n"
            f"{platform_em} Platform: {self.platform.upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 {self.title}\n\n"
            f"📅 Bouwjaar: {year_str}\n"
            f"🚗 KM-stand: {km_str}\n"
            f"💶 Prijs: €{self.price:,}\n"
            f"🎯 Max koop: €{self.buy_limit:,}\n"
            f"⏰ Tijd: {time_posted}\n"
            f"{quality_info}"
            f"{urgency}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 {self.url}\n\n"
            f"💡 Direct screenshot + bellen!"
        )

# ---------------------------------------------------------
# SEEN LINKS MANAGER
# ---------------------------------------------------------

class SeenLinksManager:
    def __init__(self, path: Path, max_age_days: int):
        self._path = path
        self._max_age = timedelta(days=max_age_days)
        self._data: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._load()
    
    def _load(self):
        if not self._path.exists():
            return
        
        try:
            raw_data = json.loads(self._path.read_text())
            self._data = {
                url: datetime.fromisoformat(ts)
                for url, ts in raw_data.items()
            }
            logger.info(f"Geladen: {len(self._data)} geziene links")
        except Exception as e:
            logger.warning(f"Kon seen links niet laden: {e}")
            self._data = {}
    
    def _save(self):
        if not self._dirty:
            return
        
        try:
            raw_data = {
                url: dt.isoformat()
                for url, dt in self._data.items()
            }
            self._path.write_text(json.dumps(raw_data, indent=2))
            self._dirty = False
        except Exception as e:
            logger.error(f"Kon seen links niet opslaan: {e}")
    
    def _clean_expired(self) -> int:
        cutoff = datetime.now() - self._max_age
        original_count = len(self._data)
        
        self._data = {
            url: dt
            for url, dt in self._data.items()
            if dt > cutoff
        }
        
        removed = original_count - len(self._data)
        if removed > 0:
            self._dirty = True
            logger.info(f"Verwijderd: {removed} verlopen links")
        
        return removed
    
    async def contains(self, url: str) -> bool:
        async with self._lock:
            return url in self._data
    
    async def add(self, url: str):
        async with self._lock:
            self._data[url] = datetime.now()
            self._dirty = True
    
    async def cleanup_and_save(self):
        async with self._lock:
            self._clean_expired()
            self._save()

# ---------------------------------------------------------
# ANALYTICS
# ---------------------------------------------------------

class DealAnalytics:
    def __init__(self, db_file: Path = Path("deals_history.json")):
        self.db_file = db_file
        self.deals: List[Dict] = self._load()
    
    def _load(self) -> List[Dict]:
        if self.db_file.exists():
            try:
                return json.loads(self.db_file.read_text())
            except:
                return []
        return []
    
    def _save(self):
        self.db_file.write_text(json.dumps(self.deals, indent=2, default=str))
    
    def add_deal(self, listing: Listing):
        deal_data = {
            'url': listing.url,
            'title': listing.title,
            'price': listing.price,
            'platform': listing.platform,
            'km': listing.km,
            'year': listing.year,
            'buy_limit': listing.buy_limit,
            'deal_quality': listing.deal_quality.value,
            'quality_score': listing.quality_score,
            'timestamp': datetime.now().isoformat(),
        }
        
        self.deals.append(deal_data)
        self._save()
    
    def get_stats(self, days: int = 7) -> Dict:
        cutoff = datetime.now() - timedelta(days=days)
        
        recent_deals = [
            d for d in self.deals
            if datetime.fromisoformat(d['timestamp']) > cutoff
        ]
        
        if not recent_deals:
            return {
                'total_deals': 0,
                'by_platform': {},
                'avg_price': 0,
                'excellent_count': 0,
            }
        
        total = len(recent_deals)
        avg_price = sum(d['price'] for d in recent_deals) / total
        
        by_platform = {}
        for deal in recent_deals:
            platform = deal.get('platform', 'unknown')
            by_platform[platform] = by_platform.get(platform, 0) + 1
        
        return {
            'total_deals': total,
            'by_platform': by_platform,
            'avg_price': int(avg_price),
            'excellent_count': sum(1 for d in recent_deals if d['deal_quality'] == 'EXCELLENT'),
        }
    
    def generate_report(self, days: int = 7) -> str:
        stats = self.get_stats(days)
        
        if stats['total_deals'] == 0:
            return f"📊 Geen deals in afgelopen {days} dagen"
        
        platform_breakdown = "\n".join([
            f"  • {platform}: {count}"
            for platform, count in stats['by_platform'].items()
        ])
        
        return (
            f"📊 DEAL RAPPORT - {days} dagen\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Totaal: {stats['total_deals']}\n"
            f"🔥 Excellent: {stats['excellent_count']}\n"
            f"💰 Gem. prijs: €{stats['avg_price']:,}\n\n"
            f"📱 Per platform:\n{platform_breakdown}"
        )

# ---------------------------------------------------------
# HTTP CLIENT
# ---------------------------------------------------------

class MultiPlatformClient:
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    
    def __init__(self, config: BotConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._stats = {"requests": 0, "errors": 0}
    
    async def __aenter__(self):
        connector = aiohttp.TCPConnector(
            limit=self.config.max_concurrent_requests,
            limit_per_host=5,
            ttl_dns_cache=300,
        )
        
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
        
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": self.USER_AGENT},
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()
    
    async def get_html(self, url: str) -> Optional[str]:
        if not self._session:
            raise RuntimeError("Client niet geïnitialiseerd")
        
        async with self._semaphore:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    self._stats["requests"] += 1
                    
                    async with self._session.get(url) as response:
                        if response.status == 200:
                            return await response.text()
                        elif response.status in (429, 503):
                            wait_time = 2 ** attempt
                            self._stats["errors"] += 1
                            logger.warning(f"Rate limited, wacht {wait_time}s")
                            await asyncio.sleep(wait_time)
                        elif response.status == 404:
                            return None
                        else:
                            logger.warning(f"HTTP {response.status} voor {url}")
                            return None
                
                except Exception as e:
                    self._stats["errors"] += 1
                    if attempt < self.config.max_retries:
                        wait_time = 2 ** attempt
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Gefaald na {self.config.max_retries} pogingen: {url}")
                        return None
        
        return None
    
    def get_stats(self) -> Dict[str, int]:
        return self._stats.copy()

# ---------------------------------------------------------
# PARSING
# ---------------------------------------------------------

class ListingParser:
    
    @staticmethod
    def parse_page(html: str, platform: str) -> Tuple[Optional[str], Optional[int], str]:
        """Parse listing page - platform agnostic."""
        
        # Probeer eerst JSON (Marktplaats)
        if platform == "marktplaats":
            result = ListingParser._parse_marktplaats_json(html)
            if result[0] and result[1]:
                return result
        
        # Fallback: generic HTML parsing
        return ListingParser._parse_generic_html(html)
    
    @staticmethod
    def _parse_marktplaats_json(html: str) -> Tuple[Optional[str], Optional[int], str]:
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not match:
            return None, None, ""
        
        try:
            data = json.loads(match.group(1))
            listing = data.get("props", {}).get("pageProps", {}).get("listing", {})
            
            title = listing.get("title", "").strip()
            description = listing.get("description", "").strip()
            price_info = listing.get("priceInfo", {})
            
            raw_price = (
                price_info.get("priceCents") or
                price_info.get("price") or
                price_info.get("askingPrice")
            )
            
            if not title or raw_price is None:
                return None, None, ""
            
            price = int(raw_price)
            if price > PRICE_CENT_THRESHOLD:
                price = price // 100
            
            return title, price, description
        
        except Exception:
            return None, None, ""
    
    @staticmethod
    def _parse_generic_html(html: str) -> Tuple[Optional[str], Optional[int], str]:
        """Generic HTML parser voor andere platforms."""
        
        # Probeer title
        title_patterns = [
            r'<h1[^>]*>(.*?)</h1>',
            r'<title>(.*?)</title>',
            r'og:title"[^>]*content="([^"]+)"',
        ]
        
        title = None
        for pattern in title_patterns:
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if match:
                title = re.sub(r'<[^>]+>', '', match.group(1)).strip()
                break
        
        # Probeer price
        price_patterns = [
            r'€\s*([0-9.,]+)',
            r'"price[^"]*"[^>]*[>:]\s*"?([0-9.,]+)',
            r'prijs[^0-9]*([0-9.,]+)',
        ]
        
        price = None
        for pattern in price_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            if matches:
                # Neem hoogste prijs (vaak de vraagprijs)
                prices = []
                for match in matches:
                    clean = re.sub(r'[.,]', '', match)
                    try:
                        p = int(clean)
                        if 500 < p < 100000:  # Redelijke auto prijs
                            prices.append(p)
                    except:
                        continue
                
                if prices:
                    price = max(prices)
                    break
        
        # Description
        desc_match = re.search(r'<meta[^>]*description[^>]*content="([^"]+)"', html, re.IGNORECASE)
        description = desc_match.group(1) if desc_match else ""
        
        return title, price, description
    
    @staticmethod
    def extract_km(text: str) -> Optional[int]:
        pattern = r'(\d{1,3}(?:[.,\s]\d{3})+|\d+)\s*km'
        match = re.search(pattern, text.lower())
        
        if match:
            raw = re.sub(r'[.,\s]', '', match.group(1))
            try:
                km = int(raw)
                if 0 < km < 1_000_000:
                    return km
            except ValueError:
                pass
        
        return None
    
    @staticmethod
    def extract_year(text: str) -> Optional[int]:
        current_year = datetime.now().year
        pattern = r'\b(19[6-9]\d|20[0-2]\d)\b'
        matches = re.findall(pattern, text)
        
        if matches:
            valid_years = [int(y) for y in matches if 1960 <= int(y) <= current_year + 1]
            if valid_years:
                return max(valid_years)
        
        return None
    
    @staticmethod
    def extract_listing_links(html: str, pattern: str, base_url: str, max_links: int = 20) -> List[str]:
        """Extract listing URLs."""
        links = re.findall(pattern, html)
        unique_links = list(dict.fromkeys(links))[:max_links]
        
        # Converteer naar absolute URLs
        absolute_links = []
        for link in unique_links:
            if link.startswith('http'):
                absolute_links.append(link)
            else:
                absolute_links.append(f"{base_url}{link}")
        
        return absolute_links

# ---------------------------------------------------------
# PLATFORM SCRAPERS
# ---------------------------------------------------------

class PlatformScraper:
    """Base class voor platform scrapers."""
    
    def __init__(self, platform_config: PlatformConfig, filter_config: FilterConfig):
        self.config = platform_config
        self.filter_config = filter_config
        self._seen_items: Set[str] = set()
    
    async def search_model(self, model: str, client: MultiPlatformClient) -> List[str]:
        """Zoek model op platform."""
        search_url = self.config.base_url + self.config.search_template.format(model=model)
        
        html = await client.get_html(search_url)
        if not html:
            return []
        
        links = ListingParser.extract_listing_links(
            html,
            self.config.listing_pattern,
            self.config.base_url,
            max_links=15
        )
        
        return links
    
    async def check_rss(self, model: str, client: MultiPlatformClient) -> List[str]:
        """Check RSS feed (alleen als ondersteund)."""
        if not self.config.supports_rss or not self.config.rss_template:
            return []
        
        rss_url = self.config.base_url + self.config.rss_template.format(model=model)
        
        data = await client.get_html(rss_url)
        if not data:
            return []
        
        new_listings = []
        
        try:
            # Marktplaats API returnt JSON
            json_data = json.loads(data)
            listings = json_data.get('listings', [])
            
            for listing in listings:
                listing_id = listing.get('itemId')
                vip_url = listing.get('vipUrl')
                
                if listing_id and listing_id not in self._seen_items:
                    self._seen_items.add(listing_id)
                    
                    if vip_url:
                        new_listings.append(f"{self.config.base_url}{vip_url}")
            
            # Cleanup
            if len(self._seen_items) > 1000:
                self._seen_items = set(list(self._seen_items)[-1000:])
        
        except Exception as e:
            logger.debug(f"RSS parse error voor {self.config.name}: {e}")
        
        return new_listings

# ---------------------------------------------------------
# MAIN SCRAPER
# ---------------------------------------------------------

class MultiPlatformScraper:
    
    def __init__(
        self,
        bot_config: BotConfig,
        filter_config: FilterConfig,
        platforms_config: PlatformsConfig,
        seen_manager: SeenLinksManager,
        analytics: DealAnalytics,
    ):
        self.bot_config = bot_config
        self.filter_config = filter_config
        self.platforms_config = platforms_config
        self.seen_manager = seen_manager
        self.analytics = analytics
        
        # Maak scrapers voor elk platform
        self.scrapers: Dict[str, PlatformScraper] = {}
        for platform_config in platforms_config.get_enabled():
            self.scrapers[platform_config.name] = PlatformScraper(
                platform_config,
                filter_config
            )
        
        self._stats = {
            'scans': 0,
            'listings_checked': 0,
            'deals_found': 0,
        }
    
    async def process_listing(
        self,
        url: str,
        platform: str,
        client: MultiPlatformClient,
    ) -> Optional[Listing]:
        """Process een listing."""
        
        if await self.seen_manager.contains(url):
            return None
        
        html = await client.get_html(url)
        if not html:
            await self.seen_manager.add(url)
            return None
        
        title, price, description = ListingParser.parse_page(html, platform)
        if not title or not price:
            await self.seen_manager.add(url)
            return None
        
        km = ListingParser.extract_km(html)
        year = ListingParser.extract_year(title) or ListingParser.extract_year(description)
        
        try:
            listing = Listing(
                url=url,
                title=title,
                price=price,
                platform=platform,
                km=km,
                year=year,
                description=description,
            )
            listing.analyze(self.filter_config, self.bot_config)
        except ValueError as e:
            logger.warning(f"Invalid listing: {e}")
            await self.seen_manager.add(url)
            return None
        
        self._stats['listings_checked'] += 1
        
        if not listing.is_good_deal:
            await self.seen_manager.add(url)
            return None
        
        self._stats['deals_found'] += 1
        await self.seen_manager.add(url)
        
        logger.info(
            f"🔥 Deal: {listing.deal_quality.value} | "
            f"{platform.upper()} | €{price} | {km}km | {url}"
        )
        
        return listing
    
    async def scan_platform_model(
        self,
        platform_name: str,
        model: str,
        client: MultiPlatformClient,
    ) -> List[Listing]:
        """Scan een model op een platform."""
        
        scraper = self.scrapers.get(platform_name)
        if not scraper:
            return []
        
        # Probeer eerst RSS (als ondersteund)
        if scraper.config.supports_rss:
            links = await scraper.check_rss(model, client)
            if links:
                logger.info(f"📡 {platform_name.upper()}: {len(links)} nieuwe via RSS voor '{model}'")
        else:
            # Anders gewone search
            links = await scraper.search_model(model, client)
            if links:
                logger.info(f"🔍 {platform_name.upper()}: {len(links)} gevonden voor '{model}'")
        
        if not links:
            return []
        
        # Process alle listings
        tasks = [
            self.process_listing(link, platform_name, client)
            for link in links
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        deals = [r for r in results if isinstance(r, Listing) and r is not None]
        return deals
    
    async def scan_all(self, client: MultiPlatformClient) -> List[Listing]:
        """Scan ALLE platforms en modellen."""
        
        self._stats['scans'] += 1
        
        all_deals = []
        
        # Voor elk platform
        for platform_name in self.scrapers.keys():
            # Voor elk model
            tasks = [
                self.scan_platform_model(platform_name, model, client)
                for model in self.filter_config.models
            ]
            
            results = await asyncio.gather(*tasks)
            
            platform_deals = [deal for model_deals in results for deal in model_deals]
            all_deals.extend(platform_deals)
        
        logger.info(f"✅ Scan compleet: {len(all_deals)} deals uit {self._stats['listings_checked']} listings")
        
        return all_deals
    
    def get_stats(self) -> Dict:
        return self._stats.copy()

# ---------------------------------------------------------
# TELEGRAM NOTIFIER
# ---------------------------------------------------------

class TelegramNotifier:
    
    def __init__(self, app, chat_id: str):
        self.app = app
        self.chat_id = chat_id
        self._stats = {"sent": 0, "errors": 0}
        self._lock = asyncio.Lock()
    
    async def send_message(self, message: str) -> bool:
        async with self._lock:
            try:
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    disable_web_page_preview=True,
                )
                self._stats["sent"] += 1
                return True
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"Telegram error: {e}")
                return False
    
    async def send_listing(self, listing: Listing) -> bool:
        return await self.send_message(listing.format_message())
    
    async def send_startup(self, platforms: List[str]) -> bool:
        message = (
            "🤖 Multi-Platform Deal Bot ACTIEF\n\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🌐 Platforms: {', '.join(platforms)}\n"
            f"🚗 Modellen: {len(platforms)} actief\n"
            "✅ Monitoring gestart"
        )
        return await self.send_message(message)
    
    async def send_stats(self, stats: Dict) -> bool:
        message = (
            "📊 BOT STATISTIEKEN\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 Scans: {stats.get('scans', 0)}\n"
            f"📋 Bekeken: {stats.get('listings_checked', 0)}\n"
            f"✅ Deals: {stats.get('deals_found', 0)}\n"
            f"🌐 Requests: {stats.get('http_requests', 0)}\n"
        )
        return await self.send_message(message)
    
    def get_stats(self) -> Dict[str, int]:
        return self._stats.copy()

# ---------------------------------------------------------
# MAIN BOT
# ---------------------------------------------------------

class MultiPlatformBot:
    
    def __init__(
        self,
        bot_config: BotConfig,
        filter_config: FilterConfig,
        platforms_config: PlatformsConfig,
    ):
        self.bot_config = bot_config
        self.filter_config = filter_config
        self.platforms_config = platforms_config
        
        self.seen_manager = SeenLinksManager(
            bot_config.seen_file,
            bot_config.seen_max_age_days
        )
        
        self.analytics = DealAnalytics()
        
        self.telegram_app = None
        self.notifier = None
        self.scraper = None
        
        self._shutdown = asyncio.Event()
        self._stats_counter = 0
        
        self._setup_signal_handlers()
    
    def _setup_signal_handlers(self):
        def handle_shutdown(signum, frame):
            logger.info(f"Shutdown signal: {signal.Signals(signum).name}")
            self._shutdown.set()
        
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)
    
    async def _scan_loop(self):
        logger.info("🚀 Scan loop gestart")
        
        enabled_platforms = [p.name for p in self.platforms_config.get_enabled()]
        await self.notifier.send_startup(enabled_platforms)
        
        async with MultiPlatformClient(self.bot_config) as client:
            while not self._shutdown.is_set():
                try:
                    deals = await self.scraper.scan_all(client)
                    
                    for deal in deals:
                        await self.notifier.send_listing(deal)
                        self.analytics.add_deal(deal)
                        await asyncio.sleep(1)
                    
                    await self.seen_manager.cleanup_and_save()
                    
                    # Stuur dagelijks rapport
                    if self._stats_counter % 720 == 0:  # Elke 720 * 5sec = 1 uur bij 5sec interval
                        report = self.analytics.generate_report(days=1)
                        await self.notifier.send_message(report)
                    
                    self._stats_counter += 1
                
                except Exception:
                    logger.exception("Fout tijdens scan")
                
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self.bot_config.check_interval
                    )
                    break
                except asyncio.TimeoutError:
                    pass
        
        logger.info("Scan loop gestopt")
    
    async def _post_init(self, app):
        self.telegram_app = app
        self.notifier = TelegramNotifier(app, self.bot_config.telegram_chat_id)
        self.scraper = MultiPlatformScraper(
            self.bot_config,
            self.filter_config,
            self.platforms_config,
            self.seen_manager,
            self.analytics,
        )
        
        asyncio.create_task(self._scan_loop())
    
    async def _post_shutdown(self, app):
        logger.info("Cleanup...")
        await self.seen_manager.cleanup_and_save()
        logger.info("Cleanup voltooid")
    
    def run(self):
        logger.info("🤖 Multi-Platform Deal Bot Start")
        logger.info(f"⚙️  Check interval: {self.bot_config.check_interval}s")
        logger.info(f"🌐 Platforms: {', '.join(p.name for p in self.platforms_config.get_enabled())}")
        logger.info(f"🚗 Modellen: {', '.join(self.filter_config.models)}")
        
        try:
            app = (
                ApplicationBuilder()
                .token(self.bot_config.telegram_token)
                .post_init(self._post_init)
                .post_shutdown(self._post_shutdown)
                .build()
            )
            
            app.run_polling(allowed_updates=[], drop_pending_updates=True)
        
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt")
        except Exception:
            logger.exception("Fatale fout")
            sys.exit(1)
        finally:
            logger.info("Bot gestopt")

# ---------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------

def main():
    try:
        bot_config = BotConfig.from_env()
        filter_config = FilterConfig.from_file(Path("filters.json"))
        platforms_config = PlatformsConfig()
        
        bot = MultiPlatformBot(bot_config, filter_config, platforms_config)
        bot.run()
    
    except ValueError as e:
        logger.error(f"Config error: {e}")
        sys.exit(1)
    except Exception:
        logger.exception("Startup error")
        sys.exit(1)

if __name__ == "__main__":
    main()