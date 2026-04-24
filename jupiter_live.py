from __future__ import annotations

import os
import base64
import json
import time
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Any

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.api import Client

SOL_MINT = "So11111111111111111111111111111111111111112"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _rpc_client() -> Client:
    rpc = _env("HELIUS_HTTP_RPC") or _env("HELIUS_RPC") or _env("RPC_URL")
    if not rpc:
        api = _env("HELIUS_API_KEY")
        if not api:
            raise RuntimeError(
                "Missing RPC: set HELIUS_HTTP_RPC or HELIUS_RPC or RPC_URL (or HELIUS_API_KEY)."
            )
        rpc = f"https://rpc.helius.xyz/?api-key={api}"
    return Client(rpc)


def _load_keypair() -> Keypair:
    """
    Expect one of:
      - PHANTOM_PRIVATE_KEY_BASE58  (recommended)
      - SOLANA_PRIVATE_KEY_BASE58
    """
    b58 = _env("PHANTOM_PRIVATE_KEY_BASE58") or _env("SOLANA_PRIVATE_KEY_BASE58")
    if not b58:
        raise RuntimeError("Missing private key env: PHANTOM_PRIVATE_KEY_BASE58")
    return Keypair.from_base58_string(b58)


@dataclass
class ExecResult:
    ok: bool
    signature: Optional[str] = None
    err: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class JupiterLive:
    """
    Jupiter v6 execution + quoting
    """

    def __init__(self):
        self.session = requests.Session()
        self.rpc = _rpc_client()
        self.kp = _load_keypair()
        self.pubkey = str(self.kp.pubkey())

        self.slippage_bps = int(os.getenv("MAX_SLIPPAGE_BPS", "150"))
        self.priority_fee_lamports = int(os.getenv("PRIORITY_FEE_LAMPORTS", "0"))

        self.quote_url = _env("JUPITER_QUOTE_URL", "https://quote-api.jup.ag/v6/quote")
        self.swap_url = _env("JUPITER_SWAP_URL", "https://quote-api.jup.ag/v6/swap")

    # -------------------------
    # LOW-LEVEL QUOTE
    # -------------------------
    def _quote(self, input_mint: str, output_mint: str, amount: int) -> Dict[str, Any]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(self.slippage_bps),
            "onlyDirectRoutes": "false",
        }
        r = self.session.get(self.quote_url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    # -------------------------
    # PUBLIC QUOTE HELPERS (NEW)
    # -------------------------
    def quote_sol_to_token(self, out_mint: str, sol_amount: float) -> Dict[str, Any]:
        """
        Used by Engine to evaluate:
          - priceImpactPct
          - route existence
          - outAmount
        """
        lamports = int(sol_amount * 1_000_000_000)
        return self._quote(SOL_MINT, out_mint, lamports)

    def quote_token_to_sol(self, in_mint: str, token_amount_raw: int) -> Dict[str, Any]:
        """
        Quote token -> SOL (raw base units)
        """
        return self._quote(in_mint, SOL_MINT, int(token_amount_raw))

    # -------------------------
    # SWAP BUILDING
    # -------------------------
    def _swap_tx(self, quote: Dict[str, Any]) -> str:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
        }

        if self.priority_fee_lamports > 0:
            payload["prioritizationFeeLamports"] = self.priority_fee_lamports

        r = self.session.post(self.swap_url, json=payload, timeout=20)
        r.raise_for_status()
        j = r.json()
        tx = j.get("swapTransaction")
        if not tx:
            raise RuntimeError(f"Jupiter swapTransaction missing: {j}")
        return tx

    def _sign_and_send(self, tx_b64: str) -> str:
        raw = base64.b64decode(tx_b64)
        vt = VersionedTransaction.from_bytes(raw)

        msg_bytes = bytes(vt.message)
        sig = self.kp.sign_message(msg_bytes)

        signed = VersionedTransaction.populate(vt.message, [sig])
        tx_sig = self.rpc.send_raw_transaction(
            bytes(signed), opts={"skip_preflight": False}
        ).value
        return str(tx_sig)

    def _confirm(self, sig: str, timeout_sec: int = 35) -> bool:
        start = time.time()
        s = Signature.from_string(sig)
        while time.time() - start < timeout_sec:
            resp = self.rpc.get_signature_statuses([s]).value
            if resp and resp[0]:
                st = resp[0]
                if st.confirmation_status in ("confirmed", "finalized"):
                    return st.err is None
            time.sleep(0.7)
        return False

    # -------------------------
    # EXECUTION
    # -------------------------
    def buy_with_sol(self, out_mint: str, sol_amount: float) -> ExecResult:
        try:
            lamports = int(sol_amount * 1_000_000_000)
            quote = self._quote(SOL_MINT, out_mint, lamports)
            tx_b64 = self._swap_tx(quote)
            sig = self._sign_and_send(tx_b64)
            ok = self._confirm(sig)
            return ExecResult(ok=ok, signature=sig, err=None if ok else "confirm_failed", meta={"quote": quote})
        except Exception as e:
            return ExecResult(ok=False, err=str(e))

    def sell_to_sol(self, in_mint: str, token_amount_raw: int) -> ExecResult:
        try:
            quote = self._quote(in_mint, SOL_MINT, int(token_amount_raw))
            tx_b64 = self._swap_tx(quote)
            sig = self._sign_and_send(tx_b64)
            ok = self._confirm(sig)
            return ExecResult(ok=ok, signature=sig, err=None if ok else "confirm_failed", meta={"quote": quote})
        except Exception as e:
            return ExecResult(ok=False, err=str(e))
