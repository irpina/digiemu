# Digitakt II OS 1.15C — findings

Target SHA-256 `62d588456e47194bd56dfee9568fb9dd4521c4ff1e8b5427eb461355532e8c6c`
(matches the target of `lalzart/digitakt-ii-firmware-research-public`).

Evidence classes used below:
**[V]** verified by running it here · **[D]** documented by prior research, not
re-checked · **[O]** open / nobody has established this.

## Scope, and the 2.01 firmwares **[V]**

Everything address-specific in this file is **Digitakt II 1.15C**, whose MAIN
OS is `sections/section_3_MAIN_OS.bin`, sha-256 `6a6a887b…`. The snapshot
ladder and the Ghidra program are the same image.

`Digitakt_II_OS1.16.syx` and `Digitone_II_OS1.11.syx` are a different
generation, and three things separate them from 1.15C/1.10E:

- **A sixth section, id 8**, packed, **103,416 bytes compressed on both
  devices** — byte-for-byte the same compressed length on Digitakt and
  Digitone, which suggests a shared component rather than per-device content.
  Decompressed, both are the same 159,948 bytes. Nothing else is known about
  it. **[V][O]**
- **The bootstrap version bumps, `0x0200` -> `0x0201`.** Section 2's `dest` is
  the version word, and it reads `0x02000000` in 1.15C and 1.10E, `0x02010000`
  in 1.16 and 1.11. So installing either of the newer firmwares performs the
  bootstrap upgrade — the one irreversible operation on the device.
- **The Unicorn depacker cannot read them.** Every packed section of 1.16
  fails under `emu.extract --oracle`. The cause is the oracle, not the new
  section: the depacker is taken from the UPDATER, and 1.16's UPDATER differs
  from 1.15C's by **43.4%** (14,231 of 32,776 bytes, first difference at
  offset `0x9b`), so the entry point at `0x80000432` has moved. **[V]**
  `dt2/elz.py`, a byte-level decoder that `emu.extract` now uses by default,
  reads them. On 1.15C and 1.10E it matches the device routine byte for byte
  on every packed section; on 1.16 and 1.11 every stream ends exactly at its
  declared length. **[V]**

Retargeting the machine work to 1.16 is therefore not an address rebase. It
needs a fresh snapshot ladder built by cold boot, a re-import to Ghidra, and
every address in "The ColdFire machine dispatch" re-derived. **[O]** The
re-import is done; see "Digitakt II 1.16 in Ghidra" below. **[V]**

## Container

`.syx` → SysEx transport (13,346 × 128-byte messages, `F0 00 20 3C 14 00 …`)
→ 8-in-7 bit decode → 8-byte preamble (content checksum at +4) → ELE3
container, 1,347,728 bytes, five aPLib-compressed sections. Nothing encrypted. **[V]**

| id | name | decoded | dest / meaning | what it is |
|---|---|---|---|---|
| 5 | meta | 15 | — | build stamp `250910 15:18:30` |
| 2 | "DSP" | 30,302 | `0x0200` = **version**, loads `0x80000400` | **the bootstrap** — misnamed in the tool |
| 3 | MAIN OS | 3,177,312 | `0x40000400` | the C++ application |
| 4 | updater | 32,776 | `0x80000400` | stored raw, not compressed |
| 7 | blob | 320,780 | — | SHARC ADI loader records |

Section 2's `dest` is not a load address. All 101 absolute call targets in it
land in `0x8000____`; solving for the base that makes its pointer table hit real
string starts gives `0x80000400` with 96 hits. **[V]**

## Integrity and the upgrade path

Update images carry a per-packet transport checksum, a 32-bit content checksum
and an HMAC-SHA256 trailer, and the device checks them before it accepts an
image. This repository deliberately does not document how they are computed,
how an image is accepted, installed or recovered, or what in it could be
changed: it contains no tooling for building or installing firmware.

## Emulator behaviour found during the Digitakt II machine work

The subsections below came out of upstream's work on how Digitakt II
dispatches its machines. That work itself is not published here.

### Driving panel chords: modifiers must latch **[V]**

A chord is not a press followed by another press. `emu/panelin.py`'s `held`
argument is a mask of other buttons *in the same channel*, and the modifier
keys are not in the same channel as the page buttons, so `held` cannot express
a chord at all:

```
code = channel*8 + bit + 1
FUNC = 17 -> channel 2, bit 0      SRC  =  2 -> channel 0, bit 1
YES  = 10 -> channel 1, bit 1      NO   = 12 -> channel 1, bit 3
UP   = 11 -> channel 1, bit 2      DOWN = 14 -> channel 1, bit 5
```

The wire carries each channel's whole 8-button state as one byte, so a
cross-channel chord is expressed by asserting one channel and *leaving it
asserted* while another changes — never by a press/release pair:

```
buttons(m, profile, 2, 0x01)   ; FUNC down, and leave it
buttons(m, profile, 0, 0x02)   ; SRC down, FUNC still held
buttons(m, profile, 0, 0x00)   ; SRC up
buttons(m, profile, 2, 0x00)   ; FUNC up, last
```

The firmware's own records confirm the difference: a plain tap gives flag
`0x01` on press and `0x10` on release, while the same button inside a latched
chord gives `0x03` and `0x12`, and the modifier's own release reads `0x00`.
Press/release pairs produce two isolated taps that no chord handler will ever
see. **[V]**

The descriptor's two name pointers are **`std::string`, not `char*`** — the
pre-C++11 libstdc++ copy-on-write representation, with a 12-byte header
immediately *before* the character data: **[V]**

```
data-0xc  length
data-0x8  capacity
data-0x4  refcount
data+0    chars, NUL-terminated
```

Read back live, the header is exactly that — `SAMPLE` at `0x44f25c7c` has
length 6, capacity 6, refcount 0; `STRETCH` at `0x44f25cfc` has 7, 7, 0;
`MANUAL SLICE` at `0x44f25ddc` has 12, 12, 0. Three of `FUN_400caf48`'s six
callers copy the whole 44-byte descriptor by value, calling a constructor and
destructor per name field — which is why they are non-trivial members rather
than pointers. The copy is `FUN_401d3aba`: **[V]**

```
401d3aba  move.l (A1),D0             ; the stored data pointer
          tst.l  -4(D0)              ; refcount
          bmi    deep_clone          ; refcount < 0 -> _M_is_leaked(), clone
          cmp.l  #DAT_44f1e088,...   ; the empty-string singleton, by address
          beq    skip                ; never refcount the singleton
          addq.l #1,-4(D0)           ; otherwise share: refcount++
```

So a new descriptor **cannot** point bare at a rodata string: the bytes before
it are not a valid header, and `*(int*)(ptr-4)` would be whatever happens to
sit there — either corrupting neighbouring data with a refcount increment, or
taking the release path against a bogus header.

It does not need a runtime-constructed string either. Laying the full rep out
statically in the cave as `[u32 length][u32 capacity][i32 -1]["NAME\0"]` and
pointing the field at the chars makes `refcount < 0` true, so every copy
deep-clones into the heap, the static bytes are never mutated, and destructors
only ever run against the clones. This is the same mechanism libstdc++ uses
for a leaked rep. Not yet tried. **[O]**

`FUN_4005e022` separately gates auto-scrolling the list to the active row on
`param_1[0x73] + 1 < 8`; that is cosmetic — an unpatched eighth row would fail
to auto-scroll rather than crash. **[O]**

The list vectors are rebuilt every time `MachineSelectionView` is constructed,
not once at static init: hooking `FUN_400607b2`, `FUN_40051fbc` and
`FUN_4005224c` on a run resumed from `snapshots/boot400M.snap` shows all three
firing ~60M instructions in. So a patch to the rodata tables or the descriptor
array can be tested by poking guest memory after a resume — no cold boot
needed. `FUN_4005e022` does *not* fire on an idle post-intro run, so the rows
are built but never drawn without navigation. **[V]**

Still open: what the literal IDs (`0xca`-`0xfe`) mean, and how `machine_type`
reaches the six callers. **[O]**

### MACHINE SEL closes itself: the UI queue falls behind **[V][O][C]**

Found with `tools/guirun.py --trace-ui` (`emu/uitrace.py`), which prints UI
queue sends and pops with their wait, each view a key event is offered to,
and view activate/close. Runs start from `snapshots/boot400M.snap` with all
five machine patches; instruction counts start at 0 there.

- The UI main loop (`mainloop`, `0x40033492`) pops the UI queue at
  `0x4094ef3c`. The queue holds item pointers, not copies: `+0x04` count,
  `+0x10` mask, `+0x14` storage, `+0x18` write index, `+0x1c` read index.
  Records are 16 bytes: byte `+0` type, long `+4` code, long `+8` flags,
  long `+0xc` timestamp. Type 5 is the DTIM3 tick (fixed item
  `0x4022aea6`); type 0 is a key event. **[V]**
- Headless, the queue never drains after boot. Depth is 1 at 80M, 10 at
  100M, 20 at 140M, 30 at 160M and 45 at 200M. The loop pops 114-121 items
  per 20M while DTIM3 posts 120-126. The wait in the queue grows from 0.4M
  instructions at 80M to about 5M at 170M and 7.4M at 200M. This is the
  `mainloop` < `dtim3` drift in the progress lines. **[V]**
- Key event flags: `0x01` press, `0x02` chord (a modifier is held), `0x08`
  auto-repeat, `0x10` release. Seen: `0x03` and `0x12` for SRC in a FUNC
  chord, `0x0b` for its repeats, `0x09` for FUNC repeats. Edges are queued
  from return address `0x40110d7c`, repeats from `0x401108f8`. The first
  repeat comes 24 counts of the counter at `0x47dc5a6c` (advanced by
  `FUN_40110820`) after the press, about 1.9M instructions, then every 8
  counts. **[V]**
- MachineSelectionView's key handler `0x40060c2c` calls View::close
  (`0x4010daa2`, returning to `0x40060c72`) for any event with code 1-6,
  whether press, repeat or release. NO (12) closes on release; YES (10)
  commits. **[V]**
- Path of a key event: case 0 of the main loop calls `0x401072bc` at
  `0x40033518` (A2 = item). Later in the same loop pass, `FUN_4010ecf2`
  offers the event to each view in turn (`jsr (a0)` at `0x4010ed64`; D3 =
  view, D0 after the call = consumed). For FUNC+SRC, 8 views pass and
  MainScreenView consumes it; MachineSelectionView is activated
  (`FUN_4010dc8a`) inside that offer, about 83k instructions after the
  pop. **[V]**
- Chord at 170M (`--panel-dwell 3`, SRC released at once): the SRC press
  was sent at 170.47M and popped at 175.78M (waited 5.31M). The SRC
  release was sent at 172.34M, before MachineSelectionView was activated
  at 175.79M. The release was popped at 177.64M, offered to
  MachineSelectionView, and closed it. With SRC held instead, the first
  auto-repeat (sent 172.53M) closes it. **[V]**
- Chord at 84-92M, backlog 2-4 items (`--panel-dwell 3` tap, `--panel-dwell
  3` with SRC held to 104M, and default `--panel-dwell 16`): the SRC press
  waited 0.30-0.49M, MachineSelectionView was activated, and no SRC repeat
  or release was queued after that, even with SRC held or released later.
  The list stayed open to the end of the run (130M) in all three. So the
  firmware stops generating that key's repeats and release once the view
  is active. Only events generated before activation reach the view, and
  they exist only because the backlog delays activation. **[V]**
- The GUI session that kept the list open had `mainloop` 12 ahead of
  `dtim3` with no drift, so its queue was probably empty when the chord
  came. That session was not traced. **[O]**
- Why the main loop cannot keep up: each popped item costs about 171k
  instructions on average, against a DTIM3 period of about 156k. Not yet
  known whether this is real UI work made too expensive by the 4.68M
  instructions-per-second timer rate (`docs/history/upstream/HANDOVER.md`, lines 31-58,
  puts the real rate about 50 times higher), or an emulator artifact such
  as a busy wait or a slow peripheral model. **[O]**
- `--ips` above 4.68M, applied from `boot400M.snap`, stalls boot: still on
  the splash screen at 200M at 4x and 10x. Not a quick test. **[O]**
- `0x4005e022` is the MachineListView constructor: it calls
  `FUN_4010d946(this, "MachineListView")`. It still gets no hits in these
  runs; why is open. **[C]**
- The display-name accessor `FUN_400dcc50` is called from `0x4005faf2` in
  `FUN_4005fab8`, the per-row text callback stored in each MenuItem, not
  directly from `FUN_4005da40`. **[C]**
- View class names can be read from any view object through the Itanium
  RTTI layout: name = `cstr(*(*(vptr - 4) + 4))`, e.g. vtable
  `0x401e36f8` → typeinfo `0x401e36b4` → `"20MachineSelectionView"`.
  **[V]**

### A1 FUNC+SRC A/B is repeatable across state and profile lanes **[V]**

`experiments/a1-func-src.json` was run from `snapshots/postintro.snap` against
1.15C (`62d588456e47194bd56dfee9568fb9dd4521c4ff1e8b5427eb461355532e8c6c`)
as `out/experiments/a1-func-src/a1-real-002/`.  It delivered the raw FUNC/SRC
sequence `2201`, `2002`, `2000`, `2200`, requested an observation while the
chord was held at 14.4M, and ended at 40M.  Two fresh repeats of each
baseline/manipulated case passed in both the non-perturbing state lane and the
separately perturbing profile lane.  Every child returned zero, touched zero
fault pages, saved at the same actual boundaries (14,664,427 and 40,388,243),
and repeated its case's snapshot, panel, UiTrace and block-profile bytes.

- The four manipulated feeds landed repeatably at 624,018, 8,736,192,
  20,592,235 and 28,704,264 instructions.  UiTrace then shows a queue-send of
  `SRC(2) 0x03`, activation of `MachineSelectionView`, no subsequent SRC
  release/repeat or MachineSelectionView close, and a later queue-send of
  `FUNC(17) 0x00`.  This is the low-backlog behaviour described above: the
  raw `2000` proves SRC-up delivery, while the firmware suppresses its queued
  `0x12` release after the view activates.
- At observation, the state panels are repeatable 1,024-byte buffers and 772
  bytes differ across cases.  The baseline is the normal track page; the
  manipulated frame is the open `MACHINE SEL > TRACK 1` list.  The latest
  untorn-frame latches were 14,554,643 (baseline) and 9,048,263
  (manipulated), both before the 14,664,427 observation save.
- The configured on-chip SRAM range `0x80000000..0x80010000` differs by zero
  bytes at both observation and final endpoint.  A provenance-bound sweep of
  every mapped snapshot page instead finds 4,487 changed bytes on 13 of 134
  pages at observation and 4,413 bytes on 13 pages at the endpoint; see
  `mapped-page-diff.json` in the run directory.  These broader differences
  are state-lane evidence, but are not yet producer ownership.
- The perturbing profile lane has 16 baseline versus 57 manipulated UiTrace
  events.  Its address-sorted basic-block-entry profiles contain 11,749 versus
  12,351 `(address, hit-count)` tuples, with 1,416 baseline-only and 2,018
  manipulated-only tuples.  This is scoped dynamic call/view and block-entry
  evidence, not instruction coverage or a complete call graph.

The earlier `a1-real-001` artifacts are diagnostic only: their state/profile
bytes were already repeatable, but the report rejected every panel because it
compared an absolute restored timer clock with run-relative save counts.
`guirun.py` now records the live hook-time clock relative to the resumed run's
timer origin; `a1-real-002` is the accepted run.  The report and the raw
snapshot, panel and profile bytes were checked independently before this was
marked verified.

### Raising the timer rate after boot drains the UI queue **[V][O][C][D]**

`tools/guirun.py --ips-at WHEN:N` changes the timers' instructions per
emulated second at instruction count WHEN. Raising the rate from the
snapshot stretches boot (4x and 10x were still on the splash screen at
200M), so these runs raise it at 80M. `--trace-tasks` (`emu/taskprof.py`)
charges instructions to RTOS tasks at each context switch.

- `--ips-at 80M:4680000` gives progress lines identical to a run without
  the option. **[V]**
- Late chord (FUNC at 164M, SRC tap at 170M) with the rate raised at 80M
  to 1.5x, 2x, 4x or 10x: the UI queue depth stays at 0-3 up to 200M, the
  SRC press waits 0.07M-1.7M instructions instead of 5.3M, and
  MachineSelectionView is activated and still open at 200M. At 1x it
  closes at 177.6M. **[V]**
- At 2x the backlog comes back later: depth 9 at 200M, 29 at 300M and 60
  at 420M, and a chord at 380M closes the list at 400.8M. At 4x the depth
  stays at 0-1 up to 420M and the list stays open. **[V]**
- At 1x after boot the UI task (TCB `0x4094eee8`) gets 99.2% of the CPU.
  The tasks with entries `0x400f1fce` (priority 3), `0x400f1eb6`
  (priority 2) and `0x4012606a` (priority 6) get almost none, and `jobs`
  stays at 1. With the rate raised, `0x400f1fce` and then `0x400f1eb6`
  take 26-71% of the CPU between 100M and 160M, `jobs` goes to 2, and then
  `0x4012606a` takes 50-78%. **[V]**
- At 2x, 4x and 10x the screen shows `+DRIVE INITIALIZING` from about
  160M, covering the list (at 4x it is still there at 419M). At 1.5x the
  list is visible from 176M to 199M. So at 1x these runs never reach
  +Drive initialization: the UI task leaves no CPU for the job tasks.
  **[V]**
- The GUI session that kept the list open reached `jobs 2` at about 260M,
  and its screen changed to 409 lit pixels at about 470M. That may have
  been this splash. **[O]**
- With the rate raised at 80M to 1.5x, 4x or 10x and no key input,
  `+DRIVE INITIALIZING` is still on screen at 1000M: 131, 49 and 20
  emulated seconds after the change. It first appears at 250M (1.5x) or
  by 150M (4x and 10x). There is no exception and no fault. Between
  frames only the spinner changes. **[V]**
- The splash task (entry `0x4012606a`) waits until the count at
  `0x44e2d5cc` is nonzero, then loops with no exit. Each pass draws a
  progress step, `(0x44e2d5d0 * 0x27) / 0x44e2d5cc`, and turns a spinner
  (`FUN_40126332`). Ghidra shows one direct writer for each of the two
  counts, both before the loop. **[V]**
- The counts are also written through the helper `0x40125f6a` (reached
  through thunks `0x4003230c` and `0x40032320` from `FUN_40032c5c`, a
  mount routine): 2121 calls between 80M and 250M at 4x. So the bar does
  advance once the task gets CPU; "one direct writer" was true but
  incomplete. **[C]**
- While the splash is up, its task takes 49-51% of the CPU at 1.5x,
  72-74% at 4x and 88-90% at 10x. The job tasks (entries `0x400f1eb6`
  and `0x400f1fce`) use CPU only in the 20M window after they start,
  then almost none. At 1.5x the UI task gets half the CPU and the UI
  queue grows again (109 at 300M, 916 at 1000M); at 4x and 10x it stays
  at 0-1. **[V]**
- Why +Drive initialization stalled: with `unblock=True`, `build()`
  force-satisfies every semaphore pend that is not on its exclusion
  lists. The progress-screen task pends on semaphore `0x44e2d148`
  before its loop (at `0x401260c0`, covered by `display_wait`) and once
  per frame inside it (at `0x40126132`, not covered). The display
  module's PIT3 ISR (`0x40125f3c`; PIT3 set to PCSR `0x0936`, PMR
  `0x4323`, about 7.5 Hz) posts that semaphore. At 4x the per-frame pend
  ran 3879 times between 80M and 250M against 93 PIT3 posts, so the
  prio-6 task drew about 40 frames per real one and starved the prio-2
  job worker (entry `0x400f1eb6`), which was ready: parked right after a
  mutex unlock's `trap #0`, resume PC `0x4000178c`. **[V]**
- Fix: `display_sem` (read from `pea.l` at `0x40125f4e`, symbol
  `display_frame_post`) is always in the never-fake set. With it and the
  rate raised to 4x at 80M, the progress-screen task uses about 2% of
  the CPU, the job worker about 69%, the splash is gone by 200M, the job
  worker rests in the job pump from about 370M, an idle task takes the
  spare CPU, and the UI queue stays at depth 0-2 up to 600M. At 1.5x the
  splash is gone by 150M and the job worker is still working at 600M
  with the UI queue at 0. At 1x nothing changes: the job worker never
  starts. **[V]**
- Pends still force-satisfied in a 600M run at 4x, by call site:
  `0x400d4038` (intro, 175, all before about 120M), `0x40120cac` (in the
  CMD25 write routine, 4), `0x40120a92` (in the CMD18 read routine, 2),
  `0x40126046` (1). The UI task hits none of them, so `unblock` does not
  explain the UI backlog at 1x. **[V]**
- At 4x the firmware issues 941 CMD18 multi-block reads (`0x401208fe`)
  and 3 CMD25 multi-block writes (`0x40120ae4`) by about 129M. The
  eSDHC model completes each command at once and reads have no backing
  data (see emu/esdhc.py). **[V]**
- Whether the +Drive being empty matters later (projects, samples).
  **[O]**
- The MCF5441x reference manual gives "Up to 385 Dhrystone 2.1 MIPS @ 250
  MHz" (`docs/refs/rm.txt`, line 2475). The firmware's bus clock is 132
  MHz (see the section on the intro running at 15.00 fps), and the manual
  fixes the bus clock at half the core clock, so the core runs at 264
  MHz, above the manual's 250 MHz maximum. **[D]**
- Why the core clock is above the rated maximum. **[O]**
- The firmware has no CPU-speed calibration or counted delay loop that
  was found: its timed waits use DTIM1 or the PITs. No MCF5441x BogoMIPS
  boot log was found online. The real instruction rate is estimated at
  200-264M instructions per second, from docs/history/upstream/HANDOVER.md lines 31-58 and
  separately from the Dhrystone figure; `INSTR_PER_SEC` is 4.68M. **[D]**

### The rate rises automatically after the intro; where wall time goes **[V][O]**

`tools/guirun.py` and `emu/gui.py` raise the timer rate to 18.72M (4x
`INSTR_PER_SEC`) at the first chunk boundary after the intro hands over.
`--post-intro-ips N` sets the rate and 0 turns it off; an explicit `--ips`
or `--ips-at` disables it. Progress lines and the GUI label show wall-clock
instructions per second and the percentage of real time.

- `--post-intro-ips 0` matches a default run from before the change, and
  `--ips-at 80M:18720000` matches the earlier `--ips-at` run, in every
  emulated field of the progress and end lines to 200M. **[V]**
- Default, headless from `snapshots/boot400M.snap` with the five parts:
  the rate changes at 52999788, UI queue depth stays 0-1 (max 2) to 500M,
  +Drive init finishes at about 360-380M, no exception. **[V]**
- After init, 76-77% of each 20M window is the init task at the parking
  loop `0x400cf3e0`; the UI task takes about 23%. **[V]**
- Wall-clock rate in that run on an Apple M3 Max: about 10M instructions
  per second while the job worker runs (about 53% of real time at 4x), and
  4.0-4.1M per second once idle (21-22%). `idle-spins` rises from 0 to
  about 3.9M per 20M window at the same point. **[V]**
- Whether the idle-spin hook is what halves the wall-clock rate. Needs an
  A/B with that hook disabled before building idle skipping. **[O]**
- An earlier baseline on the same machine ran at load average 18-25, so
  its absolute numbers are not comparable: 0-80M at about 2.5M per second
  at every rate, 80-200M at 8.4M per second at 1x and 6.0M at 4x.
  `--trace-tasks` and `--trace-ui` changed the rate by about 1%. **[V]**
- The GUI label was not checked (no Tk in the agent sandbox). **[O]**

### The ColdFire tells the SHARC through a periodic DSPI2 frame **[V][O]**

Static reading only: Ghidra, the repo disassembler and `tools/refscan.py`.
No emulator run. In this section **[V]** means a second agent re-checked the
claim against the image bytes, and **[D]** means one agent read it from
Ghidra or disassembly output and it was not re-checked.

- `FUN_4002ce4a` writes the handler `0x4002d652` to the RAM vector slot
  `0x400002fc` (vector 191, INTC1 source 63) and 5 to `ICR1_63`
  (`0xFC04C07F`). The handler reads eDMA channel 50's SADDR (`0xFC045640`).
  When the counter at `0x4028ac90` is zero, it calls
  `FUN_400cf9c4(0x802, 0x80005348, 0xabc, 0x8000488c)` at `0x4002d6ba`.
  **[V]**
- **Correction: the vector-191 producer is now identified.** Channel 50 is
  SSI0 transmit, but its major-loop completion is INTC1 source 42/vector 170,
  not source 63. The RM lists source 63 as unused hardware and documents bit
  31 of `INTFRCH1` (`0xFC04C010`) as its software-force bit. On 1.16,
  initialization arms TCD48/TCD50, writes SERQ 48 and 50, and initially
  installs the generic vector-170 handler `0x400d2f98`. It also retains
  `0x4002d322` as the later channel-50 ISR. That ISR writes 50 to
  `EDMA_CINT` at `0x4002d35a`, then sets `INTFRCH1` bit 31 at `0x4002d360`.
  Vector 191 enters `0x4002dd0c` and clears the force bit at `0x4002dd30`.
  The same chain is present at relocated addresses in 1.15C. Thus the real
  cadence is SSI0-paced eDMA50 completion -> vector 170 -> software-forced
  vector 191. No instruction reloads the frame gate counter by its literal
  address, so the exchange cadence and counter reload remain open. **[V][C][O]**
- `FUN_400cf9c4` is a DSPI2 (`0xEC038000`) send and receive driver using
  eDMA channels 28 and 29. `FUN_400cf67c` sets CTAR0 to `0xFA010000`
  (16-bit frames). Each PUSHR entry is `0x8001xxxx` (CONT, PCS0). The last
  entry gets EOQ, TCD 29 DADDR is PUSHR (`0xEC038034`), and SERQ is written
  with `0x1c`. The counts are bytes: TX `0x802` (2050 bytes, 1025 frames)
  and RX `0xabc` (2748 bytes). Both buffers are in the 64 KB on-chip SRAM
  at `0x80000000`. **[V]**
