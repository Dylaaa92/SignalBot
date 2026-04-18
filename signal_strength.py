"""
signal_strength.py — derives a 0.0–1.0 conviction score from existing signal data.
No new data sources required; all inputs come from the existing signal/veto pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# =========================================================
# Data structures
# =========================================================

@dataclass
class SignalContext:
    side: str                          # "LONG" | "SHORT"
    entry: float
    stop: float
    tp1: float
    veto: str                          # "ALLOW" | "REDUCE" | "BLOCK"
    veto_reason: str                   # e.g. "NEAR_MAJOR_STRUCTURE", "clean", ""
    btc_regime: str                    # "BULLISH" | "BEARISH" | "NEUTRAL"
    vol: str                           # "NORMAL" | "HIGH" | "LOW" | "EXTREME"
    tradability: str                   # "NORMAL" | "CAUTION" | "BLOCKED"
    concurrent_signals: int = 1        # how many symbols signalling at once
    adx: Optional[float] = None


@dataclass
class StrengthResult:
    score: float                       # 0.0–1.0
    tier: str                          # "HIGH" | "MEDIUM" | "LOW"
    size_mult: float                   # position size multiplier
    max_leverage: int                  # leverage cap for this signal
    breakdown: dict                    # per-component scores
    notes: list = field(default_factory=list)


# =========================================================
# Component scorers
# =========================================================

def _rr_score(entry: float, stop: float, tp1: float, side: str) -> float:
    risk = abs(entry - stop)
    reward = abs(tp1 - entry)
    if risk <= 0:
        return 0.1
    rr = reward / risk
    if rr >= 3.0:
        return 1.0
    if rr >= 2.0:
        return 0.75
    if rr >= 1.5:
        return 0.55
    if rr >= 1.0:
        return 0.35
    return 0.1


def _veto_score(veto: str, veto_reason: str) -> float:
    if veto == "BLOCK":
        return 0.0
    if veto == "REDUCE":
        reason_lower = veto_reason.lower()
        if "major" in reason_lower:
            return 0.35
        return 0.35
    # ALLOW
    reason_lower = veto_reason.lower()
    if "structure" in reason_lower or "near" in reason_lower:
        return 0.65
    return 1.0


def _regime_score(side: str, btc_regime: str) -> float:
    regime = btc_regime.upper()
    if regime == "NEUTRAL" or regime == "UNKNOWN":
        return 0.6
    aligned = (
        (side == "LONG" and regime == "BULLISH") or
        (side == "SHORT" and regime == "BEARISH")
    )
    if aligned:
        return 1.0
    return 0.2


def _vol_score(vol: str) -> float:
    v = vol.upper()
    if v == "NORMAL":
        return 1.0
    if v == "LOW":
        return 0.7
    if v == "HIGH":
        return 0.4
    # EXTREME
    return 0.1


def _tradability_score(tradability: str) -> float:
    t = tradability.upper()
    if t == "NORMAL":
        return 1.0
    if t == "CAUTION":
        return 0.5
    return 0.0  # BLOCKED


def _confluence_boost(concurrent_signals: int) -> float:
    if concurrent_signals >= 4:
        return 0.18
    if concurrent_signals == 3:
        return 0.12
    if concurrent_signals == 2:
        return 0.05
    return 0.0


# =========================================================
# Size multiplier (linear interpolation within tier bands)
# =========================================================

def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y0
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


def _size_mult(score: float, tier: str) -> float:
    if tier == "HIGH":
        # score 0.75–1.0 → 1.5×–2.5×
        return _lerp(score, 0.75, 1.0, 1.5, 2.5)
    if tier == "MEDIUM":
        # score 0.45–0.75 → 1.0×–1.5×
        return _lerp(score, 0.45, 0.75, 1.0, 1.5)
    # LOW: score 0.0–0.45 → 0.5×–1.0×
    return _lerp(score, 0.0, 0.45, 0.5, 1.0)


# =========================================================
# Max leverage cap
# =========================================================

def _max_leverage(score: float, tier: str, veto_reason: str, vol: str) -> int:
    reason_lower = veto_reason.lower()
    vol_upper = vol.upper()

    # Structure proximity hard cap
    if "major" in reason_lower or "structure" in reason_lower:
        return 5

    # High/extreme vol cap
    if vol_upper in ("HIGH", "EXTREME"):
        return 10

    # Tier-based caps
    if tier == "LOW":
        return 5
    if tier == "MEDIUM":
        return 10
    # HIGH tier
    if score >= 0.85:
        return 20
    if score >= 0.75:
        return 15
    return 10


# =========================================================
# Weights
# =========================================================

WEIGHTS = {
    "rr":          0.35,
    "veto":        0.25,
    "regime":      0.20,
    "vol":         0.10,
    "tradability": 0.10,
}


# =========================================================
# Main function
# =========================================================

def score_signal(ctx: SignalContext) -> StrengthResult:
    rr     = _rr_score(ctx.entry, ctx.stop, ctx.tp1, ctx.side)
    veto   = _veto_score(ctx.veto, ctx.veto_reason)
    regime = _regime_score(ctx.side, ctx.btc_regime)
    vol    = _vol_score(ctx.vol)
    trad   = _tradability_score(ctx.tradability)

    weighted = (
        rr     * WEIGHTS["rr"] +
        veto   * WEIGHTS["veto"] +
        regime * WEIGHTS["regime"] +
        vol    * WEIGHTS["vol"] +
        trad   * WEIGHTS["tradability"]
    )

    boost = _confluence_boost(ctx.concurrent_signals)
    score = min(1.0, weighted + boost)

    if score >= 0.75:
        tier = "HIGH"
    elif score >= 0.45:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    notes = []
    if ctx.veto == "BLOCK":
        notes.append("BLOCK veto — signal should not be traded")
    if ctx.tradability == "BLOCKED":
        notes.append("Tradability BLOCKED — market conditions unfavourable")
    if ctx.vol.upper() == "EXTREME":
        notes.append("Extreme volatility — consider skipping entirely")
    if rr == 0.1:
        notes.append("R:R < 1.0 — poor setup quality")
    if boost > 0:
        notes.append(f"Confluence boost +{boost:.2f} ({ctx.concurrent_signals} concurrent signals)")
    if ctx.adx is not None and ctx.adx < 20:
        notes.append(f"ADX={ctx.adx:.1f} — weak trend, lower confidence in directional follow-through")

    breakdown = {
        "rr":          round(rr, 3),
        "veto":        round(veto, 3),
        "regime":      round(regime, 3),
        "vol":         round(vol, 3),
        "tradability": round(trad, 3),
        "weighted":    round(weighted, 3),
        "confluence_boost": round(boost, 3),
        "final_score": round(score, 3),
    }

    sm = round(_size_mult(score, tier), 3)
    ml = _max_leverage(score, tier, ctx.veto_reason, ctx.vol)

    return StrengthResult(
        score=round(score, 3),
        tier=tier,
        size_mult=sm,
        max_leverage=ml,
        breakdown=breakdown,
        notes=notes,
    )


# =========================================================
# __main__ — three test examples
# =========================================================

if __name__ == "__main__":
    import json

    examples = [
        (
            "BTC LONG — clean setup, high conviction",
            SignalContext(
                side="LONG",
                entry=83_000.0,
                stop=81_500.0,
                tp1=87_000.0,
                veto="ALLOW",
                veto_reason="clean",
                btc_regime="BULLISH",
                vol="NORMAL",
                tradability="NORMAL",
                concurrent_signals=1,
                adx=32.5,
            ),
        ),
        (
            "SOL LONG — REDUCE veto near major structure, 3 concurrent signals",
            SignalContext(
                side="LONG",
                entry=135.0,
                stop=130.0,
                tp1=148.0,
                veto="REDUCE",
                veto_reason="NEAR_MAJOR_STRUCTURE",
                btc_regime="BULLISH",
                vol="NORMAL",
                tradability="NORMAL",
                concurrent_signals=3,
                adx=28.1,
            ),
        ),
        (
            "ETH SHORT — counter-trend, high vol, low R:R",
            SignalContext(
                side="SHORT",
                entry=1_600.0,
                stop=1_650.0,
                tp1=1_560.0,
                veto="ALLOW",
                veto_reason="",
                btc_regime="BULLISH",
                vol="HIGH",
                tradability="CAUTION",
                concurrent_signals=1,
                adx=19.0,
            ),
        ),
    ]

    for label, ctx in examples:
        result = score_signal(ctx)
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")
        print(f"  score      : {result.score}  ({result.tier})")
        print(f"  size_mult  : {result.size_mult}×")
        print(f"  max_lev    : {result.max_leverage}×")
        print(f"  breakdown  : {json.dumps(result.breakdown, indent=4)}")
        if result.notes:
            print(f"  notes      :")
            for n in result.notes:
                print(f"    • {n}")
    print()
