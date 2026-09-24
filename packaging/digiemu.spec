# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the portable Windows app (onedir).

Build it with tools/build-windows.ps1, which makes the offline build venv,
passes DIGIEMU_VERSION and DIGIEMU_UC_SHA256, runs the self-test on the new
dist and zips it through packaging/bundle_guard.py. By hand, from a venv
that has PyInstaller, unicorn and capstone:
    python -m PyInstaller --noconfirm --clean --distpath OUT/dist --workpath OUT/work packaging/digiemu.spec

What it makes: dist/digiemu/ with digiemu.exe (windowed), digiemu-console.exe
(the same program with a console, for support), and one shared _internal/.

Why each piece is here:
  * unicorn.dll is added by hand. Neither PyInstaller nor hooks-contrib has a
    unicorn hook, and the binding builds its LoadLibrary path at run time, so
    the import scan cannot see it. It goes to unicorn/lib, where the binding
    looks (and where rth_digiemu.py points LIBUNICORN_PATH). It must be the
    PATCHED build, and nothing but the hash tells a stock one from it before
    it runs. So its sha256 must equal DIGIEMU_UC_SHA256, or, if that is not
    set, the hash of the DLL in this repo's .venv (the one the emulator is
    developed and tested against). The hash is worked out at build time,
    never written into this file, so a rebuilt DLL cannot meet a stale pin.
    (DIGIEMU_UC_DLL names a different file to bundle; the pin still applies.
    tests/test_packaging.py uses it to run this spec on a fake DLL.)
    upx and strip are off, so the file is copied unchanged, and
    bundle_guard checks the hash again in the finished folder. capstone.dll
    comes from hooks-contrib's capstone hook.
  * tools/ is NOT on pathex. emu/gui.py puts tools/ on sys.path for a lazy
    `from machinepatch import ...` (firmware patching, which is private).
    With tools/ on the path, the import scan would follow that into the
    bundle. The private modules are also in excludes, and bundle_guard
    checks the archive for them. The repo root is on pathex, so emu/ and
    dt2/ import as packages.
  * 'X utf8' runs the frozen interpreter in UTF-8 mode, the same as
    PYTHONUTF8=1 for the dev children. Paths and pipes are then UTF-8
    whatever the Windows code page is.
  * datas carry the device files (the frozen app reads them from
    _internal/devices), plus the licences. patches/ is there because the
    DLL is a modified GPL build and must ship with the source of its
    changes.
  * hiddenimports are bundle_guard.REQUIRED_MODULES: what the app imports
    inside functions. The scan finds them today, but a module that is not
    there fails only on a user's first run, so an analysis without one
    stops the build (and the frozen --selftest imports them all).
  * After COLLECT, Control Flow Guard is switched off in both exes (the
    GUARD_CF bit only, then the PE checksum). PyInstaller's bootloader is
    built with it and python.exe is not, and under it Unicorn's first
    longjmp kills the process with 0xC0000409. bundle_guard.py has the
    details, and its audit refuses an exe that still has the bit.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import sys
import sysconfig

from PyInstaller.utils.win32 import versioninfo as vi

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))