- "TX length" and "RX length" above are misleading: those are not two
  independently sized directions. The eDMA word count of both the receive and
  the transmit descriptor is set from one variable, written once at boot by a
  function that returns `0xabc`, so the SPI transfer is a single fixed
  2,748-byte full-duplex frame. The first argument -- `0x802` on Digitakt II,
  `0xa80` on Digitone II -- is how many real payload words the driver copies
  out of the caller's buffer; the rest of the frame is tag-only entries. The
  third argument happens to equal the frame length, so the whole received
  frame is copied back. Frame offsets such as the machine type at `0x94 + 2i`
  are offsets into the payload, which maps one to one onto the frame's data
  words, so they are unaffected. Found by `angellinares` on Digitone II 1.11
  (PR #12) and checked here against `FUN_400cd2bc` on 1.16. **[V][C]**
- Ghidra lists no callers for `FUN_400cf9c4` or for the SHARC boot routine
  `FUN_400cef6c`. The two `jsr` calls to `FUN_400cf9c4` (`0x4002d6ba`,
  `0x400d13d4`) are in code that Ghidra did not assign to a function,
  because the stock ColdFire language cannot decode `movclr` (next
  section). An empty Ghidra caller list is not evidence of dead code in
  this image. **[V]**
- Frame content: a header written at `+0x00` (the constant 2) and at
  `+0x22` to `+0x32`, then a 16-pass loop at `0x4002e470`-`0x4002e63a`. The
  loop reads three per-track SRAM tables: `0x800047fc + 4*i` (longwords,
  shifted by `asr.l #8`), `0x80003340 + i*0x9a` and `0x80005b50 + i*0x8e`.
  The last base is the constant that `FUN_400db9aa` returns. **[V]** What
  the fields mean is not known. **[O]**
- A second handler, `0x400d1378`, installed by `FUN_400d15bc`, sends
  `0x802` bytes from `0x429307f4` with only the first word set (1) and
  receives nothing. **[V]** `FUN_400d15bc` is called by the console command
  parser `FUN_400cd594` on `ENTER TEST MODE` and by `FUN_400cef6c`.
  `EXIT TEST MODE` calls `FUN_4002d5c4`, which reinstalls `0x4002d652`.
  **[D]**
- **Superseded on the machine-type question by "The machine type reaches
    the SHARC, at TX frame offset `0x94 + 2i`" below.** The negative result in
    the next bullet is correct as stated -- the handler does not read `+0xa2`
    -- but it does read a copy of that byte in SRAM, and the type does reach
    the frame. **[C]**
- Machine type: no call to `FUN_4004fc02` or `FUN_400caf48`, and no read
  of `+0xa2`, was found in the handler or its frame loop. The one link
  found is in the commit `FUN_40035e90`: when the new type is 5 (MIDI) it
  calls `FUN_4002ed16(track, 0x7fff)`, which writes 1 to
  `0x80004684 + 4*track` and `0x800046c4 + 4*track` (addressed as
  `0x80003340 + (track + 0x4d1)*4` and `+ (track + 0x4e1)*4`). **[V]**
- Not known: whether the handler reads those two arrays, and whether a
  SLICE or PLACEHOLDER track produces a different frame. The writers of the
  per-track tables are linked with the handler, among functions that use
  file-browser strings (`ENTER DIR NAME`, `WRITE PROTECTED`), in about
  `0x4002c0fa`-`0x4002f000`. About 45 of them are called from elsewhere and
  they were not traced one by one. So "stock engine and own parameters" for
  a new sample machine is still open. No engine id has been found in the
  frame. **[D][O]**
- The 1.16 post-gesture checkpoint's SSI0 configuration is externally
  clocked: `MISCCR=0`, `CDRH=0`, `TCR=0x82` (FIFO0 enabled, external bit
  clock and frame sync), `RCR=0x482`, and `CCR=0x16f00` (24-bit words,
  16 words per frame). `TMASK=RMASK=0xffff0000`; `FCSR=0x88` sets the FIFO0
  receive and transmit watermarks to eight words. TCD48/50 minor loops are
  32 bytes, so one DMA request accounts for eight serial words and a 64-minor
  major loop accounts for 512 words. The external `SSI_CLKIN`/frame frequency
  is not captured, so there is no defensible numeric request-rate default;
  for a continuous stream the request rate is external bit clock / (24*8),
  or twice the frame rate with all 16 slots active. **[D][O]**
- The same checkpoint has no `0x007fffff` row head among the 32 entries in
  any of `0x4fe57100`, `0x4fe57900`, `0x4fe58100`, or `0x4fe58900`; all are
  zero. The generic vector-170 handler at `0x400d2f98` acknowledges CINT50,
  scans the currently completed RX bank for that marker, increments
  `0x43153a20` only when the marker is at index zero, and installs the pending
  `0x43153a28` callback into vector slot `0x400002a8` only after that counter
  exceeds 63. At this checkpoint the counter is zero, the pending callback is
  `0x4002d322`, and the slot still contains the generic handler. This is the
  missing external RX synchronization/handover, not merely a missing TX DMA
  tick. A second agent checked the marker scan, threshold, row heads, globals,
  and vector slot against the 1.16 bytes/restored pages. **[V][O]**
- `emu/ssi.py` now supplies an opt-in, exact-deadline event source for only
  TCD48/TCD50. It models the observed 32-byte minors, 64-minor completion,
  scatter/gather reload and vector 170; preserves RX destination bytes rather
  than inventing peer data; lets the guest CINT50/`INTFRCH1[31]` ISR run; and
  hands vector 191 over only at that ISR's image-resolved RTE boundary. Its
  request rate is mandatory, its checkpoint component is separate from the
  version-1 PIT/DTIM layout, and adding it to a legacy checkpoint requires an
  explicit upgrade. **[D]**
- At the explicitly exploratory 48,000-request/s profile,
  `out/experiments/ssi0-dma/control-001/` reaches the generic vector-170
  handler 16 times in 400,140 exact instructions with zero fault pages. A
  clean and narrow-traced run produce byte-identical endpoint snapshots
  (`11f81e1c...`). They correctly do not reach `0x4002d322` or vector 191,
  because no external RX marker was supplied. The separate
  `vector-chain-control-001` host-patches only the vector-170 slot and, at a
  separate 1,000-request/s exploration rate, records two each of the normal
  vector-170 ISR and vector 191, proving the model's
  CINT/force handoff as calibration, not natural behavioral provenance. No RX
  or SHARC payload was synthesized. **[D][O]**
- DSPI2 and eDMA channels 28/29 remain outside a general peripheral model:
  `emu/edma.py` handles UART8 channel 35, `emu/ssi.py` narrowly handles SSI0
  channels 48/50, and `0xEC03802C` is still a constant. **[V][O]**
- Ruled out as the control link **[D]**:
  - FlexBus `0x8C000000` (`FUN_400cfd40`, callers `FUN_40146148`,
    `FUN_4014653c`, `FUN_401465a4`) carries sample pages and slot headers
    (address, length, loop point). Only `FUN_400cf4a8`, `FUN_400cf534` and
    the boot routine `FUN_400cf67c` access `0x8C000000`-`0x8C00000F`.
  - `FUN_4012720e` builds 28-byte-header packets (sequence number,
    fragment offset, checksum) for the SysEx dump and receive code.
  - The `Digisharc::rpcMsgOpReq_t` and `rpcMsgPingRequest_t` constructors
    (`FUN_401bf37c`, `FUN_401bf3be`) are called from `FUN_40115372`, which
    references `"MY ANALOG FOUR"`. `rpcMsgHeader_t`'s constructor is
    `FUN_401b6316`.
  - DSPI1 (`0xFC03C000`, eDMA 14/15, `FUN_400cfb92` via `FUN_40011df8`)
    carries short opcode request and reply transactions, probably to a
    codec.
  - eDMA channel 59 is eSDHC block I/O (`FUN_400f0f7a` write,
    `FUN_401208fe` read).

### Stock Ghidra cannot decode `movclr`; a separate language fixes it **[V][C]**

- The MCF5441x is a V4m with EMAC, and EMAC instructions use line A
  (`0xAxxx`). The line-A words in the image are EMAC instructions, not
  traps. The software float routines are ordinary `jsr` calls (see "Why
  the emulator was slow").
- The handler at `0x4002d652` saves the EMAC state at `0x4002d67c`:
  `a988 a93c 0000 0000 ab84 af85 a1c0 a3c1 a5c2 a7c3 ad86` =
  `move.l MACSR,A0`, `move.l #0,MACSR`, `move.l ACCext01,D4`,
  `move.l ACCext23,D5`, `movclr.l ACC0..ACC3,D0..D3`, `move.l MASK,D6`.
  A `movem` of D0-D6/A0 follows. Digitone II 1.11 has the same bytes at
  `0x40025e60`.
- Register moves are one word. A `#imm` source adds a 32-bit extension,
  so `move.l #imm,MACSR` is 6 bytes (CFPRM).
- Stock Ghidra 12.1.3 (`68000:BE:32:Coldfire`) decodes most EMAC
  instructions but has no `movclr` constructor, so `a1c0 a3c1 a5c2 a7c3`
  decode as bad instructions and flow analysis stops there. It also copies
  `move.l ACCy,ACCx` in the wrong direction, picks the wrong accumulator
  for MAC and MSAC with load (CFPRM p.6-4), and prints `move.l Ry,ACCx`
  and the two ACCext moves with their operands swapped. An earlier reading
  in this file, that Ghidra stops at every EMAC word, was wrong. **[V][C]**
- The image has 96 `movclr` words (Digitakt II 1.15C) and 112 (Digitone II
  1.11), mostly in accumulator saves at handler entry. **[D]**
- `tools/ghidra/ColdfireEMAC/` is a separate language,
  `68000:BE:32:ColdfireEMAC`, with those instructions fixed; see its
  README and `tools/ghidra/install-coldfire-emac.sh`. Imported with it
  (`~/ghidra-projects/dt2-emac`, 98 s), `FUN_4002d652` is a 5796-byte
  function whose decompile shows
  `FUN_400cf9c4(0x802,&DAT_80005348,0xabc,0x8000488c)`. `FUN_400cf9c4`
  gets its two callers, error bookmarks drop from 64 to 41, functions rise
  from 10520 to 10526, and five checked functions keep their entry and
  size. **[V]**
- Unicorn 2.1.4 with `UC_CPU_M68K_CFV4E` runs `movclr`, `mac.l` without
  load, the MASK and ACCext moves and the handler prologue as the manual
  says (`tests/test_unicorn_emac.py`). Two gaps are pinned in that test:
  `move.l MACSR,Rx` keeps bits 31..12, and `move.l ACCy,ACCx` takes an
  exception. **[V]** A linear sweep finds no `move ACC,ACC` and no MAC with
  load in either handler (the 31 and 24 uses are elsewhere), so the
  handlers can run in Unicorn. **[D]** The frame build, though, reaches a
  MAC with load in `FUN_400db9aa` at `0x400db9e0`, which stock Unicorn
  2.1.4 cannot run; `patches/unicorn-2.1.4-m68k-emac-mac-load.patch` fixes
  it (see "The frame capture runs; the frame build is switched off"). **[C]**

### Digitone II 1.11: the same link and the same machine table shape **[V][D][O]**

- The sections come from `uv run python -m emu.extract
  Digitone_II_OS1.11.syx -o DIR`. 1.11 also has a section 8 (159,948
  bytes, mostly `0xFF`) that 1.10E does not have. **[D]**
- Dispatcher `FUN_400c248e`: `moveq #4,d1`, a bound check, `muls` by
  `0x2c`, `addi.l #0x42432b24`. Out of range it returns `0x42432bd4`
  (entry 4). **[V]** The five descriptors are 0 FM TONE, 1 WAVETONE,
  2 FM DRUM, 3 SWARMER and 4 MIDI. **[D]**
- `FUN_4004b7f2` calls vtable `+0x28` twice and returns the byte at `+0xde`
  of the result: the machine type. **[V]** `FUN_4004b860` reads `+0xdf`,
  probably the filter type. RTTI names `Digisharc::machineType_t`,
  `Digisharc::synthParams_t` and `VoiceConfig::updateMirror`. **[D]**
- Handler `FUN_40025e36` reads `0xFC045640`. When the counter
  `0x402876f8` is zero, it calls
  `FUN_400cf7be(0xa80, 0x80005e60, 0xabc, 0x800053a4)` at `0x40025e9e`:
  TX 2688 bytes, RX 2748 bytes. The installer at `0x400d11d4` puts the
  test handler `0x400d0f90` into `0x400002fc` and 5 into `0xFC04C07F`.
  **[V]**
- The frame has 16 per-track slots of `0x92` bytes, copied from
  `0x800068e4 + i*0xca` with four `FUN_40134490` copies per slot.
  `FUN_400db12a` rebuilds that table from `0x80003af0` and returns its base
  as a constant. **[D]** No read of the machine type was found on this
  path, and the code that fills `0x80003af0` was not found. **[O]**
- On Digitone II the engine must reach the DSP, yet static reading did not
  find it there either. Not finding the machine type on either image is a
  limit of static reading, not evidence that the frame lacks it. **[O]**

### LFOs and the modulation matrix are ColdFire code, in the frame ISR **[D][O]**

Read statically from the image bytes of **Digitakt II 1.16** and **Digitone II
1.11** by `angellinares/dn2_firmware_explore` (its `docs/modulation-matrix.md`),
not run in this emulator — hence **[D]**. It names the function behind the
MSAC-with-load instruction this project patched Unicorn for, and places both
kinds of modulation on the ColdFire side of the DSPI2 frame link.

**Neither the LFOs nor the MIDI modulation sources reach the SHARC as
parameters.** Both are computed per audio frame on the ColdFire and applied into
the per-track parameter value array before the frame is built. On Digitone II
the frame carries value-array indices 25–99 only; LFO parameters are indices
1–24 and are never sent.

#### The modulation kernel and the six sources

| | Digitakt II 1.16 | Digitone II 1.11 |
|---|---|---|
| MAC kernel, one source through four destinations | `0x400d9354` | `0x400db1dc` |

The kernel heads are byte-identical. Each destination descriptor is a longword
`depth:s16 << 16 | dest:s16`, and the kernel applies
`msacw %d1l,%d2u,%a1@+,%d2,%acc0` — **a MSAC with load, the form Unicorn got
wrong** — then reads, saturates and writes back the parameter at `dest`. It is
reached as `lea %pc@(...),%a3; jsr %a3@`, so a direct-call scan finds no callers.

On Digitone II 1.11 the driver `0x400db22c` runs **six sources x 4 destinations
x 16 tracks**. The six are the MIDI performance modulators — **Velocity, Mod
Wheel, Pitch Bend, Breath Controller, Aftertouch, Key Tracking** — identified
from a contiguous string run before `Sound::updateMirror`, a registration
sequence naming five of six objects, and six `*SetupView` RTTI classes; Velocity
is pinned independently by its note-time write. Rows 2–5 are assigned by
registration order, not individually pinned. **[D][O]**

#### The LFO tick

| | Digitakt II 1.16 | Digitone II 1.11 |
|---|---|---|
| LFO tick, generator and apply | `0x40139342` (806 B) | `0x40137726` (1,028 B) |
| called once per frame from | `jsr` at `0x4002e91c` | `jsr` at `0x400272d4` in `FUN_40025e36` |
| inner loop start, `moveq #2` (three LFOs) | `0x4013935e` | `0x40137784` |
| state initialisers, 16 x 3 x 40 B | `0x40138f50`, `0x40138fa4` | `0x401372f4`, `0x40137348` |
| waveform function table | `0x4022231c` | `0x4020b340` |
| random-wave slew table | `0x40222334` | `0x4020b358` |
| value-array stride per track | 142 | 202 |
| `DEST` upper bound | 70 | 100 |

Per track, per LFO, the tick reads `SPD MULT FADE DEST WAVE SPH MODE DEP` from
the value array and:

- **Tempo-synced or free.** `MULT` is clamped to 0–23. For 0–11 the rate is an
  argument the ISR passes in; for 12–23 it subtracts 12 and uses a fixed
  `14400`. Increment is `SPD x rate >> (11 - MULT)`, with `SPD` and `DEP` bipolar
  about `0x4000`.
- **Phase** wraps at 1,382,400,000 on Digitone II.
- **Waves 0–5** are dispatched through the function table. The six entries keep
  the same relative spacing on both images; by their arithmetic, in order:
  triangle, parabolic sine (EMAC `x*|x|` with 0.9 and 0.194), square, inverting
  ramp, exponential, `max(x, 0)`. Entry 6 is null.
- **Wave 6 is sample-and-hold** from a private lagged Fibonacci generator
  (Digitone II `0x4013739c`) — the LFO never calls `rand()` — slewed through the
  table above, indexed by `SPH >> 8`.
- **Apply:** `value[DEST] = clamp(value[DEST] + sample x DEP x fade, 0, 32512)`.

The number of LFOs is a literal loop start with field offsets written for the
last LFO, so a fourth needs the start, the offsets, the 120-byte state stride and
the 1,920-byte state arrays changed, plus somewhere for its parameters. **[O]**

**Why a search for it fails.** It is called from inside a function whose
boundaries a caller heuristic misplaces; stock Ghidra stops at the `movclr` in
that function (see the section above); its random wave uses a private
generator; and it lives in a clock module rather than beside the kernel.

**Open.** Neither is exercised in this emulator. The rate argument's source (the
tempo) is not traced, and a second single-track tick on Digitone II 1.11
(`0x401373dc`, its own 3 x 40 state array) is not identified. **[O]**

### Names from RTTI and code seeds in the EMAC Ghidra project **[V][D]**

- `tools/codeseeds.py` finds 31016 `jsr`/`bsr` calls to 5685 targets in
  the code ranges and 22 vector-table writes. `tools/rttiscan.py` finds
  1052 typeinfo objects (class 122, si_class 728, vmi_class 125, pointer
  50, fundamental 23, function 4), 1883 vtables with 12349 slots, 4802
  instructions that load a vtable, and 47 `Class::method` strings, 41 of
  them loaded by code. Each takes under 2 s; the output is in
  `out/symbols/`. **[V]**
- Ghidra's own `RecoverClassesFromRTTIScript` refuses the program until its
  compiler is set to gcc, and then recovers no classes. **[D]**
- `tools/ghidraapply.py seeds --analyze` on `~/ghidra-projects/dt2-emac`
  created 32 functions, rolled back 34 (an Error bookmark or no function),
  skipped 466 targets inside data, and named 19 interrupt handlers
  `vector_<n>_handler`. `tools/ghidraapply.py rtti` created 3677 functions
  from vtable slots and renamed 6811: 5774 `Class::vfunc_N`, 1016
  `Class::ctor_dtor`, and 21 from `Class::method` strings, such as
  `Project::updateMirror` and the other seven `updateMirror` methods. 152
  slot functions shared by several classes stay unnamed. Functions went
  from 10526 to 14257, and Error bookmarks stayed at 41. The project before
  these writes is in `~/ghidra-projects/backup-2026-09-14/`. **[V]**
- `vfunc_N` is the slot index, not the method's name. `ctor_dtor` marks a
  function that loads a vtable address; it can be a constructor or a
  destructor.
- Spot checks: `0x4002d652` and `0x400d1378` are both
  `vector_191_handler`, and `FUN_400cf9c4` still has exactly those two
  callers. The vtable at `0x402015ec` (5 slots) belongs to
  `std::_Sp_counted_ptr_inplace<Digisharc::rpcMsgHeader_t, ...>`, and
  `0x401b6316` is that class's `ctor_dtor`. The earlier raw search above
  read the vtable one word late, as `0x402015f0` with 4 slots. **[V]**

### Digitakt II 1.16 in Ghidra: the same layout, 14743 functions **[V]**

- `uv run python -m emu.extract Digitakt_II_OS1.16.syx -o out/sections/dt2-1.16`
  writes a MAIN OS of 3,275,616 bytes for `0x40000400`, sha-256 `57bb4dfa…`
  (1.15C: 3,177,312 bytes). Digitone II 1.11 and 1.10E are extracted to
  `out/sections/dn2-1.11/` and `out/sections/dn2-1.10E/`. **[V]**
- `~/ghidra-projects/elektron-emac` holds two programs.
  `/dt2-1.15C/section_3_MAIN_OS.bin` is a copy of the `dt2-emac` program made
  by `tools/ghidracopy.py`; a dump of the copy matches
  `out/ghidra/dt2-1.15C-emac/` in every count and in each function's entry,
  name, size, body, signature, callers and callees.
  `/dt2-1.16/section_3_MAIN_OS.bin` is a ColdfireEMAC import
  (`GHIDRA_FOLDER=dt2-1.16 tools/ghidra.sh import`, 115 s), and
  `McfLabels.java` added 87 labels and 19 blocks to it. `dt2-emac` stays the
  1.15C project. **[V]**
- A headless `-postScript` cannot pack the program it processes:
  `saveToPackedFile` fails with "Unable to lock due to active transaction",
  and the analyzer then saves the program anyway. That happened once to
  `dt2-emac`; a dump made afterwards matched the 2026-09-14 dump exactly.
  `tools/ghidracopy.py` packs the stored file and never opens the program. **[V]**
- The image has the 1.15C layout, moved. Main code ends with an `rts` at
  `0x401e97a8` (1.15C `0x401d6108`), and the next `0x400` bytes are a
  numeric table with no `rts` or `link` word. The typeinfos and vtables start
  at `0x401ec324` (1.15C `0x401d8c84`). Near the end, the `0x4305` bytes from
  `0x4030ccfc` equal those from `0x402f4c4c` in 1.15C, a shift of `+0x180b0`;
  they hold the 60 functions at `0x4030cd04-0x4030f89e`. The last byte that
  is neither `0x00` nor `0xff` is at `0x403117c3` (1.15C `0x402f9c13`). **[V]**
- `tools/entryhist.py` over a dump made before the seeds gives the code
  ranges `0x40000400-0x401f0400` and `0x40300400-0x40310400`: 10915
  functions, none in between. On the 1.15C dump it gives
  `0x40000400-0x401e0400` and `0x402f0400-0x40300400`. The 1.15C defaults in
  `tools/codeseeds.py` are wider, and their `0x402c0400-0x402f4400` holds no
  `rts` or `link` word. **[V]**
- Before the seeds, the 1.16 program has 10915 functions and 40 Error
  bookmarks. With the ranges above, `tools/codeseeds.py` finds 31726 calls to
  5345 targets and 22 vector-table writes, and `tools/rttiscan.py` finds 1066
  typeinfo objects (class 122, si_class 738, vmi_class 129, pointer 50,
  fundamental 23, function 4), 1933 vtables, 4852 vtable loads, and 47
  `Class::method` strings, 41 of them loaded by code. **[V]**
- `tools/ghidraapply.py seeds --analyze` created 32 functions, rolled back 7,
  skipped 56 targets inside data, and named 19 interrupt handlers (1.15C,
  with its wider ranges: 32, 34, 466, 19). `tools/ghidraapply.py rtti` created
  3773 functions and renamed 6970: 5918 `Class::vfunc_N`, 1032
  `Class::ctor_dtor`, and 20 from `Class::method` strings, among them all
  eight `updateMirror` methods. 153 slot functions shared by several classes
  stay unnamed. Functions went from 10915 to 14743, and Error bookmarks
  stayed at 40. **[V]**
- The two vector-191 handlers are `0x4002dd0c` and `0x400cec70` (1.15C
  `0x4002d652` and `0x400d1378`). Each is installed by `move.l #handler,dN`
  then `move.l dN,$400002fc`. **[V]**
- The dump is `out/ghidra/dt2-1.16-emac/`; `ghidradump.py --image` names the
  image under `out/sections/`, so `image_matches_sections` is true. It is
  complete, with 16 decompile failures: the same 15 `MidiRpc*Response`
  `ctor_dtor` functions as in 1.15C, and one large function in each version
  (`FUN_401bdee2`; 1.15C `FUN_401ac1be`) that overflows the decompiler's
  response buffer. **[V]**

### Version Tracking carries 1.15C addresses to 1.16 **[V][D][O]**

- `tools/ghidravt.py run` runs Ghidra's AutoVersionTrackingTask from
  pyghidra with the options of `AutoVersionTrackingScript.java` and a 32 GB
  heap, and exports the matches. The headless script, with its 2 GB heap, ran
  out of memory after 30 minutes in the duplicate function correlator on
  1.15C -> 1.16 and left the destination unchanged. The sessions are in
  `/vt/` of `~/ghidra-projects/elektron-emac`, the exports in `out/vt/`.
  **[V]**
- Digitone II 1.10E and 1.11 were imported the same way, with 13448 and
  14017 functions after seeds and RTTI; the dumps are
  `out/ghidra/dn2-1.10E-emac/` and `out/ghidra/dn2-1.11-emac/`. The
  Digitakt-to-Digitone run writes to a copy of 1.11,
  `/dn2-1.11-from-dt2/section_3_MAIN_OS.bin`. **[D]**
- Each accepted function association is one-to-one:

  | pair | seconds | accepted matches | function associations | source functions matched |
  |---|---|---|---|---|
  | Digitakt II 1.15C -> 1.16 | 1092 | 50,741 | 9778 | 68.6% of 14257 |
  | Digitone II 1.10E -> 1.11 | 897 | 47,267 | 9090 | 67.6% of 13448 |
  | Digitakt II 1.15C -> Digitone II 1.11 | 1046 | 43,414 | 8126 | 57.0% of 14257 |

  **[V]** All three runs end "with some apply markup errors", and the task
  logs nothing more about them in headless mode. **[D]**
- `tools/vtcheck.py` checks the associations against the images and against
  dumps made before Version Tracking. For 1.15C -> 1.16, 2365 function bodies
  are byte-identical, 7332 differ at the same size, and 81 changed size. Ten
  sampled same-size pairs differ only in addresses and in branch targets that
  are themselves matched; five resized pairs are the same functions with real
  code changes. **[V]** Where both dumps give a name, 7 of 5097 names differ,
  and the callees agree for 5913 of 5925 functions. **[D]**
- Some matches are wrong. `TransposeConfigMenuView::vfunc_2` ->
  `BreakOutBoxRoutingMenuView::vfunc_2` (in both Digitone runs) and
  `SamplerLedView::vfunc_17` -> `ArpSetupMenuView::vfunc_18` (1.15C ->
  Digitone II 1.11) differ in size by 43-50% and share only boilerplate.
  Check a match before relying on it. **[V]**
- `Velocity::vfunc_18` in 1.15C is `Velocity::vfunc_19` in 1.16: the
  function is unchanged, and Velocity's vtable grew from 23 to 24 slots. A
  `vfunc_N` number can shift between versions. **[V]**
- Other name differences are storage structures with new version numbers:
  `projectStorage_v4_t` -> `projectStorage_v5_t` and `projectStorage_v15_t`
  -> `projectStorage_v16_t` in Digitakt II 1.16, `kitStorage_v3_t` ->
  `kitStorage_v4_t` and `patternStorage_v3_t` -> `patternStorage_v4_t` in
  Digitone II 1.11. **[D]**
- The frame link on 1.16, carried by the 1.15C -> 1.16 run and checked
  against both images:

  | 1.15C | 1.16 | evidence |
  |---|---|---|
  | `0x4002d652` vector-191 handler | `0x4002dd0c` | calls the driver at `0x4002dd74` (1.15C `0x4002d6ba`) |
  | `0x400d1378` vector-191 handler | `0x400cec70` | calls the driver at `0x400ceccc` (1.15C `0x400d13d4`) |
  | `FUN_400cf9c4` DSPI2 driver | `FUN_400cd2bc` | its callers are the two handlers, in both |
  | `FUN_400cef6c` SHARC boot routine | `FUN_400cc864` | 1162 bytes in both |
  | `FUN_4002d602` | `FUN_4002dcb2` | 48 bytes in both |
  | call at `0x400330f6` in `FUN_40032f5a` | `0x4003395c` in `FUN_400337ba` | `jsr` to the function above; the task changed size |
  | `FUN_400db9aa` | `FUN_400d92a2` | both return `0x80005b50` |

  `FUN_4002d63e` -> `FUN_4002dcee`, called by the vector-191 handler, gains
  a bound check (`cmp #0xf`) in 1.16. **[V]**
- Not carried: the stop-flag writer `0x4002d632` lies outside any function,
  and `FUN_400caf48` has no match. The gate variables and the frame tables are
  RAM addresses, outside the image; they are to be re-found from the
  functions above. **[O]**
- The 1.15C -> Digitone II 1.11 run agrees with "Digitone II 1.11: the same
  link and the same machine table shape": 1.15C's `0x400d1378` maps to the
  1.11 test handler `0x400d0f90`, and `FUN_400db9aa` maps to `FUN_400db12a`,
  which returns `0x800068e4`. 1.15C's handler `0x4002d652` and driver
  `FUN_400cf9c4` have no match there, although 1.11's `FUN_40025e36` and
  `FUN_400cf7be` have the same roles, and the driver's callers are the two
  handlers in both. The 1.11 handler is 7582 bytes against 5796, and its
  driver compares the TX length with `0xaf0` where 1.15C uses `0xbc0`.
  **[V]**

### The machine type reaches the SHARC, at TX frame offset `0x94 + 2i` **[V][C]**

This corrects "The ColdFire tells the SHARC through a periodic DSPI2 frame"
above, which recorded that no link from a track's machine type into the frame
could be found. The link exists. Earlier searches missed it because the frame
handler never reads the machine-type field of a track object: it reads a
**copy** of that byte in on-chip SRAM. No xref query and no `tools/refscan.py`
sweep for `+0xa2` could have found it. **[C]**

Addresses are Digitakt II 1.16.

- `FUN_4002d438(src, track)` reads the machine type at `src + 0xa2`, asks
  `FUN_400da3b0` whether it is permitted, and on yes memcpys `0x9a` bytes
  **starting at that same byte** into SRAM at `0x80003cd0 + track*0x9a`. Byte
  0 of each SRAM row is therefore the track's machine type. **[V]**

  ```
  4002d458  712a 00a2       mvs.b (0xa2,A2),D0      ; the type byte
  4002d45e  4eb9 400d a3b0  jsr FUN_400da3b0.l      ; permitted?
  4002d4aa  486a 00a2       pea (0xa2,A2)           ; source = the type byte
  4002d4b8  0680 8000 3cd0  addi.l #-0x7fffc330,D0  ; dest = 0x80003cd0 + track*0x9a
  4002d4c0  4e93            jsr (A3)                ; memcpy, 0x9a bytes
  ```

- The vector-191 handler `0x4002dd0c` loads that byte into D7 and stores it,
  sign-extended to a word, into the TX frame: **[V]**

  ```
  4002eb4a  1e28 0990       move.b (0x990,A0),D7b   ; A0 = 0x80003340 + i*0x9a,
                                                    ; so +0x990 is 0x80003cd0 + i*0x9a
  4002eb80  4887            ext.w D7w
  4002ebe0  3747 0094       move.w D7w,(0x94,A3)    ; A3 = 0x80005348, the TX buffer
  ```

  Nothing between `0x4002eb80` and `0x4002ebe0` writes D7: the four
  `FUN_401360ac` copies in between take stack arguments and do not touch it.
  The `tst.b D7b` at `0x4002eb68` is a second, independent use of the same
  register, not its only one. Reading only that first use is what made an
  earlier pass conclude the type was reduced to a boolean. **[C]**

- Measured, not only read. `tools/sharcframe.py` on
  `out/snapshots/dt2-1.16/boot400M.snap` with `--open-gate --passes 3`, poking
  track 0's type byte at `0x80003cd0` and diffing the captured 2050-byte frame
  against a type-0 baseline, changes exactly two bytes -- reproducibly across
  passes 1 and 2, and across three values: **[V]**

  | poked type | frame `0x95` | frame `0x73d` |
  |---|---|---|
  | 4 | `00` -> `04` | `00` -> `01` |
  | 5 | `00` -> `05` | `00` -> `01` |
  | 6 | `00` -> `06` | `00` -> `01` |

  `0x95` is the low byte of the big-endian word at `0x94 + 2i`: the type
  verbatim. `0x73d` is the low byte of the word at `0x73c + 2i`, a derived
  flag. The static read and the measurement were made by different agents from
  different evidence and agree exactly.

- So a new machine is not a ColdFire-side concern only. The DSP is told, per
  track and every frame, which of the seven types a track is. What the SHARC
  does with the value is the open question, and it is now a sharp one: find
  the reader of receive-buffer offset `0x94 + 2i` in the DSP program. **[O]**

#### The per-track TX frame map **[D]**

The frame is 2050 bytes at `0x80005348`, sent by
`FUN_400cd2bc(0x802, 0x80005348, 0xabc, 0x8000488c)`. Sixteen tracks; each
field is a big-endian word at `offset + 2i`. Traced from the handler's
disassembly. Only `0x94` and `0x73c` are confirmed by measurement.

| TX offset | source |
|---|---|
| `0x02` | low word of `*(long *)(0x800047fc + 4i) >> 8` |
| `0x34` | word at `0x800047dc + 2i` |
| `0x54` | sign-extended byte at `0x80003cd0 + i*0x9a + 2` |
| `0x74` | word at `0x80005b50 + 2i` |
| `0x94` | sign-extended byte at `0x80003cd0 + i*0x9a + 0` -- **the machine type** **[V]** |
| `0xb4` | sign-extended byte at `0x80003cd0 + i*0x9a + 1` |
| `0x73c` | `0` if the type is 0 and the word at `src + 0x60` is 0, else `1` **[V]** |
| `0x75c` | constant `0` |
| `0x77c` | low word of `*(long *)(0x47db41d0 + i*0x14 + 8)` |
| `0x79c` | high word of the same long |
| `0x7bc` | word at `0x47db41d0 + i*0x14 + 0x12` |

There is also a per-track `0x60`-stride sub-block: when the type is 6, slice
boundaries go to `0xee`, `0xf0`, `0xf2`, `0xf4`, `0xf6` and `0xf8`, each
`+ i*0x60`. That type-6 test is at `0x4002ec18` (`moveq #6,D1`) and reads the
same SRAM byte. **[D]**

The single `FUN_400cd2bc` call is at `0x4002dd74`, at the **top** of the
handler, before the per-track loop that fills the buffer. Each firing sends
the frame built by the previous firing and then rebuilds it: a
one-cycle-delayed double buffer, not build-then-send. **[D]**

#### Corrections to the SRAM layout **[C][O]**

- The `0x9a`-stride per-track table is at **`0x80003cd0`**, not `0x80003340`.
  The handler addresses a row as `0x80003340 + i*0x9a` plus a `+0x990`
  displacement, and `0x80003340 + 0x990 = 0x80003cd0`; the base register, not
  the table, sits at `0x80003340`. `tools/framelink.py`'s `TABLES` entry
  `(0x80003340, 0x9a, 16, 'track_9a')` therefore named the wrong 2,464 bytes:
  the rows run `0x80003cd0`-`0x80004670`; the tool now uses the corrected base.
  **[C][O]**
- `FUN_4002d438`'s `0x8e`-byte copy goes to `0x80003362 + track*0x8e`, from
  `src + 0x14`. Whether that is the same structure as the
  `0x80003340 + i*0x8e` rows `FUN_400d92a2` reads -- the two bases are `0x22`
  apart with equal stride -- is unresolved. **[O]**
- `FUN_400d92a2` has two loops, and only the second writes
  `0x80005b50 + i*0x8e`: a verbatim copy of three longs from
  `0x80003340 + i*0x8e`, at `+0x2a`, `+0x3a` and `+0x4a`. The EMAC-saturating
  loop reads `0x8000dd40` and writes the adjacent `0x80005b4c`, a different
  region. **[D][C]**
- The type == 4 or 6 compare that gates `FUN_400d907e` is at `0x4002e5f8`, not
  at `0x4002eb4a`. Both read the same byte; `0x4002eb4a` feeds the `0x94` and
  `0x73c` frame fields. **[C]**
- That branch is gated on `*(long *)(local_68 + 0x64) > 0` (`ble.b` at
  `0x4002e5e4`), a slice count carried in the machine-set message. On a stock
  boot with no sliced sample the branch is never taken: confirmed in the
  emulator, where `0x800033a0` was never written in any run, patched or not.
  Testing it needs a track carrying a sample with slices. **[V][O]**
- The permission mask table is 7 longs of which only the high word is read
  (`mvz.w (0,A0,D0*4),D0`), and every high word is `0xffff`: `0x401fbda6` on
  1.15C and `0x4020ecbe` on 1.16, byte-for-byte identical. An earlier reading
  of that table as consecutive small integers came from using load base
  `0x40000000` instead of `0x40000400`, and is withdrawn. Both images load at
  `0x40000400`. **[V][C]**

#### The SRAM row refreshes only when the track's source pointer changes **[V]**

Step 1a of the 2026-09-16 handover asked whether a machine commit reaches
`FUN_4002d438` or whether the row only refreshes on a pattern load. The front
half of the chain is now mapped. Three agents read it independently, by
decompile, by disassembly and by a raw-image scan, and agree.

`FUN_4002d438(src, track)` is a wholesale resync, not an incremental one, and
it caches the pointer it last synced from: **[V]**

```
*(int *)(&DAT_80003340 + (track + 0x4f3) * 4) = src;          /* 0x4002d47a */
FUN_401360ac(track * 0x8e + -0x7fffcc9e, src + 0x14, 0x8e);   /* the 0x8e mirror */
FUN_401360ac(&DAT_80003cd0 + track * 0x9a, src + 0xa2, 0x9a); /* the DSP row */
FUN_400d9000();
```

`0x80003340 + 0x4f3*4 = 0x8000470c`, so the cache is sixteen longs at
`0x8000470c`-`0x8000474b`. The compiler materialises that address directly, as
`lea (-0x7fffb8f4).l`, in the two places that clear the whole array, which
confirms the arithmetic independently of the decompiler. **[V]**

The vector-191 handler calls `FUN_4002d438` only on a cache miss, and takes a
different path on a hit (`decomp/4002dd0c_vector_191_handler.c`, around
`0x4002e50c`-`0x4002e574`): **[V]**

```
if (iVar4 != *(int *)(&DAT_80003340 + (uVar17 + 0x4f3) * 4)) {
LAB_4002e572:
    FUN_4002d438(iVar4,uVar17);           /* pointer identity changed */
    goto LAB_4002e578;
}
if (*(int *)(&DAT_80003340 + (uVar17 + 0x4f3) * 4) == 0) {
    iVar4 = _DAT_80004704 + uVar17 * 0x450 + 0x34;
    goto LAB_4002e572;                    /* slot zeroed: forced refresh */
}
FUN_400d90ac(uVar17,&DAT_80003340,*(int *)(&DAT_80003340 + (uVar17 + 0x4f3) * 4));
```

A zero in a slot is therefore an **invalidate**: it forces a full row refresh
on the next frame.

The steady-state path `FUN_400d90ac` cannot change the machine type. It walks a
per-track dirty bitmask at `0x47db461c + track*0xc` (three words, up to 96
bits) and, for each set bit, copies one short from `src + 0x14 + idx*2` into
the `0x8e` mirror at `0x80003362 + track*0x8e` and into a DSP fixed-point table
at `0x8000dd40`. It never addresses `0x80003cd0` and never reads `src + 0xa2`:
its whole domain is source offsets `0x14`-`0xa1`, and the machine type is at
`0xa2`. Its dirty bits are set by `FUN_400d9204`, which the same handler calls
twice per interrupt when a queued parameter-edit event is present. **[V]**

Every writer of the cache, found three ways -- Ghidra `data_refs`, a `disasm/`
grep for both `8000 470c` and the `addi.l #0x4f3` displacement, and
`tools/refscan.py` over the raw image at 96.78% coverage. Only six functions in
the image hard-code the `0x4f3` displacement. **[V]**

| address | function | writes |
|---|---|---|
| `0x4002d47a` | `FUN_4002d438` | the pointer |
| `0x4002e052` | vector-191 handler | zero, all 16 slots, when the live track base `_DAT_80004704` changed |
| `0x4002e516` | vector-191 handler | zero, one slot, after a refresh from an explicit override pointer |
| `0x4002e640` | vector-191 handler | zero, one slot, in the slice branch |
| `0x4002da8a` | `FUN_4002da7a` | zero, all 16 slots, unconditional on entry, right after `_DAT_80004704 = param_1` |
| `0x4002da48` | `FUN_4002da38(track)` | zero, one slot, unconditional -- the invalidate helper |
| `0x4002d7fa` | `FUN_4002d7a4` | zero, one slot, only when the cached pointer already differs from the live one |

`FUN_4002d7a4(value, track, index)` is the per-parameter apply: it writes one
short into the `0x8e` mirror, and its callers are `SoundParameterSet::vfunc_13`,
`SoundParameterSet::vfunc_31`, `FxParameterSet::vfunc_13` and
`FxParameterSet::vfunc_31`. Its invalidate is conditional on a pointer mismatch
the handler would catch on its own, so it is a consistency fixup, not a
machine-type path. **[V]**

`FUN_4002da38` is the unconditional invalidate, and Ghidra lists **no** caller
for it. That is an artifact: `tools/refscan.py` finds two real
`jsr $4002da38.l`, at `0x4004319c` and `0x400431d4`, with the bytes
`4eb9 4002da38` at both. They lie in `0x40042fe2`-`0x4004335c`, a range no
entry in `function_ranges` covers, so Ghidra never built a function there and
its call table is silently empty -- a textbook case of the rule that an empty
Ghidra caller list is not evidence. **[V][C]**

#### The notification dispatcher at `0x40042fe2` **[V][O]**

`0x40042fe2` is a real entry: it opens with the movem prologue
`lea.l -$18(a7),a7` / `movem.l d2-d3/a2-a5,(a7)`, and Ghidra knows the address
only as `LAB_40042fe2`. It is a `DataChangeInfo` notification dispatcher in the
same style as `FUN_40042eaa`, running its third argument through five RTTI
checks in order, each one a
`FUN_401e2d66(info, &DataChangeInfo::typeinfo, &X::typeinfo, 0)`: **[V]**

| check | at | class | typeinfo |
|---|---|---|---|
| 1 | `0x40042ffc` | `MultipleSoundParamsChangedInfo` | `0x401f2970` |
| 2 | `0x40043050` | `SoundParamChangedInfo` | `0x401f2964` |
| 3 | `0x400430fe` | `SoundConfigChangedInfo` | `0x401f3fdc` |
| 4 | `0x40043132` | `SoundSlicesChangedInfo` | `0x401f3fe8` |
| 5 | `0x4004316c` | `SoundConfigPlayModeChangedInfo` | `0x401f3ff4` |

The two invalidate calls sit on: **[V]**

- `0x4004319c`, reached when check 5 matches `SoundConfigPlayModeChangedInfo`;
- `0x400431d4` at `LAB_400431d0`, the fallback, reached either when the info
  pointer is null (`beq.w $400431d0` at `0x40042ff8`) or when all five checks
  fail.

`SoundParamChangedInfo`, check 2, takes its own branch and reaches neither. It
calls `FUN_4002d7a4` per field, then `FUN_40035cca` twice, `FUN_400506ac` /
`FUN_4005063e` and `FUN_400da23e`, sends message code 5 through
`FUN_400d3418`, and exits. **[V]**

The dispatcher is registered, not virtual: `vtables` has no target in
`0x40042eaa`-`0x4004335c`. Instead `FUN_40044852` writes `FUN_40042eaa` and
`0x40042fe2` into the same record, at record offsets `0x54` and `0x5c`, sixteen
times in a loop over keys 0-15 (`0x40044ad6`-`0x40044b50`), and `FUN_40044e28`
references `0x40042fe2` twice more. It is installed deliberately, alongside a
dispatcher already known to be live. **[V]**

#### The frame handler drains a queue; it does not sweep sixteen tracks **[V][C]**

This corrects the reading above, and it corrects the premise of handover step
1a. The vector-191 handler does **not** walk tracks 0-15 refreshing rows. It
drains a linked list of change records and touches only the tracks those
records name. Found after a headless run of `tools/machinecommit.py` on
`out/snapshots/dt2-1.16/boot400M.snap` reached `FUN_4002d438` zero times in
three conditions, including one that zeroed the sync-cache slot on purpose.
**[V][C]**

The queue module is `0x4013a3c4`-`0x4013a7f4`: **[V]**

| function | role |
|---|---|
| `FUN_4013a408` | init: zeroes the head, lays the two static pools out as free lists |
| `FUN_4013a3f4` | `return _DAT_44e6b488` -- peek the outer head |
| `FUN_4013a3fc` | `_DAT_44e6b488 = p` -- pop / advance |
| `FUN_4013a3c4` | outer-node alloc, pops `_DAT_44e6b490` |
| `FUN_4013a52a` | record alloc, pops `_DAT_44e6b494`, zeroes words `[0]`, `[0xf]`, `[0x10]`, `[0x15]` |
| `FUN_4013a560` / `FUN_4013a5d8` | free a record / an outer node |
| `FUN_4013a6b0(rec, key)` | enqueue, sorted into a bucket by `key` |
| `FUN_4013a78a(rec)` | enqueue onto the front bucket (tag `[0] == 1`) |

Outer nodes chain through `+0x10`; each node's `+8` is the head of its record
list, and records chain through `+0x68`. The handler's `local_68` is a record
pointer walked down that inner list, not a stack buffer. **[V]**

A record reaches the per-track apply path only if its tag `[0]` is outside
`{2,3,4,5,6,7,8}`, `[1] == 1`, `[4]` (the track) is not `0x10`,
`DAT_47db4310[track] <= [5]`, the byte selected by `[7]`/`[8]` is
non-negative, and `([0xe] & 0x81) != 1`. Then: **[V]**

- `[0x10]` non-zero is passed straight to `FUN_4002d438([0x10], track)`, and
  the sync-cache slot is zeroed afterwards at `0x4002e516`;
- `[0x10]` zero falls back to `[0xf]`, and `[0xf]` zero falls back to the live
  track object `_DAT_80004704 + track*0x450 + 0x34`.

So `[0xf]` and `[0x10]` are per-record **source-object overrides**. That is the
shape of a sound lock, and it explains why the row is refreshed wholesale from
`src + 0xa2` rather than incrementally: each record can name a different source
object for the same track.

The producer is `FUN_40139878`, called only from `FUN_4011fe12`. It fills a
template in a static per-(track, slot) array, allocates a record, memcpys
`0x6c` bytes over it and enqueues it: **[V]**

```
iVar3 = param_1[0xc];
puVar7[0xf] = 0;
puVar7[0x10] = iVar3;          /* the source-object override */
...
iVar4 = FUN_4013a52a();                        /* alloc */
FUN_401360ac(iVar4, iVar9 + iVar6 + 0x414, 0x6c);  /* template -> record */
FUN_4013a78a();                                /* enqueue */
```

In `FUN_4011fe12` the value that becomes `[0x10]` is `local_14`, and it is set
three ways: zero; a cached pointer (`_DAT_44e08970`, else
`DAT_44e08930[track]`); or, when its sixth argument is below `0x80`, a direct
`param_6 * 0x450 + 0x4291377a`. Seven of the eight call sites pass the sentinel
`0xffffffff` and take the cached path. **[V]**

The eight callers are UI and playback sites -- `PatternGridView::vfunc_2`,
`TrackSwapMenuView::vfunc_2`, `KeyboardView::vfunc_2` (through
`FUN_4005c5e8`), `SoundManager::vfunc_32`, `KitActiveSettingsChangedInfo::
ctor_dtor` at `0x400d4460`, and three more. **[D]**

That the queue is therefore the trig path -- that a track's machine type
reaches the DSP when the track next sounds a note, carrying whatever source
object the trig resolves to -- is the natural reading of those callers and of
the per-record override, but it is inference from names and shape, not from a
trace. **[O]**

#### Why the first headless run said nothing **[V]**

`tools/machinecommit.py` resumes the snapshot three times and, per track,
compares the object's type byte, the SRAM row byte and TX frame offset
`0x94 + 2i` across three conditions: nothing poked; the track object's `+0xa2`
poked; and that poke plus the sync-cache slot zeroed. On
`out/snapshots/dt2-1.16/boot400M.snap`, track 0, type 5:

| condition | obj type | row type | frame `0x94` | hooks hit |
|---|---|---|---|---|
| base | 0 -> 0 | 0 -> 0 | 0, 0 | none |
| inplace | 0 -> 5 | 0 -> 0 | 0, 0 | none |
| invalid | 0 -> 5 | 0 -> 0 | 0, 0 | none |

Every pass reported `returned` and a 2050-byte frame, so the handler ran to
completion; the pokes landed, so the harness wrote what it meant to. But
`FUN_4002d438`, `FUN_4002da38`, `FUN_4002d7a4` and the dispatcher at
`0x40042fe2` were reached zero times **in the control as well**, so the run
does not show the mechanism working at all and says nothing about the
question. The tool prints `INCONCLUSIVE` for exactly this case. **[V]**

The cause is the correction above: with the queue empty on a stock resumed
boot, the handler's outer `while` never executes, and nothing downstream of it
can run however the cache slot is set. A sweep-based reading of the handler
would have called this run a clean negative result. **[C]**

#### The smallest qualified 1.15C panel gesture commits STRETCH **[V]**

`out/experiments/panel-machine-commit/qualify-1.15c-001/report.json` records
two clean and two narrowly traced repetitions of the same raw panel sequence:
FUNC+SRC opens Machine Selection, one DOWN tap selects the next entry, and YES
commits it. FUNC uses button channel 2 (`22xx`); DOWN and YES use channel 1
(`21xx`). Requested feed counts are deterministic lower bounds serviced at the
next 400,000-instruction loop boundary, not exact delivery counts. **[V]**

All four runs exited zero, touched no fault page, and produced the same
1024-byte panel (`8ba3d66b…`) and endpoint snapshot (`b467d62f…`). Each traced
run entered the commit setter `0x40035e90` exactly once with track 0 and type
2, then wrote `0x4263b1a2` from PC `0x40050d62`, `00 -> 02`. The final panel
highlights `STRETCH`. This qualifies the gesture and its real UART8/eDMA panel
path on 1.15C. It does not complete behavioral A2: these runs used the
historical global-unblock mode, and the target setter/back half is the
relocated 1.16 build. **[V]**

#### Faithful 1.16 panel input reaches the relocated setter, not refresh **[V]**

`out/experiments/panel-machine-commit/qualify-1.16-001/report.json` repeats
the same FUNC+SRC, DOWN, YES sequence twice clean and twice with narrow hooks
from one immutable 1.16 user-screen snapshot. Its retained command provenance
uses `--exact --no-unblock`, never enables `--weakptr`, and the source snapshot
has a wholly counted three-stage lineage from `boot400M.snap` through
`explore-1.16-pit3-004`, `-005`, and `-013`. All lineage stages and all four
qualification runs touched zero fault pages. **[V]**

The port required three hardware/model corrections rather than generic wait
satisfaction: the image-resolved UART8 TX state is `0x40964d74`; PIT3 alone
runs during the intro while DTIM remains held; and eSDHC CMD18/CMD25 transfers
now complete channel 59, post its resolved completion semaphore, and preserve
the card's sparse write overlay in checkpoints. The final user screen is
therefore reached with `unblock=False` and `weakptr=False`. **[V]**

All four qualification runs delivered the same eight feeds at the same actual
lower bounds, latched the panel at `53,047,610`, and saved at `80,004,410`.
Their 1024-byte panel hash is `8ba3d66b…`, and their endpoint snapshot hash is
`4327aebe…`; clean and traced endpoints are byte-identical. Each traced run
entered commit `0x40036798` and setter `0x40051712` exactly once, with track 0
and type 2, then wrote `0x4265338e`, `00 -> 02`, at PC `0x4005179e`. The raw
1.16 image bytes independently place the commit call into the setter and the
byte write at those addresses. **[V]**

The equivalent shorter qualification at
`out/experiments/panel-machine-commit/qualify-1.16-fast-001/report.json`
keeps the opening and DOWN timings but asks for YES at 46.4M rather than
52.4M and ends at 64M rather than 80M. Two clean and two traced exact runs
again have byte-identical endpoints, the same `8ba3d66b…` panel hash, zero
fault pages, and one type-2 commit/setter pair in each traced run. The panel
latched 5,616,000 instructions earlier and the actual final save was
15,849,359 instructions earlier. This is the preferred repeat recipe; it
shortens the scenario rather than increasing per-instruction emulator speed.
**[V]**

`FUN_4002d438` was reached zero times in both traced runs. Thus this closes the
real-panel provenance only through the setter; it does not connect the
setter's indirect notification to invalidation, the SRAM row, or the frame.
Behavioral A2 remains open. **[O]**

One follow-up control narrows the next missing event. From the committed 1.16
snapshot, a real TRIG 1 press/release in
`out/experiments/panel-machine-commit/explore-1.16-pit3-012/` reaches
`FUN_4011fe12` once and the record producer/enqueue pair twice, but still hits
`FUN_4002d438` zero times. At the time of that run SSI0-paced eDMA48/50,
vector 170, and `INTFRCH1` software-force delivery were unmodelled. The new
narrow model closes those mechanics, but without external RX data it remains
in the generic vector-170 synchronization handler and still does not provide
a natural refresh. This run identifies only the queue side dynamically.
**[C][D][O]**

An earlier proposed front-half control was to poke a type byte into a source
object and call `FUN_4011fe12` through
`emu/harness.py`'s
`call()` with a sixth argument below `0x80` so the record carries `[0x10]`
directly, then raise vector 191 and read the row and the frame. Confirm what
`0x4291377a + idx*0x450` actually is first -- the base is odd-aligned, which
is not the shape of a `0x450`-stride object array, so it may be a decompiler
artifact rather than a real address. The real TRIG path now reaches the
producer, so this is optional calibration, not the next behavioral-A2 test.
**[C][O]**

#### Direct-refresh control establishes source byte -> SRAM row -> TX frame **[V]**

`tools/machinecommit.py` now tests the narrow back half directly on Digitakt
II 1.16. It does **not** pretend that the unresolved setter notification fired:
each condition starts from a fresh restore, optionally changes the live source
object's type byte, optionally calls `FUN_4002d438(src, track)` through
`emu/harness.py`, then enters vector 191 three times. The final accepted report
is `out/experiments/a2-machine-provenance/a2-real-005/report.json`. **[V]**

This is calibration/control evidence for behavioral A2, not completion of A2.
The run used track 0 and source object `0x426532ec`; the selected addresses were
`src + 0xa2`, row `0x80003cd0`, and cache slot `0x8000470c`. Its immutable
inputs were: **[V]**

- SysEx SHA-256 `278541e466edcd77d6b3e018a91fb90185932d3c7de224dd3e68294dddf3a9ec`,
  equal to `out/sections/dt2-1.16/.source-sha256`;
- MAIN OS SHA-256 `57bb4dfa8df07d846adc72fdb4fb0d3cd3c5680c524bf498338460207e008e7d`;
- snapshot SHA-256 `c23b733dfb11eb0239e44b517102c9719d966e5cd344fa3363c9a16af920a9b2`.

The build contract was `unblock=False`, `weakptr=False`, no GUI fast mode,
and state-only `softfloat=True`, `bitmap=True`, `dsp=True`. Four conditions
were each repeated clean and with the bounded refresh-entry/full-row-write
observers. Every handler entry returned, every captured frame was 2050 bytes,
and clean/instrumented state, row hashes, frame hashes and stop reasons were
identical in all four pairs. **[V]**

| condition | source type after poke | direct refresh | row type after | frame type words |
|---|---:|---:|---:|---|
| baseline | 0 | no | 0 | `0, 0, 0` |
| source only | 5 | no | 0 | `0, 0, 0` |
| unchanged refresh | 0 | yes | 0 | `0, 0, 0` |
| changed refresh | 5 | yes | 5 | `0, 5, 5` |

In each instrumented refresh condition the entry hook fired exactly once at
`0x4002d438`, with stacked arguments `[0x426532ec, 0]`. The row observer saw
40 writes; it retained the first 32 by design and reported truncation rather
than silently losing the count. The complete row after-images provide the
discriminator: changed refresh versus unchanged refresh differs at exactly
row offset 0, `00 -> 05`. The corresponding complete-frame comparison differs
nowhere in pass 0 and, in passes 1 and 2, at exactly `0x95: 00 -> 05` and
`0x73d: 00 -> 01`. Thus the big-endian word at `0x94` becomes `0x0005`; the
`0,5,5` sequence is a measured one-cycle delay, not a claim about local static
send/rebuild ordering. **[V]**

The unchanged-refresh control also corrects an over-strong first acceptance
gate. A wholesale refresh legitimately changes other stale row bytes, so its
whole frame need not equal a no-refresh baseline. The discriminating A/B is
changed refresh versus unchanged refresh: those conditions execute the same
copy and differ in only `src + 0xa2`. `a2-real-001` was rejected solely by the
old whole-frame-equality gate; `a2-real-005` uses the corrected controlled
comparison and fails closed on duplicate/missing conditions, address drift,
input-state drift, inactive hooks or clean/instrumented disagreement. **[V]**

This establishes only the direct 1.16 chain
`source + 0xa2 -> FUN_4002d438 -> row byte 0 -> TX 0x94`. It does not establish
that `FUN_40051712`'s indirect notify vfunc invalidates the cache or schedules
that refresh, and it does not replace a future faithful 1.16 UI commit trace.
That front-half link remains open. **[O]**

### Digitone II 1.11 has the same machine machinery, with five machines **[D]**

Every anchor of the Digitakt II machine machinery has a Digitone II 1.11
counterpart of the same shape with different data.

- Five machine types, not seven: `0 FM Tone`/`FMT`, `1 WaveTone`/`WVT`,
  `2 FM Drum`/`FMD`, `3 Swarmer`/`SWM`, `4 MIDI`/`MIDI`. The display-name
  table is at `0x401f77f0`, the same 12-byte rows of three `char *` as
  Digitakt's, with the hint column null on every row. **[D]**
- The descriptor dispatch is `FUN_400c248e`: bound 4, stride `0x2c` -- **the
  same 44-byte descriptor as Digitakt** -- array base `0x42432b24`. Its
  out-of-range fallback `0x42432bd4` is MIDI's own descriptor, which is also
  what index 4 computes to, so MIDI is not special-cased. Rows 0-3 are
  confirmed by the registration function `FUN_400c2aea`, which `pea`s each
  machine-name string next to the matching row address. **[D]**
- The type byte is at `+0xde` of the per-track object, not `+0xa2`. The setter
  `FUN_4004cc08` reaches the object the same way, through the slot's vtable
  `+0x28`. **[D]**
- The permission check `FUN_400dc19a` has the same shape and its mask table
  `0x401f7932` is five entries of `0xffff`. The grouping helper `FUN_40059274`
  has the same shape too: synthesis types to group 1, MIDI to group 2,
  anything else to 0. **[D]**
- Two of the three name accessors, at `0x400dc358` and `0x400dc37e`, are not
  Ghidra functions at all -- they sit in the gap after `FUN_400dc332`. The
  same is true of the two Digitakt ones. Disassemble the gaps. **[D]**
- Three things Digitakt has were not found: the six-long list filter table,
  the sort comparator with its function-local `std::map` (there is no
  `stable_sort` anywhere in the image, and with five static entries in a fixed
  source order there may be no runtime sort to break), and the per-type byte
  table. A patch template must treat all three as optional. **[O]**
- Digitone II has a per-machine parameter-page layer Digitakt has no
  counterpart for: `MachineParameterPageView`, `SrcMachineParamPageCopy`,
  `FilterMachineParamPageCopy`. Its machines are synthesis engines with their
  own parameter pages, so a sixth machine there is a materially bigger job
  than an eighth on Digitakt -- a descriptor and a name row will not be
  enough. **[O]**

### The frame link on Digitakt II 1.16 **[V][O]**

- Every 1.15C address of the frame link has a 1.16 counterpart, found from
  the Version Tracking map and checked instruction by instruction in both
  images:

  | 1.15C | 1.16 | evidence |
  |---|---|---|
  | installer `FUN_4002ce4a` | `FUN_4002d4f2` | `move.l #handler,dN; move.l dN,$400002fc` and `move.b #5,$fc04c07f` in both |
  | vector-191 handler `0x4002d652` | `0x4002dd0c` | |
  | driver call at `0x4002d6ba` | `0x4002dd74` | after `pea $8000488c`, `pea $abc`, `pea $80005348`, `pea $802` in both |
  | DSPI2 driver `FUN_400cf9c4` | `FUN_400cd2bc` | |
  | pacing counter `0x4028ac90` | `0x402a1488` | one read and one write each, in the handler |
  | gate `0x4094e4f4` | `0x409664f4` | 7 references each |
  | countdown `0x4094e4f0` | `0x409664f0` | 4 each |
  | mode `0x4094e4f8` | `0x409664f8` | 7 each |
  | stop flag `0x4094e4ec` | `0x409664ec` | 2 each: the writer, and `tst.l` at `0x4002d6ce` / `0x4002dd88` |
  | stop-flag writer `0x4002d632` | `0x4002dce2` | `moveq #1,d0; move.l d0,stop; rts`, outside any function |
  | `FUN_4002d602` | `FUN_4002dcb2` | nine sites each, below |
  | MIDI flag writer `FUN_4002ed16` | `FUN_4002f3da` | `lea $80003340,a0`, indices `0x4d1` and `0x4e1` |
  | `FUN_400db9aa` | `FUN_400d92a2` | `lea $80005b50,a2`, row step `0x8e` |

  The gate variables moved by `+0x18000`, and their references are the
  same instructions in the same order. The frame tables did not move: TX
  `0x80005348` (`0x802`), RX `0x8000488c` (`0xabc`), `0x800047fc + 4*i`,
  `0x80003340 + i*0x9a`, `0x80005b50 + i*0x8e`, and the MIDI flags
  `0x80004684 + 4*i` and `0x800046c4 + 4*i`, each with 16 rows. The
  instruction pattern of the stop-flag writer occurs 9 times in each image;
  the one above is the one that writes the stop flag. **[V]**
- The nine sites of `FUN_4002d602`, with the argument pushed before each:

  | 1.15C | 1.16 | in | argument |
  |---|---|---|---|
  | `0x400323dc` | `0x40032c3c` | trampoline outside functions | 1 |
  | `0x400323f2` | `0x40032c52` | `FUN_400323e2` / `FUN_40032c42` | 0 |
  | `0x400330f6` | `0x4003395c` | task `FUN_40032f5a` / `FUN_400337ba` | 0 |
  | `0x400407d8` | `0x400410f8` | trampoline outside functions | 0 |
  | `0x400407ee` | `0x4004110e` | trampoline outside functions | 0 |
  | `0x40043692` | `0x40043fb2` | `OnScopeExit::ctor_dtor` | 1 |
  | `0x40046008` | `0x4004694a` | `OnScopeExit::ctor_dtor` | 1 |
  | `0x400fa6d8` | `0x40106c9c` | trampoline outside functions | 0 |
  | `0x400faa98` | `0x4010705c` | `FUN_400faa86` / `FUN_4010704a` | 1 |

  **[V]**
- Two things differ. The 1.16 installer also writes the frame's first word,
  `moveq #1,d0` then `move.w d0,$80005348`; the 1.15C installer does not.
  And the handler reads both MIDI flag arrays itself, `lea $80004684,a1` and
  `lea $800046c4,a0`, at `0x4002d70c` and `0x4002d720` in 1.15C and at
  `0x4002ddc6` and `0x4002ddda` in 1.16. **[V]**
- `tools/framelink.py` holds these addresses per image, looked up by the
  SHA-256 of the MAIN OS image. `tools/sharcframe.py` takes its addresses
  from it and stops on an image without a profile; `--open-gate` writes 0 to
  the profile's gate. On `snapshots/boot400M.snap` (1.15C) with three passes,
  the tool before the change with `--poke 0x4094e4f4=0`, and the new tool
  with `--open-gate` or with the same poke, capture the same frames: pass 0
  `382e008e…`, passes 1 and 2 `5cc9772c…`. **[V]**
- `tools/dspmap.py` lists the instructions that reference the frame tables,
  the gate variables and the pacing counter, with their functions and callers
  two levels up (`out/maps/`). It finds 186 sites in 19 functions on 1.15C
  and 187 in 20 on 1.16; the only new site is the installer's write. Its 115
  sites from the raw sweep on 1.16 match an independent sweep. Two sites are
  not table accesses: the DSPI2 driver's `move.w a0,(a1,d0.l*4)`, which
  Ghidra attributes to `0x80003340` while `a1` holds `0x80001bc0`, and
  `cmpa.l #$800047fc,a2` in `FUN_400dbaac` / `FUN_400d93a4`, the end of a
  loop over the 16 words at `0x800047dc`. Of the other 10 functions that use
  `0x80003340`, 8 load the base and index it by track; `FUN_4002cd38` and
  `FUN_4002d166` use fixed rows 14 and 15. **[V]**
- None of the eight Digitakt `*::updateMirror` methods reaches these
  functions within three callee levels of Ghidra's call graph, including its
  resolved indirect calls, and none of those callees references the tables.
  A method can still hand the handler data through shared state, which a
  call graph does not show. **[V][O]**

### The emulator boots Digitakt II 1.16 **[V][D][O]**

- `emu/symbols.py` resolves all seven required symbols on 1.16, and 57 of
  72 in all. Its `transport` signature matches the wrong function there,
  `0x40134200`; Version Tracking maps 1.15C's `0x40128c7c` to `0x40136268`.
  `call_sites` therefore lists 25 sites instead of 4; the cold boot only
  logs them. **[D]**
- `DT2_SECTIONS=out/sections/dt2-1.16 DT2_SNAPSHOTS=out/snapshots/dt2-1.16
  uv run python -m emu.checkpoint make
  60000000,120000000,200000000,280000000,400000000
  out/snapshots/dt2-1.16/boot Digitakt_II_OS1.16.syx` builds a 1.16 ladder
  in about 6 minutes:

  | rung | 1.15C tasks | 1.16 tasks | 1.16 PC |
  |---|---|---|---|
  | 60M | 5 | 5 | `0x401382aa` |
  | 120M | 5 | 5 | `0x4011e8bc` |
  | 200M | 5 | 5 | `0x400dc4f6` |
  | 280M | 9 | 5 | `0x401e55e0` |
  | 400M | 9 | 9 | `0x40182e0e` |

  The four late tasks, among them the Main OS task `FUN_400337ba` (1.15C
  `FUN_40032f5a`), start at about instruction 291.4M in 1.16 and 257.6M in
  1.15C. **[D]**
- `tools/bootwatch.py --frame-gate` boots from reset with write watches on
  the stop flag, countdown, gate and mode of the image's profile. In both
  versions there are six writes. The startup code clears all four at
  instruction 1,752,605-1,752,609 (`0x400004d2`, a loop in
  `FUN_400004b2`). Then the SHARC boot routine calls the helper
  `moveq #1,d0; move.l d0,gate; move.l d0,mode; bra.w <vector-191
  installer>` once: in 1.15C `FUN_4002d5b2`, called from `0x400cf320` in
  `FUN_400cef6c` at instruction 257,643,827; in 1.16 `FUN_4002dc62`,
  called from `0x400ccc18` in `FUN_400cc864` at instruction 291,415,290.
  That call site is the helper's only caller in each image. The stop flag
  and the countdown stay 0. So a normal boot leaves the gate at 1, which
  keeps the frame build off, and installs the handler right after. **[V]**
- `tools/sharcframe.py out/snapshots/dt2-1.16/boot400M.snap` (with
  `DT2_SECTIONS=out/sections/dt2-1.16`) enters the 1.16 handler
  `0x4002dd0c`, which returns after one driver call with TX `0x802` bytes at
  `0x80005348` and RX `0xabc` bytes at `0x8000488c`. With the gate closed,
  both passes send `eabdd389…`, the same as pass 0 with `--open-gate`;
  passes 1 and 2 with `--open-gate` send `d674f76c…`. Against 1.15C, pass 0
  and pass 1 each differ in one byte, offset 1: `0x01` and `0x03` in 1.16,
  `0x00` and `0x02` in 1.15C. The 1.16 installer's `move.w` of 1 to
  `0x80005348` sets that byte. **[V]** The bytes that change from pass 0 to
  pass 1 are at the same 41 places in both versions. **[D]**
- The frame capture does not use the 1.15C addresses still written as
  literals in the emulator: `PRINT` and `SWITCH_TO` in `emu/longrun.py`,
  `TX_STATE` `0x4094cd74` and `WAIT_LOOP` in `emu/edma.py`, the weak-pointer
  patch, and the addresses in `emu/panel.py`, `emu/screen.py`,
  `emu/hle.py`, `emu/serial.py` and the terminal hook in `emu/gui.py`. A GUI
  run on 1.16 would. Why pass 0 sends no track data, and what the slot words
  mean, are still open. **[O]**

### The frame capture runs; the frame build is switched off **[V][D][O][C]**

- `tools/sharcframe.py snapshots/boot400M.snap --passes 3` takes 0.5 s.
  Vector 191 holds `0x4002d652`. Each pass returns through the handler's
  `rte` and calls `FUN_400cf9c4(0x802, 0x80005348, 0xabc, 0x8000488c)` once,
  from `0x4002d6c0`. All 2050 TX bytes are zero in every pass. **[V]**
- The handler builds the frame only when the long at `0x4094e4f4` is 0
  (test at `0x4002d91a`). In `boot400M.snap` it is 1, `0x4094e4f8` is 1,
  and the per-track tables are zero. `FUN_4002d5b2` writes exactly those
  two values and is called from the SHARC boot routine `FUN_400cef6c`.
  **[V]**
- `FUN_4002d5c4` clears `0x4094e4f4`; its only caller is the console
  command parser `FUN_400cd594` (`0x400cda98`). **[V]**
- `FUN_4002d602(n)` has two branches. With n != 0 it writes 2 to
  `0x4094e4f8` and 5 to `0x4094e4f0`; the handler counts `0x4094e4f0` down
  once per pass (`0x4002ecaa`-`0x4002ecbe`) and writes 1 to `0x4094e4f4`
  when it reaches 0. With n == 0 it clears `0x4094e4f4` and `0x4094e4f0`
  with interrupts masked (`0x4002d61c`-`0x4002d630`). **[V]** An earlier
  reading here, that only `FUN_4002d5c4` clears the gate, was wrong. **[C]**
- `tools/refscan.py` finds nine call and jump sites to `FUN_4002d602`
  (96.75% of the image decoded); the argument is the last value pushed.
  Argument 1, stop after five passes: `0x40043692` and `0x40046008` in the
  two `OnScopeExit::ctor_dtor` functions (`0x40043654`, `0x40045fc4`), and
  `0x400faa98` in `FUN_400faa86` (`Waiting for SysEx`) **[V]**; and the
  trampoline `0x400323d6` **[D]**. Argument 0, open now: `0x400330f6` in
  `FUN_40032f5a` and `0x400323f2` in `FUN_400323e2` **[V]**; and the
  trampolines `0x400407c8`, `0x400407de` and `0x400fa6c2` **[D]**. Ghidra's
  call graph lists five of the nine: the trampolines are not in functions.
- The two `OnScopeExit` functions load `0x400407c8` and `0x400407de` as
  pointers (`0x400436b8`, `0x4004602e`) next to their argument-1 call, and
  `OsUpgradeMenuView::ctor_dtor` loads `0x400faa86` and `0x400fa6c2`
  (`0x400fb07c`, `0x400fb0b2`). This suggests the frame build stops while a
  kit or project change, a sample reload or an OS upgrade runs, and a
  callback reopens it afterwards. Where the callbacks are invoked was not
  traced. **[D][O]**
- `FUN_40032f5a` is a task body: `FUN_400329ee` pushes `$40032f5a(pc)` at
  `0x40032a16` and calls `0x400012c8` with a `0x28000` size and 6. **[V]**
  It references `Factory reset`, `Migrate presets`, `Update MMC Caches`,
  `MAINTENANCE MODE` and `MMC NOT IN SLC MODE`. Its `FUN_4002d602(0)` at
  `0x400330f6` follows the `Update MMC Caches` step with no branch in
  between. **[D]** It is the only argument-0 call that is not a callback, so
  it is the best candidate for opening the gate in normal use. Whether a
  normal boot runs it is not known. **[O]**
- `0x4094e4ec` is a stop flag: when it is non-zero, the handler writes 1 to
  `0x4094e4f4` and `0x4094e4f8` (`0x4002d6ce`-`0x4002d6de`). Its only
  writer, `0x4002d632` (`moveq #1,d0; move.l d0,$4094e4ec.l; rts`), has no
  static caller and no copy of its address in the image, and is not in a
  Ghidra function. **[V]**
- In `boot60M`, `boot120M` and `boot200M.snap`, `0x4094e4ec`, `0x4094e4f0`,
  `0x4094e4f4` and `0x4094e4f8` are all 0. In `boot280M` and
  `boot400M.snap`, `0x4094e4f4` and `0x4094e4f8` are 1 and the other two
  are 0. **[V]** `FUN_4002d5b2` or the end of a countdown could each
  produce that. A write watch during a cold boot answers it: the SHARC boot
  routine calls `FUN_4002d5b2` at instruction 257,643,827, and nothing else
  writes 1 to either variable (see "The emulator boots Digitakt II 1.16").
  **[V]**
- Forcing `0x4094e4f4` to 0 before a pass enters the build path and ends in
  the firmware's exception dump `FUN_4010fcae` (vector 64 at PC
  `0x400db9e0`), which executes `halt`; Unicorn reports that as exception
  257. **[D]** The instruction at `0x400db9e0`, in `FUN_400db9aa`, is
  `a891 00c6`, `mac.w D6u,D0u,(A1),D4,ACC0`: a MAC with load. **[V]** On
  those two words alone Unicorn 2.1.4 (CFV4E) stops with `UC_ERR_EXCEPTION`
  and leaves PC, A1 and D4 unchanged, where the CPU adds D6u*D0u to ACC0
  and loads `(A1)` into D4. **[V]** So the exception dump came from the
  emulator, not the firmware. **[C]**
