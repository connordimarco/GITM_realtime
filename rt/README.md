# GITM-RT

GITM run as a realtime service: a chain of 10-minute restart segments
driven by MIDL's multi-spacecraft solar-wind merge (14 Re, OMNI
convention), advancing only through settled data (the observation-frontier
gate in `drivers.py`).

Since 2026-08-10 the external drivers arrive as pushes from the
solsticedisk into `/data/Gitm/cdimarco/LAUREN/` (storage only, no code):
the stamped MIDL IMF generations + manifest (`realtime-midl/`), daily
F10.7 (`realtime-f107/f107.json`),
and the LAUREN corrector's AU/AL nowcast + ~1 h forecast
(`realtime-aual/aual.dat`).

`gitm_rt_tick.sh` advances the chain (cron, every minute).
`rt_config.sh` is the only file to edit per host.
`products.py` renders the public products after each segment.
`prune_state.sh` (cron, daily midnight) enforces the 2-day retention on
model output and per-segment logs.
`sme_live.py` + `sme_ridge.json` build a realtime pseudo-AL/AU driver
from 13 INTERMAGNET ground magnetometer stations. Production is
`AURORA_MODE=fta_live` (since 2026-08-10), which prefers the LAUREN
corrector AU/AL product and keeps `sme_live` as the standby; the full
fallback hierarchy in `drivers.py` is corrector -> sme_live -> previous
rt_sme.dat -> constant AE, and it never blocks the chain.
