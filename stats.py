from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import List

DB_PATH = Path("data/trades.db")

BASELINE_USD = float(os.getenv("SIM_START_BALANCE_USD", "10.83"))


def get_last_run_id(cur) -> str | None:
    cur.execute(
        "SELECT run_id FROM trades WHERE run_id IS NOT NULL ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    return row[0] if row else None


def load_run_trades(cur, run_id: str) -> List[sqlite3.Row]:
    cur.execute(
        """
        SELECT
            id,
            mint,
            reason,
            pnl
        FROM trades
        WHERE run_id = ?
        ORDER BY id ASC
        """,
        (run_id,),
    )
    return cur.fetchall()


def main() -> None:
    print("\n=== AUTONOMOUS ULTRA AUTO-RUNNER STATS (PHASE 1) ===\n")

    if not DB_PATH.exists():
        print("No database found.")
        return

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    run_id = get_last_run_id(cur)
    if not run_id:
        print("No runs found.")
        return

    trades = load_run_trades(cur, run_id)
    total = len(trades)

    if total == 0:
        print("No trades in last run.")
        return

    pnls = [float(t["pnl"] or 0.0) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    total_pnl = sum(pnls)
    expectancy = total_pnl / total
    balance_equity = BASELINE_USD + total_pnl

    # --------------------------------------------------
    # HEADER
    # --------------------------------------------------
    print(f"Run ID: {run_id}")
    print(f"Total Trades: {total}")
    print(f"Win Rate: {(wins / total * 100):.2f}%")
    print(f"Balance Equity: ${balance_equity:.2f} USD\n")

    # --------------------------------------------------
    # PNL SUMMARY
    # --------------------------------------------------
    print("PnL Summary:")
    print(f"  Avg PnL:   {expectancy:+.2f} USD paper")
    print(f"  Total PnL: {total_pnl:+.2f} USD paper\n")

    print("Expectancy:")
    print(f"  Per Trade: {expectancy:+.2f} USD paper\n")

    best_trade = max(trades, key=lambda t: float(t["pnl"] or 0))
    worst_trade = min(trades, key=lambda t: float(t["pnl"] or 0))

    print("Best Trade:")
    print(f"  {best_trade['mint']} | pnl={best_trade['pnl']:+.2f} USD\n")

    print("Worst Trade:")
    print(f"  {worst_trade['mint']} | pnl={worst_trade['pnl']:+.2f} USD\n")

    # --------------------------------------------------
    # EQUITY CURVE
    # --------------------------------------------------
    print(f"Equity Curve (Last {min(4, total)}):")
    running = BASELINE_USD
    for t in trades[-4:]:
        running += float(t["pnl"] or 0.0)
        print(
            f"  {t['mint']} | pnl={t['pnl']:+.2f} USD | equity=${running:.2f} USD | reason={t['reason']}"
        )

    con.close()


if __name__ == "__main__":
    main()
