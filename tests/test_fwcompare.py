"""emu/fwcompare.py: the key-script language and the screen and audio
comparison, on synthetic frames and sound."""
import os
import struct
import unittest

from emu import device, fwcompare, panel
from emu.session import run_script

DEVICES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'devices')


def frame(pixels):
    """-> a packed 128x64 frame lighting `pixels` (see emu/panel.py lit)."""
    buf = bytearray(panel.SIZE)
    for x, y in pixels:
        buf[(7 - (y // 8)) + 8 * x] |= 1 << (y % 8)
    return bytes(buf)


class ScriptTest(unittest.TestCase):
    def test_parse(self):
        steps = fwcompare.parse_script(
            '# a comment\n'
            'wait 500\n'
            'tap PLAY\n'
            'tap 1 80 700   # trig 1\n'
            'press FUNC 50\n'
            'release FUNC\n'
            'turn A -2\n'
            'tap #24\n'
            'snap here\n')
        self.assertEqual(steps, [
            ('wait', 500.0), ('tap', 'PLAY', 100, 300),
            ('tap', '1', 80.0, 700.0), ('press', 'FUNC', 50.0),
            ('release', 'FUNC', 100), ('turn', 'A', -2, 150),
            ('tap', '#24', 100, 300), ('snap', 'here')])

    def test_errors(self):
        for text in ('jump 5', 'wait soon', 'turn A 1.5', 'wait -1',
                     'snap a\nsnap a'):
            with self.subTest(text=text), \
                    self.assertRaises(fwcompare.ScriptError):
                fwcompare.parse_script(text)

    def test_the_default_script_follows_the_panel(self):
        """Each product's own pages: the first Digitone check stopped on a
        SRC key its panel does not have."""
        for name, pages in (('digitakt', ['src-page']),
                            ('digitone', ['syn1-page', 'syn2-page'])):
            dev = device.load(os.path.join(DEVICES, name + '.toml'))
            steps = fwcompare.parse_script(
                fwcompare.default_script(dev.labels.values()))
            snaps = [s[1] for s in steps if s[0] == 'snap']
            with self.subTest(name=name):
                self.assertIn('playing', snaps)
                for page in pages:
                    self.assertIn(page, snaps)
                self.assertEqual(fwcompare.script_problems(steps, dev), [])
        self.assertNotIn('src-page', [s[1] for s in fwcompare.parse_script(
            fwcompare.default_script(['TRIG', 'SYN1', '1', 'PLAY', 'STOP']))
            if s[0] == 'snap'])

    def test_script_problems(self):
        dn = device.load(os.path.join(DEVICES, 'digitone.toml'))
        steps = fwcompare.parse_script(
            'tap SRC\ntap syn1\npress #24\nturn I 1\nturn J 1\nturn ? 1')
        self.assertEqual(fwcompare.script_problems(steps, dn), [
            "no key called 'SRC' on the Digitone panel",
            "no encoder 'J' on the Digitone panel",
            "no encoder '?' on the Digitone panel"])

    def test_run_script_drives_a_session(self):
        calls = []

        class Fake:
            ms = 0.0
            halted = None

            def run_ms(self, ms):
                calls.append(('run', ms))
                self.ms += ms

            def tap(self, code, hold_ms, after_ms):
                calls.append(('tap', code, hold_ms, after_ms))

            def code(self, name):
                return {'PLAY': 10}[name]

            def encoder(self, name):
                return 1

            def turn(self, enc, detents, after_ms):
                calls.append(('turn', enc, detents))
        steps = fwcompare.parse_script('wait 10\ntap PLAY\nsnap x\nturn A 2')
        marks = run_script(Fake(), steps)
        self.assertEqual(marks, [('x', 10.0)])
        self.assertIn(('tap', 10, 100, 300), calls)
        self.assertIn(('turn', 1, 2), calls)


class ScreenTest(unittest.TestCase):
    def test_pixel_diff(self):
        a = frame({(1, 1), (2, 2)})
        b = frame({(1, 1), (5, 60)})
        self.assertEqual(fwcompare.pixel_diff(a, a), (0, None))
        self.assertEqual(fwcompare.pixel_diff(a, b), (2, (2, 2, 5, 60)))
        self.assertEqual(fwcompare.pixel_diff(None, None), (None, None))

    def test_first_divergence(self):
        a, b = frame({(0, 0)}), frame({(0, 1)})
        self.assertIsNone(fwcompare.first_divergence([(0, a)], [(0, a)]))
        self.assertEqual(fwcompare.first_divergence(
            [(0, a), (10, a)], [(0, a), (7, b)]), 7)

    def test_diff_png(self):
        png = fwcompare.diff_png(frame({(0, 0)}), frame({(1, 1)}), scale=1)
        self.assertTrue(png.startswith(b'\x89PNG'))


class AudioTest(unittest.TestCase):
    def pcm(self, values):
        return struct.pack('<%dh' % len(values), *values)

    def test_identical(self):
        p = self.pcm([1, 2, 3, 4] * 100)
        self.assertTrue(fwcompare.audio_diff(p, p, 48000)['identical'])

    def test_windows(self):
        quiet = [0] * 48000 * 2                  # one second of stereo
        loud = list(quiet)
        loud[48000:48010] = [1000] * 10          # at 0.5 s
        rep = fwcompare.audio_diff(self.pcm(quiet), self.pcm(loud), 48000)
        self.assertFalse(rep['identical'])
        self.assertEqual(rep['windows_differing'], 1)
        self.assertEqual(rep['first_difference_ms'], 500)


class CompareTest(unittest.TestCase):
    class FakeRun:
        def __init__(self, screen, pcm):
            self.marks = [('a', 5.0)]
            self.screens = {'a': screen}
            self.frames = [(0.0, screen)]
            self.pcm = pcm
            self.rate = 48000
            self.halted = None

    def test_same_and_different(self):
        s = frame({(3, 3)})
        same = fwcompare.compare(self.FakeRun(s, b'\0\0' * 8),
                                 self.FakeRun(s, b'\0\0' * 8))
        self.assertTrue(same['identical'])
        diff = fwcompare.compare(self.FakeRun(s, b'\0\0' * 8),
                                 self.FakeRun(frame({(4, 4)}), b'\0\0' * 8))
        self.assertFalse(diff['identical'])
        self.assertEqual(diff['snaps'][0]['pixels_differing'], 2)
        self.assertTrue(any('differ' in line
                            for line in fwcompare.summary(diff)))


if __name__ == '__main__':
    unittest.main()
