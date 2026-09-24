"""emu/panelleds.py: the panel-MCU LED stream decoder, on synthetic streams."""
import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import device, panelleds                                  # noqa: E402

RED, WHITE, DIM = 3, 0x14, 2


def stream():
    """Palette, slot definitions, selectors and OLED traffic, interleaved."""
    s = bytearray()
    s += bytes([0xB5, RED, 31, 0, 0, 0xB5, WHITE, 31, 31, 31,
                0xB5, DIM, 1, 1, 1])
    s += bytes([0x10, 0x05] + list(range(8)))          # an OLED tile
    s += bytes([0xB0, 0, DIM, 0xB1, 0, RED])           # LED 0: slot 0 dim, 1 red
    s += bytes([0xB0, 5, WHITE])                       # LED 5: slot 0 white
    s += bytes([0xB8])                                 # end of frame
    s += bytes([0x20, 0b01])                           # group 0: LED 0 -> slot 1
    s += bytes([0x21, 0b00000100])                     # group 1: LED 5 -> slot 1
    s += bytes([0xB7, 0x40, 0x05])                     # contrast, then an ignored byte
    return bytes(s)


class Decode(unittest.TestCase):
    def test_whole_stream(self):
        st = panelleds.LedState().feed(stream())
        self.assertEqual(st.skipped, 0)
        self.assertEqual(st.tail, b'')
        self.assertEqual(st.index(0), RED)
        self.assertEqual(st.index(1), None)            # selected slot undefined
        self.assertEqual(st.index(5), None)            # LED 5's slot 1 undefined
        self.assertEqual(st.index(8), None)            # group 2 never selected
        self.assertEqual(st.contrast, 0x40)
        self.assertEqual(st.colours(), {0: (255, 0, 0)})

    def test_any_chunking_gives_the_same_state(self):
        whole = panelleds.LedState().feed(stream())
        rng = random.Random(1)
        data = stream()
        for _ in range(50):
            st, i = panelleds.LedState(), 0
            while i < len(data):
                n = rng.randint(1, 7)
                st.feed(data[i:i + n])
                i += n
            self.assertEqual((st.slot, st.sel, st.palette, st.tail),
                             (whole.slot, whole.sel, whole.palette, b''))

    def test_selected_slot_redefined_recolours_at_once(self):
        st = panelleds.LedState().feed(stream())
        st.feed(bytes([0xB1, 0, WHITE]))
        self.assertEqual(st.colours()[0], (255, 255, 255))

    def test_palette_change_recolours_every_user(self):
        st = panelleds.LedState().feed(stream())
        st.feed(bytes([0xB5, RED, 0, 31, 0]))
        self.assertEqual(st.colours()[0], (0, 255, 0))

    def test_selector_moves_to_another_slot(self):
        st = panelleds.LedState().feed(stream())
        st.feed(bytes([0x20, 0b00]))
        self.assertEqual(st.colours()[0], (8, 8, 8))

    def test_message_lengths_follow_the_mcu_parser(self):
        self.assertEqual(panelleds.msg_len(0x03), 1)
        self.assertEqual(panelleds.msg_len(0x17), 10)
        self.assertEqual(panelleds.msg_len(0x2A), 2)
        self.assertEqual(panelleds.msg_len(0x60), 2)
        self.assertEqual(panelleds.msg_len(0xB5), 5)
        self.assertEqual(panelleds.msg_len(0xB4), 6)
        self.assertIsNone(panelleds.msg_len(0xB9))


