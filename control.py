import os
import re
import asyncio
from typing import Tuple, Optional, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------
# Config
# ---------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Only allow *you* to control services
# Set this to your personal chat id (e.g. 7664213318)
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Which symbols you want buttons for (edit anytime)
# Example: BTC,ETH,SOL,xyz:GOLD,xyz:COIN
SYMBOLS_ENV = os.getenv("CONTROL_SYMBOLS", "BTC,ETH,SOL").strip()
SYMBOLS = [s.strip() for s in SYMBOLS_ENV.split(",") if s.strip()]

SERVICE_PREFIX = os.getenv("CONTROL_SERVICE_PREFIX", "signalbot@").strip()

# Safety: allow only symbols matching these patterns.
# Includes normal coins and Hyperliquid "xyz:COIN" format.
SAFE_SYMBOL_RE = re.compile(r"^[A-Za-z0-9:_\-]+$")

# ---------------------------
# Helpers
# ---------------------------
def is_authorized(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True  # if you forgot to set it, don't block (but you should set it)
    cid = update.effective_chat.id if update.effective_chat else None
    return str(cid) == str(ALLOWED_CHAT_ID)

def svc(symbol: str) -> str:
    return f"{SERVICE_PREFIX}{symbol}"

async def run_cmd(*args: str, timeout: int = 15) -> Tuple[int, str, str]:
    """
    Run a command asynchronously and capture output.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    return proc.returncode, (out or b"").decode(errors="ignore"), (err or b"").decode(errors="ignore")

async def systemctl(action: str, symbol: str) -> Tuple[bool, str]:
    """
    Execute: sudo systemctl <action> signalbot@SYMBOL
    """
    if not SAFE_SYMBOL_RE.match(symbol):
        return False, "Blocked: symbol contains invalid characters."

    service = svc(symbol)

    # For actions like start/stop/restart/status/is-active
    if action in {"start", "stop", "restart"}:
        code, out, err = await run_cmd("sudo", "systemctl", action, service)
        ok = (code == 0)
        msg = (out.strip() or err.strip() or f"{action} returned code {code}")
        return ok, msg

    if action == "status":
        # status can be noisy; use is-active + show main pid as clean status
        code_a, out_a, err_a = await run_cmd("systemctl", "is-active", service)
        state = (out_a.strip() or err_a.strip() or "unknown")
        code_p, out_p, err_p = await run_cmd("systemctl", "show", service, "-p", "MainPID")
        pid_line = (out_p.strip() or err_p.strip() or "")
        return True, f"{service}: {state}\n{pid_line}"

    if action == "logs":
        code, out, err = await run_cmd("journalctl", "-u", service, "-n", "60", "--no-pager")
        ok = (code == 0)
        text = out.strip() or err.strip() or f"journalctl returned code {code}"
        return ok, text

    return False, f"Unknown action: {action}"

def symbols_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for i, sym in enumerate(SYMBOLS, start=1):
        row.append(InlineKeyboardButton(sym, callback_data=f"sym|{sym}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def actions_keyboard(symbol: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Start", callback_data=f"act|start|{symbol}"),
            InlineKeyboardButton("Stop", callback_data=f"act|stop|{symbol}"),
        ],
        [
            InlineKeyboardButton("Restart", callback_data=f"act|restart|{symbol}"),
            InlineKeyboardButton("Status", callback_data=f"act|status|{symbol}"),
        ],
        [
            InlineKeyboardButton("Logs", callback_data=f"act|logs|{symbol}"),
            InlineKeyboardButton("Back", callback_data="back"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


# ---------------------------
# Handlers
# ---------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Select a symbol to control:",
        reply_markup=symbols_keyboard(),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Commands:\n"
        "/start - choose a symbol\n"
        "/help - this message\n\n"
        f"Allowed chat id: {ALLOWED_CHAT_ID or '(not set)'}\n"
        f"Symbols: {', '.join(SYMBOLS) or '(none)'}"
    )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return

    q = update.callback_query
    await q.answer()

    data = q.data or ""

    if data == "back":
        await q.edit_message_text("Select a symbol to control:", reply_markup=symbols_keyboard())
        return

    if data.startswith("sym|"):
        _, sym = data.split("|", 1)
        await q.edit_message_text(
            f"Control: {sym}\nService: {svc(sym)}",
            reply_markup=actions_keyboard(sym),
        )
        return

    if data.startswith("act|"):
        _, action, sym = data.split("|", 2)

        # quick ack so you feel it responded instantly
        await q.edit_message_text(f"Running `{action}` on `{svc(sym)}` …")

        ok, msg = await systemctl(action, sym)

        # Keep output readable
        if action == "logs" and len(msg) > 3500:
            msg = msg[-3500:]  # last chunk

        prefix = "✅" if ok else "❌"
        text = f"{prefix} {action.upper()} {svc(sym)}\n\n{msg}"

        await q.edit_message_text(text, reply_markup=actions_keyboard(sym))
        return

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        return
    await update.message.reply_text("Unknown command. Use /start")

# ---------------------------
# Main
# ---------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN env var.")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