- QEMU's MAC translation, which Unicorn 2.1.4 uses, has four faults in the
  load forms: it faults when the Ry number has bit 0 or 1 set, reads a
  data-register Rx from D2, adds for MSAC, and applies MASK to every load
  address. `patches/unicorn-2.1.4-m68k-emac-mac-load.patch` fixes them, and
  `tests/test_unicorn_emac.py` checks four load forms against the CFPRM.
  **[V]** A sweep of the two code ranges finds about 160 MAC and MSAC with
  load, about 60 of them with such an Ry; the sweep may count some data.
  **[D]**
- With that patch, `tools/sharcframe.py snapshots/boot400M.snap --passes 3
  --poke 0x4094e4f4=0` runs all three passes to the handler's `rte` with one
  driver call each and no exception; `0x400db9e0` runs 1836 times. **[V]**
  Pass 0 sends 2050 zero bytes. Passes 1 and 2 send the same bytes, 46 of
  them non-zero: `0002` at `+0x00`, `3840` at `+0xd8`, 16 slots of `0x60`
  bytes starting at `+0x11c`, `+0x17c`, ... `+0x6bc`, each `0200 0000 0200`
  then zeros, and near
  the end `4000 1130` at `+0x736`, `0008` at `+0x7dc`, `0012` at `+0x7e0`,
  `0002` at `+0x7e8`, `7fff ffff` at `+0x7f0` and `0001` at `+0x800`. **[V]**
