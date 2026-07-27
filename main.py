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

from telegram.ext import ApplicationBuilder, CommandHandler


# ============================================================
# CONSTANTEN
# ============================================================

SETTINGS_FILE = Path("runtime_settings.json")
MARKTPLAATS_API = "https://www.marktplaats.nl/lrp/api/search"

# Belangrijk:
# De API werkte bij jou met:
# ?query=aygo&limit=10
#
# Daarom gebruiken we GEEN:
# searchInTitleAndDescription=true
# en ook geen limit=150.
MARKTPLAATS_API_LIMIT = 10

MIN_COMPARISON_SAMPLES = 1

KENTEKEN_PATTERN = re.compile(
    r"\b([A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3}-[A-Za-z0-9]{1,3})\b"
)


# ============================================================
# LOGGING
# ============================================================

class ColoredFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record):
        text = super().format(record)
        color = self.COLORS.get(record.levelname, self.RESET)
        return f"{color}{text}{self.RESET}"


def setup_logging(level=logging.INFO) -> logging.Logger:
    bot_logger = logging.getLogger("profit-bot")
    bot_logger.setLevel(level)
    bot_logger.handlers.clear()
    bot_logger.propagate = False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        ColoredFormatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    bot_logger.addHandler(console_handler)

    file_handler = logging.FileHandler("bot.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    bot_logger.addHandler(file_handler)

    return bot_logger


logger = setup_logging()


# ============================================================
# URL HELPERS
# ============================================================

def build_marktplaats_api_url(query: str, limit: int = MARKTPLAATS_API_LIMIT) -> str:
    """
    Bouwt bewust dezelfde soort URL als de werkende test:

    https://www.marktplaats.nl/lrp/api/search?query=aygo&limit=10

    Geen searchInTitleAndDescription, omdat die parameter HTTP 400 gaf.
    """
    params = {
        "query": query,
        "limit": str(limit),
    }
    return f"{MARKTPLAATS_API}?{urlencode(params)}"


def make_marktplaats_url(value: str) -> str:
    if not value:
        return ""

    value = str(value).strip()

    if value.startswith("http://") or value.startswith("https://"):
        return value

    if not value.startswith("/"):
        value = "/" + value

    return f"https://www.marktplaats.nl{value}"


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

    seen_file: Path = field(
        default_factory=lambda: Path("seen_links.json")
    )

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.getenv("TELEGRAM_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if not token:
            raise ValueError("TELEGRAM_TOKEN ontbreekt")

        if not chat_id:
            raise ValueError("TELEGRAM_CHAT_ID ontbreekt")

        return cls(
            telegram_token=token,
            telegram_chat_id=chat_id,
            check_interval=int(os.getenv("CHECK_INTERVAL", "60")),
            max_concurrent_requests=int(
                os.getenv("MAX_CONCURRENT_REQUESTS", "2")
            ),
            request_timeout=int(
                os.getenv("REQUEST_TIMEOUT", "25")
            ),
            max_retries=int(
                os.getenv("MAX_RETRIES", "3")
            ),
            min_profit_margin=int(
                os.getenv("MIN_PROFIT_MARGIN", "500")
            ),
            max_km=int(
                os.getenv("MAX_KM", "300000")
            ),
            price_per_km_limit=float(
                os.getenv("PRICE_PER_KM_LIMIT", "0.35")
            ),
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
                min_profit_margin=int(
                    data.get(
                        "min_profit_margin",
                        bot_config.min_profit_margin,
                    )
                ),
                max_km=int(
                    data.get(
                        "max_km",
                        bot_config.max_km,
                    )
                ),
                price_per_km_limit=float(
                    data.get(
                        "price_per_km_limit",
                        bot_config.price_per_km_limit,
                    )
                ),
                check_interval=int(
                    data.get(
                        "check_interval",
                        bot_config.check_interval,
                    )
                ),
                paused=bool(data.get("paused", False)),
            )

        except Exception as exc:
            logger.warning(
                f"⚠️ runtime_settings.json kon niet worden geladen: {exc}"
            )

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

        SETTINGS_FILE.write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )

    except Exception as exc:
        logger.error(
            f"❌ runtime_settings.json opslaan mislukt: {exc}"
        )


# ============================================================
# FILTER CONFIG
# ============================================================

@dataclass(frozen=True)
class FilterConfig:
    models: List[str]
    motivation_words: List[str]
    dealer_words: List[str]
    red_flags: List[str]
    quality_indicators: List[str]
    model_aliases: Dict[str, List[str]] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: Path) -> "FilterConfig":
        if not path.exists():
            raise FileNotFoundError(
                f"❌ {path} niet gevonden"
            )

        data = json.loads(path.read_text(encoding="utf-8"))

        return cls(
            models=data.get("models", []),
            motivation_words=data.get("motivation_words", []),
            dealer_words=data.get("dealer_words", []),
            red_flags=data.get("red_flags", []),
            quality_indicators=data.get("quality_indicators", []),
            model_aliases=data.get("model_aliases", {}),
        )


# ============================================================
# TELEGRAM SHUTDOWN
# ============================================================

_shutdown_notified = False


def notify_shutdown_sync(
    token: str,
    chat_id: str,
    reason: str = "Bot is gestopt",
):
    global _shutdown_notified

    if _shutdown_notified:
        return

    _shutdown_notified = True

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"

        text = (
            f"🛑 {reason}\n"
            f"📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')}"
        )

        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": text,
            }
        ).encode()

        request = urllib.request.Request(url, data=data)
        urllib.request.urlopen(request, timeout=10)

        logger.info("📤 Shutdownmelding naar Telegram verstuurd")

    except Exception as exc:
        logger.error(
            f"❌ Shutdownmelding mislukt: {exc}"
        )


# ============================================================
# RDW
# ============================================================

def extract_kenteken(text: str) -> Optional[str]:
    match = KENTEKEN_PATTERN.search(text)

    if not match:
        return None

    candidate = (
        match.group(1)
        .replace("-", "")
        .replace(" ", "")
        .upper()
    )

    if (
        5 <= len(candidate) <= 6
        and any(char.isdigit() for char in candidate)
        and any(char.isalpha() for char in candidate)
    ):
        return candidate

    return None


class RDWClient:
    BASE_URL = "https://opendata.rdw.nl/resource/m9d7-ebf2.json"

    def __init__(self):
        self._cache: Dict[str, Optional[dict]] = {}

    async def lookup(
        self,
        kenteken: str,
        client: "SmartClient",
    ) -> Optional[dict]:
        kenteken = (
            kenteken.upper()
            .replace("-", "")
            .replace(" ", "")
        )

        if kenteken in self._cache:
            return self._cache[kenteken]

        url = f"{self.BASE_URL}?kenteken={kenteken}"
        raw = await client.get_text(url)

        if not raw:
            self._cache[kenteken] = None
            return None

        try:
            results = json.loads(raw)

            if not results:
                self._cache[kenteken] = None
                return None

            self._cache[kenteken] = results[0]
            return results[0]

        except Exception as exc:
            logger.debug(f"RDW parse error: {exc}")
            self._cache[kenteken] = None
            return None


# ============================================================
# HTTP CLIENT
# ============================================================

