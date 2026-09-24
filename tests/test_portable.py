# pyright: reportMissingImports=false
"""The portable app's own logic: layout, environment, lock, protocol, flows.

No firmware, no emulator and no window. emu.release, emu.bootstrap and
emu.dtpanel are replaced by small stand-ins that follow their contracts
(emu.portable looks them up in sys.modules first for exactly this), and all
data goes to a temp home. Every test runs from an empty temp working
directory and checks it is still empty afterwards: the app must never write
relative to the cwd.
"""
import contextlib
import dataclasses
import datetime
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from emu import portable


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYX_BYTES = b'\xf0\x00\x20\x3c\x0a\x00\x7f' + b'synthetic, not firmware' + b'\xf7'
SYX_SHA = hashlib.sha256(SYX_BYTES).hexdigest()


# --- stand-ins for the contracts ---------------------------------------------

@dataclasses.dataclass
class FakePaths:
    root: str
    syx_name: str
    devices_dir: str

    @property
    def syx(self):
        return os.path.join(self.root, self.syx_name)

    @property
    def sections(self):
        return os.path.join(self.root, 'sections')

    @property
    def main_img(self):
        return os.path.join(self.sections, 'section_3_MAIN_OS.bin')

    @property
    def snapshots(self):
        return os.path.join(self.root, 'snapshots')

    @property
    def snapdir(self):
        return os.path.join(self.snapshots, os.path.splitext(self.syx_name)[0])

    @property
    def prefix(self):
        return os.path.join(self.snapdir, 'boot')

    @property
    def card(self):
        return os.path.join(self.root, 'plusdrive.img')

    @property
    def gui_raw(self):
        return os.path.join(self.snapdir, 'gui-raw.snap')

    @property
    def gui(self):
        return os.path.join(self.snapdir, 'gui.snap')

    @property
    def resume(self):
        return os.path.join(self.snapdir, 'resume.snap')

    @property
    def overlay(self):
        return os.path.join(self.root, 'devices')

    @property
    def logs(self):
        return os.path.join(self.root, 'logs')

    @property
    def state(self):
        return os.path.join(self.root, 'firmware.json')

    def env(self):
        return {'DT2_SYX': self.syx, 'DT2_SECTIONS': self.sections,
                'DT2_SNAPSHOTS': self.snapshots, 'DT2_PLUSDRIVE': self.card,
                'DT2_MAIN_IMG': self.main_img,
                'DT2_DEVICES': self.overlay if os.path.isdir(self.overlay)
                else self.devices_dir}


@dataclasses.dataclass
class Event:
    step: str
    kind: str
    done: int = 0
    total: int = 0
    text: str = ''
    data: dict = dataclasses.field(default_factory=dict)


class StepFailed(RuntimeError):
    def __init__(self, step, reason):
        super().__init__('%s: %s' % (step, reason))
        self.step, self.reason = step, reason


class FakeDeviceError(SystemExit):
    """Like emu.device.DeviceError: a SystemExit subclass."""


def _read_state(paths):
    try:
        with open(paths.state, 'rb') as fh:
            return json.loads(fh.read().decode('utf-8'))
    except FileNotFoundError:
        return {}


def _write_state(paths, state):
    tmp = paths.state + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(json.dumps(state, indent=1).encode('utf-8'))
    os.replace(tmp, paths.state)


def _card_stamp(path):
    st = os.stat(path)
    return {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


def make_bootstrap(first_run=None, choose=None):
    m = types.ModuleType('emu.bootstrap')
    m.FirmwarePaths = FakePaths
    m.Event = Event
    m.Cancelled = type('Cancelled', (Exception,), {})
    m.StepFailed = StepFailed
    m.read_state = _read_state
    m.write_state = _write_state
    m.card_stamp = _card_stamp
    m.calls = []

    def default_first_run(paths, progress=None, cancel=None):
        m.calls.append({'cwd': os.getcwd(), 'env': dict(os.environ),
                        'paths': paths})
        for ev in (Event('extract', 'start', text='extracting'),
                   Event('ladder', 'tick', 60, 400, '[60M] rung ✓'),
                   Event('settle', 'done', 1, 1, 'settled')):
            progress(ev)
        return paths.gui

    m.first_run = first_run or default_first_run
    m.choose_snapshot = choose or (lambda paths: (None, 'not-built'))
    return m


def make_release(rel):
    m = types.ModuleType('emu.release')

    class FirmwareError(ValueError):
        pass

    m.FirmwareError = FirmwareError
    m.overlays = []

    def identify_release(path, devices_dir):
        if rel is None:
            raise FirmwareError('no Elektron SysEx header')
        return rel

    def write_device_overlay(r, overlay_dir, devices_dir):
        os.makedirs(overlay_dir, exist_ok=True)
        out = os.path.join(overlay_dir, 'digitakt.toml')
        with open(out, 'wb') as fh:
            fh.write(b'# overlay\n')
        m.overlays.append((r.sha256, overlay_dir))
        return out

    m.identify_release = identify_release
    m.write_device_overlay = write_device_overlay
    m.canonical_syx_name = lambda r: 'Digitakt_OS%s.syx' % r.version
    return m


def make_panel(rc=0, save=True, raises=None):
    m = types.ModuleType('emu.dtpanel')
    m.calls = []

    def main(argv):
        m.calls.append(list(argv))
        if raises is not None:
            raise raises
        if save and '--save-on-exit' in argv:
            out = argv[argv.index('--save-on-exit') + 1]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out + '.tmp', 'wb') as fh:
                fh.write(b'resume')
            os.replace(out + '.tmp', out)
        return rc

    m.main = main
    return m


def release(status='known', short='dt1', product='Digitakt', version='9.99'):
    device = None if short is None else SimpleNamespace(short=short, name=product)
    return SimpleNamespace(
        device=device, firmware=None, product=product, version=version,
        build='0999', stamp=datetime.datetime(2026, 1, 2, 3, 4, 5),
        sha256=SYX_SHA, status=status, label='%s %s' % (product, version),
        slug='%s-%s-%s' % (short or 'x', version, SYX_SHA[:8]))


@contextlib.contextmanager
def stub_modules(_raw=None, **mods):
    """Put stand-ins at sys.modules['emu.<name>'] (and any `_raw` names) and
    restore exactly those keys afterwards (patch.dict would also drop
    modules imported meanwhile)."""
    keys = {'emu.' + k: v for k, v in mods.items()}
    keys.update(_raw or {})
    saved = {k: sys.modules.get(k) for k in keys}
    sys.modules.update(keys)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def status_with(paths, choose):
    """status_of with a bootstrap whose choose_snapshot is `choose`."""
    with stub_modules(bootstrap=make_bootstrap(choose=choose)):
        return portable.status_of(paths.root)


