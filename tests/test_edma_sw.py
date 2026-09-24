"""The software-started eDMA channels the audio render ISR waits on.

emu/edma_sw.py exists because the render routine hands block moves to eDMA and
polls the descriptor's DONE bit. Two of its rules are easy to get wrong and
both were got wrong once here, so they are pinned:

  * CITER/BITER carry a LINKED form. With bit 15 set the count is only bits
    8:0; reading the wide field turned a nine-iteration move into 16,393.
  * The completion bit cannot be written from inside the guest's own write to
    that register -- the guest's pending value lands afterwards and erases it.
    It is queued and applied by service().
"""
import struct
import unittest

from emu.edma_sw import (CSR, CSR_DONE, CSR_START, SoftwareChannel, TCD_BASE,
                         iteration_count, link_channel, modulo_add)


class FakeUc:
    """Just enough Unicorn to hold a descriptor and some memory."""

    def __init__(self):
        self.mem = bytearray(1 << 16)
        self.base = 0xFC040000
        self.ram = bytearray(1 << 16)
        self.ram_base = 0x40000000
        self.hooks = []

    def _pick(self, addr):
        if self.ram_base <= addr < self.ram_base + len(self.ram):
            return self.ram, addr - self.ram_base
        return self.mem, addr - self.base

    def mem_read(self, addr, size):
        buf, off = self._pick(addr)
        return bytes(buf[off:off + size])

    def mem_write(self, addr, data):
        buf, off = self._pick(addr)
        buf[off:off + len(data)] = data

    def hook_add(self, *a, **k):
        self.hooks.append((a, k))


class FakeMachine:
    def __init__(self):
        self.uc = FakeUc()


def make(channel=32):
    m = FakeMachine()
    c = SoftwareChannel(m, channel)
    return m, c


def program(m, c, saddr, daddr, nbytes, citer, attr=0x0202, soff=4, doff=4):
    tcd = c.tcd
    m.uc.mem_write(tcd + 0x00, struct.pack('>I', saddr))
    m.uc.mem_write(tcd + 0x04, struct.pack('>H', attr))
    m.uc.mem_write(tcd + 0x06, struct.pack('>H', soff & 0xFFFF))
    m.uc.mem_write(tcd + 0x08, struct.pack('>I', nbytes))
    m.uc.mem_write(tcd + 0x0C, struct.pack('>I', 0))
    m.uc.mem_write(tcd + 0x10, struct.pack('>I', daddr))
    m.uc.mem_write(tcd + 0x14, struct.pack('>H', citer))
    m.uc.mem_write(tcd + 0x16, struct.pack('>H', doff & 0xFFFF))
    m.uc.mem_write(tcd + 0x18, struct.pack('>I', 0))
    m.uc.mem_write(tcd + 0x1C, struct.pack('>H', citer))
    m.uc.mem_write(tcd + 0x1E, struct.pack('>H', 0))


class IterationCountTest(unittest.TestCase):
    def test_linked_form_uses_nine_bits(self):
        # Channel 32's real descriptor: nine iterations, linking channel 32.
        self.assertEqual(iteration_count(0xC009), 9)
        self.assertEqual(link_channel(0xC009), 32)

    def test_plain_form_uses_fifteen_bits(self):
        self.assertEqual(iteration_count(0x0040), 64)
        self.assertIsNone(link_channel(0x0040))

    def test_the_wide_read_is_what_went_wrong(self):
        # 0xc009 & 0x7fff, the mistake, is 16,393 -- not nine.
        self.assertNotEqual(iteration_count(0xC009), 0xC009 & 0x7FFF)


class TransferTest(unittest.TestCase):
    def test_copies_and_reports_done(self):
        m, c = make()
        src, dst = 0x40001000, 0x40002000
        m.uc.mem_write(src, bytes(range(32)))
        program(m, c, src, dst, nbytes=8, citer=4)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, CSR_START, None)
        self.assertEqual(c.transfers, 1)
        self.assertEqual(c.bytes_moved, 32)
        self.assertEqual(bytes(m.uc.mem_read(dst, 32)), bytes(range(32)))

    def test_done_is_queued_not_written_in_the_hook(self):
        m, c = make()
        m.uc.mem_write(0x40001000, b'\0' * 32)
        program(m, c, 0x40001000, 0x40002000, nbytes=8, citer=4)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, CSR_START, None)
        # Still not visible: the guest's own write has not landed yet.
        csr = struct.unpack('>H', m.uc.mem_read(c.tcd + CSR, 2))[0]
        self.assertFalse(csr & CSR_DONE)
        c.service(0)
        csr = struct.unpack('>H', m.uc.mem_read(c.tcd + CSR, 2))[0]
        self.assertTrue(csr & CSR_DONE)
        self.assertFalse(csr & CSR_START)

    def test_linked_count_moves_nine_loops_not_sixteen_thousand(self):
        m, c = make()
        m.uc.mem_write(0x40001000, b'\xAB' * 256)
        program(m, c, 0x40001000, 0x40002000, nbytes=16, citer=0xC009,
                attr=0x0401, soff=16, doff=2)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, CSR_START, None)
        self.assertEqual(c.bytes_moved, 9 * 16)
        self.assertEqual(c.links_ignored, 1)

    def test_a_start_with_no_iterations_is_refused_not_run(self):
        m, c = make()
        program(m, c, 0x40001000, 0x40002000, nbytes=8, citer=0)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, CSR_START, None)
        self.assertEqual(c.transfers, 0)
        self.assertEqual(c.refused, 1)
        self.assertIn('CITER', c.last_error)

    def test_a_write_without_start_does_nothing(self):
        m, c = make()
        program(m, c, 0x40001000, 0x40002000, nbytes=8, citer=4)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, 0x0006, None)
        self.assertEqual(c.transfers, 0)
        self.assertEqual(c.refused, 0)


