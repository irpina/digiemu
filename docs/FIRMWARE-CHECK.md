# Checking a firmware build before it goes on a device

`emu.fwcheck` runs a custom build (and, for comparison, the stock build it
came from) through everything the emulator can check, and says what passed
and what it could not check. It is meant to be run before every flash.

```text
python -m emu.fwcheck CUSTOM.syx --baseline STOCK.syx --out check
```

The exit status is 0 when the build passes. `check/report.json` has every
fact behind the verdict, and `check/compare/` has the screens that differ
from the stock build as PNGs (stock, custom and the difference side by
side). On the reference desktop a Digitakt build takes about 4 minutes
without timing and about 11 with it: the timed run stage is about 64 times
slower than the device.

| option | |
|---|---|
| `--baseline STOCK.syx` | check the stock build the same way and compare. Strongly recommended: see "Stock behaviour" below |
| `--out DIR` | where the report goes (default `fwcheck-out`). Each build is booted afresh in `DIR/work/baseline/` or `DIR/work/build/`, which the check empties first: a saved boot is never reused |
| `--script FILE` | the keys to press during the run stage and where to compare screens (below) |
| `--margin 0.10` | how much of each audio period the render must leave spare |
| `--fsys 250e6` | the core clock the timing assumes |
| `--no-timing` | skip the cycle clock: about three times faster, no render margin |
| `--no-boot-strict` | skip strict mode during the boot |
| `--stop` | stop the run at its first strict-mode violation |

## In the app

The launcher's **Check firmware...** button runs the same check. Choose the
build's .syx, then what to compare it with:
- the stock firmware you have set up in digiemu (the same version is
  offered first);
- another stock .syx;
- nothing, in which case the stock firmware's own quirks count against the
  build.

A tick adds the timing measurement. The check runs in its own process
while you keep using digiemu, and ends with PASS or FAIL and each
stage's reasons. Its folder, `checks\<time>-<file>\` next to `digiemu.exe`,
keeps `report.json`, `summary.txt`, `check.log` and any screens that differ.
Each build's work folder (a copy of the firmware, its sections and a card
image, over a gigabyte) is deleted once that build has been checked, so a
check needs about 1.5 GB free while it runs, and leaves only the report.

For CI, `digiemu-console.exe --check BUILD.syx [--baseline STOCK.syx]
[--timing]` prints the same verdict and exits 0 when the build passes.
Without `--baseline` it compares with the stock firmware set up in that
digiemu folder, if there is one.

The app cannot verify the SysEx and content checksums: the code that
computes them also builds firmware, and digiemu leaves that out. The
container stage says so in a warning. Verify them with the tool that built
the file.

## What each stage checks

A pass means every stage below passed. Each check names the part of the
device it stands for, and the last section says what none of them can see.

### 1. container: what the device receives

| check | why |
|---|---|
| every SysEx message's checksum, and the framing message's count | the device checks both and refuses the transfer |
| the content checksum over the length the preamble declares | the same, for the decoded container |
| the ELE3 header and section table; each packed section decodes to its declared length and byte sum | a section that does not unpack cannot boot |
| MAIN OS loads at `0x40000400` | where the bootstrap starts it |
| the container ends before flash `0x380000` | the updater writes it at `0x80000`, and the bootstrap reads a block of its own at `0x380000` |
| the product ids are a product digiemu runs | an OS for another product is refused on the device ("Incompatible OS") |
| **the bootstrap version is not higher than the stock one** | a higher version makes the device rewrite its bootstrap ("BOOTSTRAP UPGRADE"), the one step that cannot be undone. A lower one is kept by the device ("DOWNGRADE NOT POSSIBLE") and is only a warning |

The two checksum checks need the firmware-building code, which the public
repository does not carry. Without it they are skipped, and the report says
so. Every other check runs.

It also notes, as a warning, when the preamble's length covers the 32-byte
trailer slot and the stock one does not (stock 1.53 stops 36 bytes short).
Both are self-consistent. Whether the device minds has never been tried.

