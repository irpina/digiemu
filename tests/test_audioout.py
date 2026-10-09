"""emu/audioout.py: the PCM helpers, the Master Volume gain, and the host
output devices.

Nothing here opens a real sound device unless asked: the two device tests
play a quiet tone through the default output, and fail on a machine with
none (a remote desktop, a runner), so they run only with
DIGIEMU_AUDIO_DEVICE_TESTS=1. Everything else stands in for the device.
"""
import contextlib
import io
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock

from emu import audioout

DEVICE_TESTS = unittest.skipUnless(
    os.environ.get('DIGIEMU_AUDIO_DEVICE_TESTS') == '1'
    and sys.platform in ('win32', 'darwin', 'linux'),
    'plays through the default output: set DIGIEMU_AUDIO_DEVICE_TESTS=1 '
    '(Windows, macOS or Linux)')


def _pcm(*samples):
    return struct.pack('<%dh' % len(samples), *samples)


class AudioOutTest(unittest.TestCase):
    def test_frames_from_ssi(self):
        # 32-bit word, 24-bit sample: bytes 1 and 2 of each 4-byte word are 16-bit LE
        raw = bytes([0x00, 0x12, 0x34, 0x00, 0x00, 0x56, 0x78, 0x00])
        pcm = audioout.frames_from_ssi(raw, word_bits=32, sample_bits=24)
        self.assertEqual(len(pcm), 4)
        # first word: data[2]=0x34, data[1]=0x12 -> 0x34, 0x12
        # second word: data[6]=0x78, data[5]=0x56 -> 0x78, 0x56
        self.assertEqual(pcm, bytes([0x34, 0x12, 0x78, 0x56]))

    def test_trim_silence(self):
        silence = bytes(400)
        sound = struct.pack('<hh', 500, 500) * 100
        pcm = silence + sound + silence
        trimmed = audioout.trim_silence(pcm, channels=2, threshold=10, rate=48000, pad_ms=2)
        self.assertTrue(len(trimmed) > len(sound))
        self.assertTrue(len(trimmed) < len(pcm))

    def test_wav_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'test.wav')
            w = audioout.WavFile(path, rate=48000, channels=2)
            data = struct.pack('<hh', 100, 200) * 100
            w.write(data)
            self.assertEqual(w.queued(), 0)
            self.assertEqual(w.played, 1)
            w.close()

            with wave.open(path, 'rb') as r:
                self.assertEqual(r.getnchannels(), 2)
                self.assertEqual(r.getsampwidth(), 2)
                self.assertEqual(r.getframerate(), 48000)
                self.assertEqual(r.getnframes(), 100)

    def test_wav_file_applies_the_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'half.wav')
            w = audioout.WavFile(path)
            w.gain = 0.5
            w.write(_pcm(1000, -1000))
            w.close()
            with wave.open(path, 'rb') as r:
                self.assertEqual(r.readframes(1), _pcm(500, -500))


class GainTest(unittest.TestCase):
    """apply_gain: the Master Volume knob's software gain."""

    def test_unity_passes_the_same_bytes_through(self):
        pcm = _pcm(1, -2, 32767, -32768)
        self.assertIs(audioout.apply_gain(pcm, 1.0), pcm)
        self.assertEqual(audioout.apply_gain(b'', 0.5), b'')

    def test_scaling_and_silence(self):
        self.assertEqual(audioout.apply_gain(_pcm(1000, -1000, 3), 0.5),
                         _pcm(500, -500, 1))
        self.assertEqual(audioout.apply_gain(_pcm(1000, -1000), 0.0),
                         _pcm(0, 0))

    def test_boost_clips_to_16_bits(self):
        self.assertEqual(audioout.apply_gain(_pcm(30000, -30000, 100), 1.5),
                         _pcm(32767, -32768, 150))


