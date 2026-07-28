#!/usr/bin/env python3
"""GITM-RT product step: merge fresh 3DALL snapshots and render products.

Runs after each successful segment (hooked in gitm_rt_tick.sh, non-fatal,
under PRODUCTS_PY — the numpy/matplotlib venv, NOT system python3).

- Merges every pending fragment set in $STATE_ROOT/run/UA/data via
  srcPython/pGITM (fragments+header are DELETED after a successful merge;
  the merged .bin stays and is retention-pruned by prune_state.sh).
- Skips very fresh headers (mtime < SETTLE_S) so a concurrently-writing
  GITM (catch-up overlap) is never half-read.
- Renders the newest snapshot to $STATE_ROOT/products/:
    tec_latest.png (atomic replace) + tec_<UTC>.png + tec_latest.json

Config comes from rt/rt_config.sh (KEY=VALUE, env GITM_RT_<KEY> overrides),
same contract as segment.py.
"""
import json
import os
import sys
import time

RT_DIR = os.path.dirname(os.path.abspath(__file__))
GITM_ROOT = os.path.dirname(RT_DIR)
SETTLE_S = 15

# Interactive-product frames: reduced grids shipped as JSON for the
# Plotly page (time scrubbing + variable/altitude selection client-side).
FRAME_ALT_TARGETS_KM = [120, 200, 300, 400, 550]
FRAMES_KEEP_HOURS = 24


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


def merge_pending(data_dir):
    """pGITM every settled header; returns list of merged .bin paths."""
    sys.path.insert(0, os.path.join(GITM_ROOT, "srcPython"))
    import pGITM

    now = time.time()
    pending = [
        h for h in sorted(os.listdir(data_dir))
        if h.endswith(".header")
        and now - os.path.getmtime(os.path.join(data_dir, h)) > SETTLE_S
    ]
    merged = []
    cwd = os.getcwd()
    for h in pending:
        try:
            os.chdir(data_dir)
            pGITM.process_one_file(h)
            pGITM.remove_files(h)
            merged.append(os.path.join(data_dir, h.replace(".header", ".bin")))
        except Exception as e:  # one bad snapshot must not kill the step
            print(f"WARN merge failed for {h}: {e}")
        finally:
            os.chdir(cwd)
    return merged


