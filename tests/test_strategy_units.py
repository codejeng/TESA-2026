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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("  ok", name)