class Base(unittest.TestCase):
    """A temp home, an empty temp cwd, and os.environ restored afterwards."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, 'home')
        self.cwd = os.path.join(self.tmp, 'cwd')
        os.makedirs(self.cwd)
        self.src = os.path.join(self.tmp, 'my firmware ✓.syx')
        with open(self.src, 'wb') as fh:
            fh.write(SYX_BYTES)
        self._old_cwd = os.getcwd()
        os.chdir(self.cwd)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.startswith('DT2_')]:
            del os.environ[k]
        os.environ.pop('DIGIEMU_HOME', None)
        os.environ.pop('OneDrive', None)
        os.environ.pop('ONEDRIVE', None)
        os.environ.pop('OneDriveConsumer', None)
        os.environ.pop('ONEDRIVECONSUMER', None)
        os.environ.pop('OneDriveCommercial', None)
        os.environ.pop('ONEDRIVECOMMERCIAL', None)
        space = mock.patch('shutil.disk_usage',
                           return_value=SimpleNamespace(free=10 ** 12))
        space.start()
        self.addCleanup(space.stop)
        # Only test_too_long_a_path_is_refused is about the path limit; the
        # rest must pass whatever TEMP is (a long one trips the real 240).
        limit = mock.patch.object(portable, 'MAX_PATH_CHARS', 10_000)
        limit.start()
        self.addCleanup(limit.stop)

    def tearDown(self):
        os.chdir(self._old_cwd)
        leftover = os.listdir(self.cwd)
        self._tmp.cleanup()
        self.assertEqual(leftover, [], 'something was written to the cwd')

    def make_folder(self, device='dt1', card=True):
        """A firmware folder as --add leaves it."""
        fwdir = portable.firmware_dir('dt1-9.99-%s' % SYX_SHA[:8], self.home)
        paths = FakePaths(fwdir, 'Digitakt_OS9.99.syx', portable.devices_dir())
        os.makedirs(fwdir)
        with open(paths.syx, 'wb') as fh:
            fh.write(SYX_BYTES)
        if card:
            with open(paths.card, 'wb') as fh:
                fh.write(b'\0' * 512)
        rec = portable.release_record(release(), paths.syx_name)
        rec['device'] = device
        _write_state(paths, {'release': rec, 'app_version': portable.APP_VERSION,
                             'stages': {'extract': 1, 'card': 1, 'ladder': 1,
                                        'intro': 1, 'settle': 1}})
        return paths


# --- layout ----------------------------------------------------------------------

class LayoutTest(Base):
    def test_dev_root_is_repo_portable_then_env_then_home(self):
        self.assertFalse(portable.is_frozen())
        self.assertEqual(portable.app_root(), os.path.join(REPO, 'portable'))
        os.environ['DIGIEMU_HOME'] = self.tmp
        self.assertEqual(portable.app_root(), self.tmp)
        self.assertEqual(portable.app_root(self.home), self.home)
        self.assertEqual(portable.bundle_dir(), REPO)
        self.assertEqual(portable.devices_dir(), os.path.join(REPO, 'devices'))

    def test_frozen_root_is_the_exe_folder(self):
        exe = os.path.join(self.tmp, 'dist', 'digiemu.exe')
        meipass = os.path.join(self.tmp, 'dist', '_internal')
        os.environ['DIGIEMU_HOME'] = self.home     # ignored when frozen
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, '_MEIPASS', meipass, create=True), \
                mock.patch.object(sys, 'executable', exe):
            self.assertEqual(portable.app_root(), os.path.dirname(exe))
            self.assertEqual(portable.bundle_dir(), meipass)
            self.assertEqual(portable.devices_dir(), os.path.join(meipass, 'devices'))
            self.assertEqual(portable.firmware_root(),
                             os.path.join(os.path.dirname(exe), 'firmware'))
            self.assertEqual(portable.app_root(self.home), self.home)

    def test_slugs(self):
        good = 'dt1-1.53-9bdd44bb'
        self.assertEqual(portable.check_slug(good), good)
        self.assertEqual(portable.firmware_dir(good, self.home),
                         os.path.join(self.home, 'firmware', good))
        for bad in ('', 'CON', 'con', 'nul.1', 'com1', 'lpt9.x', 'aux',
                    'Dt1-1.53', 'a/b', 'a\\b', '../x', '..', '.x', 'x.', 'a b',
                    'x' * 65, 'é', None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                portable.check_slug(bad)
        self.assertEqual(portable.check_slug('con-1.53'), 'con-1.53')

    def test_list_ignores_what_is_not_a_firmware_folder(self):
        base = portable.firmware_root(self.home)
        for name in ('dt1-1.53-aaaaaaaa', 'Not A Slug', 'con'):
            os.makedirs(os.path.join(base, name))
        with open(os.path.join(base, 'dt1-file'), 'wb'):
            pass
        self.assertEqual(portable.list_firmware_dirs(self.home),
                         [os.path.join(base, 'dt1-1.53-aaaaaaaa')])
        self.assertEqual(portable.list_firmware_dirs(os.path.join(self.tmp, 'none')),
                         [])

    def test_longest_path_is_the_deepest_snapshot_tmp(self):
        fwdir = os.path.join(self.home, 'firmware', 'dt1-9.99-12345678')
        deepest = portable.longest_path(fwdir, 'Digitakt_OS9.99.syx')
        self.assertTrue(deepest.startswith(fwdir + os.sep))
        self.assertIn(os.path.join('snapshots', 'Digitakt_OS9.99'), deepest)
        # The longest name there: a settle set aside after failing
        # acceptance (17 characters; gui-raw.snap.tmp is 16).
        self.assertEqual(os.path.basename(deepest), 'gui.snap.rejected')


# --- environment -------------------------------------------------------------------

class TkLibraryEnvTest(unittest.TestCase):
    """_tk_library_env: from source on macOS, point Tcl/Tk at the base
    Python's scripts (a uv-managed Python's venv hides them); elsewhere,
    and in the frozen app, change nothing. It never writes files."""

    def setUp(self):
        try:
            import tkinter
        except ImportError:
            self.skipTest('no tkinter')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.prefix = tmp.name
        self.tcl = os.path.join(self.prefix, 'lib', 'tcl%s' % tkinter.TclVersion)
        self.tk = os.path.join(self.prefix, 'lib', 'tk%s' % tkinter.TkVersion)
        for d, marker in ((self.tcl, 'init.tcl'), (self.tk, 'tk.tcl')):
            os.makedirs(d)
            open(os.path.join(d, marker), 'w').close()
        # Another version beside them must not be picked.
        os.makedirs(os.path.join(self.prefix, 'lib', 'tcl9.9'))
        open(os.path.join(self.prefix, 'lib', 'tcl9.9', 'init.tcl'), 'w').close()

    def env(self, platform, **env):
        before = sorted(os.listdir(os.path.join(self.prefix, 'lib')))
        out = portable._tk_library_env(dict(env), platform=platform, prefix=self.prefix)
        self.assertEqual(sorted(os.listdir(os.path.join(self.prefix, 'lib'))), before)
        return out

    def test_macos_gets_the_base_pythons_scripts(self):
        self.assertEqual(self.env('darwin'),
                         {'TCL_LIBRARY': self.tcl, 'TK_LIBRARY': self.tk})

    def test_a_working_setting_is_kept_and_a_stale_one_replaced(self):
        keep = self.prefix
        out = self.env('darwin', TCL_LIBRARY=keep, TK_LIBRARY='/nowhere/tk8.6')
        self.assertEqual(out, {'TCL_LIBRARY': keep, 'TK_LIBRARY': self.tk})

    def test_other_platforms_and_the_frozen_app_are_left_alone(self):
        for platform in ('win32', 'linux'):
            self.assertEqual(self.env(platform), {})
        with mock.patch.object(portable, 'is_frozen', return_value=True):
            self.assertEqual(self.env('darwin'), {})


class EnvTest(Base):
    def test_apply_env_assigns_absolute_paths_and_moves_cwd(self):
        paths = self.make_folder()
        os.environ['DT2_SYX'] = os.path.join(self.tmp, 'stale.syx')
        with stub_modules(bootstrap=make_bootstrap()):
            opened = portable.open_paths(paths.root)
            portable.apply_env(opened)
        self.assertEqual(os.path.normcase(os.getcwd()), os.path.normcase(paths.root))
        for key in ('DT2_SYX', 'DT2_SECTIONS', 'DT2_SNAPSHOTS', 'DT2_PLUSDRIVE',
                    'DT2_MAIN_IMG', 'DT2_DEVICES'):
            self.assertTrue(os.path.isabs(os.environ[key]), key)
        self.assertEqual(os.environ['DT2_SYX'], paths.syx)
        self.assertEqual(os.environ['DT2_DEVICES'], portable.devices_dir())
        self.assertEqual(os.environ['PYTHONUTF8'], '1')
        self.assertEqual(os.environ['PYTHONIOENCODING'], 'utf-8')

    def test_open_paths_uses_the_recorded_name(self):
        paths = self.make_folder()
        with open(os.path.join(paths.root, 'other.syx'), 'wb'):
            pass
        with stub_modules(bootstrap=make_bootstrap()):
            self.assertEqual(portable.open_paths(paths.root).syx_name,
                             'Digitakt_OS9.99.syx')
            with self.assertRaises(portable.FolderError):
                portable.open_paths(os.path.join(self.tmp, 'missing'))

    def test_child_env(self):
        os.environ['DT2_SYX'] = 'x.syx'
        os.environ['PYTHONPATH'] = 'elsewhere'
        env = portable.child_env()
        self.assertFalse([k for k in env if k.upper().startswith('DT2_')])
        self.assertEqual(env['PYTHONUTF8'], '1')
        self.assertEqual(env['PYTHONIOENCODING'], 'utf-8')
        self.assertEqual(env['PYTHONUNBUFFERED'], '1')
        self.assertEqual(env['PYTHONPATH'].split(os.pathsep)[0], REPO)
        os.environ['PYTHONHOME'] = 'somewhere'
        with mock.patch.object(sys, 'frozen', True, create=True):
            env = portable.child_env()
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('PYTHONHOME', env)


# --- the lock ------------------------------------------------------------------------

class LockTest(Base):
    def test_second_handle_is_refused_until_released(self):
        fwdir = os.path.join(self.tmp, 'fw')
        os.makedirs(fwdir)
        first = portable.FolderLock(fwdir, 'first-run').acquire()
        try:
            with self.assertRaises(portable.Busy) as cm:
                portable.FolderLock(fwdir, 'panel').acquire(0)
            self.assertIn('first-run pid %d' % os.getpid(), cm.exception.holder)
            self.assertIn('first-run', portable.lock_holder(fwdir))
        finally:
            first.release()
        self.assertIsNone(portable.lock_holder(fwdir))
        with portable.FolderLock(fwdir, 'panel').acquire(0):
            pass

    def test_another_process_is_refused(self):
        fwdir = os.path.join(self.tmp, 'fw')
        os.makedirs(fwdir)
        code = ('import sys\n'
                'from emu import portable\n'
                'lock = portable.FolderLock(sys.argv[1], "panel").acquire()\n'
                'print("held", flush=True)\n'
                'sys.stdin.read()\n'
                'lock.release()\n')
        env = dict(os.environ, PYTHONPATH=REPO)
        proc = subprocess.Popen([sys.executable, '-c', code, fwdir], cwd=self.tmp,
                                env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE)
        try:
            self.assertEqual(proc.stdout.readline().strip(), b'held')
            with self.assertRaises(portable.Busy) as cm:
                portable.FolderLock(fwdir, 'first-run').acquire(0)
            # Not proc.pid: a venv's python.exe is a redirector that starts
            # the real interpreter as a second process.
            self.assertIn('panel pid ', str(cm.exception))
            self.assertNotIn('pid %d' % os.getpid(), str(cm.exception))
        finally:
            proc.stdin.close()
            proc.wait(30)
            proc.stdout.close()
        with portable.FolderLock(fwdir, 'first-run').acquire(5):
            pass

    def test_other_folders_are_independent(self):
        a, b = os.path.join(self.tmp, 'a'), os.path.join(self.tmp, 'b')
        os.makedirs(a)
        os.makedirs(b)
        with portable.FolderLock(a).acquire(0), portable.FolderLock(b).acquire(0):
            pass


# --- the protocol ------------------------------------------------------------------------

class ProtocolTest(Base):
    def test_lines_are_ascii_json_and_round_trip(self):
        rec = portable.event_record(Event('ladder', 'tick', 60, 400,
                                          'C:\\Zoë\\中文 [x]\\boot60M.snap',
                                          {'lit': 12}))
        line = portable.encode_line(rec)
        self.assertNotIn('\n', line)
        line.encode('ascii')
        self.assertEqual(portable.decode_line(line + '\r\n'), rec)
        self.assertEqual(portable.decode_line(line.encode('ascii')), rec)
        self.assertEqual(set(rec), {'step', 'kind', 'done', 'total', 'text', 'data'})

    def test_odd_data_never_loses_the_event(self):
        rec = portable.event_record({'step': 'settle', 'kind': 'note',
                                     'data': {(1, 2): 'tuple key'}})
        back = portable.decode_line(portable.encode_line(rec))
        self.assertEqual(back['step'], 'settle')
        self.assertIn('repr', back['data'])
        rec = portable.event_record({'step': 'intro', 'kind': 'note',
                                     'data': {'path': object()}})
        self.assertEqual(portable.decode_line(portable.encode_line(rec))['kind'],
                         'note')

    def test_non_protocol_lines_are_ignored(self):
        for junk in ('', 'TASK_CREATE 0x1234', 'Traceback (most recent call last):',
                     '{not json', '[1, 2]', '{"no": "kind"}'):
            self.assertIsNone(portable.decode_line(junk), junk)

    def test_result_record(self):
        self.assertEqual(portable.result_record(True, 'ignored'),
                         {'kind': 'result', 'ok': True, 'error': None})
        rec = portable.result_record(False, 'boom', 'ladder')
        self.assertEqual((rec['ok'], rec['error'], rec['step']),
                         (False, 'boom', 'ladder'))

    def test_progress_never_goes_backwards(self):
        self.assertAlmostEqual(sum(portable.STEP_WEIGHTS.values()), 1.0)
        m = portable.ProgressModel()
        seen = []
        for rec in ({'step': 'ladder', 'kind': 'tick', 'done': 200, 'total': 400},
                    {'step': 'extract', 'kind': 'done'},      # a resumed run
                    {'step': 'ladder', 'kind': 'done'},
                    {'step': 'intro', 'kind': 'start'},
                    {'step': 'settle', 'kind': 'tick', 'done': 1500, 'total': 1000},
                    {'step': 'settle', 'kind': 'note', 'text': 'still going'}):
            m.feed(rec)
            seen.append(m.fraction)
        self.assertEqual(seen, sorted(seen))
        self.assertAlmostEqual(seen[0], 0.55 * 0.5)
        self.assertTrue(m.indeterminate)
        m.feed(portable.result_record(True))
        self.assertEqual(m.fraction, 1.0)

    def test_console_progress(self):
        out = io.StringIO()
        con = portable.ConsoleProgress(out, every=3600)
        con({'step': 'ladder', 'kind': 'start', 'text': 'ladder ...'})
        con({'step': 'ladder', 'kind': 'tick', 'done': 1, 'total': 2})   # throttled
        con({'step': 'ladder', 'kind': 'done', 'text': 'ladder done in 300 s'})
        self.assertEqual(out.getvalue().splitlines(),
                         ['[  0%] ladder: ladder ...',
                          '[ 55%] ladder: ladder done in 300 s'])
        con = portable.ConsoleProgress(out, every=0)
        con({'step': 'settle', 'kind': 'tick', 'done': 450_000_000,
             'total': 3_000_000_000})
        self.assertIn('settle: 450M of 3000M instructions',
                      out.getvalue().splitlines()[-1])

    def test_progress_prefers_the_bootstraps_overall(self):
        m = portable.ProgressModel()
        m.feed({'step': 'settle', 'kind': 'tick', 'done': 5, 'total': 3000,
                'data': {'overall': 0.9, 'indeterminate': True}})
        self.assertEqual((m.step, m.fraction, m.indeterminate), ('settle', 0.9, True))
        m.feed({'step': 'settle', 'kind': 'tick', 'data': {'overall': 0.5}})
        self.assertEqual(m.fraction, 0.9)
        m.feed({'step': 'settle', 'kind': 'done', 'data': {'overall': 1.0}})
        self.assertEqual((m.fraction, m.indeterminate), (1.0, False))

    def test_worker_first_run_streams_events_and_a_result(self):
        paths = self.make_folder()
        boot = make_bootstrap()
        real = boot.first_run

        def noisy(p, progress=None, cancel=None):
            print('rung line from inside a hook ✓')     # must reach the log
            return real(p, progress=progress, cancel=cancel)

        boot.first_run = noisy
        out = io.StringIO()
        with stub_modules(bootstrap=boot):
            rc = portable.worker_first_run(paths.root, proto=out)
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        recs = [portable.decode_line(line) for line in lines]
        self.assertNotIn(None, recs, lines)
        for line in lines:
            line.encode('ascii')
        self.assertEqual([r['step'] for r in recs[:-1]],
                         ['extract', 'ladder', 'settle'])
        self.assertEqual(recs[1]['text'], '[60M] rung ✓')
        self.assertEqual(recs[-1]['kind'], 'result')
        self.assertIs(recs[-1]['ok'], True)
        self.assertIsNone(recs[-1]['error'])
        self.assertEqual(boot.calls[0]['env']['DT2_SYX'], paths.syx)
        self.assertEqual(os.path.normcase(boot.calls[0]['cwd']),
                         os.path.normcase(paths.root))
        with open(os.path.join(paths.logs, 'first-run.log'), 'rb') as fh:
            log = fh.read().decode('utf-8')
        self.assertIn('rung line from inside a hook ✓', log)
        self.assertNotIn(b'\r\n', log.encode('utf-8'))
        self.assertEqual(_read_state(paths)['app_version'], portable.APP_VERSION)
        self.assertIsNone(portable.lock_holder(paths.root))

    def test_worker_first_run_failure_is_a_result_not_a_crash(self):
        paths = self.make_folder()

        def fails(p, progress=None, cancel=None):
            progress(Event('ladder', 'start'))
            raise FakeDeviceError('No device file matches this firmware')

        out = io.StringIO()
        with stub_modules(bootstrap=make_bootstrap(first_run=fails)):
            rc = portable.worker_first_run(paths.root, proto=out)
        self.assertEqual(rc, 1)
        last = portable.decode_line(out.getvalue().splitlines()[-1])
        self.assertEqual(last['kind'], 'result')
        self.assertIs(last['ok'], False)
        self.assertIn('No device file matches', last['error'])
        self.assertEqual(last['step'], 'ladder')

        def step_failed(p, progress=None, cancel=None):
            raise StepFailed('settle', 'NOT SETTLED after 3000M')

        out = io.StringIO()
        with stub_modules(bootstrap=make_bootstrap(first_run=step_failed)):
            self.assertEqual(portable.worker_first_run(paths.root, proto=out), 1)
        last = portable.decode_line(out.getvalue().splitlines()[-1])
        self.assertEqual((last['step'], last['error']),
                         ('settle', 'settle: NOT SETTLED after 3000M'))

    def test_worker_first_run_on_a_missing_folder(self):
        out = io.StringIO()
        with stub_modules(bootstrap=make_bootstrap()):
            rc = portable.worker_first_run(os.path.join(self.tmp, 'gone'), proto=out)
        self.assertEqual(rc, 1)
        (line,) = out.getvalue().splitlines()
        self.assertIs(portable.decode_line(line)['ok'], False)

    def test_a_failure_before_the_build_still_reaches_the_log(self):
        paths = self.make_folder()
        boot = make_bootstrap()

        def broken(p):
            raise ImportError('DLL load failed while importing unicorn')

        boot.read_state = broken
        out = io.StringIO()
        with stub_modules(bootstrap=boot):
            self.assertEqual(portable.worker_first_run(paths.root, proto=out), 1)
        self.assertIn('DLL load failed',
                      portable.decode_line(out.getvalue().splitlines()[-1])['error'])
        with open(os.path.join(paths.logs, 'first-run.log'), 'rb') as fh:
            self.assertIn(b'DLL load failed', fh.read())

    def test_worker_refuses_a_folder_in_use(self):
        paths = self.make_folder()
        out = io.StringIO()
        with portable.FolderLock(paths.root, 'panel').acquire(), \
                mock.patch.object(portable, 'LOCK_WAIT', 0.0), \
                stub_modules(bootstrap=make_bootstrap()):
            self.assertEqual(portable.worker_first_run(paths.root, proto=out), 1)
        last = portable.decode_line(out.getvalue().splitlines()[-1])
        self.assertIn('in use', last['error'])

    def test_a_closed_pipe_does_not_stop_the_build(self):
        class Closed:
            def write(self, s):
                raise OSError(22, 'Invalid argument')

            def flush(self):
                pass

        paths = self.make_folder()
        with stub_modules(bootstrap=make_bootstrap()):
            self.assertEqual(portable.worker_first_run(paths.root, proto=Closed()), 0)


# --- child processes ----------------------------------------------------------------------

class CommandTest(Base):
    def test_frozen_uses_the_exe_itself(self):
        exe = os.path.join(self.tmp, 'digiemu.exe')
        fwdir = os.path.join(self.home, 'firmware', 'dt1-1.53-9bdd44bb')
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'executable', exe):
            self.assertEqual(portable.worker_command('first-run', fwdir),
                             [exe, '--worker', 'first-run', fwdir])
            self.assertEqual(portable.worker_command('first-run', fwdir, '--rebuild'),
                             [exe, '--worker', 'first-run', fwdir, '--rebuild'])
            self.assertEqual(portable.spawn_options(False)['cwd'], self.tmp)

    def test_dev_uses_dash_m(self):
        fwdir = os.path.join(self.home, 'firmware', 'dt1-1.53-9bdd44bb')
        self.assertEqual(portable.worker_command('panel', fwdir),
                         [sys.executable, '-m', 'emu.portable', '--worker', 'panel',
                          fwdir])
        rel = portable.worker_command('panel', 'relative')
        self.assertTrue(os.path.isabs(rel[-1]))

    def test_spawn_options(self):
        os.environ['DT2_SYX'] = 'x.syx'
        piped = portable.spawn_options(True)
        self.assertEqual(piped['stdout'], subprocess.PIPE)
        self.assertEqual(piped['stderr'], subprocess.STDOUT)
        self.assertEqual(piped['stdin'], subprocess.DEVNULL)
        self.assertNotIn('DT2_SYX', piped['env'])
        plain = portable.spawn_options(False)
        self.assertEqual(plain['stdout'], subprocess.DEVNULL)
        if sys.platform == 'win32':
            self.assertTrue(piped['creationflags'] & portable.CREATE_NO_WINDOW)
            self.assertTrue(plain['creationflags'] & portable.CREATE_NO_WINDOW)

    def test_exit_codes_in_words(self):
        self.assertEqual(portable.describe_exit(3), 'exited with code 3')
        self.assertIn('access violation', portable.describe_exit(3221225477))
        self.assertIn('access violation', portable.describe_exit(-1073741819))
        self.assertIn('0xC0001234', portable.describe_exit(0xC0001234))
        self.assertIn('signal 9', portable.describe_exit(-9))

    def test_faulthandler_is_opt_in_on_windows(self):
        os.environ.pop('DIGIEMU_FAULTHANDLER', None)
        self.assertEqual(portable._want_faulthandler(), os.name != 'nt')
        os.environ['DIGIEMU_FAULTHANDLER'] = '1'
        self.assertTrue(portable._want_faulthandler())

    def test_power_throttling_opt_out_never_raises(self):
        self.assertIsInstance(portable.disable_power_throttling(), bool)

    def test_dev_worker_runs_as_a_child(self):
        """The real spawn path: `python -m emu.portable --worker first-run`
        on a folder that does not exist ends in one result line."""
        missing = os.path.join(self.tmp, 'missing')
        proc = subprocess.run(portable.worker_command('first-run', missing),
                              **dict(portable.spawn_options(True),
                                     stdout=subprocess.PIPE), timeout=120)
        lines = proc.stdout.decode('utf-8', 'replace').splitlines()
        recs = [r for r in map(portable.decode_line, lines) if r]
        self.assertEqual(proc.returncode, 1, lines)
        self.assertEqual(recs[-1]['kind'], 'result')
        self.assertIn('No firmware folder', recs[-1]['error'])

    def test_add_relays_a_real_worker_child(self):
        """build_in_worker (what --add and --rebuild NAME use) through the
        real spawn: the child's result line becomes the console's verdict."""
        missing = FakePaths(os.path.join(self.tmp, 'gone'), 'x.syx', REPO)
        out = io.StringIO()
        rc = portable.build_in_worker(missing, 'Nothing', out)
        self.assertEqual(rc, portable.EXIT_FAILED, out.getvalue())
        self.assertIn('Setup failed: No firmware folder', out.getvalue())
        self.assertIn(os.path.join(missing.logs, 'first-run.log'), out.getvalue())


# --- --add -------------------------------------------------------------------------------------

class AddTest(Base):
    """--add, --rebuild NAME and --reset NAME. The first-run worker child is
    run in this process (inprocess_worker), so the stand-ins apply to it and
    its JSON lines come back through the same pipe-reading code."""

    def setUp(self):
        super().setUp()
        self.spawned = []
        self.spawn_error = None
        self.fake_proc = None
        spawn = mock.patch.object(portable, 'spawn_worker', self.inprocess_worker)
        spawn.start()
        self.addCleanup(spawn.stop)

    def inprocess_worker(self, mode, fwdir, *extra, piped=False,
                         kill_on_close=False):
        self.spawned.append((mode, fwdir, extra, piped))
        if self.spawn_error is not None:
            raise self.spawn_error
        if self.fake_proc is not None:
            return self.fake_proc
        buf = io.StringIO()
        cwd, env = os.getcwd(), dict(os.environ)
        try:
            rc = portable.worker_first_run(fwdir, rebuild='--rebuild' in extra,
                                           proto=buf)
        finally:
            # A real child's chdir and DT2_* never reach the parent.
            os.chdir(cwd)
            os.environ.clear()
            os.environ.update(env)
        return FakeProc(buf.getvalue().splitlines(), rc)

    def run_add(self, rel, yes=False, boot=None, rmod=None):
        rmod = rmod or make_release(rel)
        boot = boot or make_bootstrap()
        out = io.StringIO()
        with stub_modules(bootstrap=boot, release=rmod):
            rc = portable.add_firmware(self.src, yes=yes, home=self.home, out=out)
        return rc, out.getvalue(), boot, rmod

    def run_main(self, argv, boot=None, rel=None):
        boot = boot or make_bootstrap()
        with stub_modules(bootstrap=boot, release=make_release(rel or release())), \
                mock.patch('sys.stdout', io.StringIO()) as out, \
                mock.patch('sys.stderr', io.StringIO()) as err:
            rc = portable.main(list(argv) + ['--home', self.home])
        return rc, out.getvalue() + err.getvalue(), boot

    # a folder that is already here

    def test_a_ready_folder_is_not_set_up_again(self):
        paths = self.make_folder()
        _write_state(paths, dict(_read_state(paths), resume={'size': 512}))
        before = _read_state(paths)
        rc, text, boot, _ = self.run_add(
            release(), boot=make_bootstrap(choose=lambda p: (p.gui, 'gui')))
        self.assertEqual(rc, portable.EXIT_OK, text)
        self.assertIn('already set up: Digitakt 9.99 (Ready)', text)
        self.assertEqual((self.spawned, boot.calls), ([], []))
        self.assertEqual(_read_state(paths), before)

    def test_rebuild_needed_is_exit_3_with_the_rebuild_command(self):
        paths = self.make_folder()
        rc, text, boot, _ = self.run_add(
            release(), boot=make_bootstrap(choose=lambda p: (None, 'card-changed')))
        self.assertEqual(rc, portable.EXIT_NOT_READY, text)
        self.assertIn('already set up: Digitakt 9.99 (Rebuild needed)', text)
        self.assertIn('--rebuild %s' % os.path.basename(paths.root), text)
        self.assertEqual((self.spawned, boot.calls), ([], []))

    def test_an_open_folder_is_left_alone(self):
        paths = self.make_folder()
        with portable.FolderLock(paths.root, 'panel').acquire():
            rc, text, boot, _ = self.run_add(release())
        self.assertEqual(rc, portable.EXIT_OK, text)
        self.assertIn('already set up: Digitakt 9.99 (Running)', text)
        self.assertEqual(self.spawned, [])
        with portable.FolderLock(paths.root, 'first-run').acquire():
            rc, text, boot, _ = self.run_add(release())
        self.assertEqual(rc, portable.EXIT_BUSY, text)
        self.assertEqual(self.spawned, [])

    # the build goes through the worker child

    def test_the_build_runs_in_the_launchers_worker(self):
        rel = release()
        rc, text, _, _ = self.run_add(rel)
        self.assertEqual(rc, 0, text)
        fwdir = portable.firmware_dir(rel.slug, self.home)
        self.assertEqual(self.spawned, [('first-run', fwdir, (), True)])
        # Relayed as console progress, the result line itself not printed.
        self.assertIn('extract: extracting', text)
        self.assertIn('settle: settled', text)
        self.assertIn('is ready', text)
        self.assertNotIn('"kind"', text)

    def test_a_crashed_worker_is_named_with_the_log(self):
        rel = release()
        start = portable.encode_line(portable.event_record(
            Event('ladder', 'start', text='ladder ...')))
        self.fake_proc = FakeProc([start, 'a stray native line'], rc=0xC0000409)
        rc, text, _, _ = self.run_add(rel)
        self.assertEqual(rc, portable.EXIT_FAILED)
        self.assertIn('crashed (stack buffer overrun, 0xC0000409)', text)
        self.assertIn('a stray native line', text)
        log = os.path.join(portable.firmware_dir(rel.slug, self.home), 'logs',
                           'first-run.log')
        self.assertIn('Log: %s' % log, text)
        with open(log, 'rb') as fh:
            self.assertIn(b'without reporting a result', fh.read())

    def test_without_a_worker_process_it_builds_here(self):
        self.spawn_error = OSError(8, 'Not enough memory resources')
        rc, text, boot, _ = self.run_add(release())
        self.assertEqual(rc, 0, text)
        self.assertIn('cannot start a worker process', text)
        self.assertEqual(len(boot.calls), 1)
        self.assertEqual(os.path.normcase(os.getcwd()), os.path.normcase(self.cwd))

    # --rebuild NAME

    def test_rebuild_cli_rebuilds_a_ready_folder_in_the_worker(self):
        paths = self.make_folder()
        slug = os.path.basename(paths.root)
        rc, text, boot = self.run_main(
            ['--rebuild', slug], boot=make_bootstrap(choose=lambda p: (p.gui, 'gui')))
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.spawned, [('first-run', paths.root, ('--rebuild',), True)])
        self.assertIn('Rebuilding Digitakt 9.99', text)
        self.assertEqual(len(boot.calls), 1)

    def test_rebuild_cli_resumes_an_unfinished_setup(self):
        paths = self.make_folder()
        rc, text, _ = self.run_main(['--rebuild', paths.root])     # a path works too
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.spawned, [('first-run', paths.root, (), True)])

    def test_rebuild_cli_usage(self):
        self.make_folder()
        rc, text, _ = self.run_main(['--rebuild', 'dt1-0.00-00000000'])
        self.assertEqual(rc, portable.EXIT_USAGE)
        self.assertIn('dt1-9.99-%s' % SYX_SHA[:8], text)        # what is here
        rc, text, _ = self.run_main(['--rebuild'])
        self.assertEqual(rc, portable.EXIT_USAGE)
        self.assertIn('needs the name', text)
        rc, _, _ = self.run_main(['--rebuild', 'x', '--list'])
        self.assertEqual(rc, portable.EXIT_USAGE)
        self.assertEqual(self.spawned, [])

    def test_rebuild_cli_refuses_a_folder_in_use(self):
        paths = self.make_folder()
        with portable.FolderLock(paths.root, 'panel').acquire():
            rc, text, _ = self.run_main(['--rebuild', os.path.basename(paths.root)])
        self.assertEqual(rc, portable.EXIT_BUSY, text)
        self.assertEqual(self.spawned, [])

    # --check SYX

    def check_proc(self, passed=True, ok=True):
        done = {'kind': 'done', 'step': 'boot', 'text': '',
                'data': {'role': 'build', 'state': 'passed', 'passed': True}}
        result = portable.result_record(ok, None if ok else 'boom',
                                        log=portable.CHECK_LOG)
        result['data'] = {'passed': passed, 'summary': [
            'build: %s' % ('PASS' if passed else 'FAIL')]} if ok else {}
        return FakeProc([portable.encode_line(done),
                         portable.encode_line(result)], 0 if ok else 1)

    def request_of(self, checkdir):
        with open(os.path.join(checkdir, portable.CHECK_REQUEST),
                  encoding='utf-8') as fh:
            return json.load(fh)

    def test_check_cli_compares_with_the_stock_set_up_here(self):
        stock = self.make_folder()
        self.fake_proc = self.check_proc(passed=True)
        rc, text, _ = self.run_main(['--check', self.src],
                                    rel=release('untested'))
        self.assertEqual(rc, portable.EXIT_OK, text)
        [(mode, checkdir, extra, piped)] = self.spawned
        self.assertEqual((mode, extra, piped), ('check', (), True))
        req = self.request_of(checkdir)
        self.assertEqual((req['syx'], req['baseline'], req['timing']),
                         (self.src, stock.syx, False))
        self.assertIn('Comparing with Digitakt 9.99', text)
        self.assertIn('Build  boot       passed', text)
        self.assertIn('build: PASS', text)
        self.assertIn(os.path.join(checkdir, 'report.json'), text)

    def test_check_cli_a_failing_build_is_exit_1(self):
        self.fake_proc = self.check_proc(passed=False)
        rc, text, _ = self.run_main(['--check', self.src, '--timing'],
                                    rel=release('untested'))
        self.assertEqual(rc, portable.EXIT_FAILED, text)
        self.assertIn('No stock firmware to compare with', text)
        self.assertIn('build: FAIL', text)
        req = self.request_of(self.spawned[0][1])
        self.assertEqual((req['baseline'], req['timing']), (None, True))

    def test_check_cli_when_the_check_cannot_finish(self):
        self.fake_proc = self.check_proc(ok=False)
        rc, text, _ = self.run_main(['--check', self.src],
                                    rel=release('untested'))
        self.assertEqual(rc, portable.EXIT_FAILED)
        self.assertIn('The check could not finish: boom', text)
        self.assertIn(portable.CHECK_LOG, text)

    def test_check_cli_usage(self):
        rc, text, _ = self.run_main(['--timing'])
        self.assertEqual(rc, portable.EXIT_USAGE)
        self.assertIn('--check', text)
        rc, text, _ = self.run_main(
            ['--check', self.src],
            rel=release('unsupported', short=None, product='Syntakt'))
        self.assertEqual(rc, portable.EXIT_UNSUPPORTED, text)
        self.assertEqual(self.spawned, [])

    # --reset NAME

    def test_reset_cli_needs_yes_then_resets(self):
        paths = self.make_folder()
        slug = os.path.basename(paths.root)
        rc, text, _ = self.run_main(['--reset', slug])
        self.assertEqual(rc, portable.EXIT_NEEDS_YES)
        self.assertIn('--yes', text)
        self.assertTrue(os.path.exists(paths.card))
        rc, text, _ = self.run_main(['--reset', slug, '--yes'])
        self.assertEqual(rc, portable.EXIT_OK, text)
        self.assertFalse(os.path.exists(paths.card))
        self.assertIn('--rebuild %s' % slug, text)
        self.assertEqual(self.spawned, [])                  # the CLI only says how
        rc, _, _ = self.run_main(['--reset', 'dt1-0.00-00000000', '--yes'])
        self.assertEqual(rc, portable.EXIT_USAGE)

    def test_unsupported_is_refused_by_name(self):
        rc, text, boot, _ = self.run_add(release('unsupported', short=None,
                                                 product='Syntakt'))
        self.assertEqual(rc, portable.EXIT_UNSUPPORTED)
        self.assertIn('Syntakt', text)
        self.assertFalse(os.path.exists(portable.firmware_root(self.home)))
        self.assertEqual(boot.calls, [])

    def test_known_product_without_a_panel_is_refused(self):
        rc, text, boot, _ = self.run_add(release('known', short='dt2',
                                                 product='Digitakt II'))
        self.assertEqual(rc, portable.EXIT_UNSUPPORTED)
        self.assertIn('Digitakt II', text)
        self.assertEqual(boot.calls, [])

    def test_not_firmware(self):
        rc, text, boot, _ = self.run_add(None)
        self.assertEqual(rc, portable.EXIT_FAILED)
        self.assertIn('not an Elektron OS file', text)
        self.assertEqual(boot.calls, [])

    def test_untested_needs_yes(self):
        rel = release('untested')
        rc, text, boot, rmod = self.run_add(rel)
        self.assertEqual(rc, portable.EXIT_NEEDS_YES)
        self.assertIn('--yes', text)
        self.assertIn(SYX_SHA, text)
        self.assertFalse(os.path.exists(portable.firmware_dir(rel.slug, self.home)))
        self.assertEqual((boot.calls, rmod.overlays), ([], []))

    def test_untested_with_yes_builds_against_the_overlay(self):
        rel = release('untested')
        rc, text, boot, rmod = self.run_add(rel, yes=True)
        self.assertEqual(rc, 0, text)
        fwdir = portable.firmware_dir(rel.slug, self.home)
        overlay = os.path.join(fwdir, 'devices')
        # Written when the folder is made, and again by the worker.
        self.assertEqual(rmod.overlays, [(SYX_SHA, overlay)] * 2)
        self.assertEqual(boot.calls[0]['env']['DT2_DEVICES'], overlay)

    def test_known_release_is_copied_recorded_and_built(self):
        rel = release('known')
        rc, text, boot, rmod = self.run_add(rel)
        self.assertEqual(rc, 0, text)
        self.assertIn('is ready', text)
        fwdir = portable.firmware_dir(rel.slug, self.home)
        syx = os.path.join(fwdir, 'Digitakt_OS9.99.syx')
        with open(syx, 'rb') as fh:
            self.assertEqual(fh.read(), SYX_BYTES)
        self.assertEqual(rmod.overlays, [])
        state = _read_state(FakePaths(fwdir, 'x', ''))
        self.assertEqual(state['app_version'], portable.APP_VERSION)
        self.assertEqual(state['release']['syx_name'], 'Digitakt_OS9.99.syx')
        self.assertEqual(state['release']['device'], 'dt1')
        self.assertEqual(state['release']['stamp'], '2026-01-02 03:04:05')
        for value in state['release'].values():
            self.assertFalse(isinstance(value, str) and
                             (os.path.isabs(value) or self.tmp in value), value)
        (call,) = boot.calls
        self.assertEqual(os.path.normcase(call['cwd']), os.path.normcase(fwdir))
        self.assertEqual(call['env']['DT2_SYX'], syx)
        self.assertEqual(call['env']['DT2_PLUSDRIVE'],
                         os.path.join(fwdir, 'plusdrive.img'))
        self.assertEqual(call['env']['DT2_DEVICES'], portable.devices_dir())
        self.assertEqual(os.path.normcase(os.getcwd()), os.path.normcase(self.cwd))
        self.assertIsNone(portable.lock_holder(fwdir))

    def test_adding_again_resumes_in_the_same_folder(self):
        rel = release('known')
        self.run_add(rel)
        rc, text, boot, _ = self.run_add(rel)
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(boot.calls), 1)
        self.assertEqual(portable.list_firmware_dirs(self.home),
                         [portable.firmware_dir(rel.slug, self.home)])

    def test_a_failed_build_says_where_the_log_is(self):
        def fails(p, progress=None, cancel=None):
            raise StepFailed('ladder', 'stopped at 212M: UC_ERR_FETCH_UNMAPPED')

        rc, text, _, _ = self.run_add(release(), boot=make_bootstrap(first_run=fails))
        self.assertEqual(rc, portable.EXIT_FAILED)
        self.assertIn('UC_ERR_FETCH_UNMAPPED', text)
        self.assertIn('first-run.log', text)

    def test_low_disk_space_is_refused_before_anything_is_written(self):
        rel = release()
        with mock.patch('shutil.disk_usage',
                        return_value=SimpleNamespace(free=100 * 2 ** 20)):
            rc, text, boot, _ = self.run_add(rel)
        self.assertEqual(rc, portable.EXIT_FAILED)
        self.assertIn('free disk space', text)
        self.assertFalse(os.path.exists(portable.firmware_dir(rel.slug, self.home)))
        self.assertEqual(boot.calls, [])

    def test_too_long_a_path_is_refused(self):
        # The limit sits just past the home, so the deepest snapshot .tmp
        # under it is too long wherever TEMP is.
        with mock.patch.object(portable, 'MAX_PATH_CHARS', len(self.home) + 40):
            rc, text, boot, _ = self.run_add(release())
        self.assertEqual(rc, portable.EXIT_FAILED)
        self.assertIn('too long', text)
        self.assertEqual(boot.calls, [])
        self.assertEqual(self.spawned, [])

    def test_onedrive_is_a_warning_not_a_refusal(self):
        os.environ['OneDrive'] = self.tmp
        with stub_modules(release=make_release(release()),
                          bootstrap=make_bootstrap()):
            plan = portable.plan_add(self.src, self.home)
        self.assertEqual(plan.errors, [])
        self.assertEqual(len(plan.warnings), 1)
        self.assertIn('OneDrive', plan.warnings[0])

    def test_main_dispatches_add_and_list(self):
        rmod, boot = make_release(release()), make_bootstrap()
        with stub_modules(bootstrap=boot, release=rmod), \
                mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(portable.main(['--add', self.src, '--home', self.home]), 0)
            self.assertEqual(portable.main(['--list', '--home', self.home]), 0)
        self.assertIn('Digitakt 9.99', out.getvalue())
        with mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(portable.main(['--worker', 'bogus', self.tmp]),
                             portable.EXIT_USAGE)
            self.assertEqual(portable.main(['--bogus']), portable.EXIT_USAGE)
        self.assertIn('bogus', err.getvalue())


# --- --worker panel ----------------------------------------------------------------------------------

class PanelTest(Base):
    def run_panel(self, paths, choose, panel=None):
        boot = make_bootstrap(choose=choose)
        panel = panel or make_panel()
        err = io.StringIO()
        with stub_modules(bootstrap=boot, dtpanel=panel):
            rc = portable.worker_panel(paths.root, err=err)
        return rc, err.getvalue(), panel

    def test_card_changed_is_exit_3_with_the_reason(self):
        paths = self.make_folder()
        for reason in ('card-changed', 'not-built'):
            rc, err, panel = self.run_panel(paths, lambda p, r=reason: (None, r))
            self.assertEqual(rc, portable.EXIT_NOT_READY)
            self.assertIn(reason, err)
            self.assertEqual(panel.calls, [])
        with open(os.path.join(paths.logs, 'panel.log'), 'rb') as fh:
            self.assertIn(b'card-changed', fh.read())

    def test_a_clean_exit_records_the_card_for_resume(self):
        paths = self.make_folder()
        _write_state(paths, dict(_read_state(paths), card={'size': 1, 'mtime_ns': 2}))
        rc, err, panel = self.run_panel(paths, lambda p: (p.gui, 'gui'))
        self.assertEqual(rc, 0, err)
        self.assertEqual(panel.calls, [[paths.gui, '--syx', paths.syx,
                                        '--save-on-exit', paths.resume,
                                        '--app']])
        state = _read_state(paths)
        self.assertEqual(state['resume'], _card_stamp(paths.card))
        self.assertEqual(state['card'], {'size': 1, 'mtime_ns': 2})
        self.assertIsNone(portable.lock_holder(paths.root))

    def test_a_failed_save_drops_the_resume_stamp(self):
        paths = self.make_folder()
        _write_state(paths, dict(_read_state(paths), resume={'size': 1, 'mtime_ns': 2}))
        rc, _, _ = self.run_panel(paths, lambda p: (p.resume, 'resume'),
                                  make_panel(rc=2, save=False))
        self.assertEqual(rc, 2)
        self.assertNotIn('resume', _read_state(paths))

    def test_an_emulator_error_is_exit_1(self):
        paths = self.make_folder()
        rc, _, _ = self.run_panel(paths, lambda p: (p.gui, 'gui'),
                                  make_panel(raises=FakeDeviceError('no device')))
        self.assertEqual(rc, 1)
        with open(os.path.join(paths.logs, 'panel.log'), 'rb') as fh:
            self.assertIn(b'no device', fh.read())

    def test_a_device_without_a_panel_is_refused(self):
        paths = self.make_folder(device='dt2')
        rc, err, panel = self.run_panel(paths, lambda p: (p.gui, 'gui'))
        self.assertEqual(rc, portable.EXIT_UNSUPPORTED)
        self.assertEqual(panel.calls, [])

    def test_the_digitone_opens_its_own_window(self):
        paths = self.make_folder(device='dn1')
        dt, dn = make_panel(), make_panel()
        with stub_modules(bootstrap=make_bootstrap(choose=lambda p: (p.gui, 'gui')),
                          dtpanel=dt, dnpanel=dn):
            rc = portable.worker_panel(paths.root, err=io.StringIO())
        self.assertEqual(rc, 0)
        self.assertEqual(dt.calls, [])
        self.assertEqual(dn.calls, [[paths.gui, '--syx', paths.syx,
                                     '--save-on-exit', paths.resume, '--app']])
        self.assertEqual(portable.PANELS['dn1'], 'emu.dnpanel')

    def test_a_second_panel_on_the_same_folder_is_busy(self):
        paths = self.make_folder()
        with portable.FolderLock(paths.root, 'panel').acquire(), \
                mock.patch.object(portable, 'LOCK_WAIT', 0.0):
            rc, err, panel = self.run_panel(paths, lambda p: (p.gui, 'gui'))
        self.assertEqual(rc, portable.EXIT_BUSY)
        self.assertIn('in use', err)
        self.assertEqual(panel.calls, [])

    def resumable(self):
        """A folder whose resume.snap matches its card."""
        paths = self.make_folder()
        os.makedirs(paths.snapdir)
        for p in (paths.gui, paths.resume):
            with open(p, 'wb') as fh:
                fh.write(b'snap')
        _write_state(paths, dict(_read_state(paths), resume=_card_stamp(paths.card)))
        return paths

    def test_a_halt_keeps_a_resume_that_still_matches(self):
        """dtpanel 1 (halt, failed load): nothing was flushed or saved, so
        the last session and the card still agree."""
        paths = self.resumable()
        stamp = _read_state(paths)['resume']
        rc, _, _ = self.run_panel(paths, lambda p: (p.resume, 'resume'),
                                  make_panel(rc=1, save=False))
        self.assertEqual(rc, 1)
        self.assertEqual(_read_state(paths)['resume'], stamp)
        with open(os.path.join(paths.logs, 'panel.log'), 'rb') as fh:
            self.assertIn(b'kept the resume stamp', fh.read())

    def test_a_resume_that_will_not_open_drops_its_stamp(self):
        """dtpanel 5 (LOAD_FAILED) on resume.snap: the next Play must fall
        back to gui.snap (or a rebuild), not fail on it every time. The
        worker reports a failure, not dtpanel's 5 (which is EXIT_BUSY)."""
        paths = self.resumable()
        rc, _, _ = self.run_panel(paths, lambda p: (p.resume, 'resume'),
                                  make_panel(rc=portable.DTPANEL_LOAD_FAILED,
                                             save=False))
        self.assertEqual(rc, portable.EXIT_FAILED)
        self.assertNotIn('resume', _read_state(paths))
        with open(os.path.join(paths.logs, 'panel.log'), 'rb') as fh:
            self.assertIn(b'resume.snap would not open', fh.read())

    def test_a_gui_snap_that_will_not_open_keeps_the_resume_stamp(self):
        paths = self.resumable()
        stamp = _read_state(paths)['resume']
        rc, _, _ = self.run_panel(paths, lambda p: (p.gui, 'gui'),
                                  make_panel(rc=portable.DTPANEL_LOAD_FAILED,
                                             save=False))
        self.assertEqual(rc, portable.EXIT_FAILED)
        self.assertEqual(_read_state(paths)['resume'], stamp)

    def test_a_card_that_moved_without_a_save_drops_the_resume(self):
        paths = self.resumable()
        panel = make_panel(rc=1, save=False)
        real = panel.main

        def writes_the_card(argv):
            with open(paths.card, 'ab') as fh:
                fh.write(b'\x5a')
            return real(argv)

        panel.main = writes_the_card
        rc, _, _ = self.run_panel(paths, lambda p: (p.resume, 'resume'), panel)
        self.assertEqual(rc, 1)
        self.assertNotIn('resume', _read_state(paths))

    def test_loaded_samples_are_exit_8_and_drop_the_resume(self):
        """dtpanel 6: the session was saved, then LOAD SAMPLES wrote the
        card. That resume.snap predates the samples; only a rebuild shows
        them."""
        paths = self.resumable()
        panel = make_panel(rc=6)
        real = panel.main

        def loads_samples(argv):
            rc = real(argv)
            with open(paths.card, 'ab') as fh:
                fh.write(b'\x5a')
            return rc

        panel.main = loads_samples
        rc, _, _ = self.run_panel(paths, lambda p: (p.resume, 'resume'), panel)
        self.assertEqual(rc, portable.EXIT_SAMPLES_ADDED)
        self.assertNotIn('resume', _read_state(paths))
        with open(os.path.join(paths.logs, 'panel.log'), encoding='utf-8') as fh:
            self.assertIn('samples were loaded', fh.read())

    def test_an_incompatible_snapshot_is_exit_7_and_asks_for_a_rebuild(self):
        paths = self.resumable()
        stamp = _read_state(paths)['resume']
        rc, _, _ = self.run_panel(paths, lambda p: (p.resume, 'resume'),
                                  make_panel(rc=4, save=False))
        self.assertEqual(rc, portable.EXIT_INCOMPATIBLE)
        state = _read_state(paths)
        self.assertEqual(state['resume'], stamp)
        self.assertEqual(state['incompatible']['snapshot'], 'resume.snap')
        info = status_with(paths, lambda p: (p.resume, 'resume'))
        self.assertEqual((info['status'], info['reason']),
                         (portable.REBUILD, portable.INCOMPATIBLE_REASON))
        # Another file than the one marked (a rebuild writes new ones) is
        # not affected, nor is the next version of the app.
        self.assertEqual(status_with(paths, lambda p: (p.gui, 'gui'))['status'],
                         portable.READY)
        with mock.patch.object(portable, 'APP_VERSION', '9.9.9'):
            self.assertEqual(status_with(paths, lambda p: (p.resume, 'resume'))
                             ['status'], portable.READY)
        # A clean session later clears the mark.
        rc, _, _ = self.run_panel(paths, lambda p: (p.resume, 'resume'))
        self.assertEqual(rc, 0)
        self.assertNotIn('incompatible', _read_state(paths))

    def test_workers_refresh_an_untested_overlay(self):
        paths = self.make_folder()
        state = _read_state(paths)
        state['release']['status'] = 'untested'
        _write_state(paths, state)
        os.makedirs(paths.overlay)
        with open(os.path.join(paths.overlay, 'old-name.toml'), 'wb') as fh:
            fh.write(b'# a stale copy\n')
        rmod = make_release(release('untested'))
        boot = make_bootstrap(choose=lambda p: (p.gui, 'gui'))
        with stub_modules(bootstrap=boot, release=rmod, dtpanel=make_panel()):
            self.assertEqual(portable.worker_first_run(paths.root,
                                                       proto=io.StringIO()), 0)
            self.assertEqual(portable.worker_panel(paths.root, err=io.StringIO()), 0)
        self.assertEqual(rmod.overlays, [(SYX_SHA, paths.overlay)] * 2)
        self.assertEqual(os.listdir(paths.overlay), ['digitakt.toml'])
        self.assertEqual(boot.calls[0]['env']['DT2_DEVICES'], paths.overlay)
        with open(os.path.join(paths.logs, 'panel.log'), 'rb') as fh:
            self.assertIn(b'devices overlay refreshed', fh.read())

    def test_a_release_that_became_known_loses_its_overlay(self):
        paths = self.make_folder()
        state = _read_state(paths)
        state['release'].update(status='untested', label='Digitakt 9.99 (untested)')
        _write_state(paths, state)
        os.makedirs(paths.overlay)
        rmod = make_release(release('known'))
        boot = make_bootstrap()
        with stub_modules(bootstrap=boot, release=rmod):
            self.assertEqual(portable.worker_first_run(paths.root,
                                                       proto=io.StringIO()), 0)
        self.assertFalse(os.path.exists(paths.overlay))
        self.assertEqual(rmod.overlays, [])
        rec = _read_state(paths)['release']
        self.assertEqual((rec['status'], rec['label']), ('known', 'Digitakt 9.99'))
        self.assertEqual(boot.calls[0]['env']['DT2_DEVICES'], portable.devices_dir())

    def test_a_known_release_is_not_identified_again(self):
        paths = self.make_folder()
        rmod = make_release(None)       # would raise if it were asked
        with stub_modules(bootstrap=make_bootstrap(), release=rmod):
            self.assertEqual(portable.worker_first_run(paths.root,
                                                       proto=io.StringIO()), 0)
        with open(os.path.join(paths.logs, 'first-run.log'), 'rb') as fh:
            self.assertNotIn(b'overlay', fh.read())

    def test_an_overlay_that_cannot_be_refreshed_does_not_stop_the_worker(self):
        paths = self.make_folder()
        state = _read_state(paths)
        state['release']['status'] = 'untested'
        _write_state(paths, state)
        with stub_modules(bootstrap=make_bootstrap(), release=make_release(None)):
            self.assertEqual(portable.worker_first_run(paths.root,
                                                       proto=io.StringIO()), 0)
        with open(os.path.join(paths.logs, 'first-run.log'), 'rb') as fh:
            self.assertIn(b'devices overlay not refreshed', fh.read())