- After those passes, rows 1 to 15 of `0x80005b50 + i*0x8e` are non-zero and
  each starts `02 00 00 00`; row 0 and the tables at `0x800047fc` and
  `0x80003340` stay zero. **[V]** Why pass 0 sends zeros, and what the slot
  words mean, is not known. **[O]**

### SHARC+ instruction tables from the public ADI manuals **[D][O]**

- `tools/sharcspec/` builds the SHARC+ instruction decode table from two
  public Analog Devices manuals: the bit-layout figures in the SHARC+ Core
  Programming Reference Rev 1.5, read from the PDF's vector drawings, checked
  bit by bit against the classic SHARC Processor Programming Reference Rev
  2.4. The method and the manual errata it found are in
  `tools/sharcspec/README.md`; the documents are listed in
  `docs/sharc/SOURCES.md`; the results are in `docs/sharc/SPEC-FINDINGS.md`.
  It came from Em's separate sharc-spec work. **[D]**
- On the 1.15C DSP main program (104,848 bytes at `0x28382670`) its decoder
  leaves 1.1% of words unknown. `tools/sharc_disasm.py` stops at its first
  unknown word, 4.4% in; forced to continue, it cannot name 9.6%. One cause is
  traced: our `15b` entry fixes 3 bits where the manual fixes 7, so 1,928
  words decode as `17b`. **[D]**
- `tools/sharc_visa_tables.py` now loads `tools/sharcspec/decode_table.json`
  and picks a form by the same rule as the sharc-spec decoder, and
  `tools/sharc_disasm.py` still stops at the first word it cannot classify.
  `tools/sharcldr.py --main` writes the final application's code, and
  `tools/sharccompare.py` sweeps a region with both decoders. The main
  programs of 1.15C and 1.16 are both 104,848 bytes at `0x28382670`, from
  the same five blocks, with the entry at offset 0. The two decoders agree
  on the length and form of all 22,147 instructions that both decode:

  | | 1.15C | 1.16 |
  |---|---|---|
  | sharc-spec decoder: instructions, unknown words | 22,148, 249 (1.11%) | 22,147, 252 (1.12%) |
  | rebuilt decoder: instructions, unknown words | 22,147, 250 | 22,147, 252 |
  | first unknown word, both decoders | `0xbb0` (2.9% in) | `0xbb0` |
  | old decoder: walk stops at | `0x122c` (4.4% in) | `0x122c` |
  | old decoder: instructions at the spec decoder's offsets | 57.2% | 57.2% |
  | old decoder: instructions it cannot name | 1,962 (9.4%) | 1,950 (9.3%) |
  | old decoder: `17b` where the spec decoder has `15b` | 1,928 | 1,926 |

  The one difference on 1.15C is the last instruction, a `21a` at `0x1998e`
  whose 6 bytes run past the region: the rebuilt decoder refuses it, the
  sharc-spec decoder pads with zeros. After `15b`, the old decoder's largest
  confusions are `5a_move` for `5b_move` (797) and `18a` for `19a` (442).
  `tools/ghidra/gen-sharc-slaspec.py` still expects the old form names
  (`8a`, `9a`, `9b`) and fails on the rebuilt table; the SHARC language is to
  be regenerated from `tools/sharcspec/ghidra/gen_sleigh.py`. A second
  check rebuilt both regions from the boot-stream blocks, counted the mask
  bits, and reproduced the sharc-spec decoder's counts, first unknown and
  last instruction with its own sweep. **[V]** The old decoder's numbers
  come from `tools/sharccompare.py` alone. **[D]**
- `tools/ghidra/install-sharc.sh` now generates the language from
  `decode_table.json` with `tools/sharcspec/ghidra/gen_sleigh.py` and
  installs `SHARC_VISA:LE:32:default`; the generated module equals the copy
  already installed from the sharc-spec work, except the stack pointer, now
  I7 as the call convention in `docs/sharc/structure-1.16.md` uses. The old
  generator and `tools/ghidra/SHARC/` are removed; the installed
  `Processors/SHARC` stays for the `dt2_SHARC` program in `~/ghidra-projects/dt2`.
  The code space has wordsize 2, so byte offset `0x382670` shows as
  short-word address `0x1c1338`. **[D]**
- `tools/sharc_import.py --seed-calls --analyze` imports section 7 into
  `~/ghidra-projects/elektron-sharc`:

  | | 1.16 | 1.15C |
  |---|---|---|
  | memory blocks (from 104 loader blocks) | 8 | 8 |
  | call targets seeded (of 546 recovered) | 282 | 285 |
  | functions after analysis | 347 | 352 |
  | instructions, all / in the main program | 5,490 / 4,684 | 5,541 / 4,735 |
  | Error bookmarks | 60 | 63 |
  | main-program instructions where our decoder starts one of the same length | 4,663 | 4,707 |
  | ... of a different length | 0 | 0 |
  | ... where our linear sweep starts none | 21 | 28 |

  The first 12 instructions at the entry match our decoder in form and
  length. Ghidra reaches about a fifth of the main program's 22,147
  instructions, so the rest needs more seeds. The Error bookmarks are
  branches into addresses outside the loaded blocks and undecodable words
  in the first application at `0x120xxx`. **[D][O]**
- `docs/sharc/structure-1.16.md` maps the 1.16 DSP program: FreeRTOS tasks, a
  task proposed as the command dispatcher for the ColdFire, and a command block
  at `0x82a00000`. These were found on 1.16 and are hypotheses. **[D][O]**
