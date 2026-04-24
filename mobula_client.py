"""
utils/mobula_client.py
========================
Mobula API client for token enrichment and security.
Replaces broken GMGN token info (403 errors).
"""
import requests
import time
from typing import Optional, Dict

MOBULA_API_KEY = "YOUR_NEW_KEY"
BASE_URL = "https://api.mobula.io/api/2"

class MobulaClient:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"Authorization": MOBULA_API_KEY})
        self._last_call = 0.0
        self._min_interval = 0.5
        print("[MOBULA] Client ready")

    def _get(self, endpoint, params, timeout=3):
        now = time.time()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()
        try:
            r = self._session.get(
                BASE_URL + endpoint,
                params=params,
                timeout=timeout
            )
            if r.status_code == 200:
                return r.json().get("data")
            return None
        except Exception as e:
            print("[MOBULA] Error " + endpoint + ": " + str(e))
            return None

    def get_security(self, mint):
        data = self._get("/token/security", {
            "address": mint,
            "blockchain": "solana"
        })
        if not data:
            return None
        return {
            "is_mintable":    data.get("isMintable", False),
            "is_freezable":   data.get("isFreezable", False),
            "top10_holders":  data.get("top10HoldingsPercentage", 0),
            "top50_holders":  data.get("top50HoldingsPercentage", 0),
            "is_honeypot":    data.get("isHoneypot", False),
            "is_launchpad":   data.get("isLaunchpadToken", False),
            "buy_fee":        data.get("buyFeePercentage", 0),
            "sell_fee":       data.get("sellFeePercentage", 0),
            "liq_burn_pct":   data.get("liquidityBurnPercentage", 0),
            "pro_trader_vol": data.get("proTraderVolume24hPercentage", 0),
        }

    def get_price(self, mint):
        data = self._get("/token/price", {
            "address": mint,
            "blockchain": "solana"
        })
        if not data:
            return None
        # Use explicit None checks — 0 is valid, missing is unknown
        raw = data.get(mint) or data  # v2 returns {address: {...}}
        fdv = raw.get("marketCapDilutedUSD")
        liq = raw.get("liquidityUSD")
        mcap = raw.get("marketCapUSD")
        return {
            "price_usd":   raw.get("priceUSD"),
            "liq_usd":     liq,
            "mcap_usd":    mcap,
            "fdv":         fdv,
            "fdv_known":   fdv is not None,
            "liq_known":   liq is not None,
            "liq_to_fdv":  (liq / fdv) if (fdv and fdv > 0 and liq is not None) else None,
            "symbol":      raw.get("symbol", ""),
            "name":        raw.get("name", ""),
        }

mobula = MobulaClient()