# --- status, rebuild, remove ---------------------------------------------------------------------

class StatusTest(Base):
    def status(self, paths, choose):
        with stub_modules(bootstrap=make_bootstrap(choose=choose)):
            return portable.status_of(paths.root)

    def test_statuses(self):
        paths = self.make_folder()
        self.assertEqual(self.status(paths, lambda p: (p.gui, 'gui'))['status'],
                         portable.READY)
        self.assertEqual(self.status(paths, lambda p: (None, 'card-changed'))['status'],
                         portable.REBUILD)
        self.assertEqual(self.status(paths, lambda p: (None, 'not-built'))['status'],
                         portable.NEEDS_SETUP)
        with portable.FolderLock(paths.root, 'first-run').acquire():
            self.assertEqual(self.status(paths, lambda p: (p.gui, 'gui'))['status'],
                             portable.BUILDING)
        with portable.FolderLock(paths.root, 'panel').acquire():
            self.assertEqual(self.status(paths, lambda p: (p.gui, 'gui'))['status'],
                             portable.RUNNING)
        info = self.status(paths, lambda p: (p.gui, 'gui'))
        self.assertEqual(info['label'], 'Digitakt 9.99')
        self.assertGreater(info['size'], 512)
        os.remove(paths.syx)
        self.assertEqual(self.status(paths, lambda p: (p.gui, 'gui'))['status'],
                         portable.BROKEN)

    def test_a_held_folder_is_not_read(self):
        paths = self.make_folder()
        boot = make_bootstrap()

        def must_not_read(p):
            raise AssertionError('firmware.json read while a worker holds it')

        boot.read_state = must_not_read
        with stub_modules(bootstrap=boot), \
                portable.FolderLock(paths.root, 'first-run').acquire():
            info = portable.status_of(paths.root, label='Digitakt OS 9.99')
            self.assertEqual((info['status'], info['label']),
                             (portable.BUILDING, 'Digitakt OS 9.99'))
            info = portable.status_of(paths.root, running='panel', label='x')
            self.assertEqual(info['status'], portable.RUNNING)

    def test_rebuild_keeps_the_card_and_sections(self):
        paths = self.make_folder()
        for p in (paths.gui, paths.resume, paths.prefix + '400M.snap', paths.main_img):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'wb') as fh:
                fh.write(b'x')
        _write_state(paths, dict(_read_state(paths), card={'size': 512},
                                 resume={'size': 512}))
        with stub_modules(bootstrap=make_bootstrap()):
            portable.invalidate_build(paths)
        self.assertFalse(os.path.exists(paths.snapdir))
        self.assertTrue(os.path.exists(paths.card))
        self.assertTrue(os.path.exists(paths.main_img))
        state = _read_state(paths)
        self.assertEqual(state['stages'], {'extract': 1, 'card': 1})
        self.assertNotIn('card', state)
        self.assertNotIn('resume', state)

    def test_rebuild_worker_clears_before_building(self):
        paths = self.make_folder()
        os.makedirs(paths.snapdir)
        with open(paths.gui, 'wb') as fh:
            fh.write(b'old')
        seen = []

        def first_run(p, progress=None, cancel=None):
            seen.append(os.path.exists(p.gui))
            return p.gui

        with stub_modules(bootstrap=make_bootstrap(first_run=first_run)):
            rc = portable.worker_first_run(paths.root, rebuild=True, proto=io.StringIO())
        self.assertEqual((rc, seen), (0, [False]))

    def test_remove(self):
        paths = self.make_folder()
        with portable.FolderLock(paths.root, 'panel').acquire():
            with self.assertRaises(portable.Busy):
                portable.remove_firmware(paths.root, self.home)
        with self.assertRaises(portable.FolderError):
            portable.remove_firmware(paths.root, os.path.join(self.tmp, 'other'))
        with self.assertRaises(ValueError):
            portable.remove_firmware(self.tmp, self.home)
        portable.remove_firmware(paths.root, self.home)
        self.assertFalse(os.path.exists(paths.root))
        self.assertTrue(os.path.isdir(self.tmp))

    def test_list_output(self):
        out = io.StringIO()
        self.assertEqual(portable.list_firmware(self.home, out), 0)
        self.assertIn('No firmware set up', out.getvalue())
        paths = self.make_folder()
        out = io.StringIO()
        with stub_modules(bootstrap=make_bootstrap(choose=lambda p: (p.gui, 'gui'))):
            portable.list_firmware(self.home, out)
        self.assertIn(os.path.basename(paths.root), out.getvalue())
        self.assertIn(portable.READY, out.getvalue())

    def test_list_does_not_open_a_folder_another_process_holds(self):
        paths = self.make_folder()
        boot = make_bootstrap()

        def must_not_read(p):
            raise AssertionError('firmware.json read while a worker holds it')

        boot.read_state = must_not_read
        for purpose, status in (('first-run', portable.BUILDING),
                                ('panel', portable.RUNNING)):
            out = io.StringIO()
            with stub_modules(bootstrap=boot), \
                    portable.FolderLock(paths.root, purpose).acquire():
                self.assertEqual(portable.list_firmware(self.home, out), 0)
                info = portable.status_of(paths.root)
            self.assertIn(status, out.getvalue())
            self.assertIn('%s pid' % purpose, out.getvalue())
            self.assertEqual((info['status'], info['label']),
                             (status, os.path.basename(paths.root)))

    # atomic writes

    def test_the_copy_is_renamed_with_the_bootstraps_replace_retry(self):
        boot = make_bootstrap()
        calls = []

        def replace_retry(src, dst, tries=20, delay=0.1):
            calls.append((src, dst))
            os.replace(src, dst)

        boot.replace_retry = replace_retry
        dst = os.path.join(self.tmp, 'copy.syx')
        with stub_modules(bootstrap=boot):
            self.assertEqual(portable.copy_syx(self.src, dst, SYX_SHA), 'copied')
        self.assertEqual(calls, [(dst + '.tmp', dst)])

    def test_a_sharing_violation_is_retried(self):
        """Without emu.bootstrap.replace_retry, the same loop here."""
        a, b = os.path.join(self.tmp, 'a'), os.path.join(self.tmp, 'b')
        with open(a, 'wb') as fh:
            fh.write(b'new')
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(13, 'Access is denied')
            real(src, dst)

        with stub_modules(bootstrap=make_bootstrap()), \
                mock.patch.object(portable, 'REPLACE_DELAY', 0), \
                mock.patch('os.replace', flaky):
            portable.replace_retry(a, b)
        self.assertEqual(len(calls), 3)
        with open(b, 'rb') as fh:
            self.assertEqual(fh.read(), b'new')
        with stub_modules(bootstrap=make_bootstrap()), \
                mock.patch.object(portable, 'REPLACE_DELAY', 0), \
                mock.patch('os.replace', side_effect=PermissionError(13, 'x')) as rep:
            with self.assertRaises(PermissionError):
                portable.replace_retry(b, a)
        self.assertEqual(rep.call_count, portable.REPLACE_TRIES)

    # reset to factory

    def test_reset_deletes_the_card_snapshots_and_stamps(self):
        paths = self.make_folder()
        for p in (paths.gui, paths.resume, paths.prefix + '400M.snap', paths.main_img,
                  os.path.join(paths.logs, 'panel.log')):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'wb') as fh:
                fh.write(b'x')
        _write_state(paths, dict(_read_state(paths), card={'size': 512},
                                 resume={'size': 512},
                                 incompatible={'snapshot': 'gui.snap'}))
        with stub_modules(bootstrap=make_bootstrap()):
            portable.reset_firmware(paths.root, self.home)
        self.assertFalse(os.path.exists(paths.card))
        self.assertFalse(os.path.exists(paths.snapshots))
        for kept in (paths.syx, paths.main_img, os.path.join(paths.logs, 'panel.log')):
            self.assertTrue(os.path.exists(kept), kept)
        state = _read_state(paths)
        self.assertEqual(sorted(state), ['app_version', 'release'])
        self.assertIsNone(portable.lock_holder(paths.root))

    def test_rebuild_keeps_the_first_boot_marker(self):
        # A folder settled before the marker existed: the settle stamp that
        # Rebuild drops is what vouched for the card, so the marker takes
        # over (emu.bootstrap's sector-0 guard reads it).
        paths = self.make_folder()
        state = _read_state(paths)
        state['stages']['settle'] = {'inputs': {}, 'card_after': None}
        _write_state(paths, state)
        portable.invalidate_build(paths)
        state = _read_state(paths)
        self.assertTrue(state['first_boot'].get('backfilled'))
        self.assertNotIn('settle', state['stages'])
        # An existing marker is kept as it is.
        state['stages']['settle'] = {}
        state['first_boot'] = {'at': 'then'}
        _write_state(paths, state)
        portable.invalidate_build(paths)
        self.assertEqual(_read_state(paths)['first_boot'], {'at': 'then'})

    def test_reset_forgets_the_first_boot_marker(self):
        paths = self.make_folder()
        _write_state(paths, dict(_read_state(paths), first_boot={'at': 'then'}))
        with stub_modules(bootstrap=make_bootstrap()):
            portable.reset_firmware(paths.root, self.home)
        self.assertNotIn('first_boot', _read_state(paths))

    def test_reset_is_refused_while_in_use_or_outside_the_home(self):
        paths = self.make_folder()
        with stub_modules(bootstrap=make_bootstrap()):
            with portable.FolderLock(paths.root, 'panel').acquire():
                with self.assertRaises(portable.Busy):
                    portable.reset_firmware(paths.root, self.home)
            with self.assertRaises(portable.FolderError):
                portable.reset_firmware(paths.root, os.path.join(self.tmp, 'other'))
        self.assertTrue(os.path.exists(paths.card))
        self.assertIn('stages', _read_state(paths))


