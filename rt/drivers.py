"""Driver-file staging for GITM-RT segments.

Stdlib only. Each profile stages whatever files it needs into the run
directory and returns (auroral_model, driver_lines) for the UAM.in template.

Profiles:
  example2002 - the in-repo 2002-12-21 example day (M1 fidelity test):
                example IMF + SME + NGDC f107 + FISM. No staging needed;
                files are reached through the run dir's UA/DataIn symlinks.
  midl_live   - MIDL-RT IMF_32Re.dat (local path or URL) + placeholder
                aurora (constant HP or constant AE, until the M3 aurora
                decision) + static F107.
"""

import json
import os
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timedelta


class WaitingForData(Exception):
    """IMF file does not cover the requested segment window yet."""


# ---------------------------------------------------------------- config --

def load_config(path):
    """Parse rt_config.sh KEY=VALUE lines into a dict (env overrides win)."""
    cfg = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, val = line.split('=', 1)
            cfg[key.strip()] = val.strip()
    for key in list(cfg):
        env = os.environ.get('GITM_RT_' + key)
        if env is not None:
            cfg[key] = env
    return cfg


# ------------------------------------------------------------------- IMF --

def _is_url(s):
    return s.startswith('http://') or s.startswith('https://')


def _fetch(source, dest):
    tmp = dest + '.tmp'
    if _is_url(source):
        with urllib.request.urlopen(source, timeout=30) as r, open(tmp, 'wb') as f:
            shutil.copyfileobj(r, f)
    else:
        shutil.copyfile(source, tmp)
    os.replace(tmp, dest)


def resolve_imf(manifest_source, payload_key):
    """MIDL-RT manifest -> sibling path/URL of the stamped IMF payload.

    The manifest is the only stable contract: plain names (IMF_14Re.dat)
    are transient in local staging and never published; the stamped file a
    manifest references is kept for GENERATION_KEEP_MIN, so resolve+fetch
    is race-free.
    """
    if _is_url(manifest_source):
        with urllib.request.urlopen(manifest_source, timeout=30) as r:
            manifest = json.load(r)
        base = manifest_source.rsplit('/', 1)[0]
        join = lambda name: base + '/' + name
    else:
        with open(manifest_source) as f:
            manifest = json.load(f)
        base = os.path.dirname(manifest_source)
        join = lambda name: os.path.join(base, name)
    entry = manifest.get('payloads', {}).get(payload_key)
    if entry is None:
        raise ValueError('manifest at %s has no payload %r (keys: %s)'
                         % (manifest_source, payload_key,
                            sorted(manifest.get('payloads', {}))))
    stamped = join(entry['file'])

    if _is_url(manifest_source):
        return [stamped]

    # Local install: MIDL-RT rewrites a PLAIN-named copy (IMF_14Re.dat)
    # every minute, while the manifest only advances on 5-min publish
    # ticks. Prefer the plain file when it is fresher, so the chain can
    # advance the moment new solar-wind data lands rather than waiting on
    # the manifest (Connor, 2026-07-21: the trigger is data, not pacing).
    # The plain name is pruned briefly around each publish tick — the
    # stamped file stays as fallback (stage_imf tries sources in order).
    import re as _re
    plain = os.path.join(base,
                         _re.sub(r'\.\d{8}T\d{6}Z\.', '.',
                                 entry['file']))
    try:
        if os.path.getmtime(plain) > os.path.getmtime(stamped):
            return [plain, stamped]
    except OSError:
        pass
    return [stamped]


def stage_imf(sources, dest):
    """Copy (or fetch) the SWMF IMF file into the run dir, atomically.

    sources is a preference-ordered list; the first that can be read wins
    (a fresher local plain file can be pruned between resolve and read).
    """
    if isinstance(sources, str):
        sources = [sources]
    last_err = None
    for source in sources:
        try:
            _fetch(source, dest)
            return
        except (OSError, urllib.error.URLError) as e:
            last_err = e
    raise last_err


_FRONTIER_RE = None

