# GITM-RT

GITM run as a realtime service: a chain of 5-minute restart segments
driven by MIDL's multi-spacecraft solar-wind merge (14 Re, OMNI
convention), advancing only through settled data (the observation-frontier
gate in `drivers.py`).

`gitm_rt_tick.sh` advances the chain (cron, every minute).
`rt_config.sh` is the only file to edit per host.
`run_m2_probe.sh` is a test.
`products.py` renders the public products after each segment.
