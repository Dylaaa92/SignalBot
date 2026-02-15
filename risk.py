# risk.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def size_from_risk(risk_usdt: float, entry: float, stop: float) -> float:
    """
    Position size (in units of the coin) such that:
      (entry - stop) * size ~= risk_usdt
    Assumes LONG. Caller should ensure entry > stop.
    """
    dist = entry - stop
    if risk_usdt <= 0 or dist <= 0:
        return 0.0
    return float(risk_usdt / dist)


@dataclass
class RiskState:
    """
    Tracks session/day risk constraints.

    - daily_pnl resets when the UTC date changes
    - consec_losses increments on losing trades, resets on win
    - cooldown_until is set when:
        * MAX_CONSEC_LOSSES hit
        * you call register_trade_result with a cooldown_seconds argument
    """
    daily_pnl: float = 0.0
    consec_losses: int = 0
    cooldown_until: float = 0.0  # epoch seconds

    _day_key: str | None = None  # "YYYY-MM-DD" in UTC

    def _current_day_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def reset_if_new_day(self) -> None:
        day = self._current_day_key()
        if self._day_key is None:
            self._day_key = day
            return
        if day != self._day_key:
            # New UTC day: reset daily stats
            self._day_key = day
            self.daily_pnl = 0.0
            self.consec_losses = 0
            # NOTE: We intentionally do NOT clear cooldown_until.
            # If you're in cooldown across midnight, it remains in effect.

    def in_cooldown(self, now_ts: float | None = None) -> bool:
        self.reset_if_new_day()
        if now_ts is None:
            now_ts = datetime.now(timezone.utc).timestamp()
        return now_ts < self.cooldown_until

    def register_trade_result(
        self,
        pnl: float,
        cooldown_seconds: int,
        max_consec_losses: int,
        now_ts: float | None = None,
    ) -> None:
        """
        Update PnL + apply risk circuit breakers after a trade closes.
        pnl is NET pnl (after fees) if you have it.
        """
        self.reset_if_new_day()

        if now_ts is None:
            now_ts = datetime.now(timezone.utc).timestamp()

        self.daily_pnl += float(pnl)

        if pnl < 0:
            self.consec_losses += 1
        else:
            self.consec_losses = 0

        # Trigger cooldown if too many consecutive losses
        if self.consec_losses >= int(max_consec_losses):
            self.cooldown_until = now_ts + int(cooldown_seconds)
