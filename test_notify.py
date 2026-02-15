import asyncio
from notifier import notify

async def main():
    await notify("🚀 Dyl Signal Bot is LIVE")

asyncio.run(main())
