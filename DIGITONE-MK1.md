# Digitone mk1 emulator

digiemu runs Elektron Digitone (mk1) OS 1.43 on the same emulated ColdFire
MCF5441x as the Digitakt mk1 ([DIGITAKT-MK1.md](DIGITAKT-MK1.md)), and adds
the second CPU the Digitone renders its FM voices on. The firmware boots to
its live user interface, the Digitone window plays it with every key,
encoder and key LED, and the voices are rendered by the firmware's own code
live at 48 kHz.

Tested with `Digitone_and_Digitone_Keys_OS1.43.syx`, SHA-256
`c5a54cc05b921f2e4bd814834c5365c2a5aa01d7772a9a2961fac1c3095bf9aa`. One OS
file serves the Digitone and the Digitone Keys. digiemu runs it as a plain
Digitone.

## Running it

In the app, **Add firmware** with the `.syx`, then **Play**. From source:

```sh
uv run python -m emu.portable --add Digitone_and_Digitone_Keys_OS1.43.syx
uv run python -m emu.portable          # then Play
```

The first run takes about 25 seconds on the reference desktop: a cold boot
to where the firmware parks (~130M instructions, 2 s), the intro to the live
UI (8 s) and the firmware's own first boot, which initialises the blank
+Drive (13 s, settled at ~1300M). The acceptance check then requires the
card's first-boot layout and the DSP's status word at 2 (running).

To open the window on a snapshot directly, set the paths the app would
(`FirmwarePaths.env()` in `emu/bootstrap.py`: `DT2_SYX`, `DT2_SECTIONS`,
`DT2_SNAPSHOTS`, `DT2_PLUSDRIVE`, `DT2_MAIN_IMG`, `DT2_DEVICES`) and run:

```sh
uv run python -m emu.dnpanel SNAPSHOT [--syx PATH] [--save-on-exit PATH] [--no-audio]
```

## What is shared with the Digitakt mk1

The main CPU runs section 3 at 0x40000400. The code is a close relative of
the Digitakt mk1's: the RTOS sits 0x160 further on, and the Digitakt's symbol
rules resolve on the Digitone with a handful of extra signatures
(`emu/symbols.py`: `pend_b`, `intro_done`, `intro_pit3_isr`, `intro_park`,
`sd_bringup`, `view_activate`, and `_ssi0_dma_force_tail_dn`). So the boot,
the intro policy (PIT3 delivered through the intro), the settle, the panel
wire protocol, the LED stream and the audio path (SSI1 via eDMA channel 54,
INTC1 source 63 forcing the render on vector 191) are the Digitakt's, as
selected by `devices/digitone.toml`.

Differences the device file carries:

- **Card.** No sample engine, so no ekFS volume: `[card] ekfs = false`. The
  app creates a blank card, and the firmware's first boot writes sector 0
  (BEEFBACE), the 0x800 record, the sounds at 0x1000 × 512 and the project
  slots at 0x80000 × 512. That is the Digitakt's layout without the sample
  volume.
- **Panel.** Codes 1..54, all measured; the wire positions are not linear.
  72 LEDs in 18 selector groups, where the Digitakt has 44 in 11. The
  encoder table has a NULL entry 0 (rotation codes start at 1).
- **Encoders.** `[panel] encoder_counts = 4` (the Digitakt's file has it
  too). The encoder driver (0x400dbd72) has a dead zone: an idle knob's
  state rests at 48, each count takes 3 off it, and nothing steps until it
  is at or below 24; for some parameters every step adds 48 back. At one
  count per mouse-wheel notch a knob needed ~16 notches before anything
  moved.

## The second CPU

The Digitone has two MCF5441x CPUs. The second, which the firmware's
strings call the DSP ("DSP BOOT FAILURE"), runs section 7 and renders the
eight FM voices. `emu/dsplink.py` runs it.

**Wiring.** The CPUs share a 128 KB dual-port RAM: the main CPU sees it on
FlexBus at 0x10000000, the DSP at 0x0. Each interrupts the other through a
GPIO:

- main PB6 is the DSP's reset;
- DSP PG2 drives the main edge port, pin 4 (vector 68);
- main PA4 drives the DSP's DMA timer 0 capture input (vector 96).

**Boot.**

1. A main task (0x4008d56c, `dsp_boot_task`) holds the reset and streams
   section 6, a serial-boot loader, and section 7 out of DSPI2 (0xEC038000)
   as an SPI slave, using eDMA channel 29. Then it releases the reset.
   Section 6, on the DSP, is the SPI master.
2. Section 6 loads section 7 at 0x40000400 and jumps to 0x40000b92.
3. Section 7 loads an FPGA bitstream over DSPI1 and clears the shared RAM.
   Then it shakes hands:
   - it writes 'HO'/'HA' and toggles PG2;
   - the main CPU's edge-port handler (0x4008d850) answers 1 at +2, then
     'B0' at +0;
   - it writes 0xA5A5 and toggles PG2 again.
4. The handler then sets the status word 0x4137b720 to 2 (running). The
   word is 0 while a boot is in progress. It becomes 1 ("DSP BOOT FAILURE")
   when section 6 or 7 is missing, or when the boot task's watchdog runs out
   on the third attempt.

