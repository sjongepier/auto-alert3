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
from zoneinfo import ZoneInfo
from telegram.ext import ApplicationBuilder, CommandHandler
import sys
from collections import Counter

class DealQuality(Enum):
    GODLIKE = "GODLIKE"
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    AVERAGE = "AVERAGE"
    WATCHLIST = "WATCHLIST"
    POOR = "POOR"

PRICE_CENT_THRESHOLD = 100_000
MIN_COMPARISON_SAMPLES = 1
KENTEKEN_PATTERN = re.compile(r'\b([A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3})\b')
SETTINGS_FILE = Path("runtime_settings.json")

class ColoredFormatter(logging.Formatter):
    COLORS = {'DEBUG': '\033[36m', 'INFO': '\033[32m', 'WARNING': '\033[33m', 'ERROR': '\033[31m', 'CRITICAL': '\033[35m'}
    RESET = '\033[0m'

    def __init__(self, fmt, timezone='Europe/Amsterdam'):
        super().__init__(fmt)
        try:
            self.timezone = ZoneInfo(timezone)
        except:
            self.timezone = None
    
    def formatTime(self, record, datefmt=None):
        if self.timezone:
            dt = datetime.fromtimestamp(record.created, tz=self.timezone)
        else:
            dt = datetime.fromtimestamp(record.created)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        return super().format(record)

def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("profit-bot")
    logger.setLevel(level)
    logger.handlers = []
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(ColoredFormatter("%(asctime)s [%(levelname)s] %(message)s", timezone='Europe/Amsterdam'))
    logger.addHandler(console_handler)
    file_handler = logging.FileHandler("bot.log")
    file_handler.setFormatter(ColoredFormatter("%(asctime)s [%(levelname)s] %(message)s", timezone='Europe/Amsterdam'))
    logger.addHandler(file_handler)
    return logger

logger = setup_logging()

@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    telegram_chat_id: str
    check_interval: int = 60
    max_concurrent_requests: int = 3
    request_timeout: int = 20
    max_retries: int = 5
    min_profit_margin: int = 100
    max_km: int = 300_000
    price_per_km_limit: float = 0.25
    seen_max_age_days: int = 3
    market_value_samples: int = 200
    market_pool_ttl_hours: int = 1
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
            check_interval=int(os.getenv("CHECK_INTERVAL", 60)),
            min_profit_margin=int(os.getenv("MIN_PROFIT_MARGIN", 100)),
            max_km=int(os.getenv("MAX_KM", 300_000)),
            price_per_km_limit=float(os.getenv("PRICE_PER_KM_LIMIT", 0.25)),
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
            data = json.loads(SETTINGS_FILE.read_text())
            return RuntimeSettings(
                min_profit_margin=data.get("min_profit_margin", bot_config.min_profit_margin),
                max_km=data.get("max_km", bot_config.max_km),
                price_per_km_limit=data.get("price_per_km_limit", bot_config.price_per_km_limit),
                check_interval=data.get("check_interval", bot_config.check_interval),
                paused=data.get("paused", False),
            )
        except Exception as e:
            logger.warning(f"⚠️ Kon runtime_settings.json niet laden: {e}")
    return RuntimeSettings(
        min_profit_margin=bot_config.min_profit_margin,
        max_km=bot_config.max_km,
        price_per_km_limit=bot_config.price_per_km_limit,
        check_interval=bot_config.check_interval,
    )

def save_runtime_settings(settings: RuntimeSettings):
    try:
        data = {
            "min_profit_margin": settings.min_profit_margin,
            "max_km": settings.max_km,
            "price_per_km_limit": settings.price_per_km_limit,
            "check_interval": settings.check_interval,
            "paused": settings.paused,
        }
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.error(f"❌ Kon runtime_settings.json niet opslaan: {e}")

@dataclass(frozen=True)
class FilterConfig:
    models: List[str]
    motivation_words: List[str]
    dealer_words: List[str]
    red_flags: List[str]
    quality_indicators: List[str]
    model_aliases: Dict[str, List[str]] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> 'FilterConfig':
        if not path.exists():
            raise FileNotFoundError(f"❌ filters.json niet gevonden")
        data = json.loads(path.read_text())
        return cls(
            models=data.get("models", []),
            motivation_words=data.get("motivation_words", []),
            dealer_words=data.get("dealer_words", []),
            red_flags=data.get("red_flags", []),
            quality_indicators=data.get("quality_indicators", []),
            model_aliases=data.get("model_aliases", {}),
        )

_shutdown_notified = False

def notify_shutdown_sync(token: str, chat_id: str, reason: str = "Bot is gestopt"):
    global _shutdown_notified
    if _shutdown_notified:
        return
    _shutdown_notified = True
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = f"🛑 {reason}\n📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')}"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        logger.info("📤 Shutdown-notificatie verstuurd")
    except Exception as e:
        logger.error(f"❌ Shutdown notification failed: {e}")

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
            return info
        except Exception as e:
            logger.debug(f"RDW error: {e}")
            self._cache[kenteken] = None
            return None

class PriceHistoryTracker:
    def __init__(self):
        self._history: Dict[str, List[Tuple[datetime, int]]] = {}
    
    def track(self, url: str, price: int):
        if url not in self._history:
            self._history[url] = []
        self._history[url].append((datetime.now(), price))
        if len(self._history[url]) > 10:
            self._history[url] = self._history[url][-10:]
    
    def price_dropped(self, url: str, current_price: int) -> Optional[int]:
        if url not in self._history or len(self._history[url]) < 2:
            return None
        previous_price = self._history[url][-2][1]
        drop = previous_price - current_price
        return drop if drop > 0 else None
    
    def cleanup_old(self, days: int = 7):
        cutoff = datetime.now() - timedelta(days=days)
        for url in list(self._history.keys()):
            self._history[url] = [(ts, price) for ts, price in self._history[url] if ts > cutoff]
            if not self._history[url]:
                del self._history[url]

