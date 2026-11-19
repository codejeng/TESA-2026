"""Local stage-3 mock of the coffee market API, for running the real main.py end to end. stdlib only.

    .venv/bin/python tools/mock_market.py --scenario nohoard [--faults] [--port 8099] [--out result.json]

Same paths, JSON shapes, error codes and rate limit as คู่มือ-API.md. It never talks to the real market.
Game: SETUP -> APPRAISAL -> practice P1, P2 -> real R1..R15 (ROUND_OPEN -> ROUND_CLEARING) -> CLOSED.
Our team is T23 (key --team-key); 55 bot teams play in the same process (--scenario picks the mix).
Extra endpoint for tools (no key): GET /mock/stats -> phase, request stats, our per-round history, and after
CLOSED the full leaderboard and our reveal. The same JSON is written to --out when the market closes.
"""
import argparse
import json
import math
import random
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from appraisal import grade, true_value  # noqa: E402

BAG = 60
OUR = "T23"
RULES = json.loads((HERE / "data" / "sandbox-20261118" / "calendar-1021.json").read_text(encoding="utf-8"))["rules"]

# stage-3 calendar from the day-3 slides
CALENDAR = [
    {"rounds": "1-5", "label": "ปกติ", "demand_weight": 1.0, "specialty_share": None},
    {"rounds": "6-8", "label": "เทศกาลกาแฟ", "demand_weight": 1.0, "specialty_share": 0.4},
    {"rounds": "9-12", "label": "ปกติ", "demand_weight": 1.0, "specialty_share": None},
    {"rounds": "13-14", "label": "ใกล้ปิดตลาด", "demand_weight": 0.6, "specialty_share": None},
    {"rounds": "15", "label": "รอบสุดท้าย", "demand_weight": 0.35, "specialty_share": None},
]

SPEC_S16 = {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 16}
SPEC_S17 = {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 17}
SPEC_C = {"max_defect_pct": 7.0, "max_moisture_pct": 12.5, "min_screen": None}
SPEC_I14 = {"max_defect_pct": 15.0, "max_moisture_pct": 14.0, "min_screen": None}
SPEC_I15 = {"max_defect_pct": 15.0, "max_moisture_pct": 15.0, "min_screen": None}
# name, type, spec, RFQs per round, bags per RFQ (base; × demand_weight; Specialty RFQs × 3 in the festival)
# sized from stage 2 (memory-competition-f5ab5954.json: ~158 bags per round for 56 teams)
BUYERS = [
    ("1 ดอยช้างโรสเตอร์", "SPECIALTY", SPEC_S16, 2, 6),
    ("2 บางกอกบลูม", "SPECIALTY", SPEC_S17, 1, 6),
    ("3 สยามคอมเมิร์ซ", "COMMERCIAL", SPEC_C, 2, 20),
    ("4 ชุมพรคั่วบด", "COMMERCIAL", SPEC_C, 2, 15),
    ("5 อินสแตนท์ไทย", "INSTANT", SPEC_I14, 1, 26),
    ("6 ทรีอินวันฟู้ดส์", "INSTANT", SPEC_I15, 2, 18),
]
CEIL = {"SPECIALTY": 1.10, "COMMERCIAL": 1.04, "INSTANT": 0.96}   # hidden ceiling: price ÷ true TV, ±0.02 per RFQ

# lot templates (true quality); 45 bags. force = sample noise fixed on purpose (edge cases every team shares)
LOTS = [
    dict(bags=9, d=0.8, m=11.2, s=16, origin="เชียงราย - ดอยช้าง", process="Washed", force={"s": 1}),     # S; sample 17 -> บางกอกบลูม mismatch
    dict(bags=8, d=2.3, m=11.9, s=16, origin="น่าน - บ่อเกลือ", process="Honey", force={}),               # S
    dict(bags=10, d=4.2, m=12.0, s=15, origin="ชุมพร - ท่าแซะ", process="Natural (Dry)", force={}),       # C
    dict(bags=7, d=7.05, m=12.1, s=14, origin="ชุมพร - หลังสวน", process="Natural (Dry)", force={"d": -0.3}),  # looks C, really Instant
    dict(bags=7, d=11.0, m=14.2, s=13, origin="ระนอง - กระบุรี", process="Honey", force={}),              # Instant (ทรีอินวัน only)
    dict(bags=4, d=17.5, m=13.0, s=13, origin="ชุมพร - ปะทิว", process="Natural (Dry)", force={}),        # unsellable
]

SCENARIOS = {   # share of the 55 bots per behaviour
    "nohoard": {"starter": 0.5, "adaptive": 0.5},
    "hoard": {"hoarder": 0.6, "starter": 0.2, "adaptive": 0.2},
    "dump": {"dumper": 0.5, "starter": 0.25, "adaptive": 0.25},
}
START_K = {"SPECIALTY": 1.05, "COMMERCIAL": 0.975, "INSTANT": 0.92}


def spec_ok(q: dict, spec: dict) -> bool:
    return (q["defect_pct"] <= spec["max_defect_pct"] and q["moisture_pct"] <= spec["max_moisture_pct"]
            and (spec["min_screen"] is None or q["screen"] >= spec["min_screen"]))


