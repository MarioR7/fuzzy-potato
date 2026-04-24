# onchain/locks.py

MAX_SOL_PER_BUY = 0.10
BOUGHT_POOLS = set()

def can_buy(amm_id: str) -> bool:
    return amm_id not in BOUGHT_POOLS

def lock_buy(amm_id: str):
    BOUGHT_POOLS.add(amm_id)
