"""The portable Windows app: one folder, a launcher, and two hidden workers.

A person who has a Digitakt .syx and a zip should not need a shell, a venv or
a single environment variable. So everything lives NEXT TO THE EXE:

    <APP_ROOT>/logs/launcher.log
    <APP_ROOT>/firmware/<slug>/        one folder per firmware release

and every path the emulator reads (DT2_SYX, DT2_SECTIONS, DT2_SNAPSHOTS,
DT2_PLUSDRIVE, DT2_MAIN_IMG, DT2_DEVICES) is recomputed from that folder on
every start, by plain assignment and as absolute paths. Nothing absolute is
ever stored: the folder can move, change drive letter or be unzipped
somewhere else and still work (snapshots and sidecars hold hashes, not
paths). APP_ROOT is dirname(sys.executable) when frozen; in development it
is --home, else $DIGIEMU_HOME, else <repo>/portable, so running from source
never scatters firmware copies into the working directory.

WHY SEPARATE PROCESSES. The launcher never hosts Unicorn. A native crash in
the patched DLL would otherwise take the window down with it; cancelling
would need cooperative hooks, where a child can simply be terminated (every
stage writes *.tmp and os.replace's, and emu.bootstrap skips stages whose
stamp and outputs verify, so the next run resumes); and the ladder calls
back into Python on every instruction, which would fight a UI thread for
the GIL. So the same exe dispatches hidden modes:

    digiemu.exe                              the Tk launcher
    digiemu.exe --worker first-run <fwdir>   builds; JSON lines on stdout
    digiemu.exe --worker panel <fwdir>       opens the panel for that folder
    digiemu.exe --worker check <checkdir>    checks a build (emu.fwcheck)
                                             before it goes on a device

`sys.executable -m` does not exist in a frozen exe, so only development
spawns `python -m emu.portable`. Workers run with CREATE_NO_WINDOW and opt
out of Windows 11 power throttling: a windowless child on a hybrid CPU is
exactly what EcoQoS parks on an efficiency core.

WHY STDOUT IS RESERVED. The emulator prints freely -- rung lines from inside
an instruction hook, TASK_CREATE lines, symbol notes -- and a piped stdout
in a GUI-subsystem child is cp1252, where one non-Latin path character
raises UnicodeEncodeError inside the hook and kills the ladder. So while a
stage runs, sys.stdout and sys.stderr point at the folder's utf-8 log, and
progress goes out on the ORIGINAL stdout as one ASCII-only JSON object per
line (json's ensure_ascii makes the pipe's encoding irrelevant):

    {"step","kind","done","total","text","data"}  one per emu.bootstrap Event
    {"kind":"result","ok":bool,"error":str|null}   last line, always

WHY A LOCK. Two panels on one firmware both memory-map plusdrive.img and
both flush their whole overlay on exit, last writer wins byte by byte; a
second first run rewrites the snapshots the first is reading. So a worker
holds an exclusive msvcrt lock on <fwdir>/.lock for its whole life, and a
second one on the same folder fails with a clear message. Different
firmware folders run side by side.

WHY THE CARD DECIDES THE SNAPSHOT. A snapshot carries the firmware's RAM,
including its view of the +Drive filesystem. Resuming it over a card that
changed since would pair stale caches with newer content, so the panel
worker asks emu.bootstrap.choose_snapshot, which only returns resume.snap
or gui.snap when the card's recorded stamp still matches; otherwise it
exits 3 and the launcher offers Rebuild (ladder+intro+settle over the card
as it is now; the card itself is kept). After a clean panel exit, which
saved resume.snap, the worker records the card's new stamp. A session that
ends any other way leaves the stamp alone unless the card moved under it
(or dtpanel says it flushed the card but could not save): a halt neither
flushes nor saves, so the last session and the card still agree.

WHY SETTING UP AGAIN IS REFUSED. --add and the launcher's Add on a folder
that is already Ready (or Rebuild needed, or open) do not start a build: a
build reruns the cold boot and throws away the saved session. Rebuild is
its own, explicit action (--rebuild NAME, the Rebuild button), and so is
Reset to factory (--reset NAME --yes, the button), which also deletes the
card and with it every project and sample on it.

WHY THE LAUNCHER RUNS ITS SELF-TEST IN A CHILD. The check that matters runs
guest code in the patched unicorn.dll, and a bad DLL crashes the process
that loads it. So the launcher never loads Unicorn: it runs `--selftest`
as a child at start, reads the JSON report (which names the check that was
running if the child died) and shows what failed, while the list stays
usable.

EXIT CODES (the CLI modes and the workers):
    0  ok (--add of a folder that is already set up: 'already set up';
       --check: the build passed)
    1  a build, the emulator or the panel failed; --check: the build
       failed, or the check could not finish
    2  usage error; from the panel worker: the session could not be saved
    3  not ready: set it up or rebuild it first (--add: Rebuild needed)
    4  untested release (--add) or --reset without --yes
    5  another process holds this firmware folder
    6  recognised product, not supported in this version
    7  panel worker: the snapshot was made by another build of digiemu
       (dtpanel's 4, INCOMPATIBLE) -- rebuild needed, nothing is broken
   64  dtpanel could not parse its arguments (passed through)

emu.release, emu.bootstrap and emu.dtpanel are imported lazily, and looked
up in sys.modules first, so the launcher starts without Unicorn and tests
can stand in for them.
"""
import argparse
import collections
import contextlib
import dataclasses
import datetime
import faulthandler
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

APP_NAME = 'digiemu'
APP_VERSION = '0.2.0'

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Which devices have a panel in this version, and which module draws it.
# A device file can exist (Digitakt II, Digitone II) without the first-run
# recipe or a panel for it: those are named and refused.
PANELS = {'dt1': 'emu.dtpanel', 'dn1': 'emu.dnpanel'}
# How the refusals and the empty list name what this version runs.
SUPPORTED = 'Digitakt (mk1) and Digitone (mk1)'

STEPS = ('copy', 'extract', 'card', 'ladder', 'intro', 'settle')
STEP_TITLES = {
    'copy': 'Copying the firmware',
    'extract': 'Extracting the firmware sections',
    'card': 'Preparing the +Drive card',
    'ladder': 'Cold boot',
    'intro': 'Boot intro',
    'settle': 'First-boot setup',
}
# Share of the progress bar per step, from the measured first run: the ladder
# is ~55% of the wall time and the settle ~40%. Extract and card take seconds.
STEP_WEIGHTS = {'copy': 0.0, 'extract': 0.0, 'card': 0.0,
                'ladder': 0.55, 'intro': 0.05, 'settle': 0.40}

# The firmware check (emu/fwcheck.py), per build: these stages, then the
# comparison when there is a stock build to compare with.
CHECK_STAGES = ('container', 'prepare', 'bootloader', 'boot', 'run')
CHECK_TITLES = {
    'container': 'Reading the .syx',
    'prepare': 'Preparing',
    'bootloader': 'Running its bootloader',
    'boot': 'Cold boot, strict',
    'run': 'Playing the key script',
    'compare': 'Comparing screens and sound',
}
CHECK_ROLES = {'baseline': 'Stock', 'build': 'Build', 'compare': ''}
# Seconds per stage on the reference desktop, for the bar and the estimate.
# Digitakt measured (2026-09-24); the Digitone's timed run is extrapolated
# from its untimed one (the cycle clock runs about 64x slower than real time).
CHECK_SECONDS = {
    'dt1': {'container': 1, 'prepare': 1, 'bootloader': 9, 'boot': 75,
            'run': 50, 'run-timed': 340, 'compare': 2},
    'dn1': {'container': 1, 'prepare': 1, 'bootloader': 13, 'boot': 150,
            'run': 90, 'run-timed': 540, 'compare': 2},
}
CHECK_REQUEST = 'request.json'
CHECK_LOG = 'check.log'
CHECK_SUMMARY = 'summary.txt'

# A new firmware folder grows to ~1.3 GB: a 0.95 GB card (not sparse on
# NTFS), snapshots and sections. A check needs the same while one build's
# work folder exists (emu.fwcheck.run_check keep_work=False).
MIN_FREE_BYTES = 1536 * 1024 * 1024
# Long paths are off by default on Windows (MAX_PATH 260) and Tcl's own file
# I/O may not honour longPathAware either; keep a margin under it.
MAX_PATH_CHARS = 240

SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{0,63}$')
RESERVED_NAMES = frozenset(
    ['con', 'prn', 'aux', 'nul']
    + ['com%d' % i for i in range(1, 10)] + ['lpt%d' % i for i in range(1, 10)])

STATE_FILE = 'firmware.json'
LOCK_NAME = '.lock'
CARD_NAME = 'plusdrive.img'     # FirmwarePaths.card's file name
# The locked byte sits far past the end of the file, so the "who holds it"
# text at the start stays readable to a second process while the lock is held.
LOCK_OFFSET = 1 << 30
# A worker retries this long: the launcher's status probe takes the lock for
# a moment, and a worker starting in that moment must not fail.
LOCK_WAIT = 3.0

EXIT_OK = 0
EXIT_FAILED = 1         # a build or the emulator failed
EXIT_USAGE = 2          # bad arguments (from the panel worker: saving failed)
EXIT_NOT_READY = 3      # no usable snapshot: set up or rebuild first
EXIT_NEEDS_YES = 4      # untested release, and --yes was not given
EXIT_BUSY = 5           # another process holds this firmware folder
EXIT_UNSUPPORTED = 6    # recognised product, not supported in this version
EXIT_INCOMPATIBLE = 7   # panel worker: snapshot from another build; rebuild
EXIT_SAMPLES_ADDED = 8  # panel worker: LOAD SAMPLES changed the card; rebuild

# dtpanel.main's codes this module acts on: 2, the card was flushed but the
# session not saved; 4 (emu.dtpanel.INCOMPATIBLE, used when a stand-in or a
# failed import has none), the snapshot is from another build.
DTPANEL_NOT_SAVED = 2
DTPANEL_INCOMPATIBLE = 4
DTPANEL_LOAD_FAILED = 5     # the snapshot could not be opened (emu.dtpanel)
DTPANEL_SAMPLES_ADDED = 6   # LOAD SAMPLES wrote to the card (emu.dtpanel)

# Atomic writes retry a rename Windows refuses while another process (a
# second launcher, --list, a virus scanner) has the target open.
REPLACE_TRIES = 20
REPLACE_DELAY = 0.1

# The child self-test starts Python, Tk and Unicorn and runs a few guest
# instructions: seconds normally, minutes on a machine that is swapping.
SELFTEST_TIMEOUT = 180
SELFTEST_CHECKS = {
    'unicorn': 'the bundled Unicorn emulator engine',
    'native_options': "Unicorn's speed patches",
    'capstone': 'the Capstone disassembler',
    'devices': 'the bundled device files',
    'tk': 'the window toolkit (Tk)',
    'imports': "the app's own modules",
    'unicorn_compat': 'running firmware code in Unicorn',
}

READY = 'Ready'
NEEDS_SETUP = 'Needs setup'
REBUILD = 'Rebuild needed'
BUILDING = 'Building'
RUNNING = 'Running'
BROKEN = 'Broken'
UNSUPPORTED = 'Not supported'

# status_of's reason when the panel found the snapshot was made by another
# build (state['incompatible'], set by the panel worker).
INCOMPATIBLE_REASON = 'incompatible-snapshot'

CREATE_NO_WINDOW = 0x08000000


class Busy(RuntimeError):
    """Another process holds this firmware folder's lock."""

    def __init__(self, message, holder=''):
        super().__init__(message)
        self.holder = holder


class FolderError(ValueError):
    """A path is not a usable firmware folder. The message says why."""


class Refused(ValueError):
    """A firmware file cannot be added. `code` is the exit code for the CLI."""

    def __init__(self, message, code=EXIT_FAILED):
        super().__init__(message)
        self.code = code


# --- layout ------------------------------------------------------------------

def is_frozen():
    return bool(getattr(sys, 'frozen', False))


def bundle_dir():
    """-> where bundled read-only data lives: _MEIPASS when frozen, else the repo."""
    if is_frozen():
        return getattr(sys, '_MEIPASS', None) or \
            os.path.dirname(os.path.abspath(sys.executable))
    return REPO


def app_root(home=None):
    """-> APP_ROOT, absolute. An explicit --home always wins.

    Frozen, the data sits next to the exe: that is what makes the folder
    portable, so no environment variable redirects it. In development
    $DIGIEMU_HOME does, else <repo>/portable."""
    if home:
        return os.path.abspath(home)
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    env = os.environ.get('DIGIEMU_HOME')
    if env:
        return os.path.abspath(env)
    return os.path.join(REPO, 'portable')


def devices_dir():
    return os.path.join(bundle_dir(), 'devices')


def firmware_root(home=None):
    return os.path.join(app_root(home), 'firmware')


def check_slug(slug):
    """-> slug, or ValueError. A slug names a folder, so it must be safe as
    one on every Windows volume: lower-case, short, no trailing dot and no
    reserved device name (CON.txt is CON too, so the part before the first
    dot counts)."""
    if not isinstance(slug, str) or not SLUG_RE.match(slug) \
            or slug.endswith('.') or slug.split('.')[0] in RESERVED_NAMES:
        raise ValueError('not a valid firmware folder name: %r' % (slug,))
    return slug


def firmware_dir(slug, home=None):
    return os.path.join(firmware_root(home), check_slug(slug))


def list_firmware_dirs(home=None):
    """-> absolute firmware folders under APP_ROOT, sorted by name."""
    base = firmware_root(home)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for name in names:
        try:
            check_slug(name)
        except ValueError:
            continue
        path = os.path.join(base, name)
        if os.path.isdir(path):
            out.append(path)
    return out


def local_app_home():
    """-> the fallback home for a read-only exe folder: %LOCALAPPDATA%/digiemu."""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, APP_NAME)


def own_folder(fwdir, home=None):
    """-> fwdir, absolute, if it is a slug-named folder directly under this
    APP_ROOT's firmware/; else ValueError (bad name) or FolderError. Guards
    everything that deletes: nothing outside firmware/ is ever touched."""
    fwdir = os.path.abspath(fwdir)
    check_slug(os.path.basename(fwdir))
    base = os.path.normcase(os.path.abspath(firmware_root(home)))
    if os.path.normcase(os.path.dirname(fwdir)) != base:
        raise FolderError('%s is not a firmware folder of this digiemu' % fwdir)
    return fwdir


def find_folder(name, home=None):
    """-> the firmware folder a CLI argument names: its slug (the folder
    name --list shows) or a path to it. FolderError, listing what is set up,
    when there is no such folder here."""
    root = firmware_root(home)
    try:
        path = firmware_dir(name, home)
    except ValueError:
        path = os.path.abspath(name)
        try:
            own_folder(path, home)
        except ValueError:
            path = None
    if path is None or not os.path.isdir(path):
        names = [os.path.basename(d) for d in list_firmware_dirs(home)]
        raise FolderError('No firmware named %r in %s. %s'
                          % (name, root, 'Set up here: %s.' % ', '.join(names)
                             if names else 'Nothing is set up there yet.'))
    return path


def cli_prefix(home=None):
    """-> how to run this program from a console, for the hints it prints."""
    if is_frozen():
        exe = os.path.basename(sys.executable)
        # The windowed digiemu.exe prints nowhere a person would see.
        prefix = 'digiemu-console.exe' if exe.lower() == 'digiemu.exe' else exe
    else:
        prefix = 'python -m emu.portable'
    if home:
        prefix += ' --home "%s"' % os.path.abspath(home)
    return prefix


# --- the modules this one drives (lazy, stub-friendly) -----------------------
#
# Each looks in sys.modules first so a test can stand in for it, and keeps a
# literal `from emu import ...` so PyInstaller's import scan still sees it.

def _bootstrap():
    mod = sys.modules.get('emu.bootstrap')
    if mod is None:
        from emu import bootstrap as mod
    return mod


def _release():
    mod = sys.modules.get('emu.release')
    if mod is None:
        from emu import release as mod
    return mod


def _fwcheck():
    mod = sys.modules.get('emu.fwcheck')
    if mod is None:
        from emu import fwcheck as mod
    return mod


