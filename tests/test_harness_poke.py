"""emu/harness.py Machine.poke/peek: writing guest memory from inside a hook.

A memory hook runs in the middle of the guest's own access, and mapping a
page there resizes Unicorn's TLB under it: the Digitone's first EXT_CSD read,
whose buffer page nothing had touched yet, crashed the host with an access
violation at the guest address. poke() never maps: a write to an unmapped
page waits in `pending` until the page is mapped -- by the guest's first
touch, ensure() or flush_pending() -- and peek() sees it meanwhile.
"""
import struct
import unittest

from unicorn import UC_HOOK_MEM_WRITE

from emu.harness import PAGE, Machine

CODE = 0x40000000
TRIGGER = 0x40000800           # the guest stores here; the hook pokes
FAR = 0x43000000               # a page nothing has touched


class PokePeekTest(unittest.TestCase):
    def test_mapped_page_is_written_at_once(self):
        m = Machine()
        m.ensure(CODE)
        m.poke(CODE + 0x10, b'abcd')
        self.assertEqual(bytes(m.uc.mem_read(CODE + 0x10, 4)), b'abcd')
        self.assertEqual(m.pending, {})

    def test_unmapped_page_waits_and_peek_sees_it(self):
        m = Machine()
        m.poke(FAR + 4, b'\x12\x34')
        self.assertNotIn(FAR, m.mapped)
        self.assertEqual(m.peek(FAR, 8), b'\0\0\0\0\x12\x34\0\0')
        m.ensure(FAR)
        self.assertEqual(bytes(m.uc.mem_read(FAR + 4, 2)), b'\x12\x34')
        self.assertEqual(m.pending, {})

    def test_a_write_across_pages_splits(self):
        m = Machine()
        m.ensure(FAR)
        m.poke(FAR + PAGE - 2, b'wxyz')
        self.assertEqual(bytes(m.uc.mem_read(FAR + PAGE - 2, 2)), b'wx')
        self.assertEqual(m.peek(FAR + PAGE - 2, 4), b'wxyz')
        m.flush_pending()
        self.assertEqual(bytes(m.uc.mem_read(FAR + PAGE, 2)), b'yz')

    def test_from_inside_a_write_hook(self):
        """The case that crashed: a hook writes to a page nobody touched,
        then the guest reads it and sees the data."""
        m = Machine()
        m.ensure(CODE)

        def hook(uc, access, address, size, value, data):
            m.poke(FAR, struct.pack('>I', 0xCAFEF00D))
        m.uc.hook_add(UC_HOOK_MEM_WRITE, hook, begin=TRIGGER,
                      end=TRIGGER + 3)
        # clr.l TRIGGER; move.l FAR,d0; move.l d0,TRIGGER+4
        code = (b'\x42\xb9' + struct.pack('>I', TRIGGER)
                + b'\x20\x39' + struct.pack('>I', FAR)
                + b'\x23\xc0' + struct.pack('>I', TRIGGER + 4))
        m.uc.mem_write(CODE, code)
        m.uc.emu_start(CODE, CODE + len(code))
        self.assertEqual(bytes(m.uc.mem_read(TRIGGER + 4, 4)),
                         struct.pack('>I', 0xCAFEF00D))

    def test_snapshot_save_flushes(self):
        import os
        import tempfile
        from emu import snapshot
        from unicorn.m68k_const import UC_M68K_REG_SR
        m = Machine()
        m.ensure(CODE)
        # Reading SR back from an engine whose condition codes were never
        # set aborts inside Unicorn; a real run always has set them.
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        m.poke(FAR, b'kept')
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 's.snap')
            snapshot.save(m, path)
            m2, _extra, _regs = snapshot.restore(path)
            self.assertEqual(bytes(m2.uc.mem_read(FAR, 4)), b'kept')


if __name__ == '__main__':
    unittest.main()
