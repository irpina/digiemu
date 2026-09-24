"""Keep firmware and private tooling out of the Windows bundle.

The portable app keeps its data next to the exe. firmware/<slug>/ fills up
with the user's Elektron .syx, the sections extracted from it, snapshots of
the running firmware and a +Drive card image. All of that is Elektron's
copyright or the user's own (CLAUDE.md), so none of it may ever be zipped with
the app. There are three ways it could get in, and one check for each:

  * the spec collects it: a datas glob that is too wide, or collect_* on a
    package that sits next to the data. The spec calls toc_problems() on its
    Analysis before anything is written.
  * someone runs dist/digiemu/digiemu.exe in place and then zips dist.
    audit() walks the finished folder, and make_zip() writes only the files
    that audit() has just passed. Nothing else should ever zip dist.
  * a private module is pulled in by an import and lands inside an exe's
    PYZ archive, where no file walk can see it. These are the firmware
    building, signing and patching modules that tools/export-public.sh also
    keeps out of the public tree. audit() lists each exe's archive when
    PyInstaller can be imported (it can in the build venv).

The rules are deliberately blunt. A name, extension or path component that
belongs to firmware data is refused wherever it appears. So is any file whose
first bytes are an Elektron SysEx header, or whose SHA-256 is a firmware
release listed in devices/*.toml. The only unicorn.dll allowed is the one at
_internal/unicorn/lib, with the hash the build pinned. The DLL backups and the
50 MB import library that sit next to it in the dev venv are refused.

Two more things would make a bundle that looks fine but cannot run, and
audit() refuses both: an archive that lacks a module the app imports at run
time (REQUIRED_MODULES), and an exe that still runs under Control Flow Guard
(clear_guard_cf() explains why Unicorn cannot).

Stdlib only, plus PyInstaller's archive reader when present, so the tests can
run it on synthetic trees. CLI:
    python packaging/bundle_guard.py DIST [--devices DIR] [--unicorn-sha256 HEX]
                                          [--require-pyz] [--zip OUT.zip]
"""
import argparse
import array
import hashlib
import os
import stat
import struct
import sys
import zipfile

try:
    import tomllib
except ImportError:                                 # pragma: no cover (3.10)
    tomllib = None

# The top level of dist/digiemu. Anything else there (logs/ or firmware/ from
# a test run, a copied .syx) means the folder has been used, not only built.
EXES = ('digiemu.exe', 'digiemu-console.exe')
CONTENTS = '_internal'
TOP_LEVEL = EXES + (CONTENTS,)
UNICORN_DLL = CONTENTS + '/unicorn/lib/unicorn.dll'
CAPSTONE_DLL = CONTENTS + '/capstone/lib/capstone.dll'

# Firmware and firmware-derived files. '.img.' also catches the card copies
# (plusdrive.img.before-samples), '.snap.' half-written snapshots.
DENY_SUFFIXES = ('.syx', '.snap', '.img', '.wav', '.pdf')
DENY_PARTS = ('sections', 'snapshots', 'firmware', 'out', 'portable')
DENY_NAMES = ('.source-sha256', '.ladder.json', 'firmware.json', 'unicorn.lib')
DENY_PREFIXES = ('plusdrive', 'unicorn.dll.')
SYSEX = b'\xf0\x00\x20\x3c'                         # F0, Elektron's manufacturer id

# Firmware-modification code (tools/export-public.sh EXCLUDE) and the
# tracing tools only machinepatch reaches (emu/gui.py's --patch-machine).
PRIVATE_MODULES = (
    'dt2.build', 'dt2.authcode', 'dt2.aplib',
    'content_hmac', 'patchimg', 'roundtrip', 'machinepatch', 'machineprofile',
    'uidrive', 'addrtrace', 'mmiotrace',
)

