"""emu/fwcheck.py's container stage, on a synthetic .syx built with the
repo's own packer (dt2/build.py): nothing here is firmware. The public tree
has no firmware-building code, so there these tests skip. The later stages
run the emulator on a real build; see docs/FIRMWARE-CHECK.md."""
import os
import struct
import tempfile
import unittest

from emu import fwcheck

try:
    from dt2 import aplib, build
except ImportError:             # the public tree: no firmware-building code
    aplib = build = None

DEVICE_ID, OS_STREAM = 0x0A, 0x05       # Digitakt: devices/digitakt.toml


def framing(kind):
    """A 16-byte framing message from its documented fields: the transport
    id, the OS-stream id (the checksum constant) and the message count,
    which encode_syx fills in."""
    return bytes([0xF0, 0x00, 0x20, 0x3C, DEVICE_ID, 0x00, 0x7F, kind,
                  OS_STREAM, 0, 0, 0, 0, 0, 0, 0xF7])


def container(bootstrap_version=0x0300, main_dest=0x40000400):
    header = b'ELE3' + struct.pack('>I', 0x2C) + b'0104        ' + \
        b'1.53' + bytes(4)
    boot = struct.pack('>I', 12) + struct.pack('>III', 0x80010000,
                                               0x80000EAA, 0x03000900)
    main = struct.pack('>I', 0x400004E8) + bytes(range(256)) * 8
    entries = [(5, 0, b'260908 14:38:44'),
               (2, bootstrap_version << 16 | 0x0900, aplib.pack_section(boot)),
               (3, main_dest, aplib.pack_section(main)),
               (4, 0x80000400, build.store_for_section(4, bytes(range(1, 65))))]
    return build.build_container(header, entries)


def syx(c, checksum=None, corrupt_message=None):
    stream = struct.pack('>II', len(c), build.content_checksum(c)
                         if checksum is None else checksum) + c
    data = bytearray(build.encode_syx(stream, DEVICE_ID, framing(1),
                                      framing(2)))
    if corrupt_message is not None:
        at = 16 + 128 * corrupt_message + 126     # the checksum byte
        data[at] ^= 0x01
    fd, path = tempfile.mkstemp(suffix='.syx')
    os.write(fd, bytes(data))
    os.close(fd)
    return path


@unittest.skipIf(build is None, 'needs dt2/build.py to build a .syx')
class ContainerTest(unittest.TestCase):
    def check(self, path, baseline=None):
        self.addCleanup(os.remove, path)
        st = fwcheck.Stage('container')
        table = fwcheck.check_container(path, st, baseline)
        return st, table

    def test_a_good_file_passes(self):
        st, table = self.check(syx(container()))
        self.assertEqual(st.errors, [])
        self.assertEqual(sorted(table), [2, 3, 4, 5])
        self.assertEqual(st.facts['message_checksums_bad'], 0)
        self.assertEqual(st.facts['bootstrap_version'], '0x0300')
        self.assertTrue(st.facts['preamble_covers_trailer'])
        self.assertEqual(st.facts['release']['product'], 'Digitakt')

    def test_a_bad_message_checksum(self):
        st, _ = self.check(syx(container(), corrupt_message=3))
        self.assertTrue(any('fail their checksum' in e for e in st.errors))

    def test_a_bad_content_checksum(self):
        st, _ = self.check(syx(container(), checksum=0x12345678))
        self.assertTrue(any('content checksum' in e for e in st.errors))

    def test_main_os_elsewhere(self):
        st, _ = self.check(syx(container(main_dest=0x40100000)))
        self.assertTrue(any('MAIN OS loads at' in e for e in st.errors))

    def test_a_bootstrap_upgrade_fails(self):
        st, _ = self.check(syx(container(bootstrap_version=0x0301)),
                           baseline={'bootstrap_version': '0x0300'})
        self.assertTrue(any('cannot be undone' in e for e in st.errors))

    def test_an_older_bootstrap_is_a_warning(self):
        st, _ = self.check(syx(container(bootstrap_version=0x0200)),
                           baseline={'bootstrap_version': '0x0300'})
        self.assertEqual(st.errors, [])
        self.assertTrue(st.warnings)

    def test_the_stock_preamble_length_is_noted(self):
        st, _ = self.check(syx(container()),
                           baseline={'preamble_covers_trailer': False})
        self.assertTrue(any('trailer slot' in w for w in st.warnings))

    def test_a_damaged_packed_section(self):
        c = bytearray(container())
        # section 3 is the third table entry; flip a byte of its stream
        sid, off, clen, _dest = struct.unpack_from('>IIII', c, 0x20 + 2 * 16)
        self.assertEqual(sid, 3)
        c[off + 20] ^= 0xFF
        st, _ = self.check(syx(bytes(c)),
                           baseline={'sections': {'3': {'kind': 'packed'}}})
        self.assertTrue(any('header does not match' in e for e in st.errors),
                        st.errors)

    def test_too_big_for_the_flash(self):
        old = fwcheck.FLASH_LIMIT
        fwcheck.FLASH_LIMIT = 0x80100
        self.addCleanup(setattr, fwcheck, 'FLASH_LIMIT', old)
        st, _ = self.check(syx(container()))
        self.assertTrue(any('0x380000' in e or 'past' in e for e in st.errors))


