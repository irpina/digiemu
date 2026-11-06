# pyright: reportMissingImports=false
"""emu/idletick.py: the idle-spin reschedule for a patch's own idle loop.

No firmware: a scheduler laid out as the mk1 builds lay it out (a current-TCB
word just above the per-priority ready heads, each TCB's +0x08 pointing at
its own priority's head), vector 32 pointing into the image.
"""
import struct
import unittest
from unittest import mock

from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR

from emu import idletick
from emu.harness import VBR, Machine

LOAD, IMAGE_LEN = 0x40000400, 0x00100000
HANDLER = 0x40000410               # in the image, like the RTOS's switcher
HEADS = 0x41000000
CURRENT = HEADS + 16 * 4           # current_tcb sits just above the heads
TCB = 0x41001000
LOOP = 0x43EC1764                  # a patch's idle loop, past the image
STOCK = 0x4008D3CC                 # the stock spin, inside the image
STACK = 0x40200000


def _machine(pc=LOOP, sr=0x2000, prio=1, ready=(0, 1)):
    m = Machine()
    for addr in (VBR, HANDLER, HEADS, TCB, STACK - 0x100, LOOP):
        m.ensure(addr)
    m.uc.mem_write(VBR + idletick.VECTOR * 4, struct.pack('>I', HANDLER))
    m.uc.mem_write(CURRENT, struct.pack('>I', TCB))
    m.uc.mem_write(TCB + idletick.PRIO_SLOT_OFF,
                   struct.pack('>I', HEADS + 4 * prio))
    for p in ready:
        m.uc.mem_write(HEADS + 4 * p, struct.pack('>I', TCB + 0x100 * p))
    m.uc.reg_write(UC_M68K_REG_SR, sr)
    m.uc.reg_write(UC_M68K_REG_A7, STACK)
    m.uc.reg_write(UC_M68K_REG_PC, pc)
    return m


def _tick(m):
    return idletick.IdleTick(m, CURRENT, HEADS, (LOAD, LOAD + IMAGE_LEN))


class IdleTickTest(unittest.TestCase):
    def test_the_patch_loop_alone_is_idle_and_rescheduled(self):
        m = _machine()
        tick = _tick(m)
        self.assertEqual(tick.sample(), 'idle')
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), HANDLER)
        # The frame resumes the loop where it was.
        frame = bytes(m.uc.mem_read(STACK - 8, 8))
        self.assertEqual(struct.unpack('>I', frame[4:])[0], LOOP)
        self.assertEqual((tick.yields, tick.samples['idle']), (1, 1))

    def test_a_ready_task_above_makes_it_busy_and_still_reschedules(self):
        m = _machine(ready=(0, 1, 6))
        tick = _tick(m)
        self.assertEqual(tick.sample(), 'busy')
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), HANDLER)

    def test_a_higher_task_running_patch_code_is_not_idle(self):
        # The UI task (priority 2) in a mod's code, the init task and the
        # RTOS's priority-0 task ready below it.
        m = _machine(prio=2, ready=(0, 1, 2))
        self.assertEqual(_tick(m).sample(), 'busy')

    def test_stock_code_is_left_alone(self):
        for pc, sr in ((STOCK, 0x2000), (0x80008000, 0x2000),
                       (LOOP, 0x2700), (LOOP, 0x2400)):
            m = _machine(pc=pc, sr=sr)
            count = {'idle_loop_yield': 0}
            tick = idletick.IdleTick(m, CURRENT, HEADS,
                                     (LOAD, LOAD + IMAGE_LEN), count=count)
            self.assertIsNone(tick.step(0))
            self.assertIsNone(tick.sample())
            self.assertFalse(tick.service(0))
            self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), pc)
            self.assertEqual(count['idle_loop_yield'], 0)

    def test_nothing_before_the_scheduler_is_up(self):
        m = _machine()
        m.uc.mem_write(CURRENT, b'\0\0\0\0')
        self.assertIsNone(_tick(m).sample())
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), LOOP)

    def test_steps_are_cut_only_in_the_loop(self):
        self.assertEqual(_tick(_machine()).step(0), idletick.EVERY)
        self.assertIsNone(_tick(_machine(pc=STOCK)).step(0))

    def test_install_needs_the_scheduler_symbols(self):
        m = _machine()
        prof = {'current_tcb': CURRENT, 'prio_heads': HEADS}
        tick = idletick.IdleTick.install(m, prof, IMAGE_LEN, LOAD)
        self.assertEqual((tick.slots, tick.lo, tick.hi),
                         (16, LOAD, LOAD + IMAGE_LEN))
        for missing in ('current_tcb', 'prio_heads'):
            part = {k: v for k, v in prof.items() if k != missing}
            self.assertIsNone(idletick.IdleTick.install(m, part, IMAGE_LEN,
                                                        LOAD))


if __name__ == '__main__':
    unittest.main()