class SmartClient:
    USER_AGENTS = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/125.0 Safari/537.36"
        ),
    ]

    def __init__(self, config: BotConfig):
        self.config = config

        self._session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(
            config.max_concurrent_requests
        )

        self._ua_index = 0
        self._domain_delays: Dict[str, datetime] = {}
        self._domain_locks: Dict[str, asyncio.Lock] = {}
        self._domain_block_until: Dict[str, datetime] = {}

        self._stats = {
            "requests": 0,
            "errors": 0,
            "blocked": 0,
        }

    def _rotate_user_agent(self) -> str:
        user_agent = self.USER_AGENTS[self._ua_index]
        self._ua_index = (
            self._ua_index + 1
        ) % len(self.USER_AGENTS)

        return user_agent

    def _get_domain(self, url: str) -> str:
        return urlparse(url).netloc

    def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()

        return self._domain_locks[domain]

    async def _rate_limit(self, url: str):
        domain = self._get_domain(url)
        lock = self._get_domain_lock(domain)

        async with lock:
            previous = self._domain_delays.get(domain)

            if previous:
                elapsed = (
                    datetime.now() - previous
                ).total_seconds()

                # Rustig aan om blokkades te voorkomen.
                if elapsed < 1.5:
                    await asyncio.sleep(1.5 - elapsed)

            self._domain_delays[domain] = datetime.now()

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(
            limit=self.config.max_concurrent_requests,
            limit_per_host=2,
            ttl_dns_cache=300,
        )

        timeout = aiohttp.ClientTimeout(
            total=self.config.request_timeout
        )

        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
        )

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self._session:
            await self._session.close()

    async def get_text(self, url: str) -> Optional[str]:
        if not self._session:
            raise RuntimeError(
                "SmartClient is niet geïnitialiseerd"
            )

        domain = self._get_domain(url)

        block_until = self._domain_block_until.get(domain)

        if block_until and datetime.now() < block_until:
            wait_seconds = (
                block_until - datetime.now()
            ).total_seconds()

            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

        await self._rate_limit(url)

        async with self._semaphore:
            for attempt in range(1, self.config.max_retries + 1):
                try:
                    self._stats["requests"] += 1

                    headers = {
                        "User-Agent": self._rotate_user_agent(),
                        "Accept": (
                            "application/json,text/html,"
                            "application/xhtml+xml,*/*;q=0.8"
                        ),
                        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.7",
                        "Cache-Control": "no-cache",
                    }

                    async with self._session.get(
                        url,
                        headers=headers,
                        ssl=False,
                    ) as response:

                        if response.status == 200:
                            self._domain_block_until.pop(
                                domain,
                                None,
                            )

                            return await response.text(
                                errors="replace"
                            )

                        if response.status == 400:
                            body = await response.text(
                                errors="replace"
                            )

                            logger.warning(
                                f"⚠️ HTTP 400 voor {url}"
                            )
                            logger.debug(
                                f"Marktplaats 400 response: {body[:300]}"
                            )

                            # Een 400 opnieuw proberen heeft geen zin.
                            return None

                        if response.status == 403:
                            self._stats["blocked"] += 1

                            wait_seconds = min(
                                10 * attempt,
                                90,
                            )

                            logger.warning(
                                f"⚠️ HTTP 403; wacht {wait_seconds} seconden"
                            )

                            self._domain_block_until[domain] = (
                                datetime.now()
                                + timedelta(seconds=wait_seconds)
                            )

                            await asyncio.sleep(wait_seconds)
                            continue

                        if response.status in (429, 500, 502, 503, 504):
                            wait_seconds = min(
                                2 ** attempt,
                                30,
                            )

                            logger.warning(
                                f"⚠️ HTTP {response.status}; "
                                f"wacht {wait_seconds} seconden"
                            )

                            await asyncio.sleep(wait_seconds)
                            continue

                        logger.warning(
                            f"⚠️ HTTP {response.status} voor {url}"
                        )

                        return None

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    self._stats["errors"] += 1

                    logger.debug(
                        f"Requestfout poging {attempt}: {exc}"
                    )

                    if attempt < self.config.max_retries:
                        await asyncio.sleep(2 ** attempt)

        return None

    def get_stats(self) -> Dict[str, int]:
        return self._stats.copy()


# ============================================================
# PARSING
# ============================================================

