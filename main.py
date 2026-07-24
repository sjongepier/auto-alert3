import os
import asyncio
from telegram import Bot

async def main():
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    print("TOKEN:", token)
    print("CHAT_ID:", chat_id)

    if not token:
        print("❌ TOKEN IS NONE")
        return

    bot = Bot(token=token)
    await bot.send_message(chat_id=chat_id, text="✅ Auto Alert 3 werkt!")

if __name__ == "__main__":
    asyncio.run(main())