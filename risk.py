# onchain/risk.py
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------
# 1) Liquidity -> base TP/SL
# ------------------------------------------------------------
def dynamic_tp_sl(liquidity_usd: float) -> Tuple[float, float]:
    """
    Base TP/SL derived from liquidity buckets.
    (Matches your current behavior: liq ~ 10k+ => TP~25 / SL~12)
    """
    liq = float(liquidity_usd or 0.0)

    if liq >= _envf("RISK_LIQ_TIER_4", 50_000):
        return (_envf("RISK_TP_T4", 22.0), _envf("RISK_SL_T4", 10.0))
    if liq >= _envf("RISK_LIQ_TIER_3", 10_000):
        return (_envf("RISK_TP_T3", 25.0), _envf("RISK_SL_T3", 12.0))
    if liq >= _envf("RISK_LIQ_TIER_2", 2_000):
        return (_envf("RISK_TP_T2", 18.0), _envf("RISK_SL_T2", 14.0))
    if liq >= _envf("RISK_LIQ_TIER_1", 200):
        return (_envf("RISK_TP_T1", 14.0), _envf("RISK_SL_T1", 16.0))

    return (_envf("RISK_TP_T0", 10.0), _envf("RISK_SL_T0", 18.0))


# ------------------------------------------------------------
# 2) Bias TP/SL using DexScreener acceleration (m5/h1)
# ------------------------------------------------------------
def apply_volume_bias(tp_pct: float, sl_pct: float, vol_accel: float) -> Tuple[float, float]:
    tp = float(tp_pct)
    sl = float(sl_pct)
    a = float(vol_accel or 0.0)

    strong = _envf("VOL_ACCEL_STRONG", 1.6)
    weak = _envf("VOL_ACCEL_WEAK", 0.7)

    tp_up = _envf("VOL_TP_UP_MULT", 1.10)
    sl_tight = _envf("VOL_SL_TIGHT_MULT", 0.90)

    tp_down = _envf("VOL_TP_DOWN_MULT", 0.90)
    sl_loose = _envf("VOL_SL_LOOSE_MULT", 1.08)

    tp_min = _envf("TP_MIN_PCT", 8.0)
    tp_max = _envf("TP_MAX_PCT", 40.0)
    sl_min = _envf("SL_MIN_PCT", 6.0)
    sl_max = _envf("SL_MAX_PCT", 30.0)

    if a >= strong:
        tp *= tp_up
        sl *= sl_tight
    elif a > 0 and a <= weak:
        tp *= tp_down
        sl *= sl_loose

    return (_clamp(tp, tp_min, tp_max), _clamp(sl, sl_min, sl_max))


# ------------------------------------------------------------
# 3) Cap TP if liquidity drops mid-trade
# ------------------------------------------------------------
def cap_tp_on_liquidity_drop(tp_pct: float, entry_liq: float, current_liq: float) -> float:
    tp = float(tp_pct)
    e = float(entry_liq or 0.0)
    c = float(current_liq or 0.0)

    if e <= 0:
        return tp

    drop = 1.0 - (c / e)  # e.g. 0.25 = -25%

    drop_soft = _envf("LIQ_DROP_SOFT", 0.25)
    drop_hard = _envf("LIQ_DROP_HARD", 0.50)

    cap_soft = _envf("TP_CAP_SOFT_MULT", 0.85)
    cap_hard = _envf("TP_CAP_HARD_MULT", 0.70)

    if drop >= drop_hard:
        return tp * cap_hard
    if drop >= drop_soft:
        return tp * cap_soft
    return tp


