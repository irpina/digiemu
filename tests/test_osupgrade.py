"""emu/osupgrade.py: the OS's flash writes, its reset, and the container a
written flash holds.

No firmware: the container is a synthetic ELE3 with two sections, and the
flash is driven through emu/bootrom.py's DSPI model as the OS's driver
would: write enable, page program, status read.
"""
import struct
import unittest

from emu import bootrom, osupgrade
from emu.harness import Machine

PUSHR = bootrom.DSPI0 + bootrom.Dspi.PUSHR
CONT, PCS1 = 0x80000000, 0x00020000
CODE = 0x40100000


def _container():
    body = [(2, b'boot' * 4), (4, b'updater!' * 3)]
    table = b''
    data = b''
    off = 0x20 + 16 * len(body)
    for sid, blob in body:
        table += struct.pack('>IIII', sid, off + len(data), len(blob), 0)
        data += blob
    head = b'ELE3' + bytes(0x18) + struct.pack('>I', len(body))
    return head + table + data


def _machine(flash):
    m = Machine()
    m.ensure(bootrom.DSPI0)
    # The OS's setup: 8-bit frames on CTAR0.
    m.uc.mem_write(bootrom.DSPI0 + bootrom.Dspi.CTAR,
                   struct.pack('>I', 0x38000000))
    m.ensure(osupgrade.RCR)
    m.ensure(CODE)
    return m, osupgrade.FlashWrites(m, {'flash': flash})


def _run(m, code):
    """Run guest code: the models hear only the guest's own accesses."""
    m.uc.mem_write(CODE, code)
    m.uc.emu_start(CODE, CODE + len(code))


def _frames(m, frames):
    """move.l #command|byte, PUSHR for each (byte, last)."""
    code = b''
    for byte, last in frames:
        code += struct.pack('>HII', 0x23FC, (0 if last else CONT) | PCS1 | byte,
                            PUSHR)
    _run(m, code)


class ContainerTest(unittest.TestCase):
    def test_cut_to_its_sections(self):
        c = _container()
        flash = bytearray(b'\xff' * 0x100000)
        flash[0x80000:0x80000 + len(c)] = c
        self.assertEqual(osupgrade.container_from_flash(bytes(flash)), c)
        self.assertEqual(osupgrade.section(c, 4), b'updater!' * 3)
        self.assertIsNone(osupgrade.section(c, 7))

    def test_no_container(self):
        with self.assertRaises(ValueError):
            osupgrade.container_from_flash(bytes(0x100000))


class FlashWritesTest(unittest.TestCase):
    def test_a_page_program_lands_in_the_shared_image(self):
        flash = bytearray(b'\xff' * 0x100000)
        m, fw = _machine(flash)
        _frames(m, [(0x06, True),                   # write enable
                    (0x02, False), (0x08, False),   # program 0x080000
                    (0x00, False), (0x00, False),
                    (0xA5, False), (0x5A, True)])
        self.assertEqual(flash[0x80000:0x80002], b'\xa5\x5a')
        self.assertEqual(fw.writes, [('program', 0x80000, 2)])
        self.assertFalse(fw.reset)

    def test_soft_reset_is_seen(self):
        m, fw = _machine(bytearray(0x100000))
        # move.b #SOFTRST, RCR
        _run(m, struct.pack('>HHI', 0x13FC, osupgrade.SOFTRST, osupgrade.RCR))
        self.assertTrue(fw.reset)


if __name__ == '__main__':
    unittest.main()
