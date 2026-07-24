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

SEARCH_URL = "https://www.marktplaats.nl/l/auto-s/"

MODELS = [
    "aygo",
    "c1",
    "107",
    "up",
    "polo"
]

SEEN_FILE = "seen.json"


# =========================

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def extract_price(html):
    match = re.search(r'"price":\s*"?(\\d+)"?', html)
    if match:
        return int(match.group(1))

    match = re.search(r'€\s?([\d\.]{4,})', html)
    if match:
        return int(match.group(1).replace(".", ""))

    return None


def extract_km(html):
    match = re.search(r'(\d{1,3}\.\d{3})\s?km', html.lower())
    if match:
        return int(match.group(1).replace(".", ""))
    return None


def is_target_model(title):
    title = title.lower()
    return any(model in title for model in MODELS)


async def send_telegram(message):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


def get_listings():
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(SEARCH_URL, headers=headers)
    html = response.text

    listings = []
    parts = html.split('/v/auto')

    for part in parts[1:20]:
        link = "https://www.marktplaats.nl/v/auto" + part.split('"')[0]
        listings.append(link)

    return list(set(listings))


def get_detail(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)
    return response.text


# =========================

async def run():
    print("🚗 SNIPER LIVE")

    seen_links = load_seen()

    while True:
        try:
            print("🔎 Scannen...")

            listings = get_listings()

            for link in listings:

                if link in seen_links:
                    continue

                html = get_detail(link)

                title_match = re.search(r'<title>(.*?)</title>', html)
                title = title_match.group(1) if title_match else ""

                if not is_target_model(title):
                    continue

                price = extract_price(html)
                km = extract_km(html)

                if price and price > MAX_PRICE:
                    continue

                if km and km > MAX_KM:
                    continue

                message = (
                    f"🚗 {title}\n"
                    f"💰 €{price}\n"
                    f"📏 {km} km\n"
                    f"🔗 {link}"
                )

                await send_telegram(message)
                print("✅ Deal gestuurd")

                seen_links.add(link)

            save_seen(seen_links)

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("Error:", e)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(run())