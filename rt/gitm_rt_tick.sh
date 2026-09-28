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

# Failure containment (added 2026-09-23 after the 09-03 and 09-22 outages):
# an aborted GITM launch (MPI_Abort inside GITM, e.g. an IMF hole -> "Issue
# with Indices") leaves 16 PSM3 shared-memory files (~70 MB) in /dev/shm
# per attempt and the per-minute cron retry then fills the 126 GB tmpfs
# in ~1 day. So: (a) after a FAIL, delete OUR leftover psm3 files (only
# when none of our GITM/mpirun processes exist); (b) after FAIL_FAST
# consecutive FAILs, retry only every FAIL_SLOW_MIN minutes. The heartbeat
# still runs every tick so status.json / lag_min stay live for the watchdog.
FAIL_FAST="${GITM_RT_FAIL_FAST:-5}"
FAIL_SLOW_MIN="${GITM_RT_FAIL_SLOW_MIN:-10}"
FAIL_COUNT_FILE="$STATE_ROOT/fail_count"

clean_own_shm() {
    if pgrep -u "$(id -u)" -x GITM.exe >/dev/null || pgrep -u "$(id -u)" -x mpirun >/dev/null; then
        return 0
    fi
    local n
    n=$(find /dev/shm -maxdepth 1 -user "$(id -u)" -name 'psm3_shm*' -print -delete 2>/dev/null | wc -l)
    [ "$n" -gt 0 ] && echo "$(date -u +%FT%T) cleaned $n leftover psm3_shm files from /dev/shm"
    return 0
}

tick_once() {
    local fails=0 rc
    [ -f "$FAIL_COUNT_FILE" ] && fails=$(cat "$FAIL_COUNT_FILE" 2>/dev/null || echo 0)
    if [ "$fails" -ge "$FAIL_FAST" ] && [ $(( $(date -u +%s) / 60 % FAIL_SLOW_MIN )) -ne 0 ]; then
        # Backed off: no advance attempt this minute, heartbeat only.
        rc=1
    else
        flock -n "$LOCK" python3 "$RT_DIR/segment.py" advance
        rc=$?
        case "$rc" in
            0|3) rm -f "$FAIL_COUNT_FILE" ;;
            *)   echo $((fails + 1)) > "$FAIL_COUNT_FILE"
                 flock -n "$LOCK" bash -c "$(declare -f clean_own_shm); clean_own_shm"
                 if [ $((fails + 1)) -eq "$FAIL_FAST" ]; then
                     echo "$(date -u +%FT%T) $FAIL_FAST consecutive FAILs; backing off to one retry per ${FAIL_SLOW_MIN} min"
                 fi ;;
        esac
    fi
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
