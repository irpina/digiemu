# pyright: reportMissingImports=false
# ruff: noqa: I001
# fmt: off
"""Long fast run: no per-instruction hook, chunked preemption, watch everything
that matters via begin==end hooks (free between hits).

`build()` is the reusable half: it stands up a Machine with the same behaviour
hooks dspboot.run installs (flash HLE, completion-semaphore patch, ISA patches,
MMIO, exceptions) and restores a snapshot onto it. Resuming onto a bare Machine
instead silently drops those hooks and the run diverges -- see snapshot.py.
"""
import math
import struct
import sys
import os
import time
import collections
import hashlib
from typing import Any
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unicorn import (UcError, UC_HOOK_BLOCK, UC_HOOK_CODE, UC_HOOK_MEM_READ,
                     UC_HOOK_MEM_WRITE)
from unicorn.m68k_const import (UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR,
                                UC_M68K_REG_D0, UC_M68K_REG_D2, UC_M68K_REG_A0)
import emu.dspboot as db
from emu.harness import Machine
from emu.snapshot import DeferredComponentRestore, restore_into
from emu import config, semscan, symbols

PRINT              = 0x400054b4
SWITCH_TO          = 0x4000044a
USR8, UDR8         = 0xEC070004, 0xEC07000C


PEND_A, PEND_B = 0x4000141a, 0x400013a6   # sem object is the arg at 4(a7)


def register_esdhc_checkpoint_component(machine, events, profile, components):
    """Install eSDHC and register its host-side card state for snapshots."""
    from emu import config as _config
    from emu.esdhc import Card, Esdhc

    model = Esdhc(
        machine,
        card=Card(path=_config.plusdrive_image()),
        drv_status=profile.sd_status,
        cmd_sem=profile.sd_cmd_sem,
        data_sem=profile.sd_data_sem,
        dma_sem=profile.sd_dma_sem,
        capacity_addr=getattr(profile, 'sd_capacity', None),
    )
    events['esdhc'] = model
    components['esdhc'] = model
    return model