class ListingParser:

    @staticmethod
    def parse_price(
        raw_value: Any,
        is_cents: bool = False,
    ) -> Optional[int]:
        if raw_value is None:
            return None

        try:
            if isinstance(raw_value, (int, float)):
                value = float(raw_value)
            else:
                text = str(raw_value).strip()
                text = re.sub(r"[^\d.,]", "", text)

                if not text:
                    return None

                # Nederlandse prijsnotatie zoals 3.995 of 3.995,50.
                if "." in text and "," in text:
                    text = text.replace(".", "").replace(",", ".")
                elif "," in text:
                    if len(text.split(",")[-1]) == 2:
                        text = text.replace(",", ".")
                    else:
                        text = text.replace(",", "")
                elif "." in text:
                    parts = text.split(".")
                    if len(parts[-1]) == 3:
                        text = text.replace(".", "")

                value = float(text)

            if is_cents:
                value = value / 100
            elif value > 100_000:
                # Sommige responses geven eurocenten zonder
                # de naam priceCents.
                value = value / 100

            price = int(round(value))

            if price <= 0:
                return None

            return price

        except (TypeError, ValueError):
            return None

    @staticmethod
    def _find_listing_object(value: Any) -> Optional[dict]:
        if isinstance(value, dict):
            has_title = bool(value.get("title"))
            has_price = any(
                key in value
                for key in (
                    "priceInfo",
                    "price",
                    "priceCents",
                )
            )

            if has_title and has_price:
                return value

            for child in value.values():
                found = ListingParser._find_listing_object(child)

                if found:
                    return found

        elif isinstance(value, list):
            for child in value:
                found = ListingParser._find_listing_object(child)

                if found:
                    return found

        return None

    @staticmethod
    def parse_marktplaats_json(
        html: str,
    ) -> Tuple[Optional[str], Optional[int], str]:
        # Moderne Marktplaats-pagina.
        next_match = re.search(
            r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>'
            r"(.*?)"
            r"</script>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if next_match:
            try:
                data = json.loads(
                    unescape(next_match.group(1))
                )

                listing = ListingParser._find_listing_object(data)

                if listing:
                    title = str(
                        listing.get("title", "")
                    ).strip()

                    description = str(
                        listing.get("description", "")
                    ).strip()

                    price_info = listing.get(
                        "priceInfo",
                        {},
                    )

                    price = None

                    if isinstance(price_info, dict):
                        if price_info.get("priceCents") is not None:
                            price = ListingParser.parse_price(
                                price_info.get("priceCents"),
                                is_cents=True,
                            )
                        elif price_info.get("price") is not None:
                            price = ListingParser.parse_price(
                                price_info.get("price")
                            )

                    if price is None:
                        if listing.get("priceCents") is not None:
                            price = ListingParser.parse_price(
                                listing.get("priceCents"),
                                is_cents=True,
                            )
                        else:
                            price = ListingParser.parse_price(
                                listing.get("price")
                            )

                    if title and price is not None:
                        return title, price, description

            except Exception as exc:
                logger.debug(
                    f"NEXT_DATA parse mislukt: {exc}"
                )

        # Fallback voor JSON-prijs in HTML.
        title = None

        og_title = re.search(
            r'<meta[^>]+property=["\']og:title["\']'
            r'[^>]+content=["\'](.*?)["\']',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if og_title:
            title = unescape(og_title.group(1)).strip()

        if not title:
            title_match = re.search(
                r"<title[^>]*>(.*?)</title>",
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )

            if title_match:
                title = unescape(
                    title_match.group(1)
                ).strip()

        price = None

        price_cents_match = re.search(
            r'"priceCents"\s*:\s*"?([\d.,]+)"?',
            html,
            flags=re.IGNORECASE,
        )

        if price_cents_match:
            price = ListingParser.parse_price(
                price_cents_match.group(1),
                is_cents=True,
            )

        if price is None:
            price_match = re.search(
                r'"price"\s*:\s*"?([\d.,]+)"?',
                html,
                flags=re.IGNORECASE,
            )

            if price_match:
                price = ListingParser.parse_price(
                    price_match.group(1)
                )

        if title and price is not None:
            description_match = re.search(
                r'<meta[^>]+name=["\']description["\']'
                r'[^>]+content=["\'](.*?)["\']',
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )

            description = ""

            if description_match:
                description = unescape(
                    description_match.group(1)
                ).strip()

            return title, price, description

        return None, None, ""

    @staticmethod
    def extract_km(text: str) -> Optional[int]:
        patterns = [
            r"(\d{1,3}(?:[.,\s]\d{3})+)\s*km\b",
            r"\b(\d{4,6})\s*km\b",
            r"km\s*[:\-]?\s*(\d{3,6})\b",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                text,
                flags=re.IGNORECASE,
            )

            if not match:
                continue

            raw = re.sub(
                r"[.,\s]",
                "",
                match.group(1),
            )

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
            r"bouwjaar\s*[:\-]?\s*(\d{4})",
            r"\bjaar\s*[:\-]?\s*(\d{4})",
            r"\b(\d{4})[- ]model\b",
            r"\bbj\.?\s*(\d{4})",
            r"\bvan\s+(\d{4})\b",
            r"\buit\s+(\d{4})\b",
            r"\b(19\d{2}|20\d{2})\b",
        ]

        for pattern in specific_patterns:
            match = re.search(
                pattern,
                text,
                flags=re.IGNORECASE,
            )

            if not match:
                continue

            try:
                year = int(match.group(1))

                if 1990 <= year <= current_year:
                    return year

            except ValueError:
                continue

        years = re.findall(
            r"\b(19\d{2}|20\d{2})\b",
            text,
        )

        valid_years = [
            int(year)
            for year in years
            if 1990 <= int(year) <= current_year
        ]

        if valid_years:
            counts = Counter(valid_years)
            return counts.most_common(1)[0][0]

        return None


# ============================================================
# MARKET VALUE
# ============================================================

class MarketValueCalculator:

    def __init__(
        self,
        samples: int = 50,
        pool_ttl_hours: int = 1,
    ):
        self._samples = samples
        self._pool_ttl = timedelta(
            hours=pool_ttl_hours
        )

        self._pool_cache: Dict[
            str,
            Tuple[List[dict], datetime],
        ] = {}

    @staticmethod
    def _extract_all_text(item: dict) -> str:
        parts = [
            str(item.get("title", "")),
            str(item.get("description", "")),
        ]

        for key in (
            "attributes",
            "specificAttributes",
            "specifics",
        ):
            values = item.get(key)

            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict):
                        parts.append(
                            str(value.get("key", ""))
                        )
                        parts.append(
                            str(value.get("value", ""))
                        )
                    else:
                        parts.append(str(value))

        return " ".join(parts)

    async def _get_pool(
        self,
        search_term: str,
        client: SmartClient,
    ) -> List[dict]:
        cached = self._pool_cache.get(search_term)

        if cached:
            pool, timestamp = cached

            if datetime.now() - timestamp < self._pool_ttl:
                return pool

        # Belangrijk: exact dezelfde simpele API-structuur.
        search_url = build_marktplaats_api_url(
            search_term,
            limit=self._samples
            if self._samples <= 10
            else 10,
        )

        raw = await client.get_text(search_url)

        if not raw:
            if cached:
                return cached[0]

            return []

        try:
            data = json.loads(raw)
            listings = data.get("listings", [])

        except Exception as exc:
            logger.error(
                f"❌ Market pool parse error: {exc}"
            )
            return []

        pool = []

        for item in listings:
            if not isinstance(item, dict):
                continue

            price_info = item.get(
                "priceInfo",
                {},
            )

            price = None

            if isinstance(price_info, dict):
                if price_info.get("priceCents") is not None:
                    price = ListingParser.parse_price(
                        price_info.get("priceCents"),
                        is_cents=True,
                    )
                else:
                    price = ListingParser.parse_price(
                        price_info.get("price")
                    )

            if price is None:
                continue

            if not 300 < price < 50_000:
                continue

            text = self._extract_all_text(item)

            year = ListingParser.extract_year(text)
            km = ListingParser.extract_km(text)

            listing_url = make_marktplaats_url(
                item.get("vipUrl")
                or item.get("url")
                or ""
            )

            pool.append(
                {
                    "price": price,
                    "year": year,
                    "km": km,
                    "url": listing_url,
                }
            )

        self._pool_cache[search_term] = (
            pool,
            datetime.now(),
        )

        return pool

    async def get_market_value(
        self,
        search_term: str,
        year: int,
        km: int,
        exclude_url: str,
        client: SmartClient,
    ) -> Tuple[Optional[float], bool]:
        raw_pool = await self._get_pool(
            search_term,
            client,
        )

        pool = [
            item
            for item in raw_pool
            if item.get("url") != exclude_url
        ]

        if not pool:
            return None, False

        tolerances = [
            (2, 50_000),
            (3, 80_000),
            (5, 120_000),
            (10, 200_000),
        ]

        for year_tolerance, km_tolerance in tolerances:
            matches = [
                item["price"]
                for item in pool
                if item.get("year") is not None
                and item.get("km") is not None
                and abs(item["year"] - year)
                <= year_tolerance
                and abs(item["km"] - km)
                <= km_tolerance
            ]

            if len(matches) >= MIN_COMPARISON_SAMPLES:
                return statistics.median(matches), False

        prices = [
            item["price"]
            for item in pool
            if item.get("price") is not None
        ]

        if len(prices) < 3:
            return None, False

        median_price = statistics.median(prices)

        known_years = [
            item["year"]
            for item in pool
            if item.get("year") is not None
        ]

        if known_years:
            average_year = statistics.mean(known_years)
            year_difference = year - average_year

            adjustment = 1 + (year_difference * 0.08)
            adjustment = max(0.3, min(1.5, adjustment))

            return median_price * adjustment, True

        return median_price, True


