import asyncio
import httpx

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from logger import log


API = "https://api.telegram.org/bot{}/{}"


def _chat_ok(msg: dict) -> bool:
    """
    Restrict commands to your configured chat.
    """
    try:
        return str(msg["chat"]["id"]) == str(TELEGRAM_CHAT_ID)
    except Exception:
        return False


async def telegram_poll_commands(on_command, poll_error_sleep_s: int = 2):
    """
    Long-poll Telegram updates and call on_command(text) for slash commands.

    on_command: async function that accepts a string command, e.g. "/grid_status BTC"
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log({"event": "telegram_control_disabled"})
        return

    offset = None
    timeout = 30  # long-poll duration

    async with httpx.AsyncClient(timeout=timeout + 10) as client:
        log({"event": "telegram_control_started"})

        while True:
            try:
                params = {"timeout": timeout}
                if offset is not None:
                    params["offset"] = offset

                url = API.format(TELEGRAM_BOT_TOKEN, "getUpdates")
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json()

                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1

                    msg = upd.get("message") or upd.get("edited_message")
                    if not msg:
                        continue
                    if not _chat_ok(msg):
                        continue

                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue

                    await on_command(text)

            except Exception as e:
                log({"event": "telegram_control_error", "error": str(e)})
                await asyncio.sleep(poll_error_sleep_s)