class MappingMachine(FakeMachine):
    """A machine that maps on demand, like emu.harness.Machine."""

    def __init__(self, mapped):
        super().__init__()
        self.mapped = set(mapped)
        self.ensured = []

    def ensure(self, addr):
        # Records only real mappings: like the Machine, an already-mapped
        # page is a no-op.
        base = addr & ~0xFFFFF
        if base not in self.mapped:
            self.ensured.append(base)
            self.mapped.add(base)


class WhenTest(unittest.TestCase):
    """Nothing is mapped inside the guest's store; DONE shows on the poll."""

    def setUp(self):
        self.src, self.dst = 0x40001000, 0x40002000

    def start(self, m, c):
        m.uc.mem_write(self.src, bytes(range(32)))
        program(m, c, self.src, self.dst, nbytes=8, citer=4)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, CSR_START, None)

    def csr(self, m, c):
        return struct.unpack('>H', m.uc.mem_read(c.tcd + CSR, 2))[0]

    def test_an_unmapped_page_defers_the_transfer_out_of_the_hook(self):
        m = MappingMachine(mapped={0xFC000000})
        c = SoftwareChannel(m, 32)
        self.start(m, c)
        self.assertEqual(c.transfers, 0)
        self.assertEqual(m.ensured, [])          # nothing mapped in the hook
        self.assertEqual(c.deferred, 1)
        c.service(0)
        self.assertIn(0x40000000, m.ensured)     # mapped outside emulation
        self.assertEqual(c.transfers, 1)
        self.assertEqual(bytes(m.uc.mem_read(self.dst, 32)), bytes(range(32)))
        self.assertTrue(self.csr(m, c) & CSR_DONE)

    def test_mapped_pages_run_in_the_hook(self):
        m = MappingMachine(mapped={0xFC000000, 0x40000000})
        c = SoftwareChannel(m, 32)
        self.start(m, c)
        self.assertEqual(c.transfers, 1)
        self.assertEqual(c.deferred, 0)
        self.assertEqual(m.ensured, [])

    def test_the_guests_poll_sees_done_at_once(self):
        m, c = make()
        self.start(m, c)
        self.assertFalse(self.csr(m, c) & CSR_DONE)
        c._on_csr_read(m.uc, None, c.tcd + CSR, 2, 0, None)
        self.assertTrue(self.csr(m, c) & CSR_DONE)
        self.assertFalse(c.service(0))           # nothing left to apply

    def chain(self, m, c, nxt):
        """Program a two-descriptor chain; the second at `nxt` if mapped."""
        m.uc.mem_write(self.src, bytes(range(32)))
        program(m, c, self.src, self.dst, nbytes=8, citer=4)
        m.uc.mem_write(c.tcd + 0x18, struct.pack('>I', nxt))    # DLAST
        second = struct.pack('>IHhIIIHhIHH', self.src, 0x0202, 4, 8, 0,
                             self.dst + 0x100, 2, 4, 0, 2, 0)
        if 0x40000000 <= nxt < 0x40010000:
            m.uc.mem_write(nxt, second)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, CSR_START | 0x0010, None)

    def test_a_mapped_scatter_gather_chain_runs_in_the_hook(self):
        m = MappingMachine(mapped={0xFC000000, 0x40000000})
        c = SoftwareChannel(m, 30)
        self.chain(m, c, 0x40003000)
        self.assertEqual(c.deferred, 0)
        self.assertEqual(c.transfers, 1)
        self.assertEqual(c.links_followed, 1)
        self.assertEqual(c.bytes_moved, 32 + 16)

    def test_a_chain_into_an_unmapped_page_is_deferred(self):
        m = MappingMachine(mapped={0xFC000000, 0x40000000})
        c = SoftwareChannel(m, 30)
        self.chain(m, c, 0x50000000)
        self.assertEqual(c.transfers, 0)
        self.assertEqual(c.deferred, 1)
        self.assertEqual(m.ensured, [])


