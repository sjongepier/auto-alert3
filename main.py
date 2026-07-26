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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple
from datetime import datetime, timedelta
from enum import Enum
from telegram.ext import ApplicationBuilder, CommandHandler
import sys

# ---------------------------------------------------------
# CONSTANTS & ENUMS
# ---------------------------------------------------------

class DealQuality(Enum):
    GODLIKE = "GODLIKE"
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    AVERAGE = "AVERAGE"
    POOR = "POOR"

PRICE_CENT_THRESHOLD = 100_000

KENTEKEN_PATTERN = re.compile(r'\b([A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3})\b')

# ---------------------------------------------------------
# CONFIG MANAGEMENT
# ---------------------------------------------------------

@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    telegram_chat_id: str
    check_interval: int = 8
    max_concurrent_requests: int = 8
    request_timeout: int = 15
    max_retries: int = 3

    min_profit_margin: int = 500
    max_km: int = 220_000
    price_per_km_limit: float = 0.04
    seen_max_age_days: int = 30

    market_value_samples: int = 10

    seen_file: Path = field(default_factory=lambda: Path("seen_links.json"))

    @classmethod
    def from_env(cls) -> 'BotConfig':
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not token or not chat_id:
            raise ValueError("TELEGRAM_TOKEN en TELEGRAM_CHAT_ID zijn vereist")

        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            check_interval=int(os.getenv("CHECK_INTERVAL", 8)),
            min_profit_margin=int(os.getenv("MIN_PROFIT_MARGIN", 500)),
        )

@dataclass(frozen=True)
class FilterConfig:
    models: List[str]
    motivation_words: List[str]
    dealer_words: List[str]
    red_flags: List[str]
    quality_indicators: List[str]

    @classmethod
    def from_file(cls, path: Path) -> 'FilterConfig':
        if not path.exists():
            raise FileNotFoundError(f"filters.json niet gevonden")

        data = json.loads(path.read_text())
        return cls(**data)

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
    logger = logging.getLogger("profit-bot")
    logger.setLevel(level)

    console_handler = logging.StreamHandler(stream=sys.stdout)
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
# SHUTDOWN NOTIFICATIE
# ---------------------------------------------------------

_shutdown_notified = False

def notify_shutdown_sync(token: str, chat_id: str, reason: str = "Bot is gestopt"):
    global _shutdown_notified
    if _shutdown_notified:
        return
    _shutdown_notified = True

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = (
            f"🛑 {reason}\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"⚠️ De bot zoekt niet meer verder naar deals."
        )
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        logger.info("📤 Shutdown-notificatie verstuurd")
    except Exception as e:
        logger.error(f"Kon shutdown-notificatie niet versturen: {e}")


# ---------------------------------------------------------
# RDW KENTEKEN CHECK
# ---------------------------------------------------------

def extract_kenteken(text: str) -> Optional[str]:
    match = KENTEKEN_PATTERN.search(text)
    if not match:
        return None

    candidate = match.group(1).replace('-', '').replace(' ', '').upper()

    if 5 <= len(candidate) <= 6 and any(c.isdigit() for c in candidate) and any(c.isalpha() for c in candidate):
        return candidate

    return None


class RDWClient:
    BASE_URL = "https://opendata.rdw.nl/resource/m9d7-ebf2.json"

    def __init__(self):
        self._cache: Dict[str, Optional[dict]] = {}

    async def lookup(self, kenteken: str, client: 'SmartClient') -> Optional[dict]:
        kenteken = kenteken.upper().replace('-', '').replace(' ', '')

        if kenteken in self._cache:
            return self._cache[kenteken]

        url = f"{self.BASE_URL}?kenteken={kenteken}"
        raw = await client.get_html(url)

        if not raw:
            self._cache[kenteken] = None
            return None

        try:
            results = json.loads(raw)
            if not results:
                self._cache[kenteken] = None
                return None

            info = results[0]
            self._cache[kenteken] = info
            logger.info(
                f"🪪 RDW check OK: {kenteken} → "
                f"{info.get('merk', '?')} {info.get('handelsbenaming', '?')}"
            )
            return info

        except Exception as e:
            logger.debug(f"RDW parse error: {e}")
            self._cache[kenteken] = None
            return None


# ---------------------------------------------------------
# MARKET VALUE CALCULATOR
# ---------------------------------------------------------