**Running.**

1. Every 32-frame block, the SSI1 transmit DMA's interrupt (eDMA channel
   54, 0x4009c2e4) toggles PA4, once the DSP is running.
2. The edge fires the DSP's vector 96.
   - During the handshake that vector holds a handler (0x40000406) that
     restarts the bring-up.
   - After it, the vector points to a trampoline on the DSP's stack, which
     jumps to the render at 0x400008fa.
3. The render:
   - copies the voice parameters from shared RAM 0x000..0x39F into its
     SRAM;
   - renders eight voices × 32 samples (Q1.31, on the EMAC);
   - copies them to 0x3A0..0x79F;
   - returns to its idle `bra.b *` at 0x40000b90.
4. The main CPU's render (vector 191) runs eDMA channel 47 twice a block,
   through the SSRT register: once to fetch the voices, once to write the
   next parameters. It mixes the voices with its effects.

After boot, the main CPU never waits on the DSP.

**How it is emulated.**

- `DspCpu` runs section 7 on a second Unicorn engine with its own scoped
  ISA patches and exception delivery.
- The shared RAM is one host buffer, mapped into both engines.
- Section 6 is not run: section 7 is loaded where section 6 would put it.
- Three reads are answered so section 7 gets through its bring-up: PIT1's
  flag and the FPGA's INIT_B and DONE pins. Without any one of them it
  retries forever.
- The boot runs in slices at the main CPU's step boundaries. After it, each
  PA4 edge is one render: ~18.4k instructions, 0.22 ms alone and ~0.35 ms
  beside the main CPU on the reference desktop, at 1500 blocks a second.
- Batch runs (first run, tools) render at the next step boundary, so they
  are deterministic.
- The live window starts a render thread. ctypes releases the GIL inside
  Unicorn, so renders run in parallel with the main CPU; the DSP then uses
  about 80% of a second core. That is what keeps the Digitone at 100% of
  real time with audio. Rendering inline reached 83–95%.
- The main thread takes each PA4 edge off the one-slot latch as it hands
  the render over. At first the worker took it when it woke, and a block
  whose edge came before then was merged into the previous render: about a
  quarter of blocks reused stale voices (8485 renders for 11480 blocks).
  `test_a_slow_thread_still_renders_every_edge` covers it.
- The DSP's whole state (registers, pages, pending edges) is part of every
  snapshot (`DspCpu` checkpoint state).

`DspLink` stands in for the handshake alone when section 7 is not
available. The main OS then runs as on a Digitone whose DSP came up, with
no voices.

**The request semaphore is never faked.** The boot task waits on 0x4137b70c,
which the main CPU posts through 0x400019a0, a post routine that
`emu/semscan.py` does not scan. Faked, the task re-uploads the DSP thousands
of times a second, and the settle sticks on "INITIALIZING +DRIVE".

**Guest memory from inside a hook.** The eSDHC model's first EXT_CSD read
landed on a page nothing had touched. Mapping it inside the memory hook
resized Unicorn's TLB under the running access and crashed the host.
`Machine.poke`/`peek` (`emu/harness.py`) never map: a write to an unmapped
page waits until the page is mapped, and snapshots flush it first.

## Measurements

On the reference desktop, OS 1.43:

| | |
|---|---|
| Cold boot | parks at ~130M instructions, 10 tasks, 2 s |
| Intro | live UI; `intro_done` fires once; 8 s |
| First boot (settle) | 120 quiet main-UI frames at ~1300M, 13 s |
| First run in the app | 25 s |
| Panel | 64M instructions/s, 100% of real time with live audio, 0 dropouts |
| DSP render | ~18.4k instructions a block; one render per block (11480 for 11480) |
| DSP thread | ~80% of a second core |
| Output, factory pattern | RMS ~1.8k, peak ~6.7k (16-bit), silent at idle |

## Known limits

- **Digitone Keys.** Boot argument bit 19 (at 0x402292f0) selects the Keys
  behaviour, and the emulator passes 0. The Keys' extra controls (a 37-key
  keyboard on panel tags 5 and 6, the wheels on the main CPU's ADC, and codes
  55–72) are not in the device file or the window.
- **MIDI, audio in and the FPGA** are not modelled beyond what boot needs.
- **Two LEDs, PAGE (25) and FUNC (34),** come from the firmware's key/LED
  table and have not been seen lit.
- **Live renders are not deterministic** with respect to the main CPU's
  instruction count (they run on their own thread). Batch runs are.
- The factory *sounds* are in the firmware and play. No sample library is
  involved.

## Files

| | |
|---|---|
| `emu/dsplink.py` | the second CPU, its wiring and the handshake stand-in |
| `emu/dnpanel.py` | the Digitone window (on `emu/dtpanel.py`'s machinery) |
| `devices/digitone.toml` | identity, wire map, labels, LEDs, card, audio |
| `emu/panelleds.py` | the LED stream; the Digitone's seed addresses |
| `emu/edma_sw.py` | eDMA, including SSRT starts (channel 47) |
| `tests/test_dsplink.py` | the DSP model on a synthetic section 7, no firmware |
