# onchain/pumpfun_listener.py
import os
import json
import asyncio
import websockets
from dotenv import load_dotenv

from onchain.pumpfun_decoder import decode_create_from_logs

load_dotenv(".env")

HELIUS_KEY = os.getenv("HELIUS_API_KEY")
if not HELIUS_KEY:
    raise RuntimeError("❌ HELIUS_API_KEY missing")

WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"

# Pump.fun CORE program
PUMPFUN_CORE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def has_create(logs: list[str]) -> bool:
    for line in logs or []:
        if "instruction: create" in line.lower():
            return True
    return False


async def subscribe(ws):
    # logsSubscribe filter “mentions” matches transactions where this pubkey is in accountKeys
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [PUMPFUN_CORE]},
            {"commitment": "processed"},
        ],
    }
    await ws.send(json.dumps(req))


async def ping_loop(ws):
    while True:
        try:
            await ws.ping()
        except Exception:
            return
        await asyncio.sleep(20)


async def handle_messages(ws):
    while True:
        msg = await ws.recv()
        data = json.loads(msg)

        if "result" in data and data.get("id") == 1:
            print("🔥 Pump.fun logsSubscribe active (watching Create)")
            continue

        if data.get("method") != "logsNotification":
            continue

        value = data["params"]["result"]["value"]
        err = value.get("err")
        logs = value.get("logs", [])
        sig = value.get("signature")

        if err is not None or not sig:
            continue

        # Only act on Create events (this is the reliable signal)
        if not has_create(logs):
            continue

        # Decode “Program data:” from logs (no getTransaction needed)
        decoded = decode_create_from_logs(sig, logs)
        if not decoded:
            # Keep this line while debugging. If it’s noisy later, remove it.
            print(f"⚠️ Create seen but could not decode Program data (sig={sig})")
            continue


async def main():
    backoff = 1
    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=None,
                max_queue=4096,
            ) as ws:
                backoff = 1
                await subscribe(ws)
                await asyncio.gather(
                    ping_loop(ws),
                    handle_messages(ws),
                )
        except Exception as e:
            print(f"[PUMP WS] disconnected: {e}. Reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    asyncio.run(main())
