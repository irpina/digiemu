# Status

Where digiemu stands, and what is left to do. Updated 2026-09-24. The dated
session handoffs in [history/](history/README.md) hold the detail behind each
line; where one disagrees with this page, this page is newer.

Target firmware: Digitakt mk1 OS 1.53, SHA-256
`9bdd44bb6102fb25c143cfab97bc92b7a89c463f795d3112dce89771e29bcc92`, and
Digitone mk1 OS 1.43, SHA-256
`c5a54cc05b921f2e4bd814834c5365c2a5aa01d7772a9a2961fac1c3095bf9aa`
([DIGITONE-MK1.md](../DIGITONE-MK1.md)).

## What works

- **Boot.** The firmware cold-boots to its live user interface. The intro
  needs its PIT3 tick delivered, and a few waits on hardware the emulator
  does not model are stood in for;
  [DIGITAKT-MK1.md](../DIGITAKT-MK1.md) explains which.
- **The panel.** Every key and encoder, including the encoder push switches,
  with multitouch (shift-click latches); the key LEDs and the pattern-page
  LEDs.
- **Audio.** Live at 48 kHz in real time, with the six-patch Unicorn. A
  desktop has about 2.5 times the speed live audio needs. The cost is per
  instruction, not per track: the firmware renders every voice on every
  pass, so eight busy tracks cost about 8% more than one.
- **The sequencer.** Patterns play, and PLAY/STOP work.
- **The +Drive.** An ekFS image in the firmware's own format, persisted in
  `plusdrive.img`. Projects save to it, and samples put in `/incoming` load
  into a project and play.
- **The Windows app.** Bring your own `.syx`. It identifies the device and
  version from the file's header, sets up in about 25 seconds, and saves
  the session when the panel closes. LOAD SAMPLES puts WAV files on the
  +Drive, and `digiemu-console.exe` does all of it without a window.
- **The Digitone (mk1).** Boots to its live UI, first run in 25 seconds in
  the app (a blank +Drive, which the firmware's first boot initialises), its
  own window (`emu/dnpanel.py`) with every key, encoder and key LED, and
  live audio at 100% of real time: the second CPU runs the firmware's own FM
  voice code (`emu/dsplink.py`) on a thread of its own, one render per
  audio block, about 80% of a second core.
- **Tests and CI.** Every test module runs on its own
  (`tools/ci/run-tests.sh`). None uses firmware bytes, and CI runs them
  with a content guard on every pull request.

## Known limits

- **The factory sample library is missing.** A real Digitakt keeps it on
  its own storage, not in the firmware, so `/factory` is empty and sounds
  that use it are silent.
- **44.1 kHz samples:** each sample keeps its rate in its header, and the
  engine reads it. Only a 48 kHz sample has been checked against its
  rendered output, so the pitch of a 44.1 kHz sample is not verified.
- **Interrupts, approximated.** The interrupt controllers' mask registers
  are not modelled. The software-forced reschedules on the second controller
  (`INTFRCL2`) are not delivered either; PIT0 ticks and the idle-loop yield
  stand in for them.
- **First boot, faked wait.** During first boot, the main task's wait on
  `0x421cd074` is satisfied by the emulator. The firmware posts it through a
  path the semaphore scan does not know, so the main page draws during the
  factory install. It affects fidelity only.
- **Long sessions.** Once, after about an hour idle, the panel went blank
  (the main loop was still alive). Not investigated. The panel's memory use
  over multi-hour sessions has not been measured.
- **The app is unsigned**, and its Control Flow Guard flag is cleared,
  because Unicorn's `longjmp` fails under it.
- **Digitone Keys.** The OS file is shared, but digiemu runs it as a plain
  Digitone (boot argument bit 19 clear): no keyboard, wheels or Keys-only
  keys.
- **Digitone details.** Two key LEDs (PAGE, FUNC) come from the firmware's
  table and have not been seen lit. MIDI, audio in and the DSP's FPGA are
  not modelled beyond boot. Live DSP renders run on their own thread, so a
  live session is not instruction-for-instruction repeatable; batch runs
  are.
- **Other devices.** The Digitakt II and Digitone II paths from upstream
  digikit still work as upstream left them.

## Open work, in order

1. **Check a 44.1 kHz sample's pitch:** render a known tone and measure it.
   If it is off, resample to 48 kHz in `emu/ekfsformat.py`'s
   `wav_to_sample`.
2. **Test the Windows app on the slow laptop:** Add, Play, LOAD SAMPLES,
   Rebuild, Reset, Cancel, and quitting mid-build. Measure the live-audio
   headroom there with `tools\capbench.py --tracks 8`.
3. **Code signing,** if digiemu is shared widely. Tagged releases are built
   by `.github/workflows/release.yml` (README, "Releasing").
4. **The second interrupt controller's forced reschedules.**
   `emu/intfrc.py` would cover them with `base=0xFC050000`,
   `first_vector=192`, but the RTOS switcher is sensitive, so measure before
   switching it on.
5. **Model the interrupt masks.** Snapshots hold a zeroed INTC page, so the
   mask has to be replayed from the ICR levels.
6. **Redo the sequencer checks at full speed.** The checks in
   [DIGITAKT-MK1.md](../DIGITAKT-MK1.md)'s Known limits (the PLAY/STOP words,
   the starvation threshold) were all run at 4.68M instructions a second
   with 2 kHz audio. They are worth repeating at 64M with 48 kHz.
7. **A faster first run, for CI.** A `--card-from NAME` option that starts a
   folder from a copy of another folder's initialised +Drive would skip the
   factory install (about 10 of the 25 seconds). After that, the intro's
   exact idle-loop hook is the largest cost.
8. **More headroom,** if a slower PC needs it: move the SSI half-buffer
   batch and the timer register reads into native code (Python is about 30%
   of a live run), then the MAC helper (about 20%).
9. **Digitone:** test on the slow laptop (it needs a second core for the
   DSP); the Digitone Keys (bit 19, the keyboard on panel tags 5/6, the
   wheels on the ADC, codes 55–72); a panel sweep that sees the PAGE and
   FUNC LEDs lit.
10. **Upstream:** digikit would benefit from the card-identification (CID)
    fix, the INTC verification, and the ekFS index and sample format.
