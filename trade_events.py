# trade_events.py
import os
import json
import uuid
from datetime import datetime, timezone

_TRADES_DIR = os.getenv("TRADES_DIR", "trades")

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _day_key_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def ensure_trades_dir():
    os.makedirs(_TRADES_DIR, exist_ok=True)

def new_trade_id() -> str:
    # short, readable, unique enough
    return uuid.uuid4().hex[:12]

def trade_file_path(symbol: str) -> str:
    ensure_trades_dir()
    return os.path.join(_TRADES_DIR, f"{symbol}-{_day_key_utc()}.jsonl")

def append_event(symbol: str, event: dict):
    """
    Append one JSON object per line.
    Safe for a single-process per symbol systemd setup.
    """
    event = dict(event)  # copy
    event.setdefault("ts_utc", _utc_now_iso())
    event.setdefault("symbol", symbol)

    path = trade_file_path(symbol)
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)

    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
