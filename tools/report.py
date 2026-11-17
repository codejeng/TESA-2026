"""Read-only report per sandbox game (never talks to the market).

Reads logs/memory-*.json (offers sent + round results), logs/decisions.jsonl (notes per round) and
logs/reveal-*.json (saved by tools/save_reveal.py after the game closed).

    .venv/bin/python tools/report.py              # every game that has a reveal
    .venv/bin/python tools/report.py --all        # also games without a reveal (no PR/MAPE)
    .venv/bin/python tools/report.py --rounds     # + per-round pressure / fill / lot offsets
    .venv/bin/python tools/report.py --noise      # + sample vs true quality (defect, moisture) over all reveals
    .venv/bin/python tools/report.py --json       # machine-readable summary
"""
import json
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LOGS = HERE / "logs"
BAG = 60
STATUSES = ("ACCEPTED", "PARTIAL", "OUTBID", "ABOVE_LIMIT", "SPEC_MISMATCH")


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def find(d, *names, default=None):
    """First key present (reveal field names are read defensively)."""
    for n in names:
        if isinstance(d, dict) and n in d and d[n] is not None:
            return d[n]
    return default


# ---------------- decisions.jsonl -> segments per game ----------------
def decision_segments() -> list[dict]:
    path = LOGS / "decisions.jsonl"
    if not path.exists():
        return []
    segs, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row["event"] == "new_game":
            cur = {"start": row["time"], "tv": row.get("tv", {}), "rounds": {}, "events": Counter(), "closed": None}
            segs.append(cur)
            continue
        if cur is None:
            continue
        cur["events"][row["event"]] += 1
        if row["event"] == "offers_sent":
            cur["rounds"][row["round"]] = row
        elif row["event"] == "market_closed":
            cur["closed"] = row.get("standings")
    return segs


def offers_key(offers: list[dict]) -> tuple:
    return tuple(sorted((o["rfq_id"], o["lot_id"], o["kg"], o["price_per_kg"]) for o in offers))


def match_segment(mem: dict, segs: list[dict]) -> dict | None:
    for seg in segs:
        common = [r for r in seg["rounds"] if r in mem["sent"]]
        if common and all(offers_key(seg["rounds"][r].get("offers") or []) == offers_key(mem["sent"][r])
                          for r in common):
            return seg
    return None


# ---------------- notes -> numbers ----------------
def parse_notes(notes: list[str]) -> dict:
    out = {}
    for n in notes or []:
        m = re.match(r"pressure ([\d.]+) · urgency -([\d.]+) · floor ([\d.]+)", n)
        if m:
            out.update(pressure=float(m[1]), urgency=float(m[2]), floor=float(m[3]))
        m = re.match(r"fill ([\d.]+) · lot offsets (.*)", n)
        if m:
            out.update(fill=float(m[1]), lot_off=m[2])
        if n.startswith("llm skipped"):
            out["llm"] = "skipped"
        elif n.startswith("llm:"):
            out["llm"] = "used"
    return out


def round_num(rid: str) -> int:
    d = re.findall(r"\d+", rid)
    return int(d[0]) if d else 0


# ---------------- reveal ----------------
def reveal_lots(rev: dict) -> dict:
    """lot_id -> {"tv": true TV, "defect": .., "moisture": .., "screen": ..}"""
    body = rev.get("reveal", rev)
    rows = find(body, "lots", "my_lots", default=[]) or []
    out = {}
    for r in rows:
        q = find(r, "true", "true_quality", "quality", "truth", default=r)
        out[r["lot_id"]] = {
            "tv": find(r, "true_value_per_kg", "true_value", "tv", "tv_per_kg"),
            "defect": find(q, "defect_pct", "defect"),
            "moisture": find(q, "moisture_pct", "moisture"),
            "screen": find(q, "screen"),
        }
    return out


