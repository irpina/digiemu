"""emu/strict.py: what the MCF5441x would not tolerate, caught."""
import struct
import unittest

from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR

from emu import cftiming, strict
from emu.harness import Machine

CODE = 0x40000400


def machine(code, **kw):
    m = Machine()
    m.ensure(0x40000000)
    m.uc.mem_write(CODE, code)
    m.install_exceptions()
    m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
    m.uc.reg_write(UC_M68K_REG_A7, 0x40080000)
    return m, strict.Strict(m, **kw)


def run(m, code):
    m.uc.emu_start(CODE, CODE + len(code), count=200)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.s = machine(b'\x4e\x71')[1]

    def test_memory_map(self):
        c = self.s.classify
        self.assertTrue(c(0x40000000)[0])        # DDR
        self.assertTrue(c(0x4BBAF630)[0])        # DDR, an uncached alias
        self.assertTrue(c(0x8000FFFC)[0])        # SRAM
        self.assertFalse(c(0x80010000)[0])       # past RAMBAR's 64 KB
        self.assertTrue(c(0x8C000000)[0])        # Rapid GPIO
        self.assertFalse(c(0x90000000)[0])       # reserved
        self.assertFalse(c(0x00000000)[0])       # FlexBus, no chip select
        self.assertFalse(c(0xC0000000)[0])
        self.assertEqual(c(0xFC05C02C), (True, 'DSPI 0'))
        self.assertEqual(c(0xEC070004), (True, 'UART8'))
        self.assertFalse(c(0xFC00C000)[0])       # slot 3: not in Table 1-3
        self.assertFalse(c(0xE0000000)[0])       # below both controllers
        self.assertTrue(c(0x10000010)[0])        # the emulator's own page

    def test_flexbus_chip_select(self):
        s = self.s
        s._flexbus_write(strict.FLEXBUS + 0, 4, 0x00000000)     # CSAR0
        s._flexbus_write(strict.FLEXBUS + 4, 4, 0x000F0001)     # CSMR0: 1 MB, V
        self.assertTrue(s.classify(0x00001000)[0])
        self.assertFalse(s.classify(0x00200000)[0])


class RunTest(unittest.TestCase):
    def test_a_reserved_slot_stops_the_run(self):
        # move.l d0,$FC00C000 ; move.l d0,$40001000
        code = bytes.fromhex('23c0fc00c000' '23c040001000')
        m, s = machine(code)
        run(m, code)
        self.assertEqual(len(s.violations), 1)
        v = next(iter(s.violations.values()))
        self.assertEqual(v['kind'], 'memory')
        self.assertIn('reserved peripheral slot', v['what'])
        self.assertIn('0xfc00c000', s.halted)
        # stopped before the second store
        self.assertEqual(bytes(m.uc.mem_read(0x40001000, 4)), bytes(4))

    def test_keep_going_records_every_one(self):
        code = bytes.fromhex('23c0fc00c000' '23c090000000' '4e71')
        m, s = machine(code, stop=False)
        run(m, code)
        self.assertEqual(len(s.violations), 2)
        self.assertIsNone(s.halted)

    def test_peripheral_use_is_counted(self):
        code = bytes.fromhex('2039fc05c02c' '23c0fc0b0000' '4e71')
        m, s = machine(code, stop=False)
        run(m, code)
        rep = s.report()
        self.assertTrue(rep['passed'])
        names = {e['peripheral'] for e in rep['unmodelled_peripherals']}
        self.assertIn('USB On-the-Go', names)
        modelled = {e['peripheral'] for e in rep['modelled_peripherals']}
        self.assertIn('DSPI 0', modelled)

    def test_a_float_instruction(self):
        code = bytes.fromhex('4e71' 'f2000000' '4e71')
        m, s = machine(code)
        run(m, code)
        kinds = [v['what'] for v in s.violations.values()]
        self.assertTrue(any('no FPU' in w for w in kinds), kinds)

    def test_code_outside_ddr_and_sram(self):
        m, s = machine(bytes.fromhex('4ef9c0000000'))     # jmp $C0000000
        m.ensure(0xC0000000)
        m.uc.mem_write(0xC0000000, bytes.fromhex('4e71' '4e71'))
        m.uc.emu_start(CODE, 0xC0000004, count=10)
        self.assertIn(('execute', 0xC0000000), s.violations)

    def test_error_exception(self):
        m, s = machine(bytes.fromhex('4e71'))
        m.uc.mem_write(0x40000000 + 4 * 4, struct.pack('>I', CODE))
        self.assertTrue(m.raise_vector(4, from_instruction=True))
        self.assertTrue(any(v['kind'] == 'exception'
                            for v in s.violations.values()))

    def test_unhandled_vector(self):
        m, s = machine(bytes.fromhex('4e71'))
        m.halt_vec = 61
        s.check_halt()
        self.assertIn(('unhandled', 61), s.violations)

    def test_shares_the_cycle_clock(self):
        code = bytes.fromhex('4e71' 'f2000000')
        m = Machine()
        m.ensure(0x40000000)
        m.uc.mem_write(CODE, code)
        m.install_exceptions()
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        clock = cftiming.CycleClock(m)
        s = strict.Strict(m, clock=clock, stop=False)
        m.uc.emu_start(CODE, CODE + len(code), count=5)
        self.assertEqual(s.blocks_checked, 1)
        self.assertTrue(s.violations)


class WatchdogTest(unittest.TestCase):
    def test_an_unserviced_watchdog(self):
        now = [0]
        s = machine(b'\x4e\x71', cycles=lambda: now[0], stop=False)[1]
        s._cwcr_write(0x80 | 10)                 # enabled, 2^10 cycles
        now[0] = 500
        s.check_watchdog()
        self.assertFalse(s.violations)
        s._cwsr_write(0x55)
        s._cwsr_write(0xAA)
        now[0] = 1400
        s.check_watchdog()
        self.assertFalse(s.violations)
        now[0] = 3000
        s.check_watchdog()
        self.assertIn(('watchdog',), s.violations)


class BaselineTest(unittest.TestCase):
    def test_stock_violations_do_not_fail(self):
        code = bytes.fromhex('23c0fc00c000' '4e71')
        m, s = machine(code, stop=False)
        run(m, code)
        rep = s.report()
        self.assertFalse(rep['passed'])
        again = s.report(baseline=rep)
        self.assertTrue(again['passed'])
        self.assertTrue(again['violations'][0]['in_stock'])


class StandInTest(unittest.TestCase):
    def test_from_build_and_from_the_cold_boot(self):
        ev = {'stand_ins': {'idle_yield': 3}, 'satisfied': 2,
              'depack_clamps': 0, 'softfloat': {'addsf3': 5}}
        out = strict.stand_ins(ev)
        self.assertEqual(out['idle_yield'], 3)
        self.assertEqual(out['unblock_satisfied'], 2)
        self.assertEqual(out['softfloat_shortcuts'], 5)
        st = {'reads': [(0, 1, 2)] * 4, 'sem_kicks': 1, 'depack_clamps': 0,
              'spin': 7}
        self.assertEqual(strict.stand_ins(st)['flash_read'], 4)


if __name__ == '__main__':
    unittest.main()