# ------------------------------------------------------------
# 4) Trailing TP when WS accel is strong
# ------------------------------------------------------------
def trailing_tp_pct(base_tp_pct: float, ws_accel_ratio: float, hold_seconds: int) -> float:
    tp = float(base_tp_pct)
    a = float(ws_accel_ratio or 0.0)
    t = int(hold_seconds or 0)

    accel_on = _envf("TRAIL_ACCEL_ON", 1.40)
    accel_super = _envf("TRAIL_ACCEL_SUPER", 2.20)

    boost = 1.0
    if a >= accel_super:
        boost = _envf("TRAIL_TP_BOOST_SUPER", 1.15)
    elif a >= accel_on:
        boost = _envf("TRAIL_TP_BOOST", 1.08)

    tighten_start = int(_envf("TRAIL_TIGHTEN_START_SEC", 12))
    tighten_end = int(_envf("TRAIL_TIGHTEN_END_SEC", 60))
    tighten_mult_end = _envf("TRAIL_TP_END_MULT", 0.85)

    eff = tp * boost

    if t <= tighten_start:
        return eff
    if t >= tighten_end:
        return eff * tighten_mult_end

    span = max(1, tighten_end - tighten_start)
    k = (t - tighten_start) / span  # 0..1
    mult = 1.0 + (tighten_mult_end - 1.0) * k
    return eff * mult


# ------------------------------------------------------------
# 5) WS decay: force exit if activity collapses
# ------------------------------------------------------------
def should_force_exit(ws_eps: float, ws_accel_ratio: float) -> bool:
    eps = float(ws_eps or 0.0)
    accel = float(ws_accel_ratio or 0.0)

    eps_floor = _envf("WS_MIN_EPS", 0.05)
    accel_floor = _envf("WS_MIN_ACCEL", 0.45)

    if eps < eps_floor:
        return True
    if accel > 0 and accel < accel_floor:
        return True
    return False


# ------------------------------------------------------------
# Unified plan object
# ------------------------------------------------------------
@dataclass
class RiskPlan:
    tp_pct: float
    sl_pct: float
    force_exit: bool
    ws_eps: float = 0.0
    ws_accel: float = 0.0


def compute_risk_plan(
    *,
    tp_pct: float,
    sl_pct: float,
    entry_liq: float,
    current_liq: float,
    vol_accel_now: float,
    peak_accel: float,
    vol_ws,
    mint: str,
    hold_seconds: int,
) -> RiskPlan:
    """
    Unified logic:
    - liquidity drop caps TP
    - WS accel triggers trailing TP
    - WS decay triggers force exit
    - slight SL tighten when momentum is strong
    """
    base_tp = float(tp_pct)
    base_sl = float(sl_pct)

    # 1) cap TP if liquidity fell
    capped_tp = cap_tp_on_liquidity_drop(base_tp, float(entry_liq), float(current_liq))

    # 2) Pull WS snapshot (if available)
    ws_eps = 0.0
    ws_accel = 0.0
    if vol_ws is not None:
        try:
            ws_eps, ws_accel = vol_ws.snapshot(mint)
        except Exception:
            ws_eps, ws_accel = 0.0, 0.0

    # If WS not available, fallback accel proxy:
    accel_for_trailing = ws_accel if ws_accel > 0 else float(vol_accel_now or 0.0)

    # 3) trailing TP if accel is strong
    tp_eff = trailing_tp_pct(capped_tp, accel_for_trailing, int(hold_seconds))

    # 4) SL tweaks: tighten SL slightly if peak accel is strong
    sl_eff = base_sl
    if float(peak_accel or 0.0) >= _envf("SL_TIGHTEN_PEAK_ACCEL", 2.0):
        sl_eff *= _envf("SL_TIGHTEN_MULT", 0.92)

    # 5) Force exit if WS decays (only if WS exists)
    force = False
    if vol_ws is not None:
        force = should_force_exit(ws_eps=ws_eps, ws_accel_ratio=ws_accel)

    # final clamps
    tp_eff = _clamp(tp_eff, _envf("TP_MIN_PCT", 8.0), _envf("TP_MAX_PCT", 40.0))
    sl_eff = _clamp(sl_eff, _envf("SL_MIN_PCT", 6.0), _envf("SL_MAX_PCT", 30.0))

    return RiskPlan(
        tp_pct=float(tp_eff),
        sl_pct=float(sl_eff),
        force_exit=bool(force),
        ws_eps=float(ws_eps),
        ws_accel=float(ws_accel),
    )
