"""Smoke-test the event LLM gateway with the agent's real LLM step. Never calls the market API.

    .venv/bin/python tools/llm_smoke.py [--n 5] [--gap 4] [--tag c4fd7d50] [--round 4]

Uses LLM_BASE_URL / LLM_MODEL / LLM_API_KEY from .env exactly like main.py (key never printed).
Cases, n calls each, at least --gap seconds apart (429 -> wait Retry-After/30 s, retry that call once):
  a      short question ("answer OK")
  b80    strategy.llm_adjust() on a real mid-game state from data/sandbox-20261118, seconds_left=80 (sandbox)
  b230   same, seconds_left=230 (competition rounds are 4 min)
  c230   same as b230 with thinking on (LLM_THINKING=1)
Per call: seconds, ok/error, schema-valid JSON, delta per buyer (raw and applied), reason length, prompt tokens.
Writes logs/llm-smoke-<time>.json and prints a summary.
"""
import argparse
import json
import os
import statistics as st
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
from openai.resources.chat import completions as _cc  # noqa: E402

import strategy as S  # noqa: E402
from llm import LLM  # noqa: E402
from main import load_env  # noqa: E402
from test_sandbox_cases import replay  # noqa: E402

CONTEXT_LIMIT = 8192
USAGE: list = []                       # usage of every completion (prompt/completion tokens), filled by the wrapper
_orig_create = _cc.Completions.create


def _create(self, *a, **k):
    r = _orig_create(self, *a, **k)
    USAGE.append(getattr(r, "usage", None))
    return r


_cc.Completions.create = _create


class Recorder:
    """Wraps LLM.ask_json to keep the prompt and the raw answer of the last call."""

    def __init__(self, llm: LLM):
        self.llm, self.last = llm, {}

    def ask_json(self, system, user, schema, timeout=None):
        a = self.llm.ask_json(system, user, schema, timeout=timeout)
        self.last = {"system": system, "user": user, "answer": a}
        return a

    @property
    def last_error(self):
        return self.llm.last_error


def make_llm(thinking: bool) -> LLM:
    return LLM(os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ.get("LLM_API_KEY", ""),
               timeout=float(os.environ.get("LLM_TIMEOUT", "60")), thinking=thinking)


def schema_ok(a) -> bool:
    return (isinstance(a, dict) and isinstance(a.get("deltas"), list) and isinstance(a.get("reason"), str)
            and all(isinstance(d, dict) and isinstance(d.get("buyer"), str) and isinstance(d.get("delta"), (int, float))
                    for d in a["deltas"]))


def is_rate_limited(err: str | None) -> bool:
    return bool(err) and ("429" in err or "RateLimit" in err or "TOO_MANY" in err)


def call_short(llm: LLM) -> dict:
    t0, n0 = time.time(), len(USAGE)
    a = llm.ask_json("Answer only JSON.", "ตอบ OK", {"type": "object", "required": ["answer"],
                                                     "properties": {"answer": {"type": "string"}}}, timeout=20)
    u = USAGE[n0] if len(USAGE) > n0 else None
    return {"seconds": round(time.time() - t0, 2), "ok": a is not None, "error": llm.last_error,
            "schema_ok": isinstance(a, dict) and isinstance(a.get("answer"), str), "answer": a,
            "prompt_tokens": getattr(u, "prompt_tokens", None), "completion_tokens": getattr(u, "completion_tokens", None)}


def call_adjust(llm: LLM, tag: str, rnd: int, seconds_left: float) -> dict:
    state, ctx, _ = replay(tag, rnd)
    rec = Recorder(llm)
    ctx.llm, ctx.seconds_left = rec, seconds_left
    L = S.learn(state, ctx)
    before = {b: v["k"] for b, v in L["buyers"].items()}
    pres = S.pressure(state, ctx, L["fill"], S.sellable_lots(state, ctx, L))
    t0, n0 = time.time(), len(USAGE)
    used = S.llm_adjust(state, ctx, L["buyers"], pres)
    dt = time.time() - t0
    a = rec.last.get("answer")
    u = USAGE[n0] if len(USAGE) > n0 else None
    raw = {d.get("buyer"): d.get("delta") for d in (a or {}).get("deltas", []) if isinstance(d, dict)}
    applied = {b: round(L["buyers"][b]["k"] - before[b], 3) for b in before}
    return {"seconds": round(dt, 2), "ok": used, "error": llm.last_error, "schema_ok": schema_ok(a),
            "timeout_used": min(S.LLM_MAX_SECONDS, seconds_left - 10), "pressure": round(pres, 3),
            "raw_delta": raw, "applied_delta": applied, "unknown_buyers": sorted(set(raw) - set(before)),
            "reason": (a or {}).get("reason"), "reason_len": len(str((a or {}).get("reason") or "")),
            "prompt_chars": len(rec.last.get("system", "")) + len(rec.last.get("user", "")),
            "prompt_tokens": getattr(u, "prompt_tokens", None), "completion_tokens": getattr(u, "completion_tokens", None)}


