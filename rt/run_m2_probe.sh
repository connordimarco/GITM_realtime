#!/usr/bin/env bash
# M2 driver probe: can GITM initialize + run 1 sim-minute at TODAY's date with
# the midl_live profile, for each placeholder aurora mode?
#
#   hpi_const  'hpi' auroral model + synthesized constant-HP NOAA HPI file
#   fta_const  'FTA' auroral model + synthesized constant-AE SME file
#
# Each probe: fresh state root, cold init at (now - 1 h, rounded to :00),
# one 60 s segment on the live MIDL-RT IMF. Also surfaces any Apex/IGRF
# epoch complaints at 2026 dates. Cheap: ~25 s of 8 niced ranks per mode.
set -u

# Smoke test: probes both aurora modes at today's date on live IMF.
RT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$RT_DIR/rt_config.sh"

START=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:00)
FAILED=0

for MODE in hpi_const fta_const; do
    ROOT="$STATE_ROOT/m2_probe_$MODE"
    rm -rf "$ROOT"
    echo "=== probe $MODE (start $START, state $ROOT)"
    GITM_RT_STATE_ROOT="$ROOT" GITM_RT_PROFILE=midl_live \
        GITM_RT_AURORA_MODE="$MODE" \
        python3 "$RT_DIR/segment.py" init --start "$START" --force
    GITM_RT_STATE_ROOT="$ROOT" GITM_RT_PROFILE=midl_live \
        GITM_RT_AURORA_MODE="$MODE" \
        python3 "$RT_DIR/segment.py" advance --seconds 60
    rc=$?
    echo "=== probe $MODE rc=$rc"
    if [ $rc -ne 0 ]; then
        FAILED=1
        echo "--- last runlog lines:"
        tail -15 "$ROOT"/logs/segments/*.runlog 2>/dev/null | tail -15
    fi
    grep -iE "apex|igrf|warn" "$ROOT"/logs/segments/*.runlog 2>/dev/null \
        | sort -u | head -5 || true
done
exit $FAILED
