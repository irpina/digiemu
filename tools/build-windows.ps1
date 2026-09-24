# Build the portable Windows app, offline, into <Out>\digiemu-win64-<Version>.zip.
#
#     powershell -NoProfile -ExecutionPolicy Bypass -File tools\build-windows.ps1 `
#         -BuildVenv ..\.venv-build -SitePackagesFrom <site-packages with PyInstaller> `
#         -Out ..\build-out [-Version x.y.z]
#
# -SitePackagesFrom is needed only when -BuildVenv does not exist yet.
# -Version defaults to APP_VERSION in emu\portable.py, which the app shows
# and stamps into firmware folders; a release tag must match it.
#
# Steps, each checked before the next:
#  1. The build venv. If it is missing, make it with `python -m venv` from the
#     same base interpreter as this repo's .venv (pyvenv.cfg 'home'). pip and
#     setuptools come from ensurepip's bundled wheels, so nothing is
#     downloaded. PyInstaller and its pure-Python dependencies are copied, with
#     their dist-info, from -SitePackagesFrom. The source is only read from,
#     never changed. hooks-contrib registers through its entry point, and
#     PyInstaller checks versions through importlib.metadata, which is why
#     the dist-info folders are copied too. unicorn and capstone are
#     mirrored from this repo's .venv on every run, so a rebuilt patched DLL
#     is always the one bundled. The DLL backups, include\ and the 50 MB
#     unicorn.lib are left out.
#  2. Verify: pip check, the PyInstaller version, the hooks-contrib entry
#     point, the unicorn.dll hash against this repo's .venv, the compat check
#     (by behaviour) in the build venv, and that the app code compiles on
#     this interpreter (3.11 here; the WSL tree is 3.12).
#  3. PyInstaller with packaging\digiemu.spec, dist and work under -Out (never
#     inside the repo). The spec pins the DLL hash it is given, and after
#     COLLECT it clears Control Flow Guard in both exes (the GUARD_CF bit
#     only, then the PE checksum; its log lines are shown). Why: PyInstaller's
#     bootloader is built with /guard:cf (python.exe is not), and with CFG on,
#     VCRUNTIME140's longjmp fast-fails (0xC0000409) on a jmp_buf whose Frame
#     is 0. Unicorn's MSVC setjmp wrapper (qemu/util/setjmp-wrapper-win32.asm)
#     sets Frame to 0 on purpose, so longjmp does not unwind through generated
#     code, and every emulation takes that longjmp.
#     Then pefile (an independent reader) checks both exes: GUARD_CF clear,
#     the PE checksum right, and PyInstaller's appended archive still readable.
#  4. The frozen self-test, run by both exes: digiemu-console.exe, and the
#     windowed digiemu.exe with its output sent to files. It includes an
#     'imports' check of every module the app imports inside functions.
#     Exit -1073740791 (0xC0000409) during 'unicorn_compat' means the exe
#     still runs under CFG.
#  5. packaging\bundle_guard.py audits dist (no firmware, no private modules
#     and every required module in the exe archives, the pinned DLL, no CFG)
#     and writes the zip from the list it has just audited. Only this step may
#     zip dist.
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$BuildVenv,
  [string]$SitePackagesFrom,
  [Parameter(Mandatory = $true)][string]$Out,
  [string]$Version = ''
)

# Native tools write progress to stderr. Exit codes, not PowerShell's error
# stream, decide whether a step failed.
$ErrorActionPreference = 'Continue'

function Fail([string]$msg) {
  Write-Host "build-windows: FAILED: $msg"
  exit 1
}
function Step([string]$msg) { Write-Host ''; Write-Host "== $msg" }
function Sha256([string]$path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower() }

# Run a native command and return its output lines (stdout and stderr merged,
# as strings). $script:rc holds its exit code.
function Run([string]$exe, [string[]]$argv) {
  $lines = & $exe @argv 2>&1 | ForEach-Object { "$_" }
  $script:rc = $LASTEXITCODE
  return $lines
}

# robocopy: exit codes 0-7 are success, 8 and up are failures.
function Mirror([string]$src, [string]$dst, [string[]]$extra) {
  & robocopy $src $dst /NJH /NJS /NFL /NDL /NP @extra | Out-Null
  if ($LASTEXITCODE -ge 8) { Fail "robocopy $src -> $dst exited $LASTEXITCODE" }
}

function One([string]$dir, [string]$pattern) {
  $hits = @(Get-ChildItem -LiteralPath $dir -Filter $pattern -ErrorAction SilentlyContinue)
  if ($hits.Count -ne 1) { Fail "expected exactly one $pattern in $dir, found $($hits.Count)" }
  return $hits[0]
}