def round_label(n: int, cal: list) -> tuple[str, float]:
    for c in cal:
        r = c["rounds"]
        a, b = (min(r), max(r)) if isinstance(r, list) else (int(str(r).split("-")[0]), int(str(r).split("-")[-1]))
        if a <= n <= b:
            return c["label"], float(c.get("demand_weight") or 1.0)
    return "ปกติ", 1.0


def err(status: int, code: str, message: str, field=None, hint=None, allowed=None):
    e = {"code": code, "message": message}
    if field:
        e["field"] = field
    if hint:
        e["hint"] = hint
    if allowed is not None:
        e["allowed"] = allowed
    return status, {"error": e}


class Team:
    def __init__(self, tid: str, kind: str, lots: list, rng: random.Random):
        self.id, self.kind, self.rng = tid, kind, rng
        self.name = tid
        self.lots = lots                 # [{lot_id, kg_total, kg_remaining, origin, process, sample, true, tv}]
        self.revenue = 0.0
        self.kg_sold = 0
        self.appraisal = None            # {lot_id: {grade, est_value_per_kg}}
        self.appraisal_at = None
        self.k = {t: v + rng.uniform(-0.02, 0.02) for t, v in START_K.items()}
        self.spec_pref = rng.uniform(0.15, 0.55)   # chance a bag of a Specialty-grade lot goes to Specialty (stage 2: 13–32 offers/RFQ)

    def lot(self, lid):
        return next((l for l in self.lots if l["lot_id"] == lid), None)


