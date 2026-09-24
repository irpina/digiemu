# pyright: reportMissingImports=false
"""Release identification: what a .syx is, before the first run builds it.

emu/release.py reads the SysEx framing (product ids, message count) and the
ELE3 header (build, version, stamp) from the start of the file, hashes the
rest, and decides known / untested / unsupported against the device files.
These tests build their .syx files from scratch: a framing message, 128-byte
0x7E data messages carrying an 8-in-7 encoded ELE3 container, and a closing
framing message. The builder is deliberately minimal and UNFLASHABLE -- the
preamble checksum and every per-message checksum are zero, and there is no
code in it -- because all it has to exercise is the parser.

The one test that reads real firmware skips when the file is absent.
"""
import datetime
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from emu import device, release


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES = os.path.join(REPO, 'devices')
REAL_SYX = os.path.join(REPO, 'Digitakt_OS1.53.syx')

DT1 = (0x0A, 0x05)
DT2 = (0x14, 0x0F)
SYNTAKT = (0x16, 0x11)

CHUNK = 101          # decoded bytes per 128-byte data message


# --- a synthetic OS .syx -----------------------------------------------------

def ele3(build=b'0104', version=b'1.53', stamp=b'260908 14:38:44',
         body=b''):
    """-> a small ELE3 container: header, section table, stamp, one blob."""
    entries, blobs, off = [], [], 0x80
    if stamp is not None:
        entries.append((5, off, len(stamp), 0))
        blobs.append((off, stamp))
        off += (len(stamp) + 15) & ~15
    entries.append((3, off, len(body), 0x40000400))
    blobs.append((off, body))
    head = (b'ELE3' + struct.pack('>I', 0x2C) + build[:4].ljust(4)
            + version[-12:].rjust(12) + struct.pack('>II', 0, len(entries)))
    c = bytearray(head)
    for e in entries:
        c += struct.pack('>IIII', *e)
    for at, blob in blobs:
        c += bytes(at - len(c))
        c += blob
    return bytes(c)


def encode_8in7(data):
    out = bytearray()
    for start in range(0, len(data), 7):
        group = data[start:start + 7]
        marker = 0
        for n, b in enumerate(group):
            if b & 0x80:
                marker |= 1 << (6 - n)
        out.append(marker)
        out.extend(b & 0x7F for b in group)
    return bytes(out)


def framing(ids, kind, count):
    tid, sid = ids
    return bytes([0xF0, 0x00, 0x20, 0x3C, tid, 0x00, 0x7F, kind, sid,
                  0x00, 0x01, 0x72, (count >> 14) & 0x7F,
                  (count >> 7) & 0x7F, count & 0x7F, 0xF7])


def make_syx(ids=DT1, build=b'0104', version=b'1.53',
             stamp=b'260908 14:38:44', body_len=700, seed=0,
             announce=None, drop=0):
    """-> .syx bytes. `announce` overrides the framing's message count and
    `drop` leaves out that many trailing data messages (the framing still
    counts them), which is what a truncated download looks like."""
    body = bytes((i * 37 + seed * 11 + 0x81) & 0xFF for i in range(body_len))
    c = ele3(build, version, stamp, body)
    stream = struct.pack('>II', len(c), 0) + c
    chunks = [stream[k:k + CHUNK].ljust(CHUNK, b'\0')
              for k in range(0, len(stream), CHUNK)]
    count = len(chunks) if announce is None else announce
    out = bytearray(framing(ids, 0x01, count))
    for n, chunk in enumerate(chunks[:len(chunks) - drop]):
        counter = 242 + n
        payload = encode_8in7(chunk)
        assert len(payload) == 116
        out += bytes([0xF0, 0x00, 0x20, 0x3C, ids[0], 0x00, 0x7E,
                      (counter >> 14) & 0x7F, (counter >> 7) & 0x7F,
                      counter & 0x7F])
        out += payload + b'\x00\xf7'      # zero checksum: unflashable
    out += framing(ids, 0x02, count)
    return bytes(out)


