# pyright: reportMissingImports=false
"""The panel's contract with the portable app.

Four promises, each of which used to fail silently:

  * a setup failure is REPORTED. config.NotFound and device.DeviceError
    derive from SystemExit, which `except Exception` does not catch, so an
    unknown firmware or a missing devices directory killed the emulator
    thread with `ready` unset and the window waited on "loading snapshot"
    forever;
  * a snapshot made by a different build (a build manifest mismatch) is
    reported as its own kind, `incompatible`, not as a crash;
  * closing saves the session (save_on_exit) atomically, and the window
    waits for that save instead of abandoning it after 5 s; the saved
    snapshot carries no overlay for a file-backed card;
  * `python -m emu.dtpanel` returns a code the launcher can act on: 0 ok,
    1 failed or halted, 2 not saved, 4 incompatible snapshot, 64 usage.

No firmware, no Unicorn run and no window: the emulator's setup is cut off
at the device lookup or replaced by stubs, and the panel's methods run
against stand-ins for the Tk window and the worker thread.
"""
import contextlib
import importlib.util
import io
import os
import pickle
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import deque
from unittest import mock

HAVE_GUI = all(importlib.util.find_spec(m) is not None
               for m in ('tkinter', 'unicorn'))
NEEDS_GUI = unittest.skipUnless(HAVE_GUI, 'emu.gui needs tkinter and unicorn')


def _write(path, data=b''):
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


class Quiet(unittest.TestCase):
    """Collects what the code under test prints (the worker's log lines)."""

    def setUp(self):
        self.out = io.StringIO()
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)


