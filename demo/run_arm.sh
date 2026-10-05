#!/usr/bin/env bash
#
# Run one demo arm -- baseline (shadow) or routed (active) -- end to end.
#
#   bash demo/run_arm.sh <baseline|routed> <n> [--dry-run]
#
# Baseline records what the router *would* do and always runs the strong model.
# Routed applies it: some requests are rewritten to the cheap model, and their
# rows carry a model_used that differs from model_requested.
#
# What happens, in order:
#   1. regenerate the task project into /tmp/tamias-demo-task (buggy sources)
#   2. start the proxy in the background, logging to demo/runs/<arm>-<n>.db
#   3. live only: check OPENROUTER_API_KEY is present, print only "key set" or
#      "key missing" -- never the value -- write an opencode.json pointing at
#      the proxy, and run `timeout 900 opencode run --auto`
#      dry-run only: start the test suite's mock upstream on port 9001 and send
#      demo/dry_run_client.py's 12 realistic requests instead
#   4. run the task's own pytest
#   5. kill the proxy by the recorded PID
#   6. write demo/runs/<arm>-<n>.json with the result
#
# --dry-run touches no network, needs no key, and runs no agent.  It still
# starts the real proxy in front of a real upstream socket, so the routing
# decisions it records come from the same code path a live run uses.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DIR="$REPO_ROOT/demo"
RUNS_DIR="$DEMO_DIR/runs"
TASK_DIR="/tmp/tamias-demo-task"
CONFIG="$DEMO_DIR/config.env"

PYTHON="$REPO_ROOT/.venv/bin/python"
PROXY_PORT=8000
MOCK_PORT=9001
AGENT_TIMEOUT=900

# The two real OpenRouter ids priced by prices.openrouter-sim.toml.  The dry run
# uses these because the sim sheet carries their ids with invented rates, so a
# rehearsal can price a real log.
SIM_STRONG="nvidia/nemotron-3-ultra-550b-a55b:free"
SIM_CHEAP="nvidia/nemotron-3.5-lightning:free"
SIM_PRICES="prices.openrouter-sim.toml"

die() {
	echo "run_arm: $*" >&2
	exit 1
}

usage() {
	echo "usage: bash demo/run_arm.sh <baseline|routed> <n> [--dry-run]" >&2
}

# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------

ARM="${1:-}"
N="${2:-}"
DRY_RUN=""
for arg in "$@"; do
	case "$arg" in
	--dry-run) DRY_RUN="yes" ;;
	esac
done

[ -n "$ARM" ] && [ -n "$N" ] || {
	usage
	exit 2
}
case "$ARM" in
baseline | routed) ;;
*) die "unknown arm '$ARM': expected baseline or routed" ;;
esac
case "$N" in
'' | *[!0-9]*) die "run number '$N' must be a positive integer" ;;
esac
[ "$N" -ge 1 ] || die "run number '$N' must be 1 or more"

[ -x "$PYTHON" ] || die "no venv python at $PYTHON"
[ -f "$CONFIG" ] || die "no $CONFIG; copy demo/config.env.example to demo/config.env"

# shellcheck disable=SC1090
set -a
. "$CONFIG"
set +a

: "${STRONG_MODEL:=$SIM_STRONG}"
: "${CHEAP_MODEL:=$SIM_CHEAP}"
: "${UPSTREAM:=https://openrouter.ai/api}"
: "${EFFORT_POLICY:=}"
: "${PRICES:=$SIM_PRICES}"

if [ -n "$DRY_RUN" ]; then
	# Rehearsal against the mock upstream, whatever config.env says about the
	# real world.  Prices and models must agree, or rows come out UNKNOWN.
	PRICES="$SIM_PRICES"
	STRONG_MODEL="$SIM_STRONG"
	CHEAP_MODEL="$SIM_CHEAP"
	UPSTREAM="http://127.0.0.1:$MOCK_PORT"
fi

DB="$RUNS_DIR/$ARM-$N.db"
JSON="$RUNS_DIR/$ARM-$N.json"
PROXY_PID=""
MOCK_PID=""

cleanup() {
	# Kill by recorded PID only, never by pattern: another agent may be running
	# arms on this machine right now.
	for pid in "$PROXY_PID" "$MOCK_PID"; do
		if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
			kill "$pid" 2>/dev/null
			wait "$pid" 2>/dev/null
		fi
	done
}
trap cleanup EXIT INT TERM

