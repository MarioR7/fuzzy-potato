import asyncio
import os
import json
import websockets

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WS_URL = "wss://mainnet.helius-rpc.com/?api-key=" + os.getenv("HELIUS_API_KEY")

async def pumpfun_discovery(on_mint):
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMPFUN_PROGRAM_ID]},
                {"commitment": "processed"}
            ]
        }))

        print("[WS][DISCOVERY] Pump.fun mint stream live")

        while True:
            msg = json.loads(await ws.recv())
            val = msg.get("params", {}).get("result", {}).get("value", {})
            logs = val.get("logs", [])
            sig = val.get("signature")

            for line in logs:
                if "Create" in line or "initialize" in line.lower():
                    # Fetch tx → extract mint
                    on_mint(sig)

PUMPFUN_MIGRATION_AUTH = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"

async def pumpfun_migration(on_migration):
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [PUMPFUN_MIGRATION_AUTH]},
                {"commitment": "processed"}
            ]
        }))

        print("[WS][MIGRATION] Pump.fun → Raydium watch live")

        while True:
            msg = json.loads(await ws.recv())
            sig = msg["params"]["result"]["value"]["signature"]
            on_migration(sig)


RAYDIUM_AMM_PROGRAM = "RVKd61ztZW9Yz3xS6sQDQeR8y1YWMaJ8mB6r56kD2Z"

async def raydium_activity(on_swap):
    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [RAYDIUM_AMM_PROGRAM]},
                {"commitment": "processed"}
            ]
        }))

        print("[WS][RAYDIUM] AMM activity live")

        while True:
            msg = json.loads(await ws.recv())
            on_swap(msg)
