"""Pass/fail checks (task #44) for one mock-market run made by tools/run_mock.sh. Read-only.

    .venv/bin/python tools/check_mock_run.py --dir /tmp/t23-mock-hoard --scenario hoard [--restart]

Reads <dir>/agent/logs/decisions.jsonl, <dir>/agent/logs/memory-*.json and <dir>/mock-result.json.
"""
import argparse
import glob
import json
import re
import statistics as st
import sys
from pathlib import Path

EXPECT = {**{n: "pre" for n in range(1, 6)}, **{n: "fest" for n in range(6, 9)},
          **{n: "post" for n in range(9, 13)}, **{n: "close" for n in range(13, 16)}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--restart", action="store_true", help="run_mock.sh --restart: R11 is missed on purpose")
    a = ap.parse_args()
    d = Path(a.dir)
    rows = [json.loads(x) for x in (d / "agent" / "logs" / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if x]
    mock = json.loads((d / "mock-result.json").read_text(encoding="utf-8"))
    mem_files = glob.glob(str(d / "agent" / "logs" / "memory-*.json"))
    mem = json.loads(Path(mem_files[0]).read_text(encoding="utf-8")) if len(mem_files) == 1 else {"sent": {}, "rounds": []}
    ev = lambda name: [r for r in rows if r["event"] == name]
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))

    # 1. appraisal
    app = ev("appraisal_sent")
    check("appraisal_sent once, in APPRAISAL", len(app) == 1 and mock.get("appraisal_phase") == "APPRAISAL",
          f"{len(app)} sent · mock saw phase {mock.get('appraisal_phase')} · errors {len(ev('appraisal_error'))}")

    # 2. offers every real round
    skipped = {"R11"} if a.restart else set()
    sent = {r["round"] for r in ev("offers_sent")}
    again = {r["round"] for r in ev("round_already_sent")}
    want = {f"R{n}" for n in range(1, 16)} - skipped
    missing = sorted(want - sent - again, key=lambda x: int(x[1:]))
    errs = [r for r in ev("send_error") if r["round"] not in sent | again]
    check(f"offers_sent {len(want)}/{len(want)} real rounds" + (" (R11 skipped: agent down)" if skipped else ""),
          not missing and not errs,
          f"missing {missing or '-'} · send_error {len(ev('send_error'))} (unresolved {len(errs)}) · "
          f"batch_rejected {len(ev('batch_rejected'))} · offer_rejected {len(ev('offer_rejected'))} · "
          f"state_error {len(ev('state_error'))}")

    # 3. crashes
    crash = ev("strategy_crashed") + ev("fallback_crashed")
    check("no strategy_crashed / fallback_crashed", not crash, "; ".join(r.get("error", "")[:120] for r in crash) or "-")

    # 4. floor 0.80 × TV (agent's TV used for the price) and vs the true TV (info)
    low = [(rid, o["lot_id"], o["markup"]) for rid, offs in mem["sent"].items() for o in offs if o["markup"] < 0.80 - 1e-9]
    hist = {h["round"]: h for h in mock["history"]}
    true_low = [(h["round"], o["lot_id"], round(o["price_per_kg"] / o["true_tv"], 3)) for h in mock["history"]
                for o in h["offers"] if o["price_per_kg"] < 0.80 * o["true_tv"]]
    check("no offer below 0.80 × TV", not low,
          f"min markup {min((o['markup'] for offs in mem['sent'].values() for o in offs), default=None)} · "
          f"below 0.80 × true TV (sample noise, info): {true_low[:4] or '-'}")

    # 5. schedule notes vs calendar
    seen, wrong = {}, []
    for r in ev("offers_sent"):
        for n in r.get("notes") or []:
            m = re.match(r"schedule R(\d+) phase=(\w+) hoard=(\w+)", n)
            if m:
                seen[int(m.group(1))] = (m.group(2), m.group(3))
    for n, (ph, _) in seen.items():
        if EXPECT.get(n) != ph:
            wrong.append(f"R{n}={ph}")
    absent = [f"R{n}" for n in range(1, 15) if n not in seen and f"R{n}" not in skipped | again]
    check("schedule phase notes match the calendar", not wrong and not absent,
          f"wrong {wrong or '-'} · missing {absent or '-'} · R15 (last round, ladder) {seen.get(15, 'no note')}")

    # 6. hoarding signal
    hoard = {}
    for r in ev("offers_sent"):
        for n in r.get("notes") or []:
            m = re.match(r"hoarding: (.*) -> (\w+)", n)
            if m:
                hoard[int(r["round"][1:]) if r["round"].startswith("R") else 0] = (m.group(2), m.group(1))
    first_true = min((n for n, (v, _) in hoard.items() if v == "True" and n > 0), default=None)
    detail = " · ".join(f"R{n}:{v}" for n, (v, _) in sorted(hoard.items()) if n > 0)
    mkt = " ".join(f"{h['round']}:{h['specialty_offers_per_sold_rfq']}" for h in mock["history"] if h["round"] in
                   [f"R{i}" for i in range(1, 6)])
    if a.scenario == "hoard":
        check("hoarding -> True by R5 (hoard)", first_true is not None and first_true <= 5,
              f"first True R{first_true} · {detail} · market offers/sold Specialty RFQ {mkt}")
    else:
        check(f"hoarding never True ({a.scenario})", first_true is None,
              f"{detail} · market offers/sold Specialty RFQ {mkt}")

    # 7. leftover after R12
    # judged on lots some buyer can take by true quality (a reject lot can never sell: shown, not judged)
    total, sell = mock["total_bags"] * 60, mock["sellable_bags"] * 60
    r12 = hist.get("R12", {}).get("kg_remaining_sellable")
    allr12 = hist.get("R12", {}).get("kg_remaining")
    check("left after R12 <= 20% of sellable bags", r12 is not None and r12 <= 0.20 * sell,
          f"sellable {r12} kg of {sell} ({(r12 or 0) / sell:.0%}) · all lots {allr12} of {total} kg · "
          f"end {mock['history'][-1]['kg_remaining_sellable']} sellable / {mock['history'][-1]['kg_remaining']} kg")

    # 8. request rate
    check("max requests <= 5 per second", mock["max_rps"] <= 5,
          f"max {mock['max_rps']}/s over any 1 s · {mock['requests']} requests · 429 {mock['rate_limited_429']} · "
          f"faults {mock['faults_injected'] or '-'}")

    # restart checks
    if a.restart:
        b = mock["batches_per_round"]
        check("restart R7: round_already_sent, no second batch", "R7" in again and b.get("R7") == 1,
              f"round_already_sent {sorted(again)} · batches R7 {b.get('R7')}")
        bf = {r["round"] for r in ev("backfilled")}
        check("restart R12: backfilled R10 results", "R10" in bf and any(x["round_id"] == "R10" for x in mem["rounds"]),
              f"backfilled {sorted(bf)} · backfill_error {len(ev('backfill_error'))} · "
              f"new_game lines {len(ev('new_game'))} (one log file, appended)")

    # report
    board = mock["leaderboard"]
    me = next(r for r in board if r["team_id"] == "T23")
    prs = sorted((r["pr"] for r in board if r["team_id"] != "T23"), reverse=True)
    width = max(len(c[0]) for c in checks)
    print(f"scenario {a.scenario}{' (faults)' if mock.get('faults') else ''}{' (restart)' if a.restart else ''} · {d}")
    for name, ok, det in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  {det}")
    by_kind = {}
    for r in board:
        by_kind.setdefault(r["kind"], []).append(r["pr"])
    print(f"\n  T23: PR {me['pr']:.4f} · MAPE {me['mape']} · PR score {me['pr_score']} + appraisal {me['appraisal_score']} "
          f"= {me['total']} · rank {me['rank']}/{len(board)} · left {me['kg_left']} kg")
    print(f"  bots PR: best {prs[0]:.4f} · median {st.median(prs):.4f} · worst {prs[-1]:.4f} · "
          + " · ".join(f"{k} {st.mean(v):.4f}" for k, v in sorted(by_kind.items()) if k != "agent"))
    print(f"  top 3: " + ", ".join(f"#{r['rank']} {r['team_id']} {r['kind']} {r['total']}" for r in board[:3]))
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