- Checked against the main programs of 1.16 and 1.15C (short-word address
  SW = `0x1c1338` + file offset / 2), with a second check:
  - A software call is the triple `3c` (raw `0x9ff2`, push R2 through
    I7/M7), `16a` (stores the goto's short-word address minus 1) and
    `25a_direct` (the goto): 45 in each version. A return is `9b_abs` (raw
    `0x083f343f`, indirect jump through I4/M6) then `25c_rframe` (`0x1901`):
    45 in 1.16, 47 in 1.15C. **[V]**
  - The RPC dispatcher task is created at SW `0x1c3f5a`-`0x1c3f6a`,
    byte-identical in both: `17a` ureg2=`0x257800`, `17b` ureg12=1000, `17a`
    ureg8=`0x2577f0`, `17a` ureg4=`0x1c3bf0`, then `25a_direct` to
    `0xb8615d` (1.16) / `0xb86159` (1.15C). ureg8 is the name "RPC
    dispatcher" at loader byte address `0x282577f0`, so data pointers add
    `0x28000000` without doubling, while ureg4, the entry, is a short-word
    address. The call target lies outside every loaded section-7 block. The
    same target creates "Audio Task" at SW `0x1c7775` in 1.16 (entry value
    `0x1c7749`) and at SW `0x1c7708` in 1.15C (`0x1c76dc`). **[V]**
  - Exactly 30 instructions in each main program carry a value in
    `0x82a00000`-`0x82a001ff` (forms `14a`, `14d`, `16a`, `17a`), at the
    same 30 short-word addresses, all in `0x1c361a`-`0x1c48c3`. **[V]**
  - Ghidra has no function at `0x1c3bf0`-`0x1c3c3e` in either program: the
    body calls through the software-call idiom, which the language models as
    plain gotos. The SPORT/DAI setup writes 34 registers at SW
    `0x1cb28e`-`0x1cb31a` in 1.16 and `0x1cb222`-`0x1cb2ae` in 1.15C. **[D]**
  - The two main programs differ in four parts: up to SW `0x1c7501` only
    single words differ; SW `0x1c74fb`-`0x1c7781` (1.16) against
    `0x1c74fb`-`0x1c7715` (1.15C), around the Audio Task creation, has
    several edits that add 108 words; the next 26,733 words are the same
    code, `0x6c` words later in 1.16; the last 18 words of 1.16 and 126 of
    1.15C are different code, so both stay 104,848 bytes. **[V]**
  - Still open: how the dispatcher selects a command (the indirect `9a_abs`
    through I6/M3 at SW `0x1c46c4` is a candidate; the `9a_rel` at
    `0x1c551a` has a fixed target), the Audio Task entry (`0x1c7749` is not
    an instruction boundary in our decode), and how the ColdFire reaches
    `0x82a00000`; the only confirmed link is the DSPI2 frame. **[O]**

### The SHARC side of the SPI frame link **[V][O]**

- `tools/sharcimm.py` lists the immediates in DSP code. It decodes one
  instruction at every even offset and marks each hit with the longest run
  of decoded instructions that ends there (depth) and with whether the
  linear sweep that steps past undecodable words reaches it. `--words` scans
  the 32-bit words of every block of a boot stream instead. Peripheral names
  are from the ADSP-2156x SHARC+ Processor Hardware Reference Rev 1.0
  (Appendix A; Table 27-2 for the DMA channels). **[D]**
- No instruction in either main program carries an SPI
  (`0x3102e000`-`0x31030fff`), SPI DMA (`0x3102d000`-`0x3102d2ff`) or SEC0
  (`0x31089000`) address. The values in `0x30000000`-`0x31ffffff` on the
  sweep in 1.16 are: 17 in the DAI0 page at SW `0x1cb28e`-`0x1cb2da` and 17
  in the DAI1 page at `0x1cb2dd`-`0x1cb31a` (the 34 SPORT/DAI setup writes;
  these pages also hold the ASRC, SPDIF and PCG registers); `14a` writes to
  PORTA+`0x30`, PORTB+`0x30`, PORTA and PORTB at SW `0x1cb25d`-`0x1cb26c`
  and to PADS0+`0x60` and +`0x64` at `0x1cb320` and `0x1cb323`; a `14a`
  write to RCU0+`0x2c` (reset control unit) at `0x1c1414`; and `17a`
  R12=`0x30c6d751` at `0x1c673e`, which is not a register. 1.15C has the
  same, `0x6c` words lower after `0x1c7781`. `14a` with d=1 writes the
  register to memory (Core Programming Reference, Type 14a). **[V]**
- `docs/sharc/structure-1.16.md` section 3b gives `0x31004000` and
  `0x3108c000` as DMA or interrupt controller candidates. They are PORTA and
  RCU0. **[C]**
- A decode at every offset finds 32 instructions with a value in
  `0x82a00000`-`0x82a001ff`. The two at SW `0x1c3806` and `0x1c4562` lie
  inside real instructions that start at `0x1c3805` and `0x1c4560`, so the
  count of 30 above stands. **[V]**
- The SPI base addresses are data. Loader block 35 (1.16: target
  `0x28269250`, 1,576 bytes; 1.15C: `0x28269240`) is not part of the main
  program. At data pointer `0x2694a0` (1.15C: `0x269490`) it holds one
  40-byte entry per SPI instance: the SPI base, the DMA TX and DMA RX channel
  bases, the SEC ids of TX DMA, RX DMA, status, error, TX DMA error and RX
  DMA error, and a zero word. The ids match the SEC table of the Hardware
  Reference (Table 6-5). Each base occurs once as a 4-aligned word in the
  stream. **[V]**

  | entry | SPI base | DMA TX | DMA RX | SEC ids |
  |---|---|---|---|---|
  | SPI0, `0x2694a0` | `0x3102e000` | `0x3102d000` | `0x3102d080` | 85, 86, 87, 88, 158, 159 |
  | SPI1, `0x2694c8` | `0x3102f000` | `0x3102d100` | `0x3102d180` | 89, 90, 91, 92, 160, 161 |
  | SPI2, `0x2694f0` | `0x31030000` | `0x3102d200` | `0x3102d280` | 69, 70, 71, 72, 156, 157 |

- The same block holds SPORT0A-SPORT7B entries from `0x26955c` (stride
  `0x28`, each with the SPORT and DMA channel base) and LP0/LP1 entries at
  `0x269468`. Block 48 holds PORTA-PORTC, PINT0-PINT2 and PADS0 register
  addresses from `0x2d6ea0`, and block 2 holds the CGU0 and CGU1 bases at
  `0x242c88` and `0x242c98`. **[D]**
- The code that receives 2748 selects the SPI2 entry. In 1.16, SW `0x1c80a0`
  is `17b` R4=`0xabc` (2748, the bytes the ColdFire receives per frame),
  then `25a_direct` to `0x1c7bd4`. That function sets R12=`0x261a10`
  (`17a`, `0x1c7c0f`) and R4=2 (`17b`, `0x1c7c12`) and jumps to `0x1c9fd5`
  (`0x1c7c14`). There: `2c` R13=R4 (`0x1c9fe7`), `17b` R2=`0x28`
  (`0x1c9fe8`), `4a` R2=R13*R2 (`0x1c9fea`), `5a` I4=R2 (`0x1c9ff0`) and
  `19a` I1=I4+`0x2694a0` (`0x1c9ffb`), so I1=`0x2694f0`. 1.15C does the same
  from SW `0x1c8034` with R12=`0x261a00`, `0x1c9f69` and the table at
  `0x269490`. `0x1c9fd5` is also reached from `0x1c7dae` with the index in
  M6 and R12=`0x261b18`, so it serves any instance. **[V]**
- What happens to 2748 after the call is not known. One reading has
  `0x1c7bd4` copy R4 to R13 before it loads 2, and `0x1c9fd5` save and
  restore R13 without using it. No write to an SPI or DMA register has been
  found yet. **[D][O]**
- 1.16 SW `0x1c1496`-`0x1c149a` is `9b_abs` (a delayed jump), `17b`
  R0=`0xabc` and `25c_rframe`. The two instructions after a delayed jump
  execute before the jump (Core Programming Reference, Table 4-7), so this
  function returns 2748. The bytes are the same in 1.15C. **[V]** Ghidra
  starts the function at `0x1c136a` and finds one caller, SW `0x1c0027`,
  outside the main program. **[D]**
- 1.16 SW `0x1c7e4f`-`0x1c7e51`, and again `0x1c7ee3`-`0x1c7ee5`, write
  `0x401` (1025) to DM(`0x26822c`), offset `0xc` of a structure at
  `0x268220`. **[V]** The structure is then passed in R8 to `0x1c834a` and
  to `0x1c83ff`. **[D]** Whether 1025 is half of the 2050-byte frame is
  not known. **[O]**
- No 4-aligned word in either loader stream equals `0x802`, `0xabc`,
  `0x401` or `0x55e`. The `0x802` in the `4a` at SW `0x1cb423` is part of
  its compute field, not a value. **[V]**

### The DSP programs of Digitakt II 1.16 and Digitone II 1.11 in Ghidra **[V][C][O]**

- Digitone II 1.11's section 7 is a boot stream of 95 blocks. Its main
  program starts at SW `0x1c12e2` and is 105,016 bytes (`tools/sharcldr.py
  --main`). It also loads 54,792 bytes at `0x8045a6c8` and 283,032 bytes at
  `0x80467cf4` in external memory; Digitakt II 1.16 loads 3,316 bytes at
  `0x8045a6c8`. `tools/sharc_import.py --seed-calls --analyze` imports it as
  `/dn2-1.11_SHARC` in `~/ghidra-projects/elektron-sharc`, with 257
  functions. **[D]**
- A software call is CJUMP followed by its two delay slots, not a push and
  store before a goto. `25a` is CJUMP, a call that "should always use the
  DB modifier" and does `R2=I6, I6=I7` (SHARC+ Core Programming Reference,
  Type 25a, p.416); a delayed branch executes the two instructions after it
  before the target, and a delayed call returns to "the seventh address
  after the branch instruction" (pp.121-122). The classic SHARC Programming
  Reference Rev 2.4 (p.238) gives the compiler's call as `CJUMP (DB);
  DM(I7,M0)=R2; DM(I7,M0)=PC` (the PC store holds the return address - 1).
  In the images, every aligned `25a_direct` is followed by a push of R2
  through I7/M7 and a `16a` through I7/M7 that stores its own short-word
  address + 2, so the return lands after the store: 589 of 589 in Digitakt
  II 1.16 (490 with `3c` raw `0x9ff2`, 99 with a 48-bit `3a`) and 551 of 551
  in Digitone II 1.11 (479 and 72). An indirect call moves I6 to R2 and I7 to
  I6, then jumps through `9b_abs` with M5 (DB) with the same push and store:
  12 sites in each image. **[V]**
- This corrects the call triple above (`3c`, `16a`, `25a_direct`, "stores
  the goto's short-word address minus 1"): the push and store belong to the
  CJUMP before them. The triple looked right because calls often follow each
  other. **[C]**
- A return is the delayed `9b_abs` jump through I4/M6 (raw `0x083f343f`); its
  two delay slots hold `25c_rframe` and one epilogue instruction, mostly `15b`
  then `25c_rframe`: 390 of 391 returns in 1.16 and 276 of 277 in 1.11
  contain `25c_rframe` in the slots. **[D]**
- `tools/sharcspec/ghidra/gen_sleigh.py` now emits Type25a as `call`. Its
  delay slots are not modelled: they follow the call in the listing.
  `tools/sharcflow.py` lists the calls, indirect calls and returns with their
  delay slots; with `--cover --analyze --save` it sets FlowOverride.CALL on
  the indirect calls, disassembles every aligned instruction Ghidra has not
  reached, starts a function after each return's delay slots and at each run
  of code no function holds, and re-runs analysis. Both programs were
  re-imported with the new language and the pass applied; copies from before
  any pass are in `~/ghidra-projects/backup-2026-09-15-sharc`. **[D]**

  | | DT2 1.16 | DN2 1.11 |
  |---|---|---|
  | aligned instructions (our decoder) | 21,270 | 20,805 |
  | main-program instructions after import / after the pass | 6,575 / 20,468 | 6,191 / 20,234 |
  | ... inside a function, after the pass | 20,443 | 19,994 |
  | aligned instructions that clash with Ghidra's instructions or data | 36 | 60 |
  | main-program functions after import / after the pass | 234 / 1,875 | 154 / 1,666 |
  | call references after import / after the pass | 287 / 914 | 225 / 866 |

- Function boundaries are still too fine: in 1.16, 168 main-program
  functions have one instruction and 806 have two to five. Two causes are
  seen, and no-return marks are not one (clearing them and recomputing the
  bodies changes nothing). The delay slots after a return become their own
  function, such as `0x1c1498` (`17b` R0=`0xabc`, `25c_rframe`). Code reached
  only through an indirect jump becomes separate functions: the RPC
  dispatcher's first piece `0x1c3bef`-`0x1c3c3e` ends in a `9b` jump at
  `0x1c3c3c`, which the language models as a return, and its cases, such as
  `0x1c3c4f`, `0x1c3d34` and `0x1c3f16`, jump back to `0x1c3c1d`. Both need
  the language: delay slots and indirect jump targets. **[D][O]**

### Delay slots as one Ghidra instruction **[D][O]**

- A throwaway copy of the generated SHARC+ language, installed as
  `SHARC_SPIKE`, models a delayed branch and its two delay-slot instructions
  as one instruction: two identical slot subtables, each a copy of every
  constructor with an empty body, follow the branch pattern, and the body
  builds both before its `call`, `return` or `goto`. It was applied to
  CJUMP, delayed `9a_abs` and `9b_abs` jumps, and delayed `8a_abs` jumps. On
  the 1.16 main program, SW `0x1c80a2` is one 14-byte instruction (CJUMP,
  `3c`, `16a`) with p-code `CALL` to `0x1c7bd4` and fall-through
  `0x1c80a9`; `0x1c2c9a` is 18 bytes (CJUMP, `3a`, `16a`); and the return at
  `0x1c1496` is 10 bytes (`9b_abs`, `17b` R0=`0xabc`, `25c_rframe`) with
  p-code `RETURN`. **[D]**
- SLEIGH limits met: one subtable cannot appear twice in a pattern; a
  subtable must be defined before it is used; bare mnemonic words in
  subtable constructors must be quoted; a field cannot be both displayed and
  constrained in one constructor. `delayslot(n)` counts bytes, so it cannot
  express two SHARC+ instructions of varying length. **[D]**
- Not yet in the generator, and function boundaries were not measured: the
  test program was seeded only from the entry and the CJUMP targets. Its
  `FUN_001c136a` stops at SW `0x1c13bc`, where our decoder and Ghidra both
  read an `8a_rel` jump to a nonsensical target (raw `00 07 3e 02 30 00`),
  with the current language as well. **[O]**

### Conditional jumps, calls and returns in the generated language **[D][O]**

- The generated language lifted every jump, call and return that has a
  `cond` field as unconditional: with cond EQ, an `8a_abs` jump was a `goto`
  with no fall-through. In the 1.16 main program 499 such instructions have a
  cond other than TRUE (`8a_rel` 404, `11a` 37, `8a_abs` 22, `9a_abs` 15,
  `9a_rel` 15, `11c` 5, `9b_rel` 1); in Digitone II 1.11, 478. **[D]**
- `tools/sharcspec/ghidra/gen_sleigh.py` now gives each of these forms two
  constructors: cond TRUE keeps the old p-code, and any other cond tests
  `condition(cond)`, a user-defined p-code op, and falls through when it
  does not hold. `tools/sharcpcode.py` measured the old and new language back
  to back, each in a throwaway project (`sharc_import.py --seed-calls
  --analyze`, then `sharcflow.py --cover --analyze`):

| | DT2 1.16 old / new | DN2 1.11 old / new |
|---|---|---|
| main-program functions after the pass | 1,875 / 1,211 | 1,666 / 1,084 |
| ... with one instruction | 168 / 114 | 124 / 94 |
| ... with two to five | 806 / 496 | 691 / 448 |
| functions the decompiler truncates at bad instruction data | 309 / 280 | 346 / 335 |
| Error bookmarks, whole program | 106 / 165 | 113 / 172 |

- The new Error bookmarks are Bad Instruction bookmarks at addresses that had
  none before, outside the main program (Ghidra shows `0012058c`, `001208fa`),
  reached through the new fall-throughs. The decompiler's "Instruction
  overlaps" warning in 1.16 went from 5 to 70. Neither is examined yet. **[O]**
- `FUN_001c136a` now has 55 instructions (38 before) and still stops at the
  `8a_rel` decode at SW `0x1c13bc`. **[O]**

### Where the new Error bookmarks and overlap warnings come from **[D][O]**

- `tools/sharcpcode.py measure --ghidra` writes `OUT/<image>.sqlite`: our
  decoder at every decodable offset of the main program (form, fields, depth,
  aligned, computed target, the pypcode lift) and Ghidra's instructions,
  references, bookmarks, functions, decompiled C and decompiler warnings. Two
  runs compare with sqlite's `ATTACH`; the queries below are
  `tools/sharcpcode.sql`. **[D]**
- The conditional-flow change does not create the new Error bookmarks. Ghidra
  reaches more code, and that code meets decode problems the old language
  never walked into. Of the 53 new bookmarks inside the 1.16 main program: 23
  flow into memory the image does not load (17 are CJUMP calls to SW
  `0xb8xxxx`, which `FUN_001c136a` makes as well; 3 are `8a_abs` calls; 3 are
  `8a_rel` jumps whose target comes out negative), 24 are "unable to resolve
  constructor" on the fall-through of a three-word instruction (mostly `21a`,
  `1a`, `2a`), where our own linear sweep stops too, and 6 are branch targets
  inside an existing instruction. Digitone II 1.11 has 52 in the same
  proportions. **[D]**
- 65 of the 70 "overlaps instruction" warnings in 1.16 are `8a_rel` or
  `9a_rel` targets one or two words inside an aligned instruction. By the form
  they land inside: `21a` 62 (54 two words in, 8 one word in), `2a` 14,
  `6a_mem` 11; in 1.11, `21a` 70, `2a` 12, `6a_mem` 8. **[D]**
- Of the 552 aligned `8a_rel` instructions in 1.16 whose target is inside the
  main program, 434 land on an instruction start; 104 of the 118 misses land
  inside one. `25a_direct` lands on a start 314 times of 318. **[D]**
- Open: `21a` decodes as a 48-bit nop matching any first word `0x0000` to
  `0x007f`, 904 of them in 1.16, and the instructions those targets land
  inside carry nonzero later words (`0010 9b52 aa01`, `0069 4dfe 0e3f`). If
  those words are shorter instructions, `21a` swallows the one or two that
  follow, which would explain both the mid-instruction targets and the
  fall-throughs that do not decode. Check Type21a and the `21a`/`22c`
  crossing resolver in `tools/sharcspec/ghidra/gen_sleigh.py` against the PRM.
  Not checked. **[O]**

### Type21a was swallowing the two words after it **[C][D][O]**

- Corrects the open item above. `Type21a` matched any first word `0x0000` to
  `0x007f` and constrained nothing in the other two words, so it absorbed the
  short instructions that followed: 904 matches in 1.16, only 44 of them the
  all-zero word the PRM draws (1.11: 31 of 898). `tools/sharcspec/build_table.py`
  now takes every bit from the figure for Type21a and Type21c, and the
  left-over first words are the provisional 16-bit `Type21p_undoc16`
  (`docs/sharc/SPEC-FINDINGS.md` 3.7). **[D]**
- Measured with `tools/sharcpcode.py measure --ghidra` before and after, both
  images, same machine: **[D]**

| | DT2 1.16 | DN2 1.11 |
|---|---|---|
| `8a_rel` branches landing on an instruction start | 434/552 -> 520/590 | 533/649 -> 625/695 |
| decompiler "overlaps instruction" warnings | 70 -> 46 | 66 -> 48 |
| "call to offcut address" warnings | 13 -> 9 | not recorded |
| aligned instructions our decoder finds | 21,270 -> 21,792 | 20,805 -> 21,361 |
| main-program functions | 1,211 -> 1,170 | 1,084 -> 1,049 |
| ... with one instruction | 114 -> 95 | not recorded |
| functions the decompiler truncates at bad data | 280 -> 296 | 335 -> 343 |
| import and analysis / the sharcflow pass, seconds | 4.2 -> 6.0 / 7.5 -> 10.0 | 4.3 -> 5.9 / 8.2 -> 11.1 |

- Aligned counts in 1.16: `21a` 904 -> 49, `21c` 336 -> 175, `23p_undoc16`
  366 -> 407, and 883 of the new `21p_undoc16`. The branch targets that landed
  two words inside a `21a` are gone; what is left lands inside `2a`, `6a_mem`,
  `14a` and `4a`, in counts of three to ten. **[D]**
- Open: Digitone II's `25a_direct` landing fell, 239/269 -> 217/276, with no
  explanation yet. Both images decode more bytes, so Ghidra meets more bad
  instruction data and analysis takes about 35% longer. 1.16 keeps 165 Error
  bookmarks but 53 of them are different ones; 1.11 goes 172 -> 182. Twelve of
  the twenty worst mid-instruction branch targets are still unexplained: their
  container decodes cleanly and the chain still misses the target, so a
  neighbouring form is wrong too. **[O]**

### Type22a is idle, and neither image contains one **[D][O]**

- The same merge rule over every other form, audited with the new
  `tools/sharcspec/audit_bits.py`: Type22a (idle/emuidle) and Type26a (`sync`)
  print every bit in their figures but matched on a nine- and sixteen-bit
  prefix, taking 220 and 2 instructions across the two images, while their
  strict words appear in neither. Both are tightened; the words they took are
  now the provisional `Type22p_undoc48` and `Type26p_undoc48`
  (`docs/sharc/SPEC-FINDINGS.md` 3.8). **[D]**
- This buys a correct name, not a better decode: measured before and after on
  both images, every flow metric is identical -- 1.16 keeps 296 functions
  truncated at bad instruction data, 165 Error bookmarks, 1,170 main-program
  functions, the same branch-target landing and the same form counts, and both
  probes keep their earlier verdicts. **[D]**
- A 16-bit reading of the same words was measured and rejected: it split the
  RPC dispatcher at SW `0x1c3c2e`, added 33 truncated functions in 1.16 and
  shifted the alignment sweep enough to change unrelated forms' counts. **[D]**
- Restoring the dropped bits on the compute forms destroys real matches
  (Type2a: every one of its 1,089 instances in 1.16, and 22% of all aligned
  instructions), so the merge rule stands for them. Type3d, Type4d and Type20a
  are neutral. **[D]**
- Open: what the Type22p and Type26p words do. 220 instructions in two images
  are decoded at a length that has never been checked against anything but
  their neighbours' alignment. **[O]**

### Instruction Types 23 and 24, and why the modern manuals skip them **[D]**

- The SHARC+ Core Programming Reference Rev 1.5 and the classic SHARC Processor
  Programming Reference Rev 2.4 both number straight from Type 22 to Type 25
  with no note explaining the gap, so `docs/sharc/SPEC-FINDINGS.md` 3.6 recorded
  Types 23 and 24 as undocumented. Three older ADI manuals, fetched into
  `docs/refs/` on 2026-09-16, close it. **[D]**
- ADSP-2106x SHARC Processor User's Manual Rev 2.1 (March 2004) and ADSP-21065L
  SHARC DSP Technical Reference Rev 2.0 (July 2003) each document **Type 23 =
  `IDLE16`** and **Type 24 = `CJUMP`/`RFRAME`** in full -- syntax, prose and
  opcode bit maps (2106x Appendix A-53 and A-54; 21065L pages 99 and 101). **[D]**
- ADSP-21160 SHARC DSP Instruction Set Reference Rev 2.1 (April 2013) records
  the renumbering that made them vanish. Table 1-21 prints
  `Type 23: Idle16 -- Not supported on ADSP-21160`,
  `Type 24: creg<->ureg -- Not documented on ADSP-21160` and
  `Type 25: Cjump/Rframe  0001 1000 0000 0100 0000 0`
  (`out/refs/ADSP-21160_isr_rev2.1/all.txt` 2390-2401). **[D]**
- So the gap is a renumbering, not an omission. CJUMP/RFRAME moved from Type 24
  to Type 25 and kept its encoding: the classic Type 24 direct-branch word
  `0001 1000 0000 0100` is bit-for-bit the `Type25a_direct` the table already
  carries. Type numbers are ADI's labels, not a field, so the vacated number
  says nothing about encoding space and there is no hole to search. **[D]**
- `IDLE16` is an ordinary 48-bit instruction. The 16 is the clock divisor --
  "the internal clock continues to run at 1/16th the rate of CLKIN"
  (`out/refs/3789835185494138226006565l_book_tr/all.txt` 3826-3830) -- not a
  16-bit encoding, so it is not a candidate for the provisional 16-bit forms.
  Its word is `000 00000 1 01` where Type 22 `idle` is `000 00000 1`, which puts
  it inside the nine-bit prefix `Type22p_undoc48` now covers. Whether any of
  those 220 words is an `IDLE16` has not been checked. **[O]**
- `creg<->ureg` is an instruction ADI acknowledges and declines to document.
  The same manual's glossary defines `creg` as "One of 32 cache entries, an
  entry consisting of a CH, CL, & CA" (`all.txt` 1423), so it moves between an
  instruction-cache entry and a universal register -- systems code, not
  something to expect hundreds of times in an audio program. Its Table 1-21 row
  carries no opcode bits at all. **[O]**

### A third opcode source: Type 7a confirmed, Type 19a's bit 39 settled **[D][C]**

- `tools/sharcspec/compare_sources.py` diffs two sources, the PRM figures and
  the classic PGR grid. The three manuals added on 2026-09-16 give a third for
  every form the classic core had, which is enough to break the ties the two-way
  diff left open in `docs/sharc/SPEC-FINDINGS.md` 3.2 and 3.3. **[D]**
- Type 7a: the PRM figure shades nine fixed bits, `000001001`, for Type 7a and
  Type 7d alike, which is why the two are indistinguishable there. Both new
  manuals print `000 00100` over bits 47-40 and make bit 39 the `G` field,
  "Selects DAG1 or DAG2" (`out/refs/3789835185494138226006565l_book_tr/all.txt`
  2414-2454; `out/refs/ADSP-21160_isr_rev2.1/all.txt` 3712-3745). Eight fixed
  bits, not nine. **[D]**
- `decode_table.json` already has this right: `Type7a` is `mask
  0xff0000000000`, `fixed_bits 8`, bit 39 unconstrained, because
  `tools/sharcspec/build_table.py` 273-275 declines to fix a bit the PRM shades
  where the PGR leaves a blank. The third source confirms that merge rule rather
  than correcting it. Type 7a decodes 311 times in 1.16 and 369 in 1.11. **[D]**
- Type 19a: bit 39 is the bit-reverse flag, `0` for `MODIFY` and `1` for
  `BITREV`, printed explicitly as a `0`/`1` pair in both new manuals
  (`out/refs/ADSP-21160_isr_rev2.1/all.txt` 5723-5745;
  `out/refs/3789835185494138226006565l_book_tr/all.txt` 3617-3670). Bits 41-39
  are `100` without bit-reverse and `101` with. That is the PGR value the PRM
  figure contradicts with `000`, so 3.2's open item closes in the PGR's favour.
  **[C][D]**

### Type19a's mask is two bits short, and 757 instructions live in the gap **[V][O]**

- `tools/sharcfields.py`, new here, is the mirror of
  `tools/sharcspec/audit_bits.py`: where that lists bits a PRM figure prints
  that the table does not fix, this tallies what values the fields the table
  *declares* actually take across the aligned instructions of both images, and
  flags any field sitting on bits the classic PGR grid fixes. Three fields
  qualify. One is large. **[D]**
- `Type19a` fixes six bits, `000101`, where its neighbours in the opcode space
  fix eight or nine: `Type18a` `00010100`, `Type19a_bitrev` `000101101`,
  `Type20a` `00010111`. The PRM's Figure 17-2 shades only six and declares bits
  41-40 a SHARC+ field `sc[1:0]` and bit 39 a field `w`, while the classic grid
  and all three older manuals fix bits 44-40 at `10110`
  (`tools/sharcspec/classic.json` "Type 19a", pattern `000101100...`). **[D]**
- So `Type19a` claims the whole `000101` block and, by longest-leading-prefix,
  keeps whatever its tighter neighbours leave: its own `10110` with `w=0`, and
  the whole of `10101`. Of its 1,935 matches across the two images, 1,178 are
  `10110 0`, the documented `MODIFY`; **748 are `10101 1` and 9 are
  `10101 0`**. **[V]**
- Bits 44-40 = `10101` is documented by nothing. The sequence runs Type 18
  `10100` (system register bit manipulation), the gap, Type 19 `10110`, Type 20
  `10111` -- in the PGR Rev 2.4 (`all.txt` 19877-19942), the ADSP-21160 ISR Rev
  2.1 (`all.txt` 5637-5741 and Table 1-19 at 2280-2321), the 21065L and the
  2106x alike. Four manual generations, no instruction at `10101`. **[D]**
- Checked against the bytes by a second agent that used no project decoder: in
  1.16, SW `0x1c14a0` is `87 15 ff ff fc ff`, frame `0x1587fffffffc`, bits 47-39
  `000101011`; in 1.11, SW `0x1c1445` is `87 15 ff ff ee ff`, frame
  `0x1587ffffffee`. A naive scan of every even offset finds 851 frames with
  `10101 1` across the two images, of which the aligned sweep takes 748 -- a
  subset, as it must be. **[V]**
- Read through `Type19a`'s field layout, those two frames give `g=DAG1,
  idis=0, is=I7, data=-4` and `data=-18`: an index register and a small negative
  immediate, the shape of a `MODIFY`. The words are structurally a sibling of
  Type 19, not noise, and they sit in the aligned sweep without disturbing it.
  **[D]**
- Two readings, disagreeing about the table rather than the bytes. Either
  SHARC+ really did widen Type 19 with a two-bit `sc` selector, as its own
  figure says, and `sc=01` is a new variant; or `10101` is a separate
  instruction `Type19a` is swallowing. Against the first: within `sc=01`, bit 39
  is `1` in 748 of 757, so if `w` were still the bit-reverse flag then almost
  every one of them would be a `BITREV`, while the documented bit-reverse form
  occurs 8 times in both images combined. **[O]**
- (`figures.json`'s `Type19a_bitrev` pattern `000101000` is not a new problem:
  bits 41-39 = `000` is exactly the PRM erratum `docs/sharc/SPEC-FINDINGS.md`
  3.2 already records, and `build_table.py` already takes the PGR value.) **[D]**
- Done, and it holds. `Type19a` now fixes the nine bits the classic grid fixes,
  `000101100` (`mask 0xff8000000000`), with its `sc[1:0]` and `w` field
  declarations removed through a new `DROP_FIELDS` table in
  `tools/sharcspec/build_table.py` -- an override there, not an edit to
  `figures.json`, which `extract_figures.py` regenerates. The provisional 48-bit
  `Type19p_undoc48` (`00010101`, eight fixed bits) takes the gap. **[V]**
- `tools/sharcpcode.py compare out/sharcpcode/t23 out/sharcpcode/t24` reports
  **0 regressions**. Aligned and decoded counts identical (1.16 21,792 of
  21,792; 1.11 21,361 of 21,361); 1,170 and 1,049 main-program functions with
  the same size histogram; 296 and 343 functions truncated at bad instruction
  data; both probes keeping their earlier verdicts; and every Error and warning
  bookmark count byte-identical -- "Bad instruction - Truncating control flow
  here" 337, "Control flow encountered bad instruction data" 296, overlapping
  instructions 46, offcut calls 9. The only movement is timing noise of a few
  per cent in both directions. **[V]**
- Only the per-form split moved, and both sums are exact: 1.16 `19a` 969 ->
  559 plus `19p_undoc48` 410; 1.11 `19a` 966 -> 619 plus 347. That is 757 words
  across the two images, the same 757 `tools/sharcfields.py` found. `18a`,
  `20a` and `19a_bitrev` are untouched, `19p_undoc48` has no undecoded words and
  no length mismatches, `tools/sharccompare.py` still has both decoders agreeing
  on every offset of 1.16, and `sharcfields` no longer flags Type19a. **[V]**
- So 757 words keep their length, their neighbours and every flow metric, and
  only stop being called Type 19. That is the strongest evidence available short
  of a manual that the split is right, and it says nothing about what they
  do. **[D]**
- Still open: what `10101` is, and what bit 39 selects within it -- 748 of the
  757 have it set. Two of the mid-instruction branch targets the handover counts
  as unexplained still land inside a `19a` (1.16 SW `0x1c866b` inside
  `0x1c866a`, and `0x1c87a0` inside `0x1c879e`), so a neighbouring form is wrong
  as well. **[O]**

### The decode table now records which classic tables it merged **[V][C]**

- `tools/sharcfields.py` guessed a form's classic.json table from its name, and
  the guess is wrong for every split form: `Type8a_rel` resolves to `Type 8a`
  where the merge really used `Type 8a #2`. `build_table.py` knew the answer and
  threw it away, so every emitted form now carries `classic_keys`, and
  `sharcfields` reads it instead of guessing. **[V]**
- It also now mirrors the merge rule: a bit counts as fixed only where every
  merged variant prints the same digit. That corrects two false positives this
  tool reported on its first run. `Type11a` merges both `Type 11a` (`00001010`)
  and `Type 11a #2` (`00001011`), whose patterns differ at bit 40 -- so bit 40
  is the digit that picks between the two tables, `build_table.py` is right to
  leave it free, and the PRM is right to call it the field `x`. Its `lr` at bit
  24 is a second such selector. Neither was a conflict; the count of fields
  sitting on bits the classic grid fixes goes 3 to 0. **[V][C]**
- No form's decode changed: masks, values, widths and fields are identical, and
  `tools/sharccompare.py` still has both decoders agreeing on all 22,886
  offsets of 1.16. **[V]**

### The Type 8a branch forms were taking words that are not branches **[V][C]**

- `audit_bits.py` reports `Type8a_abs` and `Type8a_rel` dropping seven PRM bits
  (32-27 and 25), which the handover named as where to start on the `8a_rel`
  with the nonsensical target. Restoring all of them is wrong, and measurably:
  it costs 225 aligned instructions in 1.16 and 279 in 1.11, and takes
  `Type8a_abs` from 29 matches to 6. **[V]**
- Measured per bit over both images instead, only one bit is a real
  discriminator. Bit 25 is set in 13 of `8a_rel`'s 1,382 instructions and 19 of
  `8a_abs`'s 54, and 30 of those 32 carry a target that is not a plausible
  address. The other six gap bits are each set in 7 to 20 instances, and bit 23
  -- which the wholesale fill would also have fixed, on `Type9a` -- is set in 18
  of `Type9a_abs`'s 98 and 14 of `Type9a_rel`'s 34. Those are fields in use, not
  zeros, so the PRM's digits in the branch figures' gaps are the same stale
  template values as on the compute forms (`docs/sharc/SPEC-FINDINGS.md` 3.8).
  **[V]**
- Fixing bit 25 to zero and leaving the words homeless still regressed: decoded
  instructions fell 50 in 1.16 and 71 in 1.11 and `halt_baddata` went 296 to
  298, because an undecoded word strands the alignment sweep. Giving them a
  48-bit home instead is flat. **`Type8p_undoc48`** takes bits 47-41 = `0000011`
  with bit 25 set -- one prefix for both halves, since bit 40 is Type8a's own
  abs/rel selector, kept as the field `r` -- and holds 16 instructions per
  image. **[V]**
- Measured: `compare` reports **0 regressions** against both `out/sharcpcode/t24`
  and `out/sharcpcode/t23`. Aligned counts are identical to baseline (21,792 and
  21,361), and `8a_rel` + `8a_abs` + `8p_undoc48` sums exactly to the old
  `8a_rel` + `8a_abs`: 636 + 19 + 16 = 671 in 1.16, 733 + 16 + 16 = 765 in 1.11.
  The nonsense `jump 0x3e0030` at 1.16 SW `0x1c13bc` and 1.11 SW `0x1c1366` --
  the same six bytes `00 07 3e 02 30 00` in both images -- is gone from the
  listing. **[V]**
- **Corrects the handover.** The probe `returns 2748` does not fail because of
  that word, and `FUN_001c136a` does not stop there: it decompiles to the same
  57 instructions before and after, and removing the bogus branch leaves the
  probe exactly as it was. 2748 is `0xabc`, loaded by the `17b` at SW
  `0x1c1498`, which sits in the delay slot of the `9b_abs` return at
  `0x1c1496` (`0x083f343f`, the I4/M6 idiom). The probe cannot pass until the
  language models delay slots. It is blocked on stage 1, not on the decoder.
  **[V][C]**
- Open: what the `Type8p` words are. 32 across two images, several of them
  byte-identical in both (`0x062207100000`, `0x062207300000`, `0x06220f8e0002`,
  `0x07110f840003`), so they are real shared code, not misalignment. **[O]**

### A generated subtable with no user **[V]**

- Adding a fixed bit to a split branch form made `sleighc` warn "Unreferenced
  table `target_pcrel_6b_w0`", and `tests/test_sharc_pcode.py` caught it.
  `gen_sleigh.py` calls `get_target_subtable()` once per branch form before it
  knows whether that form's constructors will need an `extra_by_word` variant
  instead; when every sharer of a shape takes a variant, the plain one is left
  with no user and nothing prunes it. The generator now emits only the
  subtables its constructors reference, matching on whole identifiers because
  `target_pcrel_6b_w0` is a substring of `target_pcrel_6b_w0_v2`. **[V]**

### The two decoders disagreed on one word, past the end of the file **[V]**

- `tools/sharccompare.py` had them agreeing on all 22,886 offsets of 1.16 but
  differing by one on Digitone II 1.11: `unknown only in ours 1`. It is the last
  word of the image. `out/sharc/dn2-1.11-main.bin` is 105,016 bytes and ends on
  an all-zero word at byte offset `0x19a36`, where only two of the six bytes a
  48-bit instruction needs exist. **[V]**
- Both decoders zero-pad the missing words before matching, and an all-zero
  frame trivially satisfies `Type21a`, whose figure fixes every one of its 48
  bits. `tools/sharc_disasm.py` 108 checks the matched form's width against how
  many real words were read and refuses; `tools/sharcspec/sharc_decode.py`'s
  `linear()` had no such check and reported a phantom 48-bit instruction sitting
  on four bytes that are not in the file. It now makes the same check. **[V]**
- It predates today's work: the same single offset and form reproduce with
  HEAD's committed `decode_table.json` in a scratch tree, so none of
  `Type8a_abs`, `Type8a_rel`, `Type8p_undoc48`, `Type19a` or `Type19p_undoc48`
  is involved. 1.16 never showed it because its image does not end that way.
  **[V]**

### Every measurement here was of a stale language until it wasn't **[V][C]**

- `tools/sharcpcode.py measure` compiles the slaspec sitting in
  `tools/sharcspec/ghidra/SHARC_VISA/data/languages/` (`sharcpcode.py` 66,
  215-235). Nothing regenerates it, and it is git-ignored (`.gitignore` 34), so
  editing `decode_table.json` leaves it behind with no sign. **[V]**
- The guard that exists cannot catch this. `sharcpcode.py` 791-796 compares the
  compiled `.sla` against the one installed in Ghidra -- but a stale slaspec
  compiles to the stale `.sla` that is installed, so the two agree and the run
  proceeds. Runs `t24`, `t25`, `t26` and `t27` all recorded
  `slaspec_sha256 93c238502b…`, written at 11:17:10, before `Type19p_undoc48`
  and `Type8p_undoc48` existed. Their Ghidra numbers -- function counts,
  bookmarks, decompiler warnings, probe verdicts -- are of a language without
  those forms. **[C]**
- Their decoder numbers are unaffected: `aligned`, the per-form counts,
  `tools/sharcfields.py` and `tools/sharccompare.py` all read
  `decode_table.json` directly. Lengths agreed by luck -- the stale `Type19a`
  and `Type8a_rel` are 48 bits wide, like the forms that replaced them -- which
  is why nothing tripped. **[V]**
- `tests/test_sharc_pcode.py` 56-67 regenerates into a temp directory on every
  run, excluding the `SHARC_VISA` output tree from its copy. That is why the
  test caught the unreferenced-table warning and four measurements did not.
  **[V]**
- Measured properly, after `tools/ghidra/install-sharc.sh` and a fresh
  generation (slaspec `ca2362aa…`, 80 constructors), against the session's
  starting point `out/sharcpcode/t23`: **0 regressions**, and the flow metrics
  improve. **[V]**

| | DT2 1.16 | DN2 1.11 |
|---|---|---|
| "Bad instruction - Truncating control flow here" | 337 -> 323 | 414 -> 399 |
| functions truncated at bad instruction data | 296 -> 289 | 343 -> 336 |
| Error bookmarks | 165 -> 160 | 182 -> 178 |
| main-program functions | 1,170 -> 1,169 | 1,049 -> 1,048 |

- `FUN_001c136a` loses its `halt_baddata()` call and both warning comments, so
  removing the bogus branch did repair that function -- it still cannot satisfy
  the `returns 2748` probe, which needs the delay slot. `8p_undoc48` now emits
  no p-code where those same words used to emit `goto`/`call` as `8a_rel`.
  **[V]**
- `measure` now regenerates into a temp directory and refuses when the slaspec
  on disk is not what `gen_sleigh.py` produces from the current table, and
  records `decode_table_sha256` in `lint.json` so a past run can be audited.
  **[V]**

## Emulation

Function-level works well and is the practical path. Full boot was pushed as far
as it would go; four blockers were cleared, **two of them Unicorn defects**:

| # | blocker | nature | distinct addrs after |
|---|---|---|---|
| 0 | no exception dispatch / `rte` / interrupts | emulator | 444 |
| 1 | `FF1.L` at `0x401112de` — undecodable by Capstone, unimplemented in Unicorn | **emulator** | **36,474** |
| 2 | `USR8` bit 2 (TXRDY) poll, `0xEC070004` — **UART8** | hardware | 37,395 |
| 3 | `movec d0,Rc=0x009` at `0x400cf7e4` — aborts the Unicorn process (SIGABRT) | **emulator** | — |
| 4 | `DSPI0_SR` RXCTR poll, `0xFC05C02C` — **DSPI0** | hardware | 38,193 |

Peripherals identified from NXP *MCF54418RM* Rev. 5 (Table 1-4; ch. 40 §40.3.4–
40.3.7; ch. 41). Only eight distinct peripheral registers are read in the whole
boot, and peripheral setup is read-modify-write that works fine against zeros. **[V]**

### The SPI flash needs no physical dump — correction

An earlier reading of stall 4 concluded the firmware wanted "real bytes off an
SPI device we have no image of", implying a hardware dump was required. **That
was wrong.**

`0x401296fe` is the SPI NOR read routine, signature `read(offset, len, dest)` —
confirmed by `0x84020003 -> DSPI0_PUSHR`, whose low byte `0x03` is the NOR READ
command. Its callers scan the ELE3 section table at flash offset `0x80020`. The
flash content it wants at boot **is the staged OS container**, which is exactly
what `dt2.container` decodes out of the `.syx` we already have.