def run_case(name: str, fn, n: int, gap: float, results: list) -> None:
    for i in range(n):
        t_start = time.time()
        r = fn()
        if is_rate_limited(r.get("error")):
            print(f"  {name} #{i + 1}: 429, waiting 30 s then retrying once", flush=True)
            time.sleep(30)
            r = fn()
        r.update(case=name, i=i + 1)
        results.append(r)
        d = r.get("applied_delta") or {}
        print(f"  {name} #{i + 1}: {'ok ' if r['ok'] else 'ERR'} {r['seconds']:>5}s schema={r['schema_ok']} "
              f"tokens={r.get('prompt_tokens')}+{r.get('completion_tokens')} "
              f"{'Δ ' + json.dumps(d, ensure_ascii=False) if d else ''} {r['error'] or ''}"[:220], flush=True)
        time.sleep(max(0.0, gap - (time.time() - t_start)))


def summarize(results: list) -> dict:
    out = {}
    for case in dict.fromkeys(r["case"] for r in results):
        rs = [r for r in results if r["case"] == case]
        secs = sorted(r["seconds"] for r in rs)
        ok = [r for r in rs if r["ok"]]
        deltas = [v for r in ok for v in (r.get("raw_delta") or {}).values() if isinstance(v, (int, float))]
        applied = [v for r in ok for v in (r.get("applied_delta") or {}).values()]
        toks = [r["prompt_tokens"] for r in rs if r.get("prompt_tokens")]
        out[case] = {
            "calls": len(rs), "success": len(ok), "success_rate": round(len(ok) / len(rs), 3),
            "schema_ok": sum(r["schema_ok"] for r in rs),
            "sec_median": round(st.median(secs), 2), "sec_p95": round(secs[min(len(secs) - 1, int(0.95 * len(secs)))], 2),
            "sec_max": secs[-1],
            "errors": sorted({(r["error"] or "")[:80] for r in rs if r["error"]}),
            "delta_mean_raw": round(st.mean(deltas), 4) if deltas else None,
            "delta_neg_zero_pos": [sum(d < 0 for d in deltas), sum(d == 0 for d in deltas), sum(d > 0 for d in deltas)],
            "applied_mean": round(st.mean(applied), 4) if applied else None,
            "reason_len_max": max((r.get("reason_len", 0) for r in rs), default=0),
            "prompt_tokens_max": max(toks) if toks else None,
            "context_headroom": (CONTEXT_LIMIT - max(toks)) if toks else None,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--gap", type=float, default=4.0)
    ap.add_argument("--tag", default="c4fd7d50")          # game 10:29
    ap.add_argument("--round", type=int, default=4)
    ap.add_argument("--skip-thinking", action="store_true")
    a = ap.parse_args()
    load_env(HERE / ".env")
    print(f"gateway port {os.environ['LLM_BASE_URL'].rsplit(':', 1)[-1]} · model {os.environ['LLM_MODEL']} · "
          f"state: game {a.tag} R{a.round} · {a.n} calls per case, gap ≥ {a.gap}s", flush=True)
    results: list = []
    plain, think = make_llm(False), make_llm(True)
    run_case("a", lambda: call_short(plain), a.n, a.gap, results)
    run_case("b80", lambda: call_adjust(plain, a.tag, a.round, 80), a.n, a.gap, results)
    run_case("b230", lambda: call_adjust(plain, a.tag, a.round, 230), a.n, a.gap, results)
    if not a.skip_thinking:
        run_case("c230", lambda: call_adjust(think, a.tag, a.round, 230), a.n, a.gap, results)
        results.append({"case": "_thinking_style", "seconds": 0, "ok": True, "error": None, "schema_ok": True,
                        "style": ["reasoning_effort", "chat_template_kwargs"][think.style]})
    summary = summarize([r for r in results if not r["case"].startswith("_")])
    out = HERE / "logs" / f"llm-smoke-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nsummary:")
    for case, s in summary.items():
        print(f"  {case}: {json.dumps(s, ensure_ascii=False)}")
    b = [r for r in results if r["case"] in ("a", "b80", "b230")]
    rate = sum(r["ok"] for r in b) / len(b)
    p95 = sorted(r["seconds"] for r in b)[min(len(b) - 1, int(0.95 * len(b)))]
    bad = [r["error"] for r in b if r["error"] and ("401" in r["error"] or "APIConnectionError" in r["error"])]
    verdict = rate >= 0.9 and p95 <= 15 and not bad
    print(f"\nstep 1 (a, b80, b230): success {rate:.0%} · p95 {p95}s · 401/connection errors {len(bad)} -> "
          f"{'PASS' if verdict else 'FAIL'} · saved {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