def plot_newest(data_dir, out_dir):
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from gitm_routines import read_gitm_one_file

    bins = sorted(
        f for f in os.listdir(data_dir)
        if f.startswith("3DALL") and f.endswith(".bin")
    )
    if not bins:
        print("WAIT no merged snapshots yet")
        return
    newest = os.path.join(data_dir, bins[-1])

    from mpl_toolkits.basemap import Basemap
    from matplotlib.gridspec import GridSpec

    d = read_gitm_one_file(newest, vars_to_read=[0, 1, 2, 34])
    lon = np.degrees(d[0][2:-2, 2, 2])
    lat = np.degrees(d[1][2, 2:-2, 2])
    alt = d[2][2:-2, 2:-2, 2:-2]
    ne = d[34][2:-2, 2:-2, 2:-2]
    vtec = np.trapezoid(ne, alt, axis=2) / 1e16  # TECU
    ut = d["time"]
    # Wrap one column on each side so contourf closes the 0/360 seam
    # (otherwise the polar dials show a missing pie slice).
    z = vtec.T
    z = np.hstack([z[:, -1:], z, z[:, :1]])
    lon = np.r_[lon[-1] - 360.0, lon, lon[0] + 360.0]

    # Layout after Connor's original GITM plotter: global Miller panel below,
    # north/south polar dials above — geographic coordinates rotated so the
    # sub-solar longitude sits at 12 o'clock (noon up), which lets real
    # coastlines be drawn on the dials too.
    norm = plt.Normalize(vmin=0.0, vmax=max(float(vtec.max()) * 1.05, 1.0))
    sslon = (180.0 - (ut.hour + ut.minute / 60.0) * 15.0) % 360.0

    fig = plt.figure(figsize=(10, 8), dpi=110, tight_layout=True)
    fig.suptitle(f"GITM Real-Time — Vertical TEC — {ut:%Y-%m-%d %H:%M} UT",
                 weight="bold", size=14)
    gs = GridSpec(2, 2, figure=fig, height_ratios=[1, 1.15])

    axn = fig.add_subplot(gs[0, 0])
    mn = Basemap(projection="npstere", boundinglat=50, lon_0=sslon + 180.0,
                 resolution="c", round=True, ax=axn)
    xn, yn = mn(*np.meshgrid(lon, lat))
    mn.contourf(xn, yn, z, levels=60, cmap="jet", norm=norm)
    mn.drawcoastlines(linewidth=0.4)
    mn.drawparallels(np.arange(50.0, 90.0, 10.0))
    axn.set_title("North (noon up)", size=10)

    axs = fig.add_subplot(gs[0, 1])
    ms = Basemap(projection="spstere", boundinglat=-50.001, lon_0=sslon + 180.0,
                 resolution="c", round=True, ax=axs)
    xs, ys = ms(*np.meshgrid(lon, lat))
    ms.contourf(xs, ys, z, levels=60, cmap="jet", norm=norm)
    ms.drawcoastlines(linewidth=0.4)
    ms.drawparallels(np.arange(-80.0, -40.0, 10.0))
    axs.set_title("South (noon up)", size=10)

    axg = fig.add_subplot(gs[1, :])
    mg = Basemap(projection="mill", llcrnrlon=0, urcrnrlon=360,
                 llcrnrlat=-80, urcrnrlat=80, resolution="c", ax=axg)
    xg, yg = mg(*np.meshgrid(lon, lat))
    cs = mg.contourf(xg, yg, z, levels=60, cmap="jet", norm=norm)
    mg.drawcoastlines(linewidth=0.5)
    mg.drawparallels(np.arange(-60.0, 90.0, 30.0), labels=[1, 0, 0, 0])
    mg.drawmeridians(np.arange(0.0, 360.0, 60.0), labels=[0, 0, 0, 1])
    decl = -23.44 * np.cos(np.radians(360.0 / 365 * (ut.timetuple().tm_yday + 10)))
    xss, yss = mg(sslon, decl)
    axg.plot(xss, yss, "w*", ms=14, mec="k")
    cbar = mg.colorbar(cs, location="right", pad="2%")
    cbar.set_label("Vertical TEC [TECU]")

    os.makedirs(out_dir, exist_ok=True)
    stamped = os.path.join(out_dir, f"tec_{ut:%Y%m%dT%H%M}.png")
    fig.savefig(stamped)
    plt.close(fig)
    tmp = os.path.join(out_dir, ".tec_latest.png.tmp")
    with open(stamped, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    os.replace(tmp, os.path.join(out_dir, "tec_latest.png"))  # atomic

    meta = {"time_utc": ut.strftime("%Y-%m-%dT%H:%M:%S"),
            "vtec_min": round(float(vtec.min()), 2),
            "vtec_max": round(float(vtec.max()), 2),
            "vtec_mean": round(float(vtec.mean()), 2),
            "file": os.path.basename(stamped)}
    tmpj = os.path.join(out_dir, ".tec_latest.json.tmp")
    with open(tmpj, "w") as f:
        json.dump(meta, f)
    os.replace(tmpj, os.path.join(out_dir, "tec_latest.json"))
    print(f"OK {ut:%Y-%m-%dT%H:%M} vtec {meta['vtec_min']}-{meta['vtec_max']} TECU -> {stamped}")


def _sig(v):
    return float("%.4g" % v)


def _grid2(a):  # 2D lon×lat array -> rounded [lat][lon] rows
    return [[_sig(v) for v in row] for row in a.T]


def write_frames(data_dir, frames_dir):
    """One JSON frame per merged snapshot (whole-minute times only).

    Frame schema: {time, lon[], lat[], alts_km[], fields{key:{label,units,
    alt:bool, data}}} where data is [lat][lon] (2D fields) or
    [alt][lat][lon] (altitude-resolved fields).
    """
    import numpy as np
    from gitm_routines import read_gitm_one_file

    os.makedirs(frames_dir, exist_ok=True)
    made = []
    for f in sorted(os.listdir(data_dir)):
        if not (f.startswith("3DALL") and f.endswith(".bin")):
            continue
        if not f[:-4].endswith("00"):  # skip odd-second segment-start dups
            continue
        stamp = f[7:-4].replace("_", "T")  # 260728T052000
        out = os.path.join(frames_dir, f"frame_20{stamp}.json")
        if os.path.exists(out):
            continue

        # var indices in 3DALL: 3 Rho, 15 Tn, 16/17 Vn(e/n), 34 [e-],
        # 35 Te, 36 Ti, 37/38 Vi(e/n)
        d = read_gitm_one_file(os.path.join(data_dir, f),
                               vars_to_read=[0, 1, 2, 3, 15, 16, 17, 34, 35, 36, 37, 38])
        lon = np.degrees(d[0][2:-2, 2, 2])
        lat = np.degrees(d[1][2, 2:-2, 2])
        alt_m = d[2][2:-2, 2:-2, 2:-2]
        alt_km = d[2][2, 2, 2:-2] / 1000.0
        ne = d[34][2:-2, 2:-2, 2:-2]
        vtec = np.trapezoid(ne, alt_m, axis=2) / 1e16
        imax = ne.argmax(axis=2)
        nmf2 = np.take_along_axis(ne, imax[:, :, None], axis=2)[:, :, 0]
        hmf2 = np.take_along_axis(alt_m, imax[:, :, None], axis=2)[:, :, 0] / 1000.0

        ialts = sorted({int(np.abs(alt_km - t).argmin())
                        for t in FRAME_ALT_TARGETS_KM})

        def slab(idx):
            a = d[idx][2:-2, 2:-2, 2:-2]
            return [_grid2(a[:, :, i]) for i in ialts]

        frame = {
            "time": d["time"].strftime("%Y-%m-%dT%H:%M:%S"),
            "lon": [round(float(v), 2) for v in lon],
            "lat": [round(float(v), 2) for v in lat],
            "alts_km": [int(round(float(alt_km[i]))) for i in ialts],
            "fields": {
                "vtec": {"label": "Vertical TEC", "units": "TECU",
                         "alt": False, "data": _grid2(vtec)},
                "nmf2": {"label": "NmF2 (peak e⁻ density)", "units": "m⁻³",
                         "alt": False, "data": _grid2(nmf2)},
                "hmf2": {"label": "hmF2 (peak height)", "units": "km",
                         "alt": False, "data": _grid2(hmf2)},
                "ne": {"label": "Electron density", "units": "m⁻³",
                       "alt": True, "data": slab(34)},
                "tn": {"label": "Neutral temperature", "units": "K",
                       "alt": True, "data": slab(15)},
                "te": {"label": "Electron temperature", "units": "K",
                       "alt": True, "data": slab(35)},
                "ti": {"label": "Ion temperature", "units": "K",
                       "alt": True, "data": slab(36)},
                "rho": {"label": "Neutral mass density", "units": "kg/m³",
                        "alt": True, "data": slab(3)},
                "vn_east": {"label": "Neutral wind (east)", "units": "m/s",
                            "alt": True, "data": slab(16)},
                "vn_north": {"label": "Neutral wind (north)", "units": "m/s",
                             "alt": True, "data": slab(17)},
                "vi_east": {"label": "Ion drift (east)", "units": "m/s",
                            "alt": True, "data": slab(37)},
                "vi_north": {"label": "Ion drift (north)", "units": "m/s",
                             "alt": True, "data": slab(38)},
            },
        }
        tmp = out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(frame, fh, separators=(",", ":"))
        os.replace(tmp, out)
        made.append(os.path.basename(out))

    # Prune beyond the rolling window, then rewrite the index (atomic,
    # index last so it never references a missing frame).
    import datetime
    names = sorted(f for f in os.listdir(frames_dir)
                   if f.startswith("frame_") and f.endswith(".json"))
    if names:
        newest = datetime.datetime.strptime(names[-1][6:-5], "%Y%m%dT%H%M%S")
        cutoff = newest - datetime.timedelta(hours=FRAMES_KEEP_HOURS)
        for n in names[:]:
            if datetime.datetime.strptime(n[6:-5], "%Y%m%dT%H%M%S") < cutoff:
                os.remove(os.path.join(frames_dir, n))
                names.remove(n)
    idx = {"cadence_s": 300, "frames": names,
           "ranges": _window_ranges(frames_dir, names)}
    tmp = os.path.join(frames_dir, ".frames.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(idx, fh)
    os.replace(tmp, os.path.join(frames_dir, "frames.json"))
    return made


def _field_ranges(fields):
    out = {}
    for k, fd in fields.items():
        if fd["alt"]:
            out[k] = [[min(min(r) for r in g), max(max(r) for r in g)]
                      for g in fd["data"]]
        else:
            g = fd["data"]
            out[k] = [min(min(r) for r in g), max(max(r) for r in g)]
    return out


def _window_ranges(frames_dir, names):
    """Per-field (and per-altitude) min/max across the whole window, so
    the page can hold the color axis constant while stepping in time.
    Per-frame extrema live in a sidecar store; frames not yet in the
    store (e.g. written before this feature) are scanned once."""
    store_path = os.path.join(frames_dir, "ranges_store.json")
    try:
        with open(store_path) as fh:
            store = json.load(fh)
    except (OSError, ValueError):
        store = {}
    for n in names:
        if n not in store:
            try:
                with open(os.path.join(frames_dir, n)) as fh:
                    store[n] = _field_ranges(json.load(fh)["fields"])
            except (OSError, ValueError, KeyError):
                continue
    store = {n: r for n, r in store.items() if n in names}
    tmp = store_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(store, fh)
    os.replace(tmp, store_path)

    agg = {}
    for r in store.values():
        for k, v in r.items():
            if k not in agg:
                agg[k] = [list(x) for x in v] if isinstance(v[0], list) else list(v)
            elif isinstance(v[0], list):
                for i, (lo, hi) in enumerate(v):
                    agg[k][i][0] = min(agg[k][i][0], lo)
                    agg[k][i][1] = max(agg[k][i][1], hi)
            else:
                agg[k][0] = min(agg[k][0], v[0])
                agg[k][1] = max(agg[k][1], v[1])
    return agg


def mirror_frames(frames_dir, web_dir):
    """Sync the frames dir into the web working copy, index last."""
    dst = os.path.join(web_dir, "frames")
    os.makedirs(dst, exist_ok=True)
    src_names = {f for f in os.listdir(frames_dir) if f.startswith("frame_")}
    dst_names = {f for f in os.listdir(dst) if f.startswith("frame_")}
    for n in sorted(src_names - dst_names):
        tmp = os.path.join(dst, "." + n + ".tmp")
        with open(os.path.join(frames_dir, n), "rb") as s, open(tmp, "wb") as t:
            t.write(s.read())
        os.replace(tmp, os.path.join(dst, n))
    for n in dst_names - src_names:
        os.remove(os.path.join(dst, n))
    tmp = os.path.join(dst, ".frames.json.tmp")
    with open(os.path.join(frames_dir, "frames.json"), "rb") as s, open(tmp, "wb") as t:
        t.write(s.read())
    os.replace(tmp, os.path.join(dst, "frames.json"))


def main():
    t0 = time.time()
    cfg = read_config()
    state_root = cfg["STATE_ROOT"]
    data_dir = os.path.join(state_root, "run", "UA", "data")
    if not os.path.isdir(data_dir):
        print("SKIP no data dir")
        return 0
    merged = merge_pending(data_dir)
    out_dir = os.path.join(state_root, "products")
    plot_newest(data_dir, out_dir)
    frames_dir = os.path.join(out_dir, "frames")
    made = write_frames(data_dir, frames_dir)
    if made:
        print(f"frames +{len(made)} (latest {made[-1]})")

    # Mirror the latest products into the web working copy (non-fatal).
    web_dir = cfg.get("PRODUCTS_WEB_DIR", "")
    if web_dir and os.path.isdir(web_dir):
        for name in ("tec_latest.png", "tec_latest.json"):
            src = os.path.join(out_dir, name)
            if not os.path.exists(src):
                continue
            tmp = os.path.join(web_dir, "." + name + ".tmp")
            with open(src, "rb") as s, open(tmp, "wb") as t:
                t.write(s.read())
            os.replace(tmp, os.path.join(web_dir, name))
        mirror_frames(frames_dir, web_dir)
    print(f"done merged={len(merged)} wall={time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    print(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "products tick")
    sys.exit(main())