# Every emu/dt2 module that any module the app reaches imports inside a
# function, plus emu.portable, which the entry script imports only after
# --selftest has had its turn, and dt2.coldfire, which the self-test uses.
# PyInstaller's scan does find these, but nothing enforces it, and a module
# missing from the bundle would first fail on a user's first run. So each one
# is required three times over: the spec passes them as hiddenimports and
# refuses an analysis that lacks one, the frozen --selftest imports them
# (digiemu_main.IMPORTS, which must equal REQUIRED_MODULES), and audit()
# looks for them in both exes' archives. tests/test_packaging.py scans the
# app for function-level imports and fails when one is not listed here.
APP_MODULES = (
    'emu.portable', 'emu.bootstrap', 'emu.release', 'emu.dtpanel', 'emu.gui',
    'emu.dnpanel', 'emu.dsplink',
    'emu.ekfsformat', 'emu.uiresume', 'emu.checkpoint', 'emu.extract',
    'emu.snapshot', 'emu.checkpointver', 'emu.sparse', 'emu.samples', 'emu.esdhc',
    'emu.run', 'emu.config',
    'emu.device', 'emu.longrun', 'emu.panel', 'emu.screen', 'emu.symbols', 'emu.dtim',
    'emu.pit', 'emu.harness', 'emu.native', 'emu.fastuc', 'emu.unicorn_compat',
    'emu.dspboot', 'emu.dsp', 'emu.edma', 'emu.gpio', 'emu.hle',
    'emu.softfloat', 'emu.ssi', 'emu.trace', 'emu.bootrom', 'dt2.container',
    'emu.fwcheck', 'emu.fwcompare', 'emu.session', 'emu.strict', 'emu.cftiming',
    'dt2.elz',
    'dt2.coldfire',
)
# The standard library and bindings that emu/portable.py, bootstrap.py,
# dtpanel.py and gui.py import inside functions.
LIB_MODULES = (
    'ctypes', 'ctypes.wintypes', 'logging', 'tkinter', 'tkinter.filedialog',
    'tkinter.messagebox', 'tkinter.ttk', 'unicorn', 'capstone',
)
REQUIRED_MODULES = APP_MODULES + LIB_MODULES
# Imported inside those functions too, but never in a PYZ archive, so not
# required there: msvcrt and zlib are built into python3XX.dll on Windows,
# fcntl is POSIX only, and PyInstaller always puts traceback in
# base_library.zip.
UNARCHIVED_MODULES = ('msvcrt', 'zlib', 'fcntl', 'traceback')

MAX_BYTES = 150 << 20                               # last resort; a build is ~40 MB


def path_problem(rel):
    """-> why a bundle-relative path may not ship, or None."""
    parts = [p for p in rel.replace('\\', '/').lower().split('/') if p]
    if not parts:
        return None
    name = parts[-1]
    for part in parts[:-1]:
        if part in DENY_PARTS:
            return '%s: inside a %s/ folder' % (rel, part)
    if name in DENY_PARTS:
        return '%s: named like a firmware data folder' % rel
    if name in DENY_NAMES:
        return '%s: firmware state or build litter' % rel
    for p in DENY_PREFIXES:
        if name.startswith(p):
            return '%s: %s* is not shipped' % (rel, p)
    for s in DENY_SUFFIXES:
        if name.endswith(s) or (s + '.') in name:
            return '%s: %s files are firmware or user data' % (rel, s)
    return None


def module_problem(name):
    """-> why a module name may not be in an exe's archive, or None. Checks
    the bare name and the tools.<name> spelling (tools/ is a namespace
    package when the repo root is on the path)."""
    for p in PRIVATE_MODULES:
        for q in (p, 'tools.' + p):
            if name == q or name.startswith(q + '.'):
                return 'private module %s in the archive' % name
    return None


def toc_problems(entries, root=None):
    """-> problems with PyInstaller TOC entries (dest, src, typecode) before
    they are written. Checks the destination name, the source file's name,
    and, for sources inside the repo `root`, the path relative to it."""
    out = []
    for dest, src, _typecode in entries:
        why = path_problem(dest)
        if why is None and src:
            why = path_problem(os.path.basename(src))
            if why is None and root:
                try:
                    rel = os.path.relpath(src, root)
                except ValueError:                  # another drive
                    rel = None
                if rel and not rel.startswith('..'):
                    why = path_problem(rel)
        if why:
            out.append('%s (from %s)' % (why, src))
    return out


def firmware_hashes(devices_dir):
    """-> the lower-case sha256 of every [[firmware]] in devices_dir/*.toml."""
    if not devices_dir:
        return set()
    if tomllib is None:
        raise RuntimeError('tomllib (Python 3.11+) is needed to read %s' % devices_dir)
    out = set()
    for n in sorted(os.listdir(devices_dir)):
        if n.endswith('.toml'):
            with open(os.path.join(devices_dir, n), 'rb') as fh:
                data = tomllib.load(fh)
            for fw in data.get('firmware', []):
                if fw.get('sha256'):
                    out.add(str(fw['sha256']).lower())
    return out


