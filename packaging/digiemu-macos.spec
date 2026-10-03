# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the macOS app (digiemu.app, Apple silicon).

Build it with tools/build-macos.sh, which passes DIGIEMU_VERSION,
DIGIEMU_UC_SHA256 and, to sign, DIGIEMU_CODESIGN_IDENTITY, runs the
self-test on the signed app, audits it through packaging/bundle_guard.py and
makes the .dmg. By hand, from a venv that has PyInstaller, the patched
Unicorn (tools/install-patched-unicorn.sh), capstone and python-rtmidi:
    DIGIEMU_UC_SHA256=<libunicorn.2.dylib's sha256> python -m PyInstaller --noconfirm --clean \\
        --distpath OUT/dist --workpath OUT/work packaging/digiemu-macos.spec

What it makes: dist/digiemu.app. Its one executable is the launcher, the
workers (emu.portable starts them as the same program) and, run from a
Terminal, the command line. The app keeps its data in
~/Library/Application Support/digiemu (emu.portable.mac_app_home()), never
inside the bundle: that would break its signature.

It follows packaging/digiemu.spec (the Windows app) wherever it can: tools/
is kept off pathex and the private modules are excluded, the same device
files, licences and patches/ go in, the hiddenimports are
bundle_guard.REQUIRED_MODULES, and the same guards stop the build on the
analysis. What differs, and why:
  * libunicorn.2.dylib, the patched build, is added by hand, as unicorn.dll
    is there, and pinned the same way. Its sha256 must equal
    DIGIEMU_UC_SHA256, which is required here: there is no reference venv
    to fall back on. capstone's dylib comes from hooks-contrib's hook.
  * One executable, windowed. A Terminal shows its output, so there is no
    console twin; opened from the Finder, the entry script sends its output
    to launcher.log.
  * Signing. With DIGIEMU_CODESIGN_IDENTITY (a Developer ID Application
    identity) PyInstaller signs every binary and then the bundle, with the
    hardened runtime and packaging/digiemu.entitlements. Unicorn translates
    guest code into host code as it runs (a JIT), which the hardened
    runtime refuses without com.apple.security.cs.allow-jit. Without an
    identity the app is signed ad hoc, for trying it on the Mac that built it.
  * arm64 only, and no Control Flow Guard or PE version resource: those are
    Windows things. The version goes into Info.plist instead.