class DeviceChoiceTest(unittest.TestCase):
    """WaveOut picks the host's output without opening anything here."""

    def test_each_platform_gets_its_own_output(self):
        made = []

        def fake(name):
            return lambda *a: made.append((name, a)) or name
        with mock.patch.object(audioout, '_WinMMOut', fake('winmm')), \
                mock.patch.object(audioout, '_AudioQueueOut', fake('audioqueue')), \
                mock.patch.object(audioout, '_PulseOut', fake('pulse')):
            for platform, want in (('win32', 'winmm'), ('darwin', 'audioqueue'),
                                   ('linux', 'pulse')):
                with mock.patch.object(audioout.sys, 'platform', platform):
                    self.assertEqual(audioout.WaveOut(44100, 2), want)
        self.assertEqual(made, [('winmm', (44100, 2, 16, 20)),
                                ('audioqueue', (44100, 2, 16, 20)),
                                ('pulse', (44100, 2, 16, 20))])

    def test_other_platforms_have_no_device(self):
        with mock.patch.object(audioout.sys, 'platform', 'sunos5'):
            with self.assertRaises(OSError):
                audioout.WaveOut()


class _RecordingOut:
    """Stands in for a device: records each write and the gain it had."""
    made = []

    def __init__(self, rate, channels):
        self.gain = 1.0
        self.writes = []
        _RecordingOut.made.append(self)

    def write(self, pcm, block=False, abort=None):
        self.writes.append((len(pcm), self.gain))

    def drain(self, abort=None):
        pass

    def close(self):
        pass


