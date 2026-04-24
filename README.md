# Solana Memecoin Token Screener 🚀

A real-time Solana token discovery and screening bot powered by **Mobula API**.

## What it does

- Monitors PumpFun and PumpSwap for new token graduations
- Uses **Mobula API** to enrich tokens with real on-chain data:
  - Live liquidity (`liquidityUSD`)
  - Market cap and price
  - Top holder distribution
  - Bundler/sniper detection
  - Pro trader volume
- Filters low quality tokens automatically
- Logs trade signals with Mobula-sourced metrics

## Mobula API Usage

This project uses the [Mobula API](https://mobula.io) for:
- `/token/price` — real-time price and liquidity data
- `/token/security` — holder concentration and rug detection
- Token metadata enrichment for graduation events

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Add your MOBULA_API_KEY to .env
python main.py
```

## Stack

- Python 3.11
- Solana Web3 / Helius RPC
- Mobula API (real-time token data)
- SQLite (trade logging)
