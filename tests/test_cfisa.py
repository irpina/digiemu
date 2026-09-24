"""emu/cfisa.py: ColdFire instruction lengths, classes, flow and flags.

Every encoding here is hand-assembled from the ColdFire Programmer's
Reference Manual. The decoder's lengths were also checked against every
block Unicorn executed in a cold boot and a live-audio session of the
Digitakt OS (no mismatch in 6,489 + 5,778 blocks); these cases pin the
instructions that matter most: the ones Capstone cannot decode, the ones
the timing tables cost differently, and the ones the MCF5441x does not
implement at all.
"""
import unittest

from emu import cfisa
from emu.cfisa import (ABS, D16, IDX, IMM, IND, POST, PRE, R, F_BCC, F_BRA,
                       F_BSR, F_JMP, F_JSR, F_RTE, F_RTS, F_TRAP)


def dec(hexstr, addr=0x40000000):
    data = bytes.fromhex(hexstr) + bytes(8)
    return cfisa.decode(cfisa.reader(data, addr)(addr), addr)


class LengthTest(unittest.TestCase):
    CASES = [
        ('2200', 2, 'move'),                 # move.l d0,d1
        ('203c12345678', 6, 'move'),         # move.l #imm,d0
        ('2f40fffa', 4, 'move'),             # move.l d0,-6(a7)
        ('22300c00', 4, 'move'),             # move.l (a0,d0.l*4),d1
        ('23c080007706', 6, 'move'),         # move.l d0,abs.l
        ('13fc0001ec09406c', 8, 'move'),     # move.b #1,abs.l
        ('2050', 2, 'movea'),                # movea.l (a0),a0
        ('41ef0004', 4, 'lea'),              # lea 4(a7),a0
        ('4879800096b8', 6, 'pea'),          # pea abs.l
        ('4eb98000223c', 6, 'jsr'),          # jsr abs.l
        ('4e92', 2, 'jsr'),                  # jsr (a2)
        ('4efb0802', 4, 'jmp'),              # jmp (d8,pc,d0.l)
        ('60fe', 2, 'bra'),                  # bra.b *
        ('6700fe64', 4, 'bcc'),              # beq.w
        ('61ff00001000', 6, 'bsr'),          # bsr.l
        ('4e75', 2, 'rts'),
        ('4e73', 2, 'rte'),
        ('4e40', 2, 'trap'),
        ('4e560000', 4, 'link'),
        ('4e5e', 2, 'unlk'),
        ('48d70c1c', 4, 'movem'),            # movem.l d2-d4/a2-a3,(a7)
        ('4cee3c0cff98', 6, 'movem'),        # movem.l -$68(a6),...
        ('4c002800', 4, 'mul_l'),            # muls.l d0,d2
        ('4c410800', 4, 'div_l'),            # divs.l d1,d0
        ('81c1', 2, 'div_w'),                # divs.w d1,d0
        ('c1c1', 2, 'mul_w'),                # muls.w d1,d0
        ('71c8', 2, 'mvsz'),                 # mvz.w a0,d0 (the Digitakt OS has it)
        ('714c', 2, 'mvsz'),                 # mvs.w a4,d0
        ('71f98000770a', 6, 'mvsz'),         # mvz.w abs.l,d0
        ('a140', 2, 'mov3q'),                # mov3q #0,d0
        ('04c0', 2, 'ff1'),
        ('00c0', 2, 'bitrev'),
        ('02c0', 2, 'byterev'),
        ('4c80', 2, 'sats'),
        ('0c8000000400', 6, 'cmpi'),         # cmpi.l #$400,d0
        ('0c400010', 4, 'cmpi'),             # cmpi.w #$10,d0
        ('08c0000c', 4, 'bchgi'),            # bset.b #12,d0
        ('08000004', 4, 'btsti'),            # btst #4,d0
        ('e1a9', 2, 'shift'),                # lsl.l d0,d1
        ('d182', 2, 'addx'),                 # addx.l d2,d0
        ('5880', 2, 'addq'),
        ('4a80', 2, 'tst'),
        ('4298', 2, 'clr'),                  # clr.l (a0)+
        ('46fc2700', 4, 'move_to_sr'),
        ('40c0', 2, 'move_from_sr'),
        ('4e7b0801', 4, 'movec'),
        ('4e71', 2, 'nop'),
        ('4ac8', 2, 'halt'),
        ('51fb10adc0de', 6, 'tpf'),
        ('40e746fc2000', 6, 'stldsr'),
        ('fbef0003fff8', 6, 'wdebug'),       # the bootstrap's one WDEBUG
        ('f4e8', 2, 'cpushl'),               # cpushl bc,(a0)
        ('a6c10000', 4, 'mac'),              # mac.w d1,d3 (register form)
        ('a1810000', 2, 'from_acc'),         # move.l acc0,d1 + the next word
    ]

    def test_lengths_and_classes(self):
        for hexstr, length, op in self.CASES:
            with self.subTest(hexstr=hexstr):
                ins = dec(hexstr)
                self.assertEqual(ins.length, length)
                self.assertEqual(ins.op, op)

    def test_linear_decode_stays_in_step(self):
        code = bytes.fromhex(''.join(h for h, _n, _o in self.CASES[:20]))
        got = [ins.length for _a, ins in cfisa.disassemble_lengths(
            code, 0x100, 0x100, 0x100 + len(code))]
        self.assertEqual(got, [n for _h, n, _o in self.CASES[:20]])