def _load_guard():
    # By path, not by sys.path: anything on sys.path is also an import root
    # for the analysis below.
    spec = importlib.util.spec_from_file_location(
        'digiemu_bundle_guard', os.path.join(SPECPATH, 'bundle_guard.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load_guard()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# -- version -----------------------------------------------------------------
VERSION = os.environ.get('DIGIEMU_VERSION', '0.1.0')
if (not re.fullmatch(r'\d{1,5}\.\d{1,5}\.\d{1,5}', VERSION)
        or max(int(x) for x in VERSION.split('.')) > 0xFFFF):
    raise SystemExit('DIGIEMU_VERSION must be x.y.z (each 0-65535), got %r' % VERSION)
VERSION_TUPLE = tuple(int(x) for x in VERSION.split('.')) + (0,)

# -- the patched unicorn.dll -------------------------------------------------
UC_DLL = os.environ.get('DIGIEMU_UC_DLL') or os.path.join(
    importlib.util.find_spec('unicorn').submodule_search_locations[0], 'lib', 'unicorn.dll')
UC_REFERENCE = os.path.join(ROOT, '.venv', 'Lib', 'site-packages', 'unicorn', 'lib', 'unicorn.dll')
UC_SHA256 = (os.environ.get('DIGIEMU_UC_SHA256') or '').strip().lower()
if not UC_SHA256:
    if not os.path.isfile(UC_REFERENCE):
        raise SystemExit('set DIGIEMU_UC_SHA256 to the patched unicorn.dll hash '
                         '(no reference DLL at %s)' % UC_REFERENCE)
    UC_SHA256 = _sha256(UC_REFERENCE)
if not os.path.isfile(UC_DLL):
    raise SystemExit('no unicorn.dll at %s' % UC_DLL)
_got = _sha256(UC_DLL)
if _got != UC_SHA256:
    raise SystemExit('refusing to bundle %s (sha256 %s): expected the patched build %s'
                     % (UC_DLL, _got, UC_SHA256))

# -- licences ----------------------------------------------------------------
def _dist_file(dist, pattern):
    """-> the absolute path of the first file of installed `dist` whose name
    matches `pattern` (e.g. its LICENSE), or stop the build."""
    d = importlib.metadata.distribution(dist)
    for f in d.files or ():
        if re.fullmatch(pattern, os.path.basename(str(f)), re.I):
            return os.path.abspath(str(d.locate_file(f)))
    raise SystemExit('no %s in the installed %s distribution' % (pattern, dist))


def _python_license():
    # <base>\LICENSE.txt on Windows; <stdlib>/LICENSE.txt elsewhere (the
    # spec's own test runs on Linux too).
    for p in (os.path.join(sys.base_prefix, 'LICENSE.txt'),
              os.path.join(sysconfig.get_path('stdlib'), 'LICENSE.txt')):
        if os.path.isfile(p):
            return p
    raise SystemExit('no Python LICENSE.txt under %s' % sys.base_prefix)


# -- build info, read by the self-test ------------------------------------
# Written under workpath, which --clean empties.
BUILD_INFO = os.path.join(workpath, 'digiemu-build.json')
os.makedirs(workpath, exist_ok=True)
with open(BUILD_INFO, 'w', encoding='utf-8', newline='\n') as fh:
    json.dump({'version': VERSION, 'unicorn_sha256': UC_SHA256,
               'python': sys.version.split()[0]}, fh, indent=2, sort_keys=True)
    fh.write('\n')

datas = [
    (os.path.join(ROOT, 'devices', '*.toml'), 'devices'),
    (os.path.join(ROOT, 'LICENSE'), '.'),
    (os.path.join(ROOT, 'patches', '*.patch'), 'patches'),
    (os.path.join(ROOT, 'patches', 'README.md'), 'patches'),
    (_dist_file('capstone', r'LICENSE(\.txt)?'), os.path.join('licenses', 'capstone')),
    (_python_license(), os.path.join('licenses', 'python')),
    (BUILD_INFO, '.'),
]

# What the app imports inside functions (bundle_guard.REQUIRED_MODULES).
# PyInstaller only warns about a hidden import it cannot find, so the guard
# below also checks that the analysis holds every one.
HIDDEN = list(guard.REQUIRED_MODULES)
EXCLUDES = (list(guard.PRIVATE_MODULES)
            + ['tools.' + m for m in guard.PRIVATE_MODULES if '.' not in m]
            + ['unicorn.unicorn_py2', 'pypcode'])

a = Analysis(
    [os.path.join(SPECPATH, 'digiemu_main.py')],
    pathex=[ROOT],
    binaries=[(UC_DLL, os.path.join('unicorn', 'lib'))],
    datas=datas,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(SPECPATH, 'rth_digiemu.py')],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)


# -- guards: fail the build before anything is written -------------------
def _n(p):
    return os.path.normcase(os.path.normpath(p))


_uc = [(d, s) for d, s, _t in a.binaries if os.path.basename(d).lower() == 'unicorn.dll']
if len(_uc) != 1 or _n(_uc[0][0]) != _n('unicorn/lib/unicorn.dll') or _n(_uc[0][1]) != _n(UC_DLL):
    raise SystemExit('unexpected unicorn.dll collection: %r' % _uc)
if not any(_n(d) == _n('capstone/lib/capstone.dll') for d, _s, _t in a.binaries):
    raise SystemExit('capstone.dll was not collected (is hooks-contrib installed?)')
_leak = guard.toc_problems(list(a.datas) + list(a.binaries), root=ROOT)
if _leak:
    raise SystemExit('firmware-derived files would be bundled:\n  ' + '\n  '.join(_leak[:20]))
_private = [m for m in [name for name, _s, _t in a.pure] + [name for name, _s, _t in a.scripts]
            if guard.module_problem(m)]
if _private:
    raise SystemExit('private modules would be bundled: %r' % _private)
_have = {name for name, _s, _t in a.pure}
_lacking = [m for m in guard.REQUIRED_MODULES if m not in _have]
if _lacking:
    raise SystemExit('the analysis lacks modules the app imports at run time: %s'
                     % ', '.join(_lacking))

pyz = PYZ(a.pure)


def _version(fname):
    return vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=VERSION_TUPLE, prodvers=VERSION_TUPLE),
        kids=[
            vi.StringFileInfo([vi.StringTable('040904B0', [
                vi.StringStruct('FileDescription', 'digiemu - Digitakt and Digitone emulator (unofficial)'),
                vi.StringStruct('ProductName', 'digiemu'),
                vi.StringStruct('FileVersion', VERSION),
                vi.StringStruct('ProductVersion', VERSION),
                vi.StringStruct('OriginalFilename', fname),
                vi.StringStruct('LegalCopyright', 'GPL-2.0-or-later; see _internal/LICENSE'),
            ])]),
            vi.VarFileInfo([vi.VarStruct('Translation', [0x0409, 0x04B0])]),
        ])


