#!/usr/bin/env bash
# Run the real agent (main.py, unchanged) against the local stage-3 mock market, in a copy under /tmp.
#
#   tools/run_mock.sh <nohoard|hoard|dump> [--faults] [--restart] [--port 8099] [--seed N] [-- extra mock args]
#
# --restart: kill the agent right after it sends R7 and start it again at once (expects round_already_sent),
#            then kill it after R10 and start it again only when R12 opens (R11 missed, R10 results backfilled).
# The agent copy never sees .env / .env.race: only *.py are copied and the env is given on the command line.
# Output: /tmp/t23-mock-<scenario>[-faults][-restart]/ (agent.log, mock.log, mock-result.json, agent/logs/)
set -euo pipefail

SC="${1:?usage: tools/run_mock.sh <nohoard|hoard|dump> [--faults] [--restart] [--port N] [--seed N]}"
shift
FAULTS="" RESTART="" PORT=8099 SEED=20261120 EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --faults) FAULTS=1 ;;
    --restart) RESTART=1 ;;
    --port) PORT="$2"; shift ;;
    --seed) SEED="$2"; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PY="$SRC/.venv/bin/python"
NAME="t23-mock-$SC${FAULTS:+-faults}${RESTART:+-restart}"
DIR="/tmp/$NAME"
URL="http://127.0.0.1:$PORT"

rm -rf "$DIR"
mkdir -p "$DIR/agent"
cp "$SRC"/*.py "$DIR/agent/"            # agent code only: no .env*, no logs/, no data/
rm -f "$DIR/agent/pack.py"

MOCK_ARGS=(--scenario "$SC" --port "$PORT" --seed "$SEED" --out "$DIR/mock-result.json" --linger-s 30)
[ -n "$FAULTS" ] && MOCK_ARGS+=(--faults)
"$PY" "$SRC/tools/mock_market.py" "${MOCK_ARGS[@]}" "${EXTRA[@]}" > "$DIR/mock.log" 2>&1 &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null || true; [ -n "${AGENT_PID:-}" ] && kill $AGENT_PID 2>/dev/null || true' EXIT

for _ in $(seq 50); do curl -sf "$URL/v1/version" >/dev/null && break; sleep 0.2; done

start_agent() {
  ( cd "$DIR/agent" && exec env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONUNBUFFERED=1 \
      MARKET_URL="$URL" TEAM_KEY=sk-T23-mock USE_LLM=0 LLM_THINKING=0 "$PY" main.py ) >> "$DIR/agent.log" 2>&1 &
  AGENT_PID=$!
  echo "agent started (pid $AGENT_PID)"
}

stat_field() {   # stat_field <python expr on s> ; s = /mock/stats
  curl -sf "$URL/mock/stats" | "$PY" -c "import json,sys; s=json.load(sys.stdin); print($1)" 2>/dev/null || echo ""
}

wait_for() {     # wait_for <python expr> : poll until it prints True
  while [ "$(stat_field "$1")" != "True" ]; do
    kill -0 $AGENT_PID 2>/dev/null || [ -n "${2:-}" ] || { echo "agent exited early"; return 1; }
    sleep 0.5
  done
}

echo "scenario $SC · faults ${FAULTS:-0} · restart ${RESTART:-0} · $DIR"
start_agent
if [ -n "$RESTART" ]; then
  wait_for "s['round']=='R7' and s['phase']=='ROUND_OPEN' and s['our_open_offers']>0"
  kill -9 $AGENT_PID; echo "killed agent in R7 after it sent"; start_agent
  wait_for "s['round']=='R10' and s['phase']=='ROUND_OPEN' and s['our_open_offers']>0"
  kill -9 $AGENT_PID; echo "killed agent in R10 after it sent; waiting for R12"
  wait_for "s['round']=='R12' and s['phase']=='ROUND_OPEN'" nocheck
  start_agent
fi
wait $AGENT_PID || true
echo "agent finished"
for _ in $(seq 40); do [ -s "$DIR/mock-result.json" ] && break; sleep 0.5; done
CHECK=(--dir "$DIR" --scenario "$SC")
[ -n "$RESTART" ] && CHECK+=(--restart)
"$PY" "$SRC/tools/check_mock_run.py" "${CHECK[@]}" | tee "$DIR/check.txt"