class MarketValueCalculator:
    def __init__(self, samples: int = 200, pool_ttl_hours: int = 1):
        self._pool_cache: Dict[str, Tuple[List[dict], datetime]] = {}
        self._pool_ttl = timedelta(hours=pool_ttl_hours)
        self._samples = samples

    @staticmethod
    def _extract_all_text(item: dict) -> str:
        parts = [str(item.get('title', '')), str(item.get('description', ''))]
        for attr_key in ('attributes', 'specificAttributes', 'specifics'):
            attrs = item.get(attr_key)
            if isinstance(attrs, list):
                for attr in attrs:
                    if isinstance(attr, dict):
                        parts.append(str(attr.get('value', '')))
                        parts.append(str(attr.get('key', '')))
                    else:
                        parts.append(str(attr))
        return " ".join(parts)

    async def _get_pool(self, search_term: str, client: 'SmartClient') -> List[dict]:
        if search_term in self._pool_cache:
            pool, timestamp = self._pool_cache[search_term]
            if datetime.now() - timestamp < self._pool_ttl:
                return pool

        search_url = f"https://www.marktplaats.nl/lrp/api/search?query={search_term}&searchInTitleAndDescription=true&limit={self._samples}"
        raw = await client.get_html(search_url)

        if not raw:
            if search_term in self._pool_cache:
                return self._pool_cache[search_term][0]
            return []

        try:
            data = json.loads(raw)
            listings = data.get('listings', [])
        except Exception as e:
            logger.error(f"❌ Pool parse error: {e}")
            return []

        pool = []
        for item in listings:
            price_info = item.get('priceInfo', {})
            raw_price = price_info.get('priceCents') or price_info.get('price')

            if raw_price is None:
                continue

            try:
                price = int(raw_price)
            except (ValueError, TypeError):
                continue

            if price > PRICE_CENT_THRESHOLD:
                price = price // 100

            if not (300 < price < 50000):
                continue

            combined_text = self._extract_all_text(item)
            item_year = ListingParser.extract_year(combined_text)
            item_km = ListingParser.extract_km(combined_text)
            vip_url = item.get('vipUrl', '')
            full_url = f"https://www.marktplaats.nl{vip_url}" if vip_url else ""

            pool.append({'price': price, 'year': item_year, 'km': item_km, 'url': full_url})

        self._pool_cache[search_term] = (pool, datetime.now())
        return pool

    async def get_market_value(
        self,
        search_term: str,
        year: int,
        km: int,
        exclude_url: str,
        client: 'SmartClient',
    ) -> Tuple[Optional[float], bool]:
        
        raw_pool = await self._get_pool(search_term, client)
        pool = [p for p in raw_pool if p['url'] != exclude_url]

        if not pool:
            return None, False

        tolerances = [(2, 50_000), (3, 80_000), (5, 120_000), (10, 200_000)]
        
        for year_tol, km_tol in tolerances:
            matches = [
                p['price'] for p in pool
                if p['year'] is not None and p['km'] is not None
                and abs(p['year'] - year) <= year_tol
                and abs(p['km'] - km) <= km_tol
            ]
            
            if len(matches) >= MIN_COMPARISON_SAMPLES:
                value = statistics.median(matches)
                return value, False

        priced = [p for p in pool if p['price'] is not None]
        if len(priced) < 3:
            return None, False

        base_median = statistics.median([p['price'] for p in priced])
        years_known = [p['year'] for p in priced if p['year'] is not None]

        if years_known:
            avg_year = statistics.mean(years_known)
            year_diff = year - avg_year
            adjustment = 1 + (year_diff * 0.08)
            adjustment = max(0.3, min(1.5, adjustment))
            estimate = base_median * adjustment
        else:
            estimate = base_median

        return estimate, True