_ico = os.path.join(SPECPATH, 'digiemu.ico')
ICON = _ico if os.path.exists(_ico) else None


def _exe(name, console):
    return EXE(pyz, a.scripts, [('X utf8', None, 'OPTION')],
               exclude_binaries=True, name=name, debug=False,
               bootloader_ignore_signals=False, strip=False, upx=False,
               console=console, disable_windowed_traceback=False,
               icon=ICON, version=_version(name + '.exe'),
               contents_directory='_internal')


exe = _exe('digiemu', console=False)
exe_console = _exe('digiemu-console', console=True)
coll = COLLECT(exe, exe_console, a.binaries, a.datas,
               strip=False, upx=False, upx_exclude=[], name='digiemu')

# -- Control Flow Guard off in the finished exes ---------------------------
# COLLECT has written dist/digiemu by now. PyInstaller's bootloader is built
# with /guard:cf, python.exe is not, and under CFG the emulator cannot run:
# Unicorn's MSVC setjmp wrapper sets jmp_buf.Frame = 0, and VCRUNTIME140's
# longjmp fast-fails on that (0xC0000409) when CFG is on. Clear only the
# GUARD_CF bit and recompute the PE checksum; the archive PyInstaller
# appended stays byte for byte (bundle_guard.clear_guard_cf has the rest).
for _name in guard.EXES:
    _path = os.path.join(DISTPATH, 'digiemu', _name)
    if not os.path.isfile(_path):
        raise SystemExit('COLLECT did not write %s' % _path)
    print('digiemu.spec: ' + guard.describe_guard_cf(_name, guard.clear_guard_cf(_path)), flush=True)
