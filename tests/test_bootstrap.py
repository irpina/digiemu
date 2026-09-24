"""emu/bootstrap.py without firmware: paths, state, stamps, the stage chain.

The emulator stages themselves need a real .syx (~25 s end to end), so they are
exercised end to end by hand (see the module docstring); what is checked here
is everything that decides WHAT runs and what the app opens afterwards:
the folder layout and the environment it exports, the atomic state file (and
its retries on Windows sharing violations), the card stamp, choose_snapshot's
rules and that it never loads Unicorn, the streaming frame counter that
replaced panel.Capture's 4096-frame cap, the card stage (sparse on NTFS), the
acceptance checks on synthetic cards and snapshots, the half-install guard,
first_run's stage chain with the emulator stages stubbed, and the two CLI
wrappers' flags.
"""
import importlib.util
import json
import os
import pickle
import random
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zlib
from pathlib import Path
from unittest import mock

from emu import bootstrap, ekfsformat, sparse
from emu.bootstrap import (Event, FirmwarePaths, FrameCounter, StepFailed,
                           card_stamp, choose_snapshot, read_state,
                           write_state)

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / 'tools'
MB = 1 << 20


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(path):
    with open(path, 'rb') as fh:
        return fh.read()


def _touch(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data)


class Folder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.devices = os.path.join(self.tmp, 'devices-bundled')
        os.makedirs(self.devices)
        self.paths = FirmwarePaths(os.path.join(self.tmp, 'fw', 'dt1-1.53'),
                                   'Digitakt_OS1.53.syx', self.devices)


class PathsTest(Folder):
    def test_layout(self):
        p = self.paths
        root = p.root
        self.assertTrue(os.path.isabs(root))
        self.assertEqual(p.syx, os.path.join(root, 'Digitakt_OS1.53.syx'))
        self.assertEqual(p.main_img, os.path.join(
            root, 'sections', 'section_3_MAIN_OS.bin'))
        self.assertEqual(p.snapdir, os.path.join(
            root, 'snapshots', 'Digitakt_OS1.53'))
        self.assertEqual(p.prefix, os.path.join(p.snapdir, 'boot'))
        self.assertEqual(p.rung(400_000_000),
                         os.path.join(p.snapdir, 'boot400M.snap'))
        self.assertEqual(p.card, os.path.join(root, 'plusdrive.img'))
        self.assertEqual(p.gui_raw, os.path.join(p.snapdir, 'gui-raw.snap'))
        self.assertEqual(p.gui, os.path.join(p.snapdir, 'gui.snap'))
        self.assertEqual(p.resume, os.path.join(p.snapdir, 'resume.snap'))
        self.assertEqual(p.overlay, os.path.join(root, 'devices'))
        self.assertEqual(p.logs, os.path.join(root, 'logs'))
        self.assertEqual(p.state, os.path.join(root, 'firmware.json'))

    def test_relative_root_becomes_absolute(self):
        p = FirmwarePaths('rel/fw', 'X.syx', 'devices')
        self.assertTrue(os.path.isabs(p.root))
        self.assertTrue(os.path.isabs(p.devices_dir))
        self.assertTrue(all(os.path.isabs(v) for v in p.env().values()))

    def test_env_uses_the_overlay_only_when_it_exists(self):
        env = self.paths.env()
        self.assertEqual(set(env), {'DT2_SYX', 'DT2_SECTIONS', 'DT2_SNAPSHOTS',
                                    'DT2_PLUSDRIVE', 'DT2_MAIN_IMG',
                                    'DT2_DEVICES'})
        self.assertEqual(env['DT2_DEVICES'], self.devices)
        self.assertEqual(env['DT2_PLUSDRIVE'], self.paths.card)
        self.assertEqual(env['DT2_MAIN_IMG'], self.paths.main_img)
        os.makedirs(self.paths.overlay)
        self.assertEqual(self.paths.env()['DT2_DEVICES'], self.paths.overlay)

    def test_first_run_refuses_a_wrong_environment(self):
        saved = {k: os.environ.get(k) for k in self.paths.env()}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None
                                 else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        for k in saved:
            os.environ.pop(k, None)
        with self.assertRaises(ValueError) as cm:
            bootstrap.first_run(self.paths)
        self.assertIn('DT2_PLUSDRIVE', str(cm.exception))
        self.assertFalse(os.path.exists(self.paths.root))


class StateTest(Folder):
    def test_missing_and_corrupt_read_empty(self):
        self.assertEqual(read_state(self.paths), {})
        _touch(self.paths.state, b'{not json')
        self.assertEqual(read_state(self.paths), {})
        _touch(self.paths.state, b'[1, 2]')
        self.assertEqual(read_state(self.paths), {})

    def test_round_trip_is_atomic_and_lf(self):
        state = {'release': {'label': 'Digitakt é 1.53'}, 'n': 1}
        write_state(self.paths, state)
        self.assertEqual(read_state(self.paths), state)
        raw = _read(self.paths.state)
        self.assertNotIn(b'\r', raw)
        raw.decode('ascii')              # non-ASCII is escaped, not encoded
        self.assertFalse(os.path.exists(self.paths.state + '.tmp'))

    def test_update_keeps_other_keys(self):
        write_state(self.paths, {'release': 1, 'app_version': 'x'})
        bootstrap._update_state(self.paths,
                                lambda s: s.setdefault('stages', {}))
        self.assertEqual(read_state(self.paths),
                         {'release': 1, 'app_version': 'x', 'stages': {}})


class CardStampTest(Folder):
    def test_stamp(self):
        self.assertIsNone(card_stamp(self.paths.card))
        _touch(self.paths.card, bytes(1024))
        stamp = card_stamp(self.paths.card)
        self.assertEqual(stamp['size'], 1024)
        self.assertIsInstance(stamp['mtime_ns'], int)
        self.assertEqual(json.loads(json.dumps(stamp)), stamp)


class ChooseSnapshotTest(Folder):
    """resume.snap, then gui.snap, then refuse -- never a stale cache."""

    def _built(self, card=b'card'):
        p = self.paths
        _touch(p.syx, b'syx bytes')
        _touch(p.card, card)
        _touch(p.gui)
        stamp = card_stamp(p.card)
        write_state(p, {'card': stamp, 'stages': {'settle': {
            'inputs': {'syx_sha256': bootstrap._sha256(p.syx)},
            'build': bootstrap._build_id(), 'card_after': stamp}}})
        return stamp

    def test_nothing_built(self):
        self.assertEqual(choose_snapshot(self.paths), (None, 'not-built'))

    def test_gui_when_the_card_is_unchanged(self):
        self._built()
        self.assertEqual(choose_snapshot(self.paths),
                         (self.paths.gui, 'gui'))

    def test_card_changed_since_the_settle(self):
        self._built()
        _touch(self.paths.card, b'card written by a session')
        self.assertEqual(choose_snapshot(self.paths), (None, 'card-changed'))

    def test_card_deleted(self):
        self._built()
        os.remove(self.paths.card)
        self.assertEqual(choose_snapshot(self.paths), (None, 'card-changed'))

    def test_resume_when_its_stamp_matches(self):
        self._built()
        _touch(self.paths.card, b'card written by a session')
        _touch(self.paths.resume)
        now = card_stamp(self.paths.card)
        for form in (now, {'card': now, 'saved': 'whenever'}):
            bootstrap._update_state(self.paths,
                                    lambda s: s.__setitem__('resume', form))
            self.assertEqual(choose_snapshot(self.paths),
                             (self.paths.resume, 'resume'))

    def test_stale_resume_falls_back_to_gui(self):
        stamp = self._built()
        _touch(self.paths.resume)
        bootstrap._update_state(self.paths, lambda s: s.__setitem__(
            'resume', {'card': {'size': 1, 'mtime_ns': 2}}))
        self.assertEqual(card_stamp(self.paths.card), stamp)
        self.assertEqual(choose_snapshot(self.paths),
                         (self.paths.gui, 'gui'))

    def test_a_settle_for_another_firmware_or_recipe_is_not_built(self):
        self._built()
        _touch(self.paths.syx, b'a different syx')
        self.assertEqual(choose_snapshot(self.paths), (None, 'not-built'))
        self._built()
        bootstrap._update_state(self.paths, lambda s: s['stages']['settle']
                                .__setitem__('build', {'recipe': -1}))
        self.assertEqual(choose_snapshot(self.paths), (None, 'not-built'))

    def test_missing_gui_is_not_built(self):
        self._built()
        os.remove(self.paths.gui)
        self.assertEqual(choose_snapshot(self.paths), (None, 'not-built'))


