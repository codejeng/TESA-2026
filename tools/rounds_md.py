"""Read-only: write a per-round log (Markdown) of sandbox games from logs/.

    .venv/bin/python tools/rounds_md.py [--since 2026-11-18T09:10] [--out SANDBOX-ROUNDS.md] [--logs logs]

For each game that has a reveal: score, then every round — our offers (lot, buyer, kg, k, price, status, sold),
the agent's notes (pressure, buyer k, spec probe, last-round ladder) and the market for each RFQ.
"""
import argparse
import json
import re
from pathlib import Path

BAG = 60
SHORT = {"ACCEPTED": "ACC", "PARTIAL": "PART", "OUTBID": "OUTBID", "ABOVE_LIMIT": "ABOVE", "SPEC_MISMATCH": "SPEC✗",
         "CANCELLED": "CANC"}


def rnum(rid: str) -> int:
    d = re.findall(r"\d+", rid)
    return int(d[0]) if d else 0


def spec_txt(s: dict | None) -> str:
    if not s:
        return "-"
    return f"d≤{s['max_defect_pct']:g} m≤{s['max_moisture_pct']:g}" + (f" s≥{s['min_screen']}" if s.get("min_screen") else "")


def load_decisions(path: Path) -> dict:
    """(round_id, offers key) -> offers_sent row, so each game's rows are found by the offers it sent."""
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "offers_sent":
            key = tuple(sorted((o["rfq_id"], o["lot_id"], o["kg"], o["price_per_kg"]) for o in r.get("offers") or []))
            rows[(r["round"], key)] = r
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-11-18T09:10")
    ap.add_argument("--out", default="SANDBOX-ROUNDS.md")
    ap.add_argument("--logs", default="logs")
    a = ap.parse_args()
    logs = Path(a.logs)
    dec = load_decisions(logs / "decisions.jsonl")
    reveals = sorted(logs.glob("reveal-*.json"))
    out = ["# บันทึกรายรอบ ตลาดฝึก", "",
           f"สร้างจาก `{a.logs}/` ด้วย `tools/rounds_md.py` · เกมที่เริ่มหลัง {a.since} และมี reveal · "
           "k = ราคา ÷ TV ที่เราประเมิน · สถานะ: ACC รับครบ, PART รับบางส่วน, OUTBID แพ้ราคา, ABOVE เกินเพดาน, SPEC✗ ไม่ผ่านสเปกจริง", ""]
    n_games = 0
    for rp in reveals:
        rev = json.loads(rp.read_text(encoding="utf-8"))
        mp = logs / f"memory-sandbox-{rev['game_tag']}.json"
        if not mp.exists():
            continue
        mem = json.loads(mp.read_text(encoding="utf-8"))
        rounds = sorted(mem["rounds"], key=lambda r: rnum(r["round_id"]))
        # match decisions rows by the offers this game sent
        rows = {}
        for rid, offers in mem["sent"].items():
            key = tuple(sorted((o["rfq_id"], o["lot_id"], o["kg"], o["price_per_kg"]) for o in offers))
            if (rid, key) in dec:
                rows[rid] = dec[(rid, key)]
        start = min((r["time"] for r in rows.values()), default="")
        if not start or start < a.since:
            continue
        n_games += 1
        sc = rev["reveal"]["score"]
        lots = {l["lot_id"]: l for l in rev["lots"]}
        out += [f"## เกม {start[11:16]} · `{rev['game_tag']}`", "",
                f"PR **{sc['pr']}** · MAPE {sc['mape']:.2%} · ประเมิน {sc['appraisal_score']:.2f}/20 · total {sc['total']:.2f} · "
                f"อันดับ {sc['rank']} · ไฟล์ `{mp.name}`, `{rp.name}`", "",
                "| ล็อต | กระสอบ | เหลือ | ตัวอย่าง d / m / s | จริง d / m / s | TV ประเมิน | TV จริง | คลาด |",
                "|---|---|---|---|---|---|---|---|"]
        for l in rev["reveal"]["lots"]:
            s, t = l["sample"], l["true"]
            e, tv = l["your_estimate"]["est_value_per_kg"], l["true_value_per_kg"]
            lo = lots[l["lot_id"]]
            out.append(f"| {l['lot_id']} | {lo['kg_total'] // BAG} | {lo['kg_remaining'] // BAG} | "
                       f"{s['defect_pct']:g} / {s['moisture_pct']:g} / {s['screen']} | {t['defect_pct']:g} / {t['moisture_pct']:g} / {t['screen']} | "
                       f"{e:.2f} | {tv:.2f} | {100 * (e - tv) / tv:+.1f}% |")
        out.append("")
        for rnd in rounds:
            rid = rnd["round_id"]
            row = rows.get(rid, {})
            sent = {(o["rfq_id"], o["lot_id"]): o for o in mem["sent"].get(rid, [])}
            summ = {m["rfq_id"]: m for m in rnd.get("market_summary") or []}
            notes = row.get("notes") or []
            head = next((n for n in notes if n.startswith("pressure")), "")
            head = head.split(" · k:")[0] if head else "ไม่มีซอง (ไม่มีของที่ยื่นได้ หรือ agent ไม่ได้รันรอบนี้)"
            sold = sum(o.get("filled_kg", 0) for o in rnd.get("my_offers") or [])
            offered = sum(o["kg"] for o in rnd.get("my_offers") or [])
            out += [f"### {rid} · ยื่น {offered // BAG} ขายได้ {sold // BAG} กระสอบ · {head}", ""]
            if rnd.get("my_offers"):
                out += ["| RFQ | ผู้ซื้อ | ประเภท | สเปก | ล็อต | กระสอบ | k | บาท/กก. | ผล | ขายได้ |", "|---|---|---|---|---|---|---|---|---|---|"]
                for o in rnd["my_offers"]:
                    m, x = summ.get(o["rfq_id"], {}), sent.get((o["rfq_id"], o["lot_id"]), {})
                    out.append(f"| {o['rfq_id']} | {m.get('buyer', '?')} | {m.get('buyer_type', '?')} | {spec_txt(x.get('spec'))} | "
                               f"{o['lot_id']} | {o['kg'] // BAG} | {x.get('markup', '?')} | {o['price_per_kg']} | "
                               f"{SHORT.get(o['status'], o['status'])} | {o.get('filled_kg', 0) // BAG} |")
                out.append("")
            extra = [n for n in notes if n.startswith(("spec ", "last-round", "llm", "guard"))]
            buyers = [n for n in notes if re.match(r"^\d+ .*\[(SPECIALTY|COMMERCIAL|INSTANT)\] k=", n)]
            if buyers:
                out += ["k ผู้ซื้อที่ใช้รอบนี้ (เรียนจากผลรอบก่อน):", ""] + [f"- {n}" for n in buyers] + [""]
            if extra:
                out += [f"- {n}" for n in extra] + [""]
            if summ:
                out += ["<details><summary>ตลาดรอบนี้ (ทุก RFQ)</summary>", "",
                        "| RFQ | ผู้ซื้อ | ประเภท | อยากได้ | ซื้อได้ | ซองทั้งหมด | ราคาต่ำสุด | ราคาสูงสุด | เฉลี่ย |",
                        "|---|---|---|---|---|---|---|---|---|"]
                for m in sorted(summ.values(), key=lambda m: m["rfq_id"]):
                    out.append(f"| {m['rfq_id']} | {m['buyer']} | {m['buyer_type']} | {m['wanted_kg'] // BAG} | {m['bought_kg'] // BAG} | "
                               f"{m['offers']} | {m.get('price_min') or '-'} | {m.get('price_max') or '-'} | {m.get('price_avg') or '-'} |")
                out += ["", "</details>", ""]
    Path(a.out).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {a.out}: {n_games} games")


if __name__ == "__main__":
    main()
