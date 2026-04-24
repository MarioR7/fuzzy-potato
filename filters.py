# onchain/filters.py
import os
import requests
from dotenv import load_dotenv

load_dotenv(".env")

RPC = "https://mainnet.helius-rpc.com/?api-key=" + os.getenv("HELIUS_API_KEY")


def _rpc(method: str, params: list, timeout: int = 10):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    r = requests.post(RPC, json=payload, timeout=timeout)
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j.get("result")


def get_mint_info(mint: str) -> dict | None:
    """
    Returns parsed mint info:
    - decimals
    - supply (raw int)
    - mintAuthority (str|None)
    - freezeAuthority (str|None)
    """
    try:
        res = _rpc(
            "getAccountInfo",
            [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
    except Exception:
        return None

    val = (res or {}).get("value")
    if not val:
        return None

    data = val.get("data")
    if not data or not isinstance(data, dict):
        return None

    parsed = data.get("parsed", {})
    if parsed.get("type") != "mint":
        return None

    info = parsed.get("info", {})
    supply_raw = int(info.get("supply", "0"))
    decimals = int(info.get("decimals", 0))

    return {
        "decimals": decimals,
        "supply_raw": supply_raw,
        "mintAuthority": info.get("mintAuthority"),
        "freezeAuthority": info.get("freezeAuthority"),
        "isInitialized": info.get("isInitialized", False),
    }


def get_largest_holders(mint: str, top_n: int = 10) -> list[dict]:
    """
    Returns token largest accounts with uiAmount.
    NOTE: top holders include LP / vaults sometimes (so treat as a signal, not absolute truth).
    """
    res = _rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    out = []
    for row in (res or {}).get("value", [])[:top_n]:
        out.append(
            {
                "address": row.get("address"),
                "amount": row.get("amount"),  # raw string
                "uiAmount": row.get("uiAmount"),
                "decimals": row.get("decimals"),
            }
        )
    return out


def evaluate_token(
    mint: str,
    *,
    require_mint_authority_disabled: bool = True,
    require_freeze_disabled: bool = True,
    min_supply_raw: int = 1,              # must exist
    max_supply_raw: int = 10**18,         # sanity cap (adjust as you want)
    max_top1_pct: float = 25.0,           # whale check (rough)
    max_top5_pct: float = 60.0,           # whale check (rough)
    allowed_decimals: set[int] = {0, 6, 9},
) -> tuple[bool, list[str], dict]:
    """
    Returns (pass, reasons, details)
    """
    reasons = []
    details = {}

    mi = get_mint_info(mint)
    if not mi:
        reasons.append("could not fetch mint info (not a mint / not indexed yet)")
        return False, reasons, details

    details.update(mi)

    if not mi.get("isInitialized"):
        reasons.append("mint not initialized")
    if mi["decimals"] not in allowed_decimals:
        reasons.append(f"decimals not allowed ({mi['decimals']})")

    if mi["supply_raw"] < min_supply_raw:
        reasons.append("supply is zero / not minted yet")
    if mi["supply_raw"] > max_supply_raw:
        reasons.append("supply too large (sanity cap)")

    if require_mint_authority_disabled and mi.get("mintAuthority"):
        reasons.append("mint authority STILL ENABLED (can mint infinite)")
    if require_freeze_disabled and mi.get("freezeAuthority"):
        reasons.append("freeze authority STILL ENABLED (can freeze wallets)")

    # Whale concentration (approx)
    try:
        largest = get_largest_holders(mint, top_n=10)
        details["largest"] = largest

        supply = mi["supply_raw"]
        if supply > 0 and largest:
            # largest[i]["amount"] is raw string, same decimals as mint
            amounts = [int(x["amount"]) for x in largest if x.get("amount")]
            top1 = amounts[0] if len(amounts) >= 1 else 0
            top5 = sum(amounts[:5]) if len(amounts) >= 5 else sum(amounts)

            top1_pct = (top1 / supply) * 100
            top5_pct = (top5 / supply) * 100

            details["top1_pct"] = top1_pct
            details["top5_pct"] = top5_pct

            if top1_pct > max_top1_pct:
                reasons.append(f"top1 holder too large ({top1_pct:.2f}%)")
            if top5_pct > max_top5_pct:
                reasons.append(f"top5 holders too large ({top5_pct:.2f}%)")
    except Exception:
        # Don't hard-fail if this RPC sometimes flaps
        reasons.append("could not evaluate holders (rpc / indexing)")

    passed = len(reasons) == 0
    return passed, reasons, details


# =========================================================
# Pump.fun filters (EARLY-STAGE)
# =========================================================

import time
from collections import defaultdict

_CREATOR_HISTORY = defaultdict(list)

# soft blacklist (WEAK signal)
NAME_BLACKLIST = {
    "elon", "musk", "trump", "biden", "pepe",
    "doge", "shib", "inu", "ai", "gpt",
    "coin", "token", "official", "v2",
}

def pumpfun_filters(
    *,
    creator: str,
    name: str,
    symbol: str,
    uri: str,
    max_per_10min: int = 2,
    max_per_hour: int = 5,
):
    """
    Pump.fun early-stage filter.
    Returns (ok: bool, risk: int, reasons: list[str])
    """

    reasons = []
    risk = 0
    now = time.time()

    # ---- creator spam (STRONG) ----
    history = _CREATOR_HISTORY[creator]
    history.append(now)

    # keep only last hour
    history[:] = [t for t in history if now - t < 3600]

    if sum(1 for t in history if now - t < 600) > max_per_10min:
        reasons.append("creator_spam_10min")
        risk += 2

    if len(history) > max_per_hour:
        reasons.append("creator_spam_1h")
        risk += 3

    # ---- name / symbol heuristic (WEAK) ----
    lname = (name or "").lower()
    lsymbol = (symbol or "").lower()

    for bad in NAME_BLACKLIST:
        if bad in lname or bad in lsymbol:
            reasons.append(f"name_contains:{bad}")
            risk += 1
            break

    # ---- metadata sanity ----
    if not name or len(name) < 2:
        reasons.append("invalid_name")
        risk += 2

    if not symbol or len(symbol) < 2:
        reasons.append("invalid_symbol")
        risk += 2

    if not uri or not uri.startswith("http"):
        reasons.append("invalid_uri")
        risk += 2

    # ---- decision ----
    ok = risk < 5
    return ok, risk, reasons