@dataclass
class MarketAnalysis:
    asking_price: int
    market_value: Optional[float]
    profit_potential: Optional[int]
    profit_percentage: Optional[float]
    is_profitable: bool
    is_estimated: bool = False

    @classmethod
    def analyze(
        cls,
        asking_price: int,
        market_value: Optional[float],
        min_profit: int,
        is_estimated: bool = False,
    ) -> 'MarketAnalysis':

        if not market_value or market_value <= 0:
            return cls(
                asking_price=asking_price,
                market_value=None,
                profit_potential=None,
                profit_percentage=None,
                is_profitable=False,
                is_estimated=is_estimated,
            )

        if asking_price < 1000:
            cost_factor = 0.97
        elif asking_price < 2000:
            cost_factor = 0.94
        elif asking_price < 5000:
            cost_factor = 0.90
        else:
            cost_factor = 0.85

        sell_price = market_value * cost_factor
        profit = int(sell_price - asking_price)
        profit_pct = (profit / asking_price * 100) if asking_price > 0 else 0

        return cls(
            asking_price=asking_price,
            market_value=market_value,
            profit_potential=profit,
            profit_percentage=profit_pct,
            is_profitable=profit >= min_profit,
            is_estimated=is_estimated,
        )

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

    motivated_seller: bool = field(default=False, init=False, repr=False)
    is_dealer: bool = field(default=False, init=False, repr=False)
    has_red_flags: bool = field(default=False, init=False, repr=False)
    quality_score: int = field(default=0, init=False, repr=False)
    market_analysis: Optional[MarketAnalysis] = field(default=None, init=False, repr=False)
    deal_quality: DealQuality = field(default=DealQuality.POOR, init=False, repr=False)

    kenteken: Optional[str] = field(default=None, init=False, repr=False)
    rdw_verified: bool = field(default=False, init=False, repr=False)
    is_urgent: bool = field(default=False, init=False, repr=False)
    price_drop: Optional[int] = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if self.price < 0:
            raise ValueError(f"Prijs kan niet negatief zijn")
        if self.km is not None and self.km < 0:
            raise ValueError(f"KM kan niet negatief zijn")
        if self.year is not None and (self.year < 1990 or self.year > datetime.now().year + 1):
            raise ValueError(f"Ongeldig bouwjaar")

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

    def detect_urgency(self) -> bool:
        urgency_signals = [
            r'snel\s+weg', r'deze\s+week', r'vandaag\s+nog', r'moet\s+weg',
            r'emigratie', r'inruil', r'ruimte\s+nodig', r'heden',
            r'spoedverkoop', r'direct', r'vanavond',
        ]
        text = f"{self.title} {self.description}".lower()
        return any(re.search(signal, text) for signal in urgency_signals)

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
        settings: RuntimeSettings,
        market_calculator: MarketValueCalculator,
        rdw_client: RDWClient,
        client: 'SmartClient'
    ) -> None:

        combined_text = f"{self.title} {self.description}".lower()

        dealer_matches = [word for word in filter_config.dealer_words if word in combined_text]
        self.is_dealer = len(dealer_matches) >= 5
        
        self.motivated_seller = any(word in combined_text for word in filter_config.motivation_words)
        
        red_flag_matches = [flag for flag in filter_config.red_flags if flag in combined_text]
        self.has_red_flags = len(red_flag_matches) > 2
        
        self.is_urgent = self.detect_urgency()

        self.quality_score = sum(
            1 for indicator in filter_config.quality_indicators
            if indicator in combined_text
        )

        if self.is_dealer or self.has_red_flags:
            logger.info(f"   ❌ Blocked: dealer or red flags")
            self.deal_quality = DealQuality.POOR
            return

        await self._try_rdw_check(rdw_client, client)

        # ✅ STAP 1: PRIJS CHECK - EERST PRIORITEIT
        if self.price < 1200:
            logger.info(f"   ✅ VERY CHEAP (€{self.price}) → WATCHLIST")
            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.5,
                profit_potential=int(self.price * 0.4),
                profit_percentage=40,
                is_profitable=True,
                is_estimated=True,
            )
            return

        # ✅ STAP 2: ALS WE JAAR + KM HEBBEN → FULL ANALYSE
        if self.year and self.km and self.brand and self.model:
            logger.info(f"   📊 Full data: {self.brand} {self.model} {self.year} {self.km}km")
            
            if self.km > settings.max_km:
                logger.info(f"   ❌ KM too high: {self.km} > {settings.max_km}")
                self.deal_quality = DealQuality.POOR
                return

            price_per_km = self.price / self.km
            if price_per_km > settings.price_per_km_limit:
                logger.info(f"   ❌ €/km too high: €{price_per_km:.2f}")
                self.deal_quality = DealQuality.POOR
                return

            search_term = self.search_term or (self.model.lower() if self.model else "aygo")

            market_value, is_estimated = await market_calculator.get_market_value(
                search_term, self.year, self.km, self.url, client
            )

            if not market_value:
                logger.info(f"   ⚠️ No market value - fallback to cheap logic")
                if self.price < 2500:
                    self.deal_quality = DealQuality.WATCHLIST
                    self.market_analysis = MarketAnalysis(
                        asking_price=self.price,
                        market_value=self.price * 1.3,
                        profit_potential=int(self.price * 0.25),
                        profit_percentage=25,
                        is_profitable=True,
                        is_estimated=True,
                    )
                else:
                    self.deal_quality = DealQuality.POOR
                return

            adjusted_min_profit = settings.min_profit_margin
            if self.is_urgent:
                adjusted_min_profit = int(settings.min_profit_margin * 0.3)

            self.market_analysis = MarketAnalysis.analyze(
                self.price, market_value, adjusted_min_profit, is_estimated,
            )

            if not self.market_analysis.is_profitable:
                profit = self.market_analysis.profit_potential or 0
                if profit > 0:
                    logger.info(f"   👀 WATCHLIST: €{profit} (below target but positive)")
                    self.deal_quality = DealQuality.WATCHLIST
                    return
                logger.info(f"   ❌ No profit: €{profit}")
                self.deal_quality = DealQuality.POOR
                return

            profit = self.market_analysis.profit_potential or 0
            logger.info(f"   ✅ PROFIT: €{profit}")

            if profit >= 1500:
                self.deal_quality = DealQuality.GODLIKE
            elif profit >= 800:
                self.deal_quality = DealQuality.EXCELLENT
            elif profit >= 400:
                self.deal_quality = DealQuality.GOOD
            elif profit >= 150:
                self.deal_quality = DealQuality.AVERAGE
            else:
                self.deal_quality = DealQuality.WATCHLIST
            
            return
        
        # ✅ STAP 3: PARTIAL DATA - VEEL MEER TOLERANT
        logger.info(f"   ⚠️ Partial data (jaar={self.year}, km={self.km}, brand={self.brand}, model={self.model})")
        
        # Lage prijs = ALTIJD interessant
        if self.price < 1500:
            logger.info(f"   ✅ Low price (€{self.price}) → WATCHLIST")
            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.4,
                profit_potential=int(self.price * 0.3),
                profit_percentage=30,
                is_profitable=True,
                is_estimated=True,
            )
            return
        
        # EVEN MET KM → CHECK €/KM
        if self.km and self.price / self.km < 0.15:
            logger.info(f"   ✅ Cheap per km (€{self.price/self.km:.2f}) → WATCHLIST")
            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.3,
                profit_potential=int(self.price * 0.2),
                profit_percentage=20,
                is_profitable=True,
                is_estimated=True,
            )
            return
        
        # GEMOTIVEERD + LAAG PRIJS
        if self.motivated_seller and self.price < 3500:
            logger.info(f"   ✅ Motivated seller + low price (€{self.price}) → WATCHLIST")
            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.35,
                profit_potential=int(self.price * 0.25),
                profit_percentage=25,
                is_profitable=True,
                is_estimated=True,
            )
            return
        
        # ALLEEN JAAR
        if self.year and self.price < 2000:
            logger.info(f"   ✅ Year + low price (€{self.price}) → WATCHLIST")
            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.3,
                profit_potential=int(self.price * 0.25),
                profit_percentage=25,
                is_profitable=True,
                is_estimated=True,
            )
            return
        
        # ANDERS: GEEN DEAL
        logger.info(f"   ❌ No conditions met")
        self.deal_quality = DealQuality.POOR

    @property
    def is_good_deal(self) -> bool:
        return self.deal_quality in (
            DealQuality.GODLIKE, 
            DealQuality.EXCELLENT, 
            DealQuality.GOOD,
            DealQuality.AVERAGE,
            DealQuality.WATCHLIST
        )

    def format_message(self) -> str:
        quality_emoji = {
            DealQuality.GODLIKE: "💎💎💎",
            DealQuality.EXCELLENT: "🔥🔥",
            DealQuality.GOOD: "🔥",
            DealQuality.AVERAGE: "✅",
            DealQuality.WATCHLIST: "👀",
            DealQuality.POOR: "❌",
        }

        emoji = quality_emoji[self.deal_quality]

        urgency = ""
        if self.is_urgent:
            urgency = "\n🚨 URGENTE VERKOOP!"
        elif self.motivated_seller:
            urgency = "\n🚨 GEMOTIVEERDE VERKOPER!"
        
        if self.price_drop:
            urgency += f"\n💸 PRIJS GEDAALD: €{self.price_drop}"

        rdw_badge = "\n🪪 RDW geverifieerd ✅" if self.rdw_verified else ""

        quality_stars = "⭐" * min(self.quality_score, 5)
        quality_info = f"\n{quality_stars}" if self.quality_score > 0 else ""

        market_info = ""
        if self.market_analysis and self.market_analysis.market_value:
            profit = self.market_analysis.profit_potential
            profit_pct = self.market_analysis.profit_percentage
            market_val = self.market_analysis.market_value

            estimate_note = ""
            if self.market_analysis.is_estimated:
                estimate_note = "\n⚠️ (Schatting)"

            if self.price < 2000:
                cost_pct = 3
            elif self.price < 5000:
                cost_pct = 6
            else:
                cost_pct = 10

            market_info = (
                f"\n\n💰 WINST:\n"
                f"Ask: €{self.price:,}\n"
                f"Markt: €{market_val:,.0f}\n"
                f"Winst: €{profit:,} ({profit_pct:.0f}%){estimate_note}"
            )

        time_posted = self.timestamp.strftime("%H:%M")
        km_str = f"{self.km:,}" if self.km else "?"
        year_str = str(self.year) if self.year else "?"

        return (
            f"{emoji} {self.deal_quality.value}\n"
            f"{'━' * 40}\n"
            f"{self.title}\n"
            f"\n{self.brand or '?'} {self.model or '?'} | {year_str} | {km_str}km"
            f"{rdw_badge}"
            f"{market_info}"
            f"{quality_info}"
            f"{urgency}\n"
            f"{'━' * 40}\n"
            f"🔗 {self.url}"
        )

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
            self._data = {url: datetime.fromisoformat(ts) for url, ts in raw_data.items()}
            logger.info(f"✅ Geladen: {len(self._data)} links")
        except Exception as e:
            logger.warning(f"⚠️ Kon seen links niet laden: {e}")
            self._data = {}

    def _save(self):
        try:
            raw_data = {url: dt.isoformat() for url, dt in self._data.items()}
            self._path.write_text(json.dumps(raw_data, indent=2))
        except Exception as e:
            logger.error(f"❌ Save error: {e}")

    def _clean_expired(self):
        cutoff = datetime.now() - self._max_age
        original = len(self._data)
        self._data = {url: dt for url, dt in self._data.items() if dt > cutoff}
        removed = original - len(self._data)
        if removed > 0:
            logger.info(f"🧹 Cleanup: {removed} links verwijderd")

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

