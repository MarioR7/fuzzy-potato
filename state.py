# onchain/state.py
import os, json, time
from pathlib import Path

STATE_FILE = Path(os.getenv("STATE_FILE", "positions.json"))

def _load():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"positions": []}

def _save(data):
    STATE_FILE.write_text(json.dumps(data, indent=2))

def add_position(pos: dict):
    data = _load()
    data["positions"].append(pos)
    _save(data)

def list_positions():
    return _load()["positions"]

def update_position(pos_id: str, **updates):
    data = _load()
    for p in data["positions"]:
        if p.get("id") == pos_id:
            p.update(updates)
            _save(data)
            return True
    return False

def now_ts():
    return int(time.time())