class PrepareTest(unittest.TestCase):
    """Every check boots its build afresh. A stock build checked against
    itself once took the baseline's saved boot, and its boot stage 'passed'
    in 0 s without running."""

    def test_a_fresh_folder_per_role_and_per_check(self):
        from unittest import mock

        from emu import device as devmod
        from emu import release as rmod
        rel = mock.Mock(status='known', device=mock.Mock(short='td'))
        with tempfile.TemporaryDirectory() as work, \
                mock.patch.dict(os.environ), \
                mock.patch.object(rmod, 'identify_release', return_value=rel), \
                mock.patch.object(devmod, 'identify',
                                  return_value=('device', None)):
            path = os.path.join(work, 'Test_OS9.9.syx')
            with open(path, 'wb') as fh:
                fh.write(b'not firmware')
            base, _ = fwcheck.prepare(path, work, 'baseline')
            build_paths, _ = fwcheck.prepare(path, work, 'build')
            self.assertNotEqual(base.root, build_paths.root)
            left = os.path.join(build_paths.root, 'firmware.json')
            with open(left, 'w') as fh:
                fh.write('{"stages": {"settle": {}}}')   # a finished boot
            again, _ = fwcheck.prepare(path, work, 'build')
            self.assertEqual(again.root, build_paths.root)
            self.assertFalse(os.path.exists(left))
            self.assertTrue(os.path.exists(again.syx))


class RunCheckTest(unittest.TestCase):
    """run_check around check_build: the verdict first and the stock
    build's findings last, events tagged with their build, and the work
    folders gone when keep_work is off (the launcher's check)."""

    def test_order_events_and_clean_up(self):
        from unittest import mock
        seen = []

        def fake_build(path, work, *, role, on_event, baseline=None,
                       log=print, **kw):
            os.makedirs(os.path.join(work, role, 'card'))
            on_event({'kind': 'start', 'step': 'boot'})
            st = fwcheck.Stage('boot')
            if role == 'baseline':
                st.error('a stock quirk')
            return {'passed': st.passed, 'stages': {'boot': st.as_dict()}}, \
                object()

        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(fwcheck, 'check_build', fake_build), \
                mock.patch('emu.fwcompare.compare',
                           return_value={'identical': True}), \
                mock.patch('emu.fwcompare.summary',
                           return_value=['screens and sound are identical']):
            full = fwcheck.run_check(
                'build.syx', out, baseline='stock.syx', keep_work=False,
                log=lambda text: None,
                on_event=lambda role, rec: seen.append(
                    (role, rec['kind'], rec['step'])))
            self.assertFalse(os.path.exists(os.path.join(out, 'work')))
            self.assertTrue(os.path.exists(os.path.join(out, 'report.json')))
        self.assertTrue(full['passed'])
        self.assertEqual(full['summary'], [
            'build: PASS', '  boot       pass',
            'compared with the stock build:',
            '  screens and sound are identical',
            'stock build, for reference: FAIL', '  boot       FAIL',
            '      - a stock quirk'])
        self.assertEqual(seen, [('baseline', 'start', 'boot'),
                                ('build', 'start', 'boot'),
                                ('compare', 'start', 'compare'),
                                ('compare', 'done', 'compare')])


class SummaryTest(unittest.TestCase):
    def test_summary_lines(self):
        st = fwcheck.Stage('run')
        st.error('something')
        report = {'passed': False, 'stages': {'run': st.as_dict()}}
        lines = fwcheck.summary(report, 'custom')
        self.assertEqual(lines[0], 'custom: FAIL')
        self.assertIn('      - something', lines)


if __name__ == '__main__':
    unittest.main()