class SmartClient:
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]

    def __init__(self, config: BotConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        self._stats = {"requests": 0, "errors": 0, "blocked": 0}
        self._ua_index = 0
        self._domain_delays: Dict[str, datetime] = {}
        self._domain_locks: Dict[str, asyncio.Lock] = {}
        self._domain_block_until: Dict[str, datetime] = {}

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
                if elapsed < 3.0:  # ✅ VERHOOGD VAN 2.5 NAAR 3.0
                    await asyncio.sleep(3.0 - elapsed)
            self._domain_delays[domain] = datetime.now()

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(limit=self.config.max_concurrent_requests, limit_per_host=2, ttl_dns_cache=300)  # ✅ VERLAAGD VAN 3 NAAR 2
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)
        self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def get_html(self, url: str) -> Optional[str]:
        if not self._session:
            raise RuntimeError("Client niet geïnitialiseerd")

        domain = self._get_domain(url)

        block_until = self._domain_block_until.get(domain)
        if block_until and datetime.now() < block_until:
            wait = (block_until - datetime.now()).total_seconds()
            await asyncio.sleep(wait)

        await self._rate_limit(url)

        async with self._semaphore:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    self._stats["requests"] += 1

                    headers = {
                        "User-Agent": self._rotate_ua(),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
                        "Accept-Encoding": "gzip, deflate",
                        "DNT": "1",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1",
                    }

                    async with self._session.get(url, headers=headers, ssl=False) as response:
                        if response.status == 200:
                            self._domain_block_until.pop(domain, None)
                            return await response.text()
                        elif response.status == 403:
                            self._stats["blocked"] += 1
                            wait = min(2 ** attempt * 10, 120)  # ✅ VERHOOGD VAN 5 NAAR 10
                            logger.warning(f"⚠️ 403 blocked - waiting {wait}s")
                            self._domain_block_until[domain] = datetime.now() + timedelta(seconds=wait)
                            await asyncio.sleep(wait)
                        elif response.status in (429, 503):
                            wait = 2 ** attempt
                            logger.warning(f"⚠️ Rate limit {response.status} - waiting {wait}s")
                            await asyncio.sleep(wait)
                        else:
                            logger.warning(f"⚠️ HTTP {response.status} for {url}")
                            return None

                except Exception as e:
                    self._stats["errors"] += 1
                    logger.debug(f"Request error (attempt {attempt}): {e}")
                    if attempt < self.config.max_retries:
                        await asyncio.sleep(2 ** attempt)

        return None

    def get_stats(self) -> Dict[str, int]:
        return self._stats.copy()

