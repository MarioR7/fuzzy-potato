# onchain/decoder.py
import os
import requests
from dotenv import load_dotenv

from onchain.filters import evaluate_token
from onchain.locks import can_buy, lock_buy
from onchain.risk import can_open_new_trade, record_new_trade, BUY_SOL_PER_TRADE
from onchain.jupiter_swap import buy_with_sol, WSOL_MINT
from onchain.monitor import start_monitor

load_dotenv(".env")

HELIUS_KEY = os.getenv("HELIUS_API_KEY")
RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"

RAYDIUM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

WALLET_PUBKEY = os.getenv("WALLET_PUBKEY")
if not WALLET_PUBKEY:
    raise RuntimeError("❌ WALLET_PUBKEY missing in .env")

SEEN_POOLS = set()


def process_signature(sig: str):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    }

    try:
        r = requests.post(RPC, json=payload, timeout=10).json()
    except Exception as e:
        print("❌ RPC error:", e)
        return

    tx = r.get("result")
    if not tx:
        return

    instructions = tx["transaction"]["message"].get("instructions", [])

    for ix in instructions:
        if ix.get("programId") != RAYDIUM:
            continue

        accounts = ix.get("accounts", [])
        if len(accounts) < 10:
            continue

        # 🔥 Raydium Initialize2 layout heuristic
        amm_id = accounts[4]
        base_mint = accounts[8]
        quote_mint = accounts[9]

        # only /SOL pools
        if quote_mint != WSOL_MINT:
            continue
        if base_mint == WSOL_MINT:
            continue

        if amm_id in SEEN_POOLS:
            continue
        SEEN_POOLS.add(amm_id)

        print("\n🔥 NEW RAYDIUM /SOL POOL")
        print("AMM  :", amm_id)
        print("Base :", base_mint)

        # risk gate
        if not can_open_new_trade():
            print("🛑 Risk gate hit — skipping")
            print("-" * 60)
            return

        # safety filters
        ok, reasons, details = evaluate_token(
            base_mint,
            require_mint_authority_disabled=True,
            require_freeze_disabled=True,
            max_top1_pct=25.0,
            max_top5_pct=60.0,
        )

        if not ok:
            print("🛑 FILTER FAILED")
            for r in reasons:
                print(f"   ❌ {r}")
            print("-" * 60)
            return

        if not can_buy(amm_id):
            print("🔒 Already bought — skipping")
            print("-" * 60)
            return

        lock_buy(amm_id)
        record_new_trade()

        entry_lamports = int(BUY_SOL_PER_TRADE * 1_000_000_000)

        print(f"💰 BUYING {BUY_SOL_PER_TRADE:.2f} SOL")
        res = buy_with_sol(base_mint, BUY_SOL_PER_TRADE, slippage_bps=500)
        print("✅ Buy tx:", res["signature"])

        print("👀 Starting monitor (TP / SL / rug)")
        start_monitor(
            owner_pubkey=WALLET_PUBKEY,
            token_mint=base_mint,
            entry_sol_lamports=entry_lamports,
        )

        print("🚀 Trade live")
        print("-" * 60)
        return
