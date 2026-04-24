from __future__ import annotations

import os
import time
from dataclasses import dataclass


def _flag(name: str, default=False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


@dataclass
class SafetyDecision:
    ok: bool
    reason: str


class SafetyManager:
    """
    HARD execution guard before real money.
    Call this in auto_runner BEFORE engine.on_candidate().
    """

    def __init__(self):
        self.trades_today = 0
        self.consec_losses = 0
        self.cooldown_until = 0.0

    def can_enter(self, *, mode: str, liquidity: float, size_sol: float) -> SafetyDecision:
        # 0) GLOBAL KILL SWITCH
        if _flag("KILL_SWITCH"):
            return SafetyDecision(False, "KILL_SWITCH")

        # 1) LIVE ARMING REQUIRED
        if mode == "LIVE" and not _flag("LIVE_OK"):
            return SafetyDecision(False, "LIVE_OK not set")

        # 2) Cooldown after losses
        if time.time() < self.cooldown_until:
            return SafetyDecision(False, "cooldown")

        # 3) Daily cap
        if self.trades_today >= _envi("MAX_TRADES_PER_DAY", 4):
            return SafetyDecision(False, "daily cap")

        # 4) Consecutive loss cap
        if self.consec_losses >= _envi("MAX_CONSEC_LOSSES", 3):
            return SafetyDecision(False, "max losses")

        # 5) Liquidity floor
        liq_min = _envf("LIQUIDITY_MIN_USD", 25_000)
        if mode == "LIVE":
            liq_min = _envf("LIVE_LIQUIDITY_MIN_USD", 50_000)

        if liquidity < liq_min:
            return SafetyDecision(False, f"liq {liquidity:.0f} < {liq_min:.0f}")

        # 6) Hard size cap
        if mode == "LIVE":
            max_sol = _envf("MAX_LIVE_TRADE_SOL", 0.002)
            if size_sol > max_sol:
                return SafetyDecision(False, "size cap")

        return SafetyDecision(True, "ok")

    def on_trade_result(self, result: str):
        self.trades_today += 1
        if result == "SL":
            self.consec_losses += 1
            self.cooldown_until = time.time() + _envi("COOLDOWN_AFTER_SL_SEC", 15)
        else:
            self.consec_losses = 0
