from __future__ import annotations

import os
import time
import uuid
from collections import deque
from typing import Deque, Dict, Tuple

from dotenv import load_dotenv

from onchain.engine import Engine
from onchain.helius_ws import HeliusSwapMonitor
from onchain.real_feed_dexscreener import DexScreenerRealFeed
from onchain.helius_volume_ws import HeliusVolumeWS
from onchain.pumpfun_ws import PumpFunWS
from onchain import logger

load_dotenv()

# -----------------------------
# CONFIG
# -----------------------------
MODE = os.getenv("MODE", "DRYRUN").upper()
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "1"))
MIN_TOKEN_AGE_MINUTES = float(os.getenv("MIN_TOKEN_AGE_MINUTES", "1.0"))

DISCOVERY_RECHECK_SECONDS = int(os.getenv("DISCOVERY_RECHECK_SECONDS", "120"))
DISCOVERY_RECHECK_SLEEP = float(os.getenv("DISCOVERY_RECHECK_SLEEP", "1.0"))
DISCOVERY_MAX_QUEUE = int(os.getenv("DISCOVERY_MAX_QUEUE", "250"))

SIM_START_BALANCE_USD = float(os.getenv("SIM_START_BALANCE_USD", "10.83"))
SIM_FIXED_TRADE_USD = float(os.getenv("SIM_FIXED_TRADE_USD", "7.00"))

SIM_BALANCE_USD = SIM_START_BALANCE_USD
TRADES_TAKEN = 0


# -----------------------------
# EQUITY CALLBACK (EXIT)
# -----------------------------
def on_exit(result_type: str, payload: dict) -> None:
    global SIM_BALANCE_USD
    delta = float(payload.get("paper_pnl_usd") or 0.0)
    SIM_BALANCE_USD += delta
    print(
        f"[SIM] EQUITY UPDATE | "
        f"Δ={delta:+.2f} USD | "
        f"balance={SIM_BALANCE_USD:.2f} USD"
    )


# -----------------------------
# ENTRY CALLBACK (NEW)
# -----------------------------
def on_entry(result_type: str, payload: dict) -> None:
    global TRADES_TAKEN
    TRADES_TAKEN += 1
    print(
        f"[RUNNER] ENTRY CONFIRMED | "
        f"mint={payload.get('mint')} | "
        f"trade_usd={payload.get('trade_usd')}"
    )


# -----------------------------
# MAIN
# -----------------------------
def main() -> None:
    run_id = str(uuid.uuid4())

    print("\n=== AUTONOMOUS ULTRA AUTO-RUNNER (SNIPER SAFE) ===\n")
    print(f"Bot running autonomously ({MODE})")
    print("Discovery: Pump.fun WS → Validation: DexScreener → Truth: Helius → Entry: Engine")
    print(f"[RUNNER] Run ID: {run_id}")

    # ---- DB ----
    logger.init_db()

    # ---- Helius HTTP (age verification) ----
    helius = HeliusSwapMonitor(
        swap_window_sec=int(os.getenv("HELIUS_SWAP_WINDOW_SEC", "120"))
    )
    helius.start()

    # ---- Helius WS ----
    vol_ws = HeliusVolumeWS()
    try:
        vol_ws.start()
    except Exception as e:
        print(f"[RUNNER] Helius WS failed to start: {e}")

    # ---- DexScreener ----
    dex = DexScreenerRealFeed()

    # ---- Engine ----
    engine = Engine(run_id)
    engine.on_exit_notify = on_exit
    engine.on_entry_notify = on_entry   # 🔑 NEW
    engine.vol_ws = vol_ws
    engine.start()

    # ---- Discovery Queue ----
    pending: Dict[str, Tuple[float, float]] = {}
    queue: Deque[str] = deque(maxlen=DISCOVERY_MAX_QUEUE)

    # ---- Pump.fun WS ----
    def on_pumpfun_mint(mint: str):
        if mint not in pending:
            pending[mint] = (time.time(), 0.0)
            queue.append(mint)
            print(f"[DISCOVERY][PUMPFUN] queued {mint}")

    pumpfun_ws = PumpFunWS(on_pumpfun_mint)
    pumpfun_ws.start()

    try:
        while TRADES_TAKEN < MAX_TRADES_PER_DAY:

            if not queue:
                time.sleep(0.25)
                continue

            mint = queue.popleft()
            first_seen_ts, _ = pending.get(mint, (time.time(), 0.0))

            # Drop stale
            if time.time() - first_seen_ts > DISCOVERY_RECHECK_SECONDS:
                pending.pop(mint, None)
                continue

            # Skip SOL
            if mint == "So11111111111111111111111111111111111111112":
                pending.pop(mint, None)
                continue

            is_pumpfun = mint.endswith("pump")

            # ----------------------------
            # 1) AGE CHECK
            # ----------------------------
            result = helius.verify(mint=mint, min_age_minutes=MIN_TOKEN_AGE_MINUTES)
            if not result.ok:
                queue.append(mint)
                time.sleep(DISCOVERY_RECHECK_SLEEP)
                continue

            age_min = result.age_sec / 60.0

            # ----------------------------
            # 2) DEX METADATA
            # ----------------------------
            try:
                ov = dex._fetch_overview(mint)
                pair = dex._pick_pair(ov)

                symbol = "UNK"
                pool_url = None
                liquidity = 0.0
                vol_m5 = vol_h1 = vol_accel = 0.0

                if pair:
                    symbol = (pair.get("baseToken") or {}).get("symbol") or "UNK"
                    pool_url = pair.get("url")

                    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0.0)
                    vol = pair.get("volume") or {}
                    vol_m5 = float(vol.get("m5") or 0.0)
                    vol_h1 = float(vol.get("h1") or 0.0)
                    vol_accel = (vol_m5 / vol_h1) if vol_h1 > 0 else 0.0

            except Exception:
                queue.append(mint)
                time.sleep(DISCOVERY_RECHECK_SLEEP)
                continue

            # ----------------------------
            # PASS → ENGINE (NO trade counting here)
            # ----------------------------
            print(
                f"[RUNNER] Candidate → engine: {mint} | "
                f"age={age_min:.2f}m | "
                f"{'pumpfun' if is_pumpfun else f'liq=${liquidity:,.0f}'}"
            )

            engine.on_candidate(
                mint=mint,
                symbol=symbol,
                pool=pool_url,
                liquidity=liquidity,
                meta={
                    "paper_size_usd": SIM_FIXED_TRADE_USD,
                    "trade_usd": SIM_FIXED_TRADE_USD,
                    "token_age_min": age_min,
                    "vol_m5": vol_m5,
                    "vol_h1": vol_h1,
                    "vol_accel": vol_accel,
                    "is_pumpfun": is_pumpfun,
                },
            )

            pending.pop(mint, None)
            time.sleep(0.25)

        print("\n[RUNNER] Waiting for open positions...")
        while engine.open_count() > 0:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n[RUNNER] Interrupted by user")

    finally:
        pumpfun_ws.stop()
        helius.stop()
        vol_ws.stop()
        engine.stop()

        print(f"\n[STATS ANCHOR] Balance Equity: ${SIM_BALANCE_USD:.2f} USD")
        print(f"Trades taken: {TRADES_TAKEN}/{MAX_TRADES_PER_DAY}")


if __name__ == "__main__":
    main()