class ListingParser:

    @staticmethod
    def parse_marktplaats_json(html: str) -> Tuple[Optional[str], Optional[int], str]:
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)

        if match:
            try:
                data = json.loads(match.group(1))
                listing = data.get("props", {}).get("pageProps", {}).get("listing", {})

                title = listing.get("title", "").strip()
                description = listing.get("description", "").strip()
                price_info = listing.get("priceInfo", {})

                raw_price = price_info.get("priceCents") or price_info.get("price")

                if title and raw_price is not None:
                    price = int(raw_price)
                    if price > PRICE_CENT_THRESHOLD:
                        price = price // 100
                    return title, price, description
            except Exception:
                pass

        title_match = re.search(r'<title>(.*?)</title>', html)
        price_match = re.search(r'"price(?:Cents)?"\s*:\s*"?(\d+)"?', html)

        if not title_match or not price_match:
            return None, None, ""

        title = title_match.group(1).strip()
        try:
            price = int(price_match.group(1))
        except ValueError:
            return None, None, ""

        if price > PRICE_CENT_THRESHOLD:
            price = price // 100

        return title, price, ""

    @staticmethod
    def extract_km(text: str) -> Optional[int]:
        patterns = [
            r'(\d{1,3}(?:[.,\s]\d{3})*)\s*km\b',
            r'km[:\s]*(\d{1,3}(?:[.,\s]\d{3})*)',
            r'\b(\d{4,6})\s*km\b',
        ]

        for pattern in patterns:
            match = re.search(pattern, text.lower())
            if match:
                raw = re.sub(r'[.,\s]', '', match.group(1))
                try:
                    km = int(raw)
                    if 50 < km < 500_000:
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
            r'(\d{4})[- ]model',
            r'bj\.?\s*(\d{4})',
            r'van\s+(\d{4})',
            r'uit\s+(\d{4})',
            r'(\d{4})\s*-\s*heden',
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
                counts = Counter(valid)
                year = counts.most_common(1)[0][0]
                return year

        return None