High-level-emulating that one function and backing it with the container (see
`emu/flashboot.py`) makes boot progress immediately, and the reads it issues
confirm the model is right: **[V]**

```
off=0x080000 len=32      -> 0x44e4d67c    ELE3 header, into the exact address
                                           MAIN OS compares at 0x40128b8c
off=0x080020 len=16  x5                    the five section-table entries
off=0x19be60 len=184844  -> 0x45020a90    section 7 = the SHARC DSP blob
```

Coverage 36,483 -> 37,627 distinct addresses, and the firmware is now loading the
DSP image. The next frontier is the ColdFire<->SHARC link (DSPI2/eDMA), not
another storage problem.

Note the UI draws into a `Bitmap` object (`SoundBrowser::drawMain(Bitmap&)`),
so rendering the screen is a matter of locating that buffer once boot gets far
enough — not of reverse-engineering a display controller. **[O]**

What a physical dump *would* still be needed for: the +Drive contents (samples,
projects), which live elsewhere in flash and are not required to boot.

Also unresolved: `m68k` `SR` must be written **before** `A7`, or the stack
pointer lands in the banked register the CPU is about to stop using. Cost an
hour; noted in `emu/harness.py`.

## Open questions

- ~~Does MAIN OS's USB upgrade path reject a same-version image?~~ **ANSWERED
  -- no.** The comparison is a hard-coded **build-number floor**, not a
  comparison against what is running, so a same-version image passes. It was not
  found by searching for the version string because it reads the **build**
  string at ELE3 `+0x08`, through a register rather than an absolute address.
  Read on 1.16 (`0x400d9e4c`, floor `"006/"` = build >= 0060); see "MAIN OS has
  a second gate" above, which includes a byte pattern for confirming it on
  1.15C. **[V on 1.16, [O] on 1.15C]**
- Is the bootstrap rewrite atomic once triggered? **[O]**
- Sample/project/preset on-flash layout. `MmcFs`, `/factory`, and the manager
  classes are visible; the partition and directory format is not mapped. **[O]**
- `ERROR_headerVersion_wrong` and friends in MAIN OS are the **LZ4 frame error
  enum**, not OS versioning — a false lead worth recording.

## Display

The panel is **128 x 64, 8 bits per pixel**, read straight out of the firmware's
own structures rather than guessed. **[V]**

`intro_dither::px_copy_to_bitmap(PixelData&, Bitmap&)` at `0x400d315e` — the
binary carries the *demangled* signature as an assert string at `0x401f7ef0`,
which makes it an unusually good anchor. From its argument handling and copy
loop:

```
Bitmap      +0x04  width
            +0x08  height
            +0x0C  stride -- 32-bit words per COLUMN
            +0x10  pixel data pointer
PixelData   +0x00  width
            +0x04  height
            +0x08  8bpp row-major source buffer
```

Pixels are **column-major, 1 bit per pixel**, 32 rows packed per big-endian
word, MSB = lowest y:

    word_index = x * stride + (y >> 5)      bit = 0x80000000 >> (y & 31)

recovered from `Bitmap::setPixel` at `0x40104eb4`. An earlier draft of this
document called `+0x0C` a setPixel function pointer; that was wrong -- it came
from a different routine at `0x400d3244` where the register did not hold a
Bitmap. The panel is 1bpp, not 8bpp: the 8bpp `PixelData` is a greyscale source
thresholded on the way in.

The intro's `PixelData` instance lives at `0x4028ae98` and reads
`width=128, height=64, buffer=0x43139290`. The loop bound `cmpi.l #$2000`
(8192 = 128x64) at `0x400d3640` corroborates it, as does the runtime struct,
which reads back `0x80, 0x40, 0x43139290` under emulation.

**Not yet captured: actual pixels.** The buffer is allocated at runtime and is
still all zeros after 80M instructions — boot parks in DSP init before the intro
renders. Two ways forward, neither attempted:

1. Progress boot past the ColdFire<->SHARC handshake so the UI runs naturally.
2. Call the intro renderer at `0x400d3372` directly. It has no direct callers
   (`jsr (a2)`, `jsr (a5)` — invoked via lambda/vtable), so a harness would have
   to supply the allocator and callback registers. **[O]**

Note the drawing path is pure ColdFire — `MainScreenView`, `SoundBrowser::drawMain(Bitmap&)`,
and ~140 item-renderer lambdas of shape `(int, Bitmap&, int, int, bool)`. Nothing
in it needs the DSP, so route 2 should not require the SHARC at all.

### Rendering firmware graphics without booting **[V]**

`emu/screen.py` runs the device's own drawing code and captures the output. The
mechanism, and one correction to the note above:

`px_copy_to_bitmap` does **not** dispatch through `Bitmap+0x0C`. It loads a
fixed address into `a4` and calls that: `0x40104eb4` is the real
`Bitmap::setPixel(Bitmap*, x, y, value)`. Intercepting that address captures
every pixel without needing to know how `Bitmap` stores them. (`+0x0C` is used
by a *different* renderer at `0x400d3220`, so the harness intercepts both.)

The source is thresholded to 1 bit on this path — `cmpi.l #$80` then `shi.b` at
`0x400d31d8` — so an 8bpp greyscale `PixelData` becomes a monochrome Bitmap.

Validated against ground truth rather than by eye: feed a synthetic 40x16 source
through the firmware routine and the captured pixels match the thresholded input
exactly, 640 setPixel calls for 640 pixels. `./venv/bin/python emu/screen.py selftest`

This matters because a wrong `PixelData` renders as *plausible dither* rather
than failing — several structs found by heuristic scanning are false positives.
Locating genuine static image assets is still open. **[O]**

### Text rendering — still open **[O]**

Not found yet, and the obvious routes came up empty:

- 27 distinct functions call `setPixel`; **none walk a string** (no `move.b (aN)+`
  over a char buffer), so text must go through a glyph blitter one character at
  a time, called from a higher-level loop.
- No standard 5x7 or 6x8 bitmap font table is present (searched for the
  distinctive `'!'` glyph `00 00 5F 00 00` and variants).
- `PopupWindow(const Bitmap*, ...)` and `VerticalMenuView(const char*, int,
  std::string, const Bitmap*, int)` show `Bitmap` is used as an icon type, but
  scanning for static instances in the recovered layout finds none - icons are
  constructed at runtime, so the glyph/icon data is probably stored compressed
  or generated.

The rendering harness itself is done and verified, so once a text routine is
located it can be driven immediately.

### Why full boot stalls — the real reason **[V]**

The RTOS task created at boot (entry `0x400cef6c`, stack `0x4000`, priority 1)
**is the DSP bring-up task**. Walking the call tree upward from the ColdFire<->SHARC
transport lands exactly on it:

```
0x40128c7c   transport: write arg -> DSPI 0xFC074004, kick 0x841b -> 0xFC074000,
             then block on a semaphore the completion ISR would signal
  <- 0x400cf000, 0x400cf928, 0x400cfd8a, 0x4012d46e   (4 call sites)
    <- 0x400cef6c   the task entry itself
```

The transport installs an ISR at vector 97 (`0x40000184`), enables INTC sources
`0x21`/`0x1d`, starts the transfer and waits. Firing the completion interrupt by
hand gains only ~330 addresses; stubbing the transport outright gains nothing and
just moves the stall to `0x400cf956`. Four call sites means DSP bring-up is a
**stateful conversation**, not one transfer.

This is a genuinely different wall from the SPI flash. There, the data we needed
already existed in the `.syx`. Here the ColdFire is waiting on replies from a
processor with no open emulator, so the responses would have to be *synthesised*
from a protocol nobody has documented. Boot cannot complete without that, and the
UI task presumably never starts because DSP init never finishes.

**Update (next session): the "do not pursue" call above was too pessimistic.**
The DSP handshake did not need real SHARC replies synthesized -- it needed the
*ColdFire-side* synchronization primitives satisfied, which turned out to be
inspectable and fakeable without modeling the SHARC at all. See "DSP bring-up
-- session 2" below: `task_create` sites reached went from 4/16 to 10/16 this
way. The capability table remains accurate for what *doesn't* need booting:

| capability | status |
|---|---|
| CRC-32 oracle (`0x80001bd0`) | works, byte-exact |
| aPLib depacker (`0x80000432`) | works, validates a 3.1 MB repack |
| firmware graphics rendering (`emu/screen.py`) | works, pixel-exact vs ground truth |
| Bitmap framebuffer encode/decode | works, verified two independent ways |
| full boot to UI | in progress -- 10/16 `task_create` sites reached, see below |

## DSP bring-up -- session 2

Starting point: 4/16 `task_create` (`0x400012c8`) call sites reached, ~37,627
distinct code addresses, stuck inside the DSP-transport wait at `0x40128c7c`.
Ending point: **10/16 `task_create` sites, 58,337 distinct addresses.** New
tooling: `emu/dspboot.py` (instrumented boot harness, reusable) and a scoped-hook
speedup added to `emu/harness.py`. All of it verified by running, not inferred.

### Blocker 1 (cleared): the transport is a mutex+semaphore wrapper, not an RPC

Re-reading `0x40128c7c` disassembly line by line (not just skimming) shows it
is **not** "send a request word, wait for a reply word" as the earlier session
guessed. It is:

```
lock mutex @0x44e4d6a4                          (0x400015a0)
  (first call only: install ISR @0x40128c4c at vector 97,
   enable INTC sources 0x21/0x1d, init completion sem @0x44e4d69c to 0)
write timeout(!) -> 0xFC074004
write control word 0x841b -> 0xFC074000          (kicks the transfer)
jsr 0x4000141a   (sem_pend on 0x44e4d69c)        <-- blocks here
unlock mutex (tail call into 0x400016d2)
```

The value each of the 4 call sites pushes (`0xF4240`, `0x3E8`, `0x64`,
`0x2DC6C0` = 1,000,000 / 1,000 / 100 / 3,000,000) is a **microsecond timeout**
written to hardware, not a command/payload word -- it is never read back by the
ColdFire side. The real completion signal is the semaphore at `0x44e4d69c`,
which the would-be completion ISR at `0x40128c4c` posts to via
`0x4000155c -> 0x400011ee`.

Critically, `0x4000141a` (sem-pend) has a fast, non-blocking path: if the
semaphore's count field is already `>0`, it clears it and returns immediately
without ever calling the scheduler (`trap #0`) -- and **every one of the 4
callers discards its D0 return value** (overwritten immediately after the
call), so nothing downstream ever checks "did the transfer really succeed."

**Patch**: at `PC == 0x40128d08` (the `jsr 0x4000141a` instruction itself),
write `1` into the 4 bytes at `0x44e4d69c` before it executes. No interrupt
firing, no scheduler re-entry, no `rte` -- just pre-satisfying the flag the
very next instruction is about to check. This is different from, and more
surgical than, the earlier session's attempts (firing vector 97/33/29 by hand,
or blanket-stubbing the whole transport function), which is presumably why
those only gained ~330 addresses or moved the stall without progress.

Result: the one transport call site actually reached at this point in boot
(`0x400cf928`, timeout `0x3E8`) goes through. Two more polled hardware status
registers immediately downstream needed the same treatment, discovered by
running and reading what changed at the new stall PC:

- `0xEC03802C` bit 31 (`0x400cf956`, a byte-at-a-time TX loop unrelated to the
  already-known UART8/DSPI0 mocks -- a *second* status register pair,
  `0xEC094018`/`0xEC03802C`, spent uploading what is very likely the SHARC ADI
  loader blob byte-by-byte after the handshake succeeds)
- `0xFC05C02C` bit 28 / RFDF (`0x40129da2`) -- same DSPI0 status register
  already mocked for RXCTR, but a *different* bit, tested by a second,
  synchronous SPI0 read routine reached only after the transport unblocks

Both mocked the same way as the pre-existing UART8/DSPI0 mocks: force the
polled bit permanently set in `Machine.mmio`.

### A red herring that turned out to be correct behavior, not a bug

Past the above, boot hit a second embedded aPLib-style depacker at
`0x4012ab70` (distinct from the bootstrap's `0x80000432`, and from the
flash-section-table depacker used by MAIN OS's own boot-time decompression --
this one runs on in-memory buffers during DSP bring-up). One invocation
(source `0x402489b4`, a *static address inside the already-loaded MAIN OS
image*, not flash- or DSP-reply-dependent) appeared to run away: 2.6M+ hits at
the same 3 addresses (`0x4012acba/bc/be`, its copy loop) with zero new code
coverage for tens of millions of instructions -- classic infinite-loop
signature.

It is not one. Register tracing (dump D2/D3/A1 on every entry to the copy
loop) showed match lengths never exceeding ~2KB; the "stall" was thousands of
small, legitimate tokens through a tight loop, which the coverage-based stall
heuristic cannot distinguish from a hang because it only tracks *new* PCs, not
forward progress within a loop. Independently ruled out a Unicorn MVZ/MVS
decode bug (the specific opcode class flagged as broken in Capstone) by
testing `mvz.b`/`mvs.b` in isolation, register and `(a0)`/`(a0)+` addressing --
all matched 68k semantics exactly. Given more instruction budget (400M+) this
depacker completes normally and boot proceeds. A defensive safety valve was
added anyway (clamp the copy count if it ever exceeds 64K, `DEPACK_COPY` in
`emu/dspboot.py`) but it has never actually fired -- included for whatever
comes next, not because it was needed here.

**Lesson for next time**: before concluding a repeating-PC "stall" is a hang,
dump the actual loop-bound register(s) a few times. A slow-but-finite loop and
an infinite one look identical to a coverage-only heuristic.

### Blocker 2 (cleared, and generalized): idle spins need timer ticks too

With the above fixed, boot progressed in a large burst: task_create sites went
4 -> 5 -> 9 -> 10 as longer instruction budgets were tried (47M, 257M, 416M).
Between two of those bursts, boot hung again -- this time truly, 139M+
instructions with zero new coverage, at `0x400cf3e0`.

`0x400cf3e0` disassembles to `bra.b $400cf3e0` -- a literal self-branch. This
is the **exact same idiom** as the already-known `HALT` idle loop
(`0x400ceeb6`, also `bra.b $self`), which the harness already fed periodic
timer ticks (vector 32) to keep the RTOS scheduler moving. But the harness only
ever ticked *that one hardcoded address* -- a second thread/task's own
idle-wait-for-scheduler point at a different address got no ticks at all, so
once execution reached it, nothing could ever preempt it.

**Fix, generalized rather than special-cased**: scanned all of MAIN OS for the
opcode `0x60FE` (`bra.b -2`, i.e. branch-to-self) -- 13 occurrences total --
and feed periodic timer ticks to *all* of them, not just the one instance
someone happened to hit first (`find_idle_spins` in `emu/dspboot.py`). This is
exactly the kind of fix the diverging/converging distinction in the task brief
calls for: a class of blocker, not a single address.

This got two of the three known idle points working correctly (`0x400ceeb6`
and `0x400cf3e0` both now receive ticks and both did unblock at least once,
confirmed by `spin_by_addr` counters saturating at clean multiples of
`tick_every`).

### Where it stands now: a new, different kind of stop

After the burst that reached 10/16 (last new task at instruction ~416M,
`0x400f1a6c` -> entry `0x400f1eb6`, prio 2), execution parked at `0x400cf3e0`
and stayed there for the rest of a 1.4B-instruction run -- ~48,000 further
timer ticks, zero new coverage.

Traced precisely (not just inferred from the address repeating): `0x400cf3e0`
sits between a one-shot guard and a permanent idle trap in the *same* function
that produces 3 of the burst's 4 tasks:

```
400cf3d6  moveq #$40,d0
400cf3d8  and.l $40288190.l,d0
400cf3de  beq.b 400cf3e2                 ; bit clear -> do the work (it was clear: confirmed 0x40288190=0x04 at runtime)
400cf3e0  bra.b 400cf3e0                 ; <-- idle trap, same idiom as HALT
400cf3e2  jsr 0x4011311c                 ; wrapper containing task_create site 0x401131d2 (prio 7)
400cf3e8  jsr 0x4014635e                 ; wrapper containing task_create site 0x4014638e (prio 5)
400cf3ee  jsr 0x400329ee                 ; wrapper containing task_create site 0x40032a20 (prio 6)
400cf3f4  bra.b 400cf3e0                 ; done -- park here forever by design, same as HALT
```

So this is **not** a guard repeatedly failing -- it passed once, did its
one-shot job (matching the 3 near-simultaneous hits at instructions
257710408/257710485/257710555), and then deliberately loops to the same idle
trap as its designed terminal state, exactly like `HALT`. There is nothing
further for *this* thread to do; it is functioning correctly. This resolves
what looked like an open question in an earlier draft of this note.

The real open question is why **no other** thread creates any of the
remaining 6 `task_create` sites even after tens of thousands of scheduler
ticks. The newly-created prio 2/5/6/7/8 tasks are themselves candidates to be
the ones that would create more (or not -- they may simply be leaf worker
tasks). Two live hypotheses, neither confirmed:

1. One of the **other three** DSP transport call sites (`0x400cf000` timeout
   `0xF4240`, `0x400cfd8a` timeout `0x64`, `0x4012d46e` timeout `0x2DC6C0`) is
   what some other task is blocked on -- all session, only one of the four
   (`0x400cf928`) has ever been exercised (`transport calls: 1` in every run).
   If a task is parked in a *real* semaphore wait (`trap #0`, correctly
   descheduled by the RTOS) rather than a self-branch idle loop, our idle-spin
   fix does not apply to it -- it needs the same treatment as blocker 1
   (satisfy whatever it is actually waiting on), not more timer ticks.
2. The remaining 6 sites are in code that is reachable only through a
   different subsystem-init path this cascade never calls into at all under
   this configuration (not blocked -- just not on the current call graph).

**Checked, and it points at hypothesis 1.** For each of the 6 tasks created
after the first burst (prio 7/8/7/5/6/2), coverage tracking shows the RTOS
scheduler *did* switch into every single one of them -- their entry addresses
are all in `seen`, each followed by a small additional cluster of newly-hit
addresses (6 to 31 distinct addresses within a few hundred bytes of its entry
point). So `task_start` is not merely called on all 10 tasks
(`task_start hits: 10`, already known) -- the scheduler genuinely gave CPU
time to the 6 newest ones, each ran a handful of real instructions, and then
every single one went quiet with no further coverage growth for the rest of
a 450M/1.4B-instruction run. That is the signature of each one reaching its
own short init sequence and then hitting a genuine blocking wait very
quickly -- not of the scheduler failing to reach them (hypothesis 2, now
effectively ruled out) and not of them running unboundedly (they are not
CPU-bound). **Concrete next step**: for each of these 6, disassemble the
handful of instructions right past where its coverage cluster ends -- that
boundary is exactly where each one blocks, and is a small, bounded amount of
code to read per task (nothing like the earlier multi-hundred-instruction
transport functions).

### Convergence assessment

Up to 10/16: **converging**. Each fix (semaphore fast-path, two MMIO bit
forces, generalized idle-spin ticking) unlocked either the next blocker or a
burst of several `task_create` sites at once, and the depacker "stall" that
looked alarming turned out to be a false alarm resolved by patience, not a
patch. Past 10/16: **stalled, not diverging** -- one clearly-identified
address, no new blockers appearing, but the fix used for the last two blockers
(generic timer ticking) is confirmed insufficient here and the next fix needs
actual tracing of `0x40288190`'s producer(s), most plausibly tied to one of
the three still-unexercised transport call sites.

### Performance: a 2x+ harness speedup, reusable

`emu/harness.py` and `emu/dspboot.py`'s original hot path ran a single global
`UC_HOOK_CODE` callback on *every instruction*, which for the FF1/MOVEC
patches did a `mem_read` + `struct.unpack` unconditionally to check "is this
one of the two rare opcodes" -- on every single instruction of the run, not
just the rare ones. `Machine.install_isa_patches_scoped` (new) pre-scans the
image once for the actual FF1 (`0x04C0`-`0x04C7`, 232 hits) and MOVEC
(`0x4E7A`/`0x4E7B`, 7 hits) opcode addresses and registers a Unicorn hook
scoped to each exact address (`begin=addr, end=addr`) instead. `emu/dspboot.py`
does the same for its own instrumentation points (`fast=True`, the default;
`fast=False` keeps the original global-hook path for cross-checking). Measured
on identical 60M-instruction runs: 146s -> 73s wall-clock, same result
(verified byte-for-byte identical task_create hits, addresses, priorities).
This matters because runs at the scale needed here are 400M-1.4B instructions
(9-25+ minutes each even with the speedup).

### Reusable artifacts from this session

- `emu/dspboot.py` -- the instrumented DSP bring-up harness. Reports
  `task_create` sites reached (with entry/priority/tcb), transport call sites
  hit, semaphore-satisfy count, depack-clamp count, idle-spin addresses found
  and hit counts, distinct-address coverage curve, and stall PCs. Run directly:
  `./venv/bin/python -m emu.dspboot <instruction_limit> <patch_sem 0|1>`.
- `emu/harness.py` -- added `Machine.install_isa_patches_scoped`, a drop-in,
  much faster alternative to `install_isa_patches` for long runs.

### Next blockers, named **[V]**

After reaching 10/16 tasks, the remaining ones start and then go quiet. Measured,
not guessed:

1. **They are not blocked on semaphores.** There are two counting-semaphore pend
   primitives, both with a fast path when count > 0: `0x4000141a` and
   `0x400013a6`. Logging every call to both across 150M instructions finds
   **exactly one** — the already-patched transport wait at `0x40128d0e`. So the
   stalled tasks are not waiting on anything; they are not being scheduled.
   (`emu/blockers.py` does this logging.)