# ============================================================
# DEAL ANALYSE
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

    @classmethod
    def analyze(
        cls,
        asking_price: int,
        market_value: Optional[float],
        min_profit: int,
        is_estimated: bool = False,
    ) -> "MarketAnalysis":
        if not market_value or market_value <= 0:
            return cls(
                asking_price=asking_price,
                market_value=None,
                profit_potential=None,
                profit_percentage=None,
                is_profitable=False,
                is_estimated=is_estimated,
            )

        if asking_price < 1_000:
            cost_factor = 0.97
        elif asking_price < 2_000:
            cost_factor = 0.94
        elif asking_price < 5_000:
            cost_factor = 0.90
        else:
            cost_factor = 0.85

        sell_price = market_value * cost_factor
        profit = int(sell_price - asking_price)

        profit_percentage = (
            profit / asking_price * 100
            if asking_price > 0
            else 0
        )

        return cls(
            asking_price=asking_price,
            market_value=market_value,
            profit_potential=profit,
            profit_percentage=profit_percentage,
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

    timestamp: datetime = field(
        default_factory=datetime.now
    )

    motivated_seller: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    is_dealer: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    has_red_flags: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    quality_score: int = field(
        default=0,
        init=False,
        repr=False,
    )
    market_analysis: Optional[MarketAnalysis] = field(
        default=None,
        init=False,
        repr=False,
    )
    deal_quality: DealQuality = field(
        default=DealQuality.POOR,
        init=False,
        repr=False,
    )

    kenteken: Optional[str] = field(
        default=None,
        init=False,
        repr=False,
    )
    rdw_verified: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    is_urgent: bool = field(
        default=False,
        init=False,
        repr=False,
    )
    price_drop: Optional[int] = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self):
        if self.price < 0:
            raise ValueError("Prijs mag niet negatief zijn")

        if self.km is not None and self.km < 0:
            raise ValueError("KM mag niet negatief zijn")

        if self.year is not None:
            if not 1990 <= self.year <= datetime.now().year + 1:
                raise ValueError(
                    f"Ongeldig bouwjaar: {self.year}"
                )

        self._extract_brand_model()

    def _extract_brand_model(self):
        title_lower = self.title.lower()

        brands = {
            "toyota": ["aygo", "yaris", "corolla"],
            "peugeot": ["107", "108", "206", "207"],
            "citroen": ["c1", "c2", "c3"],
            "kia": ["picanto", "rio"],
            "hyundai": ["i10", "i20"],
            "volkswagen": ["up", "polo"],
            "seat": ["mii", "ibiza"],
            "skoda": ["citigo", "fabia"],
            "ford": ["fiesta", "ka"],
            "fiat": ["500", "panda"],
        }

        for brand, models in brands.items():
            brand_match = re.search(
                rf"\b{re.escape(brand)}\b",
                title_lower,
            )

            if not brand_match:
                continue

            self.brand = brand.capitalize()

            for model in models:
                model_match = re.search(
                    rf"\b{re.escape(model)}\b",
                    title_lower,
                )

                if model_match:
                    self.model = model.capitalize()
                    return

        words = self.title.split()

        if words and not self.brand:
            self.brand = words[0]

        if len(words) > 1 and not self.model:
            self.model = words[1]

    def detect_urgency(self) -> bool:
        urgency_signals = [
            r"snel\s+weg",
            r"deze\s+week",
            r"vandaag\s+nog",
            r"moet\s+weg",
            r"emigratie",
            r"inruil",
            r"ruimte\s+nodig",
            r"heden",
            r"spoedverkoop",
            r"direct",
            r"vanavond",
        ]

        text = (
            f"{self.title} {self.description}"
        ).lower()

        return any(
            re.search(signal, text)
            for signal in urgency_signals
        )

    async def _try_rdw_check(
        self,
        rdw_client: RDWClient,
        client: SmartClient,
    ):
        kenteken = extract_kenteken(
            f"{self.title} {self.description}"
        )

        if not kenteken:
            return

        self.kenteken = kenteken

        rdw_info = await rdw_client.lookup(
            kenteken,
            client,
        )

        if not rdw_info:
            return

        self.rdw_verified = True

        rdw_date = rdw_info.get(
            "datum_eerste_toelating"
        )

        if rdw_date and len(str(rdw_date)) >= 4:
            try:
                self.year = int(
                    str(rdw_date)[:4]
                )
            except ValueError:
                pass

        rdw_brand = rdw_info.get("merk")
        rdw_model = rdw_info.get("handelsbenaming")

        if rdw_brand:
            self.brand = str(
                rdw_brand
            ).capitalize()

        if rdw_model:
            self.model = str(
                rdw_model
            ).capitalize()

    async def analyze(
        self,
        filter_config: FilterConfig,
        settings: RuntimeSettings,
        market_calculator: MarketValueCalculator,
        rdw_client: RDWClient,
        client: SmartClient,
    ):
        combined_text = (
            f"{self.title} {self.description}"
        ).lower()

        dealer_matches = [
            word.lower()
            for word in filter_config.dealer_words
            if word.lower() in combined_text
        ]

        red_flag_matches = [
            flag.lower()
            for flag in filter_config.red_flags
            if flag.lower() in combined_text
        ]

        # Minder streng dan eerst:
        # vroeger werd een advertentie al snel geblokkeerd.
        # Nu zijn meerdere signalen nodig.
        self.is_dealer = len(dealer_matches) >= 4
        self.has_red_flags = len(red_flag_matches) >= 4

        self.motivated_seller = any(
            word.lower() in combined_text
            for word in filter_config.motivation_words
        )

        self.is_urgent = self.detect_urgency()

        self.quality_score = sum(
            1
            for indicator in filter_config.quality_indicators
            if indicator.lower() in combined_text
        )

        if self.is_dealer or self.has_red_flags:
            logger.info(
                "   ❌ Geblokkeerd: "
                f"dealer={self.is_dealer}, "
                f"red_flags={len(red_flag_matches)}"
            )

            self.deal_quality = DealQuality.POOR
            return

        await self._try_rdw_check(
            rdw_client,
            client,
        )

        # ----------------------------------------------------
        # GOEDKOPE AUTO'S
        # ----------------------------------------------------
        if self.price <= 1_500:
            logger.info(
                f"   ✅ Lage prijs €{self.price} "
                "→ WATCHLIST"
            )

            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.50,
                profit_potential=int(self.price * 0.40),
                profit_percentage=40,
                is_profitable=True,
                is_estimated=True,
            )
            return

        # ----------------------------------------------------
        # VOLLEDIGE ANALYSE
        # ----------------------------------------------------
        if (
            self.year
            and self.km
            and self.brand
            and self.model
        ):
            logger.info(
                f"   📊 Analyse: {self.brand} "
                f"{self.model}, {self.year}, "
                f"{self.km} km"
            )

            if self.km > settings.max_km:
                logger.info(
                    f"   ❌ Te veel KM: "
                    f"{self.km} > {settings.max_km}"
                )

                self.deal_quality = DealQuality.POOR
                return

            price_per_km = self.price / self.km

            # Iets ruimere grens dan voorheen.
            allowed_price_per_km = max(
                settings.price_per_km_limit,
                0.35,
            )

            if price_per_km > allowed_price_per_km:
                logger.info(
                    f"   ❌ €/km te hoog: "
                    f"€{price_per_km:.2f}"
                )

                self.deal_quality = DealQuality.POOR
                return

            search_term = (
                self.search_term
                or self.model.lower()
            )

            market_value, is_estimated = (
                await market_calculator.get_market_value(
                    search_term,
                    self.year,
                    self.km,
                    self.url,
                    client,
                )
            )

            if not market_value:
                logger.info(
                    "   ⚠️ Geen marktwaarde; "
                    "gebruik goedkope-auto fallback"
                )

                if self.price <= 3_500:
                    self.deal_quality = DealQuality.WATCHLIST
                    self.market_analysis = MarketAnalysis(
                        asking_price=self.price,
                        market_value=self.price * 1.30,
                        profit_potential=int(
                            self.price * 0.25
                        ),
                        profit_percentage=25,
                        is_profitable=True,
                        is_estimated=True,
                    )
                else:
                    self.deal_quality = DealQuality.POOR

                return

            adjusted_min_profit = (
                settings.min_profit_margin
            )

            if self.is_urgent:
                adjusted_min_profit = int(
                    settings.min_profit_margin * 0.50
                )

            self.market_analysis = MarketAnalysis.analyze(
                self.price,
                market_value,
                adjusted_min_profit,
                is_estimated,
            )

            profit = (
                self.market_analysis.profit_potential
                or 0
            )

            if profit >= adjusted_min_profit:
                if profit >= 1_500:
                    self.deal_quality = DealQuality.GODLIKE
                elif profit >= 700:
                    self.deal_quality = DealQuality.EXCELLENT
                elif profit >= 300:
                    self.deal_quality = DealQuality.GOOD
                elif profit >= 100:
                    self.deal_quality = DealQuality.AVERAGE
                else:
                    self.deal_quality = DealQuality.WATCHLIST

                logger.info(
                    f"   ✅ Deal gevonden: winst €{profit}"
                )
                return

            # Minder streng:
            # ook positieve/marginale deals naar Telegram
            # als WATCHLIST.
            if profit >= -50:
                logger.info(
                    f"   👀 WATCHLIST: geschatte winst €{profit}"
                )

                self.deal_quality = DealQuality.WATCHLIST
                return

            logger.info(
                f"   ❌ Geen voldoende winst: €{profit}"
            )

            self.deal_quality = DealQuality.POOR
            return

        # ----------------------------------------------------
        # GEDEELTELIJKE DATA
        # ----------------------------------------------------
        logger.info(
            "   ⚠️ Gedeeltelijke data: "
            f"jaar={self.year}, km={self.km}, "
            f"brand={self.brand}, model={self.model}"
        )

        # Minder strenge fallback-regels.
        if self.price <= 2_000:
            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.40,
                profit_potential=int(self.price * 0.30),
                profit_percentage=30,
                is_profitable=True,
                is_estimated=True,
            )
            return

        if self.km and self.km > 0:
            price_per_km = self.price / self.km

            if price_per_km <= 0.35:
                self.deal_quality = DealQuality.WATCHLIST
                self.market_analysis = MarketAnalysis(
                    asking_price=self.price,
                    market_value=self.price * 1.30,
                    profit_potential=int(self.price * 0.20),
                    profit_percentage=20,
                    is_profitable=True,
                    is_estimated=True,
                )
                return

        if self.motivated_seller and self.price <= 4_500:
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

        if self.year and self.price <= 2_500:
            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.30,
                profit_potential=int(self.price * 0.25),
                profit_percentage=25,
                is_profitable=True,
                is_estimated=True,
            )
            return

        # Extra tolerante maar duidelijk als schatting.
        if self.price <= 3_000:
            self.deal_quality = DealQuality.WATCHLIST
            self.market_analysis = MarketAnalysis(
                asking_price=self.price,
                market_value=self.price * 1.25,
                profit_potential=int(self.price * 0.20),
                profit_percentage=20,
                is_profitable=True,
                is_estimated=True,
            )
            return

        self.deal_quality = DealQuality.POOR

    @property
    def is_good_deal(self) -> bool:
        return self.deal_quality in {
            DealQuality.GODLIKE,
            DealQuality.EXCELLENT,
            DealQuality.GOOD,
            DealQuality.AVERAGE,
            DealQuality.WATCHLIST,
        }

    def format_message(self) -> str:
        emoji_map = {
            DealQuality.GODLIKE: "💎💎💎",
            DealQuality.EXCELLENT: "🔥🔥",
            DealQuality.GOOD: "🔥",
            DealQuality.AVERAGE: "✅",
            DealQuality.WATCHLIST: "👀",
            DealQuality.POOR: "❌",
        }

        emoji = emoji_map[self.deal_quality]

        extra = ""

        if self.is_urgent:
            extra += "\n🚨 URGENTE VERKOOP!"
        elif self.motivated_seller:
            extra += "\n🚨 GEMOTIVEERDE VERKOPER!"

        if self.price_drop:
            extra += (
                f"\n💸 PRIJS GEDAALD: "
                f"€{self.price_drop}"
            )

        rdw_text = ""

        if self.rdw_verified:
            rdw_text = "\n🪪 RDW geverifieerd ✅"

        quality_text = ""

        if self.quality_score > 0:
            quality_text = (
                "\n"
                + ("⭐" * min(self.quality_score, 5))
            )

        market_text = ""

        if (
            self.market_analysis
            and self.market_analysis.market_value
        ):
            profit = (
                self.market_analysis.profit_potential
                or 0
            )

            profit_percentage = (
                self.market_analysis.profit_percentage
                or 0
            )

            estimated_text = ""

            if self.market_analysis.is_estimated:
                estimated_text = "\n⚠️ Schatting"

            market_text = (
                "\n\n💰 WINSTINSCHATTING:\n"
                f"Vraagprijs: €{self.price:,}\n"
                f"Marktwaarde: "
                f"€{self.market_analysis.market_value:,.0f}\n"
                f"Geschatte winst: "
                f"€{profit:,} "
                f"({profit_percentage:.0f}%)"
                f"{estimated_text}"
            )

        km_text = (
            f"{self.km:,}"
            if self.km is not None
            else "?"
        )

        year_text = (
            str(self.year)
            if self.year is not None
            else "?"
        )

        return (
            f"{emoji} {self.deal_quality.value}\n"
            f"{'━' * 40}\n"
            f"{self.title}\n\n"
            f"{self.brand or '?'} "
            f"{self.model or '?'} | "
            f"{year_text} | {km_text} km"
            f"{rdw_text}"
            f"{market_text}"
            f"{quality_text}"
            f"{extra}\n"
            f"{'━' * 40}\n"
            f"🔗 {self.url}"
        )