@NEEDS_GUI
class SetupErrorTest(Quiet):
    """Emulator.run reports what used to kill it, and always sets ready."""

    def setUp(self):
        super().setUp()
        from emu import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        # Synthetic stand-in: nothing here reads past the hash.
        self.syx = _write(os.path.join(self.dir, 'fw.syx'),
                          b'\xf0\x00\x20\x3c' + bytes(range(64)) + b'\xf7')
        self.snap = os.path.join(self.dir, 'missing.snap')

    def emulator(self, **kw):
        return self.gui.Emulator(self.snap, syx=self.syx, **kw)

    def test_device_error_is_reported_not_fatal_to_the_thread(self):
        refusal = self.gui.devices.DeviceError('No device file matches fw.syx')
        with mock.patch.object(self.gui.devices, 'identify',
                               side_effect=refusal):
            emu = self.emulator(save_on_exit=os.path.join(self.dir, 'r.snap'))
            emu.start()
            self.assertTrue(emu.ready.wait(10), 'ready never set')
            emu.join(10)
        self.assertFalse(emu.is_alive())
        self.assertEqual(emu.error,
                         'DeviceError: No device file matches fw.syx')
        self.assertEqual(emu.stats['status'], 'failed to load')
        # Nothing ran, so nothing may be saved over a good session.
        self.assertIsNone(emu.saved)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'r.snap')))

    def test_missing_firmware_is_reported(self):
        emu = self.gui.Emulator(self.snap,
                                syx=os.path.join(self.dir, 'nope.syx'))
        emu.run()                       # returns; SystemExit does not escape
        self.assertTrue(emu.ready.is_set())
        self.assertTrue(emu.error.startswith('NotFound: No such firmware file'),
                        emu.error)

    def test_missing_devices_directory_is_reported(self):
        gone = os.path.join(self.dir, 'no-devices')
        with mock.patch.dict(os.environ, {'DT2_DEVICES': gone}):
            emu = self.emulator()
            emu.run()
        self.assertTrue(emu.ready.is_set())
        self.assertEqual(emu.error, 'DeviceError: No devices directory at %s'
                         % gone)

    def test_unknown_firmware_names_its_hash(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with mock.patch.dict(os.environ,
                             {'DT2_DEVICES': os.path.join(repo, 'devices')}):
            emu = self.emulator()
            emu.run()
        self.assertTrue(emu.error.startswith('DeviceError: No device file '
                                             'matches'), emu.error)
        self.assertIn(self.gui.devices.sha256_of(self.syx), emu.error)

    def test_control_surface_lookup_still_degrades(self):
        # After build() the machine is usable: no controls, not no emulator.
        emu = self.emulator()
        with mock.patch.object(self.gui.devices, 'identify',
                               side_effect=self.gui.devices.DeviceError('x')):
            emu._identify_device(None, None)
        self.assertEqual(emu.device_error, 'DeviceError: x')
        self.assertIsNone(emu.error)

    def test_anything_escaping_setup_still_sets_ready(self):
        emu = self.emulator()
        with mock.patch.object(self.gui.Emulator, '_run',
                               side_effect=RuntimeError('boom')):
            emu.run()
        self.assertTrue(emu.ready.is_set())
        self.assertEqual(emu.error, 'RuntimeError: boom')
        self.assertEqual(emu.stats['status'], 'failed to load')

    def test_a_crash_in_the_run_loop_is_reported(self):
        emu = self.emulator()

        def crash():
            emu.ready.set()             # past setup, inside the run loop
            raise ValueError('encoder step out of range')

        with mock.patch.object(emu, '_run', side_effect=crash):
            emu.run()
        self.assertEqual(emu.error, 'ValueError: encoder step out of range')
        self.assertEqual(emu.stats['status'], 'crashed')

    def test_describe_error_of_a_bare_exit(self):
        self.assertEqual(self.gui.describe_error(SystemExit()), 'SystemExit')


def _mismatch(saved, current):
    """What emu.snapshot._validate_manifest raises, word for word."""
    return RuntimeError('checkpoint build manifest mismatch: saved=%r '
                        'current=%r' % (saved, current))


# Two manifests the way longrun.build writes them, differing in the intro
# policy (unblock_except) only.
SAVED = {'protocol': 1, 'isa': 'scoped', 'unblock': True,
         'unblock_except': (), 'flash_sha256': 'ab' * 32,
         'ssi0_dma': {'request_hz': 48000}, 'main_sha256': 'cd' * 32}
CURRENT = dict(SAVED, unblock_except=(0x421cd074,))


@NEEDS_GUI
class IncompatibleSnapshotTest(Quiet):
    """A snapshot from another build is `incompatible`, not a crash.

    Setup is cut off at build(): identify is stubbed (no device, or one
    with audio) and build raises what restore_into raises.
    """

    def setUp(self):
        super().setUp()
        from emu import dtpanel, gui
        self.gui, self.dtpanel = gui, dtpanel
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.syx = _write(os.path.join(self.dir, 'fw.syx'),
                          b'\xf0\x00\x20\x3c' + bytes(range(64)) + b'\xf7')
        self.snap = os.path.join(self.dir, 'resume.snap')     # not read
        self.resume = os.path.join(self.dir, 'r2.snap')

    def run_emulator(self, *errors, dev=None, audio=True):
        with mock.patch.object(self.gui.devices, 'identify',
                               return_value=(dev, None)), \
                mock.patch.object(self.gui, '_accelerated',
                                  return_value=False), \
                mock.patch.object(self.gui, 'build',
                                  side_effect=list(errors)) as build:
            emu = self.gui.Emulator(self.snap, syx=self.syx, audio=audio,
                                    save_on_exit=self.resume)
            emu.run()
        self.build = build
        return emu

    def exit_code(self, emu):
        """The real DigitaktPanel.exit_code, on the real Emulator's state."""
        return self.dtpanel.DigitaktPanel.exit_code(
            types.SimpleNamespace(emu=emu))

    def test_manifest_mismatch_is_its_own_kind(self):
        emu = self.run_emulator(_mismatch(SAVED, CURRENT))
        self.assertTrue(emu.ready.is_set())
        self.assertTrue(emu.incompatible)
        self.assertTrue(emu.error.startswith(
            'incompatible snapshot: rebuild needed.'), emu.error)
        # Names the file and what changed, not two whole manifests.
        self.assertIn('resume.snap', emu.error)
        self.assertIn('differs in: unblock_except', emu.error)
        self.assertNotIn('flash_sha256', emu.error)
        self.assertEqual(emu.stats['status'], 'failed to load')
        # The log keeps the raw message, with both manifests in it.
        self.assertIn('checkpoint build manifest mismatch: saved=',
                      self.out.getvalue())
        # Nothing ran, so nothing is saved over the previous session.
        self.assertIsNone(emu.saved)
        self.assertFalse(os.path.exists(self.resume))
        self.assertEqual(self.exit_code(emu), self.dtpanel.INCOMPATIBLE)
        self.assertEqual(self.dtpanel.INCOMPATIBLE, 4)

    def test_an_older_checkpoint_format_is_incompatible_too(self):
        emu = self.run_emulator(
            RuntimeError('unsupported checkpoint version 1'))
        self.assertTrue(emu.incompatible)
        self.assertIn('(unsupported checkpoint version 1)', emu.error)
        self.assertEqual(self.exit_code(emu), 4)

    def test_a_snapshot_that_will_not_open_has_its_own_code(self):
        # A damaged or missing snapshot: LOAD_FAILED, so the launcher stops
        # offering it (a damaged resume.snap falls back to gui.snap).
        for exc in (RuntimeError('invalid checkpoint blob'),
                    RuntimeError('Timers checkpoint source count mismatch'),
                    RuntimeError('unsupported deque checkpoint version 2'),
                    OSError('No such file or directory')):
            with self.subTest(exc=exc):
                emu = self.run_emulator(exc)
                self.assertFalse(emu.incompatible)
                self.assertEqual(emu.error, self.gui.describe_error(exc))
                self.assertEqual(self.exit_code(emu),
                                 self.dtpanel.LOAD_FAILED)

    def test_a_device_error_is_still_a_failure(self):
        with mock.patch.object(self.gui.devices, 'identify',
                               side_effect=self.gui.devices.DeviceError('x')):
            emu = self.gui.Emulator(self.snap, syx=self.syx)
            emu.run()
        self.assertFalse(emu.incompatible)
        self.assertEqual(self.exit_code(emu), 1)

    def test_no_audio_retry_for_a_snapshot_from_another_build(self):
        # The legacy audio upgrade validates the manifest WITHOUT its audio
        # entry, so building again without audio is refused the same way:
        # one build, and the mismatch -- not an audio problem -- reported.
        dev = types.SimpleNamespace(
            intro_unblocks_frame_sem=True,
            audio={'request_hz': 48000, 'fallback_request_hz': 0,
                   'ssi_profile': 'mk1'})
        emu = self.run_emulator(_mismatch(SAVED, CURRENT), dev=dev)
        self.assertEqual(self.build.call_count, 1)
        self.assertTrue(self.build.call_args.kwargs['ssi0_legacy_upgrade'])
        self.assertIsNone(emu.audio_error)
        self.assertTrue(emu.incompatible)

    def test_an_audio_refusal_still_retries_without_audio(self):
        dev = types.SimpleNamespace(
            intro_unblocks_frame_sem=True,
            audio={'request_hz': 48000, 'fallback_request_hz': 0,
                   'ssi_profile': 'mk1'})
        emu = self.run_emulator(
            RuntimeError('SSI0 DMA model requires the force-ISR RTE symbol'),
            _mismatch(SAVED, CURRENT), dev=dev)
        self.assertEqual(self.build.call_count, 2)
        self.assertNotIn('ssi0_request_hz', self.build.call_args.kwargs)
        self.assertIn('force-ISR', emu.audio_error)
        self.assertTrue(emu.incompatible)

    def test_escaping_setup_is_classified_too(self):
        emu = self.gui.Emulator(self.snap, syx=self.syx)
        with mock.patch.object(self.gui.Emulator, '_run',
                               side_effect=_mismatch(SAVED, CURRENT)):
            emu.run()
        self.assertTrue(emu.incompatible)
        self.assertEqual(self.exit_code(emu), 4)

    def test_classifier(self):
        yes = self.gui.snapshot_incompatible
        self.assertTrue(yes(_mismatch({}, {'a': 1})))
        wrapped = RuntimeError('could not open resume.snap')
        wrapped.__cause__ = _mismatch({}, {'a': 1})
        self.assertTrue(yes(wrapped))
        # Only emu.snapshot's own refusal, raised as a RuntimeError.
        self.assertFalse(yes(ValueError('checkpoint build manifest mismatch')))
        self.assertFalse(yes(RuntimeError('invalid checkpoint manifest')))
        self.assertFalse(yes(None))
        loop = RuntimeError('a')
        loop.__cause__ = loop
        self.assertFalse(yes(loop))                 # bounded, no hang

    def test_manifest_diff(self):
        diff = self.gui.manifest_diff
        self.assertEqual(diff(str(_mismatch(SAVED, CURRENT))),
                         ['unblock_except'])
        # A key only one side has differs too (e.g. audio added).
        saved = {k: v for k, v in SAVED.items() if k != 'ssi0_dma'}
        self.assertEqual(diff(str(_mismatch(saved, CURRENT))),
                         ['ssi0_dma', 'unblock_except'])
        self.assertEqual(diff('unsupported checkpoint version 1'), [])
        self.assertEqual(diff('checkpoint build manifest mismatch: '
                              'saved={oops current=[1'), [])

    def test_unparsable_mismatch_still_reads_as_incompatible(self):
        exc = RuntimeError('checkpoint build manifest mismatch: saved=<x> '
                           'current=<y>')
        msg = self.gui.incompatible_message('/a/b/gui.snap', exc)
        self.assertTrue(msg.startswith('incompatible snapshot: rebuild '
                                       'needed. gui.snap was saved'), msg)
        self.assertIn('(checkpoint build manifest mismatch)', msg)


@NEEDS_GUI
class SavedSsiTest(unittest.TestCase):
    """Which build a snapshot needs is read off its manifest."""

    def setUp(self):
        from emu import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def blob(self, name, manifest):
        path = os.path.join(self.dir, name)
        with open(path, 'wb') as fh:
            pickle.dump({'manifest': manifest, 'pages': {}}, fh, protocol=4)
        return path

    def test_saved_on_exit_snapshot(self):
        path = self.blob('r.snap', {'protocol': 1,
                                    'ssi0_dma': {'request_hz': 48000}})
        self.assertEqual(self.gui.saved_ssi0(path), {'request_hz': 48000})

    def test_older_snapshots_need_the_legacy_upgrade(self):
        self.assertIsNone(self.gui.saved_ssi0(self.blob('g.snap',
                                                        {'protocol': 1})))
        self.assertIsNone(self.gui.saved_ssi0(self.blob('l.snap', None)))
        self.assertIsNone(self.gui.saved_ssi0(
            _write(os.path.join(self.dir, 'junk.snap'), b'not a pickle')))
        self.assertIsNone(self.gui.saved_ssi0(os.path.join(self.dir, 'no')))


@NEEDS_GUI
class SaveSessionTest(Quiet):
    """_save_session: the uisettle/introboot save, atomically."""

    def setUp(self):
        super().setUp()
        from emu import gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'resume.snap')
        self.emu = gui.Emulator('gui.snap', save_on_exit=self.path)
        self.emu.stats['instrs'] = 5000
        self.pits = object()
        self.claims = []

        def claim(name, component):
            self.claims.append((name, component))
            self.components[name] = component
            return False

        self.components = {'uart_in': deque()}
        self.ev = {'checkpoint_components': self.components,
                   'checkpoint_manifest': {'protocol': 1},
                   'claim_checkpoint_component': claim,
                   'tasks': [(0x40032f5a, 6, 0x41000100)]}
        self.st = {'n': 700, 'task_create_hits': {0x41000010: {'prio': 3}}}
        self.calls = []

    def fake_save(self, fail=False):
        def save(m, path, extra=None, components=None, manifest=None):
            self.calls.append(dict(path=path, extra=extra,
                                   components=dict(components),
                                   manifest=manifest))
            _write(path, b'partial' if fail else b'NEW')
            if fail:
                raise OSError('disk full')
        return mock.patch.object(self.gui, 'save_snapshot', save)

    def test_saves_to_a_temporary_then_renames(self):
        with self.fake_save():
            self.assertTrue(self.emu._save_session(object(), self.ev, self.st,
                                                   self.pits))
        call, = self.calls
        self.assertEqual(call['path'], self.path + '.tmp')
        with open(self.path, 'rb') as fh:
            self.assertEqual(fh.read(), b'NEW')
        self.assertFalse(os.path.exists(self.path + '.tmp'))
        self.assertEqual(self.emu.saved, self.path)
        self.assertIsNone(self.emu.save_error)
        # The timers are claimed, so the snapshot carries its own cadence.
        self.assertEqual(self.claims, [('timers', self.pits)])
        self.assertIs(call['components']['timers'], self.pits)
        self.assertEqual(call['manifest'], {'protocol': 1})
        # ev['tasks'] triples become the TCB-keyed map restore_into reads,
        # alongside the tasks the opened snapshot carried; n accumulates.
        self.assertEqual(call['extra']['tasks'], {
            '0x41000010': {'prio': 3},
            '0x41000100': {'entry': 0x40032f5a, 'prio': 6,
                           'tcb': 0x41000100}})
        self.assertEqual(call['extra']['n'], 5700)
        for key in call['extra']['tasks']:
            int(key, 16)                # what restore_into does with them

    def test_restored_timers_are_not_claimed_twice(self):
        self.components['timers'] = self.pits
        with self.fake_save():
            self.emu._save_session(object(), self.ev, self.st, self.pits)
        self.assertEqual(self.claims, [])

    def test_a_failed_save_keeps_the_previous_session(self):
        _write(self.path, b'OLD')
        with self.fake_save(fail=True):
            self.assertFalse(self.emu._save_session(object(), self.ev,
                                                    self.st, self.pits))
        with open(self.path, 'rb') as fh:
            self.assertEqual(fh.read(), b'OLD')
        self.assertFalse(os.path.exists(self.path + '.tmp'))
        self.assertIsNone(self.emu.saved)
        self.assertEqual(self.emu.save_error, 'OSError: disk full')
        self.assertEqual(self.emu.stats['status'], 'save failed')

    def save_with_replace_retry(self, retry):
        """_save_session with emu.bootstrap.replace_retry replaced by
        `retry`, which need not exist in emu.bootstrap yet."""
        from emu import bootstrap
        with self.fake_save(), mock.patch.object(bootstrap, 'replace_retry',
                                                 retry, create=True):
            return self.emu._save_session(object(), self.ev, self.st,
                                          self.pits)

    def test_the_rename_is_retried_through_bootstrap(self):
        # A launcher or a virus scanner reading resume.snap at the moment of
        # the rename is a sharing violation on Windows; replace_retry waits
        # it out where a bare os.replace would lose the session.
        calls = []

        def retry(src, dst, *args, **kwargs):
            calls.append((src, dst))
            os.replace(src, dst)

        self.assertTrue(self.save_with_replace_retry(retry))
        self.assertEqual(calls, [(self.path + '.tmp', self.path)])
        with open(self.path, 'rb') as fh:
            self.assertEqual(fh.read(), b'NEW')
        self.assertEqual(self.emu.saved, self.path)

    def test_a_rename_that_never_succeeds_keeps_the_previous_session(self):
        _write(self.path, b'OLD')

        def retry(src, dst, *args, **kwargs):
            raise PermissionError(13, 'Access is denied', dst)

        self.assertFalse(self.save_with_replace_retry(retry))
        with open(self.path, 'rb') as fh:
            self.assertEqual(fh.read(), b'OLD')
        self.assertFalse(os.path.exists(self.path + '.tmp'))
        self.assertIsNone(self.emu.saved)
        self.assertTrue(self.emu.save_error.startswith('PermissionError'),
                        self.emu.save_error)

    def test_without_replace_retry_it_is_a_plain_replace(self):
        # emu.gui imports the helper lazily and does without it.
        old = sys.modules.get('emu.bootstrap')
        sys.modules['emu.bootstrap'] = types.ModuleType('emu.bootstrap')
        try:
            with self.fake_save():
                ok = self.emu._save_session(object(), self.ev, self.st,
                                            self.pits)
        finally:
            if old is None:
                sys.modules.pop('emu.bootstrap', None)
            else:
                sys.modules['emu.bootstrap'] = old
        self.assertTrue(ok, self.emu.save_error)
        with open(self.path, 'rb') as fh:
            self.assertEqual(fh.read(), b'NEW')
        self.assertFalse(os.path.exists(self.path + '.tmp'))