class MarktplaatsRSSMonitor:

    def __init__(self, filter_config: FilterConfig):
        self.filter_config = filter_config
        self._seen_items: Set[str] = set()
        self._use_fallback = False
        self._seen_items_last_reset = datetime.now()  # ✅ NIEUW

    def _maybe_reset_seen_items(self):
        """Reset seen_items elke 1.5 uur om nieuwe listings te vinden"""
        if datetime.now() - self._seen_items_last_reset > timedelta(hours=1.5):
            old_count = len(self._seen_items)
            self._seen_items = set()
            self._seen_items_last_reset = datetime.now()
            logger.info(f"🔄 RESET seen_items ({old_count} items cleared)")

    async def test_api(self, client: 'SmartClient') -> bool:
        """Test API response"""
        test_url = "https://www.marktplaats.nl/lrp/api/search?query=aygo&limit=10"
        
        logger.info("🧪 Testing Marktplaats API...")
        data = await client.get_html(test_url)
        
        if not data:
            logger.error("❌ API returned nothing - will use HTML fallback")
            self._use_fallback = True
            return False
        
        logger.info(f"✅ API response: {len(data)} bytes")
        
        try:
            json_data = json.loads(data)
            listings = json_data.get('listings', [])
            logger.info(f"✅ API working: {len(listings)} listings found")
            return True
        except Exception as e:
            logger.error(f"❌ API parse failed: {e} - will use HTML fallback")
            logger.debug(f"Response preview: {data[:300]}")
            self._use_fallback = True
            return False

    async def check_rss(self, model: str, client: 'SmartClient') -> List[str]:
        """Check via API (met debug logging)"""
        rss_url = (
            f"https://www.marktplaats.nl/lrp/api/search?"
            f"query={model}&searchInTitleAndDescription=true&limit=150"
        )

        data = await client.get_html(rss_url)
        
        if not data:
            logger.warning(f"⚠️ {model}: NO DATA from API")
            return []

        new_listings = []

        try:
            json_data = json.loads(data)
            listings = json_data.get('listings', [])
            
            if len(listings) == 0:
                logger.warning(f"⚠️ {model}: API returned 0 listings")
            else:
                logger.info(f"📡 {model}: {len(listings)} listings from API")

            for listing in listings:
                listing_id = listing.get('itemId')
                vip_url = listing.get('vipUrl')

                if listing_id and listing_id not in self._seen_items:
                    self._seen_items.add(listing_id)

                    if vip_url:
                        new_listings.append(f"https://www.marktplaats.nl{vip_url}")

            if len(self._seen_items) > 10000:
                self._seen_items = set(list(self._seen_items)[-5000:])

        except Exception as e:
            logger.error(f"❌ RSS parse error for {model}: {e}")

        return new_listings

    async def check_rss_fallback(self, model: str, client: 'SmartClient') -> List[str]:
        """Fallback: scrape HTML search page - VERBETERD MET MEERDERE PATTERNS"""
        
        self._maybe_reset_seen_items()  # ✅ RESET CHECK
        
        # ✅ SORTEER OP DATUM (nieuwste eerst)
        search_url = f"https://www.marktplaats.nl/q/{model}/"
        
        html = await client.get_html(search_url)
        
        if not html:
            logger.warning(f"⚠️ {model}: Geen HTML ontvangen")
            return []
        
        # ✅ SAVE DEBUG HTML (eerste 5 modellen)
        if model in ['aygo', 'yaris', 'c1', '107', 'picanto']:
            debug_file = Path(f"debug_html_{model.replace(' ', '_')}.html")
            try:
                debug_file.write_text(html[:20000])
                logger.info(f"💾 Debug HTML saved: {debug_file}")
            except:
                pass
        
        # ✅ PROBEER ALLE MOGELIJKE PATTERNS
        patterns = [
            r'href="(/a/[^"]+/m\d+)"',
            r'data-url="(/a/[^"]+/m\d+)"',
            r'"vipUrl":"(/a/[^"]+/m\d+)"',
            r'<a[^>]+href="(/a/[^"]+/m\d+)"',
            r'"url":"(https://www\.marktplaats\.nl/a/[^"]+/m\d+)"',
            r'href=\\"(/a/[^\\]+/m\d+)\\"',  # Escaped quotes
        ]
        
        all_matches = []
        for pattern in patterns:
            matches = re.findall(pattern, html)
            all_matches.extend(matches)
        
        # Clean URLs
        cleaned_urls = []
        for match in all_matches:
            if match.startswith('http'):
                cleaned_urls.append(match)
            else:
                cleaned_urls.append(f"https://www.marktplaats.nl{match}")
        
        # Verwijder duplicaten MAAR behoud volgorde
        seen = set()
        unique_urls = []
        for url in cleaned_urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        
        logger.info(f"🔍 {model}: {len(unique_urls)} unieke URLs gevonden in HTML")
        
        # Filter op NIEUWE listings (niet in _seen_items)
        new_listings = []
        for url in unique_urls[:100]:
            if url not in self._seen_items:
                self._seen_items.add(url)
                new_listings.append(url)
        
        if len(new_listings) == 0 and len(unique_urls) > 0:
            logger.warning(f"⚠️ {model}: {len(unique_urls)} URLs maar allemaal AL GEZIEN")
        
        logger.info(f"🔎 {model}: {len(new_listings)} NIEUWE listings (HTML)")
        return new_listings

