from __future__ import annotations

import os
import sqlite3
import argparse
from typing import Optional

DB_PATH = os.getenv("TRADES_DB_PATH", "data/trades.db")


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con


def _init() -> None:
    con = _db()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS live_controls (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mint_allowlist (
              mint TEXT PRIMARY KEY,
              note TEXT,
              created_at INTEGER
            )
            """
        )
        # defaults
        con.execute(
            "INSERT OR IGNORE INTO live_controls(key,value) VALUES('LIVE_ENABLED','0')"
        )
        con.execute(
            "INSERT OR IGNORE INTO live_controls(key,value) VALUES('KILL_SWITCH','1')"
        )
        con.commit()
    finally:
        con.close()


def _get(key: str, default: str = "0") -> str:
    _init()
    con = _db()
    try:
        row = con.execute(
            "SELECT value FROM live_controls WHERE key=?",
            (key,),
        ).fetchone()
        return str(row[0]) if row else default
    finally:
        con.close()


def _set(key: str, value: str) -> None:
    _init()
    con = _db()
    try:
        con.execute(
            "INSERT INTO live_controls(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    finally:
        con.close()


# -------------------------
# Public API used by Engine
# -------------------------
def live_enabled() -> bool:
    return _get("LIVE_ENABLED", "0") == "1"


def kill_switch_on() -> bool:
    return _get("KILL_SWITCH", "1") == "1"


def mint_allowed(mint: str) -> bool:
    _init()
    con = _db()
    try:
        row = con.execute(
            "SELECT mint FROM mint_allowlist WHERE mint=?",
            (str(mint),),
        ).fetchone()
        return row is not None
    finally:
        con.close()


# -------------------------
# CLI (terminal control)
# -------------------------
def allow_mint(mint: str, note: Optional[str] = None) -> None:
    import time

    _init()
    con = _db()
    try:
        con.execute(
            "INSERT OR IGNORE INTO mint_allowlist(mint,note,created_at) VALUES(?,?,?)",
            (str(mint), note or "", int(time.time())),
        )
        con.commit()
    finally:
        con.close()


def remove_mint(mint: str) -> None:
    _init()
    con = _db()
    try:
        con.execute("DELETE FROM mint_allowlist WHERE mint=?", (str(mint),))
        con.commit()
    finally:
        con.close()


def list_allowlist() -> None:
    _init()
    con = _db()
    try:
        rows = con.execute(
            "SELECT mint, note, created_at FROM mint_allowlist ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    finally:
        con.close()
    print("\nALLOWLIST:")
    for r in rows:
        print(f"  - {r[0]}  note='{r[1]}'  ts={r[2]}")
    if not rows:
        print("  (empty)")


def main():
    p = argparse.ArgumentParser("live_guard")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("enable_live")
    sub.add_parser("disable_live")
    sub.add_parser("kill_on")
    sub.add_parser("kill_off")

    a = sub.add_parser("allow")
    a.add_argument("mint")
    a.add_argument("--note", default="")

    r = sub.add_parser("remove")
    r.add_argument("mint")

    sub.add_parser("list")

    args = p.parse_args()
    _init()

    if args.cmd == "enable_live":
        _set("LIVE_ENABLED", "1")
        print("[LIVE] enabled")
    elif args.cmd == "disable_live":
        _set("LIVE_ENABLED", "0")
        print("[LIVE] disabled")
    elif args.cmd == "kill_on":
        _set("KILL_SWITCH", "1")
        print("[LIVE] kill switch ON (blocks trades)")
    elif args.cmd == "kill_off":
        _set("KILL_SWITCH", "0")
        print("[LIVE] kill switch OFF (allowed if LIVE_ENABLED=1 + allowlist)")
    elif args.cmd == "allow":
        allow_mint(args.mint, args.note)
        print(f"[LIVE] allowlisted {args.mint}")
    elif args.cmd == "remove":
        remove_mint(args.mint)
        print(f"[LIVE] removed {args.mint}")
    elif args.cmd == "list":
        list_allowlist()


if __name__ == "__main__":
    main()
