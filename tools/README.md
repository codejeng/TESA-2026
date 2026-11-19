# tools/

| file | does |
|---|---|
| `report.py` | game summary from logs (PR, kg-weighted MAPE) |
| `save_reveal.py` | save `/v1/sandbox/reveal` after a sandbox game (refuses anything but sandbox) |
| `rounds_md.py` | per-round markdown from a memory file |
| `llm_smoke.py` | test the event LLM gateway with the agent's real LLM step (never calls the market) |
| `mock_market.py` | **local stage-3 mock market** (stdlib only) for running the real `main.py` end to end |
| `run_mock.sh` | copy the agent to `/tmp`, run it against the mock, then `check_mock_run.py` |
| `check_mock_run.py` | pass/fail checks for one mock run (task #44) |

## Mock market (stage 3)

```bash
tools/run_mock.sh nohoard                 # hoarder bots 0%
tools/run_mock.sh hoard                   # hoarder bots 60%: hold Specialty lots until the festival (R6–8)
tools/run_mock.sh dump --faults           # dumper bots 50% (−0.05 in the last 3 rounds) + faults
tools/run_mock.sh nohoard --restart       # kill the agent after it sends R7 (restart at once), and after R10 (restart at R12)
tools/run_mock.sh hoard -- --open-s 7 --clear-s 2 --setup-s 3 --appraisal-s 6    # faster game (~2.5 min)
```

- Standing sandbox for manual tests: `.venv/bin/python tools/mock_market.py --scenario hoard --port 8200 --wait-start --loop`
  (`--wait-start`: WAITING until the first request with the key, like pressing start · `--loop`: new game after each close)
  then run the agent with `MARKET_URL=http://127.0.0.1:8200 TEAM_KEY=sk-T23-mock USE_LLM=0` (`--forever` for several games).
- Default timing ≈ 8 min per game: setup 10 s, appraisal 20 s, 2 practice + 15 real rounds of 20 s open + 5 s clearing
  (real: 600 / 900 / 300 / 60 s). Run several scenarios at once with `--port`.
- Output in `/tmp/t23-mock-<scenario>[-faults][-restart]/`: `agent.log`, `mock.log`, `mock-result.json`, `check.txt`,
  `agent/logs/decisions.jsonl` and the memory file. Nothing is written to `logs/` of the repo.
- The agent copy gets only `*.py`; `MARKET_URL`, `TEAM_KEY=sk-T23-mock`, `USE_LLM=0` come from the command line
  (`env -i`), so `.env` / `.env.race` are never read or copied. The mock listens on 127.0.0.1 only.
- **The mock:** same paths, JSON and error codes as `คู่มือ-API.md` (`BATCH_REJECTED`, `ROUND_CLOSED`, `WRONG_PHASE`,
  `RATE_LIMITED` 5/s with burst 10, ...), rules from the real `/v1/calendar` (TV formula, capacity factor 1.5),
  the stage-3 calendar (or `--calendar file.json`), 6 lots / 45 bags shared by all teams (true quality hidden,
  sample noise the same for every team, edge cases: a lot whose sample screen 17 is really 16, and a lot whose sample
  defect 6.7 is really 7.0+), the 6 real buyers and specs, hidden ceilings per RFQ (price ÷ true TV), clearing by
  price ÷ true TV with bag-by-bag ties, 55 bots (starter / adaptive / hoarder / dumper).
- `--faults`: 5% of `GET /v1/state` delayed 12 s (client timeout is 10 s), one HTTP 500 per game, and the request
  rate of our team is recorded (the 5/s limit with 429 is always on).
- `GET /mock/stats` (no key): phase, request stats, our history per round, and after closing the leaderboard
  (PR, PR score = 80 × (PR − 0.80) ÷ (best PR − 0.80), appraisal score = 20 × max(0, 1 − MAPE/10%)) and our reveal.
- Not modelled: other teams' real strategies, buyer personas beyond spec + ceiling, `PUT`/`DELETE` edge cases
  beyond the basics, whether practice rounds cut stock (here they do not).
