# onchain/enricher.py
import time
import requests

DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"


def _pick_best_pair(pairs: list[dict]) -> dict | None:
    """
    Pick the best Raydium pair by liquidity (USD). If liquidity missing,
    fallback to highest volume24h.
    """
    if not pairs:
        return None

    def liquidity_usd(p: dict) -> float:
        liq = (p.get("liquidity") or {}).get("usd")
        try:
            return float(liq or 0)
        except Exception:
            return 0.0

    def volume24h(p: dict) -> float:
        vol = (p.get("volume") or {}).get("h24")
        try:
            return float(vol or 0)
        except Exception:
            return 0.0

    # Prefer Raydium pairs if present
    raydium_pairs = [p for p in pairs if (p.get("dexId") or "").lower() == "raydium"]
    candidates = raydium_pairs if raydium_pairs else pairs

    # Sort by liquidity first, then 24h volume
    candidates.sort(key=lambda p: (liquidity_usd(p), volume24h(p)), reverse=True)
    return candidates[0]


def fetch_dexscreener_token(mint: str, timeout: int = 8) -> dict | None:
    """
    Calls DexScreener token endpoint.
    Returns parsed json or None on failure.
    """
    url = DEX_TOKEN_URL.format(mint=mint)
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def wait_for_dexscreener_index(
    mint: str,
    max_wait_seconds: int = 120,
    first_delay: float = 2.0,
    max_delay: float = 15.0,
    require_liquidity_usd: float = 50.0,  # minimum liquidity to consider "live"
) -> dict | None:
    """
    Poll DexScreener until it returns at least 1 pair AND liquidity is present.

    Returns:
      - best_pair dict when indexed and liquidity meets threshold
      - None if timed out
    """
    start = time.time()
    delay = first_delay
    attempt = 0

    while True:
        attempt += 1
        data = fetch_dexscreener_token(mint)
        pairs = (data or {}).get("pairs") or []

        if pairs:
            best = _pick_best_pair(pairs)
            liq_usd = ((best.get("liquidity") or {}).get("usd") if best else None) or 0
            try:
                liq_usd = float(liq_usd)
            except Exception:
                liq_usd = 0.0

            if best and liq_usd >= require_liquidity_usd:
                # ✅ indexed + liquidity is real
                return best

        elapsed = time.time() - start
        if elapsed >= max_wait_seconds:
            return None

        # Print a clean “still waiting” line
        if attempt == 1:
            print("⏳ DexScreener: no data yet (token is fresh)")
            print("   → waiting for indexer (expected)")
        else:
            print(f"⏳ DexScreener: still waiting... ({int(elapsed)}s)")

        time.sleep(delay)
        delay = min(max_delay, delay * 1.4)  # gentle backoff


def enrich(base_mint: str):
    """
    Step 3 enricher:
    - Waits for DexScreener index
    - Prints key info once live
    """
    print(f"🔎 Enriching token: {base_mint}")

    best_pair = wait_for_dexscreener_index(
        base_mint,
        max_wait_seconds=120,
        first_delay=2.0,
        max_delay=15.0,
        require_liquidity_usd=50.0,
    )

    if not best_pair:
        print("🧊 DexScreener: timed out — falling back to Mobula API")
        try:
            from mobula_client import mobula
            price_data = mobula.get_price(mint)
            security_data = mobula.get_security(mint)
            if price_data and price_data.get("liq_usd"):
                print(f"[MOBULA] Fallback enrichment | "
                      f"price=${price_data.get('price_usd', 0):.8f} | "
                      f"liq=${price_data.get('liq_usd', 0):,.0f} | "
                      f"top10={security_data.get('top10_holders', 0) if security_data else 0:.1f}%")
                return {
                    "price_usd": price_data.get("price_usd"),
                    "liq_usd": price_data.get("liq_usd"),
                    "mcap_usd": price_data.get("mcap_usd"),
                    "top10_holders": security_data.get("top10_holders") if security_data else None,
                    "is_honeypot": security_data.get("is_honeypot") if security_data else None,
                    "source": "mobula",
                }
        except Exception as e:
            print(f"[MOBULA] Fallback failed: {e}")
        return

    # ✅ Once live, print useful sniper info
    dex = best_pair.get("dexId")
    pair_addr = best_pair.get("pairAddress")
    price_usd = best_pair.get("priceUsd")
    liq_usd = (best_pair.get("liquidity") or {}).get("usd")
    fdv = best_pair.get("fdv")  # sometimes present
    mc = best_pair.get("marketCap")  # sometimes present
    vol24h = (best_pair.get("volume") or {}).get("h24")
    buys = ((best_pair.get("txns") or {}).get("h1") or {}).get("buys")
    sells = ((best_pair.get("txns") or {}).get("h1") or {}).get("sells")

    print("✅ DexScreener: indexed + liquidity detected")
    print("   dex      :", dex)
    print("   pair     :", pair_addr)
    print("   priceUsd :", price_usd)
    print("   liqUsd   :", liq_usd)
    if fdv is not None:
        print("   fdv      :", fdv)
    if mc is not None:
        print("   mcap     :", mc)
    if vol24h is not None:
        print("   vol24h   :", vol24h)
    if buys is not None or sells is not None:
        print("   h1 txns  :", f"buys={buys} sells={sells}")
