import os
import asyncio
import aiohttp
import re
from telegram.ext import ApplicationBuilder

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 30

MODELS = ["aygo", "c1", "107", "picanto", "i10", "yaris"]

print("SMART DEALER MODE STARTING...", flush=True)


async def get_html(url, session):
    try:
        async with session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            if response.status == 200:
                return await response.text()
    except:
        return None


def extract_km(text):
    match = re.search(r'(\d{1,3}\.?\d{3})\s?km', text.lower())
    if match:
        return int(match.group(1).replace(".", ""))
    return None


def extract_year(text):
    match = re.search(r'(20\d{2}|19\d{2})', text)
    if match:
        return int(match.group(1))
    return None


def calculate_buy_limit(model, year, km):

    if not year or not km:
        return None

    if km > 230000:
        return None

    base = 0

    # Bouwjaar basis
    if year >= 2013:
        base = 2800
    elif year >= 2008:
        base = 2400
    else:
        base = 2000

    # KM correctie
    if km < 120000:
        base += 300
    elif km > 180000:
        base -= 400

    return base


async def scan_loop(app):
    print("SMART LOOP STARTED", flush=True)

    await app.bot.send_message(
        chat_id=CHAT_ID,
        text="Smart dealer mode actief (bouwjaar + km)."
    )

    async with aiohttp.ClientSession() as session:
        while True:

            print("Nieuwe smart scan...", flush=True)

            for model in MODELS:

                search_url = (
                    "https://www.marktplaats.nl/l/auto-s/q/"
                    + model +
                    "/?sortBy=SORT_INDEX&sortOrder=DECREASING"
                )

                html = await get_html(search_url, session)
                if not html:
                    continue

                links = re.findall(r'href="(/v/auto[^"]+)"', html)
                links = list(set(links))[:15]

                for link in links:

                    full_link = "https://www.marktplaats.nl" + link
                    listing_html = await get_html(full_link, session)
                    if not listing_html:
                        continue

                    title_match = re.search(r'<title>(.*?)</title>', listing_html)
                    price_match = re.search(r'"price":\s*"(\d+)"', listing_html)

                    if not title_match or not price_match:
                        continue

                    title = title_match.group(1)
                    price = int(price_match.group(1))

                    km = extract_km(listing_html)
                    year = extract_year(title)

                    buy_limit = calculate_buy_limit(model, year, km)

                    if not buy_limit:
                        continue

                    if price > buy_limit:
                        continue

                    message = (
                        "SMART DEAL\n\n"
                        "Model: " + model + "\n"
                        "Titel: " + title + "\n"
                        "Bouwjaar: " + str(year) + "\n"
                        "KM: " + str(km) + "\n"
                        "Vraagprijs: €" + str(price) + "\n"
                        "Max koopprijs: €" + str(buy_limit) + "\n"
                        + full_link
                    )

                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=message
                    )

            await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app):
    asyncio.create_task(scan_loop(app))


def main():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.run_polling()


if __name__ == "__main__":
    main()