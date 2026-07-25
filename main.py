import os
import asyncio
import aiohttp
import re
from telegram.ext import ApplicationBuilder

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 60
MAX_PRICE = 3000
MIN_MARGIN = 600

MODELS = [
    "aygo",
    "c1",
    "107",
    "picanto",
    "i10",
    "yaris"
]

print("€3000 PRO DEALER BOT STARTING...", flush=True)


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


async def get_market_average(session, model):
    url = "https://www.marktplaats.nl/l/auto-s/q/" + model + "/"
    html = await get_html(url, session)

    if not html:
        return None

    prices = re.findall(r'"price":\s*"(\d+)"', html)
    prices = [int(p) for p in prices if int(p) < 10000]

    if len(prices) < 5:
        return None

    return sum(prices[:15]) / len(prices[:15])


async def scan_loop(app):
    print("SCAN LOOP STARTED", flush=True)

    await app.bot.send_message(
        chat_id=CHAT_ID,
        text="€3000 Pro Dealer Bot actief (min €600 marge)."
    )

    async with aiohttp.ClientSession() as session:
        while True:

            print("Nieuwe scan...", flush=True)

            for model in MODELS:

                market_avg = await get_market_average(session, model)
                if not market_avg:
                    continue

                search_url = (
                    "https://www.marktplaats.nl/l/auto-s/q/"
                    + model +
                    "/?priceTo=3000&sortBy=SORT_INDEX&sortOrder=DECREASING"
                )

                html = await get_html(search_url, session)
                if not html:
                    continue

                links = re.findall(r'href="(/v/auto[^"]+)"', html)
                links = list(set(links))[:6]

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

                    if price > MAX_PRICE:
                        continue

                    margin = market_avg - price
                    if margin < MIN_MARGIN:
                        continue

                    message = (
                        "PRO DEAL\n\n"
                        "Model: " + model + "\n"
                        "Titel: " + title + "\n"
                        "Prijs: €" + str(price) + "\n"
                        "Markt: €" + str(int(market_avg)) + "\n"
                        "Marge: €" + str(int(margin)) + "\n"
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