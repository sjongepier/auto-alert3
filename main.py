import os
import asyncio
import aiohttp
import re
from telegram.ext import ApplicationBuilder

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 30

BUY_LIMITS = {
    "aygo": 2500,
    "c1": 2300,
    "107": 2300,
    "picanto": 2200,
    "i10": 2200,
    "yaris": 2500
}

print("SNIPER MODE STARTING...", flush=True)


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


async def scan_loop(app):
    print("SNIPER LOOP STARTED", flush=True)

    await app.bot.send_message(
        chat_id=CHAT_ID,
        text="Sniper mode actief (vaste koopgrenzen)."
    )

    async with aiohttp.ClientSession() as session:
        while True:

            print("Nieuwe sniper scan...", flush=True)

            for model, limit in BUY_LIMITS.items():

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

                    if price > limit:
                        continue

                    message = (
                        "SNIPER DEAL\n\n"
                        "Model: " + model + "\n"
                        "Titel: " + title + "\n"
                        "Vraagprijs: €" + str(price) + "\n"
                        "Koopgrens: €" + str(limit) + "\n"
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