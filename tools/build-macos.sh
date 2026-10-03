#!/usr/bin/env bash
# Build the macOS app, digiemu.app (Apple silicon), into
# <out>/digiemu-macos-arm64-<version>.dmg.
#
#     tools/build-macos.sh --python VENV/bin/python --out DIR [--version x.y.z]
#                          [--identity ID [--notarize]]
#
# --python is a venv that has requirements.txt, requirements-build.txt and the
# patched Unicorn (PYTHON=VENV/bin/python tools/install-patched-unicorn.sh).
# --version defaults to APP_VERSION in emu/portable.py; a release tag must
# match it. --identity is a Developer ID Application identity in the keychain
# (its name or SHA-1). Without it the app is signed ad hoc, which is for
# trying it on this Mac only. --notarize has Apple notarize the app and the
# .dmg and staples both, with an App Store Connect API key: NOTARY_KEY (the
# .p8 file), NOTARY_KEY_ID and NOTARY_ISSUER.
#
# Steps, each checked before the next:
#  1. The venv: PyInstaller's version, hooks-contrib, and the patched
#     libunicorn.2.dylib, by behaviour (emu.unicorn_compat). Its sha256 is
#     what the spec pins, and its LC_UUID what the audit looks for in the
#     app (packaging/bundle_guard.py macho_uuid() says why not the hash).
#  2. PyInstaller with packaging/digiemu-macos.spec, dist and work under
#     --out (never inside the repo). It signs every binary and then the app,
#     with the hardened runtime and packaging/digiemu.entitlements.
#  3. The signature: codesign --verify --deep --strict, and with --identity,
#     the hardened runtime and allow-jit on the executable. Unicorn's JIT
#     cannot allocate its code buffer without allow-jit ("Could not allocate
#     dynamic translator buffer").
#  4. The frozen self-test, run by the signed app as it ships, with HOME in
#     --out so that nothing lands in anyone's Application Support. Then the
#     signature again: nothing may have been written inside the app.
#  5. packaging/bundle_guard.py audits the app: no firmware, no private
#     modules, every required module in the archive, one arm64 executable,
#     libunicorn.2.dylib with the patched build's LC_UUID.
#  6. With --notarize, the app is notarized and stapled, so that it opens
#     offline once it is copied out of the .dmg.
#  7. The .dmg: the app and a link to /Applications. It is signed, and the
#     app in it, mounted read-only, is audited again: that copy is what ships.
#  8. With --notarize, the .dmg is notarized and stapled, and Gatekeeper must
#     accept both the .dmg and the app.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
py='' out='' version='' identity='' notarize=false
usage() {
  echo "usage: $0 --python VENV/bin/python --out DIR [--version x.y.z] [--identity ID [--notarize]]" >&2
  exit 2
}
while (($#)); do
  case $1 in
  --python) py=${2:-} && shift 2 ;;
  --out) out=${2:-} && shift 2 ;;
  --version) version=${2:-} && shift 2 ;;
  --identity) identity=${2:-} && shift 2 ;;
  --notarize) notarize=true && shift ;;
  *) usage ;;
  esac
done
[[ -n $py && -n $out ]] || usage

fail() {
  echo "build-macos: FAILED: $*" >&2
  exit 1
}
step() { printf '\n== %s\n' "$*"; }
sha256() { shasum -a 256 "$1" | awk '{print $1}'; }

[[ $(uname -s) == Darwin ]] || fail 'this builds the macOS app: run it on macOS'
[[ -x $py ]] || fail "no Python at $py"
if [[ -z $version ]]; then
  version=$(sed -n "s/^APP_VERSION = '\([^']*\)'.*/\1/p" "$repo/emu/portable.py" | head -1)
fi
[[ $version =~ ^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$ ]] || fail "--version must be x.y.z, got '$version'"
if $notarize; then
  [[ -n $identity ]] || fail '--notarize needs --identity'
  [[ -n ${NOTARY_KEY:-} && -n ${NOTARY_KEY_ID:-} && -n ${NOTARY_ISSUER:-} ]] ||
    fail '--notarize needs NOTARY_KEY (the .p8 file), NOTARY_KEY_ID and NOTARY_ISSUER'
  [[ -f $NOTARY_KEY ]] || fail "NOTARY_KEY: no file at $NOTARY_KEY"
