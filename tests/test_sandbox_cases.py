"""Strategy cases replayed from real sandbox data (data/sandbox-20261118/). Never talks to the market.

    .venv/bin/python tests/test_sandbox_cases.py          # or: python -m pytest tests/

Each case rebuilds the state the agent saw at the start of a round (lots and stock left, RFQs from the round's
market_summary, memory of earlier rounds) and checks what strategy.py decides.
"""
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import strategy as S  # noqa: E402
from appraisal import true_value  # noqa: E402

DATA = HERE / "data" / "sandbox-20261118"
RULES = json.loads((DATA / "calendar-1021.json").read_text(encoding="utf-8"))["rules"]   # 8 rounds, capacity 1.5
BAG = 60


class Mem:
    def __init__(self, rounds, sent):
        self.rounds, self.sent = rounds, sent


def _buyer_specs() -> dict:
    """Each buyer posts one spec all game; learn it from every offer we sent (memory stores the RFQ spec)."""
    out = {}
    for p in DATA.glob("memory-sandbox-*.json"):
        m = json.loads(p.read_text(encoding="utf-8"))
        summ = {s["rfq_id"]: s["buyer"] for r in m["rounds"] for s in r["market_summary"]}
        for offers in m["sent"].values():
            for o in offers:
                if o.get("spec") and o["rfq_id"] in summ:
                    out.setdefault(summ[o["rfq_id"]], o["spec"])
    return out


BUYER_SPEC = _buyer_specs()