def pyinstaller_lister():
    """-> exe path -> archive entry names (PYZ modules included), or None
    when PyInstaller is not importable here."""
    try:
        from PyInstaller.archive.readers import pkg_archive_contents
    except ImportError:
        return None
    return lambda exe: pkg_archive_contents(exe, recursive=True)


def _is_link(path):
    """Symlinks and Windows junctions: zip would follow them out of dist."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & reparse)


def _scan(path):
    """-> (first 4 bytes, sha256 hex) in one read."""
    h = hashlib.sha256()
    head = b''
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            if not head:
                head = chunk[:4]
            h.update(chunk)
    return head, h.hexdigest()


# -- Control Flow Guard -------------------------------------------------------
# PyInstaller's prebuilt Windows bootloader is linked with /guard:cf, so both
# exes carry IMAGE_DLLCHARACTERISTICS_GUARD_CF (DllCharacteristics 0xc160) and
# Windows runs the whole process under Control Flow Guard. python.exe does not
# (0x8160), which is the only reason the emulator runs in dev. Under CFG the
# first longjmp out of Unicorn kills the process with 0xC0000409 (a fast fail
# in VCRUNTIME140.dll at +0x21f4). Unicorn's MSVC setjmp wrapper
# (qemu/util/setjmp-wrapper-win32.asm) stores jmp_buf.Frame = 0 on purpose, so
# that longjmp does not unwind through generated code, and when CFG is on,
# VCRUNTIME's longjmp validates the jump buffer and fast-fails on a zero Frame.
# Every emulation takes that longjmp. The user chose to clear only that bit in
# the two finished exes. The process then runs with exactly the mitigations
# python.exe has (ASLR with high entropy, DEP and the rest of 0x8160 stay on);
# python311.dll and unicorn.dll are not CFG-instrumented in the first place.
# The alternatives were a 7th Unicorn patch with its own longjmp, or a
# bootloader rebuilt without /guard:cf.
IMAGE_DLLCHARACTERISTICS_GUARD_CF = 0x4000


def pe_offsets(data):
    """-> (DllCharacteristics offset, CheckSum offset) in the PE image
    `data`, or raise ValueError. Both fields sit at the same place in PE32
    and PE32+ optional headers (+70 and +64)."""
    if len(data) < 0x40 or data[:2] != b'MZ':
        raise ValueError('not a PE image (no MZ header)')
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    opt = pe + 24
    if opt + 72 > len(data) or data[pe:pe + 4] != b'PE\0\0':
        raise ValueError('not a PE image (no PE signature at 0x%x)' % pe)
    size_opt = struct.unpack_from('<H', data, pe + 20)[0]
    magic = struct.unpack_from('<H', data, opt)[0]
    if magic not in (0x10b, 0x20b) or size_opt < 72:
        raise ValueError('not a PE image (optional header magic 0x%x, size %d)' % (magic, size_opt))
    return opt + 70, opt + 64


def pe_checksum(data, checksum_offset):
    """-> the PE checksum of the whole file `data`, as the Windows loader
    and imagehlp's CheckSumMappedFile define it: the one's-complement sum of
    its 16-bit words with the CheckSum field taken as zero, plus the file
    length. The whole file counts, including the archive PyInstaller
    appends after the last section (PyInstaller sums it the same way)."""
    buf = bytearray(data)
    buf[checksum_offset:checksum_offset + 4] = bytes(4)
    if len(buf) % 2:
        buf.append(0)
    words = array.array('H', bytes(buf))
    if sys.byteorder == 'big':
        words.byteswap()
    total = sum(words)
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return (total + len(data)) & 0xffffffff


def pe_info(data):
    """-> {'dll_characteristics', 'guard_cf', 'checksum', 'computed'} for
    the PE image `data`, or raise ValueError."""
    dc_off, cs_off = pe_offsets(data)
    dc = struct.unpack_from('<H', data, dc_off)[0]
    return {'dll_characteristics': dc, 'guard_cf': bool(dc & IMAGE_DLLCHARACTERISTICS_GUARD_CF),
            'checksum': struct.unpack_from('<I', data, cs_off)[0],
            'computed': pe_checksum(data, cs_off)}


def clear_guard_cf(path):
    """Clear IMAGE_DLLCHARACTERISTICS_GUARD_CF in the exe at `path` and set
    its PE checksum; see the comment above for why. Only those 6 header bytes
    change: the file keeps its length and the archive appended to it. It is
    written to path.tmp, checked, then renamed over path. Running it again
    changes nothing. -> {'changed', 'before': pe_info, 'after': pe_info}."""
    with open(path, 'rb') as fh:
        data = fh.read()
    before = pe_info(data)
    if not before['guard_cf'] and before['checksum'] == before['computed']:
        return {'changed': False, 'before': before, 'after': before}
    dc_off, cs_off = pe_offsets(data)
    buf = bytearray(data)
    struct.pack_into('<H', buf, dc_off,
                     before['dll_characteristics'] & ~IMAGE_DLLCHARACTERISTICS_GUARD_CF)
    struct.pack_into('<I', buf, cs_off, pe_checksum(buf, cs_off))
    after = pe_info(buf)
    # Put the two fields back: what is left must be the original file.
    back = bytearray(buf)
    back[dc_off:dc_off + 2] = data[dc_off:dc_off + 2]
    back[cs_off:cs_off + 4] = data[cs_off:cs_off + 4]
    if back != data or after['guard_cf'] or after['checksum'] != after['computed']:
        raise RuntimeError('%s: the header rewrite did not verify' % path)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(buf)
    os.replace(tmp, path)
    with open(path, 'rb') as fh:
        if fh.read() != buf:
            raise RuntimeError('%s: reads back different from what was written' % path)
    return {'changed': True, 'before': before, 'after': after}


def describe_guard_cf(name, result):
    """-> one log line for a clear_guard_cf() result."""
    b, a = result['before'], result['after']
    if not result['changed']:
        return '%s: GUARD_CF already clear (DllCharacteristics 0x%04x, checksum 0x%08x)' % (
            name, b['dll_characteristics'], b['checksum'])
    return ('%s: GUARD_CF cleared: DllCharacteristics 0x%04x -> 0x%04x, '
            'PE checksum 0x%08x -> 0x%08x' % (name, b['dll_characteristics'],
                                               a['dll_characteristics'], b['checksum'], a['checksum']))


def exe_problem(path, rel):
    """-> why the exe at `path` cannot run the emulator, or None."""
    try:
        with open(path, 'rb') as fh:
            info = pe_info(fh.read())
    except (OSError, ValueError) as exc:
        return '%s: %s' % (rel, exc)
    if info['guard_cf']:
        return ('%s: built with Control Flow Guard (DllCharacteristics 0x%04x): Unicorn\'s '
                'longjmp dies under it with 0xC0000409; the spec clears it with clear_guard_cf()'
                % (rel, info['dll_characteristics']))
    if info['checksum'] != info['computed']:
        return '%s: PE checksum 0x%08x is stale (the file sums to 0x%08x)' % (
            rel, info['checksum'], info['computed'])
    return None


def audit(dist, devices_dir=None, unicorn_sha256=None, lister=None,
          require_pyz=False, max_bytes=MAX_BYTES, required=REQUIRED_MODULES):
    """-> (files, problems). `files` are the bundle-relative paths ('/'
    separated, sorted) that make_zip() may write; any problem means none.

    dist is the COLLECT folder (dist/digiemu). Both exes must be PE images
    without Control Flow Guard and with a correct PE checksum (see
    clear_guard_cf). unicorn_sha256, when given, must match
    _internal/unicorn/lib/unicorn.dll. lister maps an exe path to its
    archive entry names (pyinstaller_lister()); with require_pyz set, not
    having one is itself a problem. When the archives can be listed, each
    exe's must hold no private module and every name in `required`."""
    problems = []
    if not os.path.isdir(dist):
        return [], ['%s: no such folder' % dist]
    top = sorted(os.listdir(dist))
    for n in top:
        if n.lower() not in TOP_LEVEL:
            problems.append('%s: unexpected top-level entry (only %s belong there)'
                            % (n, ', '.join(TOP_LEVEL)))
    for n in TOP_LEVEL:
        if n not in [t.lower() for t in top]:
            problems.append('%s: missing' % n)

    bad_hashes = firmware_hashes(devices_dir)
    files, total, unicorns = [], 0, []
    for here, dirs, names in os.walk(dist):
        for d in list(dirs):
            if _is_link(os.path.join(here, d)):
                problems.append('%s: link or junction' % os.path.relpath(os.path.join(here, d), dist))
                dirs.remove(d)
        dirs.sort()
        for n in sorted(names):
            full = os.path.join(here, n)
            rel = os.path.relpath(full, dist).replace(os.sep, '/')
            if _is_link(full):
                problems.append('%s: link or junction' % rel)
                continue
            why = path_problem(rel)
            if why:
                problems.append(why)
                continue
            head, sha = _scan(full)
            total += os.path.getsize(full)
            if head == SYSEX:
                problems.append('%s: starts with an Elektron SysEx header' % rel)
                continue
            if sha in bad_hashes:
                problems.append('%s: is a firmware release listed in %s' % (rel, devices_dir))
                continue
            if rel.lower() in EXES:
                why = exe_problem(full, rel)
                if why:
                    problems.append(why)
            if n.lower() == 'unicorn.dll':
                unicorns.append((rel, sha))
            files.append(rel)

    if [r.lower() for r, _ in unicorns] != [UNICORN_DLL]:
        problems.append('unicorn.dll: expected exactly %s, found %r'
                        % (UNICORN_DLL, [r for r, _ in unicorns]))
    elif unicorn_sha256 and unicorns[0][1] != unicorn_sha256.lower():
        problems.append('%s: sha256 %s, expected the patched build %s'
                        % (UNICORN_DLL, unicorns[0][1], unicorn_sha256.lower()))
    if CAPSTONE_DLL not in [f.lower() for f in files]:
        problems.append('%s: missing' % CAPSTONE_DLL)
    if total > max_bytes:
        problems.append('bundle is %d bytes, over the %d budget' % (total, max_bytes))

    if lister is None and require_pyz:
        problems.append('cannot list the exe archives (PyInstaller not importable)')
    elif lister is not None:
        for exe in EXES:
            path = os.path.join(dist, exe)
            if not os.path.isfile(path):
                continue
            try:
                names = list(lister(path))
            except Exception as exc:                # noqa: BLE001
                problems.append('%s: cannot list its archive: %s' % (exe, exc))
                continue
            for name in names:
                why = module_problem(name)
                if why:
                    problems.append('%s: %s' % (exe, why))
            have = set(names)
            missing = [m for m in required if m not in have]
            if missing:
                problems.append('%s: the archive lacks modules the app imports at run time: %s'
                                % (exe, ', '.join(missing)))
    return (sorted(files) if not problems else []), problems


