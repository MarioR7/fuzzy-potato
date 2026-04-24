# onchain/helius_volume_ws.py
from __future__ import annotations

import os
import json
import time
import threading
from collections import defaultdict, deque
from typing import Dict, Deque, Optional, Tuple

# Prefer websockets (most common). If missing, we’ll raise a clear error.
try:
    import websocket  # websocket-client
except Exception as e:
    websocket = None


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


class HeliusVolumeWS:
    """
    Real-time "activity/volume decay" proxy using logsSubscribe + mentions filter.

    We track tx-log notifications per mint address (mentions=[mint]).
    This is not perfect "swap volume", but it’s an excellent REALTIME proxy for:
    - activity collapsing (rug / dead token)
    - acceleration slowing down

    Usage:
        vol = HeliusVolumeWS()
        vol.start()
        vol.subscribe(mint)
        ...
        eps = vol.events_per_sec(mint, window_sec=20)
        accel = vol.accel_ratio(mint, short_sec=10, long_sec=60)
        ...
        vol.unsubscribe(mint)
        vol.stop()
    """

    def __init__(self):
        api_key = _env("HELIUS_API_KEY")
        if not api_key:
            raise RuntimeError("HELIUS_API_KEY missing from environment")

        self.ws_url = _env("HELIUS_RPC_WS", f"wss://mainnet.helius-rpc.com/?api-key={api_key}")

        if websocket is None:
            raise RuntimeError(
                "websocket-client not installed. Install with:\n"
                "  pip install websocket-client\n"
            )

        # mint -> deque[timestamps]
        self._events: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=5000))

        # mint -> subscription_id
        self._subs: Dict[str, int] = {}

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ws = None
        self._thread = None

        self._next_id = 1

    # -------------------------
    # LIFECYCLE
    # -------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[HELIUS][WS] connected endpoint: {self.ws_url}")

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        print("[HELIUS][WS] stopped")

    # -------------------------
    # SUBSCRIBE / UNSUBSCRIBE
    # -------------------------
    def subscribe(self, mint: str) -> None:
        mint = str(mint)
        with self._lock:
            if mint in self._subs:
                return
        self._send_logs_subscribe(mint)

    def unsubscribe(self, mint: str) -> None:
        mint = str(mint)
        with self._lock:
            sub_id = self._subs.get(mint)
        if not sub_id:
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "logsUnsubscribe",
                "params": [sub_id],
            }
        )
        with self._lock:
            self._subs.pop(mint, None)
            self._events.pop(mint, None)

    # -------------------------
    # METRICS
    # -------------------------
    def events_per_sec(self, mint: str, window_sec: int = 20) -> float:
        mint = str(mint)
        now = time.time()
        cutoff = now - float(window_sec)

        with self._lock:
            dq = self._events.get(mint)
            if not dq:
                return 0.0
            # count events newer than cutoff
            n = 0
            for ts in reversed(dq):
                if ts < cutoff:
                    break
                n += 1
        return n / max(1.0, float(window_sec))

    def accel_ratio(self, mint: str, short_sec: int = 10, long_sec: int = 60) -> float:
        """
        Acceleration proxy:
            accel = EPS(short) / max(EPS(long), tiny)
        > 1 means speeding up, < 1 means slowing down/collapsing.
        """
        short_eps = self.events_per_sec(mint, window_sec=short_sec)
        long_eps = self.events_per_sec(mint, window_sec=long_sec)
        return short_eps / max(long_eps, 1e-9)

    def snapshot(self, mint: str) -> Tuple[float, float]:
        """
        Returns (eps_20s, accel_ratio_10v60)
        """
        eps = self.events_per_sec(mint, window_sec=int(_env("WS_EPS_WINDOW_SEC", "20")))
        accel = self.accel_ratio(
            mint,
            short_sec=int(_env("WS_ACCEL_SHORT_SEC", "10")),
            long_sec=int(_env("WS_ACCEL_LONG_SEC", "60")),
        )
        return eps, accel

    # -------------------------
    # INTERNAL WS LOOP
    # -------------------------
    def _run(self):
        fail_count = 0
        # Primary: Helius | Backup: Alchemy
        helius_url  = self.ws_url
        alchemy_key = _env("ALCHEMY_API_KEY", "")
        alchemy_url = f"wss://solana-mainnet.g.alchemy.com/v2/{alchemy_key}" if alchemy_key else None

        while not self._stop.is_set():
            # Switch to Alchemy after 3 consecutive Helius failures
            if fail_count >= 3 and alchemy_url:
                active_url = alchemy_url
                print(f"[HELIUS][WS] Switching to Alchemy backup (fail_count={fail_count})")
            else:
                active_url = helius_url

            try:
                self._ws = websocket.WebSocketApp(
                    active_url,
                    on_message=self._on_message,
                    on_open=self._on_open,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=60, ping_timeout=30, reconnect=5)
                fail_count = 0  # reset on clean disconnect
            except Exception as e:
                fail_count += 1
                print(f"[HELIUS][WS] reconnect error (attempt {fail_count}): {e}")
            # backoff
            time.sleep(2)

    def _on_open(self, ws):
        # re-subscribe existing mints on reconnect
        with self._lock:
            mints = list(self._subs.keys())
            self._subs = {}  # reset, we need fresh sub ids
        for m in mints:
            self._send_logs_subscribe(m)

    def _on_close(self, ws, *args):
        pass

    def _on_error(self, ws, err):
        # don’t spam
        print(f"[HELIUS][WS] error: {err}")

    def _on_message(self, ws, msg: str):
        try:
            j = json.loads(msg)
        except Exception:
            return

        # Subscription response:
        # {"jsonrpc":"2.0","result":123,"id":N}
        if "result" in j and "id" in j and "method" not in j:
            # we match by pending id stored inside the payload we sent? we don’t track id->mint
            # So instead: when we send subscribe we include "id" and store a mapping.
            # For simplicity: handled in _send_logs_subscribe() via _pending map.
            self._handle_subscribe_result(j)
            return

        # Notifications:
        # {"jsonrpc":"2.0","method":"logsNotification","params":{"result":...,"subscription":123}}
        if j.get("method") == "logsNotification":
            params = j.get("params") or {}
            sub_id = params.get("subscription")
            if not sub_id:
                return
            mint = self._mint_by_sub_id(sub_id)
            if not mint:
                return
            with self._lock:
                self._events[mint].append(time.time())

    # -------------------------
    # SUBSCRIBE HELPERS
    # -------------------------
    def _send_logs_subscribe(self, mint: str) -> None:
        # logsSubscribe with mentions filter:
        # params: [{"mentions":[<address>]}, {"commitment":"processed"}]
        rpc_id = self._next_rpc_id()
        with self._lock:
            if not hasattr(self, "_pending"):
                self._pending = {}  # type: ignore
            self._pending[rpc_id] = mint  # type: ignore

        self._send(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [mint]},
                    {"commitment": "processed"},
                ],
            }
        )

    def _handle_subscribe_result(self, j: dict) -> None:
        rpc_id = j.get("id")
        sub_id = j.get("result")
        if rpc_id is None or sub_id is None:
            return
        with self._lock:
            mint = getattr(self, "_pending", {}).pop(rpc_id, None)
            if not mint:
                return
            self._subs[mint] = int(sub_id)

    def _mint_by_sub_id(self, sub_id: int) -> Optional[str]:
        with self._lock:
            for mint, sid in self._subs.items():
                if sid == sub_id:
                    return mint
        return None

    def _send(self, payload: dict) -> None:
        try:
            if self._ws and self._ws.sock and self._ws.sock.connected:
                self._ws.send(json.dumps(payload))
        except Exception:
            pass

    def _next_rpc_id(self) -> int:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        return rid
    
        def seconds_since_activity(self, mint: str) -> Optional[int]:
         """
         Returns seconds since last on-chain activity seen for this mint.
         Used for decay / dead-token detection.
         """
         mint = str(mint)
         with self._lock:
            dq = self._events.get(mint)
            if not dq:
                return None
            last_ts = dq[-1]
        return max(0, int(time.time() - last_ts))
