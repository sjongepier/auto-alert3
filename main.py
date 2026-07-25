import os
import json
import logging
import asyncio
import aiohttp
import re
import signal
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, List, Set
from datetime import datetime, timedelta
from enum import Enum
from telegram.ext import ApplicationBuilder
from functools import lru_cache
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


# Prijs threshold voor cent/euro conversie
PRICE_CENT_THRESHOLD = 100_000

# ---------------------------------------------------------
# CONFIG MANAGEMENT
# ---------------------------------------------------------

@dataclass(frozen=True)
class BotConfig:
    """Immutable configuratie voor de bot."""
    
    # Telegram
    telegram_token: str
    telegram_chat_id: str
    
    # Scraping
    check_interval: int = 30
    max_concurrent_requests: int = 5
    request_timeout: int = 10
    max_retries: int = 3
    max_links_per_model: int = 12
    
    # Filtering
    max_km: int = 220_000
    price_per_km_limit: float = 0.035
    seen_max_age_days: int = 30
    low_km_threshold: int = 120_000
    high_km_threshold: int = 180_000
    low_km_bonus: int = 300
    high_km_penalty: int = 400
    
    # Data files
    seen_file: Path = field(default_factory=lambda: Path("seen_links.json"))
    config_file: Path = field(default_factory=lambda: Path("bot_config.json"))
    
    @classmethod
    def from_env(cls) -> 'BotConfig':
        """Laad configuratie uit environment variables."""
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        if not token:
            raise ValueError("TELEGRAM_TOKEN environment variable is vereist")
        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID environment variable is vereist")
        
        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            check_interval=int(os.getenv("CHECK_INTERVAL", 30)),
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", 5)),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", 10)),
            max_retries=int(os.getenv("MAX_RETRIES", 3)),
            max_km=int(os.getenv("MAX_KM", 220_000)),
            price_per_km_limit=float(os.getenv("PRICE_PER_KM_LIMIT", 0.035)),
            seen_max_age_days=int(os.getenv("SEEN_MAX_AGE_DAYS", 30)),
        )
    
    @classmethod
    def from_file(cls, path: Path) -> 'BotConfig':
        """Laad configuratie uit JSON bestand."""
        if not path.exists():
            raise FileNotFoundError(f"Config bestand niet gevonden: {path}")
        
        data = json.loads(path.read_text())
        return cls(**data)


@dataclass(frozen=True)
class FilterConfig:
    """Configuratie voor filtering van listings."""
    models: List[str]
    motivation_words: List[str]
    dealer_words: List[str]
    buy_limits: List[Dict[str, int]]
    
    @classmethod
    def load_default(cls) -> 'FilterConfig':
        """Laad default filter configuratie."""
        return cls(
            models=["aygo", "c1", "107", "picanto", "i10", "yaris"],
            motivation_words=[
                "moet weg",
                "ivm",
                "verhuizing",
                "spoed",
                "geen tijd",
                "overcompleet",
            ],
            dealer_words=[
                "inruil",
                "garantie",
                "btw",
                "bedrijf",
                "dealer",
            ],
            buy_limits=[
                {"min_year": 2013, "base": 2800},
                {"min_year": 2008, "base": 2400},
                {"min_year": 0, "base": 2000},
            ]
        )
    
    @classmethod
    def from_file(cls, path: Path) -> 'FilterConfig':
        """Laad filter configuratie uit bestand."""
        if not path.exists():
            config = cls.load_default()
            path.write_text(json.dumps(asdict(config), indent=2))
            return config
        
        data = json.loads(path.read_text())
        return cls(**data)


# ---------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------