"""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import sys
import sysconfig

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
UC_NAME = 'libunicorn.2.dylib'


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
if not re.fullmatch(r'\d{1,5}\.\d{1,5}\.\d{1,5}', VERSION):
    raise SystemExit('DIGIEMU_VERSION must be x.y.z, got %r' % VERSION)

# -- the patched libunicorn.2.dylib ----------------------------------------
UC_DYLIB = os.environ.get('DIGIEMU_UC_DLL') or os.path.join(
    importlib.util.find_spec('unicorn').submodule_search_locations[0], 'lib', UC_NAME)
UC_SHA256 = (os.environ.get('DIGIEMU_UC_SHA256') or '').strip().lower()
if not UC_SHA256:
    raise SystemExit('set DIGIEMU_UC_SHA256 to the patched %s hash' % UC_NAME)
if not os.path.isfile(UC_DYLIB):
    raise SystemExit('no %s at %s' % (UC_NAME, UC_DYLIB))
_got = _sha256(UC_DYLIB)
if _got != UC_SHA256:
    raise SystemExit('refusing to bundle %s (sha256 %s): expected the patched build %s'
                     % (UC_DYLIB, _got, UC_SHA256))

# -- signing -----------------------------------------------------------------
IDENTITY = (os.environ.get('DIGIEMU_CODESIGN_IDENTITY') or '').strip() or None
ENTITLEMENTS = os.path.join(SPECPATH, 'digiemu.entitlements')


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
    for p in (os.path.join(sysconfig.get_path('stdlib'), 'LICENSE.txt'),
              os.path.join(sys.base_prefix, 'LICENSE.txt')):
        if os.path.isfile(p):
            return p
    raise SystemExit('no Python LICENSE.txt under %s' % sys.base_prefix)


# -- build info, read by the self-test ------------------------------------
# Written under workpath, which --clean empties. The pin is recorded as the
# SOURCE's hash: PyInstaller rewrites and signs the bundled copy, so its
# bytes never match (bundle_guard.macho_uuid() has the details). The
# self-test's byte check only runs for 'unicorn_sha256', the Windows key.
BUILD_INFO = os.path.join(workpath, 'digiemu-build.json')
os.makedirs(workpath, exist_ok=True)
with open(BUILD_INFO, 'w', encoding='utf-8', newline='\n') as fh:
    json.dump({'version': VERSION, 'unicorn_source_sha256': UC_SHA256,
               'python': sys.version.split()[0]}, fh, indent=2, sort_keys=True)
    fh.write('\n')

datas = [
    (os.path.join(ROOT, 'devices', '*.toml'), 'devices'),
    (os.path.join(ROOT, 'LICENSE'), '.'),
    (os.path.join(ROOT, 'patches', '*.patch'), 'patches'),
    (os.path.join(ROOT, 'patches', 'README.md'), 'patches'),
    (_dist_file('capstone', r'LICENSE(\.txt)?'), os.path.join('licenses', 'capstone')),
    # python-rtmidi's LICENSE.md also carries RtMidi's, which it links in.
    (_dist_file('python-rtmidi', r'LICENSE(\.md|\.txt)?'), os.path.join('licenses', 'python-rtmidi')),
    (_python_license(), os.path.join('licenses', 'python')),
    (BUILD_INFO, '.'),
]

HIDDEN = list(guard.REQUIRED_MODULES)
EXCLUDES = (list(guard.PRIVATE_MODULES)
            + ['tools.' + m for m in guard.PRIVATE_MODULES if '.' not in m]
            + ['unicorn.unicorn_py2', 'pypcode'])

a = Analysis(
    [os.path.join(SPECPATH, 'digiemu_main.py')],
    pathex=[ROOT],
    binaries=[(UC_DYLIB, os.path.join('unicorn', 'lib'))],
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
    return os.path.normpath(p)


_uc = [(d, s) for d, s, _t in a.binaries if os.path.basename(d) == UC_NAME]
if len(_uc) != 1 or _n(_uc[0][0]) != _n('unicorn/lib/' + UC_NAME) or _n(_uc[0][1]) != _n(UC_DYLIB):
    raise SystemExit('unexpected %s collection: %r' % (UC_NAME, _uc))
if not any(_n(d) == _n('capstone/lib/libcapstone.dylib') for d, _s, _t in a.binaries):
    raise SystemExit('libcapstone.dylib was not collected (is hooks-contrib installed?)')
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

_icns = os.path.join(SPECPATH, 'digiemu.icns')
ICON = _icns if os.path.exists(_icns) else None

exe = EXE(pyz, a.scripts, [('X utf8', None, 'OPTION')],
          exclude_binaries=True, name='digiemu', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, argv_emulation=False,
          target_arch='arm64', codesign_identity=IDENTITY, entitlements_file=ENTITLEMENTS,
          icon=ICON)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name='digiemu')
app = BUNDLE(coll, name='digiemu.app', icon=ICON, bundle_identifier='io.github.irpina.digiemu',
             version=VERSION,
             info_plist={
                 'CFBundleName': 'digiemu',
                 'CFBundleDisplayName': 'digiemu',
                 'CFBundleVersion': VERSION,
                 'LSMinimumSystemVersion': '11.0',
                 'LSApplicationCategoryType': 'public.app-category.music',
                 'NSHighResolutionCapable': True,
                 'NSHumanReadableCopyright': 'GPL-2.0-or-later. Not affiliated with Elektron.',
             })
