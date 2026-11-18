"""Small synthetic unit tests for strategy.py (no market, no data files).

    .venv/bin/python tests/test_strategy_units.py      # or: python -m pytest tests/
"""
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import strategy as S  # noqa: E402

RULES = json.loads((HERE / "data" / "sandbox-20261118" / "calendar-1021.json").read_text(encoding="utf-8"))["rules"]
SPECIALTY = {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 16}
COMMERCIAL = {"max_defect_pct": 7.0, "max_moisture_pct": 12.5, "min_screen": None}


def test_switch_gap_uses_open_routes():
    """PREMIUM lot 8 bags · Specialty RFQs 2 × 1 bag (k 1.10) · Commercial RFQs 2 (k 0.99) · budget 6
    -> Specialty 2 + Commercial 4 in the same round (Specialty full -> the rest flows to Commercial)."""
    lot = {"lot_id": "P", "kg_total": 480, "kg_remaining": 480, "kg_available": 480,
           "sample": {"defect_pct": 0.5, "moisture_pct": 11.0, "screen": 18}}
    rfqs = [{"rfq_id": "S1", "buyer": "s1", "buyer_type": "SPECIALTY", "kg": 60, "bags": 1, "spec": SPECIALTY},
            {"rfq_id": "S2", "buyer": "s2", "buyer_type": "SPECIALTY", "kg": 60, "bags": 1, "spec": SPECIALTY},
            {"rfq_id": "C1", "buyer": "c1", "buyer_type": "COMMERCIAL", "kg": 600, "bags": 10, "spec": COMMERCIAL},
            {"rfq_id": "C2", "buyer": "c2", "buyer_type": "COMMERCIAL", "kg": 600, "bags": 10, "spec": COMMERCIAL}]
    state = {"round": {"round_id": "R2", "kind": "real", "number": 2, "of": 8, "calendar": "ปกติ"},
             "lots": [lot], "rfqs": rfqs, "round_capacity_kg": 360, "capacity_left_kg": 360}
    ctx = S.Context(RULES, [], {"P": 130.0}, random.Random(1))
    plan = S.build_plan(state, ctx, S.candidates(state, ctx), {"S1": 1.10, "S2": 1.10, "C1": 0.99, "C2": 0.99}, set())
    by_type = {"S": sum(p["bags"] for p in plan if p["rfq_id"].startswith("S")),
               "C": sum(p["bags"] for p in plan if p["rfq_id"].startswith("C"))}
    print(f"switch gap: {by_type} · {[(p['rfq_id'], p['bags']) for p in plan]}")
    assert by_type == {"S": 2, "C": 4}


def test_flow_gated_holds_when_not_contested():
    """Same market, flow=set() (pressure < FLOW_PRESSURE and nothing lost last round) -> hold for Specialty: S 2, C 0.
    Game 13:00 (memory-sandbox-7aa49679.json) R1: LOT-T23-3 sent 3 bags to Commercial at 0.99 while Specialty later
    paid 1.07-1.14 every round -> about 1.4% PR lost."""
    lot = {"lot_id": "P", "kg_total": 480, "kg_remaining": 480, "kg_available": 480,
           "sample": {"defect_pct": 0.5, "moisture_pct": 11.0, "screen": 18}}
    rfqs = [{"rfq_id": "S1", "buyer": "s1", "buyer_type": "SPECIALTY", "kg": 60, "bags": 1, "spec": SPECIALTY},
            {"rfq_id": "S2", "buyer": "s2", "buyer_type": "SPECIALTY", "kg": 60, "bags": 1, "spec": SPECIALTY},
            {"rfq_id": "C1", "buyer": "c1", "buyer_type": "COMMERCIAL", "kg": 600, "bags": 10, "spec": COMMERCIAL}]
    state = {"round": {"round_id": "R2", "kind": "real", "number": 2, "of": 8, "calendar": "ปกติ"},
             "lots": [lot], "rfqs": rfqs, "round_capacity_kg": 360, "capacity_left_kg": 360}
    ctx = S.Context(RULES, [], {"P": 130.0}, random.Random(1))
    k = {"S1": 1.10, "S2": 1.10, "C1": 0.99}
    held = S.build_plan(state, ctx, S.candidates(state, ctx), k, set(), flow=set())
    lost = S.build_plan(state, ctx, S.candidates(state, ctx), k, set(), flow={"P"})
    assert sum(p["bags"] for p in held if p["rfq_id"] == "C1") == 0
    assert sum(p["bags"] for p in held if p["rfq_id"].startswith("S")) == 2
    assert sum(p["bags"] for p in lost if p["rfq_id"] == "C1") == 4