DEVICE_TOML = '''\
[device]
name = "%(name)s"
short = "%(short)s"
sysex_id = %(tid)#04x
os_stream_id = %(sid)#04x

[[firmware]]
version = "%(version)s"
sha256 = "%(sha)s"
filename = "%(filename)s"

[panel]
linear_channels = 6
encoders = 9
'''


def write(path, data):
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def write_toml(dirpath, name='Digitakt', short='dt1', ids=DT1,
               version='1.53', sha='0' * 64,
               filename='Digitakt_OS1.53.syx', fname='test.toml'):
    text = DEVICE_TOML % dict(name=name, short=short, tid=ids[0],
                              sid=ids[1], version=version, sha=sha,
                              filename=filename)
    with open(os.path.join(dirpath, fname), 'w', encoding='utf-8',
              newline='\n') as fh:
        fh.write(text)


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.devdir = os.path.join(self.tmp, 'devices')
        os.makedirs(self.devdir)

    def tearDown(self):
        self._tmp.cleanup()

    def syx(self, name='fw.syx', **kw):
        return write(os.path.join(self.tmp, name), make_syx(**kw))


# --- the header --------------------------------------------------------------

class HeaderTest(Tmp):
    def test_reads_ids_count_build_version_and_stamp(self):
        path = self.syx()
        h = release.read_header(path)
        self.assertEqual((h.transport_id, h.stream_id), DT1)
        with open(path, 'rb') as fh:
            data = fh.read()
        self.assertEqual(h.msg_count, (len(data) - 32) // 128)
        self.assertEqual(h.build, '0104')
        self.assertEqual(h.version, '1.53')
        self.assertEqual(h.stamp, datetime.datetime(2026, 9, 8, 14, 38, 44))
        self.assertEqual(h.sections, [5, 3])

    def test_five_character_version_is_read_whole(self):
        # The field is right-justified in 12 bytes: a 4-byte read at +0x14
        # turns '1.15C' into '.15C'.
        h = release.read_header(self.syx(ids=DT2, build=b'0071',
                                         version=b'1.15C'))
        self.assertEqual(h.version, '1.15C')
        self.assertEqual(h.build, '0071')

    def test_missing_or_unparseable_stamp_is_none(self):
        h = release.read_header(self.syx(stamp=None))
        self.assertIsNone(h.stamp)
        self.assertEqual(h.sections, [3])
        h = release.read_header(self.syx(stamp=b'not a timestamp'))
        self.assertIsNone(h.stamp)

    def test_non_printable_version_bytes_do_not_escape(self):
        h = release.read_header(self.syx(version=b'1.\x01\xff'))
        self.assertEqual(h.version, '1.??')

    def test_header_does_not_need_the_rest_of_the_file(self):
        # read_header is the fast path; truncation is identify's to find.
        data = make_syx(body_len=3000)
        path = write(os.path.join(self.tmp, 'cut.syx'), data[:600])
        self.assertEqual(release.read_header(path).version, '1.53')


# --- status ------------------------------------------------------------------

class StatusTest(Tmp):
    def test_known_when_a_device_file_lists_the_hash(self):
        path = self.syx()
        sha = device.sha256_of(path)
        write_toml(self.devdir, sha=sha)
        r = release.identify_release(path, self.devdir)
        self.assertEqual(r.status, 'known')
        self.assertEqual(r.product, 'Digitakt')
        self.assertEqual(r.device.name, 'Digitakt')
        self.assertEqual(r.firmware.sha256, sha)
        self.assertEqual(r.sha256, sha)
        self.assertEqual((r.version, r.build), ('1.53', '0104'))
        self.assertEqual(r.label, 'Digitakt OS 1.53')
        self.assertEqual(r.slug, 'dt1-1.53-' + sha[:8])
        self.assertEqual(release.canonical_syx_name(r), 'Digitakt_OS1.53.syx')

    def test_untested_when_the_product_is_known_but_the_hash_is_not(self):
        path = self.syx(version=b'1.99', build=b'0105')
        write_toml(self.devdir)
        r = release.identify_release(path, self.devdir)
        self.assertEqual(r.status, 'untested')
        self.assertEqual(r.device.name, 'Digitakt')
        self.assertEqual(r.version, '1.99')
        self.assertEqual(r.firmware.version, '1.99')
        self.assertEqual(r.firmware.sha256, r.sha256)
        self.assertEqual(r.label, 'Digitakt OS 1.99 (build 0105, built '
                                  '2026-09-08, untested)')
        self.assertEqual(release.canonical_syx_name(r), 'Digitakt_OS1.99.syx')
        self.assertEqual(r.firmware.filename, 'Digitakt_OS1.99.syx')

    def test_untested_label_without_a_stamp(self):
        write_toml(self.devdir)
        r = release.identify_release(self.syx(stamp=None), self.devdir)
        self.assertEqual(r.label, 'Digitakt OS 1.53 (build 0104, untested)')

    def test_untested_five_character_version_against_the_shipped_files(self):
        path = self.syx(ids=DT2, build=b'0071', version=b'1.15C', seed=3)
        r = release.identify_release(path, DEVICES)
        self.assertEqual(r.status, 'untested')
        self.assertEqual(r.product, 'Digitakt II')
        self.assertEqual(r.version, '1.15C')
        self.assertEqual(r.slug, 'dt2-1.15c-' + r.sha256[:8])
        self.assertEqual(release.canonical_syx_name(r),
                         'Digitakt_II_OS1.15C.syx')

    def test_unsupported_product_is_named_and_refused(self):
        path = self.syx(ids=SYNTAKT, version=b'1.20')
        r = release.identify_release(path, DEVICES)
        self.assertEqual(r.status, 'unsupported')
        self.assertEqual(r.product, 'Syntakt')
        self.assertIsNone(r.device)
        self.assertIsNone(r.firmware)
        self.assertEqual(r.label, 'Syntakt OS 1.20 (not supported yet)')
        self.assertEqual(r.slug, 'syn-1.20-' + r.sha256[:8])
        self.assertEqual(release.canonical_syx_name(r), 'Syntakt_OS1.20.syx')
        with self.assertRaises(release.FirmwareError):
            release.write_device_overlay(r, os.path.join(self.tmp, 'ov'),
                                         DEVICES)

    def test_unknown_product_ids_get_a_generic_name(self):
        r = release.identify_release(self.syx(ids=(0x30, 0x01)), DEVICES)
        self.assertEqual(r.status, 'unsupported')
        self.assertEqual(r.product, 'Elektron device 0x30/0x01')
        self.assertTrue(release.valid_slug(r.slug), r.slug)

    def test_hash_listed_under_another_product_is_a_contradiction(self):
        path = self.syx()           # Digitakt ids ...
        write_toml(self.devdir, name='Digitakt II', short='dt2', ids=DT2,
                   sha=device.sha256_of(path))   # ... listed as Digitakt II
        with self.assertRaises(release.FirmwareError):
            release.identify_release(path, self.devdir)

    def test_broken_device_files_raise_firmware_error_not_system_exit(self):
        # DeviceError is a SystemExit; it must not reach the launcher as one.
        with self.assertRaises(release.FirmwareError):
            release.identify_release(self.syx(),
                                     os.path.join(self.tmp, 'nowhere'))


# --- damage and non-firmware -------------------------------------------------

class RejectTest(Tmp):
    def assertRefused(self, path):
        with self.assertRaises(release.FirmwareError):
            release.identify_release(path, DEVICES)

    def test_firmware_error_is_a_value_error(self):
        self.assertTrue(issubclass(release.FirmwareError, ValueError))

    def test_missing_data_messages_are_caught(self):
        path = self.syx(drop=1)
        release.read_header(path)             # the header is intact ...
        with self.assertRaises(release.FirmwareError) as cm:
            release.identify_release(path, DEVICES)   # ... the file is not
        self.assertIn('truncated', str(cm.exception))

    def test_count_mismatch_is_caught(self):
        n = (len(make_syx()) - 32) // 128
        self.assertRefused(self.syx(announce=n + 5))

    def test_file_cut_mid_message_is_caught(self):
        data = make_syx()
        self.assertRefused(write(os.path.join(self.tmp, 'cut.syx'),
                                 data[:-200]))

    def test_short_data_message_is_caught(self):
        data = bytearray(make_syx())
        del data[16 + 128 + 40]              # one byte out of message 2
        self.assertRefused(write(os.path.join(self.tmp, 'bad.syx'),
                                 bytes(data)))

    def test_not_syx_at_all(self):
        cases = {
            'zip.syx': b'PK\x03\x04' + bytes(200),
            'empty.syx': b'',
            'text.syx': b'hello, this is not firmware\n' * 10,
            'universal.syx': bytes([0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]),
            'dump.syx': bytes([0xF0, 0x00, 0x20, 0x3C, 0x0A, 0x00, 0x53,
                               0x01, 0x02, 0x03, 0xF7]),
            'unterminated.syx': bytes([0xF0, 0x00, 0x20, 0x3C]) + bytes(40),
        }
        for name, data in cases.items():
            path = write(os.path.join(self.tmp, name), data)
            with self.assertRaises(release.FirmwareError, msg=name):
                release.read_header(path)
            self.assertRefused(path)

    def test_framing_without_an_ele3_container(self):
        data = bytearray(make_syx())
        data[16 + 10:16 + 126] = bytes(116)   # first data message -> zeros
        self.assertRefused(write(os.path.join(self.tmp, 'noele3.syx'),
                                 bytes(data)))

    def test_stray_bytes_between_messages(self):
        data = make_syx()
        self.assertRefused(write(os.path.join(self.tmp, 'junk.syx'),
                                 data[:144] + b'\n' + data[144:]))

    def test_unreadable_paths(self):
        self.assertRefused(os.path.join(self.tmp, 'absent.syx'))
        self.assertRefused(self.tmp)          # a directory


# --- names -------------------------------------------------------------------

class NamingTest(unittest.TestCase):
    SHA = '9bdd44bb' + '0' * 56

    def test_slug_shape(self):
        self.assertEqual(release.make_slug('dt1', '1.53', self.SHA),
                         'dt1-1.53-9bdd44bb')
        self.assertEqual(release.make_slug('dt2', '1.15C', self.SHA),
                         'dt2-1.15c-9bdd44bb')

    def test_weird_versions_still_give_safe_slugs(self):
        for version in ('', '   ', 'a/b', 'x:y', '..', '\x00', '\u00c6',
                        'x' * 300, 'CON', 'nul', '1.0 beta!', '-.-',
                        'C:\\x', '1.53.'):
            slug = release.make_slug('dt1', version, self.SHA)
            self.assertTrue(release.valid_slug(slug), (version, slug))
            self.assertLessEqual(len(slug), 64)
            self.assertTrue(slug.startswith('dt1-'), slug)
            self.assertTrue(slug.endswith('-9bdd44bb'), slug)
        self.assertEqual(release.make_slug('dt1', '', self.SHA),
                         'dt1-9bdd44bb')
        self.assertEqual(release.make_slug('dt1', 'a/b', self.SHA),
                         'dt1-a-b-9bdd44bb')

    def test_reserved_names_are_refused(self):
        for name in ('con', 'prn', 'aux', 'nul', 'com1', 'lpt9', 'nul.txt',
                     'aux.1-9bdd44bb'):
            self.assertFalse(release.valid_slug(name), name)
        with self.assertRaises(release.FirmwareError):
            release.make_slug('aux.x', '', self.SHA)

    def test_slug_charset(self):
        for bad in ('', 'Dt1-1', '-dt1', '.dt1', 'dt1 1', 'dt1-1.', 'a' * 65,
                    'dt1/1', 'dt1_1'):
            self.assertFalse(release.valid_slug(bad), bad)
        for good in ('dt1-1.53-9bdd44bb', 'con-1.53-9bdd44bb', 'a' * 64):
            self.assertTrue(release.valid_slug(good), good)

    def _release(self, product, version, status='untested', fw=None):
        return release.Release(device=None, firmware=fw, product=product,
                               version=version, build='', stamp=None,
                               sha256=self.SHA, status=status, label='',
                               slug='')

    def test_canonical_name_is_sanitised(self):
        name = release.canonical_syx_name
        self.assertEqual(name(self._release('Digitakt II', '1.15C')),
                         'Digitakt_II_OS1.15C.syx')
        self.assertEqual(name(self._release('Elektron device 0x30/0x01',
                                            '2.0')),
                         'Elektron_device_0x30_0x01_OS2.0.syx')
        self.assertEqual(name(self._release('Digitakt', 'a/b:c')),
                         'Digitakt_OSa_b_c.syx')
        self.assertEqual(name(self._release('Digitakt', '')),
                         'Digitakt_OSunknown.syx')

    def test_known_release_keeps_its_device_file_name(self):
        fw = device.Firmware('1.53', self.SHA, 'Digitakt_OS1.53.syx')
        self.assertEqual(release.canonical_syx_name(
            self._release('Digitakt', '1.53', 'known', fw)),
            'Digitakt_OS1.53.syx')
        # ... but only its base name, never a path out of the folder.
        fw = device.Firmware('1.53', self.SHA, '../../CON.syx')
        self.assertEqual(release.canonical_syx_name(
            self._release('Digitakt', '1.53', 'known', fw)), 'fw_CON.syx')


# --- the overlay -------------------------------------------------------------

class OverlayTest(Tmp):
    def test_overlay_makes_identify_accept_the_hash(self):
        path = self.syx(version=b'9.99', seed=7)
        r = release.identify_release(path, DEVICES)
        self.assertEqual(r.status, 'untested')
        with self.assertRaises(device.DeviceError):   # still strict ...
            device.identify(path, DEVICES)
        overlay = os.path.join(self.tmp, 'fw', 'devices')
        dst = release.write_device_overlay(r, overlay, DEVICES)
        self.assertEqual(os.path.dirname(dst), overlay)
        dev, fw = device.identify(path, overlay)       # ... except here
        self.assertEqual(dev.name, 'Digitakt')
        self.assertEqual(fw.version, '9.99')
        self.assertEqual(fw.filename, 'Digitakt_OS9.99.syx')
        # The copy is the whole device file: panel, intro, audio, ids and
        # the tested release it already listed.
        self.assertEqual(dev.intro_channels, (3,))
        self.assertIsNotNone(dev.audio)
        self.assertEqual((dev.sysex_id, dev.os_stream_id), DT1)
        self.assertEqual(len(dev.firmwares), 2)
        with open(dst, 'rb') as fh:
            text = fh.read()
        self.assertTrue(text.endswith(b'filename = "Digitakt_OS9.99.syx"\n'))
        self.assertFalse(os.path.exists(dst + '.tmp'))
        # Identifying against the overlay now reports it as known.
        self.assertEqual(release.identify_release(path, overlay).status,
                         'known')

    def test_overlay_is_idempotent(self):
        path = self.syx(version=b'9.99', seed=8)
        r = release.identify_release(path, DEVICES)
        overlay = os.path.join(self.tmp, 'ov')
        release.write_device_overlay(r, overlay, DEVICES)
        dst = release.write_device_overlay(r, overlay, DEVICES)
        self.assertEqual(len(device.load(dst).firmwares), 2)

    def test_overlay_never_writes_into_the_devices_dir(self):
        path = self.syx(version=b'9.99')
        write_toml(self.devdir)
        r = release.identify_release(path, self.devdir)
        with self.assertRaises(release.FirmwareError):
            release.write_device_overlay(r, self.devdir, self.devdir)

    def test_overlay_escapes_toml_strings(self):
        path = self.syx(version=b'9"9\\', seed=9)
        r = release.identify_release(path, DEVICES)
        dst = release.write_device_overlay(r, os.path.join(self.tmp, 'ov'),
                                           DEVICES)
        fw = device.load(dst).firmware_for_sha256(r.sha256)
        self.assertEqual(fw.version, '9"9\\')


# --- device files ------------------------------------------------------------

class DeviceIdsTest(Tmp):
    def test_shipped_device_files_carry_product_ids(self):
        ids = {d.name: (d.sysex_id, d.os_stream_id)
               for d in device.load_all(DEVICES)}
        self.assertEqual(ids, {'Digitakt': (0x0A, 0x05),
                               'Digitakt II': (0x14, 0x0F),
                               'Digitone': (0x0D, 0x08),
                               'Digitone II': (0x15, 0x10)})
        for dev in device.load_all(DEVICES):
            self.assertEqual(release.PRODUCTS[(dev.sysex_id,
                                               dev.os_stream_id)][0],
                             dev.name)

    def test_ids_are_optional(self):
        with open(os.path.join(self.devdir, 'x.toml'), 'w', encoding='utf-8',
                  newline='\n') as fh:
            fh.write('[device]\nname = "X"\n[panel]\n')
        dev = device.load(os.path.join(self.devdir, 'x.toml'))
        self.assertIsNone(dev.sysex_id)
        self.assertIsNone(dev.os_stream_id)

    def test_bad_ids_are_refused(self):
        for value in ('"0a"', '0x80', '-1', 'true'):
            p = os.path.join(self.devdir, 'x.toml')
            with open(p, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write('[device]\nname = "X"\nsysex_id = %s\n[panel]\n'
                         % value)
            with self.assertRaises(device.DeviceError, msg=value):
                device.load(p)


class LauncherSafeTest(unittest.TestCase):
    def test_import_pulls_in_no_emulator(self):
        # The launcher imports this on start-up; unicorn would cost time and
        # tie identification to the native DLL.
        code = ('import sys, emu.release; '
                'bad = [m for m in ("unicorn", "capstone", "emu.harness") '
                'if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)')
        proc = subprocess.run([sys.executable, '-c', code], cwd=REPO,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


@unittest.skipUnless(os.path.exists(REAL_SYX), 'no Digitakt_OS1.53.syx')
class RealFirmwareTest(unittest.TestCase):
    def test_digitakt_153(self):
        r = release.identify_release(REAL_SYX, DEVICES)
        self.assertEqual(r.status, 'known')
        self.assertEqual(r.product, 'Digitakt')
        self.assertEqual(r.version, '1.53')
        self.assertEqual(r.build, '0104')
        self.assertEqual(r.slug, 'dt1-1.53-9bdd44bb')
        self.assertEqual(r.stamp, datetime.datetime(2026, 9, 8, 14, 38, 44))
        self.assertEqual(release.canonical_syx_name(r), 'Digitakt_OS1.53.syx')
        with open(REAL_SYX, 'rb') as fh:
            self.assertEqual(r.sha256, hashlib.sha256(fh.read()).hexdigest())

    def test_header_is_fast(self):
        best = min(self._time_header() for _ in range(3))
        self.assertLess(best, 0.1)

    def _time_header(self):
        t = time.perf_counter()
        h = release.read_header(REAL_SYX)
        dt = time.perf_counter() - t
        self.assertEqual((h.transport_id, h.stream_id, h.msg_count),
                         (0x0A, 0x05, 11116))
        return dt


if __name__ == '__main__':
    unittest.main()
