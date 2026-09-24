"""emu/harness.py's DDR model and the snapshot rules that go with it; the
VBR the firmware set; the ISA hooks surviving code that is replaced."""
import os
import struct
import tempfile
import unittest

from unicorn.m68k_const import (UC_M68K_REG_A7, UC_M68K_REG_D0,
                                UC_M68K_REG_PC, UC_M68K_REG_SR)

from emu import snapshot
from emu.harness import PAGE, Machine

MB = 1 << 20


class DdrTest(unittest.TestCase):
    def test_aliases_are_one_memory(self):
        m = Machine(ddr=64 * MB)
        m.ensure(0x40000000)
        m.uc.mem_write(0x40000010, b'abcd')
        for alias in (0x44000010, 0x48000010, 0x4C000010, 0x7C000010):
            m.ensure(alias)
            self.assertEqual(bytes(m.uc.mem_read(alias, 4)), b'abcd')
        self.assertEqual(m.ddr_physical(0x4BBAF630), 0x03BAF630)
        self.assertIsNone(m.ddr_physical(0x80000000))

    def test_guest_writes_through_one_alias_read_through_another(self):
        m = Machine(ddr=64 * MB)
        m.ensure(0x40200000)
        # move.l #$11223344,d0 ; move.l d0,$44000020 ; move.l $40000020,d1
        code = bytes.fromhex('203c11223344' '23c044000020' '223940000020'
                             '4e71')
        m.uc.mem_write(0x40200000, code)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        m.uc.emu_start(0x40200000, 0x40200000 + len(code) - 2)
        from unicorn.m68k_const import UC_M68K_REG_D1
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_D1), 0x11223344)

    def test_without_the_model_aliases_are_separate(self):
        m = Machine()
        m.ensure(0x40000000)
        m.ensure(0x44000000)
        m.uc.mem_write(0x40000010, b'abcd')
        self.assertEqual(bytes(m.uc.mem_read(0x44000010, 4)), bytes(4))

    def test_refused_late_or_odd(self):
        m = Machine()
        m.ensure(0x40000000)
        with self.assertRaises(RuntimeError):
            m.set_ddr(64 * MB)
        with self.assertRaises(ValueError):
            Machine(ddr=48 * MB)


class VbrTest(unittest.TestCase):
    def test_raise_vector_uses_the_firmware_vbr(self):
        m = Machine()
        m.ensure(0x80000000)
        m.ensure(0x40000000)
        m.ctlregs[0x801] = 0x80000000
        m.uc.mem_write(0x80000000 + 70 * 4, struct.pack('>I', 0x80000414))
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        m.uc.reg_write(UC_M68K_REG_A7, 0x8000FF00)
        self.assertTrue(m.raise_vector(70, level=3))
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), 0x80000414)

    def test_an_empty_slot_is_no_handler(self):
        m = Machine()
        m.ensure(0x40000000)
        self.assertFalse(m.raise_vector(70))


class IsaHookTest(unittest.TestCase):
    def test_a_movec_hook_leaves_replaced_code_alone(self):
        """The bootstrap loads its updater over itself: a MOVEC hook left at
        an address that now holds another instruction must not fire."""
        image = bytes.fromhex('203c80000000' '4e7b0801' '4e71')
        m = Machine()
        m.ensure(0x80000000)
        m.uc.mem_write(0x80000400, image)
        m.install_isa_patches_scoped(image, 0x80000400)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        m.uc.emu_start(0x80000400, 0x80000400 + len(image) - 2)
        self.assertEqual(m.ctlregs.get(0x801), 0x80000000)
        # Replace the MOVEC with moveq #7,d0 ; nop and run again.
        m.uc.mem_write(0x80000406, bytes.fromhex('7007' '4e71'))
        m.ctlregs.clear()
        m.uc.emu_start(0x80000406, 0x80000400 + len(image) - 2)
        self.assertNotIn(0x801, m.ctlregs)
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_D0), 7)


def fresh(ddr=None):
    """A Machine as the emulator leaves one: SR set. (Reading SR before it
    was ever written or run aborts inside Unicorn, and snapshot.save reads
    it.)"""
    m = Machine(ddr=ddr)
    m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
    return m


class SnapshotDdrTest(unittest.TestCase):
    def _save(self, m):
        fd, path = tempfile.mkstemp(suffix='.snap')
        os.close(fd)
        self.addCleanup(os.remove, path)
        snapshot.save(m, path)
        return path

    def test_round_trip_under_the_model(self):
        m = fresh(64 * MB)
        m.ensure(0x40000000)
        m.ensure(0x48000000)
        m.uc.mem_write(0x48000100, b'wxyz')
        path = self._save(m)
        n = fresh(64 * MB)
        snapshot.restore_into(n, path, {'seen': set(), 'n': 0})
        self.assertEqual(bytes(n.uc.mem_read(0x40000100, 4)), b'wxyz')

    def test_a_snapshot_whose_aliases_disagree_is_refused(self):
        m = fresh()
        m.ensure(0x40000000)
        m.ensure(0x44000000)
        m.uc.mem_write(0x40000000, b'one!')
        m.uc.mem_write(0x44000000, b'two!')
        path = self._save(m)
        n = fresh(64 * MB)
        with self.assertRaises(RuntimeError):
            snapshot.restore_into(n, path, {'seen': set(), 'n': 0})

    def test_only_bit_exact_shortcuts_relax(self):
        saved = {'softfloat': True, 'bitmap': True, 'unblock': True}
        now = {'softfloat': False, 'bitmap': False, 'unblock': True}
        snapshot._validate_manifest(saved, now, relax=('softfloat', 'bitmap'))
        with self.assertRaises(RuntimeError):
            snapshot._validate_manifest(saved, now)
        with self.assertRaises(ValueError):
            snapshot._validate_manifest(saved, now, relax=('unblock',))
        with self.assertRaises(ValueError):
            snapshot._validate_manifest(saved, now, relax=('ddr',))


if __name__ == '__main__':
    unittest.main()
