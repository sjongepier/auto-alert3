import os
import json
import logging
import asyncio
import aiohttp
import re
from pathlib import Path
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

SEEN_FILE = Path("seen_links.json")
SEEN_MAX_AGE_DAYS = int(os.getenv("SEEN_MAX_AGE_DAYS", 30))

# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("marktplaats-bot")

# ---------------------------------------------------------
# SEEN LINKS PERSISTENCE
# ---------------------------------------------------------

def load_seen():
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            return data
        except json.JSONDecodeError:
            logger.warning("Kon seen_links.json niet lezen, start leeg.")
    return {}


def save_seen(seen):
    try:
        SEEN_FILE.write_text(json.dumps(seen))
    except Exception as e:
        logger.warning(f"Kon seen_links.json niet opslaan: {e}")


def clean_old_seen(seen):
    import time
    cutoff = time.time() - SEEN_MAX_AGE_DAYS * 86400
    return {link: ts for link, ts in seen.items() if ts > cutoff}


seen_links = load_seen()

# ---------------------------------------------------------
# HTTP HELPERS
# ---------------------------------------------------------

async def get_html(url, session, semaphore):
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
                            f"Status {response.status} van {url}, wacht {wait}s (poging {attempt})"
                        )
                        await asyncio.sleep(wait)
                    else:
                        logger.info(f"Onverwachte status {response.status} van {url}")
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                wait = 2 ** attempt
                logger.warning(
                    f"Fout bij ophalen {url}: {e}, retry in {wait}s (poging {attempt})"
                )
                await asyncio.sleep(wait)
        logger.error(f"Alle pogingen mislukt voor {url}")
        return None


# ---------------------------------------------------------
# PARSING HELPERS
# ---------------------------------------------------------

def extract_km(text):
    match = re.search(r'(\d{1,3}\.?\d{3})\s?km', text.lower())
    if match:
        return int(match.group(1).replace(".", ""))
    return None


def extract_year(text):
    match = re.search(r'(19\d{2}|20\d{2})', text)
    if match:
        return int(match.group(1))
    return None


def contains_word(text, words):
    text = text.lower()
    return any(word in text for word in words)


def extract_title_and_price(html):
    """
    Probeert eerst gestructureerde JSON-data te vinden.
    Valt terug op regex als dat niet lukt.
    """
    # Poging 1: JSON in __NEXT_DATA__ script tag
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if match:
        try:
            data = json.loads(match.group(1))
            # NOTE: pad kan wijzigen als Marktplaats hun frontend update.
            listing = (
                data.get("props", {})
                .get("pageProps", {})
                .get("listing", {})
            )
            title = listing.get("title")
            price_info = listing.get("priceInfo", {})
            price = price_info.get("price") or price_info.get("askingPrice")
            description = listing.get("description", "")
            if title and price:
                return title, int(price), description
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    # Fallback: regex op title en price
    title_match = re.search(r'<title>(.*?)</title>', html)
    price_match = re.search(r'"price":\s*"?(\d+)"?', html)

    if not title_match or not price_match:
        return None, None, None

    return title_match.group(1), int(price_match.group(1)), html


# ---------------------------------------------------------
# BUSINESS LOGICA
# ---------------------------------------------------------

def calculate_buy_limit(year, km):
    if not year or not km:
        return None

    if km > MAX_KM:
        return None

    if year >= 2013:
        base = 2800
    elif year >= 2008:
        base = 2400
    else:
        base = 2000

    if km < 120000:
        base += 300
    elif km > 180000:
        base -= 400

    return base


def is_good_deal(title, price, km, year, description):
    if contains_word(title, DEALER_WORDS):
        return False, None

    buy_limit = calculate_buy_limit(year, km)
    if not buy_limit:
        return False, None

    if price > buy_limit:
        return False, None

    if km and price / km > PRICE_PER_KM_LIMIT:
        return False, None

    return True, buy_limit


# ---------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------

async def notify(app, message):
    try:
        await app.bot.send_message(chat_id=CHAT_ID, text=message)
    except Exception as e:
        logger.error(f"Kon Telegram-bericht niet versturen: {e}")


# ---------------------------------------------------------
# SCRAPING LOGICA
# ---------------------------------------------------------

async def process_listing(full_link, session, semaphore, app):
    if full_link in seen_links:
        return

    listing_html = await get_html(full_link, session, semaphore)
    if not listing_html:
        return

    title, price, description = extract_title_and_price(listing_html)
    if not title or not price:
        return

    km = extract_km(listing_html)
    year = extract_year(title)

    ok, buy_limit = is_good_deal(title, price, km, year, description)
    if not ok:
        return

    boost = ""
    if contains_word(description or "", MOTIVATION_WORDS):
        boost = " (MOTIVATED SELLER)"

    message = (
        "AGRESSIVE DEAL" + boost + "\n\n"
        f"Titel: {title}\n"
        f"Bouwjaar: {year}\n"
        f"KM: {km}\n"
        f"Prijs: €{price}\n"
        f"Max koopprijs: €{buy_limit}\n"
        f"{full_link}"
    )

    await notify(app, message)

    import time
    seen_links[full_link] = time.time()


async def scan_model(model, session, semaphore, app):
    url = (
        "https://www.marktplaats.nl/l/auto-s/q/"
        + model
        + "/?sortBy=SORT_INDEX&sortOrder=DECREASING"
    )

    html = await get_html(url, session, semaphore)
    if not html:
        return

    links = re.findall(r'href="(/v/auto[^"]+)"', html)
    links = list(dict.fromkeys(links))[:12]

    tasks = []
    for link in links:
        full_link = "https://www.marktplaats.nl" + link
        tasks.append(process_listing(full_link, session, semaphore, app))

    await asyncio.gather(*tasks)


async def scan_marktplaats(session, semaphore, app):
    tasks = [scan_model(model, session, semaphore, app) for model in MODELS]
    await asyncio.gather(*tasks)


# ---------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------

async def scan_loop(app):
    logger.info("Scan loop gestart")

    await notify(app, "Agressive smart dealer actief.")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession() as session:
        while True:
            logger.info("Nieuwe agressieve scan...")
            try:
                await scan_marktplaats(session, semaphore, app)
            except Exception as e:
                logger.exception(f"Onverwachte fout tijdens scan: {e}")

            cleaned = clean_old_seen(seen_links)
            seen_links.clear()
            seen_links.update(cleaned)
            save_seen(seen_links)

            await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app):
    asyncio.create_task(scan_loop(app))


def main():
    if not TOKEN or not CHAT_ID:
        logger.error("TELEGRAM_TOKEN of TELEGRAM_CHAT_ID ontbreekt in env vars.")
        return

    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.run_polling()


if __name__ == "__main__":
    main()