### 2. bootloader: the build's own bootstrap

The build's bootstrap (container section id 2) runs on the emulated
ColdFire from its reset vector, as the device runs it after the serial boot
facility has copied it into SRAM (`emu/bootrom.py`). The SPI flash holds the
build's container at `0x80000`. The run passes when the bootstrap:

- identifies the flash, finds the container, unpacks MAIN OS and jumps to
  it;
- leaves MAIN OS in DDR byte for byte as the section holds it;
- writes nothing to the flash;
- passes the OS the boot flags a device with a front panel gets
  (`0x00140000` on Digitakt 1.53 and Digitone 1.43; `0x60` in them means
  "no panel", and the OS then parks).

The two products boot differently. The Digitakt's bootstrap unpacks MAIN OS
itself. The Digitone's loads its updater (container section 4) into SRAM
and runs it. The updater is a small RTOS: it tests the DDR, unpacks MAIN OS
and starts it.

Each bootstrap asks the front panel's controller about itself, and the
device file says what the panel answers (`[boot]`):
- the key groups it reports (six on the Digitakt, seven on the Digitone);
- its card type (4 and 8);
- on the Digitone, a board strap. A GPIO pin reads low on a Digitone Keys,
  which makes the bootstrap wait for nine key groups.

The machine it leaves (CPU registers, the stack with the boot flags, SRAM,
the DDR it wrote and the peripheral registers it programmed) is where the
next stage starts. The emulator's usual start skips all of this; see
"Stock behaviour" for what that changes.

### 3. boot: from the bootloader to a settled screen

A cold boot from that handoff, through the intro, until the user interface
has settled after the first boot's factory install, the same stages as the
app's first run (`emu/bootstrap.py`). Two things are stricter than the app:

- **The DDR model.** The Digitakt has one 64 MB DDR2 part
  (`devices/*.toml` `[memory] ddr_mb`, from the bootstrap's own controller
  setup), and the controller repeats it through `0x40000000-0x7FFFFFFF`.
  The firmware uses that: its stack is at `0x47FFxxxx` and its DMA
  buffers are read through the uncached window at `0x48000000`, which is
  the same memory. The app gives every 1 MB of that space memory of its
  own. The check makes every alias the same bytes, as on the device
  (`harness.Machine.set_ddr`).
- **Strict mode** (`emu/strict.py`) watches every stage. See the next
  section for its rules.

The stage passes when the user interface comes up and settles, and strict
mode saw nothing the stock build does not also do.

### 4. run: a scripted session, on the firmware's own instructions

The settled build is driven by a key script (`emu/session.py`): the same
keys at the same emulated moments on every run. Two things change here.

- **The host shortcuts are off.** The app runs the firmware's float
  routines and its setPixel natively (`emu/softfloat.py`, `emu/hle.py`).
  They are exact, but they skip the firmware's own instructions, so a
  change there would go unseen. The run executes the firmware's own.
- **Time is core cycles** (`emu/cftiming.py`). The app's clock counts
  instructions. Here every instruction costs what MCF54418RM section 3.3.5
  says, and the firmware's timers tick on those cycles. So a render that
  needs more CPU than the device has falls behind its audio clock here
  too.

The stage passes when strict mode saw nothing new, the run did not stop,
and the audio render left at least `--margin` of every audio period spare
(below).

### 5. compare: the same keys on the stock build

With `--baseline`, the stock build goes through the same stages, and the
same script. The report then shows:

- for each `snap` in the script: whether the screens are identical, and if
  not, how many pixels differ and where;
- the first moment the two screen streams diverge;
- which 100 ms windows of the audio differ, and by how much.

Sessions are repeatable, so a difference is the builds'. The compare stage
has no pass or fail: a custom build is meant to differ somewhere. Check
that it differs only there.

## Strict mode's rules

From the MCF54418RM, with its table or section:

