# onchain/wallets.py
import os
import base58
from solders.keypair import Keypair

def load_wallet():
    secret_b58 = os.getenv("PHANTOM_PRIV_B58")
    if not secret_b58:
        raise RuntimeError("PHANTOM_PRIV_B58 not set")

    secret_bytes = base58.b58decode(secret_b58)
    return Keypair.from_bytes(secret_bytes)
