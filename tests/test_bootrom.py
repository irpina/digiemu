"""emu/bootrom.py: the SPI flash, the DSPI master, the panel link and the
handoff, on synthetic data. The real bootstrap run is in the firmware-gated
class at the end."""
import os
import struct
import unittest

from unicorn.m68k_const import (UC_M68K_REG_A7, UC_M68K_REG_D0,
                                UC_M68K_REG_D1, UC_M68K_REG_PC,
                                UC_M68K_REG_SR)

from emu import bootrom
from emu.harness import Machine


def frame(flash, *data):
    flash.select()
    out = [flash.transfer(b) for b in data]
    flash.deselect()
    return out


class SpiFlashTest(unittest.TestCase):
    def test_identify(self):
        f = bootrom.SpiFlash(size=0x10000)
        self.assertEqual(frame(f, 0x9F, 0, 0, 0)[1:], [0x01, 0x20, 0x18])

    def test_read_and_fast_read(self):
        f = bootrom.SpiFlash(bytes(range(256)) * 256)
        self.assertEqual(frame(f, 0x03, 0, 0, 0x10, 0, 0)[4:], [0x10, 0x11])
        self.assertEqual(frame(f, 0x0B, 0, 0, 0x20, 0xFF, 0, 0)[5:],
                         [0x20, 0x21])
        self.assertEqual(f.read_summary(), [(0, 0x10000)])

    def test_program_clears_bits_and_erase_sets_them(self):
        f = bootrom.SpiFlash(size=0x80000)
        frame(f, 0x02, 0, 0, 0, 0x0F)                 # no WREN: ignored
        self.assertEqual(f.mem[0], 0xFF)
        frame(f, 0x06)
        frame(f, 0x02, 0, 0, 0, 0x0F, 0xF0)
        self.assertEqual(bytes(f.mem[:2]), b'\x0f\xf0')
        frame(f, 0x06)
        frame(f, 0x02, 0, 0, 0, 0xF0)                 # 1 -> 0 only
        self.assertEqual(f.mem[0], 0x00)
        frame(f, 0x06)
        frame(f, 0xD8, 0, 0, 0)                       # 256 KB sector
        self.assertEqual(f.mem[0], 0xFF)
        self.assertEqual([w[0] for w in f.writes],
                         ['program', 'program', 'erase'])

    def test_status(self):
        f = bootrom.SpiFlash(size=0x1000)
        self.assertEqual(frame(f, 0x05, 0)[1], 0x00)
        frame(f, 0x06)
        self.assertEqual(frame(f, 0x05, 0)[1], 0x02)  # WEL


DSPI = bootrom.DSPI0


class DspiTest(unittest.TestCase):
    def test_guest_code_reads_the_flash(self):
        """The bootstrap's own shape: 8-bit frames on PCS1, CONT until the
        last, the answer popped from POPR after RFDF."""
        m = Machine()
        m.ensure(0x40000000)
        m.ensure(DSPI)
        flash = bootrom.SpiFlash(b'\x00' * 0x80000 + b'ELE3' + bytes(16))
        dspi = bootrom.Dspi(m, devices={1: flash})
        push = lambda cont, byte: ((0x80000000 if cont else 0) | 0x00020000
                                   | byte)
        code = bytearray()
        code += bytes.fromhex('203c38000000') + bytes.fromhex('23c0') + \
            struct.pack('>I', DSPI + 0x0C)            # CTAR0: 8-bit frames
        for byte, cont in ((0x03, 1), (0x08, 1), (0, 1), (0, 1), (0, 1),
                           (0, 0)):
            code += bytes.fromhex('203c') + struct.pack('>I', push(cont, byte))
            code += bytes.fromhex('23c0') + struct.pack('>I', DSPI + 0x34)
        # drain five, keep the sixth (the second data byte 'L') in d1
        for _ in range(6):
            code += bytes.fromhex('2239') + struct.pack('>I', DSPI + 0x38)
        code += bytes.fromhex('2039') + struct.pack('>I', DSPI + 0x2C)
        code += bytes.fromhex('4e71')
        m.uc.mem_write(0x40000400, bytes(code))
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        m.uc.emu_start(0x40000400, 0x40000400 + len(code) - 2)
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_D1) & 0xFF, ord('L'))
        sr = m.uc.reg_read(UC_M68K_REG_D0)
        self.assertFalse(sr & 0x00020000)               # RX drained
        self.assertTrue(sr & 0x80000000)                # TCF
        self.assertEqual(dspi.frames, 6)
        self.assertEqual(flash.commands[0x03], 1)

    def test_a_frame_with_no_device_reads_ones(self):
        m = Machine()
        m.ensure(DSPI)
        dspi = bootrom.Dspi(m, devices={})
        dspi.ctar[0] = 0x38000000
        dspi._frame(0x00010000, 0x9F)
        self.assertEqual(list(dspi.rx), [0xFF])


class ImageTest(unittest.TestCase):
    def test_bootstrap_header(self):
        body = struct.pack('>III', 0x80010000, 0x80000EAA, 0x03000900) + b'x' * 4
        section = struct.pack('>I', len(body)) + body
        image, sp, pc, version = bootrom.bootstrap_image(section)
        self.assertEqual((sp, pc, version), (0x80010000, 0x80000EAA, 0x03000900))
        self.assertEqual(image, body)
        with self.assertRaises(ValueError):
            bootrom.bootstrap_image(section[:-1])

    def test_wdebug_is_found_and_stepped_over(self):
        image = bytes.fromhex('4e71' 'fbef0003fff8' '4e71' 'fbd00003' '4e75')
        sites = bootrom.wdebug_sites(image, 0x80000400)
        self.assertEqual(sites, [(0x80000402, 6), (0x8000040a, 4)])
        m = Machine()
        m.ensure(0x80000000)
        m.uc.mem_write(0x80000400, image)
        bootrom.step_over(m, sites)
        self.assertEqual(bytes(m.uc.mem_read(0x80000402, 2)), b'\x60\x04')
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        m.uc.emu_start(0x80000400, 0x8000040e)     # runs, never translates it


