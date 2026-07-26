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

# ---------------------------------------------------------
# CONSTANTS & ENUMS
# ---------------------------------------------------------

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

def setup_logging(level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.getLogger("profit-bot")
    logger.setLevel(level)
    
    logger.handlers = []

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(
        ColoredFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            timezone='Europe/Amsterdam'
        )
    )
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler("bot.log")
    file_handler.setFormatter(
        ColoredFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            timezone='Europe/Amsterdam'
        )
    )
    logger.addHandler(file_handler)

    return logger

logger = setup_logging()

# ---------------------------------------------------------
# CONFIG MANAGEMENT
# ---------------------------------------------------------

@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    telegram_chat_id: str
    check_interval: int = 15
    max_concurrent_requests: int = 5
    request_timeout: int = 15
    max_retries: int = 3

    min_profit_margin: int = 500
    max_km: int = 220_000
    price_per_km_limit: float = 0.15
    seen_max_age_days: int = 30

    market_value_samples: int = 50
    market_pool_ttl_hours: int = 4

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
            check_interval=int(os.getenv("CHECK_INTERVAL", 15)),
            min_profit_margin=int(os.getenv("MIN_PROFIT_MARGIN", 500)),
            max_km=int(os.getenv("MAX_KM", 220_000)),
            price_per_km_limit=float(os.getenv("PRICE_PER_KM_LIMIT", 0.15)),
            market_value_samples=int(os.getenv("MARKET_VALUE_SAMPLES", 50)),
            market_pool_ttl_hours=int(os.getenv("MARKET_POOL_TTL_HOURS", 4)),
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
            logger.warning(f"Kon runtime_settings.json niet laden: {e}")

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
        logger.error(f"Kon runtime_settings.json niet opslaan: {e}")


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
            raise FileNotFoundError(f"filters.json niet gevonden")

        data = json.loads(path.read_text())
        return cls(
            models=data.get("models", []),
            motivation_words=data.get("motivation_words", []),
            dealer_words=data.get("dealer_words", []),
            red_flags=data.get("red_flags", []),
            quality_indicators=data.get("quality_indicators", []),
            model_aliases=data.get("model_aliases", {}),
        )

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
            f"📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')}\n"
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
            logger.debug(f"✅ RDW: {kenteken} → {info.get('merk', '?')} {info.get('handelsbenaming', '?')}")
            return info

        except Exception as e:
            logger.debug(f"RDW parse error: {e}")
            self._cache[kenteken] = None
            return None


# ---------------------------------------------------------
# PRICE HISTORY TRACKER
# ---------------------------------------------------------

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
            self._history[url] = [
                (ts, price) for ts, price in self._history[url]
                if ts > cutoff
            ]
            if not self._history[url]:
                del self._history[url]


# ---------------------------------------------------------
# MARKET VALUE CALCULATOR
# ---------------------------------------------------------

