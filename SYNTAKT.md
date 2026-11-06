# Syntakt emulator (work in progress)

digiemu boots Elektron Syntakt OS 1.40A on the same emulated ColdFire
MCF5441x as the Digitakt mk1 and the Digitone mk1, runs its second ColdFire
(section 7) the way it runs the Digitone's, and reaches the live user
interface with the panel, the sequencer and parameter locks working.

Tested with `Syntakt_OS1.40A.syx`, SHA-256
`266a5a3022ff565bf470173147b7f3005b7da8ef80724b6271836818ac2b5828`.

## What works, and what does not

| | |
|---|---|
| main CPU | MAIN OS from a cold boot through the intro to the live UI; first boot initialises a blank +Drive |
| panel | every key and the nine encoders, by the firmware's own panel-test names (`devices/syntakt.toml`) |
| sequencer | patterns play, trig locks apply on their step (live audio pace, see below) |
| second CPU | boots through the section 6 / section 7 handshake and renders every block: BD MODERN, SD BASIC, CY ALLOY, PC CARBON, SY TONE, SY CHORD, SD VINTAGE, CP VINTAGE, SY TOY, SY BITS, SY SWARM, SP TWINSHOT |
| not modelled | the FPGA (section 1): the original digital machines run there, and the voices go there; the analog tracks; so the main outputs are **silent** |
| not checked | LEDs (no `emu/panelleds.py` seed yet), MIDI, USB audio |

The second CPU's output can be read where it hands each block to the shared
RAM (its eDMA channel 47 start), per voice; that is how the machines above
were heard. Nothing mixes it into the SSI output yet.

## Running it

As for the other products: **Add firmware** in the app, or

```sh
uv run python -m emu.portable --add Syntakt_OS1.40A.syx
```

On the reference desktop the add takes about 45 s: the cold boot parks at
~260M instructions, the intro reaches the UI in ~9 s, and the first boot
settles at 1500M (~23 s). Acceptance requires the second CPU's status word
at 6 (below). A fresh `gui.snap` opens on a "RUN WARM-UP CALIBRATION?"
dialog; NO dismisses it.

## What the Syntakt needs that the others did not

All addresses are OS 1.40A's; the rules that find them are signatures in
`emu/symbols.py`.

### The board (`emu/synboard.py`) **[V]**

- **DMA timer 2.** The audio bring-up (`syn_ssi_sync`, 0x40000f66) waits for
  DTCN2 to pass the next 32K boundary plus 64K. The bootstrap leaves DTIM2
  counting an external clock; nothing drives it here, so every read advances
  it by 0x2000. Which clock the pin carries is open **[O]**.
- **eDMA channel 29.** The second CPU's boot task streams section 6 and then
  section 7 out of DSPI2 on channel 29 and waits for its major-loop interrupt
  (vector 149, INTC1 source 21). The Digitone polls DSPI2 instead. A start of
  channel 29 completes at once.
- **DSPI1.** The audio render (vector 191) waits for DSPI1's end of queue
  before it starts; reads of its SR report EOQF and TCF.
- **The board id.** MAIN OS reads a revision byte at address 0 and below 3
  shows "TOO OLD HARDWARE" and keeps the audio engine paused, so no frame
  ever reaches the second CPU. The board answers 4.
- **MIDI.** The MIDI input task (`syn_midi_task`) pends on a semaphore its
  drivers post from callbacks; faked, it spins at priority 7 and starves the
  second CPU's handshake. It is never faked.

### The second CPU (`emu/dsplink.py`) **[V]**

- The wiring is the Digitone's: PB6 is its reset, PA4 the main CPU's line to
  it, PG2 its line back (edge port 4, vector 68), the shared RAM at
  0x10000000 / 0.
- **Section 6 is what the main CPU first hears from.** Out of reset the
  loader raises PG2 and waits for an edge on PA4; then section 7 runs with
  PG2 still high. `DspCpu(loader=True)` stands in for section 6 and loads
  section 7 where section 6 would have.
- The handshake (`syn_dsp_boot_task`): 'HO'/'HA', 'B0', 0xA5A5, then the
  upload commands. Its status word (`syn_dsp_status`) reads 6 once the second
  CPU runs; 1..5 are its failures. The link's block and hello semaphores
  (`syn_dsp_link_init`) are posted from the edge-port handler and are never
  faked.
- **Section 7's entry** is its first longword (0x40001070), with its idle
  `bra.b *` right before it, as on the Digitone (`section7_entry`).
- **bzero.** Section 7 clears its 64 MB sample memory byte by byte at boot
  (256M instructions); the main CPU's boot watchdog gives up long before. The
  routine is found by signature and done on the host (`find_bzero`).
- **Its own eDMA channel 47.** The Syntakt's second CPU hands each rendered
  block to the shared RAM itself (SSRT start, 64 linked minor loops of 16
  bytes) and waits for DONE before the next render; it gets its own software
  eDMA bank.

### Audio pace

The SSI profile (`emu/ssi.py`, `syntakt`) is the Digitakt mk1's SSI1 and
eDMA 52/54 with a TDM frame of eight 32-bit slots. The audio render needs
about 85k cycles of a 167k-cycle block (`emu.fwcheck --timing`: margin 49 %
at 250 MHz). At the GUI's instruction pace (`emu.pit.INSTR_PER_SEC`) a block
is far too short for it, the render runs back to back at IPL 5, and the
sequencer's forced tick (INTC0 source 44) is never taken: run live audio at
the device's `[audio] ips` for patterns to advance.

## The panel

`devices/syntakt.toml` lists every position (`[panel.exceptions]`, measured
with `tools/panelsweep.py --gui`) and names them from the firmware's
factory-test tables: names at 0x401c3660, codes at 0x401c3770. 2/4 reports
no key. Codes 45..48 are encoder pushes the table does not name (F, G, H and
LEVEL/DATA in some order) **[O]**. Encoder channels 0..7 are A..H and 8 is
LEVEL/DATA; `encoder_counts = 4`, as on the mk1s (the same dead zone).

The window (`emu/synpanel.py`) places those keys on the Digitakt window's
plan; it is not the Syntakt's faceplate.

## Known limits

- **Silent.** The original digital machines, the analog tracks and the
  voice chain after the second CPU run in the FPGA, which is not modelled;
  the SSI output carries nothing.
- **Panel window.** It uses the Digitakt window's plan, not the Syntakt's
  faceplate, and codes 45..48 are not named by the firmware's table.
- **Not checked:** LEDs, MIDI, USB audio, the analog tracks' controls.
- **Run mode.** Sequencer ticks need live audio at the device's `[audio] ips`
  (see Audio pace).

## Files

| | |
|---|---|
| `emu/synboard.py` | the Syntakt's board: DMA timer 2, eDMA 29, DSPI1, board id |
| `emu/dsplink.py` | the second CPU, its wiring, the section 6 stand-in |
| `emu/synpanel.py` | the Syntakt window (on the Digitakt window's plan) |
| `emu/ssi.py` | the `syntakt` SSI profile |
| `emu/symbols.py` | the `syn_*` signature rules |
| `devices/syntakt.toml` | identity, wire map, panel names, audio |

## Open

- Mix the second CPU's voices into the SSI output, so the app is not silent.
- The FPGA: its bitstream is section 1; the original machines, the voice
  chain after the second CPU and the outputs live there.
- LED seed addresses for 1.40A.
