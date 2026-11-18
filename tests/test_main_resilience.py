"""main.play must survive network timeouts (sandbox 13:52: a slow server cost a whole round)."""
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import main  # noqa: E402
import strategy  # noqa: E402

LOT = {"lot_id": "L1", "kg_total": 120, "kg_remaining": 120, "kg_available": 120,
       "sample": {"defect_pct": 4.0, "moisture_pct": 11.5, "screen": 15}}
RFQ = {"rfq_id": "R1-01", "buyer": "b", "buyer_type": "COMMERCIAL", "kg": 120, "bags": 2,
       "spec": {"max_defect_pct": 7.0, "max_moisture_pct": 12.5, "min_screen": None}}


def _state(phase, open_offers=()):
    return {"market": "sandbox", "phase": phase, "paused": False, "appraisal_submitted": phase != "APPRAISAL",
            "round": {"round_id": "R1", "kind": "real", "number": 1, "of": 1, "calendar": "ปกติ"},
            "lots": [LOT], "rfqs": [RFQ], "my_open_offers": list(open_offers), "last_results": None,
            "seconds_left": 1, "capacity_left_kg": 120, "round_capacity_kg": 120,
            "standings": {"revenue_thb": 0}}


class FakeAPI:
    def __init__(self):
        self.states = [_state("APPRAISAL"), _state("APPRAISAL"), _state("ROUND_OPEN"), _state("ROUND_OPEN"),
                       _state("CLOSED")]
        self.appraise_calls = self.batch_calls = 0

    def calendar(self):
        from test_strategy_units import RULES
        return {"rules": RULES, "calendar": []}

    def state(self):
        return self.states.pop(0)

    def appraise(self, items):
        self.appraise_calls += 1
        if self.appraise_calls == 1:
            raise httpx.ConnectTimeout("slow server")
        return {}

    def offer_batch(self, offers):
        self.batch_calls += 1
        if self.batch_calls == 1:
            raise httpx.ReadTimeout("slow server")
        return {"offers": offers}

    def results(self, *a, **k):
        return {}


def test_play_survives_timeouts(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOGS", tmp_path)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    api = FakeAPI()
    main.play(api, None, forever=False)                    # must return at CLOSED, not raise
    log = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8")
    assert api.appraise_calls == 2 and "appraisal_sent" in log
    assert api.batch_calls == 2 and "send_error" in log and '"event": "offers_sent", "round": "R1"' in log
