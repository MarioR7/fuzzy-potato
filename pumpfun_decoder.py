# onchain/pumpfun_decoder.py
from __future__ import annotations

import base64
import struct
from datetime import datetime

from onchain.filters import pumpfun_filters


# -------------------------------
# Helpers
# -------------------------------

def _read_u32_le(b: bytes, off: int) -> tuple[int, int]:
    if off + 4 > len(b):
        raise ValueError("u32 out of range")
    return struct.unpack_from("<I", b, off)[0], off + 4


def _read_string(b: bytes, off: int) -> tuple[str, int]:
    n, off = _read_u32_le(b, off)
    if off + n > len(b):
        raise ValueError("string out of range")
    s = b[off: off + n].decode("utf-8", errors="replace")
    return s, off + n


def _read_pubkey(b: bytes, off: int) -> tuple[str, int]:
    if off + 32 > len(b):
        raise ValueError("pubkey out of range")
    raw = b[off: off + 32]
    try:
        from solders.pubkey import Pubkey  # type: ignore
        return str(Pubkey.from_bytes(raw)), off + 32
    except Exception:
        return raw.hex(), off + 32


def _find_all_program_data(logs: list[str]) -> bytes | None:
    chunks = []
    for line in logs or []:
        if "program data:" in line.lower():
            parts = line.split("Program data:", 1)
            if len(parts) == 2:
                chunks.append(parts[1].strip())

    if not chunks:
        return None

    try:
        return b"".join(base64.b64decode(c) for c in chunks)
    except Exception:
        return None


# -------------------------------
# Main decoder
# -------------------------------

def decode_create_from_logs(signature: str, logs: list[str]) -> dict | None:
    raw = _find_all_program_data(logs)
    if not raw:
        return None

    # Pump.fun discriminator (8 bytes)
    if len(raw) > 8:
        raw = raw[8:]

    try:
        off = 0
        name, off = _read_string(raw, off)
        symbol, off = _read_string(raw, off)
        uri, off = _read_string(raw, off)
        mint, off = _read_pubkey(raw, off)
        bonding_curve, off = _read_pubkey(raw, off)
        creator, off = _read_pubkey(raw, off)

        # -------------------------------
        # APPLY FILTERS
        # -------------------------------
        ok, risk, reasons = pumpfun_filters(
            creator=creator,
            name=name,
            symbol=symbol,
            uri=uri,
        )

        if not ok:
            print("\n🛑 FILTERED (PUMP.FUN)")
            print("Name    :", name)
            print("Symbol  :", symbol)
            print("Creator :", creator)
            print("Risk    :", risk)
            print("Reasons :", reasons)
            print("-" * 60)
            return None

        # -------------------------------
        # PASSED FILTERS
        # -------------------------------
        print("\n🚀 PUMP.FUN TOKEN (PASSED FILTERS)")
        print("Sig     :", signature)
        print("Name    :", name)
        print("Symbol  :", symbol)
        print("URI     :", uri)
        print("Mint    :", mint)
        print("Creator :", creator)
        print("Risk    :", risk)
        print("Time    :", datetime.utcnow().isoformat(), "UTC")
        print("-" * 60)

        return {
            "signature": signature,
            "name": name,
            "symbol": symbol,
            "uri": uri,
            "mint": mint,
            "creator": creator,
            "risk": risk,
        }

    except Exception:
        # fallback: still useful for tx-based follow-up
        print("\n🚀 PUMP.FUN CREATE (RAW, PARTIAL)")
        print("Sig:", signature)
        print("⚠️ Decode failed but CREATE confirmed")
        print("-" * 60)
        return {
            "signature": signature,
        }
