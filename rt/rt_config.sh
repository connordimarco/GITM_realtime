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
NRANKS=16
NICE=10

# Grid: blocks of 18x18x50 cells, one block per rank (compiled
# Config.pl -g=18,18,50,1 on 2026-09-02 after Aaron B: Earth needs
# nBlocksMax=1). 2x8 blocks = 36x144 cells = 10 deg lon x 1.25 deg lat
# on 16 ranks. Same binary serves 10x2.5 (2x4, 8 ranks), 5x2.5 (4x4, 16)
# and 5x1.25 (4x8, 32). Constraint: NBLK_LON*NBLK_LAT == NRANKS.
# Previous (2026-07-28 to 09-02): 9x9x50 blocks, nBlocksMax=4, 4x8 on 8
# ranks = 10x2.5 deg. Notes below are from that era:
# 4x8 blocks = 36x72 cells = 10 deg lon x 2.5 deg lat (SWORD PoC choice,
# 2026-07-28: ~14% duty at 8 ranks; 10x1.25 also fits 8 ranks at ~37%
# duty but needs a -g=9,9,50,8 rebuild — noted as the upgrade path).
# Constraint: NBLK_LON*NBLK_LAT <= 4*NRANKS.
NBLK_LON=2
NBLK_LAT=8

# Segment loop
# Pacing is DATA-DRIVEN: a segment runs as soon as the observation frontier
# admits it (advance returns "waiting" otherwise, and the loop polls every
# 60 s). Lag behind wall clock is therefore an outcome — feed latency
# (~2-7 min) plus up to one 5-min segment quantum — not a control.
# LAG_TARGET_SECONDS is only the status/watchdog alert threshold.
SEGMENT_SECONDS=600
LAG_TARGET_SECONDS=1500

# Drivers
# The IMF is resolved through MIDL-RT's manifest -> generation-stamped
# payload (plain names like IMF_14Re.dat are transient locally and absent
# remotely). IMF_MANIFEST may be a local path (solsticedisk: MIDL-RT
# staging) or an http(s) URL (SWORD disk: the herot-published copy, e.g.
# https://csem.engin.umich.edu/MIDL/realtime/realtime_manifest.json).
# IMF_PAYLOAD picks the boundary product: imf_14re (bow-shock-nose / OMNI
# convention — correct for GITM's Weimer driver) or imf_32re.
IMF_MANIFEST=/data/Gitm/cdimarco/LAUREN/realtime-midl/realtime_manifest.json
IMF_PAYLOAD=imf_14re
PROFILE=midl_live
# midl_live aurora modes:
#   hpi_const  -> 'hpi' auroral model + synthesized constant-HP NOAA HPI file
#   fta_const  -> 'FTA' auroral model + synthesized constant-AE SME file
#   hpi_live   -> 'hpi' + SWPC OVATION hemispheric-power nowcast (5-min,
#                 per-hemisphere), cache-then-constant fallback (M3 v1,
#                 2026-07-28)
#   ovation    -> OVATION Prime in-model (ext/Electrodynamics), driven by
#                 the staged IMF via #MHD_INDICES — no aurora file, no
#                 SWPC dependency; full 2D precipitation pattern from the
#                 MIDL-RT merge (M3 v2, Aaron's recommendation, 2026-08-05;
#                 probed: 38 s warm 5-min segment, HP ~8 GW quiet).
#   fta_live   -> 'FTA' + realtime AL/AU, best source first (PRODUCTION
#                 since 2026-08-10):
#                 1. the LAUREN corrector product pushed from the
#                    solsticedisk (LAUREN/realtime-aual/aual.dat, nowcast
#                    + ~1 h forecast), resampled to the 60 s SME grid;
#                 2. rt/sme_live.py + rt/sme_ridge.json — pseudo-AL/AU
#                    from 13 INTERMAGNET stations, Kyoto-calibrated,
#                    ridge-bridged 06-10/19-21 UT (~2-5 s per tick,
#                    baseline history in driver_cache/sme_hist.json;
#                    validated 2026-08: r~0.8 vs Kyoto AL, FTA(this) vs
#                    FTA(true) ~1% high-lat TEC in a 3-run hindcast);
#                 3. previous rt_sme.dat if it still covers the segment;
#                 4. constant AE. Never blocks the chain.
AURORA_MODE=fta_live
HP_CONST_GW=20.0
AE_CONST_NT=200.0
# F107_MODE=live reads daily F10.7 + 81-day mean from the product pushed
# by the solsticedisk (LAUREN/realtime-f107/f107.json — F107_LOCAL in
# drivers.py; no SWPC fetch or fallback by design). F107/F107A below are
# the const values AND the fallback if the product is unreadable or >2
# days stale.
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
# If set and the directory exists, products.py mirrors the interactive
# frames there (atomic) — the SWORD-Web working copy's live-data dir,
# pulled from there by the solsticedisk for web publishing.
PRODUCTS_WEB_DIR=/data/Gitm/cdimarco/SWORD-Web/data/gitm

# Retention: rt/prune_state.sh (daily midnight cron) deletes accumulated
# model output (3DALL fragments, ~28 GB/day at 4x8) and per-segment logs
# older than this many days. Never touches restarts/state.json.
RETENTION_DAYS=2