class MarketValueCalculator:

    def __init__(self):
        self._cache: Dict[str, Tuple[Optional[float], datetime]] = {}
        self._success_ttl = timedelta(hours=6)
        self._fail_ttl = timedelta(minutes=15)

        self._consecutive_failures = 0
        self._circuit_open_until: Optional[datetime] = None
        self._failure_threshold = 5
        self._circuit_cooldown = timedelta(minutes=30)

    def _get_cache_key(self, brand: str, model: str, year: int, km: int) -> str:
        km_bracket = (km // 20000) * 20000
        return f"{brand}_{model}_{year}_{km_bracket}"

    def _circuit_is_open(self) -> bool:
        if self._circuit_open_until is None:
            return False
        if datetime.now() >= self._circuit_open_until:
            logger.info("🔌 Circuit breaker cooldown voorbij, probeer AutoScout24 weer")
            self._circuit_open_until = None
            self._consecutive_failures = 0
            return False
        return True

    def _register_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._circuit_open_until = datetime.now() + self._circuit_cooldown
            logger.warning(
                f"🔌 Circuit breaker OPEN voor AutoScout24 "
                f"({self._circuit_cooldown.total_seconds() / 60:.0f} min)"
            )

    def _register_success(self):
        self._consecutive_failures = 0

    async def get_market_value(
        self,
        brand: str,
        model: str,
        year: int,
        km: int,
        client: 'SmartClient'
    ) -> Optional[float]:

        cache_key = self._get_cache_key(brand, model, year, km)

        if cache_key in self._cache:
            value, timestamp = self._cache[cache_key]
            ttl = self._success_ttl if value is not None else self._fail_ttl
            if datetime.now() - timestamp < ttl:
                return value

        if self._circuit_is_open():
            self._cache[cache_key] = (None, datetime.now())
            return None

        search_url = (
            f"https://www.autoscout24.nl/lst?"
            f"mmvmk0={brand}&mmvmd0={model}&mmvco=1"
            f"&fregfrom={year-1}&fregto={year+1}"
            f"&kmfrom={km-30000}&kmto={km+30000}"
            f"&sort=standard&desc=0&ustate=N%2CU"
            f"&size=20&page=0&cy=NL&atype=C"
        )

        html = await client.get_html(search_url)

        if not html:
            self._register_failure()
            self._cache[cache_key] = (None, datetime.now())
            logger.warning(f"⚠️  Geen marktdata voor {brand} {model} {year}")
            return None

        prices = self._extract_prices(html)

        if len(prices) < 3:
            self._register_failure()
            self._cache[cache_key] = (None, datetime.now())
            logger.warning(f"⚠️  Te weinig data ({len(prices)} auto's) voor {brand} {model}")
            return None

        self._register_success()

        market_value = statistics.median(prices)
        self._cache[cache_key] = (market_value, datetime.now())

        logger.info(f"💰 Marktwaarde {brand} {model} {year}: €{market_value:.0f} (van {len(prices)} auto's)")

        return market_value

    def _extract_prices(self, html: str) -> List[float]:
        prices = []

        patterns = [
            r'data-price="(\d+)"',
            r'"price"\s*:\s*(\d+)',
            r'€\s*([\d.]+)',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, html)
            for match in matches:
                try:
                    price_str = match.replace('.', '')
                    price = float(price_str)

                    if 500 < price < 50000:
                        prices.append(price)
                except (ValueError, AttributeError):
                    continue

        prices = sorted(list(set(prices)))

        return prices

# ---------------------------------------------------------
# DATACLASSES
# ---------------------------------------------------------

@dataclass
class MarketAnalysis:
    asking_price: int
    market_value: Optional[float]
    profit_potential: Optional[int]
    profit_percentage: Optional[float]
    is_profitable: bool

    @classmethod
    def analyze(
        cls,
        asking_price: int,
        market_value: Optional[float],
        min_profit: int
    ) -> 'MarketAnalysis':

        if not market_value:
            return cls(
                asking_price=asking_price,
                market_value=None,
                profit_potential=None,
                profit_percentage=None,
                is_profitable=False
            )

        sell_price = market_value * 0.90
        profit = int(sell_price - asking_price)
        profit_pct = (profit / asking_price * 100) if asking_price > 0 else 0

        return cls(
            asking_price=asking_price,
            market_value=market_value,
            profit_potential=profit,
            profit_percentage=profit_pct,
            is_profitable=profit >= min_profit
        )

@dataclass
class Listing:
    url: str
    title: str
    price: int
    platform: str
    km: Optional[int] = None
    year: Optional[int] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    description: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    motivated_seller: bool = field(default=False, init=False, repr=False)
    is_dealer: bool = field(default=False, init=False, repr=False)
    has_red_flags: bool = field(default=False, init=False, repr=False)
    quality_score: int = field(default=0, init=False, repr=False)
    market_analysis: Optional[MarketAnalysis] = field(default=None, init=False, repr=False)
    deal_quality: DealQuality = field(default=DealQuality.POOR, init=False, repr=False)

    kenteken: Optional[str] = field(default=None, init=False, repr=False)
    rdw_verified: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        if self.price < 0:
            raise ValueError(f"Prijs kan niet negatief zijn: {self.price}")
        if self.km is not None and self.km < 0:
            raise ValueError(f"KM kan niet negatief zijn: {self.km}")
        if self.year is not None and (self.year < 1990 or self.year > datetime.now().year + 1):
            raise ValueError(f"Ongeldig bouwjaar: {self.year}")

        if not self.brand or not self.model:
            self._extract_brand_model()

    def _extract_brand_model(self):
        title_lower = self.title.lower()

        brands = {
            'toyota': ['aygo', 'yaris', 'corolla'],
            'peugeot': ['107', '108', '206', '207'],
            'citroen': ['c1', 'c2', 'c3'],
            'kia': ['picanto', 'rio'],
            'hyundai': ['i10', 'i20'],
            'volkswagen': ['up', 'polo'],
            'seat': ['mii', 'ibiza'],
            'skoda': ['citigo', 'fabia'],
        }

        for brand, models in brands.items():
            if brand in title_lower:
                self.brand = brand.capitalize()
                for model in models:
                    if model in title_lower:
                        self.model = model.capitalize()
                        return

        words = self.title.split()
        if words:
            self.brand = words[0]
            if len(words) > 1:
                self.model = words[1]

    async def _try_rdw_check(self, rdw_client: RDWClient, client: 'SmartClient') -> None:
        kenteken = extract_kenteken(f"{self.title} {self.description}")
        if not kenteken:
            return

        self.kenteken = kenteken
        rdw_info = await rdw_client.lookup(kenteken, client)

        if not rdw_info:
            return

        self.rdw_verified = True

        rdw_date = rdw_info.get('datum_eerste_toelating')
        if rdw_date and len(str(rdw_date)) >= 4:
            try:
                self.year = int(str(rdw_date)[:4])
            except ValueError:
                pass

        rdw_brand = rdw_info.get('merk')
        rdw_model = rdw_info.get('handelsbenaming')
        if rdw_brand:
            self.brand = rdw_brand.capitalize()
        if rdw_model:
            self.model = rdw_model.capitalize()

    async def analyze(
        self,
        filter_config: FilterConfig,
        bot_config: BotConfig,
        market_calculator: MarketValueCalculator,
        rdw_client: RDWClient,
        client: 'SmartClient'
    ) -> None:

        combined_text = f"{self.title} {self.description}".lower()

        self.is_dealer = any(word in combined_text for word in filter_config.dealer_words)
        self.motivated_seller = any(word in combined_text for word in filter_config.motivation_words)
        self.has_red_flags = any(flag in combined_text for flag in filter_config.red_flags)

        self.quality_score = sum(
            1 for indicator in filter_config.quality_indicators
            if indicator in combined_text
        )

        if self.is_dealer or self.has_red_flags:
            self.deal_quality = DealQuality.POOR
            return

        await self._try_rdw_check(rdw_client, client)

        if not self.year or not self.km or not self.brand or not self.model:
            self.deal_quality = DealQuality.POOR
            return

        if self.km > bot_config.max_km:
            self.deal_quality = DealQuality.POOR
            return

        if self.price / self.km > bot_config.price_per_km_limit:
            self.deal_quality = DealQuality.POOR
            return

        market_value = await market_calculator.get_market_value(
            self.brand,
            self.model,
            self.year,
            self.km,
            client
        )

        self.market_analysis = MarketAnalysis.analyze(
            self.price,
            market_value,
            bot_config.min_profit_margin
        )

        if not self.market_analysis.is_profitable:
            self.deal_quality = DealQuality.POOR
            return

        profit = self.market_analysis.profit_potential or 0

        if profit >= 1000:
            self.deal_quality = DealQuality.GODLIKE
        elif profit >= 500:
            self.deal_quality = DealQuality.EXCELLENT
        elif profit >= 300:
            self.deal_quality = DealQuality.GOOD
        elif profit >= 150:
            self.deal_quality = DealQuality.AVERAGE
        else:
            self.deal_quality = DealQuality.POOR

    @property
    def is_good_deal(self) -> bool:
        return self.deal_quality in (DealQuality.GODLIKE, DealQuality.EXCELLENT, DealQuality.GOOD)

    def format_message(self) -> str:
        quality_emoji = {
            DealQuality.GODLIKE: "💎💎💎",
            DealQuality.EXCELLENT: "🔥🔥",
            DealQuality.GOOD: "🔥",
            DealQuality.AVERAGE: "✅",
            DealQuality.POOR: "❌",
        }

        emoji = quality_emoji[self.deal_quality]

        urgency = ""
        if self.motivated_seller:
            urgency = "\n🚨 GEMOTIVEERDE VERKOPER!"

        rdw_badge = "\n🪪 RDW geverifieerd ✅" if self.rdw_verified else ""

        quality_stars = "⭐" * min(self.quality_score, 5)
        quality_info = f"\n{quality_stars} Kwaliteit: {self.quality_score}/10" if self.quality_score > 0 else ""

        market_info = ""
        if self.market_analysis and self.market_analysis.market_value:
            profit = self.market_analysis.profit_potential
            profit_pct = self.market_analysis.profit_percentage
            market_val = self.market_analysis.market_value

            market_info = (
                f"\n\n💰 WINST ANALYSE:\n"
                f"├─ Vraagprijs: €{self.price:,}\n"
                f"├─ Marktwaarde: €{market_val:,.0f}\n"
                f"├─ Verkoopprijs*: €{market_val * 0.9:,.0f}\n"
                f"└─ Winst: €{profit:,} ({profit_pct:.0f}%)\n"
                f"\n*Na 10% kosten (reparatie/APK/verkoop)"
            )

        time_posted = self.timestamp.strftime("%H:%M:%S")
        km_str = f"{self.km:,}" if self.km else "onbekend"

        return (
            f"{emoji} {self.deal_quality.value} DEAL!\n"
            f"{'━' * 30}\n"
            f"🚗 {self.title}\n"
            f"\n📊 SPECIFICATIES:\n"
            f"├─ Merk: {self.brand or '?'} {self.model or ''}\n"
            f"├─ Bouwjaar: {self.year}\n"
            f"├─ KM-stand: {km_str}\n"
            f"└─ Gevonden: {time_posted}"
            f"{rdw_badge}"
            f"{market_info}"
            f"{quality_info}"
            f"{urgency}\n"
            f"{'━' * 30}\n"
            f"🔗 {self.url}\n\n"
            f"⚡ ACTIE: Screenshot + Direct Bellen!\n"
            f"💡 TIP: Bied €{self.price - 200:,} (onderhandelen)"
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
        try:
            raw_data = {url: dt.isoformat() for url, dt in self._data.items()}
            self._path.write_text(json.dumps(raw_data, indent=2))
        except Exception as e:
            logger.error(f"Save error: {e}")

    def _clean_expired(self):
        cutoff = datetime.now() - self._max_age
        original = len(self._data)
        self._data = {url: dt for url, dt in self._data.items() if dt > cutoff}
        removed = original - len(self._data)
        if removed > 0:
            logger.info(f"Cleanup: {removed} oude links verwijderd")

    async def contains(self, url: str) -> bool:
        async with self._lock:
            return url in self._data

    async def add(self, url: str):
        async with self._lock:
            self._data[url] = datetime.now()

    async def cleanup_and_save(self):
        async with self._lock:
            self._clean_expired()
            self._save()

# ---------------------------------------------------------
# HTTP CLIENT
# ---------------------------------------------------------

class SmartClient:
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    ]

    def __init__(self, config: BotConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._stats = {"requests": 0, "errors": 0, "blocked": 0}
        self._ua_index = 0
        self._domain_delays: Dict[str, datetime] = {}
        self._domain_locks: Dict[str, asyncio.Lock] = {}

    def _rotate_ua(self) -> str:
        ua = self.USER_AGENTS[self._ua_index]
        self._ua_index = (self._ua_index + 1) % len(self.USER_AGENTS)
        return ua

    def _get_domain(self, url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc

    def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()
        return self._domain_locks[domain]

    async def _rate_limit(self, url: str):
        domain = self._get_domain(url)
        lock = self._get_domain_lock(domain)

        async with lock:
            if domain in self._domain_delays:
                elapsed = (datetime.now() - self._domain_delays[domain]).total_seconds()
                if elapsed < 1.5:
                    await asyncio.sleep(1.5 - elapsed)
            self._domain_delays[domain] = datetime.now()

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(
            limit=self.config.max_concurrent_requests,
            limit_per_host=2,
            ttl_dns_cache=300,
        )

        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
        self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def get_html(self, url: str) -> Optional[str]:
        if not self._session:
            raise RuntimeError("Client niet geïnitialiseerd")

        await self._rate_limit(url)

        async with self._semaphore:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    self._stats["requests"] += 1

                    headers = {
                        "User-Agent": self._rotate_ua(),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
                        "Accept-Encoding": "gzip, deflate, br",
                        "DNT": "1",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1",
                    }

                    async with self._session.get(url, headers=headers) as response:
                        if response.status == 200:
                            return await response.text()
                        elif response.status == 403:
                            self._stats["blocked"] += 1
                            wait = min(2 ** attempt * 3, 30)
                            logger.warning(f"🚫 Blocked, wacht {wait}s")
                            await asyncio.sleep(wait)
                        elif response.status in (429, 503):
                            wait = 2 ** attempt
                            await asyncio.sleep(wait)
                        elif response.status == 404:
                            return None
                        else:
                            return None

                except Exception as e:
                    self._stats["errors"] += 1
                    if attempt < self.config.max_retries:
                        await asyncio.sleep(2 ** attempt)

        return None

    def get_stats(self) -> Dict[str, int]:
        return self._stats.copy()

# ---------------------------------------------------------
# PARSING
# ---------------------------------------------------------

class ListingParser:

    @staticmethod
    def parse_marktplaats_json(html: str) -> Tuple[Optional[str], Optional[int], str]:
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
    def extract_km(text: str) -> Optional[int]:
        patterns = [
            r'(\d{1,3}(?:[.,\s]\d{3})+)\s*km',
            r'(\d{4,6})\s*km',
            r'km[:\s]+(\d{1,3}(?:[.,\s]\d{3})+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, text.lower())
            if match:
                raw = re.sub(r'[.,\s]', '', match.group(1))
                try:
                    km = int(raw)
                    if 1000 < km < 500_000:
                        return km
                except ValueError:
                    continue

        return None

    @staticmethod
    def extract_year(text: str) -> Optional[int]:
        current_year = datetime.now().year

        specific_patterns = [
            r'bouwjaar[:\s]+(\d{4})',
            r'jaar[:\s]+(\d{4})',
            r'(\d{4})\s+model',
            r'bj[:\s]+(\d{4})',
        ]

        for pattern in specific_patterns:
            match = re.search(pattern, text.lower())
            if match:
                try:
                    year = int(match.group(1))
                    if 1990 <= year <= current_year:
                        return year
                except ValueError:
                    continue

        all_years = re.findall(r'\b(19[9]\d|20[0-2]\d)\b', text)

        if all_years:
            valid = [int(y) for y in all_years if 1990 <= int(y) <= current_year]
            if valid:
                from collections import Counter
                counts = Counter(valid)
                return counts.most_common(1)[0][0]

        return None

    @staticmethod
    def extract_listing_links(html: str) -> List[str]:
        pattern = r'href="(/v/auto-s/[^"#?]+)"'
        links = re.findall(pattern, html)
        unique_links = list(dict.fromkeys(links))
        return [f"https://www.marktplaats.nl{link}" for link in unique_links]

# ---------------------------------------------------------
# RSS MONITOR
# ---------------------------------------------------------

class MarktplaatsRSSMonitor:

    def __init__(self, filter_config: FilterConfig):
        self.filter_config = filter_config
        self._seen_items: Set[str] = set()

    async def check_rss(self, model: str, client: SmartClient) -> List[str]:
        rss_url = (
            f"https://www.marktplaats.nl/lrp/api/search?"
            f"query={model}&searchInTitleAndDescription=true&limit=25"
        )

        data = await client.get_html(rss_url)
        if not data:
            return []

        new_listings = []

        try:
            json_data = json.loads(data)
            listings = json_data.get('listings', [])

            for listing in listings:
                listing_id = listing.get('itemId')
                vip_url = listing.get('vipUrl')

                if listing_id and listing_id not in self._seen_items:
                    self._seen_items.add(listing_id)

                    if vip_url:
                        new_listings.append(f"https://www.marktplaats.nl{vip_url}")

            if len(self._seen_items) > 2000:
                self._seen_items = set(list(self._seen_items)[-1000:])

        except Exception as e:
            logger.debug(f"RSS parse error: {e}")

        return new_listings

# ---------------------------------------------------------
# SCRAPER
# ---------------------------------------------------------

class ProfitScraper:

    def __init__(
        self,
        bot_config: BotConfig,
        filter_config: FilterConfig,
        seen_manager: SeenLinksManager,
    ):
        self.bot_config = bot_config
        self.filter_config = filter_config
        self.seen_manager = seen_manager
        self.market_calculator = MarketValueCalculator()
        self.rdw_client = RDWClient()
        self.rss_monitor = MarktplaatsRSSMonitor(filter_config)

        self._stats = {
            'scans': 0,
            'listings_checked': 0,
            'deals_found': 0,
        }

    async def process_listing(
        self,
        url: str,
        client: SmartClient,
    ) -> Optional[Listing]:

        if await self.seen_manager.contains(url):
            return None

        html = await client.get_html(url)
        if not html:
            await self.seen_manager.add(url)
            return None

        title, price, description = ListingParser.parse_marktplaats_json(html)
        if not title or not price:
            await self.seen_manager.add(url)
            return None

        km = ListingParser.extract_km(f"{title} {description} {html}")
        year = ListingParser.extract_year(f"{title} {description}")

        try:
            listing = Listing(
                url=url,
                title=title,
                price=price,
                platform="marktplaats",
                km=km,
                year=year,
                description=description,
            )

            await listing.analyze(
                self.filter_config,
                self.bot_config,
                self.market_calculator,
                self.rdw_client,
                client
            )

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

        profit = listing.market_analysis.profit_potential if listing.market_analysis else 0

        logger.info(
            f"💰 DEAL: {listing.deal_quality.value} | "
            f"€{profit} winst | {listing.brand} {listing.model} {listing.year}"
        )

        return listing

    async def scan_model(self, model: str, client: SmartClient) -> List[Listing]:

        new_links = await self.rss_monitor.check_rss(model, client)

        if new_links:
            logger.info(f"📡 {model}: {len(new_links)} nieuwe listings")

        if not new_links:
            return []

        tasks = [self.process_listing(link, client) for link in new_links]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        deals = [r for r in results if isinstance(r, Listing) and r is not None]
        return deals

    async def scan_all(self, client: SmartClient) -> List[Listing]:

        self._stats['scans'] += 1

        tasks = [
            self.scan_model(model, client)
            for model in self.filter_config.models
        ]

        results = await asyncio.gather(*tasks)
        all_deals = [deal for model_deals in results for deal in model_deals]

        logger.info(
            f"✅ Scan compleet: {len(all_deals)} profit deals uit "
            f"{self._stats['listings_checked']} listings"
        )

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
        self._stats = {"sent": 0}

    async def send_message(self, message: str) -> bool:
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                disable_web_page_preview=True,
            )
            self._stats["sent"] += 1
            return True
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            return False

    async def send_listing(self, listing: Listing) -> bool:
        return await self.send_message(listing.format_message())

    async def send_startup(self) -> bool:
        message = (
            "💰 PROFIT BOT ACTIEF\n\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🎯 Min. winst: €500\n"
            "✅ Monitoring gestart"
        )
        return await self.send_message(message)

# ---------------------------------------------------------
# BOT COMMANDS (nu correct gedefinieerd VOOR ProfitBot)
# ---------------------------------------------------------

class BotCommands:
    """Handler voor bot commands."""

    def __init__(self, notifier: TelegramNotifier, scraper: ProfitScraper):
        self.notifier = notifier
        self.scraper = scraper

    async def start(self, update, context):
        welcome = (
            "🚀 *Welkom bij Auto Profit Bot!*\n\n"
            "Ik scan 24/7 Marktplaats voor winstgevende auto deals.\n\n"
            "✅ Alleen deals met €500+ winst\n"
            "✅ Realtime marktwaarde analyse\n"
            "✅ Automatische dealer filtering\n\n"
            "Je ontvangt nu automatisch alerts bij goede deals!\n\n"
            "Gebruik /help voor meer info."
        )
        await update.message.reply_text(welcome, parse_mode='Markdown')

    async def help(self, update, context):
        help_text = (
            "❓ *Hoe werkt de bot?*\n\n"
            "1️⃣ Ik scan elke 8 seconden Marktplaats\n"
            "2️⃣ Bij nieuwe auto's check ik de marktwaarde\n"
            "3️⃣ Als winst >€500: je krijgt een alert!\n\n"
            "📊 *Deal Categorieën:*\n"
            "💎 GODLIKE: €1000+ winst\n"
            "🔥 EXCELLENT: €500-1000 winst\n"
            "✅ GOOD: €300-500 winst\n\n"
            "💡 *Tips:*\n"
            "• Reageer binnen 5 minuten\n"
            "• Screenshot + direct bellen\n"
            "• Onderhandel altijd\n\n"
            "Commands:\n"
            "/stats - Bekijk statistieken\n"
            "/help - Deze uitleg"
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def stats(self, update, context):
        stats = self.scraper.get_stats()

        stats_text = (
            "📊 *Bot Statistieken*\n\n"
            f"🔍 Scans: {stats['scans']}\n"
            f"📋 Listings bekeken: {stats['listings_checked']}\n"
            f"💰 Deals gevonden: {stats['deals_found']}\n\n"
            f"⚡ Scan interval: 8 seconden\n"
            f"🎯 Min. winst: €500\n"
        )
        await update.message.reply_text(stats_text, parse_mode='Markdown')

# ---------------------------------------------------------
# MAIN BOT
# ---------------------------------------------------------

class ProfitBot:

    def __init__(self, bot_config: BotConfig, filter_config: FilterConfig):
        self.bot_config = bot_config
        self.filter_config = filter_config

        self.seen_manager = SeenLinksManager(
            bot_config.seen_file,
            bot_config.seen_max_age_days
        )

        self.scraper = None
        self.notifier = None
        self._shutdown = asyncio.Event()

        self._setup_signals()

    def _setup_signals(self):
        def handle(signum, frame):
            logger.info(f"Shutdown: {signal.Signals(signum).name}")
            self._shutdown.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    async def _scan_loop(self):
        logger.info("🚀 Profit scan gestart")
        await self.notifier.send_startup()

        async with SmartClient(self.bot_config) as client:
            while not self._shutdown.is_set():
                try:
                    deals = await self.scraper.scan_all(client)

                    for deal in deals:
                        await self.notifier.send_listing(deal)
                        await asyncio.sleep(2)

                    await self.seen_manager.cleanup_and_save()

                except Exception:
                    logger.exception("Scan error")

                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self.bot_config.check_interval
                    )
                    break
                except asyncio.TimeoutError:
                    pass

        logger.info("Bot gestopt")

    async def _post_init(self, app):
        self.notifier = TelegramNotifier(app, self.bot_config.telegram_chat_id)
        self.scraper = ProfitScraper(
            self.bot_config,
            self.filter_config,
            self.seen_manager,
        )

        # Command handlers correct geregistreerd VOORDAT polling start
        commands = BotCommands(self.notifier, self.scraper)
        app.add_handler(CommandHandler("start", commands.start))
        app.add_handler(CommandHandler("help", commands.help))
        app.add_handler(CommandHandler("stats", commands.stats))

        asyncio.create_task(self._scan_loop())

    async def _post_shutdown(self, app):
        await self.seen_manager.cleanup_and_save()

    def run(self):
        logger.info("💰 PROFIT BOT START")
        logger.info(f"🎯 Min winst: €{self.bot_config.min_profit_margin}")

        try:
            app = (
                ApplicationBuilder()
                .token(self.bot_config.telegram_token)
                .post_init(self._post_init)
                .post_shutdown(self._post_shutdown)
                .build()
            )

            app.run_polling(allowed_updates=None, drop_pending_updates=True)

        except Exception:
            logger.exception("Fatal error")
            sys.exit(1)

# ---------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------

def main():
    try:
        bot_config = BotConfig.from_env()
        filter_config = FilterConfig.from_file(Path("filters.json"))

        atexit.register(
            notify_shutdown_sync,
            bot_config.telegram_token,
            bot_config.telegram_chat_id,
        )

        bot = ProfitBot(bot_config, filter_config)
        bot.run()

    except Exception:
        logger.exception("Startup error")
        sys.exit(1)

if __name__ == "__main__":
    main()