class ModuloTest(unittest.TestCase):
    """ATTR SMOD/DMOD keep an address inside a 2**mod-byte ring."""

    def test_modulo_add(self):
        self.assertEqual(modulo_add(0x40002FF0, 0x20, 12), 0x40002010)
        self.assertEqual(modulo_add(0x40002FF0, 0x20, 0), 0x40003010)

    def test_a_destination_ring_wraps(self):
        m, c = make(42)
        src, dst = 0x40001000, 0x40002FF0            # 16 bytes before the end
        m.uc.mem_write(src, bytes(range(32)))
        m.uc.mem_write(0x40002000, b'\xEE' * 16)
        m.uc.mem_write(0x40003000, b'\xEE' * 16)
        attr = 0x0202 | (12 << 3)                    # DMOD = 12: a 4 KB ring
        program(m, c, src, dst, nbytes=8, citer=4, attr=attr)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, CSR_START, None)
        self.assertEqual(c.transfers, 1)
        self.assertEqual(bytes(m.uc.mem_read(0x40002FF0, 16)), bytes(range(16)))
        self.assertEqual(bytes(m.uc.mem_read(0x40002000, 16)),
                         bytes(range(16, 32)))
        self.assertEqual(bytes(m.uc.mem_read(0x40003000, 16)), b'\xEE' * 16)
        daddr = struct.unpack('>I', m.uc.mem_read(c.tcd + 0x10, 4))[0]
        self.assertEqual(daddr, 0x40002010)

    def test_a_source_ring_wraps(self):
        m, c = make(30)
        m.uc.mem_write(0x40002FF8, bytes(range(8)))
        m.uc.mem_write(0x40002000, bytes(range(8, 16)))
        attr = 0x0202 | (12 << 11)                   # SMOD = 12
        program(m, c, 0x40002FF8, 0x40005000, nbytes=8, citer=2, attr=attr)
        c._on_csr(m.uc, None, c.tcd + CSR, 2, CSR_START, None)
        self.assertEqual(bytes(m.uc.mem_read(0x40005000, 16)), bytes(range(16)))


class DescriptorAddressTest(unittest.TestCase):
    def test_channel_maps_to_its_descriptor(self):
        _, c = make(32)
        self.assertEqual(c.tcd, TCD_BASE + 32 * 0x20)
        self.assertEqual(c.tcd, 0xFC045400)   # the pointer at 0x80001200


class SsrtTest(unittest.TestCase):
    """SSRT starts a channel without touching its CSR: the Digitone's audio
    handler moves its DSP's voices over channel 47 that way, and a bank that
    watched only the CSRs left it spinning on DONE. A real Machine and a real
    guest store, on the Python bank and (where the library has it) the
    native one."""

    def run_ssrt(self, native):
        from emu.edma_sw import EDMA_SSRT, SoftwareBank
        from emu.harness import Machine
        m = Machine()
        code, src, dst = 0x40000000, 0x40100000, 0x40100800
        for addr in (code, src, TCD_BASE):
            m.ensure(addr)
        bank = SoftwareBank(m, native=native)
        m.uc.mem_write(src, bytes(range(64)))
        tcd = TCD_BASE + 47 * 0x20
        m.uc.mem_write(tcd, struct.pack('>IHHIIIHHIHH', src, 0x0202, 4, 16, 0,
                                        dst, 4, 4, 0, 4, 0))
        # move.b #47,SSRT
        m.uc.mem_write(code, b'\x13\xfc\x00\x2f' + struct.pack('>I', EDMA_SSRT))
        m.uc.emu_start(code, code + 8)
        bank.service(0)
        self.assertEqual(bytes(m.uc.mem_read(dst, 64)), bytes(range(64)))
        csr = struct.unpack('>H', m.uc.mem_read(tcd + CSR, 2))[0]
        self.assertTrue(csr & CSR_DONE)
        self.assertFalse(csr & CSR_START)
        self.assertEqual(bank.ssrt_starts, 1)
        # Bit 6 means every channel; nothing uses it, and it starts none.
        m.uc.mem_write(code, b'\x13\xfc\x00\x40' + struct.pack('>I', EDMA_SSRT))
        m.uc.emu_start(code, code + 8)
        self.assertEqual(bank.ssrt_starts, 1)

    def test_python_bank(self):
        self.run_ssrt(native=False)

    def test_default_bank(self):
        self.run_ssrt(native=None)


if __name__ == '__main__':
    unittest.main()