class FrameCounterTest(unittest.TestCase):
    def test_lit_matches_the_panel_layout(self):
        from emu import panel
        rng = random.Random(7)
        for _ in range(5):
            buf = bytes(rng.randrange(256) for _ in range(panel.SIZE))
            self.assertEqual(FrameCounter.lit(buf), len(panel.lit(buf)))

    def test_counts_past_panel_capture_limit(self):
        # First boot on a fresh card takes ~4100 frames. panel.Capture stored
        # 4096 and the quiet run froze there; this must keep counting.
        count = FrameCounter(min_lit=1200)
        ui = b'\xff' * 200 + bytes(824)          # 1600 lit
        overlay = b'\xff' * 50 + bytes(974)      # 400 lit
        for i in range(4090):
            count.add(overlay if i % 3 == 0 else ui)
        self.assertLess(count.run, 120)
        for _ in range(150):
            count.add(ui)
        self.assertEqual(count.frames, 4240)
        self.assertEqual(count.overlays, 1364)
        self.assertEqual(count.run, 150)
        self.assertEqual(count.last, ui)
        self.assertEqual(count.last_lit, 1600)
        count.add(overlay)
        self.assertEqual(count.run, 0)

    def test_the_threshold_itself_is_an_overlay(self):
        count = FrameCounter(min_lit=8)
        count.add(b'\xff' + bytes(1023))
        self.assertEqual((count.overlays, count.run), (1, 0))


class TaskMapTest(unittest.TestCase):
    def test_list_of_triples_becomes_a_tcb_map(self):
        # uisettle crashed here: dict() of a list of 3-tuples raises.
        ev = {'tasks': [(0x40001000, 5, 0x41000000)]}
        carried = {0x41000100: {'entry': 1, 'prio': 2, 'tcb': 0x41000100}}
        tasks = bootstrap._task_map(ev, carried)
        self.assertEqual(tasks['0x41000000'],
                         {'entry': 0x40001000, 'prio': 5, 'tcb': 0x41000000})
        self.assertIn('0x41000100', tasks)
        self.assertEqual(bootstrap._task_map({}), {})


class CardStageTest(Folder):
    """The card stage formats with the same code as ekfsadd --format.

    Region base 0 keeps the image at ~10 MB; the real card puts the region
    at sector 0x1C0000 and so is ~950 MB.
    """

    def test_formats_a_missing_card_once(self):
        card = self.paths.card
        os.makedirs(os.path.dirname(card))
        self.assertTrue(bootstrap.prepare_card(card, base=0))
        self.assertTrue(ekfsformat.is_formatted(card, 0))
        self.assertFalse(os.path.exists(card + '.tmp'))
        before = _read(card)
        self.assertFalse(bootstrap.prepare_card(card, base=0))
        self.assertEqual(_read(card), before)

    def test_matches_ekfsadd_format(self):
        card = self.paths.card
        os.makedirs(os.path.dirname(card))
        bootstrap.prepare_card(card, base=0)
        ref = os.path.join(self.tmp, 'ref.img')
        ekfsformat.format_image(ref, base=0)
        self.assertEqual(_read(card), _read(ref))

    def test_replaces_a_blank_card_but_keeps_a_formatted_one(self):
        card = self.paths.card
        _touch(card, bytes(512))              # what a bare emulator run makes
        self.assertFalse(ekfsformat.is_formatted(card, 0))
        self.assertTrue(bootstrap.prepare_card(card, base=0))
        fs = ekfsformat.Ekfs(card, base=0, write=True)
        target = ekfsformat.find_dir(fs, 'incoming')
        fs.add_file(target, 'keep.bin', b'user data')
        fs.close()
        after = _read(card)
        self.assertFalse(bootstrap.prepare_card(card, base=0))
        self.assertEqual(_read(card), after)

    def test_a_torn_superblock_is_not_formatted(self):
        card = self.paths.card
        os.makedirs(os.path.dirname(card))
        ekfsformat.format_image(card, base=0)
        with open(card, 'r+b') as fh:
            fh.seek(0x10)
            fh.write(b'\x99')
        self.assertFalse(ekfsformat.is_formatted(card, 0))

    def test_a_blank_card_is_one_zero_sector(self):
        # [card] ekfs = false: the firmware's own first boot lays the card
        # out, so the stage leaves it nothing but a sector of zeros.
        card = self.paths.card
        os.makedirs(os.path.dirname(card))
        self.assertTrue(bootstrap.blank_card(card))
        self.assertEqual(_read(card), bytes(512))
        self.assertFalse(os.path.exists(card + '.tmp'))


class SectionsTest(Folder):
    def test_current_needs_the_marker_and_one_main_image(self):
        p = self.paths
        self.assertFalse(bootstrap.sections_current(p, 'ab'))
        _touch(os.path.join(p.sections, '.source-sha256'), b'ab\n')
        self.assertFalse(bootstrap.sections_current(p, 'ab'))
        _touch(p.main_img)
        self.assertTrue(bootstrap.sections_current(p, 'ab'))
        self.assertFalse(bootstrap.sections_current(p, 'cd'))
        _touch(os.path.join(p.sections, 'section_9_MAIN_OS.bin'))
        self.assertFalse(bootstrap.sections_current(p, 'ab'))

    def test_brackets_in_the_folder_name(self):
        # config.main_image's unescaped glob found nothing under '[x]'.
        p = FirmwarePaths(os.path.join(self.tmp, 'digikit [v0.1]', 'fw'),
                          'X.syx', self.devices)
        _touch(os.path.join(p.sections, '.source-sha256'), b'ab\n')
        _touch(p.main_img)
        self.assertTrue(bootstrap.sections_current(p, 'ab'))


