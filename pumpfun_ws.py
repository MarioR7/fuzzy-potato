# onchain/pumpfun_ws.py
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

import websocket
import requests
import os


PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


class PumpFunWS:
    """
    Real Pump.fun WS discovery:
    - listens to pump.fun program logs
    - detects token creation
    - fetches tx
    - extracts REAL mint
    """

    def __init__(self, on_mint: Callable[[str], None]):
        self.on_mint = on_mint
        self.ws_url = f"wss://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY')}"
        self.http_rpc = f"https://rpc.helius.xyz/?api-key={os.getenv('HELIUS_API_KEY')}"

        self._ws: Optional[websocket.WebSocketApp] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rpc_id = 1

    # -------------------------
    # LIFECYCLE
    # -------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("[PUMPFUN][WS] started")

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        print("[PUMPFUN][WS] stopped")

    # -------------------------
    # WS LOOP
    # -------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"[PUMPFUN][WS] reconnect error: {e}")
            time.sleep(2)

    def _on_open(self, ws):
        sub = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMPFUN_PROGRAM_ID]},
                {"commitment": "processed"},
            ],
        }
        ws.send(json.dumps(sub))
        print("[PUMPFUN][WS] subscribed to pump.fun program")

    def _on_error(self, ws, err):
        print(f"[PUMPFUN][WS] error: {err}")

    def _on_message(self, ws, msg: str):
        try:
            j = json.loads(msg)
        except Exception:
            return

        if j.get("method") != "logsNotification":
            return

        val = j["params"]["result"]["value"]
        logs = val.get("logs", [])
        sig = val.get("signature")

        # Heuristic: pump.fun create logs always contain these
        if not any("Create" in l or "initialize" in l.lower() for l in logs):
            return

        mint = self._extract_mint_from_tx(sig)
        if mint:
            print(f"[PUMPFUN] NEW TOKEN → {mint}")
            self.on_mint(mint)

    # -------------------------
    # TX PARSER
    # -------------------------
    def _extract_mint_from_tx(self, sig: str) -> Optional[str]:
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [sig, {"encoding": "jsonParsed"}],
            }
            r = requests.post(self.http_rpc, json=payload, timeout=10).json()
            tx = r.get("result")
            if not tx:
                return None

            # Pump.fun mint is almost always the FIRST minted SPL token
            for ix in tx["transaction"]["message"]["instructions"]:
                info = ix.get("parsed", {}).get("info", {})
                mint = info.get("mint")
                if mint:
                    return str(mint)

        except Exception:
            pass

        return None

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id