class ColoredFormatter(logging.Formatter):
    """Colored console output voor betere leesbaarheid."""
    
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'
    
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Setup logging met colored output en file handler."""
    logger = logging.getLogger("marktplaats-bot")
    logger.setLevel(level)
    
    # Console handler met kleuren
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        ColoredFormatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(console_handler)
    
    # File handler voor persistentie
    file_handler = logging.FileHandler("bot.log")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)
    
    return logger


logger = setup_logging()

# ---------------------------------------------------------
# DATACLASS
# ---------------------------------------------------------

@dataclass
class Listing:
    """Representatie van een Marktplaats listing."""
    url: str
    title: str
    price: int
    km: Optional[int]
    year: Optional[int]
    description: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    
    # Computed fields
    buy_limit: Optional[int] = field(default=None, init=False, repr=False)
    motivated_seller: bool = field(default=False, init=False, repr=False)
    is_dealer: bool = field(default=False, init=False, repr=False)
    deal_quality: DealQuality = field(default=DealQuality.POOR, init=False, repr=False)
    
    def __post_init__(self):
        """Bereken afgeleide velden na initialisatie."""
        # Validatie
        if self.price < 0:
            raise ValueError(f"Prijs kan niet negatief zijn: {self.price}")
        if self.km is not None and self.km < 0:
            raise ValueError(f"KM kan niet negatief zijn: {self.km}")
        if self.year is not None and (self.year < 1960 or self.year > datetime.now().year + 1):
            raise ValueError(f"Ongeldig bouwjaar: {self.year}")
    
    def analyze(self, filter_config: FilterConfig, bot_config: BotConfig) -> None:
        """Analyseer de listing en bereken kwaliteit."""
        combined_text = f"{self.title} {self.description}".lower()
        
        self.is_dealer = any(word in combined_text for word in filter_config.dealer_words)
        self.motivated_seller = any(word in combined_text for word in filter_config.motivation_words)
        self.buy_limit = self._calculate_buy_limit(filter_config, bot_config)
        self.deal_quality = self._calculate_deal_quality(bot_config)
    
    def _calculate_buy_limit(
        self, 
        filter_config: FilterConfig, 
        bot_config: BotConfig
    ) -> Optional[int]:
        """Bereken maximale koopprijs op basis van jaar en KM."""
        if not self.year or not self.km:
            return None
        if self.km > bot_config.max_km:
            return None
        
        # Vind base price op basis van jaar
        base = next(
            (limit["base"] for limit in filter_config.buy_limits 
             if self.year >= limit["min_year"]),
            filter_config.buy_limits[-1]["base"],
        )
        
        # Pas aan op basis van KM
        if self.km < bot_config.low_km_threshold:
            base += bot_config.low_km_bonus
        elif self.km > bot_config.high_km_threshold:
            base -= bot_config.high_km_penalty
        
        return base
    
    def _calculate_deal_quality(self, bot_config: BotConfig) -> DealQuality:
        """Bepaal de kwaliteit van de deal."""
        if self.is_dealer or not self.buy_limit:
            return DealQuality.POOR
        
        if self.km and self.price / self.km > bot_config.price_per_km_limit:
            return DealQuality.POOR
        
        if self.price > self.buy_limit:
            return DealQuality.POOR
        
        # Bereken percentage onder buy limit
        discount_percentage = ((self.buy_limit - self.price) / self.buy_limit) * 100
        
        if discount_percentage >= 20 or self.motivated_seller:
            return DealQuality.EXCELLENT
        elif discount_percentage >= 10:
            return DealQuality.GOOD
        else:
            return DealQuality.AVERAGE
    
    @property
    def is_good_deal(self) -> bool:
        """Check of dit een goede deal is."""
        return self.deal_quality in (DealQuality.EXCELLENT, DealQuality.GOOD)
    
    def format_message(self) -> str:
        """Formatteer een mooi Telegram bericht."""
        quality_emoji = {
            DealQuality.EXCELLENT: "🔥🔥🔥",
            DealQuality.GOOD: "🔥",
            DealQuality.AVERAGE: "✅",
            DealQuality.POOR: "❌",
        }
        
        emoji = quality_emoji[self.deal_quality]
        boost = " 🚀 GEMOTIVEERDE VERKOPER" if self.motivated_seller else ""
        
        discount = ""
        if self.buy_limit:
            discount_pct = ((self.buy_limit - self.price) / self.buy_limit) * 100
            discount = f" (-{discount_pct:.1f}%)"
        
        km_str = f"{self.km:,}" if self.km else "onbekend"
        
        return (
            f"{emoji} {self.deal_quality.value} DEAL{boost}\n\n"
            f"📋 {self.title}\n"
            f"📅 Bouwjaar: {self.year or 'onbekend'}\n"
            f"🚗 Kilometerstand: {km_str} km\n"
            f"💰 Prijs: €{self.price:,}{discount}\n"
            f"🎯 Max koopprijs: €{self.buy_limit:,}\n"
            f"🔗 {self.url}"
        )

# ---------------------------------------------------------
# SEEN LINKS MANAGER
# ---------------------------------------------------------

class SeenLinksManager:
    """Thread-safe manager voor geziene links met TTL."""
    
    def __init__(self, path: Path, max_age_days: int):
        self._path = path
        self._max_age = timedelta(days=max_age_days)
        self._data: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        
        self._load()
    
    def _load(self) -> None:
        """Laad geziene links uit bestand."""
        if not self._path.exists():
            logger.info(f"Seen links bestand bestaat niet, start leeg.")
            return
        
        try:
            raw_data = json.loads(self._path.read_text())
            # Converteer timestamps terug naar datetime
            self._data = {
                url: datetime.fromisoformat(ts)
                for url, ts in raw_data.items()
            }
            logger.info(f"Geladen: {len(self._data)} geziene links")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Kon seen links niet laden: {e}, start leeg.")
            self._data = {}
    
    def _save(self) -> None:
        """Sla geziene links op naar bestand."""
        if not self._dirty:
            return
        
        try:
            # Converteer datetime naar ISO string voor JSON
            raw_data = {
                url: dt.isoformat()
                for url, dt in self._data.items()
            }
            self._path.write_text(json.dumps(raw_data, indent=2))
            self._dirty = False
            logger.debug(f"Opgeslagen: {len(self._data)} geziene links")
        except Exception as e:
            logger.error(f"Kon seen links niet opslaan: {e}")
    
    def _clean_expired(self) -> int:
        """Verwijder verlopen links."""
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
        """Check of URL al gezien is."""
        async with self._lock:
            return url in self._data
    
    async def add(self, url: str) -> None:
        """Voeg URL toe aan geziene links."""
        async with self._lock:
            self._data[url] = datetime.now()
            self._dirty = True
    
    async def add_batch(self, urls: Set[str]) -> None:
        """Voeg meerdere URLs toe in één keer."""
        async with self._lock:
            now = datetime.now()
            for url in urls:
                self._data[url] = now
            self._dirty = True
    
    async def cleanup_and_save(self) -> None:
        """Cleanup oude entries en save."""
        async with self._lock:
            self._clean_expired()
            self._save()
            logger.info(f"Cleanup voltooid. Resterende links: {len(self._data)}")
    
    async def get_stats(self) -> Dict[str, int]:
        """Verkrijg statistieken."""
        async with self._lock:
            now = datetime.now()
            day_ago = now - timedelta(days=1)
            week_ago = now - timedelta(days=7)
            
            return {
                "total": len(self._data),
                "last_24h": sum(1 for dt in self._data.values() if dt > day_ago),
                "last_week": sum(1 for dt in self._data.values() if dt > week_ago),
            }

# ---------------------------------------------------------
# HTTP CLIENT
# ---------------------------------------------------------

class MarktplaatsClient:
    """HTTP client voor Marktplaats scraping."""
    
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    
    def __init__(self, config: BotConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._stats = {"requests": 0, "errors": 0, "retries": 0}
    
    async def __aenter__(self):
        """Async context manager entry."""
        connector = aiohttp.TCPConnector(
            limit=self.config.max_concurrent_requests,
            limit_per_host=3,
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
        """Async context manager exit."""
        if self._session:
            await self._session.close()
    
    async def get_html(self, url: str) -> Optional[str]:
        """
        Haal HTML op van een URL met retry logic.
        
        Args:
            url: De URL om op te halen
            
        Returns:
            HTML string of None bij falen
        """
        if not self._session:
            raise RuntimeError("Client niet geïnitialiseerd. Gebruik 'async with'.")
        
        async with self._semaphore:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    self._stats["requests"] += 1
                    
                    async with self._session.get(url) as response:
                        if response.status == 200:
                            return await response.text()
                        
                        elif response.status in (429, 503):
                            # Rate limiting
                            wait_time = 2 ** attempt
                            self._stats["retries"] += 1
                            logger.warning(
                                f"Rate limited ({response.status}) op {url}, "
                                f"wacht {wait_time}s (poging {attempt}/{self.config.max_retries})"
                            )
                            await asyncio.sleep(wait_time)
                        
                        elif response.status == 404:
                            logger.debug(f"Listing niet gevonden (404): {url}")
                            return None
                        
                        else:
                            logger.warning(f"HTTP {response.status} voor {url}")
                            return None
                
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    self._stats["errors"] += 1
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Fout bij {url}: {type(e).__name__} — "
                        f"retry in {wait_time}s (poging {attempt}/{self.config.max_retries})"
                    )
                    
                    if attempt < self.config.max_retries:
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Alle {self.config.max_retries} pogingen gefaald voor {url}")
                        return None
        
        return None
    
    def get_stats(self) -> Dict[str, int]:
        """Verkrijg client statistieken."""
        return self._stats.copy()

# ---------------------------------------------------------
# PARSING
# ---------------------------------------------------------

class ListingParser:
    """Parser voor Marktplaats listing pages."""
    
    @staticmethod
    def parse_page(html: str) -> tuple[Optional[str], Optional[int], str]:
        """
        Parse een listing page voor titel, prijs en beschrijving.
        
        Returns:
            (titel, prijs, beschrijving) tuple
        """
        # Probeer eerst __NEXT_DATA__
        result = ListingParser._extract_from_next_data(html)
        if result[0] and result[1]:
            return result
        
        # Fallback naar regex
        return ListingParser._extract_fallback(html)
    
    @staticmethod
    def _extract_from_next_data(html: str) -> tuple[Optional[str], Optional[int], str]:
        """Extract data uit __NEXT_DATA__ JSON."""
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL
        )
        
        if not match:
            return None, None, ""
        
        try:
            data = json.loads(match.group(1))
            listing = (
                data.get("props", {})
                .get("pageProps", {})
                .get("listing", {})
            )
            
            title = listing.get("title", "").strip()
            description = listing.get("description", "").strip()
            price_info = listing.get("priceInfo", {})
            
            # Probeer verschillende price velden
            raw_price = (
                price_info.get("priceCents") or
                price_info.get("price") or
                price_info.get("askingPrice")
            )
            
            if not title or raw_price is None:
                return None, None, ""
            
            # Converteer prijs (soms in centen)
            price = int(raw_price)
            if price > PRICE_CENT_THRESHOLD:
                price = price // 100
            
            return title, price, description
        
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug(f"__NEXT_DATA__ parse error: {e}")
            return None, None, ""
    
    @staticmethod
    def _extract_fallback(html: str) -> tuple[Optional[str], Optional[int], str]:
        """Fallback parsing met regex."""
        title_match = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
        price_match = re.search(r'"price":\s*"?(\d+)"?', html)
        
        if not title_match or not price_match:
            return None, None, ""
        
        title = title_match.group(1).strip()
        price = int(price_match.group(1))
        
        return title, price, ""
    
    @staticmethod
    def extract_km(text: str) -> Optional[int]:
        """
        Extract kilometerstand uit tekst.
        
        Ondersteunt:
        - 123.456 km
        - 123456 km
        - 123 456 km
        - 123,456 km
        - 123456km (zonder spatie)
        
        Tests:
        >>> ListingParser.extract_km("Gereden 123.456 km")
        123456
        >>> ListingParser.extract_km("123456km in perfecte staat")
        123456
        >>> ListingParser.extract_km("80000 km")
        80000
        """
        # Pattern met optionele spatie voor 'km'
        pattern = r'(\d{1,3}(?:[.,\s]\d{3})+|\d+)\s*km'
        match = re.search(pattern, text.lower())
        
        if match:
            raw = re.sub(r'[.,\s]', '', match.group(1))
            try:
                km = int(raw)
                # Validatie: redelijke range
                if 0 < km < 1_000_000:
                    return km
            except ValueError:
                pass
        
        return None
    
    @staticmethod
    def extract_year(text: str) -> Optional[int]:
        """
        Extract bouwjaar uit tekst.
        
        Tests:
        >>> ListingParser.extract_year("Toyota Yaris 2015 1.0")
        2015
        >>> ListingParser.extract_year("Bouwjaar: 2008")
        2008
        """
        current_year = datetime.now().year
        
        # Zoek 4-cijferige jaren tussen 1960 en volgend jaar
        pattern = r'\b(19[6-9]\d|20[0-2]\d)\b'
        matches = re.findall(pattern, text)
        
        if matches:
            # Filter op plausibele auto jaren
            valid_years = [
                int(y) for y in matches
                if 1960 <= int(y) <= current_year + 1
            ]
            
            if valid_years:
                # Return meest recente plausibele jaar
                return max(valid_years)
        
        return None
    
    @staticmethod
    def extract_listing_links(html: str, max_links: int = 50) -> List[str]:
        """
        Extract listing URLs uit zoekresultaten pagina.
        
        Args:
            html: HTML van zoekresultaten
            max_links: Maximum aantal links
            
        Returns:
            Lijst van absolute URLs
        """
        # Pattern voor listing URLs
        pattern = r'href="(/v/auto[^"#?]+)"'
        links = re.findall(pattern, html)
        
        # Deduplicate en limiteer
        unique_links = list(dict.fromkeys(links))[:max_links]
        
        # Converteer naar absolute URLs
        return [f"https://www.marktplaats.nl{link}" for link in unique_links]

# ---------------------------------------------------------
# TELEGRAM NOTIFIER
# ---------------------------------------------------------

class TelegramNotifier:
    """Handler voor Telegram notificaties."""
    
    def __init__(self, app, chat_id: str):
        self.app = app
        self.chat_id = chat_id
        self._stats = {"sent": 0, "errors": 0}
        self._lock = asyncio.Lock()
    
    async def send_message(self, message: str) -> bool:
        """
        Stuur een bericht naar Telegram.
        
        Returns:
            True als succesvol, False bij fout
        """
        async with self._lock:
            try:
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    disable_web_page_preview=True,
                )
                self._stats["sent"] += 1
                logger.info("Telegram bericht verzonden")
                return True
            
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"Telegram fout: {e}")
                return False
    
    async def send_listing(self, listing: Listing) -> bool:
        """Stuur een listing notificatie."""
        return await self.send_message(listing.format_message())
    
    async def send_startup(self) -> bool:
        """Stuur startup bericht."""
        message = (
            "🤖 Marktplaats Deal Bot gestart\n\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "✅ Monitoring actief"
        )
        return await self.send_message(message)
    
    async def send_stats(self, stats: Dict) -> bool:
        """Stuur statistieken bericht."""
        message = (
            "📊 Bot Statistieken\n\n"
            f"🔍 Scans: {stats.get('scans', 0)}\n"
            f"📋 Listings bekeken: {stats.get('listings_checked', 0)}\n"
            f"✅ Deals gevonden: {stats.get('deals_found', 0)}\n"
            f"📨 Berichten verzonden: {stats.get('messages_sent', 0)}\n"
            f"🌐 HTTP requests: {stats.get('http_requests', 0)}\n"
            f"❌ HTTP errors: {stats.get('http_errors', 0)}\n"
        )
        return await self.send_message(message)
    
    def get_stats(self) -> Dict[str, int]:
        """Verkrijg notifier statistieken."""
        return self._stats.copy()

# ---------------------------------------------------------
# SCRAPER
# ---------------------------------------------------------

class MarktplaatsScraper:
    """Main scraper orchestrator."""
    
    def __init__(
        self,
        bot_config: BotConfig,
        filter_config: FilterConfig,
        seen_manager: SeenLinksManager,
        notifier: TelegramNotifier,
    ):
        self.bot_config = bot_config
        self.filter_config = filter_config
        self.seen_manager = seen_manager
        self.notifier = notifier
        
        self._stats = {
            "scans": 0,
            "listings_checked": 0,
            "deals_found": 0,
            "errors": 0,
        }
    
    async def process_listing(
        self,
        url: str,
        client: MarktplaatsClient,
    ) -> Optional[Listing]:
        """
        Process een enkele listing.
        
        Returns:
            Listing object als het een goede deal is, anders None
        """
        # Check of al gezien
        if await self.seen_manager.contains(url):
            return None
        
        # Haal HTML op
        html = await client.get_html(url)
        if not html:
            await self.seen_manager.add(url)
            return None
        
        # Parse listing
        title, price, description = ListingParser.parse_page(html)
        if not title or not price:
            logger.debug(f"Kon listing niet parsen: {url}")
            await self.seen_manager.add(url)
            return None
        
        # Extract metadata
        km = ListingParser.extract_km(html)
        year = (
            ListingParser.extract_year(title) or
            ListingParser.extract_year(description)
        )
        
        # Create en analyseer listing
        try:
            listing = Listing(
                url=url,
                title=title,
                price=price,
                km=km,
                year=year,
                description=description,
            )
            listing.analyze(self.filter_config, self.bot_config)
        except ValueError as e:
            logger.warning(f"Ongeldige listing data: {e}")
            await self.seen_manager.add(url)
            return None
        
        self._stats["listings_checked"] += 1
        
        # Check of het een goede deal is
        if not listing.is_good_deal:
            await self.seen_manager.add(url)
            return None
        
        # Goede deal gevonden!
        self._stats["deals_found"] += 1
        await self.seen_manager.add(url)
        
        logger.info(
            f"Deal gevonden: {listing.deal_quality.value} | "
            f"€{price} | {km} km | {year} | {url}"
        )
        
        return listing
    
    async def scan_model(
        self,
        model: str,
        client: MarktplaatsClient,
    ) -> List[Listing]:
        """
        Scan een specifiek automodel.
        
        Returns:
            Lijst van gevonden deals
        """
        search_url = (
            f"https://www.marktplaats.nl/l/auto-s/q/{model}/"
            "?sortBy=SORT_INDEX&sortOrder=DECREASING"
        )
        
        # Haal zoekresultaten op
        html = await client.get_html(search_url)
        if not html:
            logger.warning(f"Geen zoekresultaten voor model: {model}")
            return []
        
        # Extract listing URLs
        links = ListingParser.extract_listing_links(
            html,
            max_links=self.bot_config.max_links_per_model
        )
        
        if not links:
            logger.warning(f"Geen listings gevonden voor model: {model}")
            return []
        
        logger.info(f"Model '{model}': {len(links)} listings gevonden")
        
        # Process alle listings concurrent
        tasks = [
            self.process_listing(link, client)
            for link in links
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter succesvolle deals en log errors
        deals = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Fout bij verwerken listing: {result}")
                self._stats["errors"] += 1
            elif result is not None:
                deals.append(result)
        
        return deals
    
    async def scan_all_models(self, client: MarktplaatsClient) -> List[Listing]:
        """
        Scan alle geconfigureerde modellen.
        
        Returns:
            Lijst van alle gevonden deals
        """
        logger.info(f"Start scan van {len(self.filter_config.models)} modellen")
        self._stats["scans"] += 1
        
        # Scan alle modellen concurrent
        tasks = [
            self.scan_model(model, client)
            for model in self.filter_config.models
        ]
        results = await asyncio.gather(*tasks)
        
        # Flatten results
        all_deals = [deal for model_deals in results for deal in model_deals]
        
        logger.info(
            f"Scan voltooid: {len(all_deals)} deals gevonden uit "
            f"{self._stats['listings_checked']} listings"
        )
        
        return all_deals
    
    def get_stats(self) -> Dict:
        """Verkrijg scraper statistieken."""
        return self._stats.copy()

# ---------------------------------------------------------
# MAIN BOT
# ---------------------------------------------------------

class MarktplaatsBot:
    """Main bot orchestrator."""
    
    def __init__(self, bot_config: BotConfig, filter_config: FilterConfig):
        self.bot_config = bot_config
        self.filter_config = filter_config
        
        # Initialize components
        self.seen_manager = SeenLinksManager(
            bot_config.seen_file,
            bot_config.seen_max_age_days
        )
        
        # Telegram app wordt later geïnitialiseerd
        self.telegram_app = None
        self.notifier = None
        self.scraper = None
        
        # Shutdown flag
        self._shutdown = asyncio.Event()
        
        # Setup signal handlers
        self._setup_signal_handlers()
    
    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers."""
        def handle_shutdown(signum, frame):
            logger.info(f"Shutdown signal ontvangen: {signal.Signals(signum).name}")
            self._shutdown.set()
        
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)
    
    async def _scan_loop(self):
        """Main scanning loop."""
        logger.info("Scan loop gestart")
        await self.notifier.send_startup()
        
        async with MarktplaatsClient(self.bot_config) as client:
            while not self._shutdown.is_set():
                try:
                    # Scan alle modellen
                    deals = await self.scraper.scan_all_models(client)
                    
                    # Stuur notificaties voor deals
                    for deal in deals:
                        await self.notifier.send_listing(deal)
                        # Kleine delay tussen berichten
                        await asyncio.sleep(1)
                    
                    # Cleanup oude seen links
                    await self.seen_manager.cleanup_and_save()
                    
                    # Log statistieken
                    if self._stats_counter % 10 == 0:  # Elke 10 scans
                        await self._log_stats()
                    
                    self._stats_counter += 1
                    
                except Exception:
                    logger.exception("Onverwachte fout tijdens scan")
                
                # Wacht tot volgende scan of shutdown
                logger.info(
                    f"Wacht {self.bot_config.check_interval}s tot volgende scan "
                    f"(of tot shutdown signal)"
                )
                
                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self.bot_config.check_interval
                    )
                    break  # Shutdown ontvangen
                except asyncio.TimeoutError:
                    pass  # Timeout = normale flow, ga door met volgende scan
        
        logger.info("Scan loop gestopt")
    
    async def _log_stats(self):
        """Log en stuur statistieken."""
        stats = {
            "scans": self.scraper.get_stats()["scans"],
            "listings_checked": self.scraper.get_stats()["listings_checked"],
            "deals_found": self.scraper.get_stats()["deals_found"],
            "messages_sent": self.notifier.get_stats()["sent"],
            "http_requests": self._client_stats["requests"],
            "http_errors": self._client_stats["errors"],
        }
        
        seen_stats = await self.seen_manager.get_stats()
        stats.update(seen_stats)
        
        logger.info(f"Statistieken: {stats}")
        
        # Stuur stats naar Telegram (1x per dag ongeveer)
        if self._stats_counter % 288 == 0:  # 288 * 5min = 24h bij 5min interval
            await self.notifier.send_stats(stats)
    
    async def _post_init(self, app):
        """Post-init callback voor Telegram application."""
        self.telegram_app = app
        self.notifier = TelegramNotifier(app, self.bot_config.telegram_chat_id)
        self.scraper = MarktplaatsScraper(
            self.bot_config,
            self.filter_config,
            self.seen_manager,
            self.notifier,
        )
        
        self._stats_counter = 0
        self._client_stats = {}
        
        # Start scan loop
        asyncio.create_task(self._scan_loop())
    
    async def _post_shutdown(self, app):
        """Post-shutdown callback."""
        logger.info("Cleanup bij shutdown...")
        
        # Save seen links één laatste keer
        await self.seen_manager.cleanup_and_save()
        
        # Log finale stats
        await self._log_stats()
        
        logger.info("Cleanup voltooid")
    
    def run(self):
        """Start de bot."""
        logger.info("🤖 Marktplaats Deal Bot wordt opgestart...")
        logger.info(f"Config: {self.bot_config}")
        logger.info(f"Modellen: {', '.join(self.filter_config.models)}")
        
        try:
            app = (
                ApplicationBuilder()
                .token(self.bot_config.telegram_token)
                .post_init(self._post_init)
                .post_shutdown(self._post_shutdown)
                .build()
            )
            
            app.run_polling(
                allowed_updates=[],  # We gebruiken alleen outgoing messages
                drop_pending_updates=True,
            )
        
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt ontvangen")
        except Exception:
            logger.exception("Fatale fout in bot")
            sys.exit(1)
        finally:
            logger.info("Bot gestopt")

# ---------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------

def main():
    """Main entry point."""
    try:
        # Laad configuratie
        bot_config = BotConfig.from_env()
        filter_config = FilterConfig.from_file(Path("filters.json"))
        
        # Start bot
        bot = MarktplaatsBot(bot_config, filter_config)
        bot.run()
    
    except ValueError as e:
        logger.error(f"Configuratie fout: {e}")
        sys.exit(1)
    except Exception:
        logger.exception("Onverwachte fout bij opstarten")
        sys.exit(1)


if __name__ == "__main__":
    # Run doctests in debug mode
    if "--test" in sys.argv:
        import doctest
        doctest.testmod(verbose=True)
    else:
        main()