class MarketValueCalculator:

    def __init__(self, samples: int = 50, pool_ttl_hours: int = 4):
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
                logger.debug(f"📦 Pool cache hit voor {search_term}")
                return pool

        search_url = (
            f"https://www.marktplaats.nl/lrp/api/search?"
            f"query={search_term}&searchInTitleAndDescription=true&limit={self._samples}"
        )

        logger.debug(f"🔍 Ophalen pool voor {search_term}")
        raw = await client.get_html(search_url)

        if not raw:
            if search_term in self._pool_cache:
                logger.debug(f"Pool-fetch mislukt voor {search_term}, gebruik oude cache")
                return self._pool_cache[search_term][0]
            logger.warning(f"❌ Kon pool niet ophalen voor {search_term}")
            return []

        try:
            data = json.loads(raw)
            listings = data.get('listings', [])
            logger.debug(f"📊 {len(listings)} listings gevonden voor {search_term}")
        except Exception as e:
            logger.debug(f"Pool parse error voor {search_term}: {e}")
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

            pool.append({
                'price': price,
                'year': item_year,
                'km': item_km,
                'url': full_url,
            })

        self._pool_cache[search_term] = (pool, datetime.now())
        logger.debug(f"✅ Pool opgebouwd voor {search_term}: {len(pool)} bruikbare advertenties")

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
            logger.warning(f"❌ Geen pool data voor {search_term}")
            return None, False

        logger.debug(f"🔎 Zoek marktwaarde voor {search_term} {year} ({km:,} km)")

        tolerances = [(2, 50_000), (3, 80_000), (5, 120_000)]
        
        for year_tol, km_tol in tolerances:
            matches = [
                p['price'] for p in pool
                if p['year'] is not None and p['km'] is not None
                and abs(p['year'] - year) <= year_tol
                and abs(p['km'] - km) <= km_tol
            ]
            
            logger.debug(f"  Tolerantie ±{year_tol}j/±{km_tol}km: {len(matches)} matches")
            
            if len(matches) >= MIN_COMPARISON_SAMPLES:
                value = statistics.median(matches)
                logger.info(
                    f"💰 Marktwaarde {search_term} {year}: €{value:.0f} "
                    f"({len(matches)} matches, ±{year_tol}j/±{km_tol}km)"
                )
                return value, False

        # Fallback
        priced = [p for p in pool if p['price'] is not None]
        if len(priced) < MIN_COMPARISON_SAMPLES:
            logger.warning(f"⚠️  Te weinig data ({len(priced)}) voor {search_term} {year}")
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

        logger.info(f"💰 Marktwaarde {search_term} {year} (SCHATTING): €{estimate:.0f}")

        return estimate, True

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
    is_estimated: bool = False

    @classmethod
    def analyze(
        cls,
        asking_price: int,
        market_value: Optional[float],
        min_profit: int,
        is_estimated: bool = False,
    ) -> 'MarketAnalysis':

        if not market_value:
            logger.debug(f"  ❌ Geen marktwaarde beschikbaar")
            return cls(
                asking_price=asking_price,
                market_value=None,
                profit_potential=None,
                profit_percentage=None,
                is_profitable=False,
                is_estimated=is_estimated,
            )

        if asking_price < 2000:
            cost_factor = 0.95
        elif asking_price < 5000:
            cost_factor = 0.92
        else:
            cost_factor = 0.90

        sell_price = market_value * cost_factor
        profit = int(sell_price - asking_price)
        profit_pct = (profit / asking_price * 100) if asking_price > 0 else 0

        logger.debug(f"  💰 Winst analyse: €{asking_price} → €{sell_price:.0f} = €{profit} winst ({profit_pct:.0f}%)")
        logger.debug(f"  🎯 Min. winst: €{min_profit}, Behaald: €{profit}, Profitable: {profit >= min_profit}")

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

    def detect_urgency(self) -> bool:
        urgency_signals = [
            r'snel\s+weg',
            r'deze\s+week',
            r'vandaag\s+nog',
            r'moet\s+weg',
            r'emigratie',
            r'inruil',
            r'ruimte\s+nodig',
            r'heden',
            r'spoedverkoop',
            r'direct\s+op',
            r'vanavond',
        ]
        text = f"{self.title} {self.description}".lower()
        return any(re.search(signal, text) for signal in urgency_signals)

    async def _try_rdw_check(self, rdw_client: RDWClient, client: 'SmartClient') -> None:
        kenteken = extract_kenteken(f"{self.title} {self.description}")
        if not kenteken:
            logger.debug(f"  🔍 Geen kenteken gevonden")
            return

        self.kenteken = kenteken
        rdw_info = await rdw_client.lookup(kenteken, client)

        if not rdw_info:
            logger.debug(f"  ❌ RDW info niet gevonden voor {kenteken}")
            return

        self.rdw_verified = True
        logger.debug(f"  ✅ RDW verificatie OK voor {kenteken}")

        rdw_date = rdw_info.get('datum_eerste_toelating')
        if rdw_date and len(str(rdw_date)) >= 4:
            try:
                old_year = self.year
                self.year = int(str(rdw_date)[:4])
                logger.debug(f"  📅 Jaar via RDW: {old_year} → {self.year}")
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

        logger.debug(f"\n{'='*60}")
        logger.debug(f"🔍 ANALYSE: {self.title[:50]}...")
        logger.debug(f"  💶 Prijs: €{self.price:,}")
        logger.debug(f"  📏 KM: {self.km:,}" if self.km else "  📏 KM: onbekend")
        logger.debug(f"  📅 Jaar: {self.year}" if self.year else "  📅 Jaar: onbekend")

        combined_text = f"{self.title} {self.description}".lower()

        # ✅ AANGEPAST: Meer specifieke dealer check
        dealer_matches = [word for word in filter_config.dealer_words if word in combined_text]
        dealer_count = len(dealer_matches)
        self.is_dealer = dealer_count >= 3  # ← GEWIJZIGD: van 2 naar 3
        
        logger.debug(f"  🏢 Dealer check: {dealer_count} matches {dealer_matches[:3]}")
        logger.debug(f"  🏢 Is dealer: {self.is_dealer}")
        
        self.motivated_seller = any(word in combined_text for word in filter_config.motivation_words)
        
        red_flag_matches = [flag for flag in filter_config.red_flags if flag in combined_text]
        self.has_red_flags = len(red_flag_matches) > 0
        
        logger.debug(f"  🚩 Red flags: {red_flag_matches[:3] if red_flag_matches else 'geen'}")
        logger.debug(f"  😊 Gemotiveerd: {self.motivated_seller}")
        
        self.is_urgent = self.detect_urgency()
        logger.debug(f"  🚨 Urgent: {self.is_urgent}")

        self.quality_score = sum(
            1 for indicator in filter_config.quality_indicators
            if indicator in combined_text
        )
        logger.debug(f"  ⭐ Kwaliteit score: {self.quality_score}/10")

        if self.is_dealer:
            logger.debug(f"  ❌ GEBLOKKEERD: Dealer")
            self.deal_quality = DealQuality.POOR
            return
            
        if self.has_red_flags:
            logger.debug(f"  ❌ GEBLOKKEERD: Red flags")
            self.deal_quality = DealQuality.POOR
            return

        await self._try_rdw_check(rdw_client, client)

        # ✅ AANGEPAST: Verbeterde data validatie
        if self.year and self.km and self.brand and self.model:
            logger.debug(f"  ✅ Volledige data aanwezig")
            
            if self.km > settings.max_km:
                logger.debug(f"  ❌ GEBLOKKEERD: KM te hoog ({self.km:,} > {settings.max_km:,})")
                self.deal_quality = DealQuality.POOR
                return

            price_per_km = self.price / self.km
            if price_per_km > settings.price_per_km_limit:
                logger.debug(f"  ❌ GEBLOKKEERD: €/km te hoog (€{price_per_km:.2f} > €{settings.price_per_km_limit})")
                self.deal_quality = DealQuality.POOR
                return

            search_term = self.search_term or (self.model.lower() if self.model else "")
            logger.debug(f"  🔎 Zoekterm voor marktwaarde: '{search_term}'")

            market_value, is_estimated = await market_calculator.get_market_value(
                search_term,
                self.year,
                self.km,
                self.url,
                client
            )

            adjusted_min_profit = settings.min_profit_margin
            if self.is_urgent:
                adjusted_min_profit = int(settings.min_profit_margin * 0.7)
                logger.debug(f"  🚨 Urgentie: winstdrempel €{settings.min_profit_margin} → €{adjusted_min_profit}")

            self.market_analysis = MarketAnalysis.analyze(
                self.price,
                market_value,
                adjusted_min_profit,
                is_estimated,
            )

            if not self.market_analysis.is_profitable:
                profit = self.market_analysis.profit_potential or 0
                shortage = adjusted_min_profit - profit
                
                logger.debug(f"  💔 Niet winstgevend: €{profit} < €{adjusted_min_profit}")
                
                if 0 <= shortage <= 200:
                    logger.debug(f"  👀 WATCHLIST: €{shortage} tekort")
                    self.deal_quality = DealQuality.WATCHLIST
                    return
                
                logger.debug(f"  ❌ GEBLOKKEERD: Te weinig winst")
                self.deal_quality = DealQuality.POOR
                return

            profit = self.market_analysis.profit_potential or 0
            logger.debug(f"  ✅ Winstgevend: €{profit}")

            if profit >= 1500:
                self.deal_quality = DealQuality.GODLIKE
            elif profit >= 800:
                self.deal_quality = DealQuality.EXCELLENT
            elif profit >= 400:
                self.deal_quality = DealQuality.GOOD
            elif profit >= 150:
                self.deal_quality = DealQuality.AVERAGE
            else:
                self.deal_quality = DealQuality.POOR
                
            logger.debug(f"  🎯 Deal quality: {self.deal_quality.value}")
                
        else:
            # ✅ AANGEPAST: Betere fallback logica
            logger.debug(f"  ⚠️  Incomplete data (jaar={self.year}, km={self.km}, brand={self.brand}, model={self.model})")
            
            # Extreem lage prijs - altijd interessant
            if self.price < 800:
                logger.debug(f"  ✅ Extreem lage prijs: €{self.price}")
                self.deal_quality = DealQuality.AVERAGE
                return
            
            # Zeer lage €/km (maar alleen als KM bekend is)
            if self.km and self.price / self.km < 0.10:
                logger.debug(f"  ✅ Extreem lage €/km: €{self.price/self.km:.2f}")
                self.deal_quality = DealQuality.AVERAGE
                return
            
            # Gemotiveerde verkoper zonder veel data?
            if self.motivated_seller and self.price < 5000:
                logger.debug(f"  ✅ Gemotiveerde verkoper + lage prijs")
                self.deal_quality = DealQuality.WATCHLIST
                return
            
            # Anders: te incomplete
            logger.debug(f"  ❌ GEBLOKKEERD: Incomplete data + geen deal conditions")
            self.deal_quality = DealQuality.POOR

        logger.debug(f"{'='*60}\n")

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
            urgency = "\n🚨 URGENTE VERKOOP - DIRECT ACTIE!"
        elif self.motivated_seller:
            urgency = "\n🚨 GEMOTIVEERDE VERKOPER!"
        
        if self.price_drop:
            urgency += f"\n💸 PRIJS GEDAALD met €{self.price_drop}!"

        rdw_badge = "\n🪪 RDW geverifieerd ✅" if self.rdw_verified else ""

        quality_stars = "⭐" * min(self.quality_score, 5)
        quality_info = f"\n{quality_stars} Kwaliteit: {self.quality_score}/10" if self.quality_score > 0 else ""

        market_info = ""
        if self.market_analysis and self.market_analysis.market_value:
            profit = self.market_analysis.profit_potential
            profit_pct = self.market_analysis.profit_percentage
            market_val = self.market_analysis.market_value

            estimate_note = ""
            if self.market_analysis.is_estimated:
                estimate_note = "\n⚠️ Schatting (weinig vergelijkingen)"

            if self.price < 2000:
                cost_pct = 5
            elif self.price < 5000:
                cost_pct = 8
            else:
                cost_pct = 10

            market_info = (
                f"\n\n💰 WINST ANALYSE:\n"
                f"├─ Vraagprijs: €{self.price:,}\n"
                f"├─ Marktwaarde: €{market_val:,.0f}\n"
                f"├─ Verkoopprijs*: €{market_val * (1 - cost_pct/100):,.0f}\n"
                f"└─ Winst: €{profit:,} ({profit_pct:.0f}%)"
                f"{estimate_note}\n"
                f"\n*Na {cost_pct}% kosten"
            )
        elif self.deal_quality in (DealQuality.AVERAGE, DealQuality.WATCHLIST):
            market_info = "\n\n💡 LET OP: Incomplete data, handmatige check nodig!"
            if self.km:
                market_info += f"\n📊 Prijs/km: €{self.price/self.km:.2f}"

        time_posted = self.timestamp.strftime("%H:%M:%S")
        km_str = f"{self.km:,}" if self.km else "onbekend"
        year_str = str(self.year) if self.year else "onbekend"

        return (
            f"{emoji} {self.deal_quality.value} DEAL!\n"
            f"{'━' * 30}\n"
            f"🚗 {self.title}\n"
            f"\n📊 SPECIFICATIES:\n"
            f"├─ Merk: {self.brand or '?'} {self.model or ''}\n"
            f"├─ Bouwjaar: {year_str}\n"
            f"├─ KM-stand: {km_str}\n"
            f"└─ Gevonden: {time_posted}"
            f"{rdw_badge}"
            f"{market_info}"
            f"{quality_info}"
            f"{urgency}\n"
            f"{'━' * 30}\n"
            f"🔗 {self.url}\n\n"
            f"⚡ ACTIE: Screenshot + Direct Bellen!\n"
            f"💡 TIP: Bied €{max(self.price - 200, int(self.price * 0.9)):,}"
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
            logger.info(f"✅ Geladen: {len(self._data)} geziene links")
        except Exception as e:
            logger.warning(f"⚠️  Kon seen links niet laden: {e}")
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
            logger.info(f"🧹 Cleanup: {removed} oude links verwijderd")

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
                if elapsed < 2.5:
                    await asyncio.sleep(2.5 - elapsed)
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

        domain = self._get_domain(url)

        block_until = self._domain_block_until.get(domain)
        if block_until and datetime.now() < block_until:
            wait = (block_until - datetime.now()).total_seconds()
            logger.debug(f"⏳ {domain} in cooldown, wacht {wait:.0f}s")
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
                        "Accept-Encoding": "gzip, deflate, br",
                        "DNT": "1",
                        "Connection": "keep-alive",
                        "Upgrade-Insecure-Requests": "1",
                    }

                    async with self._session.get(url, headers=headers) as response:
                        if response.status == 200:
                            self._domain_block_until.pop(domain, None)
                            return await response.text()
                        elif response.status == 403:
                            self._stats["blocked"] += 1
                            wait = min(2 ** attempt * 3, 30)
                            self._domain_block_until[domain] = datetime.now() + timedelta(seconds=wait)
                            logger.warning(f"🚫 Blocked ({domain}), wacht {wait}s")
                            await asyncio.sleep(wait)
                        elif response.status in (429, 503):
                            wait = 2 ** attempt
                            await asyncio.sleep(wait)
                        elif response.status == 404:
                            return None
                        else:
                            logger.debug(f"❌ HTTP {response.status} voor {url}")
                            return None

                except Exception as e:
                    self._stats["errors"] += 1
                    logger.debug(f"❌ Request error (poging {attempt}): {e}")
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

        if match:
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

                if title and raw_price is not None:
                    price = int(raw_price)
                    if price > PRICE_CENT_THRESHOLD:
                        price = price // 100
                    
                    logger.debug(f"  ✅ Parse OK: {title[:40]}... - €{price}")
                    return title, price, description

            except Exception as e:
                logger.debug(f"  ⚠️  JSON parse failed: {e}")

        title_match = re.search(r'<title>(.*?)</title>', html)
        price_match = re.search(r'"price(?:Cents)?"\s*:\s*"?(\d+)"?', html)

        if not title_match or not price_match:
            logger.debug(f"  ❌ Geen titel/prijs gevonden in HTML")
            return None, None, ""

        title = title_match.group(1).strip()
        try:
            price = int(price_match.group(1))
        except ValueError:
            return None, None, ""

        if price > PRICE_CENT_THRESHOLD:
            price = price // 100

        logger.debug(f"  ✅ Regex parse: {title[:40]}... - €{price}")
        return title, price, ""

    @staticmethod
    def extract_km(text: str) -> Optional[int]:
        # ✅ AANGEPAST: Betere KM extractie
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
                    if 50 < km < 500_000:  # ✅ AANGEPAST: van 100 naar 50
                        logger.debug(f"  📏 KM extracted: {km:,}")
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
            r'kenteken\s+(\d{4})',
            r'(\d{4})\s*-\s*heden',
            r'(\d{4})\s*-\s*nu',
        ]

        for pattern in specific_patterns:
            match = re.search(pattern, text.lower())
            if match:
                try:
                    year = int(match.group(1))
                    if 1990 <= year <= current_year:
                        logger.debug(f"  📅 Jaar extracted (pattern): {year}")
                        return year
                except ValueError:
                    continue

        all_years = re.findall(r'\b(19[9]\d|20[0-2]\d)\b', text)

        if all_years:
            valid = [int(y) for y in all_years if 1990 <= int(y) <= current_year]
            if valid:
                counts = Counter(valid)
                year = counts.most_common(1)[0][0]
                logger.debug(f"  📅 Jaar extracted (fallback): {year}")
                return year

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
            f"query={model}&searchInTitleAndDescription=true&limit=50"
        )

        logger.debug(f"🔍 RSS fetch voor '{model}'")
        data = await client.get_html(rss_url)
        
        if not data:
            logger.warning(f"❌ Geen RSS data voor '{model}'")
            return []

        new_listings = []

        try:
            json_data = json.loads(data)
            listings = json_data.get('listings', [])
            
            logger.debug(f"  📊 {len(listings)} listings in RSS feed")

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
            logger.error(f"❌ RSS parse error voor '{model}': {e}")

        logger.debug(f"  ✅ {len(new_listings)} nieuwe listings voor '{model}'")
        return new_listings