# --- the self-test child ---------------------------------------------------------------------

class SelftestTest(Base):
    def run_fake(self, code, timeout=120):
        """run_selftest with `python -c code JSON` as the child."""
        with mock.patch.object(portable, 'selftest_command',
                               lambda p: [sys.executable, '-c', code, p]):
            return portable.run_selftest(timeout=timeout)

    def test_commands(self):
        path = os.path.join(self.tmp, 'selftest.json')
        cmd = portable.selftest_command(path)
        self.assertEqual(cmd, [sys.executable,
                               os.path.join(REPO, 'packaging', 'digiemu_main.py'),
                               '--selftest', '--json', path])
        self.assertTrue(os.path.isfile(cmd[1]))
        exe = os.path.join(self.tmp, 'digiemu.exe')
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'executable', exe):
            self.assertEqual(portable.selftest_command(path),
                             [exe, '--selftest', '--json', path])

    def test_summaries(self):
        s = portable.summarize_selftest
        self.assertTrue(s({'ok': True, 'running': None,
                           'checks': [{'name': 'tk', 'ok': True}]}, 0)['ok'])
        crash = s({'ok': False, 'running': 'unicorn_compat',
                   'checks': [{'name': 'unicorn', 'ok': True}]}, 0xC0000409)
        self.assertFalse(crash['ok'])
        self.assertIn('stack buffer overrun', crash['message'])
        self.assertIn('unicorn_compat', crash['message'])
        failed = s({'ok': False, 'running': None, 'checks': [
            {'name': 'unicorn', 'ok': True},
            {'name': 'native_options', 'ok': False,
             'detail': 'RuntimeError: unicorn.dll lacks the speed patches'}]}, 1)
        self.assertEqual(failed['failed'], ['native_options'])
        self.assertIn('native_options', failed['message'])
        self.assertIn('lacks the speed patches', failed['message'])
        self.assertIn('without writing its report', s(None, 1)['message'])
        slow = s({'running': 'tk'}, None, timed_out=True, timeout=5)
        self.assertIn('did not finish within 5 s', slow['message'])
        self.assertIn('tk', slow['message'])
        odd = s({'ok': True, 'running': None, 'checks': []}, 3)
        self.assertFalse(odd['ok'])
        self.assertIn('exited with code 3', odd['message'])

    def test_a_child_that_dies_mid_check_is_named(self):
        code = ('import json, sys\n'
                'rep = {"ok": False, "running": "unicorn_compat",\n'
                '       "checks": [{"name": "unicorn", "ok": True, "detail": {}}]}\n'
                'open(sys.argv[1], "w").write(json.dumps(rep))\n'
                'print("selftest: unicorn_compat ...", file=sys.stderr, flush=True)\n'
                'sys.exit(3)\n')
        res = self.run_fake(code)
        self.assertFalse(res['ok'])
        self.assertEqual(res['running'], 'unicorn_compat')
        self.assertIn('exited with code 3', res['message'])
        self.assertIn('running firmware code in Unicorn (unicorn_compat)', res['message'])
        self.assertIn('selftest: unicorn_compat ...', res['stderr'])

    def test_a_passing_child(self):
        code = ('import json, sys\n'
                'open(sys.argv[1], "w").write(json.dumps({"ok": True, "running": None,'
                ' "checks": [{"name": "tk", "ok": True}]}))\n')
        res = self.run_fake(code)
        self.assertTrue(res['ok'], res)

    def test_a_child_that_cannot_start(self):
        missing = os.path.join(self.tmp, 'missing', 'digiemu.exe')
        with mock.patch.object(portable, 'selftest_command', lambda p: [missing]):
            res = portable.run_selftest(timeout=30)
        self.assertFalse(res['ok'])
        self.assertIn('could not be started', res['message'])

    def test_the_launcher_side_never_loads_unicorn(self):
        """What the launcher runs, in a fresh interpreter with the real
        emu.bootstrap: the self-test (its report comes from a child) and the
        status of a settled-looking folder (choose_snapshot and its build
        id). Unicorn must stay out of this process."""
        paths = self.make_folder()
        os.makedirs(paths.snapdir)
        with open(paths.gui, 'wb') as fh:
            fh.write(b'not a snapshot')
        state = _read_state(paths)
        state['stages']['settle'] = {'build': {'recipe': -1}, 'inputs': {}}
        _write_state(paths, state)
        code = ('import sys\n'
                'from unittest import mock\n'
                'from emu import portable\n'
                'child = ["import sys", "open(sys.argv[1], \'w\').write(\'{}\')",'
                ' "sys.exit(1)"]\n'
                'cmd = lambda p: [sys.executable, "-c", "\\n".join(child), p]\n'
                'with mock.patch.object(portable, "selftest_command", cmd):\n'
                '    res = portable.run_selftest(60)\n'
                'portable.list_firmware(sys.argv[1], out=None)\n'
                'print(res["ok"], "emu.bootstrap" in sys.modules,'
                ' "unicorn" in sys.modules)\n')
        proc = subprocess.run([sys.executable, '-c', code, self.home], cwd=self.tmp,
                              env=dict(os.environ, PYTHONPATH=REPO),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(portable.NEEDS_SETUP.encode(), proc.stdout)   # not-built
        self.assertEqual(proc.stdout.split()[-3:], [b'False', b'True', b'False'],
                         proc.stdout + proc.stderr)


# --- the launcher, driven through a fake tkinter (no window) ---------------------------------

def fake_fwcheck(run_check):
    m = types.ModuleType('emu.fwcheck')
    m.run_check = run_check
    return m


class CheckTest(Base):
    """The firmware check's launcher half: what it offers as the stock
    build, where it writes, how far along it is, and the worker."""

    def test_the_stock_candidates_are_known_releases_of_the_same_product(self):
        paths = self.make_folder()
        with stub_modules(bootstrap=make_bootstrap()):
            dt = portable.baseline_candidates(release('untested'), self.home)
            dn = portable.baseline_candidates(
                release('untested', short='dn1', product='Digitone'),
                self.home)
            self.assertEqual([(c['syx'], c['same_version']) for c in dt],
                             [(paths.syx, True)])
            self.assertEqual(dn, [])
            state = _read_state(paths)
            state['release']['status'] = 'untested'   # may itself be custom
            _write_state(paths, state)
            self.assertEqual(
                portable.baseline_candidates(release('untested'), self.home), [])

    def test_plan_check_refuses_what_this_version_cannot_boot(self):
        with stub_modules(bootstrap=make_bootstrap(), release=make_release(
                release('unsupported', short=None, product='Syntakt'))):
            with self.assertRaises(portable.Refused):
                portable.plan_check(self.src, self.home)
        with stub_modules(bootstrap=make_bootstrap(),
                          release=make_release(release('untested'))):
            plan = portable.plan_check(self.src, self.home)
        self.assertEqual((plan.source, plan.errors), (self.src, []))

    def test_each_check_gets_a_new_folder(self):
        a = portable.new_check_dir(self.src, self.home, stamp='20260924-1200')
        b = portable.new_check_dir(self.src, self.home, stamp='20260924-1200')
        self.assertNotEqual(a, b)
        self.assertEqual(os.path.dirname(a), portable.checks_root(self.home))
        self.assertEqual(os.path.basename(a), '20260924-1200-my-firmware')

    def test_progress_goes_by_expected_seconds(self):
        # dt1 without timing: 1+1+9+75+50 a build, twice, and 2 to compare.
        p = portable.CheckProgress('dt1', True, False)
        self.assertEqual(p.total, 274)
        self.assertEqual(portable.check_minutes('dt1', True, False), 5)
        self.assertEqual(portable.check_minutes('dt1', False, True), 7)
        boot = {'kind': 'start', 'step': 'boot', 'data': {'role': 'baseline'}}
        p.feed(boot, now=100.0)
        self.assertEqual((p.role, p.step), ('baseline', 'boot'))
        self.assertAlmostEqual(p.fraction(now=100.0), 11 / 274)
        # the clock moves it on inside the stage, never past 95% of it
        self.assertAlmostEqual(p.fraction(now=10_000.0), (11 + 0.95 * 75) / 274)
        p.feed({'kind': 'done', 'step': 'boot',
                'data': {'role': 'baseline', 'state': 'passed',
                         'passed': True}}, now=150.0)
        self.assertAlmostEqual(p.fraction(now=150.0), 86 / 274)
        self.assertEqual(p.verdicts, [('baseline', 'boot', 'passed', True)])
        p.feed({'kind': 'start', 'step': 'boot', 'data': {'role': 'nobody'}})
        self.assertAlmostEqual(p.fraction(now=150.0), 86 / 274)
        p.feed({'kind': 'result', 'ok': True})
        self.assertEqual(p.fraction(), 1.0)

    def request(self, baseline=None, timing=False):
        checkdir = portable.new_check_dir(self.src, self.home)
        portable.write_check_request(checkdir, self.src, baseline, timing, 'dt1')
        return checkdir

    def test_worker_check_streams_records_and_the_verdict(self):
        stock = os.path.join(self.tmp, 'stock.syx')
        with open(stock, 'wb') as fh:
            fh.write(SYX_BYTES)
        checkdir = self.request(stock, timing=True)
        calls = []

        def run_check(syx, out, baseline=None, timing=True, keep_work=True,
                      log=print, on_event=None, **kw):
            calls.append((syx, out, baseline, timing, keep_work))
            print('emulator chatter ✓')                  # must reach the log
            log('== container')
            on_event('build', {'kind': 'start', 'step': 'container'})
            on_event('build', {'kind': 'done', 'step': 'container',
                               'state': 'passed', 'passed': True})
            os.makedirs(os.path.join(out, 'work', 'build'))
            return {'passed': False, 'summary': ['build: FAIL', '  run  FAIL']}

        out = io.StringIO()
        with stub_modules(fwcheck=fake_fwcheck(run_check)):
            rc = portable.worker_check(checkdir, proto=out)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [(self.src, checkdir, stock, True, False)])
        recs = [portable.decode_line(line) for line in out.getvalue().splitlines()]
        self.assertNotIn(None, recs)
        self.assertEqual([(r['kind'], r['step'], r['data'].get('role'))
                          for r in recs[:-1]],
                         [('start', 'container', 'build'),
                          ('done', 'container', 'build')])
        self.assertEqual(recs[-1]['kind'], 'result')
        self.assertIs(recs[-1]['ok'], True)
        self.assertEqual(recs[-1]['data'], {'passed': False, 'summary': [
            'build: FAIL', '  run  FAIL']})
        with open(os.path.join(checkdir, portable.CHECK_LOG),
                  encoding='utf-8') as fh:
            log = fh.read()
        self.assertIn('emulator chatter ✓', log)
        self.assertIn('== container', log)
        with open(os.path.join(checkdir, portable.CHECK_SUMMARY),
                  encoding='utf-8') as fh:
            self.assertEqual(fh.read(), 'build: FAIL\n  run  FAIL\n')
        self.assertFalse(os.path.exists(os.path.join(checkdir, 'work')))
        self.assertEqual(os.environ['DIGIEMU_DEVICES'], portable.devices_dir())

    def test_worker_check_failure_is_a_result_not_a_crash(self):
        checkdir = self.request()

        def run_check(*args, **kw):
            raise RuntimeError('boom')

        out = io.StringIO()
        with stub_modules(fwcheck=fake_fwcheck(run_check)):
            rc = portable.worker_check(checkdir, proto=out)
        self.assertEqual(rc, portable.EXIT_FAILED)
        last = portable.decode_line(out.getvalue().splitlines()[-1])
        self.assertEqual((last['ok'], last['error']), (False, 'RuntimeError: boom'))
        with open(os.path.join(checkdir, portable.CHECK_LOG),
                  encoding='utf-8') as fh:
            self.assertIn('RuntimeError: boom', fh.read())

    def test_worker_check_without_a_request(self):
        out = io.StringIO()
        rc = portable.worker_check(os.path.join(self.tmp, 'nowhere'), proto=out)
        self.assertEqual(rc, portable.EXIT_FAILED)
        last = portable.decode_line(out.getvalue().splitlines()[-1])
        self.assertIn('No firmware check', last['error'])

    def test_the_check_worker_command(self):
        self.assertEqual(portable.worker_command('check', self.tmp)[-3:],
                         ['--worker', 'check', os.path.abspath(self.tmp)])


