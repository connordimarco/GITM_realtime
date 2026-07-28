#!/usr/bin/env python3
"""GITM-RT segment engine: run GITM as a chain of restart segments.

Stdlib only; runs under any python3. Usage:

  segment.py init --start 2026-07-21T12:00:00 [--force]
  segment.py advance [--seconds N]
  segment.py status

Configuration comes from rt/rt_config.sh; any key can be overridden with a
GITM_RT_<KEY> environment variable (e.g. GITM_RT_STATE_ROOT for tests).

State layout (STATE_ROOT):
  run/                 GITM run directory (make rundir; restartIN made real)
  state.json           {head_utc, mode, segments, last_status, last_wall_s}
  logs/harness.log     one line per advance
  logs/segments/       archived per-segment GITM logs + runlogs

Restart contract (crash-safe): a segment launches from restartIN/ and GITM's
clean exit writes restartOUT/. Only after the segment is VERIFIED (the GITM
logfile's last line reached TIMEEND) is restartOUT promoted to restartIN and
the head advanced. On any failure restartIN is untouched and the same segment
is retried by the next advance.

Exit codes: 0 ok, 1 error, 2 bad usage/state, 3 waiting for driver data.
"""

import argparse
import glob
import json
import os
import shutil
import string
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drivers

RT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(RT_DIR, 'rt_config.sh')
TIME_FMT = '%Y-%m-%dT%H:%M:%S'


# ----------------------------------------------------------------- state --

def state_paths(cfg):
    root = cfg['STATE_ROOT']
    return {
        'root': root,
        'run': os.path.join(root, 'run'),
        'state': os.path.join(root, 'state.json'),
        'harness_log': os.path.join(root, 'logs', 'harness.log'),
        'seg_logs': os.path.join(root, 'logs', 'segments'),
    }


def load_state(paths):
    with open(paths['state']) as f:
        return json.load(f)


def save_state(paths, st):
    tmp = paths['state'] + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, paths['state'])


def log_line(paths, msg):
    stamp = datetime.now(timezone.utc).strftime(TIME_FMT)
    line = '%s %s' % (stamp, msg)
    print(line)
    with open(paths['harness_log'], 'a') as f:
        f.write(line + '\n')


# ------------------------------------------------------------------ init --