| rule | source |
|---|---|
| data only in DDR, the 64 KB SRAM at RAMBAR, Rapid GPIO, the peripheral slots of Tables 1-3 and 1-4, or FlexBus inside a chip select the firmware configured | Table 1-2, 1.8.1; the bus monitor ends an unterminated FlexBus cycle with a bus error |
| no access to the rest of the SRAM backdoor (`0x80010000-0x8BFFFFFF`) | it wraps onto the SRAM on the device; the emulator does not alias it |
| code only from DDR or SRAM | |
| no float instruction | the MCF5441x has no FPU: every one is a line-F exception on the device (Unicorn's V4e would run it) |
| no other unimplemented line-F or illegal opcode, and no MOVEC the harness does not intercept | |
| no access error, address error, illegal instruction, divide by zero, privilege violation, trace, line A/F, debug interrupt, format error or spurious interrupt, and no vector without a handler | Table 3-15 |
| if the core watchdog is enabled, it is serviced in time | 13.2.1 |

Every violation is recorded and the run goes on, so a stock build's own
violations do not end its run before the comparison. What follows a
violation is the emulator's guess, though: it goes on with zeros where the
device would take an exception. `--stop` ends the run at the first one.

Strict mode also reports, without failing:

- the peripherals the firmware used that the emulator has no model for,
  or only a partial one (their reads return zeros);
- the stand-ins the run needed (below), with counts, so two builds can be
  compared on them.

## Timing: what the render margin means

The Digitakt renders audio 32 frames at a time, one half of the transmit
buffer, every 0.667 ms. The run measures, for every render, the core
cycles from its interrupt's entry to its `rte`, including interrupts that
preempt it, and compares the worst with the period. On stock 1.53, one
voice playing, the estimate is about 100,000 cycles of the 166,700 in each
period at 250 MHz: a margin of about 38%.

It is an estimate against the manual's tables, not a measurement of the
device:

- **memory is zero-wait**, the manual's own assumption. There are no
  cache misses, DDR latency or bus contention.
- **no instruction pairs.** The V4 core issues some MOVE pairs in one
  cycle, and the manual gives no rules for it, so the estimate is slow on
  tight MOVE sequences.
- **the stated stall rule is applied** (3.3.5.1): a register written by one
  instruction and used for the next one's address costs 2 or 3 cycles.
- **branches:** a model of the 8-entry branch cache, 128-entry prediction
  table and 4-entry return stack. The sizes are the manual's; the index
  and allocation rules are assumed.
- **ranges:** where the manual gives 1-3 cycles (BRA, BSR, JMP/JSR to an
  absolute target), the estimate charges 3.
- **interrupt entry** is charged as TRAP's 18 cycles.
- **the core clock** is assumed to be 250 MHz, the part's maximum. The
  device's own divider comes from the serial boot configuration word in
  its flash, which is not in the `.syx`.

Compare margins between builds rather than reading one on its own. A
custom build whose render margin is well below the stock one's is at risk
on the device even when it passes.

## Stand-ins that remain

These are places where the check still does not run what the device runs.
Each is counted in the report.

| stand-in | where | why |
|---|---|---|
| the panel controller's answers at boot | `emu/bootrom.py` PanelLink | the bootstrap asks for the key state (`60 01`) and the UI card's identity (`70`/`71`/`74`). The key state is the emulator's own panel protocol. The card identity (type 4, firmware 1.2.0) is the only value the bootstrap checks: anything else is "WRONG CARD" |
| the bootstrap's timer delays end at once | `emu/bootrom.py` BootHardware | no time is modelled before the OS |
| its one WDEBUG is stepped over | `emu/bootrom.py` | QEMU's ColdFire aborts on WDEBUG; it only configures the debug module |
| the OS's flash reads are served from the flash image | `emu/dspboot.py` | not through the OS's own DSPI driver |
| DSPI 0 and DSPI 2 status forced ready | `emu/longrun.py` | |
| waits on hardware the emulator lacks are satisfied (`unblock`) | `emu/longrun.py` | the DSP link on Digitone, MIDI, USB. Semaphores with a poster that can run are never faked (`emu/semscan.py`) |
| idle-loop reschedules | `emu/longrun.py` | stand in for the second interrupt controller's forced interrupts (INTFRCL2), which are not modelled |
| the eSDHC and its card, the SSI and its DMA, the PITs and DMA timers | `emu/esdhc.py`, `emu/ssi.py`, `emu/pit.py`, `emu/dtim.py` | models, measured against the firmware but not against the device |

## What none of it can see

- **The update itself.** The check starts from a flash that already holds
  the build. It does not send the `.syx` to the bootstrap's receiver
  ("READY TO RECEIVE") or to the OS, and it does not run the updater's
  flash writes. A build the updater would refuse passes the checks above
  only when the refusal is one of the container checks.
- **The serial boot facility and the flash's first sectors.** Step 1 of
  the boot is hardware. A custom *bootstrap* is refused by the container
  stage unless its version equals the stock one, and even then only its
  run from SRAM is checked.
- **The second processor** (section 8, ARM: USB and MIDI) never runs. On
  the Digitone the DSP runs; on the Digitakt there is none.
- **Caches.** No cache is modelled, so a DMA/cache coherency bug (a buffer
  written through the cached view and read by DMA) is invisible. The
  alias model makes both views the same bytes, as a cache flush would.
- **Interrupt masks and priorities** are approximated (docs/STATUS.md).
- **The emulated CPU** is Unicorn's ColdFire with six patches
  (`patches/README.md`). Known gaps are in docs/UNICORN.md. One EMAC
  corner, a saturated accumulator, is not modelled.
- **Anything the script does not do.** The run and compare stages see
  only the keys the script presses.

## Stock behaviour the check turned up

Run on the stock builds, the check found these. They are why `--baseline`
matters: a violation the stock build also makes is the stock build's, and
is marked `in_stock` instead of failing a custom build.

- **Stock Digitakt 1.53 reads address `0x00000000`** once during boot
  (`pc=0x40068f4c`), and **stock Digitone 1.43 reads `0x0000012A`**
  during the run (`pc=0x4009de70`), steadily: 86,262 times in the 8.3
  emulated seconds of the default tour. On the device that is FlexBus space
  with no chip select configured, so the bus monitor should end it with a
  bus error. The devices evidently tolerate it: either the monitor is off,
  or something answers there.
- **The emulator's usual start passes the OS boot flags 0; the bootstrap
  passes `0x00140000`.** The OS stores its argument at `0x401F55C0` and
  tests those bits in more than twenty places. The app still starts the
  OS directly. Only the check's boot stage starts it the device's way.
- **The firmware reaches its DDR through aliases** (above). In the app,
  the aliased pages are separate memory. Measured on saved sessions, no
  data was written through two aliases of one location. The check makes
  them one memory anyway.

## Key scripts

A script is plain text, one step a line, `#` for comments:

```text
wait 1000              run 1000 emulated ms
tap PLAY [HOLD] [AFTER] press and release a key (defaults 100 ms, 300 ms)
press FUNC [AFTER]     hold a key down
release FUNC [AFTER]   let it go
turn A +3 [AFTER]      turn an encoder by detents (default 150 ms after)
snap playing           name this moment: its screen is compared and saved
```

Key names are the device file's `[panel.labels]` (`PLAY`, `STOP`, `TRIG`,
`SRC`, the trig keys `1`..`16`...), or `#24` for a raw code. Encoders are
`A`..`H`. The default script visits each parameter page the product has
(`SRC` on the Digitakt, `SYN1` and `SYN2` on the Digitone), triggers track
1, plays the pattern for two seconds and stops (`emu/fwcompare.py`
`default_script`). The prepare stage checks a script against the panel
before anything boots, so a key the product lacks fails in seconds rather
than after the boot.

`python -m emu.fwcompare STOCK_FOLDER CUSTOM_FOLDER --script FILE` runs a
script on two firmware folders the app has already built, without the
other stages.