# ============================================================
# SEEN LINKS
# ============================================================

class SeenLinksManager:

    def __init__(
        self,
        path: Path,
        max_age_days: int,
    ):
        self._path = path
        self._max_age = timedelta(
            days=max_age_days
        )
        self._data: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()

        self._load()

    def _load(self):
        if not self._path.exists():
            return

        try:
            raw = json.loads(
                self._path.read_text(
                    encoding="utf-8"
                )
            )

            self._data = {
                url: datetime.fromisoformat(timestamp)
                for url, timestamp in raw.items()
            }

            logger.info(
                f"✅ {len(self._data)} geziene links geladen"
            )

        except Exception as exc:
            logger.warning(
                f"⚠️ Gezien-links konden niet worden geladen: "
                f"{exc}"
            )
            self._data = {}

    def _save(self):
        try:
            raw = {
                url: timestamp.isoformat()
                for url, timestamp in self._data.items()
            }

            self._path.write_text(
                json.dumps(raw, indent=2),
                encoding="utf-8",
            )

        except Exception as exc:
            logger.error(
                f"❌ Gezien-links opslaan mislukt: {exc}"
            )

    def _clean_expired(self):
        cutoff = datetime.now() - self._max_age

        self._data = {
            url: timestamp
            for url, timestamp in self._data.items()
            if timestamp > cutoff
        }

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


# ============================================================
# PRIJSVERLOOP
# ============================================================