class _Uc:
    """Enough of Unicorn for Esdhc's registers and emu.snapshot.save."""

    def __init__(self):
        self.memory = {}

    def hook_add(self, *args, **kwargs):
        return 1

    def mem_read(self, addr, size):
        return bytes(self.memory.get(addr + i, 0) for i in range(size))

    def mem_write(self, addr, data):
        for i, value in enumerate(data):
            self.memory[addr + i] = value

    def reg_read(self, reg):
        return 0


class _Machine:
    """A machine with no guest pages: the blob holds registers, components
    and the manifest, which is all this test reads."""

    def __init__(self):
        self.uc = _Uc()
        self.mapped = set()
        self.mmio, self.ctlregs = {}, {}
        self.ff1_count = self.movec_count = 0

    def ensure(self, addr):
        pass


class _Clock:
    """The timers component, as a custom one (its schema is not checked)."""

    def checkpoint_state(self):
        return {'type': 'StubClock', 'version': 1}


@NEEDS_GUI
class SavedCardTest(Quiet):
    """resume.snap never carries the overlay of a file-backed card.

    After the exit flush the image file is the truth. A resume.snap that
    carried the session's card writes would replay them over the file on
    every launch -- over anything written there since -- and hold them in
    host memory as a per-byte dict. Esdhc.checkpoint_state empties them for
    a file-backed card; this checks it where it matters, in the blob the
    panel's own save writes: the real _save_session, emu.snapshot.save and
    Esdhc, on a stand-in machine and a real Card over a small image file.
    """

    def setUp(self):
        super().setUp()
        from emu import esdhc, gui, snapshot
        self.gui, self.esdhc, self.snapshot = gui, esdhc, snapshot
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, 'resume.snap')

    def save_session(self, card):
        """A session that wrote sector 3 and erased sectors 0-1, stopped
        and saved the way Emulator._run's clean stop does. -> the esdhc
        component state read back from the saved snapshot."""
        m = _Machine()
        esd = self.esdhc.Esdhc(m, card=card)
        card.write_data(25, 3, b'\x5a' * 512)
        card.erase(0, 1024)
        components = {'uart_in': deque(), 'esdhc': esd}
        ev = {'checkpoint_components': components,
              'checkpoint_manifest': {'protocol': 1},
              'claim_checkpoint_component': components.__setitem__,
              'tasks': []}
        emu = self.gui.Emulator('gui.snap', save_on_exit=self.path)
        card.flush()                        # the exit flush comes first
        self.assertTrue(emu._save_session(m, ev, {'n': 0}, _Clock()),
                        emu.save_error)
        blob = self.snapshot._load_blob(self.path)      # validates it too
        self.assertEqual(sorted(blob['components']),
                         ['esdhc', 'timers', 'uart_in'])
        return blob['components']['esdhc']

    def test_a_file_backed_card_is_not_in_the_snapshot(self):
        image = _write(os.path.join(self.dir, 'plusdrive.img'),
                       b'\xee' * 4096)
        card = self.esdhc.Card(path=image)
        self.addCleanup(card.close)         # the map pins the file
        state = self.save_session(card)
        self.assertEqual(state['card_overlay'], {})
        self.assertEqual(state['card_erased'], [])
        # ... because the writes are on the card file instead.
        card.close()
        with open(image, 'rb') as fh:
            data = fh.read()
        self.assertEqual(data[:1024], bytes(1024))
        self.assertEqual(data[1024:1536], b'\xee' * 512)
        self.assertEqual(data[1536:2048], b'\x5a' * 512)
        self.assertEqual(data[2048:], b'\xee' * 2048)

    def test_an_in_memory_card_still_carries_its_writes(self):
        # The control: the same session on a card with no file keeps its
        # bytes in the snapshot, so the check above reads the right field.
        state = self.save_session(self.esdhc.Card())
        self.assertEqual(len(state['card_overlay']), 512)
        self.assertEqual(state['card_erased'], [[0, 1024]])