# ---------------------------------------------------------
# SCRAPER
# ---------------------------------------------------------

class ProfitScraper:

    def __init__(
        self,
        filter_config: FilterConfig,
        seen_manager: SeenLinksManager,
        settings: RuntimeSettings,
        market_samples: int = 50,
        market_pool_ttl_hours: int = 4,
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
            'blocked_dealer': 0,
            'blocked_red_flags': 0,
            'blocked_km': 0,
            'blocked_price_km': 0,
            'blocked_no_profit': 0,
            'blocked_incomplete': 0,
        }

        self.found_deals: List[Listing] = []

    async def process_listing(
        self,
        url: str,
        search_term: str,
        client: SmartClient,
    ) -> Optional[Listing]:

        if await self.seen_manager.contains(url):
            logger.debug(f"⏭️  Skip (already seen): {url}")
            return None

        logger.debug(f"📄 Processing: {url}")
        html = await client.get_html(url)
        
        if not html:
            logger.debug(f"  ❌ Geen HTML opgehaald")
            await self.seen_manager.add(url)
            return None

        title, price, description = ListingParser.parse_marktplaats_json(html)
        
        if not title or not price:
            logger.debug(f"  ❌ Geen titel/prijs")
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
            logger.warning(f"  ❌ Invalid listing: {e}")
            await self.seen_manager.add(url)
            return None

        self._stats['listings_checked'] += 1

        # Track waarom dingen worden geblokkeerd
        if listing.is_dealer:
            self._stats['blocked_dealer'] += 1
        if listing.has_red_flags:
            self._stats['blocked_red_flags'] += 1

        if not listing.is_good_deal:
            logger.debug(f"  ❌ Geen goede deal (quality={listing.deal_quality.value})")
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
            f"🎉 DEAL GEVONDEN: {listing.deal_quality.value} | "
            f"€{profit} winst | {listing.brand} {listing.model} {listing.year}"
        )

        return listing

    async def scan_model(self, model: str, client: SmartClient) -> List[Listing]:

        new_links = await self.rss_monitor.check_rss(model, client)

        if new_links:
            logger.info(f"📡 {model}: {len(new_links)} nieuwe listings")

        if not new_links:
            return []

        tasks = [self.process_listing(link, model, client) for link in new_links]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        deals = [r for r in results if isinstance(r, Listing) and r is not None]
        return deals

    async def scan_all(self, client: SmartClient) -> List[Listing]:

        self._stats['scans'] += 1
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🚀 SCAN #{self._stats['scans']} GESTART")
        logger.info(f"{'='*60}\n")

        all_search_terms = []
        for model in self.filter_config.models:
            all_search_terms.append(model)
            aliases = self.filter_config.model_aliases.get(model, [])
            all_search_terms.extend(aliases)

        logger.info(f"🔎 Zoektermen: {', '.join(all_search_terms)}")

        tasks = [
            self.scan_model(term, client)
            for term in all_search_terms
        ]

        results = await asyncio.gather(*tasks)
        all_deals = [deal for model_deals in results for deal in model_deals]

        logger.info(f"\n{'='*60}")
        logger.info(
            f"✅ SCAN COMPLEET: {len(all_deals)} deals | "
            f"{self._stats['listings_checked']} bekeken | "
            f"{self._stats['urgent_deals']} urgent"
        )
        logger.info(f"📊 BLOKKADES:")
        logger.info(f"  - Dealers: {self._stats['blocked_dealer']}")
        logger.info(f"  - Red flags: {self._stats['blocked_red_flags']}")
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
            logger.debug(f"📤 Verstuur Telegram bericht ({len(message)} chars)")
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                disable_web_page_preview=True,
            )
            self._stats["sent"] += 1
            logger.info(f"✅ Telegram bericht verstuurd")
            return True
        except Exception as e:
            logger.error(f"❌ Telegram error: {e}", exc_info=True)
            return False

    async def send_listing(self, listing: Listing) -> bool:
        return await self.send_message(listing.format_message())

    async def send_startup(self, min_profit: int) -> bool:
        message = (
            "💰 PROFIT BOT ACTIEF (DEBUG MODE)\n\n"
            f"📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🎯 Min. winst: €{min_profit}\n"
            "✅ Monitoring gestart\n\n"
            "🔍 DEBUG LOGGING ACTIEF"
        )
        logger.info("📤 Verstuur startup bericht")
        return await self.send_message(message)