# ---------------- SPEC_MISMATCH on one identifiable axis -> price at the worse side of that axis ----------------
LOT1_1203 = {"defect_pct": 1.4, "moisture_pct": 13.2, "screen": 16}
# game 12:03 (memory-sandbox-9f487f97.json): LOT-T23-1's sample is not logged; rebuilt from the two TVs logged with
# its offers: 123.96 (sample) and 126.74 (R1 probe to Commercial, sample clipped to moisture 12.5).
# Only d 1.4 / m 13.2 / screen 16 or 17 gives both (screen 16 and 17 have the same factor).


class _Mem:
    def __init__(self, rounds, sent):
        self.rounds, self.sent = rounds, sent


def _state_1203(round_no: int):
    mem = json.loads((HERE / "data" / "sandbox-20261118" / "memory-sandbox-9f487f97.json").read_text(encoding="utf-8"))
    prior = [r for r in sorted(mem["rounds"], key=lambda r: int(r["round_id"][1:])) if int(r["round_id"][1:]) < round_no]
    state = {"round": {"round_id": f"R{round_no}", "kind": "real", "number": round_no, "of": 8, "calendar": "ปกติ"},
             "lots": [{"lot_id": "LOT-T23-1", "kg_total": 600, "kg_remaining": 480, "kg_available": 480,
                       "sample": LOT1_1203}],
             "rfqs": [], "round_capacity_kg": 420, "capacity_left_kg": 420}
    ctx = S.Context(RULES, [], {"LOT-T23-1": 123.96}, random.Random(1),
                    _Mem(prior, {r["round_id"]: mem["sent"].get(r["round_id"], []) for r in prior}))
    return state, ctx


def test_mismatch_axis_1203_lot1():
    """R1: Commercial (m <= 12.5) SPEC_MISMATCH. Only moisture is near/over its limit -> price at moisture
    max(13.2, 12.5) + 0.3 = 13.5 -> tv_price below 123.96. Spec checks still use the sample."""
    from appraisal import true_value
    assert abs(true_value(RULES, LOT1_1203) - 123.96) < 0.01
    assert abs(true_value(RULES, {**LOT1_1203, "moisture_pct": 12.5}) - 126.74) < 0.01
    state, ctx = _state_1203(2)
    L = S.learn(state, ctx)
    print(f"mismatch R2: tv_price {L['tv_price']['LOT-T23-1']} (sample TV 123.96) · spec_sample {L['spec_sample']['LOT-T23-1']}")
    assert L["tv_price"]["LOT-T23-1"] < 123.96
    assert abs(L["tv_price"]["LOT-T23-1"] - true_value(RULES, {**LOT1_1203, "moisture_pct": 13.5})) < 0.01
    assert L["spec_sample"]["LOT-T23-1"] == LOT1_1203


def test_mismatch_axis_rules():
    spec = {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 16}
    assert S.mismatch_axis({"defect_pct": 2.8, "moisture_pct": 11.0, "screen": 17}, spec) == ("defect_pct", 3.3)
    assert S.mismatch_axis({"defect_pct": 1.0, "moisture_pct": 12.3, "screen": 17}, spec) == ("moisture_pct", 12.8)
    assert S.mismatch_axis({"defect_pct": 1.0, "moisture_pct": 11.0, "screen": 16}, spec) == ("screen", 15)
    assert S.mismatch_axis({"defect_pct": 2.8, "moisture_pct": 12.3, "screen": 17}, spec) is None   # two axes
    assert S.mismatch_axis({"defect_pct": 1.0, "moisture_pct": 11.0, "screen": 18}, spec) is None   # none near


