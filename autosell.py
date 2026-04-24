from __future__ import annotations

import os
import random
import time
from typing import Dict, Any, Optional

from onchain.risk import compute_risk_plan


class AutoSellManager:
    """
    Phase-1 autosell:
      - DRYRUN: simulator (unchanged)
      - LIVE: real Jupiter quote + sell-to-SOL
    """

    def __init__(self, engine):
        self.engine = engine

    # -------------------------
    # Public entry
    # -------------------------
    def run(self, position: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(position.get("mode") or os.getenv("MODE", "DRYRUN").upper()).upper()

        # LIVE path
        if mode == "LIVE":
            return self._run_live(position)

        # Default DRYRUN path (original behavior)
        return self._run_dryrun(position)

    # -------------------------
    # DRYRUN (UNCHANGED)
    # -------------------------
    def _run_dryrun(self, position: Dict[str, Any]) -> Dict[str, Any]:
        entry_price = float(position.get("entry_price") or 1.0)
        size = float(position.get("size") or 0.0)
        mint = str(position.get("mint") or "")

        tp_pct = float(position.get("tp_pct") or 35.0)
        sl_pct = float(position.get("sl_pct") or 20.0)

        entry_liq = float(position.get("entry_liquidity") or 0.0)
        current_liq = float(position.get("liquidity") or entry_liq)

        vol_accel_now = float(position.get("vol_accel") or 1.0)
        peak_accel = float(position.get("peak_accel") or vol_accel_now)

        max_hold = int(position.get("max_hold") or int(os.getenv("MAX_HOLD_SECONDS", "420")))
        tick = float(os.getenv("AUTOSELL_TICK_SEC", "1.0"))

        # DRYRUN hold window
        sleep_min = float(os.getenv("DRYRUN_HOLD_MIN", "6"))
        sleep_max = float(os.getenv("DRYRUN_HOLD_MAX", "28"))
        target_hold = min(max_hold, random.uniform(sleep_min, sleep_max))

        start = time.time()

        # Defaults (if force-exit triggers early)
        reason = "SL"
        exit_price = entry_price * (1.0 - sl_pct / 100.0)

        while True:
            hold_seconds = int(time.time() - start)

            # update peak accel
            cur_accel = float(position.get("vol_accel") or vol_accel_now)
            peak_accel = max(peak_accel, cur_accel)
            position["peak_accel"] = peak_accel

            plan = compute_risk_plan(
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                entry_liq=entry_liq,
                current_liq=float(position.get("liquidity") or current_liq),
                vol_accel_now=cur_accel,
                peak_accel=peak_accel,
                vol_ws=getattr(self.engine, "vol_ws", None),
                mint=mint,
                hold_seconds=hold_seconds,
            )

            tp_used = float(plan.tp_pct)
            sl_used = float(plan.sl_pct)

            # Force exit if WS collapse detected
            if plan.force_exit:
                reason = "SL"
                exit_price = entry_price * (1.0 - (sl_used / 100.0))
                break

            # normal exit at end of hold
            if hold_seconds >= int(target_hold):
                base_win = float(os.getenv("DRYRUN_WIN_PROB", "0.58"))

                if cur_accel >= float(os.getenv("WIN_ACCEL_STRONG", "2.0")):
                    base_win += float(os.getenv("WIN_ACCEL_BONUS", "0.08"))
                elif cur_accel <= float(os.getenv("WIN_ACCEL_WEAK", "0.7")):
                    base_win -= float(os.getenv("WIN_ACCEL_PENALTY", "0.10"))

                if plan.ws_eps >= float(os.getenv("WIN_WS_EPS_GOOD", "0.20")):
                    base_win += float(os.getenv("WIN_WS_EPS_BONUS", "0.04"))

                base_win = max(0.05, min(base_win, 0.90))
                win = random.random() < base_win

                if win:
                    reason = "TP"
                    exit_price = entry_price * (1.0 + (tp_used / 100.0))
                else:
                    reason = "SL"
                    exit_price = entry_price * (1.0 - (sl_used / 100.0))
                break

            time.sleep(tick)

        hold_seconds = max(0, int(time.time() - start))

        pnl = (exit_price - entry_price) * size
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0 if entry_price else 0.0

        return {
            "mint": mint,
            "exit_price": float(exit_price),
            "pnl": float(pnl),
            "pnl_pct": float(pnl_pct),
            "reason": reason,  # "TP" or "SL"
            "hold_seconds": int(hold_seconds),
            "slippage": 0.0,
        }

    # -------------------------
    # LIVE (REAL JUPITER SELL)
    # -------------------------
    def _run_live(self, position: Dict[str, Any]) -> Dict[str, Any]:
        mint = str(position.get("mint") or "")
        entry_ts = int(position.get("timestamp_entry") or time.time())

        tp_pct = float(position.get("tp_pct") or 25.0)
        sl_pct = float(position.get("sl_pct") or 12.0)

        # We measure PnL in SOL terms:
        entry_sol = float(position.get("size") or 0.0)  # SOL spent on buy

        tick = float(os.getenv("AUTOSELL_TICK_SEC", "1.0"))
        max_hold = int(position.get("max_hold") or int(os.getenv("MAX_HOLD_SECONDS", "180")))
        settle_sec = float(os.getenv("POST_BUY_SETTLE_SEC", "2.0"))

        # Optional: allow WS collapse to force early exit
        use_ws_force_exit = str(os.getenv("LIVE_WS_FORCE_EXIT", "1")).lower() in ("1", "true", "yes", "y", "on")

        jup = getattr(self.engine, "jupiter", None)
        if not jup:
            return {
                "mint": mint,
                "reason": "ERROR",
                "err": "jupiter_not_ready",
                "hold_seconds": 0,
                "pnl_pct": 0.0,
                "exit_price": 0.0,
                "slippage": 0.0,
            }

        # Wait a moment so token account updates post-buy
        time.sleep(max(0.0, settle_sec))

        # Get initial balance (raw)
        amt_raw = 0
        for _ in range(int(os.getenv("BALANCE_RETRY", "5"))):
            amt_raw = int(jup.get_token_balance_raw(mint) or 0)
            if amt_raw > 0:
                break
            time.sleep(0.6)

        if amt_raw <= 0:
            return {
                "mint": mint,
                "reason": "ERROR",
                "err": "no_token_balance_after_buy",
                "hold_seconds": int(time.time() - entry_ts),
                "pnl_pct": 0.0,
                "exit_price": 0.0,
                "slippage": 0.0,
            }

        # Loop: quote token->SOL for CURRENT value, decide TP/SL/timeout, then sell
        reason = "TIMEOUT"
        pnl_pct = 0.0
        current_sol = 0.0
        peak_accel = float(position.get("peak_accel") or position.get("vol_accel") or 1.0)

        while True:
            hold_seconds = int(time.time() - entry_ts)

            # timeout
            if hold_seconds >= max_hold:
                reason = "TIMEOUT"
                break

            # optional ws force-exit using same risk_plan logic
            if use_ws_force_exit:
                cur_accel = float(position.get("vol_accel") or 1.0)
                peak_accel = max(peak_accel, cur_accel)
                position["peak_accel"] = peak_accel

                plan = compute_risk_plan(
                    tp_pct=tp_pct,
                    sl_pct=sl_pct,
                    entry_liq=float(position.get("entry_liquidity") or 0.0),
                    current_liq=float(position.get("liquidity") or 0.0),
                    vol_accel_now=cur_accel,
                    peak_accel=peak_accel,
                    vol_ws=getattr(self.engine, "vol_ws", None),
                    mint=mint,
                    hold_seconds=hold_seconds,
                )
                if plan.force_exit:
                    reason = "SL"
                    break

            # refresh balance (some tokens do weird things / multiple accounts)
            amt_raw = int(jup.get_token_balance_raw(mint) or 0)
            if amt_raw <= 0:
                # nothing to sell; treat as error
                return {
                    "mint": mint,
                    "reason": "ERROR",
                    "err": "no_token_balance_during_hold",
                    "hold_seconds": hold_seconds,
                    "pnl_pct": pnl_pct,
                    "exit_price": 0.0,
                    "slippage": 0.0,
                }

            # quote current sell value
            try:
                q = jup.quote_token_to_sol(mint, amt_raw)
                out_lamports = int(q.get("outAmount") or 0)
                current_sol = out_lamports / 1_000_000_000
            except Exception:
                time.sleep(tick)
                continue

            # pnl in SOL terms
            pnl_pct = ((current_sol - entry_sol) / max(entry_sol, 1e-9)) * 100.0

            if pnl_pct >= tp_pct:
                reason = "TP"
                break
            if pnl_pct <= -sl_pct:
                reason = "SL"
                break

            time.sleep(tick)

        # SELL entire balance
        amt_raw = int(jup.get_token_balance_raw(mint) or 0)
        if amt_raw <= 0:
            return {
                "mint": mint,
                "reason": "ERROR",
                "err": "no_token_balance_at_sell_time",
                "hold_seconds": int(time.time() - entry_ts),
                "pnl_pct": pnl_pct,
                "exit_price": 0.0,
                "slippage": 0.0,
            }

        sell_res = jup.sell_to_sol(mint, amt_raw)
        if not sell_res.ok:
            return {
                "mint": mint,
                "reason": "ERROR",
                "err": f"sell_failed:{sell_res.err}",
                "hold_seconds": int(time.time() - entry_ts),
                "pnl_pct": pnl_pct,
                "exit_price": 0.0,
                "slippage": 0.0,
            }

        # Optional: include quote meta for logging/debug
        return {
            "mint": mint,
            "exit_price": float(current_sol),  # in LIVE we use SOL as "price proxy"
            "pnl": 0.0,  # keep 0; engine can treat pnl via pnl_pct or later compute USD
            "pnl_pct": float(pnl_pct),
            "reason": str(reason).upper(),  # TP / SL / TIMEOUT
            "hold_seconds": int(time.time() - entry_ts),
            "slippage": 0.0,
            "exit_sig": sell_res.signature,
            "sell_meta": sell_res.meta,
        }
