from __future__ import annotations

import os
import time
import requests

from typing import Dict, Iterator, Optional, Tuple, Any, Set


# ------------------------------------------------------
# HELPERS
# ------------------------------------------------------
def envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


# ------------------------------------------------------
# CLASS
# ------------------------------------------------------
class DexScreenerRealFeed:
    """
    Pulls newest token profiles from DexScreener
    and enriches them with REAL liquidity + volume data.

    Role:
    - DISCOVERY
    - LIQUIDITY VALIDATION
    - VOLUME SNAPSHOT (m5 / h1)
    """

    def __init__(self) -> None:
        self.chain_id = os.getenv("REAL_CHAIN_ID", "solana").lower()
        self.poll_seconds = int(os.getenv("REAL_POLL_SECONDS", "5"))
        self.max_new_per_poll = int(os.getenv("REAL_MAX_NEW_PER_POLL", "20"))
        self.min_liq_usd = envf("LIQUIDITY_MIN_USD", 1.0)

        self._seen: Set[str] = set()
        self._sess = requests.Session()

        print(
            f"[DEX] HTTP mode | chain={self.chain_id} | "
            f"poll={self.poll_seconds}s | min_liq=${self.min_liq_usd:.2f}"
        )

    # --------------------------------------------------
    # FETCHERS
    # --------------------------------------------------
    def _fetch_latest_profiles(self) -> Any:
        url = "https://api.dexscreener.com/token-profiles/latest/v1"
        r = self._sess.get(url, timeout=15)
        r.raise_for_status()
        return r.json()

    def _fetch_overview(self, mint: str) -> Dict:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        r = self._sess.get(url, timeout=15)
        r.raise_for_status()
        return r.json()

    # --------------------------------------------------
    # EXTRACTION
    # --------------------------------------------------
    def _pick_pair(self, ov: Dict) -> Optional[Dict]:
        pairs = ov.get("pairs") or []
        if not pairs:
            return None

        for p in pairs:
            if (p.get("chainId") or "").lower() == self.chain_id:
                return p

        return pairs[0]

    def _extract_liquidity(self, pair: Dict) -> float:
        try:
            return float(pair.get("liquidity", {}).get("usd") or 0.0)
        except Exception:
            return 0.0

    def _extract_volumes(self, pair: Dict) -> Tuple[float, float]:
        vol = pair.get("volume") or {}
        try:
            v5 = float(vol.get("m5") or 0.0)
        except Exception:
            v5 = 0.0
        try:
            v1h = float(vol.get("h1") or 0.0)
        except Exception:
            v1h = 0.0
        return v5, v1h

    # --------------------------------------------------
    # ITERATOR
    # --------------------------------------------------
    def iter_candidates(self) -> Iterator[Dict[str, Any]]:
        while True:
            try:
                profiles = self._fetch_latest_profiles()
            except Exception as e:
                print(f"[DEX] fetch error: {e}")
                time.sleep(self.poll_seconds)
                continue

            yielded = 0

            for it in profiles:
                if yielded >= self.max_new_per_poll:
                    break

                if (it.get("chainId") or "").lower() != self.chain_id:
                    continue

                mint = it.get("tokenAddress")
                if not mint:
                    continue

                # ✅ Correct behavior: never reprocess seen tokens
                if mint in self._seen:
                    continue

                symbol = it.get("tokenSymbol") or it.get("symbol") or "UNK"

                # --------------------------------------------------
                # OVERVIEW
                # --------------------------------------------------
                try:
                    ov = self._fetch_overview(mint)
                    pair = self._pick_pair(ov)
                    if not pair:
                        self._seen.add(mint)
                        continue

                    liquidity = self._extract_liquidity(pair)
                    vol_m5, vol_h1 = self._extract_volumes(pair)

                except Exception:
                    self._seen.add(mint)
                    continue

                # --------------------------------------------------
                # LIQUIDITY GATE
                # --------------------------------------------------
                if liquidity < self.min_liq_usd:
                    print(
                        f"[FILTER][LIQ] SKIP {mint} ({symbol}) | "
                        f"liq={liquidity:.2f} < {self.min_liq_usd:.2f}"
                    )
                    self._seen.add(mint)
                    continue

                # --------------------------------------------------
                # STABLE VOLUME ACCELERATION
                # --------------------------------------------------
                if vol_h1 > 10:
                    accel = min(vol_m5 / vol_h1, 5.0)
                else:
                    accel = 0.0

                self._seen.add(mint)
                yielded += 1

                yield {
                    "mint": mint,
                    "symbol": symbol,
                    "liquidity": liquidity,
                    "pool": it.get("url"),
                    "volume_m5": vol_m5,
                    "volume_h1": vol_h1,
                    "vol_accel": accel,
                }

            if yielded == 0:
                print(
                    f"[DEX] heartbeat: no candidates | "
                    f"seen_cache={len(self._seen)} | sleep={self.poll_seconds}s"
                )

            time.sleep(self.poll_seconds)


# alias safety
DexFeed = DexScreenerRealFeed