fi
mkdir -p "$out"
out=$(cd "$out" && pwd)
case $out/ in "$repo"/*) fail "--out must be outside the repo ($out): dist/ would sit next to sections/ and snapshots/" ;; esac

app=$out/dist/digiemu.app
dmg=$out/digiemu-macos-arm64-$version.dmg
exe=$app/Contents/MacOS/digiemu
for v in TCL_LIBRARY TK_LIBRARY LIBUNICORN_PATH LIBCAPSTONE_PATH PYTHONPATH PYTHONHOME DIGIEMU_UC_DLL; do
  unset "$v"
done
export PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# -- 1. the venv ------------------------------------------------------------------
step 'verify the venv'
dylib=$("$py" -c 'import os, unicorn; print(os.path.join(os.path.dirname(unicorn.__file__), "lib", "libunicorn.2.dylib"))')
[[ -f $dylib ]] || fail "no $dylib (run tools/install-patched-unicorn.sh with PYTHON=$py)"
uc_sha=$(sha256 "$dylib")
uc_uuid=$("$py" -c 'import sys; sys.path.insert(0, sys.argv[1]); import bundle_guard as g; print(g.macho_uuid(open(sys.argv[2], "rb").read()))' "$repo/packaging" "$dylib")
echo "  libunicorn.2.dylib  sha256 $uc_sha"
echo "                      LC_UUID $uc_uuid"
(cd "$repo" && "$py" -m emu.unicorn_compat > "$out/unicorn-compat.json") ||
  fail "emu.unicorn_compat: this is not the patched Unicorn ($out/unicorn-compat.json)"
echo '  emu.unicorn_compat: compatible'
echo "  PyInstaller $("$py" -m PyInstaller --version)"
"$py" -c "import importlib.metadata as m, sys; sys.exit('_pyinstaller_hooks_contrib' not in str(sorted(e.value for e in m.entry_points(group='pyinstaller40'))))" ||
  fail 'hooks-contrib is not registered'
echo "  Python $("$py" -c 'import sys; print(sys.version.split()[0])')"

# -- 2. PyInstaller -----------------------------------------------------------------
signer='signed ad hoc'
[[ -z $identity ]] || signer="signed by $identity"
step "PyInstaller (version $version, $signer)"
rm -rf "$out/dist" "$out/work"
if ! (cd "$out" && DIGIEMU_VERSION=$version DIGIEMU_UC_SHA256=$uc_sha DIGIEMU_CODESIGN_IDENTITY=$identity \
  "$py" -m PyInstaller --noconfirm --clean --distpath "$out/dist" --workpath "$out/work" \
  "$repo/packaging/digiemu-macos.spec" > "$out/pyinstaller.log" 2>&1); then
  grep -E 'WARNING|ERROR|Error|refusing|would be bundled|lacks modules' "$out/pyinstaller.log" | tail -20 || true
  fail "PyInstaller (log: $out/pyinstaller.log)"
fi
grep -E 'WARNING|refusing|would be bundled|lacks modules|Build complete' "$out/pyinstaller.log" | sed 's/^/  /' || true
! grep -q 'tkinter installation is broken' "$out/pyinstaller.log" || fail "PyInstaller dropped tkinter (log: $out/pyinstaller.log)"
[[ -x $exe ]] || fail "no $exe"

# -- 3. the signature ---------------------------------------------------------------
step 'signature'
codesign --verify --deep --strict "$app" || fail 'codesign --verify --deep --strict'
ents=$(codesign -d --entitlements - --xml "$app" 2>/dev/null) || true
printf '%s' "$ents" | "$py" -c 'import plistlib, sys; sys.exit(plistlib.loads(sys.stdin.buffer.read()).get("com.apple.security.cs.allow-jit") is not True)' ||
  fail 'the executable lacks com.apple.security.cs.allow-jit'
if [[ -n $identity ]]; then
  sig=$(codesign -dvv "$app" 2>&1) || fail 'codesign -dvv'
  [[ $sig == *'(runtime)'* ]] || fail 'the app is not signed with the hardened runtime'
  printf '%s\n' "$sig" | grep -E '^(Authority=Developer ID Application|TeamIdentifier|Timestamp)' | sed 's/^/  /'
fi
echo '  valid, with allow-jit'

# -- 4. frozen self-test --------------------------------------------------------------
step 'self-test (frozen, signed)'
mkdir -p "$out/selftest-home"
if ! HOME=$out/selftest-home "$exe" --selftest --json "$out/selftest.json" > "$out/selftest.out" 2>&1; then
  tail -20 "$out/selftest.out" | sed 's/^/  /'
  fail "the app's --selftest (report: $out/selftest.json)"
fi
"$py" -c 'import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
for c in r["checks"]:
    print("    %-15s %s" % (c["name"], "ok" if c["ok"] else "FAILED"))
sys.exit(not r["ok"])' "$out/selftest.json" || fail "self-test report: $out/selftest.json"
codesign --verify --deep --strict "$app" || fail 'the self-test changed something inside the app'
echo '  ok, and the signature is still valid'

# -- 5. audit -------------------------------------------------------------------------
step 'audit'
audit() {
  "$py" "$repo/packaging/bundle_guard.py" "$1" --devices "$repo/devices" \
    --unicorn-source "$dylib" --require-pyz | sed 's/^/  /'
  return "${PIPESTATUS[0]}"
}
audit "$app" || fail 'bundle audit'

# -- notarization ---------------------------------------------------------------------
# Submit `file` (the app, zipped, or the .dmg) and wait; Apple's log if refused.
notarize() {
  local file=$1 res id status
  echo "  notarizing $(basename "$file")"
  res=$(xcrun notarytool submit "$file" --key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" \
    --issuer "$NOTARY_ISSUER" --wait --timeout 1h --output-format json) || true
  id=$(printf '%s' "$res" | "$py" -c 'import json, sys; print(json.load(sys.stdin).get("id", ""))' 2>/dev/null || true)
  status=$(printf '%s' "$res" | "$py" -c 'import json, sys; print(json.load(sys.stdin).get("status", ""))' 2>/dev/null || true)
  echo "  notarytool: ${id:-no id} ${status:-no status}"
  if [[ $status != Accepted ]]; then
    [[ -z $id ]] || xcrun notarytool log "$id" --key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER" || true
    fail "notarizing $(basename "$file"): $res"
  fi
}
staple() {
  xcrun stapler staple "$1" | tail -1 | sed 's/^/  /'
  xcrun stapler validate "$1" > /dev/null || fail "stapler validate $(basename "$1")"
}

# -- 6. notarize the app ----------------------------------------------------------------
if $notarize; then
  step 'notarize the app'
  ditto -c -k --keepParent "$app" "$out/digiemu-notarize.zip"
  notarize "$out/digiemu-notarize.zip"
  rm -f "$out/digiemu-notarize.zip"
  staple "$app"
  # The ticket is now inside the app: its signature must still hold.
  codesign --verify --deep --strict "$app" || fail 'codesign --verify after stapling'
fi

# -- 7. the .dmg ------------------------------------------------------------------------
step 'the .dmg'
stage=$out/dmg
rm -rf "$stage" "$dmg"
mkdir -p "$stage"
ditto "$app" "$stage/digiemu.app"
ln -s /Applications "$stage/Applications"
for attempt in 1 2 3; do # hdiutil is sometimes "busy" on CI machines
  if hdiutil create -quiet -volname "digiemu $version" -srcfolder "$stage" -fs HFS+ -format UDZO -ov "$dmg"; then
    break
  fi
  ((attempt < 3)) || fail 'hdiutil create'
  sleep 10
done
rm -rf "$stage"
if [[ -n $identity ]]; then
  codesign --force --sign "$identity" --timestamp "$dmg" || fail 'codesign the .dmg'
fi
mnt=$(mktemp -d "$out/mnt.XXXXXX")
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$mnt" "$dmg" || fail 'hdiutil attach'
trap 'hdiutil detach -quiet "$mnt" 2>/dev/null || true' EXIT
[[ $(ls "$mnt") == $'Applications\ndigiemu.app' ]] || fail "the .dmg holds $(ls "$mnt" | tr '\n' ' ')"
echo '  in the .dmg:'
audit "$mnt/digiemu.app" || fail 'bundle audit of the app in the .dmg'
codesign --verify --deep --strict "$mnt/digiemu.app" || fail 'codesign --verify of the app in the .dmg'
hdiutil detach -quiet "$mnt"
trap - EXIT
rmdir "$mnt"

# -- 8. notarize the .dmg ---------------------------------------------------------------
if $notarize; then
  step 'notarize the .dmg'
  notarize "$dmg"
  staple "$dmg"
  spctl --assess --type execute --verbose=2 "$app" 2>&1 | sed 's/^/  /'
  spctl --assess --type open --context context:primary-signature --verbose=2 "$dmg" 2>&1 | sed 's/^/  /'
  spctl --assess --type execute "$app" && spctl --assess --type open --context context:primary-signature "$dmg" ||
    fail 'Gatekeeper does not accept the app or the .dmg'
fi

echo
echo "built $dmg"
echo "  $(stat -f %z "$dmg") bytes, app $(du -sk "$app" | awk '{print $1}') KB unpacked"
echo "  sha256 $(sha256 "$dmg")"
if $notarize; then
  echo '  signed, notarized and stapled'
elif [[ -n $identity ]]; then
  echo '  signed, not notarized'
else
  echo '  signed ad hoc: for this Mac only, not for a release'
fi