class StageStampTest(Folder):
    def test_stage_ok_needs_inputs_build_and_outputs(self):
        out = os.path.join(self.tmp, 'out.snap')
        inputs = bootstrap._canon({'points': (1, 2), 'recipe': 1})
        stages = {'ladder': {'inputs': inputs,
                             'build': bootstrap._build_id()}}
        self.assertFalse(bootstrap._stage_ok(stages, 'ladder', inputs, [out]))
        _touch(out)
        self.assertTrue(bootstrap._stage_ok(stages, 'ladder', inputs, [out]))
        self.assertTrue(bootstrap._stage_ok(
            json.loads(json.dumps(stages)), 'ladder', inputs, [out]))
        self.assertFalse(bootstrap._stage_ok(
            stages, 'ladder', dict(inputs, recipe=2), [out]))
        self.assertFalse(bootstrap._stage_ok({}, 'ladder', inputs, [out]))


class EventTest(unittest.TestCase):
    def test_defaults_and_json(self):
        e = Event('ladder', 'tick')
        self.assertEqual((e.done, e.total, e.text, e.data), (0, 0, '', {}))
        self.assertIsNot(e.data, Event('ladder', 'tick').data)
        self.assertIn(e.step, bootstrap.STEPS)
        self.assertIn(e.kind, bootstrap.KINDS)

    def test_overall_weights(self):
        seen = []
        overall = bootstrap._Overall(seen.append)
        overall.enter('settle')
        overall(Event('settle', 'tick', data={'fraction': 0.5}))
        overall(Event('settle', 'note', text='a printed line'))
        overall(Event('settle', 'done'))
        # Everything before the settle, then half the settle's weight.
        half = 1.0 - bootstrap.WEIGHTS['settle'] / 2
        self.assertAlmostEqual(seen[0].data['overall'], half)
        self.assertAlmostEqual(seen[1].data['overall'], half)
        self.assertAlmostEqual(seen[2].data['overall'], 1.0)
        self.assertAlmostEqual(sum(bootstrap.WEIGHTS.values()), 1.0)

    def test_step_failed(self):
        exc = StepFailed('ladder', 'stopped early')
        self.assertEqual((exc.step, exc.reason), ('ladder', 'stopped early'))
        self.assertIn('ladder', str(exc))
        self.assertIsInstance(exc, RuntimeError)


class _FakeUc:
    def reg_read(self, _reg):
        return 0x40001234


def _fake_run(stop_after=None, stop='instruction limit'):
    """Stands in for dspboot.run: counts instructions through extra_hook."""
    def run(syx, img, limit, extra_hook, fast, verbose, machine_out, sdgate,
            esdhc):
        st = {'n': 0, 'seen': {1, 2}, 'task_create_hits': {0x40: {'x': 1}}}
        machine_out['m'] = object()
        machine_out['st'] = st
        uc = _FakeUc()
        for _ in range(limit if stop_after is None else stop_after):
            st['n'] += 1
            extra_hook(uc, 0, 2, st)
        return machine_out['m'], st, ('instruction limit'
                                      if stop_after is None else stop)
    return run


def _fake_save(machine, path, extra=None, components=None, manifest=None):
    with open(path, 'wb') as fh:
        fh.write(b'snap')
    return {'bytes_on_disk': 4}


class CheckpointMakeTest(Folder):
    """emu.checkpoint.make's ladder bookkeeping, with the cold boot faked.

    The fake counts to points[-1] + 1M through make's own per-instruction
    hook, so the real hook (rungs, reports, atomic writes, the sidecar and
    the stop reason) runs unchanged.
    """

    POINTS = [100, 200, 300]

    def setUp(self):
        super().setUp()
        from unittest import mock
        from emu import checkpoint
        self.ck = checkpoint
        self.syx = os.path.join(self.tmp, 'fw.syx')
        self.img = os.path.join(self.tmp, 'main.bin')
        _touch(self.syx)
        _touch(self.img, b'image')
        self.prefix = os.path.join(self.tmp, 'not', 'yet', 'there', 'boot')
        patcher = mock.patch.object(checkpoint, 'save', _fake_save)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mock = mock

    def _make(self, run, **kw):
        with self.mock.patch.object(self.ck.db, 'run', run):
            return self.ck.make(self.POINTS, self.prefix, self.syx, self.img,
                                **kw)

    def test_full_ladder_with_progress(self):
        seen, out = [], {}
        saved = self._make(_fake_run(), progress=lambda *a: seen.append(a),
                           report_every=250_000, out=out)
        self.assertEqual([s[0] for s in saved], self.POINTS)
        self.assertEqual(out['stop'], self.ck.LIMIT_STOP)
        rungs = [r for _n, _t, r in seen if r is not None]
        self.assertEqual([r['at'] for r in rungs], self.POINTS)
        self.assertTrue(all(r['bytes'] == 4 and '[0M]' in r['text']
                            for r in rungs))
        reports = [n for n, _t, r in seen if r is None]
        self.assertEqual(reports, [250_000, 500_000, 750_000, 1_000_000])
        self.assertTrue(all(t == 1_000_300 for _n, t, _r in seen))
        d = os.path.dirname(self.prefix)
        self.assertEqual(sorted(os.listdir(d)),
                         ['.ladder.json', 'boot0M.snap'])
        with open(os.path.join(d, '.ladder.json')) as fh:
            self.assertEqual(json.load(fh)['points'], self.POINTS)

    def test_early_stop_is_exposed_and_writes_no_sidecar(self):
        out = {}
        saved = self._make(_fake_run(stop_after=250, stop='UC_ERR_FETCH'),
                           progress=lambda *a: None, out=out)
        self.assertEqual(len(saved), 2)
        self.assertEqual(out['stop'], 'UC_ERR_FETCH')
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(self.prefix), '.ladder.json')))

    def test_without_progress_it_prints_as_before(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._make(_fake_run(stop_after=150))
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('  [0M] '))
        self.assertIn('2 addrs, 1 tasks, pc=0x40001234, 4 B', lines[0])
        self.assertIn('ladder incomplete: 1 of 3 rungs saved', lines[1])

    def test_a_raising_progress_stops_the_ladder(self):
        def cancel(n, total, rung):
            if n >= 150:
                raise bootstrap.Cancelled()
        with self.assertRaises(bootstrap.Cancelled):
            self._make(_fake_run(), progress=cancel, report_every=50)


class _ColdCard:
    def __init__(self):
        self.overlay = {0x1000: b'inode chunk'}
        self.path = 'plusdrive.img'
        self.flushed = self.closed = 0

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed += 1


class _ColdUc:
    """Runs one scripted chunk per emu_start: (idle spins, tasks created)."""

    def __init__(self, st, script, fail_at=None):
        self.st, self.script, self.fail_at = st, list(script), fail_at
        self.calls = []

    def emu_start(self, begin, until, count=0):
        from unicorn import UcError, UC_ERR_FETCH_UNMAPPED
        self.calls.append((begin, count))
        if self.fail_at == len(self.calls):
            raise UcError(UC_ERR_FETCH_UNMAPPED)
        spins, tasks = self.script.pop(0) if self.script else (0, 1)
        self.st['spin'] += spins
        for _ in range(tasks):
            self.st['task_create_hits'][0x40000000
                                        + len(self.st['task_create_hits'])] = 1

    def reg_read(self, reg):
        return 0x4006932E


