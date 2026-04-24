from __future__ import annotations

import os
import sqlite3
import time
import json
from typing import Dict, Optional, List

DB_DIR = os.getenv("DB_DIR", "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(DB_DIR, "trades.db"))


# --------------------------------------------------
# DB CONNECTION
# --------------------------------------------------
def _connect() -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def _cols(cur: sqlite3.Cursor, table: str) -> List[str]:
    cur.execute(f"PRAGMA table_info({table});")
    return [r[1] for r in cur.fetchall()]


def _add_col_if_missing(cur: sqlite3.Cursor, table: str, col: str, col_def: str) -> None:
    if col not in _cols(cur, table):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def};")


# --------------------------------------------------
# INIT DB (SAFE MIGRATIONS ONLY)
# --------------------------------------------------
def init_db() -> None:
    conn = _connect()
    cur = conn.cursor()

    # ---- trades table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,

        mode TEXT,
        mint TEXT,
        symbol TEXT,
        pool TEXT,

        entry_price REAL,
        exit_price REAL,
        size REAL,

        pnl REAL,
        pnl_pct REAL,
        equity_after REAL,

        tp REAL,
        sl REAL,

        reason TEXT,
        success INTEGER,

        liquidity REAL,
        slippage REAL,

        hold_seconds INTEGER,
        entry_hour INTEGER,
        price_change_pct REAL,
        volatility REAL,
        mock_mode INTEGER,

        timestamp_entry INTEGER,
        timestamp_exit INTEGER
    );
    """)

    # ---- run metadata
    cur.execute("""
    CREATE TABLE IF NOT EXISTS run_meta (
        run_id TEXT PRIMARY KEY,
        params TEXT,
        timestamp INTEGER,
        start_ts INTEGER,
        mode TEXT,
        chain TEXT,
        notes TEXT
    );
    """)

    # ---- events table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        event_type TEXT,
        mint TEXT,
        message TEXT,
        timestamp INTEGER
    );
    """)

    # ---- Phase-1 analytics columns (SAFE ADDS)
    _add_col_if_missing(cur, "trades", "token_age_min", "REAL")
    _add_col_if_missing(cur, "trades", "vol_m5", "REAL")
    _add_col_if_missing(cur, "trades", "vol_h1", "REAL")
    _add_col_if_missing(cur, "trades", "vol_accel", "REAL")

    conn.commit()
    conn.close()

    print(f"[LOGGER] DB ready: {DB_PATH}")


# --------------------------------------------------
# RUN META
# --------------------------------------------------
def log_run_meta(run_id: str, notes: str = "") -> None:
    conn = _connect()
    cur = conn.cursor()

    params = json.dumps(dict(os.environ), ensure_ascii=False)

    cur.execute("""
    INSERT OR REPLACE INTO run_meta
    (run_id, params, timestamp, start_ts, mode, chain, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        params,
        int(time.time()),
        int(time.time()),
        os.getenv("MODE", os.getenv("PRICE_MODE", "DRYRUN")),
        os.getenv("REAL_CHAIN_ID", "solana"),
        notes,
    ))

    conn.commit()
    conn.close()


# --------------------------------------------------
# EVENTS
# --------------------------------------------------
def log_event(run_id: str, event_type: str, mint: Optional[str], message: str) -> None:
    conn = _connect()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO events (run_id, event_type, mint, message, timestamp)
    VALUES (?, ?, ?, ?, ?)
    """, (
        run_id,
        event_type,
        mint,
        message,
        int(time.time()),
    ))

    conn.commit()
    conn.close()


# --------------------------------------------------
# TRADE LOGGER (EQUITY-SAFE)
# --------------------------------------------------
def log_trade(trade: Dict) -> None:
    """
    Inserts a trade row.
    Supports persistent equity tracking via `equity_after`.
    """

    conn = _connect()
    cur = conn.cursor()

    table_cols = set(_cols(cur, "trades"))

    payload = {
        # core
        "run_id": trade.get("run_id"),
        "mode": trade.get("mode"),

        "mint": trade.get("mint"),
        "symbol": trade.get("symbol"),
        "pool": trade.get("pool"),

        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "size": trade.get("size"),

        "pnl": trade.get("pnl"),
        "pnl_pct": trade.get("pnl_pct"),
        "equity_after": trade.get("equity_after"),

        "tp": trade.get("tp"),
        "sl": trade.get("sl"),

        "reason": trade.get("reason"),
        "success": trade.get("success"),

        "liquidity": trade.get("liquidity"),
        "slippage": trade.get("slippage"),

        "hold_seconds": trade.get("hold_seconds"),
        "entry_hour": trade.get("entry_hour"),

        "price_change_pct": trade.get("price_change_pct"),
        "volatility": trade.get("volatility"),

        "mock_mode": trade.get("mock_mode"),

        "timestamp_entry": trade.get("timestamp_entry"),
        "timestamp_exit": trade.get("timestamp_exit"),

        # phase 1 analytics
        "token_age_min": trade.get("token_age_min"),
        "vol_m5": trade.get("vol_m5"),
        "vol_h1": trade.get("vol_h1"),
        "vol_accel": trade.get("vol_accel"),
    }

    cols = [k for k in payload.keys() if k in table_cols]
    vals = [payload[k] for k in cols]

    placeholders = ",".join(["?"] * len(cols))
    col_sql = ",".join(cols)

    cur.execute(
        f"INSERT INTO trades ({col_sql}) VALUES ({placeholders})",
        vals
    )

    conn.commit()
    conn.close()