def _panel_module(name='emu.dtpanel'):
    """-> the panel module PANELS names (emu.dtpanel, emu.dnpanel), looked
    up in sys.modules first like the others. The imports are spelled out,
    not importlib'd, so the packaging scan (tests/test_packaging.py) sees
    both and the bundle carries them."""
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    if name == 'emu.dnpanel':
        from emu import dnpanel as mod
    elif name == 'emu.dtpanel':
        from emu import dtpanel as mod
    else:
        raise ImportError('no panel module %r' % name)
    return mod


def _dtpanel():
    return _panel_module('emu.dtpanel')


def _tk_library_env(env, platform=None, prefix=None):
    """macOS, from source: point TCL_LIBRARY and TK_LIBRARY at the base
    Python's Tcl/Tk scripts when they are unset or wrong. -> env.

    In a venv on a uv-managed (python-build-standalone) Python, Tcl looks for
    init.tcl beside the venv's interpreter, where it is not, and Tk fails to
    start in the panel and in the workers. Only the version tkinter was built
    with is used. Nothing is written anywhere; the frozen app bundles its own
    Tcl/Tk and is left alone, and so is every other platform."""
    platform = sys.platform if platform is None else platform
    if platform != 'darwin' or is_frozen():
        return env
    try:
        import tkinter
    except ImportError:
        return env
    prefix = sys.base_prefix if prefix is None else prefix
    for var, name, marker in (
            ('TCL_LIBRARY', 'tcl%s' % tkinter.TclVersion, 'init.tcl'),
            ('TK_LIBRARY', 'tk%s' % tkinter.TkVersion, 'tk.tcl')):
        if os.path.isdir(env.get(var) or ''):
            continue
        where = os.path.join(prefix, 'lib', name)
        if os.path.isfile(os.path.join(where, marker)):
            env[var] = where
    return env


def _ensure_import_path():
    """Workers chdir into the firmware folder; from source, keep the repo
    importable for the lazy imports that follow."""
    if not is_frozen() and REPO not in sys.path:
        sys.path.insert(0, REPO)
    _tk_library_env(os.environ)


# --- one firmware folder -------------------------------------------------------

def open_paths(fwdir):
    """-> emu.bootstrap.FirmwarePaths for an existing firmware folder.

    The .syx name comes from firmware.json (written when the folder was
    created), else from the one .syx in the folder. It is a bare filename,
    never a path: the state file holds nothing absolute."""
    root = os.path.abspath(fwdir)
    if not os.path.isdir(root):
        raise FolderError('No firmware folder at %s' % root)
    b = _bootstrap()
    probe = b.FirmwarePaths(root=root, syx_name='firmware.syx',
                            devices_dir=devices_dir())
    state = b.read_state(probe) or {}
    name = (state.get('release') or {}).get('syx_name')
    if not name:
        found = sorted(n for n in os.listdir(root) if n.lower().endswith('.syx'))
        if len(found) != 1:
            raise FolderError('%s holds %s .syx file%s and no %s naming one'
                              % (root, len(found) or 'no',
                                 '' if len(found) == 1 else 's', STATE_FILE))
        name = found[0]
    if os.path.basename(name) != name or name in ('.', '..'):
        raise FolderError('%s names %r as its firmware, which is not a plain '
                          'file name' % (STATE_FILE, name))
    return b.FirmwarePaths(root=root, syx_name=name, devices_dir=devices_dir())


def apply_env(paths):
    """Point this process at one firmware folder: the DT2_* variables by
    plain assignment (a stale value from the parent must never survive),
    utf-8 for any child, and the folder as the working directory so no
    relative default anywhere in emu/ lands somewhere else."""
    for key, value in paths.env().items():
        os.environ[key] = value
    os.environ['PYTHONUTF8'] = '1'
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    os.chdir(paths.root)


def child_env(base=None):
    """-> the environment for a worker child.

    No DT2_* at all: the child recomputes every one from its folder, so one
    firmware's paths can never leak into another's run. UTF-8 everywhere,
    unbuffered so progress arrives as it happens. From source the repo goes
    on PYTHONPATH; frozen, PYTHONHOME/PYTHONPATH could only point the
    bundled interpreter at someone else's library, so they go."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.upper().startswith('DT2_'):
            del env[key]
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    if is_frozen():
        env.pop('PYTHONHOME', None)
        env.pop('PYTHONPATH', None)
    else:
        rest = env.get('PYTHONPATH')
        env['PYTHONPATH'] = REPO + (os.pathsep + rest if rest else '')
        _tk_library_env(env)
    return env


def read_state(paths):
    return _bootstrap().read_state(paths) or {}


def write_state(paths, state):
    _bootstrap().write_state(paths, state)


def _retry_sharing(fn, *args):
    """fn(*args), retried while Windows reports a sharing violation.

    Python's open() does not share delete access, so a rename over (or the
    removal of) a file that any other process has open at that moment
    fails with 'access denied': a second launcher refreshing its list, a
    --list, a virus scanner. They let go within milliseconds."""
    for attempt in range(REPLACE_TRIES):
        try:
            return fn(*args)
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_DELAY)


def replace_retry(src, dst):
    """os.replace for every atomic write here: emu.bootstrap.replace_retry,
    or the same retry loop when the bootstrap in use has none (a stand-in,
    or a tree synced before it had one)."""
    fn = getattr(_bootstrap(), 'replace_retry', None)
    if fn is not None:
        return fn(src, dst)
    return _retry_sharing(os.replace, src, dst)


def release_record(rel, syx_name):
    """-> what firmware.json keeps about a release. Names and hashes only."""
    stamp = getattr(rel, 'stamp', None)
    if isinstance(stamp, datetime.datetime):
        stamp = stamp.isoformat(sep=' ')
    elif stamp is not None:
        stamp = str(stamp)
    device = getattr(rel, 'device', None)
    return {
        'product': rel.product,
        'version': rel.version,
        'build': rel.build,
        'stamp': stamp,
        'sha256': rel.sha256,
        'status': rel.status,
        'label': rel.label,
        'slug': rel.slug,
        'syx_name': syx_name,
        'device': getattr(device, 'short', None),
        'device_name': getattr(device, 'name', None),
    }


# --- the lock ----------------------------------------------------------------

class FolderLock:
    """An exclusive lock on <fwdir>/.lock, held for the life of a worker.

    msvcrt.locking is a mandatory byte-range lock owned by the handle, so a
    second handle -- in this process or another -- is refused, and Windows
    drops it when the process dies, crash included. The text at the start
    of the file says who holds it, for the message a second process shows.
    """

    def __init__(self, fwdir, purpose='worker'):
        self.path = os.path.join(os.path.abspath(fwdir), LOCK_NAME)
        self.purpose = purpose
        self.fd = None

    def acquire(self, timeout=0.0):
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                self._try()
                return self
            except Busy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def _try(self):
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0) \
            | getattr(os, 'O_NOINHERIT', 0) | getattr(os, 'O_CLOEXEC', 0)
        fd = os.open(self.path, flags, 0o644)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            holder = read_lock_info(self.path)
            raise Busy('This firmware folder is in use by another digiemu '
                       'process%s. Close it first, or wait for it to finish.'
                       % (' (%s)' % holder if holder else ''), holder)
        self.fd = fd
        info = '%s pid %d' % (self.purpose, os.getpid())
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, info.encode('ascii', 'replace'))
        except OSError:
            pass

    def release(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            _unlock_fd(fd)
        except OSError:
            pass
        os.close(fd)

    def __enter__(self):
        if self.fd is None:
            self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _lock_fd(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        # flock, not lockf: POSIX record locks are per process, and a second
        # handle in the same process must be refused here as on Windows.
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd):
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def read_lock_info(path):
    try:
        with open(path, 'rb') as fh:
            return fh.read(200).decode('ascii', 'replace').strip()
    except OSError:
        return ''


def lock_holder(fwdir):
    """-> None if nobody holds the folder, else the holder's description."""
    if not os.path.isdir(fwdir):
        return None
    lock = FolderLock(fwdir, 'probe')
    try:
        lock.acquire(0)
    except Busy as exc:
        return exc.holder or 'busy'
    except OSError:
        return None
    lock.release()
    return None


# --- the progress protocol ---------------------------------------------------

def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def event_record(ev):
    """-> the JSON-able dict for one emu.bootstrap Event (or a dict of one)."""
    get = ev.get if isinstance(ev, dict) else (lambda k, d=None: getattr(ev, k, d))
    data = get('data', None)
    return {'step': str(get('step', '') or ''),
            'kind': str(get('kind', '') or ''),
            'done': _int(get('done', 0)),
            'total': _int(get('total', 0)),
            'text': str(get('text', '') or ''),
            'data': data if isinstance(data, dict) else {}}


def result_record(ok, error=None, step=None, log=None):
    rec = {'kind': 'result', 'ok': bool(ok),
           'error': None if ok else (error or 'failed')}
    if step:
        rec['step'] = step
    if log:
        rec['log'] = log
    return rec


def encode_line(rec):
    """-> one line of ASCII JSON (no newline). Never raises: data the
    encoder cannot take is replaced by its repr rather than losing the
    event."""
    try:
        return json.dumps(rec, ensure_ascii=True, separators=(',', ':'),
                          default=str)
    except (TypeError, ValueError):
        safe = dict(rec)
        safe['data'] = {'repr': repr(rec.get('data'))}
        return json.dumps(safe, ensure_ascii=True, separators=(',', ':'),
                          default=str)


def decode_line(line):
    """-> the dict on a protocol line, or None for anything else (a stray
    print from native code, a traceback before logging started)."""
    if isinstance(line, bytes):
        line = line.decode('utf-8', 'replace')
    line = line.strip()
    if not line.startswith('{'):
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and 'kind' in rec else None


class LineEmitter:
    """Writes each record as one flushed JSON line. If the reader goes away
    (the launcher was closed) the build carries on without it."""

    def __init__(self, stream):
        self.stream = stream

    def __call__(self, rec):
        if self.stream is None:
            return
        try:
            self.stream.write(encode_line(rec) + '\n')
            self.stream.flush()
        except (OSError, ValueError):
            self.stream = None


class ProgressModel:
    """Turns records into a step, an overall fraction and recent lines.

    The fraction never goes backwards: a resumed run starts by skipping
    stages, and a bar that jumps back reads as something having gone wrong.
    A step with total 0, or past its total (the settle runs past its
    expected point), is marked indeterminate."""

    def __init__(self, keep=200):
        self.step = None
        self.fraction = 0.0
        self.indeterminate = False
        self.text = ''
        self.lines = collections.deque(maxlen=keep)
        self.result = None

    def feed(self, rec):
        kind = rec.get('kind')
        if kind == 'result':
            self.result = rec
            if rec.get('ok'):
                self.fraction = 1.0
                self.indeterminate = False
            return
        step = rec.get('step')
        data = rec.get('data') if isinstance(rec.get('data'), dict) else {}
        overall = data.get('overall')
        if step in STEP_WEIGHTS and isinstance(overall, (int, float)) \
                and not isinstance(overall, bool):
            # emu.bootstrap knows each stage's expected length (instructions
            # to a live UI, to a settle) and says where the whole run is.
            self.step = step
            if kind in ('start', 'done'):
                self.indeterminate = False
            if 'indeterminate' in data:
                self.indeterminate = bool(data['indeterminate'])
            self.fraction = max(self.fraction, min(1.0, max(0.0, float(overall))))
        elif step in STEP_WEIGHTS:
            self.step = step
            done, total = _int(rec.get('done')), _int(rec.get('total'))
            if kind in ('start', 'done'):
                part = 1.0 if kind == 'done' else 0.0
                self.indeterminate = False
            elif total > 0:
                part = min(1.0, done / total)
                self.indeterminate = done > total
            else:
                # A tick with no total has no scale; a note says nothing
                # about progress either way.
                part = 0.0
                if kind == 'tick':
                    self.indeterminate = True
            idx = STEPS.index(step)
            base = sum(STEP_WEIGHTS[s] for s in STEPS[:idx])
            now = min(1.0, base + STEP_WEIGHTS[step] * part)
            self.fraction = max(self.fraction, now)
        text = rec.get('text')
        if text:
            self.text = str(text)
            self.lines.append(self.text)

    def feed_text(self, line):
        line = line.rstrip('\r\n')
        if line.strip():
            self.lines.append(line)


class ConsoleProgress:
    """--add's console view: every start/done/note line, ticks at most every
    `every` seconds (rewritten in place on a terminal)."""

    def __init__(self, stream, every=None):
        self.stream = stream
        self.model = ProgressModel()
        self.last = 0.0
        isatty = getattr(stream, 'isatty', None)
        try:
            self.live = bool(isatty and isatty())
        except (OSError, ValueError):
            self.live = False
        # A terminal line is rewritten in place; a redirected one is a log.
        self.every = every if every is not None else (1.0 if self.live else 30.0)
        self.open_line = False

    def __call__(self, rec):
        self.model.feed(rec)
        kind = rec.get('kind')
        now = time.monotonic()
        if kind == 'tick' and now - self.last < self.every:
            return
        self.last = now
        line = '[%3d%%] %s: %s' % (round(self.model.fraction * 100),
                                   rec.get('step') or '-',
                                   rec.get('text') or _count_text(rec) or kind)
        if self.live and kind == 'tick':
            _say(self.stream, '\r' + line[:78].ljust(78), end='')
            self.open_line = True
            return
        self.say(line)

    def say(self, text):
        """A plain line, after ending a tick line left open in place."""
        if self.open_line:
            _say(self.stream, '')
            self.open_line = False
        _say(self.stream, text)