class ColdBootTest(Folder):
    """cold_boot's chunk loop and park rule, with the emulator faked.

    Counted chunks of 1000 (native budget off), so a chunk is idle when it
    made more than 250 idle-spin passes.
    """

    def setUp(self):
        super().setUp()
        _touch(self.paths.main_img, b'main os')
        self.card = _ColdCard()
        self.saved = []
        self.m = None
        for target, name, fn in (
                ('emu.dspboot', 'run', self._run),
                ('emu.native', 'budget_available', lambda uc: False),
                ('emu.snapshot', 'save', self._save)):
            patcher = mock.patch(target + '.' + name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, syx, img, limit=None, machine_out=None, coverage=True,
             **kw):
        self.assertIsNone(limit)
        self.assertFalse(coverage)
        st = {'spin': 0, 'task_create_hits': {}}
        self.m = mock.Mock(uc=_ColdUc(st, self.script, self.fail_at),
                           halt_vec=None, esdhc=mock.Mock(card=self.card))
        machine_out.update(m=self.m, start_pc=0x40000400)
        return self.m, st, 'not started'

    def _save(self, m, path, extra=None, **kw):
        self.saved.append((path, extra))
        _touch(path, b'snap')

    def boot(self, script, fail_at=None, **kw):
        self.script, self.fail_at = script, fail_at
        events = []
        kw.setdefault('chunk', 1000)
        kw.setdefault('limit', 10_000)
        res = bootstrap.cold_boot(self.paths, progress=events.append, **kw)
        return res, events

    def test_parks_at_the_first_idle_chunk_without_a_new_task(self):
        old = self.paths.rung(400_000_000)
        _touch(old, b'old ladder')
        # Busy, busy, idle but a task started, then idle and quiet.
        res, events = self.boot([(0, 3), (10, 2), (900, 1), (900, 0)])
        self.assertEqual((res['stop'], res['parked_at'], res['tasks']),
                         ('parked', 4000, 6))
        self.assertEqual((res['card_writes'], res['native']), (1, False))
        self.assertEqual(len(self.m.uc.calls), 4)
        self.assertEqual(self.m.uc.calls[0], (0x40000400, 1000))
        self.assertEqual([p for p, _e in self.saved],
                         [self.paths.cold + '.tmp'])
        self.assertEqual(self.saved[0][1]['n'], 4000)
        self.assertTrue(os.path.exists(self.paths.cold))
        self.assertFalse(os.path.exists(self.paths.cold + '.tmp'))
        self.assertFalse(os.path.exists(old))
        self.assertEqual((self.card.flushed, self.card.closed), (1, 1))
        ticks = [e for e in events if e.kind == 'tick']
        self.assertEqual([e.done for e in ticks], [1000, 2000, 3000, 4000])
        self.assertTrue(all(e.data['fraction'] < 1 for e in ticks))
        self.assertIn('parked at', events[-1].text)

    def test_a_quarter_of_the_chunk_is_not_enough(self):
        res, _ev = self.boot([(250, 0), (251, 0)])
        self.assertEqual(res['parked_at'], 2000)

    def test_never_parking_fails_and_leaves_the_card(self):
        with self.assertRaises(StepFailed) as cm:
            self.boot([], limit=5000)
        self.assertEqual(cm.exception.step, 'ladder')
        self.assertIn('never settled', cm.exception.reason)
        self.assertEqual(len(self.m.uc.calls), 5)
        self.assertEqual((self.card.flushed, self.card.closed), (0, 1))
        self.assertFalse(os.path.exists(self.paths.cold))

    def test_an_emulator_error_fails(self):
        with self.assertRaises(StepFailed) as cm:
            self.boot([(0, 1)] * 5, fail_at=3, chunk=1_000_000,
                      limit=10_000_000)
        self.assertIn('stopped after 2M instructions', cm.exception.reason)
        self.assertEqual((self.card.flushed, self.card.closed), (0, 1))
        self.assertEqual(self.saved, [])

    def test_an_unhandled_vector_fails(self):
        def run(*a, **kw):
            m, st, why = self._run(*a, **kw)
            real = m.uc.emu_start

            def emu_start(*sa, **skw):
                real(*sa, **skw)
                m.halt_vec = 61
            m.uc.emu_start = emu_start
            return m, st, why
        self.script, self.fail_at = [], None
        with mock.patch('emu.dspboot.run', run):
            with self.assertRaises(StepFailed) as cm:
                bootstrap.cold_boot(self.paths, chunk=1000, limit=10_000)
        self.assertIn('unhandled vector 61', cm.exception.reason)
        self.assertEqual(self.card.flushed, 0)

    def test_cancel_stops_between_chunks_and_flushes_nothing(self):
        asked = []

        def cancel():
            asked.append(1)
            return len(asked) > 2
        self.script, self.fail_at = [(0, 1)] * 5, None
        with self.assertRaises(bootstrap.Cancelled):
            bootstrap.cold_boot(self.paths, cancel=cancel, chunk=1000,
                                limit=10_000)
        self.assertEqual(len(self.m.uc.calls), 2)
        self.assertEqual((self.card.flushed, self.card.closed), (0, 1))
        self.assertFalse(os.path.exists(self.paths.cold))


class CliTest(unittest.TestCase):
    """The tools keep their flags and defaults; the body is bootstrap's."""

    def test_introboot_flags(self):
        ap = _load_tool('introboot').parser()
        a = ap.parse_args(['--syx', 'Digitakt_OS1.53.syx', '--snapshot',
                           'snapshots/Digitakt_OS1.53/boot400M.snap',
                           '--min-lit', '1200', '--out', 'gui-raw.snap'])
        self.assertEqual((a.budget, a.chunk, a.png, a.min_lit, a.no_except),
                         (600_000_000, 20_000_000, 'out/introboot.png', 1200,
                          False))
        d = ap.parse_args(['--syx', 's', '--snapshot', 'r'])
        self.assertEqual((d.min_lit, d.out), (500, ''))
        self.assertEqual(ap.parse_args(['--syx', 's', '--snapshot', 'r',
                                        '--budget', '0x10']).budget, 16)

    def test_uisettle_flags(self):
        ap = _load_tool('uisettle').parser()
        a = ap.parse_args(['--syx', 'Digitakt_OS1.53.syx', '--snapshot',
                           'gui-raw.snap', '--out', 'gui.snap'])
        self.assertEqual((a.min_lit, a.quiet, a.budget, a.chunk, a.png),
                         (1200, 120, 3_000_000_000, 50_000_000,
                          'out/uisettle.png'))
        self.assertEqual(ap.parse_args(['--syx', 's']).snapshot,
                         'snapshots/Digitakt_OS1.53/gui.snap')

    def test_ekfsadd_is_the_library(self):
        tool = _load_tool('ekfsadd')
        self.assertIs(tool.format_image, ekfsformat.format_image)
        self.assertIs(tool.Ekfs, ekfsformat.Ekfs)
        self.assertIs(tool.wav_to_sample, ekfsformat.wav_to_sample)