class PlayerGainTest(unittest.TestCase):
    def test_replay_follows_master_volume(self):
        _RecordingOut.made = []
        with mock.patch.object(audioout, 'WaveOut', _RecordingOut):
            player = audioout.Player(rate=48000, channels=2)
            player.gain = 0.25
            player.play(bytes(48000 * 4 // 4))           # a quarter second
            player._thread.join(5)
        out, = _RecordingOut.made
        self.assertEqual(sum(n for n, _g in out.writes), 48000)
        self.assertEqual({g for _n, g in out.writes}, {0.25})
        self.assertGreater(len(out.writes), 1)            # in tenths of a second


class _FakePulse:
    """Stands in for libpulse-simple and for libpulse (ctypes.CDLL returns
    it for both): a server that takes every block at once, unless told to
    fail, to refuse a connection, or to hold a call back."""

    def __init__(self):
        self.written = []           # the blocks pa_simple_write was given
        self.fail = False           # calls fail, as with the server gone
        self.refuse = False         # pa_simple_new fails
        self.latency = 10 ** 9      # pa_simple_get_latency's answer, us
        self.gate = None            # an Event a write waits for
        self.drain_gate = None      # and one a drain waits for
        self.entered = threading.Event()        # a write has begun
        self.asked = self.opened = self.freed = 0
        self.drained = self.flushed = 0
        self.tlength = None
        for name in ('pa_simple_new', 'pa_simple_write', 'pa_simple_free',
                     'pa_simple_get_latency', 'pa_simple_flush',
                     'pa_simple_drain', 'pa_strerror'):
            # A plain function, which takes .argtypes and .restype.
            setattr(self, name,
                    (lambda f: lambda *a: f(*a))(getattr(self, '_' + name)))

    def _pa_simple_new(self, *a):
        self.asked += 1
        if self.refuse:
            a[8]._obj.value = 6
            return None
        self.tlength = a[7]._obj.tlength
        self.opened += 1
        return self.opened

    def _pa_simple_write(self, pa, chunk, n, err):
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            err._obj.value = 6
            return -1
        self.written.append(bytes(chunk[:n]))
        return 0

    def _pa_simple_get_latency(self, pa, err):
        return self.latency

    def _pa_simple_free(self, pa):
        self.freed += 1

    def _pa_simple_flush(self, pa, err):
        self.flushed += 1
        return 0

    def _pa_simple_drain(self, pa, err):
        if self.drain_gate is not None:
            self.drain_gate.wait(10)
        if self.fail:
            err._obj.value = 6
            return -1
        self.drained += 1
        return 0

    def _pa_strerror(self, code):
        return b'Connection refused'


class PulseOutTest(unittest.TestCase):
    """_PulseOut against a stand-in library and a clock that only moves
    when told to: no server, no sound."""

    def setUp(self):
        self.lib = _FakePulse()
        self.said = io.StringIO()
        self.now = 100.0
        cls = audioout._PulseOut
        for p in (mock.patch.object(audioout.ctypes, 'CDLL',
                                    lambda path: self.lib),
                  mock.patch.object(audioout.ctypes.util, 'find_library',
                                    lambda name: name),
                  mock.patch.object(cls, '_libs', None),
                  mock.patch.object(cls, '_now',
                                    staticmethod(lambda: self.now)),
                  mock.patch.object(cls, 'RETRY_S', 0.01),
                  mock.patch.object(cls, 'CLOSE_WAIT_S', 0.2),
                  mock.patch.dict(os.environ),
                  contextlib.redirect_stdout(self.said)):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)
        os.environ.pop('DIGIEMU_PULSE_MS', None)

    def open(self, buffers=8, block_ms=10):
        out = audioout._PulseOut(48000, 2, buffers, block_ms)
        self.addCleanup(out.close)
        return out

    def until(self, cond, seconds=5.0):
        end = time.monotonic() + seconds
        while not cond():
            self.assertLess(time.monotonic(), end, 'still waiting')
            time.sleep(0.002)

    def blocks(self, out, n):
        return [bytes([i + 1]) * out.block for i in range(n)]

    def give(self, out, n):
        """Write n blocks and wait until the server has them all."""
        have = len(self.lib.written)
        out.write(b''.join(self.blocks(out, n)), block=True)
        self.until(lambda: len(self.lib.written) == have + n
                   and not out._busy)

    def hold_writes(self):
        self.lib.gate = threading.Event()
        self.addCleanup(self.lib.gate.set)

    def test_blocks_reach_the_server_in_order(self):
        out = self.open()
        blocks = self.blocks(out, 3)
        out.write(b''.join(blocks) + b'\x7f' * 8)
        self.until(lambda: len(self.lib.written) == 3)
        self.assertEqual(self.lib.written, blocks)
        self.assertEqual((out.played, out.dropped), (3, 0))

    def test_master_volume_is_applied(self):
        out = self.open()
        out.gain = 0.5
        out.write(_pcm(1000, -1000) * (out.block // 4))
        self.until(lambda: len(self.lib.written) == 1)
        self.assertEqual(self.lib.written[0],
                         _pcm(500, -500) * (out.block // 4))

    def test_the_server_buffer_is_two_blocks_and_at_least_1024_frames(self):
        self.open(block_ms=10)
        self.assertEqual(self.lib.tlength, 1024 * 4)        # 21 ms
        self.open(block_ms=20)
        self.assertEqual(self.lib.tlength, 2 * 960 * 4)     # 40 ms
        os.environ['DIGIEMU_PULSE_MS'] = '43'
        self.open(block_ms=10)
        self.assertEqual(self.lib.tlength, 48000 * 43 // 1000 * 4)
        os.environ['DIGIEMU_PULSE_MS'] = '1'            # one block at least
        self.open(block_ms=10)
        self.assertEqual(self.lib.tlength, 480 * 4)
        os.environ['DIGIEMU_PULSE_MS'] = 'a lot'
        self.open(block_ms=10)
        self.assertEqual(self.lib.tlength, 1024 * 4)

    def test_the_library_is_looked_up_once(self):
        with mock.patch.object(audioout.ctypes.util, 'find_library',
                               side_effect=lambda name: name) as find:
            self.open()
            self.open()
        self.assertEqual(find.call_count, 2)        # the two libraries

    def test_no_server_is_a_missing_device(self):
        self.lib.refuse = True
        with self.assertRaisesRegex(OSError, 'Connection refused'):
            audioout._PulseOut()

    def test_a_block_that_finds_the_queue_full_is_dropped(self):
        self.hold_writes()
        out = self.open(buffers=2)
        first, *rest = self.blocks(out, 5)
        out.write(first)
        self.until(self.lib.entered.is_set)         # the writer holds it
        out.write(b''.join(rest))
        self.assertEqual((out.played, out.dropped), (3, 2))
        self.assertEqual(out.queued(), 3)
        self.lib.gate.set()
        self.until(lambda: len(self.lib.written) == 3)
        self.assertEqual(self.lib.written, [first] + rest[:2])

    def test_a_blocking_write_waits_for_room_and_can_be_abandoned(self):
        self.hold_writes()
        out = self.open(buffers=1)
        first, second, third = self.blocks(out, 3)
        out.write(first)
        self.until(self.lib.entered.is_set)
        out.write(second)                           # the queue is full now
        asked = []
        out.write(third, block=True,
                  abort=lambda: asked.append(1) or len(asked) > 3)
        self.assertEqual(len(asked), 4)
        self.assertEqual((out.played, out.dropped), (2, 0))

    def test_what_the_server_has_counts_until_it_has_played(self):
        out = self.open()
        self.give(out, 2)                   # under prebuf: it holds them back
        self.assertEqual(out.queued(), 2)
        self.now += 1.0
        self.assertEqual(out.queued(), 2)
        self.give(out, 1)                   # 30 ms: it plays from here
        self.assertEqual(out.queued(), 3)
        self.now += 0.005
        self.assertEqual(out.queued(), 3)   # the first is not finished
        self.now += 0.006
        self.assertEqual(out.queued(), 2)
        self.now += 0.018
        self.assertEqual(out.queued(), 1)   # a millisecond is left
        self.now += 0.002
        self.assertEqual(out.queued(), 0)   # dry
        self.give(out, 1)                   # and it holds back again
        self.now += 1.0
        self.assertEqual(out.queued(), 1)

    def test_the_server_never_has_more_than_its_buffer_and_a_block(self):
        out = self.open()
        self.give(out, 8)           # 80 ms taken with the clock standing
        self.assertEqual(out.queued(), 4)           # 31 ms

    def test_the_servers_latency_is_an_upper_limit_once_it_has_settled(self):
        self.lib.latency = 4_000
        out = self.open()
        self.give(out, 8)
        self.assertEqual(out.queued(), 4)           # too new to go by
        self.give(out, 3)
        self.assertEqual(out.queued(), 1)           # 4 ms

    def test_the_devices_own_latency_is_not_counted_as_queued(self):
        self.lib.latency = 500_000                  # a slow sink
        out = self.open()
        self.give(out, 12)
        self.assertEqual(out.queued(), 4)
        self.now += 0.032
        self.assertEqual(out.queued(), 0)

    def test_a_latency_the_server_cannot_give_limits_nothing(self):
        self.lib.latency = 2 ** 64 - 1              # PA_USEC_INVALID
        out = self.open()
        self.give(out, 12)
        self.assertEqual(out.queued(), 4)

    def test_drain_plays_what_is_left_and_waits_for_it(self):
        self.lib.drain_gate = threading.Event()
        self.addCleanup(self.lib.drain_gate.set)
        out = self.open()
        out.write(self.blocks(out, 1)[0] + b'\x7f' * 8)
        done = threading.Event()
        thread = threading.Thread(target=lambda: (out.drain(), done.set()),
                                  daemon=True)
        thread.start()
        self.until(lambda: len(self.lib.written) == 2)
        self.assertEqual(self.lib.written[1],
                         (b'\x7f' * 8).ljust(out.block, b'\0'))
        self.assertFalse(done.wait(0.05))           # the server is playing
        self.lib.drain_gate.set()
        thread.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(self.lib.drained, 1)
        self.assertEqual(out.queued(), 0)           # though no time passed

    def test_drain_can_be_abandoned(self):
        self.lib.drain_gate = threading.Event()
        self.addCleanup(self.lib.drain_gate.set)
        out = self.open()
        asked = []
        out.drain(abort=lambda: asked.append(1) or len(asked) > 3)
        self.assertEqual(len(asked), 4)
        self.assertEqual(self.lib.drained, 0)

    def test_a_stopped_replay_is_not_played_out(self):
        out = self.open()
        self.give(out, 3)
        out.drain(abort=lambda: True)
        out.close()
        self.assertEqual((self.lib.drained, self.lib.flushed), (0, 1))

    def test_a_replay_goes_through_and_is_drained(self):
        with mock.patch.object(audioout.sys, 'platform', 'linux'):
            player = audioout.Player(rate=48000, channels=2)
            player.play(bytes(48000 * 4 // 4 + 8))  # a quarter second, and 8
            player._thread.join(5)
        self.assertFalse(player.playing)
        self.assertIsNone(player.error)
        self.assertEqual(sum(map(len, self.lib.written)), 13 * 3840)
        self.assertEqual((self.lib.drained, self.lib.freed), (1, 1))

    def test_a_failed_write_drops_until_there_is_a_server_again(self):
        out = self.open()
        self.give(out, 3)
        self.assertEqual(out.queued(), 3)
        self.lib.fail = self.lib.refuse = True      # the server went away
        out.write(b''.join(self.blocks(out, 2)))
        self.until(lambda: out.lost == 1)
        self.assertEqual((out.played, out.dropped), (3, 2))
        self.assertEqual(out.queued(), 0)           # what it had went too
        out.write(self.blocks(out, 1)[0])
        out.drain()                                 # nothing to wait for
        self.assertEqual((out.played, out.dropped), (3, 3))
        self.assertEqual((self.lib.freed, self.lib.drained), (1, 0))
        self.assertIn('lost the server (Connection refused)',
                      self.said.getvalue())

        self.lib.fail = self.lib.refuse = False     # and came back
        self.until(lambda: 'connected again' in self.said.getvalue())
        self.give(out, 1)
        self.assertEqual((out.played, out.lost), (4, 1))
        out.close()
        self.assertEqual(self.lib.freed, 2)
        self.assertIn('dropped 3, server lost 1 times', self.said.getvalue())

    def test_a_blocking_write_waits_for_the_server_to_come_back(self):
        out = self.open()
        self.lib.fail = self.lib.refuse = True
        out.write(self.blocks(out, 1)[0])
        self.until(lambda: out.lost == 1)
        block = self.blocks(out, 1)[0]
        done = threading.Event()
        thread = threading.Thread(
            target=lambda: (out.write(block, block=True), done.set()),
            daemon=True)
        thread.start()
        self.assertFalse(done.wait(0.05))
        self.lib.fail = self.lib.refuse = False
        thread.join(5)
        self.until(lambda: self.lib.written == [block])
        self.assertEqual(out.dropped, 1)            # only the one that failed

    def test_a_new_connection_is_tried_for_every_so_often(self):
        with mock.patch.object(audioout._PulseOut, 'RETRY_S', 0.05):
            out = self.open()
            self.lib.fail = self.lib.refuse = True
            out.write(self.blocks(out, 1)[0])
            self.until(lambda: out.lost == 1)
            time.sleep(0.2)
            self.assertLessEqual(self.lib.asked, 1 + 5)
            out.close()                             # while it is waiting
        self.assertEqual((self.lib.opened, self.lib.freed), (1, 1))

    def test_a_failed_drain_is_a_lost_server_too(self):
        out = self.open()
        self.give(out, 1)
        self.lib.fail = self.lib.refuse = True
        out.drain()
        self.assertEqual((out.lost, out.played, out.dropped), (1, 1, 0))
        self.assertEqual(out.queued(), 0)

    def test_close_does_not_wait_for_a_write_that_never_returns(self):
        self.hold_writes()
        out = self.open()
        out.write(self.blocks(out, 1)[0])
        self.until(self.lib.entered.is_set)
        began = time.monotonic()
        out.close()
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(self.lib.freed, 0)         # not under the write
        self.assertIn('not answering', self.said.getvalue())
        self.lib.gate.set()                         # it returns after all
        self.until(lambda: not out._thread.is_alive())
        self.assertEqual((self.lib.flushed, self.lib.freed), (1, 1))

    def test_a_write_that_fails_after_close_gave_up_says_nothing(self):
        self.hold_writes()
        out = self.open()
        out.write(self.blocks(out, 1)[0])
        self.until(self.lib.entered.is_set)
        out.close()
        self.lib.fail = True
        self.lib.gate.set()
        self.until(lambda: not out._thread.is_alive())
        self.assertEqual(self.lib.freed, 1)
        self.assertNotIn('lost the server', self.said.getvalue())

    def test_close_drops_what_is_still_queued(self):
        self.hold_writes()
        out = self.open()
        first, *rest = self.blocks(out, 3)
        out.write(first)
        self.until(self.lib.entered.is_set)
        out.write(b''.join(rest))
        with mock.patch.object(audioout._PulseOut, 'CLOSE_WAIT_S', 5):
            thread = threading.Thread(target=out.close, daemon=True)
            thread.start()
            self.until(lambda: out._closing)
            self.lib.gate.set()
            thread.join(5)
        self.assertEqual(self.lib.written, [first])
        self.assertEqual(out.played, 1)
        self.assertEqual((self.lib.flushed, self.lib.freed), (1, 1))

    def test_nothing_is_taken_after_close(self):
        out = self.open()
        out.close()
        out.write(b''.join(self.blocks(out, 3)) + b'\x7f' * 8, block=True)
        self.assertEqual(len(out._pending), 0)
        out.drain()
        self.assertEqual((out.played, out.dropped), (0, 0))
        self.assertEqual(self.lib.written, [])

    def test_a_clean_session_closes_once_and_says_nothing(self):
        out = self.open()
        self.give(out, 20)
        out.close()
        out.close()
        self.assertEqual((self.lib.flushed, self.lib.freed), (1, 1))
        self.assertEqual(self.said.getvalue(), '')

    def test_a_writer_thread_that_dies_leaves_nothing_waiting(self):
        out = self.open()
        self.lib.pa_simple_write = None             # the call raises
        with mock.patch.object(threading, 'excepthook', lambda args: None):
            out.write(self.blocks(out, 1)[0])
            out._thread.join(5)
        self.assertEqual(out.queued(), 0)
        out.write(self.blocks(out, 1)[0], block=True)
        out.drain()
        out.close()
        self.assertEqual(self.lib.freed, 1)


class DeviceTest(unittest.TestCase):
    @DEVICE_TESTS
    def test_waveout_lifecycle(self):
        out = audioout.WaveOut(rate=48000, channels=2, buffers=8, block_ms=10)
        self.assertEqual(out.rate, 48000)
        self.assertEqual(out.channels, 2)
        self.assertEqual(out.queued(), 0)

        # Write two blocks
        pcm = struct.pack('<hh', 100, 100) * (out.block // 4) * 2
        out.write(pcm, block=True)
        self.assertTrue(out.played > 0)
        out.drain()
        self.assertEqual(out.queued(), 0)
        out.close()

    @DEVICE_TESTS
    def test_player_lifecycle(self):
        player = audioout.Player(rate=48000, channels=2)
        self.assertIsNone(player.error)
        self.assertFalse(player.playing)

        pcm = struct.pack('<hh', 100, 100) * 960  # 20ms
        player.play(pcm)
        time.sleep(0.01)
        player.stop()
        self.assertFalse(player.playing)
        self.assertIsNone(player.error)


if __name__ == '__main__':
    unittest.main()
