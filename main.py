import os
import json
import logging
import asyncio
import aiohttp
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from telegram.ext import ApplicationBuilder

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 30))
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", 5))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 10))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))

MAX_KM = int(os.getenv("MAX_KM", 220000))
PRICE_PER_KM_LIMIT = float(os.getenv("PRICE_PER_KM_LIMIT", 0.035))
SEEN_MAX_AGE_DAYS = int(os.getenv("SEEN_MAX_AGE_DAYS", 30))

MODELS = ["aygo", "c1", "107", "picanto", "i10", "yaris"]

MOTIVATION_WORDS = [
    "moet weg",
    "ivm",
    "verhuizing",
    "spoed",
    "geen tijd",
    "overcompleet",
]

DEALER_WORDS = [
    "inruil",
    "garantie",
    "btw",
    "bedrijf",
    "dealer",
]

BUY_LIMITS = [
    {"min_year": 2013, "base": 2800},
    {"min_year": 2008, "base": 2400},
    {"min_year": 0,    "base": 2000},
]

LOW_KM_THRESHOLD  = 120_000
HIGH_KM_THRESHOLD = 180_000
LOW_KM_BONUS      = 300
HIGH_KM_PENALTY   = 400

SEEN_FILE = Path("seen_links.json")
MAX_LINKS_PER_MODEL = 12

# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("marktplaats-bot")

# ---------------------------------------------------------
# DATACLASS
# ---------------------------------------------------------

@dataclass
class Listing:
    url: str
    title: str
    price: int
    km: Optional[int]
    year: Optional[int]
    description: str = ""
    buy_limit: Optional[int] = field(default=None, init=False)
    motivated_seller: bool = field(default=False, init=False)
    is_dealer: bool = field(default=False, init=False)

    def __post_init__(self):
        combined = f"{self.title} {self.description}".lower()
        self.is_dealer = any(w in combined for w in DEALER_WORDS)
        self.motivated_seller = any(w in combined for w in MOTIVATION_WORDS)
        self.buy_limit = self._calculate_buy_limit()

    def _calculate_buy_limit(self) -> Optional[int]:
        if not self.year or not self.km:
            return None
        if self.km > MAX_KM:
            return None

        base = next(
            (b["base"] for b in BUY_LIMITS if self.year >= b["min_year"]),
            BUY_LIMITS[-1]["base"],
        )

        if self.km < LOW_KM_THRESHOLD:
            base += LOW_KM_BONUS
        elif self.km > HIGH_KM_THRESHOLD:
            base -= HIGH_KM_PENALTY

        return base

    @property
    def is_good_deal(self) -> bool:
        if self.is_dealer:
            return False
        if not self.buy_limit:
            return False
        if self.price > self.buy_limit:
            return False
        if self.km and self.price / self.km > PRICE_PER_KM_LIMIT:
            return False
        return True

    def format_message(self) -> str:
        boost = " (MOTIVATED SELLER)" if self.motivated_seller else ""
        return (
            f"AGGRESSIVE DEAL{boost}\n\n"
            f"Titel:       {self.title}\n"
            f"Bouwjaar:    {self.year}\n"
            f"KM:          {self.km:,}\n" if self.km else
            f"KM:          onbekend\n"
        ) + (
            f"Prijs:       €{self.price}\n"
            f"Max koopprijs: €{self.buy_limit}\n"
            f"{self.url}"
        )

# ---------------------------------------------------------
# SEEN LINKS
# ---------------------------------------------------------

class SeenLinks:
    def __init__(self, path: Path, max_age_days: int):
        self._path = path
        self._max_age_seconds = max_age_days * 86400
        self._data: dict[str, float] = self._load()
        self._lock = asyncio.Lock()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except json.JSONDecodeError:
                logger.warning("Kon seen_links.json niet lezen, start leeg.")
        return {}

    def _save(self):
        try:
            self._path.write_text(json.dumps(self._data))
        except Exception as e:
            logger.warning(f"Kon seen_links.json niet opslaan: {e}")

    def _clean(self):
        cutoff = time.time() - self._max_age_seconds
        self._data = {k: v for k, v in self._data.items() if v > cutoff}

    async def contains(self, url: str) -> bool:
        async with self._lock:
            return url in self._data

    async def add(self, url: str):
        async with self._lock:
            self._data[url] = time.time()
            self._save()

    async def clean_and_save(self):
        async with self._lock:
            self._clean()
            self._save()
            logger.info(f"Seen links na cleanup: {len(self._data)}")


seen = SeenLinks(SEEN_FILE, SEEN_MAX_AGE_DAYS)

# ---------------------------------------------------------
# HTTP
# ---------------------------------------------------------