def _count_text(rec):
    """-> 'done/total' for a record with no text, in M when they are
    instruction counts."""
    done, total = _int(rec.get('done')), _int(rec.get('total'))
    if not done and not total:
        return ''
    if max(done, total) >= 10_000_000:
        return '%dM of %dM instructions' % (done // 1_000_000, total // 1_000_000) \
            if total else '%dM instructions' % (done // 1_000_000)
    return '%d of %d' % (done, total) if total else str(done)


def _say(stream, text, end='\n'):
    """print() that survives a missing stdout (windowed exe) and a console
    that cannot encode the text."""
    if stream is None:
        return
    try:
        stream.write(text + end)
    except UnicodeEncodeError:
        enc = getattr(stream, 'encoding', None) or 'ascii'
        stream.write((text + end).encode(enc, 'replace').decode(enc, 'replace'))
    except (OSError, ValueError):
        return
    try:
        stream.flush()
    except (OSError, ValueError):
        pass


def describe(exc):
    """-> one line for the user. config.NotFound and device.DeviceError are
    SystemExit subclasses whose message is the whole point, so str() them;
    anything unexpected keeps its type name."""
    step, reason = getattr(exc, 'step', None), getattr(exc, 'reason', None)
    if step and reason:
        return '%s: %s' % (step, reason)
    msg = str(exc).strip()
    if isinstance(exc, (SystemExit, Busy, FolderError, Refused)):
        return msg or type(exc).__name__
    return '%s: %s' % (type(exc).__name__, msg) if msg else type(exc).__name__


# --- child processes ---------------------------------------------------------

def worker_command(mode, fwdir, *extra):
    """-> argv for a worker. Frozen, the exe IS the interpreter and has no -m."""
    args = ['--worker', mode, os.path.abspath(fwdir)] + list(extra)
    if is_frozen():
        return [sys.executable] + args
    return [sys.executable, '-m', 'emu.portable'] + args


def spawn_options(piped):
    """-> Popen keyword arguments for a worker (no window, no stdin)."""
    kw = {'stdin': subprocess.DEVNULL, 'env': child_env(),
          'cwd': os.path.dirname(os.path.abspath(sys.executable))
          if is_frozen() else REPO}
    if piped:
        kw['stdout'] = subprocess.PIPE
        kw['stderr'] = subprocess.STDOUT
    else:
        kw['stdout'] = subprocess.DEVNULL
        kw['stderr'] = subprocess.DEVNULL
    if os.name == 'nt':
        kw['creationflags'] = CREATE_NO_WINDOW
    return kw


class WorkerJob:
    """A Windows job object around one worker's process tree.

    terminate() ends the whole tree, which proc.terminate() cannot do in
    dev: there the venv's python.exe is a launcher that runs the real
    interpreter as its own child, so killing it left the build running and
    holding the folder lock. With kill_on_close, closing the job -- this
    process exiting, however it exits -- ends the tree too: for builds
    started from a console (--add, --rebuild), whose window the user may
    close; the worker has no console of its own to notice. The launcher's
    builds do without it, since quitting the launcher already asks whether
    to let them finish. Elsewhere, or if Windows refuses, a no-op."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, kill_on_close=False):
        self.handle = None
        self._k32 = None
        if os.name != 'nt':
            return
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,
                                                     wintypes.HANDLE]
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return
            if kill_on_close:
                class IoCounters(ctypes.Structure):
                    _fields_ = [(n, ctypes.c_ulonglong) for n in (
                        'Read', 'Write', 'Other', 'ReadBytes', 'WriteBytes',
                        'OtherBytes')]

                class Basic(ctypes.Structure):
                    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                                ('PerJobUserTimeLimit', ctypes.c_longlong),
                                ('LimitFlags', wintypes.DWORD),
                                ('MinimumWorkingSetSize', ctypes.c_size_t),
                                ('MaximumWorkingSetSize', ctypes.c_size_t),
                                ('ActiveProcessLimit', wintypes.DWORD),
                                ('Affinity', ctypes.c_size_t),
                                ('PriorityClass', wintypes.DWORD),
                                ('SchedulingClass', wintypes.DWORD)]

                class Extended(ctypes.Structure):
                    _fields_ = [('BasicLimitInformation', Basic),
                                ('IoInfo', IoCounters),
                                ('ProcessMemoryLimit', ctypes.c_size_t),
                                ('JobMemoryLimit', ctypes.c_size_t),
                                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                                ('PeakJobMemoryUsed', ctypes.c_size_t)]
                info = Extended()
                info.BasicLimitInformation.LimitFlags = \
                    self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not k32.SetInformationJobObject(
                        handle, self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                        ctypes.byref(info), ctypes.sizeof(info)):
                    k32.CloseHandle(handle)
                    return
            self.handle, self._k32 = handle, k32
        except Exception:       # a missing export, ctypes.ArgumentError, ...
            self.handle = None

    def add(self, proc):
        """-> True if `proc` (a Popen) is now in the job."""
        if self.handle is None:
            return False
        try:
            return bool(self._k32.AssignProcessToJobObject(
                self.handle, int(proc._handle)))
        except Exception:       # a stand-in process, or no _handle
            return False

    def terminate(self):
        """-> True if every process in the job was told to end."""
        if self.handle is None:
            return False
        try:
            return bool(self._k32.TerminateJobObject(self.handle, 1))
        except Exception:
            return False

    def close(self):
        if self.handle is not None:
            try:
                self._k32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


def spawn_worker(mode, fwdir, *extra, piped=False, kill_on_close=False):
    """Start a worker. -> the Popen, with .digiemu_job (a WorkerJob) when
    Windows put it in one; stop it with stop_worker()."""
    proc = subprocess.Popen(worker_command(mode, fwdir, *extra),
                            **spawn_options(piped))
    job = WorkerJob(kill_on_close)
    if job.add(proc):
        proc.digiemu_job = job
    else:
        job.close()
    return proc


def stop_worker(proc):
    """End a worker and anything it started (see WorkerJob)."""
    job = getattr(proc, 'digiemu_job', None)
    if job is not None and job.terminate():
        return
    proc.terminate()


def disable_power_throttling():
    """Opt this process out of EXECUTION_SPEED power throttling (EcoQoS).

    -> True if Windows accepted it. Failure is harmless (older Windows, or
    not Windows at all): the build is only slower."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class PowerThrottlingState(ctypes.Structure):
            _fields_ = [('Version', wintypes.ULONG),
                        ('ControlMask', wintypes.ULONG),
                        ('StateMask', wintypes.ULONG)]

        process_power_throttling = 4        # PROCESS_INFORMATION_CLASS
        execution_speed = 0x1               # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        # ControlMask says which policy we set; StateMask 0 turns it off.
        state = PowerThrottlingState(1, execution_speed, 0)
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.SetProcessInformation.restype = wintypes.BOOL
        return bool(k32.SetProcessInformation(
            k32.GetCurrentProcess(), process_power_throttling,
            ctypes.byref(state), ctypes.sizeof(state)))
    except Exception:       # ctypes.ArgumentError, a missing export, ...
        return False


# --- checks before a build -----------------------------------------------------

def _card_disk_size(entry):
    """-> the card's allocated size, or its length if that cannot be told."""
    try:
        from emu import sparse          # stdlib only: no Unicorn here
        size = sparse.allocated_size(entry.path)
    except Exception:                   # noqa: BLE001
        size = None
    return size if size is not None else entry.stat(follow_symlinks=False).st_size


def folder_size(path):
    """-> bytes the folder takes on disk. The +Drive card counts at its
    allocated size: it is sparse (~950 MB long, a few MB on disk)."""
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.name == CARD_NAME:
                            total += _card_disk_size(entry)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def human_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024
    return '%.1f TB' % n


def longest_path(fwdir, syx_name):
    """-> the longest path the app creates under a firmware folder.

    The deepest is snapshots/<syx stem>/<longest snapshot name>; the .tmp
    twins count because every output is written beside its final name
    first, and gui.snap.rejected is where a settle that failed acceptance
    goes."""
    stem = os.path.splitext(syx_name)[0]
    try:
        tomls = [n for n in os.listdir(devices_dir()) if n.endswith('.toml')]
    except OSError:
        tomls = []
    toml = max(tomls + ['digitone-ii.toml'], key=len)
    rel = [syx_name + '.tmp', STATE_FILE + '.tmp', 'plusdrive.img.tmp', LOCK_NAME,
           os.path.join('logs', 'first-run.log'), os.path.join('logs', 'panel.log'),
           os.path.join('devices', toml + '.tmp'),
           os.path.join('sections', 'section_3_MAIN_OS.bin.tmp'),
           os.path.join('sections', 'section_4_UPDATER.bin.tmp'),
           os.path.join('sections', '.source-sha256.tmp'),
           os.path.join('snapshots', stem, 'gui-raw.snap.tmp'),
           os.path.join('snapshots', stem, 'gui.snap.rejected'),
           os.path.join('snapshots', stem, 'boot.snap.tmp'),
           os.path.join('snapshots', stem, 'resume.snap.tmp')]
    root = os.path.abspath(fwdir)
    return max((os.path.join(root, r) for r in rel), key=len)


def onedrive_roots(environ=None):
    env = os.environ if environ is None else environ
    out = []
    for key in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
        value = env.get(key) or env.get(key.upper())
        if value and value not in out:
            out.append(value)
    return out


def under_onedrive(path, environ=None):
    """-> the OneDrive root (or a reason) if `path` is synced, else None.

    A synced folder would upload ~1 GB after every session, and a
    dehydrated (cloud-only) card makes the memory map download or fail
    offline."""
    p = os.path.normcase(os.path.abspath(path))
    for root in onedrive_roots(environ):
        r = os.path.normcase(os.path.abspath(root)).rstrip('\\/')
        if p == r or p.startswith(r + os.sep):
            return root
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        attrs = getattr(os.stat(probe), 'st_file_attributes', 0)
    except OSError:
        return None
    # RECALL_ON_DATA_ACCESS, RECALL_ON_OPEN, PINNED, UNPINNED: cloud files.
    if attrs & (0x400000 | 0x40000 | 0x80000 | 0x100000):
        return 'a cloud-synced folder (%s)' % probe
    return None


def is_writable(dirpath):
    try:
        os.makedirs(dirpath, exist_ok=True)
        probe = os.path.join(dirpath, '.digiemu-write-test-%d' % os.getpid())
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.close(fd)
        os.remove(probe)
        return True
    except OSError:
        return False


def _existing_ancestor(path):
    path = os.path.abspath(path)
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def preflight(fwdir, syx_name, home=None):
    """-> (errors, warnings) for creating or resuming a firmware folder."""
    errors, warnings = [], []
    deepest = longest_path(fwdir, syx_name)
    if len(deepest) > MAX_PATH_CHARS:
        errors.append(
            'The folder path is too long: the deepest file digiemu writes would '
            'be %d characters, and Windows only guarantees %d. Move the digiemu '
            'folder somewhere shorter, such as C:\\digiemu.\n\n%s'
            % (len(deepest), MAX_PATH_CHARS, deepest))
    base = app_root(home)
    if not is_writable(firmware_root(home)):
        errors.append(
            'digiemu cannot write to %s. Move the digiemu folder somewhere you '
            'can write to (not Program Files), or use %s.'
            % (base, local_app_home()))
    else:
        try:
            free = shutil.disk_usage(_existing_ancestor(fwdir)).free
        except OSError:
            free = None
        need = max(0, MIN_FREE_BYTES - folder_size(fwdir))
        if free is not None and free < need:
            errors.append('Not enough free disk space: setting up a firmware needs '
                          'about %s, and the drive has %s free.'
                          % (human_size(need), human_size(free)))
    synced = under_onedrive(base)
    if synced:
        warnings.append(
            'The digiemu folder is inside OneDrive (%s). The +Drive image is '
            'about 1 GB and changes every session, so OneDrive will keep '
            'uploading it, and a cloud-only copy fails to open offline. A '
            'folder outside OneDrive, such as C:\\digiemu, is better.' % synced)
    return errors, warnings


# --- adding a firmware ---------------------------------------------------------

@dataclasses.dataclass
class AddPlan:
    source: str
    release: object
    fwdir: str
    syx_name: str
    errors: list
    warnings: list


def release_summary(rel):
    lines = ['Product: %s' % rel.product,
             'Version: %s (build %s)' % (rel.version, rel.build)]
    stamp = getattr(rel, 'stamp', None)
    if isinstance(stamp, datetime.datetime):
        lines.append('Built: %s' % stamp.strftime('%Y-%m-%d %H:%M'))
    lines.append('SHA-256: %s' % rel.sha256)
    return '\n'.join(lines)


def untested_text(rel):
    return ('%s\n\nThis %s release is not one digiemu has been tested with. An '
            'official Elektron release for this product will most likely work; '
            'custom or modified firmware may not boot. Setting it up takes '
            'about a minute.' % (release_summary(rel), rel.product))


def identify_supported(src):
    """-> the release of the .syx at `src`, refused (Refused) when it is not
    an Elektron OS file or not a product this version runs."""
    rmod = _release()
    if not os.path.isfile(src):
        raise Refused('No such file: %s' % src)
    try:
        rel = rmod.identify_release(src, devices_dir())
    except rmod.FirmwareError as exc:
        raise Refused('%s is not an Elektron OS file (%s).'
                      % (os.path.basename(src), exc))
    device = getattr(rel, 'device', None)
    if rel.status == 'unsupported' or device is None:
        raise Refused('%s %s is an Elektron %s firmware, but %s is not supported '
                      'yet. This version runs the %s.'
                      % (rel.product, rel.version, rel.product, rel.product,
                         SUPPORTED), EXIT_UNSUPPORTED)
    if getattr(device, 'short', None) not in PANELS:
        raise Refused('%s %s is recognised, but this version has no panel for the '
                      '%s yet. It runs the %s.'
                      % (rel.product, rel.version, rel.product, SUPPORTED),
                      EXIT_UNSUPPORTED)
    return rel


def plan_add(src, home=None):
    """Identify a .syx and work out where it goes. Refused if it is not an
    Elektron OS file or not a product this version runs."""
    rmod = _release()
    src = os.path.abspath(src)
    rel = identify_supported(src)
    try:
        fwdir = firmware_dir(rel.slug, home)
    except ValueError as exc:
        raise Refused(str(exc))
    syx_name = rmod.canonical_syx_name(rel)
    if os.path.basename(syx_name) != syx_name or not syx_name.lower().endswith('.syx'):
        raise Refused('unusable firmware file name %r' % (syx_name,))
    errors, warnings = preflight(fwdir, syx_name, home)
    return AddPlan(src, rel, fwdir, syx_name, errors, warnings)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def copy_syx(src, dst, sha256):
    """Copy the user's .syx in, verified. -> 'kept' or 'copied'."""
    sha256 = sha256.lower()
    if os.path.exists(dst) and sha256_file(dst) == sha256:
        return 'kept'
    tmp = dst + '.tmp'
    shutil.copyfile(src, tmp)
    got = sha256_file(tmp)
    if got != sha256:
        os.remove(tmp)
        raise OSError('the copy of %s does not match the original (sha256 %s, '
                      'expected %s)' % (os.path.basename(src), got, sha256))
    replace_retry(tmp, dst)
    return 'copied'


def prepare_folder(plan, sink=None):
    """Create the folder, copy the .syx under its canonical name, write the
    devices overlay for an untested release, and record the release.

    -> FirmwarePaths. Holds the folder lock while it writes, so it refuses
    (Busy) a folder a worker is using."""
    rmod, b = _release(), _bootstrap()
    rel = plan.release
    os.makedirs(plan.fwdir, exist_ok=True)
    with FolderLock(plan.fwdir, 'add').acquire(LOCK_WAIT):
        paths = b.FirmwarePaths(root=plan.fwdir, syx_name=plan.syx_name,
                                devices_dir=devices_dir())
        if sink:
            sink({'step': 'copy', 'kind': 'start', 'text': 'copying %s'
                  % os.path.basename(plan.source)})
        how = copy_syx(plan.source, paths.syx, rel.sha256)
        if sink:
            sink({'step': 'copy', 'kind': 'done', 'text': '%s %s'
                  % (how, plan.syx_name)})
        if rel.status == 'untested':
            rmod.write_device_overlay(rel, paths.overlay, devices_dir())
        state = read_state(paths)
        state['release'] = release_record(rel, plan.syx_name)
        state['app_version'] = APP_VERSION
        write_state(paths, state)
    return paths


def refresh_overlay(paths):
    """Rewrite an untested release's devices/ overlay from the bundled
    device file. -> a line for the log, or None when there is nothing to do.

    The overlay is a copy of the shipped device toml plus this release's
    hash. A copy taken when the firmware was added would keep what that app
    version's device file said -- intro policy, audio, labels, panel symbols
    -- through every later update, so each worker writes it again before it
    starts (idempotent, milliseconds). When a later version lists the hash
    itself, the overlay goes and the release is recorded as known. The
    caller holds the folder lock and calls this before apply_env, since the
    overlay's existence decides DT2_DEVICES."""
    state = read_state(paths)
    rec = state.get('release') or {}
    had = os.path.isdir(paths.overlay)
    if rec.get('status') != 'untested' and not had:
        return None
    rmod = _release()
    rel = rmod.identify_release(paths.syx, devices_dir())
    if rel.status == 'untested':
        written = os.path.abspath(
            rmod.write_device_overlay(rel, paths.overlay, devices_dir()))
        # One device file only: a stale copy under an older file name would
        # make two files claim the same device.
        for name in os.listdir(paths.overlay):
            p = os.path.join(paths.overlay, name)
            if name.lower().endswith('.toml') and \
                    os.path.normcase(p) != os.path.normcase(written):
                os.remove(p)
        return 'devices overlay refreshed from the bundled %s' \
            % os.path.basename(written)
    if rel.status == 'known':
        if had:
            shutil.rmtree(paths.overlay)
        state['release'] = release_record(rel, rec.get('syx_name')
                                          or paths.syx_name)
        write_state(paths, state)
        return ('this release is now a known one; the devices overlay was '
                'removed')
    return 'devices overlay kept: %s' % rel.label


def _refresh_overlay_note(paths):
    """refresh_overlay, whose failure must not stop the worker: the overlay
    written last time still works, and a firmware that no longer identifies
    fails later with its own message."""
    try:
        return refresh_overlay(paths)
    except (Exception, SystemExit) as exc:      # noqa: BLE001 -- DeviceError
        return 'devices overlay not refreshed: %s' % describe(exc)


# --- building ------------------------------------------------------------------

def _want_faulthandler():
    """Native crash dumps in the log: always on POSIX, opt-in on Windows.

    On Windows faulthandler's vectored handler sees FIRST-chance exceptions,
    and Unicorn raises and handles access violations of its own while
    mapping guest memory (measured: 11 per Machine, in mem_map). Each one
    would be logged as 'Windows fatal exception' although nothing failed.
    A real crash there still shows as the exit code (describe_exit)."""
    if os.name != 'nt':
        return True
    return os.environ.get('DIGIEMU_FAULTHANDLER', '') not in ('', '0')


def describe_exit(rc):
    """-> a worker's exit code in words, naming Windows crash codes."""
    if rc is None:
        return 'still running'
    if -256 < rc < 0:
        return 'was stopped (signal %d)' % -rc
    code = rc & 0xFFFFFFFF      # NTSTATUS, whether it arrived signed or not
    crashes = {0xC0000005: 'access violation', 0xC00000FD: 'stack overflow',
               0xC0000409: 'stack buffer overrun', 0xC0000017: 'out of memory',
               0xC000001D: 'illegal instruction', 0xC0000374: 'heap corruption'}
    if code in crashes:
        return 'crashed (%s, 0x%08X)' % (crashes[code], code)
    if code >= 0xC0000000:
        return 'crashed (0x%08X)' % code
    return 'exited with code %d' % rc


@contextlib.contextmanager
def _session_output(path):
    """Send this process's prints and tracebacks (and, where they mean
    something, native crash dumps) to a utf-8 log for the duration, and put
    everything back afterwards."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    log = open(path, 'a', encoding='utf-8', errors='replace', newline='\n',
               buffering=1)
    saved = sys.stdout, sys.stderr
    was_faulting = faulthandler.is_enabled()
    sys.stdout = sys.stderr = log
    if _want_faulthandler():
        try:
            faulthandler.enable(file=log, all_threads=True)
        except (OSError, ValueError, RuntimeError, AttributeError):
            pass
    try:
        yield log
    finally:
        try:
            faulthandler.disable()
            if was_faulting and saved[1] is not None:
                faulthandler.enable(file=saved[1])
        except (OSError, ValueError, RuntimeError, AttributeError):
            pass
        sys.stdout, sys.stderr = saved
        log.close()


def _append_log(fwdir, name, text):
    """Best-effort line in <fwdir>/logs/<name>, for failures that happen
    before a session log is open (a windowed exe has no stderr to show)."""
    try:
        d = os.path.join(os.path.abspath(fwdir), 'logs')
        if not os.path.isdir(os.path.dirname(d)):
            return
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), 'a', encoding='utf-8', errors='replace',
                  newline='\n') as fh:
            fh.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'),
                                  text.rstrip('\n')))
    except OSError:
        pass


def _log_header(log, what, paths):
    log.write('\n=== %s %s  digiemu %s  python %s  %s\n'
              % (time.strftime('%Y-%m-%d %H:%M:%S'), what, APP_VERSION,
                 sys.version.split()[0], 'frozen' if is_frozen() else 'source'))
    log.write('folder %s\n' % paths.root)
    for key, value in sorted(paths.env().items()):
        log.write('  %s=%s\n' % (key, value))


def invalidate_build(paths):
    """Forget the snapshots so the next first run boots the card as it is now.

    Only the ladder, intro and settle go: the sections and above all the
    card are kept -- the card is the user's projects and samples. A settle
    stamp about to go vouched that first boot finished on this card; the
    first_boot marker keeps saying so (emu.bootstrap's sector-0 guard reads
    it), also for a folder settled before the marker existed."""
    state = read_state(paths)
    stages = state.get('stages')
    if isinstance(stages, dict):
        if isinstance(stages.get('settle'), dict):
            state.setdefault('first_boot', {
                'at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'backfilled': True})
        for name in ('ladder', 'intro', 'settle'):
            stages.pop(name, None)
    for key in ('card', 'resume', 'incompatible'):
        state.pop(key, None)
    write_state(paths, state)
    if os.path.isdir(paths.snapdir):
        shutil.rmtree(paths.snapdir)


def reset_firmware(fwdir, home=None):
    """Reset to factory: delete the +Drive card, every snapshot and the
    stamps that vouch for them, so the next first run starts as if the
    firmware had just been added. -> FirmwarePaths.

    The .syx, the sections and the logs stay. Under the folder lock, and
    refused (Busy) while anything uses the folder. The stamps go first, so
    an interrupted reset never leaves one vouching for a card that is gone;
    a new card is created and formatted by the first run (which also means
    no half-initialised card from before is ever booted again)."""
    fwdir = own_folder(fwdir, home)
    with FolderLock(fwdir, 'reset').acquire(0):
        paths = open_paths(fwdir)
        state = read_state(paths)
        for key in ('stages', 'card', 'resume', 'incompatible', 'first_boot'):
            state.pop(key, None)
        write_state(paths, state)
        if os.path.isdir(paths.snapshots):
            shutil.rmtree(paths.snapshots)
        for p in (paths.card + '.tmp', paths.card):
            if os.path.exists(p):
                _retry_sharing(os.remove, p)
    return paths


def run_build(paths, sink, rebuild=False, note=None):
    """Run emu.bootstrap.first_run in this process, with its output in
    logs/first-run.log and each Event passed to `sink` as a record.

    The environment must already hold paths.env() (apply_env) and the caller
    must hold the folder lock. `note` goes into the log after the header.
    -> (ok, error, failed_step)."""
    model = ProgressModel()

    with _session_output(os.path.join(paths.logs, 'first-run.log')) as log:
        _log_header(log, 'rebuild' if rebuild else 'first run', paths)
        if note:
            log.write(note + '\n')

        def progress(ev):
            rec = event_record(ev)
            model.feed(rec)
            try:
                log.write('%s %-7s %-5s %s\n' % (time.strftime('%H:%M:%S'),
                                                rec['step'], rec['kind'],
                                                rec['text']))
            except (OSError, ValueError):
                pass
            sink(rec)

        try:
            b = _bootstrap()
            log.write('power throttling off: %s\n' % disable_power_throttling())
            if rebuild:
                invalidate_build(paths)
                log.write('rebuild: snapshots cleared, card kept\n')
            gui = b.first_run(paths, progress=progress)
            state = read_state(paths)
            state['app_version'] = APP_VERSION
            write_state(paths, state)
            log.write('first run complete: %s\n' % gui)
            return True, None, None
        except BaseException as exc:    # NotFound/DeviceError are SystemExit
            log.write(traceback.format_exc())
            step = getattr(exc, 'step', None) or model.step
            return False, describe(exc), step


def worker_first_run(fwdir, rebuild=False, proto=None):
    """--worker first-run: build one folder, JSON lines on `proto`.

    -> 0 if the folder is ready, else 1. The last line is always the result
    record, whatever went wrong."""
    proto = sys.stdout if proto is None else proto
    emit = LineEmitter(proto)
    lock = None
    ok, error, step = False, None, None
    try:
        # The lock before firmware.json is read: another worker may be
        # renaming it right now.
        lock = FolderLock(_existing_folder(fwdir), 'first-run').acquire(LOCK_WAIT)
        paths = open_paths(fwdir)
        note = _refresh_overlay_note(paths)
        apply_env(paths)
        ok, error, step = run_build(paths, emit, rebuild=rebuild, note=note)
    except BaseException as exc:
        ok, error = False, describe(exc)
        _append_log(fwdir, 'first-run.log', 'worker failed before the build: %s\n%s'
                    % (error, traceback.format_exc()))
    finally:
        if lock is not None:
            lock.release()
    emit(result_record(ok, error, step, os.path.join('logs', 'first-run.log')))
    return EXIT_OK if ok else EXIT_FAILED


def _existing_folder(fwdir):
    root = os.path.abspath(fwdir)
    if not os.path.isdir(root):
        raise FolderError('No firmware folder at %s' % root)
    return root


def _file_sig(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _incompatible_marker(snap):
    """-> what firmware.json records when the panel found `snap` was made by
    another build. It names that exact file (size and mtime), so any rebuild,
    which writes new snapshots, retires it without being told."""
    sig = _file_sig(snap)
    return {'snapshot': os.path.basename(snap),
            'sig': list(sig) if sig else None, 'app_version': APP_VERSION}


def _marked_incompatible(state, snap):
    mark = state.get('incompatible')
    if not isinstance(mark, dict) or not snap:
        return False
    sig = _file_sig(snap)
    return (mark.get('snapshot') == os.path.basename(snap)
            and mark.get('app_version') == APP_VERSION
            and sig is not None and mark.get('sig') == list(sig))


def _run_panel(paths, b, snap, log, module='emu.dtpanel'):
    """Run the device's panel (`module`, from PANELS: emu.dtpanel or
    emu.dnpanel, which share main()'s arguments and codes) on `snap`, then
    record in firmware.json what the session left behind. -> (the panel's
    return code, incompatible?).

    'resume' is the card stamp resume.snap was saved against:
      - a clean exit that wrote resume.snap: the card as it is now;
      - rc 2 (the card was flushed, the session not saved), or any other
        exit while the card moved: dropped -- the old resume.snap no
        longer describes that card;
      - dtpanel's LOAD_FAILED on resume.snap (damaged, truncated): dropped,
        so the next Play falls back to gui.snap, or offers a rebuild if the
        card has moved on since -- instead of failing on it every time;
      - dtpanel's SAMPLES_ADDED: dropped. The session was saved, but then
        samples went onto the card, and only a rebuild can show them;
      - anything else, e.g. a halt (the emulator neither flushes nor saves
        then): kept. The last session and the card still agree, and
        dropping it would turn the next Play into a rebuild.
    The card is compared by its stamp before and after the session, not
    against the old 'resume': a first session starts from gui.snap."""
    incompat_code = DTPANEL_INCOMPATIBLE
    card_before = b.card_stamp(paths.card)
    before = _file_sig(paths.resume)
    try:
        panel = _panel_module(module)
        incompat_code = getattr(panel, 'INCOMPATIBLE', DTPANEL_INCOMPATIBLE)
        rc = panel.main([snap, '--syx', paths.syx, '--save-on-exit',
                         paths.resume, '--app'])
        rc = EXIT_OK if rc is None else int(rc)
    except BaseException:
        log.write(traceback.format_exc())
        rc = EXIT_FAILED
    after = _file_sig(paths.resume)
    written = after is not None and after != before
    card_after = b.card_stamp(paths.card)
    moved = card_after != card_before
    incompatible = rc == incompat_code
    unloadable = (rc == DTPANEL_LOAD_FAILED
                  and os.path.normcase(os.path.abspath(snap))
                  == os.path.normcase(os.path.abspath(paths.resume)))
    try:
        state = read_state(paths)
        if rc == EXIT_OK and written:
            state['resume'] = card_after
            what = 'recorded the card for resume.snap'
        elif unloadable:
            state.pop('resume', None)
            what = ('dropped the resume stamp (resume.snap would not open; '
                    'the next Play starts from the last full setup)')
        elif rc == DTPANEL_SAMPLES_ADDED:
            state.pop('resume', None)
            what = ('dropped the resume stamp (samples were loaded onto the '
                    'card: rebuild)')
        elif rc == DTPANEL_NOT_SAVED or moved:
            state.pop('resume', None)
            what = 'dropped the resume stamp (%s)' % (
                'the session was not saved' if rc == DTPANEL_NOT_SAVED
                else 'the card changed without a saved session')
        else:
            what = 'kept the resume stamp (the card did not change)'
        if incompatible:
            state['incompatible'] = _incompatible_marker(snap)
            what += '; %s is from another build: rebuild needed' \
                % os.path.basename(snap)
        elif rc == EXIT_OK:
            state.pop('incompatible', None)
        write_state(paths, state)
    except (OSError, ValueError) as exc:
        what = 'could not update %s: %s' % (STATE_FILE, describe(exc))
    log.write('panel exited %d; resume.snap %s; card %s; %s\n'
              % (rc, 'saved' if written else 'not saved',
                 'changed' if moved else 'unchanged', what))
    return rc, incompatible


def worker_panel(fwdir, err=None):
    """--worker panel: open this folder's panel on the right snapshot.

    -> dtpanel's exit code (0 clean, 1 emulator failed, 2 saving failed),
    EXIT_INCOMPATIBLE for dtpanel's INCOMPATIBLE (the snapshot is from
    another build: rebuild), EXIT_SAMPLES_ADDED for its SAMPLES_ADDED
    (samples went onto the card: rebuild, then reopen), or EXIT_NOT_READY
    with the reason
    ('card-changed', 'not-built') on stderr and in logs/panel.log,
    EXIT_BUSY, EXIT_UNSUPPORTED."""
    err = sys.stderr if err is None else err
    lock = None
    try:
        lock = FolderLock(_existing_folder(fwdir), 'panel').acquire(LOCK_WAIT)
        paths = open_paths(fwdir)
        b = _bootstrap()
        rel = read_state(paths).get('release') or {}
        short = rel.get('device')
        if short not in PANELS:
            msg = ('No panel for %s in this version (device %r).'
                   % (rel.get('label') or os.path.basename(paths.root), short))
            _say(err, msg)
            _append_log(fwdir, 'panel.log', msg)
            return EXIT_UNSUPPORTED
        note = _refresh_overlay_note(paths)
        apply_env(paths)
        with _session_output(os.path.join(paths.logs, 'panel.log')) as log:
            _log_header(log, 'panel', paths)
            if note:
                log.write(note + '\n')
            snap, reason = b.choose_snapshot(paths)
            if snap is None:
                msg = 'not ready: %s' % reason
                log.write(msg + '\n')
                _say(err, msg)
                return EXIT_NOT_READY
            log.write('snapshot %s\n' % snap)
            log.write('power throttling off: %s\n' % disable_power_throttling())
            rc, incompatible = _run_panel(paths, b, snap, log,
                                          module=PANELS[short])
            if incompatible:
                return EXIT_INCOMPATIBLE
            if rc == DTPANEL_SAMPLES_ADDED:
                return EXIT_SAMPLES_ADDED
            # dtpanel's 5 would read as EXIT_BUSY here; it is a failure.
            return EXIT_FAILED if rc == DTPANEL_LOAD_FAILED else rc
    except Busy as exc:
        _say(err, str(exc))
        _append_log(fwdir, 'panel.log', str(exc))
        return EXIT_BUSY
    except BaseException as exc:
        _say(err, describe(exc))
        _append_log(fwdir, 'panel.log', 'panel worker failed: %s\n%s'
                    % (describe(exc), traceback.format_exc()))
        return EXIT_FAILED
    finally:
        if lock is not None:
            lock.release()


def _build_in_process(paths, progress, rebuild=False):
    """The first run in this process, for when no worker can be spawned.
    -> (ok, error, step); Busy or OSError when the folder cannot be taken."""
    cwd = os.getcwd()
    lock = None
    try:
        lock = FolderLock(paths.root, 'first-run').acquire(LOCK_WAIT)
        note = _refresh_overlay_note(paths)
        apply_env(paths)
        return run_build(paths, progress, rebuild=rebuild, note=note)
    finally:
        if lock is not None:
            lock.release()
        try:
            os.chdir(cwd)
        except OSError:
            pass


def build_in_worker(paths, label, out=None, rebuild=False):
    """Set a folder up through the same first-run worker the launcher uses,
    relaying its JSON lines as console progress. -> the exit code.

    A child, not this process, because the ladder runs guest code in a
    native DLL: if it dies (0xC0000409 and the like) the child's exit code
    still says so, with the log's path, where an in-process build would
    just stop mid-line. Only when no child can be started at all does the
    build run here."""
    out = sys.stdout if out is None else out
    progress = ConsoleProgress(out)
    log_path = os.path.join(paths.logs, 'first-run.log')
    extra = ('--rebuild',) if rebuild else ()
    try:
        # kill_on_close: closing this console (or killing this process) must
        # not leave an invisible build holding the folder lock.
        proc = spawn_worker('first-run', paths.root, *extra, piped=True,
                            kill_on_close=True)
    except OSError as exc:
        progress.say('note: cannot start a worker process (%s); setting up in '
                     'this one' % describe(exc))
        try:
            ok, error, step = _build_in_process(paths, progress, rebuild)
        except Busy as exc2:
            progress.say(str(exc2))
            return EXIT_BUSY
        except OSError as exc2:
            progress.say('error: ' + describe(exc2))
            return EXIT_FAILED
        result, rc = result_record(ok, error, step), EXIT_OK if ok else EXIT_FAILED
    else:
        try:
            result, rc = _relay_worker(proc, progress)
        finally:
            job = getattr(proc, 'digiemu_job', None)
            if job is not None:
                job.close()
        if rc is None:
            progress.say('Stopped. Running the same command again resumes '
                         'where it stopped.')
            return EXIT_FAILED
    if result is not None and result.get('ok') and rc == EXIT_OK:
        progress.say('%s is ready.' % label)
        return EXIT_OK
    if result is None:
        why = describe_exit(rc)
        _append_log(paths.root, 'first-run.log',
                    'the first-run worker %s without reporting a result' % why)
        progress.say('Setup failed: the setup process %s.' % why)
        if (rc & 0xFFFFFFFF) >= 0xC0000000 and not _want_faulthandler():
            progress.say('For crash details in the log, run it again with '
                         'DIGIEMU_FAULTHANDLER=1 set.')
    else:
        step = result.get('step')
        progress.say('Setup failed%s: %s'
                     % (' at %s' % step if step else '',
                        result.get('error') or describe_exit(rc)))
    progress.say('Log: %s' % log_path)
    progress.say('Running the same command again resumes where it stopped.')
    return EXIT_FAILED


def _relay_worker(proc, progress):
    """Feed a first-run worker's lines to `progress` until it exits.
    -> (the result record or None, its exit code); rc None when Ctrl-C
    stopped it (the worker has no console of its own to receive it)."""
    result = None
    try:
        for raw in iter(proc.stdout.readline, b''):
            rec = decode_line(raw)
            if rec is None:
                text = raw.decode('utf-8', 'replace').rstrip()
                if text.strip():
                    progress.say(text)
            elif rec.get('kind') == 'result':
                result = rec
            else:
                progress(rec)
        return result, proc.wait()
    except KeyboardInterrupt:
        try:
            stop_worker(proc)
            proc.wait(30)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return result, None
    finally:
        try:
            proc.stdout.close()
        except (OSError, ValueError, AttributeError):
            pass


def _rebuild_hint(slug, home=None):
    return '%s --rebuild %s' % (cli_prefix(home), slug)


def add_firmware(syx, yes=False, home=None, out=None):
    """--add: identify, create the folder, copy, and build in a worker.

    A folder that is already Ready, open, or waiting for a Rebuild is left
    alone: setting it up again would rerun the cold boot and throw away its
    saved session. -> 0 ('already set up' included), 3 for Rebuild needed."""
    out = sys.stdout if out is None else out
    try:
        plan = plan_add(syx, home)
    except Refused as exc:
        _say(out, str(exc))
        return exc.code
    rel = plan.release
    _say(out, '%s  ->  %s' % (rel.label, plan.fwdir))
    if os.path.isdir(plan.fwdir):
        info = status_of(plan.fwdir, label=rel.label)
        st = info['status']
        if st in (READY, RUNNING, REBUILD):
            _say(out, 'already set up: %s (%s)' % (rel.label, st))
            if st != REBUILD:
                return EXIT_OK
            _say(out, 'Its saved state no longer matches (%s). To boot it '
                 'again from its +Drive card (kept), run:\n  %s'
                 % (info['reason'] or st, _rebuild_hint(rel.slug, home)))
            return EXIT_NOT_READY
        if st == BUILDING:
            _say(out, 'It is being set up by another digiemu process (%s).'
                 % info['reason'])
            return EXIT_BUSY
    if rel.status == 'untested' and not yes:
        _say(out, untested_text(rel))
        _say(out, '\nRun again with --yes to set it up anyway.')
        return EXIT_NEEDS_YES
    for w in plan.warnings:
        _say(out, 'warning: ' + w)
    if plan.errors:
        for e in plan.errors:
            _say(out, 'error: ' + e)
        return EXIT_FAILED
    progress = ConsoleProgress(out)
    try:
        paths = prepare_folder(plan, progress)
    except Busy as exc:
        _say(out, str(exc))
        return EXIT_BUSY
    except (OSError, ValueError) as exc:
        _say(out, 'error: ' + describe(exc))
        return EXIT_FAILED
    return build_in_worker(paths, rel.label, out)


def rebuild_firmware(name, home=None, out=None):
    """--rebuild NAME: boot a firmware that is already here again.

    Ready or Rebuild needed: its snapshots go (the card is kept) and the
    first run rebuilds them, as the launcher's Rebuild does. Needs setup:
    the first run resumes where it stopped. -> the exit code."""
    out = sys.stdout if out is None else out
    try:
        fwdir = find_folder(name, home)
    except FolderError as exc:
        _say(out, str(exc))
        return EXIT_USAGE
    info = status_of(fwdir)
    st = info['status']
    if st in (BUILDING, RUNNING):
        _say(out, '%s is in use (%s). Close it first, or wait for it to finish.'
             % (info['label'], info['reason']))
        return EXIT_BUSY
    if st == UNSUPPORTED:
        _say(out, '%s: %s.' % (info['label'], info['reason']))
        return EXIT_UNSUPPORTED
    try:
        paths = open_paths(fwdir)
    except (Exception, SystemExit) as exc:          # noqa: BLE001
        _say(out, 'error: %s' % describe(exc))
        return EXIT_FAILED
    rebuild = st in (READY, REBUILD)
    _say(out, '%s %s  ->  %s' % ('Rebuilding' if rebuild else 'Setting up',
                                 info['label'], fwdir))
    return build_in_worker(paths, info['label'], out, rebuild=rebuild)


def reset_cli(name, yes=False, home=None, out=None):
    """--reset NAME [--yes]: reset a firmware to factory (reset_firmware),
    then say how to set it up again. Without --yes it only says what would
    go: the card holds the user's projects and samples."""
    out = sys.stdout if out is None else out
    try:
        fwdir = find_folder(name, home)
    except FolderError as exc:
        _say(out, str(exc))
        return EXIT_USAGE
    info = status_of(fwdir)
    slug = os.path.basename(fwdir)
    if not yes:
        _say(out, 'Resetting %s to factory deletes its +Drive image -- every '
             'project and sample saved on it -- and its saved sessions, in %s. '
             'It cannot be undone.\n\nRun again with --yes to reset it.'
             % (info['label'], fwdir))
        return EXIT_NEEDS_YES
    try:
        reset_firmware(fwdir, home)
    except Busy as exc:
        _say(out, str(exc))
        return EXIT_BUSY
    except (Exception, SystemExit) as exc:          # noqa: BLE001
        _say(out, 'error: %s' % describe(exc))
        return EXIT_FAILED
    _say(out, '%s was reset to factory: the +Drive image and the saved '
         'sessions are gone.' % info['label'])
    _say(out, 'Set it up again (about a minute) with:\n  %s'
         % _rebuild_hint(slug, home))
    return EXIT_OK


# --- status of the folders -----------------------------------------------------

def _busy_status(holder):
    purpose = holder.split()[0] if holder.split() else ''
    return BUILDING if purpose in ('first-run', 'add') else RUNNING


def status_of(fwdir, running=None, label=None):
    """-> {'fwdir','slug','label','status','reason','size','device',
    'app_version'} for the list. `running` is 'first-run'/'panel' when the
    caller already knows a worker of its own holds the folder.

    A folder that a worker holds is never read: its status comes from the
    lock ('first-run pid N' is Building, anything else Running) and its
    label is `label` or the folder name. On Windows the worker's atomic
    rename of firmware.json fails while any other process has the file
    open, so --list and a launcher refreshing every few seconds must keep
    their hands off it.

    Otherwise: Ready, Needs setup or Rebuild needed from choose_snapshot --
    Rebuild needed also when the panel found the snapshot it would open was
    made by another build (reason INCOMPATIBLE_REASON)."""
    info = {'fwdir': os.path.abspath(fwdir), 'slug': os.path.basename(fwdir),
            'label': label or os.path.basename(fwdir), 'status': NEEDS_SETUP,
            'reason': '', 'size': folder_size(fwdir), 'device': None,
            'app_version': None}
    holder = running or lock_holder(fwdir)
    if holder:
        info.update(status=_busy_status(holder), reason=holder)
        return info
    try:
        paths = open_paths(fwdir)
        state = read_state(paths)
    except BaseException as exc:
        info.update(status=BROKEN, reason=describe(exc))
        return info
    rel = state.get('release') or {}
    info['label'] = rel.get('label') or info['slug']
    info['device'] = rel.get('device')
    info['app_version'] = state.get('app_version')
    if not os.path.exists(paths.syx):
        info.update(status=BROKEN, reason='the firmware file %s is missing'
                    % os.path.basename(paths.syx))
        return info
    if info['device'] not in PANELS:
        info.update(status=UNSUPPORTED,
                    reason='no panel for this product in this version')
        return info
    try:
        snap, reason = _bootstrap().choose_snapshot(paths)
    except BaseException as exc:
        info.update(status=BROKEN, reason=describe(exc))
        return info
    if snap and _marked_incompatible(state, snap):
        info.update(status=REBUILD, reason=INCOMPATIBLE_REASON)
    elif snap:
        info['status'] = READY
    elif reason == 'card-changed':
        info.update(status=REBUILD, reason=reason)
    else:
        info.update(status=NEEDS_SETUP, reason=reason or '')
    return info


def list_firmware(home=None, out=None):
    out = sys.stdout if out is None else out
    dirs = list_firmware_dirs(home)
    if not dirs:
        _say(out, 'No firmware set up in %s. Add one with --add FILE.syx.'
             % firmware_root(home))
        return EXIT_OK
    for d in dirs:
        info = status_of(d)
        _say(out, '%-28s %-24s %-15s %9s%s'
             % (info['slug'], info['label'], info['status'],
                human_size(info['size']),
                '  (%s)' % info['reason'] if info['reason'] else ''))
    return EXIT_OK


def remove_firmware(fwdir, home=None):
    """Delete one firmware folder, card included. Only a slug-named folder
    directly under this APP_ROOT's firmware/, and only while no worker
    holds it."""
    fwdir = own_folder(fwdir, home)
    lock = FolderLock(fwdir, 'remove').acquire(0)
    try:
        for name in os.listdir(fwdir):
            if name == LOCK_NAME:
                continue
            p = os.path.join(fwdir, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    finally:
        lock.release()
    os.remove(os.path.join(fwdir, LOCK_NAME))
    os.rmdir(fwdir)


# --- the self-test, in a child ---------------------------------------------------

def selftest_command(json_path):
    """-> argv for `--selftest --json json_path` (packaging/digiemu_main.py).
    Frozen, the exe is the entry script; from source, run the script."""
    if is_frozen():
        return [sys.executable, '--selftest', '--json', json_path]
    return [sys.executable, os.path.join(REPO, 'packaging', 'digiemu_main.py'),
            '--selftest', '--json', json_path]


def _check_title(name):
    return '%s (%s)' % (SELFTEST_CHECKS[name], name) if name in SELFTEST_CHECKS \
        else repr(name)


def summarize_selftest(report, rc, stderr='', timed_out=False,
                       timeout=SELFTEST_TIMEOUT):
    """-> {'ok', 'message', 'failed', 'running', 'rc', 'report', 'stderr'}
    from the child's JSON report and exit code.

    The report is rewritten before each check with 'running' naming it, so
    a child that crashed leaves the check that killed it; a finished one
    has running None and each check's ok/detail under 'checks'."""
    report = report if isinstance(report, dict) else None
    checks = [c for c in (report or {}).get('checks') or [] if isinstance(c, dict)]
    failed = [c for c in checks if not c.get('ok')]
    running = (report or {}).get('running')
    out = {'ok': False, 'rc': rc, 'running': running, 'report': report,
           'failed': [c.get('name') for c in failed],
           'stderr': '\n'.join(str(stderr or '').splitlines()[-20:])}
    if timed_out:
        msg = 'The self-test did not finish within %d s%s.' % (
            timeout, ' (it was checking %s)' % _check_title(running)
            if running else '')
    elif report is not None and report.get('ok') and rc == 0 and not failed:
        out['ok'] = True
        msg = 'The self-test passed.'
    elif running:
        msg = 'The self-test %s while checking %s.' % (describe_exit(rc),
                                                       _check_title(running))
    elif failed:
        msg = 'The self-test failed: %s' % '; '.join(
            '%s: %s' % (_check_title(c.get('name')),
                        str(c.get('detail') or 'failed')[:300]) for c in failed)
    elif report is None:
        msg = 'The self-test %s without writing its report.' % describe_exit(rc)
    else:
        msg = 'The self-test %s.' % describe_exit(rc)
    out['message'] = msg
    return out


def run_selftest(timeout=SELFTEST_TIMEOUT):
    """Run the self-test in a CHILD process -> summarize_selftest's dict.

    A child because its last check runs guest code in the patched
    unicorn.dll, and a DLL that crashes takes its process with it; the
    launcher never loads Unicorn at all. The report goes to a temp file."""
    with tempfile.TemporaryDirectory(prefix='digiemu-selftest-',
                                     ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, 'selftest.json')
        kw = spawn_options(False)
        kw['stderr'] = subprocess.PIPE
        timed_out = False
        try:
            proc = subprocess.run(selftest_command(path), timeout=timeout, **kw)
            rc, err = proc.returncode, proc.stderr
        except subprocess.TimeoutExpired as exc:
            rc, err, timed_out = None, exc.stderr, True
        except OSError as exc:
            return dict(summarize_selftest(None, None),
                        message='The self-test could not be started: %s'
                        % describe(exc))
        try:
            with open(path, 'rb') as fh:
                report = json.loads(fh.read().decode('utf-8'))
        except (OSError, ValueError):
            report = None
    if isinstance(err, bytes):
        err = err.decode('utf-8', 'replace')
    return summarize_selftest(report, rc, err or '', timed_out, timeout)


# --- the firmware check ----------------------------------------------------------
#
# emu.fwcheck runs a build the way the device would -- its container, its own
# bootloader, a strict cold boot, a scripted session -- and, given the stock
# build, the same on that and a comparison. It runs Unicorn for minutes, so
# like a build it runs in a worker, `--worker check <checkdir>`, reading the
# request the launcher wrote there and writing its report next to it:
#
#     <APP_ROOT>/checks/<time>-<file>/request.json   what to check
#                                     check.log      everything it printed
#                                     report.json    every fact (emu.fwcheck)
#                                     summary.txt    the verdict in lines
#                                     compare/       screens that differ
#
# Each build's work folder (a .syx copy, sections, a card image and
# snapshots: over a gigabyte) goes as soon as that build is checked.

@dataclasses.dataclass
class CheckPlan:
    source: str
    release: object
    baselines: list     # baseline_candidates
    errors: list


def checks_root(home=None):
    return os.path.join(app_root(home), 'checks')


def baseline_candidates(rel, home=None):
    """-> the firmware set up here that can stand for the stock build when
    checking `rel`: the same product, a release digiemu knows (an untested
    one may itself be custom), its .syx present. The same version first.
    Each {'label', 'syx', 'version', 'same_version'}.

    A folder a worker holds is left out: its firmware.json is not read
    while a worker may be renaming it (status_of)."""
    short = getattr(getattr(rel, 'device', None), 'short', None)
    out = []
    for d in list_firmware_dirs(home):
        if lock_holder(d):
            continue
        try:
            paths = open_paths(d)
            state = read_state(paths)
        except BaseException:                   # noqa: BLE001 -- a broken folder
            continue
        r = state.get('release') or {}
        if r.get('device') != short or r.get('status') in ('untested',
                                                           'unsupported'):
            continue
        if not os.path.isfile(paths.syx):
            continue
        out.append({'label': r.get('label') or os.path.basename(d),
                    'syx': paths.syx, 'version': r.get('version'),
                    'same_version': r.get('version') == rel.version})
    out.sort(key=lambda c: (not c['same_version'], c['label']))
    return out


def check_preflight(home=None):
    """-> errors that stop a check before it starts: nowhere to write, or
    too little room for one build's work folder."""
    root = checks_root(home)
    if not is_writable(root):
        return ['digiemu cannot write to %s. Move the digiemu folder somewhere '
                'you can write to (not Program Files), or use %s.'
                % (app_root(home), local_app_home())]
    try:
        free = shutil.disk_usage(_existing_ancestor(root)).free
    except OSError:
        return []
    if free < MIN_FREE_BYTES:
        return ['Not enough free disk space: a check needs about %s while it '
                'runs, and the drive has %s free.'
                % (human_size(MIN_FREE_BYTES), human_size(free))]
    return []


def plan_check(src, home=None):
    """Identify a build to check. Refused (Refused) like Add: the check
    boots it, so it must be a product this version runs."""
    src = os.path.abspath(src)
    rel = identify_supported(src)
    return CheckPlan(src, rel, baseline_candidates(rel, home),
                     check_preflight(home))


def new_check_dir(src, home=None, stamp=None):
    """-> a new, empty folder for one check: checks/<time>-<file name>."""
    stamp = stamp or time.strftime('%Y%m%d-%H%M%S')
    stem = os.path.splitext(os.path.basename(src))[0].lower()
    stem = re.sub(r'[^a-z0-9.-]+', '-', stem).strip('-.')[:40] or 'build'
    base = os.path.join(checks_root(home), '%s-%s' % (stamp, stem))
    path, n = base, 1
    while True:
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            n += 1
            path = '%s-%d' % (base, n)


def write_check_request(checkdir, syx, baseline, timing, device):
    with open(os.path.join(checkdir, CHECK_REQUEST), 'w', encoding='utf-8',
              newline='\n') as fh:
        json.dump({'syx': os.path.abspath(syx),
                   'baseline': os.path.abspath(baseline) if baseline else None,
                   'timing': bool(timing), 'device': device,
                   'app_version': APP_VERSION}, fh, indent=1)


def check_plan_seconds(device, baseline, timing):
    """-> [((role, stage), expected seconds)] in the order a check runs."""
    secs = CHECK_SECONDS.get(device) or CHECK_SECONDS['dt1']
    roles = (('baseline',) if baseline else ()) + ('build',)
    plan = [((role, stage), secs['run-timed' if timing and stage == 'run'
                                 else stage])
            for role in roles for stage in CHECK_STAGES]
    if baseline:
        plan.append((('compare', 'compare'), secs['compare']))
    return plan


def check_minutes(device, baseline, timing):
    """-> the expected length of a check, in whole minutes."""
    total = sum(s for _key, s in check_plan_seconds(device, baseline, timing))
    return max(1, int(round(total / 60.0)))


class CheckProgress:
    """A check's records -> where it is, an overall fraction and each
    stage's verdict so far.

    The fraction goes by expected seconds (CHECK_SECONDS). Inside the stage
    running now it creeps on with the clock, to at most 95% of that stage's
    share, so a two-minute boot does not look stuck. It never goes back."""

    def __init__(self, device, baseline, timing):
        plan = check_plan_seconds(device, baseline, timing)
        self.keys = [key for key, _s in plan]
        self.weights = [s for _key, s in plan]
        self.total = float(sum(self.weights))
        self.done = 0.0
        self.current = None             # (index, started) of the running stage
        self.role = self.step = None
        self.verdicts = []              # (role, stage, state, passed)
        self.result = None

    def feed(self, rec, now=None):
        now = time.monotonic() if now is None else now
        kind = rec.get('kind')
        if kind == 'result':
            self.result = rec
            return
        data = rec.get('data') if isinstance(rec.get('data'), dict) else {}
        key = (data.get('role'), rec.get('step'))
        if key not in self.keys:
            return
        i = self.keys.index(key)
        if kind == 'start':
            self.done = max(self.done, sum(self.weights[:i]))
            self.current = (i, now)
            self.role, self.step = key
        elif kind == 'done':
            self.done = max(self.done, sum(self.weights[:i + 1]))
            self.current = None
            self.verdicts.append((key[0], key[1], str(data.get('state') or ''),
                                  bool(data.get('passed'))))

    def fraction(self, now=None):
        if self.result is not None and self.result.get('ok'):
            return 1.0
        done = self.done
        if self.current is not None:
            i, started = self.current
            now = time.monotonic() if now is None else now
            done = max(done, sum(self.weights[:i])
                       + min(max(0.0, now - started), 0.95 * self.weights[i]))
        return min(1.0, done / self.total)


def worker_check(checkdir, proto=None):
    """--worker check: run emu.fwcheck on the request in `checkdir`, JSON
    lines on `proto`.

    Each fwcheck record goes out as {'kind', 'step', 'text', 'data':
    {'role', 'state', 'passed'}}. The last line is the result: ok when the
    check ran to its end, whatever it found; the verdict is data['passed']
    and data['summary'] its lines. -> 0 when the check ran to its end, else
    1. What the emulator prints goes to check.log, as in a build."""
    proto = sys.stdout if proto is None else proto
    emit = LineEmitter(proto)
    root = os.path.abspath(checkdir)
    ok, error, data = False, None, {}
    try:
        request = os.path.join(root, CHECK_REQUEST)
        if not os.path.isfile(request):
            raise FolderError('No firmware check at %s' % root)
        with open(request, encoding='utf-8') as fh:
            req = json.load(fh)
        os.environ['DIGIEMU_DEVICES'] = devices_dir()
        os.environ['PYTHONUTF8'] = '1'
        os.environ['PYTHONIOENCODING'] = 'utf-8'
        os.chdir(root)
        with _session_output(os.path.join(root, CHECK_LOG)) as log:
            log.write('\n=== %s firmware check  digiemu %s  python %s  %s\n'
                      % (time.strftime('%Y-%m-%d %H:%M:%S'), APP_VERSION,
                         sys.version.split()[0],
                         'frozen' if is_frozen() else 'source'))
            log.write('build %s\nstock %s\ntiming %s\n'
                      % (req.get('syx'), req.get('baseline') or '(none)',
                         bool(req.get('timing'))))
            log.write('power throttling off: %s\n' % disable_power_throttling())

            def say(text):
                log.write('%s\n' % text)

            def on_event(role, rec):
                emit({'kind': str(rec.get('kind') or ''),
                      'step': str(rec.get('step') or ''),
                      'text': str(rec.get('text') or ''),
                      'data': {'role': role, 'state': rec.get('state'),
                               'passed': rec.get('passed')}})

            try:
                full = _fwcheck().run_check(
                    req['syx'], root, baseline=req.get('baseline') or None,
                    timing=bool(req.get('timing')), keep_work=False, log=say,
                    on_event=on_event)
            except BaseException:
                log.write(traceback.format_exc())
                raise
            lines = [str(x) for x in full.get('summary') or ()]
            log.write('\n'.join(lines) + '\n')
        with open(os.path.join(root, CHECK_SUMMARY), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write('\n'.join(lines) + '\n')
        ok = True
        data = {'passed': bool(full.get('passed')), 'summary': lines}
    except BaseException as exc:                # noqa: BLE001 -- always a result
        error = describe(exc)
        try:
            with open(os.path.join(root, CHECK_LOG), 'a', encoding='utf-8',
                      errors='replace', newline='\n') as fh:
                fh.write('the check failed: %s\n%s' % (error,
                                                       traceback.format_exc()))
        except OSError:
            pass
    finally:
        # A check stopped half way leaves its work folder behind.
        shutil.rmtree(os.path.join(root, 'work'), ignore_errors=True)
    rec = result_record(ok, error, log=CHECK_LOG)
    rec['data'] = data
    emit(rec)
    return EXIT_OK if ok else EXIT_FAILED


class CheckConsole:
    """--check's console view: a line as each stage finishes."""

    def __init__(self, stream):
        self.stream = stream

    def __call__(self, rec):
        data = rec.get('data') if isinstance(rec.get('data'), dict) else {}
        if rec.get('kind') == 'done':
            self.say('  %-6s %-10s %s' % (CHECK_ROLES.get(data.get('role'), ''),
                                          rec.get('step') or '',
                                          data.get('state') or ''))

    def say(self, text):
        _say(self.stream, text)


def check_cli(syx, baseline=None, timing=False, home=None, out=None):
    """--check: the launcher's Check firmware on the console, through the
    same worker. Without `baseline`, the stock firmware set up here (the
    same version first), as the launcher offers. -> 0 when the build
    passes; 1 when it fails or the check could not finish; 6 for a product
    this version does not run."""
    out = sys.stdout if out is None else out
    try:
        plan = plan_check(syx, home)
        if baseline:
            baseline = os.path.abspath(baseline)
            stock = identify_supported(baseline)
            if getattr(stock.device, 'short', None) != \
                    getattr(plan.release.device, 'short', None):
                raise Refused('%s is %s firmware and %s is %s firmware.'
                              % (os.path.basename(baseline), stock.product,
                                 os.path.basename(plan.source),
                                 plan.release.product), EXIT_USAGE)
    except Refused as exc:
        _say(out, str(exc))
        return exc.code
    if plan.errors:
        for e in plan.errors:
            _say(out, e)
        return EXIT_FAILED
    if not baseline and plan.baselines:
        baseline = plan.baselines[0]['syx']
        _say(out, 'Comparing with %s, set up here (--baseline FILE to choose).'
             % plan.baselines[0]['label'])
    elif not baseline:
        _say(out, 'No stock firmware to compare with: the stock firmware\'s '
             'own quirks count against this build (--baseline FILE).')
    device = getattr(plan.release.device, 'short', None)
    checkdir = new_check_dir(plan.source, home)
    write_check_request(checkdir, plan.source, baseline, timing, device)
    _say(out, 'Checking %s: about %d minutes. The report goes to %s.'
         % (os.path.basename(plan.source),
            check_minutes(device, bool(baseline), timing), checkdir))
    console = CheckConsole(out)
    try:
        # kill_on_close: closing this console must not leave an invisible
        # check running the emulator for minutes.
        proc = spawn_worker('check', checkdir, piped=True, kill_on_close=True)
    except OSError as exc:
        _say(out, 'error: cannot start the check (%s)' % describe(exc))
        return EXIT_FAILED
    try:
        result, rc = _relay_worker(proc, console)
    finally:
        job = getattr(proc, 'digiemu_job', None)
        if job is not None:
            job.close()
        shutil.rmtree(os.path.join(checkdir, 'work'), ignore_errors=True)
    if rc is None:
        _say(out, 'Stopped.')
        return EXIT_FAILED
    if result is None or not result.get('ok'):
        _say(out, 'The check could not finish: %s' % (
            (result or {}).get('error') or 'the check process %s'
            % describe_exit(rc)))
        _say(out, 'Log: %s' % os.path.join(checkdir, CHECK_LOG))
        return EXIT_FAILED
    data = result.get('data') if isinstance(result.get('data'), dict) else {}
    _say(out, '')
    for line in data.get('summary') or ():
        _say(out, str(line))
    _say(out, 'Report: %s' % os.path.join(checkdir, 'report.json'))
    return EXIT_OK if data.get('passed') else EXIT_FAILED


# --- the launcher ----------------------------------------------------------------

def _launcher_log(home):
    import logging
    log = logging.getLogger('digiemu.launcher')
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    for base in (app_root(home), local_app_home()):
        try:
            d = os.path.join(base, 'logs')
            os.makedirs(d, exist_ok=True)
            h = logging.FileHandler(os.path.join(d, 'launcher.log'),
                                    encoding='utf-8')
        except OSError:
            continue
        h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        log.addHandler(h)
        log.log_path = h.baseFilename
        break
    else:
        log.addHandler(logging.NullHandler())
        log.log_path = None
    return log


def open_in_explorer(path):
    if os.name == 'nt':
        os.startfile(path)
    else:
        subprocess.Popen(['xdg-open', path])


@dataclasses.dataclass
class Job:
    kind: str
    proc: object
    dialog: object = None
    open_after: bool = False    # a build: open the panel when it is ready


class BuildDialog:
    """The progress window for one first-run worker."""

    def __init__(self, app, fwdir, label, proc):
        tk, ttk = app.tk, app.ttk
        self.app, self.fwdir, self.proc = app, fwdir, proc
        self.model = ProgressModel()
        self.started = time.monotonic()
        self.cancelled = self.done = False
        self.ok = False
        self.last_was_tick = False
        w = self.win = tk.Toplevel(app.root)
        w.title('Setting up %s' % label)
        w.transient(app.root)
        w.protocol('WM_DELETE_WINDOW', self.close_or_cancel)
        body = ttk.Frame(w, padding=12)
        body.pack(fill='both', expand=True)
        self.step_var = tk.StringVar(value='Starting...')
        ttk.Label(body, textvariable=self.step_var,
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(body, wraplength=520, justify='left', text=(
            'digiemu runs the firmware\'s own first boot once, which takes '
            'about a minute. Cancelling keeps the work done so far; the next run '
            'resumes from there.')).pack(anchor='w', pady=(2, 8))
        self.bar = ttk.Progressbar(body, maximum=1000, length=520,
                                   mode='determinate')
        self.bar.pack(fill='x')
        self.pct_var = tk.StringVar(value='')
        ttk.Label(body, textvariable=self.pct_var).pack(anchor='w', pady=(2, 6))
        self.text = tk.Text(body, height=9, width=80, wrap='none',
                            font=('Consolas', 9), state='disabled')
        self.text.pack(fill='both', expand=True)
        btns = ttk.Frame(body)
        btns.pack(fill='x', pady=(8, 0))
        self.cancel_btn = ttk.Button(btns, text='Cancel', command=self.cancel)
        self.cancel_btn.pack(side='right')
        ttk.Button(btns, text='Open log', command=self.open_log).pack(side='right',
                                                                      padx=6)
        self._clock()

    def _clock(self):
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        if not self.done:
            secs = int(time.monotonic() - self.started)
            pct = int(self.model.fraction * 100)
            self.pct_var.set('%d%%   %d:%02d elapsed%s'
                             % (pct, secs // 60, secs % 60,
                                '   (finishing; time varies)'
                                if self.model.indeterminate else ''))
            self.win.after(1000, self._clock)

    def _append(self, line, replace=False):
        t = self.text
        t.configure(state='normal')
        if replace:
            t.delete('end-2l linestart', 'end-1c')
        t.insert('end', line + '\n')
        n = int(t.index('end-1c').split('.')[0])
        if n > 400:
            t.delete('1.0', '%d.0' % (n - 400))
        t.see('end')
        t.configure(state='disabled')

    def feed_line(self, line):
        rec = decode_line(line)
        if rec is None:
            if line.strip():
                self._append(line.rstrip())
                self.last_was_tick = False
            return
        self.model.feed(rec)
        if rec.get('kind') == 'result':
            return
        title = STEP_TITLES.get(self.model.step, self.model.step or 'Working')
        self.step_var.set(title)
        if self.model.indeterminate and str(self.bar['mode']) != 'indeterminate':
            self.bar.configure(mode='indeterminate')
            self.bar.start(40)
        elif not self.model.indeterminate and str(self.bar['mode']) != 'determinate':
            self.bar.stop()
            self.bar.configure(mode='determinate')
        if not self.model.indeterminate:
            self.bar['value'] = int(self.model.fraction * 1000)
        tick = rec.get('kind') == 'tick'
        text = rec.get('text') or (_count_text(rec) if tick else '')
        if text:
            self._append('%s: %s' % (rec.get('step') or '-', text),
                         replace=tick and self.last_was_tick)
            self.last_was_tick = tick

    def finished(self, rc):
        self.done = True
        res = self.model.result or {}
        self.ok = rc == 0 and bool(res.get('ok'))
        self.bar.stop()
        self.bar.configure(mode='determinate')
        self.cancel_btn.configure(text='Close', command=self.close)
        if self.ok:
            self.bar['value'] = 1000
            self.step_var.set('Ready')
            self.pct_var.set('Done.')
        elif self.cancelled:
            self.step_var.set('Cancelled')
            self.pct_var.set('The next run resumes where this one stopped.')
        else:
            err = res.get('error') or ('the setup process %s' % describe_exit(rc))
            self.step_var.set('Setup failed')
            self.pct_var.set('Retry resumes at the step that failed.')
            self._append('ERROR: %s' % err)
            self._append('Log: %s' % os.path.join(self.fwdir, 'logs',
                                                  'first-run.log'))

    def open_log(self):
        path = os.path.join(self.fwdir, 'logs', 'first-run.log')
        if os.path.exists(path):
            open_in_explorer(path)

    def cancel(self):
        if self.done:
            return self.close()
        if not self.app.mb.askyesno(
                'Cancel setup', 'Stop setting up? The work done so far is kept, '
                'and the next run resumes from there.', parent=self.win):
            return
        self.cancelled = True
        try:
            stop_worker(self.proc)
        except OSError:
            pass

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass

    def close_or_cancel(self):
        if self.done:
            self.close()
        else:
            self.cancel()


CHECK_INTRO = (
    'The check runs this build the way the device would: it reads the .syx '
    'as the device receives it, runs the build\'s own bootloader, cold-boots '
    'it with the processor\'s rules enforced (memory map, instruction set, '
    'watchdog), then presses keys through a short session. With the stock '
    'firmware it does the same to that and compares the screens and sound. '
    'Nothing is written to your devices or your firmware here.')
CHECK_PASSED = (
    'Nothing the check can see stands in the way. It cannot see everything '
    '(the analog audio path, timing to the cycle, keys the session does not '
    'press), so still flash with care.')
CHECK_FAILED = ('The check found problems, listed below. The report folder '
                'has every detail (report.json) and any screens that differ.')


class CheckSetupDialog:
    """What to compare a build with, and whether to time it."""

    NONE = 'none'
    OTHER = 'other'

    def __init__(self, app, plan):
        tk, ttk = app.tk, app.ttk
        self.app, self.plan = app, plan
        self.other = None               # a stock .syx chosen by hand
        rel = plan.release
        self.device = getattr(getattr(rel, 'device', None), 'short', None)
        w = self.win = tk.Toplevel(app.root)
        w.title('Check firmware')
        w.transient(app.root)
        body = ttk.Frame(w, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=os.path.basename(plan.source),
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(body, text='%s %s, %s' % (
            rel.product, rel.version,
            'a release digiemu knows' if rel.status != 'untested'
            else 'not a release digiemu knows (custom or modified)')
                  ).pack(anchor='w')
        ttk.Label(body, wraplength=520, justify='left',
                  text=CHECK_INTRO).pack(anchor='w', pady=(6, 10))
        ttk.Label(body, text='Compare with the stock firmware',
                  font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        self.base_var = tk.StringVar(value='0' if plan.baselines else self.NONE)
        self.last_choice = self.base_var.get()
        for i, c in enumerate(plan.baselines):
            ttk.Radiobutton(body, text=c['label'], value=str(i),
                            variable=self.base_var,
                            command=self._changed).pack(anchor='w')
        self.other_btn = ttk.Radiobutton(
            body, text='Another .syx file...', value=self.OTHER,
            variable=self.base_var, command=self._choose_other)
        self.other_btn.pack(anchor='w')
        ttk.Radiobutton(body, text='Nothing: the stock firmware\'s own quirks '
                        'then count against this build', value=self.NONE,
                        variable=self.base_var,
                        command=self._changed).pack(anchor='w')
        self.timing_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text='Also measure the audio render\'s timing '
                        '(several times slower)', variable=self.timing_var,
                        command=self._changed).pack(anchor='w', pady=(10, 0))
        self.estimate_var = tk.StringVar()
        ttk.Label(body, textvariable=self.estimate_var, foreground='#666',
                  wraplength=520, justify='left').pack(anchor='w', pady=(6, 10))
        btns = ttk.Frame(body)
        btns.pack(fill='x')
        ttk.Button(btns, text='Cancel', command=self.close).pack(side='right')
        ttk.Button(btns, text='Start check',
                   command=self.start).pack(side='right', padx=6)
        self._changed()

    def baseline(self):
        """-> the stock .syx chosen, or None."""
        choice = self.base_var.get()
        if choice == self.OTHER:
            return self.other
        if choice == self.NONE:
            return None
        return self.plan.baselines[int(choice)]['syx']

    def _changed(self):
        choice = self.base_var.get()
        if choice != self.OTHER:
            self.last_choice = choice
        stock = self.baseline()
        text = 'About %d minutes. You can keep using digiemu meanwhile.' % (
            check_minutes(self.device, stock is not None,
                          bool(self.timing_var.get())))
        if choice == self.OTHER and stock:
            text = 'Stock: %s. %s' % (os.path.basename(stock), text)
        self.estimate_var.set(text)

    def _choose_other(self):
        path = self.app.fd.askopenfilename(
            parent=self.win, title='Choose the stock firmware',
            filetypes=[('Elektron OS (.syx)', '*.syx'), ('All files', '*.*')])
        if path:
            try:
                rel = identify_supported(os.path.abspath(path))
            except Refused as exc:
                self.app.mb.showerror('Cannot compare with this file', str(exc),
                                      parent=self.win)
                path = None
            else:
                short = getattr(rel.device, 'short', None)
                if short != self.device:
                    self.app.mb.showerror(
                        'Cannot compare with this file',
                        '%s is %s firmware; the build is %s firmware.'
                        % (os.path.basename(path), rel.product,
                           self.plan.release.product), parent=self.win)
                    path = None
        if path:
            self.other = os.path.abspath(path)
        elif not self.other:
            self.base_var.set(self.last_choice)
        self._changed()

    def start(self):
        stock = self.baseline()
        if self.base_var.get() == self.OTHER and not stock:
            return self._choose_other()
        timing = bool(self.timing_var.get())
        self.close()
        self.app.start_check(self.plan, stock, timing)

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class CheckDialog:
    """The progress and the verdict of one firmware check."""

    def __init__(self, app, checkdir, label, proc, progress):
        tk, ttk = app.tk, app.ttk
        self.app, self.checkdir, self.proc = app, checkdir, proc
        self.progress = progress
        self.started = time.monotonic()
        self.cancelled = self.done = False
        self.passed = None
        w = self.win = tk.Toplevel(app.root)
        w.title('Checking %s' % label)
        w.transient(app.root)
        w.protocol('WM_DELETE_WINDOW', self.close_or_cancel)
        body = ttk.Frame(w, padding=12)
        body.pack(fill='both', expand=True)
        self.step_var = tk.StringVar(value='Starting...')
        self.step_label = ttk.Label(body, textvariable=self.step_var,
                                    font=('Segoe UI', 11, 'bold'))
        self.step_label.pack(anchor='w')
        self.about_var = tk.StringVar(value=(
            'Checking %s. This runs the firmware for several minutes; the '
            'report is kept in %s.' % (label, checkdir)))
        ttk.Label(body, textvariable=self.about_var, wraplength=520,
                  justify='left').pack(anchor='w', pady=(2, 8))
        self.bar = ttk.Progressbar(body, maximum=1000, length=520,
                                   mode='determinate')
        self.bar.pack(fill='x')
        self.pct_var = tk.StringVar(value='')
        ttk.Label(body, textvariable=self.pct_var).pack(anchor='w', pady=(2, 6))
        self.text = tk.Text(body, height=12, width=80, wrap='none',
                            font=('Consolas', 9), state='disabled')
        self.text.pack(fill='both', expand=True)
        btns = ttk.Frame(body)
        btns.pack(fill='x', pady=(8, 0))
        self.cancel_btn = ttk.Button(btns, text='Stop', command=self.cancel)
        self.cancel_btn.pack(side='right')
        ttk.Button(btns, text='Open report folder',
                   command=self.open_report).pack(side='right', padx=6)
        self._clock()

    def _clock(self):
        try:
            if not self.win.winfo_exists():
                return
        except Exception:
            return
        if not self.done:
            secs = int(time.monotonic() - self.started)
            frac = self.progress.fraction()
            self.bar['value'] = int(frac * 1000)
            self.pct_var.set('%d%%   %d:%02d elapsed'
                             % (int(frac * 100), secs // 60, secs % 60))
            self.win.after(1000, self._clock)

    def _append(self, line):
        t = self.text
        t.configure(state='normal')
        t.insert('end', line + '\n')
        t.see('end')
        t.configure(state='disabled')

    def feed_line(self, line):
        rec = decode_line(line)
        if rec is None:
            if line.strip():
                self._append(line.rstrip())
            return
        self.progress.feed(rec)
        kind = rec.get('kind')
        if kind == 'result':
            return
        data = rec.get('data') if isinstance(rec.get('data'), dict) else {}
        role, step = data.get('role'), rec.get('step')
        who = CHECK_ROLES.get(role, role or '')
        title = CHECK_TITLES.get(step, step or '')
        if kind == 'start':
            self.step_var.set('%s: %s' % (who, title) if who else title)
        elif kind == 'done':
            self._append('%-6s %-10s %s' % (who, step, data.get('state') or ''))
        elif kind == 'note' and rec.get('text'):
            self._append('         %s' % rec['text'])

    def finished(self, rc):
        self.done = True
        res = self.progress.result or {}
        data = res.get('data') if isinstance(res.get('data'), dict) else {}
        self.cancel_btn.configure(text='Close', command=self.close)
        if self.cancelled:
            self.step_var.set('Stopped')
            self.pct_var.set('The check was stopped before it could judge '
                             'anything.')
        elif res.get('ok'):
            self.passed = bool(data.get('passed'))
            self.bar['value'] = 1000
            self.step_var.set('PASS' if self.passed else 'FAIL')
            self.step_label.configure(
                foreground='#1b7a1b' if self.passed else '#b00020')
            self.about_var.set(CHECK_PASSED if self.passed else CHECK_FAILED)
            self.pct_var.set('Done in %d:%02d.' % divmod(
                int(time.monotonic() - self.started), 60))
            self._append('')
            for line in data.get('summary') or ():
                self._append(str(line))
        else:
            err = res.get('error') or ('the check process %s'
                                       % describe_exit(rc))
            self.step_var.set('The check could not finish')
            self.pct_var.set('')
            self._append('ERROR: %s' % err)
            self._append('Log: %s' % os.path.join(self.checkdir, CHECK_LOG))

    def open_report(self):
        if os.path.isdir(self.checkdir):
            open_in_explorer(self.checkdir)

    def cancel(self):
        if self.done:
            return self.close()
        if not self.app.mb.askyesno(
                'Stop the check', 'Stop checking? Nothing will have been '
                'judged; a new check starts from the beginning.',
                parent=self.win):
            return
        self.cancelled = True
        try:
            stop_worker(self.proc)
        except OSError:
            pass

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass

    def close_or_cancel(self):
        if self.done:
            self.close()
        else:
            self.cancel()


class Launcher:
    """The window: firmware folders, their state, and what to do with them.

    Tk runs on the main thread only; worker output arrives through a queue
    that `_poll` drains with after()."""

    POLL_MS = 100
    REFRESH_MS = 4000

    def __init__(self, root, home, log):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.ttk, self.mb, self.fd = tk, ttk, messagebox, filedialog
        self.root, self.home, self.log = root, home, log
        self.jobs = {}
        self.infos = {}
        self.events = queue.Queue()
        self.selftest = None            # run_selftest's result, once it is in
        self.check_setup = None         # the last Check firmware dialog
        root.title('digiemu %s' % APP_VERSION)
        root.minsize(640, 320)
        root.report_callback_exception = self._callback_error
        root.protocol('WM_DELETE_WINDOW', self.quit)
        self._build()
        self._check_home()
        self.refresh()
        root.after(self.POLL_MS, self._poll)
        root.after(self.REFRESH_MS, self._auto_refresh)
        threading.Thread(target=self._run_selftest, args=(), daemon=True).start()

    # layout

    def _build(self):
        tk, ttk = self.tk, self.ttk
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill='both', expand=True)
        # Empty unless the self-test failed: then it says what, in red.
        self.banner = tk.StringVar()
        ttk.Label(frm, textvariable=self.banner, foreground='#b00020',
                  wraplength=600, justify='left').pack(anchor='w')
        ttk.Label(frm, text='Firmware', font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        tv = self.tree = ttk.Treeview(frm, columns=('status', 'size'),
                                      selectmode='browse', height=8)
        tv.heading('#0', text='Release')
        tv.heading('status', text='Status')
        tv.heading('size', text='Size on disk')
        tv.column('#0', width=320)
        tv.column('status', width=150)
        tv.column('size', width=110, anchor='e')
        tv.pack(fill='both', expand=True, pady=(4, 8))
        tv.bind('<<TreeviewSelect>>', lambda e: self._update_buttons())
        tv.bind('<Double-1>', lambda e: self.play())
        bar = ttk.Frame(frm)
        bar.pack(fill='x')
        self.buttons = {}
        for key, text, cmd in (('add', 'Add firmware...', self.add),
                               ('check', 'Check firmware...', self.check),
                               ('play', 'Play', self.play),
                               ('rebuild', 'Rebuild', self.rebuild),
                               ('reset', 'Reset to factory', self.reset),
                               ('open', 'Open folder', self.open_folder),
                               ('remove', 'Remove', self.remove)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side='left', padx=(0, 6))
            self.buttons[key] = b
        self.note = tk.StringVar()
        ttk.Label(frm, textvariable=self.note, foreground='#666',
                  wraplength=600, justify='left').pack(anchor='w', pady=(8, 0))

    def _check_home(self):
        base = app_root(self.home)
        if not is_writable(base):
            alt = local_app_home()
            if self.mb.askyesno(
                    'Folder is read-only',
                    'digiemu keeps its firmware next to the program, but %s '
                    'cannot be written to (Program Files and read-only drives '
                    'are like that).\n\nUse %s instead?' % (base, alt)):
                self.home = alt
                self.log.info('home switched to %s', alt)
        synced = under_onedrive(app_root(self.home))
        if synced:
            self.mb.showwarning(
                'OneDrive folder',
                'This digiemu folder is inside OneDrive (%s). Each firmware '
                'keeps a ~1 GB +Drive image that changes every session, so '
                'OneDrive will keep uploading it. A folder outside OneDrive, '
                'such as C:\\digiemu, works better.' % synced)

    # state

    def selected(self):
        sel = self.tree.selection()
        return self.infos.get(sel[0]) if sel else None

    def _status(self, fwdir):
        """status_of, knowing which folders this launcher's own jobs hold."""
        job = self.jobs.get(fwdir)
        known = self.infos.get(os.path.basename(fwdir)) or {}
        return status_of(fwdir, running=job.kind if job else None,
                         label=known.get('label'))

    def refresh(self):
        infos = [self._status(d) for d in list_firmware_dirs(self.home)]
        new = {i['slug']: i for i in infos}
        tv = self.tree
        for iid in tv.get_children():
            if iid not in new:
                tv.delete(iid)
        for i in infos:
            values = (i['status'], human_size(i['size']))
            if tv.exists(i['slug']):
                tv.item(i['slug'], text=i['label'], values=values)
            else:
                tv.insert('', 'end', iid=i['slug'], text=i['label'], values=values)
        self.infos = new
        if not tv.selection() and infos:
            tv.selection_set(infos[0]['slug'])
        self.note.set('%d firmware in %s' % (len(infos), firmware_root(self.home))
                      if infos else
                      'No firmware yet. "Add firmware..." takes an Elektron OS '
                      '.syx file (%s).' % SUPPORTED)
        self._update_buttons()

    def _auto_refresh(self):
        try:
            self.refresh()
        finally:
            self.root.after(self.REFRESH_MS, self._auto_refresh)

    def _update_buttons(self):
        info = self.selected()
        st = info['status'] if info else None
        state = {
            'play': st in (READY, NEEDS_SETUP, REBUILD, BUILDING),
            'rebuild': st in (READY, REBUILD),
            'reset': st in (READY, REBUILD, NEEDS_SETUP),
            'open': info is not None,
            'remove': info is not None and st not in (BUILDING, RUNNING),
        }
        for key, on in state.items():
            self.buttons[key].state(['!disabled'] if on else ['disabled'])
        self.buttons['play'].configure(
            text='Set up' if st == NEEDS_SETUP else 'Play')

    # actions

    def add(self):
        path = self.fd.askopenfilename(
            parent=self.root, title='Choose an Elektron OS file',
            filetypes=[('Elektron OS (.syx)', '*.syx'), ('All files', '*.*')])
        if not path:
            return
        try:
            plan = plan_add(path, self.home)
        except Refused as exc:
            self.mb.showerror('Cannot add this firmware', str(exc))
            return
        rel = plan.release
        if os.path.isdir(plan.fwdir):
            # Already here: setting it up again would rerun the cold boot
            # and throw away its saved session. Show it instead.
            info = self._status(plan.fwdir)
            if info['status'] in (READY, REBUILD, RUNNING, BUILDING):
                self.log.info('%s is already here (%s)', rel.label, info['status'])
                self.refresh()
                if self.tree.exists(rel.slug):
                    self.tree.selection_set(rel.slug)
                    self._update_buttons()
                if info['status'] != READY:
                    self._act_on(info)
                elif self.mb.askyesno('Already set up', '%s is already set up. '
                                      'Open it now?' % info['label']):
                    self.start_panel(info)
                return
        if rel.status == 'untested' and not self.mb.askyesno(
                'Untested firmware', untested_text(rel) + '\n\nSet it up anyway?'):
            return
        if plan.errors:
            self.mb.showerror('Cannot add this firmware', '\n\n'.join(plan.errors))
            return
        if plan.warnings and not self.mb.askokcancel(
                'Before you continue', '\n\n'.join(plan.warnings)
                + '\n\nContinue anyway?'):
            return
        try:
            paths = prepare_folder(plan)
        except Busy as exc:
            self.mb.showinfo('In use', str(exc))
            return
        except (OSError, ValueError) as exc:
            self.log.exception('adding %s', path)
            self.mb.showerror('Cannot add this firmware', describe(exc))
            return
        self.log.info('added %s as %s', rel.label, paths.root)
        self.refresh()
        if self.tree.exists(rel.slug):
            self.tree.selection_set(rel.slug)
        self.start_build(paths.root, rel.label)

    def start_build(self, fwdir, label, rebuild=False, open_after=False):
        job = self.jobs.get(fwdir)
        if job is not None:
            if job.dialog is not None:
                job.dialog.win.lift()
            return
        try:
            proc = spawn_worker('first-run', fwdir,
                                *(['--rebuild'] if rebuild else []), piped=True)
        except OSError as exc:
            self.log.exception('starting the first-run worker')
            self.mb.showerror('Cannot start', describe(exc))
            return
        self.log.info('first-run worker pid %s for %s%s', proc.pid, fwdir,
                      ' (rebuild)' if rebuild else '')
        dialog = BuildDialog(self, fwdir, label, proc)
        self.jobs[fwdir] = Job('first-run', proc, dialog, open_after)
        threading.Thread(target=self._read_worker, args=(fwdir, proc),
                         daemon=True).start()
        self.refresh()

    def _read_worker(self, fwdir, proc):
        try:
            for raw in iter(proc.stdout.readline, b''):
                self.events.put(('line', fwdir,
                                 raw.decode('utf-8', 'replace').rstrip('\r\n')))
        except (OSError, ValueError):
            pass
        self.events.put(('exit', fwdir, proc.wait()))

    def _wait_panel(self, fwdir, proc):
        self.events.put(('exit', fwdir, proc.wait()))

    def _run_selftest(self):
        """On a thread: the child self-test, its result into the queue."""
        try:
            res = run_selftest()
        except BaseException as exc:            # noqa: BLE001 -- never lost
            res = dict(summarize_selftest(None, None),
                       message='The self-test could not run: %s' % describe(exc))
        self.events.put(('selftest', None, res))

    def _selftest_done(self, res):
        self.selftest = res
        if res.get('ok'):
            self.log.info('self-test passed')
            return
        self.log.error('self-test FAILED: %s\nreport: %s\nstderr:\n%s',
                       res.get('message'),
                       encode_line({'kind': 'selftest', 'data': res.get('report')}),
                       res.get('stderr') or '')
        self.banner.set('Self-test failed: %s' % res.get('message'))
        where = getattr(self.log, 'log_path', None)
        self.mb.showerror(
            'digiemu self-test failed',
            '%s\n\nYou can still look at your firmware here, but setting one '
            'up or playing it will probably fail.%s'
            % (res.get('message'), '\n\nDetails are in %s.' % where if where else ''))

    def _poll(self):
        try:
            for _ in range(500):
                try:
                    what, fwdir, arg = self.events.get_nowait()
                except queue.Empty:
                    break
                if what == 'selftest':
                    self._selftest_done(arg)
                    continue
                job = self.jobs.get(fwdir)
                if job is None:
                    continue
                if what == 'line' and job.dialog is not None:
                    job.dialog.feed_line(arg)
                elif what == 'exit':
                    del self.jobs[fwdir]
                    if job.kind == 'first-run':
                        self._build_exited(fwdir, job, arg)
                    elif job.kind == 'check':
                        self._check_exited(fwdir, job, arg)
                    else:
                        self._panel_exited(fwdir, arg)
        finally:
            self.root.after(self.POLL_MS, self._poll)

    def _build_exited(self, fwdir, job, rc):
        self.log.info('first-run worker for %s exited %s', fwdir, rc)
        job.dialog.finished(rc)
        if job.dialog.model.result is None and not job.dialog.cancelled:
            # A native crash ends the worker mid-line: its log then stops
            # without a word, so say it there too.
            _append_log(fwdir, 'first-run.log', 'the first-run worker %s '
                        'without reporting a result' % describe_exit(rc))
        self.refresh()
        info = status_of(fwdir)
        if job.dialog.ok and info['status'] == READY:
            job.dialog.close()
            if job.open_after or self.mb.askyesno(
                    'Ready', '%s is ready. Open it now?' % info['label']):
                self.start_panel(info)

    def _check_exited(self, checkdir, job, rc):
        self.log.info('check worker for %s exited %s', checkdir, rc)
        # A stopped or crashed worker never reached its own clean-up, and
        # its work folder holds a card image and snapshots.
        shutil.rmtree(os.path.join(checkdir, 'work'), ignore_errors=True)
        job.dialog.finished(rc)

    def _panel_exited(self, fwdir, rc):
        self.log.info('panel worker for %s exited %s', fwdir, rc)
        self.refresh()
        log = os.path.join(fwdir, 'logs', 'panel.log')
        if rc == EXIT_OK:
            return
        if rc == EXIT_NOT_READY:
            info = status_of(fwdir)
            if info['status'] == REBUILD:
                self._offer_rebuild(info)
            elif info['status'] == NEEDS_SETUP:
                self._offer_setup(info)
        elif rc == EXIT_INCOMPATIBLE:
            info = dict(status_of(fwdir), reason=INCOMPATIBLE_REASON)
            self._offer_rebuild(info)
        elif rc == EXIT_SAMPLES_ADDED:
            # The panel asked before it closed: rebuild with the samples on
            # the card and open it again, without asking twice.
            info = status_of(fwdir)
            self.start_build(fwdir, info['label'], rebuild=True,
                             open_after=True)
        elif rc == EXIT_USAGE:
            self.mb.showwarning('Session not saved',
                                'The panel closed, but its state could not be '
                                'saved. Details are in %s.' % log)
        elif rc == EXIT_BUSY:
            self.mb.showinfo('Already open',
                             'This firmware is already open in another window.')
        else:
            self.mb.showerror('The emulator stopped',
                              'The panel %s. Anything saved to the +Drive since '
                              'the session started may be lost. Details are in '
                              '%s.' % (describe_exit(rc), log))

    def _offer_rebuild(self, info):
        if info.get('reason') == INCOMPATIBLE_REASON:
            why = ('The saved state of %s was made by a different build of '
                   'digiemu, so this version cannot open it.' % info['label'])
        else:
            why = ('The +Drive image of %s changed since its last saved '
                   'session, so the saved state no longer matches it.'
                   % info['label'])
        if self.mb.askyesno(
                'Rebuild needed',
                '%s Rebuilding boots the firmware again from the card as it is '
                'now, about a minute. The card and everything on it is '
                'kept.\n\nRebuild now?' % why):
            self.start_build(info['fwdir'], info['label'], rebuild=True)

    def _offer_setup(self, info):
        if self.mb.askyesno(
                'Set up', '%s is not set up yet. Set it up now? It takes about '
                'a minute.' % info['label']):
            self.start_build(info['fwdir'], info['label'])

    def start_panel(self, info):
        fwdir = info['fwdir']
        if fwdir in self.jobs:
            return
        try:
            proc = spawn_worker('panel', fwdir, piped=False)
        except OSError as exc:
            self.log.exception('starting the panel worker')
            self.mb.showerror('Cannot start', describe(exc))
            return
        self.log.info('panel worker pid %s for %s', proc.pid, fwdir)
        self.jobs[fwdir] = Job('panel', proc)
        threading.Thread(target=self._wait_panel, args=(fwdir, proc),
                         daemon=True).start()
        self.refresh()

    def play(self):
        info = self.selected()
        if info:
            self._act_on(info)

    def _act_on(self, info):
        """What Play means for a folder in each state."""
        st = info['status']
        if st == BUILDING:
            job = self.jobs.get(info['fwdir'])
            if job is not None and job.dialog is not None:
                job.dialog.win.lift()
            else:
                self.mb.showinfo('Setting up', '%s is being set up by another '
                                 'digiemu window.' % info['label'])
        elif st == RUNNING:
            self.mb.showinfo('Already open', '%s is already open.' % info['label'])
        elif st == NEEDS_SETUP:
            self._offer_setup(info)
        elif st == REBUILD:
            self._offer_rebuild(info)
        elif st == READY:
            self.start_panel(info)
        else:
            self.mb.showerror(st, info['reason'] or st)

    def rebuild(self):
        info = self.selected()
        if not info or info['status'] not in (READY, REBUILD):
            return
        if self.mb.askyesno(
                'Rebuild', 'Rebuild %s? This boots the firmware again from its '
                '+Drive card, about a minute. The card is kept; the last '
                'saved session state is not.' % info['label']):
            self.start_build(info['fwdir'], info['label'], rebuild=True)

    def reset(self):
        """Reset to factory: delete the card and the saved state, set up
        again from scratch."""
        info = self.selected()
        if not info or info['status'] not in (READY, REBUILD, NEEDS_SETUP):
            return
        if not self.mb.askyesno(
                'Reset to factory',
                'Reset %s to factory?\n\nThis deletes its +Drive image -- every '
                'project and sample saved on it -- and its saved sessions, then '
                'sets the firmware up again from scratch, about a minute. '
                'It cannot be undone.' % info['label'], icon='warning'):
            return
        try:
            reset_firmware(info['fwdir'], self.home)
        except Busy as exc:
            self.mb.showinfo('In use', str(exc))
            self.refresh()
            return
        except (Exception, SystemExit) as exc:      # noqa: BLE001
            self.log.exception('resetting %s', info['fwdir'])
            self.mb.showerror('Cannot reset', describe(exc))
            self.refresh()
            return
        self.log.info('reset %s to factory', info['fwdir'])
        self.refresh()
        self.start_build(info['fwdir'], info['label'])

    def check(self):
        """Check firmware: a build (usually a custom one) against the stock
        firmware set up here, before it goes on a device."""
        path = self.fd.askopenfilename(
            parent=self.root, title='Choose the firmware build to check',
            filetypes=[('Elektron OS (.syx)', '*.syx'), ('All files', '*.*')])
        if not path:
            return
        try:
            plan = plan_check(path, self.home)
        except Refused as exc:
            self.mb.showerror('Cannot check this firmware', str(exc))
            return
        if plan.errors:
            self.mb.showerror('Cannot check this firmware',
                              '\n\n'.join(plan.errors))
            return
        self.check_setup = CheckSetupDialog(self, plan)

    def start_check(self, plan, baseline, timing):
        device = getattr(getattr(plan.release, 'device', None), 'short', None)
        try:
            checkdir = new_check_dir(plan.source, self.home)
            write_check_request(checkdir, plan.source, baseline, timing, device)
            proc = spawn_worker('check', checkdir, piped=True)
        except OSError as exc:
            self.log.exception('starting a firmware check')
            self.mb.showerror('Cannot start the check', describe(exc))
            return
        self.log.info('check worker pid %s for %s (stock %s, timing %s) in %s',
                      proc.pid, plan.source, baseline, timing, checkdir)
        dialog = CheckDialog(self, checkdir, os.path.basename(plan.source), proc,
                             CheckProgress(device, baseline is not None, timing))
        self.jobs[checkdir] = Job('check', proc, dialog)
        threading.Thread(target=self._read_worker, args=(checkdir, proc),
                         daemon=True).start()

    def open_folder(self):
        info = self.selected()
        if info:
            open_in_explorer(info['fwdir'])

    def remove(self):
        info = self.selected()
        if not info or info['status'] in (BUILDING, RUNNING):
            return
        if not self.mb.askyesno(
                'Remove firmware',
                'Remove %s?\n\nThis deletes its folder (%s), including the +Drive '
                'image and every project and sample saved on it. It cannot be '
                'undone.' % (info['label'], human_size(info['size'])),
                icon='warning'):
            return
        try:
            remove_firmware(info['fwdir'], self.home)
        except Busy as exc:
            self.mb.showinfo('In use', str(exc))
        except (OSError, ValueError) as exc:
            self.log.exception('removing %s', info['fwdir'])
            self.mb.showerror('Cannot remove', describe(exc))
        else:
            self.log.info('removed %s', info['fwdir'])
        self.refresh()

    def quit(self):
        builds = [j for j in self.jobs.values() if j.kind == 'first-run']
        checks = [j for j in self.jobs.values() if j.kind == 'check']
        if builds:
            answer = self.mb.askyesnocancel(
                'Setup still running',
                'A firmware is still being set up.\n\nYes: stop it (the next run '
                'resumes where it stopped).\nNo: let it finish in the background.')
            if answer is None:
                return
            if answer:
                for job in builds:
                    try:
                        stop_worker(job.proc)
                    except OSError:
                        pass
        if checks:
            answer = self.mb.askyesnocancel(
                'Check still running',
                'A firmware check is still running.\n\nYes: stop it.\nNo: let '
                'it finish in the background; its report lands in %s.'
                % checks_root(self.home))
            if answer is None:
                return
            if answer:
                for job in checks:
                    try:
                        stop_worker(job.proc)
                    except OSError:
                        pass
        self.root.destroy()

    def _callback_error(self, exc, value, tb):
        self.log.error('unhandled error\n%s',
                       ''.join(traceback.format_exception(exc, value, tb)))
        where = getattr(self.log, 'log_path', None)
        self.mb.showerror('Error', '%s%s' % (describe(value),
                          '\n\nDetails are in %s.' % where if where else ''))


def launcher(home=None):
    import tkinter as tk
    log = _launcher_log(home)
    log.info('launcher start, digiemu %s, root %s', APP_VERSION, app_root(home))
    root = tk.Tk()
    try:
        Launcher(root, home, log)
        root.mainloop()
    except BaseException:
        log.exception('launcher failed')
        raise
    return EXIT_OK


# --- entry -------------------------------------------------------------------------

def _parser():
    ap = argparse.ArgumentParser(
        prog=APP_NAME, description='Digitakt and Digitone emulator: the '
                                   'portable app.')
    ap.add_argument('--home', metavar='DIR',
                    help='data folder (default: next to the exe; from source '
                         '$DIGIEMU_HOME or <repo>/portable)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--worker', nargs=2, metavar=('MODE', 'FWDIR'),
                      help=argparse.SUPPRESS)
    mode.add_argument('--add', metavar='SYX',
                      help='set up a firmware without the window')
    mode.add_argument('--list', action='store_true',
                      help='list the firmware set up here')
    mode.add_argument('--reset', metavar='NAME',
                      help='reset a firmware to factory: delete its +Drive '
                           'image and saved sessions (needs --yes)')
    mode.add_argument('--check', metavar='SYX',
                      help='check a firmware build before it goes on a '
                           'device; exit 0 when it passes')
    ap.add_argument('--baseline', metavar='STOCK',
                    help='with --check: the stock .syx to compare with '
                         '(default: the stock firmware set up here)')
    ap.add_argument('--timing', action='store_true',
                    help='with --check: also measure the audio render\'s '
                         'timing (several times slower)')
    # Bare, after --worker first-run FWDIR: the launcher's Rebuild (snapshots
    # cleared, card kept). With a NAME, the CLI mode.
    ap.add_argument('--rebuild', nargs='?', const=True, default=None,
                    metavar='NAME',
                    help='boot a firmware set up here again, from its +Drive '
                         'card as it is now (or finish setting it up); NAME '
                         'is the folder name --list shows')
    ap.add_argument('--yes', action='store_true',
                    help='with --add: accept an untested release; with '
                         '--reset: really reset')
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    _ensure_import_path()
    if args.worker:
        mode, fwdir = args.worker
        if mode == 'first-run':
            return worker_first_run(fwdir, rebuild=bool(args.rebuild))
        if mode == 'panel':
            return worker_panel(fwdir)
        if mode == 'check':
            return worker_check(fwdir)
        _say(sys.stderr, 'unknown worker mode %r' % mode)
        return EXIT_USAGE
    other = args.add or args.list or args.reset or args.check
    if (args.baseline or args.timing) and not args.check:
        _say(sys.stderr, '--baseline and --timing go with --check')
        return EXIT_USAGE
    if isinstance(args.rebuild, str):
        if other:
            _say(sys.stderr, '--rebuild NAME cannot be combined with --add, '
                 '--list, --reset or --check')
            return EXIT_USAGE
        return rebuild_firmware(args.rebuild, home=args.home)
    if args.rebuild is True and not other:
        _say(sys.stderr, '--rebuild needs the name of a firmware set up here '
             '(the folder name --list shows)')
        return EXIT_USAGE
    if args.add:
        return add_firmware(args.add, yes=args.yes, home=args.home)
    if args.list:
        return list_firmware(home=args.home)
    if args.reset:
        return reset_cli(args.reset, yes=args.yes, home=args.home)
    if args.check:
        return check_cli(args.check, baseline=args.baseline,
                         timing=args.timing, home=args.home)
    return launcher(home=args.home)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