class ReplaceRetryTest(Folder):
    """Windows refuses os.replace while another process has the target open."""

    def test_retries_a_sharing_violation_then_succeeds(self):
        real = os.replace
        seen = []

        def flaky(src, dst):
            seen.append(dst)
            if len(seen) < 3:
                raise PermissionError(13, 'Access is denied')
            real(src, dst)
        src, dst = os.path.join(self.tmp, 'a'), os.path.join(self.tmp, 'b')
        _touch(src, b'new')
        with mock.patch.object(bootstrap.os, 'replace', flaky):
            bootstrap.replace_retry(src, dst, tries=5, delay=0)
        self.assertEqual(len(seen), 3)
        self.assertEqual(_read(dst), b'new')

    def test_gives_up_after_its_tries(self):
        calls = []

        def denied(src, dst):
            calls.append(1)
            raise PermissionError(13, 'Access is denied')
        with mock.patch.object(bootstrap.os, 'replace', denied):
            with self.assertRaises(PermissionError):
                bootstrap.replace_retry('a', 'b', tries=4, delay=0)
        self.assertEqual(len(calls), 4)

    def test_other_errors_are_not_retried(self):
        with self.assertRaises(FileNotFoundError):
            bootstrap.replace_retry(os.path.join(self.tmp, 'missing'),
                                    os.path.join(self.tmp, 'b'), delay=5)

    @unittest.skipUnless(os.name == 'nt', 'the sharing rule is Windows only')
    def test_write_state_survives_a_reader_in_another_process(self):
        write_state(self.paths, {'n': 1})
        holder = subprocess.Popen(
            [sys.executable, '-c', textwrap.dedent('''
                import sys, time
                fh = open(sys.argv[1], encoding="utf-8")
                print("open", flush=True)
                time.sleep(0.6)
                fh.close()
            '''), self.paths.state], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), 'open')
        with self.assertRaises(PermissionError):
            os.replace(self.paths.state, self.paths.state + '.x')
        write_state(self.paths, {'n': 2})        # would fail without retries
        holder.stdout.close()
        self.assertEqual(read_state(self.paths), {'n': 2})


class NoUnicornTest(unittest.TestCase):
    """The launcher asks choose_snapshot about every folder it lists."""

    def test_choose_snapshot_leaves_unicorn_unloaded(self):
        code = textwrap.dedent('''
            import json, os, sys
            from emu import bootstrap
            root = sys.argv[1]
            p = bootstrap.FirmwarePaths(root, 'X.syx', root)
            os.makedirs(p.snapdir)
            for path in (p.syx, p.card, p.gui):
                with open(path, 'wb') as fh:
                    fh.write(b'x')
            stamp = bootstrap.card_stamp(p.card)
            bootstrap.write_state(p, {'stages': {'settle': {
                'inputs': {'syx_sha256': bootstrap._sha256(p.syx)},
                'build': bootstrap._build_id(), 'card_after': stamp}}})
            heavy = sorted(m for m in sys.modules
                           if m.split('.')[0] in ('unicorn', 'capstone')
                           or m in ('emu.snapshot', 'emu.harness'))
            print(json.dumps([bootstrap.choose_snapshot(p)[1], heavy]))
        ''')
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, PYTHONPATH=str(REPO))
            out = subprocess.run([sys.executable, '-c', code, d], cwd=d,
                                 env=env, capture_output=True, text=True,
                                 timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1]),
                         ['gui', []])

    def test_the_version_is_the_one_snapshots_are_written_with(self):
        from emu import snapshot
        self.assertEqual(bootstrap._build_id()['checkpoint_version'],
                         snapshot.CHECKPOINT_VERSION)


def _sparse_file(path, size):
    """A file of `size` that allocates only what is written (NTFS/POSIX)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w+b') as fh:
        sparse.make_sparse(fh)
        sparse.extend(fh, size)


def _poke(path, offset, data):
    with open(path, 'r+b') as fh:
        fh.seek(offset)
        fh.write(data)


def _finished_card(path):
    """What a first boot leaves: ekFS, BEEFBACE, record, project, sounds."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ekfsformat.format_image(path)
    _poke(path, 0, bootstrap.BEEFBACE + bytes(4))
    _poke(path, bootstrap.CARD_RECORD, b'\x01' + bytes(15) + b'\xff' * 8)
    _poke(path, bootstrap.CARD_PROJECT0, bootstrap.BEEFBACE)
    _poke(path, bootstrap.CARD_SOUNDS[0] + 0x400 * 7, b'\x12\x34')


class CardCheckTest(Folder):
    """check_card: the firmware-independent half of the acceptance check."""

    def setUp(self):
        super().setUp()
        self.card = self.paths.card
        _finished_card(self.card)

    def test_a_finished_card_passes(self):
        self.assertEqual(bootstrap.check_card(self.card), [])

    def _fails(self, words):
        problems = bootstrap.check_card(self.card)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(words, problems[0])

    def test_sector_0(self):
        _poke(self.card, 4, b'\x01')
        self._fails('sector 0 holds be ef ba ce 01 00 00 00')
        _poke(self.card, 0, bytes(8))
        self._fails('never initialised')

    def test_record(self):
        _poke(self.card, bootstrap.CARD_RECORD, b'\x00')
        self._fails('sector-0x800 record starts 00')

    def test_project_slot_0(self):
        _poke(self.card, bootstrap.CARD_PROJECT0, bytes(4))
        self._fails('no factory project')

    def test_sounds(self):
        _poke(self.card, bootstrap.CARD_SOUNDS[0] + 0x400 * 7, bytes(2))
        self._fails('sound slots 0x200000..0x23ffff are empty')

    def test_ekfs(self):
        _poke(self.card, ekfsformat.REGION * 512 + 0x10, b'\x99')
        self._fails('no valid ekFS superblock')

    def test_a_product_without_a_sample_volume_skips_the_ekfs(self):
        _poke(self.card, ekfsformat.REGION * 512 + 0x10, b'\x99')
        self.assertEqual(bootstrap.check_card(self.card, ekfs=False), [])
        self.assertEqual(
            bootstrap.check_initialised_card(self.card, ekfs=False), [])
        _poke(self.card, 0, bytes(4))
        self.assertEqual(
            len(bootstrap.check_initialised_card(self.card, ekfs=False)), 1)

    def test_a_short_or_missing_card(self):
        with open(self.card, 'r+b') as fh:
            fh.truncate(4096)
        self.assertEqual(len(bootstrap.check_card(self.card)), 4)
        os.remove(self.card)
        self.assertIn('cannot be read', bootstrap.check_card(self.card)[0])


GOOD_RAM = {0x406480e0: 0, 0x421cd4f4: 0x40000, 0x421cd4f0: 0x40000,
            0x420edc50: 1}


def _shipped_acceptance():
    from emu import device
    dev = device.load(os.path.join(REPO, 'devices', 'digitakt.toml'))
    return dev.firmwares[0].acceptance


def _reader(words):
    def read(addr, n):
        if addr not in words:
            return None
        return words[addr].to_bytes(4, 'big')[:n]
    return read