def replay(tag: str, round_no: int, rules: dict = RULES):
    """(state, ctx) as the agent saw them when round `round_no` of game `tag` opened."""
    mem = json.loads((DATA / f"memory-sandbox-{tag}.json").read_text(encoding="utf-8"))
    rev = next(json.loads(p.read_text(encoding="utf-8")) for p in DATA.glob("reveal-*.json")
               if json.loads(p.read_text(encoding="utf-8"))["game_tag"] == tag)
    rounds = sorted(mem["rounds"], key=lambda r: int(r["round_id"][1:]))
    prior = [r for r in rounds if int(r["round_id"][1:]) < round_no]
    this = next(r for r in rounds if int(r["round_id"][1:]) == round_no)
    sold = defaultdict(int)
    for r in prior:
        for o in r["my_offers"]:
            sold[o["lot_id"]] += o.get("filled_kg", 0)
    lots = []
    for l in rev["lots"]:
        left = l["kg_total"] - sold[l["lot_id"]]
        lots.append({"lot_id": l["lot_id"], "kg_total": l["kg_total"], "kg_remaining": left, "kg_available": left,
                     "sample": l["sample"]})
    rfqs = [{"rfq_id": s["rfq_id"], "buyer": s["buyer"], "buyer_type": s["buyer_type"], "kg": s["wanted_kg"],
             "bags": s["wanted_kg"] // BAG, "spec": BUYER_SPEC[s["buyer"]]} for s in this["market_summary"]]
    total_bags = sum(l["kg_total"] for l in rev["lots"]) // BAG
    cap = math.ceil(rules["round_capacity_factor"] * total_bags / rules["timeline"]["rounds"]) * BAG
    state = {"round": {"round_id": f"R{round_no}", "kind": "real", "number": round_no,
                       "of": rules["timeline"]["rounds"], "calendar": "ปกติ"},
             "lots": lots, "rfqs": rfqs, "round_capacity_kg": cap, "capacity_left_kg": cap}
    tv = {l["lot_id"]: l["your_estimate"]["est_value_per_kg"] for l in rev["reveal"]["lots"]}
    truth = {l["lot_id"]: l for l in rev["reveal"]["lots"]}
    sent = {r["round_id"]: mem["sent"].get(r["round_id"], []) for r in prior}
    ctx = S.Context(rules, [], tv, random.Random(1), Mem(prior, sent))
    return state, ctx, truth


def offers_for(offers, lot):
    return [o for o in offers if o["lot_id"] == lot]


# ---------------- A: pricing TV from proven specs, probe lots slightly over a spec ----------------
def test_A_1021_lot2_probed_at_loosest_instant_priced_at_m15():
    """10:21 LOT-T23-2: sample moisture 15.8 (true 14.36) -> 1 probe bag to Instant m<=15, priced at TV(m=15.0).
    R1 has only Instant m<=14 (1.8 over: no probe); the first m<=15 RFQ is in R2."""
    state, ctx, truth = replay("b28910f2", 1)
    assert not offers_for(S.decide(state, ctx)[0], "LOT-T23-2")
    state, ctx, truth = replay("b28910f2", 2)
    offers, _ = S.decide(state, ctx)
    mine = offers_for(offers, "LOT-T23-2")
    specs = {o["rfq_id"]: r["spec"] for r in state["rfqs"] for o in mine if r["rfq_id"] == o["rfq_id"]}
    tv15 = round(true_value(RULES, {**state["lots"][1]["sample"], "moisture_pct": 15.0}), 2)
    assert sum(o["kg"] for o in mine) == BAG, mine                         # 1 bag only
    assert all(s["max_moisture_pct"] == 15.0 for s in specs.values()), specs  # loosest Instant, not m<=14
    k = mine[0]["price_per_kg"] / tv15
    assert 0.85 <= k <= 1.4, (mine, tv15)
    print(f"A  10:21 R2 LOT-2 probe {mine} · TV(m=15.0) {tv15} (est {ctx.tv['LOT-T23-2']}, true "
          f"{truth['LOT-T23-2']['true_value_per_kg']}) · k {k:.3f}")


def test_A_1021_lot2_after_probe_passes_sells_full_at_tv15():
    """If that probe comes back OUTBID/ACCEPTED, the lot is known to pass m<=15: priced at TV(m=15.0), sold in full
    wherever the spec allows (R3 has only Instant m<=14 -> now 1.0 over -> probe there at TV(m=14.0))."""
    state, ctx, _ = replay("b28910f2", 3)
    probe_rfq = next(r for r in replay("b28910f2", 2)[0]["rfqs"] if r["spec"]["max_moisture_pct"] == 15.0)
    ctx.memory.sent["R2"] = ctx.memory.sent["R2"] + [{"rfq_id": probe_rfq["rfq_id"], "lot_id": "LOT-T23-2", "kg": 60,
                                                      "price_per_kg": 120.0, "buyer_type": "INSTANT", "markup": 0.93,
                                                      "spec": probe_rfq["spec"]}]
    ctx.memory.rounds[1] = {**ctx.memory.rounds[1], "my_offers": ctx.memory.rounds[1]["my_offers"] + [
        {"rfq_id": probe_rfq["rfq_id"], "lot_id": "LOT-T23-2", "kg": 60, "status": "OUTBID", "filled_kg": 0}]}
    L = S.learn(state, ctx)
    tv15 = round(true_value(RULES, {**state["lots"][1]["sample"], "moisture_pct": 15.0}), 2)
    assert L["spec_sample"]["LOT-T23-2"]["moisture_pct"] == 15.0
    assert abs(L["tv_price"]["LOT-T23-2"] - tv15) < 0.01
    offers, _ = S.decide(state, ctx)
    print(f"A  10:21 R3 after R2 probe OUTBID: LOT-2 tv_price {L['tv_price']['LOT-T23-2']} · offers "
          f"{[(o['rfq_id'], o['kg'] // 60, o['price_per_kg']) for o in offers_for(offers, 'LOT-T23-2')]}")
    assert sum(o["kg"] for o in offers_for(offers, "LOT-T23-2")) >= BAG


# ---------------- B: near-boundary lot hit ABOVE_LIMIT -> price at the worse side, don't blame the buyer ----------------
def test_B_1007_lot2_defect_boundary_7():
    """10:07 LOT-T23-2: sample defect 6.8 (true 7.01). After its ABOVE_LIMIT at Instant in R5, R6 prices it at
    TV(defect 7.01) and buyer 5 learns no ceiling from it."""
    state, ctx, truth = replay("bf469890", 6)
    L = S.learn(state, ctx)
    worse = round(true_value(RULES, {**state["lots"][1]["sample"], "defect_pct": 7.01}), 2)
    true_tv = truth["LOT-T23-2"]["true_value_per_kg"]
    print(f"B  10:07 R6 LOT-2 tv_price {L['tv_price']['LOT-T23-2']} (est {ctx.tv['LOT-T23-2']}, worse side {worse}, "
          f"true {true_tv}) · buyer5 {L['buyers']['5 อินสแตนท์ไทย']['last']} ceil {L['buyers']['5 อินสแตนท์ไทย']['ceil']}")
    assert abs(L["tv_price"]["LOT-T23-2"] - worse) < 0.01
    assert abs(worse - true_tv) / true_tv < 0.01
    assert not L["buyers"]["5 อินสแตนท์ไทย"]["ceil"]
    assert "LOT-T23-2" not in L["lot_off"]


def test_B_1021_lot5_defect_boundary_5():
    """10:21 LOT-T23-5: sample defect 5.0 (true 5.28), ABOVE_LIMIT at 3 สยามคอมเมิร์ซ in R3 -> R4 prices it at
    TV(defect 5.01). Commercial d<=7 passing does not prove d<=5, so the worse side still applies."""
    state, ctx, truth = replay("b28910f2", 4)
    L = S.learn(state, ctx)
    worse = round(true_value(RULES, {**[l for l in state["lots"] if l["lot_id"] == "LOT-T23-5"][0]["sample"],
                                    "defect_pct": 5.01}), 2)
    true_tv = truth["LOT-T23-5"]["true_value_per_kg"]
    print(f"B  10:21 R4 LOT-5 tv_price {L['tv_price']['LOT-T23-5']} (est {ctx.tv['LOT-T23-5']}, worse side {worse}, "
          f"true {true_tv}) · buyer3 {L['buyers']['3 สยามคอมเมิร์ซ']['last']} ceil {L['buyers']['3 สยามคอมเมิร์ซ']['ceil']}")
    assert abs(L["tv_price"]["LOT-T23-5"] - worse) < 0.01
    assert abs(worse - true_tv) / true_tv < 0.01
    assert not L["buyers"]["3 สยามคอมเมิร์ซ"]["ceil"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("  ok", name)