def imf_coverage(path):
    """(first, last, frontier) of an SWMF IMF file from MIDL-RT.

    first/last bound the data rows. frontier is the observation frontier
    parsed from the MIDL-RT header ("Rows after <t> (the observation
    frontier) ..."): rows after it are ballistic projections that later
    ticks may rewrite (a faster parcel measured next minute overtakes and
    replaces them), so a chained nowcast segment must never end past it.
    None if the header line is absent (pre-lead-fix files, where the file
    simply ended at the frontier).
    """
    global _FRONTIER_RE
    if _FRONTIER_RE is None:
        import re
        _FRONTIER_RE = re.compile(
            r'Rows after (\d{4}-\d{2}-\d{2}T\d{2}:\d{2})Z '
            r'\(the observation frontier\)')
    first = last = frontier = None
    started = False
    with open(path) as f:
        for line in f:
            if not started:
                m = _FRONTIER_RE.search(line)
                if m:
                    frontier = datetime.strptime(m.group(1),
                                                 '%Y-%m-%dT%H:%M')
                if line.startswith('#START'):
                    started = True
                continue
            parts = line.split()
            if len(parts) < 7:
                continue
            try:
                t = datetime(*[int(x) for x in parts[:6]])
            except ValueError:
                continue
            if first is None:
                first = t
            last = t
    return first, last, frontier


# ----------------------------------------------- live indices (SWPC) -----
# Both fetchers are non-fatal by design: network failure falls back to the
# on-disk cache, and a cold cache falls back to the config constants. A bad
# SWPC day degrades the science, never the chain.

SWPC_DSD_URL = 'https://services.swpc.noaa.gov/text/daily-solar-indices.txt'
SWPC_HP_URL = 'https://services.swpc.noaa.gov/text/aurora-nowcast-hemi-power.txt'


def _fetch_url(url, timeout_s=10):
    import urllib.request
    return urllib.request.urlopen(url, timeout=timeout_s).read().decode(
        'utf-8', 'replace')


def get_live_f107(cache_dir, fallback, fallback_a):
    """(f107, f107a) from SWPC daily solar indices, as strings for UAM.in.

    Keeps a rolling history in cache (the SWPC file only carries ~30 days;
    the cache grows toward a true 81-day window over time). Refetches at
    most every 6 h. Falls back to cached history, then to the config
    constants.
    """
    path = os.path.join(cache_dir, 'f107_history.json')
    hist = {'fetched_utc': '1970-01-01T00:00:00', 'flux': {}}
    try:
        with open(path) as f:
            hist = json.load(f)
    except (OSError, ValueError):
        pass
    now = datetime.utcnow()
    fetched = datetime.strptime(hist['fetched_utc'], '%Y-%m-%dT%H:%M:%S')
    if (now - fetched) > timedelta(hours=6):
        try:
            for line in _fetch_url(SWPC_DSD_URL).splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[0].isdigit() and len(parts[0]) == 4:
                    day = '%s-%s-%s' % (parts[0], parts[1], parts[2])
                    hist['flux'][day] = float(parts[3])
            hist['fetched_utc'] = now.strftime('%Y-%m-%dT%H:%M:%S')
            tmp = path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(hist, f)
            os.replace(tmp, path)
        except Exception as e:
            print('f107 fetch failed (%s); using cache/fallback' % e)
    days = sorted(d for d, v in hist['flux'].items() if v > 0)
    if not days:
        return fallback, fallback_a
    f107 = hist['flux'][days[-1]]
    window = [hist['flux'][d] for d in days[-81:]]
    f107a = sum(window) / len(window)
    return '%.1f' % f107, '%.1f' % f107a