class ProfitScraper:

    def __init__(
        self,
        filter_config: FilterConfig,
        seen_manager: SeenLinksManager,
        settings: RuntimeSettings,
        market_samples: int = 200,
        market_pool_ttl_hours: int = 1,
    ):
        self.filter_config = filter_config
        self.seen_manager = seen_manager
        self.settings = settings
        self.market_calculator = MarketValueCalculator(
            samples=market_samples,
            pool_ttl_hours=market_pool_ttl_hours,
        )
        self.rdw_client = RDWClient()
        self.rss_monitor = MarktplaatsRSSMonitor(filter_config)
        self.price_tracker = PriceHistoryTracker()

        self._stats = {
            'scans': 0,
            'listings_checked': 0,
            'deals_found': 0,
            'urgent_deals': 0,
            'watchlist_deals': 0,
        }

        self.found_deals: List[Listing] = []

    async def process_listing(
        self,
        url: str,
        search_term: str,
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

        price_drop = self.price_tracker.price_dropped(url, price)
        self.price_tracker.track(url, price)

        km = ListingParser.extract_km(f"{title} {description}")
        if km is None:
            km = ListingParser.extract_km(html)

        year = ListingParser.extract_year(f"{title} {description}")
        if year is None:
            year = ListingParser.extract_year(html)

        try:
            listing = Listing(
                url=url,
                title=title,
                price=price,
                platform="marktplaats",
                search_term=search_term,
                km=km,
                year=year,
                description=description,
            )
            
            listing.price_drop = price_drop

            await listing.analyze(
                self.filter_config,
                self.settings,
                self.market_calculator,
                self.rdw_client,
                client
            )

        except ValueError as e:
            await self.seen_manager.add(url)
            return None

        self._stats['listings_checked'] += 1

        if not listing.is_good_deal:
            await self.seen_manager.add(url)
            return None

        self._stats['deals_found'] += 1
        
        if listing.is_urgent:
            self._stats['urgent_deals'] += 1
        
        if listing.deal_quality == DealQuality.WATCHLIST:
            self._stats['watchlist_deals'] += 1
        
        await self.seen_manager.add(url)

        self.found_deals.append(listing)
        if len(self.found_deals) > 100:
            self.found_deals = self.found_deals[-100:]

        profit = listing.market_analysis.profit_potential if listing.market_analysis else 0

        logger.info(
            f"🎉 DEAL GEVONDEN: {listing.deal_quality.value} | €{profit} | {listing.title[:40]}"
        )

        return listing

    async def scan_model(self, model: str, client: SmartClient) -> List[Listing]:
        # Probeer eerst API
        new_links = await self.rss_monitor.check_rss(model, client)
        
        # Als API faalt OF _use_fallback is True, probeer HTML scraping
        if not new_links or self.rss_monitor._use_fallback:
            fallback_links = await self.rss_monitor.check_rss_fallback(model, client)
            new_links.extend(fallback_links)

        if not new_links:
            return []

        logger.info(f"🔍 {model}: Processing {len(new_links)} nieuwe links...")

        tasks = [self.process_listing(link, model, client) for link in new_links]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        deals = [r for r in results if isinstance(r, Listing) and r is not None]
        
        if deals:
            logger.info(f"✅ {model}: {len(deals)} DEALS FOUND!")
        
        return deals

    async def scan_all(self, client: SmartClient) -> List[Listing]:

        self._stats['scans'] += 1
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🚀 SCAN #{self._stats['scans']}")
        logger.info(f"{'='*60}")

        all_search_terms = []
        for model in self.filter_config.models:
            all_search_terms.append(model)
            aliases = self.filter_config.model_aliases.get(model, [])
            all_search_terms.extend(aliases)

        logger.info(f"🔎 Scanning {len(all_search_terms)} search terms...")

        tasks = [self.scan_model(term, client) for term in all_search_terms]
        results = await asyncio.gather(*tasks)
        all_deals = [deal for model_deals in results for deal in model_deals]

        logger.info(f"{'='*60}")
        logger.info(f"📊 SCAN RESULTS:")
        logger.info(f"  Listings checked: {self._stats['listings_checked']}")
        logger.info(f"  Deals found: {len(all_deals)}")
        logger.info(f"  Total deals in memory: {len(self.found_deals)}")
        
        if len(all_deals) > 0:
            logger.info(f"✅ {len(all_deals)} DEALS TO SEND TO TELEGRAM:")
            for deal in all_deals:
                profit = deal.market_analysis.profit_potential if deal.market_analysis else 0
                logger.info(f"  💰 {deal.deal_quality.value}: €{profit} - {deal.title[:40]}")
        else:
            logger.info(f"❌ NO DEALS FOUND THIS SCAN")
        
        logger.info(f"{'='*60}\n")

        self.price_tracker.cleanup_old()

        return all_deals

    def get_stats(self) -> Dict:
        return self._stats.copy()

    def get_top_deals(self, n: int = 5) -> List[Listing]:
        def profit_of(listing: Listing) -> int:
            if listing.market_analysis and listing.market_analysis.profit_potential:
                return listing.market_analysis.profit_potential
            return 0

        sorted_deals = sorted(self.found_deals, key=profit_of, reverse=True)
        return sorted_deals[:n]

class TelegramNotifier:

    def __init__(self, app, chat_id: str):
        self.app = app
        self.chat_id = chat_id
        self._stats = {"sent": 0}

    async def send_message(self, message: str) -> bool:
        try:
            logger.info(f"📤 Sending to Telegram ({len(message)} chars)...")
            result = await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                disable_web_page_preview=True,
            )
            self._stats["sent"] += 1
            logger.info(f"✅ Telegram message sent (ID: {result.message_id})")
            return True
        except Exception as e:
            logger.error(f"❌ Telegram error: {e}")
            return False

    async def send_listing(self, listing: Listing) -> bool:
        return await self.send_message(listing.format_message())

    async def send_startup(self, min_profit: int) -> bool:
        message = (
            "💰 PROFIT BOT ACTIEF\n\n"
            f"📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🎯 Min. winst: €{min_profit}\n"
            "✅ Scanning started"
        )
        return await self.send_message(message)

class BotCommands:

    def __init__(
        self,
        notifier: TelegramNotifier,
        scraper: ProfitScraper,
        settings: RuntimeSettings,
        bot_config: BotConfig,
    ):
        self.notifier = notifier
        self.scraper = scraper
        self.settings = settings
        self.bot_config = bot_config

    def _is_authorized(self, update) -> bool:
        user_id = str(update.effective_chat.id)
        return user_id == str(self.bot_config.telegram_chat_id)

    async def start(self, update, context):
        if not self._is_authorized(update):
            return
        welcome = "🚀 *Auto Profit Bot*\n\nScanning Marktplaats 24/7.\n\nUse /help for commands"
        await update.message.reply_text(welcome, parse_mode='Markdown')

    async def help(self, update, context):
        if not self._is_authorized(update):
            return
        help_text = "❓ *Commands:*\n\n/stats - Statistics\n/top - Top 5 deals\n/pause - Pause\n/resume - Resume"
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def stats(self, update, context):
        if not self._is_authorized(update):
            return
        stats = self.scraper.get_stats()
        status = "⏸️ PAUSED" if self.settings.paused else "▶️ ACTIVE"
        stats_text = (
            f"📊 *Stats*\n\n"
            f"Status: {status}\n"
            f"Scans: {stats['scans']}\n"
            f"Checked: {stats['listings_checked']}\n"
            f"Deals: {stats['deals_found']}"
        )
        await update.message.reply_text(stats_text, parse_mode='Markdown')

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
        top_deals = self.scraper.get_top_deals(5)
        if not top_deals:
            await update.message.reply_text("📭 No deals yet")
            return
        lines = ["🏆 *Top 5:*\n"]
        for i, deal in enumerate(top_deals, start=1):
            profit = deal.market_analysis.profit_potential if deal.market_analysis else 0
            lines.append(f"{i}. {deal.deal_quality.value} | €{profit}")
        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

