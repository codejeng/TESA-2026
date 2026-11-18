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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("  ok", name)
