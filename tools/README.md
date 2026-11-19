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
| `round_report.py` | in-game report (≤ 60 lines) from local files + `/display/data` (no key): offers, k per buyer, market, overrides, PLAYBOOK signals |
| `wait_round.py` | wait for a new round result / market close / timeout, then print `round_report` (see `PLAYBOOK.md`) |

## In-game control (#44)

```bash
.venv/bin/python tools/wait_round.py --timeout 420 --display http://HOST:PORT --team See-It2026
.venv/bin/python tools/round_report.py --dir /tmp/t23-mock-hoard-ov/agent --display http://127.0.0.1:8320
```

- `main.py` records the last round's results when a round opens, then waits until `seconds_left <= SEND_AT`
  (env, default 180, min 30) before it decides, so there is time to edit `overrides.json` (rules in `PLAYBOOK.md`).
- Neither tool calls the market API with a key; the leaderboard comes from `<url>/display/data` at most every 30 s
  (cache `logs/.display_cache.json`) and is skipped on any error.

## Mock market (stage 3)

```bash
tools/run_mock.sh nohoard                 # hoarder bots 0%
tools/run_mock.sh hoard                   # hoarder bots 60%: hold Specialty lots until the festival (R6–8)
tools/run_mock.sh dump --faults           # dumper bots 50% (−0.05 in the last 3 rounds) + faults
tools/run_mock.sh nohoard --restart       # kill the agent after it sends R7 (restart at once), and after R10 (restart at R12)
tools/run_mock.sh hoard --seed 1 --tag s1-off --schedule-off   # A/B: SCHEDULE = False in the /tmp copy only
tools/run_mock.sh hoard -- --open-s 7 --clear-s 2 --setup-s 3 --appraisal-s 6    # faster game (~2.5 min)
```

- **`/trade` = the organizers' trade page** (copied from the sandbox `/trade`, `tools/mock_trade/trade.html` + fonts):
  log in with the team key, team name and university, exactly like the real one. The mock serves what the page calls:
  `/v1/version` (`mode`, `trade_page`), `/v1/profile`, `/v1/state`, `/v1/calendar`, `/v1/appraisals`, `/v1/offers`
  (POST/PUT/DELETE), `/v1/sandbox/advance` ("ข้ามไปช่วงถัดไป"), `/v1/sandbox/reset`, `/v1/sandbox/reveal`.
  - `--mode sandbox`: the page can send offers and shows the sandbox buttons · `--mode competition` (default): the page is
    read-only and manual orders get `MANUAL_DISABLED`, like day 3.
  - `--any-key`: any `sk-...` key plays as T23 (keys are never stored), so `main.py` runs with `.env` as is
    (only `MARKET_URL` points at the mock).
  - Page requests (`X-Client: trade-page`) share the team's 5/s budget as on the real market, but are not counted in
    the agent's request stats and never start a `--wait-start` game.
  - `/mock/ui`: our own extra dashboard (history per round, leaderboard).
- **`/display` = the organizers' stage display** (`tools/mock_trade/display.html`; `?mode=reveal` after close: Space/→
  reveals the final ranking from last place). `/display/data` is built from the fields that page reads (the real one is
  admin-only): live ranking by PR without numbers, ▲▼ moves, frozen after `leaderboard_freeze_after_round` (12),
  15-round calendar with festival / demand bars, average price per buyer type, wanted vs bought per round.
- Standing stage-3 sandbox: `.venv/bin/python tools/mock_market.py --scenario hoard --port 8200 --mode sandbox --any-key --wait-start --loop`
  then open http://127.0.0.1:8200/trade and run the agent with `MARKET_URL=http://127.0.0.1:8200`.
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