class FlagTest(unittest.TestCase):
    def test_float_instructions_are_flagged(self):
        """The MCF5441x has no FPU; Unicorn's V4e has one."""
        for hexstr in ('f2000000', 'f2800002', 'f3100000'):
            with self.subTest(hexstr=hexstr):
                self.assertTrue(dec(hexstr).flags & cfisa.FPU)

    def test_other_line_f_is_flagged(self):
        self.assertTrue(dec('f800').flags & cfisa.LINEF)

    def test_illegal(self):
        self.assertTrue(dec('4afc').flags & cfisa.ILLEGAL)
        self.assertTrue(dec('4e76').flags & cfisa.ILLEGAL)   # trapv: not ColdFire

    def test_movec_and_privileged(self):
        ins = dec('4e7b0c05')
        self.assertTrue(ins.flags & cfisa.MOVEC)
        self.assertTrue(ins.flags & cfisa.PRIV)
        self.assertEqual(ins.extra, 0xC05)


class FlowTest(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(dec('60fe', 0x100).target, 0x100)
        self.assertEqual(dec('6602', 0x100).target, 0x104)
        self.assertEqual(dec('6700fe64', 0x80000e3e).target, 0x80000ca4)
        self.assertEqual(dec('4eb98000223c').target, 0x8000223c)
        self.assertEqual(dec('4efa0010', 0x200).target, 0x212)   # jmp d16(pc)

    def test_kinds(self):
        self.assertEqual(dec('60fe').flow, F_BRA)
        self.assertEqual(dec('6602').flow, F_BCC)
        self.assertEqual(dec('6104').flow, F_BSR)
        self.assertEqual(dec('4ed0').flow, F_JMP)
        self.assertEqual(dec('4e90').flow, F_JSR)
        self.assertEqual(dec('4e75').flow, F_RTS)
        self.assertEqual(dec('4e73').flow, F_RTE)
        self.assertEqual(dec('4e4f').flow, F_TRAP)


class OperandTest(unittest.TestCase):
    def test_move_classes(self):
        ins = dec('2210')                    # move.l (a0),d1
        self.assertEqual((ins.src, ins.dst), (IND, R))
        ins = dec('2280')                    # move.l d0,(a1)
        self.assertEqual((ins.src, ins.dst), (R, IND))
        ins = dec('22d8')                    # move.l (a0)+,(a1)+
        self.assertEqual((ins.src, ins.dst), (POST, POST))
        ins = dec('2320')                    # move.l -(a0),-(a1)
        self.assertEqual((ins.src, ins.dst), (PRE, PRE))
        ins = dec('2228000c')                # move.l 12(a0),d1
        self.assertEqual((ins.src, ins.dst), (D16, R))
        ins = dec('22300c00')                # move.l (a0,d0.l*4),d1
        self.assertEqual(ins.src, IDX)
        ins = dec('203c12345678')
        self.assertEqual(ins.src, IMM)
        ins = dec('23c080007706')
        self.assertEqual(ins.dst, ABS)

    def test_register_use_for_stalls(self):
        ins = dec('22300c00')                # move.l (a0,d0.l*4),d1
        self.assertIn((8, cfisa.BASE), ins.agen)
        self.assertIn((0, cfisa.INDEXN), ins.agen)
        self.assertEqual(ins.writes, 1)
        self.assertEqual(dec('2050').writes, 8)          # movea.l (a0),a0
        self.assertEqual(dec('7005').writes, 0)          # moveq #5,d0


if __name__ == '__main__':
    unittest.main()
