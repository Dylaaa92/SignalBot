import httpx
import asyncio

async def main():
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "meta"}

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()

    coins = data.get("universe", [])
    print("AVAILABLE COINS:")
    for c in coins:
        print(c["name"])

asyncio.run(main())