def write_live_hpi(dest, t0, t1, cache_dir, fallback_gw):
    """rt_hpi.txt from SWPC's OVATION hemispheric-power nowcast (5-min,
    per-hemisphere). Edge rows are extended flat to cover [t0, t1] (the
    reader buffers ~1 h; the feed reaches ~now, segments end ≤ frontier).
    Falls back to a ≤2 h-old cached copy, then to the constant."""
    raw = None
    cache = os.path.join(cache_dir, 'hemi_power_last.txt')
    try:
        raw = _fetch_url(SWPC_HP_URL)
        tmp = cache + '.tmp'
        with open(tmp, 'w') as f:
            f.write(raw)
        os.replace(tmp, cache)
    except Exception as e:
        try:
            if (datetime.utcnow() - datetime.utcfromtimestamp(
                    os.path.getmtime(cache))) < timedelta(hours=2):
                with open(cache) as f:
                    raw = f.read()
                print('hpi fetch failed (%s); using cached copy' % e)
        except OSError:
            pass
    rows = []
    if raw is not None:
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 4 and not line.startswith('#'):
                try:
                    t = datetime.strptime(parts[0], '%Y-%m-%d_%H:%M')
                    rows.append((t, float(parts[2]), float(parts[3])))
                except ValueError:
                    continue
    if not rows:
        print('hpi: no live data; writing constant %.1f GW' % fallback_gw)
        write_const_hpi(dest, t0, t1, fallback_gw)
        return 'const'
    rows.sort()
    #

    def clamp(t):
        if t <= rows[0][0]:
            return rows[0]
        if t >= rows[-1][0]:
            return rows[-1]
        return None
    with open(dest, 'w') as f:
        f.write(':Data_list: ' + os.path.basename(dest) + '\n')
        f.write(':Created: {0:%a %b %d %H:%M:%S UTC %Y}\n'.format(
            datetime.utcnow()))
        f.write('# GITM-RT live hemispheric power from SWPC OVATION '
                'nowcast (N/S GW).\n')
        f.write('# 2006-09-05 00:54:25 NOAA-16 (S)  7  29.67   0.82\n')
        f.write('# F7.2  Normalizing factor\n')
        f.write('\n')
        # flat back-extension, real rows in window, flat forward-extension
        t = t0
        while t < rows[0][0]:
            f.write(_HPI_LINE.format(t, 'N', rows[0][1]))
            f.write(_HPI_LINE.format(t, 'S', rows[0][2]))
            t += timedelta(seconds=1800)
        for (t, n, s) in rows:
            if t0 <= t <= t1:
                f.write(_HPI_LINE.format(t, 'N', n))
                f.write(_HPI_LINE.format(t, 'S', s))
        t = rows[-1][0] + timedelta(seconds=1800)
        while t <= t1:
            f.write(_HPI_LINE.format(t, 'N', rows[-1][1]))
            f.write(_HPI_LINE.format(t, 'S', rows[-1][2]))
            t += timedelta(seconds=1800)
    return 'live'


# ------------------------------------------------- placeholder aurora ----

_HPI_LINE = '{0:%Y-%m-%d} {0:%H:%M:%S} NOAA-17 ({1})  6{2:7.2f}   0.75\n'

def write_const_hpi(dest, t0, t1, hp_gw, cadence_s=1800):
    """NOAA-HPI-format file with constant hemispheric power, both hemispheres.

    Format mirrors srcPython/create_fake_noaa_hpi_input.py (which mimics the
    pre-2013 NOAA POES power files GITM's #NOAAHPI_INDICES reader expects).
    """
    with open(dest, 'w') as f:
        f.write(':Data_list: ' + os.path.basename(dest) + '\n')
        f.write(':Created: {0:%a %b %d %H:%M:%S UTC %Y}\n'.format(t0))
        f.write('# GITM-RT placeholder: CONSTANT hemispheric power '
                '(%.1f GW) until a realtime aurora driver is chosen.\n' % hp_gw)
        f.write('# 2006-09-05 00:54:25 NOAA-16 (S)  7  29.67   0.82\n')
        # The Fortran reader (read_NOAAHPI_Indices_new.f90) starts parsing at
        # the line containing 'Normalizing factor' ('F7.2' selects the format)
        # and then discards exactly one line before the data rows.
        f.write('# F7.2  Normalizing factor\n')
        f.write('\n')
        t = t0
        while t <= t1:
            f.write(_HPI_LINE.format(t, 'N', hp_gw))
            f.write(_HPI_LINE.format(t, 'S', hp_gw))
            t += timedelta(seconds=cadence_s)


def write_const_sme(dest, t0, t1, ae_nt, cadence_s=60):
    # 60 s cadence: GITM's SME reader interpolates within a narrow window
    # (~±3 min); a coarser grid can leave too few samples for short
    # segments and fail ieModel init ("SME values could not be set").
    """SME-format file (as srcData/Examples/ae20021221.dat) with constant AE."""
    with open(dest, 'w') as f:
        f.write('File created by GITM-RT (constant-AE placeholder)\n')
        f.write('\n')
        f.write('=' * 60 + '\n')
        f.write('<year>  <month>  <day>  <hour>  <min>  <sec>  '
                '<SME (nT)>  <SML (nT)>  <SMU (nT)>\n')
        sml = -ae_nt / 2.0
        smu = ae_nt / 2.0
        t = t0
        while t <= t1:
            f.write('%4d  %02d  %02d  %02d  %02d  %02d  %8.2f %8.2f %8.2f\n'
                    % (t.year, t.month, t.day, t.hour, t.minute, t.second,
                       ae_nt, sml, smu))
            t += timedelta(seconds=cadence_s)


# -------------------------------------------------------------- profiles --

