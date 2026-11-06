# Windows twin of install-patched-unicorn.sh: build the m68k SR-read, code-hook
# CCR-sync, EMAC MAC-with-load and EMAC fractional/signed-integer mode fixes,
# the fast-memory path, the digikit accelerators and speed options, and the
# aarch64 host's constant-subtraction fix (see patches/README.md) from
# official Unicorn 2.1.4 with the MSVC toolchain, and
# drop unicorn.dll into the project venv.
#
#     powershell -ExecutionPolicy Bypass -File tools\install-patched-unicorn.ps1 [-DryRun] [-Source PATH]
#
# -Source clones from a local Unicorn repository instead of GitHub (it must
# hold tag 2.1.4 at the pinned commit), so a rebuild needs no download.
#
# Needs git, Visual Studio 2022 with the C++ build tools (its bundled CMake is
# used when none is on PATH), and a venv with unicorn==2.1.4 (pip restores the
# stock wheel, which emu.unicorn_compat rejects).
[CmdletBinding()]
param([switch]$DryRun, [string]$Source = 'https://github.com/unicorn-engine/unicorn.git')

# Native tools write warnings to stderr; explicit exit-code checks below, not
# PowerShell's error stream, decide whether a step failed.
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $PSScriptRoot
$commit = '8028ec436f2d9376525352dd38ed9ed6b9f6be10'
# Applied in this order, each pinned by SHA-256.
$patches = @(
  @{ Path = Join-Path $root 'patches\unicorn-2.1.4-m68k-hook-ccr-sync.patch'
     Sha  = '56de71acf2adbd5ca2f448095478e65e49fd79d378aeb5b5e4217d2c90f52f4e' },
  @{ Path = Join-Path $root 'patches\unicorn-2.1.4-m68k-emac-mac-load.patch'
     Sha  = 'ac128dd6836997de55e0d2ad70d7a2978639168090f552c5634da50fddc70bfe' },
  @{ Path = Join-Path $root 'patches\unicorn-2.1.4-m68k-emac-modes.patch'
     Sha  = '5fc44429ea913c5256c34a6e9c93ab90f727f120b4f12922a8f35a952fd1dace' },
  @{ Path = Join-Path $root 'patches\unicorn-2.1.4-m68k-fast-mem.patch'
     Sha  = '7f79332f318352cbd8055386dbf5149efc801d0663ae2450e33f9f39fed53374' },
  @{ Path = Join-Path $root 'patches\unicorn-2.1.4-m68k-digikit-accel.patch'
     Sha  = '7048704a068cb074b93b751df4b1a73fd8972c1c0cc27e30774c4c2771ec2b5c' },
  @{ Path = Join-Path $root 'patches\unicorn-2.1.4-m68k-digikit-speed.patch'
     Sha  = '9560613223502656e82adc1ecc36d5d8486995bed13ea8ddad168f8c4558a799' },
  @{ Path = Join-Path $root 'patches\unicorn-2.1.4-tcg-aarch64-addsub2.patch'
     Sha  = '42e7ebca72efd671f6ed1e012bd5284d8bf0c99a6308c82ddef21e0951aee96e' }
)
$python = if ($env:PYTHON) { $env:PYTHON } else { Join-Path $root '.venv\Scripts\python.exe' }

function Fail($msg) { [Console]::Error.WriteLine("error: $msg"); exit 2 }

if (-not (Test-Path $python)) { Fail "project Python not found: $python (set PYTHON=...)" }
$ver = & $python -c 'import unicorn; print(unicorn.__version__)'
if ($ver -ne '2.1.4') { Fail 'interpreter must have unicorn==2.1.4; pip install unicorn==2.1.4 first' }
foreach ($p in $patches) {
  if (-not (Test-Path $p.Path)) { Fail "patch missing: $($p.Path)" }
  $h = (Get-FileHash $p.Path -Algorithm SHA256).Hash.ToLower()
  if ($h -ne $p.Sha) { Fail "unexpected patch SHA-256: $($p.Path)" }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail 'git is required' }

# vswhere locates the VS instance that has the C++ tools; CMake comes from
# PATH first, then the copy Visual Studio ships.
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vsroot = $null
if (Test-Path $vswhere) {
  $vsroot = & $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
}
if (-not $vsroot) { Fail 'Visual Studio 2022 with the C++ build tools is required' }
$cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
if (-not $cmake) {
  $c = Join-Path $vsroot 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
  if (Test-Path $c) { $cmake = $c }
}
if (-not $cmake) { Fail 'cmake is required (PATH, or the copy bundled with the VS C++ tools)' }

# The exact dynamic-library name the binding loads on win32; see
# unicorn_py3/unicorn.py. Do not glob: the wheel also contains unicorn.lib.
# Single quotes inside: PowerShell strips inner double quotes from arguments
# it hands to a native executable.
$target = & $python -c "import os, unicorn; print(os.path.join(os.path.dirname(unicorn.__file__), 'lib', 'unicorn.dll'))"
if (-not $target -or -not (Test-Path $target)) { Fail "unsupported Unicorn native-library layout: expected $target" }

if ($DryRun) {
  "dry-run: target=$target"
  "dry-run: cmake=$cmake"
  "dry-run: vs=$vsroot"
  "dry-run: source=$Source"
  exit 0
}

$work = Join-Path $env:TEMP ('digitakt2-unicorn.' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $work | Out-Null
try {
  git clone --quiet --branch 2.1.4 --depth 1 $Source "$work\src"
  if ($LASTEXITCODE -ne 0) { Fail 'git clone failed' }
  $head = (git -C "$work\src" rev-parse HEAD).Trim()
  if ($head -ne $commit) { Fail "tag 2.1.4 did not resolve to expected commit (got $head)" }
  foreach ($p in $patches) {
    git -C "$work\src" apply --check $p.Path
    if ($LASTEXITCODE -ne 0) { Fail "patch does not apply cleanly: $($p.Path)" }
    git -C "$work\src" apply $p.Path
    if ($LASTEXITCODE -ne 0) { Fail "patch failed: $($p.Path)" }
  }
  & $cmake -S "$work\src" -B "$work\build" -G 'Visual Studio 17 2022' -A x64 `
    -DCMAKE_BUILD_TYPE=Release -DUNICORN_ARCH=m68k -DUNICORN_BUILD_TESTS=OFF
  if ($LASTEXITCODE -ne 0) { Fail 'cmake configure failed' }
  & $cmake --build "$work\build" --config Release --target unicorn
  if ($LASTEXITCODE -ne 0) { Fail 'cmake build failed' }
  $built = Join-Path $work 'build\Release\unicorn.dll'
  if (-not (Test-Path $built)) { Fail "expected m68k dynamic-library payload not produced: $built" }

  # Replace through a temp file in the same directory, so an interrupted
  # install never leaves a partial DLL behind.
  $tmp = Join-Path (Split-Path -Parent $target) ('.unicorn.dll.' + [guid]::NewGuid().ToString('N').Substring(0, 6))
  Copy-Item $built $tmp
  Move-Item -Force $tmp $target

  "unicorn commit=$commit"
  foreach ($p in $patches) { "patch=$(Split-Path -Leaf $p.Path) sha256=$($p.Sha)" }
  "library=$target"
  "library_sha256=$((Get-FileHash $target -Algorithm SHA256).Hash.ToLower())"
  & $python -m emu.unicorn_compat
  exit $LASTEXITCODE
}
finally {
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
