"""Entry point of the portable app: digiemu.exe, digiemu-console.exe and dev.

PyInstaller freezes this file as __main__ for both executables (digiemu.exe is
windowed; digiemu-console.exe is the same program with a console, for
support). In dev, `python packaging/digiemu_main.py ...` behaves the same.
It does three things, in this order, because each has to be in place before
the next can go wrong:

1. stdio. A windowed exe starts with sys.stdout and sys.stderr set to None.
   print() is then a silent no-op, and the emulator reports everything
   through print(), so every diagnostic would be lost. When either stream is
   None, it goes to <exe dir>/logs/launcher.log (UTF-8, line-buffered)
   before anything can print. With PYTHONFAULTHANDLER or DIGIEMU_FAULTHANDLER
   set, faulthandler writes there too. Real streams (a console, or the pipes
   the launcher reads a worker's JSON lines from) are switched to UTF-8.
   On a cp1252 pipe a non-Latin path raises UnicodeEncodeError, and
   checkpoint.make prints its snapshot path from inside an emulation hook,
   where that error kills the ladder.
2. --selftest. It checks that this bundle can run the emulator at all:
   the patched unicorn.dll (checked by behaviour, not just by name), capstone,
   the bundled device files, Tk, and that every module the app imports
   inside functions is in the bundle (IMPORTS; imported, nothing started).
   Each check is isolated, so a broken launcher module is reported as a
   failed 'imports' check and the rest still run. The build script runs it
   on every fresh dist, and support can ask a user to run it.
3. Everything else goes to emu.portable.main(), which is imported only then.
"""
import datetime
import hashlib
import importlib
import json
import os
import sys

if not getattr(sys, 'frozen', False):
    # Dev: this file lives in packaging/, and emu/ and dt2/ are packages at
    # the repo root. Frozen, they are in the bundle's archive already.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_NAME = 'launcher.log'
LOG_ROTATE_BYTES = 4 << 20
BUILD_INFO = 'digiemu-build.json'
NATIVE_OPTIONS = 0x7            # NATIVE_RTE | NO_MEM_EXIT | NO_HOOK_PC_SYNC

# What the app imports inside functions, so an import scan could miss it and
# the first sign would be a user's failed first run. The same list as
# packaging/bundle_guard.py REQUIRED_MODULES (the spec's hiddenimports and
# the archive audit; tests/test_packaging.py keeps the two equal). It is
# spelled out here because bundle_guard is a build tool, not in the bundle.
IMPORTS = (
    'emu.portable', 'emu.bootstrap', 'emu.release', 'emu.dtpanel', 'emu.gui',
    'emu.dnpanel', 'emu.dsplink',
    'emu.ekfsformat', 'emu.uiresume', 'emu.checkpoint', 'emu.extract',
    'emu.snapshot', 'emu.checkpointver', 'emu.sparse', 'emu.samples', 'emu.esdhc',
    'emu.run', 'emu.config',
    'emu.device', 'emu.longrun', 'emu.panel', 'emu.screen', 'emu.symbols', 'emu.dtim',
    'emu.pit', 'emu.harness', 'emu.native', 'emu.fastuc', 'emu.unicorn_compat',
    'emu.dspboot', 'emu.dsp', 'emu.edma', 'emu.gpio', 'emu.hle',
    'emu.softfloat', 'emu.ssi', 'emu.trace', 'emu.bootrom', 'dt2.container',
    'emu.fwcheck', 'emu.fwcompare', 'emu.session', 'emu.strict', 'emu.cftiming',
    'dt2.elz',
    'dt2.coldfire',
    'ctypes', 'ctypes.wintypes', 'logging', 'tkinter', 'tkinter.filedialog',
    'tkinter.messagebox', 'tkinter.ttk', 'unicorn', 'capstone',
)

_log = None                     # the launcher.log stream when stdio was None
_import_module = importlib.import_module    # the tests put a stand-in here


