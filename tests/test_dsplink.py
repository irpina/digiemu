"""emu/dsplink.py: the Digitone's second CPU, its handshake and the CPU itself.

No firmware: the DSP here is a dozen instructions laid out where section 7's
entry, idle loop and render interrupt are (0x40000b92, 0x40000b90, vector
96), which is all DspCpu relies on. The main CPU is a bare Machine with the
edge-port vector armed; its GPIO writes are real guest stores, so the hooks
see exactly what the firmware's would.
"""
import struct
import unittest

from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR

from emu import dsplink, pit
from emu.harness import VBR, Machine

CODE = 0x40001000
MAIN_HANDLER = 0x40100000
STACK = 0x40200000
INTC0 = 0xFC048000
DSP_HANDLER = 0x40000c00
COUNTER = 0x100                 # a longword in the shared RAM


def _main():
    """The main CPU: vector 68 (edge port pin 4) at level 6, IPL 0."""
    m = Machine()
    for addr in (VBR, CODE, MAIN_HANDLER, STACK - 0x100, INTC0,
                 dsplink.GPIO, dsplink.EPORT):
        m.ensure(addr)
    m.uc.mem_write(VBR + dsplink.VECTOR * 4, struct.pack('>I', MAIN_HANDLER))
    m.uc.mem_write(INTC0 + pit.ICR_BASE + dsplink.EPORT_PIN, bytes([6]))
    m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
    m.uc.reg_write(UC_M68K_REG_A7, STACK)
    return m


def _guest_byte(m, addr, value):
    """Run `move.b #value,addr.l` as main-CPU guest code."""
    m.uc.mem_write(CODE, b'\x13\xfc' + struct.pack('>H', value)
                   + struct.pack('>I', addr))
    m.uc.emu_start(CODE, CODE + 8)


def _guest_word(m, addr, value):
    """Run `move.w #value,addr.l` as main-CPU guest code."""
    m.uc.mem_write(CODE, b'\x33\xfc' + struct.pack('>H', value)
                   + struct.pack('>I', addr))
    m.uc.emu_start(CODE, CODE + 8)


def _section7():
    """A stand-in for section 7: entry word first, the idle loop at
    0x40000b90, an entry that installs vector 96, says 'HO', toggles PG2 and
    idles, and a vector-96 handler that counts renders in the shared RAM."""
    img = bytearray(0x900)

    def put(addr, code):
        img[addr - dsplink.DSP_LOAD:addr - dsplink.DSP_LOAD + len(code)] = code
    put(dsplink.DSP_LOAD, struct.pack('>I', dsplink.DSP_ENTRY))
    put(dsplink.DSP_IDLE, b'\x60\xfe')                        # bra.b *
    entry = (b'\x2e\x7c' + struct.pack('>I', 0x48000000)      # movea.l #,a7
             + b'\x23\xfc' + struct.pack('>II', DSP_HANDLER,  # move.l #,vec96
                                         VBR + dsplink.DSP_VECTOR * 4)
             + b'\x33\xfc' + struct.pack('>HI', dsplink.HO, 0)  # 'HO' -> +0
             + b'\x13\xfc' + struct.pack('>HI', dsplink.PG2_BIT,  # PG2 up
                                         dsplink.PG2_SET)
             + b'\x46\xfc\x20\x00'                            # IPL 0
             + b'\x4e\xf9' + struct.pack('>I', dsplink.DSP_IDLE))  # jmp idle
    put(dsplink.DSP_ENTRY, entry)
    put(DSP_HANDLER, b'\x52\xb9' + struct.pack('>I', COUNTER)  # addq #1
        + b'\x4e\x73')                                          # rte
    return bytes(img)


class _Profile:
    dsp_boot_task = 0x4008d56c
    dsp_request_sem = 0x4137b70c


