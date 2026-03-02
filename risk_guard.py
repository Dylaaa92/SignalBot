# risk_guard.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class RiskGuard:
    daily_dd_limit_pct: float = 0.01  # 1%
    day: object = None
    equity_start: float | None = None
    daily_stop: bool = False

    def maybe_reset_day(self, equity_now: float) -> None:
        today = datetime.now(timezone.utc).date()
        if self.day != today:
            self.day = today
            self.equity_start = equity_now
            self.daily_stop = False

    def update_and_check(self, equity_now: float) -> bool:
        """
        True = entries allowed
        False = daily stop triggered (no NEW entries)
        """
        self.maybe_reset_day(equity_now)

        if self.equity_start is None:
            self.equity_start = equity_now

        # Protect against divide-by-zero or weird API values
        if self.equity_start <= 0:
            return True

        dd = (self.equity_start - equity_now) / self.equity_start
        if dd >= self.daily_dd_limit_pct:
            self.daily_stop = True

        return not self.daily_stop
