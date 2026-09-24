"""emu/cftiming.py: the MCF54418RM timing tables, the change-of-flow models,
and the cycle clock on real (hand-assembled) code."""
import struct
import unittest

from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_D0, UC_M68K_REG_SR

from emu import cfisa, cftiming
from emu.harness import VBR, Machine


def dec(hexstr):
    data = bytes.fromhex(hexstr) + bytes(8)
    return cfisa.decode(cfisa.reader(data, 0)(0), 0)


class TableTest(unittest.TestCase):
    """Figures straight from Tables 3-13 to 3-20."""

    def cost(self, hexstr, accel_max=True):
        return cftiming.static_cost(dec(hexstr), accel_max)

    def test_moves(self):
        self.assertEqual(self.cost('2200'), 1)          # Dy -> Rx
        self.assertEqual(self.cost('2210'), 1)          # (Ay) -> Rx
        self.assertEqual(self.cost('2280'), 1)          # Dy -> (Ax)
        self.assertEqual(self.cost('2290'), 2)          # (Ay) -> (Ax)
        self.assertEqual(self.cost('22300c00'), 2)      # (d8,Ay,Xi) -> Rx
        self.assertEqual(self.cost('23c080007706'), 1)  # Dy -> xxx.l

    def test_arithmetic(self):
        self.assertEqual(self.cost('d081'), 1)          # add.l d1,d0
        self.assertEqual(self.cost('4c410800'), 35)     # divs.l
        self.assertEqual(self.cost('81c1'), 20)         # divs.w
        self.assertEqual(self.cost('4c002800'), 4)      # muls.l
        self.assertEqual(self.cost('c1f00c00'), 5)      # muls.w (d8,a0,d0.l*4)
        self.assertEqual(self.cost('08c0000c'), 2)      # bset #n,d0
        self.assertEqual(self.cost('08000004'), 1)      # btst #n,d0

    def test_misc(self):
        self.assertEqual(self.cost('4e71'), 6)          # nop
        self.assertEqual(self.cost('4e40'), 18)         # trap
        self.assertEqual(self.cost('4e73'), 15)         # rte
        self.assertEqual(self.cost('4e560000'), 2)      # link
        self.assertEqual(self.cost('48d70c1c'), 5)      # movem, 5 registers
        self.assertEqual(self.cost('4e7b0801'), 20)     # movec
        self.assertEqual(self.cost('46fc0700'), 4)      # move #imm,sr, S clear
        self.assertEqual(self.cost('46fc2700'), 1)      # imm[13] (S) set: 1
        self.assertEqual(self.cost('46fc2000'), 1)      # ... with imm[13] set

    def test_flow_ranges(self):
        self.assertEqual(self.cost('4eb98000223c'), 3)          # jsr abs, max
        self.assertEqual(self.cost('4eb98000223c', False), 1)   # ... min
        self.assertEqual(self.cost('4e92'), 5)                  # jsr (a2)
        self.assertEqual(self.cost('4efb0802'), 6)              # jmp idx
        self.assertEqual(self.cost('60fe'), 3)
        self.assertEqual(self.cost('6602'), 0)   # all of it is the prediction

    def test_stall(self):
        prev = dec('7005')                       # moveq #5,d0 (writes d0)
        self.assertEqual(cftiming.stall(prev.writes, dec('22300c00')), 3)
        self.assertEqual(cftiming.stall(prev.writes, dec('22300800')), 2)
        self.assertEqual(cftiming.stall(prev.writes, dec('2210')), 0)
        load_a0 = dec('2050')                    # movea.l (a0),a0
        self.assertEqual(cftiming.stall(load_a0.writes, dec('2210')), 2)