def cmd_init(cfg, args):
    paths = state_paths(cfg)
    run = paths['run']
    if os.path.exists(paths['state']) and not args.force:
        print('state exists at %s (use --force to re-init)' % paths['state'])
        return 2
    os.makedirs(paths['seg_logs'], exist_ok=True)

    if not os.path.isdir(os.path.join(run, 'UA')):
        env = dict(os.environ)
        env['PATH'] = cfg['MPI_BIN'] + ':' + env.get('PATH', '')
        subprocess.run(
            ['make', 'rundir', 'RUNDIR=' + run, 'STANDALONE=YES',
             'UADIR=' + cfg['GITM_ROOT']],
            cwd=cfg['GITM_ROOT'], env=env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # make rundir leaves UA/restartIN as a symlink to restartOUT; the crash
    # safety of the chain depends on them being separate real directories.
    ua = os.path.join(run, 'UA')
    rin = os.path.join(ua, 'restartIN')
    if os.path.islink(rin):
        os.unlink(rin)
    os.makedirs(rin, exist_ok=True)
    os.makedirs(os.path.join(ua, 'restartOUT'), exist_ok=True)
    top_rin = os.path.join(run, 'restartIN')   # run-dir convenience symlink
    if os.path.islink(top_rin):
        os.unlink(top_rin)
        os.symlink(os.path.join('UA', 'restartIN'), top_rin)

    start = datetime.strptime(args.start, TIME_FMT)
    save_state(paths, {
        'head_utc': start.strftime(TIME_FMT),
        'mode': 'cold',
        'segments': 0,
        'last_status': 'initialized',
        'last_wall_s': None,
    })
    log_line(paths, 'init head=%s profile=%s run=%s'
             % (args.start, cfg['PROFILE'], run))
    return 0


# --------------------------------------------------------------- advance --

def render_uam(cfg, run, restart, t0, t1, auroral_model, driver_lines):
    with open(os.path.join(RT_DIR, 'templates', 'UAM.in.rt')) as f:
        template = string.Template(f.read())
    text = template.substitute(
        RESTART='T' if restart else 'F',
        START_YEAR=t0.year, START_MONTH=t0.month, START_DAY=t0.day,
        START_HOUR=t0.hour, START_MINUTE=t0.minute, START_SECOND=t0.second,
        END_YEAR=t1.year, END_MONTH=t1.month, END_DAY=t1.day,
        END_HOUR=t1.hour, END_MINUTE=t1.minute, END_SECOND=t1.second,
        NBLK_LON=cfg['NBLK_LON'], NBLK_LAT=cfg['NBLK_LAT'],
        DT_RESTART=cfg['DT_RESTART'], DT_PLOT=cfg['DT_PLOT'],
        AURORAL_MODEL=auroral_model, DRIVER_LINES=driver_lines,
    )
    with open(os.path.join(run, 'UAM.in'), 'w') as f:
        f.write(text)


def gitm_log_end_time(run):
    """Sim time of the last line of the newest GITM logfile, or None."""
    logs = glob.glob(os.path.join(run, 'UA', 'data', 'log*.dat'))
    if not logs:
        return None, None
    newest = max(logs, key=os.path.getmtime)
    last = None
    with open(newest) as f:
        for line in f:
            parts = line.split()
            if len(parts) > 8 and parts[0].isdigit() and len(parts[1]) == 4:
                last = parts
    if last is None:
        return None, newest
    try:
        return datetime(*[int(x) for x in last[1:7]]), newest
    except ValueError:
        return None, newest


def clear_dir(d):
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


def _recover_restarts(ua):
    """Repair a promote interrupted between its atomic renames.

    Crash after rout->restartIN.new but before the swap: restartIN still
    holds the old (valid) state — discard the staging dir and let the
    segment rerun. Crash after restartIN was removed: the staging dir IS
    the completed new state — install it (the head in state.json is still
    the old time, so the next run is a zero-length no-op that re-syncs).
    """
    rin = os.path.join(ua, 'restartIN')
    rnew = os.path.join(ua, 'restartIN.new')
    if os.path.isdir(rnew):
        if os.path.isdir(rin):
            shutil.rmtree(rnew)
        else:
            os.replace(rnew, rin)


def cmd_advance(cfg, args):
    paths = state_paths(cfg)
    run = paths['run']
    _recover_restarts(os.path.join(run, 'UA'))
    st = load_state(paths)
    seconds = args.seconds or int(cfg['SEGMENT_SECONDS'])
    t0 = datetime.strptime(st['head_utc'], TIME_FMT)
    t1 = t0 + timedelta(seconds=seconds)
    cold = (st['mode'] == 'cold')
    tag = t1.strftime('%Y%m%dT%H%M%S')

    # 1. drivers
    try:
        auroral_model, driver_lines = drivers.stage(
            cfg['PROFILE'], cfg, run, t0, t1)
    except drivers.WaitingForData as e:
        log_line(paths, 'advance %s..%s WAIT %s'
                 % (st['head_utc'], t1.strftime(TIME_FMT), e))
        return 3

    # 2. input file + clean slate for this attempt
    render_uam(cfg, run, not cold, t0, t1, auroral_model, driver_lines)
    ua = os.path.join(run, 'UA')
    clear_dir(os.path.join(ua, 'restartOUT'))
    for old in glob.glob(os.path.join(ua, 'data', 'log*.dat')):
        os.unlink(old)   # promoted segments archive theirs; leftovers = failed

    # 3. run
    env = dict(os.environ)
    env['PATH'] = cfg['MPI_BIN'] + ':' + env.get('PATH', '')
    env.setdefault('TMPDIR', cfg.get('TMPDIR_OVERRIDE', '/tmp'))
    timeout = 120 + int(0.5 * seconds)
    runlog = os.path.join(paths['seg_logs'], 'seg_%s.runlog' % tag)
    wall0 = time.time()
    try:
        with open(runlog, 'w') as out:
            proc = subprocess.run(
                ['nice', '-n', cfg['NICE'], 'mpirun', '-np', cfg['NRANKS'],
                 './GITM.exe'],
                cwd=run, env=env, stdout=out, stderr=subprocess.STDOUT,
                timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -9
    wall = time.time() - wall0

    # 4. verify — GITM's adaptive dt does not always land exactly on
    # TIMEEND (the last logged step can be a fraction of a second short,
    # e.g. :59 for a :00 end), so accept within one max timestep. The
    # restart carries the exact time; the nominal head stays within one
    # dt of truth and the error does not compound across segments.
    end_time, gitm_log = gitm_log_end_time(run)
    ok = (rc == 0 and end_time is not None
          and abs((end_time - t1).total_seconds()) <= 5)
    if not ok:
        clear_dir(os.path.join(ua, 'restartOUT'))
        st['last_status'] = 'FAIL rc=%s reached=%s' % (rc, end_time)
        save_state(paths, st)
        log_line(paths, 'advance %s..%s FAIL rc=%s reached=%s wall=%.0fs (%s)'
                 % (st['head_utc'], t1.strftime(TIME_FMT), rc, end_time, wall,
                    runlog))
        return 1

    # 5. promote — via whole-directory renames so no crash point can
    # destroy the only good checkpoint: rename restartOUT to a staging
    # name (atomic), remove old restartIN, rename staging into place
    # (atomic). _recover_restarts() at advance start handles a crash
    # between any two of these steps.
    rin = os.path.join(ua, 'restartIN')
    rout = os.path.join(ua, 'restartOUT')
    rnew = os.path.join(ua, 'restartIN.new')
    os.replace(rout, rnew)
    shutil.rmtree(rin)
    os.replace(rnew, rin)
    os.makedirs(rout, exist_ok=True)
    if gitm_log:
        os.replace(gitm_log,
                   os.path.join(paths['seg_logs'], 'log_%s.dat' % tag))

    st.update({
        'head_utc': t1.strftime(TIME_FMT),
        'mode': 'chain',
        'segments': st['segments'] + 1,
        'last_status': 'ok',
        'last_wall_s': round(wall, 1),
    })
    save_state(paths, st)
    log_line(paths, 'advance %s..%s OK wall=%.0fs cold=%s'
             % (t0.strftime(TIME_FMT), t1.strftime(TIME_FMT), wall, cold))
    return 0


# ---------------------------------------------------------------- status --

def cmd_status(cfg, args):
    paths = state_paths(cfg)
    if not os.path.exists(paths['state']):
        print('no state at %s (run init)' % paths['state'])
        return 2
    st = load_state(paths)
    head = datetime.strptime(st['head_utc'], TIME_FMT)
    lag = (datetime.now(timezone.utc).replace(tzinfo=None) - head)
    print('head:      %s UTC' % st['head_utc'])
    print('lag:       %.1f min (target %.1f)'
          % (lag.total_seconds() / 60.0,
             int(cfg['LAG_TARGET_SECONDS']) / 60.0))
    print('mode:      %s' % st['mode'])
    print('segments:  %s' % st['segments'])
    print('last:      %s (wall %ss)' % (st['last_status'], st['last_wall_s']))
    return 0


# ------------------------------------------------------------------ main --

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_init = sub.add_parser('init')
    p_init.add_argument('--start', required=True,
                        help='cold-start UTC time, e.g. 2026-07-21T12:00:00')
    p_init.add_argument('--force', action='store_true')
    p_adv = sub.add_parser('advance')
    p_adv.add_argument('--seconds', type=int, default=None)
    sub.add_parser('status')
    args = parser.parse_args()

    cfg = drivers.load_config(CONFIG_PATH)
    return {'init': cmd_init, 'advance': cmd_advance,
            'status': cmd_status}[args.cmd](cfg, args)


if __name__ == '__main__':
    sys.exit(main())