class StandInTest(unittest.TestCase):
    def test_the_handshake(self):
        m = _main()
        link = dsplink.DspLink(m)
        self.assertEqual(link.step(0, 100), 100)
        _guest_byte(m, dsplink.RESET_SET, dsplink.RESET_BIT)   # out of reset
        self.assertEqual(link.state, dsplink.BOOTING)
        self.assertEqual(link.step(0), dsplink.BOOT_DELAY)
        self.assertTrue(link.service(dsplink.BOOT_DELAY))
        self.assertEqual(m.peek(dsplink.WINDOW, 4), b'HOHA')
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), MAIN_HANDLER)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        _guest_word(m, dsplink.WINDOW, dsplink.B0)             # 'B0'
        self.assertEqual(link.state, dsplink.READY)
        self.assertTrue(link.service(dsplink.BOOT_DELAY
                                     + dsplink.REPLY_DELAY))
        self.assertEqual(m.peek(dsplink.WINDOW + 4, 2), b'\xa5\xa5')
        self.assertEqual(link.fired['irq'], 2)
        _guest_byte(m, dsplink.RESET_CLR, 0xFF & ~dsplink.RESET_BIT)
        self.assertEqual(link.state, dsplink.OFF)

    def test_waits_for_the_edge_port_to_be_armed(self):
        m = _main()
        m.uc.mem_write(INTC0 + pit.ICR_BASE + dsplink.EPORT_PIN, b'\0')
        link = dsplink.DspLink(m)
        _guest_byte(m, dsplink.RESET_SET, dsplink.RESET_BIT)
        self.assertFalse(link.service(dsplink.BOOT_DELAY))
        self.assertEqual(link.pulses, 1)                       # kept pending
        m.uc.mem_write(INTC0 + pit.ICR_BASE + dsplink.EPORT_PIN, b'\x06')
        self.assertTrue(link.service(dsplink.BOOT_DELAY + 1))

    def test_checkpoint_round_trip(self):
        m = _main()
        link = dsplink.DspLink(m)
        _guest_byte(m, dsplink.RESET_SET, dsplink.RESET_BIT)
        state = link.checkpoint_state()
        again = dsplink.DspLink(_main())
        again.restore_checkpoint_state(state)
        self.assertEqual((again.state, again.due), (link.state, link.due))
        with self.assertRaises(RuntimeError):
            again.restore_checkpoint_state(dict(state, state='sideways'))


