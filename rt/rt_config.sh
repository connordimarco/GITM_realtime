# GITM-RT host configuration — the ONLY file that should need editing when
# moving between hosts (solsticedisk prototype -> SWORD disk production).
# Sourced by the bash wrappers; parsed as KEY=VALUE by segment.py/drivers.py,
# so keep it to plain assignments (no logic).

GITM_ROOT=/data/Gitm/cdimarco/GITM_realtime
STATE_ROOT=/data/Gitm/cdimarco/gitm_rt_state

# Toolchain
MPI_BIN=/usr/lib64/openmpi/bin
# NOTE (SWORD): system python3's numpy is broken; harness is stdlib-only so
# this is fine for the service, but point PY at a numpy-capable env before
# running dev/tests/compare_states.py.
PY=/usr/bin/python3
TMPDIR_OVERRIDE=/data/Gitm/cdimarco/tmp

# Resources (shared-box courtesy: nice everything, modest ranks)
NRANKS=8
NICE=10

# Grid: blocks of 9x9x50 cells (compiled Config.pl -g=9,9,50,4).
# 4x8 blocks = 36x72 cells = 10 deg lon x 2.5 deg lat (SWORD PoC choice,
# 2026-07-28: ~14% duty at 8 ranks; 10x1.25 also fits 8 ranks at ~37%
# duty but needs a -g=9,9,50,8 rebuild — noted as the upgrade path).
# Constraint: NBLK_LON*NBLK_LAT <= 4*NRANKS.
NBLK_LON=4
NBLK_LAT=8

# Segment loop
# Pacing is DATA-DRIVEN: a segment runs as soon as the observation frontier
# admits it (advance returns "waiting" otherwise, and the loop polls every
# 60 s). Lag behind wall clock is therefore an outcome — feed latency
# (~2-7 min) plus up to one 5-min segment quantum — not a control.
# LAG_TARGET_SECONDS is only the status/watchdog alert threshold.
SEGMENT_SECONDS=300
LAG_TARGET_SECONDS=900

# Drivers
# The IMF is resolved through MIDL-RT's manifest -> generation-stamped
# payload (plain names like IMF_14Re.dat are transient locally and absent
# remotely). IMF_MANIFEST may be a local path (solsticedisk: MIDL-RT
# staging) or an http(s) URL (SWORD disk: the herot-published copy, e.g.
# https://csem.engin.umich.edu/MIDL/realtime/realtime_manifest.json).
# IMF_PAYLOAD picks the boundary product: imf_14re (bow-shock-nose / OMNI
# convention — correct for GITM's Weimer driver) or imf_32re.
IMF_MANIFEST=/data/Gitm/cdimarco/MIDL_Realtime/realtime_manifest.json
IMF_PAYLOAD=imf_14re
PROFILE=midl_live
# midl_live aurora placeholder until the M3 aurora decision:
#   hpi_const  -> 'hpi' auroral model + synthesized constant-HP NOAA HPI file
#   fta_const  -> 'FTA' auroral model + synthesized constant-AE SME file
#   hpi_live   -> 'hpi' + SWPC OVATION hemispheric-power nowcast (5-min,
#                 per-hemisphere), cache-then-constant fallback (M3 v1,
#                 2026-07-28)
AURORA_MODE=hpi_live
HP_CONST_GW=20.0
AE_CONST_NT=200.0
# F107_MODE=live fetches daily F10.7 + 81-day mean from SWPC daily solar
# indices (6 h cache in $STATE_ROOT/driver_cache); F107/F107A below are
# the const values AND the fallback if SWPC is unreachable with a cold
# cache.
F107_MODE=live
F107=140.0
F107A=140.0

# UAM.in knobs
DT_RESTART=86400.0
DT_PLOT=300.0

# Products (rt/products.py, hooked in gitm_rt_tick.sh after each OK
# segment, non-fatal): merges pending 3DALL fragments (deleting them —
# the merged .bin stays, ~52 MB vs ~96 MB) and renders VTEC map + JSON to
# $STATE_ROOT/products/. PRODUCTS_PY must be a numpy/matplotlib python —
# NOT system python3 (its numpy is broken on SWORD).
PRODUCTS_ENABLE=1
PRODUCTS_PY=/data/Gitm/cdimarco/venv/bin/python
# If set and the directory exists, products.py also copies tec_latest.*
# there (atomic) — the SWORD-Web working copy's live-data dir. How these
# files reach the public web host is the open M4 publishing question.
PRODUCTS_WEB_DIR=/data/Gitm/cdimarco/SWORD-Web/data/gitm

# Retention: rt/prune_state.sh (daily midnight cron) deletes accumulated
# model output (3DALL fragments, ~28 GB/day at 4x8) and per-segment logs
# older than this many days. Never touches restarts/state.json.
RETENTION_DAYS=2