wait_for_port() {
	local port="$1" tries=100
	while [ "$tries" -gt 0 ]; do
		if "$PYTHON" -c "
import socket, sys
s = socket.socket()
s.settimeout(0.2)
sys.exit(0 if s.connect_ex(('127.0.0.1', $port)) == 0 else 1)
" 2>/dev/null; then
			return 0
		fi
		tries=$((tries - 1))
		sleep 0.1
	done
	return 1
}

wait_for_pid() {
	local pid="$1" log="$2" tries=100
	while [ "$tries" -gt 0 ]; do
		if ! kill -0 "$pid" 2>/dev/null; then
			return 1
		fi
		if wait_for_port "$PROXY_PORT"; then
			return 0
		fi
		tries=$((tries - 1))
		sleep 0.1
	done
	echo "run_arm: proxy did not come up; last log lines:" >&2
	tail -n 20 "$log" >&2 2>/dev/null
	return 1
}

# --------------------------------------------------------------------------
# 1. The task project
# --------------------------------------------------------------------------

mkdir -p "$RUNS_DIR"
rm -rf "$TASK_DIR"
"$PYTHON" "$DEMO_DIR/make_task.py" --out "$TASK_DIR" >/dev/null || die "make_task.py failed"

# The prompt the agent is given: the template with {PYTEST} replaced by the
# absolute command that runs *this* repo's pytest against the task project.
PYTEST_CMD="$PYTHON -m pytest -q"
PROMPT_FILE="$TASK_DIR/prompt.txt"
"$PYTHON" - "$DEMO_DIR/TASK_PROMPT.txt" "$PYTEST_CMD" "$PROMPT_FILE" <<'PY'
import pathlib
import sys

template, pytest_cmd, out = sys.argv[1], sys.argv[2], sys.argv[3]
text = pathlib.Path(template).read_text(encoding="utf-8")
pathlib.Path(out).write_text(text.replace("{PYTEST}", pytest_cmd), encoding="utf-8")
PY
[ -s "$PROMPT_FILE" ] || die "prompt.txt is empty"

echo "== $ARM $N $([ -n "$DRY_RUN" ] && echo '(dry run)' || echo '(live)')"
echo "   task     $TASK_DIR"
echo "   db       $DB"
echo "   prices   $PRICES"
echo "   models   strong=$STRONG_MODEL  cheap=$CHEAP_MODEL"
echo "   upstream $UPSTREAM"

# --------------------------------------------------------------------------
# 2. The proxy
# --------------------------------------------------------------------------

rm -f "$DB"
SERVE_LOG="$RUNS_DIR/$ARM-$N.serve.log"

# The dry run's upstream is the test suite's own mock, served over a real socket.
# It is the same app tests/test_proxy.py drives in-process
# (tests/mock_upstream.py:create_mock_upstream), so the proxy talks to something
# real without anything leaving the machine.
if [ -n "$DRY_RUN" ]; then
	echo "   starting mock upstream on port $MOCK_PORT"
	(
		cd "$REPO_ROOT" &&
			"$PYTHON" -m uvicorn \
			--factory tests.mock_upstream:create_mock_upstream \
			--host 127.0.0.1 --port "$MOCK_PORT" --log-level warning
	) >"$RUNS_DIR/$ARM-$N.mock.log" 2>&1 &
	MOCK_PID=$!
	wait_for_port "$MOCK_PORT" || die "mock upstream never listened on $MOCK_PORT"
fi

# baseline: shadow records the decision and never touches the bytes sent.
# routed: active applies it, and --inject-usage asks the upstream for token
# counts on streams the client did not ask for.
if [ "$ARM" = "baseline" ]; then
	SERVE_ARGS=(--router-mode shadow)
else
	SERVE_ARGS=(
		--router-mode active
		--cheap-model "$CHEAP_MODEL"
		--strong-model "$STRONG_MODEL"
		--inject-usage
	)
	if [ -n "$EFFORT_POLICY" ]; then
		SERVE_ARGS+=(--effort-policy "$EFFORT_POLICY")
	fi
fi

