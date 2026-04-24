from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional, Any, Callable

from onchain.autosell import AutoSellManager
from onchain import logger
from onchain.risk import dynamic_tp_sl, apply_volume_bias
from onchain.helius_volume_ws import HeliusVolumeWS

from onchain.jupiter_live import JupiterLive
from onchain.live_guard import mint_allowed


def _env_bool(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default)).strip().lower() in ("1", "true", "yes", "y", "on")


def _is_pumpfun_source(pool: Optional[str]) -> bool:
    if not pool:
        return False
    p = str(pool).lower()
    return p == "pumpfun" or p.startswith("pumpfun") or ("pump.fun" in p)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class Engine:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

        # ----------------------------
        # REAL-TIME VOLUME FEED
        # ----------------------------
        try:
            self.vol_ws = HeliusVolumeWS()
        except Exception as e:
            print(f"[ENGINE] WS disabled: {e}")
            self.vol_ws = None

        # ----------------------------
        # JUPITER (LIVE only)
        # ----------------------------
        self.jupiter: Optional[JupiterLive] = None
        if os.getenv("MODE", "DRYRUN").upper() == "LIVE":
            try:
                self.jupiter = JupiterLive()
            except Exception as e:
                print(f"[ENGINE] Jupiter disabled: {e}")
                self.jupiter = None

        self.autosell = AutoSellManager(self)

        self.on_exit_notify: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.on_entry_notify: Optional[Callable[[str, Dict[str, Any]], None]] = None

    # --------------------------------------------------
    # LIFECYCLE
    # --------------------------------------------------
    def start(self) -> None:
        logger.log_event(self.run_id, "ENGINE_START", None, "Engine started")
        if self.vol_ws:
            try:
                self.vol_ws.start()
            except Exception:
                self.vol_ws = None

    def stop(self) -> None:
        if self.vol_ws:
            try:
                self.vol_ws.stop()
            except Exception:
                pass
        logger.log_event(self.run_id, "ENGINE_STOP", None, "Engine stopped")

    def open_count(self) -> int:
        with self.lock:
            return len(self.positions)

    # --------------------------------------------------
    # RUNNER ENTRYPOINT
    # --------------------------------------------------
    def on_candidate(
        self,
        mint: str,
        symbol: Optional[str],
        pool: Optional[str],
        liquidity: Optional[float],
        meta: Optional[Dict[str, Any]],
    ) -> None:
        meta = meta or {}
        mint = str(mint)
        mode = os.getenv("MODE", "DRYRUN").upper()

        # ----------------------------
        # POSITION GUARDS
        # ----------------------------
        max_open = int(os.getenv("MAX_OPEN_POSITIONS", "1"))
        with self.lock:
            if mint in self.positions or len(self.positions) >= max_open:
                return

        # ----------------------------
        # WS SUBSCRIBE
        # ----------------------------
        if self.vol_ws:
            try:
                self.vol_ws.subscribe(mint)
            except Exception:
                pass

        # ----------------------------
        # SNIPER SAFE GATE
        # ----------------------------
        sniper_safe = mode == "SNIPER_SAFE"
        is_pumpfun = bool(meta.get("is_pumpfun")) or _is_pumpfun_source(pool)

        if sniper_safe and is_pumpfun and not _env_bool("ALLOW_PUMPFUN_LIVE", "0"):
            print(f"[ENGINE][SKIP] pumpfun discovery only | {mint}")
            return

        # ----------------------------
        # BASIC QUALITY FILTERS
        # ----------------------------
        liq = float(liquidity or 0.0)
        vol_m5 = float(meta.get("vol_m5") or 0.0)
        vol_accel = float(meta.get("vol_accel") or 1.0)

        min_liq = float(os.getenv("MIN_LIQ_USD", "100000"))
        min_vol_m5 = float(os.getenv("MIN_VOL_M5_USD", "50000"))
        max_impact = float(os.getenv("MAX_PRICE_IMPACT_PCT", "2.0"))

        if liq < min_liq or vol_m5 < min_vol_m5:
            return

        # ----------------------------
        # USD → SOL SIZING (simple)
        # ----------------------------
        trade_usd = float(meta.get("trade_usd") or os.getenv("TRADE_USD", "7"))
        sol_usd = float(os.getenv("SOL_USD", "150"))
        size_sol = trade_usd / sol_usd
        if size_sol <= 0:
            return

        # ----------------------------
        # REAL JUPITER QUOTE GATE (and store quote)
        # ----------------------------
        impact_pct = 0.0
        entry_quote = None

        if self.jupiter:
            try:
                entry_quote = self.jupiter.quote_sol_to_token(mint, size_sol)
                impact_pct = float(entry_quote.get("priceImpactPct", 1)) * 100.0
            except Exception as e:
                print(f"[ENGINE][SKIP] quote failed {mint}: {e}")
                return

            if impact_pct > max_impact:
                print(f"[ENGINE][SKIP] impact too high | {mint} | {impact_pct:.2f}% > {max_impact:.2f}%")
                return

        # ----------------------------
        # DYNAMIC TP / SL
        # ----------------------------
        base_tp, base_sl = dynamic_tp_sl(liq)
        tp_pct, sl_pct = apply_volume_bias(base_tp, base_sl, vol_accel)

        tp_pct = float(os.getenv("TAKE_PROFIT_PCT", tp_pct))
        sl_pct = float(os.getenv("STOP_LOSS_PCT", sl_pct))

        tp_pct = _clamp(tp_pct, 5.0, 300.0)
        sl_pct = _clamp(sl_pct, 3.0, 80.0)

        # ----------------------------
        # LIVE BUY
        # ----------------------------
        entry_sig = None
        buy_meta = None

        if mode == "LIVE":
            if not self.jupiter:
                return
            if not _env_bool("LIVE_TRADE_ENABLED", "0") or _env_bool("LIVE_KILL_SWITCH", "0"):
                return
            if _env_bool("LIVE_ALLOWLIST_ONLY", "0") and not mint_allowed(mint):
                return

            res = self.jupiter.buy_with_sol(mint, size_sol)
            if not res.ok:
                print(f"[LIVE][BUY FAILED] {mint} | {res.err}")
                return

            entry_sig = res.signature
            buy_meta = res.meta or {}

        # ----------------------------
        # POSITION REGISTER
        # ----------------------------
        position = {
            "run_id": self.run_id,
            "mode": mode,
            "mint": mint,
            "symbol": symbol,
            "pool": pool,

            "liquidity": liq,
            "entry_liquidity": liq,

            # we keep placeholder entry_price for DRYRUN;
            # LIVE PnL is computed via autosell quotes.
            "entry_price": 1.0,

            "entry_sig": entry_sig,
            "exit_sig": None,

            "size": size_sol,          # SOL spent on buy
            "trade_usd": trade_usd,
            "impact_pct": impact_pct,

            "tp_pct": tp_pct,
            "sl_pct": sl_pct,

            "timestamp_entry": int(time.time()),

            "paper_size_usd": trade_usd,

            # analytics
            "token_age_min": meta.get("token_age_min"),
            "vol_m5": vol_m5,
            "vol_h1": meta.get("vol_h1"),
            "vol_accel": vol_accel,
            "is_pumpfun": is_pumpfun,

            # store quote/meta for later accurate accounting
            "entry_quote": entry_quote,
            "buy_meta": buy_meta,
        }

        with self.lock:
            self.positions[mint] = position

        print(
            f"[ENGINE] ENTERED {mint} | mode={mode} | trade=${trade_usd:.2f} | "
            f"impact={impact_pct:.2f}% | TP={tp_pct:.1f}% SL={sl_pct:.1f}% | entry_sig={entry_sig}"
        )

        if self.on_entry_notify:
            self.on_entry_notify("ENTRY", {"mint": mint, "trade_usd": trade_usd, "mode": mode})

        threading.Thread(
            target=self._run_autosell,
            args=(position,),
            daemon=True,
        ).start()

    # --------------------------------------------------
    # AUTOSELL
    # --------------------------------------------------
    def _run_autosell(self, position: Dict[str, Any]) -> None:
        try:
            result = self.autosell.run(position)
            self._close_position(position, result)
        except Exception as e:
            print(f"[AUTOSELL][ERROR] {position.get('mint')} | {e}")
            with self.lock:
                self.positions.pop(position["mint"], None)

    # --------------------------------------------------
    # CLOSE (log LIVE exit_sig + pnl_pct)
    # --------------------------------------------------
    def _close_position(self, position: Dict[str, Any], result: Dict[str, Any]) -> None:
        mint = position["mint"]
        mode = str(position.get("mode") or os.getenv("MODE", "DRYRUN")).upper()

        if self.vol_ws:
            try:
                self.vol_ws.unsubscribe(mint)
            except Exception:
                pass

        reason_raw = str(result.get("reason") or "SL").upper()
        if reason_raw in ("TP", "TAKE_PROFIT"):
            result_type = "TP"
        elif reason_raw in ("TIMEOUT", "TIMER", "STALE"):
            result_type = "TIMEOUT"
        elif reason_raw == "ERROR":
            result_type = "ERROR"
        else:
            result_type = "SL"

        exit_sig = result.get("exit_sig")
        pnl_pct = float(result.get("pnl_pct") or 0.0)

        # Keep paper pnl for compatibility (until we compute real USD)
        paper_size = float(position.get("paper_size_usd") or 0.0)
        if result_type == "TP":
            pnl = +paper_size
        elif result_type == "TIMEOUT":
            pnl = float(result.get("paper_pnl_usd") or (-0.25 * paper_size))
        elif result_type == "ERROR":
            pnl = float(result.get("paper_pnl_usd") or (-0.50 * paper_size))
        else:
            pnl = -paper_size

        logger.log_trade({
            "run_id": self.run_id,
            "mode": mode,
            "mint": mint,
            "symbol": position.get("symbol"),
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": result_type,
            "success": 1 if result_type == "TP" else 0,

            "entry_sig": position.get("entry_sig"),
            "exit_sig": exit_sig,

            "impact_pct": position.get("impact_pct"),
            "trade_usd": position.get("trade_usd"),

            "timestamp_entry": position.get("timestamp_entry"),
            "timestamp_exit": int(time.time()),
        })

        with self.lock:
            self.positions.pop(mint, None)

        print(f"[AUTOSELL][{mode}] {mint} EXIT {result_type} | pnl_pct={pnl_pct:.2f}% | exit_sig={exit_sig}")

        if self.on_exit_notify:
            self.on_exit_notify(result_type, {"paper_pnl_usd": pnl, "pnl_pct": pnl_pct, "exit_sig": exit_sig})