2. **The scheduler is hand-cranked.** Injecting `trap #0` (vector 32 — the
   task-switch trap) is the only thing that helps. Compared at 60M instructions:

   | injected vector | tasks | distinct addrs |
   |---|---|---|
   | 32 (trap #0) | 5 | 38,247 |
   | 66 | 2 | 329 |
   | 221 | 2 | 260 |
   | 222 | 2 | 260 |

   The candidate hardware ISRs installed during init (vectors 221/222/66 →
   handlers `0x400019bc`/`0x400019e6`/`0x40001a10`) are **not** the system tick.
   So we are forcing context switches rather than running a real scheduler.

**The prize is identified.** Task `0x400d3fb6` (prio 7, created at instr 47M) is
the **intro/animation task**: it calls a RNG at `0x40144bd8`, compares results
against `0x7fdf` and `0x3ffe`, and selects among static structs at
`0x4028ae5c`/`0x4028ae6c` — the same neighbourhood as the known intro
`PixelData` at `0x4028ae98`. If that task runs, it draws, and `emu/screen.py`
can capture the result.

**So the next blocker to attack is the scheduler itself**, not another
peripheral: find the real tick source, or drive the context-switch path directly
against the ready-list/TCB structures so tasks round-robin properly. **[O]**

### Snapshots — stop replaying boot **[V]**

Chasing each blocker meant re-running from the entry point: ~32M instructions of
identical setup (the memory-clear loop alone is ~8M) before reaching anything
new, then 15M more to the next event. Every experiment paid that toll.

`emu/snapshot.py` + `emu/checkpoint.py` remove it:

```
./venv/bin/python -m emu.checkpoint make 40000000 snapshots/boot40M.snap   # once
./venv/bin/python -m emu.checkpoint resume snapshots/boot40M.snap 5000000  # thereafter
```

Checkpoint creation: **42 s**. Resume + 5M instructions: **6.9 s**. Only non-zero
pages are stored (22 of 134 mapped), so 23 MB of live memory compresses to
1.8 MB on disk.

**Resume must happen inside `dspboot.run`, not onto a bare Machine.** The first
attempt restored state onto a fresh `Machine` with only the base hooks, which
silently dropped the flash HLE, the semaphore patch and the scheduler tick — and
diverged by ~50 addresses over 5M instructions while looking plausible. Fixed by
`restore_into()`, which loads onto an already-hooked Machine. Verified: snapshot
at 40M + 5M resume gives **coverage identical** to a straight 45M run (37,616
addresses both ways).

Snapshots are firmware-derived state, so `snapshots/` and `*.snap` are gitignored.

`make` takes a comma-separated ladder and saves them all in **one** pass, since
saving only reads state and emulation continues afterwards:

```
./venv/bin/python -m emu.checkpoint make 60000000,120000000,200000000,280000000
```

One 5-minute pass produced:

| checkpoint | distinct addrs | tasks | on disk |
|---|---|---|---|
| 60M  | 38,247 | 5 | 1.9 MB |
| 120M | 39,244 | 5 | 2.0 MB |
| 200M | 42,234 | 5 | 2.3 MB |
| **280M** | **47,335** | **9** | 2.4 MB |

Resuming from 280M reaches 9 tasks in **10 seconds**.

Two things that immediately became visible once iteration was cheap:

- The apparent "stall" at `0x40175288` is **`__mulsf3`** — a softfloat multiply
  (23 shift-and-add iterations = float mantissa). The stall metric flags any hot
  address after a window with no *new* coverage, so ordinary hot arithmetic looks
  like a hang. Not a blocker.
- Boot is **still progressing** past 280M, just slowly: 47,335 -> 47,637 distinct
  addresses over 60M further instructions, arriving in bursts. Not deadlocked.

### The scheduler was never running **[V]**

With cheap iteration, the actual state at the 280M checkpoint turned out to be
much simpler than "many blockers":

- **Zero context switches in 20M instructions.** The tick only fired at idle-spin
  addresses, and a *busy* task never reaches one. So one task held the CPU
  outright and the other eight never ran.
- **Preemption must respect the interrupt mask.** An earlier attempt at periodic
  `trap #0` injection crashed the machine (`pc=0`). The cause was injecting
  regardless of `SR`; a maskable interrupt cannot fire at IPL 7. Skipping
  injection when `(SR & 0x0700) == 0x0700` makes it stable — 100 injections,
  99 switches, no crash.
- **Scheduling is priority-based, not round-robin.** The ready-list cursor at
  `0x4094c914` points at a node whose `->next` is itself, i.e. a single ready
  task. Lower-priority tasks starve until the running one blocks — and our mocks
  are precisely what stop it blocking.

**The task holding the CPU is the intro task** (`tcb=0x43135210`, prio 7). It is
not stuck: outside the softfloat library it sits at `0x400d3900`, inside
`intro_dither`, doing per-pixel float work with the constants `0x3c000000`
(= 1/128) and `0x3f800000` (= 1.0) — consistent with normalising x across the
128-pixel-wide panel. It is generating the boot animation, just very slowly:
software floating point, per pixel, at ~300k emulated instructions/sec.

The known intro framebuffer at `0x43139290` is still all zeros after +100M, so
either a frame has not completed or the output goes to a different buffer.
Finding it is the next step — watch writes issued from the `0x400d3xxx` code
range. **[O]**

### Where the intro writes its pixels **[V]**

Watching writes issued from the `0x400d3xxx` code range (via the new `pre_start`
hook on `dspboot.run`) gives two destinations:

| destination | writes | what |
|---|---|---|
| `0x43135000` | 161,621 | the intro task's own stack (`tcb=0x43135210`) — not output |
| **`0x44f52000`** | 1024 per 4KB page | **the pixel work buffer** |

1024 longword writes per 4KB page means every word is written. The footprint is
**128 x 64 x 4 bytes = 32,768 bytes** — a float per pixel, matching the softfloat
work and the `1/128` normalisation constant.

Reading it back after +120M from the 280M checkpoint: **7,925 of 8,192 floats
non-zero**, and rendering with a relative-intensity ramp shows clear structure
with mirror symmetry — a smoothly varying field, consistent with `intro_dither`
generating a dither/noise field rather than a finished logo.

So the pipeline appears to be: generate a float field at `0x44f52000` -> combine
with a source image -> threshold into a `Bitmap`. The values are very small in
absolute terms (both min and max print as 0.0000 at 4dp), so this is an
intermediate, not the final image. **[O]** The `Bitmap` at `0x43139290` is still
zero, so the threshold/copy step has not run yet in emulation.

### The intro buffer is a particle array, not a framebuffer **[V]**

The 32,768-byte buffer at `0x44f52000` is **not** floats, despite sitting next to
heavy softfloat use. Only byte 3 of each 32-bit word is ever non-zero, so these
are small big-endian integers. Splitting them by parity settles it:

| | range | meaning |
|---|---|---|
| even indices | 0..127 | **x** — panel width |
| odd indices | 0..63 | **y** — panel height |

It is an array of **4,096 (x, y) particle positions** — the state of the boot
animation, which is what `intro_dither` animates. Rendering the captured buffer
plots all 4,096 in range and shows clear left-right mirror symmetry.

So the chain is: animate particles at `0x44f52000` -> rasterise into the 8bpp
`PixelData` at `0x43139290` -> `>>2` into `0x43137290` (loop at `0x400d3628`)
-> `px_copy_to_bitmap` -> `Bitmap`. Both 8bpp stages are still zero in emulation,
so the rasterise step has not run yet. **[O]**

Note this corrects the previous entry, which read the buffer as floats and
described it as a dither field. The values that made it look like a smoothly
varying field were coordinates.

### The firmware's draw path has not executed — timeboxed negative **[V]**

Two traces from the 280M checkpoint, 100M instructions each:

- **No reads of the particle array** at `0x44f52000` — so nothing has consumed
  the animation state yet.
- **No writes to either 8bpp buffer** (`0x43139290`, `0x43137290`).
- **`Bitmap::setPixel` (0x40104eb4): 0 calls. `px_copy_to_bitmap` (0x400d315e):
  0 calls.**

So the intro task is still in its *compute* phase — animating particles — and the
rasterise/draw stage begins later, or waits on something not yet satisfied. No
callable entry point for it was found, so per the agreed timebox this stops here
rather than becoming another grind.

What we do have: the animation state itself is readable and renderable
(4,096 particles plotted on the real 128x64 geometry), and the final stage
(`px_copy_to_bitmap` -> `Bitmap` -> decode) is independently verified pixel-exact
by `emu/screen.py`. Only the middle link — particles to 8bpp raster — is missing,
and it is missing because it has not *run*, not because it is not understood.

### Emulation is 9x faster than it was **[V]**

The per-instruction Python hook used for coverage tracking capped throughput at
~300k instr/sec. Almost none of it was necessary: every HLE side effect lives at
a known address, and Unicorn hooks registered with `begin == end` cost nothing
between hits. The one thing that appeared to need a global hook — the preemption
tick, fired on an instruction count — doesn't: `emu_start(pc, 0, count=N)`
returns after N instructions, so the tick can be driven by *chunking* instead.

`emu/fastrun.py`: **2.72M instr/sec**, a 9x improvement. A billion instructions
is now ~6 minutes rather than an hour.

### The firmware has a serial command console **[V]**

MAIN OS carries a serial command console, dispatched by a `strcmp` chain at
`0x400cd93e` onward. The handshake is `#HELLO` -> `HOW DO YOU DO?`; the rest
of its command set is factory service functionality and is not listed here.

Key handles:
- **`0x400054b4` is the print function.** Hooking it captures all console output
  regardless of transport — no UART modelling needed.
- The console is a **task**, entry `0x400cd594`, priority 2 — one of the 16
  `task_create` sites, and one our boot has not reached.
- UART8 is modelled properly in `emu/console.py` (USR8 `0xEC070004` with real
  RXRDY/TXRDY, data register `0xEC07000C` popping queued input and capturing
  output) rather than pinned to a constant.

Starting the console task manually from a snapshot runs but yields almost
immediately into an idle spin, so it needs more of the system up first. **[O]**

## Making the emulator actually run -- session 3

Marks: **[V]** verified in this session, **[C]** corrects an earlier claim,
**[O]** open.

### The scheduler never worked, and one line explains it **[V][C]**

Every run before this one scheduled exactly **one task**. The stated ceiling in
`docs/history/upstream/NEXT.md` -- "1 new task per ~250M instructions, and the gaps are
widening" -- was not a property of the firmware. It was this bug.

`harness.raise_vector` pushed the *current* PC into the exception frame. The
RTOS yields with `trap #0`, and Unicorn reports a trap with PC still pointing
**at** the trap instruction. So every task that blocked in `sem_pend` got a
stack frame that resumed onto its own `trap #0`. The instant the scheduler
restored it, it trapped again. Tasks could block but could never wake.

The evidence is direct: `emu/tasks.py` decodes each TCB's parked PC, and
before the fix all eight blocked tasks sat at `0x40001486` / `0x40001414` --
the `trap #0` instructions themselves. After it they sit at `0x40001488` /
`0x40001416`, the `move.w d0,sr; rts` that follows.

`raise_vector` now takes `from_instruction=True` from the interrupt hook and
advances the pushed PC by 2 when the faulting word is `0x4E40-0x4E4F`.
Asynchronous injections still push the interrupted PC, which is correct.

**Distinct TCBs scheduled: 1 -> 5.** `emu/oracle.py` and
`emu/screen.py selftest` both still pass.

### TCB layout, from the context switcher **[V]**

`0x40000410` gives it away:

    movea.l $47d9adb4,a0        ; current TCB
    movem.l d0-d7/a0-a7,$c(a0)  ; registers at TCB+0x0C
    move.l  -4(a7),$2c(a0)      ; => a0 at +0x2C, a7 at +0x48
    movea.l $4094c914,a1        ; ready-list cursor
    movea.l (a1),a0 ; movea.l (a0),a0   ; TCB+0x00 = next pointer

A parked task's PC is on its own stack: ColdFire pushes two longwords,
`[a7]` = format/vector/SR and `[a7+4]` = PC. `emu/tasks.py` prints the whole
table plus the ready list from any snapshot.

**Priorities run low-number = low priority.** prio 0 and 1 are the init/idle
tasks; the real work is at 5-10.

### `0x400cf3e0` is not an idle spin needing ticks **[V][C]**

`emu/dspboot.py`'s comment calls it "a different task/thread's idle point"
that was blocking progress. It is actually where the prio-1 init task **parks
after finishing its work**, reached by the `bra.b` at `0x400cf3f4` at the end
of its main loop:

    400cf3e2  jsr $4011311c
    400cf3e8  jsr $4014635e
    400cf3ee  jsr $400329ee
    400cf3f4  bra.b $400cf3e0     ; -> bra self

Feeding it timer ticks does nothing, because it is a *ready* task at priority
1 and the scheduler correctly keeps choosing it. It parks there because
everything above it is blocked.

### A boot-mode flag word at `0x40288190` **[V]**

Two bits of it gate real behaviour in the init task, and its value in every
snapshot is `0x00000004`:

| bit | test site | effect when set |
|---|---|---|
| 5 (`0x20`) | `0x400cf386` | creates and starts the **serial console task** |
| 6 (`0x40`) | `0x400cf3d8` | falls into `bra self` at `0x400cf3e0` -- deliberate halt |

Bit 5 clear is why the console task never existed. Setting it before the init
task reaches `0x400cf384` creates it:
`TASK entry=0x400cd594 prio=2 tcb=0x40383e58`.

Note bit 6 is a *halt*, not a hang: `beq` past it is the normal path. The
earlier reading of `0x400cf3e0` as an idle spin conflated the two.

### The six task_create sites that never fire **[V]**

`0x400cd594` has no absolute reference anywhere in MAIN OS -- it is pushed
PC-relative (`pea.l $400cd594(pc)`), which is why searching for the address
found nothing. Reading the entry operand out of each unreached site:

| site | entry | prio | |
|---|---|---|---|
| `0x400ced72` | `0x400cd594` | 2 | serial console |
| `0x401135a8` | `0x401136ee` | 3 | |
| `0x401279e8` | `0x40127c78` | 4 | |
| `0x40127a7c` | `0x40127d9e` | 4 | |
| `0x40127960` | `0x40127b24` | 5 | |
| `0x40125fde` | `0x4012606a` | 6 | |

### Every task waits on a device event that never happens **[V]**

With the trap fix in, tasks block properly -- and then all of them block, on
semaphores that only real hardware would post. `dspboot` already force-satisfies
one such semaphore (the DSP transport completion sem). Generalising that to
*any* pend whose count is <= 0 is `longrun.build(unblock=True)`.

Sweeping all ~20 installed device ISRs and injecting each one wakes nothing:
the two that look like timers (`0x400cf424` vec 65, `0x400cf450` vec 68)
dispatch a one-shot callback pointer that is null, so they are timeout slots,
not the event source.

### The panel draws **[V]**

With `unblock=True` from `boot400M`, `Bitmap::setPixel` executes for the first
time in this project: **688,128 calls = exactly 84 frames of 128x64**, all into
one Bitmap object at **`0x4313b298`** -- the panel framebuffer instance, which
was previously unknown. The rendered frame is the Elektron logo.

`emu/frame.py` captures it and writes a PNG. This is firmware code drawing
through the firmware's own `setPixel`; nothing about the raster is
reimplemented.

Note this also settles the older open item: the intro's rasteriser does run,
and reaching it needed no new entry point -- only a scheduler that works.
`unblock=True` does change semantics (nothing ever really waits), so
inter-task ordering under it is not the hardware's.

### Resume fidelity: the chunk-boundary tick was corrupting runs **[V][C]**

`longrun.spin` injected a vector-32 trap at every 500k-instruction chunk
boundary. Vector 32 *is* `trap #0`, the scheduler yield, so this forced a
reschedule in the middle of arbitrary code. A run resumed from `boot200M` then
never reached the init task's own flag test at `0x400cf384`, while
`dspboot.run(resume_from=...)` reproduced the from-entry timeline exactly
(task creations at n=257642531, 257710409, 257710486, 257710556, 416346994).

`spin()` no longer ticks by default. `build()` instead installs the two
behaviour hooks it had been missing -- the depack copy clamp and the idle-spin
ticks -- so it now matches `dspboot.run` while staying ~2x quicker.
This is trap 4 in a subtler dress: the hook set, not just the Machine, has to
match the run that produced the snapshot.

### Emulation is 3.2x faster again **[V]**

`longrun.build` was calling `install_isa_patches` (a Python callback on every
instruction) where the snapshots had been made with
`install_isa_patches_scoped`. Switching to scoped: **0.79 -> 2.80M instr/sec**,
with byte-identical state (same PC, `ff1=120821`, `movec=4`, same particle
count). `isa='global'` remains available.

### The console blocker, named exactly **[V][O]**

With bit 5 set the console task starts, runs **22 instructions**, and blocks --
never reaching the UART read or the `strcmp` dispatcher. The last instruction is

    400cd5e6  pea.l $40388eac.l
    400cd5ec  jsr   $40001928.l      ; queue-receive on the console input queue

`0x40001928` is a ring-buffer queue receive. Reading it:

    a2 = queue (0x40388eac)
    d2 = a2 + 8                 ; the semaphore, 0x40388eb4
    loop: if 4(a2) == 0 { pend(a2+8); repeat }
    ...  head/tail at 0x1c(a2), mask at 0x10(a2), buffer at 0x14(a2)

So the console needs an *item enqueued*, not merely a semaphore post -- posting
`0x40388eb4` via the firmware's own post primitive (`0x4000148c`, driven from a
synthetic ISR) lets the pend return, but `4(a2)` is still 0 so it loops
straight back. Confirmed: 0 instructions of console-task code execute.

`0x40388eac` has only three static references, all inside the console task and
its own creation, so the producer reaches the queue through a pointer --
most likely the object at `0x40303e50` registered at `0x400cd5a8`
(`jsr $40110592`) right before the receive loop. **That registration is the
thread to pull next.** **[O]**

Also worth noting for whoever picks this up: the console protocol words
start with `#` (`#HELLO`) -- not `help`.

## Why the emulator was slow: 93% of it was soft-float **[V]**

The GUI ran at ~1.3 firmware frames/sec. Profiling found the cause is not the
harness at all -- a minimal machine (scoped ISA patches + mmio + exceptions)
runs at 2.16M instr/sec and the full hooked machine at 2.19M, so **every hook
in this project is free**. Chunk size makes no difference either.

> **Corrected 2026-09-13.** This section used to end "~2.2M instr/sec is
> simply what Unicorn's m68k core does here." That is wrong, and it steered
> later decisions. ~2.2M is what the core does *when `count=` is passed to
> emu_start*, which Unicorn implements by counting every instruction and
> which breaks TB chaining. The same machine with the identical hook set and
> the identical stop mechanism runs at **15.5M instr/sec uncounted -- a 7.6x
> tax**, and a cProfile of the running configuration puts **99.5% of wall
> time inside emu_start** with every Python callback in this project under
> 0.5% combined. The hooks really are free; the ceiling was never the core.
> `longrun.spin(fast=True)` buys it back for interactive use, at the cost of
> timers landing on a block boundary rather than an exact instruction. The
> paragraph below -- "the only way to go faster is to execute fewer
> instructions" -- followed from the wrong premise.

So the only way to go faster is to execute fewer instructions. An exact PC
histogram over the intro says where they go:

| region | share |
|---|---|
| `0x40174000-0x40176000` (soft-float) | **93.2%** |
| everything else | 6.8% |

This ColdFire build has no hardware FPU, so every float operation is a
libgcc-style routine, and the particle animation is float-heavy.

### Identifying the routines, rather than guessing

Entry points were found by watching which addresses execution *enters* the
region at (transitions from outside it), then identified by calling each one
with known values and comparing against real arithmetic:

| entry | routine | share |
|---|---|---|
| `0x40175204` | `__mulsf3` | 46.3% |
| `0x40174f1c` | `__subsf3` -- `bchg.b #$1f,$8(a7)` then falls into add | 22.1% |
| `0x40174f22` | `__addsf3` | |
| `0x40175346` | `__divsf3` | 4.5% |
| `0x40175a94` | `__fixsfsi` (float -> int, truncate) | |
| `0x40174134` | `fabsf` | |
| `0x40175834` | float compare -> -1/0/1 | |

The other hot addresses in the region (`0x40175644`, `0x4017550a`, ...) are
internal helpers of these, so intercepting the entries removes them too.

### The firmware's float routines are not IEEE-754 **[V]**

Comparing a native implementation against the firmware's own code found
systematic disagreement, all at the edges: the add returns **-0.0 on exact
cancellation** where IEEE gives +0.0, and it gets **infinity signs wrong**
(`-1.0 - inf` yields `+inf`). `__fixsfsi` returns `0xFFFFFFFF` for
out-of-range input, which is undefined behaviour in C.

Rather than replicate those quirks, `emu/softfloat.py` intercepts **only the
fast path** -- finite arguments producing a finite, normal, non-zero result --
and falls through to the real routine for everything else, which then defines
the answer by construction. Same shape as the sem_pend patch, which takes the
primitive's own fast path instead of reimplementing it.

For normal values this is not an approximation: computing in float64 and
rounding once to float32 gives exactly the correctly-rounded float32 result
for +, -, * and /, since 2*24+2 = 50 <= 53 bits. `uv run python -m emu.softfloat`
checks all seven routines against the firmware's: **1,774 intercepted cases,
0 mismatches**, 1,154 edge cases deferred.

### Then setPixel became the bottleneck **[V]**

With the float work gone, the soft-float region fell to 1.9% and the top cost
became `Bitmap::setPixel` (`0x40104eb4`) plus `getPixel` (`0x40104f80`) at
~54% combined -- the rasteriser touches all 8,192 pixels per frame and reads
many back. Both are small, fully understood bit-twiddlers, HLE'd in
`emu/hle.py`. Two details matter: the bounds comparisons are **signed**, and
the value is tested with `btst.b #0`, so **val=2 clears a pixel**. The HLE
writes the same bits into emulated memory, so anything reading the bitmap back
sees identical state. Verified: 680 cases, 0 mismatches.

### Result

| configuration | fps | instructions for 12 frames | |
|---|---|---|---|
| all emulated | 1.32 | 20,750,000 | 1.0x |
| + soft-float HLE | 2.37 | 9,250,000 | 1.8x |
| + bitmap HLE | 4.06 | 3,750,000 | **3.1x** |

Frames are **pixel-identical** across all three, which is the gate that makes
the optimisation trustworthy.

What is left is mostly the rasteriser itself (`0x400d3d7e` 39%, `0x400d3bea`
12%) -- the firmware logic the whole exercise exists to watch, so HLE'ing it
would defeat the point. Another ~17% is scattered math worth maybe 1.2x more.

Both HLEs are **off by default** in `longrun.build`. They are bit-exact so
program state evolves identically, but instruction *counts* change, and too
much here depends on a resumed run matching the run that made its snapshot.
`emu/frame.py` and `emu/gui.py` opt in; `FAST=1` turns them on for the
`emu.longrun` CLI.

The GUI's own redraw was measured at 0.94ms (~2% of a core) and was never the
problem; it now skips redrawing when no pixel changed, and reuses one zoomed
image instead of allocating per frame.

## The intro is meant to run at 15.00 fps, and the bus clock is 132 MHz **[V]**

"How fast should it be?" is answerable exactly, not by eye.

**The draw loop is paced by a semaphore, not by how fast it can go.** The task
at `0x400d3fb6` ends in:

    400d402a  jsr (a2)              ; render one frame  (a2 = 0x400d3e94)
    400d402c  tst.b d0
    400d4030  pea.l $43131200
    400d4036  jsr (a3)              ; a3 = 0x400013a6, sem_pend
    400d403a  bra.b $400d402a

and `0x43131200` is posted by the ISR at `0x400d2d70`, installed at vector 208
(`move.l #$400d2d70,$40000340` at `0x400d3a5a`), which acknowledges PIT3 and
calls sem_post. **One PIT3 interrupt = one frame.**

PIT3 is configured at `0x400d3a7a`: `PCSR = 0x0936` (PRE=9, so prescaler
2^10 = 1024), `PMR = 0x2191` = 8593, then `PCSR |= 9` (EN|PIE). One frame is
therefore `(8593+1) * 1024 = 8,800,256` bus cycles.

**The bus clock comes from the UART, not a guess.** The serial init computes
its baud divider at `0x400024a4`:

    4000245a  lsl.l  #5,d0          ; baud * 32
    400024a4  move.l #$07de2900,d2
    400024b0  divs.l d0,d2          ; divider = f_bus / (32 * baud)
    400024ce  move.b d2,$ec07001c   ; UBG2

`0x07DE2900` = **132,000,000**, and the ColdFire UART divider is exactly
`f_bus / (32 * baud)`, so that constant is f_sys/bus clock.

It cross-checks against all four PITs landing on round rates, which is what
makes 132 MHz trustworthy rather than merely plausible:

| timer | PMR | prescaler | bus cycles | period | rate |
|---|---|---|---|---|---|
| PIT0 (RTOS tick) | 41249 | 64 | 2,640,000 | 20.0000 ms | **50.0000 Hz** |
| PIT2 | 17187 | 128 | 2,200,064 | 16.6672 ms | **59.998 Hz** |
| PIT3 (intro frame) | 8593 | 1024 | 8,800,256 | 66.6686 ms | **14.9996 Hz** |

So the boot animation runs at **15 fps** on hardware, the RTOS tick is 50 Hz,
and PIT2 is a 60 Hz something. PIT1 is set up at `0x40128d34` in the DSP
transport path.

### What that says about the emulator

The GUI reaches ~4.2-4.7 fps, i.e. **~30% of real time**, and the status line
now reports it that way instead of leaving it to the eye.

Arithmetic for closing the gap: real hardware runs ~1.73M instructions per
frame; with both HLEs on we execute ~312k. At Unicorn's ~2.2M instr/sec that
is ~7 fps of headroom before hook overhead, and we measure 4.2-4.7. Reaching a
true 15 fps needs ~147k instructions per frame. The remaining scattered math
(~17%) is worth maybe 1.3x; past that the cost is the rasteriser itself
(~51%), so matching real time would mean reimplementing the very thing the
emulator exists to watch.

### unblock=True removes the pacing -- and distorts boot **[V]**

Because `unblock=True` satisfies *every* wait, it satisfies the frame
semaphore too: the animation runs unpaced rather than at 15 fps.

Excluding `0x43131200` and driving vector 208 from a modelled PIT3 was tried
and **does not work on its own**: the ISR fires and the semaphore count climbs
(observed reaching 33), but the draw task never runs, because with every other
wait satisfied the prio-6 task never yields and the scheduler never
reschedules. Faithful pacing needs cycle accounting so the 50 Hz RTOS tick can
preempt as well. `longrun.build` now takes `unblock_except` for whoever picks
this up.

Worth noting: leaving the frame semaphore unsatisfied changed the boot path
and created **two further tasks**, including `0x4012606a` (prio 6) -- one of
the six that never appear under blanket unblock. That is more evidence that
blanket unblock distorts boot, and a hint for reaching the remaining tasks.

## What we were overlooking about ColdFire: eDMA **[V]**

Feeding bytes to the UART model in `emu/console.py` could never have produced
console input, because **the firmware never reads UDR8 to receive**. UART8
receive is done by eDMA channel 34, with no CPU involvement per byte.

Checked and ruled out first: the PIT counter registers (`PCNTR`, `0xFC08x004`)
are **never read** by MAIN OS, so they do not need modelling. eDMA is a
different story -- 14 channels are configured.

From the init at `0x40002516`:

    TCD34.SADDR  = 0xEC07000C     ; UDR8, fixed (SOFF = 0)
    TCD34.ATTR   = 0x0050         ; DMOD = 10 -> destination modulo 1024
    TCD34.DADDR  = 0x4FE1A000     ; a 1024-byte ring
    TCD34.NBYTES = 1              ; one byte per request

and the ISR at `0x40001f1a`, vector 154:

    idx  = [0x4094CDA4]                        ; consume index
    base = [0x4094CD84]                        ; ring base
    while base + idx != [0xFC045450]:          ; DADDR = live write pointer
        byte = ring[idx]; idx = (idx + 1) & 0x3FF
        [0x4094CDB4](byte)                     ; registered callback

The ATTR decode (destination modulo 1024) matches the ISR's `andi.l #$3ff`
exactly, which is what confirms the reading.

**The channel's own DADDR register is the producer pointer**, polled by the
ISR. So injecting input needs no general eDMA emulation -- write into the
ring, advance DADDR with the same modulo, raise vector 154. That is
`emu/serial.py`, and it works: feeding `#HELLO\r\n` drives the RX callback
exactly 8 times, the consume index advances 0 -> 8, and the bytes are enqueued
onto the serial message queue at `0x47D9ADC0` (count 6 -> 8).

TCD35 is the matching transmit channel; the ISR at `0x40001d00` (vector 180)
is UART8 **transmit** only, pulling from a ring at `0x4094CD80`.

### The remaining console blocker, one step further on **[O]**

Nothing drains `0x47D9ADC0` -- it already holds 6 unconsumed messages before
any input is injected. Its consumer is the task at **`0x401136EE` (prio 3)**,
one of the six that never get created. It is created lazily by the singleton
at `0x401134CC` (guard `0x44F1E070`, allocation via `0x401114A8`) on first use
of the serial service, and nothing in our boot ever asks.

`emu.serial.create_serial_task` runs that initialiser and the task **is**
created (`entry=0x401136ee prio=3 tcb=0x44dfccb4`, an 11th task). It has not
been observed draining the queue yet -- created is not the same as started and
scheduled, and that is the next thing to check (whether `task_start`
`0x40001314` runs for that TCB, and whether the scheduler ever selects it).

So the chain is now fully mapped and only its last link is missing:

    DMA ch34 -> ring 0x4FE1A000 -> vector 154 -> callback 0x40110F20
      -> queue 0x47D9ADC0 -> [task 0x401136EE, not draining]
      -> queue 0x40388EAC -> console task 0x400CD594 -> dispatch 0x400CD93E

### Other ColdFire details worth knowing

- `raise_vector` does not set SR on exception entry. Real ColdFire sets S,
  clears T and, for interrupts, raises the mask. Most ISRs here begin with
  `move.w #$2700,sr` themselves, but the PIT3 ISR at `0x400d2d70` does not, so
  this is a latent reentrancy difference rather than a proven bug.
- The exception frame's format field is written as 0; ColdFire uses 4 for a
  normal 2-longword frame. Harmless here because `rte` is implemented by hand
  and ignores it.

## The MMIO hook was global too **[V][C]**

The earlier conclusion "every hook in this project is free" was wrong, and
wrong for a methodological reason worth remembering: it compared a *minimal*
machine against a *fully hooked* one, but `install_mmio()` was in **both**, so
its cost cancelled out and never appeared in the comparison.

`install_mmio` registered `hook_add(UC_HOOK_MEM_READ, on_read)` with no
`begin`/`end` -- a Python callback, plus a loop over the mmio dict, on **every
memory read the firmware makes**. Exactly the same mistake as
`install_isa_patches`, which had already cost 3.2x.

There are only three MMIO addresses, so one narrow hook each:

| configuration | throughput |
|---|---|
| global hook (as shipped) | 2.15M instr/s |
| scoped per-address hooks | **2.86M instr/s** (1.33x) |
| no MMIO hook at all (ceiling) | 2.91M instr/s |

Scoped lands within 2% of the ceiling, so this is the whole of that cost.

**But it barely moves the GUI**, and that is the interesting part: on the
fully-emulated path it is worth 1.33x (1.32 -> 1.50 fps end to end), while on
the softfloat+bitmap HLE path the GUI actually runs it is worth ~3% (3.99 ->
4.10 fps). With HLE we execute 5.5x fewer instructions, so there are far fewer
memory reads to tax, and the bottleneck has moved from TCG to Python callback
dispatch -- ~88k HLE calls per 12 frames. Further speed has to come from
making those callbacks cheaper or fewer, not from removing more hooks.

Ordering hazard, now fixed: `restore_into` merges the snapshot's own mmio
entries, so `install_mmio` has to run *after* the restore or a
snapshot-carried address gets no hook. `longrun.build` does that now.

## Correct-speed playback **[V]**

Emulating at the real 15 fps needs ~3x more throughput than we have, but the
frames themselves are correct and pixel-identical to a fully emulated run --
so the animation can be *shown* at its true speed even though producing it is
slower. `emu/gui.py` keeps every completed frame and its **Replay 15fps**
button plays them back at `FRAME_HZ`, self-correcting for drift. Measured 83
frames cycling at ~14-15 fps against the 14.9996 target.

That separates the two things that were conflated: emulator throughput (30% of
real time, and bounded by the rasteriser) versus what the animation actually
looks like on the device (now viewable).

## The serial console works **[V]**

    #HELLO            -> HOW DO YOU DO?
    #NOPE             -> (no reply, correctly rejected)

`uv run python -m emu.serial console '#HELLO'`.

The last piece was realising **what the console queue actually carries**. It
does `sscanf(item, "%s", buf)` (format `'%s'` at `0x4022A912`, via
`0x400CC93A`) and then strcmps `buf` against its command table using strcmp at
`0x4017C300`. So a queue item is a **pointer to a NUL-terminated string**.

That is why routing the raw serial stream at it did not work. The chain from
DMA does deliver messages -- pointing the sink at the console queue made the
console wake and run its dispatcher twice -- but those messages are the
timestamped 16-byte records built at `0x40110D40` by what is really a
MIDI-style router (8 ports, a timestamp from `0xFC07000C` at `+0x0C`), not
text. The dispatcher ran and matched nothing, exactly as it should.

Two things found along the way:

- **The serial sink is a global**: the producer pushes the destination queue
  from `[0x4029D864]`, and `0x401109E0` is `set_serial_sink(queue)` (its only
  caller is `0x40033130`). By default it points at `0x47D9ADC0`, a queue that
  **no `queue_receive` call site in the firmware reads** -- there are only
  three such sites in the whole image, for queues `0x4094EF3C`, `0x40388EAC`
  (console) and `0x44E0C290`.
- The console task at `0x400CD594` is created only when **bit 5 of
  `0x40288190`** is set (see the boot-mode flag section), and it registers
  `(0x80008, 0x40303E50)` into `0x44DADD0C`/`0x44DADD10` at `0x400CD5A8`.

`emu.serial.send_command` enqueues through the firmware's own `queue_send`
(`0x40001896`), so the semaphore is posted and the task woken exactly as it
would be normally; output is captured by hooking `print` at `0x400054B4`.


## Boot reaches the main OS: the stall was eDMA, not a semaphore **[V]**

The previous session's conclusion -- that `unblock=True` was needed for the
intro and poisoned everything after it, and that `0x400d404a` was unreachable
in 900M instructions -- had the right symptom and the wrong cause. The intro
was not failing to *exit*; it was failing to *finish*. It stopped rendering at
exactly frame 88 of 175, every time, and never got near its exit path.

### The intro's own termination condition

`0x400d3e94` (the render call at `0x400d402a`) returns 0 when the intro is
over, and it is driven purely by call count, not by time:

| | |
|---|---|
| scene count `[0x4313b290]` | 1 |
| scene table `[0x4313b294]` | `0x4028ae2c` |
| draw fn / frames | `0x400d3ab6` / **175** |

So the intro is 176 render calls and nothing else. It is not waiting for a
timer, and there was never a reason it could not finish.

### Where it actually stopped

`0x4000220c` is the firmware's "queue bytes for the console" routine. It opens
with a spin loop waiting for room in a 4096-byte ring at `0x4FE1B000`:

    4000221c  d2 = [0x4094cd90] + len          ; bytes wanted
    40002232  d1 = w[0xFC045474]               ; TCD35.CITER
    40002238  d3 = w[0xFC04547C]               ; TCD35.BITER
    40002244  d1 = (d1 - d3) + ([cd88] - [cd94])
    40002246  d1 &= 0xfff                      ; -> bytes still in the ring
    40002252  if 0x1000 - d1 < d2: goto 4000221c

Nothing advanced that channel, so the ring never drained. The intro draw task
span there at priority 7 and starved everything -- which looked exactly like
the priority-7 busy-spin `unblock=True` was blamed for.

### Channel 35 is UART8 transmit

TCD35 at `0xFC045460`, in the **ColdFire** eDMA layout where CITER is at +0x14
and BITER at +0x1C (not the Kinetis order):

    SADDR  = 0x4FE1B000   ring; ATTR = 0x6000 -> SMOD 12, source modulo 4096
    NBYTES = 1            one byte per request
    DADDR  = 0xEC07000C   UDR8, DOFF = 0

`0xFC044018` is EDMA_SERQ (start), `0xFC044019` CERQ (stop). Vector **155**
points at `0x40001e7c`, the channel-35 completion ISR -- ch34 (RX) is 154, so
the vectors are contiguous. The ISR clears EDMA_CINT, sets `[cd94] = [cd88]`,
and either parks the channel or chains the next transfer.

`emu/edma.py` runs the whole major loop on a SERQ write, advances SADDR with
the ring modulo, reloads CITER from BITER as hardware does at major-loop
completion, and raises vector 155 so the firmware's own ISR does the
bookkeeping. The completion is queued rather than raised inside the write
hook: the enqueue routine writes SERQ with SR = 0x2700, so hardware could not
deliver it there either.

The firmware ring fields, all confirmed against the enqueue routine and the
ISR: `cd74` state (0 idle / 1 running / 2 draining), `cd7c` ring base, `cd88`
head, `cd8c` write index, `cd90` bytes queued but not yet handed to DMA,
`cd94` offset fully drained.

### Result

Intro runs all 175 frames, then reaches `0x400d404a`, `0x400d4058` and
`0x400d4060`. Six previously-missing tasks spawn (`0x400f1eb6` prio 2,
`0x4012606a` prio 6, `0x400f1fce` prio 3, `0x40127b24` prio 5, `0x40127c78`
and `0x40127d9e` prio 4) and `0x40000e82` formats real parameter values:
`'%s: %.16s' 'ONE'`, `'FWD'`, `'OFF'`, `'0.00'`.

PIT3's PCSR goes `0x093f -> 0x0000` across the intro, by the firmware's own
`move.w d0,$fc08c000` -- so a PCSR-gated PIT model stops delivering frames
after the intro without being told to. PIT0 (50 Hz, vec 205) and PIT2
(59.998 Hz, vec 207) stay enabled; PIT1 is off.