def test_mismatch_does_not_override_proven_spec():
    """A later pass of Instant m <= 14 (R2 ACCEPTED in that game) caps the moisture used for pricing at 14.0."""
    from appraisal import true_value
    state, ctx = _state_1203(3)
    L = S.learn(state, ctx)
    print(f"mismatch R3 (after Instant m<=14 ACCEPTED): tv_price {L['tv_price']['LOT-T23-1']}")
    assert L["tv_price"]["LOT-T23-1"] >= true_value(RULES, {**LOT1_1203, "moisture_pct": 14.0}) - 0.01
    assert L["tv_price"]["LOT-T23-1"] < 123.96




def test_ceiling_cap_and_no_probe_above_it():
    """Game 13:21 (memory-sandbox-3946b135.json): ทรีอินวัน ABOVE_LIMIT at 0.95 (R5), sold at 0.93 (R6), then R7 went
    0.94 + probe 0.95 and both hit ABOVE_LIMIT again. Now: k <= ceiling − CEIL_GAP and no +PROBE above that."""
    INST = {"max_defect_pct": 15.0, "max_moisture_pct": 15.0, "min_screen": None}
    lot = {"lot_id": "L", "kg_total": 1200, "kg_remaining": 600, "kg_available": 600,
           "sample": {"defect_pct": 10.0, "moisture_pct": 12.0, "screen": 15}}
    def rnd(rid, k, st):
        sent = [{"rfq_id": f"{rid}-1", "lot_id": "L", "kg": 60, "price_per_kg": round(k * 100, 2),
                 "buyer_type": "INSTANT", "spec": INST, "tv_used": 100.0, "markup": k}]
        res = {"round_id": rid, "kind": "real",
               "my_offers": [{"rfq_id": f"{rid}-1", "lot_id": "L", "kg": 60, "price_per_kg": k * 100, "status": st,
                              "filled_kg": 60 if st == "ACCEPTED" else 0}],
               "market_summary": [{"rfq_id": f"{rid}-1", "buyer": "6 ทรีอิน", "buyer_type": "INSTANT", "wanted_kg": 600,
                                   "bought_kg": 300, "offers": 9, "price_min": 90.0, "price_max": 96.0, "price_avg": 93.0}]}
        return sent, res
    s5, r5 = rnd("R5", 0.95, "ABOVE_LIMIT")
    s6, r6 = rnd("R6", 0.93, "ACCEPTED")
    rfqs = [{"rfq_id": f"R7-{j}", "buyer": "6 ทรีอิน", "buyer_type": "INSTANT", "kg": 600, "bags": 10, "spec": INST}
            for j in (1, 2)]
    state = {"round": {"round_id": "R7", "kind": "real", "number": 3, "of": 8, "calendar": "ปกติ"},
             "lots": [lot], "rfqs": rfqs, "round_capacity_kg": 360, "capacity_left_kg": 360}
    ctx = S.Context(RULES, [], {"L": 100.0}, random.Random(1), _Mem([r5, r6], {"R5": s5, "R6": s6}))
    L = S.learn(state, ctx)
    b = L["buyers"]["6 ทรีอิน"]
    k_rfq, probes = S.price_rfqs(state, ctx, L["buyers"], 0.5)
    print("ceiling:", b["k"], b.get("cap"), k_rfq, probes)
    assert b["k"] <= 0.93 + 1e-9
    assert not probes and max(k_rfq.values()) <= 0.93 + 1e-9