def build(snapshot, send=b'', syx=None, isa='scoped',
          unblock=False, softfloat=False, bitmap=False, on_pixel=None,
          unblock_except=(), edma=True, real_sleep=False, dsp=False,
          srtrap=False, weakptr=False, slc=False, sdgate=True, esdhc=True,
          trace=None, trace_path=None, trace_ranges=(), trace_registers=None,
          deferred_components=(), idle_yield=20000, fast_idle=False,
          ssi0_request_hz=None,
          ssi0_legacy_upgrade=False, ssi0_profile=None,
          modeled_vectors=(208,), dsp_cpu=True):
    """Stand up a hooked Machine and restore `snapshot` onto it.

    dsp_cpu: on a Digitone (an image with the DSP boot task), run its
    second CPU for real from section 7 in the sections directory (the synth
    voices); False, or no section 7 there, gives the handshake stand-in.
    Which one ran is part of the checkpoint manifest. See emu/dsplink.py.

    -> (m, ev, st, pc, inq, at) where `at(addr, fn)` registers a further
    begin==end code hook and `inq` is the UART8 receive queue (a deque of
    ints; append to it to feed the firmware input).

    Pass ``deferred_components=('timers',)`` when the snapshot was saved with
    a ``Timers`` component.  Build restores guest state, UART, and eDMA before
    its legacy kick, then exposes ``ev['claim_checkpoint_component']``. Create
    timers against ``m`` and call ``ev['claim_checkpoint_component']('timers',
    timers)`` before ``spin`` or ``run_until``. The claim also registers it in
    ``ev['checkpoint_components']`` for the next save. Unclaimed saved state
    fails at execution rather than silently running with a fresh timer clock.

    idle_yield: raise vector 32 (reschedule) every N passes through an idle
    spin (bra.b $self); 20000 is the long-standing value.

    fast_idle: under the fast stepper, treat reaching an idle spin like a
    wait-for-interrupt -- end the step there and credit the rest of it as
    spent spinning (the passes still count toward idle_yield). Nothing can
    change before the next deadline, and every pass of the spin used to be a
    Python call: with a busy audio clock the CPU idles ~40% and that alone
    held the run to 14% of real time. Off by default; counted runs ignore it.

    isa='scoped' pre-scans MAIN OS for the FF1/MOVEC addresses and hooks only
    those, instead of running a Python callback on every instruction. This is
    what dspboot.run's fast path uses, so it is also what produced the
    snapshots -- resuming with it keeps the run faithful *and* is ~3x quicker.
    isa='global' is the belt-and-braces version: it also catches those opcodes
    outside the MAIN OS image, at the cost of that per-instruction callback.

    unblock=True force-satisfies every sem_pend whose count is <= 0, so the
    wait takes the primitive's non-blocking fast path. Without it every task
    but the idle one parks forever waiting on a device event -- DSP, panel,
    MIDI -- that no emulated hardware will ever raise. dspboot already does
    this for the one DSP transport semaphore; this is the same trick applied
    to every wait, and it is what makes the draw task actually draw.
    It does change semantics: nothing ever really waits, so inter-task
    ordering is not the hardware's. `unblock_except` lists semaphore objects
    to leave alone, for waits you want to drive properly instead -- the intro
    frame semaphore 0x43131200 is the case that matters, since satisfying it
    is what makes the animation run unpaced. The progress screen's frame
    semaphore (`display_sem`) is always excluded too, since the PIT3 ISR
    that posts it is modelled.

    On top of that hand-kept list, every semaphore emu/semscan.py finds a
    runnable guest poster for is excluded automatically; see the never-fake
    block below. `modeled_vectors` is what tells that scan which interrupt
    vectors this build actually raises, and so which ISR posts really happen
    here. It defaults to PIT3 (208) alone, which every path through this
    module delivers. Widen it only to vectors the run's Timers really
    deliver -- naming a vector that never fires marks its semaphores
    never-fake, and the run then waits for a post that never comes, which
    presents as a hang rather than as an error.

    `unblock` never satisfies a pend from any of `recheck` -- call sites
    that re-check a condition after the wait and loop, so satisfying them
    spins instead of sleeping. It also stops satisfying the intro frame
    semaphore by itself once the intro's exit path is reached, which covers a
    run that executes the intro; `recheck` covers a run resumed from a
    snapshot taken after it.

    softfloat=True runs the firmware's float routines natively instead of
    emulating them. It is OFF by default: it is bit-exact but changes
    instruction counts, and too much in this project depends on a resumed run
    matching the run that made its snapshot. Turn it on for watching, leave it
    off for coverage, differential or checkpoint work. 93% of executed instructions were soft-float, so this is
    the difference between ~2M and ~20M instructions/sec. It is bit-exact --
    only the fast path is intercepted and everything else defers to the real
    routine (see emu/softfloat.py) -- so program state evolves identically.
    Instruction *counts* do not: a run with it on is not comparable to one
    without, so turn it off for coverage or differential work.

    bitmap=True does the same for Bitmap::setPixel/getPixel, the top cost once
    the float work is gone. `on_pixel(x, y, val)` then receives every pixel
    drawn, which is how the frame capture observes drawing -- so callers must
    not also register their own setPixel hook.

    `edma` models eDMA channel 35, the UART8 transmit ring. It defaults ON
    because without it the firmware's console-enqueue routine spins forever
    waiting for ring space and boot cannot get past the intro -- see
    emu/edma.py. Unlike the softfloat/bitmap HLEs this is a hardware model,
    not a shortcut, so there is no faithful configuration with it off.

    real_sleep=True makes `0x40128c7c` a real sleep instead of a no-op, by
    letting `unblock` block at sleep_pend instead of force-satisfying it.
    It requires that DTIM1 is being delivered (see emu/dtim.py) or the
    priority-3 job worker will block forever.

    srtrap=True routes exception entry through guest code so the frame
    carries the CPU's real condition codes. Unicorn's m68k never reports
    computed flags through `reg_read(UC_M68K_REG_SR)`, so the frame we used to
    build carried a stale CCR that `rte` then installed over the flags of the
    code being resumed -- see Machine.install_srtrap. Default off because the
    fix is not finished: it delivers interrupts and runs the ISRs correctly
    but the boot stops progressing.

    dsp=True backs the `0x8C000000` coprocessor port's ready line, without
    which the priority-3 job worker wedges on its first transfer -- see
    emu/dsp.py. Default off because it is new, in the same spirit as
    softfloat and bitmap defaulting off.

    weakptr=True neutralises the two branches in `weak_ptr::lock` that send
    the main task into the terminal loop at `0x4012d2fa`:

        40188b40  6714 beq.b $40188b56  ->  4e71 nop     (a0 is NOT null)
        40188b50  660a bne.b $40188b5c  ->  600a bra.b   (d0 is NOT zero)

    Both branches contradict the memory they were taken on -- measured at the
    hang the control block reads 0x4509e3f0 and the use count reads 2 -- so
    this is the condition-code corruption of HANDOVER section 10 showing
    through, not a firmware decision. It is a DIAGNOSTIC, not a fix: it papers
    over one symptom of the exception model and leaves the cause alone.

    It is a two-byte memory write applied before `emu_start` is ever called,
    so it adds no hook (HANDOVER warning 2) and no translation block can be
    stale. Without it the main task burns the CPU forever -- 252,977,319 of
    300M instructions in a `bra.b` to itself -- and the message loop stops at
    153. With it there is no terminal loop and the loop reaches 168.

    slc=True sets the image-resolved cached EXT_CSD SLC-status byte to 1.
    `profile.slc_status_predicate` is the firmware's tri-state check: it
    returns 1 for ok, 0 for unset and -1 for error. The main task displays
    `MMC NOT IN SLC MODE` when its caller's `cmp.l #1,d0` fails. The address
    differs between builds (0x4fe49198 on the reference Digitakt image and
    0x4e531198 on Digitone), so it must never be a shared literal.

    The host write models state normally established before these snapshots;
    it is opt-in and fails clearly if the image has no resolvable predicate.

    Measured from postintro.snap over 60M with weakptr=True: the modal dialog
    goes away and the panel settles on the real main screen (project name,
    tempo, encoder labels) instead of `Loading...`; both job workers start and
    then block properly in the RTOS wait at 0x4000165c instead of one wedging
    in the coprocessor spin; and channels=(3, 1) stops faulting -- it runs to
    the limit where it used to die at 55.6M with `unhandled vector 257`.

    sdgate=True models the board loopback that gates storage: port C bit 3
    follows port D bit 4, which is what `0x4011fe60` spends ten iterations
    checking. Without it that gate returns 1 on its first pass and
    `0x400cf216` skips the eSDHC card init entirely -- measured from
    boot40M.snap over 120M instructions, the driver is never entered and not
    one eSDHC register is ever touched. See emu/gpio.py.

    esdhc=True models the SD/MMC controller at 0xFC0CC000 and a minimal eMMC
    behind it, which is what `sdgate=True` exists to reach. It carries the
    whole card identification sequence -- CMD0, CMD1 until OCR bit 31 sets,
    CMD2/3/10/9 for CID and CSD, CMD7 select, CMD6 for HS_TIMING and
    BUS_WIDTH, the CMD19/CMD14 bus test, CMD16 block length, and CMD8
    SEND_EXT_CSD -- and card init runs to completion instead of spinning.
    See emu/esdhc.py.

    It needs `sdgate=True` to be any use: without the gate the driver is never
    entered and the model is never touched. With both, PRSSTAT goes from
    36,988,314 reads to 7. CMD18 and CMD25 bulk data moves through SoC eDMA
    channel 59. An absent backing image reads as zero-filled media; writes
    are retained in a sparse card overlay and checkpointed so later reads
    and resumed runs see them.

    Both default to True now. Without them the firmware's SD bring-up never
    runs, the storage-ready flag stays 0, and every block-storage read
    returns -1. With them on, both builds reach MAIN_OS_RUNNING under
    tools/bootcheck.py --verify: Digitone's display module initialises for
    the first time, and Digitakt's cold boot creates 9 tasks instead of 5,
    including the priority-6 Main OS task at entry 0x40032f5a. Digitakt
    reaches MAIN_OS_RUNNING both with and without them, so turning them on
    does not regress the previously-working build. Pass sdgate=False and/or
    esdhc=False to get the old unmodelled-storage behaviour back.

    ``ssi0_request_hz`` opts into the separate SSI0/eDMA48/50 event source.
    The explicit rate is mandatory because the board's external SSI_CLKIN
    frequency is not recovered. ``ssi0_legacy_upgrade=True`` is the only way
    to add it to an old checkpoint: the restored guest TCDs are validated,
    the fresh SSI clock begins at that checkpoint boundary, and subsequent
    saves carry both a topology manifest entry and an independent component.
    It is never silently added to legacy snapshots.
    """
    if trace is not None and trace_path is not None:
        raise ValueError('pass either trace or trace_path, not both')
    if ssi0_legacy_upgrade and ssi0_request_hz is None:
        raise ValueError('SSI0 legacy upgrade requires an explicit request rate')
    syx = config.firmware(syx)
    flash = db.build_flash(syx)
    # This is intentionally bounded data, not the hook closures themselves.
    # A stateful checkpoint may only resume under the same hook topology.
    checkpoint_manifest = {
        'protocol': 1, 'isa': isa, 'unblock': bool(unblock),
        'softfloat': bool(softfloat), 'bitmap': bool(bitmap), 'edma': bool(edma),
        'dsp': bool(dsp), 'srtrap': bool(srtrap), 'weakptr': bool(weakptr),
        'slc': bool(slc), 'sdgate': bool(sdgate), 'esdhc': bool(esdhc),
        'real_sleep': bool(real_sleep), 'unblock_except': tuple(unblock_except),
        'flash_sha256': hashlib.sha256(flash).hexdigest(),
    }
    if ssi0_request_hz is not None and not ssi0_legacy_upgrade:
        checkpoint_manifest['ssi0_dma'] = {'request_hz': int(ssi0_request_hz)}
    # modeled_vectors changes which pends unblock fakes, so it is part of the
    # hook topology -- but it is recorded only when it is NOT the default, the
    # way ssi0_dma is. Writing it unconditionally would change every existing
    # snapshot's manifest and refuse to reopen ladders built before it, for a
    # value none of them chose.
    if tuple(modeled_vectors) != (208,):
        checkpoint_manifest['modeled_vectors'] = tuple(sorted(modeled_vectors))
    m = Machine(); st = {'seen': set(), 'n': 0, 'task_create_hits': {}}
    ev = {'tasks': [], 'prints': [], 'setpixel': 0, 'pxcopy': 0,
          'switch': collections.Counter(), 'switch_seq': [],
          'uart_out': bytearray(), 'satisfied': 0, 'satisfied_by': collections.Counter(),
          'satisfied_by_sem': collections.Counter(),
          'depack_clamps': 0}
    inq = collections.deque(send)
    with open(config.main_image(), 'rb') as fh:
        main_img = fh.read()
    checkpoint_manifest['main_sha256'] = hashlib.sha256(main_img).hexdigest()
    # Resolve addresses from the image itself rather than dspboot's
    # Digitakt-specific module constants -- see emu/symbols.py. Cached per
    # image SHA-256, so this costs nothing extra when dspboot.run has already
    # resolved the same image (e.g. checkpoint.py builds a snapshot with
    # dspboot.run and then resumes it here).
    profile = symbols.resolve(main_img, load_addr=db.MAIN_LOAD)

    # Sites that decide how far `unblock` may go. Each of these is a pend
    # whose caller re-checks a condition afterwards and loops, so
    # force-satisfying the semaphore turns a sleep into an infinite spin.
    # Blocking is the correct behaviour at all of them: nothing has arrived.
    #
    #   queue_recv    the pend inside queue_receive. It waits on the queue's
    #               own semaphore at queue+8, then re-reads queue->count.
    #               Measured at 8.9M iterations, ~92% of all post-intro
    #               cycles. Digitakt 0x40001946; resolved per build as
    #               profile.queue_recv.
    #   intro_park    the intro task's park loop. It pends the SAME semaphore
    #               the intro loop pends, which must be satisfied -- so this
    #               has to be told apart by caller, not by semaphore.
    #               Blocking here is what frees the CPU once the intro is
    #               over, and unlike the intro_done hook below it also works
    #               on a snapshot taken after the intro had already
    #               finished. Digitakt 0x400d4068; resolved per build as
    #               profile.intro_park.
    #   display_wait  the prio-6 progress-screen task waiting on the frame
    #               semaphore and re-checking a flag. Note this is the
    #               loading screen, not the user interface -- see HANDOVER.
    #               Digitakt 0x401260c2; resolved per build as
    #               profile.display_wait.
    #   pump_wait     the job worker pool's "is there work" pend, at the top
    #               of the pump (`jsr (a5)`, a5 = PEND_B). The semaphore is a
    #               plain count of queued jobs, so satisfying it hands the
    #               worker a ring slot nobody wrote. It then runs a job that
    #               is not there and destroys the record, whose std::string
    #               has a null data pointer -- and `_M_dispose` frees
    #               `_M_data() - sizeof(_Rep)`, which for a null is
    #               0xfffffff4. That trips the allocator's own bounds check
    #               and takes vector 4 at the `illegal` opcode at
    #               0x40111458. It is very likely the whole "C++ throw
    #               nothing can unwind" story of section 9: the recorded
    #               message is `basic_string::_S_construct null not valid`,
    #               which is the same null string seen from the other end.
    #               Measured from postintro.snap with dsp=True: blocking
    #               here takes a run that faulted at 70.2M to a clean 100M,
    #               and takes the pump from one job to two. Digitakt
    #               0x400f1bb0; resolved per build as profile.pump_wait.
    #   sleep_pend    the microsecond sleep. It arms DMA timer 1 and pends a
    #               semaphore, and the timer's own ISR posts it. Satisfying
    #               it makes every sleep in the firmware a no-op, which is
    #               only harmless while DTIM1 is not delivered -- see
    #               emu/dtim.py. Blocking here is correct once it is, and is
    #               what stops the priority-3 job worker monopolising the
    #               CPU. Digitakt 0x40128d0e; resolved per build as
    #               profile.sleep_pend.
    #   tick_pend     the pend at the top of the RTOS tick dispatcher: pend_b
    #               (tick_sem); mutex_lock(m); run every due callback;
    #               mutex_unlock(m); repeat. Unlike the entries above, this
    #               wait does not gate a re-checked condition -- it IS the
    #               tick pacing. Satisfying it turns a tick-paced loop
    #               free-running, which ran the 54-slot software timer wheel
    #               ~100x per real tick and starved the priority-6 Main OS
    #               task before it could finish initialising. The pend site
    #               is the same address, 0x40002a70, in both builds; the
    #               semaphore it pends differs per build. Resolved per build
    #               as profile.tick_pend.
    recheck = tuple(a for a in (profile.queue_recv, profile.intro_park,
                                profile.display_wait, profile.pump_wait,
                                profile.tick_pend)
                    if a is not None)
    # Only correct when DTIM1 is actually delivered; see build(real_sleep=...).
    real_sleep_pends = tuple(a for a in (profile.sleep_pend,) if a is not None)

    if isa == 'scoped':
        m.install_isa_patches_scoped(main_img, db.MAIN_LOAD)
    else:
        m.install_isa_patches()

    def at(addr, fn):
        m.uc.hook_add(UC_HOOK_CODE, fn, begin=addr, end=addr)

    def maybe_at(addr, fn):
        # OPTIONAL symbols degrade gracefully: unresolved just means the
        # hook is not installed, never a crash -- see emu/symbols.py.
        if addr is not None:
            at(addr, fn)

    def flash_read(uc, a, s, d):
        sp = uc.reg_read(UC_M68K_REG_A7)
        ret, off, ln, dest = struct.unpack('>IIII', uc.mem_read(sp, 16))
        if ln and dest and off + ln <= len(flash):
            for p in range(0, ln + 0x100000, 0x100000): m.ensure(dest + p)
            uc.mem_write(dest, flash[off:off + ln])
        uc.reg_write(UC_M68K_REG_D0, 0); uc.reg_write(UC_M68K_REG_A7, sp + 4)
        uc.reg_write(UC_M68K_REG_PC, ret)

    def task_create(uc, a, s, d):
        sp = uc.reg_read(UC_M68K_REG_A7)
        _r, tcb, entry, prio = struct.unpack('>IIII', uc.mem_read(sp, 16))
        ev['tasks'].append((entry, prio, tcb))
        print('   TASK entry=0x%08x prio=%d tcb=0x%08x' % (entry, prio, tcb), flush=True)

    def do_print(uc, a, s, d):
        sp = uc.reg_read(UC_M68K_REG_A7)
        p = struct.unpack('>I', uc.mem_read(sp + 4, 4))[0]
        try: txt = bytes(uc.mem_read(p, 160)).split(b'\x00')[0].decode('latin1')
        except Exception: txt = '<0x%08x>' % p
        ev['prints'].append(txt)
        print('   PRINT %r' % txt, flush=True)

    def switch_to(uc, a, s, d):
        tcb = uc.reg_read(UC_M68K_REG_A0)
        ev['switch'].update([tcb])
        if not ev['switch_seq'] or ev['switch_seq'][-1] != tcb:
            ev['switch_seq'].append(tcb)

    at(profile.flash_read, flash_read)
    at(profile.pend_call, lambda uc,a,s,d: uc.mem_write(profile.completion_sem, struct.pack('>I',1)))
    maybe_at(profile.task_create, task_create)
    at(PRINT, do_print)
    maybe_at(profile.set_pixel, lambda uc,a,s,d: ev.__setitem__('setpixel', ev['setpixel']+1))
    maybe_at(profile.px_copy,   lambda uc,a,s,d: ev.__setitem__('pxcopy',  ev['pxcopy']+1))
    at(SWITCH_TO, switch_to)

    # dspboot.run installs two more behaviour hooks, and the snapshots were
    # made with them. Leaving them out here makes a resumed run diverge from
    # the run that produced the snapshot -- the same trap as resuming onto a
    # bare Machine, just less obvious: the init task then never reaches its
    # own flag test at 0x400cf384.
    def depack_clamp(uc, a, s, d):
        if uc.reg_read(UC_M68K_REG_D2) > db.DEPACK_LEN_CAP:
            uc.reg_write(UC_M68K_REG_D2, 1)
            ev['depack_clamps'] += 1
    at(profile.depack_copy, depack_clamp)

    tx = None
    if edma:                           # see emu/edma.py
        from emu.edma import install as install_edma
        tx = install_edma(m, at, ev, wait_loop=profile.uart8_tx_wait)

    ssi0 = None
    if ssi0_request_hz is not None:
        if profile.ssi0_dma_force_rte is None:
            raise RuntimeError("SSI0 DMA model requires the force-ISR RTE symbol")
        from emu.pit import INSTR_PER_SEC
        from emu.ssi import DIGITAKT2, PROFILES
        from emu.ssi import install as install_ssi0
        # Which product's SSI block, eDMA channels and vectors to drive.
        # Named explicitly rather than sniffed: picking the wrong one would
        # move bytes through descriptors that are not the audio path.
        chosen = ssi0_profile or DIGITAKT2
        if isinstance(chosen, str):
            if chosen not in PROFILES:
                raise RuntimeError('unknown SSI profile %r; have %s'
                                   % (chosen, sorted(PROFILES)))
            chosen = PROFILES[chosen]
        ssi0 = install_ssi0(
            m,
            at,
            ev,
            request_hz=int(ssi0_request_hz),
            instr_per_sec=INSTR_PER_SEC,
            force_rte=profile.ssi0_dma_force_rte,
            profile=chosen,
        )

    if dsp:                            # see emu/dsp.py
        from emu.dsp import install as install_dsp
        install_dsp(m, ev)

    spins = {'n': 0}
    ev['idle_spins'] = spins

    def do_halt(uc, a, s, d):
        before = spins['n'] // idle_yield
        spins['n'] += 1
        if tx is not None:
            tx.deliver()
        stepper = getattr(m, '_fast_stepper_obj', None) if fast_idle else None
        skip = stepper.left if stepper is not None else 0
        if skip > 0:
            # The rest of the step would be spent spinning here: count those
            # passes and end the step, so the time is credited exactly once.
            # (Counting them without ending the step recounted the same
            # budget on every return to the spin -- a reschedule storm of a
            # million vector-32s where a few thousand were due.)
            spins['n'] += skip
            stepper.idle_skip(skip)
        if spins['n'] // idle_yield != before:
            m.raise_vector(32)
    for spin_addr in db.find_idle_spins(main_img, db.MAIN_LOAD):
        at(spin_addr, do_halt)

    if softfloat:                      # see emu/softfloat.py
        from emu.softfloat import install as install_softfloat
        ev['softfloat'] = collections.Counter()
        # Per-build entry points. The soft-float block relocates like any
        # other application code, and an HLE hook on the wrong address
        # corrupts the guest rather than merely missing -- so unresolved
        # names are dropped, not defaulted. See emu/softfloat.py.
        sf = {name: profile.get('sf_' + name)
              for name in ('mulsf3', 'subsf3', 'addsf3', 'divsf3',
                           'abssf2', 'fixsfsi', 'cmpsf2')}
        ev['softfloat_hooked'] = install_softfloat(at, ev['softfloat'], sf)

    if bitmap:
        from emu.hle import install_bitmap
        ev['bitmap'] = collections.Counter()
        install_bitmap(at, ev['bitmap'], on_pixel,
                       set_pixel=profile.set_pixel, get_pixel=profile.get_pixel)

    if unblock:
        # Both sets are kept mutable and exposed on `ev` so a run can change
        # policy partway through, which the intro needs: its frame semaphore
        # has to be satisfied while the intro runs and must NOT be once it
        # finishes, or the draw task busy-spins at prio 7 and starves the rest
        # of the system. That handoff is wired up below rather than left to
        # each caller -- getting it wrong is silent, it just looks like a hang.
        skip = set(unblock_except)
        # The progress screen's frame semaphore is posted by the display
        # module's own PIT3 ISR at ~7.5 Hz, and PIT3 is modelled, so it must
        # never be faked. `display_wait` only covers the task's first pend;
        # its per-frame pend (0x40126132) was being force-satisfied, so the
        # prio-6 task drew about 40 frames per real one and starved the prio-2
        # job worker doing +Drive initialization.
        if profile.display_sem is not None:
            skip.add(profile.display_sem)
        # display_sem above was found by chasing one hang. emu/semscan.py
        # generalises that to "enumerate every give/give_b post site in the
        # image and classify it": a semaphore whose poster is ordinary task
        # code, or an ISR of a vector this build really raises, has a poster
        # that CAN run here, so faking its pend does not avoid a wait that
        # would never end -- it races ahead of a poster that was going to run.
        # One with no such poster (an ISR of hardware nothing here models)
        # stays fakeable, which is the whole point of unblock.
        #
        # On this image the scan finds display_sem (agreeing with the hand
        # result above), frame_sem, and a producer/consumer pair that had been
        # faked. It costs ~0.1s and runs off the image alone -- no Ghidra.
        # Both post primitives have to have resolved. If either did not, the
        # scan would search for call sites that are not there and return an
        # empty set, which is indistinguishable from "this build has no task
        # posters" -- the silent-None failure mode that `Fixed` rules have.
        # Say so on ev instead and fall back to the hand-kept list alone.
        never_fake = set()
        if profile.give is None or profile.give_b is None:
            ev['never_fake_unavailable'] = (
                'give/give_b did not resolve; unblock is using the hand-kept '
                'list only')
        else:
            never_fake = set(semscan.never_fake_semaphores(
                main_img, load_addr=db.MAIN_LOAD,
                modeled_vectors=modeled_vectors,
                give=profile.give, give_b=profile.give_b))
        # frame_sem is posted from a modelled vector too (PIT3, by the intro's
        # own handler, before the display module re-points vector 208 at its
        # own), but it must START fakeable: the intro needs it satisfied while
        # it runs, and only becomes never-fake at intro_done, through the
        # dynamic skip.add() below. Leaving it in here would make it
        # permanently never-fake and bring back the prio-7 busy-spin that the
        # dynamic handling exists to avoid.
        never_fake.discard(profile.frame_sem)
        if esdhc:
            # Not findable by the scan: emu/esdhc.py posts these three from
            # host code at command/transfer completion rather than through any
            # vector, so they have a poster that runs here but no give site in
            # the image. Faking them races ahead of the model.
            never_fake |= {getattr(profile, n, None) for n in
                           ('sd_cmd_sem', 'sd_data_sem', 'sd_dma_sem')}
        skip.update(s for s in never_fake if s is not None)
        ev['never_fake'] = frozenset(s for s in never_fake if s is not None)
        ev['unblock_skip'] = skip
        skip_callers = set(recheck)
        if real_sleep:
            skip_callers |= set(real_sleep_pends)
        ev['unblock_skip_callers'] = skip_callers

        def satisfy(uc, a, s, d):
            sp = uc.reg_read(UC_M68K_REG_A7)
            ret, sem = struct.unpack('>II', uc.mem_read(sp, 8))
            if not sem or sem in skip or ret in skip_callers:
                return
            try:
                if struct.unpack('>i', uc.mem_read(sem, 4))[0] <= 0:
                    uc.mem_write(sem, struct.pack('>I', 1))
                    ev['satisfied'] += 1
                    ev['satisfied_by'][ret] += 1
                    ev['satisfied_by_sem'][sem] += 1
            except Exception:
                pass
        at(profile.sem_pend, satisfy); maybe_at(profile.pend_b, satisfy)
        if profile.intro_done is not None and profile.frame_sem is not None:
            frame_sem = profile.frame_sem
            at(profile.intro_done, lambda uc, a, s, d: skip.add(frame_sem))

    # The Digitone's second CPU (see emu/dsplink.py): section 7 run for real
    # when the sections directory has it, else the handshake stand-in. Only
    # an image with the DSP boot task gets either, so a Digitakt's run and
    # manifest are unchanged. Its request semaphore has a poster semscan
    # cannot see, so it is never faked.
    from emu import dsplink as _dsplink
    link = _dsplink.install(m, ev, profile, sections_dir=config.sections_dir(),
                            real=dsp_cpu)
    if link is not None:
        checkpoint_manifest['dsplink'] = ('cpu' if isinstance(
            link, _dsplink.DspCpu) else 1)
        m.async_sources = getattr(m, 'async_sources', ()) + (link,)
        if unblock and link.request_sem is not None:
            skip.add(link.request_sem)
            ev['never_fake'] = ev['never_fake'] | {link.request_sem}

    def onr(uc, typ, addr, size, val, data):
        if addr == USR8: uc.mem_write(USR8, bytes([0x04 | (0x01 if inq else 0)]))
        elif addr == UDR8: uc.mem_write(UDR8, bytes([inq.popleft() if inq else 0]))
    def onw(uc, typ, addr, size, val, data):
        if addr == UDR8: ev['uart_out'].append(val & 0xFF)
    m.uc.hook_add(UC_HOOK_MEM_READ, onr, begin=USR8, end=UDR8+3)
    m.uc.hook_add(UC_HOOK_MEM_WRITE, onw, begin=USR8, end=UDR8+3)
    m.mmio[0xFC05C02C] = 0x100000F0; m.mmio[0xEC03802C] = 0x80000000
    # Route exception entry through guest code that writes the TRUE SR into
    # the frame. Off by default: it is a correct fix for a proven defect (see
    # Machine.install_srtrap) but it is not yet a working one -- with it on,
    # the main task is still scheduled and the ISRs still run, yet it never
    # reaches task_create and the boot makes no progress. Finish that before
    # turning it on. HANDOVER section 10 has the standing warning about how
    # sensitive this run is to anything that touches the exception path.
    if srtrap:
        m.install_srtrap()
    m.install_exceptions()
    weak_sites = ((0x40188b40, b'\x67\x14', b'\x4e\x71'),
                  (0x40188b50, b'\x66\x0a', b'\x60\x0a'))
    # restore_into merges the snapshot's own mmio entries, and install_mmio
    # registers a hook per address, so it has to come after the merge or a
    # snapshot-carried address would go unhooked.
    # Observations (tasks, prints, switches, UART output) deliberately are
    # not checkpoint state: checkpoint comparisons use their post-save suffix.
    checkpoint_components: dict[str, Any] = {'uart_in': inq}
    if link is not None:
        checkpoint_components['dsplink'] = link
    if tx is not None:
        checkpoint_components['edma_tx'] = tx
    if ssi0 is not None:
        checkpoint_components['ssi0_dma'] = ssi0
    if esdhc:
        # Construct before restore_into: the constructor seeds reset values,
        # then the snapshot overwrites them with its actual controller state.
        # Constructing afterward silently reset in-flight register state on
        # every resume.
        register_esdhc_checkpoint_component(
            m, ev, profile, checkpoint_components
        )
    deferred_restore = DeferredComponentRestore(deferred_components)

    def claim_checkpoint_component(name, component):
        """Claim deferred state and include this component in subsequent saves."""
        restored = deferred_restore.claim(name, component)
        checkpoint_components[name] = component
        return restored

    def restore_checkpoint_timers():
        """Safely construct, claim, and register saved timer cadence."""
        from emu.dtim import restore_timers
        timers = restore_timers(m, deferred_restore)
        if timers is not None:
            checkpoint_components['timers'] = timers
        return timers

    # Pass these unchanged to snapshot.save() when checkpointing a build().
    ev['checkpoint_components'] = checkpoint_components
    ev['checkpoint_manifest'] = checkpoint_manifest
    ev['deferred_checkpoint_restore'] = deferred_restore
    ev['claim_checkpoint_component'] = claim_checkpoint_component
    ev['restore_checkpoint_timers'] = restore_checkpoint_timers
    # spin/run_until enforce completion before entering Unicorn.
    m._checkpoint_deferred_restore = deferred_restore
    pc = restore_into(m, snapshot, st, components=checkpoint_components,
                      manifest=checkpoint_manifest, deferred=deferred_restore)
    if ssi0 is not None:
        if ssi0_legacy_upgrade:
            assert ssi0_request_hz is not None
            ssi0.arm_legacy()
            checkpoint_manifest['ssi0_dma'] = {
                'request_hz': int(ssi0_request_hz)
            }
        elif not ssi0._checkpoint_restored:
            raise RuntimeError(
                "SSI0 DMA topology is absent; use an explicit legacy upgrade"
            )
    if weakptr:
        # After restore_into, or the snapshot's own copy of MAIN OS would
        # overwrite the patch.
        for addr, want, new in weak_sites:
            cur = bytes(m.uc.mem_read(addr, 2))
            if cur != want:
                raise RuntimeError('weakptr: %#010x holds %s, expected %s'
                                   % (addr, cur.hex(), want.hex()))
            m.uc.mem_write(addr, new)
    if slc:
        # After restore_into for the same reason weakptr is. Host writes do not
        # enter Machine._fault, so map the otherwise guest-demand-mapped page
        # first. The address is extracted from the resolved guest predicate;
        # using Digitakt's old literal leaves Digitone's predicate at zero.
        if profile.slc_status_addr is None:
            raise RuntimeError('slc=True requires an image-resolved SLC status predicate')
        m.ensure(profile.slc_status_addr)
        m.uc.mem_write(profile.slc_status_addr, b'\x01')
    if sdgate:
        from emu.gpio import SdGate
        ev['sdgate'] = SdGate(m)
    m.install_mmio()
    if trace_path is not None:
        from emu.trace import JsonlMmioTrace
        trace = JsonlMmioTrace(trace_path)
    try:
        m.install_mmio_trace(trace, ranges=trace_ranges,
                             registers=trace_registers, owned=trace_path is not None)
        if tx is not None:
            from emu import edma as edma_model
            # A stateful checkpoint has the model's pending completions;
            # rerunning its guest-derived transfer duplicates bytes.
            if edma_model.needs_legacy_kick(tx):
                edma_model.kick(m, tx, tx_state=profile.uart8_tx_state)
        return m, ev, st, pc, inq, at
    except Exception:
        m.close()
        raise


