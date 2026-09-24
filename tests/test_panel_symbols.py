# pyright: reportMissingImports=false
"""The front-panel symbol rules, tested without any firmware.

`StringTable` locates the factory-test control-name tables by the strings
their entries point at, which is what lets both products -- and, in
principle, a firmware version neither has seen -- describe their own panels
instead of having a map written down per build. That logic is pure: it takes
bytes and a load address and returns an address. So it is tested here against
a synthetic image, and needs neither the copyrighted firmware nor the
emulator.

The tail-merge case has its own test because it is the one that actually bit:
the compiler stores `SRC` as the last three bytes of `PAGE SRC`, so an anchor
string is NOT generally preceded by a NUL, and a rule that demands one
matches nothing at all.
"""
import struct
import unittest

from emu import panelin
from emu.symbols import StringTable


LOAD = 0x40000400


def build_image(strings, table_at, entries, size=0x400, filler=b'\xde'):
    """-> bytes of a synthetic image with `strings` laid out and a char* table.

    `strings` maps offset -> raw bytes to place there (callers place their own
    NULs, so a test can build tail-merged literals). `entries` is the list of
    guest addresses the table's slots point at.
    """
    img = bytearray(filler * size)
    for off, raw in strings.items():
        img[off:off + len(raw)] = raw
    for i, addr in enumerate(entries):
        struct.pack_into('>I', img, table_at + 4 * i, addr)
    return bytes(img)


class StringTableTest(unittest.TestCase):
    def test_resolves_a_simple_table(self):
        strings = {0x100: b'UNDEFINED\x00', 0x120: b'TRIG\x00', 0x140: b'SRC\x00'}
        img = build_image(
            strings, table_at=0x200,
            entries=[LOAD + 0x100, LOAD + 0x120, LOAD + 0x140])
        rule = StringTable(('UNDEFINED', 'TRIG', 'SRC'))
        value, detail = rule.resolve(img, LOAD, {})
        self.assertEqual(value, LOAD + 0x200, detail)

    def test_resolves_when_literals_are_tail_merged(self):
        # "SRC" is the tail of "PAGE SRC", so it has no leading NUL. This is
        # what the real images do and what the first version of the rule got
        # wrong.
        strings = {0x100: b'UNDEFINED\x00', 0x120: b'TRIG\x00',
                   0x140: b'PAGE SRC\x00'}
        img = build_image(
            strings, table_at=0x200,
            entries=[LOAD + 0x100, LOAD + 0x120, LOAD + 0x145])
        rule = StringTable(('UNDEFINED', 'TRIG', 'SRC'))
        value, detail = rule.resolve(img, LOAD, {})
        self.assertEqual(value, LOAD + 0x200, detail)

    def test_refuses_when_no_table_matches(self):
        strings = {0x100: b'UNDEFINED\x00', 0x120: b'TRIG\x00'}
        img = build_image(strings, table_at=0x200,
                          entries=[LOAD + 0x100, LOAD + 0x120])
        rule = StringTable(('UNDEFINED', 'TRIG', 'NOTPRESENT'))
        value, detail = rule.resolve(img, LOAD, {})
        self.assertIsNone(value)
        self.assertIn('need 1', detail)

    def test_refuses_when_two_tables_match(self):
        # An ambiguous image must be refused, not guessed at: picking one of
        # two candidates is how a symbol silently resolves to the wrong thing.
        strings = {0x100: b'UNDEFINED\x00', 0x120: b'TRIG\x00'}
        entries = [LOAD + 0x100, LOAD + 0x120]
        img = bytearray(build_image(strings, table_at=0x200, entries=entries))
        for i, addr in enumerate(entries):
            struct.pack_into('>I', img, 0x300 + 4 * i, addr)
        rule = StringTable(('UNDEFINED', 'TRIG'))
        value, detail = rule.resolve(bytes(img), LOAD, {})
        self.assertIsNone(value)
        self.assertIn('2 table(s)', detail)

    def test_ignores_a_misaligned_run(self):
        strings = {0x100: b'UNDEFINED\x00', 0x120: b'TRIG\x00'}
        img = build_image(strings, table_at=0x202,
                          entries=[LOAD + 0x100, LOAD + 0x120])
        rule = StringTable(('UNDEFINED', 'TRIG'))
        value, _ = rule.resolve(img, LOAD, {})
        self.assertIsNone(value)