class PriceHistoryTracker:

    def __init__(self):
        self._history: Dict[
            str,
            List[Tuple[datetime, int]],
        ] = {}

    def track(self, url: str, price: int):
        self._history.setdefault(url, []).append(
            (datetime.now(), price)
        )

        self._history[url] = self._history[url][-10:]

    def price_dropped(
        self,
        url: str,
        current_price: int,
    ) -> Optional[int]:
        history = self._history.get(url, [])

        if len(history) < 2:
            return None

        previous_price = history[-2][1]
        difference = previous_price - current_price

        return difference if difference > 0 else None

    def cleanup_old(self, days: int = 7):
        cutoff = datetime.now() - timedelta(days=days)

        for url in list(self._history.keys()):
            self._history[url] = [
                item
                for item in self._history[url]
                if item[0] > cutoff
            ]

            if not self._history[url]:
                del self._history[url]


# ============================================================
# MARKTPLAATS MONITOR
# ============================================================

class MarktplaatsMonitor:

    def __init__(
        self,
        filter_config: FilterConfig,
    ):
        self.filter_config = filter_config
        self._seen_items: Set[str] = set()
        self._last_seen_reset = datetime.now()
        self._use_fallback = False

    def _maybe_reset_seen_items(self):
        # Alleen de tijdelijke monitor-cache resetten.
        # De permanente seen_links.json blijft intact.
        if datetime.now() - self._last_seen_reset > timedelta(
            hours=6
        ):
            old_count = len(self._seen_items)

            self._seen_items.clear()
            self._last_seen_reset = datetime.now()

            logger.info(
                f"🔄 Tijdelijke Marktplaats-cache gereset "
                f"({old_count} items)"
            )

    async def test_api(
        self,
        client: SmartClient,
    ) -> bool:
        """
        Test exact dezelfde endpoint-vorm die bij jou werkte.
        """
        test_url = build_marktplaats_api_url(
            "aygo",
            limit=10,
        )

        logger.info(
            "🧪 Marktplaats API testen..."
        )

        raw = await client.get_text(test_url)

        if not raw:
            logger.error(
                "❌ API-test mislukt"
            )
            self._use_fallback = True
            return False

        try:
            data = json.loads(raw)
            listings = data.get("listings", [])

            logger.info(
                f"✅ API werkt: {len(listings)} listings"
            )

            self._use_fallback = False
            return True

        except Exception as exc:
            logger.error(
                f"❌ API JSON parse mislukt: {exc}"
            )

            self._use_fallback = True
            return False

    async def check_api(
        self,
        model: str,
        client: SmartClient,
    ) -> Tuple[List[str], bool]:
        """
        Retourneert:
        - lijst met URLs
        - True als API-request technisch gelukt is
        """
        self._maybe_reset_seen_items()

        api_url = build_marktplaats_api_url(
            model,
            limit=MARKTPLAATS_API_LIMIT,
        )

        raw = await client.get_text(api_url)

        if not raw:
            logger.warning(
                f"⚠️ {model}: geen data van API"
            )
            return [], False

        try:
            data = json.loads(raw)
            listings = data.get("listings", [])

            if not isinstance(listings, list):
                logger.warning(
                    f"⚠️ {model}: listings is geen lijst"
                )
                return [], False

            logger.info(
                f"📡 {model}: {len(listings)} listings van API"
            )

            new_urls = []

            for listing in listings:
                if not isinstance(listing, dict):
                    continue

                item_id = (
                    listing.get("itemId")
                    or listing.get("id")
                    or listing.get("item_id")
                )

                listing_url = make_marktplaats_url(
                    listing.get("vipUrl")
                    or listing.get("url")
                    or listing.get("itemUrl")
                    or ""
                )

                if not listing_url:
                    continue

                seen_key = (
                    f"id:{item_id}"
                    if item_id is not None
                    else f"url:{listing_url}"
                )

                if seen_key in self._seen_items:
                    continue

                self._seen_items.add(seen_key)
                new_urls.append(listing_url)

            if len(self._seen_items) > 20_000:
                self._seen_items = set(
                    list(self._seen_items)[-10_000:]
                )

            logger.info(
                f"🔎 {model}: "
                f"{len(new_urls)} nieuwe listings"
            )

            return new_urls, True

        except Exception as exc:
            logger.error(
                f"❌ API parse error voor {model}: {exc}"
            )
            return [], False

    async def check_html_fallback(
        self,
        model: str,
        client: SmartClient,
    ) -> List[str]:
        self._maybe_reset_seen_items()

        encoded_model = quote(
            model,
            safe="",
        )

        search_url = (
            "https://www.marktplaats.nl/q/"
            f"{encoded_model}/"
        )

        html = await client.get_text(search_url)

        if not html:
            logger.warning(
                f"⚠️ {model}: geen HTML fallback-data"
            )
            return []

        # Meerdere mogelijke URL-vormen.
        patterns = [
            r'href="(/a/[^"]+/m\d+[^"]*)"',
            r'href="(https://www\.marktplaats\.nl/a/[^"]+/m\d+[^"]*)"',
            r'"vipUrl"\s*:\s*"(/a/[^"]+/m\d+[^"]*)"',
            r'"url"\s*:\s*"(/a/[^"]+/m\d+[^"]*)"',
            r'href=\\"(/a/[^\\]+/m\d+[^\\]*)\\"',
        ]

        found = []

        for pattern in patterns:
            found.extend(
                re.findall(
                    pattern,
                    html,
                    flags=re.IGNORECASE,
                )
            )

        unique_urls = []
        unique_set = set()

        for value in found:
            value = unescape(value)
            full_url = make_marktplaats_url(value)

            if full_url and full_url not in unique_set:
                unique_set.add(full_url)
                unique_urls.append(full_url)

        new_urls = []

        for listing_url in unique_urls[:100]:
            seen_key = f"url:{listing_url}"

            if seen_key in self._seen_items:
                continue

            self._seen_items.add(seen_key)
            new_urls.append(listing_url)

        logger.info(
            f"🔎 {model}: "
            f"{len(new_urls)} nieuwe listings "
            "(HTML fallback)"
        )

        return new_urls


# ============================================================
# SCRAPER
# ============================================================

