"""A headless, repeatable firmware session: the window's machine, run by script.

The window (emu/gui.py) runs the firmware for a person. It paces itself to
the wall clock, takes input whenever a click arrives, and skips idle time,
so no two sessions are the same. A check needs the opposite: the same input
at the same emulated moment on every run, and a record of what the firmware
drew and played. A Session is that. It stands up the machine the window
builds (the same build flags, timers and audio path), then steps it in
emulated time only, delivering key presses and encoder turns at emulated
milliseconds and keeping every frame the firmware finished and every audio
sample it rendered.

Two runs of the same Session on the same firmware and snapshot give the same
frames and the same audio, byte for byte (tests/test_session.py). That is
what lets emu/fwcompare.py say a difference between a stock and a custom
build is the firmware's.

Options that change what is measured:

  hle=False     run the firmware's own float and setPixel routines instead
                of the host shortcuts (emu/softfloat.py, emu/hle.py). The
                results are the same bits; the instructions are the
                firmware's. Timing needs this.
  timing=dict   count core cycles with emu.cftiming.CycleClock and step the
                timers in cycles: the firmware then sees time pass as fast
                as the tables say the MCF5441x would run it. Keys are
                CycleClock's (fsys, accel, stalls, icache, miss_penalty).
  strict=dict   hold the firmware to the MCF5441x (emu/strict.py): the
                memory map, the instruction set, exceptions, the watchdog.
                Keys are Strict's (stop, rambar); read strict_report().
  ddr=bytes     decode the SDRAM space as the device does, every alias the
                same memory (harness.Machine.set_ddr). A snapshot opens only
                under the DDR model it was saved with.
  on_machine    a callable(session) run once the machine is built and
                before it runs, for probes.
"""
import os

from emu import (audioout, config, device as devices, edma_sw, intfrc,
                 longrun, panel, panelin, symbols)
from emu.dtim import Dtims, Timers
from emu.pit import INSTR_PER_SEC, Pits, intro_running

# Input and bookkeeping happen between steps of this many emulated ms.
CHUNK_MS = 5


def saved_ssi0(path):
    """-> the snapshot manifest's SSI0 entry, or None (as emu/gui.py)."""
    from emu.snapshot import _SnapshotUnpickler
    try:
        with open(path, 'rb') as fh:
            blob = _SnapshotUnpickler(fh).load()
        entry = (blob.get('manifest') or {}).get('ssi0_dma')
    except Exception:                                   # noqa: BLE001
        return None
    if isinstance(entry, dict) and isinstance(entry.get('request_hz'), int):
        return dict(entry)
    return None


def encoder_number(name):
    """-> the encoder rotation code for 'A'..'Z' or a number (1-based), or
    None. Whether this product has that encoder is the device's to say
    (Device.encoder_channel)."""
    text = str(name).strip().upper()
    if text.isdigit():
        return int(text)
    if len(text) == 1 and 'A' <= text <= 'Z':
        return ord(text) - ord('A') + 1
    return None


class SessionError(RuntimeError):
    pass