@NEEDS_GUI
class ArgumentTest(Quiet):
    def setUp(self):
        super().setUp()
        from emu import dtpanel
        self.dtpanel = dtpanel

    def parse(self, *argv):
        return self.dtpanel.parse_args(list(argv))

    def test_contract_form(self):
        a = self.parse('gui.snap', '--syx', 'fw.syx', '--save-on-exit',
                       'resume.snap', '--no-audio')
        self.assertEqual((a.snapshot, a.syx, a.save_on_exit, a.audio),
                         ('gui.snap', 'fw.syx', 'resume.snap', False))

    def test_defaults(self):
        a = self.parse()
        self.assertEqual((a.snapshot, a.syx, a.save_on_exit, a.audio),
                         (None, None, None, True))

    def test_old_positional_syx_still_works(self):
        a = self.parse('gui.snap', 'fw.syx')
        self.assertEqual((a.snapshot, a.syx), ('gui.snap', 'fw.syx'))
        self.assertFalse(hasattr(a, 'legacy_syx'))

    def test_options_before_the_snapshot(self):
        a = self.parse('--save-on-exit', 'r.snap', 'gui.snap')
        self.assertEqual((a.snapshot, a.save_on_exit), ('gui.snap', 'r.snap'))

    def test_bad_arguments_return_rather_than_exit(self):
        for argv in (['--save-on-exit'], ['--bogus'],
                     ['gui.snap', 'a.syx', '--syx', 'b.syx']):
            with self.subTest(argv=argv):
                with self.assertRaises(self.dtpanel._Stop) as cm:
                    self.dtpanel.parse_args(argv)
                self.assertEqual(cm.exception.code,
                                 self.dtpanel.USAGE_ERROR)
        self.assertIn('usage:', self.out.getvalue())

    def test_help_returns_zero(self):
        with self.assertRaises(self.dtpanel._Stop) as cm:
            self.dtpanel.parse_args(['--help'])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn('--save-on-exit', self.out.getvalue())

    def test_a_windowed_build_has_nowhere_to_print(self):
        # pythonw / a windowed exe: sys.stdout and sys.stderr are None.
        with mock.patch('sys.stdout', None), mock.patch('sys.stderr', None):
            with self.assertRaises(self.dtpanel._Stop) as cm:
                self.dtpanel.parse_args(['--bogus'])
        self.assertEqual(cm.exception.code, self.dtpanel.USAGE_ERROR)


class _FakePanel:
    """Stands in for DigitaktPanel in main(): records, never opens a window."""
    made = []
    code = 0

    def __init__(self, snapshot, syx=None, audio=True, save_on_exit=None,
                 app=False):
        self.args = dict(snapshot=snapshot, syx=syx, audio=audio,
                         save_on_exit=save_on_exit, app=app)
        self.env_card = os.environ.get('DT2_PLUSDRIVE')
        self.quit = 0
        _FakePanel.made.append(self)

    def mainloop(self):
        pass

    def quit_all(self):
        self.quit += 1

    def exit_code(self):
        return _FakePanel.code


