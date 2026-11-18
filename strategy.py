"""Decide this round's offers — กลยุทธ์ "ตามราคาตลาดรายผู้ซื้อ" (buyer-tracking) v2

ภาพรวม (ทุกรอบ):
  1. learn()        ไล่ผลทุกรอบจาก memory (เรียงตามเลขรอบ) แล้วคำนวณใหม่ทั้งหมด -> รีสตาร์ทไม่ลืม
                      - k ของผู้ซื้อแต่ละราย (ไม่ใช่แค่ประเภท)
                      - ส่วนลดเฉพาะล็อต (ล็อตที่แพ้ตลอดทั้งที่ล็อตอื่นราคาเดียวกันชนะ = TV เราประเมินสูงไป)
                      - อัตราขายได้ของเรา (fill rate)
  2. pressure()     ของที่เหลือ เทียบกับที่ยังขายได้จริงภายใต้เพดานกระสอบต่อรอบ -> ตัดสินว่าถือของได้ไหม
  3. price_rfqs()   k ของแต่ละ RFQ = k ผู้ซื้อ − ความเร่ง (+ ซองหยั่งราคาเมื่อขายทันแผน)
  4. build_plan()   แจกกระสอบทีละ 1 ไปยัง RFQ ที่จ่ายดีสุดแบบวนรอบ จนเต็ม capacity
  5. llm_adjust()   (ไม่บังคับ) LLM ขยับ k รายผู้ซื้อได้ไม่เกิน ±0.02
  6. guard()        ตรวจทุกซองก่อนส่ง

price_per_kg = k × TV ที่เราประเมิน · ผู้ซื้อเรียงซองด้วย ราคา ÷ TV จริง (ถูกสุดได้ก่อน) แล้วจ่ายตามซอง
"""
import json
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field

from appraisal import true_value

BAG = 60

# ---------------- ตัวเลขที่จูนได้ ----------------
START = {"SPECIALTY": 1.06, "COMMERCIAL": 0.99, "INSTANT": 0.93}   # Specialty 1.10 -> 1.06 (ตลาดฝึก 18 พ.ย. ขายได้ที่ 1.057–1.107)
MIN_MARKUP, MAX_MARKUP = 0.85, 1.40     # guard: ห้ามออกนอกช่วงนี้ไม่ว่ากรณีใด
FLOOR_EARLY = 0.88                      # ขายทันแผน: ไม่ขายต่ำกว่านี้ (ถือไว้ขายรอบหลังได้)
FLOOR_LATE = 0.85                       # ขายไม่ทันแผน หรือ 2 รอบสุดท้าย: ยอมลงถึงนี้ (ยังดีกว่า 0.80)

UP = 0.01            # ขายหมด -> ขึ้น
UP_FAST = 0.02       # ขายหมด และผู้ซื้อยังซื้อไม่ครบ -> ขึ้นเร็ว
DOWN = 0.01          # แพ้หมด -> ลง
MAX_DROP = 0.03      # ลงได้มากสุดต่อรอบ
BELOW_CEIL = 0.02    # ABOVE_LIMIT ทุกซอง -> ต่ำกว่าเพดานที่เจอเท่านี้
CEIL_MEMORY = 3      # จำเพดานไว้กี่รอบ
CEIL_GAP = 0.02      # k ต้องต่ำกว่าเพดานล่าสุดอย่างน้อยเท่านี้ (เพดานแต่ละ RFQ สุ่มต่างกันราว ±0.02)
PROBE_UNDER_CEIL = True  # ซองหยั่งราคา (+PROBE) ต้องไม่เกินเพดานล่าสุด − CEIL_GAP
MKT_ADJ = 0.01       # price_max ÷ TV ประมาณต่ำกว่าจุดตัดจริง -> บวกชดเชยเท่านี้
DRIFT_SEEN = 0.3     # รอบที่ไม่ได้ยื่นให้ผู้ซื้อที่เคยยื่น: ขยับเข้าหาราคาตลาดเท่านี้
DRIFT_NEW = 0.5      # ผู้ซื้อที่ไม่เคยยื่นเลย: ขยับเข้าหาราคาตลาดเท่านี้

LOT_STEP = 0.01      # ล็อตแพ้ทั้งที่ล็อตอื่นราคาเดียวกันชนะ -> ลดราคาล็อตนี้ลง
LOT_MAX_OFF = 0.04
BOUNDARY_DEFECT = 0.40   # เมล็ดเสียห่างขอบเกรดไม่ถึงเท่านี้ = TV อาจกระโดด 4% -> ไม่ใช้สอน k ผู้ซื้อ (noise ตลาดฝึก ~0.2–0.7)
BOUNDARY_MOIST = 0.50    # ความชื้นห่าง hard_limit ไม่ถึงเท่านี้ (noise ตลาดฝึกถึง 0.9–1.4)

PROBE = 0.01         # ซองหยั่งราคา: สูงกว่าราคาหลักเท่านี้ · 1 กระสอบ · เฉพาะตอนขายทันแผน
PROBE_MAX_PRESSURE = 0.8
FLOW_PRESSURE = 0.9  # ไหลไปทางรองเมื่อทางดีสุดเต็ม: เฉพาะตอน pressure >= ค่านี้ หรือล็อตนั้นเพิ่งแพ้รอบก่อน
FLOW_ON_LOSS = True
HOLD_WHILE_PROBING = True  # ล็อตที่กำลังหยั่งสเปกผู้ซื้อที่จ่ายดีกว่า: ขายทางรองแค่ส่วนแบ่งต่อรอบ รอผลหยั่งก่อน
SWITCH_GAP = 0.03    # ล็อตไปขายผู้ซื้อที่จ่ายต่ำกว่าทางที่ดีสุดของมันได้ไม่เกินเท่านี้
FILL_PRIOR_KG = 600   # ถ่วง fill ด้วย FILL_DEFAULT เหมือนเคยยื่นไปแล้ว 10 กระสอบ -> ต้นเกมไม่ตื่นตูมจากข้อมูลไม่กี่ซอง
FILL_DEFAULT = 0.7   # อัตราขายได้ตั้งต้น (ยังไม่มีข้อมูล)
TIGHT = 1.0          # pressure เกินนี้ = ขายไม่ทัน -> ใช้ FLOOR_LATE
URGENCY_SLOPE = 0.05 # pressure เกิน 1 ทุก 0.1 -> ลด k 0.005
URGENCY_MAX = 0.04
FESTIVAL_BONUS = 0.01
LAST_LADDER = (0.0, -0.03, -0.06)   # รอบจริงรอบสุดท้าย: ไล่ราคาลงทีละซอง ซองท้ายๆ ลด 0.03, 0.06 (ไม่ต่ำกว่า FLOOR_LATE)
                                    # ซองของผู้ซื้อที่รอบก่อนซองเราโดน ABOVE_LIMIT ได้ราคาต่ำก่อน