class Session:
    def __init__(self, snapshot, syx=None, *, audio=True, hle=True,
                 timing=None, strict=None, ddr=None, on_machine=None,
                 verbose=False):
        self.snapshot = snapshot
        self.syx = config.firmware(syx)
        self.verbose = verbose
        self.device, _fw = devices.identify(self.syx)
        with open(config.main_image(), 'rb') as fh:
            self.main_img = fh.read()
        self.profile = prof = symbols.resolve(self.main_img)
        intro_except = ()
        if not self.device.intro_unblocks_frame_sem and prof.frame_sem is not None:
            intro_except = (prof.frame_sem,)

        cfg = self.device.audio if audio else None
        saved = saved_ssi0(snapshot)
        audio_kw = {}
        if cfg:
            if saved:
                audio_kw = dict(ssi0_request_hz=saved['request_hz'],
                                ssi0_profile=cfg['ssi_profile'])
            else:
                audio_kw = dict(ssi0_request_hz=cfg['request_hz'],
                                ssi0_legacy_upgrade=True,
                                ssi0_profile=cfg['ssi_profile'])
        elif saved and self.device.audio:
            # The snapshot carries the audio model: it has to be built, and
            # is then simply not recorded.
            cfg = self.device.audio
            audio_kw = dict(ssi0_request_hz=saved['request_hz'],
                            ssi0_profile=cfg['ssi_profile'])
            audio = False
        self.audio_cfg = cfg
        self.record_audio = bool(audio and cfg)

        self.frames = []            # (ms, bytes), each a newly latched frame
        self.pcm = bytearray()      # 16-bit LE stereo at audio_cfg['rate']
        self._raw = bytearray()
        self.inputs = []            # (ms, what) as delivered
        self.ms = 0.0
        self.halted = None

        # A snapshot opens only under the DDR model it was saved with.
        relax = () if hle else ('softfloat', 'bitmap')
        m, ev, st, pc, inq, at = longrun.build(
            snapshot, syx=self.syx, unblock=True, softfloat=hle, bitmap=hle,
            dsp=True, unblock_except=intro_except,
            deferred_components=('timers',), fast_idle=False,
            manifest_relax=relax, ddr=ddr, **audio_kw)
        self.m, self.ev, self.st, self.pc, self.at = m, ev, st, pc, at

        pits = None
        restore = ev.get('restore_checkpoint_timers')
        if restore is not None:
            try:
                pits = restore()
            except Exception as exc:                    # noqa: BLE001
                if verbose:
                    print('[session] saved timers unusable (%s)' % exc)
        intro = intro_running(m, prof.intro_pit3_isr)
        if intro:
            raise SessionError('the snapshot is still in the boot intro; a '
                               'session starts from a settled user interface')
        if pits is None:
            pits = Timers(Pits(m, hold=False), Dtims(m, channels=(3,), hold=False))
        self.pits = pits

        self.sources = ()
        if audio_kw:
            ssi = ev['ssi0_dma']
            ssi.batch = True
            self.sources = (ssi, edma_sw.install_bank(m, ev),
                            intfrc.install(m, ev))
            ssi.sink = self._raw.extend

        # The emulated clock: instructions a second, or core cycles a second.
        self.clock = None
        self.stepper = True                         # longrun's fast stepper
        if timing is not None:
            from emu import cftiming
            opts = dict(timing)
            idle = set(longrun.db.find_idle_spins(self.main_img,
                                                  longrun.db.MAIN_LOAD))
            self.clock = cftiming.CycleClock(m, idle=idle, **opts)
            self.stepper = cftiming.CycleStepper(self.clock)
            self.ips = self.clock.fsys
        elif cfg and cfg.get('ips'):
            self.ips = int(cfg['ips'])
        else:
            self.ips = self.device.post_intro_ips or INSTR_PER_SEC
        for source in pits.sources:
            source.ips = self.ips
        if self.sources:
            self.sources[0].ips = self.ips
            self.sources[0].align(pits.now)
        self._t0 = pits.now

        # Frames, latched where emu/panel.py says [FRONT] is complete.
        self._latched = None
        if prof.panel_diff is not None and prof.fb_front is not None:
            def latch(uc, a, s_, d):
                buf = panel.read(m, prof.fb_front)
                if buf is not None:
                    self._latched = buf
            at(prof.panel_diff, latch)
        self.held = panelin.Held(self.device)

        # Strict mode (emu/strict.py), sharing the cycle clock's block hook
        # when there is one. Its watchdog runs on core cycles: the clock's,
        # or emulated seconds at the nominal core clock.
        self.strict = None
        if strict is not None:
            from emu import cftiming
            from emu import strict as strictmod
            if self.clock is not None:
                cycles = (lambda: self.clock.cycles)
            else:
                cycles = (lambda: int((self.pits.now - self._t0)
                                      * cftiming.FSYS / self.ips))
            load = longrun.db.MAIN_LOAD
            self.strict = strictmod.Strict(
                m, clock=self.clock, cycles=cycles,
                prescanned=(load, load + len(self.main_img)),
                ddr_size=ddr or 64 * 1024 * 1024, **dict(strict))
        if on_machine is not None:
            on_machine(self)

    # -- time -------------------------------------------------------------------
    def now_ms(self):
        return (self.pits.now - self._t0) * 1000.0 / self.ips

    def run_ms(self, ms):
        """Run `ms` emulated milliseconds (to the next chunk boundary past)."""
        end = self.ms + ms
        while self.ms < end - 1e-9 and self.halted is None:
            self._chunk(min(CHUNK_MS, end - self.ms))
        return self

    def _chunk(self, ms):
        budget = max(1, int(ms * self.ips / 1000))
        pc, executed, stop = longrun.spin(
            self.m, self.pc, budget, pits=self.pits, fast=self.stepper,
            async_events=self.sources)
        self.pc = pc
        if stop != 'limit':
            self.halted = '%s at pc=0x%08x' % (stop, pc)
        if self.strict is not None:
            self.strict.check_halt()
            self.strict.check_watchdog()
            if self.strict.halted and self.strict.stop and not self.halted:
                self.halted = 'strict: ' + self.strict.halted
        self.ms = self.now_ms()
        self._collect()

    def _collect(self):
        buf = self._latched
        if buf is not None:
            if not self.frames or self.frames[-1][1] != buf:
                self.frames.append((round(self.ms, 3), buf))
            self._latched = None
        if self._raw:
            n = len(self._raw) // 8 * 8
            if n:
                if self.record_audio:
                    self.pcm += audioout.frames_from_ssi(
                        bytes(self._raw[:n]), 32, self.audio_cfg['sample_bits'])
                del self._raw[:n]

    # -- input ------------------------------------------------------------------
    def _feed(self, data, what):
        self.pc = panelin.feed(self.m, self.profile, bytes(data))
        self.inputs.append((round(self.ms, 3), what))

    def press(self, code):
        pos = self.held.press(code)
        if pos is None:
            raise SessionError('button %r is not on the %s panel'
                               % (code, self.device.name))
        self._feed(panelin.encode_buttons(*pos), 'press %s' % code)

    def release(self, code):
        pos = self.held.release(code)
        if pos is not None:
            self._feed(panelin.encode_buttons(*pos), 'release %s' % code)

    def tap(self, code, hold_ms=100, after_ms=300):
        self.press(code)
        self.run_ms(hold_ms)
        self.release(code)
        self.run_ms(after_ms)

    def turn(self, encoder, detents, after_ms=150):
        channel = self.device.encoder_channel(encoder)
        if channel is None:
            raise SessionError('encoder %r is not on the %s panel'
                               % (encoder, self.device.name))
        counts = detents * getattr(self.device, 'encoder_counts', 1)
        while counts:
            step = max(-127, min(127, counts))
            self._feed(panelin.encode_encoder(channel, step),
                       'turn %s %+d' % (encoder, step))
            counts -= step
        self.run_ms(after_ms)

    # -- names ------------------------------------------------------------------
    def code(self, name):
        """-> the button code for a label ('PLAY', 'TRIG', '5': the device
        file's [panel.labels], so '5' is trig key 5), '#24' for a raw code,
        or an int."""
        if isinstance(name, int):
            return name
        text = str(name).strip()
        want = text.upper()
        for code, label in self.device.labels.items():
            if label.upper() == want:
                return code
        if text.startswith('#') and text[1:].isdigit():
            return int(text[1:])
        raise SessionError('no button called %r on the %s panel'
                           % (name, self.device.name))

    def encoder(self, name):
        """-> an encoder rotation code: 'A'..'H' or its number (1-based)."""
        number = encoder_number(name)
        if number is None:
            raise SessionError('no encoder called %r' % name)
        return number

    # -- results ----------------------------------------------------------------
    def screen_at(self, ms):
        """-> the last frame latched at or before `ms`, or None."""
        best = None
        for t, buf in self.frames:
            if t <= ms + 1e-9:
                best = buf
            else:
                break
        return best

    def strict_report(self, baseline=None):
        from emu import strict as strictmod
        if self.strict is None:
            return None
        return self.strict.report(baseline,
                                  stand_ins=strictmod.stand_ins(self.ev))

    def audio_seconds(self):
        return len(self.pcm) / 4 / self.audio_cfg['rate'] if self.audio_cfg else 0.0

    def close(self):
        card = getattr(self.ev.get('esdhc'), 'card', None) if self.ev else None
        if card is not None:
            try:
                card.close()
            except Exception:                           # noqa: BLE001
                pass
        if self.clock is not None:
            self.clock.close()


def run_script(session, steps):
    """Run parsed script `steps` (emu.fwcompare.parse_script) on a session.
    -> list of (label, ms) marks where a 'snap' step asked for a capture."""
    marks = []
    for step in steps:
        kind = step[0]
        if kind == 'wait':
            session.run_ms(step[1])
        elif kind == 'tap':
            session.tap(session.code(step[1]), hold_ms=step[2], after_ms=step[3])
        elif kind == 'press':
            session.press(session.code(step[1]))
            session.run_ms(step[2])
        elif kind == 'release':
            session.release(session.code(step[1]))
            session.run_ms(step[2])
        elif kind == 'turn':
            session.turn(session.encoder(step[1]), step[2], after_ms=step[3])
        elif kind == 'snap':
            marks.append((step[1], session.ms))
        else:
            raise SessionError('unknown script step %r' % (step,))
        if session.halted:
            break
    return marks
