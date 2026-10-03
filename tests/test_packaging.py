# pyright: reportMissingImports=false
"""The app bundles: what may ship, how they are specified, how they start.

The portable app keeps the user's firmware next to the exe, so the one thing
packaging must never do is zip any of it. packaging/bundle_guard.py is the
check, and these tests feed it synthetic trees: one clean bundle, plus one
tree per deny rule. The PyInstaller spec is run here with stand-ins for
Analysis/EXE/COLLECT. That pins what goes into the build (tools/ kept off
the path, the private modules excluded, the unicorn.dll pin, the UTF-8
option, the windowed + console pair) without PyInstaller being installed.
The entry script is checked for the three things it does before the app
starts: stdio, --selftest, and handing everything else to emu.portable.

The exes must also be able to run the emulator: Control Flow Guard off (a
synthetic PE header stands in for PyInstaller's bootloader) and every module
the app imports inside functions in the bundle (the source is scanned for
them, and the self-test's imports check is run against a stub importer).

The macOS app (digiemu.app) gets the same treatment: bundle_guard.audit_app()
on synthetic app trees (arm64 Mach-O headers stand in for the binaries), and
packaging/digiemu-macos.spec run with stand-ins for Analysis/EXE/COLLECT/
BUNDLE.

No firmware, no emulator, no PyInstaller. packaging/ has no __init__.py
(and PyPI's `packaging` would shadow it anyway), so its modules are loaded
by path.
"""
import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import runpy
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(REPO, 'packaging')
SPEC = os.path.join(PACKAGING, 'digiemu.spec')
MAC_SPEC = os.path.join(PACKAGING, 'digiemu-macos.spec')


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(PACKAGING, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load('digiemu_bundle_guard', 'bundle_guard.py')
entry = _load('digiemu_entry', 'digiemu_main.py')

UC_BYTES = b'MZ fake patched unicorn'
NO_CFG, CFG = 0x8160, 0xc160            # python.exe's DllCharacteristics; the bootloader's
# Stands in for the archive PyInstaller appends after the last section (it
# ends in the 'MEI' cookie); odd-sized so the checksum's padding is exercised.
ARCHIVE = b'PYZ\0' + bytes(range(256)) * 3 + b'MEI\014\013\012\013\016' + b'\x7f'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _put(root, rel, data=b'x'):
    path = os.path.join(root, *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


def _ref_checksum(data, offset):
    """pefile's PE.generate_checksum() restated (32-bit words, end-around
    carry), independent of bundle_guard's 16-bit version. Small inputs only."""
    padded = data + bytes(-len(data) % 4)
    total = 0
    for i in range(len(padded) // 4):
        if i == offset // 4:
            continue
        total += struct.unpack_from('<I', padded, i * 4)[0]
        if total >= 1 << 32:
            total = (total & 0xffffffff) + (total >> 32)
    total = (total & 0xffff) + (total >> 16)
    total += total >> 16
    return (total & 0xffff) + len(data)


def _pe(dll_characteristics=NO_CFG, tail=ARCHIVE, pe32plus=True, checksum=None):
    """-> a minimal PE image: DOS header, 'PE\\0\\0', COFF and optional
    header, then `tail`, with a correct PE checksum unless one is given."""
    lfanew, size_opt = 0x80, (240 if pe32plus else 224)
    buf = bytearray(lfanew + 24 + size_opt)
    buf[:2] = b'MZ'
    struct.pack_into('<I', buf, 0x3c, lfanew)
    buf[lfanew:lfanew + 4] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', buf, lfanew + 4, 0x8664, 0, 0, 0, 0, size_opt, 0x22)
    opt = lfanew + 24
    struct.pack_into('<H', buf, opt, 0x20b if pe32plus else 0x10b)
    struct.pack_into('<HH', buf, opt + 68, 3, dll_characteristics)     # console subsystem
    buf += tail
    struct.pack_into('<I', buf, opt + 64,
                     _ref_checksum(bytes(buf), opt + 64) if checksum is None else checksum)
    return bytes(buf)


PE_DC, PE_CS = 0x80 + 24 + 70, 0x80 + 24 + 64      # where _pe() puts the two fields


def _clean_dist(root):
    """A minimal bundle that passes every rule."""
    dist = os.path.join(root, 'digiemu')
    _put(dist, 'digiemu.exe', _pe(tail=b'windowed' + ARCHIVE))
    _put(dist, 'digiemu-console.exe', _pe(tail=b'console' + ARCHIVE))
    _put(dist, '_internal/unicorn/lib/unicorn.dll', UC_BYTES)
    _put(dist, '_internal/capstone/lib/capstone.dll', b'MZ capstone')
    _put(dist, '_internal/devices/digitakt.toml', b'[device]\nname = "Digitakt"\n')
    _put(dist, '_internal/LICENSE', b'GPL')
    _put(dist, '_internal/patches/README.md', b'# Patches')
    return dist


class BundleGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.dist = _clean_dist(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def audit(self, **kw):
        kw.setdefault('unicorn_sha256', _sha(UC_BYTES))
        return guard.audit(self.dist, **kw)

    def test_clean_bundle_passes(self):
        files, problems = self.audit()
        self.assertEqual(problems, [])
        self.assertEqual(files, sorted(files))
        self.assertIn('_internal/unicorn/lib/unicorn.dll', files)
        self.assertIn('digiemu.exe', files)

    def test_each_firmware_path_rule_fires_on_its_own(self):
        for rel in ('_internal/Digitakt_OS1.53.syx', '_internal/x/gui.snap',
                    '_internal/plusdrive.img', '_internal/card.img.before-samples',
                    '_internal/rec.wav', '_internal/manual.pdf',
                    '_internal/sections/section_3_MAIN_OS.bin',
                    '_internal/snapshots/Digitakt_OS1.53/boot400M.bin',
                    '_internal/firmware/dt1-1.53-9bdd44bb/notes.txt',
                    '_internal/out/panel.png', '_internal/portable/x.txt',
                    '_internal/sections', '_internal/.source-sha256',
                    '_internal/.ladder.json', '_internal/firmware.json',
                    '_internal/plusdrive-spare.bin', '_internal/gui.snap.tmp',
                    '_internal/SNAPSHOTS/Boot.BIN'):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as tmp:
                dist = _clean_dist(tmp)
                _put(dist, rel)
                files, problems = guard.audit(dist)
                self.assertEqual(files, [])
                self.assertEqual(len(problems), 1, problems)

    def test_unicorn_backups_and_import_library_are_refused(self):
        for name in ('unicorn.dll.5patch', 'unicorn.dll.6patch',
                     'unicorn.dll.pre-fractional', 'unicorn.lib'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                dist = _clean_dist(tmp)
                _put(dist, '_internal/unicorn/lib/' + name)
                self.assertTrue(guard.audit(dist)[1])

    def test_only_the_pinned_unicorn_dll_passes(self):
        self.assertTrue(self.audit(unicorn_sha256='0' * 64)[1])
        _put(self.dist, '_internal/unicorn.dll', UC_BYTES)       # a second copy
        self.assertTrue(any('unicorn.dll' in p for p in self.audit()[1]))

    def test_missing_pieces_are_refused(self):
        for rel in ('_internal/capstone/lib/capstone.dll', 'digiemu-console.exe',
                    '_internal/unicorn/lib/unicorn.dll'):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as tmp:
                dist = _clean_dist(tmp)
                os.remove(os.path.join(dist, *rel.split('/')))
                self.assertTrue(guard.audit(dist)[1])

    def test_used_folder_is_refused_at_the_top_level(self):
        # digiemu.exe run in place makes logs/ and firmware/ next to itself.
        for rel in ('logs/launcher.log', 'firmware/dt1/firmware.json',
                    'README.txt', 'Digitakt_OS1.53.syx'):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as tmp:
                dist = _clean_dist(tmp)
                _put(dist, rel)
                self.assertTrue(guard.audit(dist)[1])

    def test_sysex_content_is_refused_whatever_the_name(self):
        _put(self.dist, '_internal/readme.txt', b'\xf0\x00\x20\x3c\x0a\x00\x7f')
        problems = self.audit()[1]
        self.assertEqual(len(problems), 1)
        self.assertIn('SysEx', problems[0])

    def test_file_matching_a_listed_firmware_hash_is_refused(self):
        data = b'pretend firmware bytes'
        devices = os.path.join(self.tmp, 'devices')
        _put(devices, 'x.toml', ('[device]\nname = "X"\nshort = "x"\n\n[[firmware]]\n'
                                 'version = "1"\nsha256 = "%s"\nfilename = "X.syx"\n'
                                 % _sha(data).upper()).encode())
        _put(self.dist, '_internal/renamed.bin', data)
        problems = self.audit(devices_dir=devices)[1]
        self.assertEqual(len(problems), 1)
        self.assertIn('firmware release', problems[0])

    # What a real archive listing holds besides the app: the PKG entries and
    # modules whose names only look like private ones.
    ARCHIVE_NAMES = ['PYZ-00.pyz', 'pyimod01_archive', 'dt2.builder', 'tools.ekfsadd',
                     'encodings.cp1252'] + list(guard.REQUIRED_MODULES)

    @staticmethod
    def lister(names, seen=None):
        def run(exe):
            if seen is not None:
                seen.append(os.path.basename(exe))
            return names
        return run

    def test_private_modules_in_the_exe_archive_are_refused(self):
        seen = []
        self.assertEqual(self.audit(lister=self.lister(self.ARCHIVE_NAMES, seen))[1], [])
        self.assertEqual(sorted(seen), ['digiemu-console.exe', 'digiemu.exe'])
        for bad in ('machinepatch', 'tools.machinepatch', 'dt2.build', 'dt2.authcode',
                    'dt2.aplib', 'content_hmac', 'tools.patchimg.sub', 'mmiotrace'):
            with self.subTest(bad=bad):
                self.assertTrue(self.audit(lister=self.lister(self.ARCHIVE_NAMES + [bad]))[1])
        self.assertTrue(self.audit(require_pyz=True)[1])

    def test_an_archive_without_a_required_module_is_refused(self):
        for gone in ('emu.ekfsformat', 'emu.portable', 'tkinter.ttk', 'unicorn'):
            with self.subTest(gone=gone):
                names = [n for n in self.ARCHIVE_NAMES if n != gone]
                files, problems = self.audit(lister=self.lister(names))
                self.assertEqual(files, [])
                self.assertEqual(len(problems), 2, problems)          # one per exe
                for p, exe in zip(sorted(problems), ('digiemu-console.exe', 'digiemu.exe')):
                    self.assertTrue(p.startswith(exe + ':'), p)
                    self.assertTrue(p.endswith(': ' + gone), p)

    def test_an_exe_with_control_flow_guard_is_refused(self):
        _put(self.dist, 'digiemu-console.exe', _pe(CFG))
        files, problems = self.audit()
        self.assertEqual(files, [])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn('digiemu-console.exe: built with Control Flow Guard', problems[0])
        self.assertIn('0xc160', problems[0])

    def test_an_exe_with_a_stale_checksum_or_no_pe_header_is_refused(self):
        for data, why in ((_pe(checksum=0), 'PE checksum 0x00000000 is stale'),
                          (_pe(checksum=0x1234), 'is stale'),
                          (b'MZ windowed', 'not a PE image'),
                          (_pe()[:0x80] + b'NE\0\0' + _pe()[0x84:], 'no PE signature'),
                          (_pe()[:0x98] + b'\x07\x01' + _pe()[0x9a:], 'magic 0x107')):
            with self.subTest(why=why), tempfile.TemporaryDirectory() as tmp:
                dist = _clean_dist(tmp)
                _put(dist, 'digiemu.exe', data)
                problems = guard.audit(dist)[1]
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith('digiemu.exe: '), problems[0])
                self.assertIn(why, problems[0])

    def test_size_budget(self):
        self.assertTrue(self.audit(max_bytes=10)[1])

    def test_zip_holds_exactly_the_audited_files(self):
        files, problems = self.audit()
        self.assertEqual(problems, [])
        out = guard.make_zip(self.dist, files, os.path.join(self.tmp, 'digiemu-win64-9.9.9.zip'))
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(sorted(zf.namelist()), ['digiemu/' + f for f in files])
            self.assertEqual(zf.read('digiemu/_internal/unicorn/lib/unicorn.dll'), UC_BYTES)
        self.assertFalse(os.path.exists(out + '.tmp'))
        with self.assertRaises(ValueError):
            guard.make_zip(self.dist, [], out)

    def test_cli_refuses_to_zip_a_dirty_bundle(self):
        out = os.path.join(self.tmp, 'out.zip')
        _put(self.dist, 'firmware/dt1/plusdrive.img')
        with mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(guard.main([self.dist, '--zip', out]), 1)
        self.assertFalse(os.path.exists(out))

    def test_cli_zips_a_clean_bundle(self):
        out = os.path.join(self.tmp, 'out.zip')
        with mock.patch('sys.stdout', io.StringIO()) as buf, \
                mock.patch.object(guard, 'pyinstaller_lister', return_value=None):
            rc = guard.main([self.dist, '--unicorn-sha256', _sha(UC_BYTES), '--zip', out])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertTrue(zipfile.is_zipfile(out))

    def test_toc_problems_checks_destination_source_and_repo_path(self):
        root = os.path.join(self.tmp, 'repo')
        ok = [('devices/digitakt.toml', os.path.join(root, 'devices', 'digitakt.toml'), 'DATA'),
              ('unicorn/lib/unicorn.dll', os.path.join(self.tmp, 'venv', 'unicorn.dll'), 'BINARY')]
        self.assertEqual(guard.toc_problems(ok, root=root), [])
        for bad in [('sections/a.bin', '/x/a.bin', 'DATA'),
                    ('data/a.bin', os.path.join(root, 'snapshots', 'a.bin'), 'DATA'),
                    ('data/fw.bin', os.path.join(self.tmp, 'Digitakt_OS1.53.syx'), 'DATA')]:
            with self.subTest(bad=bad):
                self.assertEqual(len(guard.toc_problems([bad], root=root)), 1)


class ControlFlowGuardTest(unittest.TestCase):
    """bundle_guard.clear_guard_cf: the one change the spec makes to the
    exes PyInstaller writes (Unicorn's longjmp cannot run under CFG)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_checksum_matches_the_reference_algorithm(self):
        for n in (0, 1, 2, 3, 5, 0x1ff, 4097):
            for pe32plus in (True, False):
                with self.subTest(tail=n, pe32plus=pe32plus):
                    data = _pe(CFG, tail=bytes((i * 37 + 11) & 0xff for i in range(n)),
                               pe32plus=pe32plus)
                    dc, cs = guard.pe_offsets(data)
                    self.assertEqual((dc, cs), (PE_DC, PE_CS))
                    self.assertEqual(guard.pe_checksum(data, cs), _ref_checksum(data, cs))
                    info = guard.pe_info(data)
                    self.assertEqual(info['checksum'], info['computed'])
                    self.assertTrue(info['guard_cf'])

    def test_checksum_matches_real_signed_binaries(self):
        # Windows' own tools set these; the whole file counts (the signature
        # is appended after the sections, like PyInstaller's archive).
        base = sys.base_prefix
        paths = {sys.executable, getattr(sys, '_base_executable', ''),
                 os.path.join(base, 'python.exe'),
                 os.path.join(base, 'python%d%d.dll' % sys.version_info[:2])}
        checked = 0
        for path in sorted(p for p in paths if p and os.path.isfile(p)):
            with open(path, 'rb') as fh:
                data = fh.read()
            if data[:2] != b'MZ':
                continue
            info = guard.pe_info(data)
            if info['checksum']:
                with self.subTest(path=path):
                    self.assertEqual(info['computed'], info['checksum'])
                    checked += 1
        if not checked:
            self.skipTest('no PE file with a checksum here (not Windows)')

    def test_clear_changes_only_the_bit_and_the_checksum(self):
        path = _put(self.tmp, 'digiemu-console.exe', _pe(CFG))
        with open(path, 'rb') as fh:
            before = fh.read()
        result = guard.clear_guard_cf(path)
        with open(path, 'rb') as fh:
            after = fh.read()
        self.assertTrue(result['changed'])
        self.assertEqual(result['before']['dll_characteristics'], CFG)
        self.assertEqual(result['after']['dll_characteristics'], NO_CFG)
        self.assertEqual(len(after), len(before))
        self.assertTrue(after.endswith(ARCHIVE))                 # the appended archive is kept
        self.assertEqual([i for i in range(len(after)) if after[i] != before[i]],
                         [i for i in (PE_CS, PE_CS + 1, PE_CS + 2, PE_CS + 3, PE_DC, PE_DC + 1)
                          if after[i] != before[i]])
        self.assertEqual(struct.unpack_from('<H', after, PE_DC)[0], NO_CFG)
        self.assertEqual(struct.unpack_from('<I', after, PE_CS)[0], _ref_checksum(after, PE_CS))
        # Byte for byte what a toolchain that never set the bit would write.
        self.assertEqual(after, _pe(NO_CFG))
        self.assertEqual(os.listdir(self.tmp), ['digiemu-console.exe'])     # no .tmp left
        self.assertIsNone(guard.exe_problem(path, 'digiemu-console.exe'))
        line = guard.describe_guard_cf('digiemu-console.exe', result)
        self.assertIn('GUARD_CF cleared: DllCharacteristics 0xc160 -> 0x8160', line)
        # Again: nothing to do, the file is not rewritten.
        mtime = os.stat(path).st_mtime_ns
        again = guard.clear_guard_cf(path)
        self.assertFalse(again['changed'])
        self.assertEqual(os.stat(path).st_mtime_ns, mtime)
        self.assertIn('already clear', guard.describe_guard_cf('x.exe', again))

    def test_clear_also_repairs_a_stale_checksum(self):
        path = _put(self.tmp, 'digiemu.exe', _pe(NO_CFG, checksum=0))
        self.assertTrue(guard.clear_guard_cf(path)['changed'])
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), _pe(NO_CFG))

    def test_clear_refuses_what_is_not_a_pe_image(self):
        path = _put(self.tmp, 'digiemu.exe', b'MZ but nothing else')
        with self.assertRaises(ValueError):
            guard.clear_guard_cf(path)
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), b'MZ but nothing else')

    @unittest.skipUnless(sys.platform == 'win32', 'needs a real Windows PE')
    def test_round_trip_on_a_real_exe_restores_it_exactly(self):
        # python.exe has no CFG. Setting the bit (with a correct checksum,
        # as PyInstaller leaves its bootloader) and clearing it again must
        # give back the original file byte for byte.
        with open(sys.executable, 'rb') as fh:
            original = fh.read()
        info = guard.pe_info(original)
        if info['guard_cf'] or info['checksum'] != info['computed']:
            self.skipTest('this python.exe already has CFG or no valid checksum')
        dc, cs = guard.pe_offsets(original)
        buf = bytearray(original)
        struct.pack_into('<H', buf, dc, info['dll_characteristics'] | 0x4000)
        struct.pack_into('<I', buf, cs, guard.pe_checksum(buf, cs))
        path = _put(self.tmp, 'python-cfg.exe', bytes(buf))
        self.assertIn('Control Flow Guard', guard.exe_problem(path, 'python-cfg.exe'))
        guard.clear_guard_cf(path)
        with open(path, 'rb') as fh:
            self.assertEqual(fh.read(), original)


class PrivateModuleListTest(unittest.TestCase):
    EXPORT = os.path.join(REPO, 'tools', 'export-public.sh')

    @unittest.skipUnless(os.path.exists(EXPORT), 'export-public.sh is not in the public tree')
    def test_every_excluded_python_module_is_private_to_the_bundle(self):
        with open(self.EXPORT, encoding='utf-8') as fh:
            text = fh.read()
        block = text[text.index('EXCLUDE = {'):text.index('}', text.index('EXCLUDE = {'))]
        mods = re.findall(r"'(dt2|tools)/([A-Za-z0-9_]+)\.py'", block)
        self.assertTrue(mods)
        for pkg, name in mods:
            dotted = '%s.%s' % (pkg, name)
            with self.subTest(module=dotted):
                self.assertIsNotNone(guard.module_problem(dotted))
                if pkg == 'tools':          # tools/ is not a package: also the bare name
                    self.assertIsNotNone(guard.module_problem(name))


class _Recorder:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


def _fake_versioninfo():
    mod = types.ModuleType('PyInstaller.utils.win32.versioninfo')
    for name in ('VSVersionInfo', 'FixedFileInfo', 'StringFileInfo', 'StringTable',
                 'StringStruct', 'VarFileInfo', 'VarStruct'):
        setattr(mod, name, type(name, (_Recorder,), {}))
    pkgs = {n: types.ModuleType(n) for n in
            ('PyInstaller', 'PyInstaller.utils', 'PyInstaller.utils.win32')}
    pkgs['PyInstaller'].utils = pkgs['PyInstaller.utils']
    pkgs['PyInstaller.utils'].win32 = pkgs['PyInstaller.utils.win32']
    pkgs['PyInstaller.utils.win32'].versioninfo = mod
    pkgs['PyInstaller.utils.win32.versioninfo'] = mod
    return pkgs


class SpecTest(unittest.TestCase):
    """Runs packaging/digiemu.spec with stand-ins for PyInstaller's classes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.dll = _put(self.tmp, 'venv/unicorn/lib/unicorn.dll', UC_BYTES)
        self.env = {'DIGIEMU_UC_DLL': self.dll, 'DIGIEMU_UC_SHA256': _sha(UC_BYTES),
                    'DIGIEMU_VERSION': '1.2.3'}
        self.extra_datas, self.extra_pure = [], []
        # A real analysis finds every required module; the tests drop some.
        self.pure = [(m, '/r/%s.py' % m.replace('.', '/'), 'PYMODULE')
                     for m in guard.REQUIRED_MODULES]
        # COLLECT writes the exes as PyInstaller does: with the bootloader's CFG.
        self.collect_writes = {'digiemu.exe': _pe(CFG, tail=b'w' + ARCHIVE),
                               'digiemu-console.exe': _pe(CFG, tail=b'c' + ARCHIVE)}
        self.dist = os.path.join(self.tmp, 'dist', 'digiemu')
        self.spec_out = ''

    def tearDown(self):
        self._tmp.cleanup()

    def run_spec(self):
        test = self
        made = {'exe': [], 'collect': []}

        class Analysis(_Recorder):
            def __init__(self, scripts, **kw):
                super().__init__(scripts, **kw)
                made['analysis'] = self
                import glob
                self.binaries = [(os.path.join(dest, os.path.basename(src)), src, 'BINARY')
                                 for src, dest in kw['binaries']]
                self.binaries.append(('capstone/lib/capstone.dll', '/venv/capstone.dll', 'BINARY'))
                self.datas = [(os.path.join(dest, os.path.basename(f)), f, 'DATA')
                              for src, dest in kw['datas'] for f in glob.glob(src)]
                self.datas += test.extra_datas
                self.pure = test.pure + test.extra_pure
                self.scripts = [('digiemu_main', scripts[0], 'PYSOURCE')]

        class EXE(_Recorder):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                made['exe'].append(self)

        class COLLECT(_Recorder):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                made['collect'].append(self)
                for name, data in test.collect_writes.items():
                    _put(test.dist, name, data)

        ns = {'SPEC': SPEC, 'SPECPATH': PACKAGING, 'workpath': os.path.join(self.tmp, 'work'),
              'DISTPATH': os.path.join(self.tmp, 'dist'), 'specnm': 'digiemu',
              'Analysis': Analysis, 'PYZ': _Recorder, 'EXE': EXE, 'COLLECT': COLLECT, 'os': os}
        with open(SPEC, 'rb') as fh:
            code = compile(fh.read(), SPEC, 'exec')
        with mock.patch.dict(os.environ, self.env), mock.patch.dict(sys.modules, _fake_versioninfo()), \
                mock.patch('sys.stdout', io.StringIO()) as out:
            try:
                exec(code, ns)
            finally:
                self.spec_out = out.getvalue()
        return made

    def test_analysis_inputs(self):
        made = self.run_spec()
        kw = made['analysis'].kwargs
        self.assertEqual(made['analysis'].args[0], [os.path.join(PACKAGING, 'digiemu_main.py')])
        # tools/ must never be an import root: machinepatch lives there.
        self.assertEqual(kw['pathex'], [os.path.abspath(REPO)])
        self.assertEqual(kw['binaries'], [(self.dll, os.path.join('unicorn', 'lib'))])
        self.assertEqual(kw['runtime_hooks'], [os.path.join(PACKAGING, 'rth_digiemu.py')])
        for m in guard.PRIVATE_MODULES:
            self.assertIn(m, kw['excludes'])
        self.assertIn('tools.machinepatch', kw['excludes'])
        dests = {os.path.normpath(d) for _s, d in kw['datas']}
        for d in ('devices', '.', 'patches', os.path.join('licenses', 'capstone'),
                  os.path.join('licenses', 'python-rtmidi'), os.path.join('licenses', 'python')):
            self.assertIn(os.path.normpath(d), dests)
        names = {os.path.basename(s) for s, _d in kw['datas']}
        self.assertTrue({'*.toml', 'LICENSE', '*.patch', 'README.md'} <= names)
        with open(os.path.join(self.tmp, 'work', 'digiemu-build.json'), encoding='utf-8') as fh:
            info = json.load(fh)
        self.assertEqual(info['version'], '1.2.3')
        self.assertEqual(info['unicorn_sha256'], _sha(UC_BYTES))

    def test_windowed_and_console_exes_share_one_collect(self):
        made = self.run_spec()
        exes = {e.kwargs['name']: e for e in made['exe']}
        self.assertEqual(set(exes), {'digiemu', 'digiemu-console'})
        self.assertFalse(exes['digiemu'].kwargs['console'])
        self.assertTrue(exes['digiemu-console'].kwargs['console'])
        for e in exes.values():
            self.assertIn(('X utf8', None, 'OPTION'), e.args[2])
            self.assertFalse(e.kwargs['upx'])
            self.assertFalse(e.kwargs['strip'])
            self.assertTrue(e.kwargs['exclude_binaries'])
            self.assertEqual(e.kwargs['contents_directory'], '_internal')
        (coll,) = made['collect']
        self.assertEqual(coll.kwargs['name'], 'digiemu')
        self.assertFalse(coll.kwargs['upx'])
        self.assertEqual([a for a in coll.args if a in made['exe']], made['exe'])

    def test_wrong_unicorn_dll_stops_the_build(self):
        self.env['DIGIEMU_UC_SHA256'] = '0' * 64
        with self.assertRaises(SystemExit) as cm:
            self.run_spec()
        self.assertIn('refusing to bundle', str(cm.exception))

    def test_firmware_in_the_datas_stops_the_build(self):
        self.extra_datas = [('snapshots/Digitakt_OS1.53/gui.snap', '/x/gui.snap', 'DATA')]
        with self.assertRaises(SystemExit) as cm:
            self.run_spec()
        self.assertIn('firmware-derived', str(cm.exception))

    def test_private_module_in_the_analysis_stops_the_build(self):
        self.extra_pure = [('dt2.build', '/r/dt2/build.py', 'PYMODULE')]
        with self.assertRaises(SystemExit):
            self.run_spec()

    def test_bad_version_stops_the_build(self):
        for v in ('1.2', '1.2.3-beta', '70000.0.0'):
            with self.subTest(version=v):
                self.env['DIGIEMU_VERSION'] = v
                with self.assertRaises(SystemExit):
                    self.run_spec()

    def test_hidden_imports_are_the_required_modules(self):
        kw = self.run_spec()['analysis'].kwargs
        self.assertEqual(kw['hiddenimports'], list(guard.REQUIRED_MODULES))

    def test_an_analysis_without_a_required_module_stops_the_build(self):
        self.pure = [e for e in self.pure if e[0] not in ('emu.uiresume', 'tkinter.filedialog')]
        with self.assertRaises(SystemExit) as cm:
            self.run_spec()
        self.assertIn('lacks modules the app imports at run time: emu.uiresume, tkinter.filedialog',
                      str(cm.exception))
        self.assertFalse(os.path.exists(self.dist))          # stopped before COLLECT

    def test_control_flow_guard_is_cleared_in_both_exes_after_collect(self):
        self.run_spec()
        for name, data in self.collect_writes.items():
            with self.subTest(exe=name):
                path = os.path.join(self.dist, name)
                with open(path, 'rb') as fh:
                    now = fh.read()
                self.assertEqual(guard.pe_info(data)['dll_characteristics'], CFG)
                self.assertEqual(struct.unpack_from('<H', now, PE_DC)[0], NO_CFG)
                self.assertEqual(struct.unpack_from('<I', now, PE_CS)[0], _ref_checksum(now, PE_CS))
                self.assertEqual(now[PE_DC + 2:], data[PE_DC + 2:])     # the archive is untouched
                self.assertIsNone(guard.exe_problem(path, name))
                self.assertIn('digiemu.spec: %s: GUARD_CF cleared: DllCharacteristics '
                              '0xc160 -> 0x8160' % name, self.spec_out)
        self.assertEqual(sorted(os.listdir(self.dist)), ['digiemu-console.exe', 'digiemu.exe'])

    def test_a_missing_exe_after_collect_stops_the_build(self):
        del self.collect_writes['digiemu-console.exe']
        with self.assertRaises(SystemExit) as cm:
            self.run_spec()
        self.assertIn('COLLECT did not write', str(cm.exception))


class EntryTest(unittest.TestCase):
    def test_importing_it_starts_nothing(self):
        code = ('import importlib.util, sys; '
                's = importlib.util.spec_from_file_location("m", %r); '
                'm = importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                'print(sorted(n for n in ("emu.portable", "tkinter", "unicorn", "capstone") '
                'if n in sys.modules))' % os.path.join(PACKAGING, 'digiemu_main.py'))
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                             cwd=REPO, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '[]')

    def test_app_root(self):
        with mock.patch.dict(os.environ, {'DIGIEMU_HOME': os.path.join('x', 'home')}):
            self.assertEqual(entry.app_root(), os.path.abspath(os.path.join('x', 'home')))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(entry.app_root(), os.path.join(REPO, 'portable'))
        exe = os.path.join(os.path.abspath(os.sep), 'Apps', 'digi kit', 'digiemu.exe')
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(sys, 'executable', exe), \
                mock.patch.dict(os.environ, {'DIGIEMU_HOME': 'ignored'}):
            self.assertEqual(entry.app_root(), os.path.dirname(exe))
        # The macOS app: never inside the signed bundle.
        app = os.path.join(os.path.abspath(os.sep), 'Applications', 'digiemu.app')
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(sys, 'executable', os.path.join(app, 'Contents', 'MacOS', 'digiemu')), \
                mock.patch.dict(os.environ, {'DIGIEMU_HOME': 'ignored', 'HOME': os.path.join('x', 'me')}):
            self.assertEqual(entry.app_root(), os.path.join('x', 'me', 'Library', 'Application Support',
                                                            'digiemu'))

    def test_windowed_stdio_goes_to_the_log_in_utf8(self):
        with tempfile.TemporaryDirectory() as home:
            code = ('import importlib.util, sys; sys.stdout = sys.stderr = None; '
                    's = importlib.util.spec_from_file_location("m", %r); '
                    'm = importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                    'm.setup_stdio(); print("t\\u00ebst \\u65e5"); '
                    'print("to stderr", file=sys.stderr)' % os.path.join(PACKAGING, 'digiemu_main.py'))
            env = dict(os.environ, DIGIEMU_HOME=home, PYTHONIOENCODING='cp1252')
            out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                 env=env, cwd=REPO, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout, b'')
            with open(os.path.join(home, 'logs', 'launcher.log'), encoding='utf-8') as fh:
                log = fh.read()
        self.assertIn('t\u00ebst \u65e5\n', log)
        self.assertIn('to stderr\n', log)
        self.assertTrue(log.startswith('---- '))

    def test_macos_app_stdio_on_devnull_goes_to_the_log(self):
        # Opened from the Finder, the macOS app's stdout and stderr are
        # /dev/null. From source (not frozen) they are left alone.
        code = ('import importlib.util, sys; sys.platform = "darwin"; %s'
                's = importlib.util.spec_from_file_location("m", %r); '
                'm = importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                'm.setup_stdio(); print("to the log"); print("err too", file=sys.stderr)')
        for frozen in (True, False):
            with self.subTest(frozen=frozen), tempfile.TemporaryDirectory() as home:
                env = dict(os.environ, HOME=home, DIGIEMU_HOME=os.path.join(home, 'dev'))
                out = subprocess.run(
                    [sys.executable, '-c', code % ('sys.frozen = True; ' if frozen else '',
                                                   os.path.join(PACKAGING, 'digiemu_main.py'))],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=REPO,
                    timeout=60)
                self.assertEqual(out.returncode, 0)
                logs = [os.path.join(home, 'Library', 'Application Support', 'digiemu', 'logs'),
                        os.path.join(home, 'dev', 'logs')]
                if frozen:
                    with open(os.path.join(logs[0], 'launcher.log'), encoding='utf-8') as fh:
                        log = fh.read()
                    self.assertIn('to the log\n', log)
                    self.assertIn('err too\n', log)
                else:
                    self.assertFalse(any(os.path.exists(d) for d in logs))

    CHECKS = ('_check_unicorn', '_check_compat', '_check_native', '_check_capstone',
              '_check_devices', '_check_tk', '_check_imports')

    def _fake_checks(self, failing=(), real=()):
        def ok(name):
            def fn(*_a):
                if name in failing:
                    raise failing[name]
                return {'fine': name}
            return fn
        return [mock.patch.object(entry, n, ok(n)) for n in self.CHECKS if n not in real]

    def _run_selftest(self, failing=(), real=()):
        patches = self._fake_checks(failing, real)
        for p in patches:
            p.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, 'r\u00ebport.json')
                with mock.patch('sys.stdout', io.StringIO()), mock.patch('sys.stderr', io.StringIO()) as err:
                    rc = entry.selftest(['--json', out])
                with open(out, encoding='utf-8') as fh:
                    return rc, json.load(fh), err.getvalue()
        finally:
            for p in patches:
                p.stop()

    def test_selftest_passes_when_every_check_passes(self):
        rc, report, err = self._run_selftest()
        self.assertEqual(rc, 0)
        self.assertTrue(report['ok'])
        self.assertIsNone(report['running'])
        # imports before unicorn_compat: that one runs guest code and a
        # native crash there ends the process.
        self.assertEqual([c['name'] for c in report['checks']],
                         ['unicorn', 'native_options', 'capstone', 'devices', 'tk',
                          'imports', 'unicorn_compat'])
        self.assertEqual(sorted(report), ['build', 'bundle', 'checks', 'executable', 'frozen',
                                          'ok', 'python', 'running', 'utf8_mode'])
        for c in report['checks']:
            self.assertEqual(sorted(c), ['detail', 'name', 'ok'])
        self.assertIn('selftest: unicorn_compat ok', err)

    def test_selftest_isolates_failures_including_systemexit(self):
        from emu import device
        rc, report, err = self._run_selftest(
            {'_check_devices': device.DeviceError('No devices directory at x'),
             '_check_compat': RuntimeError('stock unicorn')})
        self.assertEqual(rc, 1)
        self.assertFalse(report['ok'])
        by = {c['name']: c for c in report['checks']}
        self.assertEqual(len(by), 7)
        self.assertFalse(by['devices']['ok'])
        self.assertIn('DeviceError', by['devices']['detail'])
        self.assertFalse(by['unicorn_compat']['ok'])
        self.assertTrue(by['tk']['ok'])
        self.assertTrue(by['imports']['ok'])
        self.assertIn('selftest: devices FAILED', err)

    def test_imports_check_names_every_module_it_cannot_import(self):
        from emu import device
        tried = []

        def importer(name):
            tried.append(name)
            if name == 'emu.ekfsformat':
                raise ModuleNotFoundError("No module named 'emu.ekfsformat'", name=name)
            if name == 'emu.gui':           # a SystemExit subclass from import-time code
                raise device.DeviceError('No devices directory at x')
            return types.ModuleType(name)

        with mock.patch.object(entry, '_import_module', importer):
            with self.assertRaises(RuntimeError) as cm:
                entry._check_imports()
        self.assertEqual(tried, list(entry.IMPORTS))            # carried on past both
        msg = str(cm.exception)
        self.assertIn('cannot import 2 of %d modules' % len(entry.IMPORTS), msg)
        self.assertIn("emu.ekfsformat (ModuleNotFoundError: No module named 'emu.ekfsformat')", msg)
        self.assertIn('emu.gui (DeviceError: No devices directory at x)', msg)
        with mock.patch.object(entry, '_import_module', types.ModuleType):
            self.assertEqual(entry._check_imports(), {'imported': len(entry.IMPORTS)})

    def test_selftest_reports_a_missing_module_and_still_runs_the_rest(self):
        def importer(name):
            if name == 'emu.portable':
                raise ModuleNotFoundError("No module named 'emu.portable'", name=name)
            return types.ModuleType(name)

        with mock.patch.object(entry, '_import_module', importer):
            rc, report, err = self._run_selftest(real=('_check_imports',))
        self.assertEqual(rc, 1)
        self.assertFalse(report['ok'])
        by = {c['name']: c for c in report['checks']}
        self.assertFalse(by['imports']['ok'])
        self.assertIn('emu.portable', by['imports']['detail'])
        self.assertTrue(by['imports']['detail'].startswith('RuntimeError: cannot import 1 of'))
        self.assertTrue(by['unicorn_compat']['ok'])
        self.assertIn('selftest: imports FAILED', err)

    def test_selftest_imports_what_the_bundle_must_hold(self):
        self.assertEqual(tuple(entry.IMPORTS), guard.REQUIRED_MODULES)
        for m in ('emu.portable', 'emu.bootstrap', 'emu.release', 'emu.dtpanel', 'emu.gui',
                  'emu.ekfsformat', 'emu.uiresume', 'emu.checkpoint', 'emu.extract',
                  'emu.snapshot', 'emu.esdhc'):
            self.assertIn(m, entry.IMPORTS)

    @unittest.skipUnless(sys.platform == 'win32', 'the bundle and its self-test are Windows only')
    def test_imports_check_works_on_this_tree(self):
        # The real thing, in a child so this process's modules stay as they
        # are: nothing missing, and importing starts no emulator.
        code = ('import importlib.util, sys; '
                's = importlib.util.spec_from_file_location("m", %r); '
                'm = importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                'print(m._check_imports()); '
                'import threading; print(threading.active_count())'
                % os.path.join(PACKAGING, 'digiemu_main.py'))
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                             cwd=REPO, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["{'imported':", '%d}' % len(entry.IMPORTS), '1'])

    def test_selftest_dispatch_does_not_import_the_app(self):
        with mock.patch.object(entry, 'setup_stdio'), \
                mock.patch.object(entry, '_log_thread_exceptions'), \
                mock.patch.object(entry, 'selftest', return_value=7) as st, \
                mock.patch.dict(sys.modules, {'emu.portable': None}):
            self.assertEqual(entry.main(['--selftest', '--json', 'x']), 7)
        st.assert_called_once_with(['--json', 'x'])

    def test_everything_else_goes_to_emu_portable(self):
        import emu
        fake = types.ModuleType('emu.portable')
        fake.main = mock.Mock(return_value=5)
        with mock.patch.object(entry, 'setup_stdio'), \
                mock.patch.object(entry, '_log_thread_exceptions'), \
                mock.patch.dict(sys.modules, {'emu.portable': fake}), \
                mock.patch.object(emu, 'portable', fake, create=True):
            self.assertEqual(entry.main(['--worker', 'first-run', 'C:\\t\u00ebst dir\\fw']), 5)
        fake.main.assert_called_once_with(['--worker', 'first-run', 'C:\\t\u00ebst dir\\fw'])


class LazyImportTest(unittest.TestCase):
    """Every module the app imports inside a function is listed in
    bundle_guard.REQUIRED_MODULES, and so is a hidden import, required in
    both archives and imported by the frozen self-test. Found by reading
    the source: the emu/dt2 modules reachable from emu.portable and the
    entry script, and for the standard library and bindings, the modules
    that make up the app itself (MIDI's among them)."""
    APP_SOURCES = ('emu.portable', 'emu.bootstrap', 'emu.dtpanel', 'emu.gui',
                   'emu.midi')
    ROOTS = ('emu', 'dt2')

    @classmethod
    def _source(cls, name):
        if name.split('.')[0] not in cls.ROOTS:
            return None
        base = os.path.join(REPO, *name.split('.'))
        for path in (base + '.py', os.path.join(base, '__init__.py')):
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _lib_submodule(pkg, sub):
        """Is pkg.sub a module (from pkg import sub), not a name in pkg?"""
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError):
            return False
        for d in (spec and spec.submodule_search_locations) or ():
            if os.path.isfile(os.path.join(d, sub + '.py')) or os.path.isdir(os.path.join(d, sub)):
                return True
        return False

    @classmethod
    def _imports(cls, name, path):
        """-> [(module, from-names, inside a function)] for the file."""
        with open(path, 'rb') as fh:
            tree = ast.parse(fh.read(), path)
        package = name if path.endswith('__init__.py') else name.rpartition('.')[0]
        out = []

        def visit(node, nested):
            for ch in ast.iter_child_nodes(node):
                inner = nested or isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                if isinstance(ch, ast.Import):
                    out.extend((a.name, (), inner) for a in ch.names)
                elif isinstance(ch, ast.ImportFrom):
                    base = ch.module or ''
                    if ch.level:
                        pkg = package
                        for _ in range(ch.level - 1):
                            pkg = pkg.rpartition('.')[0]
                        base = pkg + ('.' + base if base else '')
                    out.append((base, tuple(a.name for a in ch.names), inner))
                visit(ch, inner)

        visit(tree, False)
        return out

    def _scan(self):
        """-> (reachable repo modules, {lazy repo module: [where]},
        {lazy other module in APP_SOURCES: [where]})."""
        todo = [('digiemu_main', os.path.join(PACKAGING, 'digiemu_main.py')),
                ('emu.portable', self._source('emu.portable'))]
        seen, lazy_repo, lazy_lib = set(), {}, {}
        while todo:
            name, path = todo.pop()
            if name in seen:
                continue
            seen.add(name)
            for base, names, inner in self._imports(name, path):
                # `from emu import release` imports emu.release; `from
                # emu.harness import Machine` imports emu.harness.
                targets = [base + '.' + n for n in names if self._source(base + '.' + n)] or [base]
                for t in targets:
                    if guard.module_problem(t):
                        continue            # private, gated (gui's --patch-machine)
                    where = '%s: %s' % (name, t)
                    if self._source(t):
                        todo.append((t, self._source(t)))
                        if inner:
                            lazy_repo.setdefault(t, []).append(where)
                    elif inner and name in self.APP_SOURCES:
                        subs = [base + '.' + n for n in names if self._lib_submodule(base, n)]
                        for lib in subs or [t]:
                            lazy_lib.setdefault(lib, []).append(where)
        return seen, lazy_repo, lazy_lib

    def test_every_module_imported_inside_a_function_is_required(self):
        seen, lazy_repo, lazy_lib = self._scan()
        for m in self.APP_SOURCES + ('emu.snapshot', 'emu.esdhc', 'emu.ekfsformat'):
            self.assertIn(m, seen)               # the scan really reached the app
        self.assertIn('emu.bootstrap', lazy_repo)
        self.assertIn('tkinter.ttk', lazy_lib)
        self.assertIn('rtmidi', lazy_lib)        # MIDI's host side, in the app
        missing = {m: w for m, w in lazy_repo.items() if m not in guard.APP_MODULES}
        self.assertEqual(missing, {}, 'add these to bundle_guard.APP_MODULES and '
                                      'digiemu_main.IMPORTS')
        missing = {m: w for m, w in lazy_lib.items()
                   if m not in guard.LIB_MODULES and m not in guard.UNARCHIVED_MODULES}
        self.assertEqual(missing, {}, 'add these to bundle_guard.LIB_MODULES and '
                                      'digiemu_main.IMPORTS')

    def test_required_app_modules_exist(self):
        for m in guard.APP_MODULES:
            with self.subTest(module=m):
                self.assertIsNotNone(self._source(m))
                self.assertIsNone(guard.module_problem(m))

    @unittest.skipUnless(sys.platform == 'win32', 'about the Windows interpreter')
    def test_unarchived_modules_are_builtin_or_absent_on_windows(self):
        self.assertIn('msvcrt', sys.builtin_module_names)
        self.assertIn('zlib', sys.builtin_module_names)
        self.assertIsNone(importlib.util.find_spec('fcntl'))


# -- the macOS app --------------------------------------------------------------------

CPU_ARM64, CPU_X86_64 = 0x0100000c, 0x01000007
UUID = bytes(range(0x10, 0x20))


def _macho(cputype=CPU_ARM64, uuid=UUID, filetype=6, tail=b''):
    """-> a minimal thin 64-bit Mach-O image: the header, an LC_SEGMENT_64
    stand-in, then LC_UUID (unless uuid is None), then `tail`."""
    cmds = struct.pack('<II', 0x19, 72) + bytes(64)
    ncmds = 1
    if uuid is not None:
        cmds += struct.pack('<II', 0x1b, 24) + uuid
        ncmds += 1
    head = struct.pack('<IIIIIIII', 0xfeedfacf, cputype, 0, filetype, ncmds, len(cmds), 0, 0)
    return head + cmds + tail


def _clean_app(root):
    """A minimal digiemu.app that passes every rule, with the links
    PyInstaller makes between Resources and Frameworks."""
    app = os.path.join(root, 'digiemu.app')
    _put(app, 'Contents/Info.plist', b'<plist/>')
    _put(app, 'Contents/MacOS/digiemu', _macho(filetype=2, uuid=None, tail=ARCHIVE))
    _put(app, 'Contents/Frameworks/unicorn/lib/libunicorn.2.dylib', _macho())
    _put(app, 'Contents/Frameworks/capstone/lib/libcapstone.dylib', _macho(uuid=bytes(16)))
    _put(app, 'Contents/Resources/devices/digitakt.toml', b'[device]\nname = "Digitakt"\n')
    _put(app, 'Contents/Resources/LICENSE', b'GPL')
    os.symlink(os.path.join('..', 'Resources', 'devices'),
               os.path.join(app, 'Contents', 'Frameworks', 'devices'))
    os.symlink(os.path.join('..', 'Frameworks', 'unicorn'),
               os.path.join(app, 'Contents', 'Resources', 'unicorn'))
    os.symlink(os.path.join('..', 'Resources', 'LICENSE'),
               os.path.join(app, 'Contents', 'Frameworks', 'LICENSE'))
    return app


@unittest.skipIf(os.name == 'nt', 'the app tree has symlinks')
class AppBundleGuardTest(unittest.TestCase):
    ARCHIVE_NAMES = list(guard.REQUIRED_MODULES) + ['pyimod02_importers']

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.app = _clean_app(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def audit(self, **kw):
        kw.setdefault('unicorn_uuid', UUID.hex())
        return guard.audit_app(self.app, **kw)

    def lister(self, names, seen=None):
        def fn(exe):
            if seen is not None:
                seen.append(os.path.relpath(exe, self.app))
            return names
        return fn

    def test_clean_app_passes(self):
        seen = []
        files, problems = self.audit(lister=self.lister(self.ARCHIVE_NAMES, seen))
        self.assertEqual(problems, [])
        self.assertEqual(seen, [os.path.join('Contents', 'MacOS', 'digiemu')])
        self.assertIn(guard.APP_UNICORN, files)
        self.assertIn(guard.APP_EXE, files)
        # Links are not files of their own: their targets are audited.
        self.assertNotIn('Contents/Frameworks/LICENSE', files)
        self.assertIn('Contents/Resources/LICENSE', files)

    def test_firmware_in_the_app_is_refused(self):
        for rel, data in (('Contents/Resources/Digitakt_OS1.53.syx', b'x'),
                          ('Contents/Resources/snapshots/a.bin', b'x'),
                          ('Contents/Frameworks/plusdrive.img', b'x'),
                          ('Contents/Resources/notes.txt', b'\xf0\x00\x20\x3c rest')):
            with self.subTest(rel=rel):
                path = _put(self.app, rel, data)
                self.assertTrue(self.audit()[1])
                os.remove(path)
        self.assertEqual(self.audit()[1], [])

    def test_a_link_out_of_the_app_is_refused(self):
        for name, target in (('escape', os.path.join('..', '..', '..')),
                             ('abs', os.path.abspath(os.sep))):
            with self.subTest(name=name):
                link = os.path.join(self.app, 'Contents', 'Resources', name)
                os.symlink(target, link)
                files, problems = self.audit()
                self.assertEqual(files, [])
                self.assertEqual(problems, ['Contents/Resources/%s: link out of the app' % name])
                os.remove(link)

    def test_the_layout_is_checked(self):
        _put(self.app, 'stray.txt')
        _put(self.app, 'Contents/MacOS/digiemu-console')
        problems = self.audit()[1]
        self.assertEqual(len(problems), 2, problems)
        self.assertIn('the app must hold only Contents', problems[0])
        self.assertIn('Contents/MacOS: must hold only digiemu', problems[1])

    def test_the_executable_must_be_arm64_mach_o(self):
        exe = os.path.join(self.app, *guard.APP_EXE.split('/'))
        for data, why in ((_macho(CPU_X86_64, filetype=2), 'not arm64'),
                          (b'#!/bin/sh\n', 'not a thin 64-bit Mach-O')):
            with self.subTest(why=why):
                with open(exe, 'wb') as fh:
                    fh.write(data)
                problems = self.audit()[1]
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(why, problems[0])

    def test_unicorn_is_identified_by_its_build_uuid(self):
        self.assertEqual(self.audit(unicorn_uuid=UUID.hex().upper())[1], [])
        problems = self.audit(unicorn_uuid='ab' * 16)[1]
        self.assertEqual(problems, ['%s: LC_UUID %s, expected the patched build %s'
                                    % (guard.APP_UNICORN, UUID.hex(), 'ab' * 16)])
        _put(self.app, guard.APP_UNICORN, _macho(uuid=None))
        self.assertIn('unreadable (no LC_UUID)', self.audit()[1][0])

    def test_one_unicorn_in_the_one_place_and_capstone(self):
        _put(self.app, 'Contents/Resources/libunicorn.2.dylib', _macho())
        self.assertIn('expected exactly', self.audit()[1][0])
        os.remove(os.path.join(self.app, 'Contents', 'Resources', 'libunicorn.2.dylib'))
        _put(self.app, 'Contents/Frameworks/unicorn/lib/libunicorn.a', b'!<arch>')
        self.assertIn('build litter', self.audit()[1][0])
        os.remove(os.path.join(self.app, 'Contents', 'Frameworks', 'unicorn', 'lib', 'libunicorn.a'))
        os.remove(os.path.join(self.app, *guard.APP_CAPSTONE.split('/')))
        self.assertEqual(self.audit()[1], ['%s: missing' % guard.APP_CAPSTONE])

    def test_the_archive_is_checked_like_the_windows_exes(self):
        self.assertTrue(self.audit(lister=self.lister(self.ARCHIVE_NAMES + ['machinepatch']))[1])
        names = [n for n in self.ARCHIVE_NAMES if n != 'emu.portable']
        self.assertEqual(self.audit(lister=self.lister(names))[1],
                         ['%s: the archive lacks modules the app imports at run time: emu.portable'
                          % guard.APP_EXE])
        self.assertTrue(self.audit(require_pyz=True)[1])

    def test_cli(self):
        source = _put(self.tmp, 'venv/libunicorn.2.dylib', _macho())
        with mock.patch.object(guard, 'pyinstaller_lister', lambda: self.lister(self.ARCHIVE_NAMES)), \
                mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(guard.main([self.app, '--unicorn-source', source]), 0)
        self.assertIn('bundle audit OK', out.getvalue())
        _put(self.tmp, 'venv/libunicorn.2.dylib', _macho(uuid=bytes(16)))
        with mock.patch.object(guard, 'pyinstaller_lister', lambda: None), \
                mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(guard.main([self.app, '--unicorn-source', source]), 1)
        self.assertIn('LC_UUID', out.getvalue())
        # A .app is never zipped: it ships in a .dmg.
        with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            guard.main([self.app, '--zip', os.path.join(self.tmp, 'x.zip')])


class MachoTest(unittest.TestCase):
    def test_uuid(self):
        self.assertEqual(guard.macho_uuid(_macho()), UUID.hex())
        self.assertEqual(guard.macho_header(_macho(CPU_X86_64))[0], CPU_X86_64)

    def test_not_a_usable_image(self):
        for data, why in ((b'MZ' + bytes(64), 'not a thin 64-bit Mach-O'),
                          (_macho(uuid=None), 'no LC_UUID'),
                          (_macho()[:40], 'run past the end'),
                          (_macho()[:32] + struct.pack('<II', 0x19, 4) + _macho()[40:],
                           'bad Mach-O load command')):
            with self.subTest(why=why), self.assertRaises(ValueError) as cm:
                guard.macho_uuid(data)
            self.assertIn(why, str(cm.exception))

    @unittest.skipUnless(sys.platform == 'darwin', 'needs a real Mach-O binary and dwarfdump')
    def test_matches_dwarfdump_on_a_real_binary(self):
        exe = os.path.realpath(sys.executable)
        with open(exe, 'rb') as fh:
            data = fh.read()
        if struct.unpack_from('<I', data, 0)[0] != 0xfeedfacf:
            self.skipTest('%s is not a thin 64-bit Mach-O' % exe)
        out = subprocess.run(['dwarfdump', '--uuid', exe], capture_output=True, text=True)
        if out.returncode != 0:
            self.skipTest('no dwarfdump')
        want = out.stdout.split()[1].replace('-', '').lower()
        self.assertEqual(guard.macho_uuid(data), want)


class MacSpecTest(unittest.TestCase):
    """Runs packaging/digiemu-macos.spec with stand-ins for PyInstaller's classes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.dylib = _put(self.tmp, 'venv/unicorn/lib/libunicorn.2.dylib', UC_BYTES)
        self.env = {'DIGIEMU_UC_DLL': self.dylib, 'DIGIEMU_UC_SHA256': _sha(UC_BYTES),
                    'DIGIEMU_VERSION': '1.2.3', 'DIGIEMU_CODESIGN_IDENTITY': ''}
        self.extra_datas, self.extra_pure = [], []
        self.capstone = [('capstone/lib/libcapstone.dylib', '/venv/libcapstone.dylib', 'BINARY')]
        self.pure = [(m, '/r/%s.py' % m.replace('.', '/'), 'PYMODULE')
                     for m in guard.REQUIRED_MODULES]

    def tearDown(self):
        self._tmp.cleanup()

    def run_spec(self):
        test = self
        made = {}

        class Analysis(_Recorder):
            def __init__(self, scripts, **kw):
                super().__init__(scripts, **kw)
                made['analysis'] = self
                import glob
                self.binaries = [(os.path.join(dest, os.path.basename(src)), src, 'BINARY')
                                 for src, dest in kw['binaries']] + test.capstone
                self.datas = [(os.path.join(dest, os.path.basename(f)), f, 'DATA')
                              for src, dest in kw['datas'] for f in glob.glob(src)]
                self.datas += test.extra_datas
                self.pure = test.pure + test.extra_pure
                self.scripts = [('digiemu_main', scripts[0], 'PYSOURCE')]

        def recorder(key):
            class R(_Recorder):
                def __init__(self, *a, **kw):
                    super().__init__(*a, **kw)
                    made[key] = self
            return R

        ns = {'SPEC': MAC_SPEC, 'SPECPATH': PACKAGING, 'workpath': os.path.join(self.tmp, 'work'),
              'DISTPATH': os.path.join(self.tmp, 'dist'), 'specnm': 'digiemu-macos',
              'Analysis': Analysis, 'PYZ': _Recorder, 'EXE': recorder('exe'),
              'COLLECT': recorder('collect'), 'BUNDLE': recorder('bundle'), 'os': os}
        with open(MAC_SPEC, 'rb') as fh:
            code = compile(fh.read(), MAC_SPEC, 'exec')
        with mock.patch.dict(os.environ, self.env):
            exec(code, ns)
        return made

    def test_analysis_inputs_are_the_windows_specs(self):
        kw = self.run_spec()['analysis'].kwargs
        self.assertEqual(kw['pathex'], [os.path.abspath(REPO)])
        self.assertEqual(kw['binaries'], [(self.dylib, os.path.join('unicorn', 'lib'))])
        self.assertEqual(kw['runtime_hooks'], [os.path.join(PACKAGING, 'rth_digiemu.py')])
        self.assertEqual(kw['hiddenimports'], list(guard.REQUIRED_MODULES))
        for m in guard.PRIVATE_MODULES:
            self.assertIn(m, kw['excludes'])
        dests = {os.path.normpath(d) for _s, d in kw['datas']}
        for d in ('devices', '.', 'patches', os.path.join('licenses', 'capstone'),
                  os.path.join('licenses', 'python-rtmidi'), os.path.join('licenses', 'python')):
            self.assertIn(os.path.normpath(d), dests)

    def test_one_windowed_arm64_executable_signed_with_allow_jit(self):
        made = self.run_spec()
        exe = made['exe'].kwargs
        self.assertEqual(exe['name'], 'digiemu')
        self.assertFalse(exe['console'])
        self.assertFalse(exe['argv_emulation'])
        self.assertEqual(exe['target_arch'], 'arm64')
        self.assertIsNone(exe['codesign_identity'])                  # ad hoc
        self.assertEqual(exe['entitlements_file'], os.path.join(PACKAGING, 'digiemu.entitlements'))
        self.assertIn(('X utf8', None, 'OPTION'), made['exe'].args[2])
        self.assertIs(made['collect'].args[0], made['exe'])
        self.env['DIGIEMU_CODESIGN_IDENTITY'] = 'Developer ID Application: Someone (TEAM)'
        self.assertEqual(self.run_spec()['exe'].kwargs['codesign_identity'],
                         'Developer ID Application: Someone (TEAM)')

    def test_the_bundle(self):
        made = self.run_spec()
        b = made['bundle']
        self.assertIs(b.args[0], made['collect'])
        self.assertEqual(b.kwargs['name'], 'digiemu.app')
        self.assertEqual(b.kwargs['bundle_identifier'], 'io.github.irpina.digiemu')
        self.assertEqual(b.kwargs['version'], '1.2.3')
        self.assertEqual(b.kwargs['info_plist']['CFBundleVersion'], '1.2.3')
        self.assertEqual(b.kwargs['info_plist']['LSMinimumSystemVersion'], '11.0')

    def test_build_info_pins_the_source_dylib_not_the_bundled_bytes(self):
        self.run_spec()
        with open(os.path.join(self.tmp, 'work', 'digiemu-build.json'), encoding='utf-8') as fh:
            info = json.load(fh)
        self.assertEqual(info['unicorn_source_sha256'], _sha(UC_BYTES))
        # 'unicorn_sha256' would make the self-test compare the bundled
        # bytes, which PyInstaller rewrites and signs on macOS.
        self.assertNotIn('unicorn_sha256', info)

    def test_what_stops_the_build(self):
        cases = {
            'no pin': ({'DIGIEMU_UC_SHA256': ''}, None, 'set DIGIEMU_UC_SHA256'),
            'wrong dylib': ({'DIGIEMU_UC_SHA256': '0' * 64}, None, 'refusing to bundle'),
            'bad version': ({'DIGIEMU_VERSION': '1.2'}, None, 'x.y.z'),
            'firmware': ({}, ('extra_datas', [('snapshots/a/gui.snap', '/x/gui.snap', 'DATA')]),
                         'firmware-derived'),
            'private': ({}, ('extra_pure', [('dt2.build', '/r/dt2/build.py', 'PYMODULE')]),
                        'private modules'),
            'no capstone': ({}, ('capstone', []), 'libcapstone.dylib was not collected'),
            'missing module': ({}, ('pure', [e for e in self.pure if e[0] != 'emu.uiresume']),
                               'lacks modules the app imports at run time: emu.uiresume'),
        }
        for name, (env, attr, why) in cases.items():
            with self.subTest(name), mock.patch.dict(self.env, env):
                saved = getattr(self, attr[0]) if attr else None
                if attr:
                    setattr(self, attr[0], attr[1])
                try:
                    with self.assertRaises(SystemExit) as cm:
                        self.run_spec()
                    self.assertIn(why, str(cm.exception))
                finally:
                    if attr:
                        setattr(self, attr[0], saved)

    def test_entitlements_ask_for_allow_jit_and_nothing_else(self):
        import plistlib
        with open(os.path.join(PACKAGING, 'digiemu.entitlements'), 'rb') as fh:
            self.assertEqual(plistlib.load(fh), {'com.apple.security.cs.allow-jit': True})


class RuntimeHookTest(unittest.TestCase):
    HOOK = os.path.join(PACKAGING, 'rth_digiemu.py')

    def test_frozen_points_both_libraries_at_the_bundle(self):
        meipass = os.path.join(os.path.abspath(os.sep), 'app', '_internal')
        with mock.patch.dict(os.environ, {'LIBUNICORN_PATH': 'C:\\stock'}), \
                mock.patch.object(sys, '_MEIPASS', meipass, create=True):
            runpy.run_path(self.HOOK)
            self.assertEqual(os.environ['LIBUNICORN_PATH'], os.path.join(meipass, 'unicorn', 'lib'))
            self.assertEqual(os.environ['LIBCAPSTONE_PATH'], os.path.join(meipass, 'capstone', 'lib'))

    def test_unfrozen_changes_nothing(self):
        self.assertFalse(hasattr(sys, '_MEIPASS'))
        with mock.patch.dict(os.environ, {'LIBUNICORN_PATH': 'keep'}):
            runpy.run_path(self.HOOK)
            self.assertEqual(os.environ['LIBUNICORN_PATH'], 'keep')


class RepoFilesTest(unittest.TestCase):
    def test_gitignore_covers_app_data_and_build_output(self):
        with open(os.path.join(REPO, '.gitignore'), encoding='utf-8') as fh:
            lines = {ln.strip() for ln in fh}
        for pat in ('/portable/', '/firmware/', '/build/', '/dist/', '/build-out/',
                    '__pycache__/', '*.syx', '*.img', '*.snap', '*.zip', '*.log'):
            self.assertIn(pat, lines)

    def test_git_ignores_app_data(self):
        if subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], cwd=REPO,
                          capture_output=True).returncode != 0:
            self.skipTest('not a git work tree')
        for rel in ('portable/firmware/dt1-1.53-9bdd44bb/firmware.json',
                    'firmware/dt1-1.53-9bdd44bb/.ladder.json', 'build/digiemu/x',
                    'dist/digiemu/digiemu.exe', 'build-out/digiemu-win64-0.1.0.zip',
                    'digiemu.spec', 'packaging/__pycache__/x.pyc'):
            with self.subTest(rel=rel):
                self.assertEqual(subprocess.run(['git', 'check-ignore', '-q', rel],
                                                cwd=REPO).returncode, 0)
        for tracked in ('packaging/digiemu.spec', 'packaging/digiemu-macos.spec',
                        'packaging/digiemu.entitlements'):
            self.assertNotEqual(subprocess.run(['git', 'check-ignore', '-q', tracked],
                                               cwd=REPO).returncode, 0)

    def test_build_script_checks_control_flow_guard_before_the_self_test(self):
        with open(os.path.join(REPO, 'tools', 'build-windows.ps1'), encoding='ascii') as fh:
            text = fh.read()
        pe = text.index("Step 'Control Flow Guard (pefile)'")
        self.assertLess(text.index('# -- 3. PyInstaller'), pe)
        self.assertLess(pe, text.index("Step 'self-test (frozen)'"))
        self.assertIn('generate_checksum()', text)
        self.assertIn('GUARD_CF', text[:pe])        # the spec's log lines are shown

    def test_macos_build_script_order(self):
        # Signed and checked before the self-test, the self-test before the
        # audit, the audit before the .dmg, and the app notarized (so its
        # ticket can be stapled) before it goes into the .dmg.
        with open(os.path.join(REPO, 'tools', 'build-macos.sh'), encoding='ascii') as fh:
            text = fh.read()
        order = ['# -- 2. PyInstaller', "step 'signature'", "step 'self-test (frozen, signed)'",
                 "step 'audit'", "step 'notarize the app'", "step 'the .dmg'",
                 'audit "$mnt/digiemu.app"', "step 'notarize the .dmg'"]
        at = [text.index(s) for s in order]
        self.assertEqual(at, sorted(at))
        self.assertIn('com.apple.security.cs.allow-jit', text)
        self.assertIn('HOME=$out/selftest-home', text)

    def test_build_script_is_ascii(self):
        # Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page.
        with open(os.path.join(REPO, 'tools', 'build-windows.ps1'), 'rb') as fh:
            data = fh.read()
        self.assertTrue(all(b < 0x80 for b in data))


if __name__ == '__main__':
    unittest.main()
