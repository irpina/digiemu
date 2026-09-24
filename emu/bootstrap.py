"""First run: turn a firmware folder into a settled gui.snap, once, resumably.

The portable app gives every firmware release its own folder (emu/portable.py)
and this module fills it. It is the recipe that used to be four commands --
`emu.extract`, `emu.checkpoint make`, tools/introboot.py, tools/uisettle.py --
lifted into functions, so one worker process can run them in order, report
progress as Events, stop when asked, and on the next start pick up at the
first stage whose outputs do not verify. The two tools are now thin argparse
wrappers over boot_to_ui() and settle_ui() and print exactly what these
functions put in Event.text.

    extract  sections/ from the .syx
    card     plusdrive.img, created and FORMATTED (ekFS) before the cold boot
    ladder   cold_boot()           -> boot.snap, where the firmware parks
    intro    boot_to_ui(boot.snap) -> gui-raw.snap, the first live UI frame
    settle   settle_ui(gui-raw)    -> gui.snap, after first-boot work is over

The stage is still called 'ladder' (stamps, logs, the launcher) though it no
longer builds the fixed 60M..400M ladder of `emu.checkpoint make`: the cold
boot stops where the firmware parks, and the intro and settle use the
panel's block-bounded stepping (longrun.spin fast=True). That took the first
run from ~8 minutes to ~25 s on the reference desktop.

Why the card comes before the ladder. The cold boot identifies the card
and mounts it, no resume ever repeats either, and so whatever the card holds
then is frozen into every snapshot that follows. A
blank card made the firmware do its whole "INITIALIZING +DRIVE" format during
the settle and never finish inside the budget; a card laid down by
emu/ekfsformat.py -- the firmware's own format, byte for byte -- finishes
first boot and settles (measured: settled at 1050M instructions).

Why the stamps follow the card. After settle, gui.snap's RAM and the card file
describe the same machine: the snapshot no longer carries card writes (see
Esdhc.checkpoint_state), the file is the truth. Each emulator stage therefore
records the card's size and mtime when it finished ('card_after'), and a chain
whose last verified stage does not match the card as it is now is rebuilt from
the ladder: the firmware has to meet the card it will actually run against.
That is also what "Rebuild" after the user changed the card amounts to -- call
first_run again. The card itself is only ever formatted when it has no valid
ekFS, so a rebuild never touches the user's samples or projects.

Why a snapshot that matches the card stops a rebuild. A played folder has a
card that changed since the settle and a resume.snap saved against it; that
pair is as good as gui.snap and its card, and first_run leaves it alone (it
used to rebuild from the cold boot and delete the session). Only when neither
gui.snap nor resume.snap matches the card is the chain rebuilt.

Why settle counts frames as they arrive. uisettle used panel.Capture, which
stops storing frames at 4096, and recomputed the trailing run from the stored
list -- so the run of main-UI frames froze at frame 4096. First boot on a
fresh card takes about 4100 frames, so the tool could never see it finish and
reported NOT SETTLED after its whole 3000M budget. settle_ui keeps running
counts and only the last frame.

Why settle is not stamped on pixels alone. A first-boot job that fails stops
drawing its overlay too. accept_settled checks the card the firmware wrote
(check_card) and, where the device file gives addresses for the release, the
job's error code, progress and the sample-volume mount in the saved RAM. And
because the job writes BEEFBACE to sector 0 long before it finishes -- after
which no cold boot starts it again -- a rebuild of a card that never finished
first boot clears sector 0 first (clear_half_install).

Everything emulator-side is imported inside the functions, so the launcher can
use FirmwarePaths, the state file and choose_snapshot without loading Unicorn
(the build id comes from emu.checkpointver, not emu.snapshot; a test checks).
Every stage reads the main image and the card from the DT2_* environment
(longrun.build and Esdhc take no explicit paths), which is why first_run must
be called with os.environ already holding paths.env().
"""
import datetime
import gc
import glob
import hashlib
import json
import os
import shutil
import struct
import time
from dataclasses import dataclass, field

STEPS = ('copy', 'extract', 'card', 'ladder', 'intro', 'settle')
KINDS = ('start', 'tick', 'note', 'done')

# Bump when what a stage produces changes in a way an existing folder has to
# be rebuilt for (a different ladder, intro policy, settle rule, snapshot
# content). Stored in every stamp; a mismatch reruns from that stage.
RECIPE = 1

# How much of the whole first run each stage is, for one progress bar. On
# the reference desktop the cold boot takes ~4 s, the intro ~7 s and the
# settle ~11 s (a first boot's factory install; ~1 s on a card that already
# has it); extract and card take a second.
WEIGHTS = {'extract': 0.0, 'card': 0.0, 'ladder': 0.15, 'intro': 0.35,
           'settle': 0.50}
# Where a stage is expected to finish, in instructions, for its fraction.
# Past it the bar holds just short of full and the Event says indeterminate.
COLD_EXPECTED = 170_000_000
INTRO_EXPECTED = 100_000_000
SETTLE_EXPECTED = 800_000_000

# The first run's cold boot (cold_boot): run in chunks of `chunk`
# (estimated) instructions until the firmware PARKS -- a chunk spent mostly
# in its idle spin (more than 1/idle_fraction of the chunk's blocks) with no
# task created. On mk1 1.53 that is ~159M in: the tasks are up and the intro
# waits for a PIT3 tick the cold boot does not deliver, so every instruction
# after it is idle -- the old fixed 400M ladder spent 60% of its time there.
# `max` bounds a firmware that never parks.
COLD = dict(chunk=10_000_000, max=600_000_000, idle_fraction=4)

# The recipe's parameters. min_lit 1200 separates the main UI from the small
# +Drive progress pages (a frame at or under it is an overlay); introboot's
# CLI default of 500 is kept for the CLI only.
INTRO = dict(min_lit=1200, budget=600_000_000, chunk=20_000_000)
SETTLE = dict(min_lit=1200, quiet=120, budget=3_000_000_000,
              chunk=50_000_000)

# boot_to_ui: the PIT3 vector slot and PIT3 control register (hardware), and
# the fallbacks used only when the image's profile cannot supply a symbol.
# These are Digitakt mk1 OS 1.53 addresses and every one of them moves when
# the image is relinked; the profile supplies all of them for 1.53.
PIT3_VECTOR_SLOT = 0x40000340
PIT3_BASE = 0xFC08C000
INTRO_ISR = 0x4006C154
INTRO_DONE = 0x4006CB94
FRAME_SEM = 0x41988BE4
MAINLOOP_DEFAULT = 0x4000B6E4
MAIN_LOAD = 0x40000400
GUI_FLAGS = dict(unblock=True, softfloat=True, bitmap=True, dsp=True)


@dataclass
class Event:
    """One progress report. `text` is the line the CLI tools print.

    step is one of STEPS, kind one of KINDS. done/total are the stage's own
    units (instructions for the emulator stages); total 0 means unknown.
    data carries plain JSON values: 'fraction' (this stage, 0..1),
    'overall' (the whole first run, added by first_run), and per-stage
    figures such as frames, overlays and quiet_run.
    """
    step: str
    kind: str
    done: int = 0
    total: int = 0
    text: str = ''
    data: dict = field(default_factory=dict)


class Cancelled(Exception):
    """Raised when the cancel callback asked the run to stop."""


class StepFailed(RuntimeError):
    """A stage could not produce its output. `.step` and `.reason` say why."""

    def __init__(self, step, reason):
        super().__init__('%s: %s' % (step, reason))
        self.step = step
        self.reason = reason