class FakeVar:
    def __init__(self, value=''):
        self.value = value

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


class FakeWidget:
    def __init__(self, *args, **kw):
        self.opts = dict(kw)
        self.disabled = False
        self.afters = []
        self.destroyed = False

    def pack(self, **kw):
        pass

    def bind(self, *args):
        pass

    def configure(self, **kw):
        self.opts.update(kw)

    def state(self, spec):
        self.disabled = 'disabled' in spec

    def __getitem__(self, key):
        return self.opts.get(key, 'determinate' if key == 'mode' else 0)

    def __setitem__(self, key, value):
        self.opts[key] = value

    def start(self, *args):
        pass

    def stop(self):
        pass

    # Tk / Toplevel
    def title(self, text):
        self.opts['title'] = text

    def minsize(self, *args):
        pass

    def protocol(self, *args):
        pass

    def transient(self, *args):
        pass

    def lift(self):
        pass

    def after(self, ms, fn):
        self.afters.append(fn)

    def winfo_exists(self):
        return not self.destroyed

    def destroy(self):
        self.destroyed = True


class FakeText(FakeWidget):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.lines = []

    def insert(self, where, text):
        self.lines.append(text.rstrip('\n'))

    def delete(self, first, last=None):
        if first == 'end-2l linestart' and self.lines:
            self.lines.pop()

    def index(self, where):
        return '%d.0' % (len(self.lines) + 1)

    def see(self, where):
        pass


