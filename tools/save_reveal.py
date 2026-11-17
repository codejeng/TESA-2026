"""Read-only: wait for each sandbox game to close, then save GET /v1/sandbox/reveal to logs/reveal-<time>.json.

Never sends, edits or cancels offers. Polls /v1/state every POLL seconds (far below the 5 calls/s limit).

    .venv/bin/python tools/save_reveal.py            # keep watching, one file per closed game
    .venv/bin/python tools/save_reveal.py --once     # fetch the reveal for the game that is closed now
"""
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from main import load_env  # noqa: E402
from market import Market  # noqa: E402

POLL = 10
LOGS = HERE / "logs"


def game_tag(state: dict) -> str:
    """Same tag main.py uses for logs/memory-<market>-<tag>.json."""
    key = tuple(sorted((l["lot_id"], json.dumps(l["sample"], sort_keys=True)) for l in state["lots"]))
    return hashlib.sha1(repr(key).encode()).hexdigest()[:8]


def saved_tags() -> set:
    tags = set()
    for p in LOGS.glob("reveal-*.json"):
        try:
            tags.add(json.loads(p.read_text(encoding="utf-8")).get("game_tag"))
        except (OSError, ValueError):
            pass
    return tags


def save(api: Market, state: dict) -> None:
    reveal = api.call("GET", "/v1/sandbox/reveal")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = {"game_tag": game_tag(state), "saved_at": stamp, "standings": state.get("standings"),
           "lots": state.get("lots"), "reveal": reveal}
    LOGS.mkdir(exist_ok=True)
    path = LOGS / f"reveal-{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{stamp} saved {path.name} (game {out['game_tag']})", flush=True)


def main() -> None:
    import os
    load_env(HERE / ".env")
    api = Market(os.environ["MARKET_URL"], os.environ["TEAM_KEY"])
    once = "--once" in sys.argv
    while True:
        try:
            state = api.state()
            if state.get("market") != "sandbox":
                sys.exit("not the sandbox market: refusing to run")
            if state["phase"] == "CLOSED" and state.get("lots") and game_tag(state) not in saved_tags():
                save(api, state)
            if once:
                return
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 — keep watching through network errors
            print("error:", e, flush=True)
            if once:
                return
        time.sleep(POLL)


if __name__ == "__main__":
    main()