@NEEDS_GUI
class MainTest(Quiet):
    def setUp(self):
        super().setUp()
        from emu import dtpanel
        self.dtpanel = dtpanel
        self.real_panel = dtpanel.DigitaktPanel
        _FakePanel.made = []
        _FakePanel.code = 0
        patch = mock.patch.object(dtpanel, 'DigitaktPanel', _FakePanel)
        patch.start()
        self.addCleanup(patch.stop)

    def main(self, *argv, env=None, clear=()):
        with mock.patch.dict(os.environ, env or {}):
            for name in clear:
                os.environ.pop(name, None)
            code = self.dtpanel.main(list(argv))
        return code

    def test_passes_the_contract_arguments_through(self):
        code = self.main('/abs/gui.snap', '--syx', '/abs/fw.syx',
                         '--save-on-exit', '/abs/resume.snap',
                         env={'DT2_PLUSDRIVE': '/abs/plusdrive.img'})
        self.assertEqual(code, 0)
        panel, = _FakePanel.made
        self.assertEqual(panel.args, dict(snapshot='/abs/gui.snap',
                                          syx='/abs/fw.syx', audio=True,
                                          save_on_exit='/abs/resume.snap',
                                          app=False))
        self.assertEqual(panel.quit, 1)

    def test_returns_the_panels_exit_code(self):
        for code in (0, 1, 2, 4):
            _FakePanel.code = code
            self.assertEqual(self.main('gui.snap'), code)

    def test_exit_codes_from_the_emulators_state(self):
        # main() -> the real DigitaktPanel.exit_code, on a stand-in for the
        # Emulator's end state. No window, no thread.
        real_exit_code = self.real_panel.exit_code
        mismatch = ('incompatible snapshot: rebuild needed. resume.snap was '
                    'saved by a different build of the emulator or firmware')
        cases = [
            # (error, incompatible, saved) -> code
            ((None, False, '/abs/resume.snap'), 0),
            (('halted: unhandled vector 4 at pc=0x40001000', False, None), 1),
            (('RuntimeError: invalid checkpoint blob', False, None), 1),
            ((None, False, None), 2),
            # A snapshot from another build: its own code, whatever else is
            # true -- nothing was saved over it, and error is set as well.
            ((mismatch, True, None), self.dtpanel.INCOMPATIBLE),
        ]
        for (error, incompatible, saved), want in cases:
            emu = types.SimpleNamespace(
                error=error, incompatible=incompatible, saved=saved,
                save_on_exit='/abs/resume.snap', is_alive=lambda: False)

            class Panel(_FakePanel):
                def exit_code(self):
                    return real_exit_code(self)

            Panel.emu = emu
            with self.subTest(error=error, incompatible=incompatible,
                              saved=saved), \
                    mock.patch.object(self.dtpanel, 'DigitaktPanel', Panel):
                self.assertEqual(self.main('/abs/resume.snap', '--syx',
                                           '/abs/fw.syx', '--save-on-exit',
                                           '/abs/resume.snap'), want)
        self.assertEqual(self.dtpanel.INCOMPATIBLE, 4)

    def test_app_and_loaded_samples(self):
        _FakePanel.code = self.dtpanel.SAMPLES_ADDED
        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(self.main('gui.snap', '--app'), 6)
        self.assertTrue(_FakePanel.made[0].args['app'])
        self.assertNotIn('rebuild', out.getvalue())
        with mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(self.main('gui.snap'), 6)
        self.assertFalse(_FakePanel.made[1].args['app'])
        self.assertIn('rebuild them from the cold boot', out.getvalue())

    def test_an_explicit_card_is_never_replaced(self):
        self.main('gui.snap', env={'DT2_PLUSDRIVE': '/abs/card.img'})
        self.main('gui.snap', env={'DT2_PLUSDRIVE': ''})
        self.assertEqual([p.env_card for p in _FakePanel.made],
                         ['/abs/card.img', ''])

    def test_the_default_card_when_none_is_set(self):
        self.main('gui.snap', clear=('DT2_PLUSDRIVE',))
        self.assertEqual(_FakePanel.made[0].env_card, 'plusdrive.img')

    def test_usage_error_opens_no_window(self):
        self.assertEqual(self.main('--save-on-exit'), self.dtpanel.USAGE_ERROR)
        self.assertEqual(_FakePanel.made, [])

    def test_no_firmware_is_a_failure_not_an_exit(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                code = self.main(clear=('DT2_SYX',))
            finally:
                os.chdir(old)
        self.assertEqual(code, 1)
        self.assertEqual(_FakePanel.made, [])
        self.assertIn('No firmware found', self.out.getvalue())


class _FakeWorker(threading.Thread):
    """The Emulator's stop protocol, with the timings under test control."""
    daemon = True

    def __init__(self, load=0.0, finish=0.0, stuck=False, save=True):
        super().__init__()
        self.load, self.finish, self.stuck = load, finish, stuck
        self.stop_flag = threading.Event()
        self.pause = threading.Event()
        self.ready = threading.Event()
        self.finishing = threading.Event()
        self.release = threading.Event()        # frees a stuck worker
        self.save_on_exit = 'resume.snap' if save else None
        self.saved = None
        self.error = None

    def run(self):
        time.sleep(self.load)
        self.ready.set()
        if self.stuck:                          # inside Unicorn for good
            self.release.wait(30)
            return
        self.stop_flag.wait(30)
        self.finishing.set()
        time.sleep(self.finish)                 # flush and save
        self.saved = self.save_on_exit


class _SampleWorker(_FakeWorker):
    """_FakeWorker plus the card side of a clean stop: `flushed`, and the
    `release_card` the panel sets before stopping."""

    def __init__(self, flush=True, **kw):
        super().__init__(**kw)
        self.flush_ok = flush
        self.flushed = False
        self.release_card = False
        self.save_error = None
        self.released_at_stop = None

    def run(self):
        super().run()
        self.released_at_stop = self.release_card
        if self.flush_ok:
            self.flushed = True
        else:
            self.saved = None
            self.save_error = '+Drive image flush failed: disk full'


@NEEDS_GUI
class LoadSamplesTest(Quiet):
    """LOAD SAMPLES: check first, then save and stop, then write, then
    SAMPLES_ADDED -- and nothing stopped when there is nothing to load."""

    def setUp(self):
        super().setUp()
        from emu import dtpanel, samples
        self.dtpanel, self.samples = dtpanel, samples
        cls = dtpanel.DigitaktPanel

        class Window:
            STOP_TIMEOUT = 2.0
            load_samples = cls.load_samples
            _close_soon = cls._close_soon
            quit_all = cls.quit_all
            _stop_emulator = cls._stop_emulator
            _wait_for_worker = cls._wait_for_worker
            exit_code = cls.exit_code
            _failure = cls._failure

            def __init__(self, emu, app=True):
                self.emu, self.player, self.app = emu, None, app
                self.samples_added = []
                self.said, self.destroyed, self.idle = [], 0, []

            def _say(self, text, fill=None):
                self.said.append(text)

            def destroy(self):
                self.destroyed += 1

            def after_idle(self, fn):
                self.idle.append(fn)

        self.Window = Window
        self.calls = []
        self.picked = ('C:/s/kick.wav', 'C:/s/snare.wav')
        self.answer = True
        self.plan_result = None
        self.plan_error = None
        self.write_error = None
        env = mock.patch.dict(os.environ, {'DT2_PLUSDRIVE': 'C:/fw/plusdrive.img'})
        env.start()
        self.addCleanup(env.stop)
        for target, fn in (
                ('tkinter.filedialog.askopenfilenames', self._pick),
                ('tkinter.messagebox.askokcancel', self._ask),
                ('tkinter.messagebox.showerror', self._shown('showerror')),
                ('tkinter.messagebox.showinfo', self._shown('showinfo')),
                ('emu.samples.plan', self._plan),
                ('emu.samples.write', self._write)):
            p = mock.patch(target, fn)
            p.start()
            self.addCleanup(p.stop)

    # -- stand-ins -------------------------------------------------------
    def _pick(self, **kw):
        self.calls.append(('pick', kw.get('filetypes')))
        return self.picked

    def _ask(self, title, text, **kw):
        self.calls.append(('ask', text))
        return self.answer

    def _shown(self, kind):
        def show(title, text, **kw):
            self.calls.append((kind, text))
        return show

    def _plan(self, files, card):
        self.calls.append(('plan', tuple(files), card))
        if self.plan_error:
            raise self.plan_error
        if self.plan_result is not None:
            return self.plan_result
        p = self.samples.Plan(card)
        p.samples = [self.samples.Sample(f, os.path.basename(f)[:-4], 48000,
                                         4800, 1) for f in files]
        return p

    def _write(self, plan, progress=None):
        emu = self.win.emu
        self.calls.append(('write', emu.is_alive(), emu.released_at_stop,
                           emu.saved))
        for s in plan.samples:
            progress(s)
            if self.write_error and plan.written:
                raise self.write_error
            plan.written.append((s.name, 10 + len(plan.written)))
        return plan.written

    def window(self, worker=None, app=True):
        worker = worker or _SampleWorker()
        self.addCleanup(worker.release.set)
        worker.start()
        worker.ready.wait(5)
        self.win = self.Window(worker, app)
        return self.win

    def kinds(self):
        return [c[0] for c in self.calls]

    # -- tests -----------------------------------------------------------
    def test_saves_stops_writes_and_asks_for_a_rebuild(self):
        win = self.window()
        win.load_samples()
        self.assertEqual(self.kinds(), ['pick', 'plan', 'ask', 'write'])
        self.assertEqual(self.calls[0][1], self.samples.WAV_TYPES)
        self.assertEqual(self.calls[1][2], 'C:/fw/plusdrive.img')
        # Written only once the emulator had stopped, saved the session and
        # been asked to let go of the card.
        self.assertEqual(self.calls[3][1:], (False, True, 'resume.snap'))
        self.assertEqual(win.samples_added, ['kick', 'snare'])
        # Closed after the click, never inside it: destroying the window
        # from the canvas binding that ran this made Tcl panic ('alloc:
        # invalid block', 0x80000003).
        self.assertEqual((win.destroyed, win.idle), (0, [win.destroy]))
        win.idle.pop()()
        self.assertEqual(win.destroyed, 1)
        self.assertEqual(win.exit_code(), self.dtpanel.SAMPLES_ADDED)
        self.assertTrue(any('loading snare' in t for t in win.said))

    def test_nothing_picked_or_cancelled_changes_nothing(self):
        win = self.window()
        self.picked = ''
        win.load_samples()
        self.answer = False
        self.picked = ('C:/s/kick.wav',)
        win.load_samples()
        self.assertEqual(self.kinds(), ['pick', 'pick', 'plan', 'ask'])
        self.assertTrue(win.emu.is_alive())
        self.assertEqual((win.samples_added, win.destroyed), ([], 0))
        win.quit_all()
        self.assertEqual(win.exit_code(), 0)

    def test_files_that_cannot_go_on_stop_nothing(self):
        win = self.window()
        p = self.samples.Plan('card')
        p.rejected = [('C:/s/notes.wav', 'not a RIFF/WAVE file')]
        self.plan_result = p
        win.load_samples()
        self.assertEqual(self.kinds(), ['pick', 'plan', 'showerror'])
        self.assertIn('notes.wav: not a RIFF/WAVE file', self.calls[-1][1])
        self.plan_error = self.samples.Error('the +Drive has no /incoming folder')
        win.load_samples()
        self.assertIn('no /incoming', self.calls[-1][1])
        self.assertTrue(win.emu.is_alive())
        self.assertEqual(win.destroyed, 0)

    def test_needs_a_card_and_a_running_emulator(self):
        with mock.patch.dict(os.environ, {'DT2_PLUSDRIVE': ''}):
            self.window().load_samples()
        self.assertEqual(self.kinds(), ['showinfo'])
        self.assertIn('no +Drive image', self.calls[0][1])
        loading = _SampleWorker(load=5.0)
        self.addCleanup(loading.release.set)
        loading.start()
        self.Window(loading).load_samples()
        self.assertEqual(self.kinds(), ['showinfo', 'showinfo'])
        self.assertIn('once the Digitakt is running', self.calls[1][1])

    def test_a_card_that_did_not_flush_gets_nothing(self):
        win = self.window(_SampleWorker(flush=False))
        win.load_samples()
        self.assertEqual(self.kinds(), ['pick', 'plan', 'ask', 'showerror'])
        self.assertIn('flush failed', self.calls[-1][1])
        self.assertEqual(win.samples_added, [])
        self.assertEqual((win.destroyed, win.idle), (0, [win.destroy]))
        self.assertNotEqual(win.exit_code(), self.dtpanel.SAMPLES_ADDED)

    def test_a_write_that_fails_part_way_still_asks_for_a_rebuild(self):
        win = self.window()
        self.write_error = OSError('disk full')
        win.load_samples()
        self.assertEqual(self.kinds(), ['pick', 'plan', 'ask', 'write',
                                        'showerror'])
        self.assertIn('1 of 2', self.calls[-1][1])
        self.assertEqual(win.samples_added, ['kick'])
        self.assertEqual(win.exit_code(), self.dtpanel.SAMPLES_ADDED)

    def test_the_question_says_what_happens_next(self):
        p = self.samples.Plan('card')
        p.samples = [self.samples.Sample('a/kick.wav', 'kick', 44100, 44100, 6),
                     self.samples.Sample('b/kick.wav', 'kick-2', 48000, 24000,
                                         3, renamed=True)]
        p.rejected = [('c/x.txt', 'not a RIFF/WAVE file')]
        app = self.dtpanel.load_question(p, app=True)
        self.assertIn('Load 2 samples into /incoming', app)
        self.assertIn('kick  (1.00 s, 44100 Hz)', app)
        self.assertIn('kick-2  [renamed: that name is taken]  (0.50 s', app)
        self.assertIn('x.txt: not a RIFF/WAVE file', app)
        self.assertIn('rebuilds it', app)
        alone = self.dtpanel.load_question(p, app=False)
        self.assertIn('rebuild the snapshots', alone)
        self.assertNotIn('rebuilds it', alone)


class StopCleanlyTest(Quiet):
    """The emulator's clean stop: flush, save, then -- only when asked and
    only after a good flush -- close the card."""

    def setUp(self):
        super().setUp()
        from emu import gui
        self.order = []
        self.emu = gui.Emulator('gui.snap', save_on_exit='resume.snap')
        self.emu._save_session = lambda *a: self.order.append('save')
        order = self.order

        class Card:
            fail = False

            def flush(self):
                order.append('flush')
                if self.fail:
                    raise OSError('disk full')

            def close(self):
                order.append('close')

        self.card = Card()
        self.ev = {'esdhc': types.SimpleNamespace(card=self.card)}

    def stop(self):
        self.emu._stop_cleanly(None, self.ev, {}, None)

    def test_the_card_stays_open_unless_asked(self):
        self.stop()
        self.assertEqual(self.order, ['flush', 'save'])
        self.assertTrue(self.emu.flushed and self.emu.finishing.is_set())

    def test_released_after_the_save(self):
        self.emu.release_card = True
        self.stop()
        self.assertEqual(self.order, ['flush', 'save', 'close'])

    def test_a_failed_flush_neither_saves_nor_releases(self):
        self.emu.release_card = True
        self.card.fail = True
        self.stop()
        self.assertEqual(self.order, ['flush'])
        self.assertFalse(self.emu.flushed)
        self.assertIn('flush failed', self.emu.save_error)


@NEEDS_GUI
class CloseTest(Quiet):
    """quit_all waits for a save; it gives up only on a stuck step."""

    def setUp(self):
        super().setUp()
        from emu import dtpanel
        cls = dtpanel.DigitaktPanel

        class Window:
            STOP_TIMEOUT = 0.2
            quit_all = cls.quit_all
            _stop_emulator = cls._stop_emulator
            _wait_for_worker = cls._wait_for_worker
            exit_code = cls.exit_code
            _failure = cls._failure

            def __init__(self, emu):
                self.emu, self.player = emu, None
                self.said, self.destroyed = [], 0

            def _say(self, text, fill=None):
                self.said.append(text)

            def destroy(self):
                self.destroyed += 1

        self.Window = Window

    def close(self, worker):
        self.addCleanup(worker.release.set)
        worker.start()
        if not worker.load:
            worker.ready.wait(5)
        win = self.Window(worker)
        t0 = time.monotonic()
        win.quit_all()
        return win, time.monotonic() - t0

    def test_waits_for_a_save_longer_than_the_timeout(self):
        win, took = self.close(_FakeWorker(finish=0.8))
        self.assertFalse(win.emu.is_alive())
        self.assertGreaterEqual(took, 0.7)
        self.assertEqual(win.emu.saved, 'resume.snap')
        self.assertEqual(win.exit_code(), 0)
        self.assertEqual(win.destroyed, 1)
        self.assertTrue(win.said and win.said[0].startswith('saving'))

    def test_waits_for_a_snapshot_still_loading(self):
        win, _took = self.close(_FakeWorker(load=0.6, finish=0.1))
        self.assertFalse(win.emu.is_alive())
        self.assertEqual(win.exit_code(), 0)

    def test_gives_up_on_a_stuck_worker(self):
        win, took = self.close(_FakeWorker(stuck=True))
        self.assertTrue(win.emu.is_alive())
        self.assertLess(took, 3.0)
        self.assertEqual(win.exit_code(), 1)
        self.assertEqual(win.destroyed, 1)

    def test_is_idempotent(self):
        win, _took = self.close(_FakeWorker(save=False))
        win.quit_all()
        self.assertEqual(win.destroyed, 2)
        self.assertEqual(win.said, [])          # nothing to save, no notice
        self.assertEqual(win.exit_code(), 0)

    def test_exit_codes(self):
        emu = types.SimpleNamespace(error=None, save_on_exit='r.snap',
                                    saved=None, is_alive=lambda: False)
        win = self.Window(emu)
        self.assertEqual(win.exit_code(), 2)            # save failed
        emu.saved = 'r.snap'
        self.assertEqual(win.exit_code(), 0)
        emu.error = 'halted: unhandled vector 4'
        self.assertEqual(win.exit_code(), 1)            # failure wins
        emu.error, emu.save_on_exit, emu.saved = None, None, None
        self.assertEqual(win.exit_code(), 0)
        # An Emulator from before `incompatible` existed reads as False.
        self.assertFalse(hasattr(emu, 'incompatible'))

    def test_an_incompatible_snapshot_has_its_own_code(self):
        emu = types.SimpleNamespace(
            error='incompatible snapshot: rebuild needed. gui.snap ...',
            incompatible=True, save_on_exit='r.snap', saved=None,
            is_alive=lambda: False)
        win = self.Window(emu)
        self.assertEqual(win.exit_code(), 4)     # not 1 (error), not 2
        emu.incompatible = False
        self.assertEqual(win.exit_code(), 1)

    def test_failure_text(self):
        emu = types.SimpleNamespace(error='DeviceError: no match',
                                    is_alive=lambda: True,
                                    stop_flag=threading.Event())
        win = self.Window(emu)
        self.assertEqual(win._failure(), 'DeviceError: no match')
        emu.error = None
        self.assertIsNone(win._failure())
        emu.is_alive = lambda: False                    # died, not stopped
        self.assertIn('ended unexpectedly', win._failure())
        emu.stop_flag.set()
        self.assertIsNone(win._failure())


class _Canvas:
    """Records what the panel draws, per canvas item."""

    def __init__(self):
        self.items = {}

    def itemconfigure(self, item, **kw):
        self.items.setdefault(item, {}).update(kw)

    def tag_raise(self, item):
        pass


@NEEDS_GUI
class ShowFailureTest(unittest.TestCase):
    """The window names the kind of failure. A stand-in canvas, no Tk."""

    def setUp(self):
        from emu import dtpanel
        self.dtpanel = dtpanel

    def show(self, error, status='failed to load', incompatible=False):
        emu = types.SimpleNamespace(error=error, stats={'status': status},
                                    incompatible=incompatible)
        win = types.SimpleNamespace(emu=emu, canvas=_Canvas(),
                                    _error_shown=None, err_text='err_text',
                                    err_box='err_box', status='status')
        self.dtpanel.DigitaktPanel._show_failure(win, error)
        items = win.canvas.items
        self.assertEqual(items['err_box']['state'], 'normal')
        return items['err_text']['text'], items['status']

    def test_an_incompatible_snapshot_is_not_shown_as_a_crash(self):
        text, status = self.show('incompatible snapshot: rebuild needed. '
                                 'gui.snap was saved by a different build',
                                 incompatible=True)
        self.assertTrue(text.startswith('This snapshot was made by a '
                                        'different build.'), text)
        self.assertTrue(status['text'].startswith(
            'incompatible snapshot: rebuild needed.'), status['text'])
        self.assertNotIn('STOPPED', status['text'])
        self.assertEqual(status['fill'], self.dtpanel.AMBER)

    def test_a_snapshot_that_will_not_open(self):
        text, status = self.show('RuntimeError: invalid checkpoint blob')
        self.assertTrue(text.startswith('The snapshot could not be opened.'))
        self.assertEqual(status['text'], 'EMULATOR STOPPED  RuntimeError: '
                                         'invalid checkpoint blob')
        self.assertEqual(status['fill'], self.dtpanel.ERR)

    def test_a_halt(self):
        text, status = self.show('halted: unhandled vector 4',
                                 status='halted: unhandled vector 4')
        self.assertTrue(text.startswith('The emulator stopped.'))
        self.assertEqual(status['fill'], self.dtpanel.ERR)


@NEEDS_GUI
class FirstLineTest(unittest.TestCase):
    def test_first_line(self):
        from emu.dtpanel import _first_line
        self.assertEqual(_first_line('\n  No device file matches x\n\n  sha'),
                         'No device file matches x')
        self.assertEqual(_first_line('a' * 200, limit=10), 'aaaaaaa...')


@NEEDS_GUI
class EncoderScaleTest(Quiet):
    """_drain_input sends each detent as [panel] encoder_counts wire counts,
    clamped to what one encoder message carries."""

    def drain(self, *events, counts=4):
        from emu import gui, panelin
        emu = types.SimpleNamespace(
            held=object(), _dwell_ms=0, inbox=deque(events), stats={'instrs': 0},
            device=types.SimpleNamespace(encoder_channel=lambda code: code - 1,
                                         encoder_counts=counts))
        sent = []
        with mock.patch.object(panelin, 'feed',
                               lambda m, prof, data: sent.append(data) or 0x1234):
            pc = gui.Emulator._drain_input(emu, None, None, 0x40)
        return pc, sent, panelin

    def test_a_detent_is_four_counts(self):
        pc, sent, panelin = self.drain(('encoder', 1, 3), ('encoder', 9, -1))
        self.assertEqual(pc, 0x1234)
        self.assertEqual(sent, [panelin.encode_encoder(0, 12)
                                + panelin.encode_encoder(8, -4)])

    def test_a_fast_spin_is_clamped(self):
        _pc, sent, panelin = self.drain(('encoder', 2, 40), ('encoder', 2, -40))
        self.assertEqual(sent, [panelin.encode_encoder(1, 127)
                                + panelin.encode_encoder(1, -127)])

    def test_one_count_per_detent_is_unchanged(self):
        _pc, sent, panelin = self.drain(('encoder', 1, 3), counts=1)
        self.assertEqual(sent, [panelin.encode_encoder(0, 3)])


@NEEDS_GUI
class PanelLayoutTest(unittest.TestCase):
    """Both windows draw every key their device file names, on the panel,
    clear of the screen, the Master Volume knob, the page LEDs and each
    other -- captions included, which is how YES's "Reload" once sat on NO.
    Each window's own SCREEN_X is used: the Digitone window inherits the
    Digitakt's, and a check of the module constant alone missed it."""

    def setUp(self):
        from emu import device, dnpanel, dtpanel, gui
        self.dn, self.dt, self.gui = dnpanel, dtpanel, gui
        devices = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'devices')
        self.windows = (
            (dtpanel.DigitaktPanel, device.load(os.path.join(devices, 'digitakt.toml'))),
            (dnpanel.DigitonePanel, device.load(os.path.join(devices, 'digitone.toml'))))

    def boxes(self, cls, dev):
        out = {'button ' + k: (x, y, x + w, y + h)
               for k, (x, y, w, h, _sub, _tint) in cls.BUTTONS.items()}
        # A caption is centred under its key at y + h + 11, 8-point text.
        out.update({'caption ' + k: (x + w / 2 - 3.2 * len(sub), y + h + 5,
                                     x + w / 2 + 3.2 * len(sub), y + h + 17)
                    for k, (x, y, w, h, sub, _tint) in cls.BUTTONS.items() if sub})
        out.update({'encoder ' + k: (x - r, y - r, x + r, y + r)
                    for k, (x, y, r) in cls.ENCODERS.items()})
        x, y, r = self.dt.MASTER_VOLUME
        out['master volume'] = (x - r, y - r, x + r, y + r + 17)   # + its label
        px, py = cls.BUTTONS['PAGE'][:2]
        for i, _led in enumerate(dev.page_leds):
            cx, cy = px + 13 + i * 22, py - 12
            out['page led %d' % i] = (cx - 5, cy - 5, cx + 5, cy + 5)
        sx, sy = cls.SCREEN_X, cls.SCREEN_Y
        out['screen'] = (sx, sy, sx + self.gui.W * self.dt.SCALE,
                         sy + self.gui.H * self.dt.SCALE)
        return out

    def test_every_measured_key_has_a_place(self):
        for cls, dev in self.windows:
            missing = set(dev.labels.values()) - set(cls.BUTTONS)
            self.assertEqual(missing, set(), cls.PRODUCT)
            self.assertIn('PAGE', cls.BUTTONS)      # the page LEDs hang off it

    def test_nine_encoders_including_level_data(self):
        for cls, dev in self.windows:
            self.assertEqual(len(cls.ENCODERS), dev.encoders, cls.PRODUCT)
            self.assertEqual(sorted(cls.ENCODERS),
                             sorted('ABCDEFGH') + ['LEVEL/DATA'])

    def test_nothing_overlaps_or_leaves_the_panel(self):
        for cls, dev in self.windows:
            boxes = sorted(self.boxes(cls, dev).items())
            for name, (x0, y0, x1, y1) in boxes:
                self.assertTrue(0 <= x0 < x1 <= cls.PANEL_W
                                and 70 <= y0 < y1 <= cls.PANEL_H,
                                '%s: %s' % (cls.PRODUCT, name))
            for i, (a, ba) in enumerate(boxes):
                for b, bb in boxes[i + 1:]:
                    apart = (ba[2] <= bb[0] or bb[2] <= ba[0]
                             or ba[3] <= bb[1] or bb[3] <= ba[1])
                    self.assertTrue(apart, '%s: %s overlaps %s'
                                    % (cls.PRODUCT, a, b))

    def test_it_is_its_own_product_with_no_sample_loader(self):
        cls = self.dn.DigitonePanel
        self.assertTrue(issubclass(cls, self.dt.DigitaktPanel))
        self.assertEqual(cls.PRODUCT, 'Digitone')
        self.assertIn('Digitone', cls.TITLE)
        self.assertFalse(cls.SAMPLES)
        self.assertTrue(self.dt.DigitaktPanel.SAMPLES)