echo "   tamias serve ${SERVE_ARGS[*]}"
(
	cd "$REPO_ROOT" &&
		"$PYTHON" -m tamias.cli serve \
		--upstream "$UPSTREAM" \
		--prices "$PRICES" \
		--db "$DB" \
		--host 127.0.0.1 \
		--port "$PROXY_PORT" \
		"${SERVE_ARGS[@]}"
) >"$SERVE_LOG" 2>&1 &
PROXY_PID=$!
wait_for_pid "$PROXY_PID" "$SERVE_LOG" || die "proxy never listened on $PROXY_PORT"
echo "   proxy up (pid $PROXY_PID${MOCK_PID:+, mock pid $MOCK_PID})"

STARTED_AT=$SECONDS

# --------------------------------------------------------------------------
# 3. The work
# --------------------------------------------------------------------------

TESTS_PASSED=false

if [ -n "$DRY_RUN" ]; then
	echo "   sending the 12-request dry-run session"
	"$PYTHON" "$DEMO_DIR/dry_run_client.py" \
		--proxy "http://127.0.0.1:$PROXY_PORT" \
		--model "$STRONG_MODEL" \
		--session "demo-$ARM-$N" || die "dry-run client failed"
else
	# Never print, echo or expand the key: only whether it is there.
	if [ -n "${OPENROUTER_API_KEY:-}" ]; then
		echo "   key set"
	else
		echo "   key missing"
		die "OPENROUTER_API_KEY is not set; see demo/RUNBOOK.md step 3"
	fi

	# An OpenAI-compatible provider whose baseURL is the proxy.  Per
	# docs/OPENROUTER.md: the AI SDK appends /chat/completions, so baseURL
	# needs the /v1; the apiKey is a {env:...} substitution so no secret is
	# written to this file; and the model ids must match the price sheet keys
	# character for character, because those are what gets logged.
	cat >"$TASK_DIR/opencode.json" <<JSON
{
  "\$schema": "https://opencode.ai/config.json",
  "provider": {
    "tamias": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "tamias (local proxy)",
      "options": {
        "baseURL": "http://127.0.0.1:$PROXY_PORT/v1",
        "apiKey": "{env:OPENROUTER_API_KEY}"
      },
      "models": {
        "$STRONG_MODEL": { "name": "strong" },
        "$CHEAP_MODEL": { "name": "cheap" }
      }
    }
  },
  "model": "tamias/$STRONG_MODEL"
}
JSON

	echo "   opencode run --auto (timeout ${AGENT_TIMEOUT}s)"
	(cd "$TASK_DIR" && timeout "$AGENT_TIMEOUT" opencode run --auto "$(cat prompt.txt)") ||
		echo "   opencode exited non-zero; recording tests_passed=false"
fi

# --------------------------------------------------------------------------
# 4. Score the task
# --------------------------------------------------------------------------

echo "   running the task's pytest"
if (cd "$TASK_DIR" && "$PYTHON" -m pytest -q); then
	TESTS_PASSED=true
fi

WALL_SECONDS=$((SECONDS - STARTED_AT))

# --------------------------------------------------------------------------
# 5. Stop the proxy, by the PID we recorded at step 2
# --------------------------------------------------------------------------

if [ -n "$PROXY_PID" ] && kill -0 "$PROXY_PID" 2>/dev/null; then
	echo "   stopping proxy (pid $PROXY_PID)"
	kill "$PROXY_PID" 2>/dev/null
	wait "$PROXY_PID" 2>/dev/null
fi
PROXY_PID=""

# --------------------------------------------------------------------------
# 6. Record the run
# --------------------------------------------------------------------------

"$PYTHON" - "$JSON" "$ARM" "$N" "$TESTS_PASSED" "$WALL_SECONDS" "$DB" \
	"$STRONG_MODEL" "$CHEAP_MODEL" "$PRICES" "$([ -n "$DRY_RUN" ] && echo true || echo false)" <<'PY'
import json
import pathlib
import sys

out, arm, n, passed, wall, db, strong, cheap, prices, dry = sys.argv[1:11]

document = {
    "arm": arm,
    "n": int(n),
    "dry_run": dry == "true",
    "tests_passed": passed == "true",
    "wall_seconds": int(wall),
    "models": {"strong": strong, "cheap": cheap},
    "price_sheet": prices,
    "db": db,
}

path = pathlib.Path(out)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
print(f"   wrote {out}")
print(f"   arm={document['arm']} n={document['n']} dry_run={document['dry_run']} "
      f"tests_passed={document['tests_passed']} wall={document['wall_seconds']}s")
PY

echo "== done"