async def get_html(
    url: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> Optional[str]:
    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as response:
                    if response.status == 200:
                        return await response.text()
                    elif response.status in (429, 503):
                        wait = 2 ** attempt
                        logger.warning(
                            f"Rate limited ({response.status}) op {url}, "
                            f"wacht {wait}s (poging {attempt})"
                        )
                        await asyncio.sleep(wait)
                    else:
                        logger.info(
                            f"Onverwachte status {response.status} van {url}"
                        )
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                wait = 2 ** attempt
                logger.warning(
                    f"Fout bij {url}: {e} — retry in {wait}s (poging {attempt})"
                )
                await asyncio.sleep(wait)
        logger.error(f"Alle {MAX_RETRIES} pogingen mislukt voor {url}")
        return None

# ---------------------------------------------------------
# PARSING
# ---------------------------------------------------------

def _extract_from_next_data(html: str) -> tuple[Optional[str], Optional[int], str]:
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
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
        raw_price = price_info.get("priceCents") or price_info.get("price") or price_info.get("askingPrice")

        if not title or raw_price is None:
            return None, None, ""

        # Marktplaats levert soms prijs in centen
        price = int(raw_price)
        if price > 100_000:
            price = price // 100

        return title, price, description
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.debug(f"__NEXT_DATA__ parse fout: {e}")
        return None, None, ""


def _extract_fallback(html: str) -> tuple[Optional[str], Optional[int], str]:
    title_match = re.search(r'<title>(.*?)</title>', html)
    price_match = re.search(r'"price":\s*"?(\d+)"?', html)

    if not title_match or not price_match:
        return None, None, ""

    return (
        title_match.group(1).strip(),
        int(price_match.group(1)),
        "",
    )


def parse_listing_page(html: str) -> tuple[Optional[str], Optional[int], str]:
    title, price, desc = _extract_from_next_data(html)
    if title and price:
        return title, price, desc
    return _extract_fallback(html)


def extract_km(text: str) -> Optional[int]:
    """
    Accepteert:
      123.456 km | 123456 km | 123 456 km | 123,456 km
    """
    pattern = r'(\d{1,3}(?:[.,\s]?\d{3}))\s*km'
    match = re.search(pattern, text.lower())
    if match:
        raw = re.sub(r'[.,\s]', '', match.group(1))
        return int(raw)
    return None


def extract_year(text: str) -> Optional[int]:
    matches = re.findall(r'\b(19[6-9]\d|20[0-2]\d)\b', text)
    if matches:
        # Pak het meest voorkomende of eerste plausibele jaar
        return int(matches[0])
    return None


# ---------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------

async def notify(app, message: str):
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=message)
        logger.info("Telegram bericht verstuurd.")
    except Exception as e:
        logger.error(f"Telegram fout: {e}")

# ---------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------

async def process_listing(
    url: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    app,
):
    if await seen.contains(url):
        return

    html = await get_html(url, session, semaphore)
    if not html:
        return

    title, price, description = parse_listing_page(html)
    if not title or not price:
        logger.debug(f"Kon title/prijs niet parsen: {url}")
        return

    km   = extract_km(html)
    year = extract_year(title) or extract_year(description)

    listing = Listing(
        url=url,
        title=title,
        price=price,
        km=km,
        year=year,
        description=description,
    )

    if not listing.is_good_deal:
        await seen.add(url)
        return

    await notify(app, listing.format_message())
    await seen.add(url)
    logger.info(f"Deal gevonden: {url} | €{price} | {km} km | {year}")


async def scan_model(
    model: str,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    app,
):
    url = (
        f"https://www.marktplaats.nl/l/auto-s/q/{model}/"
        "?sortBy=SORT_INDEX&sortOrder=DECREASING"
    )

    html = await get_html(url, session, semaphore)
    if not html:
        logger.warning(f"Geen HTML voor model: {model}")
        return

    links = re.findall(r'href="(/v/auto[^"#?]+)"', html)
    links = list(dict.fromkeys(links))[:MAX_LINKS_PER_MODEL]

    if not links:
        logger.warning(f"Geen links gevonden voor model: {model}")
        return

    logger.info(f"Model '{model}': {len(links)} listings gevonden.")

    await asyncio.gather(
        *[
            process_listing(
                f"https://www.marktplaats.nl{link}",
                session,
                semaphore,
                app,
            )
            for link in links
        ]
    )


async def scan_all(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    app,
):
    await asyncio.gather(
        *[scan_model(m, session, semaphore, app) for m in MODELS]
    )

# ---------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------

async def scan_loop(app):
    logger.info("Scan loop gestart.")
    await notify(app, "Aggressive deal bot is actief.")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_REQUESTS)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            logger.info("Nieuwe scan gestart...")
            try:
                await scan_all(session, semaphore, app)
            except Exception:
                logger.exception("Onverwachte fout tijdens scan.")

            await seen.clean_and_save()
            logger.info(f"Wacht {CHECK_INTERVAL}s tot volgende scan.")
            await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app):
    asyncio.create_task(scan_loop(app))


def main():
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN ontbreekt.")
        return
    if not CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID ontbreekt.")
        return

    logger.info("Bot wordt opgestart...")
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.run_polling()


if __name__ == "__main__":
    main()