class ProfitScraper:

    def __init__(
        self,
        filter_config: FilterConfig,
        seen_manager: SeenLinksManager,
        settings: RuntimeSettings,
        market_samples: int,
        market_pool_ttl_hours: int,
    ):
        self.filter_config = filter_config
        self.seen_manager = seen_manager
        self.settings = settings

        self.market_calculator = MarketValueCalculator(
            samples=market_samples,
            pool_ttl_hours=market_pool_ttl_hours,
        )

        self.rdw_client = RDWClient()
        self.monitor = MarktplaatsMonitor(
            filter_config
        )

        self.price_tracker = PriceHistoryTracker()

        self._stats = {
            "scans": 0,
            "listings_checked": 0,
            "deals_found": 0,
            "urgent_deals": 0,
            "watchlist_deals": 0,
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

        html = await client.get_text(url)

        if not html:
            # Niet permanent markeren bij netwerkfout.
            return None

        title, price, description = (
            ListingParser.parse_marktplaats_json(html)
        )

        if not title or price is None:
            logger.debug(
                f"⚠️ Listing kon niet worden gelezen: {url}"
            )
            return None

        price_drop = self.price_tracker.price_dropped(
            url,
            price,
        )

        self.price_tracker.track(url, price)

        combined_text = f"{title} {description}"

        km = ListingParser.extract_km(combined_text)

        if km is None:
            km = ListingParser.extract_km(html)

        year = ListingParser.extract_year(combined_text)

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
                client,
            )

        except ValueError as exc:
            logger.debug(
                f"⚠️ Ongeldige listing {url}: {exc}"
            )
            return None

        self._stats["listings_checked"] += 1

        if not listing.is_good_deal:
            # Slechte listings niet steeds opnieuw analyseren.
            await self.seen_manager.add(url)
            return None

        self._stats["deals_found"] += 1

        if listing.is_urgent:
            self._stats["urgent_deals"] += 1

        if listing.deal_quality == DealQuality.WATCHLIST:
            self._stats["watchlist_deals"] += 1

        await self.seen_manager.add(url)

        self.found_deals.append(listing)
        self.found_deals = self.found_deals[-100:]

        profit = 0

        if listing.market_analysis:
            profit = (
                listing.market_analysis.profit_potential
                or 0
            )

        logger.info(
            f"🎉 DEAL: {listing.deal_quality.value} | "
            f"€{profit} | {listing.title[:60]}"
        )

        return listing

    async def scan_model(
        self,
        model: str,
        client: SmartClient,
    ) -> List[Listing]:
        api_urls, api_ok = await self.monitor.check_api(
            model,
            client,
        )

        new_urls = api_urls

        # Alleen fallback gebruiken als API-request faalt.
        # Als API succesvol is maar alle advertenties al gezien
        # zijn, hoeft HTML niet nogmaals te worden opgehaald.
        if not api_ok:
            new_urls = await self.monitor.check_html_fallback(
                model,
                client,
            )

        if not new_urls:
            return []

        logger.info(
            f"🔍 {model}: "
            f"{len(new_urls)} links verwerken"
        )

        tasks = [
            self.process_listing(
                url,
                model,
                client,
            )
            for url in new_urls
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        deals = []

        for result in results:
            if isinstance(result, Listing):
                deals.append(result)
            elif isinstance(result, Exception):
                logger.error(
                    f"❌ Listing verwerken mislukt: {result}"
                )

        if deals:
            logger.info(
                f"✅ {model}: {len(deals)} deals"
            )

        return deals

    async def scan_all(
        self,
        client: SmartClient,
    ) -> List[Listing]:
        self._stats["scans"] += 1

        scan_number = self._stats["scans"]

        logger.info("")
        logger.info("=" * 60)
        logger.info(f"🚀 SCAN #{scan_number}")
        logger.info("=" * 60)

        terms = []

        for model in self.filter_config.models:
            terms.append(model)
            terms.extend(
                self.filter_config.model_aliases.get(
                    model,
                    [],
                )
            )

        # Dubbelen uit filters verwijderen.
        terms = list(dict.fromkeys(terms))

        logger.info(
            f"🔎 Scanning {len(terms)} zoektermen"
        )

        before_checked = self._stats["listings_checked"]

        tasks = [
            self.scan_model(term, client)
            for term in terms
        ]

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True,
        )

        all_deals = []

        for result in results:
            if isinstance(result, list):
                all_deals.extend(result)
            elif isinstance(result, Exception):
                logger.error(
                    f"❌ Zoekterm mislukt: {result}"
                )

        checked_this_scan = (
            self._stats["listings_checked"]
            - before_checked
        )

        logger.info("=" * 60)
        logger.info("📊 SCAN RESULTATEN")
        logger.info(
            f"  Listings gecontroleerd deze scan: "
            f"{checked_this_scan}"
        )
        logger.info(
            f"  Deals gevonden: {len(all_deals)}"
        )
        logger.info(
            f"  Totaal deals in geheugen: "
            f"{len(self.found_deals)}"
        )

        if all_deals:
            logger.info(
                "✅ Deals worden naar Telegram gestuurd"
            )
        else:
            logger.info(
                "❌ Geen deals gevonden deze scan"
            )

        logger.info("=" * 60)

        self.price_tracker.cleanup_old()

        return all_deals

    def get_stats(self) -> Dict:
        return self._stats.copy()

    def get_top_deals(
        self,
        amount: int = 5,
    ) -> List[Listing]:
        def profit_of(listing: Listing) -> int:
            if (
                listing.market_analysis
                and listing.market_analysis.profit_potential
            ):
                return listing.market_analysis.profit_potential

            return 0

        return sorted(
            self.found_deals,
            key=profit_of,
            reverse=True,
        )[:amount]


# ============================================================
# TELEGRAM
# ============================================================

class TelegramNotifier:

    def __init__(self, app, chat_id: str):
        self.app = app
        self.chat_id = chat_id
        self._stats = {"sent": 0}

    async def send_message(self, message: str) -> bool:
        try:
            # Telegram limiet is ongeveer 4096 tekens.
            if len(message) > 4090:
                message = message[:4090]

            logger.info(
                f"📤 Telegram send ({len(message)} tekens)"
            )

            result = await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                disable_web_page_preview=True,
            )

            self._stats["sent"] += 1

            logger.info(
                f"✅ Telegram verzonden "
                f"(ID: {result.message_id})"
            )

            return True

        except Exception as exc:
            logger.error(
                f"❌ Telegram verzenden mislukt: {exc}"
            )
            return False

    async def send_listing(
        self,
        listing: Listing,
    ) -> bool:
        return await self.send_message(
            listing.format_message()
        )

    async def send_startup(
        self,
        min_profit: int,
    ) -> bool:
        message = (
            "💰 PROFIT BOT ACTIEF\n\n"
            f"📅 {datetime.now(ZoneInfo('Europe/Amsterdam')).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🎯 Minimum winst: €{min_profit}\n"
            "✅ Scanning gestart"
        )

        return await self.send_message(message)


# ============================================================
# BOT COMMANDS
# ============================================================

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
        if not update.effective_chat:
            return False

        return str(update.effective_chat.id) == str(
            self.bot_config.telegram_chat_id
        )

    async def start(self, update, context):
        if not self._is_authorized(update):
            return

        await update.message.reply_text(
            "🚀 Auto Profit Bot actief.\n\n"
            "Gebruik /help voor de commando's."
        )

    async def help(self, update, context):
        if not self._is_authorized(update):
            return

        await update.message.reply_text(
            "❓ Commando's:\n\n"
            "/stats - Statistieken\n"
            "/top - Beste deals\n"
            "/pause - Scanning pauzeren\n"
            "/resume - Scanning hervatten"
        )

    async def stats(self, update, context):
        if not self._is_authorized(update):
            return

        stats = self.scraper.get_stats()
        status = (
            "⏸️ PAUSED"
            if self.settings.paused
            else "▶️ ACTIVE"
        )

        text = (
            "📊 Statistieken\n\n"
            f"Status: {status}\n"
            f"Scans: {stats['scans']}\n"
            f"Gecontroleerd: {stats['listings_checked']}\n"
            f"Deals: {stats['deals_found']}\n"
            f"Watchlist: {stats['watchlist_deals']}"
        )

        await update.message.reply_text(text)

    async def pause(self, update, context):
        if not self._is_authorized(update):
            return

        self.settings.paused = True
        save_runtime_settings(self.settings)

        await update.message.reply_text(
            "⏸️ Scanning gepauzeerd"
        )

    async def resume(self, update, context):
        if not self._is_authorized(update):
            return

        self.settings.paused = False
        save_runtime_settings(self.settings)

        await update.message.reply_text(
            "▶️ Scanning hervat"
        )

    async def top(self, update, context):
        if not self._is_authorized(update):
            return

        deals = self.scraper.get_top_deals(5)

        if not deals:
            await update.message.reply_text(
                "📭 Nog geen deals gevonden"
            )
            return

        lines = ["🏆 Beste deals:\n"]

        for index, deal in enumerate(deals, 1):
            profit = 0

            if deal.market_analysis:
                profit = (
                    deal.market_analysis.profit_potential
                    or 0
                )

            lines.append(
                f"{index}. "
                f"{deal.deal_quality.value} | "
                f"€{profit} | "
                f"{deal.title[:45]}"
            )

        await update.message.reply_text(
            "\n".join(lines)
        )