# ---------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------

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
        authorized = user_id == str(self.bot_config.telegram_chat_id)
        
        logger.debug(f"🔐 Auth check: user={user_id}, expected={self.bot_config.telegram_chat_id}, authorized={authorized}")
        
        return authorized

    async def start(self, update, context):
        logger.info(f"📨 /start command ontvangen van {update.effective_chat.id}")
        
        if not self._is_authorized(update):
            logger.warning(f"⛔ Ongeautoriseerde toegang geweigerd")
            return

        welcome = (
            "🚀 *Welkom bij Auto Profit Bot (DEBUG)*\n\n"
            "Ik scan 24/7 Marktplaats voor deals.\n\n"
            "✅ Marktwaarde analyse\n"
            "✅ Urgentie detectie\n"
            "✅ Prijsdaling tracking\n\n"
            "🔍 DEBUG MODE ACTIEF\n"
            "Bekijk bot.log voor details\n\n"
            "Gebruik /help voor meer info."
        )
        await update.message.reply_text(welcome, parse_mode='Markdown')
        logger.info("✅ Start bericht verstuurd")

    async def help(self, update, context):
        logger.info(f"📨 /help command ontvangen")
        
        if not self._is_authorized(update):
            return

        help_text = (
            "❓ *Commands:*\n\n"
            "/stats - Statistieken\n"
            "/top - Beste deals\n"
            "/settings - Instellingen\n"
            "/pause - Pauzeren\n"
            "/resume - Hervatten\n"
            "/test - Test Telegram verbinding"
        )
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def test(self, update, context):
        """Test command"""
        logger.info(f"📨 /test command ontvangen")
        
        if not self._is_authorized(update):
            return
        
        test_msg = (
            "✅ TELEGRAM WERKT!\n\n"
            f"Chat ID: {update.effective_chat.id}\n"
            f"Tijd: {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%H:%M:%S')}\n"
            f"Bot draait: JA"
        )
        await update.message.reply_text(test_msg)
        logger.info("✅ Test bericht verstuurd")

    async def stats(self, update, context):
        logger.info(f"📨 /stats command ontvangen")
        
        if not self._is_authorized(update):
            return

        stats = self.scraper.get_stats()
        status = "⏸️ GEPAUZEERD" if self.settings.paused else "▶️ ACTIEF"

        stats_text = (
            "📊 *Bot Statistieken (DEBUG)*\n\n"
            f"Status: {status}\n\n"
            f"🔍 Scans: {stats['scans']}\n"
            f"📋 Bekeken: {stats['listings_checked']}\n"
            f"💰 Deals: {stats['deals_found']}\n"
            f"🚨 Urgent: {stats['urgent_deals']}\n"
            f"👀 Watchlist: {stats['watchlist_deals']}\n\n"
            f"❌ BLOKKADES:\n"
            f"Dealers: {stats['blocked_dealer']}\n"
            f"Red flags: {stats['blocked_red_flags']}\n\n"
            f"⚡ Interval: {self.settings.check_interval}s\n"
            f"🎯 Min. winst: €{self.settings.min_profit_margin}"
        )
        await update.message.reply_text(stats_text, parse_mode='Markdown')

    async def pause(self, update, context):
        logger.info(f"📨 /pause command ontvangen")
        
        if not self._is_authorized(update):
            return

        self.settings.paused = True
        save_runtime_settings(self.settings)
        await update.message.reply_text("⏸️ Bot gepauzeerd")
        logger.info("⏸️ Bot gepauzeerd")

    async def resume(self, update, context):
        logger.info(f"📨 /resume command ontvangen")
        
        if not self._is_authorized(update):
            return

        self.settings.paused = False
        save_runtime_settings(self.settings)
        await update.message.reply_text("▶️ Bot hervat")
        logger.info("▶️ Bot hervat")

    async def settings_cmd(self, update, context):
        logger.info(f"📨 /settings command ontvangen")
        
        if not self._is_authorized(update):
            return

        args = context.args

        if not args:
            text = (
                "⚙️ *Instellingen*\n\n"
                f"🎯 Min. winst: €{self.settings.min_profit_margin}\n"
                f"🛣️ Max. KM: {self.settings.max_km:,}\n"
                f"💶 Max €/km: {self.settings.price_per_km_limit}\n"
                f"⏱️ Interval: {self.settings.check_interval}s\n\n"
                "*Wijzigen:*\n"
                "/settings minprofit <bedrag>\n"
                "/settings maxkm <km>\n"
                "/settings interval <sec>"
            )
            await update.message.reply_text(text, parse_mode='Markdown')
            return

        if len(args) < 2:
            await update.message.reply_text("⚠️ Gebruik: /settings <optie> <waarde>")
            return

        option = args[0].lower()
        value_raw = args[1]

        try:
            if option == "minprofit":
                value = int(value_raw)
                self.settings.min_profit_margin = value
                save_runtime_settings(self.settings)
                await update.message.reply_text(f"✅ Min. winst: €{value}")
                logger.info(f"⚙️  Min profit aangepast naar €{value}")

            elif option == "maxkm":
                value = int(value_raw)
                self.settings.max_km = value
                save_runtime_settings(self.settings)
                await update.message.reply_text(f"✅ Max. KM: {value:,}")

            elif option == "interval":
                value = int(value_raw)
                self.settings.check_interval = value
                save_runtime_settings(self.settings)
                await update.message.reply_text(f"✅ Interval: {value}s")
                
            else:
                await update.message.reply_text("⚠️ Opties: minprofit, maxkm, interval")
        except ValueError:
            await update.message.reply_text("⚠️ Ongeldige waarde")

    async def top(self, update, context):
        logger.info(f"📨 /top command ontvangen")
        
        if not self._is_authorized(update):
            return

        top_deals = self.scraper.get_top_deals(5)

        if not top_deals:
            await update.message.reply_text("📭 Nog geen deals")
            return

        lines = ["🏆 *Top 5 deals:*\n"]
        for i, deal in enumerate(top_deals, start=1):
            profit = deal.market_analysis.profit_potential if deal.market_analysis else 0
            urgent = " 🚨" if deal.is_urgent else ""
            lines.append(
                f"{i}. {deal.deal_quality.value}{urgent}\n"
                f"   {deal.brand} {deal.model} {deal.year}\n"
                f"   €{deal.price:,} → €{profit:,}\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode='Markdown',
        )