PRACTICE_LADDER = (-0.03, 0.0, 0.03)    # รอบซ้อม: ยื่นหลายระดับเพื่อหยั่งตลาด (ถ้ารอบซ้อมตัดสต็อกจริง ให้ตั้ง (0, 0, 0))
PACE_NO_CAP = 2.0

MARGIN_DEFECT = 0.40     # ผ่านสเปกแต่ห่างขอบไม่ถึงเท่านี้ = "เฉียดขอบ" (ผลวัดตัวอย่างคลาดได้) · screen == min_screen ก็เฉียดขอบ
MARGIN_MOIST = 0.40
MISMATCH_PAD = 0.3       # SPEC_MISMATCH ที่ระบุแกนได้: ตั้งราคาด้วยค่าแกนนั้น = max(ตัวอย่าง, ขีด) + เท่านี้ (screen: −1)
SPEC_PROBE_BAGS = 1      # ล็อตเฉียดขอบที่ยังไม่รู้ว่าผ่านสเปกนั้นจริงไหม: ยื่นได้เท่านี้ต่อรอบต่อสเปก
                         # ผลเป็น ACCEPTED/PARTIAL/OUTBID = ผ่านจริง -> ยื่นเต็ม · SPEC_MISMATCH = ไม่ผ่าน -> ห้ามยื่นสเปกนั้นอีก
OVER_MOIST = 1.0         # ตัวอย่างเกินสเปกไม่เกินเท่านี้ ค่าจริงอาจผ่าน -> หยั่ง 1 กระสอบ/รอบ ไปยังสเปกที่หลวมที่สุด
OVER_DEFECT = 0.5
OVER_SCREEN = 1

LLM_MAX_DELTA = 0.02
LLM_MAX_SECONDS = 20
# -------------------------------------------------


@dataclass
class Context:
    rules: dict
    calendar: list
    tv: dict
    rng: random.Random
    memory: object = None
    llm: object = None
    seconds_left: float = 999
    notes: list = field(default_factory=list)
    tv_price: dict | None = None      # lot -> TV ที่ใช้ตั้งราคา (learn(): ปรับตามสเปกที่รู้ว่าผ่านจริง) · None = ใช้ tv
    spec_sample: dict | None = None   # lot -> ค่าคุณภาพที่ใช้เช็กสเปก (ตัวอย่างที่ตัดด้วยสเปกที่ผ่านจริง)
    offer_tv: dict = field(default_factory=dict)   # (rfq_id, lot_id) -> TV ที่ใช้คิดราคาซองนั้น (main.py เก็บลง memory)