class DspCpuTest(unittest.TestCase):
    def boot(self, m=None):
        m = m or _main()
        cpu = dsplink.DspCpu(m, _section7(), request_sem=0x4137b70c)
        _guest_byte(m, dsplink.RESET_SET, dsplink.RESET_BIT)
        self.assertIsNotNone(cpu.dsp)
        done = 0
        for _ in range(50):
            done += dsplink.BOOT_SLICE
            cpu.service(done)
            if cpu.booted:
                break
        self.assertTrue(cpu.booted)
        return m, cpu

    def test_boots_says_hello_and_interrupts_the_main_cpu(self):
        m, cpu = self.boot()
        # The shared RAM is one memory: the DSP's 'HO' at its 0 is the main
        # CPU's 0x10000000.
        self.assertEqual(m.peek(dsplink.WINDOW, 2), b'HO')
        self.assertTrue(cpu.idle)
        self.assertEqual(cpu.pc, dsplink.DSP_IDLE)
        # Its PG2 edge became the main CPU's vector 68.
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), MAIN_HANDLER)
        self.assertEqual(cpu.fired['irq'], 1)

    def test_a_pa4_edge_is_one_render(self):
        m, cpu = self.boot()
        self.assertEqual(cpu.step(0, 500), 500)                # nothing due
        _guest_byte(m, dsplink.PA4_SET, dsplink.PA4_BIT)        # block clock
        self.assertEqual(cpu.irqs, 1)
        self.assertEqual(cpu.step(0), pit.PENDING_STEP)
        cpu.service(10_000_000)
        self.assertEqual(cpu.renders, 1)
        self.assertTrue(cpu.idle)
        # What the DSP wrote at its 0x100 the main CPU reads at 0x10000100.
        self.assertEqual(m.peek(dsplink.WINDOW + COUNTER, 4),
                         struct.pack('>I', 1))
        # The same level again is no edge; the other level is.
        _guest_byte(m, dsplink.PA4_SET, dsplink.PA4_BIT)
        self.assertEqual(cpu.irqs, 0)
        _guest_byte(m, dsplink.PA4_CLR, 0xFF & ~dsplink.PA4_BIT)
        cpu.service(10_000_001)
        self.assertEqual(m.peek(dsplink.WINDOW + COUNTER, 4),
                         struct.pack('>I', 2))

    def test_renders_on_a_thread(self):
        m, cpu = self.boot()
        cpu.start_thread()
        try:
            for n in range(1, 6):
                _guest_byte(m, dsplink.PA4_SET if n % 2 else dsplink.PA4_CLR,
                            dsplink.PA4_BIT if n % 2
                            else 0xFF & ~dsplink.PA4_BIT)
                cpu.service(20_000_000 + n)
            cpu.wait_idle()
            self.assertEqual(m.peek(dsplink.WINDOW + COUNTER, 4),
                             struct.pack('>I', 5))
            self.assertEqual(cpu.renders, 5)
            self.assertIsNone(cpu.error)
        finally:
            cpu.close()

    def test_a_slow_thread_still_renders_every_edge(self):
        # The render thread waking late must not merge the next block's edge
        # into this one: the hardware takes each within microseconds.
        import time
        m, cpu = self.boot()
        real = cpu._service_dsp

        def late(*args, **kw):
            time.sleep(0.02)                    # the host's wake-up time
            return real(*args, **kw)
        cpu._service_dsp = late
        cpu.start_thread()
        try:
            for n in range(1, 5):
                _guest_byte(m, dsplink.PA4_SET if n % 2 else dsplink.PA4_CLR,
                            dsplink.PA4_BIT if n % 2
                            else 0xFF & ~dsplink.PA4_BIT)
                cpu.service(40_000_000 + n)
            cpu.wait_idle()
            self.assertEqual(cpu.renders, 4)
            self.assertEqual(m.peek(dsplink.WINDOW + COUNTER, 4),
                             struct.pack('>I', 4))
        finally:
            cpu.close()

    def test_reset_stops_it(self):
        m, cpu = self.boot()
        _guest_byte(m, dsplink.RESET_CLR, 0xFF & ~dsplink.RESET_BIT)
        self.assertIsNone(cpu.dsp)
        _guest_byte(m, dsplink.PA4_SET, dsplink.PA4_BIT)
        self.assertEqual(cpu.irqs, 0)                          # no DSP to take it

    def test_checkpoint_round_trip(self):
        m, cpu = self.boot()
        _guest_byte(m, dsplink.PA4_SET, dsplink.PA4_BIT)
        cpu.service(30_000_000)
        state = cpu.checkpoint_state()
        self.assertTrue(state['on'])
        self.assertNotIn(dsplink.DSP_WINDOW, state['pages'])   # the main has it
        m2 = _main()
        # The main snapshot carries the shared RAM; here, copy it by hand.
        again = dsplink.DspCpu(m2, _section7())
        m2.uc.mem_write(dsplink.WINDOW, bytes(m.uc.mem_read(dsplink.WINDOW,
                                                            0x200)))
        again.restore_checkpoint_state(state)
        self.assertTrue(again.booted)
        self.assertEqual(again.pc, dsplink.DSP_IDLE)
        again.pa4 = 1
        _guest_byte(m2, dsplink.PA4_CLR, 0xFF & ~dsplink.PA4_BIT)
        again.service(30_000_001)
        self.assertEqual(m2.peek(dsplink.WINDOW + COUNTER, 4),
                         struct.pack('>I', 2))

    def test_off_checkpoint(self):
        cpu = dsplink.DspCpu(_main(), _section7())
        state = cpu.checkpoint_state()
        self.assertFalse(state['on'])
        again = dsplink.DspCpu(_main(), _section7())
        again.restore_checkpoint_state(state)
        self.assertIsNone(again.dsp)


class InstallTest(unittest.TestCase):
    def test_no_boot_task_no_model(self):
        class NoDsp:
            dsp_boot_task = None
        self.assertIsNone(dsplink.install(_main(), {}, NoDsp()))

    def test_section7_chooses_the_cpu(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ev = {}
            link = dsplink.install(_main(), ev, _Profile(), sections_dir=d)
            self.assertIsInstance(link, dsplink.DspLink)       # none there
            with open(os.path.join(d, 'section_7_BLOB.bin'), 'wb') as fh:
                fh.write(_section7())
            ev = {}
            link = dsplink.install(_main(), ev, _Profile(), sections_dir=d)
            self.assertIsInstance(link, dsplink.DspCpu)
            self.assertIs(ev['dspcpu'], link)
            self.assertEqual(link.request_sem, 0x4137b70c)
            link = dsplink.install(_main(), {}, _Profile(), sections_dir=d,
                                   real=False)
            self.assertIsInstance(link, dsplink.DspLink)

    def test_a_section7_that_is_not_the_dsp_is_refused(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'section_7_BLOB.bin'), 'wb') as fh:
                fh.write(b'\x00\x00\x00\x00' * 8)     # a SHARC blob, say
            self.assertIsNone(dsplink.find_section7(d))


if __name__ == '__main__':
    unittest.main()
