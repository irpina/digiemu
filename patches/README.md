# Patches

Seven diffs against [Unicorn Engine](https://github.com/unicorn-engine/unicorn)
2.1.4, tag commit `8028ec436f2d9376525352dd38ed9ed6b9f6be10`, applied in this
order, each against the tree with the ones before it applied:

- `unicorn-2.1.4-m68k-hook-ccr-sync.patch` touches
  `qemu/target/m68k/translate.c` and `qemu/target/m68k/unicorn.c`.
- `unicorn-2.1.4-m68k-emac-mac-load.patch` touches
  `qemu/target/m68k/translate.c`.
- `unicorn-2.1.4-m68k-emac-modes.patch` touches
  `qemu/target/m68k/helper.c` and `qemu/target/m68k/translate.c`.
- `unicorn-2.1.4-m68k-fast-mem.patch` touches `qemu/accel/tcg/cputlb.c`,
  `qemu/accel/tcg/translate-all.c` and `.h`, `qemu/include/tcg/tcg.h`,
  `include/uc_priv.h` and `uc.c`.
- `unicorn-2.1.4-m68k-digikit-accel.patch` touches
  `qemu/target/m68k/cpu.c` and appends to `uc.c`.
- `unicorn-2.1.4-m68k-digikit-speed.patch` touches `include/uc_priv.h`,
  `qemu/accel/tcg/cpu-exec.c` and `cputlb.c`, `qemu/target/m68k/helper.c`,
  `helper.h` and `translate.c`, `qemu/tcg/tcg-op.c`, and appends to `uc.c`.
- `unicorn-2.1.4-tcg-aarch64-addsub2.patch` touches
  `qemu/tcg/aarch64/tcg-target.inc.c`.

The first three are correctness fixes, and the emulator refuses to run
without them. The next three are speed: they are what make live audio on the
mk1 run faster than real time. Without fast-mem and accel the emulator takes
the same code paths in Python, with byte-identical audio but about nine times
slower: two emulated seconds of 48 kHz audio from the loaded-sample snapshot
took 11.3 s of wall time instead of 1.26 s (WSL, 2026-09-23). The speed patch
is optional in the same way: without it the audio and CPU state are the same
byte for byte, at about two thirds of the speed. The seventh is a correctness
fix for aarch64 hosts (Apple silicon, Linux on Arm) and changes nothing on
others.

Everything except the `uc.c` additions is QEMU source vendored inside
Unicorn. **The patches are
derivative works of that code and carry its licence, not this repository's
choice of one** — take their terms from Unicorn and QEMU upstream. This is why
the repository as a whole is GPL-2.0-or-later rather than something
permissive; see the licence section of the top-level [README](../README.md).

## What the CCR patch fixes

Unicorn's m68k translator keeps condition codes lazily and commits them when a
translation block ends normally. A code hook, or a `count=` stop, can return to
the host *mid-block*, before that commit — so a guest `CMP` followed by a
hook-visible `SR` read, or by a counted stop on the next instruction, exposes
stale flags. Guest branches then take the wrong arm.

The patch commits the pending condition codes before handing control back.

## What the EMAC patch fixes

`DISAS_INSN(mac)` handles MAC and MSAC with load (ColdFire Programmer's
Reference Manual, p.6-3 to 6-5 and p.6-22 to 6-23) wrongly in four ways.
QEMU master had the same code on 2026-09-15.

- It takes extension-word bits 1..0 as a dual-accumulate request and, on a
  core without EMAC_B such as the CFV4E, raises an illegal-instruction
  exception. In the load forms those bits are part of Ry, so every Ry whose
  register number has bit 0 or 1 set faulted. The patch honours the request
  only on EMAC_B cores.
- It reads a data-register Rx from operation-word bits 14..12, which are
  always 2 for these opcodes, so Rx was always D2. Rx is extension-word bits
  15..12.
- It reads the MSAC bit from operation-word bit 8, which is always 0 for
  these opcodes, so MSAC added. The bit is extension-word bit 8. This also
  affects MSAC without load.
- It ANDs MASK into every load address. Extension-word bit 5 says whether
  MASK is used.

The Digitakt II SHARC frame build reaches one of these instructions at
`0x400db9e0`.

## What the EMAC modes patch fixes

The EMAC's arithmetic outside plain signed-integer-with-positive-operands,
checked against the MCF5441x reference manual (5.2.1 MACSR, Table 5-3, the
5.3.5 pseudo-code). Digitakt mk1's audio engine computes at MACSR `0xA0`
(signed fractional, saturating) and mixes at `0x80` (signed integer,
saturating); with stock Unicorn it rendered silence, and with only the
fractional half fixed it rendered the negative half-wave only.

- Fractional products were unsigned and missing the `<< 1` that aligns the
  binary point, so 0.970 x 0.5 gave 0.2425 and every gain collapsed toward
  zero. Now signed, rounded (R/T) or truncated to 40 bits, `-1 x -1`
  zero-filled.
- The fractional store shifted the accumulator logically: with OMC every
  negative value was stored as 0. Now arithmetic, with 16-bit rounding for
  S/U and saturation that follows the sign.
- MACSR[S/U] was backwards in integer mode: clear is signed, set is unsigned
  (Table 5-3). Word operands, products, saturation, stores, loads and
  extension words all chose by it.
- The signed product was computed unsigned; signed accumulation overflow and
  the signed store saturated in the wrong direction; a signed `>>1` scale
  shifted logically.
- Changing MACSR[F/I,S/U] repacked the accumulators by the old mode, so a
  mode change converted nothing.
- EV used bit 40/32 boundaries; the manual's are ACC[47:39] and ACC[47:31].

Still not modelled, as before: the product/accumulation-overflow skip that
freezes a saturated accumulator until PAVn is cleared.

## What the fast-memory patch changes

Stock Unicorn sends every guest load and store through the slow C helper as
soon as any memory hook exists, and the emulator always has one (the global
`UC_HOOK_MEM_INVALID` demand mapper). On top of that the dirty-page bitmap is
a stub, so every store also ran the self-modifying-code check. Together that
made memory access the largest single cost of the mk1 audio render.

- Only pages a read or write hook covers take the slow path. They are marked
  per page in the TLB (`TLB_WATCHPOINT`, which is otherwise a no-op in
  Unicorn); every other page keeps QEMU's inline fast path.
- A page is cleaned for fast stores when it holds no translated code, and
  re-armed when code is translated on it (`tlb_protect_code`), so
  self-modifying code is still caught.
- Adding or deleting a memory hook refills the TLB. From inside a hook that
  is deferred to the next top-level `uc_emu_start`, because refilling mid-access
  frees the entry in use.

`tests/test_unicorn_fastmem.py` checks hook routing (including hooks added
after a run, deleted hooks, global hooks and a global invalid-memory hook) and
self-modifying code across and between translation blocks.

## What the digikit accelerator patch adds

- The CFV4E gets `M68K_FEATURE_CF_ISA_APLUSC`. The MCF5441x is an ISA_C core
  (MCF54418RM 3.3.2, Table 3-4), so FF1, BITREV and BYTEREV are real
  instructions; stock Unicorn raised illegal-instruction for them and the
  emulator emulated FF1 from a hook. `emu/harness.py` skips that hook when the
  library has FF1 natively.
- `uc_digikit_add_budget_hook`: ends `emu_start` after N translation blocks
  without a Python call per block (the fast stepper in `emu/longrun.py`).
- `uc_digikit_edma_install`: the software-started eDMA channels the mk1 audio
  render starts and polls, done natively. `emu/edma_sw.py` is the Python
  original and states the rules; `emu/native.py` holds the ctypes mirrors and
  checks their size against `uc_digikit_edma_size()`.

`emu/native.py` probes for both exports and the emulator falls back to the
Python paths when they are missing.

## What the digikit speed patch changes

A profile of the live-audio render (a sampling profiler inside the library,
pattern playing) put 12% of the time in an exit-request check after every
guest load and store, about a quarter in the three helper calls each MAC
compiled to, and a few percent in re-translating a block to find the exact PC
for every hooked memory access.

- Every non-dual MAC and MSAC, with or without load, is one helper call
  (`mac_fused`) instead of a multiply, a saturate and a flags helper, each of
  which synced the TCG globals. The mode helpers' bodies are `static inline`
  so the fused one inlines them; the exported helpers are one-line wrappers.
  Always on.
- `uc_digikit_set_options(uc, bits)` turns on three engine options, all off
  by default (`uc_digikit_options_supported()` returns the bits the library
  knows; an unknown bit is `UC_ERR_ARG`):
  - `NATIVE_RTE` (1): `rte` runs QEMU's own ColdFire return instead of
    surfacing as `UC_HOOK_INTR` 0x100 for the host to pop the frame. Other
    exceptions still go to the hook. `emu/harness.py` turns it on after
    installing its exception hook.
  - `NO_MEM_EXIT` (2): no exit-request check after each load and store.
    Those checks exist so a memory hook that calls `emu_stop` stops at that
    access; with the option on, emulation stops at the next translation
    block instead. Changing it flushes the translation cache. On for every
    `Machine`: none of the emulator's memory hooks stops emulation.
  - `NO_HOOK_PC_SYNC` (4): read and write hooks see the PC as last synced,
    not the PC of the accessing instruction. Only for callers whose memory
    hooks never read the PC; the live-audio GUI turns it on unless
    `DIGIKIT_PIT3_PROBE=1`.

`tests/test_unicorn_speed.py` checks each option against the behaviour
without it, and the EMAC tests run through `mac_fused`. Over four emulated
seconds of a playing pattern, audio and CPU state were byte-identical to the
five-patch library at 2.76x to 2.83x real time instead of 1.82x to 1.97x
(WSL, 2026-09-23).

## What the aarch64 patch fixes

On an aarch64 host, Unicorn's code generator emits an invalid instruction for
a `sub2` whose low operand is a nonzero constant, and the process dies with
SIGILL the first time that block runs. `tcg_out_addsub2` picks SUBS for a
subtraction of a positive constant and also negates the constant, so the
negative immediate spills into the encoding. QEMU fixed it in 707b45a247
("tcg/aarch64: Fix constant subtraction in tcg_out_addsub2", QEMU 6.0), which
Unicorn's QEMU predates; the patch is that hunk. Unicorn issue
[#2430](https://github.com/unicorn-engine/unicorn/issues/2430).

An m68k `subx` reaches it when its source register and the X flag are both
constants in the block, as in gcc's code for `((uint64_t)1 << n) - 1`:

    suba.l %a1,%a1; move.l %a1,%d5; clr.l %d6; subq.l #1,%d5; subx.l %d6,%d4

A code hook on the block, or a `count=` step, stops the constants from
reaching the `sub2`, so the crash comes and goes with how the block is run.
The code is legal ColdFire, and x86_64 hosts run it correctly.

## Checks

`emu/unicorn_compat.py` exercises the CCR shapes, the `0x400db9e0`
instruction and fractional and signed-integer MACs, and refuses to run on an
interpreter whose Unicorn lacks any of the patches, rather than letting a
subtly wrong emulation pass for a working one. `tests/test_unicorn_emac.py`
checks the MAC and MSAC load forms, fractional and integer modes and mode
switches against the manual. The speed options are not required, so the
compat check does not test them; `tests/test_unicorn_speed.py` does. Nor
does it test the aarch64 fix: without it the process dies rather than
emulating wrongly, so a probe would have to run in a child process at every
start. `tests/test_unicorn_aarch64.py` runs the `subx` case in one.

## Applying them

Do not apply these by hand. `tools/install-patched-unicorn.sh` (or its
Windows twin, `tools/install-patched-unicorn.ps1`) pins the upstream
commit, verifies each patch's SHA-256, builds only the m68k target,
clones from a local repository instead of GitHub when given one
(`UNICORN_GIT=PATH` / `-Source PATH`),
replaces the dynamic library the Python bindings actually load, and then runs
the compat check. `uv sync` can restore the stock wheel, which puts the
emulator back to refusing to start until the installer is rerun.
