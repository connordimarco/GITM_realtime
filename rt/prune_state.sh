#!/bin/bash
# prune_state.sh — daily retention for GITM-RT state (cron: midnight).
# Deletes ONLY accumulated output older than RETENTION_DAYS:
#   - 3DALL block fragments in $STATE_ROOT/run/UA/data/
#   - per-segment logs in $STATE_ROOT/logs/segments/
# NEVER touches restartIN/, restartOUT/, state.json, harness.log, or the
# LAUREN/ landing zone (the solsticedisk manages its contents). Safe to
# run any time; the tick does not need to be paused (GITM only appends
# new output).

set -u
RT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$RT_DIR/rt_config.sh"
STATE_ROOT="${GITM_RT_STATE_ROOT:-$STATE_ROOT}"
DAYS="${GITM_RT_RETENTION_DAYS:-${RETENTION_DAYS:-2}}"

stamp() { date -u +%Y-%m-%dT%H:%M:%S; }

if [ ! -d "$STATE_ROOT" ]; then
    echo "$(stamp) SKIP no state root at $STATE_ROOT"
    exit 0
fi

n_data=0
if [ -d "$STATE_ROOT/run/UA/data" ]; then
    n_data=$(find "$STATE_ROOT/run/UA/data" -maxdepth 1 -type f \
        \( -name '3DALL*' -o -name '2DGEL*' -o -name '3DUSR*' \) \
        -mtime +"$DAYS" -print -delete | wc -l)
fi

n_logs=0
if [ -d "$STATE_ROOT/logs/segments" ]; then
    n_logs=$(find "$STATE_ROOT/logs/segments" -type f -mtime +"$DAYS" \
        -print -delete | wc -l)
fi

echo "$(stamp) OK pruned files_data=$n_data files_seglogs=$n_logs (older than ${DAYS}d) $(du -sh "$STATE_ROOT/run/UA/data" 2>/dev/null | cut -f1) remain"
