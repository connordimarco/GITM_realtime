#!/usr/bin/env python3
"""Realtime pseudo-AE driver for AURORA_MODE=fta_live. STDLIB ONLY (the
harness runs under system python3; its numpy is broken).

Builds an SME-format file (yyyy mm dd hh mm ss AE AL AU) for GITM's
#SME_INDICES from:
  - AL/AU: envelope of baseline-subtracted H over 13 realtime INTERMAGNET
    stations (BGS GIN "reported" 1-min), Kyoto-calibrated. Validated vs
    Kyoto quicklook AL Jul 2026: r 0.74-0.81 active days.
  - AL in the 06-10 & 19-21 UT windows (nightside over embargoed Canada /
    the Urals): ridge regression on the staged IMF (rt/sme_ridge.json,
    held-out r=0.77) — stations lose the nightside oval there.
  - GITM-run fidelity: FTA(this feed) vs FTA(true AL/AU) differ ~1% RMS
    high-lat TEC (storm max 7%) — 3x smaller than OVATION-vs-FTA.

Fallback hierarchy (never blocks the chain):
  blend -> ridge-only (AU from climatological ratio) -> previous good file
  (if it still covers the segment) -> constant AE 200.

Baselines: per-station quiet diurnal curve = per-minute-of-day MEDIAN over
the last BASELINE_DAYS days of raw H (median resists storm contamination),
smoothed 61 min, wrapped. Daily H arrays cached in
$STATE_ROOT/driver_cache/sme_hist.json (auto-backfilled, ~10 days kept).
"""

import concurrent.futures
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

GIN_URL = "https://imag-data.bgs.ac.uk/GIN_V1/GINServices"
REQ_TIMEOUT_S = 20

# (code, calibration applies to all): observed <=~40 min latency 2026-08-05.
# KIR deliberately EXCLUDED: its GIN H channel is sign-inverted (mirrors
# negative bays into the AU envelope; verified vs ABK/SOD neighbors).
STATIONS = ["BRW", "CMO", "DED", "SHU", "SIT",
            "NUR", "SOD", "ABK", "LYC", "UPS",
            "NGK", "WNG", "ARS"]
CAL_AL = 1.005          # pseudo -> Kyoto scale (fit vs Kyoto AL, Jul 2026)
CAL_AU = 0.946
GAP_HOURS = (6, 7, 8, 9, 19, 20)   # UT hours where the ridge drives AL
STEP_NT = 500.0         # variometer step threshold (nT/min)
MIN_STATIONS = 5        # envelope needs at least this many live stations
BASELINE_DAYS = 7
HIST_KEEP_DAYS = 10
AU_FLOOR = 25.0         # FTA's own AU limiter floor
AU_FALLBACK_RATIO = 0.4  # AU ~ 0.4*|AL| when station AU is unavailable
CONST_AE = 200.0


def _fetch_station(code, start_date, days):
    """1-min H series (list, NaN for missing) starting 00 UT start_date."""
    params = urllib.parse.urlencode({
        "Request": "GetData", "format": "Iaga2002", "testObsys": "0",
        "observatoryIagaCode": code, "samplesPerDay": "1440",
        "publicationState": "adj-or-rep",
        "dataStartDate": start_date.strftime("%Y-%m-%d"),
        "dataDuration": str(days)})
    with urllib.request.urlopen(GIN_URL + "?" + params,
                                timeout=REQ_TIMEOUT_S) as r:
        body = r.read().decode("utf-8", "replace")
    h = [float("nan")] * (1440 * days)
    comps = None
    day0 = start_date.date()
    for line in body.splitlines():
        parts = line.split()
        if line.startswith("DATE"):
            comps = [c[-1] for c in parts[3:]]
            continue
        if comps is None or len(parts) < 7 or not parts[0][:2].isdigit():
            continue
        try:
            d = datetime.strptime(parts[0], "%Y-%m-%d").date()
            hh, mm = int(parts[1][:2]), int(parts[1][3:5])
            vals = {c: float(v) for c, v in zip(comps, parts[3:])}
        except ValueError:
            continue
        good = {c: v for c, v in vals.items() if abs(v) < 88887.0}
        if "H" in good:
            hv = good["H"]
        elif "X" in good and "Y" in good:
            hv = math.hypot(good["X"], good["Y"])
        else:
            continue
        idx = (d - day0).days * 1440 + hh * 60 + mm
        if 0 <= idx < len(h):
            h[idx] = hv
    return h


