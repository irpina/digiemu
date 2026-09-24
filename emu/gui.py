"""Boot the thing and watch the panel.

A live view of the Digitakt II's 128x64 OLED, driven by the real firmware
running under Unicorn. The emulator runs on a worker thread and writes into a
shared framebuffer whenever the firmware calls Bitmap::setPixel; the UI thread
just samples that framebuffer on a timer. Nothing here reimplements the raster
-- every lit pixel is one setPixel call the firmware actually made.

There are TWO screens and this window has to switch between them, because the
intro and the main OS draw by different routes. Measured, over the same build:

    boot400M.snap, intro running    setPixel 616,823   panel buffer     17 lit
    postintro.snap, OS running      setPixel       0   panel buffer  2,373 lit

The intro draws through `Bitmap::setPixel`, so `on_pixel` is the right source
for it. The main OS composes straight into the firmware's own framebuffer and
never calls the intercepted primitive, so after the handover the source has to
become `emu.panel.read`. Showing setPixel throughout is what made this window
sit on the intro's last frame forever while a complete user interface was
rendering in RAM -- see HANDOVER warning 6. The switch happens at INTRO_DONE,
the same point the timers are released.

    uv run python -m emu.gui [snapshot]

--patch-machine installs the experimental eighth machine (PLACEHOLDER) into
the running emulator's machine list. Bare, it applies all nine parts of the
patch (list, dispatch, group, name, rank, permit, hint, pertype, clone);
--patch-machine=list, =dispatch, =group, =name, =rank, =permit, =hint,
=pertype, or =clone applies just one, and a
+-separated combination (--patch-machine=list+dispatch) applies exactly
those, for bisecting a boot failure. An optional :N suffix on the parts
value (--patch-machine=list:6)
sets the 8th list entry's value, default 7, to distinguish "eight entries is
too many" from "the value 7 is the problem". This patches guest memory in
the running emulator only -- it modifies no file on disk and is not a
flashable patch.

--machine=NAME:SHORT[:CLONE_OF[:POSITION]] overrides the new machine's
names, cloned descriptor and display position (see
tools/machinepatch.py's MachineSpec); without it the default spec
(Placeholder/PLC, cloned from type 6) is used.

--ips-at WHEN:N (repeatable) changes the timer rate to N instructions per
emulated second at instruction count WHEN (e.g. --ips-at 80M:18.72M after
boot); the GUI then runs slower than real time if the emulator cannot
keep up.

--post-intro-ips N sets the timer rate applied when the intro hands over;
default 18720000 (4x INSTR_PER_SEC); 0 keeps the default rate; ignored when
--ips-at is given.

Emulator(..., save_on_exit=PATH) saves the running machine to PATH when it is
stopped cleanly, after the +Drive image has been flushed, so the next session
resumes where this one ended instead of on RAM that no longer matches the
card. See Emulator._save_session.

tkinter only, no third-party GUI dependency. Note Homebrew's python@3.14 does
not ship tkinter; uv's managed CPython does, which is why pyproject pins 3.12.
"""
import ast
import collections
import os
import struct
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unicorn import UcError
from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR
from emu.longrun import build, spin
from emu.dtim import Dtims, Timers
from emu import (audioout, config, device as devices, edma_sw, intfrc,
                 native, panel, panelin, panelleds, symbols)
from emu.pit import INSTR_PER_SEC, Pits, intro_running
from emu.screen import png
from emu.snapshot import _SnapshotUnpickler, save as save_snapshot

# The intro's frame rate is not a guess. PIT3 is configured at 0x400d3a7a with
# PCSR=0x0936 (prescaler 2^10) and PMR=0x2191, so one frame is (8593+1)*1024 =
# 8,800,256 bus cycles; its ISR (vector 208, 0x400d2d70 on Digitakt -- resolved
# per build as profile.intro_pit3_isr) posts the semaphore the draw loop waits
# on at 0x400d4036 (also Digitakt-specific). The bus clock is 132 MHz, taken
# from the UART baud divider at 0x400024a4 (132000000 / (32*baud)) -- which
# checks out because it also makes the RTOS tick exactly 50.000 Hz and PIT2
# 60.0 Hz.
FRAME_VECTOR = 208
FRAME_HZ = 132_000_000 / ((0x2191 + 1) * 1024)     # 14.9996

# Buttons that latch instead of behaving momentarily. A mouse cannot hold one
# button while clicking another, so a modifier click toggles it and stays
# asserted for the next press -- which is what makes FUNC+SRC reach SRC's
# secondary function rather than its primary. Keyed off the device TOML's
# group name; that grouping was previously editorial only, and this is the
# first thing to read it semantically.
LATCHING_GROUPS = frozenset({'modifiers'})

W, H = 128, 64

# Vertical space the window owes to everything that is not the panel: the
# toolbar, three status lines, and the control surface, which is several rows
# of buttons deep. Without this the auto-zoom happily fills the screen with a
# 128x64 framebuffer and clips the controls off the bottom.
RESERVE_H = 520
MAX_SCALE = 6

# Panel palette: an OLED is emissive, so the lit pixel is the bright thing and
# the ground is genuinely black rather than dark grey.
OFF = b'\x0c\x0e\x12'
ON = b'\xe8\xf6\xff'
# The worker runs under spin(pits=...), which steps to each PIT deadline and
# so delivers the OS heartbeat: the RTOS time slice (PIT0), the software timer
# wheel (PIT2) and the display frame timer (PIT3). Without it the GUI ran the
# firmware with no interrupts at all, which is why it showed none of the
# post-intro progress the harness could already reach -- the display task sat
# blocked on a semaphore only PIT3's handler ever posts.
#
# It is expensive: deadline stepping needs `count=` on emu_start, and that
# makes Unicorn count every instruction, which breaks TB chaining. Measured on
# this machine with every hook installed, 2.0M instructions a second counted
# against 15.5M uncounted -- 7.6x, not the ~1.8x this comment used to claim.
# The cost is in `count` itself and not in how often emu_start is called, so
# there is nothing to win by making BUDGET bigger than responsiveness wants.
# The GUI defaults to spin(fast=True), which drops `count=` entirely; --exact
# puts it back for comparison against bootcheck. See longrun._FastStepper.
BUDGET = 100_000          # instructions per pass: one RTOS tick (PIT0 is 20 ms)
                          # on the mk1. Panel input is delivered once per pass,
                          # and the firmware's encoder acceleration is keyed on
                          # the ticks between events; at 400K a real-time turn
                          # reached it as one event per 4 ticks. Also bounds
                          # pause/stop latency and how long a click waits.
                          # The cost is in emu_start's `count` rather than in
                          # how often it is called, so a smaller pass buys
                          # responsiveness almost for free.

# Emulated dwell between panel state changes. _drain_input used to deliver
# everything queued in one feed, so a press and its release reached the
# firmware a few emulated milliseconds apart however slowly the user
# clicked -- and a chord collapsed into an instant. A real press lasts
# 50-200 ms. This is EMULATED milliseconds, converted to chunks against
# the timer rate in force when the input is delivered, because the rate
# changes at the intro hand-over. On the mk1 that rate is 4.68M instr/s, a
# 400K-instruction chunk is 85 ms, and the dwell is one chunk. The old fixed
# count of 16 chunks was calibrated for a 3 ms chunk and came to 1.4 s
# there: every click waited that long for its release to land, and every
# encoder detent turned meanwhile piled into one packet behind it.
PANEL_DWELL_MS = 50


def _accelerated():
    """-> True when this Unicorn has the digikit accelerators.

    patches/unicorn-2.1.4-m68k-digikit-accel.patch (native block budget,
    software eDMA and FF1; it applies on top of the fast-memory patch) is
    what makes the audio render faster than real time. Probed on a scratch
    engine, so the running machine is never touched.
    """
    try:
        from emu import native
        from emu.harness import native_ff1
        from unicorn import UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, Uc
        probe = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        return bool(native.budget_available(probe)
                    and native.edma_available(probe) and native_ff1())
    except Exception:                                   # noqa: BLE001
        return False


# config.NotFound and device.DeviceError derive from SystemExit, so that a
# command-line tool prints their message and exits. On this worker thread
# that means the thread dies with nothing to show for it: `ready` is never
# set, `error` stays None and the window sits on "loading snapshot" forever
# (a windowed build has no stderr for the traceback either). Nothing on a
# worker thread can exit the process anyway, so setup catches SystemExit as
# well as Exception and reports it like any other failure.
SETUP_ERRORS = (Exception, SystemExit)


def describe_error(exc):
    """-> 'Type: message' for the status line and the log."""
    msg = str(exc).strip()
    return '%s: %s' % (type(exc).__name__, msg) if msg else type(exc).__name__


def saved_ssi0(path):
    """-> the SSI0 entry of a snapshot's build manifest, or None.

    A snapshot this window saved (save_on_exit) carries the audio model's
    clock as a component AND a manifest entry for it. The legacy upgrade that
    opens every older snapshot builds a manifest WITHOUT that entry and adds
    it only after validation, so it would refuse such a snapshot as a
    manifest mismatch -- and so would the retry without audio. Reading the
    manifest first says which of the two builds the snapshot needs. Only the
    manifest is looked at; anything unreadable is left for build() to report.
    """
    try:
        with open(path, 'rb') as fh:
            blob = _SnapshotUnpickler(fh).load()
        entry = (blob.get('manifest') or {}).get('ssi0_dma')
    except Exception:                                   # noqa: BLE001
        return None
    if isinstance(entry, dict) and isinstance(entry.get('request_hz'), int):
        return dict(entry)
    return None


# What emu.snapshot says when a snapshot was made by a DIFFERENT build: its
# build manifest (the hook topology, the flash and MAIN OS hashes) is not the
# one this build would make, or it is in an older checkpoint format. Both are
# refused before the machine is touched, and neither is a fault in the
# snapshot or a reason to retry: the fix is to boot the firmware again and
# make a new one. So they are reported as their own kind (Emulator.
# incompatible), which the portable app turns into its Rebuild offer, rather
# than as 'The emulator stopped', which reads as a crash and loses the card.
INCOMPATIBLE_ERRORS = ('checkpoint build manifest mismatch',
                       'unsupported checkpoint version')


def snapshot_incompatible(exc):
    """-> True when `exc` (or what it was raised from) refuses a snapshot
    saved by a different build. See INCOMPATIBLE_ERRORS."""
    for _ in range(8):              # a __cause__ chain, bounded
        if exc is None:
            break
        if (isinstance(exc, RuntimeError)
                and str(exc).startswith(INCOMPATIBLE_ERRORS)):
            return True
        exc = exc.__cause__
    return False


