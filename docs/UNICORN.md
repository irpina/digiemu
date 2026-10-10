# Patched Unicorn requirement

The emulator requires `unicorn==2.1.4` with the patches in `patches/`: three
correctness fixes it refuses to run without (the two below and the EMAC
modes fix), three speed patches it uses when present (fast memory, the
digikit accelerators and speed options), and a fix for aarch64 hosts, where
stock Unicorn emits an invalid instruction for a constant subtraction and
dies with SIGILL. `patches/README.md` describes all seven.

The m68k CCR patch, `unicorn-2.1.4-m68k-hook-ccr-sync.patch`: stock Unicorn
both mutates lazy condition-code state when the host reads SR and leaves lazy
CCR uncommitted when its count/code hook stops at the instruction after a flag
producer. Either defect can change the following guest branch.

The EMAC patch, `unicorn-2.1.4-m68k-emac-mac-load.patch`: stock Unicorn
decodes ColdFire MAC and MSAC with load wrongly. It faults on most Ry
registers, reads a data-register Rx from D2, adds where MSAC subtracts, and
applies MASK to every load address. The Digitakt II SHARC frame build reaches
one of these instructions at `0x400db9e0`. See `patches/README.md`.

Install it into the project interpreter after `uv sync`:

```sh
tools/install-patched-unicorn.sh
```

On Windows, with Visual Studio 2022's C++ build tools installed and a venv
holding `unicorn==2.1.4`:

```powershell
powershell -ExecutionPolicy Bypass -File tools\install-patched-unicorn.ps1
```

The installer makes a temporary clone of official Unicorn tag 2.1.4, verifies
commit `8028ec436f2d9376525352dd38ed9ed6b9f6be10` and the checked-in patches,
builds only m68k in Release mode, atomically replaces that interpreter's native
library, then runs the semantic check. It does not vendor Unicorn source or use
a fork. Set `PYTHON=/path/to/python` to target another already-installed project
interpreter.

`python -m emu.unicorn_compat` runs the compatibility check directly. It covers
zero/nonzero results when SR is read from the actual code hook after a flag
producer, plus an equal `CMP` stopped by the count hook at the following
instruction, and the MAC with load at Digitakt II `0x400db9e0`. The translator
patch calls `update_cc_op(dc)` before `gen_uc_tracecode(...)`, committing the preceding instruction's lazy CCR before
a host callback can terminate emulation. The check is semantic rather than a
binary hash allowlist. `Machine` and `emu.run
--check` refuse an incompatible runtime before normal emulation begins. Running
`uv sync` can restore the stock wheel, in which case the guard intentionally
rejects it; rerun the installer.

This establishes that Digitakt renders and that Digitone's Main OS executes and
renders. It is not a claim of a full hardware-equivalent Digitone boot.

`tests/test_unicorn_real.py` is opt-in (`DT2_UNICORN_REAL=1`). Supply
`DT2_UNICORN_REAL_DT_MAIN`, `..._DT_SNAPSHOT`, `..._DT_INSTRS` and matching
`DN` variables for lawfully obtained inputs. It checks the semantic guard and
full panel PNG hashes without retaining inputs or outputs.

Timer-stepped execution has no arbitrary `cap` boundary. Only actual timer
deadlines may subdivide a timer-stepped run; arbitrary subdivisions are
unsupported because they change firmware-visible execution boundaries.