# ---------------- ตัวช่วย ----------------
def spec_ok(sample: dict, spec: dict, margin: bool = False) -> bool:
    """margin=True: ต้องห่างขอบ (defect/moisture เกิน MARGIN_*, screen มากกว่าขั้นต่ำอย่างน้อย 1 เพราะ screen คลาดได้ ±1)"""
    md, mm, ms = (MARGIN_DEFECT, MARGIN_MOIST, 1) if margin else (0.0, 0.0, 0)
    return (sample["defect_pct"] <= spec["max_defect_pct"] - md
            and sample["moisture_pct"] <= spec["max_moisture_pct"] - mm
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"] + ms))


def clip_to_spec(sample: dict, spec: dict) -> dict:
    """ค่าคุณภาพที่ "อย่างแย่สุด" ยังผ่านสเปกนี้ (ใช้เมื่อรู้ว่าผ่านจริง หรือเพื่อตั้งราคาซองหยั่ง)"""
    return {"defect_pct": min(sample["defect_pct"], spec["max_defect_pct"]),
            "moisture_pct": min(sample["moisture_pct"], spec["max_moisture_pct"]),
            "screen": max(sample["screen"], spec["min_screen"] or 0)}


def over_spec(sample: dict, spec: dict, rules: dict) -> bool:
    """ไม่ผ่านสเปกตามตัวอย่าง แต่เกินไม่มาก (OVER_*) ค่าจริงอาจผ่าน · ราคาที่ตัดตามสเปกต้องไม่ติดโทษ hard limit"""
    if spec_ok(sample, spec):
        return False
    if (sample["defect_pct"] - spec["max_defect_pct"] > OVER_DEFECT
            or sample["moisture_pct"] - spec["max_moisture_pct"] > OVER_MOIST
            or (spec["min_screen"] or 0) - sample["screen"] > OVER_SCREEN):
        return False
    return clip_to_spec(sample, spec)["moisture_pct"] <= rules["moisture"]["hard_limit"]


def spec_key(spec: dict) -> tuple:
    return (spec["max_defect_pct"], spec["max_moisture_pct"], spec["min_screen"] or 0)


def spec_known(lot_id: str, spec: dict, passed: set, failed: set) -> bool | None:
    """True = เคยผ่านสเปกนี้ (หรือสเปกที่เข้มกว่า) จริง · False = เคย SPEC_MISMATCH กับสเปกนี้ (หรือที่หลวมกว่า) · None = ยังไม่รู้"""
    d, m, s = spec_key(spec)
    if any(l == lot_id and d <= fd and m <= fm and s >= fs for l, (fd, fm, fs) in failed):
        return False
    if any(l == lot_id and pd <= d and pm <= m and ps >= s for l, (pd, pm, ps) in passed):
        return True
    return None


def rounds_left(state: dict, ctx: Context) -> int:
    rnd, total = state["round"], ctx.rules["timeline"]["rounds"]
    return total if rnd["kind"] == "practice" else max(1, total - rnd["number"] + 1)


def near_boundary(sample: dict, rules: dict) -> bool:
    """ผลวัดอยู่ใกล้จุดที่ TV กระโดด (ขอบเกรด หรือขอบความชื้นหนัก) -> TV ที่ประเมินอาจผิดหลาย %"""
    d, m = sample["defect_pct"], sample["moisture_pct"]
    if any(abs(d - s["max_defect_pct"]) < BOUNDARY_DEFECT for s in rules["defect_steps"]):
        return True
    return abs(m - rules["moisture"]["hard_limit"]) < BOUNDARY_MOIST


def worse_side(ss: dict, rules: dict, known_d: float, known_m: float) -> dict:
    """ล็อตใกล้ขอบที่ราคาโดน ABOVE_LIMIT: สมมติว่าค่าจริงข้ามขอบที่ใกล้ที่สุดไปฝั่งแย่ (defect = ขอบ+0.01 หรือ
    moisture = hard_limit+0.01) · ขอบที่สเปกที่ผ่านจริงพิสูจน์แล้วว่าไม่ข้าม (known_*) ไม่นับ"""
    d, m = ss["defect_pct"], ss["moisture_pct"]
    opts = []
    for st in rules["defect_steps"][:-1]:
        b = st["max_defect_pct"]
        if d <= b and b - d < BOUNDARY_DEFECT and known_d > b:
            opts.append(((b - d) / BOUNDARY_DEFECT, {"defect_pct": round(b + 0.01, 2)}))
    hard = rules["moisture"]["hard_limit"]
    if m <= hard and hard - m < BOUNDARY_MOIST and known_m > hard:
        opts.append(((hard - m) / BOUNDARY_MOIST, {"moisture_pct": round(hard + 0.01, 2)}))
    return {**ss, **min(opts, key=lambda o: o[0])[1]} if opts else ss


def mismatch_axis(sample: dict, spec: dict) -> tuple[str, float] | None:
    """SPEC_MISMATCH: ถ้ามีแกนเดียวที่ตัวอย่างอยู่ใกล้ขอบ (ห่างไม่ถึง MARGIN หรือเกินขอบแล้ว) แกนนั้นคือแกนที่ค่าจริงหลุดสเปก
    คืน (แกน, ค่าที่ใช้ตั้งราคา) · หลายแกนหรือไม่มีแกนไหนใกล้ = ระบุไม่ได้ -> None"""
    near = []
    if spec["max_defect_pct"] - sample["defect_pct"] < MARGIN_DEFECT:
        near.append(("defect_pct", round(max(sample["defect_pct"], spec["max_defect_pct"]) + MISMATCH_PAD, 2)))
    if spec["max_moisture_pct"] - sample["moisture_pct"] < MARGIN_MOIST:
        near.append(("moisture_pct", round(max(sample["moisture_pct"], spec["max_moisture_pct"]) + MISMATCH_PAD, 2)))
    if spec["min_screen"] is not None and sample["screen"] - spec["min_screen"] < 1:
        near.append(("screen", min(sample["screen"], spec["min_screen"]) - 1))
    return near[0] if len(near) == 1 else None


def round_order(rnd: dict) -> tuple:
    digits = re.findall(r"\d+", str(rnd.get("round_id", "")))
    return (0 if rnd.get("kind") == "practice" else 1, int(digits[0]) if digits else 0)


def tv_reference(state: dict, ctx: Context) -> dict:
    ref = {}
    for r in state["rfqs"]:
        tvs = [ctx.tv[l["lot_id"]] for l in state["lots"] if spec_ok(l["sample"], r["spec"])]
        if tvs:
            ref[r["buyer_type"]] = max(ref.get(r["buyer_type"], 0), max(tvs))
    best = max(ctx.tv.values()) if ctx.tv else 100.0
    return defaultdict(lambda: best, ref)


# ---------------- 1. เรียนรู้ ----------------
def learn(state: dict, ctx: Context) -> dict:
    """คืน {"buyers": {...}, "lot_off": {lot: offset}, "fill": อัตราขายได้}"""
    mem = ctx.memory
    tv_ref = tv_reference(state, ctx)
    boundary = {l["lot_id"]: near_boundary(l["sample"], ctx.rules) for l in state["lots"]}
    buyers, lot_off = {}, defaultdict(float)
    offered = filled = 0
    passed, failed = set(), set()                     # {(lot_id, spec_key)} ที่รู้ผลจริงจากผู้ซื้อแล้ว
    # memory เก่าที่ยังไม่เก็บ spec: เดาจาก buyer_type ถ้าทุก RFQ ของประเภทนั้นในรอบนี้ใช้สเปกเดียวกัน
    type_specs = defaultdict(set)
    for r in state["rfqs"]:
        type_specs[r["buyer_type"]].add(spec_key(r["spec"]))
    rounds = sorted(mem.rounds, key=round_order) if mem else []
    limited_lots = set()                              # ล็อตใกล้ขอบที่เคยโดน ABOVE_LIMIT -> ตั้งราคาด้วย TV ฝั่งแย่
    samples = {l["lot_id"]: l["sample"] for l in state["lots"]}
    mismatch_px = defaultdict(dict)                   # lot -> {แกน: ค่าที่ใช้ตั้งราคา} จาก SPEC_MISMATCH ที่ระบุแกนได้

    for i, rnd in enumerate(rounds):
        sent = {(o["rfq_id"], o["lot_id"]): o for o in mem.sent.get(rnd["round_id"], [])}
        summ = {s["rfq_id"]: s for s in rnd.get("market_summary") or []}

        # ผลของซองเรา: base_k = k ที่ส่ง − ส่วนลดเฉพาะล็อต ณ ตอนนั้น (replay จึงได้ค่าเดียวกับตอนส่ง)
        entries = defaultdict(list)                   # buyer -> [(lot, status, base_k, lost_bags, won_bags)]
        for o in rnd.get("my_offers") or []:
            s, m = sent.get((o["rfq_id"], o["lot_id"])), summ.get(o["rfq_id"])
            if s:                                     # หยั่งสเปก: OUTBID ก็แปลว่าผ่านสเปกแล้ว (ผู้ซื้อคัดสเปกก่อนเรียงราคา)
                key = spec_key(s["spec"]) if s.get("spec") else (
                    next(iter(type_specs[s.get("buyer_type")])) if len(type_specs[s.get("buyer_type")]) == 1 else None)
                if key and o["status"] in ("ACCEPTED", "PARTIAL", "OUTBID"):
                    passed.add((o["lot_id"], key))
                elif key and o["status"] == "SPEC_MISMATCH":
                    failed.add((o["lot_id"], key))
                    ax = mismatch_axis(samples[o["lot_id"]], s["spec"]) if s.get("spec") and o["lot_id"] in samples else None
                    if ax:
                        a, v = ax
                        old = mismatch_px[o["lot_id"]].get(a)
                        mismatch_px[o["lot_id"]][a] = v if old is None else (min(old, v) if a == "screen" else max(old, v))
            if not s or not m or o["status"] in ("SPEC_MISMATCH", "CANCELLED"):
                continue
            won_b = o.get("filled_kg", 0) // BAG
            entries[m["buyer"]].append((o["lot_id"], o["status"], s["markup"] - lot_off[o["lot_id"]],
                                        o["kg"] // BAG - won_b, won_b))       # (..., กระสอบที่แพ้, กระสอบที่ชนะ)
            if o["status"] == "ABOVE_LIMIT" and boundary.get(o["lot_id"]):
                limited_lots.add(o["lot_id"])
            if rnd.get("kind") != "practice":
                offered += o["kg"]
                filled += o.get("filled_kg", 0)

        # ส่วนลดเฉพาะล็อต: แพ้ ทั้งที่ล็อตอื่นของเรา k เท่ากันหรือสูงกว่าขายได้ที่ผู้ซื้อรายเดียวกัน
        for res in entries.values():
            won = [k for _, st, k, *_ in res if st in ("ACCEPTED", "PARTIAL")]
            for lot, st, k, *_ in res:
                if st in ("OUTBID", "ABOVE_LIMIT") and won and max(won) >= k - 1e-9:
                    lot_off[lot] = max(-LOT_MAX_OFF, lot_off[lot] - LOT_STEP)
                elif st == "ACCEPTED" and lot_off[lot] < 0:
                    lot_off[lot] = min(0.0, lot_off[lot] + LOT_STEP / 2)

        rows_by_buyer = defaultdict(list)
        for m in summ.values():
            rows_by_buyer[m["buyer"]].append(m)
        # ผู้ซื้อที่รอบนี้ซองของเราโดน ABOVE_LIMIT: รอบสุดท้ายให้ซองราคาต่ำไปที่ผู้ซื้อกลุ่มนี้ก่อน
        last_limited = {b for b, res in entries.items() if any(st == "ABOVE_LIMIT" for _, st, *_ in res)}

        for buyer, rows in rows_by_buyer.items():
            t = rows[0]["buyer_type"]
            b = buyers.setdefault(buyer, {"type": t, "k": START.get(t, 1.0), "ceil": [], "own": False, "last": ""})
            sold = [r for r in rows if r["bought_kg"] > 0 and r.get("price_max")]
            k_mkt = max(MIN_MARKUP, max(r["price_max"] for r in sold) / tv_ref[t] + MKT_ADJ) if sold else None
            short = any(r["bought_kg"] < r["wanted_kg"] for r in rows)
            res = entries.get(buyer, [])
            # ล็อตใกล้ขอบ: TV เราอาจผิดหลาย % -> ไม่ใช้สอน k ผู้ซื้อ ถ้าไม่มีล็อตอื่นก็ใช้ได้ แต่ไม่ใช้ ABOVE_LIMIT สอนเพดาน
            clean = [(st, k, lo, wo) for lot, st, k, lo, wo in res if not boundary.get(lot)] or [
                (st, k, lo, wo) for _, st, k, lo, wo in res if st != "ABOVE_LIMIT"]
            st_all = [x[0] for x in clean]
            k_old = b["k"]

            if clean:
                b["own"] = True
                sold_k = [x[1] for x in clean if x[0] in ("ACCEPTED", "PARTIAL")]
                if "ABOVE_LIMIT" in st_all:
                    c = min(x[1] for x in clean if x[0] == "ABOVE_LIMIT")
                    b["ceil"].append((i, c))
                    new = max(sold_k) if sold_k else c - BELOW_CEIL
                    why = "เกินเพดานบางซอง คงราคาที่ขายได้" if sold_k else f"เกินเพดาน {c:.3f}"
                elif all(st == "ACCEPTED" for st in st_all):
                    new = max(x[1] for x in clean) + (UP_FAST if short else UP)
                    why = "ขายหมด" + (" + ผู้ซื้อยังขาดของ" if short else "")
                elif all(st == "OUTBID" for st in st_all):
                    lo = min(x[1] for x in clean)
                    new = lo - DOWN
                    if k_mkt:
                        new = max(lo - MAX_DROP, min(new, k_mkt - 0.005))
                    why = "แพ้หมด" + (f" (ตลาด≈{k_mkt:.3f})" if k_mkt else "")
                else:                                 # ผลปนกัน: ยึด k ต่ำสุดที่ขายได้ ไม่ใช่สูงสุด
                    new = min(sold_k) if sold_k else k_old
                    why = "ผลปนกัน ใช้ k ต่ำสุดที่ขายได้"
                    lost, won_n = sum(x[2] for x in clean), sum(x[3] for x in clean)
                    if sold_k and lost > won_n:       # แพ้มากกว่าชนะ: ลงไปใต้ k ต่ำสุดที่แพ้
                        new = min(new, min(x[1] for x in clean if x[2] > 0)) - 0.005
                        why += f" · แพ้ {lost} > ชนะ {won_n} กระสอบ"
            elif k_mkt:
                w = DRIFT_SEEN if b["own"] else DRIFT_NEW
                new = k_old + w * ((k_mkt - 0.005) - k_old)
                why = f"ไม่ได้ยื่น · ขยับหาตลาด≈{k_mkt:.3f}"
            else:
                new, why = k_old, "ไม่มีข้อมูลใหม่"

            recent = [c for j, c in b["ceil"] if i - j < CEIL_MEMORY]
            b["cap"] = round(min(recent) - CEIL_GAP, 3) if recent else None
            if recent and new > b["cap"]:
                new, why = b["cap"], why + " · ติดเพดานล่าสุด"
            b["k"] = round(min(max(new, MIN_MARKUP), MAX_MARKUP), 3)
            b["last"] = f"{rnd['round_id']}: {'/'.join(st_all) or '-'} -> {b['k']:.3f} ({why})"

    fill = (min(1.0, max(0.3, (filled + FILL_DEFAULT * FILL_PRIOR_KG) / (offered + FILL_PRIOR_KG)))
            if offered >= 3 * BAG else FILL_DEFAULT)

    # ค่าคุณภาพสำหรับเช็กสเปกและตั้งราคา: ล็อตที่เคยผ่านสเปกจริง ค่าจริงต้องอยู่ในสเปกนั้น -> ตัดตัวอย่างด้วยสเปกที่ผ่าน
    spec_sample, tv_price = {}, {}
    for lot in state["lots"]:
        ss = dict(lot["sample"])
        for l, (pd, pm, ps) in passed:
            if l == lot["lot_id"]:
                ss = clip_to_spec(ss, {"max_defect_pct": pd, "max_moisture_pct": pm, "min_screen": ps or None})
        spec_sample[lot["lot_id"]] = ss
        # ค่าที่ใช้ตั้งราคา (ไม่ใช้เช็กสเปก): แกนที่ SPEC_MISMATCH ระบุได้ -> ฝั่งแย่ · แล้วตัดด้วยสเปกที่ผ่านจริง (ค่าที่พิสูจน์แล้วชนะ)
        price_s = {**lot["sample"], **mismatch_px.get(lot["lot_id"], {})}
        for l, (pd, pm, ps) in passed:
            if l == lot["lot_id"]:
                price_s = clip_to_spec(price_s, {"max_defect_pct": pd, "max_moisture_pct": pm, "min_screen": ps or None})
        if lot["lot_id"] in limited_lots:
            mine = [k for l, k in passed if l == lot["lot_id"]]
            price_s = worse_side(price_s, ctx.rules, min((k[0] for k in mine), default=100.0),
                                 min((k[1] for k in mine), default=100.0))
            lot_off.pop(lot["lot_id"], None)          # TV ฝั่งแย่แก้ราคาแล้ว ไม่ลดซ้ำ
        tv_price[lot["lot_id"]] = (round(true_value(ctx.rules, price_s), 2) if price_s != lot["sample"]
                                   else ctx.tv[lot["lot_id"]])
    return {"buyers": buyers, "lot_off": {k: round(v, 3) for k, v in lot_off.items() if v}, "fill": fill,
            "spec": (passed, failed), "limited_buyers": last_limited if rounds else set(),
            "spec_sample": spec_sample, "tv_price": tv_price}


# ---------------- 2. ขายทันไหม ----------------
def sellable_lots(state: dict, ctx: Context, L: dict) -> set:
    """ล็อตที่ยังมีทางขาย: ผ่าน (หรืออยู่ในช่วงหยั่งของ) สเปก RFQ ใดๆ ที่เห็นในเกมนี้ และสเปกนั้นยังไม่ถูก SPEC_MISMATCH บล็อก"""
    specs = {spec_key(r["spec"]): r["spec"] for r in state["rfqs"]}
    for offers in (ctx.memory.sent.values() if ctx.memory else []):
        for o in offers:
            if o.get("spec"):
                specs.setdefault(spec_key(o["spec"]), o["spec"])
    passed, failed = L["spec"]
    out = set()
    for lot in state["lots"]:
        ss = L["spec_sample"].get(lot["lot_id"], lot["sample"])
        if any((spec_ok(ss, sp) or over_spec(ss, sp, ctx.rules))
               and spec_known(lot["lot_id"], sp, passed, failed) is not False for sp in specs.values()):
            out.add(lot["lot_id"])
    return out


def pressure(state: dict, ctx: Context, fill: float, sellable: set | None = None) -> float:
    """ของที่เหลือ ÷ ที่น่าจะยังขายได้ (เพดานกระสอบต่อรอบ × รอบที่เหลือ × อัตราขายได้)
    > 1 = ถ้าขายแบบนี้ต่อจะมีของเหลือไปขายที่ 0.80 แน่ๆ · นับเฉพาะล็อตที่ยังมีทางขาย (sellable) ถ้าให้มา"""
    lots = [l for l in state["lots"] if sellable is None or l["lot_id"] in sellable]
    left_bags = sum(l["kg_remaining"] for l in lots) / BAG
    cap = state.get("round_capacity_kg")
    if cap is None:
        cap = state.get("capacity_left_kg")
    if cap is None:            # ไม่มีเพดาน: เทียบกับแผนขายเท่าๆ กันทุกรอบ
        total = sum(l["kg_total"] for l in lots) / BAG or 1
        expected = total * rounds_left(state, ctx) / ctx.rules["timeline"]["rounds"]
        return left_bags / max(expected, 1)
    return left_bags / max(cap / BAG * rounds_left(state, ctx) * fill, 1)


# ---------------- 3. ตั้งราคาแต่ละ RFQ ----------------
def price_rfqs(state: dict, ctx: Context, buyers: dict, pres: float) -> tuple[dict, set]:
    rnd = state["round"]
    practice = rnd["kind"] == "practice"
    left = rounds_left(state, ctx)
    tight = not practice and (pres > TIGHT or left <= 2)
    floor = FLOOR_LATE if tight else FLOOR_EARLY
    cut = 0.0 if practice else min(URGENCY_MAX, max(0.0, (pres - 1.0) * URGENCY_SLOPE))
    bonus = FESTIVAL_BONUS if "เทศกาล" in str(rnd.get("calendar", "")) else 0.0
    allow_probe = not practice and pres < PROBE_MAX_PRESSURE and left > 1

    by_type = defaultdict(list)
    for b in buyers.values():
        by_type[b["type"]].append(b["k"])

    k_rfq, probes, seen = {}, set(), defaultdict(int)
    for r in sorted(state["rfqs"], key=lambda r: (r["buyer"], -r["bags"])):
        t = r["buyer_type"]
        base = buyers[r["buyer"]]["k"] if r["buyer"] in buyers else (
            sum(by_type[t]) / len(by_type[t]) if by_type[t] else START.get(t, 1.0))
        n = seen[r["buyer"]]
        seen[r["buyer"]] += 1
        if practice:
            k = base + PRACTICE_LADDER[n % len(PRACTICE_LADDER)]
        else:
            k = base + bonus - cut
            cap = (buyers.get(r["buyer"]) or {}).get("cap")
            if allow_probe and n == 1 and not (PROBE_UNDER_CEIL and cap is not None and k + PROBE > cap + 1e-9):
                k += PROBE
                probes.add(r["rfq_id"])
        k_rfq[r["rfq_id"]] = round(min(max(k, floor), MAX_MARKUP), 3)
    ctx.notes.append(f"pressure {pres:.2f} · urgency -{cut:.3f} · floor {floor} · probe {sorted(probes) or '-'} · k: "
                     + ", ".join(f"{r['rfq_id']}={k_rfq[r['rfq_id']]}" for r in state["rfqs"]))
    return k_rfq, probes


# ---------------- 4. แจกกระสอบ ----------------
def candidates(state: dict, ctx: Context) -> list[dict]:
    """คู่ (ล็อต × RFQ) ที่ยื่นได้ · tv = TV ที่ใช้ตั้งราคาคู่นั้น
    over = ตัวอย่างเกินสเปกเล็กน้อย (หยั่งเฉพาะสเปกที่หลวมที่สุดของล็อตนั้น ราคาคิดจากค่าที่ตัดตามสเปก)"""
    ss_all = ctx.spec_sample or {}
    tvp = ctx.tv_price or ctx.tv
    out = []
    for lot in state["lots"]:
        if lot["kg_available"] < BAG:
            continue
        lid = lot["lot_id"]
        ss = ss_all.get(lid, lot["sample"])
        over = []
        for r in state["rfqs"]:
            base = {"lot_id": lid, "rfq_id": r["rfq_id"], "buyer": r["buyer"], "buyer_type": r["buyer_type"],
                    "spec": r["spec"]}
            if spec_ok(ss, r["spec"]):
                out.append({**base, "safe": spec_ok(ss, r["spec"], margin=True), "over": False, "tv": tvp[lid]})
            elif over_spec(ss, r["spec"], ctx.rules):
                over.append({**base, "safe": False, "over": True,
                             "tv": round(true_value(ctx.rules, clip_to_spec(ss, r["spec"])), 2)})
        if over:
            loose = max((spec_key(c["spec"]) for c in over), key=lambda k: (k[1], k[0], -k[2]))   # ชื้นได้มากสุด > เสียได้มากสุด > ขนาดต่ำสุด
            out += [c for c in over if spec_key(c["spec"]) == loose]
    return out


def round_budget_bags(state: dict, ctx: Context) -> int:
    avail = sum(l["kg_available"] // BAG for l in state["lots"])
    cap = state.get("capacity_left_kg")
    if cap is not None:
        return min(avail, cap // BAG)
    left = rounds_left(state, ctx)
    return avail if left <= 1 else min(avail, math.ceil(avail / left * PACE_NO_CAP))


def classify_spec(cands: list[dict], spec_mem: tuple | None, last: bool, notes: list | None = None) -> list[dict]:
    """ok = ห่างขอบสเปก หรือเคยผ่านสเปกนี้จริง · probe = เฉียดขอบและยังไม่รู้ (ยื่นได้ SPEC_PROBE_BAGS)
    เคย SPEC_MISMATCH -> ตัดทิ้ง · รอบสุดท้าย ล็อตที่ไม่มีทาง ok เลยยื่น probe ได้เต็ม (ไม่ขายก็เหลือแค่ 0.80)"""
    passed, failed = spec_mem or (set(), set())
    out = []
    for c in cands:
        known = spec_known(c["lot_id"], c["spec"], passed, failed)
        if known is False:
            if notes is not None:
                notes.append(f"spec blocked {c['lot_id']}->{c['rfq_id']} (เคย SPEC_MISMATCH)")
            continue
        out.append({**c, "probe": not c["safe"] and known is None})
    has_ok = {c["lot_id"] for c in out if not c["probe"]}
    for c in out:
        c["probe_limit"] = c["probe"] and (c.get("over") or not (last and c["lot_id"] not in has_ok))
    if notes is not None:
        pr = sorted({f"{c['lot_id']}~{spec_key(c['spec'])}" for c in out if c["probe"]})
        if pr:
            notes.append(f"spec probe: {', '.join(pr)}")
    return out


def build_plan(state: dict, ctx: Context, cands: list[dict], k_rfq: dict, probes: set,
               lot_off: dict | None = None, spec_mem: tuple | None = None, limited: set | None = None,
               flow: set | bool = True) -> list[dict]:
    lot_off = lot_off or {}
    last = rounds_left(state, ctx) <= 1
    rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
    pairs = classify_spec(cands, spec_mem, last, ctx.notes)
    # ทางที่นับเป็น "ทางที่ดีสุด" ของล็อต: ทางที่ ok เท่านั้น (ซองหยั่งสเปก 1 กระสอบไม่ใช่ทางขายจริง)
    has_ok = {c["lot_id"] for c in pairs if not c["probe"]}
    routes = defaultdict(list)                        # lot -> [rfq_id] ที่ใช้คิดทางที่ดีสุด
    for c in pairs:
        if not c["probe"] or c["lot_id"] not in has_ok:
            routes[c["lot_id"]].append(c["rfq_id"])
    probe_used = defaultdict(int)                     # (lot, spec_key) -> กระสอบที่ใช้หยั่งรอบนี้
    # ล็อตที่หยั่งสเปกไปผู้ซื้อที่จ่ายดีกว่าทางที่พิสูจน์แล้วเกิน SWITCH_GAP (เช่น ขนาดเม็ดเท่าขอบ Specialty):
    # ทางรอง (ok) ขายได้แค่ ceil(กระสอบ / รอบที่เหลือ) ในรอบนี้ ถ้าหยั่งผ่าน รอบหน้าค่อยขายทางที่ดีกว่า
    hold = {}
    if HOLD_WHILE_PROBING and flow is not True and not last:
        left_r = rounds_left(state, ctx)
        for lid in has_ok:
            ok_k = max((k_rfq[c["rfq_id"]] for c in pairs if c["lot_id"] == lid and not c["probe"]), default=0.0)
            if any(c["lot_id"] == lid and c["probe"] and k_rfq[c["rfq_id"]] >= ok_k + SWITCH_GAP for c in pairs):
                n = next(l["kg_available"] // BAG for l in state["lots"] if l["lot_id"] == lid)
                hold[lid] = -(-n // left_r)
    if hold and ctx.notes is not None:
        ctx.notes.append(f"hold while probing (max ok bags this round): {hold}")
    ok_used = defaultdict(int)

    stock = {l["lot_id"]: l["kg_available"] // BAG for l in state["lots"]}
    rfq_room = {i: (min(1, r["bags"]) if i in probes else r["bags"]) for i, r in rfqs.items()}

    def open_best(lot: str) -> float:
        """k สูงสุดของทางที่ยังมีที่รับรอบนี้ · RFQ ที่จ่ายดีเต็มแล้ว กระสอบที่เหลือไหลไปทางรองได้ (SWITCH_GAP เทียบกับค่านี้)"""
        free = flow is True or (flow and lot in flow)     # ไม่ไหล: เทียบกับทางดีสุดแม้เต็มแล้ว (รอรอบหน้า)
        return max((k_rfq[r] for r in routes[lot] if rfq_room[r] > 0 or not free), default=0.0)
    budget = round_budget_bags(state, ctx)
    # ราคาหลักก่อน (ผู้ซื้อจ่ายดีก่อน) ซองหยั่งราคาไปท้ายสุด -> ซองหยั่งไม่แย่งโควตา
    order = sorted(rfqs, key=lambda i: (i in probes, -k_rfq[i], -rfqs[i]["bags"]))
    alloc = defaultdict(int)

    # ซองหยั่งของล็อตที่เกินสเปกเล็กน้อยได้ที่ก่อน (1 กระสอบ ไป RFQ ที่ k สูงสุดของสเปกนั้น): ถ้าไม่หยั่ง ล็อตนี้ไม่มีทางขายเลย
    for key in sorted({(c["lot_id"], spec_key(c["spec"])) for c in pairs if c.get("over") and c["probe"]}):
        opts = [c for c in pairs if (c["lot_id"], spec_key(c["spec"])) == key and rfq_room[c["rfq_id"]] > 0]
        if budget <= 0 or stock[key[0]] <= 0 or not opts:
            continue
        c = max(opts, key=lambda c: k_rfq[c["rfq_id"]])
        alloc[(c["lot_id"], c["rfq_id"])] += 1
        probe_used[key] += 1
        stock[c["lot_id"]] -= 1
        rfq_room[c["rfq_id"]] -= 1
        budget -= 1

    while budget > 0:
        progressed = False
        for rid in order:
            if budget <= 0 or rfq_room[rid] <= 0:
                continue
            opts = [c for c in pairs if c["rfq_id"] == rid and stock[c["lot_id"]] > 0
                    and (last or k_rfq[rid] >= open_best(c["lot_id"]) - SWITCH_GAP)
                    and not (c["probe_limit"] and probe_used[(c["lot_id"], spec_key(c["spec"]))] >= SPEC_PROBE_BAGS)
                    and not (not c["probe"] and c["lot_id"] in hold and ok_used[c["lot_id"]] >= hold[c["lot_id"]])]
            if not opts:
                continue
            c = min(opts, key=lambda c: (open_best(c["lot_id"]) - k_rfq[rid], c["probe"], -c["tv"]))
            if c["probe"]:
                probe_used[(c["lot_id"], spec_key(c["spec"]))] += 1
            else:
                ok_used[c["lot_id"]] += 1
            alloc[(c["lot_id"], rid)] += 1
            stock[c["lot_id"]] -= 1
            rfq_room[rid] -= 1
            budget -= 1
            progressed = True
        if not progressed:
            break
    tv_of = {(c["lot_id"], c["rfq_id"]): c["tv"] for c in pairs}
    plan = [{"lot_id": lid, "rfq_id": rid, "bags": n, "markup": k_rfq[rid] + lot_off.get(lid, 0.0),
             "tv": tv_of[(lid, rid)]} for (lid, rid), n in alloc.items()]
    if last and not state["round"]["kind"] == "practice":
        plan = last_round_ladder(plan, rfqs, limited or set(), ctx.notes)
    return plan


def last_round_ladder(plan: list[dict], rfqs: dict, limited: set, notes: list) -> list[dict]:
    """รอบสุดท้าย: ซองตามลำดับเดิม ซองแรกๆ ใช้ k ปกติ ซองท้ายสุดลด LAST_LADDER (เช่น −0.03, −0.06)
    ซองของผู้ซื้อที่รอบก่อนซองเราโดน ABOVE_LIMIT ไปอยู่ท้ายแถว = ได้ราคาต่ำก่อน (ผู้ซื้อที่แค่ซื้อไม่ครบอยู่ต้นแถว ไม่ลด)
    ซองเดียวก็ได้ขั้นต่ำสุด (กันเหลือ 1 กระสอบ)"""
    plan = sorted(plan, key=lambda p: rfqs[p["rfq_id"]]["buyer"] in limited)    # sort คงลำดับเดิมในแต่ละกลุ่ม
    n, depth = len(plan), len(LAST_LADDER)
    for pos, p in enumerate(plan):
        delta = LAST_LADDER[max(0, depth - (n - pos))]
        if delta < 0:
            p["markup"] = min(p["markup"], max(FLOOR_LATE, p["markup"] + delta))
    notes.append("last-round ladder: " + ", ".join(f"{p['rfq_id']}/{p['lot_id']}={p['markup']:.3f}" for p in plan)
                 + f" · limited buyers {sorted(limited) or '-'}")
    return plan


def rule_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict]:
    """ใช้ได้ทั้งตอนปกติและตอน main.py เรียกเป็นทางสำรองเมื่อ decide() พัง"""
    L = learn(state, ctx)
    ctx.tv_price, ctx.spec_sample = L["tv_price"], L["spec_sample"]
    k_rfq, probes = price_rfqs(state, ctx, L["buyers"], pressure(state, ctx, L["fill"], sellable_lots(state, ctx, L)))
    return build_plan(state, ctx, cands, k_rfq, probes, L["lot_off"], L["spec"], L["limited_buyers"])


# ---------------- 5. LLM (ไม่บังคับ) ----------------
SYSTEM = """You advise a coffee-bean seller in a sealed-offer market with many competing sellers.
price = markup x lot value. Buyers take the cheapest offers (price/value) first, reject offers above a hidden
ceiling, and pay the offered price. Unsold beans are worth only 0.80 at the end, and each round we may offer
only a limited number of bags, so falling behind cannot be fixed later.
For each buyer you get our current markup and what happened last round, plus our sales pressure
(above 1 = we are behind and will have leftovers). Return a small delta per buyer in [-0.02, 0.02]:
positive if the buyer looks undersupplied or we sold everything easily, negative if we keep losing.
Use 0 when unsure. Answer only JSON matching the schema."""

SCHEMA = {"type": "object", "required": ["deltas", "reason"], "properties": {
    "deltas": {"type": "array", "items": {"type": "object", "required": ["buyer", "delta"], "properties": {
        "buyer": {"type": "string"}, "delta": {"type": "number"}}}},
    "reason": {"type": "string", "maxLength": 200}}}


def llm_adjust(state: dict, ctx: Context, buyers: dict, pres: float) -> bool:
    timeout = min(LLM_MAX_SECONDS, ctx.seconds_left - 10)
    if not ctx.llm or not buyers or timeout < 3:
        return False
    rnd = state["round"]
    table = [{"buyer": n, "type": b["type"], "markup": b["k"], "last": b["last"]} for n, b in buyers.items()]
    user = (f"Round {rnd['number']} of {rnd['of']} ({rnd['kind']}, calendar {rnd.get('calendar')}). "
            f"Rounds left: {rounds_left(state, ctx)}. Sales pressure: {pres:.2f}.\n"
            f"Buyers:\n{json.dumps(table, ensure_ascii=False)}")
    answer = ctx.llm.ask_json(SYSTEM, user, SCHEMA, timeout=timeout)
    if not answer or not isinstance(answer.get("deltas"), list):
        ctx.notes.append(f"llm skipped: {getattr(ctx.llm, 'last_error', None)}")
        return False
    for d in answer["deltas"]:
        try:
            b = buyers[d["buyer"]]
            delta = min(max(float(d["delta"]), -LLM_MAX_DELTA), LLM_MAX_DELTA)
        except (KeyError, TypeError, ValueError):
            continue
        b["k"] = round(min(max(b["k"] + delta, MIN_MARKUP), MAX_MARKUP), 3)
    ctx.notes.append(f"llm: {str(answer.get('reason'))[:200]}")
    return True


# ---------------- 6. guard ----------------
def guard(state: dict, ctx: Context, plan: list[dict]) -> list[dict]:
    """แปลงแผนเป็นซองที่ตลาดรับแน่นอน · ไม่เชื่อตัวเลขจาก LLM หรือโค้ดส่วนอื่นตรงๆ"""
    lots = {l["lot_id"]: l for l in state["lots"]}
    rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
    room = state["capacity_left_kg"] if state.get("capacity_left_kg") is not None else 10**9
    lot_left = {i: l["kg_available"] for i, l in lots.items()}
    rfq_left = {i: r["kg"] for i, r in rfqs.items()}
    merged = {}
    for p in plan:
        key = (p["lot_id"], p["rfq_id"])
        if key in merged:
            merged[key]["bags"] += p["bags"]
        else:
            merged[key] = dict(p)
    offers = []
    for p in merged.values():
        lot, rfq = lots.get(p["lot_id"]), rfqs.get(p["rfq_id"])
        ss = (ctx.spec_sample or {}).get(p["lot_id"], lot["sample"]) if lot else None
        if lot is None or rfq is None or not (spec_ok(ss, rfq["spec"]) or over_spec(ss, rfq["spec"], ctx.rules)):
            ctx.notes.append(f"guard dropped {p}")
            continue
        kg = min(int(p["bags"]) * BAG, lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room) // BAG * BAG
        if kg < BAG:
            continue
        markup = min(max(float(p["markup"]), MIN_MARKUP), MAX_MARKUP)
        tv = p.get("tv") or (ctx.tv_price or ctx.tv)[lot["lot_id"]]
        offers.append({"rfq_id": rfq["rfq_id"], "lot_id": lot["lot_id"], "kg": kg,
                       "price_per_kg": round(markup * tv, 2)})
        ctx.offer_tv[(rfq["rfq_id"], lot["lot_id"])] = tv
        lot_left[lot["lot_id"]] -= kg
        rfq_left[rfq["rfq_id"]] -= kg
        room -= kg
    return offers


# ---------------- รวมทุกขั้น ----------------
def decide(state: dict, ctx: Context) -> tuple[list[dict], str]:
    L = learn(state, ctx)
    ctx.tv_price, ctx.spec_sample = L["tv_price"], L["spec_sample"]
    cands = candidates(state, ctx)
    if not cands:
        return [], "none"
    moved = {l: (ctx.tv[l], t) for l, t in L["tv_price"].items() if abs(t - ctx.tv[l]) > 0.005}
    if moved:
        ctx.notes.append("pricing TV: " + ", ".join(f"{l} {a:.2f}->{b:.2f}" for l, (a, b) in moved.items()))
    sellable = sellable_lots(state, ctx, L)
    dead = sorted(l["lot_id"] for l in state["lots"] if l["lot_id"] not in sellable and l["kg_remaining"] >= BAG)
    if dead:
        ctx.notes.append(f"no way to sell (not counted in pressure): {dead}")
    pres = pressure(state, ctx, L["fill"], sellable)
    used_llm = llm_adjust(state, ctx, L["buyers"], pres)
    for n, b in sorted(L["buyers"].items()):
        ctx.notes.append(f"{n} [{b['type']}] k={b['k']:.3f} · {b['last']}")
    ctx.notes.append(f"fill {L['fill']:.2f} · lot offsets {L['lot_off'] or '-'}")
    k_rfq, probes = price_rfqs(state, ctx, L["buyers"], pres)
    offers = guard(state, ctx, build_plan(state, ctx, cands, k_rfq, probes, L["lot_off"], L["spec"], L["limited_buyers"],
                                          flow_lots(state, ctx, pres)))
    return offers, "rules+llm" if used_llm else "rules"


def flow_lots(state: dict, ctx: Context, pres: float) -> set | bool:
    """ล็อตที่ยอมให้ไหลไปทางรองรอบนี้: ทุกล็อตถ้า pressure สูง · ไม่งั้นเฉพาะล็อตที่รอบก่อนแพ้ (OUTBID/ABOVE_LIMIT) = ทางดีสุดแย่งกันจริง"""
    if pres >= FLOW_PRESSURE:
        return True
    lost = set()
    if FLOW_ON_LOSS:
        for o in (state.get("last_results") or {}).get("my_offers") or []:
            if o.get("status") in ("OUTBID", "ABOVE_LIMIT"):
                lost.add(o["lot_id"])
    if lost:
        ctx.notes.append(f"flow (lost last round): {sorted(lost)}")
    return lost
