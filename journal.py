from __future__ import annotations

import csv
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Dict


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_row(path: str, fieldnames: list[str], row: Dict[str, Any]) -> None:
    _ensure_dir(os.path.dirname(path) or ".")
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        safe = {k: row.get(k, "") for k in fieldnames}
        w.writerow(safe)


def _stringify_list(v: Any) -> Any:
    if isinstance(v, list):
        return "|".join(str(x) for x in v)
    return v


def flatten_for_csv(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _stringify_list(v) for k, v in d.items()}


class Journal:
    def __init__(self, base_dir: str = "data", session_id: str = "default"):
        self.base_dir = base_dir
        self.session_id = session_id

    def write_snapshot(self, snap: Dict[str, Any]) -> None:
        path = os.path.join(self.base_dir, "snapshots.csv")
        fieldnames = [
            "ts_utc",
            "symbol",
            "tf",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "ema9",
            "ema21",
            "vwap",
            "poc",
            "rsi",
            "session_id",
        ]
        row = dict(snap)
        row.setdefault("ts_utc", utc_now_iso())
        row.setdefault("session_id", self.session_id)
        _write_row(path, fieldnames, row)

    def write_decision(self, report: Any) -> None:
        path = os.path.join(self.base_dir, "decisions.csv")
        if is_dataclass(report):
            d = asdict(report)
        else:
            d = dict(report)

        d = flatten_for_csv(d)
        d.setdefault("ts_utc", utc_now_iso())
        d.setdefault("session_id", self.session_id)

        base_order = [
            "ts_utc",
            "symbol",
            "mode",
            "strategy",
            "data_fresh_ms",
            "px_last",
            "spread_bps",
            "vwap_5m",
            "poc_5m",
            "vwap_1h",
            "poc_1h",
            "ema9_1h",
            "ema21_1h",
            "bias_1h",
            "bias_reason",
            "rsi_1h",
            "bos_dir",
            "bos_level",
            "retest_level",
            "retest_state",
            "acceptance_bars",
            "acceptance_required",
            "acceptance_state",
            "vol_5m",
            "vol_ma_20",
            "vol_state",
            "vol_reason",
            "entry_plan",
            "entry_px",
            "invalidation_px",
            "stop_px",
            "tp1_px",
            "runner_trail",
            "rr_to_tp1",
            "gates_required",
            "gates_passed",
            "gates_failed",
            "action",
            "confidence",
            "notes",
            "session_id",
        ]
        extras = [k for k in d.keys() if k not in base_order]
        fieldnames = base_order + sorted(extras)
        _write_row(path, fieldnames, d)

    def write_trade(self, trade: Dict[str, Any]) -> None:
        path = os.path.join(self.base_dir, "trades.csv")
        fieldnames = [
            "ts_utc",
            "symbol",
            "side",
            "qty",
            "entry_px",
            "stop_px",
            "tp1_px",
            "exit_px",
            "pnl_usd",
            "pnl_r",
            "reason",
            "order_id",
            "fill_id",
            "mode",
            "session_id",
        ]
        row = dict(trade)
        row.setdefault("ts_utc", utc_now_iso())
        row.setdefault("session_id", self.session_id)
        _write_row(path, fieldnames, row)
