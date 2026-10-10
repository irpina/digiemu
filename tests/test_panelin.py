"""emu/panelin.py: panel input through UART8's eDMA channel 34 ring and vector 154.

No firmware: the ring pointer and channel 34's write pointer point at a ring,
vector 154 points at an `rte`, and INTC1 source 26 is armed at level 3, as on
both mk1s.
"""
import struct
import types
import unittest

from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR

from emu import panelin, pit
from emu.harness import VBR, Machine

HANDLER = 0x40100000
STACK = 0x40200000
RING = 0x40300000
RING_PTR = 0x40300800
INTC1 = 0xFC04C000
SOURCE = panelin.RX_VECTOR - 128
PROFILE = types.SimpleNamespace(uart8_ring_ptr=RING_PTR)


def _machine(level=3):
    m = Machine()
    for addr in (VBR, HANDLER, STACK - 0x100, INTC1, panelin.TCD34_DADDR, RING):
        m.ensure(addr)
    m.uc.mem_write(VBR + panelin.RX_VECTOR * 4, struct.pack('>I', HANDLER))
    m.uc.mem_write(HANDLER, b'\x4e\x73')                     # rte
    m.uc.mem_write(INTC1 + pit.ICR_BASE + SOURCE, bytes([level]))
    m.uc.mem_write(RING_PTR, struct.pack('>I', RING))
    m.uc.mem_write(panelin.TCD34_DADDR, struct.pack('>I', RING))
    m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
    m.uc.reg_write(UC_M68K_REG_A7, STACK)
    return m


def _panel(m, **kw):
    p = panelin.PanelIn(**kw)
    p.attach(m, PROFILE)
    return p


def _ipl(m):
    return (m.uc.reg_read(UC_M68K_REG_SR) >> 8) & 7


class TakeLevelTest(unittest.TestCase):
    def test_below_the_mask_only(self):
        m = _machine()
        for sr, level in ((0x2000, 3), (0x2200, 3), (0x2300, None),
                          (0x2600, None)):
            m.uc.reg_write(UC_M68K_REG_SR, sr)
            self.assertEqual(pit.take_level(m, panelin.RX_VECTOR), level)

    def test_not_armed(self):
        self.assertIsNone(pit.take_level(_machine(level=0), panelin.RX_VECTOR))

    def test_masked_in_the_intc(self):
        m = _machine()
        m.uc.mem_write(INTC1 + pit.IMR_BASE + 4, struct.pack('>I', 1 << SOURCE))
        self.assertIsNone(pit.take_level(m, panelin.RX_VECTOR))


class PanelInTest(unittest.TestCase):
    def test_delivered_at_its_level_with_its_whats(self):
        m = _machine()
        seen = []
        p = _panel(m, on_feed=seen.append)
        p.put(panelin.encode_buttons(1, 0x08), 'press A')
        p.put(panelin.encode_buttons(1, 0x00), 'release A')
        self.assertEqual(p.step(0), 1)
        self.assertTrue(p.service(0))
        self.assertEqual(bytes(m.uc.mem_read(RING, 4)), b'\x21\x08\x21\x00')
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), HANDLER)
        self.assertEqual(_ipl(m), 3)
        self.assertEqual(seen, [['press A', 'release A']])
        self.assertEqual((p.pending, p.delivered), (0, 4))
        self.assertIsNone(p.step(0))

    def test_held_back_while_the_cpu_masks_it(self):
        m = _machine()
        m.uc.reg_write(UC_M68K_REG_SR, 0x2600)
        p = _panel(m)
        p.put(b'\x21\x08')
        self.assertEqual(p.step(0), pit.PENDING_STEP)
        self.assertFalse(p.service(0))
        self.assertEqual((p.pending, p.deferred), (2, 1))
        self.assertEqual(bytes(m.uc.mem_read(RING, 2)), b'\x00\x00')
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertTrue(p.service(0))
        self.assertEqual(bytes(m.uc.mem_read(RING, 2)), b'\x21\x08')

    def test_while_the_render_holds_the_mask_its_end_decides(self):
        m = _machine()
        m.uc.reg_write(UC_M68K_REG_SR, 0x2600)
        m.render_ipl, m.render_since = 6, 0
        p = _panel(m)
        p.put(b'\x21\x08')
        self.assertIsNone(p.step(0))
        self.assertTrue(m.render_waiting)

    def test_not_armed_waits_without_polling(self):
        m = _machine(level=0)
        p = _panel(m)
        p.put(b'\x21\x08')
        self.assertIsNone(p.step(0))
        self.assertFalse(p.service(0))
        self.assertEqual(p.pending, 2)

    def test_nothing_pending(self):
        p = _panel(_machine())
        self.assertIsNone(p.step(0))
        self.assertFalse(p.service(0))
        self.assertEqual(p.deferred, 0)

    def test_not_attached(self):
        p = panelin.PanelIn()
        p.put(b'\x21\x08')
        self.assertIsNone(p.step(0))
        self.assertFalse(p.service(0))


class FeedTest(unittest.TestCase):
    def test_raises_at_once_leaving_the_mask(self):
        m = _machine()
        m.uc.reg_write(UC_M68K_REG_SR, 0x2600)
        pc = panelin.feed(m, PROFILE, panelin.encode_buttons(1, 0x08))
        self.assertEqual(bytes(m.uc.mem_read(RING, 2)), b'\x21\x08')
        self.assertEqual(pc, HANDLER)
        self.assertEqual(_ipl(m), 6)


if __name__ == '__main__':
    unittest.main()