class ControlNamesTest(unittest.TestCase):
    """control_names() walks a table in guest memory to its terminator."""

    TABLE = 0x40001000
    STRINGS = 0x40001100

    def machine(self, names):
        """A Machine whose encoder table holds `names` (None = a NULL slot)."""
        from types import SimpleNamespace
        from emu.harness import Machine
        m = Machine()
        m.ensure(self.TABLE)
        at = self.STRINGS
        for i, name in enumerate(names):
            ptr = 0
            if name is not None:
                m.uc.mem_write(at, name.encode() + b'\0')
                ptr, at = at, at + len(name) + 1
            m.uc.mem_write(self.TABLE + 4 * i, struct.pack('>I', ptr))
        m.uc.mem_write(self.TABLE + 4 * len(names), b'\0' * 8)
        profile = SimpleNamespace(panel_button_names=self.TABLE,
                                  panel_encoder_names=self.TABLE)
        return m, profile

    def test_entry_zero_is_kept_when_present(self):
        m, prof = self.machine(['TRIG 1', 'TRIG 2'])
        self.assertEqual(panelin.control_names(m, prof),
                         {0: 'TRIG 1', 1: 'TRIG 2'})

    def test_a_null_entry_zero_does_not_end_the_table(self):
        # The Digitone's encoder table: rotation codes start at 1.
        m, prof = self.machine([None, 'ENC A', 'ENC B'])
        self.assertEqual(panelin.control_names(m, prof, 'encoder'),
                         {1: 'ENC A', 2: 'ENC B'})

    def test_a_later_null_ends_it(self):
        m, prof = self.machine(['A', None, 'C'])
        self.assertEqual(panelin.control_names(m, prof), {0: 'A'})


class CodeForTest(unittest.TestCase):
    def test_linear_region_matches_the_measured_formula(self):
        for channel in range(6):
            for bit in range(8):
                self.assertEqual(panelin.code_for(channel, bit),
                                 channel * 8 + bit + 1)

    def test_first_and_last_linear_codes(self):
        self.assertEqual(panelin.code_for(0, 0), 1)
        self.assertEqual(panelin.code_for(5, 7), 48)

    def test_channel_six_is_not_guessed(self):
        # Channel 6 is non-linear and differs between the two products, so
        # the helper declines rather than inventing an answer.
        self.assertIsNone(panelin.code_for(6, 0))

    def test_rejects_an_out_of_range_bit(self):
        with self.assertRaises(ValueError):
            panelin.code_for(0, 8)


class PanelWireFormatTest(unittest.TestCase):
    def test_tags_match_the_recovered_protocol(self):
        self.assertEqual(panelin.TAG_BUTTON, 0x2)
        self.assertEqual(panelin.TAG_ENCODER, 0x3)

    def test_nine_encoders(self):
        # Eight data encoders plus level, corroborated by the firmware's own
        # channel->index table having exactly nine valid entries.
        self.assertEqual(panelin.ENCODERS, 9)


class HeldTest(unittest.TestCase):
    """Held-button bookkeeping, against the real shipped device files."""

    def setUp(self):
        import os
        from emu import device
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        devices = device.load_all(os.path.join(repo, 'devices'))
        self.by_name = {d.name: d for d in devices}
        self.dn2 = self.by_name['Digitone II']
        self.dt2 = self.by_name['Digitakt II']

    def test_press_sets_one_bit(self):
        held = panelin.Held(self.dn2)
        self.assertEqual(held.press(1), (0, 0x01))

    def test_two_buttons_in_one_channel_share_a_mask(self):
        # The whole point: a chord is one message with two bits set, not two
        # separate press messages.
        held = panelin.Held(self.dn2)
        held.press(1)
        self.assertEqual(held.press(2), (0, 0x03))
        self.assertEqual(sorted(held.down_codes()), [1, 2])

    def test_release_clears_only_its_own_bit(self):
        held = panelin.Held(self.dn2)
        held.press(1)
        held.press(2)
        self.assertEqual(held.release(1), (0, 0x02))
        self.assertEqual(held.down_codes(), [2])

    def test_buttons_in_different_channels_do_not_interfere(self):
        held = panelin.Held(self.dn2)
        self.assertEqual(held.press(1), (0, 0x01))
        self.assertEqual(held.press(9), (1, 0x01))
        self.assertEqual(sorted(held.down_codes()), [1, 9])

    def test_func_chord(self):
        # FUNC is code 17 -> channel 2 bit 0; a page button is code 1.
        held = panelin.Held(self.dn2)
        self.assertEqual(held.press(17), (2, 0x01))
        self.assertEqual(held.press(1), (0, 0x01))
        self.assertTrue(held.is_down(17))
        self.assertTrue(held.is_down(1))

    def test_code_this_product_lacks_is_declined(self):
        # Digitone has code 54; Digitakt's panel stops at 50.
        self.assertIsNotNone(panelin.Held(self.dn2).press(54))
        self.assertIsNone(panelin.Held(self.dt2).press(54))

    def test_release_all_lets_go_of_every_channel(self):
        held = panelin.Held(self.dn2)
        held.press(1)
        held.press(17)
        self.assertEqual(held.release_all(), [(0, 0), (2, 0)])
        self.assertEqual(held.down_codes(), [])

    def test_release_all_is_empty_when_nothing_is_down(self):
        held = panelin.Held(self.dn2)
        held.press(1)
        held.release(1)
        self.assertEqual(held.release_all(), [])

    def test_is_down_is_false_for_an_absent_code(self):
        self.assertFalse(panelin.Held(self.dt2).is_down(54))


if __name__ == '__main__':
    unittest.main()
