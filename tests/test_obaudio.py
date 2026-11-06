"""emu/obaudio.py: the Overbridge sender's entry hands its frames on, without running a CPU."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unicorn.m68k_const import UC_M68K_REG_A7

from emu import obaudio


class FakeUc:
    def __init__(self, mem, sp):
        self.mem, self.sp = mem, sp

    def reg_read(self, reg):
        assert reg == UC_M68K_REG_A7
        return self.sp

    def mem_read(self, addr, size):
        return bytearray(self.mem[addr:addr + size])


class Profile(dict):
    def __getattr__(self, name):
        return self[name]


class ObAudioTest(unittest.TestCase):
    def call(self, ob, frames_at, count, payload):
        mem = bytearray(0x2000)
        mem[0x100:0x110] = struct.pack('>IIII', 0x40001234, frames_at, 0x900, count)
        mem[0x400:0x400 + len(payload)] = payload
        ob._on_frames(FakeUc(mem, 0x100), 0x40003592, 2, None)

    def test_frames_go_to_the_sink(self):
        got = []
        ob = obaudio.ObAudio(sink=got.append)
        frames = b''.join(struct.pack('>12i', *range(n, n + 12)) for n in range(32))
        self.call(ob, 0x400, 32, frames)
        self.assertEqual(got, [frames])
        self.call(ob, 0, 4, b'')                 # no buffer: silence of the same length
        self.assertEqual(got[-1], bytes(4 * obaudio.FRAME))
        self.call(ob, 0x400, 0, b'')             # nothing to send
        self.assertEqual((len(got), ob.frames, ob.blocks), (2, 36, 2))
        self.assertIn('36 frames', ob.report())

    def test_install_needs_the_symbol(self):
        hooks = []
        self.assertFalse(obaudio.ObAudio().install(None, lambda a, f: hooks.append(a),
                                                   Profile(ob_audio_frames=None)))
        self.assertTrue(obaudio.ObAudio().install(None, lambda a, f: hooks.append(a),
                                                  Profile(ob_audio_frames=0x40003592)))
        self.assertEqual(hooks, [0x40003592])


if __name__ == '__main__':
    unittest.main()