def app_root():
    """Where the app keeps its data: next to the exe when frozen. In dev it
    is $DIGIEMU_HOME, or <repo>/portable, as in emu.portable."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    home = os.environ.get('DIGIEMU_HOME')
    if home:
        return os.path.abspath(home)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, 'portable')


def bundle_dir():
    """The read-only resources: _internal when frozen, the repo in dev."""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _open_log():
    """-> an append-mode UTF-8 line-buffered stream, or None.

    <app root>/logs first; %LOCALAPPDATA%/digiemu/logs if that folder cannot
    be written (the exe unpacked somewhere read-only)."""
    dirs = [os.path.join(app_root(), 'logs')]
    local = os.environ.get('LOCALAPPDATA')
    if local:
        dirs.append(os.path.join(local, 'digiemu', 'logs'))
    for d in dirs:
        path = os.path.join(d, LOG_NAME)
        try:
            os.makedirs(d, exist_ok=True)
            try:
                if os.path.getsize(path) > LOG_ROTATE_BYTES:
                    os.replace(path, path + '.1')
            except OSError:
                pass            # missing, or held open by another process
            return open(path, 'a', encoding='utf-8', errors='replace',
                        buffering=1, newline='\n')
        except OSError:
            continue
    return None


def setup_stdio():
    """Give print() somewhere to go, in UTF-8. See the module docstring."""
    global _log
    for s in (sys.stdout, sys.stderr):
        if s is not None and hasattr(s, 'reconfigure'):
            try:
                s.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
            except (OSError, ValueError):
                pass
    if sys.stdout is not None and sys.stderr is not None:
        return
    _log = _open_log()
    if _log is None:
        return
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log
    _log.write('---- %s pid %d %s\n' % (
        datetime.datetime.now().isoformat(timespec='seconds'), os.getpid(),
        ' '.join([os.path.basename(sys.executable)] + sys.argv[1:])))
    # Only on request: on Windows faulthandler also reports exceptions that
    # are handled later, and Unicorn takes (and handles) an access violation
    # in the first mem_map of every engine, so each launch would log a
    # "Windows fatal exception" that is not one. The standard
    # PYTHONFAULTHANDLER name is honoured too, pointed at the log.
    if os.environ.get('PYTHONFAULTHANDLER') or os.environ.get('DIGIEMU_FAULTHANDLER'):
        try:
            import faulthandler
            faulthandler.enable(file=_log, all_threads=True)
        except (RuntimeError, ValueError, OSError):
            pass


def _log_thread_exceptions():
    """threading's default hook drops SystemExit without a word, and
    config.NotFound and device.DeviceError are SystemExit subclasses: say
    what killed the thread before the default handling."""
    import threading
    default = threading.excepthook

    def hook(args):
        if issubclass(args.exc_type, SystemExit) and sys.stderr is not None:
            print('thread %s exited: %s: %s' % (
                getattr(args.thread, 'name', '?'), args.exc_type.__name__,
                args.exc_value), file=sys.stderr, flush=True)
        default(args)

    threading.excepthook = hook


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def build_info():
    """-> the dict the spec wrote into the bundle (version, pinned hashes),
    or None when running from source."""
    try:
        with open(os.path.join(bundle_dir(), BUILD_INFO), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _inside(path, root):
    try:
        path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([path, root]) == root
    except ValueError:          # different drives
        return False


def _check_unicorn(info):
    import unicorn
    from unicorn.unicorn_py3.unicorn import uclib
    path = getattr(uclib, '_name', None) or ''
    out = {'version': getattr(unicorn, '__version__', None), 'path': path,
           'LIBUNICORN_PATH': os.environ.get('LIBUNICORN_PATH')}
    out['sha256'] = sha256_of(path) if os.path.isfile(path) else None
    if getattr(sys, 'frozen', False):
        if not _inside(path, bundle_dir()):
            raise RuntimeError('unicorn.dll loaded from outside the bundle: %r' % path)
        want = (info or {}).get('unicorn_sha256')
        if want and out['sha256'] != want:
            raise RuntimeError('unicorn.dll sha256 %s, but this build bundled %s'
                               % (out['sha256'], want))
    return out


def _check_compat():
    from emu import unicorn_compat
    result = unicorn_compat.require_compatible_unicorn()
    return {name: case['pass'] for name, case in result['cases'].items()}


def _check_native():
    from unicorn import UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, Uc
    from emu import native
    got = native.options_supported()
    uc = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
    out = {'options_supported': got, 'budget': native.budget_available(uc),
           'edma': native.edma_available(uc)}
    if got != NATIVE_OPTIONS or not (out['budget'] and out['edma']):
        raise RuntimeError('unicorn.dll lacks the speed patches: %r' % out)
    return out


def _check_capstone():
    import capstone
    from dt2 import coldfire
    lib = getattr(getattr(capstone, '_cs', None), '_name', None)
    got = list(coldfire.disasm(b'\x4e\x71', 0, 0, 2))
    if [g[2] for g in got] != ['nop']:
        raise RuntimeError('capstone decoded 4e71 as %r' % (got,))
    if getattr(sys, 'frozen', False) and not (lib and _inside(lib, bundle_dir())):
        raise RuntimeError('capstone.dll loaded from outside the bundle: %r' % lib)
    return {'version': getattr(capstone, '__version__', None), 'path': lib}


def _check_devices():
    from emu import device
    where = os.path.join(bundle_dir(), 'devices')
    found = device.load_all(where)
    if not found:
        raise RuntimeError('no device files in %s' % where)
    return {'dir': where, 'devices': [d.name for d in found]}


def _check_tk():
    import tkinter
    root = tkinter.Tk()
    try:
        root.withdraw()
        level = root.tk.call('info', 'patchlevel')
        root.update_idletasks()
    finally:
        root.destroy()
    return {'tcl': str(level), 'TCL_LIBRARY': os.environ.get('TCL_LIBRARY')}


def _check_imports(names=IMPORTS):
    """Import every module in `names`. Importing starts no emulation (the
    modules only define things and bind unicorn.dll's exports). All of them
    are tried, so the failure names every module that is missing or broken."""
    failed = []
    for name in names:
        try:
            _import_module(name)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:            # noqa: BLE001 -- SystemExit subclasses too
            failed.append('%s (%s: %s)' % (name, type(exc).__name__, exc))
    if failed:
        raise RuntimeError('cannot import %d of %d modules: %s'
                           % (len(failed), len(names), '; '.join(failed)))
    return {'imported': len(names)}


def run_selftest(on_step=None):
    """-> a JSON-able report; report['ok'] is True only if every check
    passed. Each check is isolated: one failing (or raising SystemExit, as
    device.DeviceError does) still lets the rest report.

    The only check that runs guest code comes last. Each check writes a line
    to stderr before it starts and another when it ends, and on_step(report)
    is called before each one with report['running'] naming it. A native
    crash inside Unicorn ends the process without the final report; the last
    line in stderr (or launcher.log), or the last report on_step saw, then
    says which check it was."""
    info = build_info()
    report = {'frozen': bool(getattr(sys, 'frozen', False)),
              'executable': sys.executable, 'bundle': bundle_dir(),
              'python': sys.version.split()[0], 'utf8_mode': sys.flags.utf8_mode,
              'build': info, 'checks': []}
    checks = [('unicorn', lambda: _check_unicorn(info)),
              ('native_options', _check_native),
              ('capstone', _check_capstone),
              ('devices', _check_devices),
              ('tk', _check_tk),
              ('imports', _check_imports),
              ('unicorn_compat', _check_compat)]
    report['ok'] = False
    for name, fn in checks:
        _note('selftest: %s ...' % name)
        report['running'] = name
        if on_step is not None:
            on_step(report)
        try:
            entry = {'name': name, 'ok': True, 'detail': fn()}
        except KeyboardInterrupt:
            raise
        except BaseException as exc:            # noqa: BLE001 -- SystemExit subclasses too
            entry = {'name': name, 'ok': False,
                     'detail': '%s: %s' % (type(exc).__name__, exc)}
        _note('selftest: %s %s' % (name, 'ok' if entry['ok'] else 'FAILED: %s' % entry['detail']))
        report['checks'].append(entry)
    report['running'] = None
    report['ok'] = all(c['ok'] for c in report['checks'])
    return report


def _note(text):
    if sys.stderr is not None:
        print(text, file=sys.stderr, flush=True)


def _write_atomic(path, text):
    path = os.path.abspath(path)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)
    os.replace(tmp, path)


def selftest(argv):
    """digiemu --selftest [--json OUT] -> exit status (0 = all checks pass)."""
    import argparse
    ap = argparse.ArgumentParser(prog='digiemu --selftest')
    ap.add_argument('--json', metavar='OUT', help='also write the report here')
    args = ap.parse_args(argv)

    def dump(report):
        return json.dumps(report, indent=2, sort_keys=True)

    def save(text):
        try:
            _write_atomic(args.json, text + '\n')
            return True
        except OSError as exc:
            _note('selftest: cannot write %s: %s' % (args.json, exc))
            return False

    writable = [bool(args.json)]

    def partial(report):
        # A crash then leaves a report that says ok: false and what ran.
        if writable[0]:
            writable[0] = save(dump(report))

    report = run_selftest(partial)
    text = dump(report)
    if sys.stdout is not None:
        print(text, flush=True)
    if args.json and not save(text):
        return 1
    return 0 if report['ok'] else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    setup_stdio()
    _log_thread_exceptions()
    if argv[:1] == ['--selftest']:
        return selftest(argv[1:])
    import emu.portable
    try:
        return emu.portable.main(argv)
    except SystemExit:
        raise
    except BaseException:
        # Log it, then let it propagate: the windowed bootloader shows it in
        # a dialog, and the exit status stays non-zero.
        import traceback
        traceback.print_exc()
        raise


if __name__ == '__main__':
    sys.exit(main())