$Repo = Split-Path -Parent $PSScriptRoot
if (-not $Version) {
  $m = Select-String -LiteralPath (Join-Path $Repo 'emu\portable.py') -Pattern "^APP_VERSION = '([^']*)'" | Select-Object -First 1
  if (-not $m) { Fail 'no APP_VERSION in emu\portable.py; pass -Version' }
  $Version = $m.Matches[0].Groups[1].Value
}
if ($Version -notmatch '^\d{1,5}\.\d{1,5}\.\d{1,5}$') { Fail "-Version must be x.y.z, got '$Version'" }

$DevVenv = Join-Path $Repo '.venv'
$DevSP = Join-Path $DevVenv 'Lib\site-packages'
$RefDll = Join-Path $DevSP 'unicorn\lib\unicorn.dll'
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Out = (Resolve-Path -LiteralPath $Out).Path
$BuildVenv = [IO.Path]::GetFullPath($BuildVenv)
$Py = Join-Path $BuildVenv 'Scripts\python.exe'
$BvSP = Join-Path $BuildVenv 'Lib\site-packages'
$Dist = Join-Path $Out 'dist\digiemu'
$Zip = Join-Path $Out "digiemu-win64-$Version.zip"

if (($BuildVenv + '\').StartsWith($Repo + '\', [StringComparison]::OrdinalIgnoreCase)) {
  Fail "-BuildVenv must be outside the repo ($BuildVenv)"
}
if (($Out + '\').StartsWith($Repo + '\', [StringComparison]::OrdinalIgnoreCase)) {
  Fail "-Out must be outside the repo ($Out): dist\ would sit next to sections\ and snapshots\"
}
if (-not (Test-Path -LiteralPath $RefDll)) { Fail "no patched unicorn.dll at $RefDll (run tools\install-patched-unicorn.ps1)" }
$UcSha = Sha256 $RefDll
Write-Host "repo        $Repo"
Write-Host "reference   $RefDll"
Write-Host "            sha256 $UcSha"

# -- 1. build venv ------------------------------------------------------------
Step 'build venv'
if (-not (Test-Path -LiteralPath $Py)) {
  $cfg = Join-Path $DevVenv 'pyvenv.cfg'
  $homeLine = Get-Content -LiteralPath $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
  if (-not $homeLine) { Fail "no 'home' in $cfg" }
  $baseHome = ($homeLine -replace '^\s*home\s*=\s*', '').Trim()
  $basePy = Join-Path $baseHome 'python.exe'
  if (-not (Test-Path -LiteralPath $basePy)) { Fail "base interpreter $basePy (from $cfg) is missing" }
  if (-not $SitePackagesFrom) { Fail "-SitePackagesFrom is needed to create $BuildVenv" }
  if (-not (Test-Path -LiteralPath (Join-Path $SitePackagesFrom 'PyInstaller'))) {
    Fail "no PyInstaller in $SitePackagesFrom"
  }
  Write-Host "creating $BuildVenv from $basePy"
  $null = Run $basePy @('-m', 'venv', $BuildVenv)
  if ($script:rc -ne 0 -or -not (Test-Path -LiteralPath $Py)) { Fail "python -m venv exited $script:rc" }
  $dirs = @('PyInstaller', 'pyinstaller-*.dist-info', '_pyinstaller_hooks_contrib',
            'pyinstaller_hooks_contrib-*.dist-info', 'altgraph', 'altgraph-*.dist-info',
            'ordlookup', 'pefile-*.dist-info', 'win32ctypes', 'pywin32_ctypes-*.dist-info',
            'packaging', 'packaging-*.dist-info')
  foreach ($pat in $dirs) {
    $d = One $SitePackagesFrom $pat
    Mirror $d.FullName (Join-Path $BvSP $d.Name) @('/E', '/XD', '__pycache__')
  }
  foreach ($f in @('pefile.py', 'peutils.py')) {
    Copy-Item -LiteralPath (Join-Path $SitePackagesFrom $f) -Destination $BvSP
  }
}
# unicorn and capstone: mirrored from the dev venv every time.
Mirror (Join-Path $DevSP 'unicorn') (Join-Path $BvSP 'unicorn') @('/MIR', '/XD', '__pycache__', 'include', '/XF', 'unicorn.lib', '*.5patch', '*.6patch', '*.pre-fractional')
Mirror (Join-Path $DevSP 'capstone') (Join-Path $BvSP 'capstone') @('/MIR', '/XD', '__pycache__', 'include')
# unicorn\lib holds exactly unicorn.dll, whatever backups the dev venv grows.
Get-ChildItem -LiteralPath (Join-Path $BvSP 'unicorn\lib') | Where-Object { $_.Name -ne 'unicorn.dll' } |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
foreach ($pkg in @('unicorn', 'capstone')) {
  $d = One $DevSP "$pkg-*.dist-info"
  Get-ChildItem -LiteralPath $BvSP -Filter "$pkg-*.dist-info" | Where-Object { $_.Name -ne $d.Name } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
  Mirror $d.FullName (Join-Path $BvSP $d.Name) @('/MIR')
}

# -- 2. verify ------------------------------------------------------------------
Step 'verify build venv'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_NO_INDEX = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
foreach ($v in @('TCL_LIBRARY', 'TK_LIBRARY', 'LIBUNICORN_PATH', 'LIBCAPSTONE_PATH', 'PYTHONPATH', 'PYTHONHOME', 'DIGIEMU_UC_DLL')) {
  Remove-Item -Path "Env:$v" -ErrorAction SilentlyContinue
}
$lines = Run $Py @('-m', 'pip', 'check')
$lines | ForEach-Object { Write-Host "  $_" }
if ($script:rc -ne 0) { Fail 'pip check' }
$piv = (Run $Py @('-m', 'PyInstaller', '--version')) -join ''
if ($script:rc -ne 0) { Fail 'PyInstaller does not run in the build venv' }
Write-Host "  PyInstaller $piv"
$ep = (Run $Py @('-c', "import importlib.metadata as m; print(sorted(e.value for e in m.entry_points(group='pyinstaller40')))")) -join ''
Write-Host "  pyinstaller40 entry points: $ep"
if ($ep -notmatch '_pyinstaller_hooks_contrib') { Fail 'hooks-contrib is not registered' }
$bvSha = Sha256 (Join-Path $BvSP 'unicorn\lib\unicorn.dll')
if ($bvSha -ne $UcSha) { Fail "build venv unicorn.dll sha256 $bvSha != reference $UcSha" }
Write-Host "  unicorn.dll matches the reference"
Push-Location -LiteralPath $Repo
try {
  $lines = Run $Py @('-m', 'emu.unicorn_compat')
  if ($script:rc -ne 0) { $lines | ForEach-Object { Write-Host "  $_" }; Fail 'emu.unicorn_compat in the build venv' }
  Write-Host '  emu.unicorn_compat: compatible'
} finally { Pop-Location }
# Compile the app code on this interpreter without writing .pyc files into the tree.
$check = Join-Path $Out 'compile-check.py'
$src = @'
import os, sys
root, bad, n = sys.argv[1], [], 0
paths = [os.path.join(root, 'tools', f) for f in ('introboot.py', 'uisettle.py', 'ekfsadd.py')]
for pkg in ('emu', 'dt2', 'packaging'):
    for here, _dirs, files in os.walk(os.path.join(root, pkg)):
        paths += [os.path.join(here, f) for f in files if f.endswith('.py')]
for p in paths:
    if os.path.isfile(p):
        n += 1
        try:
            with open(p, 'rb') as fh:
                compile(fh.read(), p, 'exec')
        except SyntaxError as exc:
            bad.append('%s: %s' % (p, exc))
print('compiled %d files on Python %s' % (n, sys.version.split()[0]))
for b in bad:
    print('SYNTAX ERROR ' + b)
sys.exit(1 if bad else 0)
'@
[IO.File]::WriteAllText($check, $src.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
$lines = Run $Py @($check, $Repo)
$lines | ForEach-Object { Write-Host "  $_" }
if ($script:rc -ne 0) { Fail 'app code does not compile on the build interpreter' }

# -- 3. PyInstaller -------------------------------------------------------------
Step "PyInstaller (version $Version)"
$env:DIGIEMU_VERSION = $Version
$env:DIGIEMU_UC_SHA256 = $UcSha
$log = Join-Path $Out 'pyinstaller.log'
Push-Location -LiteralPath $Out
try {
  $lines = Run $Py @('-m', 'PyInstaller', '--noconfirm', '--clean',
                     '--distpath', (Join-Path $Out 'dist'), '--workpath', (Join-Path $Out 'work'),
                     (Join-Path $Repo 'packaging\digiemu.spec'))
} finally { Pop-Location }
[IO.File]::WriteAllLines($log, [string[]]@($lines), (New-Object Text.UTF8Encoding $false))
$lines | Where-Object { $_ -match 'WARNING|ERROR|refusing|would be bundled|lacks modules|GUARD_CF|COLLECT did not|Build complete' } | ForEach-Object { Write-Host "  $_" }
if ($script:rc -ne 0) { Fail "PyInstaller exited $script:rc (log: $log)" }
if ($lines -match 'tkinter installation is broken') { Fail "PyInstaller dropped tkinter (log: $log)" }
if (-not (Test-Path -LiteralPath (Join-Path $Dist 'digiemu-console.exe'))) { Fail "no $Dist\digiemu-console.exe" }
Write-Host "  log: $log"
Write-Host "  missing-module report: $(Join-Path $Out 'work\digiemu\warn-digiemu.txt')"

# Control Flow Guard, checked with pefile rather than the spec's own reader.
Step 'Control Flow Guard (pefile)'
$pecheck = Join-Path $Out 'pe-check.py'
$src = @'
import os, sys
import pefile
from PyInstaller.archive.readers import pkg_archive_contents
GUARD_CF = 0x4000
bad = 0
for name in ('digiemu.exe', 'digiemu-console.exe'):
    path = os.path.join(sys.argv[1], name)
    with pefile.PE(path, fast_load=True) as pe:
        dc = pe.OPTIONAL_HEADER.DllCharacteristics
        cs = pe.OPTIONAL_HEADER.CheckSum
        want = pe.generate_checksum()
    entries = len(pkg_archive_contents(path, recursive=True))
    ok = not dc & GUARD_CF and cs == want and entries > 0
    bad += not ok
    print('%s: DllCharacteristics 0x%04x (GUARD_CF %s), PE checksum 0x%08x (%s), '
          'archive %d entries, %d bytes' % (
              name, dc, 'SET' if dc & GUARD_CF else 'clear', cs,
              'ok' if cs == want else 'WRONG, the file sums to 0x%08x' % want,
              entries, os.path.getsize(path)))
with pefile.PE(sys.executable, fast_load=True) as pe:
    print('for comparison %s: DllCharacteristics 0x%04x'
          % (os.path.basename(sys.executable), pe.OPTIONAL_HEADER.DllCharacteristics))
sys.exit(1 if bad else 0)
'@
[IO.File]::WriteAllText($pecheck, $src.Replace("`r`n", "`n"), (New-Object Text.UTF8Encoding $false))
$lines = Run $Py @($pecheck, $Dist)
$lines | ForEach-Object { Write-Host "  $_" }
if ($script:rc -ne 0) { Fail 'an exe still has Control Flow Guard, a wrong PE checksum or an unreadable archive' }

# -- 4. frozen self-test ----------------------------------------------------------
Step 'self-test (frozen)'
$st = Join-Path $Out 'selftest-console.json'
$lines = Run (Join-Path $Dist 'digiemu-console.exe') @('--selftest', '--json', $st)
if ($script:rc -ne 0) { $lines | ForEach-Object { Write-Host "  $_" }; Fail "digiemu-console.exe --selftest exited $script:rc (report: $st)" }
(Get-Content -LiteralPath $st -Raw -Encoding UTF8 | ConvertFrom-Json).checks |
  ForEach-Object { Write-Host ("    {0,-15} {1}" -f $_.name, $(if ($_.ok) { 'ok' } else { 'FAILED' })) }
Write-Host "  digiemu-console.exe: ok ($st)"
# The windowed exe: PowerShell does not wait for a GUI-subsystem program, so
# Start-Process -Wait. Its output goes to files, which also keeps it from
# creating logs\ inside dist.
$stw = Join-Path $Out 'selftest-windowed.json'
$p = Start-Process -FilePath (Join-Path $Dist 'digiemu.exe') -Wait -PassThru `
  -ArgumentList @('--selftest', '--json', ('"' + $stw + '"')) `
  -RedirectStandardOutput (Join-Path $Out 'selftest-windowed.out') -RedirectStandardError (Join-Path $Out 'selftest-windowed.err')
if ($p.ExitCode -ne 0) { Fail "digiemu.exe --selftest exited $($p.ExitCode) (report: $stw)" }
Write-Host "  digiemu.exe: ok ($stw)"

# -- 5. audit and zip ---------------------------------------------------------------
Step 'audit and zip'
$lines = Run $Py @((Join-Path $Repo 'packaging\bundle_guard.py'), $Dist, '--devices', (Join-Path $Repo 'devices'),
                   '--unicorn-sha256', $UcSha, '--require-pyz', '--zip', $Zip)
$lines | ForEach-Object { Write-Host "  $_" }
if ($script:rc -ne 0) { Fail 'bundle audit' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [IO.Compression.ZipFile]::OpenRead($Zip)
try { $count = $z.Entries.Count } finally { $z.Dispose() }
$size = (Get-Item -LiteralPath $Zip).Length
$unpacked = (Get-ChildItem -LiteralPath $Dist -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ''
Write-Host ("built {0}" -f $Zip)
Write-Host ("  {0:N0} bytes zipped, {1:N0} bytes unpacked, {2} files" -f $size, $unpacked, $count)
Write-Host ("  sha256 {0}" -f (Sha256 $Zip))
exit 0