def test_fill_prior():
    """fill = (filled + FILL_DEFAULT × FILL_PRIOR_KG) / (offered + FILL_PRIOR_KG) once >= 3 bags offered:
    7 bags offered, 4 sold -> (240 + 420) / (420 + 600) = 0.647 (was 240/420 = 0.571); under 3 bags -> FILL_DEFAULT."""
    INST = {"max_defect_pct": 15.0, "max_moisture_pct": 15.0, "min_screen": None}
    lot = {"lot_id": "L", "kg_total": 1200, "kg_remaining": 960, "kg_available": 960,
           "sample": {"defect_pct": 10.0, "moisture_pct": 12.0, "screen": 15}}

    def game(offered_bags, sold_bags):
        sent = [{"rfq_id": "R1-1", "lot_id": "L", "kg": offered_bags * 60, "price_per_kg": 93.0,
                 "buyer_type": "INSTANT", "spec": INST, "tv_used": 100.0, "markup": 0.93}]
        res = {"round_id": "R1", "kind": "real",
               "my_offers": [{"rfq_id": "R1-1", "lot_id": "L", "kg": offered_bags * 60, "price_per_kg": 93.0,
                              "status": "PARTIAL" if sold_bags else "OUTBID", "filled_kg": sold_bags * 60}],
               "market_summary": [{"rfq_id": "R1-1", "buyer": "i", "buyer_type": "INSTANT", "wanted_kg": 600,
                                   "bought_kg": 600, "offers": 9, "price_min": 90.0, "price_max": 96.0, "price_avg": 93.0}]}
        state = {"round": {"round_id": "R2", "kind": "real", "number": 2, "of": 8, "calendar": "ปกติ"},
                 "lots": [lot], "rfqs": [], "round_capacity_kg": 420, "capacity_left_kg": 420}
        return S.learn(state, S.Context(RULES, [], {"L": 100.0}, random.Random(1), _Mem([res], {"R1": sent})))["fill"]

    f7, f2 = game(7, 4), game(2, 0)
    print(f"fill: 4/7 bags -> {f7:.3f} · 0/2 bags -> {f2:.3f}")
    assert abs(f7 - (240 + S.FILL_DEFAULT * S.FILL_PRIOR_KG) / (420 + S.FILL_PRIOR_KG)) < 1e-9 and abs(f7 - 0.647) < 0.001
    assert f2 == S.FILL_DEFAULT



def test_hold_while_probing_specialty():
    """Games 13:21 / 13:51: screen == min_screen -> Specialty is only probed (1 bag) and the other bags all went to
    Commercial at 0.99 in R1, while Specialty paid 1.06-1.10 later. Now R1 sells Commercial only ceil(6/8) = 1 bag."""
    lot = {"lot_id": "E", "kg_total": 360, "kg_remaining": 360, "kg_available": 360,
           "sample": {"defect_pct": 1.0, "moisture_pct": 11.0, "screen": 16}}
    rfqs = [{"rfq_id": "S1", "buyer": "s1", "buyer_type": "SPECIALTY", "kg": 240, "bags": 4, "spec": SPECIALTY},
            {"rfq_id": "C1", "buyer": "c1", "buyer_type": "COMMERCIAL", "kg": 600, "bags": 10, "spec": COMMERCIAL}]
    state = {"round": {"round_id": "R1", "kind": "real", "number": 1, "of": 8, "calendar": "ปกติ"},
             "lots": [lot], "rfqs": rfqs, "round_capacity_kg": 360, "capacity_left_kg": 360}
    ctx = S.Context(RULES, [], {"E": 130.0}, random.Random(1))
    k = {"S1": 1.06, "C1": 0.99}
    plan = S.build_plan(state, ctx, S.candidates(state, ctx), k, set(), flow=set())
    got = {p["rfq_id"]: p["bags"] for p in plan}
    print("hold:", got, ctx.notes[-2:])
    assert got.get("S1") == 1 and got.get("C1") == 1
    S.HOLD_WHILE_PROBING = False
    try:
        plan = S.build_plan(state, ctx, S.candidates(state, ctx), k, set(), flow=set())
        assert {p["rfq_id"]: p["bags"] for p in plan}.get("C1", 0) > 1
    finally:
        S.HOLD_WHILE_PROBING = True



CAL3 = [{"rounds": "1-5", "label": "ปกติ", "demand_weight": 1.0, "specialty_share": None},
        {"rounds": "6-8", "label": "เทศกาลกาแฟ", "demand_weight": 1.0, "specialty_share": 0.4},
        {"rounds": "9-12", "label": "ปกติ", "demand_weight": 1.0, "specialty_share": None},
        {"rounds": "13-14", "label": "ใกล้ปิดตลาด", "demand_weight": 0.6, "specialty_share": None},
        {"rounds": "15", "label": "รอบสุดท้าย", "demand_weight": 0.3, "specialty_share": None}]