class FakeTree(FakeWidget):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.items, self.order, self.sel = {}, [], ()

    def heading(self, *args, **kw):
        pass

    def column(self, *args, **kw):
        pass

    def get_children(self):
        return tuple(self.order)

    def delete(self, iid):
        self.order.remove(iid)
        del self.items[iid]

    def exists(self, iid):
        return iid in self.items

    def item(self, iid, **kw):
        self.items[iid].update(kw)

    def insert(self, parent, where, iid, **kw):
        self.items[iid] = dict(kw)
        self.order.append(iid)

    def selection(self):
        return self.sel

    def selection_set(self, iid):
        self.sel = (iid,)


class FakeDialogs:
    """messagebox and filedialog: canned answers, every call recorded."""

    def __init__(self):
        self.calls = []
        self.texts = {}
        self.answers = {}
        self.path = ''

    def _ask(self, kind, title, *args, **kw):
        self.calls.append((kind, title))
        self.texts[title] = args[0] if args else kw.get('message', '')
        return self.answers.get(title, False)

    def __getattr__(self, name):
        if name == 'askopenfilename':
            return lambda **kw: self.path
        return lambda title='', *a, **kw: self._ask(name, title, *a, **kw)


class FakeProc:
    def __init__(self, lines=(), rc=0):
        self.pid = 4242
        self.stdout = io.BytesIO(b''.join(line.encode('ascii') + b'\r\n'
                                          for line in lines))
        self.rc = rc
        self.terminated = False

    def wait(self, timeout=None):
        return self.rc

    def terminate(self):
        self.terminated = True