class RamCheckTest(unittest.TestCase):
    """check_ram and the [firmware.acceptance] table of the shipped 1.53."""

    def setUp(self):
        self.acc = bootstrap.validate_acceptance(_shipped_acceptance())

    def test_the_shipped_table(self):
        self.assertEqual(self.acc, {
            'error_u32': 0x406480e0, 'progress_done': 0x421cd4f4,
            'progress_total': 0x421cd4f0, 'mounted_u32': 0x420edc50})

    def test_a_finished_job_passes(self):
        values, problems = bootstrap.check_ram(_reader(GOOD_RAM), self.acc)
        self.assertEqual(problems, [])
        self.assertEqual(values['progress_done'], 0x40000)

    def test_each_failure_is_named(self):
        cases = [(0x406480e0, 0x20, 'reports error 0x20'),
                 (0x421cd4f4, 0x101000, 'stopped at 0x101000 of 0x40000'),
                 (0x420edc50, 0, 'not mounted')]
        for addr, value, words in cases:
            ram = dict(GOOD_RAM)
            ram[addr] = value
            _values, problems = bootstrap.check_ram(_reader(ram), self.acc)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn(words, problems[0])

    def test_an_address_the_snapshot_lacks(self):
        ram = dict(GOOD_RAM)
        del ram[0x420edc50]
        values, problems = bootstrap.check_ram(_reader(ram), self.acc)
        self.assertIsNone(values['mounted_u32'])
        self.assertIn('mounted_u32 (0x420edc50) is not in the snapshot',
                      problems)

    def test_the_dsp_must_be_running(self):
        # The Digitone's handshake status: 0 in progress, 1 DSP BOOT FAILURE,
        # 2 running.
        acc = dict(self.acc, dsp_running_u32=0x4137b720)
        self.assertEqual(bootstrap.validate_acceptance(acc), acc)
        ram = dict(GOOD_RAM)
        ram[0x4137b720] = bootstrap.DSP_RUNNING
        self.assertEqual(bootstrap.check_ram(_reader(ram), acc)[1], [])
        ram[0x4137b720] = 1
        _values, problems = bootstrap.check_ram(_reader(ram), acc)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn('the DSP did not come up', problems[0])

    def test_validation(self):
        self.assertEqual(bootstrap.validate_acceptance(None), {})
        for bad in ({'error_32': 1}, {'progress_done': 4},
                    {'error_u32': True}, {'error_u32': -4}):
            with self.assertRaises(ValueError):
                bootstrap.validate_acceptance(bad)

    def test_device_file_refuses_non_integers(self):
        from emu import device
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'x.toml')
            with open(path, 'wb') as fh:
                fh.write(b'[device]\nname = "X"\n[panel]\n[[firmware]]\n'
                         b'sha256 = "ab"\n[firmware.acceptance]\n'
                         b'error_u32 = "0x406480e0"\n')
            with self.assertRaises(device.DeviceError):
                device.load(path)


def _snapshot_blob(path, words):
    """A snapshot emu.snapshot accepts, holding `words` in its RAM."""
    from emu import snapshot
    pages = {}
    for addr, value in words.items():
        base = addr - addr % snapshot.PAGE
        page = pages.setdefault(base, bytearray(snapshot.PAGE))
        off = addr - base
        page[off:off + 4] = value.to_bytes(4, 'big')
    mapped = sorted(set(pages) | {0x40000000})       # mapped, never written
    blob = {'regs': {name: 0 for name, _ in snapshot.REGS},
            'pages': {b: zlib.compress(bytes(p)) for b, p in pages.items()},
            'all_mapped': mapped, 'mmio': {}, 'ctlregs': {}, 'ff1_count': 0,
            'movec_count': 0, 'extra': {},
            'checkpoint_version': snapshot.CHECKPOINT_VERSION}
    with open(path, 'wb') as fh:
        pickle.dump(blob, fh, protocol=4)


class SnapshotReaderTest(Folder):
    def test_reads_what_the_snapshot_holds(self):
        snap = os.path.join(self.tmp, 'gui.snap')
        _snapshot_blob(snap, GOOD_RAM)
        read = bootstrap.snapshot_reader(snap)
        self.assertEqual(read(0x421cd4f0, 8), bytes.fromhex('0004000000040000'))
        self.assertEqual(read(0x40000010, 4), bytes(4))   # mapped, all zero
        self.assertIsNone(read(0x50000000, 4))            # never mapped
        values, problems = bootstrap.check_ram(
            read, bootstrap.validate_acceptance(_shipped_acceptance()))
        self.assertEqual(problems, [])


class HalfInstallGuardTest(Folder):
    """BEEFBACE in sector 0 without a finished first boot is cleared."""

    def setUp(self):
        super().setUp()
        self.card = self.paths.card
        _sparse_file(self.card, 0x10000000 + MB)
        _poke(self.card, 0, bootstrap.BEEFBACE + bytes(4) + b'rest')

    def _head(self):
        with open(self.card, 'rb') as fh:
            return fh.read(12)

    def test_clears_the_first_8_bytes_and_says_so(self):
        notes = []
        self.assertTrue(bootstrap.clear_half_install(
            self.card, False, lambda kind, text: notes.append(text)))
        self.assertEqual(self._head(), bytes(8) + b'rest')
        self.assertIn('never finished', notes[0])

    def test_a_vouched_card_is_kept(self):
        self.assertFalse(bootstrap.clear_half_install(self.card, True))
        self.assertEqual(self._head()[:4], bootstrap.BEEFBACE)

    def test_a_card_holding_the_factory_content_is_kept(self):
        # A played card whose stamps a Rebuild dropped: zeroing it would make
        # the firmware erase the user's projects.
        _poke(self.card, bootstrap.CARD_RECORD, b'\x01')
        _poke(self.card, bootstrap.CARD_PROJECT0, bootstrap.BEEFBACE)
        _poke(self.card, bootstrap.CARD_SOUNDS[0], b'\x01')
        self.assertFalse(bootstrap.clear_half_install(self.card, False))
        self.assertEqual(self._head()[:4], bootstrap.BEEFBACE)

    def test_any_content_after_sector_0_keeps_the_card(self):
        # The user's own changes, with nothing vouching for the card: project
        # 0 unprotected but project 1 protected, or only sounds, or only a
        # project -- each means first boot finished, so sector 0 stays.
        for offset, data in ((bootstrap.CARD_RECORD, b'\x02'),
                             (bootstrap.CARD_SOUNDS[1] - 1, b'\x01'),
                             (bootstrap.CARD_PROJECT0, b'user')):
            with self.subTest(offset=hex(offset)):
                _poke(self.card, offset, data)
                self.assertFalse(bootstrap.clear_half_install(self.card, False))
                self.assertEqual(self._head()[:4], bootstrap.BEEFBACE)
                _poke(self.card, offset, bytes(len(data)))

    def test_a_blank_or_missing_card_is_left_alone(self):
        _poke(self.card, 0, bytes(8))
        self.assertFalse(bootstrap.clear_half_install(self.card, False))
        self.assertFalse(bootstrap.clear_half_install(
            os.path.join(self.tmp, 'none.img'), False))


class RebuildCardCheckTest(Folder):
    """What a rebuild over a card the user has changed must accept."""

    def setUp(self):
        super().setUp()
        self.card = self.paths.card
        _finished_card(self.card)

    def test_a_finished_first_boot_passes(self):
        self.assertEqual(bootstrap.check_card(self.card), [])

    def test_the_user_may_protect_other_projects(self):
        # Byte 0 of the record holds projects 0-7; only bit 0 is the install.
        _poke(self.card, bootstrap.CARD_RECORD, b'\x03')
        self.assertEqual(bootstrap.check_card(self.card), [])
        _poke(self.card, bootstrap.CARD_RECORD, b'\x02')
        self.assertEqual(len(bootstrap.check_card(self.card)), 1)

    def test_an_initialised_card_needs_only_sector_0_and_the_ekfs(self):
        # What a rebuild over a card the user has changed must accept.
        for offset, n in ((bootstrap.CARD_RECORD, 1),
                          (bootstrap.CARD_PROJECT0, 4),
                          (bootstrap.CARD_SOUNDS[0] + 0x400 * 7, 2)):
            _poke(self.card, offset, bytes(n))
        self.assertEqual(len(bootstrap.check_card(self.card)), 3)
        self.assertEqual(bootstrap.check_initialised_card(self.card), [])
        _poke(self.card, 0, bytes(4))
        self.assertEqual(len(bootstrap.check_initialised_card(self.card)), 1)


