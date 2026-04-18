import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional, Literal, Dict, Any, Tuple

from logger import log
from hl_trade import HL
from signal_strength import score_signal, SignalContext
from sizing_engine import calculate_sizing
import collateral_manager as cm

Side = Literal["LONG", "SHORT"]


@dataclass
class PaperPosition:
    side: Side
    size: float
    entry_px: float
    entry_time: int
    trade_id: str
    tp1_done: bool = False
    realized_pnl: float = 0.0


class Executor:
    """
    Execution layer supporting:
      - paper mode (simulated fills + PnL)
      - live mode (Hyperliquid orders)

    Enable live mode with:
        export TRADING_MODE=live
        export LIVE_GUARD=I_UNDERSTAND

    Safety:
      - free collateral / margin guard before entry
      - live has_position() checks exchange state (best effort, wrapper-dependent)
    """

    def __init__(self, env: str, symbol: str):
        self.env = env
        self.symbol = symbol
        self.mode = os.getenv("TRADING_MODE", "paper").lower()

        # Live safety guard
        if self.mode == "live" and os.getenv("LIVE_GUARD", "") != "I_UNDERSTAND":
            raise RuntimeError(
                "LIVE_GUARD not set. "
                "Export LIVE_GUARD=I_UNDERSTAND to enable live trading."
            )

        self.slippage_bps = float(os.getenv("SLIP_BPS", "5"))  # pseudo-market slippage model
        self.leverage = float(os.getenv("LEVERAGE", "5"))
        self.risk_usd = float(os.getenv("RISK_USDT_PER_TRADE", "5.0"))

        self.paper_position: Optional[PaperPosition] = None
        self.hl: Optional[HL] = None

        if self.mode == "live":
            self.hl = HL(env)
            # best-effort leverage set (only if wrapper supports it)
            asyncio.get_event_loop().create_task(self._maybe_set_leverage())

    # =========================================================
    # Internal helpers
    # =========================================================

    async def _maybe_set_leverage(self):
        """
        Best-effort set leverage on exchange, if wrapper supports it.
        If not supported, no worries (Hyperliquid uses cross margin, leverage per coin may still apply).
        """
        if not self.hl:
            return
        try:
            if hasattr(self.hl, "set_leverage"):
                await asyncio.to_thread(self.hl.set_leverage, self.symbol, self.leverage)
                log({"event": "leverage_set", "symbol": self.symbol, "leverage": self.leverage})
            else:
                log({"event": "leverage_set_skip", "reason": "hl_no_set_leverage", "symbol": self.symbol, "leverage": self.leverage})
        except Exception as e:
            log({"event": "leverage_set_error", "symbol": self.symbol, "error": str(e)})

    def _apply_slippage(self, price: float, side: Side, is_entry: bool) -> float:
        """
        Pseudo-market pricing.
        Entries stay tighter.
        Exits are more aggressive to prioritize getting flat.
        """
        entry_bps = float(os.getenv("SLIP_BPS", "5"))
        exit_bps = float(os.getenv("EXIT_SLIP_BPS", "20"))  # more aggressive for stops / TP exits

        use_bps = entry_bps if is_entry else exit_bps
        slip = price * (use_bps / 10000.0)

        if is_entry:
            return price + slip if side == "LONG" else price - slip
        else:
            return price - slip if side == "LONG" else price + slip

    async def _get_mid(self) -> Optional[float]:
        if not self.hl:
            return None
        try:
            return await asyncio.to_thread(self.hl.mid, self.symbol)
        except Exception as e:
            log({"event": "mid_error", "symbol": self.symbol, "error": str(e)})
            return None

    async def _get_free_collateral_usd(self) -> Optional[float]:
        """
        Best-effort fetch of free collateral.
        Supports multiple wrapper shapes:
          - hl.free_collateral_usd() function
          - hl.get_free_collateral_usd() function
          - hl.account_state()/user_state() and parsing (not implemented w/out wrapper)
        """
        if not self.hl:
            return None

        try:
            if hasattr(self.hl, "free_collateral_usd"):
                val = await asyncio.to_thread(self.hl.free_collateral_usd)
                return float(val)
            if hasattr(self.hl, "get_free_collateral_usd"):
                val = await asyncio.to_thread(self.hl.get_free_collateral_usd)
                return float(val)
        except Exception as e:
            log({"event": "free_collateral_error", "symbol": self.symbol, "error": str(e)})
            return None

        # If your wrapper doesn't expose it yet, we can’t safely margin-guard.
        # We will log and return None; caller will decide policy.
        log({"event": "free_collateral_unavailable", "symbol": self.symbol})
        return None
    def account_value_usd(self) -> float:
        """
        Best-effort account value for sizing.
        Hyperliquid user_state shape can vary; handle common keys.
        """
        if self.hl is None:
            return 0.0

        s = self.hl.user_state() or {}

        for path in [
            ("marginSummary", "accountValue"),
            ("crossMarginSummary", "accountValue"),
            ("marginSummary", "totalEquity"),
            ("crossMarginSummary", "totalEquity"),
        ]:
            try:
                cur = s
                for k in path:
                    cur = cur[k]
                return float(cur)
            except Exception:
                pass

        for k in ("accountValue", "totalEquity"):
            try:
                return float(s[k])
            except Exception:
                pass

        return 0.0

    async def _get_live_position_size(self) -> Optional[float]:
        """
        Best-effort fetch current live position size for this symbol.
        Returns:
          - float size (positive long, negative short), or 0.0 if flat
          - None if wrapper does not support position querying yet
        """
        if not self.hl:
            return None

        # Try a few common wrapper method names
        candidates = [
            "position_size",          # (symbol) -> float
            "get_position_size",      # (symbol) -> float
            "pos_size",               # (symbol) -> float
            "open_position_size",     # (symbol) -> float
        ]

        for name in candidates:
            if hasattr(self.hl, name):
                fn = getattr(self.hl, name)
                try:
                    val = await asyncio.to_thread(fn, self.symbol)
                    return float(val)
                except Exception as e:
                    log({"event": "position_size_error", "symbol": self.symbol, "method": name, "error": str(e)})
                    return None

        # Try generic "positions()" style
        if hasattr(self.hl, "positions"):
            try:
                pos = await asyncio.to_thread(self.hl.positions)
                # Expect list/dict. We'll attempt best-effort parse.
                # If it’s not parseable, we’ll return None and improve once we see hl_trade.py.
                if isinstance(pos, dict):
                    # maybe { "ETH": {"size": ...}, ... }
                    if self.symbol in pos and isinstance(pos[self.symbol], dict) and "size" in pos[self.symbol]:
                        return float(pos[self.symbol]["size"])
                if isinstance(pos, list):
                    # maybe [ {"coin":"ETH","szi":"0.1"}, ... ]
                    for p in pos:
                        if isinstance(p, dict) and (p.get("coin") == self.symbol or p.get("symbol") == self.symbol):
                            if "size" in p:
                                return float(p["size"])
                            if "szi" in p:
                                return float(p["szi"])
                return None
            except Exception as e:
                log({"event": "positions_error", "symbol": self.symbol, "error": str(e)})
                return None

        log({"event": "position_query_unavailable", "symbol": self.symbol})
        return None

    async def _margin_guard_ok(self, side: Side, size: float) -> Tuple[bool, str]:
        """
        Prevent new entries if free collateral is too low (protects manual trading on other markets).
        Returns (ok, reason).
        """
        if self.mode != "live":
            return True, "not_live"

        mid = await self._get_mid()
        if mid is None:
            return False, "no_mid"

        free = await self._get_free_collateral_usd()
        if free is None or free == 0.0:
            # Agent wallet setup — cannot read main account balance.
            # Let Hyperliquid make the collateral decision at order time.
            log({
                "event": "free_collateral_check_skipped",
                "symbol": self.symbol,
                "reason": "agent_wallet_setup",
                "note": "Hyperliquid will enforce margin at order time",
            })
            return True, "ok"

        intended_notional = float(mid) * float(size)
        required_margin = intended_notional / float(self.leverage)

        # Add headroom to avoid flapping when you place manual trades
        headroom_mult = float(os.getenv("MARGIN_HEADROOM_MULT", "1.2"))
        if free is not None and free > 0.0 and free < required_margin * headroom_mult:
            log({
                "event": "SKIP_ENTRY",
                "symbol": self.symbol,
                "reason": "INSUFFICIENT_FREE_COLLATERAL",
                "free": free,
                "required_margin": required_margin,
                "intended_notional": intended_notional,
                "leverage": self.leverage,
                "headroom_mult": headroom_mult,
                "side": side,
                "size": size,
            })
            return False, "insufficient_free_collateral"

        return True, "ok"

    # =========================================================
    # Public: position status
    # =========================================================

    async def has_position(self) -> bool:
        """
        True if currently holding a position (paper or live).
        In live, this is best-effort and depends on hl_trade wrapper support.
        """
        if self.mode == "paper":
            return self.paper_position is not None

        size = await self._get_live_position_size()
        if size is None:
            # Policy: if we can't query, assume "unknown" = treat as has position to avoid overtrading.
            log({"event": "has_position_unknown_assume_true", "symbol": self.symbol})
            return True

        return abs(size) > float(os.getenv("POS_EPS", "1e-8"))

    async def live_position_snapshot(self) -> Dict[str, Any]:
        """
        Best-effort live position snapshot for this symbol.

        Returns:
          {
            "ok": True/False,
            "in_position": bool,
            "side": "LONG" | "SHORT" | None,
            "size": float,
            "error": str | None,
          }
        """
        if self.mode != "live":
            return {
                "ok": True,
                "in_position": self.paper_position is not None,
                "side": self.paper_position.side if self.paper_position else None,
                "size": abs(self.paper_position.size) if self.paper_position else 0.0,
                "error": None,
            }

        try:
            size = await self._get_live_position_size()
            if size is None:
                return {
                    "ok": False,
                    "in_position": False,
                    "side": None,
                    "size": 0.0,
                    "error": "position_query_unavailable",
                }

            if abs(size) <= float(os.getenv("POS_EPS", "1e-8")):
                return {
                    "ok": True,
                    "in_position": False,
                    "side": None,
                    "size": 0.0,
                    "error": None,
                }

            return {
                "ok": True,
                "in_position": True,
                "side": "LONG" if size > 0 else "SHORT",
                "size": abs(float(size)),
                "error": None,
            }
        except Exception as e:
            log({
                "event": "live_position_snapshot_error",
                "symbol": self.symbol,
                "error": str(e),
            })
            return {
                "ok": False,
                "in_position": False,
                "side": None,
                "size": 0.0,
                "error": str(e),
            }

    # =========================================================
    # Paper Trading
    # =========================================================

    async def paper_open(self, side: Side, size: float, entry_px: float, trade_id: str):
        if self.paper_position is not None:
            return {"ok": False, "error": "paper_position_exists"}

        self.paper_position = PaperPosition(
            side=side,
            size=size,
            entry_px=entry_px,
            entry_time=int(time.time()),
            trade_id=trade_id,
        )

        log({
            "event": "paper_open",
            "symbol": self.symbol,
            "side": side,
            "size": size,
            "entry": entry_px,
            "trade_id": trade_id,
        })

        return {"ok": True}

    async def paper_tp1(self, tp_price: float, qty_pct: float):
        p = self.paper_position
        if p is None or p.tp1_done:
            return {"ok": False}

        qty_to_close = abs(p.size) * (qty_pct / 100.0)

        if p.side == "LONG":
            pnl = (tp_price - p.entry_px) * qty_to_close
        else:
            pnl = (p.entry_px - tp_price) * qty_to_close

        p.realized_pnl += pnl
        p.tp1_done = True

        remaining = abs(p.size) - qty_to_close
        p.size = remaining if p.side == "LONG" else -remaining

        log({
            "event": "paper_tp1",
            "symbol": self.symbol,
            "tp_price": tp_price,
            "pnl": pnl,
            "remaining_size": p.size,
        })

        if abs(p.size) < 1e-10:
            self.paper_position = None

        return {"ok": True, "pnl": pnl}

    async def paper_close(self, exit_price: float, reason: str):
        p = self.paper_position
        if p is None:
            return {"ok": False}

        qty = abs(p.size)

        if p.side == "LONG":
            pnl = (exit_price - p.entry_px) * qty
        else:
            pnl = (p.entry_px - exit_price) * qty

        total_pnl = p.realized_pnl + pnl

        log({
            "event": "paper_close",
            "symbol": self.symbol,
            "exit_price": exit_price,
            "reason": reason,
            "trade_pnl": pnl,
            "total_pnl": total_pnl,
        })

        self.paper_position = None

        return {"ok": True, "total_pnl": total_pnl}

    # =========================================================
    # Live Trading (market-like via limit + slippage)
    # =========================================================

    async def live_open_marketlike(self, side: Side, size: float, _leverage_override: Optional[int] = None):
        """
        Sends a non-post-only limit order at a small slippage away from mid.
        Includes:
          - margin guard (free collateral)
          - optional "already has position" guard (best effort)
        """
        if self.hl is None:
            return {"ok": False, "error": "hl_not_initialized"}

        # Guard: don't stack positions if we can detect a position exists
        already = await self.has_position()
        if already:
            log({"event": "SKIP_ENTRY", "symbol": self.symbol, "reason": "POSITION_ALREADY_EXISTS", "side": side, "size": size})
            return {"ok": False, "error": "position_exists"}

        ok, reason = await self._margin_guard_ok(side, size)
        if not ok:
            return {"ok": False, "error": reason}

        is_buy = side == "LONG"

        # Pre-check rounded size before sending order
        normalized_size = self.hl.normalize_entry_size_for_coin(self.symbol, size)
        if normalized_size <= 0:
            log({
                "event": "SKIP_ENTRY",
                "symbol": self.symbol,
                "reason": "INVALID_SIZE_AFTER_ROUNDING",
                "side": side,
                "raw_size": size,
                "normalized_size": normalized_size,
            })
            return {"ok": False, "error": "invalid_size_after_rounding"}

        # Explicitly set leverage before every live order
        _effective_leverage = _leverage_override if _leverage_override is not None else int(self.leverage)
        try:
            if hasattr(self.hl, "set_leverage"):
                await asyncio.to_thread(
                    self.hl.set_leverage,
                    self.symbol,
                    _effective_leverage
                )
                log({
                    "event": "leverage_set_pre_order",
                    "symbol": self.symbol,
                    "leverage": _effective_leverage,
                })
            else:
                log({
                    "event": "leverage_set_skip_pre_order",
                    "symbol": self.symbol,
                    "reason": "method_not_found",
                })
        except Exception as _le:
            log({
                "event": "leverage_set_pre_order_error",
                "symbol": self.symbol,
                "error": str(_le),
            })
            # Do not block the order — continue even if leverage set fails

        def _call():
            mid = self.hl.mid(self.symbol)
            px = self._apply_slippage(mid, side, is_entry=True)
            return self.hl.place_limit(
                coin=self.symbol,
                is_buy=is_buy,
                px=px,
                sz=normalized_size,
                reduce_only=False,
                post_only=False,
            )

        resp = await asyncio.to_thread(_call)

        log({
            "event": "live_entry_sent",
            "symbol": self.symbol,
            "side": side,
            "size": size,
            "resp": resp,
        })

        def _extract_order_error(r):
            try:
                statuses = r.get("response", {}).get("data", {}).get("statuses", [])
                for s in statuses:
                    if isinstance(s, dict) and "error" in s:
                        return s["error"]
            except Exception:
                pass
            return None

        def _extract_order_id(r):
            try:
                statuses = r.get("response", {}).get("data", {}).get("statuses", [])
                for s in statuses:
                    if isinstance(s, dict):
                        if "resting" in s:
                            return s["resting"].get("oid")
                        if "filled" in s:
                            return s["filled"].get("oid")
            except Exception:
                pass
            return None

        # resp may already be the raw exchange response
        exchange_resp = resp if isinstance(resp, dict) else {"raw": resp}
        err = _extract_order_error(exchange_resp)

        if err:
            return {"ok": False, "error": err, "resp": resp}

        order_id = _extract_order_id(exchange_resp)
        log({"event": "live_entry_order_id", "symbol": self.symbol, "order_id": order_id})
        return {"ok": True, "resp": resp, "order_id": order_id}

    async def live_close_marketlike(self, side: Side, size: float):
        """
        Reduce-only close at a more aggressive slippage away from mid.
        Then check for residual dust and sweep once more if needed.
        """
        if self.hl is None:
            return {"ok": False, "error": "hl_not_initialized"}

        is_buy = side == "SHORT"  # opposite action to close the position

        def _call(close_size: float):
            mid = self.hl.mid(self.symbol)
            px = self._apply_slippage(mid, side, is_entry=False)
            return self.hl.place_limit(
                coin=self.symbol,
                is_buy=is_buy,
                px=px,
                sz=close_size,
                reduce_only=True,
                post_only=False,
            )

        def _extract_order_error(r):
            try:
                statuses = r.get("response", {}).get("data", {}).get("statuses", [])
                for s in statuses:
                    if isinstance(s, dict) and "error" in s:
                        return s["error"]
            except Exception:
                pass
            return None

        # First close attempt
        resp = await asyncio.to_thread(_call, size)

        log({
            "event": "live_exit_sent",
            "symbol": self.symbol,
            "side": side,
            "size": size,
            "resp": resp,
        })

        exchange_resp = resp if isinstance(resp, dict) else {"raw": resp}
        err = _extract_order_error(exchange_resp)
        if err:
            return {"ok": False, "error": err, "resp": resp}

        # Give exchange a moment, then check if any residual remains
        await asyncio.sleep(1.0)
        snap = await self.live_position_snapshot()

        if snap.get("ok") and snap.get("in_position"):
            remaining_side = snap.get("side")
            remaining_size = float(snap.get("size", 0.0))

            # Only sweep if leftover is still the same-side residual
            if remaining_side == side and remaining_size > float(os.getenv("DUST_EPS", "1e-6")):
                log({
                    "event": "live_exit_residual_detected",
                    "symbol": self.symbol,
                    "side": side,
                    "remaining_size": remaining_size,
                })

                sweep_resp = await asyncio.to_thread(_call, remaining_size)

                log({
                    "event": "live_exit_sweep_sent",
                    "symbol": self.symbol,
                    "side": side,
                    "remaining_size": remaining_size,
                    "resp": sweep_resp,
                })

                sweep_exchange_resp = sweep_resp if isinstance(sweep_resp, dict) else {"raw": sweep_resp}
                sweep_err = _extract_order_error(sweep_exchange_resp)
                if sweep_err:
                    return {"ok": False, "error": sweep_err, "resp": resp, "sweep_resp": sweep_resp}

                return {"ok": True, "resp": resp, "sweep_resp": sweep_resp}

        return {"ok": True, "resp": resp}

    # =========================================================
    # Unified entry / exit (signal scoring + sizing + collateral)
    # =========================================================

    async def open_trade(
        self,
        side: Side,
        size: float,
        stop_px: float,
        tp_px: float,
        trade_id: str,
        signal_ctx: Optional[SignalContext] = None,
        atr: Optional[float] = None,
        _from_queue: bool = False,
    ):
        """
        Unified entry point used by signalbot (signal_ctx provided) and
        optionally by queue-fire re-entries (_from_queue=True).
        Scalpbot continues to call paper_open / live_open_marketlike directly.

        Flow when signal_ctx provided:
          1. score_signal → StrengthResult
          2. calculate_sizing → size, tp1, leverage
          3. collateral gate (live only)  — queue if unaffordable
          4. cm.allocate (live only)
          5. set_leverage via _leverage_override in live_open_marketlike
          6. dispatch to paper_open or live_open_marketlike
        """
        # ── Step: sizing ──────────────────────────────────────────────────────
        if signal_ctx is not None:
            strength = score_signal(signal_ctx)
            sizing = calculate_sizing(
                signal_ctx, strength, self.risk_usd, atr or 0.0, self.symbol
            )
            size = sizing.size
            tp_px = sizing.tp1
            final_leverage = sizing.leverage
            log({
                "event": "open_trade_sizing",
                "symbol": self.symbol,
                "score": strength.score,
                "tier": strength.tier,
                "size": size,
                "leverage": final_leverage,
                "tp": tp_px,
                "tp_payout_usd": round(sizing.tp_payout_usd, 2),
            })
        else:
            strength = None
            final_leverage = int(self.leverage)   # scalpbot path — unchanged

        # ── Step: collateral gate (live only) ────────────────────────────────
        if self.mode == "live":
            est_entry = await asyncio.to_thread(self.hl.mid, self.symbol)
            req_margin = cm.required_margin(size, est_entry, final_leverage)
            affordable, free = cm.can_afford(req_margin)
            if not affordable:
                if not _from_queue:
                    cm.queue_order({
                        "id": trade_id,
                        "symbol": self.symbol,
                        "side": side,
                        "size": size,
                        "entry": est_entry,
                        "stop": stop_px,
                        "tp1": tp_px,
                        "leverage": final_leverage,
                        "required_margin": req_margin,
                        "signal_score": strength.score if strength is not None else 0.0,
                    })
                    log({
                        "event": "open_trade_queued",
                        "symbol": self.symbol,
                        "side": side,
                        "free_collateral": round(free, 2),
                        "required_margin": round(req_margin, 2),
                    })
                else:
                    log({
                        "event": "open_trade_queue_drop",
                        "symbol": self.symbol,
                        "side": side,
                        "reason": "queued_order_still_unaffordable",
                        "free_collateral": round(free, 2),
                        "required_margin": round(req_margin, 2),
                    })
                return

            cm.allocate(trade_id, req_margin)

        # ── Step: dispatch ────────────────────────────────────────────────────
        if self.mode == "paper":
            entry_px = signal_ctx.entry if signal_ctx is not None else stop_px
            await self.paper_open(side, size, entry_px, trade_id)
        else:
            await self.live_open_marketlike(
                side, size, _leverage_override=final_leverage
            )

    async def close_trade(
        self,
        side: Side,
        size: float,
        reason: str,
        trade_id: str = "",
        exit_px: float = 0.0,
    ):
        """
        Unified exit point. Releases margin and fires any queued orders
        that are now affordable (live mode only).
        """
        if self.mode == "paper":
            await self.paper_close(exit_px, reason)
        else:
            await self.live_close_marketlike(side, size)

        # ── Margin release + queue flush (live only) ─────────────────────────
        if self.mode == "live":
            released = cm.release(trade_id)
            if released > 0:
                fired = cm.flush_queue(get_mid_fn=self.hl.mid)
                for queued_order in fired:
                    log({
                        "event": "queue_order_firing",
                        "symbol": self.symbol,
                        "queued_id": queued_order.get("id"),
                        "queued_symbol": queued_order.get("symbol"),
                        "side": queued_order.get("side"),
                    })
                    await self.open_trade(
                        side=queued_order["side"],
                        size=queued_order["size"],
                        stop_px=queued_order["stop"],
                        tp_px=queued_order["tp1"],
                        trade_id=queued_order["id"],
                        _from_queue=True,
                    )