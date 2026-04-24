# onchain/helius_ws.py

import os
import time
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

import requests


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


@dataclass
class ActivityResult:
    ok: bool
    reason: str
    last_seen_ts: Optional[float] = None
    age_sec: Optional[float] = None


class HeliusSwapMonitor:
    """
    Helius-based verification WITHOUT WebSockets.

    - ACTIVITY: uses getSignaturesForAddress(mint, limit=1) and checks blockTime within window
    - AGE: defines token "age" as time since FIRST time we observed activity for that mint
      (practical for anti-insta-rug: "has this thing existed for >= N minutes with swaps?")
    """

    def __init__(self, swap_window_sec: int = 90):
        api_key = _env("HELIUS_API_KEY")
        if not api_key:
            raise RuntimeError("HELIUS_API_KEY missing from environment")

        # Prefer your working endpoint (rpc.helius.xyz)
        base = _env("HELIUS_RPC_HTTP", f"https://rpc.helius.xyz/?api-key={api_key}")

        self.http = base
        self.swap_window_sec = int(swap_window_sec)

        self._sess = requests.Session()
        self._lock = threading.Lock()

        # mint -> (first_seen_ts, last_seen_ts)
        self._seen: Dict[str, Tuple[float, float]] = {}

        # Simple rate limiter
        self._last_call_ts = 0.0
        self._min_interval = float(_env("HELIUS_MIN_CALL_INTERVAL_SEC", "0.10"))  # 10 calls/sec max

        print(f"[HELIUS] Using HTTP RPC: {self.http}")

    # Keep same interface your auto_runner expects
    def start(self):
        # no background thread needed, but keep API consistent
        print("[HELIUS] Monitor ready (HTTP mode)")

    def stop(self):
        try:
            self._sess.close()
        except Exception:
            pass
        print("[HELIUS] Monitor stopped")

    # ------------------------
    # Core RPC helpers
    # ------------------------
    def _rpc(self, method: str, params):
        # light throttle
        now = time.time()
        wait = self._min_interval - (now - self._last_call_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_call_ts = time.time()

        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        r = self._sess.post(self.http, json=payload, timeout=20)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(j["error"])
        return j.get("result")

    def _latest_sig_blocktime(self, address: str) -> Optional[int]:
        """
        Returns the most recent blockTime (unix seconds) for an address, or None if no sigs.
        """
        res = self._rpc("getSignaturesForAddress", [address, {"limit": 1}])
        if not res:
            return None
        bt = res[0].get("blockTime")
        return bt

    # ------------------------
    # Public API used by runner
    # ------------------------
    def has_recent_activity(self, mint: str, min_swaps: int = 1) -> bool:
        """
        Treat 'activity' as: the mint address had a recent signature within swap_window_sec.
        This is a cheap and reliable proxy to confirm it's doing on-chain stuff NOW.
        """
        try:
            bt = self._latest_sig_blocktime(mint)
        except Exception as e:
            print(f"[HELIUS] activity error for {mint}: {e}")
            return False

        if bt is None:
            return False

        now = int(time.time())
        if now - bt > self.swap_window_sec:
            return False

        with self._lock:
            first, last = self._seen.get(mint, (float(bt), float(bt)))
            if mint not in self._seen:
                first = float(bt)
            last = float(bt)
            self._seen[mint] = (first, last)

        return True

    def get_token_age_sec(self, mint: str) -> Optional[float]:
        """
        Age = time since FIRST time we've observed recent activity (first_seen_ts).
        If we haven't observed activity yet, returns None.
        """
        with self._lock:
            if mint not in self._seen:
                return None
            first, _last = self._seen[mint]

        return max(0.0, time.time() - first)

    def verify(self, mint: str, min_age_minutes: float, min_swaps: int = 1) -> ActivityResult:
        if not self.has_recent_activity(mint, min_swaps=min_swaps):
            return ActivityResult(False, "no recent activity (within window)")

        age_sec = self.get_token_age_sec(mint)
        if age_sec is None:
            return ActivityResult(False, "age unknown (no first_seen)")
        if age_sec < (min_age_minutes * 60):
            return ActivityResult(False, f"age {age_sec/60:.2f}m < {min_age_minutes}m", age_sec=age_sec)

        with self._lock:
            first, last = self._seen[mint]
        return ActivityResult(True, "ok", last_seen_ts=last, age_sec=age_sec)