async def telegram_error_handler(update, context):
    error = context.error

    if error and "Conflict" in str(error):
        logger.error(
            "❌ TELEGRAM CONFLICT: er draait nog een tweede "
            "bot-instance met dezelfde token. Stop de oude "
            "container/worker."
        )
    else:
        logger.error(
            f"❌ Telegram error: {error}"
        )


# ============================================================
# PROFIT BOT
# ============================================================

class ProfitBot:

    def __init__(
        self,
        bot_config: BotConfig,
        filter_config: FilterConfig,
    ):
        self.bot_config = bot_config
        self.filter_config = filter_config

        self.settings = load_runtime_settings(
            bot_config
        )

        self.seen_manager = SeenLinksManager(
            bot_config.seen_file,
            bot_config.seen_max_age_days,
        )

        self.scraper: Optional[ProfitScraper] = None
        self.notifier: Optional[TelegramNotifier] = None

        self._shutdown = asyncio.Event()

        self._setup_signals()

    def _setup_signals(self):
        def handle_signal(signum, frame):
            logger.info(
                "🛑 Shutdown-signaal ontvangen"
            )
            self._shutdown.set()

        signal.signal(
            signal.SIGTERM,
            handle_signal,
        )
        signal.signal(
            signal.SIGINT,
            handle_signal,
        )

    async def _scan_loop(self):
        logger.info(
            "🚀 Scan loop gestart"
        )

        for _ in range(300):
            if self.notifier is not None:
                break

            await asyncio.sleep(0.1)

        if self.notifier is None:
            logger.error(
                "❌ Notifier niet beschikbaar"
            )
            return

        startup_sent = await self.notifier.send_startup(
            self.settings.min_profit_margin
        )

        if not startup_sent:
            logger.error(
                "❌ Startupbericht kon niet worden verzonden"
            )
            return

        async with SmartClient(self.bot_config) as client:
            api_works = await self.scraper.monitor.test_api(
                client
            )

            if not api_works:
                logger.warning(
                    "⚠️ API werkt niet; HTML fallback wordt gebruikt"
                )

            while not self._shutdown.is_set():
                if not self.settings.paused:
                    try:
                        deals = await self.scraper.scan_all(
                            client
                        )

                        logger.info(
                            f"✅ Scan klaar: "
                            f"{len(deals)} deals"
                        )

                        if deals:
                            logger.info(
                                f"📤 {len(deals)} deals "
                                "naar Telegram sturen"
                            )

                            for deal in deals:
                                sent = await self.notifier.send_listing(
                                    deal
                                )

                                if sent:
                                    logger.info(
                                        f"✅ Verzonden: "
                                        f"{deal.title[:50]}"
                                    )
                                else:
                                    logger.error(
                                        f"❌ Niet verzonden: "
                                        f"{deal.title[:50]}"
                                    )

                                await asyncio.sleep(1)

                        else:
                            logger.info(
                                "ℹ️ Geen deals om te verzenden"
                            )

                        await self.seen_manager.cleanup_and_save()

                    except asyncio.CancelledError:
                        raise

                    except Exception as exc:
                        logger.exception(
                            f"❌ Fout in scan loop: {exc}"
                        )
                        await asyncio.sleep(5)

                try:
                    await asyncio.wait_for(
                        self._shutdown.wait(),
                        timeout=self.settings.check_interval,
                    )
                    break

                except asyncio.TimeoutError:
                    pass

        logger.info(
            "🛑 Scan loop beëindigd"
        )

    async def _post_init(self, app):
        logger.info(
            "🔧 Botcomponenten initialiseren..."
        )

        self.notifier = TelegramNotifier(
            app,
            self.bot_config.telegram_chat_id,
        )

        logger.info(
            "✅ Notifier aangemaakt"
        )

        self.scraper = ProfitScraper(
            filter_config=self.filter_config,
            seen_manager=self.seen_manager,
            settings=self.settings,
            market_samples=self.bot_config.market_value_samples,
            market_pool_ttl_hours=self.bot_config.market_pool_ttl_hours,
        )

        logger.info(
            "✅ Scraper aangemaakt"
        )

        commands = BotCommands(
            notifier=self.notifier,
            scraper=self.scraper,
            settings=self.settings,
            bot_config=self.bot_config,
        )

        app.add_handler(
            CommandHandler("start", commands.start)
        )
        app.add_handler(
            CommandHandler("help", commands.help)
        )
        app.add_handler(
            CommandHandler("stats", commands.stats)
        )
        app.add_handler(
            CommandHandler("top", commands.top)
        )
        app.add_handler(
            CommandHandler("pause", commands.pause)
        )
        app.add_handler(
            CommandHandler("resume", commands.resume)
        )

        app.add_error_handler(
            telegram_error_handler
        )

        asyncio.create_task(
            self._scan_loop()
        )

        logger.info(
            "✅ Scan loop task gestart"
        )

    async def _post_shutdown(self, app):
        logger.info(
            "🔧 Bot wordt afgesloten..."
        )

        await self.seen_manager.cleanup_and_save()

    def run(self):
        logger.info("=" * 60)
        logger.info("💰 AUTO PROFIT BOT")
        logger.info(
            f"🎯 Minimum winst: "
            f"€{self.settings.min_profit_margin}"
        )
        logger.info("=" * 60)

        try:
            app = (
                ApplicationBuilder()
                .token(self.bot_config.telegram_token)
                .post_init(self._post_init)
                .post_shutdown(self._post_shutdown)
                .build()
            )

            logger.info(
                "🚀 Telegram polling starten..."
            )

            app.run_polling(
                allowed_updates=None,
                drop_pending_updates=True,
            )

        except Exception as exc:
            logger.exception(
                f"❌ FATALE FOUT: {exc}"
            )
            sys.exit(1)


# ============================================================
# MAIN
# ============================================================

def main():
    try:
        logger.info(
            "Loading config..."
        )

        bot_config = BotConfig.from_env()

        logger.info(
            "Loading filters..."
        )

        filter_config = FilterConfig.from_file(
            Path("filters.json")
        )

        logger.info(
            f"✅ Ready: "
            f"{len(filter_config.models)} modellen"
        )

        atexit.register(
            notify_shutdown_sync,
            bot_config.telegram_token,
            bot_config.telegram_chat_id,
        )

        bot = ProfitBot(
            bot_config,
            filter_config,
        )

        bot.run()

    except Exception as exc:
        logger.exception(
            f"❌ Startup error: {exc}"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()