def _rules15():
    r = dict(RULES)
    r["timeline"] = {**RULES["timeline"], "rounds": 15}
    return r


def test_calendar_weights_and_weighted_left():
    ctx = S.Context(_rules15(), CAL3, {}, random.Random(1))
    W = S.calendar_weights(ctx)
    assert W[6] == (1.0, True) and W[13] == (0.6, False) and W[15] == (0.3, False) and W[1] == (1.0, False)
    st = {"round": {"round_id": "R13", "kind": "real", "number": 13, "of": 15}}
    assert abs(S.weighted_left(st, ctx) - 1.5) < 1e-9           # 0.6 + 0.6 + 0.3
    st = {"round": {"round_id": "P1", "kind": "practice", "number": 1, "of": 15}}
    assert abs(S.weighted_left(st, ctx) - 13.5) < 1e-9
    assert abs(S.weighted_left(st, ctx, 1.0) - 16.5) < 1e-9     # festival rounds count double
    bad = S.Context(_rules15(), [{"rounds": "x"}, {"rounds": "3", "demand_weight": "?"}], {}, random.Random(1))
    assert S.calendar_weights(bad)[3] == (1.0, False)            # garbage -> neutral


def test_pressure_uses_calendar():
    lot = {"lot_id": "A", "kg_total": 600, "kg_remaining": 600, "kg_available": 600,
           "sample": {"defect_pct": 5.0, "moisture_pct": 11.0, "screen": 15}}
    st = {"round": {"round_id": "R13", "kind": "real", "number": 13, "of": 15}, "lots": [lot],
          "round_capacity_kg": 300, "capacity_left_kg": 300}
    flat = S.Context(_rules15(), [], {}, random.Random(1))
    cal = S.Context(_rules15(), CAL3, {}, random.Random(1))
    p_flat, p_cal = S.pressure(st, flat, 0.7), S.pressure(st, cal, 0.7)
    assert p_cal > p_flat * 1.9                                  # 3 rounds left but only 1.5 rounds of demand


def test_festival_reserve_limits_commercial_before_festival():
    lot = {"lot_id": "P", "kg_total": 540, "kg_remaining": 540, "kg_available": 540,
           "sample": {"defect_pct": 0.5, "moisture_pct": 11.0, "screen": 18}}
    rfqs = [{"rfq_id": "S1", "buyer": "s1", "buyer_type": "SPECIALTY", "kg": 60, "bags": 1, "spec": SPECIALTY},
            {"rfq_id": "C1", "buyer": "c1", "buyer_type": "COMMERCIAL", "kg": 600, "bags": 10, "spec": COMMERCIAL}]
    state = {"round": {"round_id": "R2", "kind": "real", "number": 2, "of": 15, "calendar": "ปกติ"},
             "lots": [lot], "rfqs": rfqs, "round_capacity_kg": 300, "capacity_left_kg": 300}
    ctx = S.Context(_rules15(), CAL3, {"P": 130.0}, random.Random(1))
    k = {"S1": 1.02, "C1": 1.0}
    plan = S.build_plan(state, ctx, S.candidates(state, ctx), k, set(), flow=True)
    got = {p["rfq_id"]: p["bags"] for p in plan}
    # 9 bags · weight now 1 / (14 rounds left with 3 festival rounds x2 = 15.6) -> ceil(0.58) = 1 commercial bag
    assert got.get("S1") == 1 and got.get("C1") == 1, got
    state["round"].update(round_id="R6", number=6, calendar="เทศกาลกาแฟ")   # in the festival: no reserve
    plan = S.build_plan(state, ctx, S.candidates(state, ctx), k, set(), flow=True)
    assert {p["rfq_id"]: p["bags"] for p in plan}.get("C1", 0) == 4


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("  ok", name)
