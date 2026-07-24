import os
import asyncio
import requests
import json
import re
from telegram import Bot

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 180
MAX_PRICE = 5000
MAX_KM = 150000

MODELS = ["aygo", "c1", "107", "up", "polo"]
SEEN_FILE = "seen.json"


# ================= STORAGE =================

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


# ================= HELPERS =================

def is_target_model(title):
    return any(model in title.lower() for model in MODELS)


async def send_telegram(message):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


def get_html(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    return requests.get(url, headers=headers).text


# ================= MARKTPLAATS =================

def scrape_marktplaats():
    url = "https://www.marktplaats.nl/l/auto-s/"
    html = get_html(url)

    links = re.findall(r'href="(/v/auto[^"]+)"', html)

    unique_links = list(set([
        "https://www.marktplaats.nl" + link
        for link in links
    ]))

    return unique_links[:20]


def parse_marktplaats(html):
    title_match = re.search(r'<title>(.*?)</title>', html)
    title = title_match.group(1) if title_match else ""

    # JSON-LD structured data (veel betrouwbaarder)
    price_match = re.search(r'"price":\s*"(\d+)"', html)
    price = int(price_match.group(1)) if price_match else None

    km_match = re.search(r'"mileageFromOdometer":\s*{\s*"value":\s*(\d+)', html)
    km = int(km_match.group(1)) if km_match else None

    return title, price, km


# ================= SCHADEAUTOS =================

def scrape_schadeautos():
    url = "https://www.schadeautos.nl/nl/aanbod"
    html = get_html(url)

    links = re.findall(r'href="(/nl/auto/[^"]+)"', html)

    unique_links = list(set([
        "https://www.schadeautos.nl" + link
        for link in links
    ]))

    return unique_links[:20]


def parse_schadeautos(html):
    title_match = re.search(r'<title>(.*?)</title>', html)
    title = title_match.group(1) if title_match else ""

    price_match = re.search(r'€\s?([\d\.]+)', html)
    price = int(price_match.group(1).replace(".", "")) if price_match else None

    km_match = re.search(r'(\d{1,3}\.\d{3})\s?km', html.lower())
    km = int(km_match.group(1).replace(".", "")) if km_match else None

    return title, price, km


# ================= ENGINE =================

async def process_link(link, seen_links):

    if link in seen_links:
        return

    html = get_html(link)

    if "marktplaats" in link:
        title, price, km = parse_marktplaats(html)
    elif "schadeautos" in link:
        title, price, km = parse_schadeautos(html)
    else:
        return

    if not title or not is_target_model(title):
        return

    if price and price > MAX_PRICE:
        return

    if km and km > MAX_KM:
        return

    message = (
        f"🚗 {title}\n"
        f"💰 €{price}\n"
        f"📏 {km} km\n"
        f"🔗 {link}"
    )

    await send_telegram(message)
    print("✅ Deal gestuurd:", link)

    seen_links.add(link)


async def run():
    print("🚗 PRO SNIPER V3 LIVE")

    seen_links = load_seen()

    while True:
        try:
            print("🔎 Scannen bronnen...")

            links = scrape_marktplaats() + scrape_schadeautos()

            for link in links:
                await process_link(link, seen_links)

            save_seen(seen_links)

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("Error:", e)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(run())