def run_until(m, pc, timeout_ms=250):
    """Run with no instruction budget until a hook calls `uc.emu_stop()`.

    -> (pc, stop_reason). Prefer this over `spin` wherever the stopping
    condition can be written as a hook, because passing `count` to emu_start
    makes Unicorn install an internal per-instruction hook to decrement the
    budget, and that defeats its fast dispatch path. Measured over the same 40
    rendered frames: 8.03s with `count=250_000` against 4.45s with no count,
    a 1.8x difference for identical work. Measured again in 2026-09-13 on the
    settled main OS rather than the intro, the same comparison is **7.6x**
    (2.0M instructions a second against 15.5M) -- the penalty grows with the
    amount of translated code in play, so treat 1.8x as a floor, not a figure.

    The cost is in `count` itself, not in how often emu_start is called --
    over the same 100 frames, count=20k (1308 calls), count=500k (53 calls)
    and count=1e9 (1 call) all land within 3% of each other.

    `timeout_ms` bounds how long a single call may stay inside Unicorn, so a
    caller that also has to honour a pause or stop flag keeps responding even
    when the firmware stops doing whatever the hook was watching for. Without
    it, a hook-only stop condition hangs the caller the moment the firmware
    stops meeting it -- which is exactly what happens when the intro ends and
    nothing draws any more. A timeout return is not distinguishable from a
    hook return, so the caller re-checks its own condition and calls again,
    which is what a loop does anyway. Pass 0 for no bound.

    Unlike `count`, a timeout is free: over those same 40 frames, uncounted
    measures 4.45s and uncounted with a 0.5s timeout measures 4.43s. `count`
    installs a per-instruction hook; a timeout only arms a timer thread.

    Stop only from a hook that has already moved PC past the current
    instruction -- the setPixel HLE writes PC = return address, so it
    qualifies. Stopping from a plain code hook leaves PC on the hooked
    address, and resuming re-enters the same hook immediately: the run then
    spins making no progress while appearing to iterate.
    """
    deferred = getattr(m, '_checkpoint_deferred_restore', None)
    if deferred is not None:
        deferred.require_claimed()
    if pc == 0:
        return pc, 'pc zero'
    try:
        m.uc.emu_start(pc, 0, timeout=timeout_ms * 1000)
        stop = 'stopped'
    except UcError as e:
        stop = str(e)
    pc = m.uc.reg_read(UC_M68K_REG_PC)
    if stop == 'stopped' and pc == 0:
        stop = 'pc zero'
    return pc, stop