@NEEDS_GUI
class ScreenFrameTest(unittest.TestCase):
    """The window's screen buffer holds nothing but whole firmware frames.

    It once showed leftovers of earlier screens after a page change or a knob
    turn, two ways: the OS's setPixel calls (for bitmaps that are not the
    screen) were drawn over the frame, and each new frame cleared and
    refilled the buffer in place while the window thread copied it. Measured
    on a Digitakt flipping pages: 194 of 419 window reads were no frame the
    firmware drew; 0 after the fix."""

    def setUp(self):
        from emu import gui, panel
        self.gui, self.panel = gui, panel
        self.W, self.H = gui.W, gui.H

    def emulator(self, use_panel):
        return types.SimpleNamespace(
            use_panel=use_panel, fb=bytearray(self.W * self.H), _seen=set(),
            stats={'px': 0, 'frames': 0}, captured=[], version=0, _frame_t=0.0,
            _panel_latch=None, _last_panel=None, _panel_live=False)

    def test_the_os_setpixel_never_reaches_the_screen(self):
        emu = self.emulator(use_panel=True)
        self.gui.Emulator._on_pixel(emu, 3, 4, 1, 0x1234)
        self.assertEqual(emu.fb, bytearray(self.W * self.H))
        self.assertEqual(emu.version, 0)
        intro = self.emulator(use_panel=False)                 # the intro does
        self.gui.Emulator._on_pixel(intro, 3, 4, 1, 0x1234)
        self.assertEqual(intro.fb[4 * self.W + 3], 1)

    def test_a_new_frame_replaces_the_buffer_whole(self):
        emu = self.emulator(use_panel=True)
        old = emu.fb
        old[:] = b'\x01' * len(old)                   # a frame being copied
        buf = bytearray(self.panel.SIZE)
        buf[0] = 0x81                                 # two lit pixels
        emu._panel_latch = bytes(buf)
        self.gui.Emulator._publish_panel(emu, None)
        self.assertIsNot(emu.fb, old)
        self.assertEqual(old, b'\x01' * len(old))     # never touched in place
        lit = {(i % self.W, i // self.W) for i, v in enumerate(emu.fb) if v}
        self.assertEqual(lit, self.panel.lit(bytes(buf)))
        self.assertEqual(emu.captured, [bytes(emu.fb)])


@NEEDS_GUI
class MasterVolumeTest(unittest.TestCase):
    """The knob's indicator spans its whole range, so a gain past unity never
    points back towards silent (it once wrapped: 1.5 read as 10 o'clock)."""

    def test_the_sweep_runs_7_to_5_oclock_over_the_whole_range(self):
        import math
        from emu.dtpanel import DigitaktPanel, master_volume_angle
        top = DigitaktPanel._MV_MAX

        def oclock(v):
            return (math.degrees(master_volume_angle(v, top)) % 360) / 30 or 12
        self.assertAlmostEqual(oclock(0.0), 7.0)
        self.assertAlmostEqual(oclock(top), 5.0)
        angles = [master_volume_angle(v / 20, top) for v in range(0, 31)]
        self.assertEqual(angles, sorted(angles))                 # always louder
        self.assertLess(angles[-1] - angles[0], math.radians(301))
        self.assertEqual(master_volume_angle(9.0, top), master_volume_angle(top, top))
        self.assertEqual(master_volume_angle(-1.0, top), master_volume_angle(0.0, top))

    def test_every_window_draws_the_knob_and_it_reaches_both_outputs(self):
        # The Digitone window once had no knob: it was drawn after the early
        # return that skips LOAD SAMPLES.
        from emu import dnpanel, dtpanel

        class Canvas:
            def __init__(self):
                self.drawn, self.binds, self.n = [], {}, 0

            def _new(self, kind, **kw):
                self.n += 1
                self.drawn.append((kind, kw))
                return self.n
            create_text = lambda self, *a, **kw: self._new('text', **kw)
            create_oval = lambda self, *a, **kw: self._new('oval', **kw)
            create_line = lambda self, *a, **kw: self._new('line', **kw)

            def tag_bind(self, item, seq, fn):
                self.binds.setdefault(item, []).append(seq)

            def coords(self, item, *xy):
                pass

        for cls in (dtpanel.DigitaktPanel, dnpanel.DigitonePanel):
            win = types.SimpleNamespace(
                canvas=Canvas(), SAMPLES=cls.SAMPLES, _MV_MAX=cls._MV_MAX,
                _MV_STEP=cls._MV_STEP, _rr=lambda *a, **kw: 0,
                emu=types.SimpleNamespace(set_volume=lambda v: gains.append(v)),
                player=types.SimpleNamespace(gain=1.0))
            for name in ('audio_toggle_mute', 'audio_play', 'audio_clear',
                         'audio_save', 'load_samples'):
                setattr(win, name, lambda: None)
            for name in ('_draw_master_volume', '_paint_master_volume'):
                setattr(win, name, getattr(cls, name).__get__(win))
            gains = []
            cls._draw_audio_controls(win)
            texts = [kw.get('text') for kind, kw in win.canvas.drawn if kind == 'text']
            self.assertIn('Master Volume', texts, cls.PRODUCT)
            self.assertIn('<MouseWheel>', win.canvas.binds[win._mv_oval], cls.PRODUCT)
            self.assertEqual(win._mv_value, 1.0)
            cls._turn_master_volume(win, -2)
            self.assertAlmostEqual(win._mv_value, 0.9)
            self.assertEqual(gains, [win._mv_value])              # live output
            self.assertEqual(win.player.gain, win._mv_value)      # PLAY's replay


class GlobEscapeTest(unittest.TestCase):
    """A '[' in the app's folder must not hide the sections."""

    def test_brackets_in_the_sections_path(self):
        from emu import config
        with tempfile.TemporaryDirectory() as d:
            sections = os.path.join(d, 'digikit [v0.1]', 'sections')
            os.makedirs(sections)
            main = _write(os.path.join(sections, 'section_3_MAIN_OS.bin'))
            boot = _write(os.path.join(sections, 'section_2_BOOT.bin'))
            with mock.patch.dict(os.environ, {'DT2_SECTIONS': sections}):
                os.environ.pop('DT2_MAIN_IMG', None)
                self.assertEqual(config.main_image(), main)
                self.assertEqual(config.bootstrap(), boot)

    def test_brackets_in_the_working_directory(self):
        from emu import config
        with tempfile.TemporaryDirectory() as d:
            cwd = os.path.join(d, '[old]')
            os.makedirs(cwd)
            _write(os.path.join(cwd, 'fw.syx'))
            old = os.getcwd()
            os.chdir(cwd)
            try:
                with mock.patch.dict(os.environ):
                    os.environ.pop('DT2_SYX', None)
                    self.assertEqual(config.firmware(), 'fw.syx')
            finally:
                os.chdir(old)


if __name__ == '__main__':
    unittest.main()