@dataclass
class FirmwarePaths:
    """Where everything for one firmware release lives, under `root`.

    All absolute, so env() can hand them to the emulator modules, which
    resolve relative paths against the current directory.
    """
    root: str
    syx_name: str
    devices_dir: str

    def __post_init__(self):
        self.root = os.path.abspath(self.root)
        self.devices_dir = os.path.abspath(self.devices_dir)

    @property
    def syx(self):
        return os.path.join(self.root, self.syx_name)

    @property
    def sections(self):
        return os.path.join(self.root, 'sections')

    @property
    def main_img(self):
        return os.path.join(self.sections, 'section_3_MAIN_OS.bin')

    @property
    def snapshots(self):
        return os.path.join(self.root, 'snapshots')

    @property
    def snapdir(self):
        # snapshots/<syx stem>, as emu/run.py's paths_for lays it out.
        return os.path.join(self.snapshots,
                            os.path.splitext(self.syx_name)[0])

    @property
    def prefix(self):
        return os.path.join(self.snapdir, 'boot')

    def rung(self, at):
        """-> the ladder snapshot taken at `at` instructions."""
        return '%s%dM.snap' % (self.prefix, at // 1_000_000)

    @property
    def cold(self):
        """-> the cold boot's snapshot, taken where the firmware parks."""
        return os.path.join(self.snapdir, 'boot.snap')

    @property
    def card(self):
        return os.path.join(self.root, 'plusdrive.img')

    @property
    def gui_raw(self):
        return os.path.join(self.snapdir, 'gui-raw.snap')

    @property
    def gui(self):
        return os.path.join(self.snapdir, 'gui.snap')

    @property
    def resume(self):
        return os.path.join(self.snapdir, 'resume.snap')

    @property
    def overlay(self):
        return os.path.join(self.root, 'devices')

    @property
    def logs(self):
        return os.path.join(self.root, 'logs')

    @property
    def state(self):
        return os.path.join(self.root, 'firmware.json')

    def env(self):
        """-> the DT2_* variables every stage and the panel must run under."""
        devices = self.overlay if os.path.isdir(self.overlay) \
            else self.devices_dir
        return {
            'DT2_SYX': self.syx,
            'DT2_SECTIONS': self.sections,
            'DT2_SNAPSHOTS': self.snapshots,
            'DT2_PLUSDRIVE': self.card,
            'DT2_MAIN_IMG': self.main_img,
            'DT2_DEVICES': devices,
        }


# ------------------------------------------------------------------- state
def replace_retry(src, dst, tries=20, delay=0.1):
    """os.replace(src, dst), retried while Windows refuses it.

    Windows will not replace a file another process has open without
    delete sharing -- and Python's open() never grants it -- so a launcher
    listing folders, a second launcher, or an antivirus scan reading
    firmware.json at the wrong moment made the atomic rename fail with
    PermissionError (WinError 5 or 32), after a long stage had
    finished. Such holds last milliseconds; retry for about `tries * delay`
    seconds, then let the last error out. Any other error is raised at once.
    """
    for attempt in range(max(1, tries)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= tries - 1:
                raise
            time.sleep(delay)


def read_state(paths):
    """-> firmware.json as a dict; {} when it is missing or unreadable."""
    try:
        with open(paths.state, encoding='utf-8') as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(paths, state):
    """Replace firmware.json atomically: write <file>.tmp, then rename."""
    os.makedirs(paths.root, exist_ok=True)
    tmp = paths.state + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.write('\n')
        fh.flush()
        os.fsync(fh.fileno())
    replace_retry(tmp, paths.state)


def _update_state(paths, fn):
    """Read, let `fn` change the dict in place, write. -> the new state.

    Re-read every time: other keys ('release', 'resume', 'app_version')
    belong to the caller and must survive this module's writes.
    """
    state = read_state(paths)
    fn(state)
    write_state(paths, state)
    return state


def card_stamp(path):
    """-> {'size', 'mtime_ns'} of the card file, or None if it is missing.

    Relies on Card.flush stamping the mtime explicitly: writes through a
    memory map do not move it on Windows.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


def _resume_card(state):
    """state['resume'] may be the stamp itself or {'card': stamp, ...}."""
    res = state.get('resume')
    if isinstance(res, dict) and isinstance(res.get('card'), dict):
        return res['card']
    return res if isinstance(res, dict) else None


def choose_snapshot(paths):
    """-> (path, reason) to open the panel on, or (None, reason).

    resume.snap when it exists and was saved against the card as it is now;
    else gui.snap when the settle stamp is current and the card has not
    changed since the settle; else (None, 'card-changed') when a settled
    build exists but the card moved on without a matching resume.snap -- the
    firmware's RAM would disagree with its own card, so rebuild instead of
    resuming a stale cache -- or (None, 'not-built').
    """
    state = read_state(paths)
    now = card_stamp(paths.card)
    settle = (state.get('stages') or {}).get('settle')
    built = (isinstance(settle, dict) and os.path.exists(paths.gui)
             and settle.get('build') == _build_id()
             and settle.get('inputs', {}).get('syx_sha256')
             == _sha256_or_none(paths.syx))
    if not built:
        return None, 'not-built'
    if now is not None and os.path.exists(paths.resume) \
            and _resume_card(state) == now:
        return paths.resume, 'resume'
    if now is not None and settle.get('card_after') == now:
        return paths.gui, 'gui'
    return None, 'card-changed'


# ----------------------------------------------------------------- helpers
def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _sha256_or_none(path):
    try:
        return _sha256(path)
    except OSError:
        return None


def _build_id():
    """What, besides RECIPE, decides whether an old snapshot still opens.

    From emu.checkpointver, not emu.snapshot: choose_snapshot runs in the
    launcher for every folder, and emu.snapshot imports Unicorn.
    """
    from emu.checkpointver import CHECKPOINT_VERSION
    return {'recipe': RECIPE, 'checkpoint_version': CHECKPOINT_VERSION}


def _canon(value):
    """Round-trip through JSON, so a fresh stamp compares equal to a stored
    one (tuples come back as lists)."""
    return json.loads(json.dumps(value))


def _now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def _describe(exc):
    text = str(exc).strip()
    return text if text else type(exc).__name__


def _emitter(progress, step):
    """-> emit(kind, text='', done=0, total=0, **data), a no-op without one."""
    def emit(kind, text='', done=0, total=0, **data):
        if progress is not None:
            progress(Event(step, kind, int(done), int(total), text, data))
    return emit


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise Cancelled()


def _close_card(ev_or_machine):
    """Close the eSDHC card reachable from a longrun ev dict or a Machine."""
    if ev_or_machine is None:
        return
    if isinstance(ev_or_machine, dict):
        model = ev_or_machine.get('esdhc')
    else:
        model = getattr(ev_or_machine, 'esdhc', None)
    card = getattr(model, 'card', None)
    if card is not None and hasattr(card, 'close'):
        card.close()


def _save_atomic(m, path, extra, ev):
    """snapshot.save to path.tmp, then rename over path."""
    from emu.snapshot import save
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    try:
        save(m, tmp, extra=extra, components=ev['checkpoint_components'],
             manifest=ev.get('checkpoint_manifest'))
        replace_retry(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def card_state_in(snapshot):
    """-> (overlay bytes, erased ranges) the snapshot carries for the card.

    (0, 0) is what a snapshot of a file-backed card must hold; see
    Esdhc.checkpoint_state. None when it has no eSDHC component.
    """
    from emu.snapshot import _load_blob
    comp = (_load_blob(snapshot).get('components') or {}).get('esdhc')
    if not isinstance(comp, dict):
        return None
    return (len(comp.get('card_overlay') or {}),
            len(comp.get('card_erased') or []))


def _from_profile(prof):
    """-> (intro_isr, intro_done, frame_sem, mainloop), profile first.

    profile.intro_done anchors two bytes before the `pea`, because frame_sem's
    Operand rule needs it there (see emu/symbols.py). The hook wants the pea
    itself, which is intro_done + 2.
    """
    isr = getattr(prof, 'intro_pit3_isr', None) or INTRO_ISR
    done = getattr(prof, 'intro_done', None)
    done = (done + 2) if done else INTRO_DONE
    sem = getattr(prof, 'frame_sem', None) or FRAME_SEM
    main = getattr(prof, 'mainloop', None) or MAINLOOP_DEFAULT
    return isr, done, sem, main


def _u32(m, a):
    try:
        return struct.unpack('>I', m.uc.mem_read(a, 4))[0]
    except Exception:                                   # noqa: BLE001
        return None


def _u16(m, a):
    try:
        return struct.unpack('>H', m.uc.mem_read(a, 2))[0]
    except Exception:                                   # noqa: BLE001
        return None


def _task_map(ev, carried=None):
    """ev['tasks'] as the TCB-keyed map emu/tasks.py and restore_into read.

    ev['tasks'] is a list of (entry, prio, tcb) triples; dict() of the raw
    list raises, which is how uisettle crashed at save whenever a task had
    been created. `carried` (st['task_create_hits'] after a restore of a
    snapshot that already had a TCB-keyed map) is merged in, as emu/gui.py
    does when it saves a session.
    """
    tasks = {'%#010x' % tcb: info for tcb, info in (carried or {}).items()}
    tasks.update(('%#010x' % tcb, {'entry': entry, 'prio': prio, 'tcb': tcb})
                 for entry, prio, tcb in ev.get('tasks', []))
    return tasks


# -------------------------------------------------------------- frame count
class FrameCounter:
    """Untorn frames counted as they are drawn; only the last one is kept.

    A frame with more than `min_lit` lit pixels is the main UI, anything at
    or under it one of the +Drive progress overlays. `run` is the number of
    main-UI frames since the last overlay. Nothing is stored per frame, so
    there is no cap and the count keeps moving however long first boot takes.
    """

    def __init__(self, min_lit):
        self.min_lit = min_lit
        self.frames = 0
        self.overlays = 0
        self.run = 0
        self.last = None
        self.last_lit = 0

    @staticmethod
    def lit(buf):
        # Every bit of the 1024-byte panel buffer is exactly one pixel (see
        # emu/panel.py's layout), so this equals len(panel.lit(buf)) without
        # building a set of 8192 coordinates per frame.
        return int.from_bytes(buf, 'big').bit_count()

    def add(self, buf):
        n = self.lit(buf)
        self.frames += 1
        if n <= self.min_lit:
            self.overlays += 1
            self.run = 0
        else:
            self.run += 1
        self.last = buf
        self.last_lit = n
        return n


# -------------------------------------------------------------- boot_to_ui
@dataclass
class IntroResult:
    up: bool
    total: int
    lit: int
    mainloop: int
    intro_done: int
    flipped_at: object
    saved: object
    png: object


def boot_to_ui(syx, rung, out=None, progress=None, cancel=None, *,
               min_lit=1200, budget=600_000_000, chunk=20_000_000, png=None,
               intro_channels=(3,), except_frame_sem=True, step='intro',
               fast=False):
    """Boot from a ladder rung to a live UI; save it to `out` once live.

    fast=True steps with longrun's block-bounded fast stepper, as the panel
    does, instead of exact counted stepping: ~3x faster, and a timer fires
    up to one block late, so the instruction stream (not the outcome)
    differs from an exact run. The first run uses it; the CLI keeps exact.

    -> IntroResult. `up` is the verdict: the main loop has run more than 100
    times and the front buffer has more than `min_lit` pixels lit. Nothing
    is saved unless it is up. The snapshot is written to out.tmp and renamed
    into place, and only then is a PNG written, and only if `png` names one.

    The intro policy (tools/introboot.py explains it): PIT3 on
    `intro_channels` paces the intro, PIT0/PIT2 and the DMA timers are held
    until the intro's exit sequence starts, and then the full timer set is
    released. `except_frame_sem` leaves the intro's frame semaphore to PIT3
    rather than letting unblock satisfy it -- the mk1 policy. It becomes
    unblock_except, which is part of the checkpoint manifest, so it must be
    what the panel will build: first_run derives it from the device file
    exactly as emu/gui.py does.

    Raises StepFailed if the emulator stops (any spin stop but 'limit'),
    Cancelled if `cancel()` turns true between chunks.
    """
    from emu import config, longrun, panel, symbols
    from emu.dtim import Dtims, Timers
    from emu.pit import Pits

    emit = _emitter(progress, step)
    with open(config.main_image(), 'rb') as fh:
        img = fh.read()
    prof = symbols.resolve(img, load_addr=MAIN_LOAD)
    intro_isr, intro_done, frame_sem, mainloop = _from_profile(prof)
    emit('note', 'symbols: intro_pit3_isr=0x%08X intro_done_hook=0x%08X '
         'frame_sem=0x%08X mainloop=0x%08X'
         % (intro_isr, intro_done, frame_sem, mainloop))

    except_sems = (frame_sem,) if except_frame_sem else ()
    # Always the GUI's flag set: this exists to produce gui.snap, and
    # unblock_except is part of the checkpoint manifest, so a snapshot built
    # under one policy will not reopen under the other.
    m, ev, st, pc, inq, at = longrun.build(
        rung, syx=syx, unblock=True, softfloat=True, bitmap=True, dsp=True,
        unblock_except=except_sems, deferred_components=('timers',))
    try:
        # PIT3 paces the intro; PIT0/PIT2 and the DMA timers stay held until
        # the intro is over, because PIT2 during the intro stops the OS tasks
        # spawning at all (emu/pit.py).
        pit = Pits(m, channels=tuple(intro_channels), hold=False)
        dtim = Dtims(m, channels=(1, 3), hold=True)
        pits = Timers(pit, dtim)

        hits = {'mainloop': 0, 'intro_done': 0}
        at(mainloop,
           lambda *_: hits.__setitem__('mainloop', hits['mainloop'] + 1))
        skip = ev.get('unblock_skip')

        def handover(*_a):
            if hits['intro_done']:
                return
            hits['intro_done'] += 1
            # Stop faking the frame semaphore from here on even if it was
            # being faked, then give the OS the full timer set it expects.
            if skip is not None:
                skip.add(frame_sem)
            pit.channels = (3, 2, 0)
            pits.release()

        at(intro_done, handover)

        emit('note', 'resume %s  except_sem=%s'
             % (os.path.basename(rung), [hex(s) for s in except_sems]))
        emit('note', '%6s %-8s %-8s %-12s %-9s %s'
             % ('instr', 'PCSR', 'lit', 'vector', 'mainloop', 'phase'))

        flipped = None
        total = 0
        lit = -1
        while total < budget:
            _check_cancel(cancel)
            pc, executed, stop = longrun.spin(m, pc, chunk, pits=pits,
                                              fast=fast)
            if stop != 'limit':
                raise StepFailed(step, 'the emulator stopped after %dM '
                                 'instructions: %s'
                                 % ((total + executed) // 1_000_000, stop))
            total += chunk
            slot = _u32(m, PIT3_VECTOR_SLOT)
            buf = panel.read(m, prof.fb_front)
            lit = len(panel.lit(buf)) if buf else -1
            if flipped is None and slot not in (0, intro_isr):
                flipped = total
                emit('note', '  >> display module claimed vector 208 at %dM'
                     % (total // 1_000_000))
            phase = ('OS' if (slot and slot != intro_isr)
                     else ('post-intro' if hits['intro_done'] else 'intro'))
            emit('tick', '%5dM 0x%04x   %-8d 0x%08x   %-9d %s'
                 % (total // 1_000_000, _u16(m, PIT3_BASE) or 0, lit,
                    slot or 0, hits['mainloop'], phase),
                 done=total, total=budget, lit=lit,
                 mainloop=hits['mainloop'], phase=phase,
                 fraction=min(total / INTRO_EXPECTED, 0.99))
            if hits['mainloop'] > 100 and lit > min_lit:
                emit('note', '  -> user interface is live')
                break

        emit('note', '\nintro_done fired %d time(s); mainloop %d; lit %d'
             % (hits['intro_done'], hits['mainloop'], lit))
        up = hits['mainloop'] > 100 and lit > min_lit
        emit('note', 'VERDICT: %s' % ('USER INTERFACE IS LIVE' if up else
                                      'did not reach a live UI'))

        saved = None
        if out and up:
            ev['claim_checkpoint_component']('timers', pits)
            # ev['tasks'] is a list of (entry, prio, tcb) triples; emu/tasks.py
            # wants a map keyed by TCB. (The rung's own map is keyed by
            # task-create SITE, so it is not merged in here.)
            _save_atomic(m, out, {'n': total, 'tasks': _task_map(ev)}, ev)
            saved = out
            emit('note', 'saved %s' % out, path=out)
        elif out:
            emit('note', 'not saving: the UI never came up')

        written = None
        buf = panel.read(m, prof.fb_front)
        if png and buf:
            panel.write_png(buf, png, scale=6)
            written = png
            emit('note', 'screen -> %s' % png, path=png)
        return IntroResult(up=up, total=total, lit=lit,
                           mainloop=hits['mainloop'],
                           intro_done=hits['intro_done'], flipped_at=flipped,
                           saved=saved, png=written)
    finally:
        _close_card(ev)


# --------------------------------------------------------------- settle_ui
@dataclass
class SettleResult:
    settled_at: object
    total: int
    frames: int
    overlays: int
    quiet_run: int
    saved: object
    png: object


def settle_ui(syx, snap, out=None, progress=None, cancel=None, *,
              min_lit=1200, quiet=120, budget=3_000_000_000,
              chunk=50_000_000, png=None, except_frame_sem=True,
              step='settle', expected=SETTLE_EXPECTED, fast=False,
              fast_idle=False):
    """Run on until the UI has SETTLED, then save it to `out`.

    fast=True uses the fast stepper (see boot_to_ui) and fast_idle=True lets
    it treat an idle spin as a wait for the next timer deadline, as the
    panel does -- together ~9x faster than exact stepping, with the same
    settle point and frame counts on mk1 1.53. fast_idle is not part of the
    checkpoint manifest, so the snapshot opens the same either way. (It must
    NOT be used for the intro: skipping the intro's idle waits keeps its
    main loop from ever running.)

    -> SettleResult; settled_at is None if it did not settle in `budget`.
    tools/uisettle.py explains why the first live frame is the wrong moment:
    on first boot the firmware interleaves its +Drive progress pages with the
    main page for a long while after that. Settled = the last `quiet` untorn
    frames are all main-UI frames, checked at the end of each `chunk`.

    Frames are counted as they arrive by a hook on the panel diff's entry
    (the only untorn moment; see emu/panel.py), with no cap. The snapshot is
    saved to out.tmp and renamed into place BEFORE any PNG is written, and a
    PNG is written only if `png` names one.

    Raises StepFailed if the emulator stops or the image has no panel_diff
    symbol, Cancelled if `cancel()` turns true (checked on every frame).
    """
    from emu import config, longrun, panel, symbols
    from emu.uiresume import open_snapshot

    emit = _emitter(progress, step)
    with open(config.main_image(), 'rb') as fh:
        img = fh.read()
    prof = symbols.resolve(img, load_addr=MAIN_LOAD)
    if prof.panel_diff is None or prof.fb_front is None:
        raise StepFailed(step, 'this image has no resolved panel_diff/'
                         'fb_front, so its frames cannot be counted')

    flags = dict(GUI_FLAGS)
    if except_frame_sem and prof.frame_sem is not None:
        flags['unblock_except'] = (prof.frame_sem,)
    if fast_idle:
        flags['fast_idle'] = True
    m, ev, st, pc, inq, at, pits = open_snapshot(
        snap, syx, prof, verbose=False, **flags)
    try:
        restored = ev['checkpoint_components'].get('timers') is pits
        emit('note', '  timers: restored saved cadence' if restored
             else '  timers: fresh (cold-ladder snapshot)')

        hits = {'job_pump': 0, 'mainloop': 0}
        for name in ('job_pump', 'mainloop'):
            addr = getattr(prof, name, None)
            if addr:
                at(addr, (lambda n: (lambda *_: hits.__setitem__(
                    n, hits[n] + 1)))(name))

        count = FrameCounter(min_lit)
        front = prof.fb_front
        start = pits.now
        clock = {'tick': time.monotonic()}

        def fraction(done):
            return min(done / expected, 0.99) if expected else 0.0

        def grab(uc, addr, size, data):
            buf = panel.read(uc, front)
            if buf is None:
                return
            count.add(buf)
            if cancel is not None and cancel():
                raise Cancelled()
            now = time.monotonic()
            if progress is not None and now - clock['tick'] >= 1.0:
                # A silent tick (no text) between chunk rows, so a progress
                # bar moves during the slow second half of first boot, where
                # one 50M chunk takes ~20 s.
                clock['tick'] = now
                done = pits.now - start
                emit('tick', done=done,
                     total=expected if done < expected else 0,
                     frames=count.frames, overlays=count.overlays,
                     quiet_run=count.run, fraction=fraction(done),
                     indeterminate=done >= expected)

        at(prof.panel_diff, grab)

        emit('note', '%6s %-8s %-9s %-10s %-9s %s'
             % ('instr', 'frames', 'overlays', 'quiet-run', 'jobs', 'state'))
        total = 0
        settled = None
        while total < budget:
            _check_cancel(cancel)
            pc, executed, stop = longrun.spin(m, pc, chunk, pits=pits,
                                              fast=fast)
            if stop != 'limit':
                raise StepFailed(step, 'the emulator stopped after %dM '
                                 'instructions: %s'
                                 % ((total + executed) // 1_000_000, stop))
            total += chunk
            done = count.run >= quiet
            emit('tick', '%5dM %-8d %-9d %-10d %-9d %s'
                 % (total // 1_000_000, count.frames, count.overlays,
                    count.run, hits['job_pump'],
                    'settled' if done else 'first-boot work'),
                 done=total, total=expected if total < expected else 0,
                 frames=count.frames, overlays=count.overlays,
                 quiet_run=count.run, jobs=hits['job_pump'],
                 fraction=fraction(total), indeterminate=total >= expected)
            if done:
                settled = total
                break

        if settled is None:
            emit('note', '\nNOT SETTLED after %dM instructions -- the +Drive '
                 'overlay is still being drawn.' % (budget // 1_000_000))
        else:
            emit('note', '\nSETTLED at %dM: %d consecutive main-UI frames '
                 'with no overlay.' % (settled // 1_000_000, quiet))

        saved = None
        if out and settled is not None:
            components = ev['checkpoint_components']
            if components.get('timers') is not pits:
                ev['claim_checkpoint_component']('timers', pits)
            tasks = _task_map(ev, st.get('task_create_hits'))
            _save_atomic(m, out, {'n': total, 'tasks': tasks}, ev)
            saved = out
            emit('note', 'saved %s' % out, path=out)
        elif out:
            emit('note', 'not saving: the UI never settled, so this snapshot '
                 'would have the same fault as the one it replaces')

        written = None
        if png and count.last:
            panel.write_png(count.last, png, scale=6)
            written = png
            emit('note', 'screen (%d lit) -> %s' % (count.last_lit, png),
                 path=png)
        return SettleResult(settled_at=settled, total=total,
                            frames=count.frames, overlays=count.overlays,
                            quiet_run=count.run, saved=saved, png=written)
    finally:
        _close_card(ev)


# -------------------------------------------------------------- the stages
def sections_current(paths, syx_sha):
    """True when sections/ came from this .syx and holds one MAIN OS image."""
    try:
        with open(os.path.join(paths.sections, '.source-sha256'),
                  encoding='utf-8') as fh:
            recorded = fh.read().strip()
    except OSError:
        return False
    found = glob.glob(os.path.join(glob.escape(paths.sections),
                                   '*MAIN_OS*.bin'))
    return (recorded == syx_sha and len(found) == 1
            and os.path.normcase(found[0]) == os.path.normcase(paths.main_img))


def extract_sections(paths, progress=None):
    """Decompress the .syx into sections/, via sections.tmp and a rename."""
    from emu import extract
    emit = _emitter(progress, 'extract')
    tmp = paths.sections + '.tmp'
    shutil.rmtree(tmp, ignore_errors=True)

    def started(sid, kind, name):
        emit('tick', '  %-26s %s' % (name, kind), done=sid, section=name)

    extract.extract(paths.syx, tmp, progress=started)
    if os.path.isdir(paths.sections):
        shutil.rmtree(paths.sections)
    replace_retry(tmp, paths.sections)


def prepare_card(path, base=None, progress=None):
    """Create and format the +Drive image unless it already holds an ekFS.

    -> True if it formatted one. A card that mounts (valid ekFS superblock)
    is left alone whatever else it holds: it may carry the user's projects
    and samples. Anything else -- missing, the 512-byte blank an earlier
    emulator run creates, a torn format -- is replaced by a fresh format,
    built in path.tmp and renamed into place. This is exactly what
    `tools/ekfsadd.py IMAGE --format` does, the recipe the settle was proven
    on.
    """
    from emu import ekfsformat
    base = ekfsformat.REGION if base is None else base
    emit = _emitter(progress, 'card')
    if ekfsformat.is_formatted(path, base):
        return False
    tmp = path + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    emit('tick', 'formatting %s' % path)
    ekfsformat.format_image(tmp, base)
    if not ekfsformat.is_formatted(tmp, base):
        raise StepFailed('card', 'the freshly formatted card does not verify')
    replace_retry(tmp, path)
    return True


def blank_card(path):
    """Create an empty +Drive image, for a product whose first boot lays
    down everything it needs itself (no sample volume to pre-format). The
    same sparse one-sector file emu/esdhc.py's Card makes for a missing
    path; reads past its end are zeros. -> True."""
    from emu import sparse
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fh:
        sparse.make_sparse(fh)
        sparse.extend(fh, 512)
    replace_retry(tmp, path)
    return True


# ------------------------------------------------------ first boot's card
# What the firmware's first-boot job leaves on the card (the +Drive first-
# boot investigation of 2026-09-23, summarised in
# docs/history/HANDOFF-2026-09-23.md; and emu/ekfsformat.py for the ekFS).
# "INITIALIZING +DRIVE" erases the project and sound regions and writes
# BEEFBACE to sector 0; "FACTORY PROJECT/SOUNDS >> +DRIVE" then unpacks the
# factory project into slot 0 and 256 x 1 KB of sounds, and marks them
# protected in the sector-0x800 record. These are properties of the card
# format, not of one firmware build, so they are checked for every release.
BEEFBACE = b'\xbe\xef\xba\xce'
CARD_RECORD = 0x800 * 512            # protect bitmap; bit 0 of byte 0 = project 0
CARD_RECORD_LEN = 0x110              # what INITIALIZING +DRIVE clears
CARD_SOUNDS = (0x1000 * 512, 0x1200 * 512)       # 0x200000..0x23FFFF
CARD_PROJECT0 = 0x80000 * 512        # project slot 0, 0x10000000

# The [firmware.acceptance] keys a device file may give, each a guest
# address of a u32 in RAM after first boot; see validate_acceptance.
# dsp_running_u32 is not written in device files: _intro_policy adds it from
# the image (symbols.dsp_status) on a Digitone, whose second CPU has to have
# come up (2) -- see emu/dsplink.py.
ACCEPTANCE_KEYS = ('error_u32', 'progress_done', 'progress_total',
                   'mounted_u32', 'dsp_running_u32')
DSP_RUNNING = 2


def _card_problems(path, ekfs=True):
    """-> what is missing from a card that finished first boot ([] = none)."""
    from emu import ekfsformat
    problems = []
    try:
        with open(path, 'rb') as fh:
            def at(offset, n):
                fh.seek(offset)
                return fh.read(n)
            head = at(0, 8)
            record = at(CARD_RECORD, 1)
            project = at(CARD_PROJECT0, 4)
            lo, hi = CARD_SOUNDS
            sounds = at(lo, hi - lo)
    except OSError as exc:
        return ['the card cannot be read: %s' % _describe(exc)]
    if head != BEEFBACE + bytes(4):
        problems.append('sector 0 holds %s, not be ef ba ce 00 00 00 00: '
                        'the +Drive was never initialised'
                        % (head.hex(' ') or 'nothing'))
    # Byte 0 holds the protect bits of projects 0-7; only project 0's is
    # the factory install's (the user may protect 1-7 as well).
    if not record or not record[0] & 0x01:
        problems.append('the sector-0x800 record starts %s, not xxxxxxx1: '
                        'the factory project was not marked installed'
                        % (record.hex() or 'past the end of the card'))
    if project != BEEFBACE:
        problems.append('project slot 0 (byte 0x%x) does not start be ef ba '
                        'ce: no factory project' % CARD_PROJECT0)
    if not sounds.strip(b'\0'):
        problems.append('the factory sound slots 0x%x..0x%x are empty'
                        % (CARD_SOUNDS[0], CARD_SOUNDS[1] - 1))
    if ekfs and not ekfsformat.is_formatted(path):
        problems.append('no valid ekFS superblock at sector 0x%x: the sample '
                        'volume would not mount' % ekfsformat.REGION)
    return problems


def check_card(path, ekfs=True):
    """-> the reasons `path` is not a card that finished first boot.

    [] when sector 0 is BE EF BA CE 00 00 00 00, bit 0 of the sector-0x800
    record (project 0 protected) is set, project slot 0 starts BE EF BA CE,
    the sound slots are not all zero and the ekFS superblock verifies. None
    of this depends on the firmware version. `ekfs` False drops the last
    check, for a product with no sample volume (the Digitone): its first
    boot leaves the same sector 0, record, project slot and sounds.
    """
    return _card_problems(path, ekfs=ekfs)


def check_initialised_card(path, ekfs=True):
    """-> the reasons `path` is not an initialised +Drive ([] = none).

    For a settle whose cold boot found the card already initialised (sector
    0 BEEFBACE), so first boot did not run in this chain: the firmware
    installed nothing, and what the user has done since -- unprotected or
    overwritten project 0, deleted the factory sounds, FORMAT +DRIVE -- is
    theirs. Only sector 0 and a mountable sample volume (where the product
    has one: `ekfs`) are required.
    """
    from emu import ekfsformat
    try:
        with open(path, 'rb') as fh:
            head = fh.read(8)
    except OSError as exc:
        return ['the card cannot be read: %s' % _describe(exc)]
    problems = []
    if head[:4] != BEEFBACE:
        problems.append('sector 0 holds %s, not be ef ba ce: the +Drive is '
                        'not initialised' % (head.hex(' ') or 'nothing'))
    if ekfs and not ekfsformat.is_formatted(path):
        problems.append('no valid ekFS superblock at sector 0x%x: the sample '
                        'volume would not mount' % ekfsformat.REGION)
    return problems


def _sector0(path):
    """-> the first 8 bytes of the card (b'' if it cannot be read)."""
    try:
        with open(path, 'rb') as fh:
            return fh.read(8)
    except OSError:
        return b''


def _half_installed(path):
    """-> True if `path` holds exactly what an unfinished first boot leaves.

    INITIALIZING +DRIVE writes BEEFBACE to sector 0 and clears the sector-
    0x800 record, project slot 0 and the sound region; FACTORY PROJECT/
    SOUNDS then fill them. So a card with BEEFBACE and all three still blank
    stopped in between. Anything in any of them -- the factory install, or
    the user's own projects and sounds -- means first boot finished."""
    try:
        with open(path, 'rb') as fh:
            if fh.read(4) != BEEFBACE:
                return False
            fh.seek(CARD_RECORD)
            record = fh.read(CARD_RECORD_LEN)
            fh.seek(CARD_PROJECT0)
            project = fh.read(4)
            lo, hi = CARD_SOUNDS
            fh.seek(lo)
            sounds = fh.read(hi - lo)
    except OSError:
        return False
    return not (record.strip(b'\0') or project.strip(b'\0')
                or sounds.strip(b'\0'))


def validate_acceptance(acceptance):
    """-> a device file's [firmware.acceptance] table, checked; {} if none.

    Raises ValueError for a key this module does not know (a typo would
    silently skip a check), an address that is not a u32, or only one of
    progress_done / progress_total.
    """
    if not acceptance:
        return {}
    table = dict(acceptance)
    unknown = sorted(set(table) - set(ACCEPTANCE_KEYS))
    if unknown:
        raise ValueError('[firmware.acceptance] has unknown check(s) %s; '
                         'known: %s' % (', '.join(unknown),
                                        ', '.join(ACCEPTANCE_KEYS)))
    for key, addr in table.items():
        if isinstance(addr, bool) or not isinstance(addr, int) \
                or not 0 <= addr <= 0xFFFFFFFC:
            raise ValueError('[firmware.acceptance] %s must be a guest '
                             'address, got %r' % (key, addr))
    if ('progress_done' in table) != ('progress_total' in table):
        raise ValueError('[firmware.acceptance] needs progress_done and '
                         'progress_total together')
    return table


def snapshot_reader(path):
    """-> read(addr, n): guest RAM as the snapshot at `path` holds it.

    None for an address in a page the snapshot never mapped. Loads (and
    validates) the snapshot once; imports emu.snapshot, so Unicorn.
    """
    import zlib
    from emu.snapshot import PAGE, _load_blob
    blob = _load_blob(path)
    mapped = set(blob['all_mapped'])
    pages = blob['pages']
    cache = {}

    def read(addr, n):
        out = bytearray()
        while n > 0:
            base = addr - addr % PAGE
            if base not in mapped:
                return None
            if base not in cache:
                comp = pages.get(base)
                cache[base] = zlib.decompress(comp) if comp else None
            off = addr - base
            k = min(n, PAGE - off)
            page = cache[base]
            out += page[off:off + k] if page is not None else bytes(k)
            addr += k
            n -= k
        return bytes(out)
    return read


def check_ram(read, acceptance):
    """-> ({key: value or None}, [problems]) for a validated acceptance table.

    error_u32 must be 0 (the first-boot job's error code: 0x0a is a failed
    +Drive initialisation, 0x1e + N a failed factory step N);
    progress_done must equal progress_total; mounted_u32 must be 1 (the ekFS
    sample volume is mounted).
    """
    values = {}
    for key, addr in sorted(acceptance.items()):
        raw = read(addr, 4)
        values[key] = (struct.unpack('>I', raw)[0]
                       if raw is not None and len(raw) == 4 else None)
    problems = ['%s (0x%08x) is not in the snapshot' % (key, acceptance[key])
                for key, value in sorted(values.items()) if value is None]
    err = values.get('error_u32')
    if err not in (None, 0):
        problems.append('the first-boot job reports error 0x%x at 0x%08x '
                        '(0x0a: +Drive initialisation failed; 0x1e+N: '
                        'factory step N failed)'
                        % (err, acceptance['error_u32']))
    done, total = values.get('progress_done'), values.get('progress_total')
    if done is not None and total is not None and done != total:
        problems.append('the first-boot job stopped at 0x%x of 0x%x'
                        % (done, total))
    mounted = values.get('mounted_u32')
    if mounted not in (None, 1):
        problems.append('the +Drive sample volume is not mounted (0x%08x = '
                        '%d)' % (acceptance['mounted_u32'], mounted))
    dsp = values.get('dsp_running_u32')
    if dsp not in (None, DSP_RUNNING):
        problems.append('the DSP did not come up (0x%08x = %d; 1 is DSP BOOT '
                        'FAILURE)' % (acceptance['dsp_running_u32'], dsp))
    return values, problems


def accept_settled(paths, acceptance=None, first_boot=True, ekfs=True):
    """Check that first boot really finished before settle is stamped.

    The settle rule only looks at pixels: 120 main-UI frames in a row. A
    first-boot job that failed also stops drawing its overlay, so that alone
    would stamp a card with no factory content. This adds a card check on
    the card as the settle's save left it and, when the device file gives an
    acceptance table for this release, check_ram on the snapshot just saved.
    `first_boot` says whether this chain's cold boot found an uninitialised
    card, i.e. whether the firmware ran its factory install: then the card
    must hold it (check_card); otherwise the card is the user's and only
    has to be initialised (check_initialised_card) -- a rebuild after the
    user changed project 0 or the sounds must not be refused for good.
    `ekfs` is the device's [card] ekfs: False for a product with no sample
    volume to check.
    -> {'card': 'ok' or 'initialised', 'ram': values or None};
    StepFailed('settle') listing every problem otherwise.
    """
    if first_boot:
        problems, verdict = check_card(paths.card, ekfs=ekfs), 'ok'
    else:
        problems, verdict = (check_initialised_card(paths.card, ekfs=ekfs),
                             'initialised')
    ram = None
    if acceptance:
        ram, bad = check_ram(snapshot_reader(paths.gui), acceptance)
        problems += bad
    if problems:
        raise StepFailed('settle', '%s: %s' % (
            'first boot did not finish' if first_boot
            else 'the +Drive did not come up',
            '; '.join(problems)))
    return {'card': verdict, 'ram': ram}


def clear_half_install(path, vouched, emit=None):
    """Undo a first boot that stopped after it wrote sector 0.

    INITIALIZING +DRIVE writes BEEFBACE to sector 0 early (the intro's save
    flushes it), and a cold boot that reads BEEFBACE there never queues the
    factory install again (state 2 in FUN_400e35a0). So a card left between
    intro and a finished settle -- an app update in between, a card changed
    before settle, a settle that failed acceptance -- would come out of a
    rebuild with no factory project or sounds, for good. Zeroing sector 0's
    first 8 bytes makes the next cold boot see an uninitialised +Drive and
    run the whole install again.

    Only when nothing says first boot finished on this card: `vouched`
    (the caller's record of a completed settle) is false AND the card holds
    exactly what an unfinished first boot leaves (_half_installed: sector 0
    set, record, project slot 0 and sounds all blank). A played card holds
    something in one of those even after the user unprotected or replaced
    the factory content, and zeroing its sector 0 would make the firmware's
    INITIALIZING +DRIVE erase the user's projects.
    -> True if sector 0 was cleared.
    """
    if vouched or not _half_installed(path):
        return False
    with open(path, 'r+b') as fh:
        fh.write(bytes(8))
        fh.flush()
        os.fsync(fh.fileno())
    if emit is not None:
        emit('note', 'the card holds a first boot that never finished '
             '(sector 0 is be ef ba ce, no completed settle); cleared sector '
             "0 so the firmware installs its factory content again")
    return True


def _set_aside(path):
    """Move a rejected output out of the way, keeping it for a look."""
    try:
        replace_retry(path, path + '.rejected')
    except OSError:
        pass


def cold_boot(paths, progress=None, cancel=None, *, chunk=None, limit=None,
              idle_fraction=None):
    """The first run's cold boot: from reset to where the firmware PARKS.

    -> {'stop', 'parked_at', 'card_writes', 'tasks', 'native'}; the snapshot
    is paths.cold. The same machine as `emu.checkpoint make`'s ladder
    (emu/dspboot.py: flash HLE, scoped ISA patches, the storage models,
    idle-spin scheduler ticks) but without its per-instruction coverage
    hook, and -- with the patched Unicorn -- free-running under the native
    block budget instead of `count=`, so Python runs only in the scoped
    hooks: ~160M instructions in about 4 s, against ~5 minutes for the
    fixed 400M ladder the first run used to build.

    It runs in chunks and stops at the first chunk that was mostly idle-spin
    passes (more than 1/idle_fraction of its blocks) with no task created:
    the firmware has come up and now waits for the intro's PIT3 tick, which
    this boot never delivers. That point is found, not fixed, so a firmware
    that parks somewhere else still stops in the right place. Instruction
    counts under the budget are longrun's blocks x PER_BLOCK estimate.
    Without the native budget it falls back to counted chunks (slower, same
    rule). The cold boot's own card writes (the mount's inode chunk) are
    then flushed into the card: every snapshot from here on has them in
    RAM, and a card without them would disagree with its own snapshots. On
    failure or cancel nothing is flushed. StepFailed if the emulator stops,
    a vector has no handler, or it never parks within `limit`.
    """
    from emu import dspboot, native
    from emu.longrun import _FastStepper
    from emu.harness import UC_M68K_REG_PC
    chunk = COLD['chunk'] if chunk is None else chunk
    limit = COLD['max'] if limit is None else limit
    idle_fraction = COLD['idle_fraction'] if idle_fraction is None \
        else idle_fraction
    emit = _emitter(progress, 'ladder')
    with open(paths.main_img, 'rb') as fh:
        img = fh.read()
    for old in glob.glob(os.path.join(glob.escape(paths.snapdir), 'boot*M.snap')):
        os.remove(old)                  # the old ladder's rungs; unused now
    box = {}
    try:
        m, st, _ = dspboot.run(paths.syx, img, limit=None, extra_hook=None,
                               fast=True, coverage=False, verbose=False,
                               machine_out=box, sdgate=True, esdhc=True)
        pc = box['start_pc']
        budget = native.NativeBudget(m.uc) if native.budget_available(m.uc) \
            else None
        per_block = _FastStepper.PER_BLOCK
        blocks = max(1, int(chunk / per_block))
        done, stop = 0, None
        while done < limit:
            _check_cancel(cancel)
            spins, tasks = st['spin'], len(st['task_create_hits'])
            m.halt_vec = None
            try:
                if budget is not None:
                    budget.state.left = blocks
                    m.uc.emu_start(pc, 0)
                    units = blocks
                else:
                    m.uc.emu_start(pc, 0, count=chunk)
                    units = chunk
            except dspboot.UcError as exc:
                raise StepFailed('ladder', 'the cold boot stopped after %dM '
                                 'instructions: %s'
                                 % (done // 1_000_000, exc)) from exc
            pc = m.uc.reg_read(UC_M68K_REG_PC)
            if m.halt_vec is not None:
                raise StepFailed('ladder', 'unhandled vector %d at %#010x '
                                 'during the cold boot' % (m.halt_vec, pc))
            done += chunk
            emit('tick', done=done, total=COLD_EXPECTED,
                 fraction=min(done / COLD_EXPECTED, 0.99))
            idle = st['spin'] - spins > units // idle_fraction
            if idle and len(st['task_create_hits']) == tasks:
                stop = 'parked'
                break
        if stop is None:
            raise StepFailed('ladder', 'the firmware never settled into its '
                             'idle loop within %dM instructions of the cold '
                             'boot' % (limit // 1_000_000))
        tasks = len(st['task_create_hits'])
        from emu.snapshot import save
        tmp = paths.cold + '.tmp'
        save(m, tmp, extra={'n': done, 'tasks': {
            hex(k): v for k, v in st['task_create_hits'].items()}})
        replace_retry(tmp, paths.cold)
        card = getattr(getattr(m, 'esdhc', None), 'card', None)
        writes = len(card.overlay) if card is not None else 0
        if card is not None and card.path:
            card.flush()
        emit('note', 'parked at ~%dM instructions, %d tasks, pc=0x%08x'
             % (done // 1_000_000, tasks, pc), parked_at=done)
        return {'stop': stop, 'parked_at': done, 'card_writes': writes,
                'tasks': tasks, 'native': budget is not None}
    finally:
        # Close, never flush, on failure: the card stays as it was.
        _close_card(box.get('m'))
        box.clear()
        for p in (paths.cold + '.tmp',):
            if os.path.exists(p):
                os.remove(p)


def _intro_policy(paths):
    """-> (device, intro_channels, except_frame_sem, acceptance), the way
    emu/gui.py decides the first three, so the saved manifest is the one the
    panel will build. `acceptance` is the release's [firmware.acceptance]
    table ({} when the device file has none), checked here so a typo in it
    fails in seconds rather than after the settle."""
    from emu import device as devices, symbols
    try:
        dev, fw = devices.identify(paths.syx)
    except devices.DeviceError as exc:
        raise StepFailed('extract', 'no device file accepts this firmware: %s'
                         % _describe(exc)) from exc
    try:
        acceptance = validate_acceptance(getattr(fw, 'acceptance', None))
    except ValueError as exc:
        raise StepFailed('extract', '%s: %s' % (dev.path, exc)) from exc
    with open(paths.main_img, 'rb') as fh:
        prof = symbols.resolve(fh.read(), load_addr=MAIN_LOAD)
    except_sem = (not dev.intro_unblocks_frame_sem
                  and prof.frame_sem is not None)
    if prof.dsp_status is not None and 'dsp_running_u32' not in acceptance:
        # A Digitone: its second CPU must have come up (emu/dsplink.py).
        acceptance = dict(acceptance, dsp_running_u32=prof.dsp_status)
    return dev, tuple(dev.intro_channels), except_sem, acceptance


def _check_env(paths):
    want = paths.env()
    wrong = []
    for key, value in sorted(want.items()):
        cur = os.environ.get(key)
        if not cur or os.path.normcase(os.path.abspath(cur)) \
                != os.path.normcase(value):
            wrong.append('%s=%r (want %r)' % (key, cur, value))
    if wrong:
        raise ValueError('first_run needs os.environ to hold paths.env(); '
                         'wrong: ' + '; '.join(wrong))


class _Overall:
    """Adds data['overall'], the whole first run's 0..1, to every Event."""

    def __init__(self, progress):
        self.progress = progress
        self.base = 0.0
        self.weight = 0.0
        self.frac = 0.0

    def enter(self, step):
        self.base = sum(WEIGHTS[s] for s in STEPS[:STEPS.index(step)]
                        if s in WEIGHTS)
        self.weight = WEIGHTS.get(step, 0.0)
        self.frac = 0.0

    def __call__(self, event):
        if self.progress is None:
            return
        # An Event without a fraction (a printed note) keeps the stage's
        # last one, so the bar never steps backwards.
        if event.kind == 'done':
            self.frac = 1.0
        elif event.data.get('fraction') is not None:
            self.frac = max(self.frac, event.data['fraction'])
        event.data['overall'] = round(self.base + self.weight * self.frac, 4)
        self.progress(event)


def _stage_ok(stages, name, inputs, outputs):
    stamp = stages.get(name)
    return (isinstance(stamp, dict) and stamp.get('inputs') == inputs
            and stamp.get('build') == _build_id()
            and all(os.path.exists(p) for p in outputs))


def first_run(paths, progress=None, cancel=None):
    """Run every stage that does not verify; -> the gui.snap path.

    Must be called with os.environ already holding paths.env() (checked).
    The caller has copied the .syx into paths.root. Stages that verify --
    stamp in firmware.json's 'stages' matches the current inputs and the
    outputs exist -- are skipped; see the module docstring for the card rule
    that chains ladder, intro and settle. When choose_snapshot already finds
    a snapshot for the card as it is now (gui.snap, or a session's
    resume.snap), ladder, intro and settle are all up to date and resume.snap
    and state['resume'] are kept. Each emulator stage runs its own Machine,
    which is closed (card) and collected before the next starts. A settle is
    stamped only if accept_settled passes; then state['card'] is the card as
    the settle left it and state['first_boot'] records that first boot
    finished on this card.

    Raises StepFailed (with .step) when a stage cannot finish, Cancelled when
    `cancel()` turns true or `progress` raises it.
    """
    _check_env(paths)
    overall = _Overall(progress)
    for d in (paths.root, paths.snapdir, paths.logs):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(paths.syx):
        raise StepFailed('extract', 'firmware file missing: %s' % paths.syx)
    syx_sha = _sha256(paths.syx)

    def stage(name, fn):
        """Run fn() as stage `name`: start/done Events, errors -> StepFailed."""
        _check_cancel(cancel)
        overall.enter(name)
        emit = _emitter(overall, name)
        emit('start', '%s ...' % name)
        t0 = time.monotonic()
        try:
            result = fn(overall)
        except (Cancelled, StepFailed, KeyboardInterrupt):
            raise
        except (Exception, SystemExit) as exc:          # noqa: BLE001
            # config.NotFound and device.DeviceError are SystemExit
            # subclasses; neither may end the worker without a reason.
            raise StepFailed(name, _describe(exc)) from exc
        finally:
            gc.collect()
        seconds = round(time.monotonic() - t0, 1)
        emit('done', '%s done in %.0f s' % (name, seconds), seconds=seconds)
        return result, seconds

    def skipped(name, why):
        overall.enter(name)
        _emitter(overall, name)('done', '%s: %s' % (name, why), skipped=True)

    def record(name, stamp, drop_after=True, also=None):
        stamp = dict(stamp, at=_now(), build=_build_id())

        def change(state):
            stages = state.setdefault('stages', {})
            if drop_after:
                for later in STEPS[STEPS.index(name) + 1:]:
                    stages.pop(later, None)
            stages[name] = _canon(stamp)
            if also is not None:
                also(state)
        try:
            return _update_state(paths, change)['stages']
        except OSError as exc:
            # Outside stage(): without this a stage that finished would end
            # the worker with a bare OSError that names no step.
            raise StepFailed(name, 'finished, but could not be recorded in '
                             '%s: %s' % (paths.state, _describe(exc))) from exc

    stages = dict(read_state(paths).get('stages') or {})
    if isinstance(stages.get('settle'), dict) \
            and not read_state(paths).get('first_boot'):
        # A folder settled before the first_boot marker existed: record now,
        # while the settle stamp is still here to vouch, that first boot
        # finished on this card. A later rebuild drops the stamp, and without
        # the marker the sector-0 guard would have only the card to go on.
        _update_state(paths, lambda s: s.setdefault(
            'first_boot', {'at': _now(), 'syx_sha256': syx_sha,
                           'backfilled': True}))

    # extract ------------------------------------------------------------
    want = _canon({'recipe': RECIPE, 'syx_sha256': syx_sha})
    if (sections_current(paths, syx_sha)
            and _stage_ok(stages, 'extract', want, [paths.main_img])):
        skipped('extract', 'sections up to date')
    else:
        _r, secs = stage('extract', lambda p: extract_sections(paths, p))
        if not sections_current(paths, syx_sha):
            raise StepFailed('extract', 'no single MAIN OS image in %s'
                             % paths.sections)
        # Nothing downstream is dropped: identical sections leave the
        # ladder's inputs (both hashes) unchanged, and changed ones fail them.
        stages = record('extract', {'inputs': want, 'seconds': secs},
                        drop_after=False)
    main_sha = _sha256(paths.main_img)

    # Settle the device and its intro policy before the long stages: an
    # unknown firmware fails here, not after the cold boot.
    try:
        _dev, channels, except_sem, acceptance = _intro_policy(paths)
    except StepFailed:
        raise
    except (Exception, SystemExit) as exc:              # noqa: BLE001
        raise StepFailed('extract', _describe(exc)) from exc

    # card ---------------------------------------------------------------
    # A product with no sample volume (the Digitone: [card] ekfs = false)
    # gets a blank card, and its own first boot initialises it.
    from emu import ekfsformat
    ekfs = getattr(_dev, 'card_ekfs', True)
    if not ekfs:
        if os.path.exists(paths.card):
            if 'card' not in stages:
                stages = record('card', {'inputs': {'recipe': RECIPE},
                                         'formatted': False}, drop_after=False)
            skipped('card', 'the card exists')
        else:
            _r, secs = stage('card', lambda p: blank_card(paths.card))
            stages = record('card', {'inputs': {'recipe': RECIPE},
                                     'formatted': False, 'blank': True,
                                     'seconds': secs},
                            also=lambda s: s.pop('first_boot', None))
    elif ekfsformat.is_formatted(paths.card):
        if 'card' not in stages:
            stages = record('card', {'inputs': {'recipe': RECIPE},
                                     'formatted': False}, drop_after=False)
        skipped('card', 'the card holds an ekFS')
    else:
        _r, secs = stage('card', lambda p: prepare_card(paths.card,
                                                        progress=p))
        # A new card: no first boot has happened on it, whatever the state
        # said about the one it replaces.
        stages = record('card', {'inputs': {'recipe': RECIPE},
                                 'formatted': True, 'seconds': secs},
                        also=lambda s: s.pop('first_boot', None))

    # The chain: ladder -> intro -> settle, each tied to the card. ---------
    # The 'ladder' stage is the cold boot (cold_boot); the stage keeps its
    # name so the launcher, the logs and older firmware.json files agree.
    rung = paths.cold
    ladder_in = _canon({'recipe': RECIPE, 'syx_sha256': syx_sha,
                        'main_sha256': main_sha, 'cold': COLD,
                        'sdgate': True, 'esdhc': True})
    intro_in = _canon(dict(ladder_in, rung=os.path.basename(rung),
                           intro_channels=list(channels),
                           except_frame_sem=except_sem, stepping='fast',
                           **INTRO))
    settle_in = _canon(dict(intro_in, settle=SETTLE, fast_idle=True))
    chain = [('ladder', ladder_in, [paths.cold]),
             ('intro', intro_in, [paths.gui_raw]),
             ('settle', settle_in, [paths.gui])]
    last = None
    todo = []
    # A settled build whose snapshot still matches the card -- gui.snap
    # with the card as the settle left it, or resume.snap saved against the
    # card as it is now -- is what the panel opens, so there is nothing to
    # rebuild. Without this a played folder (card changed since the settle,
    # resume.snap matching it) was rebuilt from the cold boot and lost its
    # session whenever first_run ran again, e.g. adding the same .syx.
    chosen = None
    if _stage_ok(stages, 'settle', settle_in, [paths.gui]):
        chosen, _why = choose_snapshot(paths)
    if chosen is None:
        for name, inputs, outputs in chain:
            if not todo and _stage_ok(stages, name, inputs, outputs):
                last = name
            else:
                todo.append(name)
        card_now = card_stamp(paths.card)
        if last is not None and stages[last].get('card_after') != card_now:
            # The card moved on since the last stage that used it, and no
            # snapshot matches it: the RAM in those snapshots describes a
            # different card. Start over from the cold boot, which is where
            # the firmware reads the card.
            todo = [name for name, _i, _o in chain]
            skipped_note = 'the card changed since %s; rebuilding' % last
            overall.enter('ladder')
            _emitter(overall, 'ladder')('note', skipped_note)
    for name, _i, _o in chain:
        if name not in todo:
            skipped(name, 'up to date' if chosen is None else
                    'up to date (%s matches the card)'
                    % os.path.basename(chosen))

    # Whether anything vouches for a first boot that finished on this card,
    # read before the stamps go: a settle stamp, or the marker a settle that
    # passed acceptance leaves (it outlives the launcher's Rebuild, which
    # drops the stamps but keeps the card).
    vouched = (isinstance(stages.get('settle'), dict)
               or bool(read_state(paths).get('first_boot')))

    if todo:
        # A resume.snap continues the previous gui.snap; once that lineage
        # is rebuilt it is stale, and it must not win over the new gui.snap
        # just because its card stamp happens to match.
        for p in (paths.resume, paths.resume + '.tmp'):
            if os.path.exists(p):
                os.remove(p)

        # Stamps of stages about to rerun go first: their outputs are
        # overwritten as the run goes, so an interrupted rerun must not leave
        # an old stamp vouching for a half-new set of files.
        def forget(state):
            state.pop('resume', None)
            for name in todo:
                (state.get('stages') or {}).pop(name, None)
        stages = _update_state(paths, forget).get('stages') or {}

    if 'ladder' in todo:
        def ladder(p):
            # Before the cold boot, which is where the firmware decides from
            # sector 0 whether to install its factory content.
            clear_half_install(paths.card, vouched, _emitter(p, 'ladder'))
            fresh = _sector0(paths.card)[:4] != BEEFBACE
            res = cold_boot(paths, progress=p, cancel=cancel)
            return dict(res, first_boot=fresh)
        res, secs = stage('ladder', ladder)
        # first_boot: the cold boot found an uninitialised +Drive, so the
        # firmware's factory install runs in this chain and the settle must
        # find it on the card (accept_settled).
        stages = record('ladder', {'inputs': ladder_in, 'seconds': secs,
                                   'stop': res['stop'],
                                   'parked_at': res['parked_at'],
                                   'card_writes': res['card_writes'],
                                   'first_boot': res['first_boot'],
                                   'card_after': card_stamp(paths.card)})

    if 'intro' in todo:
        def intro(p):
            res = boot_to_ui(paths.syx, rung, out=paths.gui_raw, progress=p,
                             cancel=cancel, png=None,
                             intro_channels=channels,
                             except_frame_sem=except_sem, fast=True, **INTRO)
            if not res.up or res.saved is None:
                raise StepFailed('intro', 'no live user interface after %dM '
                                 'instructions (lit %d, mainloop %d)'
                                 % (res.total // 1_000_000, res.lit,
                                    res.mainloop))
            _require_clean_card_state(paths.gui_raw, 'intro')
            return res
        res, secs = stage('intro', intro)
        stages = record('intro', {'inputs': intro_in, 'seconds': secs,
                                  'live_at': res.total, 'lit': res.lit,
                                  'mainloop': res.mainloop,
                                  'card_after': card_stamp(paths.card)})

    if 'settle' in todo:
        def settle(p):
            res = settle_ui(paths.syx, paths.gui_raw, out=paths.gui,
                            progress=p, cancel=cancel, png=None,
                            except_frame_sem=except_sem, fast=True,
                            fast_idle=True, **SETTLE)
            if res.settled_at is None or res.saved is None:
                raise StepFailed('settle', 'not settled after %dM '
                                 'instructions (%d frames, %d overlays, '
                                 'quiet run %d)'
                                 % (res.total // 1_000_000, res.frames,
                                    res.overlays, res.quiet_run))
            _require_clean_card_state(paths.gui, 'settle')
            first_boot = (stages.get('ladder') or {}).get('first_boot')
            if first_boot is None:
                # A ladder stamped before the key existed: strict unless a
                # finished first boot on this card is on record.
                first_boot = not read_state(paths).get('first_boot')
            try:
                verdict = accept_settled(paths, acceptance,
                                         first_boot=first_boot, ekfs=ekfs)
            except StepFailed:
                # Unstamped, so nothing opens it; kept for a look.
                _set_aside(paths.gui)
                raise
            ram = verdict['ram']
            _emitter(p, 'settle')(
                'note', 'accepted: ' + (
                    'the card holds the finished first boot' if first_boot
                    else 'the +Drive was already initialised; it came up')
                + ('' if ram is None else '; RAM %s' % ', '.join(
                    '%s=0x%x' % kv for kv in sorted(ram.items()))),
                acceptance=verdict)
            return res, verdict
        (res, verdict), secs = stage('settle', settle)
        after = card_stamp(paths.card)

        def settled(state):
            state['card'] = after
            state['first_boot'] = {'at': _now(), 'syx_sha256': syx_sha}
        stages = record('settle', {'inputs': settle_in, 'seconds': secs,
                                   'settled_at': res.settled_at,
                                   'frames': res.frames,
                                   'overlays': res.overlays,
                                   'acceptance': verdict,
                                   'card_after': after}, also=settled)
    elif 'card' not in read_state(paths):
        _update_state(paths, lambda s: s.__setitem__(
            'card', stages['settle'].get('card_after')))
    return paths.gui


def _require_clean_card_state(snapshot, step):
    """A snapshot of a file-backed card must not carry card writes."""
    carried = card_state_in(snapshot)
    if carried is None:
        raise StepFailed(step, '%s has no eSDHC state' % snapshot)
    if carried != (0, 0):
        raise StepFailed(step, '%s carries %d card bytes and %d erased '
                         'ranges; they would replay over the card on every '
                         'launch' % ((snapshot,) + carried))


__all__ = [
    'Cancelled', 'Event', 'FirmwarePaths', 'FrameCounter', 'IntroResult',
    'SettleResult', 'StepFailed', 'accept_settled', 'boot_to_ui',
    'card_stamp', 'cold_boot', 'card_state_in', 'check_card', 'check_ram',
    'choose_snapshot', 'clear_half_install', 'extract_sections', 'first_run',
    'prepare_card', 'read_state', 'replace_retry', 'sections_current',
    'settle_ui', 'snapshot_reader', 'validate_acceptance', 'write_state',
]
