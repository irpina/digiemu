"""patches/unicorn-2.1.4-tcg-aarch64-addsub2.patch: a constant subtraction.

Without the patch an aarch64 host dies with SIGILL translating these blocks
(patches/README.md), so each runs in a child process. Other hosts run them
correctly either way.
"""
import subprocess
import sys
import unittest

CHILD = r'''
import sys
from unicorn import Uc, UC_ARCH_M68K, UC_MODE_BIG_ENDIAN
from unicorn.m68k_const import UC_CPU_M68K_CFV4E, UC_M68K_REG_D4
code = bytes.fromhex(sys.argv[1]) + b'\x4e\x71'
uc = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
uc.ctl_set_cpu_model(UC_CPU_M68K_CFV4E)
uc.mem_map(0x1000, 0x1000)
uc.mem_write(0x1000, code)
uc.reg_write(UC_M68K_REG_D4, 7)
uc.emu_start(0x1000, 0x1000 + len(code))
print(uc.reg_read(UC_M68K_REG_D4))
'''

# Each leaves X = 1 (a borrow) and d6 = 0 known in the block, then
# subx.l %d6,%d4: d4 = 7 - 0 - 1.
CASES = {
    # suba.l %a1,%a1; move.l %a1,%d5; clr.l %d6; subq.l #1,%d5; subx.l %d6,%d4
    'subq': '93c9 2a09 4286 5385 9986',
    # ... moveq #0,%d6 in place of clr.l %d6
    'moveq': '93c9 2a09 7c00 5385 9986',
    # ... moveq #1,%d7; sub.l %d7,%d5 in place of subq.l #1,%d5
    'sub': '93c9 2a09 4286 7e01 9a87 9986',
}


def _run(code):
    return subprocess.run([sys.executable, '-c', CHILD, code.replace(' ', '')],
                          capture_output=True, text=True, timeout=60)


class ConstantSubxTest(unittest.TestCase):
    def setUp(self):
        try:
            import unicorn  # noqa: F401
        except ImportError:
            self.skipTest('unicorn is not installed')

    def test_subx_borrowing_from_constants(self):
        for name, code in CASES.items():
            with self.subTest(name):
                p = _run(code)
                self.assertEqual(p.returncode, 0,
                                 'exit %d: %s' % (p.returncode, p.stderr[-300:]))
                self.assertEqual(p.stdout.strip(), '6')


if __name__ == '__main__':
    unittest.main()