class Market:
    def __init__(self, a):
        self.a = a
        self.rng = random.Random(a.seed)
        self.lock = threading.RLock()
        self.calendar = json.loads(Path(a.calendar).read_text(encoding="utf-8")) if a.calendar else CALENDAR
        self.rules = json.loads(json.dumps(RULES))
        self.rules["timeline"] = {"setup_s": a.setup_s, "appraisal_s": a.appraisal_s, "practice_rounds": 2,
                                  "rounds": a.rounds, "open_s": a.open_s, "clearing_s": a.clear_s}
        self.total_bags = sum(t["bags"] for t in LOTS)
        self.cap_bags = math.ceil(self.rules["round_capacity_factor"] * self.total_bags / a.rounds)
        # sample noise is the same for every team (manual: "ความคลาดของผลวัดตัวอย่างเท่ากัน")
        self.noise = []
        for t in LOTS:
            nd = self._tail(0.25)
            nm = self._tail(0.25)
            ns = self.rng.choice([-1, 0, 0, 0, 0, 0, 1])
            f = t["force"]
            self.noise.append((f.get("d", nd), f.get("m", nm), f.get("s", ns)))
        kinds = []
        for k, share in SCENARIOS[a.scenario].items():
            kinds += [k] * round(share * 55)
        kinds = (kinds + ["starter"] * 55)[:55]
        self.rng.shuffle(kinds)
        ids = [f"T{i:02d}" for i in range(1, 57) if i != 23]
        self.teams = {OUR: Team(OUR, "agent", self._make_lots(OUR), random.Random(a.seed + 23))}
        for i, (tid, kind) in enumerate(zip(ids, kinds)):
            self.teams[tid] = Team(tid, kind, self._make_lots(tid), random.Random(a.seed * 100 + i))
        # schedule
        self.schedule = [("SETUP", a.setup_s, None), ("APPRAISAL", a.appraisal_s, None)]
        self.round_defs = [("P1", "practice", 1, 2), ("P2", "practice", 2, 2)] + \
                          [(f"R{n}", "real", n, a.rounds) for n in range(1, a.rounds + 1)]
        for rd in self.round_defs:
            self.schedule += [("ROUND_OPEN", a.open_s, rd), ("ROUND_CLEARING", a.clear_s, rd)]
        self.step, self.ends_at = 0, time.time() + a.setup_s
        self.phase, self.round = "SETUP", None
        self.rfqs, self.ceil = [], {}
        self.offers = {}                 # offer_id -> offer (all teams, current round)
        self.next_offer = 1
        self.results = defaultdict(dict)  # team -> round_id -> results
        self.last_round = None
        # our stats
        self.req_times = deque()
        self.max_rps, self.requests, self.n429, self.faults = 0, 0, 0, defaultdict(int)
        self.bucket, self.bucket_t = 10.0, time.time()
        self.fault500_at = self.rng.randint(40, 300)
        self.history = []                # our per-round snapshot
        self.batches = defaultdict(int)  # round -> our accepted batch/single offer posts
        self.appraisal_phase = None
        self.final = None
        self.events = []

    def _tail(self, sd):
        x = self.rng.gauss(0, sd)
        if self.rng.random() < 0.12:
            x += self.rng.choice([-1, 1]) * self.rng.uniform(0.6, 1.5)
        return round(x, 2)

    def _make_lots(self, tid):
        r = random.Random(f"{self.a.seed}-{tid}")
        out = []
        for i, (t, (nd, nm, ns)) in enumerate(zip(LOTS, self.noise)):
            true = {"defect_pct": round(max(0.0, t["d"] + r.uniform(-0.08, 0.08)), 2),
                    "moisture_pct": round(t["m"] + r.uniform(-0.08, 0.08), 2), "screen": t["s"]}
            if i == 3:      # keep the edge lot over 7.0 for everyone
                true["defect_pct"] = max(true["defect_pct"], 7.02)
            sample = {"defect_pct": round(max(0.0, true["defect_pct"] + nd), 1),
                      "moisture_pct": round(true["moisture_pct"] + nm, 1), "screen": int(true["screen"] + ns)}
            out.append({"kg_total": t["bags"] * BAG, "kg_remaining": t["bags"] * BAG, "origin": t["origin"],
                        "process": t["process"], "sample": sample, "true": true,
                        "tv": round(true_value(self.rules, true), 2)})
        r.shuffle(out)       # lot order differs per team
        for j, l in enumerate(out, 1):
            l["lot_id"] = f"LOT-{tid}-{j}"
        return out

    # ---------------- clock ----------------
    def tick(self):
        with self.lock:
            while self.phase != "CLOSED" and time.time() >= self.ends_at:
                self._leave(self.phase, self.round)
                self.step += 1
                if self.step >= len(self.schedule):
                    self.phase, self.ends_at = "CLOSED", None
                    self._close()
                    return
                ph, dur, rd = self.schedule[self.step]
                self.phase, self.round = ph, rd
                self.ends_at = self.ends_at + dur
                self._enter(ph, rd)

    def _enter(self, ph, rd):
        self.events.append({"t": round(time.time(), 1), "phase": ph, "round": rd[0] if rd else None})
        if ph == "APPRAISAL":
            for t in self.teams.values():
                if t.id != OUR:
                    t.appraisal = {l["lot_id"]: {"grade": grade(self.rules, l["sample"]["defect_pct"])[0],
                                                 "est_value_per_kg": round(true_value(self.rules, l["sample"]), 2)}
                                   for l in t.lots}
                    t.appraisal_at = 1.0
        elif ph == "ROUND_OPEN":
            self._make_rfqs(rd)
            for t in self.teams.values():
                if t.id != OUR:
                    self._bot_offers(t, rd)

    def _leave(self, ph, rd):
        if ph == "ROUND_OPEN":
            self._clear(rd)

    def _make_rfqs(self, rd):
        rid, kind, n, _ = rd
        label, w = round_label(n, self.calendar) if kind == "real" else ("ปกติ", 1.0)
        fest = "เทศกาล" in label
        self.rfqs, self.ceil, j = [], {}, 0
        for name, typ, spec, cnt, bags in BUYERS:
            cnt = cnt * 3 if fest and typ == "SPECIALTY" else cnt
            for _ in range(cnt):
                b = max(1, round(bags * w * self.rng.uniform(0.8, 1.2)))
                j += 1
                r = {"rfq_id": f"{rid}-{j:02d}", "buyer": name, "buyer_type": typ, "kg": b * BAG, "bags": b,
                     "spec": dict(spec)}
                self.rfqs.append(r)
                prem = self.a.fest_premium if fest and typ == "SPECIALTY" else 0.0
                self.ceil[r["rfq_id"]] = CEIL[typ] + prem + self.rng.uniform(-0.02, 0.02)
        self.rng.shuffle(self.rfqs)

    # ---------------- bots ----------------
    def _bot_offers(self, t: Team, rd):
        rid, kind, n, of = rd
        real = kind == "real"
        left_r = (self.a.rounds - n + 1) if real else self.a.rounds
        fest_rounds = [x for x in range(1, self.a.rounds + 1) if "เทศกาล" in round_label(x, self.calendar)[0]]
        first_fest = min(fest_rounds) if fest_rounds else 99
        in_fest = real and n in fest_rounds
        lots = [l for l in t.lots if l["kg_remaining"] >= BAG]
        is_s = {l["lot_id"]: spec_ok(l["sample"], SPEC_S16) for l in lots}
        if t.kind == "hoarder" and real and n < first_fest:
            lots = [l for l in lots if not is_s[l["lot_id"]]]
        avail_bags = sum(l["kg_remaining"] // BAG for l in t.lots)
        dump = t.kind == "dumper" and real and n > self.a.rounds - 3
        quota = self.cap_bags if (dump or in_fest or left_r <= 1) else min(self.cap_bags, math.ceil(avail_bags / left_r) + 1)
        k = dict(t.k)
        if dump:
            k = {x: v - 0.05 for x, v in k.items()}
        stock = {l["lot_id"]: l["kg_remaining"] // BAG for l in lots}
        room = {r["rfq_id"]: r["bags"] for r in self.rfqs}
        alloc = defaultdict(int)
        order = sorted(lots, key=lambda l: (-(t.kind == "hoarder" and in_fest and is_s[l["lot_id"]]), t.rng.random()))
        progressed = True
        while quota > 0 and progressed:
            progressed = False
            for l in order:
                if quota <= 0 or stock[l["lot_id"]] <= 0:
                    continue
                est = true_value(self.rules, l["sample"])
                opts = [r for r in self.rfqs if room[r["rfq_id"]] > 0 and spec_ok(l["sample"], r["spec"])]
                if t.kind == "hoarder" and in_fest and is_s[l["lot_id"]]:
                    opts = [r for r in opts if r["buyer_type"] == "SPECIALTY"] or opts
                if not opts:
                    continue
                if not (t.kind == "hoarder" and in_fest) and t.rng.random() > t.spec_pref:
                    opts = [r for r in opts if r["buyer_type"] != "SPECIALTY"] or opts
                best = max(k[r["buyer_type"]] for r in opts)
                opts = [r for r in opts if k[r["buyer_type"]] >= best - 1e-9]
                r = t.rng.choice(opts)
                alloc[(l["lot_id"], r["rfq_id"], round(max(0.82, k[r["buyer_type"]]) * est, 2))] += 1
                stock[l["lot_id"]] -= 1
                room[r["rfq_id"]] -= 1
                quota -= 1
                progressed = True
        for (lid, rfq, price), bags in alloc.items():
            self._add_offer(t, {"rfq_id": rfq, "lot_id": lid, "kg": bags * BAG, "price_per_kg": price})

    def _bot_learn(self, t: Team, my_offers: list):
        if t.kind not in ("adaptive", "hoarder", "dumper"):
            return
        by = defaultdict(list)
        types = {r["rfq_id"]: r["buyer_type"] for r in self.rfqs}
        for o in my_offers:
            by[types.get(o["rfq_id"], "COMMERCIAL")].append(o["status"])
        for typ, st in by.items():
            if all(s == "ACCEPTED" for s in st):
                t.k[typ] = min(1.15, t.k[typ] + 0.01)
            elif not any(s in ("ACCEPTED", "PARTIAL") for s in st):
                t.k[typ] = max(0.85, t.k[typ] - 0.01)

    # ---------------- offers ----------------
    def _open_of(self, tid):
        return [o for o in self.offers.values() if o["team"] == tid and o["status"] == "OPEN"]

    def _add_offer(self, t: Team, o: dict) -> dict:
        oid = f"O{self.next_offer:05d}"
        self.next_offer += 1
        rec = {"offer_id": oid, "round_id": self.round[0], "rfq_id": o["rfq_id"], "lot_id": o["lot_id"],
               "kg": int(o["kg"]), "price_per_kg": float(o["price_per_kg"]), "status": "OPEN", "filled_kg": 0,
               "team": t.id, "created": time.time()}
        self.offers[oid] = rec
        return rec

    @staticmethod
    def public(o):
        return {k: v for k, v in o.items() if k not in ("team", "created")}

    def validate(self, t: Team, o, used_lot: dict, used_cap: int):
        """-> (code, message, field, allowed) or None"""
        if not isinstance(o, dict):
            return ("INVALID_FIELD", "offer must be an object", None, None)
        for f in ("rfq_id", "lot_id", "kg", "price_per_kg"):
            if f not in o:
                return ("INVALID_FIELD", f"missing {f}", f, None)
        kg, price = o["kg"], o["price_per_kg"]
        if not isinstance(kg, int) or isinstance(kg, bool) or kg <= 0:
            return ("INVALID_FIELD", "kg must be a positive integer", "kg", None)
        if kg % BAG:
            return ("NOT_MULTIPLE_OF_BAG", f"kg must be a multiple of {BAG}", "kg", {"nearest": max(BAG, round(kg / BAG) * BAG)})
        if not isinstance(price, (int, float)) or isinstance(price, bool) or not (0 < price <= 1000):
            return ("INVALID_FIELD", "price_per_kg must be > 0 and <= 1000", "price_per_kg", {"min": 0, "max": 1000})
        rfq = next((r for r in self.rfqs if r["rfq_id"] == o["rfq_id"]), None)
        if rfq is None:
            return ("UNKNOWN_RFQ", f"{o['rfq_id']} is not open this round", "rfq_id", None)
        if kg > rfq["kg"]:
            return ("INVALID_FIELD", f"{rfq['rfq_id']} wants only {rfq['kg']} kg", "kg", {"max_kg": rfq["kg"]})
        lot = t.lot(o["lot_id"])
        if lot is None:
            mine = any(x.lot(o["lot_id"]) for x in self.teams.values())
            return ("NOT_YOUR_LOT" if mine else "UNKNOWN_LOT", f"{o['lot_id']} is not your lot", "lot_id", None)
        free = lot["kg_remaining"] - used_lot[lot["lot_id"]]
        if kg > free:
            return ("EXCEEDS_REMAINING", f"{lot['lot_id']} has {lot['kg_remaining']} kg left; open offers already use "
                    f"{used_lot[lot['lot_id']]} kg. You can offer at most {max(0, free)} kg more.", "kg", {"max_kg": max(0, free)})
        if used_cap + kg > self.cap_bags * BAG:
            return ("OVER_ROUND_CAPACITY", "team offers exceed the round capacity", "kg",
                    {"max_kg": max(0, self.cap_bags * BAG - used_cap)})
        return None

    def _usage(self, tid, exclude=()):
        used_lot, cap = defaultdict(int), 0
        for o in self._open_of(tid):
            if o["offer_id"] in exclude:
                continue
            used_lot[o["lot_id"]] += o["kg"]
            cap += o["kg"]
        return used_lot, cap

    # ---------------- clearing ----------------
    def _clear(self, rd):
        rid, kind = rd[0], rd[1]
        real = kind == "real"
        summary = []
        by_rfq = defaultdict(list)
        for o in self.offers.values():
            if o["status"] == "OPEN":
                by_rfq[o["rfq_id"]].append(o)
        for r in self.rfqs:
            offs = by_rfq.get(r["rfq_id"], [])
            ok = []
            for o in offs:
                lot = self.teams[o["team"]].lot(o["lot_id"])
                if not spec_ok(lot["true"], r["spec"]):
                    o["status"] = "SPEC_MISMATCH"
                elif o["price_per_kg"] / lot["tv"] > self.ceil[r["rfq_id"]]:
                    o["status"] = "ABOVE_LIMIT"
                else:
                    ok.append((round(o["price_per_kg"] / lot["tv"], 6), o))
            ok.sort(key=lambda x: x[0])
            need = r["bags"]
            i = 0
            while i < len(ok) and need > 0:
                j = i
                while j < len(ok) and ok[j][0] == ok[i][0]:
                    j += 1
                group = [o for _, o in ok[i:j]]
                self.rng.shuffle(group)
                left = {o["offer_id"]: o["kg"] // BAG for o in group}
                while need > 0 and any(left.values()):       # equal value: split bag by bag
                    for o in group:
                        if need > 0 and left[o["offer_id"]] > 0:
                            o["filled_kg"] += BAG
                            left[o["offer_id"]] -= 1
                            need -= 1
                i = j
            sold = [o for _, o in ok if o["filled_kg"] > 0]
            for _, o in ok:
                o["status"] = "ACCEPTED" if o["filled_kg"] == o["kg"] else ("PARTIAL" if o["filled_kg"] else "OUTBID")
            prices = [o["price_per_kg"] for o in sold]
            summary.append({"rfq_id": r["rfq_id"], "buyer": r["buyer"], "buyer_type": r["buyer_type"],
                            "wanted_kg": r["kg"], "bought_kg": sum(o["filled_kg"] for o in sold), "offers": len(offs),
                            "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
                            "price_avg": round(sum(prices) / len(prices), 2) if prices else None})
        mine = defaultdict(list)
        for o in self.offers.values():
            if o["status"] == "OPEN":
                o["status"] = "OUTBID"
            mine[o["team"]].append(o)
            if real and o["filled_kg"]:
                t = self.teams[o["team"]]
                lot = t.lot(o["lot_id"])
                lot["kg_remaining"] -= o["filled_kg"]
                t.revenue += o["filled_kg"] * o["price_per_kg"]
                t.kg_sold += o["filled_kg"]
        for tid, t in self.teams.items():
            res = {"round_id": rid, "kind": kind, "my_offers": [self.public(o) for o in mine.get(tid, [])],
                   "market_summary": summary}
            self.results[tid][rid] = res
            if tid != OUR:
                self._bot_learn(t, res["my_offers"])
        self.last_round = rid
        us = self.teams[OUR]
        self.history.append({
            "round": rid, "kind": kind,
            "offers": [{**self.public(o), "true_tv": us.lot(o["lot_id"])["tv"],
                        "buyer_type": next(r["buyer_type"] for r in self.rfqs if r["rfq_id"] == o["rfq_id"])}
                       for o in mine.get(OUR, [])],
            "kg_remaining": sum(l["kg_remaining"] for l in us.lots), "revenue": round(us.revenue, 2),
            "kg_remaining_sellable": sum(l["kg_remaining"] for l in us.lots
                                         if any(spec_ok(l["true"], b[2]) for b in BUYERS)),
            "specialty_offers_per_sold_rfq": (lambda v: round(sum(v) / len(v), 1) if v else None)(
                [s["offers"] for s in summary if s["buyer_type"] == "SPECIALTY" and s["bought_kg"] > 0])})
        self.offers = {}

    # ---------------- scoring ----------------
    def scores(self):
        rows = []
        for t in self.teams.values():
            total = sum(l["tv"] * l["kg_total"] for l in t.lots)
            left = sum(l["tv"] * l["kg_remaining"] for l in t.lots)
            pr = (t.revenue + self.rules["salvage_ratio"] * left) / total
            mape = None
            if t.appraisal:
                kg = sum(l["kg_total"] for l in t.lots)
                mape = sum(l["kg_total"] * abs(t.appraisal.get(l["lot_id"], {}).get("est_value_per_kg", 0) - l["tv"]) / l["tv"]
                           for l in t.lots) / kg
            rows.append({"team_id": t.id, "name": t.name, "kind": t.kind, "pr": round(pr, 4),
                         "mape": round(mape, 4) if mape is not None else None,
                         "revenue_thb": round(t.revenue, 2), "kg_left": sum(l["kg_remaining"] for l in t.lots)})
        top = max(r["pr"] for r in rows)
        for r in rows:
            r["pr_score"] = round(max(0.0, 80 * (r["pr"] - 0.8) / (top - 0.8)) if top > 0.8 else 0.0, 2)
            r["appraisal_score"] = round(20 * max(0.0, 1 - r["mape"] / 0.10), 2) if r["mape"] is not None else 0.0
            r["total"] = round(r["pr_score"] + r["appraisal_score"], 2)
        rows.sort(key=lambda r: -r["total"])
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return rows

    def reveal(self):
        t = self.teams[OUR]
        me = next(r for r in self.scores() if r["team_id"] == OUR)
        return {"lots": [{"lot_id": l["lot_id"], "true": l["true"], "sample": l["sample"],
                          "true_grade": grade(self.rules, l["true"]["defect_pct"])[0], "true_value_per_kg": l["tv"],
                          "your_estimate": (t.appraisal or {}).get(l["lot_id"])} for l in t.lots],
                "score": {"team_id": OUR, "name": t.name, "pr": me["pr"], "mape": me["mape"], "appraisal_at": t.appraisal_at,
                          "pr_score": me["pr_score"], "appraisal_score": me["appraisal_score"], "total": me["total"],
                          "rank": me["rank"]}}

    def stats(self):
        out = {"phase": self.phase, "round": self.round[0] if self.round else None, "scenario": self.a.scenario,
               "faults": self.a.faults, "seed": self.a.seed, "requests": self.requests, "max_rps": self.max_rps,
               "rate_limited_429": self.n429, "faults_injected": dict(self.faults), "appraisal_phase": self.appraisal_phase,
               "batches_per_round": dict(self.batches), "capacity_bags": self.cap_bags, "total_bags": self.total_bags,
               "sellable_bags": sum(l["kg_total"] for l in self.teams[OUR].lots
                                    if any(spec_ok(l["true"], b[2]) for b in BUYERS)) // BAG,
               "our_open_offers": len(self._open_of(OUR)) if self.phase == "ROUND_OPEN" else 0,
               "history": self.history, "events": self.events, "calendar": self.calendar}
        if self.phase == "CLOSED":
            out["leaderboard"] = self.scores()
            out["reveal"] = self.reveal()
        return out

    def _close(self):
        self.events.append({"t": round(time.time(), 1), "phase": "CLOSED", "round": None})
        self.final = self.stats()
        if self.a.out:
            Path(self.a.out).write_text(json.dumps(self.final, ensure_ascii=False, indent=1), encoding="utf-8")
        board = self.final["leaderboard"]
        print(f"\nCLOSED · scenario {self.a.scenario} · top PR {board[0]['pr']}", flush=True)
        for r in board[:5] + [r for r in board if r["team_id"] == OUR]:
            print(f"  #{r['rank']:>2} {r['team_id']} {r['kind']:<8} PR {r['pr']:.4f} MAPE {r['mape']} total {r['total']}", flush=True)

    # ---------------- state ----------------
    def state(self, t: Team):
        rd = self.round
        rnd = None
        if rd:
            label = round_label(rd[2], self.calendar)[0] if rd[1] == "real" else "ซ้อม"
            rnd = {"round_id": rd[0], "kind": rd[1], "number": rd[2], "of": rd[3], "counts_for_score": rd[1] == "real",
                   "calendar": label}
        used_lot, used_cap = self._usage(t.id)
        lots = [{"lot_id": l["lot_id"], "kg_total": l["kg_total"], "kg_remaining": l["kg_remaining"],
                 "kg_in_open_offers": used_lot[l["lot_id"]], "kg_available": l["kg_remaining"] - used_lot[l["lot_id"]],
                 "origin": l["origin"], "process": l["process"], "sample": l["sample"]} for l in t.lots]
        open_ = self.phase in ("ROUND_OPEN", "ROUND_CLEARING")
        kg_left = sum(l["kg_remaining"] for l in t.lots)
        return {
            "market": "competition", "team_id": t.id, "phase": self.phase, "paused": False,
            "seconds_left": round(max(0.0, self.ends_at - time.time()), 1) if self.ends_at else None,
            "phase_ends_at": self.ends_at, "round": rnd, "lots": lots,
            "rfqs": [dict(r) for r in self.rfqs] if open_ else [],
            "my_open_offers": [self.public(o) for o in self._open_of(t.id)],
            "round_capacity_kg": self.cap_bags * BAG,
            "capacity_left_kg": self.cap_bags * BAG - used_cap if self.phase == "ROUND_OPEN" else self.cap_bags * BAG,
            "appraisal_submitted": t.appraisal is not None,
            "last_results": self.results[t.id].get(self.last_round) if self.last_round else None,
            "standings": {"revenue_thb": round(t.revenue, 2), "kg_sold": t.kg_sold, "kg_remaining": kg_left,
                          "kg_total": sum(l["kg_total"] for l in t.lots),
                          "avg_price_per_kg": round(t.revenue / t.kg_sold, 2) if t.kg_sold else None},
            "salvage_ratio": self.rules["salvage_ratio"],
        }

    # ---------------- request handling ----------------
    def note_request(self):
        now = time.time()
        self.requests += 1
        self.req_times.append(now)
        while self.req_times and self.req_times[0] < now - 1.0:
            self.req_times.popleft()
        self.max_rps = max(self.max_rps, len(self.req_times))
        self.bucket = min(10.0, self.bucket + (now - self.bucket_t) * 5.0)
        self.bucket_t = now
        if self.bucket < 1.0:
            self.n429 += 1
            return False
        self.bucket -= 1.0
        return True

    def handle(self, method, path, query, body, t: Team):
        if path == "/v1/state" and method == "GET":
            return 200, self.state(t)
        if path == "/v1/calendar" and method == "GET":
            return 200, {"calendar": self.calendar, "rules": self.rules}
        if path == "/v1/lots" and method == "GET":
            return 200, {"lots": self.state(t)["lots"]}
        if path == "/v1/rfqs" and method == "GET":
            return 200, {"rfqs": self.state(t)["rfqs"]}
        if path == "/v1/appraisals":
            if method == "GET":
                if t.appraisal is None:
                    return err(409, "NOT_AVAILABLE", "no appraisal submitted yet")
                return 200, {"items": [{"lot_id": k, **v} for k, v in t.appraisal.items()]}
            if method == "POST":
                if self.phase != "APPRAISAL":
                    return err(409, "WRONG_PHASE", f"appraisals are accepted only in APPRAISAL (now {self.phase})",
                               allowed={"phase": "APPRAISAL"})
                if t.appraisal is not None:
                    return err(409, "APPRAISAL_ALREADY_SUBMITTED", "appraisal already submitted")
                items = (body or {}).get("items")
                if not isinstance(items, list):
                    return err(400, "INVALID_FIELD", "items must be a list", field="items")
                app = {}
                for it in items:
                    if not isinstance(it, dict) or t.lot(it.get("lot_id")) is None:
                        return err(404, "UNKNOWN_LOT", f"unknown lot {it.get('lot_id') if isinstance(it, dict) else it}")
                    v = it.get("est_value_per_kg")
                    if not isinstance(v, (int, float)) or v <= 0:
                        return err(400, "INVALID_FIELD", "est_value_per_kg must be > 0", field="est_value_per_kg")
                    app[it["lot_id"]] = {"grade": it.get("grade"), "est_value_per_kg": float(v)}
                t.appraisal = app
                t.appraisal_at = round(self.a.appraisal_s - max(0.0, self.ends_at - time.time()), 1)
                if t.id == OUR:
                    self.appraisal_phase = self.phase
                return 201, {"items": [{"lot_id": k, **v} for k, v in app.items()], "submitted": True}
            return err(405, "METHOD_NOT_ALLOWED", f"{method} not allowed on {path}")
        if path.startswith("/v1/offers"):
            return self.handle_offers(method, path, body, t)
        if path == "/v1/results" and method == "GET":
            rid = (query.get("round") or [None])[0] or self.last_round
            if not rid or rid not in self.results[t.id]:
                return err(409, "NOT_AVAILABLE", f"no cleared results for {rid or 'any round'} yet",
                           hint="results appear after the round clears")
            return 200, self.results[t.id][rid]
        if path == "/v1/standings" and method == "GET":
            return 200, self.state(t)["standings"]
        if path == "/v1/leaderboard" and method == "GET":
            board = self.scores()
            if self.phase != "CLOSED":
                return 200, {"leaderboard": [{"rank": r["rank"], "team_id": r["team_id"], "name": r["name"]} for r in board]}
            return 200, {"leaderboard": board}
        if path == "/v1/profile":
            if method == "POST":
                t.name = str((body or {}).get("team_name") or t.name)
            return 200, {"team_id": t.id, "team_name": t.name, "university": None}
        if path in ("/v1/sandbox/reveal", "/v1/reveal") and method == "GET":
            if self.phase != "CLOSED":
                return err(409, "NOT_AVAILABLE", "reveal is available after the market closes")
            return 200, self.reveal()
        if path == "/v1/sandbox/reset":
            return err(409, "NOT_AVAILABLE", "reset is not available in this mode (competition)")
        known = {"/v1/state", "/v1/calendar", "/v1/lots", "/v1/rfqs", "/v1/results", "/v1/standings", "/v1/leaderboard"}
        if path in known:
            return err(405, "METHOD_NOT_ALLOWED", f"{method} not allowed on {path}")
        return err(404, "UNKNOWN_ENDPOINT", f"no endpoint {path}", hint="every path starts with /v1")

    def handle_offers(self, method, path, body, t: Team):
        def phase_err():
            if self.phase == "ROUND_CLEARING":
                return err(409, "ROUND_CLOSED", "the round is closed for offers", hint="wait for the next round")
            return err(409, "WRONG_PHASE", f"offers are accepted only in ROUND_OPEN (now {self.phase})",
                       allowed={"phase": "ROUND_OPEN"})
        if path == "/v1/offers":
            if method == "GET":
                return 200, {"offers": [self.public(o) for o in self._open_of(t.id)]}
            if method != "POST":
                return err(405, "METHOD_NOT_ALLOWED", f"{method} not allowed on {path}")
            if self.phase != "ROUND_OPEN":
                return phase_err()
            used_lot, cap = self._usage(t.id)
            e = self.validate(t, body, used_lot, cap)
            if e:
                code, msg, field, allowed = e
                status = 404 if code in ("UNKNOWN_RFQ", "UNKNOWN_LOT", "NOT_YOUR_LOT") else 400
                return err(status, code, msg, field=field, allowed=allowed)
            if t.id == OUR:
                self.batches[self.round[0]] += 1
            return 201, self.public(self._add_offer(t, body))
        if path == "/v1/offers:batch":
            if method != "POST":
                return err(405, "METHOD_NOT_ALLOWED", f"{method} not allowed on {path}")
            if self.phase != "ROUND_OPEN":
                return phase_err()
            offers = (body or {}).get("offers")
            if not isinstance(offers, list):
                return err(400, "INVALID_FIELD", "offers must be a list", field="offers")
            replace = bool((body or {}).get("replace_existing", False))
            exclude = {o["offer_id"] for o in self._open_of(t.id)} if replace else set()
            used_lot, cap = self._usage(t.id, exclude)
            errors = []
            for i, o in enumerate(offers):
                e = self.validate(t, o, used_lot, cap)
                if e:
                    errors.append({"index": i, "code": e[0], "message": e[1], "field": e[2], "allowed": e[3]})
                else:
                    used_lot[o["lot_id"]] += o["kg"]
                    cap += o["kg"]
            if errors:
                return err(400, "BATCH_REJECTED", f"{len(errors)} of {len(offers)} offers are invalid; nothing was saved",
                           allowed={"errors": errors})
            for oid in exclude:
                self.offers[oid]["status"] = "CANCELLED"
            if t.id == OUR:
                self.batches[self.round[0]] += 1
            return 201, {"offers": [self.public(self._add_offer(t, o)) for o in offers]}
        oid = path.rsplit("/", 1)[-1]
        o = self.offers.get(oid)
        if o is None or o["team"] != t.id:
            return err(404, "UNKNOWN_OFFER", f"no offer {oid}")
        if self.phase != "ROUND_OPEN" or o["status"] != "OPEN":
            return err(409, "ROUND_CLOSED", "the offer has expired")
        if method == "DELETE":
            o["status"] = "CANCELLED"
            return 200, self.public(o)
        if method == "PUT":
            new = {**{k: o[k] for k in ("rfq_id", "lot_id", "kg", "price_per_kg")},
                   **{k: v for k, v in (body or {}).items() if k in ("kg", "price_per_kg")}}
            used_lot, cap = self._usage(t.id, {oid})
            e = self.validate(t, new, used_lot, cap)
            if e:
                return err(400, e[0], e[1], field=e[2], allowed=e[3])
            o["kg"], o["price_per_kg"] = new["kg"], float(new["price_per_kg"])
            return 200, self.public(o)
        return err(405, "METHOD_NOT_ALLOWED", f"{method} not allowed on {path}")


def make_handler(m: Market):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send(self, status, body, headers=None):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def go(self, method):
            u = urlparse(self.path)
            path, query = u.path, parse_qs(u.query)
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if path == "/v1/version":
                return self.send(200, {"version": "mock-stage3-1", "mode": "competition", "mock": True,
                                       "server_time": time.time()})
            if path == "/mock/stats":
                with m.lock:
                    return self.send(200, m.stats())
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {m.a.team_key}":
                return self.send(*err(401, "UNAUTHORIZED", "missing or wrong key",
                                      hint="send Authorization: Bearer <team key> for this market"))
            with m.lock:
                ok = m.note_request()
                fault500 = m.a.faults and m.requests == m.fault500_at
            if not ok:
                return self.send(*err(429, "RATE_LIMITED", "more than 5 requests per second",
                                      allowed={"retry_after_s": 1}), headers={"Retry-After": "1"})
            if fault500:
                m.faults["http_500"] += 1
                return self.send(500, {"error": {"code": "INTERNAL", "message": "mock fault: internal error"}})
            if m.a.faults and path == "/v1/state" and m.rng.random() < 0.05:
                m.faults["state_delay_12s"] += 1
                time.sleep(12)
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return self.send(*err(400, "INVALID_FIELD", "body is not valid JSON"))
            m.tick()
            with m.lock:
                status, out = m.handle(method, path, query, body, m.teams[OUR])
            self.send(status, out)

        def do_GET(self):
            self.go("GET")

        def do_POST(self):
            self.go("POST")

        def do_PUT(self):
            self.go("PUT")

        def do_DELETE(self):
            self.go("DELETE")
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="nohoard")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--team-key", default="sk-T23-mock")
    ap.add_argument("--seed", type=int, default=20261120)
    ap.add_argument("--faults", action="store_true")
    ap.add_argument("--calendar", help="JSON file with a calendar list (default: stage-3 slides)")
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--setup-s", type=float, default=10)
    ap.add_argument("--appraisal-s", type=float, default=20)
    ap.add_argument("--open-s", type=float, default=20)
    ap.add_argument("--clear-s", type=float, default=5)
    ap.add_argument("--fest-premium", type=float, default=0.02, help="extra Specialty ceiling in festival rounds")
    ap.add_argument("--linger-s", type=float, default=60, help="keep serving this long after CLOSED")
    ap.add_argument("--out")
    a = ap.parse_args()
    m = Market(a)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(m))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"mock market on http://127.0.0.1:{a.port} · scenario {a.scenario} · faults {a.faults} · "
          f"capacity {m.cap_bags} bags/round · {len(m.teams) - 1} bots", flush=True)
    while True:
        m.tick()
        if m.phase == "CLOSED":
            time.sleep(a.linger_s)
            srv.shutdown()
            return
        time.sleep(0.1)


if __name__ == "__main__":
    main()
