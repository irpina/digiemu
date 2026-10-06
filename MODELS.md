# Model:Cycles and Model:Samples

digiemu runs Elektron's Model:Cycles and Model:Samples, OS 1.13, on the same
emulated ColdFire MCF5441x as the Digitakt mk1 ([DIGITAKT-MK1.md](DIGITAKT-MK1.md)).
Both boot to their live user interface, one window plays both with every key,
pad and knob and the key LEDs, and the firmware's own engine renders live
48 kHz audio.

Tested with `model-cycles_OS1.13.syx`, SHA-256
`44fe586269631a0ca7da25a3383fc6733c314809505fc3cc52f1e0ed9800640c`, and
`model-samples_OS1.13.syx`, SHA-256
`e11859b68deb7e5e3fe86ab32581212093849c4be5d3950add011eac398a2ce8`. Their
OS files carry SysEx ids 0x11/0x0C and 0x0F/0x0A.

## Running it

In the app, **Add firmware** with the `.syx`, then **Play**. From source:

```sh
uv run python -m emu.portable --add model-cycles_OS1.13.syx
uv run python -m emu.portable          # then Play
```

On the reference desktop the Model:Samples sets up in about 16 seconds and
the Model:Cycles in about 36. The Cycles' first boot writes its factory
sounds to the +Drive, and its screen redraws rarely while it does, so its
settle needs about 600M instructions to see 120 quiet frames. The window
runs both at 100% of real time with live audio.

The window is `emu/mdpanel.py`:

```sh
uv run python -m emu.mdpanel SNAPSHOT [--syx PATH] [--save-on-exit PATH] [--no-audio]
```

## What is shared with the Digitakt mk1