_TREE = ('import subprocess, sys, time\n'
         'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])\n'
         'print(p.pid, flush=True)\n'
         'time.sleep(120)\n')


@unittest.skipUnless(os.name == 'nt', 'Windows job objects')
class WorkerJobTest(unittest.TestCase):
    """WorkerJob on real processes: a child that starts its own child, the
    shape of a dev worker (the venv's python.exe runs the real interpreter
    as a child) and of anything a worker might start."""

    def _alive(self, pid):
        import ctypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x00100000, 0, pid)          # SYNCHRONIZE
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 10_000) != 0   # 0: it exited
        finally:
            k32.CloseHandle(h)

    def _tree(self, job):
        proc = subprocess.Popen([sys.executable, '-c', _TREE],
                                stdout=subprocess.PIPE,
                                creationflags=portable.CREATE_NO_WINDOW)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertTrue(job.add(proc))
        grandchild = int(proc.stdout.readline())
        proc.stdout.close()
        return proc, grandchild

    def test_terminate_ends_the_whole_tree(self):
        job = portable.WorkerJob()
        self.addCleanup(job.close)
        proc, grandchild = self._tree(job)
        self.assertTrue(job.terminate())
        proc.wait(10)
        self.assertFalse(self._alive(grandchild))

    def test_kill_on_close_ends_the_tree_when_the_job_closes(self):
        job = portable.WorkerJob(kill_on_close=True)
        proc, grandchild = self._tree(job)
        job.close()
        proc.wait(10)
        self.assertFalse(self._alive(grandchild))

    def test_stop_worker_falls_back_to_terminate(self):
        stub = SimpleNamespace(terminated=False)
        stub.terminate = lambda: setattr(stub, 'terminated', True)
        portable.stop_worker(stub)
        self.assertTrue(stub.terminated)
        self.assertFalse(portable.WorkerJob().add(stub))     # no _handle