class _FastStepper:
    """Bound a run without `count=`, by counting basic-block entries.

    `count=` is what makes emu_start stop on an exact instruction, and
    Unicorn implements it by counting every instruction, which breaks TB
    chaining. Measured on this firmware, same machine, same hooks, same stop
    mechanism: 2.0M instructions a second counted against 15.5M uncounted, a
    7.6x tax. That is where the emulator's speed went -- a cProfile of the
    running configuration puts 99.5% of wall time inside emu_start and under
    0.5% in every Python callback in this project combined.

    A block hook can bound a run instead, and it leaves the fast dispatch
    path intact. Two things it cannot do exactly:

      * The hook says a block was ENTERED, not how many instructions ran, so
        the count is entries times a fixed instructions-per-entry figure.
      * The run stops at the first block boundary at or after the target, so
        a timer fires up to one basic block late (about four instructions
        here) rather than on the exact instruction it was due.

    So this changes the instruction stream, and therefore the boot digest.
    It is opt-in, never the default, and nothing making a determinism or
    pass/fail claim should use it. See spin's `fast` argument.

    Two earlier designs failed here, and both failures are worth keeping:

    It summed block `size` in BYTES and divided by a bytes-per-instruction
    ratio. But `size` is the size of the TRANSLATED block, not of what
    executed, so a block entered and branched out of early still counted its
    whole length. Entries do not have that problem.

    It re-measured that ratio every sixteen steps from a `count=`-ed step,
    and smoothed the sample in at 0.25. Two things made that a one-way
    ratchet. Deadline stepping routinely asks for a step of 1, 3 or 11
    instructions when two timers come due together, and `step` was the
    divisor, so a single translated block's byte count swamped it. And the
    budget MULTIPLIED by the ratio, so a bad sample lengthened the next run
    instead of shortening it. Successive calibrations read 3.8 -> 16.2 ->
    849 -> 17145, after which one nominal 62,377-instruction step consumed
    1.07 GB of blocks -- roughly 200M instructions -- in 435 seconds while
    crediting the emulated clock its 62,377. The GUI showed 1% of real time
    and a frozen panel.

    Both are now structural rather than guarded. The budget DIVIDES by
    `per_block`, which is never below one instruction, so `left` can never
    exceed `step` entries however wrong the figure is: it can make a step
    short and the timers choppy, it cannot make one run away. And there is
    no calibration to go wrong, because measurement showed there is nothing
    to calibrate -- see PER_BLOCK.
    """

    # Instructions per basic-block entry, measured on the main OS with
    # `tools/steptrace.py --hookprobe 1000000 --warmup`: 1,000,000
    # instructions entered 258,332 blocks, 3.871 an entry.
    #
    # The figure has to come from an UNCOUNTED run, which is why that probe
    # counts instructions with a code hook rather than asking `count=` for a
    # known number. Under `count=` the block hook's call count bears no
    # relation to the run: the same probe's counted arm saw 692 calls for
    # those million instructions, and in situ the calibration this replaced
    # read anything from 0.009 to 1.2 instructions an entry. Calibrating
    # from a counted step was measuring nothing, and spent 1.1-1.4 seconds a
    # call to do it -- more wall time than the stepping it accelerated.
    PER_BLOCK = 3.87
    # A basic block is at least one instruction, and nothing on this image
    # comes near the ceiling. A figure outside the band is a mistake, not a
    # tuning choice, so it is refused rather than clamped.
    PER_BLOCK_MIN = 1.0
    PER_BLOCK_MAX = 64.0

    def __init__(self, m, per_block=PER_BLOCK):
        if not self.PER_BLOCK_MIN <= per_block <= self.PER_BLOCK_MAX:
            raise ValueError(
                'instructions per block must be between %g and %g, not %r'
                % (self.PER_BLOCK_MIN, self.PER_BLOCK_MAX, per_block))
        self.m = m
        self.per_block = per_block
        self.steps = 0
        # The budget lives in a native block hook when the patched Unicorn
        # has one (emu/native.py): same rule, no Python call per block,
        # which alone held the emulator to ~10M instructions a second.
        self._native = None
        from emu import native
        if native.budget_available(m.uc):
            self._native = native.NativeBudget(m.uc)
        else:
            self._blocks = 0
            self._left = 0
            m.uc.hook_add(UC_HOOK_BLOCK, self._on_block)

    # `left` and `blocks` read and write the native counters when there are
    # some, so callers that end a step early (`stepper.left = 0` from a
    # hook) work either way.
    @property
    def left(self):
        return self._native.state.left if self._native else self._left

    @left.setter
    def left(self, v):
        if self._native:
            self._native.state.left = v
        else:
            self._left = v

    @property
    def blocks(self):
        return self._native.state.blocks if self._native else self._blocks

    @blocks.setter
    def blocks(self, v):
        if self._native:
            self._native.state.blocks = v
        else:
            self._blocks = v

    def idle_skip(self, blocks):
        """The CPU is parked in an idle spin: end the step at the next block
        and count the `blocks` it would have spun as run (see fast_idle)."""
        self.blocks = self.blocks + blocks
        self.left = 0

    def _on_block(self, uc, addr, size, data):
        # Checked BEFORE counting, so the block that exhausts the step still
        # runs and is still counted, and the one after it is neither.
        if self._left <= 0:
            uc.emu_stop()
            return
        self._blocks += 1
        self._left -= 1

    def run(self, pc, step):
        """Execute about `step` instructions from `pc`. -> instructions run."""
        self.steps += 1
        self.blocks = 0
        # Round the block budget up. Rounded down, a full step reports a
        # count a few instructions short of `step`, so the deadline it was
        # aimed at is not yet due and `spin` runs a second, one-block step
        # to cross it: two emu_start calls per deadline, the same blocks.
        self.left = max(1, math.ceil(step / self.per_block))
        self.m.uc.emu_start(pc, 0)
        return max(1, int(self.blocks * self.per_block))