async def telegram_error_handler(update, context):
    logger.error(f"❌ Telegram Error: {context.error}")

class ProfitBot:

    def __init__(self, bot_config: BotConfig, filter_config: FilterConfig):
        self.bot_config = bot_config
        self.filter_config = filter_config
        self.settings = load_runtime_settings(bot_config)
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
            logger.info(f"🛑 Shutdown signal received")
            self._shutdown.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    async def _scan_loop(self):
        logger.info("🚀 Scan loop started")
        
        max_wait = 30
        for i in range(max_wait):
            if self.notifier is not None:
                logger.info("✅ Notifier ready")
                break
            await asyncio.sleep(0.1)
        else:
            logger.error("❌ Notifier initialization FAILED")
            return

        startup_ok = await self.notifier.send_startup(self.settings.min_profit_margin)
        if not startup_ok:
            logger.error("❌ Startup notification FAILED")
            return

        logger.info("✅ Startup notification sent")

        async with SmartClient(self.bot_config) as client:
            # ✅ TEST API EERST
            api_works = await self.scraper.rss_monitor.test_api(client)
            if not api_works:
                logger.warning("⚠️ API test failed - will use HTML fallback for all searches")
            
            # ✅ TEST DIRECTE LISTING
            logger.info("🧪 Testing direct listing URL...")
            test_url = "https://www.marktplaats.nl/a/auto-s/personenautos/m2100928353-toyota-aygo-1-0-vvt-i-x-play.html"
            html = await client.get_html(test_url)
            if html:
                title, price, desc = ListingParser.parse_marktplaats_json(html)
                if title and price:
                    logger.info(f"✅ Direct listing works: {title} - €{price}")
                else:
                    logger.warning(f"⚠️ Direct listing returned HTML but parsing failed")
            else:
                logger.error(f"❌ Direct listing FAILED - mogelijk IP geblokkeerd!")
            
            while not self._shutdown.is_set():

                if not self.settings.paused:
                    try:
                        deals = await self.scraper.scan_all(client)
                        
                        logger.info(f"✅ Scan complete: {len(deals)} deals found")

                        if deals:
                            logger.info(f"📤 Sending {len(deals)} deals to Telegram...")
                            for deal in deals:
                                sent = await self.notifier.send_listing(deal)
                                if sent:
                                    logger.info(f"✅ Sent to Telegram: {deal.title[:30]}")
                                else:
                                    logger.error(f"❌ Failed to send: {deal.title[:30]}")
                                await asyncio.sleep(1)
                            logger.info(f"✅ All {len(deals)} deals sent to Telegram!")
                        else:
                            logger.info("ℹ️ No deals to send this scan")

                        await self.seen_manager.cleanup_and_save()

                    except Exception as e:
                        logger.exception(f"❌ Error in scan loop: {e}")
                        await asyncio.sleep(5)

                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self.settings.check_interval
                    )
                    break
                except asyncio.TimeoutError:
                    pass

        logger.info("🛑 Scan loop ended")

    async def _post_init(self, app):
        logger.info("🔧 Initializing bot components...")
        
        self.notifier = TelegramNotifier(app, self.bot_config.telegram_chat_id)
        logger.info("✅ Notifier created")
        
        self.scraper = ProfitScraper(
            self.filter_config,
            self.seen_manager,
            self.settings,
            self.bot_config.market_value_samples,
            self.bot_config.market_pool_ttl_hours,
        )
        logger.info("✅ Scraper created")

        commands = BotCommands(self.notifier, self.scraper, self.settings, self.bot_config)
        
        app.add_handler(CommandHandler("start", commands.start))
        app.add_handler(CommandHandler("help", commands.help))
        app.add_handler(CommandHandler("stats", commands.stats))
        app.add_handler(CommandHandler("pause", commands.pause))
        app.add_handler(CommandHandler("resume", commands.resume))
        app.add_handler(CommandHandler("top", commands.top))
        app.add_error_handler(telegram_error_handler)

        logger.info("✅ Command handlers registered")
        
        await asyncio.sleep(0.5)
        asyncio.create_task(self._scan_loop())
        logger.info("✅ Scan loop task started")

    async def _post_shutdown(self, app):
        logger.info("🔧 Shutting down...")
        await self.seen_manager.cleanup_and_save()

    def run(self):
        logger.info("="*60)
        logger.info("💰 AUTO PROFIT BOT")
        logger.info(f"🎯 Min profit: €{self.settings.min_profit_margin}")
        logger.info("="*60)

        try:
            app = (
                ApplicationBuilder()
                .token(self.bot_config.telegram_token)
                .post_init(self._post_init)
                .post_shutdown(self._post_shutdown)
                .build()
            )

            logger.info("🚀 Starting Telegram polling...")
            app.run_polling(allowed_updates=None, drop_pending_updates=True)

        except Exception as e:
            logger.exception(f"❌ FATAL ERROR: {e}")
            sys.exit(1)

def main():
    try:
        logger.info("Loading config...")
        bot_config = BotConfig.from_env()
        
        logger.info("Loading filters...")
        filter_config = FilterConfig.from_file(Path("filters.json"))
        
        logger.info(f"✅ Ready: {len(filter_config.models)} models")

        atexit.register(
            notify_shutdown_sync,
            bot_config.telegram_token,
            bot_config.telegram_chat_id,
        )

        bot = ProfitBot(bot_config, filter_config)
        bot.run()

    except Exception as e:
        logger.exception(f"❌ STARTUP ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()