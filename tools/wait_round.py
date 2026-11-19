"""Wait until the agent has a new round result (or the market closes, or timeout), then print round_report (#44).
Reads local files only; never calls the market API.

    .venv/bin/python tools/wait_round.py --timeout 420 [--dir .] [--display http://HOST:PORT] [--team See-It2026]

"New result" = the newest logs/memory-*.json has more rounds than when this started (main.py records the last
round's results as soon as the next round opens, before it waits for SEND_AT), or a new game's memory file appears.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import round_report as R  # noqa: E402


def snapshot(d: Path):
    logs = d / "logs"
    mems = sorted(logs.glob("memory-*.json"), key=lambda f: f.stat().st_mtime)
    n = 0
    if mems:
        try:
            n = len(json.loads(mems[-1].read_text(encoding="utf-8")).get("rounds", []))
        except ValueError:                           # being written right now: try again next poll
            n = -1
    p = logs / "decisions.jsonl"
    size = p.stat().st_size if p.exists() else 0
    return (mems[-1].name if mems else None), n, size


def closed_since(d: Path, offset: int) -> bool:
    p = d / "logs" / "decisions.jsonl"
    if not p.exists():
        return False
    with open(p, "rb") as f:
        f.seek(offset)
        return b'"event": "market_closed"' in f.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=420)
    ap.add_argument("--dir", default=str(R.ROOT))
    ap.add_argument("--display", default=None)
    ap.add_argument("--team", default=None)
    ap.add_argument("--fest", default="6-8")
    ap.add_argument("--poll", type=float, default=2.0)
    a = ap.parse_args()
    d = Path(a.dir)
    name0, n0, size0 = snapshot(d)
    t0, why = time.time(), "timeout"
    while time.time() - t0 < a.timeout:
        name, n, _ = snapshot(d)
        if name != name0 and name is not None:
            why = f"เกมใหม่ ({name})"
            break
        if n0 >= 0 and n > n0:
            why = f"ผลรอบใหม่ (memory {n0} -> {n} รอบ)"
            break
        if n0 < 0 and n >= 0:
            n0 = n
        if closed_since(d, size0):
            why = "market_closed"
            break
        time.sleep(a.poll)
    print(f"[wait_round] {why} หลัง {time.time() - t0:.0f} วิ")
    f = tuple(int(x) for x in a.fest.split("-"))
    print(R.report(d, a.display, a.team, (f[0], f[-1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