def _fast_stepper(m):
    stepper = getattr(m, '_fast_stepper_obj', None)
    if stepper is None:
        stepper = _FastStepper(m)
        m._fast_stepper_obj = stepper
    return stepper


def spin(m, pc, instrs, chunk=500_000, on_chunk=None, tick=False, pits=None,
         fast=False, async_events=()):
    """Run in chunks. -> (pc, executed, stop_reason).

    Pass `pits` (an emu.pit.Pits) to run to each timer deadline exactly
    instead of to a fixed chunk. `chunk` is then unused: the step is
    whatever remains before the next PIT is due, so an interrupt lands on
    the instruction the timer was due at. Without this, the same run from
    the same snapshot gives different fault counts and different display
    callback counts for nothing but a different chunk size. Only timer
    deadlines may subdivide timer-stepped execution; arbitrary subdivisions
    are unsupported. Servicing the timers is part of this loop when `pits` is given, so do
    not also service them from `on_chunk`. `async_events` contains additional
    exact-deadline sources such as Ssi0Dma. They share the timers' absolute
    instruction clock but retain independent checkpoint components; the
    legacy Timers checkpoint topology is not changed.

    A `Pits` holds its deadlines as absolute instruction counts, and `done`
    here restarts at zero on every call, so successive calls with the same
    `Pits` resume from `pits.now` rather than rewinding the clock. Without
    that, a caller that spins in a loop -- the GUI does -- gets timer ticks
    during its first call and silence afterwards, because every deadline is
    already in the past-that-is-now-the-future.

    In `pits` mode `instrs` is a floor, not a ceiling: the loop finishes the
    deadline step it is on, so it returns having executed up to one timer
    period more than asked. That is deliberate. It makes one call of N
    instructions and ten calls of N/10 execute the identical instruction
    stream, which is what lets the GUI and the measurement harness agree.

    Accounting is of what actually executed, not what was requested. When a
    vector has no handler, `install_exceptions` stops the run from inside
    the hook: emu_start returns normally, having executed nothing, and a
    loop that credits itself the full step races to the instruction budget
    in seconds and reports a run that never happened. PC zero is Unicorn's
    known end sentinel: a normal return there reports ``pc zero`` and credits
    none of the incomplete step (a conservative lower bound), and does not
    service timers.

    tick=True injects a vector-32 (scheduler) trap at every chunk boundary.
    That is NOT what the hardware does and NOT what dspboot.run does -- it
    forces a reschedule in the middle of whatever code happens to be running,
    and a resumed run then diverges from the run that produced the snapshot
    (the init task never reaches its own flag test at 0x400cf384). Ticking
    idle spins, which build() does, is the faithful mechanism. Left available
    only for deliberate "shake it and see" experiments.

    Every chunk boundary costs a `count=` argument to emu_start. That is far
    more expensive than it looks: measured on this machine with every hook
    installed, counted execution runs at 2.0M instructions a second against
    15.5M uncounted, a 7.6x tax, and a cProfile of the running configuration
    puts 99.5% of wall time inside emu_start. An older note here put the cost
    at ~1.8x; that was wrong.

    `fast=True` buys that back with `_FastStepper`, which bounds each step
    with a block hook instead. It is opt-in because it costs exactness: the
    instruction count becomes an estimate, and a timer fires up to one basic
    block late rather than on the instruction it was due. That changes the
    instruction stream and so the boot digest. Use it for interactive running
    -- the GUI does -- and never for a determinism or pass/fail claim.
    """
    deferred = getattr(m, '_checkpoint_deferred_restore', None)
    if deferred is not None:
        deferred.require_claimed()
    if async_events and pits is None:
        raise ValueError("async event sources require the shared timer clock")
    # Sources build() attached to the machine itself (the Digitone's DSP
    # link) run on the same clock, so every caller that steps with timers
    # services them without having to know they exist.
    own = getattr(m, 'async_sources', ())
    if own and pits is not None:
        async_events = tuple(async_events) + tuple(own)
    done, stop = 0, 'limit'
    base = pits.now if pits is not None else 0      # resume, do not rewind
    while done < instrs:
        if pc == 0:
            stop = 'pc zero'
            break
        # No `remaining` in pits mode: run the whole deadline step and
        # overshoot `instrs` rather than truncating, so every emu_start
        # boundary is a timer deadline no matter how the caller splits its
        # budget. See Pits.step.
        if pits is not None:
            steps = [pits.step(base + done, None)]
            steps.extend(
                event_step
                for event in async_events
                if (event_step := event.step(base + done, None)) is not None
            )
            step = min(steps)
        else:
            step = min(chunk, instrs - done)
        m.halt_vec = None
        try:
            if fast:
                executed = _fast_stepper(m).run(pc, step)
            else:
                m.uc.emu_start(pc, 0, count=step)
                executed = step
        except UcError as e: stop = str(e); break
        pc = m.uc.reg_read(UC_M68K_REG_PC)
        if m.halt_vec is not None:
            stop = 'unhandled vector %d at %#010x' % (m.halt_vec, pc)
            break
        if pc == 0:
            stop = 'pc zero'
            break
        # What actually ran, which in fast mode is an estimate and is not
        # exactly `step`. In counted mode the two are identical, so the
        # default path's accounting is unchanged.
        done += executed
        if pits is not None:
            pits.now = base + done
            for event in async_events:
                event.service(base + done)
            pits.service(base + done)
            # raise_vector moves PC. Resuming at the stale one leaves the
            # exception frame stranded on the stack: the next rts pops it as
            # a return address and jumps to nowhere. Cost a session once, as
            # a vector-4 fault exactly one timer tick after the first.
            pc = m.uc.reg_read(UC_M68K_REG_PC)
        if on_chunk:
            on_chunk(pc, done)
            pc = m.uc.reg_read(UC_M68K_REG_PC)
        if tick and (m.uc.reg_read(UC_M68K_REG_SR) & 0x0700) != 0x0700:
            m.raise_vector(32); pc = m.uc.reg_read(UC_M68K_REG_PC)
    return pc, done, stop