class LauncherTest(Base):
    def setUp(self):
        super().setUp()
        self.dialogs = FakeDialogs()
        tk = types.ModuleType('tkinter')
        tk.Tk = tk.Toplevel = FakeWidget
        tk.StringVar = tk.BooleanVar = FakeVar
        tk.Text = FakeText
        ttk = types.ModuleType('tkinter.ttk')
        ttk.Frame = ttk.Label = ttk.Button = ttk.Progressbar = FakeWidget
        ttk.Radiobutton = ttk.Checkbutton = FakeWidget
        ttk.Treeview = FakeTree
        tk.ttk, tk.messagebox, tk.filedialog = ttk, self.dialogs, self.dialogs
        self.tk_modules = {'tkinter': tk, 'tkinter.ttk': ttk,
                           'tkinter.messagebox': self.dialogs,
                           'tkinter.filedialog': self.dialogs}
        self.spawned = []
        self.next_proc = FakeProc()
        spawn = mock.patch.object(portable, 'spawn_worker', self.fake_spawn)
        spawn.start()
        self.addCleanup(spawn.stop)
        self.thread_targets = []

        def thread(target, args, daemon):
            self.thread_targets.append(target)
            return SimpleNamespace(start=lambda: None)

        threads = mock.patch.object(portable, 'threading',
                                    SimpleNamespace(Thread=thread))
        threads.start()
        self.addCleanup(threads.stop)
        self.choice = (None, 'not-built')

    def fake_spawn(self, mode, fwdir, *extra, piped=False,
                   kill_on_close=False):
        self.spawned.append((mode, fwdir, extra, piped))
        return self.next_proc

    def launch(self, rel=None):
        boot = make_bootstrap(choose=lambda p: self.choice)
        stubs = stub_modules(self.tk_modules, bootstrap=boot,
                             release=make_release(rel or release()))
        stubs.__enter__()
        self.addCleanup(stubs.__exit__, None, None, None)
        log = mock.Mock(log_path=os.path.join(self.home, 'logs', 'launcher.log'))
        root = FakeWidget()
        return portable.Launcher(root, self.home, log), root

    def test_empty_home(self):
        app, root = self.launch()
        self.assertIn('No firmware yet', app.note.get())
        self.assertTrue(app.buttons['play'].disabled)
        self.assertFalse(app.buttons['add'].disabled)
        self.assertEqual(root.opts['title'], 'digiemu %s' % portable.APP_VERSION)

    def test_add_identifies_prepares_and_starts_the_build(self):
        rel = release('known')
        self.dialogs.path = self.src
        app, _ = self.launch(rel)
        app.add()
        fwdir = portable.firmware_dir(rel.slug, self.home)
        self.assertEqual(self.spawned, [('first-run', fwdir, (), True)])
        self.assertTrue(os.path.exists(os.path.join(fwdir, 'Digitakt_OS9.99.syx')))
        self.assertEqual(app.tree.sel, (rel.slug,))
        self.assertEqual(app.infos[rel.slug]['status'], portable.BUILDING)

    def test_add_refuses_unsupported_and_asks_for_untested(self):
        self.dialogs.path = self.src
        app, _ = self.launch(release('unsupported', short=None, product='Syntakt'))
        app.add()
        self.assertEqual(self.dialogs.calls, [('showerror', 'Cannot add this firmware')])
        self.assertEqual(self.spawned, [])
        self.dialogs.calls = []
        app, _ = self.launch(release('untested'))
        app.add()                                   # answered no
        self.assertEqual(self.dialogs.calls, [('askyesno', 'Untested firmware')])
        self.assertEqual(self.spawned, [])
        self.dialogs.answers['Untested firmware'] = True
        app.add()
        self.assertEqual(len(self.spawned), 1)

    def test_build_progress_and_ready(self):
        paths = self.make_folder()
        app, _ = self.launch()
        lines = [portable.encode_line(portable.event_record(e)) for e in (
            Event('ladder', 'start', text='cold boot'),
            Event('ladder', 'tick', 200, 400, '[200M] rung'),
            Event('ladder', 'tick', 280, 400, '[280M] rung'))]
        lines.insert(1, 'a stray native print')
        lines.append(portable.encode_line(portable.result_record(True)))
        self.next_proc = FakeProc(lines, rc=0)
        app.start_build(paths.root, 'Digitakt 9.99')
        dialog = app.jobs[paths.root].dialog
        app._read_worker(paths.root, self.next_proc)
        self.choice = (paths.gui, 'gui')
        app._poll()
        self.assertEqual(dialog.text.lines, ['ladder: cold boot', 'a stray native print',
                                             'ladder: [280M] rung'])
        self.assertTrue(dialog.done and dialog.ok)
        self.assertEqual(dialog.bar['value'], 1000)
        self.assertEqual(dialog.step_var.get(), 'Ready')
        self.assertIn(('askyesno', 'Ready'), self.dialogs.calls)
        self.assertNotIn(paths.root, app.jobs)

    def test_failed_build_shows_the_error(self):
        paths = self.make_folder()
        app, _ = self.launch()
        self.next_proc = FakeProc([portable.encode_line(
            portable.result_record(False, 'settle: NOT SETTLED', 'settle'))], rc=1)
        app.start_build(paths.root, 'Digitakt 9.99')
        dialog = app.jobs[paths.root].dialog
        app._read_worker(paths.root, self.next_proc)
        app._poll()
        self.assertFalse(dialog.ok)
        self.assertEqual(dialog.step_var.get(), 'Setup failed')
        self.assertIn('ERROR: settle: NOT SETTLED', dialog.text.lines)

    def test_cancel_terminates_the_worker(self):
        paths = self.make_folder()
        app, _ = self.launch()
        app.start_build(paths.root, 'Digitakt 9.99')
        dialog = app.jobs[paths.root].dialog
        self.dialogs.answers['Cancel setup'] = True
        dialog.cancel()
        self.assertTrue(self.next_proc.terminated)

    def test_play_ready_then_card_changed_offers_rebuild(self):
        paths = self.make_folder()
        self.choice = (paths.gui, 'gui')
        app, _ = self.launch()
        slug = os.path.basename(paths.root)
        self.assertEqual(app.infos[slug]['status'], portable.READY)
        app.play()
        self.assertEqual(self.spawned, [('panel', paths.root, (), False)])
        self.assertEqual(app.infos[slug]['status'], portable.RUNNING)
        self.choice = (None, 'card-changed')
        self.dialogs.answers['Rebuild needed'] = True
        app.events.put(('exit', paths.root, portable.EXIT_NOT_READY))
        app._poll()
        self.assertEqual(self.spawned[-1], ('first-run', paths.root, ('--rebuild',), True))

    def test_panel_errors_are_reported(self):
        paths = self.make_folder()
        self.choice = (paths.gui, 'gui')
        app, _ = self.launch()
        for rc, kind in ((1, 'showerror'), (2, 'showwarning'), (5, 'showinfo')):
            app.play()
            app.events.put(('exit', paths.root, rc))
            app._poll()
            self.assertEqual(self.dialogs.calls[-1][0], kind)

    def test_remove(self):
        paths = self.make_folder()
        app, _ = self.launch()
        app.remove()                                  # answered no
        self.assertTrue(os.path.exists(paths.root))
        self.dialogs.answers['Remove firmware'] = True
        app.remove()
        self.assertFalse(os.path.exists(paths.root))
        self.assertEqual(app.tree.get_children(), ())

    # a folder that is already here

    def test_adding_a_ready_folder_offers_play_not_a_build(self):
        paths = self.make_folder()
        self.choice = (paths.gui, 'gui')
        self.dialogs.path = self.src
        app, _ = self.launch()
        app.add()                                   # "Open it now?" no
        self.assertEqual(self.dialogs.calls, [('askyesno', 'Already set up')])
        self.assertEqual(self.spawned, [])
        self.dialogs.answers['Already set up'] = True
        app.add()
        self.assertEqual(self.spawned, [('panel', paths.root, (), False)])
        self.assertEqual(app.tree.sel, (os.path.basename(paths.root),))

    def test_adding_a_folder_that_needs_a_rebuild_offers_it(self):
        paths = self.make_folder()
        self.choice = (None, 'card-changed')
        self.dialogs.path = self.src
        app, _ = self.launch()
        app.add()
        self.assertEqual(self.dialogs.calls, [('askyesno', 'Rebuild needed')])
        self.assertEqual(self.spawned, [])
        self.dialogs.answers['Rebuild needed'] = True
        app.add()
        self.assertEqual(self.spawned, [('first-run', paths.root, ('--rebuild',), True)])

    def test_adding_an_unfinished_folder_resumes_its_build(self):
        paths = self.make_folder()
        self.dialogs.path = self.src
        app, _ = self.launch()
        app.add()
        self.assertEqual(self.spawned, [('first-run', paths.root, (), True)])

    def test_an_incompatible_snapshot_offers_a_rebuild(self):
        paths = self.make_folder()
        self.choice = (paths.gui, 'gui')
        app, _ = self.launch()
        app.play()
        self.dialogs.answers['Rebuild needed'] = True
        app.events.put(('exit', paths.root, portable.EXIT_INCOMPATIBLE))
        app._poll()
        self.assertIn('different build', self.dialogs.texts['Rebuild needed'])
        self.assertEqual(self.spawned[-1], ('first-run', paths.root, ('--rebuild',), True))

    def test_loaded_samples_rebuild_then_reopen_without_asking(self):
        paths = self.make_folder()
        self.choice = (paths.gui, 'gui')
        app, _ = self.launch()
        app.play()
        self.assertEqual(self.spawned[-1][0], 'panel')
        self.next_proc = FakeProc([portable.encode_line(
            portable.result_record(True))], rc=0)
        app.events.put(('exit', paths.root, portable.EXIT_SAMPLES_ADDED))
        app._poll()
        self.assertEqual(self.spawned[-1],
                         ('first-run', paths.root, ('--rebuild',), True))
        self.assertTrue(app.jobs[paths.root].open_after)
        app._read_worker(paths.root, self.next_proc)
        app._poll()
        self.assertEqual(self.spawned[-1], ('panel', paths.root, (), False))
        self.assertNotIn(('askyesno', 'Ready'), self.dialogs.calls)
        self.assertEqual([c for c in self.dialogs.calls
                          if c[0] == 'askyesno'], [])

    # reset to factory

    def test_reset_to_factory(self):
        paths = self.make_folder()
        self.choice = (paths.gui, 'gui')
        app, _ = self.launch()
        self.assertFalse(app.buttons['reset'].disabled)
        app.reset()                                   # answered no
        self.assertTrue(os.path.exists(paths.card))
        self.assertEqual(self.spawned, [])
        self.dialogs.answers['Reset to factory'] = True
        app.reset()
        self.assertFalse(os.path.exists(paths.card))
        self.assertNotIn('stages', _read_state(paths))
        self.assertEqual(self.spawned, [('first-run', paths.root, (), True)])

    def test_reset_is_not_offered_while_a_folder_is_in_use(self):
        paths = self.make_folder()
        self.choice = (paths.gui, 'gui')
        with portable.FolderLock(paths.root, 'panel').acquire():
            app, _ = self.launch()
            self.assertTrue(app.buttons['reset'].disabled)
            self.dialogs.answers['Reset to factory'] = True
            app.reset()
        self.assertTrue(os.path.exists(paths.card))

    # the self-test

    def test_the_selftest_runs_at_start_and_a_failure_is_shown(self):
        app, _ = self.launch()
        self.assertIn(app._run_selftest, self.thread_targets)
        res = portable.summarize_selftest(
            {'ok': False, 'running': 'unicorn_compat', 'checks': []}, 0xC0000409)
        with mock.patch.object(portable, 'run_selftest', return_value=res):
            app._run_selftest()
        app._poll()
        self.assertIn('unicorn_compat', app.banner.get())
        self.assertIn(('showerror', 'digiemu self-test failed'), self.dialogs.calls)
        self.assertIn('unicorn_compat', self.dialogs.texts['digiemu self-test failed'])
        self.assertIs(app.selftest, res)
        self.assertFalse(app.buttons['add'].disabled)       # still usable

    def test_a_passing_selftest_says_nothing(self):
        app, _ = self.launch()
        res = portable.summarize_selftest({'ok': True, 'running': None,
                                           'checks': []}, 0)
        with mock.patch.object(portable, 'run_selftest', return_value=res):
            app._run_selftest()
        app._poll()
        self.assertEqual((app.banner.get(), self.dialogs.calls), ('', []))

    def test_a_selftest_that_cannot_run_is_shown(self):
        app, _ = self.launch()
        with mock.patch.object(portable, 'run_selftest',
                               side_effect=RuntimeError('no handles left')):
            app._run_selftest()
        app._poll()
        self.assertIn('no handles left', app.banner.get())

    def test_a_crashed_build_is_named_in_its_log(self):
        paths = self.make_folder()
        app, _ = self.launch()
        self.next_proc = FakeProc([], rc=0xC0000409)
        app.start_build(paths.root, 'Digitakt 9.99')
        dialog = app.jobs[paths.root].dialog
        app._read_worker(paths.root, self.next_proc)
        app._poll()
        self.assertEqual(dialog.step_var.get(), 'Setup failed')
        with open(os.path.join(paths.logs, 'first-run.log'), 'rb') as fh:
            self.assertIn(b'stack buffer overrun', fh.read())

    def test_quit_with_a_build_running(self):
        paths = self.make_folder()
        app, root = self.launch()
        app.start_build(paths.root, 'Digitakt 9.99')
        self.dialogs.answers['Setup still running'] = None
        app.quit()
        self.assertFalse(root.destroyed)
        self.dialogs.answers['Setup still running'] = True
        app.quit()
        self.assertTrue(root.destroyed and self.next_proc.terminated)

    # the firmware check

    def check_lines(self, passed):
        recs = [{'kind': 'start', 'step': 'boot', 'text': '',
                 'data': {'role': 'build'}},
                {'kind': 'note', 'step': 'boot', 'text': 'intro done',
                 'data': {'role': 'build'}},
                {'kind': 'done', 'step': 'boot', 'text': '',
                 'data': {'role': 'build', 'state': 'passed', 'passed': True}}]
        result = portable.result_record(True, log=portable.CHECK_LOG)
        result['data'] = {'passed': passed,
                          'summary': ['build: %s' % ('PASS' if passed else 'FAIL')]}
        return [portable.encode_line(r) for r in recs + [result]]

    def test_check_offers_the_stock_build_and_runs_the_worker(self):
        stock = self.make_folder()
        self.dialogs.path = self.src
        app, _ = self.launch(release('untested'))
        self.next_proc = FakeProc(self.check_lines(True), rc=0)
        app.check()
        setup = app.check_setup
        self.assertEqual(setup.base_var.get(), '0')        # the stock one
        self.assertEqual(setup.baseline(), stock.syx)
        self.assertIn('About 5 minutes', setup.estimate_var.get())
        setup.timing_var.set(True)
        setup._changed()
        self.assertIn('About 14 minutes', setup.estimate_var.get())
        setup.start()
        self.assertTrue(setup.win.destroyed)
        [(mode, checkdir, extra, piped)] = self.spawned
        self.assertEqual((mode, extra, piped), ('check', (), True))
        self.assertEqual(os.path.dirname(checkdir),
                         portable.checks_root(self.home))
        with open(os.path.join(checkdir, portable.CHECK_REQUEST),
                  encoding='utf-8') as fh:
            req = json.load(fh)
        self.assertEqual((req['syx'], req['baseline'], req['timing'],
                          req['device']), (self.src, stock.syx, True, 'dt1'))
        dialog = app.jobs[checkdir].dialog
        self.assertEqual(app.jobs[checkdir].kind, 'check')
        os.makedirs(os.path.join(checkdir, 'work', 'build'))   # left by a crash
        app._read_worker(checkdir, self.next_proc)
        app._poll()
        self.assertNotIn(checkdir, app.jobs)
        self.assertIs(dialog.passed, True)
        self.assertEqual(dialog.step_var.get(), 'PASS')
        self.assertEqual(dialog.about_var.get(), portable.CHECK_PASSED)
        self.assertIn('Build  boot       passed', dialog.text.lines)
        self.assertIn('         intro done', dialog.text.lines)
        self.assertIn('build: PASS', dialog.text.lines)
        self.assertFalse(os.path.exists(os.path.join(checkdir, 'work')))
        # The firmware list is untouched by a check.
        self.assertEqual(sorted(app.infos), [os.path.basename(stock.root)])

    def test_a_failing_check_says_so(self):
        self.dialogs.path = self.src
        app, _ = self.launch(release('untested'))
        self.next_proc = FakeProc(self.check_lines(False), rc=0)
        app.check()
        self.assertEqual(app.check_setup.base_var.get(), 'none')  # no stock here
        self.assertIn('About 2 minutes', app.check_setup.estimate_var.get())
        app.check_setup.start()
        [(_mode, checkdir, _extra, _piped)] = self.spawned
        dialog = app.jobs[checkdir].dialog
        app._read_worker(checkdir, self.next_proc)
        app._poll()
        self.assertIs(dialog.passed, False)
        self.assertEqual(dialog.step_var.get(), 'FAIL')
        self.assertEqual(dialog.about_var.get(), portable.CHECK_FAILED)

    def test_a_check_that_crashed_shows_why(self):
        self.dialogs.path = self.src
        app, _ = self.launch(release('untested'))
        self.next_proc = FakeProc([], rc=0xC0000005)
        app.check()
        app.check_setup.start()
        [(_mode, checkdir, _extra, _piped)] = self.spawned
        dialog = app.jobs[checkdir].dialog
        app._read_worker(checkdir, self.next_proc)
        app._poll()
        self.assertEqual(dialog.step_var.get(), 'The check could not finish')
        self.assertTrue(any('access violation' in line
                            for line in dialog.text.lines))

    def test_check_refuses_what_it_cannot_boot(self):
        self.dialogs.path = self.src
        app, _ = self.launch(release('unsupported', short=None,
                                     product='Syntakt'))
        app.check()
        self.assertEqual(self.dialogs.calls,
                         [('showerror', 'Cannot check this firmware')])
        self.assertIsNone(app.check_setup)
        self.assertEqual(self.spawned, [])

    def test_choosing_another_stock_file(self):
        self.dialogs.path = self.src
        app, _ = self.launch(release('untested'))
        app.check()
        setup = app.check_setup
        other = os.path.join(self.tmp, 'Digitakt_OS9.99-stock.syx')
        with open(other, 'wb') as fh:
            fh.write(SYX_BYTES)
        self.dialogs.path = other
        setup.base_var.set(setup.OTHER)
        setup._choose_other()
        self.assertEqual(setup.baseline(), other)
        self.assertIn('Stock: Digitakt_OS9.99-stock.syx', setup.estimate_var.get())
        self.dialogs.path = ''                        # a second try, cancelled
        setup._choose_other()
        self.assertEqual(setup.baseline(), other)     # the first choice stands

    def test_cancelling_the_stock_file_choice_goes_back(self):
        self.dialogs.path = self.src
        app, _ = self.launch(release('untested'))
        app.check()
        setup = app.check_setup
        self.dialogs.path = ''
        setup.base_var.set(setup.OTHER)
        setup._choose_other()
        self.assertEqual(setup.base_var.get(), setup.NONE)
        self.assertIsNone(setup.baseline())

    def test_quit_with_a_check_running(self):
        self.dialogs.path = self.src
        app, root = self.launch(release('untested'))
        app.check()
        app.check_setup.start()
        self.dialogs.answers['Check still running'] = None
        app.quit()
        self.assertFalse(root.destroyed)
        self.dialogs.answers['Check still running'] = True
        app.quit()
        self.assertTrue(root.destroyed and self.next_proc.terminated)


if __name__ == '__main__':
    unittest.main()
