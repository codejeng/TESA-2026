"""#44 SEND_AT in main.play: record results at once, decide/send when seconds_left <= SEND_AT, retry until sent.

    .venv/bin/python -m pytest -q tests/test_send_at.py
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import main  # noqa: E402
import strategy as S  # noqa: E402
from test_strategy_units import RULES  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    S.reset_overrides()
    yield
    S.reset_overrides()


def _write(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------- SEND_AT ----------------
LOT = {"lot_id": "L1", "kg_total": 120, "kg_remaining": 120, "kg_available": 120,
       "sample": {"defect_pct": 4.0, "moisture_pct": 11.5, "screen": 15}}
RFQ = {"rfq_id": "R1-01", "buyer": "b", "buyer_type": "COMMERCIAL", "kg": 120, "bags": 2,
       "spec": {"max_defect_pct": 7.0, "max_moisture_pct": 12.5, "min_screen": None}}


def _st(phase, left, last=None):
    return {"market": "sandbox", "phase": phase, "paused": False, "appraisal_submitted": True,
            "round": {"round_id": "R1", "kind": "real", "number": 1, "of": 1, "calendar": "ปกติ"},
            "lots": [LOT], "rfqs": [RFQ], "my_open_offers": [], "last_results": last,
            "seconds_left": left, "capacity_left_kg": 120, "round_capacity_kg": 120, "standings": {}}


class API:
    def __init__(self, lefts, fail=0):
        self.states = [_st("ROUND_OPEN", x) for x in lefts] + [_st("CLOSED", None)]
        self.sent_at, self.fail, self.calls = [], fail, 0
        self.cur = None

    def calendar(self):
        return {"rules": RULES, "calendar": []}

    def state(self):
        self.cur = self.states.pop(0)
        return self.cur

    def offer_batch(self, offers):
        self.calls += 1
        if self.calls <= self.fail:
            import httpx
            raise httpx.ReadTimeout("slow")
        self.sent_at.append(self.cur["seconds_left"])
        return {"offers": offers}


def _play(tmp_path, monkeypatch, api, send_at=None):
    monkeypatch.setattr(main, "LOGS", tmp_path)
    monkeypatch.setattr(main, "OVERRIDES", tmp_path / "overrides.json")
    sleeps = []
    monkeypatch.setattr(main.time, "sleep", sleeps.append)
    if send_at is None:
        monkeypatch.delenv("SEND_AT", raising=False)
    else:
        monkeypatch.setenv("SEND_AT", str(send_at))
    main.play(api, None, forever=False)
    rows = [json.loads(x) for x in (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    return rows, sleeps


def test_send_at_waits_then_sends_in_time(tmp_path, monkeypatch):
    api = API([300, 290, 250, 200, 181, 179, 170])
    rows, sleeps = _play(tmp_path, monkeypatch, api)
    assert api.sent_at == [179]
    w = [r for r in rows if r["event"] == "waiting_to_send"]
    assert len(w) == 1 and w[0]["seconds_left"] == 300 and w[0]["send_at"] == 180 and w[0]["lots"] == {"L1": 120}
    assert max(sleeps[:5]) <= 5.0 and sleeps[4] == pytest.approx(1.2)     # wakes right at SEND_AT
    sent = [r for r in rows if r["event"] == "offers_sent"]
    assert len(sent) == 1 and any(n.startswith("overrides: ") for n in sent[0]["notes"])


def test_short_round_sends_at_once(tmp_path, monkeypatch):
    api = API([20, 15])                                   # mock rounds: 20 s < SEND_AT
    rows, _ = _play(tmp_path, monkeypatch, api)
    assert api.sent_at == [20] and not [r for r in rows if r["event"] == "waiting_to_send"]


def test_unknown_seconds_left_sends_at_once(tmp_path, monkeypatch):
    api = API([None])
    _play(tmp_path, monkeypatch, api)
    assert api.sent_at == [None]


def test_send_error_retried_until_sent(tmp_path, monkeypatch):
    api = API([300, 180, 150, 30, 18, 10], fail=3)       # 3 timeouts: sent on the 4th try at 18 s (send_late)
    rows, _ = _play(tmp_path, monkeypatch, api, send_at=180)
    ev = [r["event"] for r in rows]
    assert api.sent_at == [18] and ev.count("send_error") == 3 and "send_late" in ev and "offers_sent" in ev


def test_send_at_floor(monkeypatch):
    monkeypatch.setenv("SEND_AT", "5")
    assert main.send_at() == main.SEND_AT_MIN
    monkeypatch.setenv("SEND_AT", "abc")
    assert main.send_at() == 180.0


def test_overrides_applied_logged_by_main(tmp_path, monkeypatch):
    _write(tmp_path / "overrides.json", {"set": {"FLOOR_EARLY": 0.9}, "reason": "r", "author": "fable"})
    rows, _ = _play(tmp_path, monkeypatch, API([20]))
    a = [r for r in rows if r["event"] == "overrides_applied"]
    assert len(a) == 1 and a[0]["set"] == {"FLOOR_EARLY": 0.9} and a[0]["round"] == "R1"