def _level_steps(h):
    """Remove variometer step artifacts by zeroing implausible 1-min jumps."""
    out = list(h)
    last = None
    offset = 0.0
    for i, v in enumerate(out):
        if not math.isfinite(v):
            continue
        if last is not None and abs(v - last) > STEP_NT:
            offset += v - last
        last = v
        out[i] = v - offset
    return out


def _smooth_wrap(x, w=61):
    n = len(x)
    half = w // 2
    out = [0.0] * n
    for i in range(n):
        s = 0.0
        for j in range(i - half, i + half + 1):
            s += x[j % n]
        out[i] = s / w
    return out


class HistCache(object):
    """Per-station daily 1-min H arrays, JSON on disk."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (IOError, ValueError):
            self.data = {}

    def get(self, code, day):
        v = self.data.get(code, {}).get(day)
        if v is None:
            return None
        return [float("nan") if x is None else x for x in v]

    def put(self, code, day, series):
        self.data.setdefault(code, {})[day] = [
            None if not math.isfinite(v) else round(v, 2) for v in series]

    def prune(self, keep_days):
        cutoff = (datetime.utcnow() - timedelta(days=keep_days)
                  ).strftime("%Y-%m-%d")
        for code in self.data:
            self.data[code] = {d: v for d, v in self.data[code].items()
                               if d >= cutoff}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)


def _baseline(hist, code, today):
    """Quiet diurnal curve: per-minute median over cached history days."""
    days = []
    for k in range(1, HIST_KEEP_DAYS + 1):
        day = (today - timedelta(days=k)).strftime("%Y-%m-%d")
        s = hist.get(code, day)
        if s is not None:
            days.append(s)
        if len(days) >= BASELINE_DAYS:
            break
    if len(days) < 3:
        return None
    curve = []
    for m in range(1440):
        vals = sorted(v[m] for v in days if math.isfinite(v[m]))
        curve.append(vals[len(vals) // 2] if vals else float("nan"))
    fin = [i for i, v in enumerate(curve) if math.isfinite(v)]
    if len(fin) < 720:
        return None
    for i in range(1440):           # wrap-interpolate gaps
        if not math.isfinite(curve[i]):
            lo = max(j for j in fin)
            prev = max((j for j in fin if j < i), default=lo - 1440)
            nxt = min((j for j in fin if j > i), default=min(fin) + 1440)
            a, b = curve[prev % 1440], curve[nxt % 1440]
            curve[i] = a + (b - a) * (i - prev) / max(nxt - prev, 1)
    return _smooth_wrap(curve)


def _ridge_al(imf_path, t0, t1):
    """Ridge AL prediction per minute over [t0, t1] from the staged IMF."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "sme_ridge.json")) as f:
        M = json.load(f)
    rows = []       # (datetime, by, bz, v, n)
    started = False
    for line in open(imf_path):
        if line.startswith("#START"):
            started = True
            continue
        if not started:
            continue
        p = line.split()
        if len(p) < 15:
            continue
        try:
            d = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]),
                         int(p[4]), int(p[5]))
            by, bz = float(p[8]), float(p[9])
            v = math.sqrt(float(p[10])**2 + float(p[11])**2 + float(p[12])**2)
            n = float(p[13])
        except ValueError:
            continue
        rows.append((d, by, bz, v, n))
    if not rows:
        return {}
    # 5-min means from 3.5 h before t0 (trailing-mean features need history)
    g0 = t0 - timedelta(hours=3, minutes=30)
    nbins = int((t1 - g0).total_seconds() // 300) + 1
    acc = [[0.0, 0.0, 0.0, 0.0, 0] for _ in range(nbins)]
    for d, by, bz, v, n in rows:
        i = int((d - g0).total_seconds() // 300)
        if 0 <= i < nbins:
            a = acc[i]
            a[0] += by; a[1] += bz; a[2] += v; a[3] += n; a[4] += 1
    feats, newells, vbss = [], [], []
    preds5 = [float("nan")] * nbins
    for i, a in enumerate(acc):
        if a[4] == 0:
            newells.append(float("nan")); vbss.append(float("nan"))
            feats.append(None)
            continue
        by, bz, v, n = a[0]/a[4], a[1]/a[4], a[2]/a[4], a[3]/a[4]
        bt = math.hypot(by, bz)
        theta = math.atan2(abs(by), bz)
        newell = v**(4.0/3) * bt**(2.0/3) * abs(math.sin(theta/2))**(8.0/3)
        vbs = v * max(0.0, -bz)
        newells.append(newell); vbss.append(vbs)
        feats.append([newell, math.sqrt(max(newell, 0.0)), vbs, v, n, bz])
    def tmean(series, i, w):
        vals = [x for x in series[max(0, i-w+1):i+1] if math.isfinite(x)]
        return sum(vals)/len(vals) if len(vals) >= w*0.5 else float("nan")
    for i in range(nbins):
        if feats[i] is None:
            continue
        x = list(feats[i])
        for w in (3, 12, 36):
            x.append(tmean(newells, i, w))
            x.append(tmean(vbss, i, w))
        if any(not math.isfinite(v) for v in x):
            continue
        s = M["b"]
        for k in range(12):
            s += M["w"][k] * (x[k] - M["mu"][k]) / M["sd"][k]
        preds5[i] = s
    out = {}
    t = t0
    while t <= t1:
        i = int((t - g0).total_seconds() // 300)
        if 0 <= i < nbins and math.isfinite(preds5[i]):
            out[t] = preds5[i]
        t += timedelta(minutes=1)
    return out


def _interp_nan(x):
    fin = [i for i, v in enumerate(x) if math.isfinite(v)]
    if len(fin) < 2:
        return x
    out = list(x)
    for i in range(len(x)):
        if math.isfinite(out[i]):
            continue
        prev = max((j for j in fin if j < i), default=None)
        nxt = min((j for j in fin if j > i), default=None)
        if prev is None:
            out[i] = x[nxt]
        elif nxt is None:
            out[i] = x[prev]
        else:
            out[i] = x[prev] + (x[nxt] - x[prev]) * (i - prev) / (nxt - prev)
    return out


def build_sme(dest, t_start, t_end, cache_dir):
    """Write an SME file covering [t_start, t_end]. Returns a status string.
    Raises nothing fatal by itself — callers still guard (network etc.)."""
    now = datetime.utcnow()
    day0 = datetime(t_start.year, t_start.month, t_start.day) \
        - timedelta(days=1)
    ndays = (datetime(t_end.year, t_end.month, t_end.day)
             - day0).days + 1

    hist = HistCache(os.path.join(cache_dir, "sme_hist.json"))

    # fetch current window (+ backfill baseline history on the same request
    # when the cache is cold: one wide fetch per station)
    need_backfill = any(
        _count_hist_days(hist, c, now) < 3 for c in STATIONS)
    fetch_day0 = day0 - timedelta(days=BASELINE_DAYS + 1) if need_backfill \
        else day0
    fetch_days = (datetime(t_end.year, t_end.month, t_end.day)
                  - fetch_day0).days + 1

    series = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_station, c, fetch_day0, fetch_days): c
                for c in STATIONS}
        for fut in concurrent.futures.as_completed(futs):
            c = futs[fut]
            try:
                series[c] = _level_steps(fut.result())
            except Exception:
                series[c] = None

    # archive complete past days into the history cache
    for c, s in series.items():
        if s is None:
            continue
        for k in range(fetch_days):
            d = fetch_day0 + timedelta(days=k)
            if d.date() >= now.date():    # today is incomplete
                continue
            day_slice = s[k * 1440:(k + 1) * 1440]
            if sum(1 for v in day_slice if math.isfinite(v)) > 720:
                hist.put(c, d.strftime("%Y-%m-%d"), day_slice)
    hist.prune(HIST_KEEP_DAYS)
    hist.save()

    # station envelopes on the [day0, end] grid
    nmin = ndays * 1440
    off = (day0 - fetch_day0).days * 1440
    pAL = [float("nan")] * nmin
    pAU = [float("nan")] * nmin
    per_station = []
    for c, s in series.items():
        if s is None:
            continue
        b = _baseline(hist, c, now)
        if b is None:
            continue
        per_station.append((c, s, b))
    for m in range(nmin):
        vals = []
        for c, s, b in per_station:
            v = s[off + m]
            if math.isfinite(v):
                vals.append(v - b[m % 1440])
        if len(vals) >= MIN_STATIONS:
            pAL[m] = min(vals) * CAL_AL
            pAU[m] = max(vals) * CAL_AU

    # ridge AL over the window (from the staged IMF next to dest)
    imf_path = os.path.join(os.path.dirname(dest), "rt_imf.dat")
    try:
        model_al = _ridge_al(imf_path, day0, day0 + timedelta(minutes=nmin))
    except Exception:
        model_al = {}

    have_station = sum(1 for v in pAL if math.isfinite(v))
    pAU = _interp_nan(pAU)

    rows = 0
    used_model = 0
    with open(dest + ".tmp", "w") as f:
        f.write("File created by GITM-RT sme_live (pseudo AL/AU, "
                "Kyoto-calibrated; see rt/sme_live.py)\n\n")
        f.write("=" * 60 + "\n")
        f.write("<year>  <month>  <day>  <hour>  <min>  <sec>  "
                "<SME (nT)>  <SML (nT)>  <SMU (nT)>\n")
        for m in range(nmin):
            t = day0 + timedelta(minutes=m)
            in_gap = t.hour in GAP_HOURS
            mo = model_al.get(t)
            al = None
            if in_gap and mo is not None:
                al = mo
                used_model += 1
            elif math.isfinite(pAL[m]):
                al = pAL[m]
            elif mo is not None:
                al = mo
                used_model += 1
            if al is None:
                continue
            au = pAU[m] if math.isfinite(pAU[m]) \
                else AU_FALLBACK_RATIO * abs(al)
            au = max(au, AU_FLOOR, al + 10.0)
            f.write("%5d %6d %6d %6d %6d %6d %9.1f %9.1f %9.1f\n"
                    % (t.year, t.month, t.day, t.hour, t.minute, 0,
                       au - al, al, au))
            rows += 1
    if rows == 0:
        os.remove(dest + ".tmp")
        raise RuntimeError("sme_live produced no rows")
    os.replace(dest + ".tmp", dest)
    return ("rows=%d stations=%d station_min=%d model_min=%d"
            % (rows, len(per_station), have_station, used_model))


def _count_hist_days(hist, code, now):
    n = 0
    for k in range(1, HIST_KEEP_DAYS + 1):
        if hist.get(code, (now - timedelta(days=k)).strftime("%Y-%m-%d")):
            n += 1
    return n


def coverage_end(path):
    """Last timestamp in an existing SME file (None if unreadable)."""
    last = None
    try:
        for line in open(path):
            p = line.split()
            if len(p) == 9 and p[0].isdigit():
                last = p
    except IOError:
        return None
    if last is None:
        return None
    try:
        return datetime(int(last[0]), int(last[1]), int(last[2]),
                        int(last[3]), int(last[4]), int(last[5]))
    except ValueError:
        return None