The OS file is the same container: a bootstrap (section 2), the main OS at
0x40000400 (1.7 MB, against the Digitakt's 2.5 MB) and the updater. There is
no ARM USB/MIDI controller (the Digitakt's section 8). Every symbol digiemu
needs to boot resolves by signature on both, and so do the display, view
system, soft-float, SD card and main-loop symbols. The display is the
Digitakt's 128x64 buffer pair; its pointers are lower in memory
(0x401492f0 on the Cycles), so the search for them now starts at 0x40100000.
The bootstrap programs 128 MB of DDR, as the Digitakt's does.

MIDI is the Digitakt's DIN port, unchanged: UART9 in through eDMA channel 36
and vector 156, out through channel 37 (emu/midi.py). Notes on channels 1 to
6 play the six tracks.

## What the board adds (emu/modelboard.py)

Four things the Digitakt does not have, all installed only when
`emu/symbols.py` finds `model_scan_start`, the Models' panel-scan handler:

### The front panel

The main CPU scans the panel itself; there is no panel MCU. A FlexBus device
at 0x8C000000 multiplexes the keys, encoders and LEDs, and the ADC reads the
pads. PIT3 runs the scan as a chain of interrupt handlers that re-point
vector 208 at each other (addresses on Model:Cycles 1.13):

- `0x40059cd0` starts a frame: it selects column 0.
- `0x40059d2c`, eight times: read column N at 0x8C000002 and an ADC sample,
  then select column N + 1 by writing `(N + 1) << 5 | 1` there. After the
  eighth it forces INTC1 source 2, whose handler (vector 130, `0x40059e64`)
  debounces the keys, decodes the encoders and hands the pads on.
- `0x40059db8`, three times: drive one of seven LED rows, writing
  `(row bits << 8) | row << 5` to 0x8C000002, active low.

Columns 0 to 3 are 32 keys, a set bit pressed: the 16 trigs, 15 function
keys and PITCH's push switch. Columns 4 to 7 are 16 encoders, two bits each
(encoder *e* is bits 2(*e* mod 4) and 2(*e* mod 4) + 1 of column 4 + *e*/4);
the firmware decodes them in quadrature, one step per two Gray-code
transitions. Pad *n* is ADC channel 6 − *n*: it arms above about 0x2148 in
the 16-bit sample, takes its velocity from the next sample (127 at 27824),
and is released below about 0x199A once it has been down for 30 frames.

`ModelPanel` is that device. It counts frames (a read of column 0 starts
one) and applies the window's input on frame boundaries, holding each key,
pad and encoder phase for as many frames as the firmware needs. A frame is
12 PIT3 periods, about 2 ms.

The names, codes and LEDs come from the firmware's factory UI test
(`0x400521c8`), which lists each key's name, its code and its LED, each pad
and each encoder. Its LED numbers are logical; a table at `0x4010abfc` maps
them onto the matrix. Every key was then pressed from a fresh resume and
judged by what it drew and lit (the Cycles 1.13 and Samples 1.13 tables are
identical), and every encoder turned and named by the parameter it showed.
The Cycles' MACHINE, PUNCH and GATE are the Samples' WAVE, LOOP and FLIP,
and five knobs differ (COLOR, SHAPE, SWEEP and CONTOUR against SMPL START,
SMPL LENGTH, CUTOFF and RESONANCE; Track Swing and Chance against
SWING/NUDGE and CHANCE/COND). PITCH's push switch is code 32: the key
callback asks whether PITCH turned while it was held.

### The audio codec

The codec is set up over I2C0, at address 0x1A (`emu/i2c.py`). The firmware's
codec task (`0x40044188`) reads register 1 back on every pass and sets the
codec up again unless it reads 0x17, so the codec is a register file that
keeps what is written. The task waits on a semaphore that a software-timer
callback posts through a primitive `emu/semscan.py` does not know, so it is
never faked: faked, the task spun on the I2C bus and the run fell to a
tenth of real time.

### The delay timer

The busy-wait delay (`0x4008f46c`) counts PIT1 wraps by polling PIF. PIT1 is
not otherwise modelled, so PIF reads as set and every delay is instant, as
for the Digitone's DSP.

### The scan's processing interrupt

It is software-forced on INTC1, so `emu/intfrc.py`'s class watches INTC1
source 2 as well as INTC0's sources.

## Audio

The Models transmit on SSI0 through eDMA channel 50, the Digitakt II's, with
the Digitakt mk1's buffer shape (a 512-byte ring, 8 bytes a minor loop,
64 a major loop, half and full interrupts), and receive nothing: neither has
an audio input. `emu/ssi.py` has a `models` profile, and a profile's
receive channel may now be absent. The SSI's clock and word-length
registers are the Digitakt mk1's, so the output is 48 kHz.

On the Model:Samples a 48 kHz, 440 Hz test tone, loaded with LOAD SAMPLES
and played from a pad, came out at 440 Hz (442 by a zero-crossing count).

## The +Drive and samples

Both Models format their +Drive's ekFS volume with the Digitakt's routine
and constants (sector 0x1C0000, the same block layout), so digiemu prepares
their card as it prepares the Digitakt's, and accepts a first boot only once
the volume is mounted. On the Model:Samples, LOAD SAMPLES puts WAV files in
`/incoming`; after the rebuild they are in the sample browser: WAVE opens
it, LEVEL/DATA moves through the folders, and PITCH's push opens a folder and
picks a sample for the track.

The Model:Samples' factory samples are on the real device's storage, not in
the firmware, so its factory patterns are silent until samples are loaded.
The Model:Cycles' factory sounds are in the firmware and play from the first
boot.

## Checking a build (Check firmware...)

`emu.fwcheck` runs on the Models as on the Digitakt. Two things about their
bootstrap are in the device files' `[boot]`: it reads the OS container at
0x20000 in the SPI flash, not 0x80000; and at power-up it can wait for the
power button (GPIO 0xEC094018 bit 2, high while pressed) and switch the
board off unless it is held, so the emulated bootstrap sees it held. The OS
reads the same bit as released: it powers off on a long press.

The default key script visits the MACHINE (or WAVE) and LFO pages and plays
track 1 from its pad. A check of each stock build against itself passes,
with identical screens and sound, in about 4 minutes (Cycles) and 3
(Samples). Run alone, the stock builds break strict mode's rules in their
own ways (the Cycles writes to unmapped FlexBus space at boot, the Samples
runs two opcodes outside ColdFire ISA_C), which is why a build is judged
against its stock build.

## Not modelled

- USB audio and USB MIDI.
- The battery (Power Handle) and its power-on sequence beyond the
  bootstrap's check.
- The pads' pressure after the hit: a pad reads as one velocity until it is
  released.