### `unblock` had to be narrowed, not removed **[V]**

Blanket-satisfying every pend hid a second copy of the same mistake.
`queue_receive` (`0x40001928`) pends on the queue's own semaphore at queue+8
and then **re-reads `queue->count`**. Satisfying the semaphore without also
enqueuing an item turns a sleep into an infinite spin: 8.9M iterations, ~92%
of all post-intro cycles, on one queue.

The fix is caller-based, not semaphore-based, so it generalises to every queue
in the system: never satisfy a pend whose return address is `0x40001946`. The
same shape appears again at `0x401260c2` in the prio-6 task, which pends on
`0x44e2d148` then re-checks a flag at `0x44e2d5cc`.

Caller-based discrimination also removes the intro handoff entirely. The intro
loop pends the frame semaphore from `0x400d4038` and must be satisfied; the
park loop pends the *same* semaphore from `0x400d4068` and must not be. Two
different callers, one rule, no state to hand over -- and it survives a
snapshot taken after the intro.

Post-intro pends satisfied: **8.9M -> 1.** Nine tasks reach clean blocking
waits instead of spinning.

## The part is an NXP MCF5441x (ColdFire V4m) **[V]**

Established from the peripheral map the firmware itself uses, which is an
exact fingerprint:

| base | module |
|---|---|
| `0xEC070000` | UART8 (a part needs 10 UARTs for UART8 to live here) |
| `0xEC094000` | GPIO |
| `0xFC044000` | eDMA, TCDs at +0x1000 |
| `0xFC048000` / `0xFC04C000` / `0xFC050000` | INTC0 / INTC1 / INTC2 |
| `0xFC05C000` | DSPI0 |
| `0xFC080000`-`0xFC08C000` | PIT0-PIT3 |
| `0xFC090000` | EPORT |

### Flash and DDR capacity **[V]**

Both come out of the firmware's own code; neither needs a datasheet or a probe.

DDR is **64 MiB**, from the bootstrap's own DDRMC writes:

```
DDR_CR04 @0xFC0B8010 = 0x00010101   ; bit 8 8BNK=1     -> 8 banks
DDR_CR15 @0xFC0B803C = 0x02000103   ; ADDPINS=2        -> rows = 15-2 = 13
DDR_CR16 @0xFC0B8040 = 0x02000407   ; COLSIZ=2         -> cols = 12-2 = 10
```

with the controller's fixed 1 chip select and x8 datapath: `2^23 * 8 * 1 =
67,108,864`. The init sequence is byte-identical on both devices.
`tools/ddr_geometry.py` re-derives this from any bootstrap image.

NOR flash is **16 MiB**. The bootstrap issues RDID (`0x9F`) and dispatches on
the 5 ID bytes at `FUN_800024ec`; only the branch matching mfg `0x01`, id
`0x2018`, ext `0x00` — an S25FL127S-class part, 128 Mbit — selects the
512-byte page and 256 KB erase geometry the flash loop actually uses. Weaker
than the DDR result by one step: the firmware *recognises* the part, it never
computes a capacity. **[V]/[D]**

Against a store-only repacked image this is not close. What is staged and
flashed is the decoded container, **4.00 MB** against a stock 1.35 MB — the
5.07 MB `.syx` figure includes 8-in-7 transport framing that never lands in
memory. Headroom is 16.8x on DDR and 4.1x on flash. A real LZ77 packer is not
needed. That everything past the OS container to the end of the chip is free
is an assumption; no partition table has been located. **[O]**

**V4m, not V4e** -- MMU and EMAC but **no FPU**. That is the real reason 93%
of executed instructions were soft-float: it is not a compiler flag, the part
has no hardware float. (We run Unicorn as `UC_CPU_M68K_CFV4E`, a superset;
harmless because the firmware never issues FPU instructions.)

One loose end: the firmware's own bus-clock constant is 132 MHz
(`0x07DE2900`), while the datasheet headline is 250 MHz core. If the bus were
core/2 that implies a 264 MHz core, slightly over the published maximum. The
15 fps result does not depend on resolving this -- the PIT and UART share a
clock domain and we used the firmware's own constant, cross-checked by three
timers landing on round rates.

## What actually limits speed: our hook layer, not Unicorn **[V]**

Superseded by measurement. An earlier version of this section reported
"Unicorn m68k ceiling here 2.90M instr/s" and concluded that live 15 fps was
out of reach for Unicorn plus Python hooks. **The ceiling figure was
mis-attributed.** It is the speed of *this workload with our hooks*, not
anything Unicorn imposes.

| measured on this machine | |
|---|---|
| hook-free m68k loop, `count=` on | **250.8M instr/s** |
| our workload, both HLEs on | 1.3-2.5M instr/s |

Two orders of magnitude sit between those, and all of it is ours.

### `count=` on emu_start costs 1.84x

Passing `count` makes Unicorn install an internal per-instruction hook to
decrement the budget, which defeats its fast dispatch path. Over the same 40
rendered frames:

| | |
|---|---|
| `count=250_000`, chunked loop | 8.07s |
| uncounted, stop from a hook at frame completion | 4.39s |

The cost is `count` itself, not the number of emu_start calls: over the same
100 frames, `count=20k` (1308 calls), `count=500k` (53 calls) and `count=1e9`
(1 call) all land within 3% of each other. Chunking was never the price.

`emu/longrun.py:run_until` is the uncounted form. Stop only from a hook that
has already advanced PC past the current instruction -- the setPixel HLE
writes PC = return address, so it qualifies. Stopping from a plain code hook
leaves PC on the hooked address and the resume re-enters the same hook
immediately: the run then spins making no progress while appearing to
iterate, which is how an early attempt at this measured a fictitious 118x.

`emu/gui.py` now stops per completed panel frame instead of every 250k
instructions: **~8.7-9.3 fps during the intro, ~58-62% of the real 15.00 Hz,
against ~30% before.**

A hook-only stop condition also needs a wall-clock floor, or the caller hangs
the moment the firmware stops meeting it -- the end of the intro does exactly
that. **A timeout is free, unlike `count`.** Same 40 frames, identical 327,681
setPixel calls each way:

| | |
|---|---|
| `count=250_000` | 8.03s |
| uncounted | 4.45s |
| uncounted + 0.5s timeout | 4.43s |

`count` installs a per-instruction hook; a timeout only arms a timer thread.
Bound wall-clock time freely; bound instruction counts only when something
genuinely has to happen per fixed number of instructions.

### Where the remaining time goes

cProfile over 10M instructions, both HLEs on:

| | share |
|---|---|
| `emu_start` -- Unicorn actually executing m68k | 34% |
| Unicorn's Python ctypes binding | ~54% |
| our own handler logic | ~12% |

1.02M of 10M instructions cross into Python. Per crossing we pay a ctypes
`create_string_buffer` allocation for every `mem_read` (1.94M of them) and a
separate FFI call for every `reg_write` (1.85M). The dominant cost is the FFI
boundary, not our logic and not the chip model -- so the next wins are fewer
crossings and cheaper crossings, not a better peripheral model. Real time is
no longer ruled out.

### A measurement mistake worth recording

An earlier attempt to split "hook dispatch" from "handler work" gave dispatch
4% and handler bodies 93%, which looked like large headroom in our Python.
**That reading was wrong.** With no-op handlers the firmware still executes all
the soft-float code, so the two runs cover completely different amounts of
firmware work per instruction -- the comparison was not apples to apples.

Acting on it produced only 8% (4.10 -> 4.43 fps): precompiled `struct.Struct`
codecs and unpacking arguments directly as `>f` instead of bits-then-convert
(the old `b2f`/`f2b` each cost a pack *and* an unpack). Those are worth
keeping. The third change in that batch -- caching Bitmap geometry per pointer
-- was **a bug**, not a win, and has been reverted: the firmware mutates the
fields of an existing Bitmap. See the note in `emu/hle.py`.

The lesson matches the earlier `install_mmio` one, and the fictitious 118x
above, and the "2.90M ceiling" this section replaces: only trust an A/B where
the two sides do the same work.

## Ghidra misses functions that are only ever pointed at **[V]**

Ghidra records a data reference to an address held in an immediate and stops
there. If nothing ever reaches that address with a `jsr`, no function is
created, so it gets no decompilation and does not appear in `decomp/` at all --
a blind spot that is silent rather than noisy, because a `rg` over the dump
returns nothing and looks like a clean negative. Two hours were lost to this on
2026-09-16 before the dispatcher at `0x40042fe2` was found by
`tools/refscan.py`.

Measured on Digitakt II 1.16, MAIN OS sha-256 `57bb4dfa…`:

- Decoding is not the problem. Of the code-dominant region
  `0x40000400`-`0x401e97a7` (2,003,879 bytes, 61% of the image; the rest is a
  1.19 MB string/RTTI/data blob), **99.92%** decodes cleanly -- 1,546 undecoded
  bytes in 339 spans, 242 of them exactly two bytes. A sample of the largest
  and of ~25 scattered spans found no failed instruction: they are switch
  displacement tables (`0x40021fe4` sits right after a
  `jmp $21fe4(pc,d0.l)`) and runs of one repeated word. **[V][C]**

  The 96.78% figure quoted in earlier handovers is a whole-image number. A
  linear sweep of the data blob still "decodes" ~89% of it into plausible
  instructions, so that average says nothing about decoder quality. Writing a
  better ColdFire SLEIGH language would recover nothing. **[C]**

- Function discovery is the problem. **6.87%** of that code region, 148,862
  bytes, sat outside every `function_ranges` entry. Of the twenty largest
  in-code gaps, none look like data and at least eleven open with a prologue.

- **Ghidra's own analyzers recover none of them.** On a copy, with
  `tools/ghidraopts.py`, `Function Start Search` was already on;
  `Function Start Search.Search Data Blocks` and `Aggressive Instruction
  Finder` were off. Turning both on and re-analysing: functions 14,743 ->
  14,743, and the two entry sets are identical in both directions. Code
  coverage unchanged at 93.43%. `0x40042fe2` still had no function. The only
  effect was seven more Error bookmarks. **[V]**

### Seeding them from the pointers **[V]**

`tools/codeseeds.py` gained a third candidate kind, `pointers`: an address
taken as an immediate (`#$X` in any instruction) or by `pea $X.l`, which lands
in a code range on a function prologue -- `lea -N(a7),a7` (`0x4fef` with a
negative displacement), `link.w aN,#-d` (`0x4e50`-`0x4e57`), or a `movem.l`
save (`0x48e7`, `0x48d7`). `tools/ghidraapply.py seeds` applies them through
the same `ensure_function` guards as the call targets, which skip a target
inside defined data or mid-instruction and roll the edit back if it raises an
Error bookmark.

```
uv run python tools/codeseeds.py out/sections/dt2-1.16/section_3_MAIN_OS.bin \
  --base 0x40000400 \
  --code 0x40000400 0x401e97a8 --code 0x4030cd04 0x4030f89d \
  --json out/symbols/dt2-1.16-seeds.json
uv run python tools/ghidraapply.py seeds out/symbols/dt2-1.16-seeds.json \
  --project ~/ghidra-projects/elektron-emac --project-name elektron-emac \
  --program /dt2-1.16-seeded/section_3_MAIN_OS.bin --analyze
```

The 1.16 code ranges above come from `function_ranges` (functions span
`0x40000410`-`0x4030f89d` with one 1.19 MB gap); `codeseeds.py`'s defaults are
1.15C's. Result: **[V]**

| | baseline | seeded |
|---|---|---|
| functions | 14,743 | **14,857** (+114) |
| entries missing from the other set | 0 | 0 |
| code coverage, `0x40000400`-`0x401e97a7` | 93.43% | **94.37%** |
| Error bookmarks | 40 | **40** |
| decompile failures | 16 | **16** (the same 16) |

1,067 pointer records to 921 distinct targets, 816 of them not a call target;
464 `link`, 456 `lea`, 1 `movem` by distinct target. Most already had functions
by another route, and 114 were new. The dry run predicted 114 and the real run
created 114.

Nothing suggests junk: Error bookmarks and decompile failures both held exactly
still, no baseline function disappeared, and the new functions run 40 to 4,694
bytes with none under 8. `0x40042fe2` came back as a single clean 558-byte
range, `0x40042fe2`-`0x4004320f`.

Worth noting as a cross-check: an independent sweep for addresses that are
data-referenced, not a function entry, and start with a prologue predicted
**115**; the pipeline created **114**. Two methods, written separately, landing
one apart.

The remaining ~130,000 uncovered bytes are mostly gaps whose first bytes are
alignment or tail data rather than the entry, plus a repeated non-standard
prologue idiom (`8f2f 0a2f 0224` after a varying first word) that the three
patterns above do not match. **[O]**

## Digitone (mk1) 1.43: the second CPU, and the port **[V][C][D][O]**

`Digitone_and_Digitone_Keys_OS1.43.syx`, SHA-256 `c5a54cc0...`:

- section 3 is the main CPU, at `0x40000400`;
- section 7 is the second ColdFire, which the firmware's strings call the
  DSP;
- section 6 is the DSP's serial-boot loader.

Every claim below marked **[V]** was read from the image bytes by a second
agent on 2026-09-24, using static disassembly, with EMAC and `mvz`/`mvs`
decoded by hand. The emulator (`emu/dsplink.py`,
[DIGITONE-MK1.md](../DIGITONE-MK1.md)) runs the same code to live sound.

### Loading and the idle loop **[V]**

- Section 6 reads section 7 over SPI to `0x40000400`
  (`move.l #$40000400,-(a7)` / `jsr $80000314`). It then jumps through the
  image's first long (`movea.l $40000400.l,a0` ... `jmp (a0)` at
  `0x80000554`), which is `0x40000b92`.
- `0x40000b90` is `bra.b *`, the idle loop. `0x40000b92` starts with
  `movea.l #$48000000,a7`.
- The absolute pointers inside section 7 line up only with base
  `0x40000400`. A scratch disassembler that mapped it at `0x40000000` was
  wrong by `0x400`.

### Vector 96 is reached through a trampoline **[V][C]**

- VBR is `0x40000000`: `movec d0,vbr` at `0x40000a7c`, loaded from a long
  at `0x40006794` that nothing writes.
- A loop at `0x40000a5c` fills all 256 vectors with `0x40000404` (`rte`).
- Vector 96 (offset `0x180`) is written twice:
  1. `0x40000ab8` stores `0x40000406`, a handshake-phase handler. It clears
     the DTIM0 flag and sets the restart flag `0x400166e0`, so an edge
     during the handshake sends the code back to `0x40000aec` to redo the
     FPGA setup.
  2. `0x40000b8c` stores a pointer to a 10-byte trampoline built on the DSP
     stack at `0x40000a38`–`a56`: `movea.l #&base,a1; jmp $400008fa`.
- **Correction:** this session's first write-up said vector 96 *is*
  `0x400008fa`.
- The emulator does not depend on the difference: it raises the vector
  and runs whatever the DSP installed.

### The render **[V]**

- `0x400008fa` copies the parameters: `pea $3a0.w; clr.l -(a7);
  pea $80000000.l; jsr $40003848` (memcpy) moves shared RAM `0..0x39F`
  into SRAM.
- It copies the output back: `lea $3a0(a1),a1` ... `pea $400.w;
  pea $80000e00.l; jsr $40003848` puts `0x400` bytes at `0x3A0..0x79F`.
- The mixer at `0x400038c2` lays out eight voices of 32 longs: an inner
  `moveq #$20`, two voices `0x80` apart, and an outer loop that steps by 2
  up to 8.
- It ends with `rte` at `0x40000a2e`.
- Measured in the emulator: about 18.4k instructions a render.

### The handshake **[V]**

The DSP side:

- It writes `0x484F` ('HO') at shared `+0` and `0x4841` ('HA') at `+2`
  (`0x40000b1a`/`b1e`).
- `0x400008d4` toggles PG2: `move.b #4,$ec09401e` or
  `move.b #$fb,$ec09402a`.
- It waits for `0x4230` ('B0') at `0x40000b4e`, then writes `0xA5A5` at
  `+4` (`0x40000b72`) and toggles PG2 again.

The main side:

- It installs `0x4008d850` as vector 68 (`0x4008d62a`), with EPPAR set for
  both edges, EPIER bit 4 and ICR04 = 6.
- The handler checks HO/HA and answers **1 at `+2` first** (`0x4008d8c2`),
  **then 'B0' at `+0`** (`0x4008d8c8`).
- On `0xA5A5` it writes 2 to `0x4137b720` (`0x4008d8ec`).

Also:

- Section 6 drives PG2 low before it jumps.
- A flag in the image (`0x40006790` = 1) makes the first render toggle PG2
  a third time. The main side ignores that pulse, because its counter
  `0x4137b718` is already 0.
- An HO/HA mismatch retries up to five times by toggling PA4.

### The status word `0x4137b720` **[V][C]**

- 0: a boot is **in progress**. It is cleared before the reset is released
  and written again when 'B0' is sent. **Correction:** not "booted".
- 1: failure. It is set in three places:
  - section 6 missing (`0x4008d7c4`);
  - section 7 missing (`0x4008d7b8`);
  - the watchdog `0x4008d492`, after more than 18 ticks on the third
    attempt (`0x4008d4d8`).
- 2: running.
- "DSP BOOT FAILURE" is at `0x401d9082`. It is shown when `0x4008d52e`
  returns 1 (`0x40019bc4`, and again at `0x40019d46`).

### Booting the DSP from the main CPU **[V]**

- The boot task `0x4008d56c` asserts the reset with
  `move.b #$bf,$ec094025` (PCLRR_B, bit 6) and releases it with
  `move.b #$40,$ec094019` (PPDSDR_B).
- DSPI2 is at `0xEC038000` with MCR MSTR = 0, so the main CPU is the SPI
  slave. Section 6 is the master: it sends a `0x03` read command and skips
  bytes until `0xAA`.
- eDMA channel 29 (TCD at `0xFC0453A0`, DADDR PUSHR `0xEC038037`, SERQ
  `0x1d`) streams section 6 first, with eight `0xFF` in front. Its
  interrupt (vector 149, `0x4008d40c`) then re-arms it for section 7, with
  `0xAA` in front.
- The task pends on `0x4137b70c` at the top of each attempt.
- That semaphore is posted through `0x400019a0`, from `0x4008d80a`,
  `0x4008d820` and `0x4008d504`. `0x400019a0` is a post with interrupts
  masked: `move.w sr,d2; move.w #$2700,sr; jsr $40001632; move.w d2,sr`.
- `0x40001632` sets the count, readies a waiting task, and forces a
  reschedule with `bset #13` on INTFRCL2 (`0xFC050014`).
- `emu/semscan.py` does not know this post routine, so the emulator must
  never fake this semaphore. Faked, the task re-uploads the DSP thousands
  of times a second.

### Before the handshake: PIT1 and the FPGA **[V][D]**

- The DSP polls three things:
  - PIT1's PCSR `0xFC084000` for PIF (`and.l #4`, at `0x40000458`);
  - `0xEC09401C` bit 0 (`btst #0`, up to 100 tries, at `0x400007f8`);
  - `0xEC09401D` bit 7, tested through the N flag (`blt`, at
    `0x40000892`).
- It pulses PH4 low then high (`0xEC09402B`/`0xEC09401F`, bit 4).
- It decompresses a bitstream from `0x400077a0` to `0x40009000` and
  streams it through DSPI1 (`0xFC03C000`). It returns 1–4 on failure,
  which retries.
- The names INIT_B (PE0), DONE (PF7) and PROGRAM_B (PH4) are inferred from
  that sequence, not stated in the image. **[D]**
- The emulator answers the three polled reads and nothing else. Without any
  one of them, section 7 retries forever. **[V]**

### Once running: one edge per block **[V]**

- `0x4009c2e4` is the interrupt of eDMA channel 54 (vector 174). TCD54
  feeds SSI1 transmit (`0xFC0C8000`) with CSR set for half and major, so
  it fires every `0x100` bytes (32 stereo frames).
- When `0x41391f28` is set (after A5A5), it calls `0x4008d82a`. That
  routine flips `0x413776b0` and writes either `#$10` to `0xEC094018` or
  `#$ef` to `0xEC094024`: one PA4 edge per call.
- The DSP's DTIM0 captures on any edge (DTMR `0xC1`).
- The same ISR forces INTC1 source 63 (`or.l #$80000000,$fc04c010`), which
  is the render on vector 191 (`0x4009d100`, installed at `0x4009c614`).
- That render starts eDMA channel 47 through SSRT (`moveq #$2f,d0` ...
  `move.b d0,$fc04401e.l` at `0x4009d18c`), twice a block:
  - voices, `0x100003A0` → `0x80004110`, `0x400` bytes;
  - parameters, `0x80001C00` → `0x10000000`, `0x3A0` bytes.
- The emulator counts 2 SSRT starts per block (22962 for 11480 blocks).
  **[V]**

### Boot argument bit 19 is the Keys **[V]**

- `0x402292f0` is captured from the boot arguments at entry
  (`0x400004ec`).
- About 70 of its 109 references test `andi.l #$80000`.
- One of them, at `0x40019b68`, leads to the Keys' wheel-calibration check
  ("WHEEL CALIBRATION" / "INCOMPLETE", `0x401d9070`/`0x401d9065`).
- This settles the **[O]** under the build floors above. The emulator
  passes 0, so it runs a plain Digitone.

### The encoder dead zone **[V][C]**

- The encoder driver at `0x400dbd72` keeps its state at `+0x10` in a
  `0x14`-byte slot, 18 slots from `+0x20` (`0x400dbd4a`).
- The tick routine `0x400dbcb8` counts an idle encoder's state back up to
  48 (`moveq #$2f; cmp; blt; addq #1`).
- A turn subtracts 3×|delta| (`pea (a3,a3.l*2)`, then abs). Nothing steps
  while the state is above 24 (`moveq #$18,d0; cmp.l d2,d0; bge` at
  `0x400dbe24`).
- After an emitted step it adds 48 back (`moveq #$30,d0;
  add.l d0,$10(a2)` at `0x400dbea0`), but only for configs whose first
  byte is 0.
- **Correction:** this session first wrote "a 16-count dead zone". That was
  a reading of the measurement (at one count per notch, ~16 notches before
  anything moved), not of the code.
- `[panel] encoder_counts = 4` in both mk1 device files sends each notch as
  four counts.

### Measured in the emulator **[V]**

- `--add` takes 25 s on the reference desktop:
  - the cold boot parks at ~130M instructions;
  - the intro reaches the live UI;
  - the first boot settles at ~1300M;
  - acceptance passes on the card's first-boot layout and status 2.
- The card has no ekFS: the firmware initialises a blank one.
- Live playback:
  - 64M instructions a second, 100% of real time, 0 dropouts;
  - one render per block, on a render thread using ~80% of a second core;
  - the factory pattern gives RMS ~1.8k, and the output is silent at idle.
- The panel's 54 codes were measured by pressing each one. It has 72 LEDs
  in 18 groups, seeded from RAM (`emu/panelleds.py`).
- PAGE (25) and FUNC (34) come from the firmware's key/LED table and have
  not been seen lit. **[O]**

## The mk1 boot chain, run from the bootstrap (2026-09-24) **[V][D][O]**

`emu/bootrom.py` runs container section id 2 from its reset vector on the
emulated ColdFire, with the container in an emulated SPI flash at `0x80000`.
On Digitakt 1.53 and Digitone 1.43 it reaches the OS entry `0x400004e8`, and
MAIN OS in DDR is then byte-identical to the section. Nothing is written to
the flash. **[V]**

### The bootstrap image **[V]**

- Section 2 is `[u32 length][image]`. The image links at `0x80000400`: its
  first longwords are the initial SP `0x80010000`, the entry (`0x80000eaa`
  on the Digitakt) and the version word read at `0x80000408` (`0x0300` on
  Digitakt 1.53, `0x0200` on Digitone 1.43).
- The entry is a function of one argument, a boot mode (`move.l $c(a7),d2`
  after its prologue). Its switch at `0x80000f82` takes 0 and 1 to the
  normal path (0 configures the debug module first with WDEBUG, which QEMU
  aborts on at translation), 2, 3 and 5 to `halt` for a debugger, and 4 to
  hardware init only. **[D]**
- It points VBR at `0x80000000`, sets RAMBAR to `0x80000235` and copies its
  tail to `0x8000f000`, from where it reads the flash while the updater is
  loaded over its lower part.

### What it touches, in order **[V]**

1. The PLL: it waits on `PLL_SR` LOCK (bit 4).
2. The crossbar, the pins, the interrupt controller.
3. The DDR controller: it writes every control register (the 64 MB
   geometry `tools/ddr_geometry.py` decodes), then polls `DDR_CR27`
   (`0xFC0B806C`) for bit 3 up to 1000 times. If the bit never sets it
   records a DDR failure (`0x80007706`) and later halts.
4. PIT1 as a busy-wait delay.
5. UART8, the front-panel link. See below.
6. DSPI 0, where the flash is on **PCS1**: `9F` (RDID; the model answers
   as an S25FL127S, `01 20 18 4D 00 80`), `06`, `E1` (DYB write), `05`,
   then `03` reads. It reads the ELE3 header
   and section table at `0x80000`, and 36 bytes at `0x380000`. That block
   is erased in the emulated flash, and the boot does not depend on it.
   What it holds on a device is **[O]**.

The Digitakt's bootstrap then unpacks MAIN OS itself (`0x800005fe`) and
calls its entry. The Digitone's loads the updater (section 4, raw: a
longword with its entry `0x80000492`, a zero longword, the image at
`0x80000408`) and calls that. The updater is a small RTOS with PIT0 as its
tick (vector 205). It tests the DDR, unpacks MAIN OS and calls it.

### The front panel at power-on **[V]**

The bootstrap talks to the panel controller over UART8, one received byte
per interrupt (vector 180, handler `0x800010de`, a 512-byte ring at
`0x8000772e`):

- `60 01`: report every key group. The panel sends `[0x20|group, mask]`
  per group, the same button report the OS reads (`emu/panelin.py`). The
  bootstrap keeps them as a 16-byte key map (`0x8000771e`) that its
  startup checks test. The Digitakt waits for 6 groups, the Digitone for 7
  (`0x7f`), and a Digitone Keys for 9 (`0x1ff`).
- `70 00` / `71 00`: the UI card's identity, `[0x7_][type][fw minor][fw
  patch][tested]`. The type must be 4 on the Digitakt and 8 on the Digitone
  (0xA on a Keys), or the test report reads "WRONG CARD". **[D]**
- `74 00`: the serial number the report prints as `SN:`.

With no answer to `60 01` the bootstrap sets boot flags `0x60`, and the OS
then parks at `0x4006932e` (Digitakt).

### The boot flags **[V][D]**

The OS entry stores its argument at `0x401F55C0` (Digitakt 1.53) and tests
it in more than twenty places. With a panel answering, both products'
bootstraps pass **`0x00140000`**. The emulator's direct start passes 0.

- Bit 20 is always set (`bset #$14`).
- Bit 18 is set when `0x80005c88` returns 0 (Digitakt). **[D]**
- Bit 19 is the Keys (above). On the Digitone it comes from a board strap:
  `0x80000416` reads bit 3 of GPIO `0xEC09401B` up to four times, and low
  every time means Keys. **[D]**
- `0x40 | 0x20` means no panel.
- `0x20` alone follows a soft reset whose DDR mailbox holds `0xB0B0DADA`
  at `0x48000000`.

The Digitakt OS with `0x40` set skips its normal init (`0x400691da`) and
ends in `bra.b` to itself at `0x4006932e`.

### The DDR is used through its aliases **[V]**

- DDR is 64 MB on both mk1 products (one x8 part, per the DDRMC setup
  above). The controller repeats it through `0x40000000-0x7FFFFFFF`.
- ACR0 is `0x4007E020`: `0x40000000-0x47FFFFFF` copyback, everything else
  cache-inhibited (`CACR` `0xA50CE100`).
- The firmware relies on the aliases:
  - its initial stack is `0x48000000` (the top of `0x47Fxxxxx`, which is
    `0x43Fxxxxx`);
  - the SSI transmit buffer is at `0x4BA8F080`;
  - it reads buffers through the uncached `0x4Bxxxxxx` window.
- Saved sessions from the app, which gives every alias its own memory,
  hold no data written through two aliases of one location.
- Merging such a session's aliases into one memory broke the audio. Boot
  the aliased model from reset instead: `Machine.set_ddr`, `emu.fwcheck`.

### The container's checksums, mk1 **[V]**

- The per-message checksum's constant K is the framing message's byte 8,
  counting the F0, which is the OS-stream id (`0x05` on the Digitakt). "Byte
  7" above counts from after the F0.
- The data-message count is at bytes 12-14 of the framing message.
- The content checksum covers the length the preamble's first longword
  declares: 1,122,672 bytes on Digitakt 1.53, which stops short of the 4
  padding bytes and the 32-byte trailer slot. Which length the device's
  updater expects of a rebuilt container is **[O]**.

### Two reads the stock firmware makes of unpopulated space **[V][O]**

- Digitakt 1.53 reads `0x00000000` once during the cold boot
  (`pc=0x40068f4c`).
- Digitone 1.43 reads `0x0000012A` during a session (`pc=0x4009de70`),
  steadily: 86,262 times in 8.3 emulated seconds of `emu.fwcheck`'s
  default tour (2026-09-24). None during its boot.
- Both are FlexBus space with no chip select configured, which the part's
  bus monitor should end with a bus error. The devices evidently tolerate
  them. Why is **[O]**.

### The audio render's cost in core cycles **[V][D]**

Measured with `emu/cftiming.py` (the MCF54418RM 3.3.5 tables; zero-wait
memory, no pairs), on Digitakt 1.53 with one voice playing:
- The render is the forced vector 191 (the SSI's `FORCE_VECTOR`).
- It costs 100,300 cycles on average and 103,640 at worst, in each
  0.667 ms period of 166,685 cycles at 250 MHz: a margin of 38%.
- The transmit DMA's vector 174 takes about 40 cycles.
- The CPU is 62% busy, at 1.57 cycles per instruction.