# ---------------------------------------------------------
# TELEGRAM ERROR HANDLER
# ---------------------------------------------------------

async def telegram_error_handler(update, context):
    logger.error(f"❌ Telegram error: {context.error}", exc_info=context.error)

# ---------------------------------------------------------
# MAIN BOT
# ---------------------------------------------------------

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
            logger.info(f"🛑 Shutdown signal: {signal.Signals(signum).name}")
            self._shutdown.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    async def _scan_loop(self):
        logger.info("🚀 Scan loop gestart")
        await self.notifier.send_startup(self.settings.min_profit_margin)

        async with SmartClient(self.bot_config) as client:
            while not self._shutdown.is_set():

                if self.settings.paused:
                    logger.info("⏸️ Gepauzeerd")
                else:
                    try:
                        deals = await self.scraper.scan_all(client)

                        logger.info(f"📤 Verstuur {len(deals)} deals naar Telegram")
                        for deal in deals:
                            await self.notifier.send_listing(deal)
                            await asyncio.sleep(1)

                        await self.seen_manager.cleanup_and_save()

                    except Exception as e:
                        logger.exception(f"❌ Scan error: {e}")

                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self.settings.check_interval
                    )
                    break
                except asyncio.TimeoutError:
                    pass

        logger.info("🛑 Bot gestopt")

    async def _post_init(self, app):
        logger.info("🔧 Post-init gestart")
        
        self.notifier = TelegramNotifier(app, self.bot_config.telegram_chat_id)
        self.scraper = ProfitScraper(
            self.filter_config,
            self.seen_manager,
            self.settings,
            self.bot_config.market_value_samples,
            self.bot_config.market_pool_ttl_hours,
        )

        commands = BotCommands(self.notifier, self.scraper, self.settings, self.bot_config)
        
        app.add_handler(CommandHandler("start", commands.start))
        app.add_handler(CommandHandler("help", commands.help))
        app.add_handler(CommandHandler("test", commands.test))
        app.add_handler(CommandHandler("stats", commands.stats))
        app.add_handler(CommandHandler("pause", commands.pause))
        app.add_handler(CommandHandler("resume", commands.resume))
        app.add_handler(CommandHandler("settings", commands.settings_cmd))
        app.add_handler(CommandHandler("top", commands.top))
        app.add_error_handler(telegram_error_handler)

        logger.info("✅ Handlers geregistreerd")
        
        asyncio.create_task(self._scan_loop())
        logger.info("✅ Scan loop task gestart")

    async def _post_shutdown(self, app):
        logger.info("🔧 Post-shutdown")
        await self.seen_manager.cleanup_and_save()

    def run(self):
        logger.info("="*60)
        logger.info("💰 PROFIT BOT START (DEBUG MODE)")
        logger.info(f"🎯 Min winst: €{self.settings.min_profit_margin}")
        logger.info(f"📱 Chat ID: {self.bot_config.telegram_chat_id}")
        logger.info("="*60)

        try:
            app = (
                ApplicationBuilder()
                .token(self.bot_config.telegram_token)
                .post_init(self._post_init)
                .post_shutdown(self._post_shutdown)
                .build()
            )

            logger.info("🚀 Start polling...")
            app.run_polling(allowed_updates=None, drop_pending_updates=True)

        except Exception as e:
            logger.exception(f"❌ Fatal error: {e}")
            sys.exit(1)

# ---------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------

def main():
    try:
        logger.info("🔧 Laden config...")
        bot_config = BotConfig.from_env()
        
        logger.info("🔧 Laden filters...")
        filter_config = FilterConfig.from_file(Path("filters.json"))
        
        logger.info(f"✅ Config geladen: {len(filter_config.models)} modellen")

        atexit.register(
            notify_shutdown_sync,
            bot_config.telegram_token,
            bot_config.telegram_chat_id,
        )

        bot = ProfitBot(bot_config, filter_config)
        bot.run()

    except Exception as e:
        logger.exception(f"❌ Startup error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()