def reveal_score(rev: dict) -> dict:
    body = rev.get("reveal", rev)
    s = find(body, "score", "scores", "my_score", default={}) or {}
    return s if isinstance(s, dict) else {"score": s}


# ---------------- per game ----------------
def game_report(mem_path: Path, mem: dict, seg: dict | None, rev: dict | None) -> dict:
    sent = mem["sent"]
    status = Counter()
    k_sold = defaultdict(list)      # buyer -> [markup of offers that sold]
    rounds = []
    for rnd in sorted(mem["rounds"], key=lambda r: (r.get("kind") != "practice", round_num(r["round_id"]))):
        s_map = {(o["rfq_id"], o["lot_id"]): o for o in sent.get(rnd["round_id"], [])}
        buyer = {m["rfq_id"]: m["buyer"] for m in rnd.get("market_summary") or []}
        rs = Counter()
        for o in rnd.get("my_offers") or []:
            rs[o["status"]] += 1
            if rnd.get("kind") != "practice":
                status[o["status"]] += 1
            s = s_map.get((o["rfq_id"], o["lot_id"]))
            if s and o.get("filled_kg", 0) > 0:
                k_sold[buyer.get(o["rfq_id"], o["rfq_id"])].append(s["markup"])
        info = {"round": rnd["round_id"], "kind": rnd.get("kind"), "status": dict(rs),
                "sold_kg": sum(o.get("filled_kg", 0) for o in rnd.get("my_offers") or [])}
        if seg and rnd["round_id"] in seg["rounds"]:
            row = seg["rounds"][rnd["round_id"]]
            info.update(parse_notes(row.get("notes")), source=row.get("source"), seconds=row.get("seconds"),
                        offered_kg=row.get("kg"))
        rounds.append(info)

    out = {"memory": mem_path.name, "start": seg["start"] if seg else None, "rounds": rounds,
           "status": {k: status.get(k, 0) for k in STATUSES},
           "k_sold": {b: round(st.mean(ks), 3) for b, ks in sorted(k_sold.items())},
           "events": dict(seg["events"]) if seg else {}}
    errors = {e: n for e, n in out["events"].items()
              if e in ("strategy_crashed", "batch_rejected", "offer_rejected", "appraisal_error", "backfill_error")}
    out["errors"] = errors

    standings = (rev or {}).get("standings") or (seg or {}).get("closed")
    if standings:
        out["revenue"] = standings.get("revenue_thb")
        out["bags_left"] = standings.get("kg_remaining", 0) // BAG
    if rev:
        truth = reveal_lots(rev)
        est = (seg or {}).get("tv") or {}
        errs = [abs(est[l] - t["tv"]) / t["tv"] for l, t in truth.items() if l in est and t.get("tv")]
        out["mape"] = round(st.mean(errs), 4) if errs else None
        lots = {l["lot_id"]: l for l in rev.get("lots") or []}
        total_tv = sum(t["tv"] * lots[l]["kg_total"] for l, t in truth.items() if l in lots and t.get("tv"))
        left_tv = sum(t["tv"] * lots[l]["kg_remaining"] for l, t in truth.items() if l in lots and t.get("tv"))
        if total_tv and standings:
            out["pr_calc"] = round((standings["revenue_thb"] + 0.80 * left_tv) / total_tv, 4)
            # lots we never offered all game = no RFQ spec ever accepted them (e.g. REJECT grade):
            # they always end at 0.80, so leave them out to compare strategies fairly
            offered = {o["lot_id"] for offers in sent.values() for o in offers}
            dead = [l for l in truth if l in lots and l not in offered]
            out["unsellable"] = {l: lots[l]["kg_total"] // BAG for l in dead}
            tv_a = sum(truth[l]["tv"] * lots[l]["kg_total"] for l in truth if l in lots and l not in dead)
            left_a = sum(truth[l]["tv"] * lots[l]["kg_remaining"] for l in truth if l in lots and l not in dead)
            if tv_a:
                out["pr_adj"] = round((standings["revenue_thb"] + 0.80 * left_a) / tv_a, 4)
                out["bags_left_adj"] = out["bags_left"] - sum(out["unsellable"].values())
        out["score"] = reveal_score(rev)
        out["reveal_file"] = rev.get("_file")
    return out


def noise(reveals: list[dict]) -> dict:
    """Sample - true, over every lot of every reveal."""
    dd, dm, ds = [], [], []
    for rev in reveals:
        truth = reveal_lots(rev)
        for l in rev.get("lots") or []:
            t = truth.get(l["lot_id"])
            if not t or t.get("defect") is None:
                continue
            dd.append(l["sample"]["defect_pct"] - t["defect"])
            dm.append(l["sample"]["moisture_pct"] - t["moisture"])
            if t.get("screen") is not None:
                ds.append(l["sample"]["screen"] - t["screen"])

    def summ(xs):
        if not xs:
            return None
        a = sorted(abs(x) for x in xs)
        return {"n": len(xs), "mean": round(st.mean(xs), 3), "sd": round(st.pstdev(xs), 3),
                "p90_abs": round(a[int(0.9 * (len(a) - 1))], 3), "max_abs": round(a[-1], 3)}
    return {"defect": summ(dd), "moisture": summ(dm), "screen": summ(ds)}


def main() -> None:
    segs = decision_segments()
    reveals = {}
    for p in sorted(LOGS.glob("reveal-*.json")):
        r = load_json(p)
        r["_file"] = p.name
        reveals[r.get("game_tag")] = r
    games = []
    for p in sorted(LOGS.glob("memory-sandbox-*.json"), key=lambda p: p.stat().st_mtime):
        tag = p.stem.rsplit("-", 1)[-1]
        rev = reveals.get(tag)
        if rev is None and "--all" not in sys.argv:
            continue
        mem = load_json(p)
        games.append(game_report(p, mem, match_segment(mem, segs), rev))

    if "--json" in sys.argv:
        print(json.dumps({"games": games, "noise": noise(list(reveals.values()))}, ensure_ascii=False, indent=1))
        return
    for i, g in enumerate(games, 1):
        score = g.get("score") or {}
        print(f"\n=== game {i} · {g['start']} · {g['memory']}")
        print(f"PR(calc) {g.get('pr_calc')} · reveal score {json.dumps(score, ensure_ascii=False)[:160]}")
        print(f"MAPE {g.get('mape')} · revenue {g.get('revenue')} · bags left {g.get('bags_left')}")
        tot = sum(g["status"].values()) or 1
        print("status " + " · ".join(f"{k} {v} ({v / tot:.0%})" for k, v in g["status"].items()))
        print("k sold " + ", ".join(f"{b} {k}" for b, k in g["k_sold"].items()))
        print(f"errors {g['errors'] or 'none'}")
        if "--rounds" in sys.argv:
            for r in g["rounds"]:
                print(f"  {r['round']:<4} {r.get('source', '-'):<10} {str(r.get('seconds', '-')):>5}s "
                      f"pressure {r.get('pressure', '-')} fill {r.get('fill', '-')} floor {r.get('floor', '-')} "
                      f"offered {r.get('offered_kg', '-')} sold {r['sold_kg']} · {r['status']} · lot_off {r.get('lot_off', '-')}"
                      + (f" · llm {r['llm']}" if r.get("llm") else ""))
    if games:
        prs = [g["pr_calc"] for g in games if g.get("pr_calc") is not None]
        mapes = [g["mape"] for g in games if g.get("mape") is not None]
        print(f"\nmean PR {st.mean(prs):.4f} over {len(prs)} games" if prs else "")
        print(f"mean MAPE {st.mean(mapes):.4f}" if mapes else "")
    if "--noise" in sys.argv:
        print("\nsample − true:", json.dumps(noise(list(reveals.values())), indent=1))


if __name__ == "__main__":
    main()