class StubbedFirstRunTest(Folder):
    """first_run with the emulator stages stubbed: what reruns, what stays.

    The card stage is real (a sparse ~950 MB file, ~2 MB on disk); the
    cold boot, intro and settle only write their output files and, like the
    real ones, touch the card: the intro's save puts BEEFBACE in sector 0.
    """

    def setUp(self):
        super().setUp()
        p = self.paths
        _touch(p.syx, b'synthetic, not a firmware')
        env = p.env()
        saved = {k: os.environ.get(k) for k in env}

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ.update(env)
        self.calls = []
        self.kw = {}
        self.heads = []
        self.events = []
        self.accept = None
        self.first_boots = []
        self.flushes = 0
        self.real_accept = False
        self._accept_settled = bootstrap.accept_settled
        stubs = {
            'extract_sections': self._extract,
            '_intro_policy': lambda paths: (None, (3,), True, {}),
            'cold_boot': self._ladder,
            'boot_to_ui': self._intro,
            'settle_ui': self._settle,
            '_require_clean_card_state': lambda snap, step: None,
            'accept_settled': self._accept,
        }
        for name, fn in stubs.items():
            patcher = mock.patch.object(bootstrap, name, fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- the stubs -------------------------------------------------------
    def _extract(self, paths, progress=None):
        self.calls.append('extract')
        _touch(os.path.join(paths.sections, '.source-sha256'),
               bootstrap._sha256(paths.syx).encode())
        _touch(paths.main_img, b'main os')

    def _ladder(self, paths, progress=None, cancel=None, **kw):
        self.calls.append('ladder')
        with open(paths.card, 'rb') as fh:
            self.heads.append(fh.read(8))
        _touch(paths.cold, b'cold')
        return {'stop': 'parked', 'parked_at': 160_000_000,
                'card_writes': 0, 'tasks': 30, 'native': True}

    def _intro(self, syx, rung, out=None, **kw):
        self.calls.append('intro')
        self.kw['intro'] = dict(kw, rung=rung)
        _poke(self.paths.card, 0, bootstrap.BEEFBACE + bytes(4))
        _touch(out, b'gui-raw')
        return bootstrap.IntroResult(True, 80_000_000, 2000, 140, 1, None,
                                     out, None)

    def _settle(self, syx, snap, out=None, **kw):
        self.calls.append('settle')
        self.kw['settle'] = kw
        # Like the firmware: the factory install runs only when the cold
        # boot found an uninitialised card, and a failed one (self.accept
        # set) leaves the record, project and sounds blank.
        if self.heads and self.heads[-1][:4] != bootstrap.BEEFBACE \
                and self.accept is None:
            _poke(self.paths.card, bootstrap.CARD_RECORD, b'\x01')
        # The settle's save flushes the card, which always moves its stamp.
        self.flushes += 1
        t = 1_500_000_000_000_000_000 + self.flushes
        os.utime(self.paths.card, ns=(t, t))
        _touch(out, b'gui')
        return bootstrap.SettleResult(1_050_000_000, 1_050_000_000, 4190,
                                      1347, 120, out, None)

    def _accept(self, paths, acceptance=None, first_boot=True, ekfs=True):
        self.first_boots.append(first_boot)
        if self.real_accept:
            return self._accept_settled(paths, acceptance,
                                        first_boot=first_boot, ekfs=ekfs)
        if self.accept is not None:
            raise StepFailed('settle', self.accept)
        return {'card': 'ok' if first_boot else 'initialised', 'ram': None}

    # -- helpers ---------------------------------------------------------
    def run_first(self):
        self.calls.clear()
        self.heads.clear()
        return bootstrap.first_run(self.paths, progress=self.events.append)

    def play(self, stamp_ns):
        """A panel session: the card is written and flushed, resume saved."""
        _poke(self.paths.card, 0x300000, b'user edit')
        os.utime(self.paths.card, ns=(stamp_ns, stamp_ns))
        _touch(self.paths.resume, b'session')
        now = card_stamp(self.paths.card)
        bootstrap._update_state(self.paths,
                                lambda s: s.__setitem__('resume', now))
        return now

    # -- the tests -------------------------------------------------------
    def test_setup_then_nothing_to_do(self):
        p = self.paths
        self.assertEqual(self.run_first(), p.gui)
        self.assertEqual(self.calls, ['extract', 'ladder', 'intro', 'settle'])
        self.assertEqual(choose_snapshot(p), (p.gui, 'gui'))
        state = read_state(p)
        self.assertEqual(state['card'], card_stamp(p.card))
        self.assertEqual(state['stages']['settle']['acceptance'],
                         {'card': 'ok', 'ram': None})
        self.assertIn('first_boot', state)
        self.assertLess(sparse.allocated_size(p.card), 64 * MB)
        self.run_first()
        self.assertEqual(self.calls, [])

    def test_the_chain_is_the_fast_one(self):
        # The cold boot stops where the firmware parks, the intro resumes
        # its snapshot, and both emulator stages step block-bounded; the
        # settle also credits idle spins. The stamps say so, so a folder
        # built the old way (a 400M ladder, exact stepping) is rebuilt.
        p = self.paths
        self.run_first()
        self.assertEqual(self.kw['intro']['rung'], p.cold)
        self.assertIs(self.kw['intro']['fast'], True)
        self.assertEqual((self.kw['settle']['fast'],
                          self.kw['settle']['fast_idle']), (True, True))
        stages = read_state(p)['stages']
        self.assertEqual(stages['ladder']['parked_at'], 160_000_000)
        self.assertEqual(stages['ladder']['inputs']['cold'],
                         bootstrap._canon(bootstrap.COLD))
        self.assertNotIn('points', stages['ladder']['inputs'])
        self.assertEqual(stages['intro']['inputs']['stepping'], 'fast')
        self.assertIs(stages['settle']['inputs']['fast_idle'], True)

    def test_a_folder_built_with_the_old_ladder_is_rebuilt(self):
        p = self.paths
        self.run_first()

        def old_recipe(state):
            for name in ('ladder', 'intro', 'settle'):
                inputs = state['stages'][name]['inputs']
                inputs.pop('cold', None)
                inputs['points'] = [60_000_000, 400_000_000]
        bootstrap._update_state(p, old_recipe)
        # No snapshot matches the card any more once the stamps are stale?
        # gui.snap still does, but its settle stamp no longer verifies.
        self.run_first()
        self.assertEqual(self.calls, ['ladder', 'intro', 'settle'])

    def test_a_played_folder_is_not_rebuilt(self):
        # setup -> session (resume stamped) -> first_run again, which is
        # what adding the same .syx again does: nothing reruns and the
        # session survives.
        p = self.paths
        self.run_first()
        stamp = self.play(2_000_000_000_000_000_000)
        self.assertEqual(choose_snapshot(p), (p.resume, 'resume'))
        self.events.clear()
        self.run_first()
        self.assertEqual(self.calls, [])
        self.assertTrue(os.path.exists(p.resume))
        self.assertEqual(read_state(p)['resume'], stamp)
        self.assertEqual(choose_snapshot(p), (p.resume, 'resume'))
        texts = [e.text for e in self.events]
        self.assertIn('settle: up to date (resume.snap matches the card)',
                      texts)

    def test_a_card_matching_no_snapshot_is_rebuilt_and_kept(self):
        p = self.paths
        self.run_first()
        self.play(2_000_000_000_000_000_000)
        os.utime(p.card, ns=(3_000_000_000_000_000_000,) * 2)
        self.assertEqual(choose_snapshot(p), (None, 'card-changed'))
        self.run_first()
        self.assertEqual(self.calls, ['ladder', 'intro', 'settle'])
        self.assertFalse(os.path.exists(p.resume))
        self.assertNotIn('resume', read_state(p))
        # A settle vouched for this card: sector 0 was left for the firmware.
        self.assertEqual(self.heads, [bootstrap.BEEFBACE + bytes(4)])
        self.assertEqual(choose_snapshot(p), (p.gui, 'gui'))

    def test_a_first_boot_that_never_finished_is_redone(self):
        p = self.paths
        self.accept = 'the factory project is missing'
        with self.assertRaises(StepFailed) as cm:
            self.run_first()
        self.assertEqual(cm.exception.step, 'settle')
        self.assertFalse(os.path.exists(p.gui))            # set aside
        self.assertTrue(os.path.exists(p.gui + '.rejected'))
        state = read_state(p)
        self.assertNotIn('settle', state['stages'])
        self.assertNotIn('first_boot', state)
        self.assertEqual(choose_snapshot(p), (None, 'not-built'))
        # The retry: the settle's flush moved the card on since the intro,
        # the card holds BEEFBACE without the factory content, and nothing
        # vouches for it -- sector 0 is cleared before the cold boot.
        self.accept = None
        self.events.clear()
        self.run_first()
        self.assertEqual(self.calls, ['ladder', 'intro', 'settle'])
        self.assertEqual(self.heads, [bytes(8)])
        self.assertTrue(any('never finished' in e.text for e in self.events))
        self.assertEqual(choose_snapshot(p), (p.gui, 'gui'))

    def test_a_rebuild_keeps_a_card_first_boot_finished_on(self):
        # The launcher's Rebuild drops the settle stamp, 'card' and 'resume'
        # but keeps the card; the first_boot marker still vouches for it.
        p = self.paths
        self.run_first()
        state = read_state(p)
        for name in ('ladder', 'intro', 'settle'):
            state['stages'].pop(name)
        state.pop('card')
        write_state(p, state)
        self.run_first()
        self.assertEqual(self.heads, [bootstrap.BEEFBACE + bytes(4)])
        self.assertEqual(self.first_boots[-1], False)
        # Without the marker and the stamp (a folder from before the marker,
        # rebuilt), the card's own content still saves it: the record holds
        # the factory project's protect bit, so first boot finished.
        state = read_state(p)
        state['stages'].pop('settle')
        state.pop('first_boot')
        write_state(p, state)
        self.run_first()
        self.assertEqual(self.heads, [bootstrap.BEEFBACE + bytes(4)])

    def test_a_folder_from_before_the_marker_gets_it(self):
        p = self.paths
        self.run_first()
        state = read_state(p)
        state.pop('first_boot')
        write_state(p, state)
        self.run_first()                        # nothing to do, but backfilled
        self.assertEqual(self.calls, [])
        self.assertTrue(read_state(p)['first_boot'].get('backfilled'))

    def test_a_rebuild_after_the_user_changed_the_factory_content(self):
        # The review's case: project 0 unprotected and overwritten, then a
        # rebuild. The cold boot sees an initialised card, so no factory
        # install runs, and the real acceptance must not demand one.
        p = self.paths
        self.run_first()
        self.real_accept = True
        _poke(p.card, bootstrap.CARD_RECORD, b'\x00')
        _poke(p.card, bootstrap.CARD_PROJECT0, b'user')
        os.utime(p.card, ns=(3_000_000_000_000_000_000,) * 2)
        self.assertEqual(choose_snapshot(p), (None, 'card-changed'))
        self.run_first()
        self.assertEqual(self.calls, ['ladder', 'intro', 'settle'])
        self.assertEqual(self.first_boots[-1], False)
        stamp = read_state(p)['stages']['settle']
        self.assertEqual(stamp['acceptance']['card'], 'initialised')
        self.assertEqual(choose_snapshot(p), (p.gui, 'gui'))
        # The user's change is still there: nothing re-installed over it.
        with open(p.card, 'rb') as fh:
            fh.seek(bootstrap.CARD_PROJECT0)
            self.assertEqual(fh.read(4), b'user')

    def test_a_new_card_forgets_the_old_first_boot(self):
        p = self.paths
        self.run_first()
        os.remove(p.card)
        self.run_first()
        self.assertEqual(self.calls, ['ladder', 'intro', 'settle'])
        self.assertEqual(self.heads, [bytes(8)])           # freshly formatted
        self.assertIn('first_boot', read_state(p))         # set again by settle

    def test_a_state_write_that_fails_names_the_stage(self):
        with mock.patch.object(bootstrap, 'write_state',
                               side_effect=PermissionError(13, 'denied')):
            with self.assertRaises(StepFailed) as cm:
                self.run_first()
        self.assertEqual(cm.exception.step, 'extract')
        self.assertIn('could not be recorded', cm.exception.reason)

    def test_a_product_without_a_sample_volume_gets_a_blank_card(self):
        # The Digitone: no ekFS to format; its own first boot initialises
        # the card, and the acceptance check does not look for a volume.
        from types import SimpleNamespace
        seen = []
        dev = SimpleNamespace(card_ekfs=False)
        with mock.patch.object(bootstrap, '_intro_policy',
                               lambda paths: (dev, (3,), False, {})):
            accept = self._accept

            def spy(paths, acceptance=None, first_boot=True, ekfs=True):
                seen.append(ekfs)
                return accept(paths, acceptance, first_boot, ekfs)
            with mock.patch.object(bootstrap, 'accept_settled', spy):
                self.assertEqual(self.run_first(), self.paths.gui)
        self.assertEqual(self.calls, ['extract', 'ladder', 'intro', 'settle'])
        self.assertEqual(self.heads[0], bytes(8))       # blank at cold boot
        self.assertFalse(ekfsformat.is_formatted(self.paths.card))
        self.assertEqual(seen, [False])
        card = read_state(self.paths)['stages']['card']
        self.assertIs(card['formatted'], False)
        self.assertIs(card['blank'], True)


class PrepareCardSparseTest(Folder):
    """A new card occupies what the format writes, not ~950 MB."""

    def test_a_formatted_card_is_small_on_disk(self):
        card = self.paths.card
        os.makedirs(os.path.dirname(card))
        self.assertTrue(bootstrap.prepare_card(card))
        self.assertTrue(ekfsformat.is_formatted(card))
        size = os.path.getsize(card)
        self.assertEqual(size, 949_657_600)          # as the build reported
        if not sparse.is_sparse(card) and os.name == 'nt':
            self.skipTest('no sparse files here')
        self.assertLess(sparse.allocated_size(card), 16 * MB)


if __name__ == '__main__':
    unittest.main()
