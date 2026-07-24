import os
import time
import requests
import asyncio
from telegram import Bot

print("=== DEBUG START ===")
print("ENV TOKEN:", os.getenv("TELEGRAM_TOKEN"))
print("ENV CHAT ID:", os.getenv("TELEGRAM_CHAT_ID"))
print("=== DEBUG END ===")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 180
SEARCH_URL = "https://www.marktplaats.nl/l/auto-s/q/aygo/"

seen_links = set()


async def send_telegram(message):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


def get_listings():
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(SEARCH_URL, headers=headers)
    html = response.text

    listings = []
    parts = html.split('/v/auto')

    for part in parts[1:6]:
        link = "https://www.marktplaats.nl/v/auto" + part.split('"')[0]
        listings.append(link)

    return listings


async def run():
    print("🚗 Smart Auto Alert gestart!")

    while True:
        try:
            print("🔎 Controleren...")
            listings = get_listings()

            for link in listings:
                if link not in seen_links:
                    seen_links.add(link)
                    await send_telegram(f"🚗 Nieuwe listing:\n{link}")

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("Error:", e)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(run())