def make_zip(dist, files, out, arcroot='digiemu'):
    """Write `files` (from a clean audit) under arcroot/ in the zip `out`,
    atomically: out.tmp, then os.replace. -> out."""
    if not files:
        raise ValueError('nothing to zip: audit first, and only zip a clean result')
    out = os.path.abspath(out)
    tmp = out + '.tmp'
    with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in files:
            zf.write(os.path.join(dist, *rel.split('/')), arcroot + '/' + rel)
    os.replace(tmp, out)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('dist', help='the COLLECT folder, e.g. build-out/dist/digiemu')
    ap.add_argument('--devices', help='devices/ dir: its firmware hashes are refused')
    ap.add_argument('--unicorn-sha256', help='the patched unicorn.dll hash to require')
    ap.add_argument('--require-pyz', action='store_true',
                    help='fail if the exe archives cannot be listed')
    ap.add_argument('--zip', metavar='OUT', help='write OUT.zip if the audit is clean')
    ap.add_argument('--arcroot', default='digiemu', help='folder name inside the zip')
    args = ap.parse_args(argv)
    files, problems = audit(args.dist, args.devices, args.unicorn_sha256,
                            pyinstaller_lister(), args.require_pyz)
    for p in problems:
        print('REFUSED: %s' % p)
    if problems:
        print('bundle audit FAILED: %d problem(s); nothing zipped' % len(problems))
        return 1
    print('bundle audit OK: %d files' % len(files))
    if args.zip:
        out = make_zip(args.dist, files, args.zip, args.arcroot)
        print('zip %s: %d bytes, %d files' % (out, os.path.getsize(out), len(files)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
