"""emu/usbmidi.py: USB-MIDI event packets, and the post the USB interrupt
would make, from the idle spin, with the stack left as it was.

No firmware: the deferred-call poster is an `rts`, the idle spin a
`bra.b` to itself, and the profile is a stand-in with their addresses.
"""
import struct
import unittest

from unicorn import UC_HOOK_CODE
from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR

from emu import usbmidi
from emu.harness import Machine

CODE = 0x40100000
SPIN = CODE + 0x10          # bra.b *
DEFER = CODE + 0x20         # rts
PARSE = CODE + 0x30         # rts
RELEASE = CODE + 0x40       # rts
SEND = CODE + 0x50
SEND_SHORT = CODE + 0x60
RING = 0x4BC11000
STACK = 0x40200000


class Profile(dict):
    def __getattr__(self, name):
        return self.get(name)


def _profile():
    return Profile(usb_midi_send=SEND, usb_midi_send_short=SEND_SHORT,
                   usb_defer=DEFER, usb_midi_parse=PARSE,
                   usb_rx_release=RELEASE, usb_midi_short_ring=RING,
                   idle_spins=(SPIN,))


def _machine():
    m = Machine()
    for addr in (CODE, STACK - 0x100, RING):
        m.ensure(addr)
    m.uc.mem_write(SPIN, b'\x60\xfe')
    for addr in (DEFER, PARSE, RELEASE, SEND, SEND_SHORT):
        m.uc.mem_write(addr, b'\x4e\x75')
    m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
    m.uc.reg_write(UC_M68K_REG_A7, STACK)

    def at(addr, fn):
        m.uc.hook_add(UC_HOOK_CODE, fn, begin=addr, end=addr)
    return m, at


class PacketsTest(unittest.TestCase):
    def test_channel_messages(self):
        self.assertEqual(usbmidi.packets([0x93, 60, 100]),
                         bytes([0x09, 0x93, 60, 100]))
        self.assertEqual(usbmidi.packets([0xC2, 5]), bytes([0x0C, 0xC2, 5, 0]))

    def test_system(self):
        self.assertEqual(usbmidi.packets([0xF8]), bytes([0x0F, 0xF8, 0, 0]))
        self.assertEqual(usbmidi.packets([0xF2, 1, 2]), bytes([0x03, 0xF2, 1, 2]))
        self.assertEqual(usbmidi.packets([0xF3, 1]), bytes([0x02, 0xF3, 1, 0]))

    def test_sysex_endings(self):
        # Ends with one, two and three bytes in the last packet.
        self.assertEqual(usbmidi.packets([0xF0, 1, 2, 0xF7]),
                         bytes([0x04, 0xF0, 1, 2, 0x05, 0xF7, 0, 0]))
        self.assertEqual(usbmidi.packets([0xF0, 1, 2, 3, 0xF7]),
                         bytes([0x04, 0xF0, 1, 2, 0x06, 3, 0xF7, 0]))
        self.assertEqual(usbmidi.packets([0xF0, 1, 0xF7]),
                         bytes([0x07, 0xF0, 1, 0xF7]))

    def test_cable(self):
        self.assertEqual(usbmidi.packets([0x80, 1, 2], cable=1)[0], 0x18)


class UsbMidiTest(unittest.TestCase):
    def setUp(self):
        self.m, at = _machine()
        self.out = []
        self.usb = usbmidi.UsbMidi(sink=self.out.append)
        self.assertTrue(self.usb.install(self.m, at, _profile()))
        self.posts = []

        def defer(uc, a, s, d):
            sp = uc.reg_read(UC_M68K_REG_A7)
            self.posts.append(struct.unpack('>IIII', uc.mem_read(sp, 16)))
        at(DEFER, defer)

    def _run(self, pc, count):
        self.m.uc.emu_start(pc, 0, count=count)
        return self.m.uc.reg_read(UC_M68K_REG_PC)

    def test_post_from_idle_leaves_the_stack(self):
        self.usb.put(bytes([0x90, 60, 100]))
        # The spin posts (the poster is an rts), the post returns to the
        # spin, and there its three arguments are dropped.
        pc = self._run(SPIN, 4)
        self.assertEqual(pc, SPIN)
        self.assertEqual(self.m.uc.reg_read(UC_M68K_REG_A7), STACK)
        self.assertEqual(len(self.posts), 1)
        ret, fn, n, buf = self.posts[0]
        self.assertEqual((ret, fn, n), (SPIN, PARSE, 4))
        self.assertEqual(bytes(self.m.uc.mem_read(buf, 4)),
                         bytes([0x09, 0x90, 60, 100]))

    def test_at_most_one_transfer_per_post(self):
        self.usb.put(bytes([0xF0] + [1] * 600 + [0xF7]))
        self._run(SPIN, 2)
        self.assertEqual(self.posts[0][2], usbmidi.PACKETS * 4)
        self.assertTrue(self.usb.pending)

    def test_sender_returns_to_its_caller(self):
        msg = bytes([0xF0, 0, 0x20, 0x3C, 0xF7])
        self.m.uc.mem_write(STACK - 0x80, msg)
        self.m.uc.mem_write(STACK - 12, struct.pack('>III', SPIN, len(msg),
                                                    STACK - 0x80))
        self.m.uc.reg_write(UC_M68K_REG_A7, STACK - 12)
        pc = self._run(SEND, 1)
        self.assertEqual(pc, SPIN)
        self.assertEqual(self.m.uc.reg_read(UC_M68K_REG_A7), STACK - 8)
        self.assertEqual(self.out, [msg])


if __name__ == '__main__':
    unittest.main()
