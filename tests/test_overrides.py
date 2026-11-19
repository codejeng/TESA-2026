"""#44 overrides.json (strategy.apply_overrides) and SEND_AT (main.play). No market, no data written outside tmp.

    .venv/bin/python -m pytest -q tests/test_overrides.py
"""
import itertools
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import strategy as S  # noqa: E402
from test_sandbox_cases import DATA, replay  # noqa: E402

TAG = "1a781f50"           # a real 8-round sandbox game with reveal and known buyer specs


@pytest.fixture(autouse=True)
def _clean():
    S.reset_overrides()
    yield
    S.reset_overrides()


def _write(p, data):
    p.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _events(p):
    ev, note = S.apply_overrides(p)
    return [e for e, _ in ev], ev, note


# ---------------- file handling ----------------
def test_valid_file_applies_and_missing_key_returns_default(tmp_path):
    p = tmp_path / "overrides.json"
    _write(p, {"set": {"START.SPECIALTY": 1.03, "FLOOR_EARLY": 0.90, "URGENCY_SLOPE": 0.07, "SCHEDULE": True,
                       "BUYER_K_MAX": {"b1": 1.05}, "LOT_K_OFFSET": {"L1": -0.02}},
               "reason": "test", "author": "fable"})
    names, ev, note = _events(p)
    assert names == ["overrides_applied"] and ev[0][1]["reason"] == "test" and ev[0][1]["hash"]
    assert S.START["SPECIALTY"] == 1.03 and S.START["COMMERCIAL"] == 0.99
    assert S.FLOOR_EARLY == 0.90 and S.URGENCY_SLOPE == 0.07 and S.SCHEDULE is True
    assert S.BUYER_K_MAX == {"b1": 1.05} and S.LOT_K_OFFSET == {"L1": -0.02}
    assert note.startswith("overrides: {") and "fable: test" in note
    assert _events(p)[0] == []                                   # same file again: no new event
    _write(p, {"set": {"FLOOR_EARLY": 0.90}, "reason": "drop the rest"})
    assert _events(p)[0] == ["overrides_applied"]
    assert S.START["SPECIALTY"] == 1.06 and S.SCHEDULE is False and S.URGENCY_SLOPE == 0.05
    assert S.BUYER_K_MAX == {} and S.LOT_K_OFFSET == {} and S.FLOOR_EARLY == 0.90
    _write(p, {"set": {}})
    _events(p)
    assert S.FLOOR_EARLY == 0.88 and S.FLOOR_LATE == 0.85


@pytest.mark.parametrize("bad, why", [
    ({"set": {"FLOOR_LATE": 0.80}}, "นอกช่วง"),                  # never below 0.82
    ({"set": {"FLOOR_EARLY": 0.79}}, "นอกช่วง"),
    ({"set": {"START.SPECIALTY": 1.25}}, "นอกช่วง"),
    ({"set": {"URGENCY_MAX": 0.09}}, "นอกช่วง"),
    ({"set": {"SHORT_RATIO": "0.8"}}, "นอกช่วง"),
    ({"set": {"SCHEDULE": 1}}, "true/false"),
    ({"set": {"START.SPECIALTY": True}}, "นอกช่วง"),
    ({"set": {"BUYER_K_MAX": {"b1": 1.31}}}, "นอกช่วง"),
    ({"set": {"BUYER_K_MAX": {"b1": 0.79}}}, "นอกช่วง"),
    ({"set": {"LOT_K_OFFSET": {"L1": 0.06}}}, "นอกช่วง"),
    ({"set": {"LOT_K_OFFSET": [1]}}, "ต้องเป็น"),
    ({"set": {"MIN_MARKUP": 0.5}}, "ไม่อนุญาต"),                 # strange key
    ({"set": {"START.ROBUSTA": 1.0}}, "ไม่อนุญาต"),
    ({"set": {"FLOOR_EARLY": 0.9}, "comment": "x"}, "คีย์บนสุด"),
    ({"FLOOR_EARLY": 0.9}, '"set"'),
    ("{not json", "JSON เสีย"),
    ('{"set": {"FLOOR_EARLY": NaN}}', "นอกช่วง"),
])
def test_bad_file_rejected_whole_and_last_good_kept(tmp_path, bad, why):
    p = tmp_path / "overrides.json"
    _write(p, {"set": {"START.COMMERCIAL": 1.01, "FLOOR_EARLY": 0.91}, "reason": "good"})
    _events(p)
    _write(p, bad)
    names, ev, note = _events(p)
    assert names == ["overrides_rejected"] and why in ev[0][1]["reason"], ev
    assert ev[0][1]["keep"] == {"START.COMMERCIAL": 1.01, "FLOOR_EARLY": 0.91}
    assert S.START["COMMERCIAL"] == 1.01 and S.FLOOR_EARLY == 0.91      # nothing from the bad file
    assert "ถูกปฏิเสธ" in note
    assert _events(p)[0] == []                                   # rejected once, not every round


def test_missing_file(tmp_path):
    p = tmp_path / "overrides.json"
    names, _, note = _events(p)
    assert names == [] and note == "overrides: default" and S.FLOOR_EARLY == 0.88
    _write(p, {"set": {"FLOOR_EARLY": 0.93}})
    _events(p)
    p.unlink()
    names, _, note = _events(p)
    assert names == ["overrides_rejected"] and S.FLOOR_EARLY == 0.93 and "ไม่พบไฟล์" in note
    assert _events(p)[0] == []


