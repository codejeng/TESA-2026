"""Short in-game report for the AI/human running the agent (#44). Reads local files only; never calls the market API.

    .venv/bin/python tools/round_report.py [--dir .] [--display http://HOST:PORT] [--team See-It2026] [--fest 6-8]

Sources: <dir>/logs/decisions.jsonl (current game = after the last new_game), the newest <dir>/logs/memory-*.json,
<dir>/overrides.json, `pgrep` for main.py, and (optional) <display>/display/data, the JSON the organizers' /display
page reads (no key; fetched at most once per 30 s, cached in logs/.display_cache.json, skipped on any error).
Ends with the PLAYBOOK.md signals that match (rule, evidence, suggested override).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import strategy as S  # noqa: E402  (constants / defaults only)

ERRORS = {"state_error", "send_error", "batch_rejected", "offer_rejected", "strategy_crashed", "fallback_crashed",
          "appraisal_error", "backfill_error", "calendar_error", "overrides_rejected", "send_late", "llm_error"}
TYPES = ("SPECIALTY", "COMMERCIAL", "INSTANT")
DISPLAY_EVERY = 30


def rnum(rid: str) -> int:
    return int(rid[1:]) if rid and rid[0] == "R" and rid[1:].isdigit() else 0


def ts(row) -> float:
    return datetime.fromisoformat(row["time"]).timestamp()


def load(d: Path):
    logs = d / "logs"
    rows = []
    p = logs / "decisions.jsonl"
    if p.exists():
        for x in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(x))
            except ValueError:
                pass
    start = max((i for i, r in enumerate(rows) if r.get("event") == "new_game"), default=0)
    rows = rows[start:]
    mems = sorted(logs.glob("memory-*.json"), key=lambda f: f.stat().st_mtime)
    mem = json.loads(mems[-1].read_text(encoding="utf-8")) if mems else {"sent": {}, "rounds": []}
    return rows, mem, (mems[-1].name if mems else "-")


def agent_alive() -> list[str]:
    try:
        out = subprocess.run(["pgrep", "-af", "main.py"], capture_output=True, text=True, timeout=5).stdout
    except Exception:                                   # noqa: BLE001
        return []
    me = str(os.getpid())
    return [l for l in out.splitlines() if l.split(" ", 1)[0] != me and re.search(r"python[\d.]*\s+(\S*/)?main\.py", l)]


def display(url: str | None, d: Path):
    if not url:
        return None, "ไม่ได้ให้ --display"
    cache = d / "logs" / ".display_cache.json"
    try:
        c = json.loads(cache.read_text(encoding="utf-8"))
        if time.time() - c["t"] < DISPLAY_EVERY:
            return c["data"], f"cache {time.time() - c['t']:.0f} วิ"
    except Exception:                                   # noqa: BLE001
        pass
    full = url.rstrip("/")
    full = full if full.endswith("/display/data") else (full[:-len("/display")] if full.endswith("/display") else full) + "/display/data"
    try:
        with urllib.request.urlopen(urllib.request.Request(full, headers={"User-Agent": "round-report"}), timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps({"t": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
        return data, "สด"
    except Exception as e:                              # noqa: BLE001 — leaderboard is optional
        return None, f"ดึงไม่ได้ ({type(e).__name__})"


def market_by_type(rnd) -> dict:
    out = {t: {"offers": 0, "wanted": 0, "bought": 0, "rev": 0.0} for t in TYPES}
    for s in rnd.get("market_summary") or []:
        o = out.setdefault(s["buyer_type"], {"offers": 0, "wanted": 0, "bought": 0, "rev": 0.0})
        o["offers"] += s.get("offers") or 0
        o["wanted"] += s["wanted_kg"]
        o["bought"] += s["bought_kg"]
        if s["bought_kg"] and s.get("price_avg"):
            o["rev"] += s["price_avg"] * s["bought_kg"]
    for o in out.values():
        o["avg"] = o["rev"] / o["bought"] if o["bought"] else None
    return out


def current_values(d: Path) -> dict:
    v = {**{f"START.{t}": S._DEFAULTS["START"][t] for t in TYPES}, "URGENCY_SLOPE": S._DEFAULTS["URGENCY_SLOPE"],
         "BUYER_K_MAX": {}, "LOT_K_OFFSET": {}}
    try:
        data = json.loads((d / "overrides.json").read_text(encoding="utf-8"))
        if S.validate_overrides(data) is None:
            v.update(data["set"])
    except Exception:                                   # noqa: BLE001
        pass
    return v


def signals(real, mem, rows, cur, fest, last_lots, first_lots, dead):
    """PLAYBOOK.md rules -> [(rule, evidence, suggestion)]"""
    out = []
    sent = mem.get("sent", {})
    info = []                                           # per round: [(lot, buyer, type, status, k)]
    for r in real:
        summ = {s["rfq_id"]: s for s in r.get("market_summary") or []}
        k = {(o["rfq_id"], o["lot_id"]): o.get("markup") for o in sent.get(r["round_id"], [])}
        info.append([(o["lot_id"], summ.get(o["rfq_id"], {}).get("buyer", "?"),
                      summ.get(o["rfq_id"], {}).get("buyer_type", "?"), o["status"], k.get((o["rfq_id"], o["lot_id"])))
                     for o in r.get("my_offers") or []])
    # 1. OUTBID >= 50% of our offers to one buyer type, 2 rounds in a row
    for t in TYPES:
        sh = []
        for res in info[-2:]:
            mine = [x for x in res if x[2] == t]
            sh.append(sum(x[3] == "OUTBID" for x in mine) / len(mine) if mine else None)
        if len(sh) == 2 and all(x is not None and x >= 0.5 for x in sh):
            key = f"START.{t}"
            out.append(("1 OUTBID ≥50% 2 รอบติด", f"{t}: OUTBID {sh[0]:.0%}, {sh[1]:.0%}",
                        f'"{key}": {max(0.85, round(cur[key] - 0.01, 3))}'))
    # 2. ABOVE_LIMIT at the same buyer in >= 2 of the last 3 rounds
    lim = defaultdict(int)
    for res in info[-3:]:
        for b in {x[1] for x in res if x[3] == "ABOVE_LIMIT"}:
            lim[b] += 1
    for b, n in lim.items():
        if n >= 2:
            ok = [x[4] for res in info for x in res if x[1] == b and x[3] in ("ACCEPTED", "PARTIAL") and x[4]]
            sug = (f'"BUYER_K_MAX": {{"{b}": {min(1.30, max(0.80, round(max(ok), 3)))}}}' if ok
                   else "ไม่เคยขายได้: ยังไม่ตั้ง (รอข้อมูล)")
            out.append(("2 ABOVE_LIMIT ผู้ซื้อเดิม ≥2/3 รอบ", f"{b}: {n} รอบ · k ที่ขายได้สูงสุด {max(ok) if ok else '-'}", sug))
    # 3. same lot ABOVE_LIMIT/OUTBID 3 rounds in a row (no fill) while other lots sold
    if len(info) >= 3:
        lots = {x[0] for res in info[-3:] for x in res}
        for lot in sorted(lots):
            bad = all(any(x[0] == lot for x in res) and all(x[3] in ("ABOVE_LIMIT", "OUTBID") for x in res if x[0] == lot)
                      and any(x[0] != lot and x[3] in ("ACCEPTED", "PARTIAL") for x in res) for res in info[-3:])
            if bad:
                v = round(cur["LOT_K_OFFSET"].get(lot, 0.0) - 0.02, 3)
                out.append(("3 ล็อตแพ้ 3 รอบติด ล็อตอื่นขายได้", f"{lot}", f'"LOT_K_OFFSET": {{"{lot}": {max(-0.05, v)}}}'))
    # 4. festival: Specialty bought < 70% and price up -> no action
    if real and fest[0] <= rnum(real[-1]["round_id"]) <= fest[1] and len(real) >= 2:
        a, b = market_by_type(real[-2])["SPECIALTY"], market_by_type(real[-1])["SPECIALTY"]
        if b["wanted"] and b["bought"] / b["wanted"] < 0.7 and a["avg"] and b["avg"] and b["avg"] > a["avg"]:
            out.append(("4 เทศกาล Specialty ขาดของ ราคาขึ้น", f"ซื้อได้ {b['bought'] / b['wanted']:.0%} · ราคา {a['avg']:.1f}->{b['avg']:.1f}",
                        "ไม่ต้องทำอะไร (SCHEDULE ปิด ล็อต S แข่งตามราคาอยู่แล้ว)"))
    # 5. deciding R11 or later with > 30% of sellable bags left
    nxt = rnum(real[-1]["round_id"]) + 1 if real else 0
    if nxt >= 11 and first_lots and last_lots:
        tot = sum(v for l, v in first_lots.items() if l not in dead)
        left = sum(v for l, v in last_lots.items() if l not in dead)
        if tot and left / tot > 0.30:
            out.append(("5 R11+ ของขายได้เหลือ >30%", f"เหลือ {left // 60}/{tot // 60} กระสอบ ({left / tot:.0%})",
                        f'"URGENCY_SLOPE": {min(0.10, round(cur["URGENCY_SLOPE"] + 0.02, 3))}'))
    # 6. since R9: average price of every type down >= 3% two rounds in a row
    late = [r for r in real if rnum(r["round_id"]) >= 8]
    if len(late) >= 3:
        m = [market_by_type(r) for r in late[-3:]]
        drops = all(m[i][t]["avg"] and m[i + 1][t]["avg"] and m[i + 1][t]["avg"] <= 0.97 * m[i][t]["avg"]
                    for i in (0, 1) for t in TYPES if m[0][t]["avg"])
        if drops and rnum(late[-2]["round_id"]) >= 9:
            out.append(("6 ราคาทุกประเภทลด ≥3% 2 รอบติด (R9+)", " · ".join(
                f"{t[:4]} {m[0][t]['avg']:.1f}->{m[2][t]['avg']:.1f}" for t in TYPES if m[0][t]["avg"] and m[2][t]["avg"]),
                f'"URGENCY_SLOPE": {min(0.10, round(cur["URGENCY_SLOPE"] + 0.02, 3))}'))
    return out


def report(d: Path, display_url: str | None = None, team: str | None = None, fest=(6, 8), send_at: float | None = None) -> str:
    rows, mem, mem_name = load(d)
    send_at = send_at if send_at is not None else max(30.0, float(os.environ.get("SEND_AT", "180") or 180))
    L = []
    real = sorted([r for r in mem.get("rounds", []) if r.get("kind") != "practice"], key=lambda r: rnum(r["round_id"]))
    ev = [r for r in rows if r.get("event") in ("waiting_to_send", "offers_sent", "round_already_sent", "market_closed")]
    last = ev[-1] if ev else None
    closed = any(r.get("event") == "market_closed" for r in rows)
    phase = "CLOSED" if closed else ("รอส่ง" if last and last["event"] == "waiting_to_send" else "ส่งแล้ว/รอผล" if last else "ยังไม่เริ่มรอบ")
    L.append(f"== round report {datetime.now():%H:%M:%S} · {mem_name} · ผลล่าสุด {real[-1]['round_id'] if real else '-'} ==")
    eta = ""
    if last and last["event"] == "waiting_to_send":
        e = ts(last) + (last["seconds_left"] - last["send_at"]) - time.time()
        eta = f" · ส่งในอีก ~{max(0, e):.0f} วิ (SEND_AT {last['send_at']:.0f})" + (" ← แก้ overrides.json ก่อนเวลานี้" if e > 10 else "")
    L.append(f"รอบ {last.get('round', '-') if last else '-'} · {phase}{eta}")
    alive = agent_alive()
    L.append(f"main.py: {'ทำงาน ' + str(len(alive)) + ' โปรเซส' if alive else '⚠ ไม่พบโปรเซส'}" + (" ⚠ มากกว่า 1" if len(alive) > 1 else ""))
    sends = [i for i, r in enumerate(rows) if r.get("event") == "offers_sent"]
    since = sends[-2] if len(sends) >= 2 else 0
    errs = [r for r in rows[since:] if r.get("event") in ERRORS]
    L.append("error ใหม่: " + (" · ".join(f"{r['time'][11:]} {r['event']} {str(r.get('error') or r.get('reason') or '')[:60]}" for r in errs[-4:]) or "-"))

    if real:
        r = real[-1]
        summ = {s["rfq_id"]: s for s in r.get("market_summary") or []}
        k = {(o["rfq_id"], o["lot_id"]): o.get("markup") for o in mem.get("sent", {}).get(r["round_id"], [])}
        L.append(f"ซองเรา {r['round_id']}:")
        for o in r.get("my_offers") or []:
            s = summ.get(o["rfq_id"], {})
            L.append(f"  {o['lot_id']} -> {s.get('buyer', '?')} [{s.get('buyer_type', '?')[:4]}] k={k.get((o['rfq_id'], o['lot_id']), '?')}"
                     f" {o['kg'] // 60}b {o['status']} ขายได้ {o.get('filled_kg', 0) // 60}")
    sent_rows = [x for x in rows if x.get("event") == "offers_sent"]
    if sent_rows:
        notes = sent_rows[-1].get("notes") or []
        L.append(f"k ต่อผู้ซื้อ (ตอนส่ง {sent_rows[-1]['round']}):")
        for n in notes:
            m = re.match(r"(.+?) \[(\w+)\] k=([\d.]+) · (.*)", n)
            if m:
                L.append(f"  {m.group(1)} [{m.group(2)[:4]}] {m.group(3)} · {m.group(4)[:90]}")
        for n in notes:
            if n.startswith("pressure ") or n.startswith("fill "):
                L.append("  " + n[:150])
    lot_rows = [x for x in rows if isinstance(x.get("lots"), dict)]
    first_lots, last_lots = (lot_rows[0]["lots"], lot_rows[-1]["lots"]) if lot_rows else ({}, {})
    dead = set()
    for x in sent_rows[-1:]:
        for n in x.get("notes") or []:
            if n.startswith("no way to sell"):
                dead |= set(re.findall(r"LOT-[\w-]+", n))
    if last_lots:
        L.append("กระสอบเหลือ: " + ", ".join(f"{l} {v // 60}" + ("(ขายไม่ได้)" if l in dead else "") for l, v in sorted(last_lots.items())))
    if real:
        L.append("ตลาด (ซอง/ต้องการ/ซื้อได้ kg/ราคาเฉลี่ย):")
        for r in real[-3:]:
            m = market_by_type(r)
            L.append(f"  {r['round_id']}: " + " · ".join(
                f"{t[:4]} {m[t]['offers']}/{m[t]['wanted']}/{m[t]['bought']}/{m[t]['avg']:.1f}" if m[t]["avg"] else
                f"{t[:4]} {m[t]['offers']}/{m[t]['wanted']}/{m[t]['bought']}/-" for t in TYPES))
    cur = current_values(d)
    try:
        ov = (d / "overrides.json").read_text(encoding="utf-8").strip()
        ov = json.dumps(json.loads(ov).get("set"), ensure_ascii=False) if ov else "(ว่าง)"
    except FileNotFoundError:
        ov = "(ไม่มีไฟล์ = default)"
    except ValueError:
        ov = "⚠ JSON เสีย"
    oev = [x for x in rows if x.get("event") in ("overrides_applied", "overrides_rejected")]
    L.append(f"overrides.json: {ov}")
    if oev:
        x = oev[-1]
        L.append(f"  agent ล่าสุด: {x['time'][11:]} {x['event']} {x.get('round', '')} {x.get('reason', '')[:80]}")
    data, how = display(display_url, d)
    if data:
        lb = data.get("leaderboard") or {}
        rk = lb.get("ranking") or []
        mine = [x for x in rk if team and team in x.get("name", "")]
        top = " · ".join(f"{x['rank']}.{x['name'][:14]}" for x in rk[:5])
        L.append(f"leaderboard ({how}{', แช่แข็ง' if lb.get('frozen') else ''}): "
                 + (f"เรา #{mine[0]['rank']} ({mine[0].get('move', 0):+d}) · " if mine else "") + (top or "ยังไม่มีอันดับ"))
    else:
        L.append(f"leaderboard: {how}")
    sig = signals(real, mem, rows, cur, fest, last_lots, first_lots, dead)
    applied_since = any(x.get("event") == "overrides_applied" for x in rows[sends[-1]:]) if sends else False
    L.append("สัญญาณ PLAYBOOK:" + ("" if sig else " - (ไม่มีข้อไหนเข้า → ไม่ต้องแก้)"))
    for rule, why, sug in sig:
        L.append(f"  [{rule}] {why} → {sug}")
    if sig:
        L.append("  ปรับได้ 1 อย่างต่อรอบ (เลือกข้อบนสุดที่ใช้ได้)" + (" · ⚠ รอบนี้มี overrides_applied แล้ว" if applied_since else ""))
    return "\n".join(L[:60])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT), help="agent folder (has logs/ and overrides.json)")
    ap.add_argument("--display", default=None, help="market base URL or its /display page (leaderboard, no key)")
    ap.add_argument("--team", default=None, help="part of our team name, to find us in the leaderboard")
    ap.add_argument("--fest", default="6-8", help="festival rounds a-b (slide day 20)")
    ap.add_argument("--send-at", type=float, default=None)
    a = ap.parse_args()
    f = tuple(int(x) for x in a.fest.split("-"))
    print(report(Path(a.dir), a.display, a.team, (f[0], f[-1]), a.send_at))
    return 0


if __name__ == "__main__":
    sys.exit(main())