class PredictorTest(unittest.TestCase):
    def test_a_loop_branch_folds_once_learned(self):
        p = cftiming.Predictor()
        costs = [p.branch(0x100, 0xF0, True) for _ in range(10)]
        self.assertEqual(costs[0], cftiming.BCC_MISPREDICTED)  # weakly not-taken
        self.assertEqual(costs[-1], cftiming.BCC_FOLDED)
        self.assertEqual(p.branch(0x100, 0xF0, False),
                         cftiming.BCC_MISPREDICTED)

    def test_return_stack(self):
        p = cftiming.Predictor()
        self.assertEqual(p.ret(0x10), cftiming.RTS_UNPREDICTED)
        p.call(0x20)
        self.assertEqual(p.ret(0x20), cftiming.RTS_PREDICTED)
        p.call(0x30)
        self.assertEqual(p.ret(0x34), cftiming.RTS_MISPREDICTED)
        for a in range(6):                       # four entries, LIFO
            p.call(a)
        self.assertEqual([p.ret(a) for a in (5, 4, 3, 2)],
                         [cftiming.RTS_PREDICTED] * 4)
        self.assertEqual(p.ret(1), cftiming.RTS_UNPREDICTED)


CODE = 0x40000400
# moveq #N,d0 ; loop: subq.l #1,d0 ; bne.b loop ; stop at end
LOOP = bytes.fromhex('7005' '5380' '66fc' '4e71')
HANDLER = 0x40001000


def machine(code=LOOP):
    m = Machine()
    m.ensure(0x40000000)
    m.uc.mem_write(CODE, code)
    m.install_exceptions()
    m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
    m.uc.reg_write(UC_M68K_REG_A7, 0x40080000)
    return m


class ClockTest(unittest.TestCase):
    def test_loop_cycles(self):
        m = machine()
        clock = cftiming.CycleClock(m)
        m.uc.emu_start(CODE, CODE + len(LOOP) - 2)
        self.assertEqual(clock.decode_mismatches, 0)
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_D0), 0)
        # moveq 1, five passes of subq (1) + bne, then the exit bne. The
        # first taken bne mispredicts (8), the rest are learned: at most
        # 1 + 5 + 5 * 8 cycles, at least 1 + 5 + 8.
        self.assertGreaterEqual(clock.cycles, 1 + 5 + 8)
        self.assertLessEqual(clock.cycles, 1 + 5 + 5 * 8)
        counts = clock.predictor.counts
        self.assertGreaterEqual(counts['mispredicted'], 1)

    def test_budget_stops_the_run(self):
        m = machine(bytes.fromhex('60fe'))       # bra.b *
        clock = cftiming.CycleClock(m)
        stepper = cftiming.CycleStepper(clock)
        ran = stepper.run(CODE, 100)
        self.assertGreaterEqual(ran, 100)
        self.assertLess(ran, 110)

    def test_interrupt_accounting(self):
        # handler: moveq #1,d1 ; moveq #2,d2 ; rte
        m = machine(bytes.fromhex('60fe'))
        m.uc.mem_write(HANDLER, bytes.fromhex('7201' '7402' '4e73'))
        m.uc.mem_write(VBR + 64 * 4, struct.pack('>I', HANDLER))
        clock = cftiming.CycleClock(m)
        stepper = cftiming.CycleStepper(clock)
        stepper.run(CODE, 20)
        from unicorn.m68k_const import UC_M68K_REG_PC
        m.uc.reg_write(UC_M68K_REG_PC, CODE)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertTrue(m.raise_vector(64, level=5))
        stepper.run(m.uc.reg_read(UC_M68K_REG_PC), 60)
        stats = clock.vectors[64]
        self.assertEqual(stats.count, 1)
        # 1 + 1 + rte 15, measured from entry (interrupt entry is charged
        # before the frame opens).
        self.assertEqual(stats.wall_max, 17)
        self.assertEqual(clock.entry_cycles, cftiming.EXCEPTION_ENTRY)
        rep = cftiming.deadline_report(clock, 64, 34)
        self.assertEqual(rep['late'], 0)
        self.assertAlmostEqual(rep['margin'], 0.5)

    def test_idle_cycles_are_counted_apart(self):
        m = machine(bytes.fromhex('60fe'))
        clock = cftiming.CycleClock(m, idle={CODE})
        cftiming.CycleStepper(clock).run(CODE, 50)
        self.assertEqual(clock.idle_cycles, clock.cycles)
        self.assertEqual(clock.report()['busy_fraction'], 0)


if __name__ == '__main__':
    unittest.main()
