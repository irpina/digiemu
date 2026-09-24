# pyright: reportMissingImports=false
"""Device files: identity by hash, and the wire mapping.

`emu/config.py` settles a tie between firmware files by hardcoded FILENAME,
which is how one product silently runs under another's name. A device file
keys on the firmware's SHA-256 instead. These tests cover that, the
(channel, bit) <-> control-code mapping including each product's non-linear
channel 6, and that the real files in devices/ parse and round-trip.

No firmware and no emulator: the shipped device files are plain data, and
everything else is built in a temp directory.
"""
import os
import tempfile
import unittest

from emu import device


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES = os.path.join(REPO, 'devices')

SYNTHETIC = '''
[device]
name = "Test Device"
short = "td"

[[firmware]]
version = "9.9Z"
sha256 = "AABBCC"
filename = "Test_OS9.9Z.syx"

[panel]
linear_channels = 2
encoders = 3

[panel.exceptions]
20 = [6, 1]

[[panel.group]]
name = "keys"
kind = "button"
codes = [1, 2]
layout = "row"

[[panel.group]]
name = "odd"
kind = "button"
codes = [20]
layout = "single"

[[panel.group]]
name = "encoders"
kind = "encoder"
codes = [1, 2, 3]
layout = "row"
'''


def write_device(dirpath, text=SYNTHETIC, name='test.toml'):
    path = os.path.join(dirpath, name)
    with open(path, 'w') as fh:
        fh.write(text)
    return path


class ParseTest(unittest.TestCase):
    def test_parses_identity_and_panel(self):
        with tempfile.TemporaryDirectory() as d:
            dev = device.load(write_device(d))
        self.assertEqual(dev.name, 'Test Device')
        self.assertEqual(dev.short, 'td')
        self.assertEqual(dev.linear_channels, 2)
        self.assertEqual(dev.encoders, 3)
        self.assertEqual(len(dev.groups), 3)

    def test_sha256_is_lowercased(self):
        # Hashes get pasted in both cases; comparison must not care.
        with tempfile.TemporaryDirectory() as d:
            dev = device.load(write_device(d))
        self.assertEqual(dev.firmwares[0].sha256, 'aabbcc')
        self.assertIsNotNone(dev.firmware_for_sha256('AaBbCc'))

    def test_exception_codes_are_integers(self):
        # TOML bare keys are strings even when they look like integers, so
        # '20' must arrive as 20 or every lookup silently misses.
        with tempfile.TemporaryDirectory() as d:
            dev = device.load(write_device(d))
        self.assertEqual(dev.exceptions[20], (6, 1))

    def test_missing_section_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_device(d, '[device]\nname = "X"\n')
            with self.assertRaises(device.DeviceError):
                device.load(path)

    def test_card_panel_kind_and_encoder_counts_default(self):
        # A file that says nothing keeps the Digitakt's behaviour: an ekFS
        # card, the Digitakt's window and one wire count per detent.
        with tempfile.TemporaryDirectory() as d:
            dev = device.load(write_device(d))
        self.assertTrue(dev.card_ekfs)
        self.assertIsNone(dev.panel_kind)
        self.assertEqual(dev.encoder_counts, 1)

    def test_card_panel_kind_and_encoder_counts(self):
        text = SYNTHETIC.replace(
            '[panel]\n', '[card]\nekfs = false\n\n[panel]\nkind = "digitone"\n'
            'encoder_counts = 4\n')
        with tempfile.TemporaryDirectory() as d:
            dev = device.load(write_device(d, text))
        self.assertFalse(dev.card_ekfs)
        self.assertEqual(dev.panel_kind, 'digitone')
        self.assertEqual(dev.encoder_counts, 4)

    def test_bad_card_and_counts_are_refused(self):
        for bad in ('[card]\nekfs = "no"\n\n[panel]\n',
                    '[panel]\nencoder_counts = 0\n',
                    '[panel]\nencoder_counts = 17\n',
                    '[panel]\nencoder_counts = true\n'):
            with tempfile.TemporaryDirectory() as d:
                path = write_device(d, SYNTHETIC.replace('[panel]\n', bad))
                with self.assertRaises(device.DeviceError, msg=bad):
                    device.load(path)

    def test_ddr_size(self):
        """[memory] ddr_mb, for the strict check's DDR model: one part of
        16 to 256 MB (MCF54418RM 1.7.11), absent meaning unknown."""
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(device.load(write_device(d)).ddr_bytes)
        with tempfile.TemporaryDirectory() as d:
            path = write_device(d, SYNTHETIC.replace(
                '[panel]\n', '[memory]\nddr_mb = 64\n\n[panel]\n'))
            self.assertEqual(device.load(path).ddr_bytes, 64 << 20)
        for bad in ('48', '512', 'true'):
            with tempfile.TemporaryDirectory() as d:
                path = write_device(d, SYNTHETIC.replace(
                    '[panel]\n', '[memory]\nddr_mb = %s\n\n[panel]\n' % bad))
                with self.assertRaises(device.DeviceError, msg=bad):
                    device.load(path)

    def test_the_mk1_products_have_64_mb(self):
        for name in ('digitakt.toml', 'digitone.toml'):
            dev = device.load(os.path.join(DEVICES, name))
            self.assertEqual(dev.ddr_bytes, 64 << 20, name)

    def test_boot_panel_facts(self):
        """[boot]: what the panel tells a bootstrap (emu/bootrom.py)."""
        dt = device.load(os.path.join(DEVICES, 'digitakt.toml'))
        dn = device.load(os.path.join(DEVICES, 'digitone.toml'))
        self.assertEqual((dt.ui_card, dt.button_groups(), dt.straps),
                         (4, 6, {}))
        self.assertEqual((dn.ui_card, dn.button_groups(), dn.straps),
                         (8, 7, {0xEC09401B: 0x08}))
        for bad in ('ui_card = 300', 'straps = { "PORT" = 1 }',
                    'straps = { "0x10" = 256 }'):
            with tempfile.TemporaryDirectory() as d:
                path = write_device(d, SYNTHETIC.replace(
                    '[panel]\n', '[boot]\n%s\n\n[panel]\n' % bad))
                with self.assertRaises(device.DeviceError, msg=bad):
                    device.load(path)


class WireMappingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dev = device.load(write_device(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_linear_region(self):
        self.assertEqual(self.dev.wire_for(1), (0, 0))
        self.assertEqual(self.dev.wire_for(9), (1, 0))
        self.assertEqual(self.dev.wire_for(16), (1, 7))

    def test_exception_wins_over_the_formula(self):
        self.assertEqual(self.dev.wire_for(20), (6, 1))
        self.assertEqual(self.dev.code_at(6, 1), 20)

    def test_code_beyond_this_product_has_no_wire_position(self):
        # Not an error: one product simply carries fewer controls.
        self.assertIsNone(self.dev.wire_for(40))

    def test_encoder_codes_are_a_separate_space(self):
        self.assertEqual(self.dev.encoder_channel(1), 0)
        self.assertEqual(self.dev.encoder_channel(3), 2)
        self.assertIsNone(self.dev.encoder_channel(4))


class IdentifyTest(unittest.TestCase):
    def test_unknown_hash_is_refused_not_guessed(self):
        with tempfile.TemporaryDirectory() as d:
            write_device(d)
            target = os.path.join(d, 'firmware.syx')
            with open(target, 'wb') as fh:
                fh.write(b'not a known firmware')
            with self.assertRaises(device.DeviceError):
                device.identify(target, d)

    def test_matches_by_content_not_filename(self):
        with tempfile.TemporaryDirectory() as d:
            payload = b'pretend firmware'
            import hashlib
            sha = hashlib.sha256(payload).hexdigest()
            write_device(d, SYNTHETIC.replace('"AABBCC"', '"%s"' % sha))
            # A name that matches no filename field in the device file.
            target = os.path.join(d, 'renamed-by-the-user.syx')
            with open(target, 'wb') as fh:
                fh.write(payload)
            dev, fw = device.identify(target, d)
            self.assertEqual(dev.name, 'Test Device')
            self.assertEqual(fw.version, '9.9Z')

    def test_missing_directory_is_refused(self):
        with self.assertRaises(device.DeviceError):
            device.load_all('/nonexistent-devices-dir')


class ShippedDeviceFilesTest(unittest.TestCase):
    """The real files in devices/ -- data only, no firmware needed."""

    def setUp(self):
        self.devices = device.load_all(DEVICES)

    def test_every_shipped_product_is_present(self):
        # Three since the mk1 port added devices/digitakt.toml, four since
        # devices/digitone.toml. This used to read "both products" and assert
        # the two Digitakt II-era ones.
        names = sorted(d.name for d in self.devices)
        self.assertEqual(names, ['Digitakt', 'Digitakt II', 'Digitone',
                                 'Digitone II'])

    def test_digitone_panel_is_complete(self):
        """devices/digitone.toml: every measured code 1..54 has a label, a
        wire position of its own and at most one LED; the four page LEDs
        light no key; it has no sample volume and its own window."""
        dn = {d.name: d for d in self.devices}['Digitone']
        self.assertEqual(sorted(dn.labels), list(range(1, 55)))
        self.assertEqual(len(set(dn.labels.values())), 54)
        wires = [dn.wire_for(c) for c in range(1, 55)]
        self.assertNotIn(None, wires)
        self.assertEqual(len(set(wires)), 54)
        self.assertIsNone(dn.wire_for(0))            # 0 is no key
        self.assertEqual(len(set(dn.leds.values())), len(dn.leds))
        self.assertTrue(set(dn.leds.values()) <= set(dn.labels))
        self.assertFalse(set(dn.page_leds) & set(dn.leds))
        self.assertEqual(dn.labels[11], 'PLAY')
        self.assertFalse(dn.card_ekfs)
        self.assertEqual(dn.panel_kind, 'digitone')
        dt = {d.name: d for d in self.devices}['Digitakt']
        self.assertTrue(dt.card_ekfs)
        self.assertIsNone(dt.panel_kind)

    def test_every_button_code_round_trips(self):
        for dev in self.devices:
            for code in dev.button_codes():
                pos = dev.wire_for(code)
                self.assertIsNotNone(pos, '%s code %d' % (dev.name, code))
                self.assertEqual(dev.code_at(*pos), code,
                                 '%s code %d' % (dev.name, code))

    def test_products_differ_where_the_hardware_does(self):
        by_name = {d.name: d for d in self.devices}
        # Digitakt's name table ends at 50; Digitone carries five more.
        self.assertEqual(max(by_name['Digitakt II'].button_codes()), 50)
        self.assertEqual(max(by_name['Digitone II'].button_codes()), 54)

    def test_no_duplicate_codes_within_a_kind(self):
        for dev in self.devices:
            codes = dev.button_codes()
            self.assertEqual(len(codes), len(set(codes)), dev.name)

    def test_each_has_exactly_one_known_firmware_hash(self):
        for dev in self.devices:
            self.assertEqual(len(dev.firmwares), 1, dev.name)
            self.assertEqual(len(dev.firmwares[0].sha256), 64, dev.name)


if __name__ == '__main__':
    unittest.main()
