# onchain/ws_pumpfun_discovery.py
from __future__ import annotations

import os
import json
import threading
import time
from queue import Queue
from typing import Callable

import websocket

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


class PumpFunWSDiscovery:
    """
    Real-time Pump.fun mint discovery using Helius WS.
    Pushes tx signatures into a queue.
    """

    def __init__(self, out_queue: Queue):
        api_key = os.getenv("HELIUS_API_KEY")
        if not api_key:
            raise RuntimeError("HELIUS_API_KEY missing")

        self.ws_url = f"wss://mainnet.helius-rpc.com/?api-key={api_key}"
        self.queue = out_queue
        self._ws = None
        self._thread = None
        self._stop = False

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("[WS][DISCOVERY] Pump.fun WS started")

    def stop(self):
        self._stop = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _run(self):
        def on_open(ws):
            sub = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [PUMPFUN_PROGRAM_ID]},
                    {"commitment": "processed"},
                ],
            }
            ws.send(json.dumps(sub))

        def on_message(ws, msg):
            try:
                j = json.loads(msg)
            except Exception:
                return

            val = j.get("params", {}).get("result", {}).get("value", {})
            sig = val.get("signature")
            logs = val.get("logs", [])

            if not sig:
                return

            for line in logs:
                if "Create" in line or "initialize" in line.lower():
                    self.queue.put(sig)
                    break

        while not self._stop:
            try:
                self._ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=on_open,
                    on_message=on_message,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"[WS][DISCOVERY] reconnect error: {e}")
                time.sleep(2)