class PanelLinkTest(unittest.TestCase):
    def test_answers(self):
        m = Machine()
        m.ensure(0xEC000000)
        link = bootrom.PanelLink(m, held={2: 0x10})
        for b in (0x60, 0x01):
            link._on_tx(m.uc, 0, bootrom.PanelLink.URB, 1, b, None)
        self.assertEqual(list(link.rx)[:6], [0x20, 0, 0x21, 0, 0x22, 0x10])
        link.rx.clear()
        for b in (0x70, 0x00):
            link._on_tx(m.uc, 0, bootrom.PanelLink.URB, 1, b, None)
        self.assertEqual(bytes(link.rx), b'\x70' + bootrom.PanelLink.CARD)
        link._on_read(m.uc, 0, bootrom.PanelLink.USR, 1, 0, None)
        self.assertEqual(m.uc.mem_read(bootrom.PanelLink.USR, 1)[0] & 1, 1)
        link._on_read(m.uc, 0, bootrom.PanelLink.URB, 1, 0, None)
        self.assertEqual(m.uc.mem_read(bootrom.PanelLink.URB, 1)[0], 0x70)


class PanelInterruptTest(unittest.TestCase):
    """The panel's replies interrupt only while the UART and the interrupt
    controller let them: on the Digitone the updater never unmasks UART8,
    and delivering anyway starved its RTOS tick."""

    def link(self):
        m = Machine()
        m.ensure(0xEC000000)
        m.ensure(0xFC000000)
        link = bootrom.PanelLink(m, groups=7)
        for b in (0x60, 0x01):
            link._on_tx(m.uc, 0, link.URB, 1, b, None)
        return m, link

    def test_seven_groups(self):
        _m, link = self.link()
        self.assertEqual(len(link.rx), 14)

    def test_gated_by_uimr_and_the_intc_mask(self):
        m, link = self.link()
        self.assertFalse(link.pending())                 # masked at reset
        link._on_intc(m.uc, 0, link.INTC1 + 0x1D, 1, link.SOURCE, None)
        self.assertFalse(link.pending())                 # UIMR still clear
        link._on_control(m.uc, 0, link.UIMR, 1, 0x02, None)
        self.assertTrue(link.pending())
        link._on_intc(m.uc, 0, link.INTC1 + 0x1C, 1, 0x40, None)   # mask all
        self.assertFalse(link.pending())
        link._on_intc(m.uc, 0, link.INTC1 + 0x08, 4, 0, None)      # IMRH clear
        self.assertTrue(link.pending())
        link._on_control(m.uc, 0, link.UCR, 1, 0x20, None)  # reset receiver
        self.assertFalse(link.rx)


class StrapTest(unittest.TestCase):
    def test_a_strap_reads_its_byte(self):
        m = Machine()
        m.ensure(0xEC000000)
        m.ensure(0x40000000)
        bootrom.BootHardware(m, straps={0xEC09401B: 0x08})
        # move.b $EC09401B,d0
        code = bytes.fromhex('1039ec09401b' '4e71')
        m.uc.mem_write(0x40000400, code)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        m.uc.emu_start(0x40000400, 0x40000406)
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_D0) & 0xFF, 0x08)


class HandoffTest(unittest.TestCase):
    def test_apply(self):
        regs = {'d%d' % i: i for i in range(8)}
        regs.update({'a%d' % i: 0x100 * i for i in range(8)})
        regs.update(a7=0x8000FFE8, sr=0x2500)
        data = {'pc': 0x400004E8, 'regs': regs, 'ctlregs': {0x801: 0x80000000},
                'sram': b'\x11' * bootrom.SRAM_SIZE,
                'ddr': {0x40200000: b'\x22' * 0x100000},
                'mmio': [(0xFC0B8010, 4, 0x00010101)], 'boot_flags': 0x140000}
        m = Machine()
        pc = bootrom.apply_handoff(m, data)
        self.assertEqual(pc, 0x400004E8)
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_A7), 0x8000FFE8)
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_D0 + 3), 3)
        self.assertEqual(m.ctlregs[0x801], 0x80000000)
        self.assertEqual(bytes(m.uc.mem_read(0x80000000, 2)), b'\x11\x11')
        self.assertEqual(bytes(m.uc.mem_read(0x40200000, 1)), b'\x22')
        self.assertEqual(bytes(m.uc.mem_read(0xFC0B8010, 4)),
                         bytes.fromhex('00010101'))


SECTIONS = os.environ.get('DIGIEMU_SECTIONS')
SYX = os.environ.get('DIGIEMU_SYX')


@unittest.skipUnless(SECTIONS and SYX, 'needs DIGIEMU_SYX and DIGIEMU_SECTIONS '
                     '(a Digitakt OS 1.53 .syx and its extracted sections)')
class RealBootTest(unittest.TestCase):
    def test_the_bootstrap_starts_the_os(self):
        boot = open(os.path.join(SECTIONS, 'section_2_DSP.bin'), 'rb').read()
        main = open(os.path.join(SECTIONS, 'section_3_MAIN_OS.bin'), 'rb').read()
        r = bootrom.boot(SYX, boot, main)
        self.assertEqual(r.reached, 0x400004E8)
        self.assertTrue(r.main_os_matches)
        self.assertEqual(r.flash_writes, [])
        self.assertEqual(r.boot_flags, 0x00140000)


if __name__ == '__main__':
    unittest.main()
