# History

Dated session handoffs. Each was written at the end of a working session for
whoever picked the work up next: what changed, what was measured, what was
still open. They are kept as written. Later sessions corrected some of their
claims, and each handoff lists the corrections to the ones before it, so for
the current state read [../STATUS.md](../STATUS.md) and the reference
documents ([DIGITAKT-MK1.md](../../DIGITAKT-MK1.md),
[docs/mk1/](../mk1/00-INDEX.md)).

## Digitakt mk1 (this fork)

Newest first. Each one continues the one below it.

| Date | Handoff | Headline |
|---|---|---|
| 2026-09-24 | [HANDOFF-2026-09-24.md](HANDOFF-2026-09-24.md) | The Digitone mk1 runs: its second CPU renders the FM voices live, its own window, first run in 25 s. The mk1 encoder dead zone; a DSP thread race found and fixed. |
| 2026-09-23 | [HANDOFF-2026-09-23.md](HANDOFF-2026-09-23.md) | Live 48 kHz audio in real time, and patterns play. The sixth Unicorn patch for headroom; the portable Windows app, digiemu; the first run cut from 8.5 minutes to ~25 s; LOAD SAMPLES. |
| 2026-09-22 | [HANDOFF-2026-09-22.md](HANDOFF-2026-09-22.md) | A sample loads into a project: the +Drive writer's directory indexes, sample format and content hash, each found by measurement. Key names settled, key LEDs working. |
| 2026-09-21 | [HANDOFF-2026-09-21.md](HANDOFF-2026-09-21.md) | A load-bearing earlier conclusion was wrong. The sequencer advances; the "+DRIVE" overlay diagnosis corrected; new firmware documents on the +Drive and audio. |
| 2026-09-20 | [HANDOFF-2026-09-20.md](HANDOFF-2026-09-20.md) | The mk1 boots to its live user interface. The flicker, +Drive flash and slowdown investigation; the native Windows run; the whole key map. |

## Upstream: Digitakt II and Digitone II

From [m-dwyer/digikit](https://github.com/m-dwyer/digikit), before this fork
(upstream commit `a5643ba`). They describe the Digitakt II and Digitone II
work that digiemu grew out of, and are kept for reference.

| Date | Handoff | Headline |
|---|---|---|
| 2026-09-13 | [upstream/HANDOVER-2026-09-13.md](upstream/HANDOVER-2026-09-13.md) | The front panel works on both builds: its wire protocol, device files, and why the emulator was `count=`-bound rather than CPU-bound. |
| 2026-09-12 | [upstream/HANDOVER-2026-09-12.md](upstream/HANDOVER-2026-09-12.md) | Both builds reach the running main OS; the SHARC DSP. |
| 2026-09-11 | [upstream/HANDOVER-2026-09-11.md](upstream/HANDOVER-2026-09-11.md) | Digitakt II 1.15C boots to its main OS and renders; the open Digitone bug. |
| — | [upstream/HANDOVER.md](upstream/HANDOVER.md) | The cold-start handover: the Digitakt II main OS boots and draws, and the job pipeline is what stalls. |
| — | [upstream/NEXT.md](upstream/NEXT.md) | Upstream's "start here" guide to its emulator and patcher. |