def manifest_diff(text):
    """-> the build-manifest keys a mismatch message says differ, sorted.

    emu.snapshot's message is 'checkpoint build manifest mismatch: saved=%r
    current=%r'; both are dicts of plain literals, so they parse back. [] for
    any other text, or when they do not.
    """
    _head, sep, rest = str(text).partition(': saved=')
    saved_s, sep2, current_s = rest.rpartition(' current=')
    if not (sep and sep2):
        return []
    try:
        saved = ast.literal_eval(saved_s)
        current = ast.literal_eval(current_s)
    except Exception:                                   # noqa: BLE001
        return []
    if not isinstance(saved, dict) or not isinstance(current, dict):
        return []
    missing = object()
    return sorted(str(k) for k in set(saved) | set(current)
                  if saved.get(k, missing) != current.get(k, missing))


def incompatible_message(snapshot, exc):
    """-> Emulator.error for a snapshot another build made.

    Leads with the verdict, because the panel's status line shows only the
    first line; the full saved and current manifests go to the log instead.
    """
    name = os.path.basename(str(snapshot)) if snapshot else 'the snapshot'
    text = str(exc)
    keys = manifest_diff(text)
    if keys:
        detail = 'its build manifest differs in: %s' % ', '.join(keys)
    else:
        detail = text.split(': saved=')[0]
    return ('incompatible snapshot: rebuild needed. %s was saved by a '
            'different build of the emulator or firmware (%s), so this build '
            'cannot resume it. Boot the firmware again to make a new one.'
            % (name, detail))


def _replace(src, dst):
    """os.replace, retried while Windows reports a sharing violation.

    A launcher or a virus scanner reading resume.snap at the moment of the
    rename makes a bare os.replace fail with 'access denied', and the session
    is then lost. emu.bootstrap.replace_retry retries that; it is imported
    here, lazily, so this module does not depend on it being present.
    """
    try:
        from emu.bootstrap import replace_retry
    except ImportError:
        return os.replace(src, dst)
    return replace_retry(src, dst)