# ---------------- effect on real k ----------------
def _offers(round_no=4):
    state, ctx, _ = replay(TAG, round_no)
    offers, _ = S.decide(state, ctx)
    buyer = {r["rfq_id"]: r["buyer"] for r in state["rfqs"]}
    k = {(o["rfq_id"], o["lot_id"]): o["price_per_kg"] / ctx.offer_tv[(o["rfq_id"], o["lot_id"])] for o in offers}
    return state, ctx, offers, buyer, k


def test_buyer_k_max_caps_every_offer_to_that_buyer(tmp_path):
    _, _, offers, buyer, k = _offers()
    top = max(k, key=k.get)
    b, before = buyer[top[0]], k[top]
    cap = round(before - 0.04, 3)
    p = tmp_path / "o.json"
    _write(p, {"set": {"BUYER_K_MAX": {b: cap}}})
    S.apply_overrides(p)
    _, ctx, offers2, buyer2, k2 = _offers()
    mine = [v for key, v in k2.items() if buyer2[key[0]] == b]
    assert mine and max(mine) <= max(cap, S.MIN_MARKUP) + 0.001, (b, cap, mine)
    others = {key: v for key, v in k.items() if buyer[key[0]] != b}
    assert any(abs(k2.get(key, 0) - v) < 0.002 for key, v in others.items())   # other buyers not capped


def test_lot_k_offset_moves_that_lot_and_learn_does_not_absorb_it(tmp_path):
    state, ctx, offers, buyer, k = _offers()
    lot = offers[0]["lot_id"]
    p = tmp_path / "o.json"
    _write(p, {"set": {"LOT_K_OFFSET": {lot: -0.03}}})
    S.apply_overrides(p)
    state2, ctx2, offers2, _, k2 = _offers()
    same = [key for key in k if key in k2 and key[1] == lot]
    assert same and all(k2[key] < k[key] - 0.015 for key in same), [(k[x], k2[x]) for x in same]
    assert ctx2.lot_ov[lot] == pytest.approx(-0.03 if abs(S.learn(state2, ctx2)["lot_off"].get(lot, 0)) <= 0.03 else ctx2.lot_ov[lot])
    assert all(ctx2.offer_ov.get(key) == ctx2.lot_ov[lot] for key in same)
    # total with the learned offset is held to ±0.06
    eff, ov = S.lot_offsets({lot: -0.04})
    assert eff[lot] == -0.06 and ov[lot] == pytest.approx(-0.02)
    # memory stores ov_off; learn() subtracts it, so the buyer's k is learned in the same units as before
    from memory import Memory
    m = Memory(tmp_path / "mem.json")
    m.record_sent("R4", offers2, state2, ctx2.tv, ctx2.offer_tv, ctx2.offer_ov)
    assert all(r.get("ov_off") == pytest.approx(-0.03) for r in m.sent["R4"] if r["lot_id"] == lot)
    assert all("ov_off" not in r for r in m.sent["R4"] if r["lot_id"] != lot)


def test_learn_subtracts_ov_off():
    state, ctx, _ = replay(TAG, 5)
    base = S.learn(state, ctx)["buyers"]
    rid = "R4"
    for o in ctx.memory.sent[rid]:
        o["markup"] = round(o["markup"] - 0.03, 3)
        o["ov_off"] = -0.03
    after = S.learn(state, ctx)["buyers"]
    assert {b: v["k"] for b, v in after.items()} == {b: v["k"] for b, v in base.items()}


def test_no_offer_below_080_tv_for_any_override_set(tmp_path):
    """extremes of every key together, across every replayed round of one game"""
    p = tmp_path / "o.json"
    mem = json.loads((DATA / f"memory-sandbox-{TAG}.json").read_text(encoding="utf-8"))
    rounds = sorted(int(r["round_id"][1:]) for r in mem["rounds"])
    buyers = sorted({s["buyer"] for r in mem["rounds"] for s in r["market_summary"]})
    lots = sorted({o["lot_id"] for offs in mem["sent"].values() for o in offs})
    sets = []
    for fl, urg, bk, lo, flags in itertools.product((0.82, 0.95), ((0.10, 0.08), (0.0, 0.0)), (0.80, 1.30),
                                                    (-0.05, 0.05), (True, False)):
        sets.append({"START.SPECIALTY": 0.85 if flags else 1.20, "START.COMMERCIAL": 0.85, "START.INSTANT": 0.85,
                     "FLOOR_EARLY": fl, "FLOOR_LATE": 0.82, "URGENCY_SLOPE": urg[0], "URGENCY_MAX": urg[1],
                     "SHORT_RATIO": 0.5, "SCHEDULE": flags, "FEST_RESERVE": flags, "LOT_SHARE": flags,
                     "CAL_PRESSURE": flags, "BUYER_K_MAX": {b: bk for b in buyers},
                     "LOT_K_OFFSET": {l: lo for l in lots}})
    n = 0
    for st in sets:
        _write(p, {"set": st})
        assert S.apply_overrides(p)[0][0][0] == "overrides_applied"
        for rn in rounds:
            state, ctx, _ = replay(TAG, rn)
            offers, _ = S.decide(state, ctx)
            for o in offers:
                tv = ctx.offer_tv[(o["rfq_id"], o["lot_id"])]
                assert o["price_per_kg"] >= 0.80 * tv and o["price_per_kg"] >= S.MIN_MARKUP * tv - 0.01, (st, o, tv)
                n += 1
    assert n > 100