def main(snapshot, instrs, chunk=500_000, send=b'', unblock=False, fast=False):
    m, ev, st, pc, inq, at = build(snapshot, send, unblock=unblock,
                                   softfloat=fast, bitmap=fast)
    t0 = time.time()

    def note(pc_, done):
        if done % 250_000_000 == 0:
            print('  .. %dM instrs, %.1fs, tasks=%d prints=%d setPixel=%d'
                  % (done//1_000_000, time.time()-t0, len(ev['tasks']),
                     len(ev['prints']), ev['setpixel']), flush=True)

    pc, done, stop = spin(m, pc, instrs, chunk, on_chunk=note)
    return m, ev, done, time.time()-t0, stop


if __name__ == '__main__':
    snap = sys.argv[1]; n = int(sys.argv[2])
    send = (sys.argv[3]+'\r\n').encode() if len(sys.argv) > 3 else b''
    m, ev, done, dt, stop = main(snap, n, send=send,
                                 unblock=bool(os.environ.get('UNBLOCK')),
                                 fast=bool(os.environ.get('FAST')))
    print('\n=== %d instrs in %.0fs (%.2fM/s) stop=%s ===' % (done, dt, done/dt/1e6, stop))
    print('new tasks : %d' % len(ev['tasks']))
    print('prints    : %d' % len(ev['prints']))
    print('setPixel  : %d   px_copy_to_bitmap: %d' % (ev['setpixel'], ev['pxcopy']))
    print('distinct TCBs scheduled: %d   pends satisfied: %d'
          % (len(ev['switch']), ev['satisfied']))
    print('uart out  : %r' % bytes(ev['uart_out'])[:200])
    w,h,buf = struct.unpack('>III', m.uc.mem_read(0x4028ae98, 12))
    px = bytes(m.uc.mem_read(buf, w*h))
    print('intro framebuffer 0x%08x nonzero: %d/%d' % (buf, sum(1 for b in px if b), w*h))
# fmt: on
