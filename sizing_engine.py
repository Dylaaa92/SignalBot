"""
sizing_engine.py — converts a conviction score into concrete position sizing.
Imports SignalContext / StrengthResult from signal_strength.py.
No other existing files are modified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from signal_strength import SignalContext, StrengthResult

MAX_POSITION_NOTIONAL = {
    "BTC":  3000.0,
    "ETH":  1500.0,
    "SOL":  1000.0,
    "HYPE": 750.0,
    "XRP":  750.0,
    "ADA":  750.0,
    "ALGO": 500.0,
    "ZEC":  500.0,
}
DEFAULT_NOTIONAL_CAP = 500.0

LEVERAGE_STEPS = [3, 5, 7, 10, 15, 20]


# =========================================================
# Result dataclass
# =========================================================

@dataclass
class SizingResult:
    size: float                        # final position size (base units)
    leverage: int                      # selected leverage
    tp1: float                         # final TP1 price (may be compressed)
    risk_usd: float                    # adjusted risk in USD
    tp_payout_usd: float               # expected payout at TP1
    sizing_notes: List[str] = field(default_factory=list)


# =========================================================
# Internal helpers
# =========================================================

def _snap_leverage(x: int) -> int:
    return min(LEVERAGE_STEPS, key=lambda v: abs(v - x))


# =========================================================
# Main function
# =========================================================

def calculate_sizing(
    ctx: SignalContext,
    strength: StrengthResult,
    base_risk_usd: float,
    atr: float,
    symbol: str = "",
) -> SizingResult:

    notes: List[str] = []
    notional_cap_usd = MAX_POSITION_NOTIONAL.get(symbol.upper(), DEFAULT_NOTIONAL_CAP)

    # ------------------------------------------------------------------
    # Step 1 — adjusted risk
    # ------------------------------------------------------------------
    adjusted_risk_usd = base_risk_usd * strength.size_mult
    safety_ceiling = base_risk_usd * 3.0
    if adjusted_risk_usd > safety_ceiling:
        notes.append(
            f"adjusted_risk capped at 3× ceiling "
            f"({adjusted_risk_usd:.2f} → {safety_ceiling:.2f})"
        )
        adjusted_risk_usd = safety_ceiling

    # ------------------------------------------------------------------
    # Step 2 — raw size
    # ------------------------------------------------------------------
    risk_per_unit = abs(ctx.entry - ctx.stop)
    if risk_per_unit <= 0:
        raise ValueError(f"entry and stop are equal: {ctx.entry}")

    raw_size = adjusted_risk_usd / risk_per_unit
    notional_cap = notional_cap_usd / ctx.entry

    if raw_size > notional_cap:
        notes.append(
            f"size notional-capped: {raw_size:.6f} → {notional_cap:.6f} "
            f"(${raw_size * ctx.entry:.2f} → ${notional_cap_usd:.2f})"
        )
        raw_size = notional_cap

    # ------------------------------------------------------------------
    # Step 3 — leverage selection
    # ------------------------------------------------------------------
    raw_lev = max(3, round(strength.score * strength.max_leverage))
    raw_lev = min(raw_lev, strength.max_leverage)
    actual_leverage = _snap_leverage(raw_lev)
    notes.append(
        f"leverage: raw={raw_lev} → snapped={actual_leverage}× "
        f"(score={strength.score:.3f} × max_lev={strength.max_leverage})"
    )

    # ------------------------------------------------------------------
    # Step 4 — $20 TP floor
    # ------------------------------------------------------------------
    tp_range = abs(ctx.tp1 - ctx.entry)
    final_size = raw_size
    final_tp1 = ctx.tp1

    # Path A — breakout mode
    if actual_leverage >= 15 and tp_range < 1.0 * atr:
        if raw_size > 0:
            if ctx.side == "LONG":
                compressed_tp = ctx.entry + (20.0 / raw_size)
            else:
                compressed_tp = ctx.entry - (20.0 / raw_size)
        else:
            compressed_tp = ctx.tp1

        notes.append(
            f"BREAKOUT MODE: TP compressed "
            f"{ctx.tp1:.4f} → {compressed_tp:.4f} "
            f"(tp_range {tp_range:.4f} < 1×ATR {atr:.4f}, lev={actual_leverage}×)"
        )
        final_tp1 = compressed_tp

    else:
        # Path B — normal mode
        tp_payout = tp_range * raw_size
        if tp_payout < 20.0:
            required_size = 20.0 / tp_range if tp_range > 0 else raw_size
            scaled_size = max(raw_size, required_size)

            # Re-apply notional cap
            if scaled_size > notional_cap:
                notes.append(
                    f"TP floor scale-up capped by notional: "
                    f"{scaled_size:.6f} → {notional_cap:.6f}"
                )
                scaled_size = notional_cap
            else:
                notes.append(
                    f"size scaled up for $20 TP floor: "
                    f"{raw_size:.6f} → {scaled_size:.6f} "
                    f"(payout {tp_payout:.2f} → {tp_range * scaled_size:.2f})"
                )

            final_size = scaled_size

    # ------------------------------------------------------------------
    # Step 5 — final TP payout + warning
    # ------------------------------------------------------------------
    tp_payout_usd = abs(final_tp1 - ctx.entry) * final_size

    if tp_payout_usd < 20.0:
        notes.append(
            f"WARNING: tp_payout_usd={tp_payout_usd:.2f} still below $20 "
            f"— notional cap prevented full scaling"
        )

    return SizingResult(
        size=round(final_size, 6),
        leverage=actual_leverage,
        tp1=round(final_tp1, 6),
        risk_usd=round(adjusted_risk_usd, 4),
        tp_payout_usd=round(tp_payout_usd, 4),
        sizing_notes=notes,
    )


# =========================================================
# __main__ — three test cases
# =========================================================

if __name__ == "__main__":
    from signal_strength import score_signal

    cases = [
        (
            "SOL LONG — MEDIUM strength, REDUCE/NEAR_MAJOR_STRUCTURE",
            SignalContext(
                side="LONG",
                entry=89.52,
                stop=87.09,
                tp1=91.95,
                veto="REDUCE",
                veto_reason="NEAR_MAJOR_STRUCTURE",
                btc_regime="BULLISH",
                vol="NORMAL",
                tradability="NORMAL",
                concurrent_signals=3,
                adx=28.1,
            ),
            5.0,   # base_risk_usd
            1.8,   # atr
        ),
        (
            "BTC LONG — HIGH strength, clean setup",
            SignalContext(
                side="LONG",
                entry=75_532.0,
                stop=74_412.0,
                tp1=76_651.0,
                veto="ALLOW",
                veto_reason="clean",
                btc_regime="BULLISH",
                vol="NORMAL",
                tradability="NORMAL",
                concurrent_signals=1,
                adx=32.5,
            ),
            5.0,
            800.0,
        ),
        (
            "ETH breakout — HIGH strength, TP < 1×ATR → compression path",
            SignalContext(
                side="LONG",
                entry=3_200.0,
                stop=3_150.0,
                tp1=3_240.0,
                veto="ALLOW",
                veto_reason="clean",
                btc_regime="BULLISH",
                vol="NORMAL",
                tradability="NORMAL",
                concurrent_signals=1,
                adx=38.0,
            ),
            5.0,
            45.0,
        ),
    ]

    # Use manually specified strength values for SOL (as required by spec),
    # and score_signal() for BTC/ETH — override for ETH to force score=0.85.
    from dataclasses import replace

    for i, (label, ctx, base_risk, atr) in enumerate(cases):
        if i == 0:
            strength = StrengthResult(
                score=0.87,
                tier="MEDIUM",
                size_mult=1.98,
                max_leverage=5,
                breakdown={},
                notes=[],
            )
        elif i == 1:
            strength = StrengthResult(
                score=0.912,
                tier="HIGH",
                size_mult=2.15,
                max_leverage=20,
                breakdown={},
                notes=[],
            )
        else:
            strength = StrengthResult(
                score=0.85,
                tier="HIGH",
                size_mult=2.0,
                max_leverage=20,
                breakdown={},
                notes=[],
            )

        result = calculate_sizing(ctx, strength, base_risk, atr, symbol=["SOL", "BTC", "ETH"][i])

        print(f"\n{'='*62}")
        print(f"  {label}")
        print(f"{'='*62}")
        print(f"  size          : {result.size} units")
        print(f"  leverage      : {result.leverage}×")
        print(f"  tp1           : {result.tp1}")
        print(f"  risk_usd      : ${result.risk_usd:.4f}")
        print(f"  tp_payout_usd : ${result.tp_payout_usd:.4f}")
        if result.sizing_notes:
            print(f"  notes:")
            for n in result.sizing_notes:
                print(f"    • {n}")
    print()
