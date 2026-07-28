#!/usr/bin/env bash
# GITM-RT tick: advance the segment chain by one segment (flock-guarded).
#
#   gitm_rt_tick.sh          one advance (the future cron entry)
#   gitm_rt_tick.sh --loop   keep advancing; sleep to the segment cadence
#                            once caught up to the lag target. For supervised
#                            soaks — NOT for cron. Ctrl-C/kill to stop.
#
# Mirrors MIDL's realtime_tick.sh conventions: flock -n (skip beats queue),
# state outside the repo, config in rt/rt_config.sh (env GITM_RT_* overrides).
set -u

RT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$RT_DIR/rt_config.sh"
STATE_ROOT="${GITM_RT_STATE_ROOT:-$STATE_ROOT}"
mkdir -p "$STATE_ROOT/logs"

LOCK="$STATE_ROOT/tick.lock"
export TMPDIR="${GITM_RT_TMPDIR_OVERRIDE:-$TMPDIR_OVERRIDE}"

tick_once() {
    flock -n "$LOCK" python3 "$RT_DIR/segment.py" advance
    local rc=$?
    # Product step: non-fatal, only after a successful segment. Failures
    # land in products.log and never touch the chain.
    if [ "$rc" -eq 0 ] && [ "${GITM_RT_PRODUCTS_ENABLE:-${PRODUCTS_ENABLE:-0}}" = "1" ]; then
        flock -n "$STATE_ROOT/products.lock" nice -n "${GITM_RT_NICE:-$NICE}" \
            "${GITM_RT_PRODUCTS_PY:-$PRODUCTS_PY}" "$RT_DIR/products.py" \
            >> "$STATE_ROOT/logs/products.log" 2>&1 || true
    fi
    # Heartbeat on EVERY tick outcome (the solsticedisk watchdog's signal).
    python3 "$RT_DIR/heartbeat.py" "$rc" > /dev/null 2>&1 || true
    return "$rc"
}

if [ "${1:-}" != "--loop" ]; then
    tick_once
    exit $?
fi

# Data-driven pacing (Connor, 2026-07-21): the model runs whenever new
# solar-wind data is present — advance() itself is gated on the observation
# frontier, so rc=0 means "there was a segment's worth of new settled data"
# (go straight back for more: catch-up needs no special case) and rc=3
# means "no new data yet" (poll again in a minute). Lag is an OUTCOME of
# feed latency + the 5-min segment quantum, not a pacing control.
echo "GITM-RT supervised loop (state: $STATE_ROOT). Ctrl-C to stop."
while true; do
    tick_once
    case $? in
        0)  ;;
        3)  sleep 60 ;;
        *)  echo "advance failed; sleeping 60s before retry"; sleep 60 ;;
    esac
done