class Seed(unittest.TestCase):
    def ram(self, palette_entry=(0, 31, 0, 0), slot_value=RED):
        where = panelleds.SEED['1.53']
        mem = {}
        cache = bytearray([0xFF]) * ((panelleds.LEDS + 1) * 4)
        cache[(0 + 1) * 4 + 2] = slot_value            # LED 0, slot 2
        mem[where['slot_cache']] = bytes(cache)
        mem[where['selectors']] = bytes([0b10] + [0] * 10)   # LED 0 -> slot 2
        pal = bytearray(panelleds.PALETTE * 4)
        pal[RED * 4:RED * 4 + 4] = bytes(palette_entry)
        mem[where['palette']] = bytes(pal)
        return lambda addr, n: mem[addr][:n]

    def test_seed_reproduces_the_shadow(self):
        st = panelleds.seed(self.ram(), '1.53')
        self.assertEqual(st.index(0), RED)
        self.assertEqual(st.colours()[0], (255, 0, 0))
        self.assertIsNone(st.index(1))                 # slot 0 is FF: undefined

    def test_unknown_version_is_not_seeded(self):
        self.assertIsNone(panelleds.seed(self.ram(), '1.52'))

    def test_implausible_ram_is_refused(self):
        self.assertIsNone(panelleds.seed(self.ram((0, 32, 0, 0)), '1.53'))
        self.assertIsNone(panelleds.seed(self.ram((1, 0, 0, 0)), '1.53'))
        self.assertIsNone(panelleds.seed(self.ram(slot_value=41), '1.53'))

    def test_the_digitakt_names_itself_or_not(self):
        self.assertIsNotNone(panelleds.seed(self.ram(), '1.53', 'Digitakt'))
        # Another product never borrows the Digitakt's bare-version table.
        self.assertIsNone(panelleds.seed(self.ram(), '1.53', 'Digitone'))


class DigitoneSeed(unittest.TestCase):
    """The Digitone's panel has 72 LEDs in 18 selector groups."""

    def ram(self):
        where = panelleds.SEED[('Digitone', '1.43')]
        mem = {}
        cache = bytearray([0xFF]) * ((where['leds'] + 1) * 4)
        cache[(70 + 1) * 4 + 1] = RED                  # LED 70, slot 1
        mem[where['slot_cache']] = bytes(cache)
        sel = bytearray(where['groups'])
        sel[70 >> 2] = 0b01 << ((70 & 3) * 2)          # LED 70 -> slot 1
        mem[where['selectors']] = bytes(sel)
        pal = bytearray(panelleds.PALETTE * 4)
        pal[RED * 4:RED * 4 + 4] = bytes((0, 31, 0, 0))
        mem[where['palette']] = bytes(pal)
        return lambda addr, n: mem[addr][:n]

    def test_seeds_past_the_digitakts_44(self):
        st = panelleds.seed(self.ram(), '1.43', 'Digitone')
        self.assertEqual((st.leds, st.groups), (72, 18))
        self.assertEqual(st.colours()[70], (255, 0, 0))

    def test_a_fresh_state_is_sized_for_the_product(self):
        st = panelleds.new_state('1.43', 'Digitone')
        self.assertEqual((st.leds, st.groups), (72, 18))
        st.feed(bytes([0x2F, 0x01]))                   # group 15's selectors
        self.assertEqual(st.sel[15], 0x01)
        dt = panelleds.new_state('1.53', 'Digitakt')
        self.assertEqual((dt.leds, dt.groups), (44, 11))
        dt.feed(bytes([0x2F, 0x01]))                   # past its 11 groups
        self.assertEqual(dt.sel, [None] * 11)


class DeviceFile(unittest.TestCase):
    def test_digitakt_led_map(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dev = device.load(os.path.join(root, 'devices', 'digitakt.toml'))
        self.assertEqual(len(dev.leds), 39)
        self.assertEqual([dev.leds[i] for i in range(16)], list(range(24, 40)))
        self.assertEqual(dev.leds[27], 13)             # NO lights NO
        self.assertEqual(dev.labels[dev.leds[27]], 'NO')
        self.assertEqual(dev.page_leds, (43, 42, 41, 40))
        self.assertFalse(set(dev.page_leds) & set(dev.leds))

    def test_digitone_led_map(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dev = device.load(os.path.join(root, 'devices', 'digitone.toml'))
        self.assertEqual([dev.leds[i] for i in range(16)], list(range(26, 42)))
        self.assertEqual(dev.labels[dev.leds[39]], 'PLAY')
        self.assertEqual(dev.labels[dev.leds[46]], 'T1')
        self.assertEqual(dev.page_leds, (45, 44, 43, 42))
        self.assertFalse(set(dev.page_leds) & set(dev.leds))
        self.assertTrue(all(led < 72 for led in dev.leds))
        # every mapped code is a key the device can press
        for code in dev.leds.values():
            self.assertIsNotNone(dev.wire_for(code), code)


if __name__ == '__main__':
    unittest.main()
