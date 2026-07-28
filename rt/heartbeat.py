#!/usr/bin/env python3
"""Write status.json — the GITM-RT heartbeat.

Called by gitm_rt_tick.sh after EVERY tick (OK, WAIT, or FAIL), so the
file's written_utc advances whenever the tick cron is alive. It lands in
$STATE_ROOT/products/ (and the web mirror), rides the existing
solsticedisk pull, and is what the solsticedisk-side watchdog alerts on.
Stdlib-only, like the rest of the harness. Never raises on missing
inputs — a partial heartbeat is better than none.
"""
import json
import os
import sys
import datetime

RT_DIR = os.path.dirname(os.path.abspath(__file__))


def read_config():
    cfg = {}
    with open(os.path.join(RT_DIR, "rt_config.sh")) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k] = os.environ.get("GITM_RT_" + k, v)
    return cfg


def main():
    tick_rc = int(sys.argv[1]) if len(sys.argv) > 1 else None
    cfg = read_config()
    state_root = cfg["STATE_ROOT"]
    now = datetime.datetime.utcnow()
    hb = {"written_utc": now.strftime("%Y-%m-%dT%H:%M:%S"),
          "tick_rc": tick_rc}

    try:
        with open(os.path.join(state_root, "state.json")) as f:
            st = json.load(f)
        hb.update({k: st.get(k) for k in
                   ("head_utc", "mode", "segments", "last_status", "last_wall_s")})
        head = datetime.datetime.strptime(st["head_utc"], "%Y-%m-%dT%H:%M:%S")
        hb["lag_min"] = round((now - head).total_seconds() / 60.0, 1)
    except Exception as e:
        hb["state_error"] = str(e)

    try:
        frames_dir = os.path.join(state_root, "products", "frames")
        with open(os.path.join(frames_dir, "frames.json")) as f:
            names = json.load(f)["frames"]
        newest = datetime.datetime.strptime(names[-1][6:-5], "%Y%m%dT%H%M%S")
        hb["newest_frame_utc"] = newest.strftime("%Y-%m-%dT%H:%M:%S")
        hb["product_age_min"] = round((now - newest).total_seconds() / 60.0, 1)
    except Exception as e:
        hb["frames_error"] = str(e)

    try:
        mdir = os.path.dirname(cfg["IMF_MANIFEST"])
        with open(cfg["IMF_MANIFEST"]) as f:
            gen = json.load(f)["generation"]
        gent = datetime.datetime.strptime(gen, "%Y%m%dT%H%M%SZ")
        hb["imf_manifest_generation"] = gen
        hb["imf_manifest_age_min"] = round((now - gent).total_seconds() / 60.0, 1)
    except Exception as e:
        hb["imf_error"] = str(e)

    payload = json.dumps(hb, indent=1)
    targets = [os.path.join(state_root, "products", "status.json")]
    web_dir = cfg.get("PRODUCTS_WEB_DIR", "")
    if web_dir and os.path.isdir(web_dir):
        targets.append(os.path.join(web_dir, "status.json"))
    for t in targets:
        os.makedirs(os.path.dirname(t), exist_ok=True)
        tmp = t + ".tmp"
        with open(tmp, "w") as f:
            f.write(payload)
        os.replace(tmp, t)
    print(payload)


if __name__ == "__main__":
    main()