class Emulator(threading.Thread):
    """Runs the firmware and publishes a framebuffer. Owns no widgets.

    Failures are reported, never fatal to the window: `error` holds
    'Type: message' when the snapshot could not be opened, the run halted
    or the worker died, and `ready` is set in every case, so a UI waiting on
    it cannot hang. `incompatible` is True when the failure was a snapshot
    made by a different build (see INCOMPATIBLE_ERRORS): nothing is wrong
    with it or the card, it just has to be made again.

    save_on_exit: a path to save the machine to on a clean stop (stop_flag).
    `finishing` is set once the run loop has ended at a step boundary and
    the card flush and save have begun -- work a caller must wait for rather
    than abandon on a timeout. `saved` is the path once it is written;
    `save_error` says why it was not. `flushed` is True once a clean stop
    has written the card's changes to its file; with `release_card` set
    beforehand, the stop then also closes the card (see _stop_cleanly).
    """

    daemon = True

    def __init__(self, snapshot, weakptr=False, slc=False, syx=None,
                 fast=True, realtime=True, patch_machine=False,
                 patch_eighth=7, patch_machine_spec=None,
                 panel_dwell=PANEL_DWELL_MS, ips_at=(),
                 post_intro_ips=4 * INSTR_PER_SEC, audio=True,
                 save_on_exit=None):
        super().__init__()
        self.snapshot = snapshot
        self.save_on_exit = save_on_exit
        self.saved = None
        self.save_error = None
        self.finishing = threading.Event()
        # Set before stop_flag to have a clean stop close the card once it
        # is flushed (and the session saved); `flushed` says it was.
        self.release_card = False
        self.flushed = False
        # Record the audio output, for a device whose [audio] path is
        # modelled. See _start_audio.
        self.audio_wanted = audio
        self.audio_on = False
        self.audio_cfg = None
        self.audio_error = None
        self.audio_frames = 0       # frames recorded since start
        self.audio_speed = 0.0      # audio seconds rendered per wall second
        self._audio_raw = bytearray()   # SSI words not yet converted
        self._audio_pcm = bytearray()   # 16-bit LE stereo, the recording
        self._audio_lock = threading.Lock()
        self._audio_sources = ()
        self._audio_t = None
        self._audio_mark = 0
        # Live output (accelerated Unicorn only): see _live_write.
        self.audio_live = False
        self.audio_muted = False
        self.live_underruns = 0
        self._live_out = None
        self._live_error = None
        self._live_started = False
        self._live_buf = bytearray()
        # Master Volume knob position (software gain on live output). 1.0
        # = unity; 0.0 = silent; the knob goes a bit above unity if turned
        # past 12 o'clock, with clipping at the host device.
        self._volume = 1.0
        self.weakptr = weakptr
        self.slc = slc
        self.syx = syx
        self.patch_machine = patch_machine
        self.patch_eighth = patch_eighth
        self.patch_machine_spec = patch_machine_spec
        self._pending_ips = sorted(ips_at)
        self._slept = 0.0        # seconds given up to pacing, for diagnosis
        # Timer rate applied once the intro hands over; see --post-intro-ips.
        # An explicit --ips-at wins, so recorded sessions replay unchanged.
        self._post_intro_ips = 0 if ips_at else post_intro_ips
        # See PANEL_DWELL_MS. 0 means no pacing: the old coalesce-and-
        # deliver-once-per-chunk behaviour, for an A/B against this one.
        self._dwell_ms = panel_dwell
        self._pits = None           # the timers, once built; see run()
        self._chunks_since_delivery = 0
        self._delivered_before = False
        # Interactive running, not measurement. `fast` drops the `count=`
        # argument to emu_start, which costs 7.6x on this machine, in exchange
        # for timers landing on a basic-block boundary rather than an exact
        # instruction -- so it changes the instruction stream and must never be
        # used for a determinism or pass/fail claim. See longrun._FastStepper.
        # `realtime` then paces the worker back down: uncounted it runs several
        # times faster than the hardware, and a sequencer at 3x tempo is worse
        # than one at a third.
        self.fast = fast
        self.realtime = realtime
        self._paced = 0
        self._pace_t0 = None
        self._rate_t = None         # wall-clock instruction rate window
        self._rate_instrs = 0
        self.fb = bytearray(W * H)
        self.pause = threading.Event()
        self.stop_flag = threading.Event()
        self.ready = threading.Event()
        self.stats = {'frames': 0, 'px': 0,
                      'pc': 0, 'tcb': 0, 'tasks': 0, 'prints': 0, 'fps': 0.0,
                      'bmp': 0, 'instrs': 0, 'pit': (0, 0, 0),
                      'status': 'loading snapshot', 'mainloop': 0, 'jobs': 0,
                      'dtim3': 0, 'terminal': False, 'panel_lit': 0,
                      'source': 'setPixel', 'wall_ips': 0.0, 'real': 0.0}
        self._uc = None             # set once the machine is built
        self.error = None
        self.incompatible = False   # error is a snapshot from another build
        self._seen = set()
        self.version = 0            # bumped on every pixel, so the UI can
        self._frame_t = time.time()  # skip redrawing an unchanged panel
        self.captured = []          # completed frames, for correct-speed replay
        self.use_panel = False      # False: setPixel (intro). True: the
                                    # firmware's own framebuffer (main OS).
        self._last_panel = None     # last panel buffer drawn, to skip repeats
        self._panel_live = False    # seen the OS draw into it at least once
        self._panel_latch = None    # newest untorn frame, grabbed at diff entry
        self.fb_front = None        # resolved once the image is known -- see run()
        self.profile = None         # the whole symbol profile, same point
        # Panel input. The UI thread must never touch guest memory: the
        # worker sits inside emu_start for a whole BUDGET at a time. So
        # clicks arrive on this queue and are applied between chunks, the
        # same safe point pause already uses.
        self.inbox = collections.deque()
        self.device = None          # which product, identified by firmware hash
        self.firmware = None        # its [[firmware]] entry (version etc.)
        # Key LEDs, decoded from the UART8 stream to the panel MCU (see
        # emu/panelleds.py). leds is replaced, never mutated, so the UI thread
        # can read it without a lock; led_version says when it changed.
        self.leds = {}              # LED id -> (r, g, b) 0..255
        self.led_version = 0
        self._led_state = None
        self._uart = None
        self.held = None            # panelin.Held, once the device is known
        self.button_names = {}      # control code -> the firmware's own name
        self.encoder_names = {}
        self.device_error = None    # why there is no control surface, if so
        self._faulted_pages = set()  # pages already reported by _fault_sink
        self._fault_summary_printed = False  # print the report once, not per chunk
        self._backtrace_printed = False  # print the stack scan once, not per chunk

    def _identify_device(self, m, profile):
        """Work out which product this is and read its control names.

        Degrades rather than fails: an unrecognised firmware means no control
        surface, not a dead emulator. The names are read out of the image, so
        they are this firmware's own rather than a table that can go stale.
        """
        try:
            self.device, self.firmware = devices.identify(
                config.firmware(self.syx))
            self.held = panelin.Held(self.device)
            self.button_names = panelin.control_names(m, profile, 'button')
            self.encoder_names = panelin.control_names(m, profile, 'encoder')
        except SETUP_ERRORS as exc:                    # noqa: BLE001
            # DeviceError and NotFound too (see SETUP_ERRORS): the machine is
            # built by now, so this degrades to no control surface.
            self.device_error = describe_error(exc)

    def _drain_input(self, m, profile, pc):
        """Apply queued panel input at a chunk boundary. -> the new PC.

        Everything delivered in one pass is encoded into ONE byte stream and
        sent with a single feed, because the firmware's ISR drains the whole
        receive ring: one raised vector covers every message in it. Raising
        once per event would nest exception frames for input the ring
        already holds.

        A single feed used to mean a single drain of the WHOLE queue, once
        per BUDGET chunk -- so a press and its release, however far apart the
        user actually clicked, reached the firmware a few emulated
        milliseconds apart, and a chord collapsed into an instant. See
        PANEL_DWELL_MS. Now a press/release (a button STATE change) is
        held back until that much emulated time has passed since the last
        one was delivered, so it dwells for something like a real press. Encoder
        events are relative and bursty by nature rather than a state that can
        be held, so they are not paced: every queued encoder event is drained
        in the same pass as the one button transition (or on its own, if no
        button transition is pending). Nothing queued is ever dropped, only
        delayed until its dwell elapses. --panel-dwell 0 disables all of
        this and restores the old drain-everything-every-chunk behaviour.

        Returns the PC because delivering input raises a vector, which moves
        it. Dropping the result would strand the run at the old address.
        """
        if self.held is None:
            return pc
        paced = self._dwell_ms > 0
        # The dwell in chunks, from the timer rate in force NOW.
        if paced:
            ips = (self._pits.sources[0].ips if self._pits is not None
                   else INSTR_PER_SEC)
            dwell = max(1, -(-int(self._dwell_ms * ips / 1000) // BUDGET))
        else:
            dwell = 0
        # Buttons wait out the dwell; encoders never do. A detent is not a
        # state that has to be held for a realistic time, and holding it
        # back behind a pending release is what turned a second of knob
        # into one lump delivered late.
        button_ok = not (paced and self._delivered_before
                         and self._chunks_since_delivery < dwell)
        if not button_ok:
            self._chunks_since_delivery += 1
        out = bytearray()
        took_button = False
        deferred = []
        while self.inbox:
            kind, code, arg = self.inbox.popleft()
            if kind == 'encoder':
                channel = self.device.encoder_channel(code)
                if channel is not None:
                    # A detent from the window is several counts on the
                    # wire ([panel] encoder_counts): the firmware's encoder
                    # driver has a dead zone, and at one count a notch a
                    # knob needed ~16 notches before anything moved.
                    step = arg * getattr(self.device, 'encoder_counts', 1)
                    out += panelin.encode_encoder(
                        channel, max(-127, min(127, step)))
            elif not button_ok or (paced and took_button):
                deferred.append((kind, code, arg))
            else:
                took_button = True
                if kind == 'press':
                    pos = self.held.press(code)
                    if pos is not None:
                        out += panelin.encode_buttons(*pos)
                elif kind == 'release':
                    pos = self.held.release(code)
                    if pos is not None:
                        out += panelin.encode_buttons(*pos)
                elif kind == 'release_all':
                    for pos in self.held.release_all():
                        out += panelin.encode_buttons(*pos)
        for item in reversed(deferred):
            self.inbox.appendleft(item)
        if not out:
            return pc
        if took_button:                 # an encoder-only packet starts no dwell
            self._chunks_since_delivery = 0
            self._delivered_before = True
        try:
            new_pc = panelin.feed(m, profile, bytes(out))
        except Exception as exc:                       # noqa: BLE001
            self.stats['status'] = 'panel input failed: %s' % exc
            return pc
        # Replayable: paste these into tools/guirun.py to reproduce the session.
        # stats['instrs'] is the count at this chunk boundary, before the next
        # spin, which is exactly where guirun delivers a --feed.
        print('[gui] input --feed %d:%s' % (self.stats['instrs'], bytes(out).hex()),
              flush=True)
        return new_pc

    def run(self):
        """The thread body: _run, with nothing allowed to escape unreported.

        _run catches its own setup failures. This is for the rest -- an
        exception between setup and `ready`, or out of a hook inside the run
        loop -- which used to end the thread with `ready` unset and no
        message, a window that looked alive and never moved again.
        """
        try:
            self._run()
        except BaseException as exc:                    # noqa: BLE001
            if self.error is None:
                self.error = self._describe_failure(exc)
            self.stats['status'] = ('crashed' if self.ready.is_set()
                                    else 'failed to load')
            print('[gui] EMULATOR STOPPED: %s' % self.error, flush=True)
            traceback.print_exc(file=sys.stdout)
            self._close_live()
        finally:
            self.ready.set()

    def _describe_failure(self, exc):
        """-> `error` for `exc`, flagging a snapshot from another build.

        That one is worded as a verdict (incompatible_message) and sets
        `incompatible`; the raw message, which holds both whole manifests,
        goes to the log, where it says exactly what changed.
        """
        if snapshot_incompatible(exc):
            self.incompatible = True
            print('[gui] %s' % describe_error(exc), flush=True)
            return incompatible_message(self.snapshot, exc)
        return describe_error(exc)

    def _on_pixel(self, x, y, val, bmp):
        """The setPixel hook: the intro's screen, pixel by pixel."""
        if self.use_panel:
            # Once the OS owns the panel its framebuffer is the screen
            # (_publish_panel). The OS still calls setPixel for bitmaps that
            # are not the screen, and writing those into `fb` put stray
            # pixels over the frame until the next one replaced it: leftovers
            # of earlier screens after a page change or a knob turn.
            return
        self.stats['bmp'] = bmp
        if (x, y) in self._seen and len(self._seen) > W * H // 2:
            now = time.time()
            self.captured.append(bytes(self.fb))    # snapshot the finished frame
            self.stats['frames'] += 1              # coordinate repeat = new frame
            self.stats['fps'] = 1.0 / max(1e-6, now - self._frame_t)
            self._frame_t = now
            self._seen.clear()
            # Deliberately no emu_stop here any more. Under spin() a hook
            # that stops the run early makes the instruction accounting a
            # lie -- emu_start returns having executed fewer than it was
            # asked for, the loop credits itself the full step, and every
            # timer deadline drifts away from the instructions actually
            # executed. The worker regains control every BUDGET
            # instructions instead, which is soon enough for pause and
            # stop to feel immediate.
        self._seen.add((x, y))
        self.fb[y * W + x] = val
        self.stats['px'] += 1
        self.version += 1

    def _run(self):
        on_pixel = self._on_pixel

        try:
            # NOTE: unblock=True also satisfies the frame semaphore, so the
            # animation runs unpaced -- as fast as the host manages, not at
            # FRAME_HZ. Excluding FRAME_SEM and driving vector 208 instead was
            # tried and does not work on its own: with every other wait
            # satisfied, the prio-6 task never yields, so the scheduler never
            # reschedules and the woken draw task never runs (the semaphore
            # count just climbs). Faithful pacing needs cycle accounting so
            # the RTOS tick can preempt too. Until then the status line
            # reports the shortfall against the real 15 fps rather than
            # pretending.
            # dsp=True backs the 0x8C000000 coprocessor port's ready line.
            # Without it the priority-3 job worker wedges in the ready-bit
            # spin at 0x400cf4ec on its very first transfer and none of the
            # five jobs queued at boot ever runs. See emu/dsp.py.
            extra = {'syx': self.syx} if self.syx else {}
            # sdgate/esdhc are not passed here -- build()'s own defaults
            # (True) supply the SD storage models, so they come along with
            # every call site that does not explicitly override them.
            # The note above is the Digitakt II case. Digitakt mk1 is the
            # opposite one: its intro PARKS on the frame semaphore, and
            # unblock only ever sees a pend on the way IN, so it can never
            # satisfy a wait that is already blocked -- the PIT3 tick is the
            # only thing left that can post it. Measured, faking it as well
            # double-posts it, so the draw loop exits after 77 ticks instead
            # of 180 frames and no OS task runs afterwards. Which policy a
            # product needs is in its device file; one that says nothing
            # keeps the Digitakt II behaviour. This has to be settled BEFORE
            # build(), because unblock_except is part of the checkpoint build
            # manifest: a snapshot saved under one policy will not reopen
            # under the other.
            # Identified HERE rather than read off self.device, because
            # _identify_device needs the machine for the control-name tables
            # and so cannot run until after build() -- which is too late, as
            # unblock_except is an argument to build(). This only needs the
            # firmware file, so it can run first. A failure to READ the device
            # files keeps the default policy, the same way the control
            # surface degrades rather than failing. A REFUSAL does not:
            # device.identify raises DeviceError (a SystemExit) for a hash no
            # device file lists or a missing devices directory, and NotFound
            # for a missing .syx. Guessing the policy then either opens the
            # snapshot under the wrong one -- a mk1 intro run the Digitakt II
            # way wedges without a word -- or is refused as a manifest
            # mismatch that names neither the firmware nor the fix. So those
            # pass through to the handler below, which reports their own
            # message; before SETUP_ERRORS they killed this thread silently.
            intro_except = ()
            try:
                dev, _fw = devices.identify(config.firmware(self.syx))
            except Exception:                           # noqa: BLE001
                dev = None
            if dev is not None and not dev.intro_unblocks_frame_sem:
                pre = symbols.resolve(open(config.main_image(), 'rb').read())
                if pre.frame_sem is not None:
                    intro_except = (pre.frame_sem,)
            # deferred_components: a snapshot saved mid-run carries its own
            # Timers, and restoring one without declaring the deferral fails
            # at execution rather than silently running on a fresh clock.
            # Declaring it costs a cold-ladder snapshot nothing, since that
            # has no such component.
            # Audio: the SSI transmit model plus the software eDMA the render
            # waits on, when the device says how. Arming it claims the
            # descriptors the firmware already programmed, so a snapshot from
            # before the audio init (a cold boot) refuses -- and then runs
            # without audio rather than not at all.
            # LIVE audio needs the accelerated Unicorn (native budget, eDMA
            # and FF1; see patches/README.md): with it the render runs faster
            # than real time. Without it, fall back to recording at a slow
            # audio clock and playing afterwards.
            # A snapshot saved on exit (save_on_exit) already carries the
            # audio model -- see saved_ssi0 -- so it is resumed as saved, at
            # the rate it was saved with, rather than legacy-upgraded. The
            # model is then part of the snapshot's topology and cannot be
            # left out: without audio it is still built, just kept silent.
            saved_ssi = saved_ssi0(self.snapshot)
            cfg = getattr(dev, 'audio', None)
            if not self.audio_wanted:
                if saved_ssi and cfg:
                    self.audio_muted = True
                else:
                    cfg = None
            audio_kw = {}
            if cfg:
                self.audio_live = _accelerated()
                hz = cfg['request_hz']
                if not self.audio_live and cfg['fallback_request_hz']:
                    hz = cfg['fallback_request_hz']
                if saved_ssi:
                    audio_kw = dict(ssi0_request_hz=saved_ssi['request_hz'],
                                    ssi0_profile=cfg['ssi_profile'])
                else:
                    audio_kw = dict(ssi0_request_hz=hz,
                                    ssi0_legacy_upgrade=True,
                                    ssi0_profile=cfg['ssi_profile'])

            def _build(**kw):
                # fast_idle: an idle spin ends the step and is credited as
                # time spent, instead of a Python call on every pass. This is
                # a viewer, not a measurement, and it is what lets a busy
                # emulated CPU (see [audio] ips) idle for free.
                return build(self.snapshot, unblock=True,
                             softfloat=True, bitmap=True,
                             dsp=True, on_pixel=on_pixel,
                             weakptr=self.weakptr, slc=self.slc,
                             unblock_except=intro_except,
                             deferred_components=('timers',),
                             fast_idle=True, **kw, **extra)
            try:
                m, ev, st, pc, inq, at = _build(**audio_kw)
            except RuntimeError as exc:
                # A snapshot that carries the audio model cannot open
                # without it: retrying would only replace the real reason
                # with a manifest mismatch. And a snapshot from another build
                # is refused before audio is looked at -- the legacy upgrade
                # adds its manifest entry only after validation -- so the
                # retry would be refused the same way, a second build later.
                if not audio_kw or saved_ssi or snapshot_incompatible(exc):
                    raise
                self.audio_error = str(exc)
                print('[gui] audio unavailable for this snapshot (%s); '
                      'running without it' % exc, flush=True)
                audio_kw = {}
                m, ev, st, pc, inq, at = _build()
            if audio_kw:
                self.audio_cfg = dict(cfg, request_hz=audio_kw[
                    'ssi0_request_hz'])
                ssi = ev['ssi0_dma']
                # Serve the SSI a half-buffer at a time: the guest sees the
                # same interrupts and positions, at 1/32 of the steps.
                ssi.batch = True
                # The render also clocks the sequencer, by forcing INTC0
                # software interrupts (emu/intfrc.py); without them PLAY
                # runs no pattern.
                self._audio_sources = (ssi, edma_sw.install_bank(m, ev),
                                       intfrc.install(m, ev))
                ssi.sink = self._audio_raw.extend
                # None of the memory hooks on this path reads the PC, so the
                # engine need not rebuild an exact one for every hooked access
                # (emu/native.py). The PIT3 write probe below does read it.
                if os.environ.get('DIGIKIT_PIT3_PROBE') != '1':
                    native.enable_options(m.uc, native.NO_HOOK_PC_SYNC)
                # The Digitone's DSP (emu/dsplink.py): under live audio its
                # renders run on their own thread, in parallel with the main
                # CPU, as the two chips do.
                dsp = ev.get('dspcpu')
                if dsp is not None and self.audio_live:
                    dsp.start_thread()
            else:
                self.audio_live = False
            if self.patch_machine:
                sys.path.insert(0, os.path.join(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__))), 'tools'))
                try:
                    from machinepatch import (patch_b, DEFAULT_CAVE_B,
                                              spec_from_arg, DEFAULT_SPEC)
                except ImportError as exc:
                    # The public tree carries no firmware-modification tools.
                    raise RuntimeError('machine patching is not available in '
                                       'this tree (%s)' % exc) from exc
                # patch_b (and spec_from_arg) report a failed precondition
                # with SystemExit, which derives from BaseException and so
                # would slip past the handler below -- and a SystemExit on a
                # worker thread kills it silently, leaving this window stuck
                # on "loading snapshot". Convert it into something catchable.
                try:
                    spec = (spec_from_arg(self.patch_machine_spec)
                            if self.patch_machine_spec else DEFAULT_SPEC)
                    patch_b(m, DEFAULT_CAVE_B, parts=self.patch_machine,
                            eighth=self.patch_eighth, spec=spec)
                except SystemExit as exc:
                    raise RuntimeError('machine patch refused: %s' % exc) from exc
                self.stats['status'] = ('patched: ' + '+'.join(self.patch_machine)
                                        + ' (8th=%d)' % self.patch_eighth)
            # build() already resolved (and required) this same profile
            # internally -- see emu/symbols.py -- so re-resolving here is a
            # cache hit, not a rescan. fb_front is OPTIONAL: if it did not
            # resolve for this image, _publish_panel below just never has
            # anything to read, which is the documented degrade-gracefully
            # behaviour rather than a crash.
            main_img = open(config.main_image(), 'rb').read()
            profile = symbols.resolve(main_img)
            self.fb_front = profile.fb_front
            self.profile = profile
            self._identify_device(m, profile)
        except SETUP_ERRORS as exc:                    # noqa: BLE001
            self.error = self._describe_failure(exc)
            self.stats['status'] = 'failed to load'
            print('[gui] FAILED TO LOAD: %s' % self.error, flush=True)
            self.ready.set()
            return

        self._uc = m.uc
        self._m = m
        self._start_leds(m, ev)

        def fault_sink(rec):
            # Called from inside a Unicorn hook on the worker thread: no
            # locks, no Tk calls, no guest memory access, and nothing may
            # raise into the run -- same defensiveness as Machine._fault.
            try:
                page = rec['page']
                if page in self._faulted_pages:
                    return
                self._faulted_pages.add(page)
                kinds = '+'.join(sorted(rec['kinds'])) if rec['kinds'] else '?'
                print('[gui] FAULT page=0x%08x first=0x%08x pc=0x%08x %s'
                      % (page, rec['first_addr'], rec['first_pc'], kinds),
                      flush=True)
            except Exception:
                pass
        m.fault_sink = fault_sink
        self._reported = 0
        # PIT0 time slice, PIT2 wheel, PIT3 display -- but not until the intro
        # has handed over. Delivering into a running intro stops it ever
        # ending (PIT3 double-posts the frame semaphore that unblock is
        # already satisfying) and stops the OS tasks spawning (PIT2). See
        # emu.pit.Pits.
        # ...and DMA timer 3, which is the 30.05 Hz tick whose ISR
        # (0x400c30e4) is the only thing at boot that sends a message to
        # 0x4094ef3c, the queue the main application task blocks on. Without
        # it that task makes exactly one pass through its message loop and
        # waits forever, which is what this window used to show. See
        # emu/dtim.py.
        # DIAGNOSTIC: PIT3 stops being delivered at a reproducible point in
        # the GUI and never does headless, at the same snapshot, flags, ips
        # and chunk size. Enumerating the differences by inspection has
        # produced three wrong answers, so catch the write itself: log the PC
        # of whatever stores to PIT3's control register.
        # Off by default. This calls reg_read from inside a memory-write
        # hook, on every store to PIT3's control register, for the life of the
        # session -- and the GUI segfaults inside the Unicorn binding. That
        # makes it a suspect in its own right, so it is no longer in the
        # default path: set DIGIKIT_PIT3_PROBE=1 to get it back.
        if os.environ.get('DIGIKIT_PIT3_PROBE') == '1':
          try:
            from unicorn import UC_HOOK_MEM_WRITE as _UCW

            def _pit3_write(uc, typ, addr, size, val, data):
                try:
                    pc_now = uc.reg_read(UC_M68K_REG_PC)
                except Exception:
                    pc_now = 0
                print('[gui] PIT3 PCSR <- 0x%04x from pc=0x%08x (size %d) at '
                      '%dM instr' % (val & 0xFFFF, pc_now, size,
                                     self.stats['instrs'] // 1_000_000),
                      flush=True)
            m.uc.hook_add(_UCW, _pit3_write, begin=0xFC08C000, end=0xFC08C001)
          except Exception as _exc:                      # noqa: BLE001
            print('[gui] could not install the PIT3 write probe: %s' % _exc,
                  flush=True)

        # A product can set its own post-intro timer rate. It decides how
        # much EMULATED time passes per wall second -- emulated_rate =
        # host_instructions_per_second / post_intro_ips -- so it is the knob
        # that decides whether the window feels responsive. Measured on mk1:
        # at 18.72M the emulator delivers 2.25M instr/s, i.e. 12% of real
        # time; at 4.68M it delivers 1.96M, i.e. 42%, and the firmware's main
        # loop runs 3.4x as often. A 13% throughput cost for 3.5x the
        # responsiveness.
        if self.device is not None and getattr(self.device, 'post_intro_ips', 0) \
                and not self._pending_ips:
            self._post_intro_ips = self.device.post_intro_ips
        # Live audio needs an emulated CPU that can keep up with the render
        # ([audio] ips); with fast_idle the unused part of it costs nothing.
        if self._audio_sources and self.audio_live and self.audio_cfg['ips'] \
                and not self._pending_ips:
            self._post_intro_ips = self.audio_cfg['ips']
        intro = intro_running(m, profile.intro_pit3_isr)
        # A snapshot saved mid-run carries its own timer cadence and it has to
        # be RESTORED, not rebuilt: constructing fresh Dtims repairs "stale"
        # guest DTMR registers, which disarms the DTIM3 the firmware armed for
        # itself and leaves a perfect-looking framebuffer that never executes
        # another instruction. See emu/uiresume.py.
        pits = None
        restore = ev.get('restore_checkpoint_timers')
        if restore is not None:
            try:
                pits = restore()
            except Exception as exc:                    # noqa: BLE001
                print('[gui] saved timer cadence unusable (%s); building fresh'
                      % exc, flush=True)
                pits = None
        if pits is None:
            # Which PIT channels to DELIVER while the intro still owns vector
            # 208. Empty means hold everything: that is Digitakt II, where
            # unblock satisfies the frame semaphore and a real PIT3 tick would
            # post it a second time. Digitakt mk1 names [3] instead, because
            # its intro is paced by PIT3 and nothing else can post it.
            chans = tuple(getattr(self.device, 'intro_channels', ()) or ())
            pit = (Pits(m, channels=chans, hold=False) if (intro and chans)
                   else Pits(m, hold=intro))
            pits = Timers(pit, Dtims(m, channels=(3,), hold=intro))
        # "the intro is still running" is `intro`, not `pits.held`: a product
        # that paces its intro off PIT3 has that channel live throughout it,
        # so not all the timers are held and pits.held would claim the intro
        # was already over. A snapshot taken after the intro belongs to the OS
        # and the panel buffer is the screen from the first frame.
        self.use_panel = not intro
        if intro:
            def handover(uc, a, s_, d):
                # The intro switches its own PIT3 off on the way out. Widen to
                # the full OS set, or PIT0's time slice and PIT2's timer wheel
                # never start and no OS task ever runs.
                pits.sources[0].channels = (3, 2, 0)
                pits.release()
                self.use_panel = True
                if self._post_intro_ips:
                    # Applied at the next chunk boundary, like --ips-at.
                    self._pending_ips.append((self.stats['instrs'],
                                              self._post_intro_ips))
            if profile.intro_done is not None:
                at(profile.intro_done, handover)
            else:
                print('[gui] WARNING: intro_done did not resolve for this '
                      'image; timers will stay held and the intro will '
                      'never hand over', flush=True)
        elif self._post_intro_ips:
            self._pending_ips.append((0, self._post_intro_ips))
        print('[gui] timer rate %d, after intro %s'
              % (pits.sources[0].ips, self._post_intro_ips or 'unchanged'),
              flush=True)

        # Progress markers, so the status line can say what the firmware is
        # actually doing rather than only how many pixels it drew. Resolved
        # per build now (profile.mainloop / profile.job_pump); they say
        # whether the OS actually took over after the intro: mainloop is the
        # main application task's message-loop head, jobs is the job-worker
        # pump.
        mark = self.stats
        if profile.mainloop is not None:
            at(profile.mainloop, lambda uc, a, s, d: mark.__setitem__(
                'mainloop', mark['mainloop'] + 1))
        if profile.job_pump is not None:
            at(profile.job_pump, lambda uc, a, s, d: mark.__setitem__(
                'jobs', mark['jobs'] + 1))
        # 0x4012d2fa is `bra.b` to itself -- the loop the abort path lands in.
        at(0x4012d2fa, lambda uc, a, s, d: mark.__setitem__('terminal', True))

        # Latch the frame at the diff's entry, which emu/panel.py documents as
        # the one moment [FRONT] is a complete, just-rendered frame. Reading it
        # at an arbitrary moment instead -- which _publish_panel used to do --
        # is wrong twice over: mid-flush it is torn on a page boundary, and
        # once the diff has swapped, [FRONT] is the buffer being rendered into
        # NEXT rather than the one on the panel. On screen that is a UI that
        # flickers and elements that come and go between frames.
        #
        # This is the same hook emu.panel.Capture installs, and it does change
        # the run it observes -- but this window already hooks intro_done,
        # mainloop, job_pump and the terminal loop, and it is a viewer, not a
        # measurement. Anything comparing totals should not be reading a GUI.
        if profile.panel_diff is not None and profile.fb_front is not None:
            def latch_frame(uc, a, s_, d):
                buf = panel.read(m, profile.fb_front)
                if buf is not None:
                    self._panel_latch = buf
            at(profile.panel_diff, latch_frame)
        else:
            print('[gui] WARNING: panel_diff/fb_front did not resolve for this '
                  'image; falling back to reading the framebuffer at an '
                  'arbitrary moment, which may tear', flush=True)
        self._pits = pits
        if self._audio_sources:
            # The SSI clock starts at the timers' own instruction count, so
            # both share one clock from the first step.
            self._audio_sources[0].align(pits.now)
            self.audio_on = True
            if self.audio_live:
                print('[gui] audio: LIVE at %d Hz (emulated CPU %.0fM '
                      'instructions/s)' % (self.audio_cfg['rate'],
                                           (self._post_intro_ips
                                            or pits.sources[0].ips) / 1e6),
                      flush=True)
            else:
                print('[gui] audio: recording at a %d Hz emulated clock, '
                      'played back at %d Hz (Unicorn lacks the digikit '
                      'accelerators)' % (self.audio_cfg['request_hz'],
                                         self.audio_cfg['rate']), flush=True)
        self.ready.set()
        self.stats['status'] = 'running'
        self._pace_t0 = time.time()
        while not self.stop_flag.is_set():
            if self.pause.is_set():
                self.stats['status'] = 'paused'
                self._rate_t = None
                time.sleep(0.05)
                continue
            # Work out the status BEFORE blocking, not after: spin sits
            # inside Unicorn for a whole BUDGET, so whatever is set here is
            # what the UI shows for that whole window. Setting it afterwards
            # leaves the stale value on screen for the entire block and the
            # fresh one for microseconds.
            #
            # fps is measured between completed frames, so it holds its last
            # value forever once the firmware stops drawing. Decay it, or the
            # panel sits frozen while the status line claims 15 fps.
            idle = time.time() - self._frame_t
            if idle > 1.0:
                self.stats['fps'] = 0.0
                self.stats['status'] = 'running, no frame for %.0fs' % idle
            else:
                self.stats['status'] = 'running'
            due_ips, self._pending_ips[:] = (
                [e for e in self._pending_ips
                 if e[0] <= self.stats['instrs']],
                [e for e in self._pending_ips
                 if e[0] > self.stats['instrs']])
            for when, n in due_ips:
                for source in pits.sources:
                    source.ips = n
                if self._audio_sources:
                    # The SSI's request period is in the same instructions.
                    self._audio_sources[0].ips = n
                print('[gui] ips -> %d at %d' % (n, self.stats['instrs']),
                      flush=True)
            pc = self._drain_input(m, profile, pc)
            # A chunk of about 5 emulated ms: BUDGET instructions is that at
            # the stock rate, but a fraction of it once audio raises the rate.
            budget = max(BUDGET, pits.sources[0].ips // 200)
            pc, executed, stop = spin(m, pc, budget, pits=pits, fast=self.fast,
                                      async_events=self._audio_sources)
            if stop != 'limit':
                self.stats['status'] = 'halted: %s' % stop
                # Also to stdout: the status label is invisible to anyone
                # watching the terminal, which is where emu.run prints
                # everything else, so a halt there reads as a freeze.
                total = self.stats['instrs'] + executed
                print('[gui] HALTED: %s  at pc=0x%08x after %dM instr'
                      % (stop, pc, total // 1_000_000), flush=True)
                # And to `error`, so a window shows it and a caller can
                # tell a halt from a clean stop. The card is deliberately
                # NOT flushed and nothing is saved: the last saved session
                # and the image file still agree, which a half-run one
                # would not.
                self.error = ('halted: %s at pc=0x%08x after %dM instructions'
                              % (stop, pc, total // 1_000_000))
                break
            self.stats['instrs'] += executed
            now = time.time()
            if self._rate_t is None:
                self._rate_t, self._rate_instrs = now, self.stats['instrs']
            elif now - self._rate_t >= 1.0:
                rate = ((self.stats['instrs'] - self._rate_instrs)
                        / (now - self._rate_t))
                self.stats['wall_ips'] = rate
                self.stats['real'] = rate / pits.sources[0].ips
                self._rate_t, self._rate_instrs = now, self.stats['instrs']
            if self.realtime:
                # Sleep off whatever we are ahead of the hardware by. _paced
                # accumulates emulated seconds at the timers' live rate, so a
                # rate change from --ips-at is reflected immediately instead
                # of leaving the pacing keyed to the instruction count at the
                # old rate. Capped per sleep so pause and stop stay
                # responsive.
                self._paced += executed / pits.sources[0].ips
                ahead = self._paced - (time.time() - self._pace_t0)
                if ahead > 0.003:
                    naptime = min(ahead, 0.05)
                    self._slept += naptime
                    time.sleep(naptime)
            self._publish_panel(m)
            self._update_leds()
            self._update_audio()
            fired = pits.fired
            self.stats['pit'] = (fired.get('PIT0', 0), fired.get('PIT2', 0),
                                 fired.get('PIT3', 0))
            self.stats['dtim3'] = fired.get('DTIM3', 0)
            # Also say it on stdout: the window shows the panel, but the
            # interesting part of a post-intro run is what the OS is doing,
            # and that was previously visible only from emu.uiprobe.
            self._report()
            self.stats['pc'] = pc
            self.stats['tasks'] = len(ev['tasks'])
            self.stats['prints'] = len(ev['prints'])
            if self.profile.current_tcb is not None:
                try:
                    self.stats['tcb'] = struct.unpack(
                        '>I', m.uc.mem_read(self.profile.current_tcb, 4))[0]
                except UcError:
                    pass
        else:
            self._stop_cleanly(m, ev, st, pits)
        # The Digitone's DSP renders on a thread of its own under live
        # audio; it must be stopped before the engines go.
        dsp = ev.get('dspcpu')
        if dsp is not None:
            dsp.close()
        self._close_live()
        n_seen = len(m.fault_pages)
        n_kept = len(m.faults)
        capped = ' (truncated at max_fault_records)' if n_kept < n_seen else ''
        print('[gui] faults: %d distinct pages touched, %d records kept%s'
              % (n_seen, n_kept, capped), flush=True)

    def _stop_cleanly(self, m, ev, st, pits):
        """The run loop ended at a step boundary because stop_flag was set:
        flush the card, save the session (save_on_exit), and with
        release_card close the card, in that order."""
        self.finishing.set()
        self.stats['status'] = 'stopped'
        # The card's writes reach its image file only at flush; a stop is
        # the last chance before the process exits.
        esd = ev.get('esdhc')
        try:
            if esd is not None:
                esd.card.flush()
            self.flushed = True
        except Exception as exc:                       # noqa: BLE001
            print('[gui] +Drive image flush failed: %s' % exc, flush=True)
            self.save_error = '+Drive image flush failed: %s' % exc
        # Only after a good flush: a snapshot is half of a pair with the
        # card file, and saving one the file does not match is exactly
        # the stale pairing save_on_exit exists to prevent.
        if self.save_on_exit and self.flushed:
            self._save_session(m, ev, st, pits)
        # Last, once nothing of this machine will touch the card again: the
        # map pins the file until the Machine is collected, and whoever asked
        # (the panel, adding samples) is about to write it.
        if self.release_card and self.flushed and esd is not None:
            esd.card.close()

    def _save_session(self, m, ev, st, pits):
        """Save the stopped machine to save_on_exit, atomically. -> bool.

        Why at all: the firmware's mount state and its inode and bitmap
        caches live in RAM, the card's contents in the image file. Opening
        the old snapshot on top of a card this session wrote pairs stale
        caches with newer contents. Saving the RAM that matches the flushed
        card keeps the two together.

        The same save tools/introboot.py and tools/uisettle.py make, from
        the same build(), so it reopens here under the same manifest: the
        timers are claimed as a component, and ev['tasks'] -- a list of
        (entry, prio, tcb) -- becomes the TCB-keyed map restore_into reads,
        merged with the tasks the opened snapshot already carried.

        Written to PATH.tmp and renamed over PATH, so a failure part way
        leaves the previous session's snapshot intact rather than a torn
        one; the rename is retried while Windows reports PATH in use (see
        _replace). Runs on this thread, at a step boundary, after the run
        loop.

        The card is not in it: for a file-backed card the esdhc component
        stores an empty overlay and no erased ranges (Esdhc.
        checkpoint_state), because the flushed file already holds them and a
        resume.snap that carried them would replay them over the card on
        every launch. tests/test_panel_app.py checks that on a saved blob.
        """
        path = self.save_on_exit
        tmp = path + '.tmp'
        self.stats['status'] = 'saving'
        try:
            components = ev['checkpoint_components']
            if components.get('timers') is not pits:
                ev['claim_checkpoint_component']('timers', pits)
            tasks = {'%#010x' % tcb: info
                     for tcb, info in st.get('task_create_hits', {}).items()}
            tasks.update(('%#010x' % tcb,
                          {'entry': entry, 'prio': prio, 'tcb': tcb})
                         for entry, prio, tcb in ev.get('tasks', []))
            extra = {'n': st.get('n', 0) + self.stats['instrs'],
                     'tasks': tasks,
                     'note': 'saved on exit by emu.gui'}
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            save_snapshot(m, tmp, extra=extra, components=components,
                          manifest=ev.get('checkpoint_manifest'))
            _replace(tmp, path)
        except Exception as exc:                        # noqa: BLE001
            self.save_error = describe_error(exc)
            self.stats['status'] = 'save failed'
            print('[gui] session NOT saved to %s: %s' % (path, self.save_error),
                  flush=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        self.saved = path
        self.stats['status'] = 'saved'
        print('[gui] session saved to %s' % path, flush=True)
        return True

    def _start_leds(self, m, ev):
        """Start decoding the panel-MCU stream, seeded from the firmware's RAM.

        A resumed snapshot's stream starts empty, so without the seed every
        LED would stay dark until the firmware next happened to resend it.
        Only a device with a decoded LED map gets any of this.
        """
        if self.device is None or not getattr(self.device, 'leds', None):
            return
        self._uart = ev.get('uart_out')
        if self._uart is None:
            return
        version = getattr(self.firmware, 'version', None)
        product = getattr(self.device, 'name', None)
        self._led_state = panelleds.seed(
            lambda a, n: bytes(m.uc.mem_read(a, n)), version, product)
        if self._led_state is None:
            print('[gui] LEDs: no RAM seed for firmware %s; they light as the '
                  'firmware next sends them' % version, flush=True)
            self._led_state = panelleds.new_state(version, product)
        self._update_leds()

    def _update_leds(self):
        """Decode what the firmware sent the panel MCU since the last chunk.

        The bytes are consumed: nothing else in a GUI run reads the stream,
        and it carries every OLED tile too, so kept whole it would grow for
        the life of the session.
        """
        st, buf = self._led_state, self._uart
        if st is None:
            return
        if buf:
            data = bytes(buf)
            del buf[:]
            st.feed(data)
        elif st is getattr(self, '_leds_from', None):
            # Nothing new since the colours were last read: they only
            # change on feed, and working them out is not free.
            return
        self._leds_from = st
        now = st.colours()
        if now != self.leds:
            self.leds = now
            self.led_version += 1

    # Longest recording kept; older audio is dropped from the front.
    AUDIO_KEEP_S = 120

    def _update_audio(self):
        """Convert what the SSI moved since the last chunk into the recording.

        The transmit channel hands over raw 8-byte frames (two big-endian
        32-bit words); they are converted here, between chunks, rather than
        in the sink, which runs once per frame inside the run loop.
        """
        raw = self._audio_raw
        if not self.audio_on:
            return
        n = len(raw) // 8 * 8
        if n:
            pcm = audioout.frames_from_ssi(bytes(raw[:n]), 32,
                                           self.audio_cfg['sample_bits'])
            del raw[:n]
            keep = self.AUDIO_KEEP_S * self.audio_cfg['rate'] * 4
            with self._audio_lock:
                self._audio_pcm += pcm
                over = len(self._audio_pcm) - keep
                if over > 0:
                    del self._audio_pcm[:over + (-over % 4)]
            self.audio_frames += n // 8
            if self.audio_live and not self.audio_muted:
                self._live_write(pcm)
        now = time.time()
        if self._audio_t is None:
            self._audio_t, self._audio_mark = now, self.audio_frames
        elif now - self._audio_t >= 1.0:
            self.audio_speed = ((self.audio_frames - self._audio_mark)
                                / self.audio_cfg['rate'] / (now - self._audio_t))
            self._audio_t, self._audio_mark = now, self.audio_frames

    # Audio queued before live output starts, and again after it runs dry:
    # it absorbs the pacing's jitter (a Windows sleep can be 15 ms).
    LIVE_PREBUFFER_MS = 80

    def _live_write(self, pcm):
        """Send freshly rendered audio to the host device (worker thread)."""
        out = self._live_out
        if out is None:
            if self._live_error:
                return
            try:
                out = self._live_out = audioout.WaveOut(
                    self.audio_cfg['rate'], 2, buffers=40, block_ms=10)
            except OSError as exc:
                self._live_error = str(exc)
                print('[gui] live audio unavailable: %s' % exc, flush=True)
                return
            out.gain = self._volume
        if self._live_started and out.queued() == 0:
            # Ran dry (the emulator fell behind): build the cushion again
            # rather than dribbling out block by block.
            self._live_started = False
            self.live_underruns += 1
        self._live_buf += pcm
        if not self._live_started:
            need = self.audio_cfg['rate'] * 4 * self.LIVE_PREBUFFER_MS // 1000
            if len(self._live_buf) < need:
                return
            self._live_started = True
        out.write(bytes(self._live_buf))
        del self._live_buf[:]

    def audio_mute(self, muted):
        """Silence (or restore) live output; the recording carries on."""
        self.audio_muted = bool(muted)
        self._live_started = False
        del self._live_buf[:]

    def set_volume(self, gain):
        """Software gain on the live output (Master Volume knob)."""
        self._volume = max(0.0, float(gain))
        if self._live_out is not None:
            self._live_out.gain = self._volume

    def live_latency_ms(self):
        out = self._live_out
        return 0 if out is None else out.queued() * 10

    def _close_live(self):
        out, self._live_out = self._live_out, None
        if out is not None:
            try:
                out.close()
            except Exception:                            # noqa: BLE001
                pass

    def audio_seconds(self):
        """Seconds of audio in the recording."""
        if not self.audio_cfg:
            return 0.0
        with self._audio_lock:
            n = len(self._audio_pcm)
        return n / 4 / self.audio_cfg['rate']

    def audio_take(self):
        """A copy of the recording (16-bit LE stereo), safe from any thread."""
        with self._audio_lock:
            return bytes(self._audio_pcm)

    def audio_clear(self):
        with self._audio_lock:
            del self._audio_pcm[:]

    def _publish_panel(self, m):
        """Once the OS owns the panel, draw the firmware's framebuffer.

        The frame comes from `_panel_latch`, grabbed at the diff's entry where
        emu/panel.py guarantees [FRONT] is complete and untorn. Polling the
        pointer here instead would sample at an arbitrary point in the flush
        and, after a swap, read the buffer being rendered into next -- the
        window flickered for exactly that reason. The fallback read is only
        for an image where panel_diff did not resolve, so no latch exists.

        A frame is counted when the bytes change, which is the firmware's own
        notion of a new frame -- unlike the setPixel path, which has to infer
        one from a repeated coordinate.
        """
        if not self.use_panel:
            return
        buf = self._panel_latch
        if buf is None:
            buf = panel.read(m, self.fb_front)
        if buf is None or buf == self._last_panel:
            return
        px = panel.lit(buf)
        if not px and not self._panel_live:
            # INTRO_DONE fires tens of millions of instructions before the OS
            # first composes a frame, and the buffer is empty until it does.
            # Blanking the window for that whole stretch would look like a
            # regression, so hold the intro's last frame until there is
            # something real to replace it with. Once the OS has drawn, later
            # blanks are genuine and do get shown.
            return
        self._panel_live = True
        self._last_panel = buf
        # A new buffer, swapped in whole: the window thread copies `fb`
        # whenever it redraws, and clearing and refilling the old one in
        # place let it copy a frame that was half the last and half this.
        fb = bytearray(W * H)
        for x, y in px:
            fb[y * W + x] = 1
        self.fb = fb
        now = time.time()
        self.captured.append(bytes(fb))
        self.stats['frames'] += 1
        self.stats['fps'] = 1.0 / max(1e-6, now - self._frame_t)
        self.stats['panel_lit'] = len(px)
        self.stats['source'] = 'panel'
        self._frame_t = now
        self.version += 1

    def _stack_backtrace(self, m, depth=64):
        """Scan upward from A7 for values that look like main OS code addresses.

        This is not a real unwound backtrace -- it is a raw scan of `depth`
        longwords above the current stack pointer, reporting every one that
        falls inside the main OS code span. Some of those will be stale data
        left over from earlier calls rather than live return addresses, but
        with 34 call sites funneling into the same 2-byte trap, even a noisy
        list of candidates is more than the bare PC tells us.

        Does NOT read SR -- reg_read(SR) between emu_start calls clobbers
        condition codes and has deadlocked a guest mutex before.
        """
        candidates = []
        a7 = m.uc.reg_read(UC_M68K_REG_A7)
        for i in range(depth):
            offset = i * 4
            try:
                word = struct.unpack('>I', m.uc.mem_read(a7 + offset, 4))[0]
            except Exception:
                break
            if 0x40000400 <= word <= 0x40307f60:
                candidates.append((offset, word))
        return candidates

    def _report(self):
        """One stdout line per ~20M instructions of OS progress.

        The window shows the panel, which after the intro is mostly blank; the
        part worth watching is what the OS is doing behind it. Printing it here
        means `uv run python -m emu.gui` says the same thing
        `python -m emu.uiprobe run` would, without needing a second run.
        """
        s = self.stats
        # Every ~20M instructions at the stock rate, i.e. every ~4 emulated
        # seconds -- scaled so a raised rate (live audio) does not flood.
        ips = self._pits.sources[0].ips if self._pits is not None else 0
        step = s['instrs'] // max(20_000_000, 4 * ips)
        if step == self._reported:
            return
        self._reported = step
        note = ''
        if s['terminal']:
            note = ('   TERMINAL LOOP at 0x4012d2fa -- the main task is hung '
                    'on a weak pointer; re-run with --weakptr to step over it')
            if not self._fault_summary_printed:
                self._fault_summary_printed = True
                m = self._m
                recs = sorted(m.faults, key=lambda r: r['count'],
                              reverse=True)[:20]
                print('[gui] fault summary at terminal loop: %d distinct '
                      'pages touched, top %d by count'
                      % (len(m.fault_pages), len(recs)), flush=True)
                for rec in recs:
                    kinds = '+'.join('%s:%d' % (k, c)
                                      for k, c in sorted(rec['kinds'].items()))
                    print('[gui]   page=0x%08x first=0x%08x pc=0x%08x '
                          'count=%d %s'
                          % (rec['page'], rec['first_addr'], rec['first_pc'],
                             rec['count'], kinds), flush=True)
            if not self._backtrace_printed:
                self._backtrace_printed = True
                try:
                    a7 = self._uc.reg_read(UC_M68K_REG_A7)
                    frames = self._stack_backtrace(self._m)[:24]
                    print('[gui] stack at terminal loop (A7=0x%08x, '
                          'candidate return addresses from a raw stack '
                          'scan, not a real unwound backtrace -- some will '
                          'be stale data):' % a7, flush=True)
                    for offset, addr in frames:
                        print('[gui]   +0x%03x  0x%08x' % (offset, addr),
                              flush=True)
                except Exception:
                    pass
        print('[gui] %5.0fM instr  PIT0/2/3 %d/%d/%d  DTIM3 %d  '
              'mainloop %d  jobs %d  tasks %d  %s %d  %.2fM instr/s  '
              '%.0f%% of real time%s'
              % (s['instrs'] / 1e6, s['pit'][0], s['pit'][1], s['pit'][2],
                 s['dtim3'],
                 s['mainloop'], s['jobs'], s['tasks'],
                 s['source'], s['panel_lit'] if s['source'] == 'panel'
                 else s['px'], s['wall_ips'] / 1e6, 100.0 * s['real'],
                 note), flush=True)


class Controls(tk.Frame):
    """The front panel: click a button, scroll an encoder.

    Laid out from the device file's groups and labelled with the firmware's
    own control names, so the same code draws both products and neither the
    arrangement nor the labels are written down here.

    Every interaction goes onto the emulator's queue rather than touching
    guest memory, because the worker is inside Unicorn for a whole BUDGET at
    a time. A mouse cannot hold one button down while clicking another, so
    buttons in a LATCHING_GROUPS group (e.g. FUNC) toggle instead of being
    momentary: a click asserts the modifier and it STAYS asserted -- through
    as many other button clicks and encoder turns as needed -- until it is
    clicked again or explicitly cleared with the "clear" button. Chords are
    formed by latching the modifier, then clicking as many other buttons as
    needed, which is what lets a click on FUNC followed by a click on SRC
    reach SRC's secondary function. An earlier version
    auto-released the modifier after the next non-modifier button's release,
    but that put the modifier's release in the same input-drain window as
    the chorded button's, and the firmware appeared to react to both going
    up together; clearing is explicit now instead.
    """

    BG = '#15181d'
    FACE = '#222831'
    TEXT = '#cdd6e3'

    def __init__(self, master, device, button_names, encoder_names, send):
        super().__init__(master, bg=self.BG)
        self.device = device
        self.button_names = button_names
        self.encoder_names = encoder_names
        self.send = send
        self._latching = frozenset(
            c for g in device.groups if g.name in LATCHING_GROUPS
            for c in g.codes)
        self._latched = {}          # code -> widget, currently latched
        self._build()

    # Character cells across before wrapping to a new row. Measured, not
    # guessed: on a 14" display 104 packs into 3 rows but 1414px wide, which
    # crowds the window edge, while 60 wraps to 7 rows and makes the surface
    # taller than the screen it saved. Anything from 84 to 92 lands on the
    # same 4-row, 1028x261 layout; 88 sits in the middle of that plateau.
    ROW_BUDGET = 88

    def _label(self, code, kind):
        # A measured name from the device file wins over the firmware's own
        # table: on Digitakt mk1 that table labels the panel-test screen, so
        # its "PLAY" is a trig. See devices/digitakt.toml [panel.labels].
        if kind == 'button':
            measured = getattr(self.device, 'labels', None) or {}
            if code in measured:
                return measured[code]
        names = self.button_names if kind == 'button' else self.encoder_names
        return names.get(code) or '#%d' % code

    def _group_labels(self, group):
        """-> {code: text}, with a prefix the whole group shares dropped.

        `TRIG 1`..`TRIG 16`, inside a box already titled `trigs`, spends most
        of its width repeating the word TRIG. Only a prefix ending in a space
        is cut, so PLAY and PLUS are never mangled into Y and S. A lone
        control keeps whatever follows its last space for the same reason:
        `ENCODER LEVEL` becomes `LEVEL`, while `SAMPLING` is left alone.
        """
        labels = {c: self._label(c, group.kind) for c in group.codes}
        if len(labels) < 2:
            return {c: t.rsplit(' ', 1)[-1] for c, t in labels.items()}
        cut = os.path.commonprefix(list(labels.values())).rfind(' ') + 1
        if cut <= 0:
            return labels
        return {c: (t[cut:] or t) for c, t in labels.items()}

    def _group_width(self, group, labels):
        """-> roughly how many character cells wide this group will render."""
        cells = group.columns or len(group.codes)
        widest = max((len(t) for t in labels.values()), default=2)
        return cells * (widest + (4 if group.kind == 'encoder' else 2))

    def _build(self):
        # Pack groups into rows greedily rather than on a fixed column count.
        # They differ enormously in width -- sixteen trig keys against a lone
        # LEVEL -- so a rigid grid sizes every column to its widest member,
        # leaving most of the window empty while still clipping the rest off
        # the bottom.
        row = column = used = 0
        for group in self.device.groups:
            labels = self._group_labels(group)
            width = self._group_width(group, labels)
            if column and used + width > self.ROW_BUDGET:
                row, column, used = row + 1, 0, 0
            box = tk.LabelFrame(self, text=group.name, bg=self.BG,
                                fg='#5d6a7c', bd=1, labelanchor='nw',
                                font=('SF Mono', 8))
            box.grid(row=row, column=column, sticky='nw', padx=4, pady=3)
            if group.kind == 'encoder':
                self._encoders(box, group, labels)
            elif group.layout == 'dpad':
                self._dpad(box, group, labels)
            else:
                self._buttons(box, group, labels)
            column += 1
            used += width
        if column and used + 2 > self.ROW_BUDGET:
            row, column = row + 1, 0
        box = tk.LabelFrame(self, text='latch', bg=self.BG, fg='#5d6a7c',
                            bd=1, labelanchor='nw', font=('SF Mono', 8))
        box.grid(row=row, column=column, sticky='nw', padx=4, pady=3)
        tk.Button(box, text='clear', bg=self.FACE, fg=self.TEXT,
                  activebackground='#3a4654', activeforeground='#ffffff',
                  relief='raised', bd=1, highlightthickness=0,
                  font=('SF Mono', 8), padx=0, pady=0,
                  command=self._consume_latched).pack(padx=1, pady=1)

    def _buttons(self, box, group, labels):
        columns = group.columns or len(group.codes)
        for i, code in enumerate(group.codes):
            self._button(box, code, labels[code]).grid(
                row=i // columns, column=i % columns, padx=1, pady=1)

    # Where each arrow belongs, keyed by the firmware's own label. Placing by
    # NAME rather than by code order matters: Digitakt II's table lists the
    # arrows UP, LEFT, DOWN, RIGHT but Digitakt (mk1)'s lists them LEFT, UP,
    # DOWN, RIGHT, so a fixed positional zip puts mk1's cursor keys in the
    # wrong holes -- silently, since every code still lands somewhere.
    DPAD_PLACES = {'UP': (0, 1), 'LEFT': (1, 0), 'DOWN': (1, 1),
                   'RIGHT': (1, 2)}

    def _dpad(self, box, group, labels):
        fallback = ((0, 1), (1, 0), (1, 1), (1, 2))
        for i, code in enumerate(group.codes):
            place = self.DPAD_PLACES.get(labels[code].strip().upper())
            if place is None:
                place = fallback[i] if i < len(fallback) else (2, i)
            self._button(box, code, labels[code]).grid(
                row=place[0], column=place[1], padx=1, pady=1)

    def _button(self, box, code, text):
        widget = tk.Button(box, text=text, bg=self.FACE, fg=self.TEXT,
                           activebackground='#3a4654',
                           activeforeground='#ffffff', relief='raised', bd=1,
                           highlightthickness=0, font=('SF Mono', 8),
                           width=max(2, len(text)), padx=0, pady=0)
        # Bound rather than given a `command`, which fires only on release:
        # the wire carries button STATE, so a held button must stay held.
        if code in self._latching:
            widget.bind('<ButtonPress-1>',
                        lambda _e, c=code, w=widget: self._toggle_latch(c, w))
        else:
            widget.bind('<ButtonPress-1>',
                        lambda _e, c=code: self.send('press', c, 0))
            widget.bind('<ButtonRelease-1>',
                        lambda _e, c=code: self.send('release', c, 0))
        return widget

    def _toggle_latch(self, code, widget):
        if code in self._latched:
            self.send('release', code, 0)
            del self._latched[code]
            widget.configure(relief='raised', bg=self.FACE)
        else:
            self.send('press', code, 0)
            self._latched[code] = widget
            widget.configure(relief='sunken', bg='#3a4654')

    def _consume_latched(self):
        for code, widget in self._latched.items():
            self.send('release', code, 0)
            widget.configure(relief='raised', bg=self.FACE)
        self._latched.clear()

    def _encoders(self, box, group, labels):
        columns = group.columns or len(group.codes)
        for i, code in enumerate(group.codes):
            cell = tk.Frame(box, bg=self.BG)
            cell.grid(row=i // columns, column=i % columns, padx=2, pady=1)
            face = tk.Label(cell, text=labels[code], bg=self.FACE,
                            fg=self.TEXT, font=('SF Mono', 9), width=5, pady=2)
            face.pack()
            strip = tk.Frame(cell, bg=self.BG)
            strip.pack()
            for text, step in (('-', -1), ('+', 1)):
                tk.Button(strip, text=text, bg=self.FACE, fg=self.TEXT, bd=1,
                          font=('SF Mono', 8), width=2, padx=0, pady=0,
                          command=lambda c=code, s=step:
                          self._encoder_turn(c, s)).pack(side='left')
            # Wheel over an encoder turns it. Tk reports the wheel differently
            # per platform -- a signed delta on macOS and Windows, buttons 4
            # and 5 on X11 -- so all three are bound.
            for widget in (cell, face):
                widget.bind('<MouseWheel>',
                            lambda e, c=code: self._wheel(e, c))
                widget.bind('<Button-4>',
                            lambda _e, c=code: self._encoder_turn(c, 1))
                widget.bind('<Button-5>',
                            lambda _e, c=code: self._encoder_turn(c, -1))

    def _encoder_turn(self, code, step):
        self.send('encoder', code, step)

    def _wheel(self, event, code):
        step = 1 if event.delta > 0 else -1
        if event.state & 0x0001:            # shift held: coarse
            step *= 10
        self._encoder_turn(code, step)


class Panel(tk.Frame):
    def __init__(self, master, scale=7):
        super().__init__(master, bg='#0b0d10')
        self.scale = scale
        self.img = tk.PhotoImage(width=W, height=H)
        self.big = tk.PhotoImage(width=W * scale, height=H * scale)
        self.view = tk.Label(self, bd=0, highlightthickness=0, bg='#0b0d10',
                             image=self.big)
        self.view.pack(padx=18, pady=18)
        self._blank()

    def _blank(self):
        self.draw(bytearray(W * H))

    def draw(self, fb):
        body = b''.join(ON if v else OFF for v in fb)
        self.img.put(b'P6\n%d %d\n255\n' % (W, H) + body, to=(0, 0, W, H))
        # copy -zoom writes into the existing image; PhotoImage.zoom would
        # allocate a new one every refresh.
        self.tk.call(self.big, 'copy', self.img, '-zoom', self.scale, self.scale)


class App(tk.Tk):
    def __init__(self, snapshot, weakptr=False, slc=False, scale=None,
                 syx=None, fast=True, realtime=True, patch_machine=False,
                 patch_eighth=7, patch_machine_spec=None,
                 panel_dwell=PANEL_DWELL_MS, ips_at=(),
                 post_intro_ips=4 * INSTR_PER_SEC):
        super().__init__()
        self.title('Digi emulator')
        self.configure(bg='#15181d')
        self.snapshot = snapshot
        # 128x64 is unreadable at 1:1, but the panel is not the point of the
        # window any more -- the control surface below it is, and it needs
        # real estate. RESERVE_H is what the buttons, the toolbar and the
        # status lines want; the zoom is whatever integer fits in the rest.
        # Capped well below what a large display would allow, because a panel
        # scaled edge to edge pushes the controls off the bottom. Integer
        # only: a fractional zoom would resample and invent pixels the
        # firmware never drew. Override with --scale.
        if scale is None:
            avail_w = max(1, self.winfo_screenwidth() - 160)
            avail_h = max(1, self.winfo_screenheight() - RESERVE_H)
            scale = max(1, min(MAX_SCALE, avail_w // W, avail_h // H))
        self.scale = scale

        self.panel = Panel(self, scale=scale)
        self.panel.pack(padx=14, pady=(14, 6))
        self.controls = None        # built once the worker knows the device

        bar = tk.Frame(self, bg='#15181d')
        bar.pack(fill='x', padx=20, pady=(0, 6))
        self.btn = ttk.Button(bar, text='Pause', width=9, command=self.toggle)
        self.btn.pack(side='left')
        ttk.Button(bar, text='Restart', width=9,
                   command=self.restart).pack(side='left', padx=6)
        ttk.Button(bar, text='Save PNG', width=9,
                   command=self.save).pack(side='left')
        self.replay_btn = ttk.Button(bar, text='Replay 15fps', width=12,
                                     command=self.toggle_replay)
        self.replay_btn.pack(side='left', padx=6)
        self.frames_lbl = tk.Label(bar, text='', bg='#15181d', fg='#7f8b9c',
                                   font=('SF Mono', 11))
        self.frames_lbl.pack(side='right')

        self.status = tk.Label(self, text='', bg='#15181d', fg='#9aa7b8',
                               font=('SF Mono', 11), anchor='w', justify='left')
        self.status.pack(fill='x', padx=20, pady=(0, 14))

        self.emu = None
        self.weakptr = weakptr
        self.slc = slc
        self.syx = syx
        self.fast = fast
        self.realtime = realtime
        self.patch_machine = patch_machine
        self.patch_eighth = patch_eighth
        self.patch_machine_spec = patch_machine_spec
        self.panel_dwell = panel_dwell
        self.ips_at = ips_at
        self.post_intro_ips = post_intro_ips
        self.shown = -1
        self.replay = None          # (frames, index, next_due) while replaying
        self.start()
        self.protocol('WM_DELETE_WINDOW', self.quit_all)
        self.after(60, self.tick)

    def start(self):
        self.emu = Emulator(self.snapshot, weakptr=self.weakptr,
                            slc=self.slc, syx=self.syx, fast=self.fast,
                            realtime=self.realtime,
                            patch_machine=self.patch_machine,
                            patch_eighth=self.patch_eighth,
                            patch_machine_spec=self.patch_machine_spec,
                            panel_dwell=self.panel_dwell,
                            ips_at=self.ips_at,
                            post_intro_ips=self.post_intro_ips)
        self.emu.start()

    def send_input(self, kind, code, arg):
        """Hand one panel event to the worker. Never touches guest memory."""
        if self.emu:
            self.emu.inbox.append((kind, code, arg))

    def _ensure_controls(self):
        """Build the control surface once the worker has identified the device."""
        if self.controls is not None or not self.emu or not self.emu.device:
            return
        self.controls = Controls(self, self.emu.device, self.emu.button_names,
                                 self.emu.encoder_names, self.send_input)
        self.controls.pack(padx=14, pady=(0, 10))

    def restart(self):
        if self.controls is not None:
            self.controls.destroy()
            self.controls = None
        if self.emu:
            self.emu.stop_flag.set()
            self.emu.pause.clear()
            self.emu.join(timeout=3)
        self.panel._blank()
        self.shown = -1
        self.replay = None
        self.replay_btn.configure(text='Replay 15fps')
        self.start()
        self.btn.configure(text='Pause')

    def toggle_replay(self):
        """Play the captured frames back at the rate the firmware asks for.

        Emulating in real time needs ~3x more throughput than we have, but the
        frames themselves are correct -- so replaying them at FRAME_HZ shows
        the animation at its true speed even though producing it was slower.
        """
        if self.replay is not None:
            self.replay = None
            self.replay_btn.configure(text='Replay 15fps')
            return
        frames = list(self.emu.captured) if self.emu else []
        if not frames:
            self.status.configure(text='nothing captured yet - let it run first')
            return
        if self.emu:
            self.emu.pause.set()
            self.btn.configure(text='Resume')
        self.replay = [frames, 0, time.time()]
        self.replay_btn.configure(text='Stop replay')

    def toggle(self):
        if not self.emu:
            return
        if self.emu.pause.is_set():
            self.emu.pause.clear()
            self.btn.configure(text='Pause')
        else:
            self.emu.pause.set()
            self.btn.configure(text='Resume')

    def save(self):
        os.makedirs('out', exist_ok=True)
        s = 6
        px = bytearray(W * s * H * s)
        for i, v in enumerate(self.emu.fb):
            if v:
                x, y = i % W, i // W
                for dy in range(s):
                    row = (y * s + dy) * W * s + x * s
                    for dx in range(s):
                        px[row + dx] = 255
        open('out/panel.png', 'wb').write(png(px, W * s, H * s))
        self.status.configure(text='wrote out/panel.png')

    def tick(self):
        if self.replay is not None:
            frames, i, due = self.replay
            now = time.time()
            if now >= due:
                self.panel.draw(frames[i])
                i = (i + 1) % len(frames)
                self.replay = [frames, i, max(now, due + 1.0 / FRAME_HZ)]
                self.frames_lbl.configure(
                    text='replay %d/%d at %.2f fps (true speed)'
                         % (i, len(frames), FRAME_HZ))
                self.status.configure(
                    text='replaying captured frames at the firmware\'s own rate\n'
                         'PIT3: (0x2191+1) x 1024 = 8,800,256 bus cycles @ 132 MHz',
                    fg='#9aa7b8')
            self.after(10, self.tick)
            return
        e = self.emu
        if e:
            if e.error:
                self.status.configure(text=e.error, fg='#ff8f8f')
            else:
                self._ensure_controls()
                if e.version != self.shown:
                    self.panel.draw(e.fb)      # skip if nothing was drawn
                    self.shown = e.version
                s = e.stats
                self.frames_lbl.configure(
                    text='frame %d   %.1f / %.1f fps   %.2fM instr/s  '
                         '(%.0f%% of real time)'
                         % (s['frames'], s['fps'], FRAME_HZ,
                            s['wall_ips'] / 1e6, 100.0 * s['real']))
                extra = ('  HUNG: terminal loop 0x4012d2fa (try --weakptr)'
                         if s['terminal'] else '')
                self.status.configure(
                    text='%s   %.1f fps   %d frames   %d tasks%s\n'
                         'pc 0x%08x   task 0x%08x   source %s   %s %d\n'
                         'PIT0 %d   PIT2 %d   PIT3 %d   DTIM3 %d   '
                         'mainloop %d   jobs %d   %.1fM instructions'
                         % (s['status'], s['fps'], s['frames'], s['tasks'],
                            extra,
                            s['pc'], s['tcb'], s['source'],
                            'lit' if s['source'] == 'panel' else 'setPixel',
                            s['panel_lit'] if s['source'] == 'panel'
                            else s['px'],
                            s['pit'][0], s['pit'][1], s['pit'][2],
                            s['dtim3'], s['mainloop'], s['jobs'],
                            s['instrs'] / 1e6),
                    fg='#9aa7b8')
        self.after(60, self.tick)

    def quit_all(self):
        # Join before tearing down: the worker is inside Unicorn between
        # chunks, and letting the interpreter kill a daemon thread mid-
        # emu_start crashes the process on exit (SIGBUS).
        if self.emu:
            self.emu.stop_flag.set()
            self.emu.pause.clear()
            self.emu.join(timeout=3)
        self.destroy()


def parse_count(s):
    if s and s[-1] in ('M', 'm'):
        return int(float(s[:-1]) * 1_000_000)
    return int(s, 0)


if __name__ == '__main__':
    # --weakptr steps over the weak-pointer branches that otherwise freeze the
    # main task in the terminal loop after 153 messages. It is a diagnostic,
    # not a fix -- see longrun.build.  --scale N forces the integer panel zoom.
    argv = sys.argv[1:]
    weakptr = '--weakptr' in argv
    slc = '--slc' in argv
    # --exact restores `count=` stepping: slower by about 7.6x, but every
    # timer lands on the instruction it was due at. Use it when comparing a
    # run against bootcheck, never for ordinary interactive use.
    # --unthrottled lets the worker run as fast as it can instead of pacing
    # itself to the hardware's clock.
    fast = '--exact' not in argv
    realtime = '--unthrottled' not in argv
    # --patch-machine installs the experimental eighth machine (PLACEHOLDER)
    # into the machine list. Bare, it applies all nine parts (list, dispatch,
    # group, name, rank, permit, hint, pertype, clone); --patch-machine=list,
    # =dispatch, =group, =name, =rank, =permit, =hint, =pertype, or =clone
    # applies just that part, and a +-separated combination
    # (--patch-machine=list+dispatch) applies exactly those, for bisecting.
    # An optional :N suffix on the parts value (e.g. --patch-machine=list:6)
    # sets the 8th list entry's value, default 7.
    # Unknown part names are refused by machinepatch.patch_b.
    patch_machine = False
    patch_eighth = 7
    patch_machine_spec = None
    for a in argv:
        if a == '--patch-machine':
            patch_machine = ('list', 'dispatch', 'group', 'name', 'rank',
                              'permit', 'hint', 'pertype', 'clone')
        elif a.startswith('--patch-machine='):
            value = a.split('=', 1)[1]
            if ':' in value:
                parts_str, eighth_str = value.split(':', 1)
                patch_eighth = int(eighth_str, 0)
            else:
                parts_str = value
            patch_machine = tuple(parts_str.split('+'))
        elif a.startswith('--machine='):
            patch_machine_spec = a.split('=', 1)[1]
    scale = None
    if '--scale' in argv:
        i = argv.index('--scale')
        scale = max(1, int(argv[i + 1]))
        del argv[i:i + 2]
    # --panel-dwell N (emulated ms) overrides PANEL_DWELL_MS; 0 disables pacing and
    # restores the old coalesce-everything-into-one-feed behaviour, for
    # testing the two against each other.
    panel_dwell = PANEL_DWELL_MS
    if '--panel-dwell' in argv:
        i = argv.index('--panel-dwell')
        panel_dwell = max(0, int(argv[i + 1]))
        del argv[i:i + 2]
    syx = None
    if '--syx' in argv:
        i = argv.index('--syx')
        syx = argv[i + 1]
        del argv[i:i + 2]
    # --ips-at WHEN:N (repeatable) changes the timer rate to N instructions
    # per emulated second once instruction count WHEN is reached. WHEN and N
    # both accept a plain integer or an M-suffixed count (80M, 18.72M).
    ips_at = []
    while '--ips-at' in argv or any(a.startswith('--ips-at=') for a in argv):
        if '--ips-at' in argv:
            i = argv.index('--ips-at')
            spec = argv[i + 1]
            del argv[i:i + 2]
        else:
            i = next(j for j, a in enumerate(argv)
                     if a.startswith('--ips-at='))
            spec = argv[i].split('=', 1)[1]
            del argv[i:i + 1]
        if ':' not in spec:
            raise SystemExit('--ips-at expects WHEN:N, got %r' % spec)
        when_str, n_str = spec.split(':', 1)
        ips_at.append((parse_count(when_str), parse_count(n_str)))
    ips_at.sort()
    # --post-intro-ips N sets the timer rate applied once the intro hands
    # over (default 4x INSTR_PER_SEC, which keeps the UI queue drained); 0
    # keeps the default rate. Ignored when --ips-at is given.
    post_intro_ips = 4 * INSTR_PER_SEC
    if '--post-intro-ips' in argv:
        i = argv.index('--post-intro-ips')
        post_intro_ips = max(0, parse_count(argv[i + 1]))
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith('--')]
    snap = args[0] if args else 'snapshots/boot400M.snap'
    if not os.path.exists(snap):
        raise SystemExit('no such snapshot: %s\n'
                         'build one with:  uv run python -m emu.checkpoint make '
                         '60000000,120000000,200000000,280000000,400000000' % snap)
    App(snap, weakptr=weakptr, slc=slc, scale=scale, syx=syx, fast=fast,
        realtime=realtime, patch_machine=patch_machine,
        patch_eighth=patch_eighth, patch_machine_spec=patch_machine_spec,
        panel_dwell=panel_dwell, ips_at=ips_at,
        post_intro_ips=post_intro_ips).mainloop()