def stage(profile, cfg, run_dir, t_start, t_end):
    """Stage driver files for [t_start, t_end]; return (auroral_model, lines).

    Raises WaitingForData if the live IMF does not yet cover t_end.
    """
    if profile == 'example2002':
        lines = """\
#MHD_INDICES
UA/DataIn/Examples/imf20021221.dat

#SME_INDICES
UA/DataIn/Examples/ae20021221.dat	SME Filename
none              			onset time delay file
T					convert SME to Hemispheric Power

#NGDC_INDICES
UA/DataIn/f107.txt

#EUV_DATA
T						Use FISM solar flux data
UA/DataIn/FISM/fismflux_daily_2002.dat		Filename"""
        return 'FTA', lines

    if profile == 'midl_live':
        imf_dest = os.path.join(run_dir, 'rt_imf.dat')
        imf_source = resolve_imf(cfg['IMF_MANIFEST'],
                                 cfg.get('IMF_PAYLOAD', 'imf_14re'))
        stage_imf(imf_source, imf_dest)
        first, last, frontier = imf_coverage(imf_dest)
        if last is None or last < t_end:
            raise WaitingForData(
                'IMF covers to %s, segment ends %s' % (last, t_end))
        if first is not None and first > t_start:
            raise WaitingForData(
                'IMF starts %s, segment starts %s (window too old)'
                % (first, t_start))
        # Structural safety gate, independent of loop pacing: a chained
        # segment may only consume settled arrivals. Rows past the
        # observation frontier are projections the next tick may rewrite
        # (overtaking parcels) — consuming them would bake unstable driver
        # data into the restart chain. The disposable forecast head is the
        # only consumer allowed past this line.
        if frontier is not None and t_end > frontier:
            raise WaitingForData(
                'segment ends %s, past the observation frontier %s'
                % (t_end, frontier))

        cache_dir = os.path.join(cfg['STATE_ROOT'], 'driver_cache')
        os.makedirs(cache_dir, exist_ok=True)

        # Pad the aurora file well past both ends (readers buffer ~1 h).
        pad = timedelta(hours=2)
        aurora_mode = cfg.get('AURORA_MODE', 'hpi_const')
        if aurora_mode == 'hpi_const':
            write_const_hpi(os.path.join(run_dir, 'rt_hpi.txt'),
                            t_start - pad, t_end + pad,
                            float(cfg.get('HP_CONST_GW', 20.0)))
            model = 'hpi'
            aurora_lines = """\
#NOAAHPI_INDICES
rt_hpi.txt"""
        elif aurora_mode == 'hpi_live':
            write_live_hpi(os.path.join(run_dir, 'rt_hpi.txt'),
                           t_start - pad, t_end + pad, cache_dir,
                           float(cfg.get('HP_CONST_GW', 20.0)))
            model = 'hpi'
            aurora_lines = """\
#NOAAHPI_INDICES
rt_hpi.txt"""
        elif aurora_mode == 'ovation':
            # OVATION Prime inside GITM (ext/Electrodynamics), driven by
            # the staged IMF via #MHD_INDICES — no aurora driver file at
            # all, and no SWPC dependency (their HPI is itself OVATION
            # output collapsed to a scalar). Electron diffuse+mono+wave
            # on, ion precipitation off pending the Aaron B review.
            model = 'ovation'
            aurora_lines = """\
#AURORATYPES
T		UseDiffuseAurora
T		UseMonoAurora
T		UseWaveAurora
F		UseIonAurora"""
        elif aurora_mode == 'fta_const':
            write_const_sme(os.path.join(run_dir, 'rt_sme.dat'),
                            t_start - pad, t_end + pad,
                            float(cfg.get('AE_CONST_NT', 200.0)))
            model = 'FTA'
            aurora_lines = """\
#SME_INDICES
rt_sme.dat	SME Filename
none		onset time delay file
T		convert SME to Hemispheric Power"""
        else:
            raise ValueError('unknown AURORA_MODE: %s' % aurora_mode)

        f107 = cfg.get('F107', '140.0')
        f107a = cfg.get('F107A', '140.0')
        if cfg.get('F107_MODE', 'const') == 'live':
            f107, f107a = get_live_f107(cache_dir, f107, f107a)

        lines = """\
#MHD_INDICES
rt_imf.dat

%s

#F107
%s		f10.7
%s		f10.7 averaged over 81 days""" % (aurora_lines, f107, f107a)
        return model, lines

    raise ValueError('unknown profile: %s' % profile)
