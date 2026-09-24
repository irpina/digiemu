"""DSP bring-up experiment: extends flashboot.py with instrumentation for the
ColdFire<->SHARC transport at 0x40128c7c and task_create/task_start tracking.

Key finding (see docs/FINDINGS.md update): 0x40128c7c is NOT "send arg, wait for
reply" in the sense of an RPC round trip with payload -- it is a
lock-mutex / kick-transfer / wait-on-completion-semaphore / unlock-mutex wrapper,
and the argument pushed by every one of the 4 call sites (0xF4240, 0x3E8, 0x64,
0x2DC6C0 -- 1000000, 1000, 100, 3000000) is a **microsecond timeout**, written
into a hardware register at 0xFC074004 that is *not* read back by the ColdFire
side afterwards. The real completion signal is a semaphore object at 0x44e4d69c:

    0x40128c7c  lock mutex @0x44e4d6a4                (400015a0)
                (first call only: install ISR @0x40128c4c at vector 97,
                 enable INTC sources 0x21/0x1d, init semaphore @0x44e4d69c to 0)
                write timeout -> 0xFC074004
                write control word 0x841b -> 0xFC074000   (kicks the transfer)
                jsr 0x4000141a  (sem_pend on 0x44e4d69c)  <-- BLOCKS HERE
                unlock mutex (tail call into 0x400016d2)

    0x40128c4c  (the completion ISR the real hardware would fire)
                ack/clear @0xFC074003, clear @0xFC074000, sem_post(0x44e4d69c)
                via 0x4000155c -> 0x400011ee

0x4000141a (sem_pend) has a **fast, non-blocking path**: if the semaphore's
count field (offset 0) is > 0, it clears it and returns immediately without
ever calling the scheduler (`trap #0`). Also, critically, its return value in
D0 is *discarded* by every one of the 4 callers (D0 is immediately overwritten
right after the call) -- so callers never inspect a "successful transfer"
value from 40128c7c itself, only from other paths (checked separately below).

So instead of firing interrupts (tried before, ~330 addrs gained -- likely
because it raced the scheduler / hit paths that expect real ISR side effects),
this patches the semaphore field directly, right before the pend call, so the
fast path is taken. No interrupt dispatch, no scheduler re-entry, no rte.

Patch point: PC == 0x40128d08 (the `jsr $4000141a` instruction itself) ->
write 1 into the 4 bytes at 0x44e4d69c before letting it execute.
"""
import struct, sys, os, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unicorn import UcError, UC_HOOK_CODE
from unicorn.m68k_const import (UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR,
                                 UC_M68K_REG_D0, UC_M68K_REG_D2, UC_M68K_REG_D3)
from emu.harness import Machine, VBR
from dt2.container import container
from emu import config, symbols

MAIN_LOAD, ENTRY = 0x40000400, 0x400004e8
FLASH_READ = 0x401296fe
SLOT = 0x80000
HALT = 0x400ceeb6

# Generalization of HALT: the RTOS idiom for "nothing to do, wait for the
# scheduler's timer tick to preempt me" is a literal self-branch,
# `bra.b $self` (opcode 0x60FE) -- confirmed HALT (0x400ceeb6, the init
# task's idle loop) is exactly this shape. Found a SECOND one blocking
# progress at 0x400cf3e0 (a different task/thread's idle point, reached
# right after it creates 4 more tasks at prio 5/6/7/8) that our original
# single-address HALT hook never fed a timer tick to.
#
# CORRECTION (session 3): 0x400cf3e0 is NOT another task's idle point. It is
# where the prio-1 init task parks after finishing, reached by the `bra.b` at
# 0x400cf3f4 that ends its main loop. Ticking it changes nothing -- it is a
# *ready* task at priority 1 and the scheduler correctly keeps picking it. It
# sits there because everything above it is blocked, which before the
# raise_vector trap-frame fix meant everything, forever. See docs/FINDINGS.md,
# "Making the emulator actually run". Scanning for 0x60FE is still the right
# generalisation; the reading of this particular address was wrong.
# Scanning MAIN OS for every occurrence of this opcode and feeding all of
# them ticks (not just the one instance anyone happened to trip over first)
# should generalize past this whole class of blocker in one shot.
def find_idle_spins(image, load_addr):
    return [load_addr + off for off in range(0, len(image) - 1, 2)
            if image[off] == 0x60 and image[off + 1] == 0xFE]

TRANSPORT = 0x40128c7c
PEND_CALL = 0x40128d08          # `jsr 0x4000141a`  (sem_pend on the completion sem)
COMPLETION_SEM = 0x44e4d69c     # the semaphore 40128c4c (ISR) would post to
CALL_SITES = [                  # the 4 call sites into TRANSPORT, and their timeout arg
    (0x400cf000, 0x0F4240),
    (0x400cf928, 0x0003E8),
    (0x400cfd8a, 0x000064),
    (0x4012d46e, 0x2DC6C0),
]

TASK_CREATE = 0x400012c8
TASK_START = 0x40001314
# all 16 static call sites into TASK_CREATE, found by scanning MAIN OS for the
# absolute-long operand of `jsr $400012c8.l` (opcode 0x4EB9 immediately before it)
# NOTE: these are the `jsr` OPCODE addresses (2 bytes before the absolute-long
# operand found by scanning for the 0x400012c8 byte pattern -- the opcode
# 0x4EB9 sits immediately before it). The code hook fires on opcode addresses;
# using the operand address here means the hook is dead code -- caught by
# checking task_create_hits against the docs' known count of 4 in baseline.
TASK_CREATE_SITES = [
    0x40001148, 0x40002c22, 0x40032a20, 0x400ced72, 0x400ceea0, 0x400cf40c,
    0x400d3a42, 0x400f1a6c, 0x401131d2, 0x401135a8, 0x4011fbf8, 0x40125fde,
    0x40127960, 0x401279e8, 0x40127a7c, 0x4014638e,
]

# --- depack copy-loop safety valve -----------------------------------------
# There is a second, unrelated aPLib-style depacker embedded in MAIN OS at
# 0x4012ab70 (distinct from the bootstrap's 0x80000432, and from the flash
# section-table format entirely -- this one runs at DSP-bring-up time on
# in-memory buffers). One invocation, call site 0x400cf2d2 (source a static
# address *inside the already-loaded MAIN OS image itself*, 0x402489b4 --
# not flash- or DSP-reply-dependent data), decodes a match/copy token whose
# length comes out enormous (into the millions) and the resulting byte-copy
# loop at 0x4012acb8-0x4012acbe never terminates within any reasonable
# instruction budget.
#
# Verified NOT a Unicorn MVZ/MVS decode bug (see docs/FINDINGS.md and the
# session log): tested mvz.b/mvs.b in isolation, register and (a0)/(a0)+
# addressing, all matched expected 68k semantics exactly. The bad length is
# either a real bug/edge case in this second depacker's gamma-code decode
# that has never been exercised on hardware with this input, or (more likely)
# this call is not meant to be reached via the code path our semaphore patch
# takes -- i.e. a knock-on effect of *skipping* the real scheduler yield
# rather than a hardware-response question at all.
#
# Rather than fully reverse the gamma2 length code to chase that down, this
# clamps the copy count if it is absurdly large right where the loop reads
# it (0x4012acb8, after the final `sub.l d0,d2` adjustment) -- WITHOUT
# touching the bit-reader (d1) or source pointer (a0) state, so every other
# token in the stream still gets parsed from the correct bitstream position.
# Only this one token's *output* is truncated/corrupted; the parse continues.
DEPACK_COPY = 0x4012acb8
DEPACK_LEN_CAP = 0x10000   # 64K; real match lengths in this data are tens-hundreds of bytes


CALL_SITES_MAP = dict(CALL_SITES)
TASK_CREATE_SET = set(TASK_CREATE_SITES)
# NOTE: idle-spin addresses (HALT and friends) are found dynamically inside
# run() from the actual image and are NOT included here -- the slow (fast=False)
# path below computes its own HOT_ADDRS per-call to include them.
HOT_ADDRS_BASE = ({FLASH_READ, DEPACK_COPY, PEND_CALL, TASK_START} |
                   set(CALL_SITES_MAP) | TASK_CREATE_SET)


def build_flash(syx_path, size=0x1000000):
    flash = bytearray(size)
    c = container(syx_path)
    flash[SLOT:SLOT + len(c)] = c
    return bytes(flash)


def run(syx_path, main_img, limit=120_000_000, tick_vec=32, tick_every=20000,
        patch_sem=True, patch_depack=True, verbose=False, stall_window=3_000_000,
        extra_hook=None, fast=True, resume_from=None, machine_out=None,
        pre_start=None, sdgate=True, esdhc=True, coverage=True, ddr=None,
        handoff=None):
    """resume_from: path to a snapshot (see emu/snapshot.py). Loads registers
    and memory instead of starting at ENTRY, but installs the *same* hooks, so
    a resumed run behaves identically to the equivalent straight run. Without
    that the resumed run would miss flash HLE, the semaphore patch and the
    scheduler tick, and silently diverge.
    sdgate: install emu.gpio.SdGate, modelling the GPIO loopback the cold-boot
    continuity check reads. esdhc: install emu.esdhc.Esdhc behind it, using
    profile.sd_status as its drv_status (None if unresolved, in which case
    Esdhc falls back to its own module default). Both default to True: without
    them the firmware's SD bring-up never runs, the storage-ready flag stays
    0, and every block-storage read returns -1. With them on, both builds
    reach MAIN_OS_RUNNING under tools/bootcheck.py --verify -- Digitone's
    display module initialises for the first time, and Digitakt's cold boot
    creates 9 tasks instead of 5, including the priority-6 Main OS task at
    entry 0x40032f5a. Digitakt reaches MAIN_OS_RUNNING both with and without
    them, so turning them on does not regress the previously-working build.
    Pass sdgate=False and/or esdhc=False for the old unmodelled-storage
    behaviour.
    machine_out: if given, receives 'm' (the Machine) and 'st' (the stats
    dict) before emu_start is called, so a pre_start hook can see both.
    coverage=False (fast path only) leaves out the global per-instruction
    hook that counts instructions and records coverage: st['n'], 'seen',
    'curve' and 'stall_pcs' then stay empty and extra_hook is never called.
    That hook is a Python call on every guest instruction; without it the
    cold boot runs about ten times faster. Nothing else depends on it.
    limit=None sets the machine up and returns without running it (stop
    'not started'); machine_out also receives 'start_pc' then, so the caller
    can drive emu_start itself (emu.bootstrap.cold_boot does, under a native
    block budget)."""
    """fast=True (default): FF1/MOVEC and every HOT_ADDRS side effect are
    registered as per-address Unicorn hooks (begin=end=addr) instead of one
    global UC_HOOK_CODE that runs Python on every instruction and then
    branches. Only instruction counting / coverage tracking (which
    inherently needs to see every instruction) stays global, and is kept as
    small as possible. See harness.install_isa_patches_scoped's docstring.
    fast=False keeps the original single-global-hook implementation, useful
    to cross-check the two give identical results.
    """
    # Resolve every address this run needs from the image itself, instead of
    # the module-level constants above (which stay put as the Digitakt
    # reference -- other modules, e.g. console.py and fastrun.py, still read
    # them directly and are unaffected by this). See emu/symbols.py. This is
    # what lets a second firmware (different addresses, same RTOS) run here
    # instead of failing silently on every hook.
    profile = symbols.resolve(main_img, load_addr=MAIN_LOAD)

    flash = build_flash(syx_path)
    # ddr: decode the SDRAM space as the device does (Machine.set_ddr).
    m = Machine(ddr=ddr)
    st = {
        'n': 0, 'seen': set(), 'reads': [], 'spin': 0, 'curve': [],
        'transport_calls': [], 'sem_kicks': 0, 'depack_clamps': 0,
        'task_create_hits': {}, 'task_start_hits': {},
        'stall_pcs': collections.Counter(), 'last_new_n': 0,
        'spin_by_addr': collections.Counter(), 'idle_spins_found': [],
    }

    # -- side-effect handlers, shared between fast/slow paths ---------------
    def do_flash_read(uc):
        sp = uc.reg_read(UC_M68K_REG_A7)
        ret, off, ln, dest = struct.unpack('>IIII', uc.mem_read(sp, 16))
        if ln and dest and off + ln <= len(flash):
            for p in range(0, ln + 0x100000, 0x100000):
                m.ensure(dest + p)
            uc.mem_write(dest, flash[off:off + ln])
            st['reads'].append((off, ln, dest))
        uc.reg_write(UC_M68K_REG_D0, 0)
        uc.reg_write(UC_M68K_REG_A7, sp + 4)
        uc.reg_write(UC_M68K_REG_PC, ret)

    def do_transport_call(addr):
        st['transport_calls'].append((st['n'], addr))
        if verbose:
            # CALL_SITES_MAP is Digitakt-specific reference data (the known
            # timeout argument at each of its 4 sites) kept only for this
            # print; a resolved-but-different-firmware call site just prints
            # without one rather than a KeyError.
            timeout = CALL_SITES_MAP.get(addr)
            print('  [n=%d] transport call site 0x%08x timeout=%s' %
                  (st['n'], addr, '0x%x' % timeout if timeout is not None else '?'))

    def do_depack_copy(uc):
        d2 = uc.reg_read(UC_M68K_REG_D2)
        if d2 > DEPACK_LEN_CAP:
            st['depack_clamps'] += 1
            uc.reg_write(UC_M68K_REG_D2, 1)
            if verbose:
                print('  [n=%d] clamped depack copy length 0x%x -> 1 (clamp #%d)' %
                      (st['n'], d2, st['depack_clamps']))

    def do_pend_call(uc):
        uc.mem_write(profile.completion_sem, struct.pack('>I', 1))
        st['sem_kicks'] += 1
        if verbose:
            print('  [n=%d] satisfied completion sem @0x%08x (kick #%d)' %
                  (st['n'], profile.completion_sem, st['sem_kicks']))

    def do_task_create(uc, addr):
        if addr in st['task_create_hits']:
            return
        sp = uc.reg_read(UC_M68K_REG_A7)
        tcb, entry, prio, stack, ssize = struct.unpack('>IIIII', uc.mem_read(sp, 20))
        st['task_create_hits'][addr] = dict(tcb=tcb, entry=entry, prio=prio,
                                             stack=stack, stacksize=ssize, n=st['n'])
        print('  [n=%d] TASK_CREATE site 0x%08x -> entry=0x%08x prio=%d tcb=0x%08x stack=0x%08x size=0x%x' %
              (st['n'], addr, entry, prio, tcb, stack, ssize))

    def do_task_start(uc):
        sp = uc.reg_read(UC_M68K_REG_A7)
        (tcb,) = struct.unpack('>I', uc.mem_read(sp + 4, 4))
        st['task_start_hits'].setdefault(tcb, st['n'])

    def do_halt(addr):
        st['spin'] += 1
        st['spin_by_addr'][addr] += 1
        if st['spin'] % tick_every == 0:
            m.raise_vector(tick_vec)

    idle_spins = set(find_idle_spins(main_img, MAIN_LOAD))
    st['idle_spins_found'] = sorted(idle_spins)

    # task_create_sites and call_sites are diagnostic only (do_task_create and
    # do_transport_call only ever record/print; neither touches a register or
    # memory), so an unresolved OPTIONAL profile symbol just means fewer
    # hooks installed here, not a degraded run -- see emu/symbols.py.
    task_create_sites = set(profile.task_create_sites or ())
    call_sites = set(profile.call_sites or ())

    if fast:
        # lightweight, GLOBAL: only counting + coverage (must see every insn)
        def cover(uc, addr, size, data):
            st['n'] += 1
            if addr not in st['seen']:
                st['seen'].add(addr)
                st['last_new_n'] = st['n']
            elif st['n'] - st['last_new_n'] > stall_window:
                st['stall_pcs'][addr] += 1
            if st['n'] % 10_000_000 == 0:
                st['curve'].append((st['n'] // 1_000_000, len(st['seen'])))
            if extra_hook:
                extra_hook(uc, addr, size, st)
        if coverage:
            m.uc.hook_add(UC_HOOK_CODE, cover)
        m.install_isa_patches_scoped(main_img, MAIN_LOAD)

        def scoped(addr, fn):
            m.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, d: fn(uc), begin=addr, end=addr)
        scoped(profile.flash_read, do_flash_read)
        for site in call_sites:
            m.uc.hook_add(UC_HOOK_CODE, (lambda s: lambda uc, a, sz, d: do_transport_call(s))(site),
                          begin=site, end=site)
        scoped(profile.depack_copy, lambda uc: patch_depack and do_depack_copy(uc))
        scoped(profile.pend_call, lambda uc: patch_sem and do_pend_call(uc))
        for site in task_create_sites:
            m.uc.hook_add(UC_HOOK_CODE, (lambda s: lambda uc, a, sz, d: do_task_create(uc, s))(site),
                          begin=site, end=site)
        m.uc.hook_add(UC_HOOK_CODE, lambda uc, a, s, d: do_task_start(uc),
                      begin=profile.task_start, end=profile.task_start)
        for spin_addr in idle_spins:
            m.uc.hook_add(UC_HOOK_CODE, (lambda a: lambda uc, ax, sz, d: do_halt(a))(spin_addr),
                          begin=spin_addr, end=spin_addr)
    else:
        hot_addrs = ({profile.flash_read, profile.depack_copy, profile.pend_call, profile.task_start} |
                     call_sites | task_create_sites | idle_spins)

        def extra(uc, addr, size):
            st['n'] += 1
            if addr not in st['seen']:
                st['seen'].add(addr)
                st['last_new_n'] = st['n']
            elif st['n'] - st['last_new_n'] > stall_window:
                st['stall_pcs'][addr] += 1
            if st['n'] % 10_000_000 == 0:
                st['curve'].append((st['n'] // 1_000_000, len(st['seen'])))

            if addr not in hot_addrs:
                if extra_hook:
                    extra_hook(uc, addr, size, st)
                return

            if addr == profile.flash_read:
                do_flash_read(uc)
                return
            if addr in call_sites:
                do_transport_call(addr)
            if addr == profile.depack_copy and patch_depack:
                do_depack_copy(uc)
            if addr == profile.pend_call and patch_sem:
                do_pend_call(uc)
            if addr in task_create_sites:
                do_task_create(uc, addr)
            if addr == profile.task_start:
                do_task_start(uc)
            if addr in idle_spins:
                do_halt(addr)
            if extra_hook:
                extra_hook(uc, addr, size, st)
        m.install_isa_patches(extra_code_hook=extra)

    m.mmio[0xEC070004] = 0x0D000000
    m.mmio[0xFC05C02C] = 0x100000F0   # DSPI0 SR: RXCTR nonzero + RFDF (bit 0x1c)
    m.mmio[0xEC03802C] = 0x80000000   # secondary SPI/serial TX-done status (bit31)

    # The SD bring-up routine is guarded by a ten-iteration GPIO continuity
    # check (Digitone FUN_4011d604, Digitakt FUN_4011fe60, byte-identical)
    # that runs at roughly 25-30M instructions -- long before the first
    # snapshot rung at 60M. Enabling these models only on the emu/longrun.py
    # resume path is therefore useless: by the time any snapshot is restored
    # the decision has already been made and the flag is already zero.
    # Measured: a boot280M resume with sdgate=True, esdhc=True gets zero hits
    # on the guard and zero on the bring-up.
    #
    # Unmodelled GPIO reads zero, so the continuity check fails on its first
    # pass and the bring-up is skipped, in BOTH builds.
    #
    # emu/gpio.py's SdGate is the loopback the check is testing for; it is
    # useless without Esdhc behind it (the driver then spins on SYSCTL
    # INITA), so these two are meant to be turned on together.
    if sdgate:
        from emu.gpio import SdGate
        m.sdgate = SdGate(m)
    if esdhc:
        from emu.esdhc import Esdhc
        # cmd_sem/data_sem are per-image for the same reason drv_status is.
        # dma_sem too: CMD18 waits on it (sd_flag+0x3C) BEFORE it waits on
        # data_sem, and Esdhc posts it at bulk-transfer completion -- but only
        # if it was given one. Leaving it None makes every multi-block read
        # hang after the model has already served the data, which looks like
        # a storage failure and is really just an unposted semaphore.
        m.esdhc = Esdhc(m, drv_status=profile.sd_status,
                        cmd_sem=profile.sd_cmd_sem,
                        data_sem=profile.sd_data_sem,
                        dma_sem=profile.sd_dma_sem,
                        capacity_addr=getattr(profile, 'sd_capacity', None))

    m.install_mmio()
    m.install_exceptions()
    if resume_from:
        from emu.snapshot import restore_into
        start_pc = restore_into(m, resume_from, st)
    else:
        m.load(main_img, MAIN_LOAD)
        m.ensure(0x40800000)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        m.uc.reg_write(UC_M68K_REG_A7, 0x40800000)
        start_pc = profile.entry
        if handoff is not None:
            # Start where the real bootstrap left the device instead
            # (emu/bootrom.py): its stack and boot flags, SRAM, controller
            # setup. MAIN OS in DDR is the same bytes either way.
            from emu import bootrom
            start_pc = bootrom.apply_handoff(m, bootrom.load_handoff(handoff))
    if machine_out is not None:
        machine_out['m'] = m
        # st['n'] is the live instruction counter -- exposing it here lets a
        # pre_start hook timestamp itself against it while the run is in
        # progress, not just after emu_start returns.
        machine_out['st'] = st
        machine_out['start_pc'] = start_pc
    if pre_start:                 # add extra Unicorn hooks before execution starts
        pre_start(m)
    if limit is None:
        # count=0 would mean "no limit" to Unicorn, so there is no way to
        # ask emu_start for nothing: set up only, the caller runs it.
        return m, st, 'not started'
    try:
        m.uc.emu_start(start_pc, 0, count=limit)
        stop = 'instruction limit'
    except UcError as e:
        stop = str(e)
    return m, st, stop


if __name__ == '__main__':
    syx = config.firmware()
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 120_000_000
    patch = (sys.argv[2] != '0') if len(sys.argv) > 2 else True
    img = open(config.main_image(), 'rb').read()
    # run() resolves the same profile internally (cached by image SHA-256),
    # so this is a second lookup, not a second scan -- see emu/symbols.py.
    # It is only needed here for the site COUNT printed below, which must
    # match whichever firmware was actually loaded, not the Digitakt
    # TASK_CREATE_SITES module constant.
    n_task_create_sites = len(symbols.resolve(img).task_create_sites or ())
    m, st, stop = run(syx, img, limit=limit, patch_sem=patch, verbose=True)
    print('=' * 70)
    print('instructions      : %d' % st['n'])
    print('distinct addrs    : %d' % len(st['seen']))
    print('stopped           : %s (pc=0x%08x)' % (stop, m.uc.reg_read(UC_M68K_REG_PC)))
    print('FF1 emulated      : %d   MOVEC emulated: %d' % (m.ff1_count, m.movec_count))
    print('transport calls   : %d   sem satisfies: %d   depack clamps: %d' %
          (len(st['transport_calls']), st['sem_kicks'], st['depack_clamps']))
    print('idle spins found  : %d  ->  %s' %
          (len(st['idle_spins_found']), [hex(a) for a in st['idle_spins_found']]))
    print('idle spins hit    : %s' %
          {hex(a): c for a, c in st['spin_by_addr'].items()})
    print('task_create hit   : %d / %d' % (len(st['task_create_hits']), n_task_create_sites))
    for a, info in st['task_create_hits'].items():
        print('    site=0x%08x  entry=0x%08x  prio=%-3d  tcb=0x%08x  n=%d' %
              (a, info['entry'], info['prio'], info['tcb'], info['n']))
    print('task_start hits   : %d' % len(st['task_start_hits']))
    print('curve:', '  '.join('%dM:%d' % c for c in st['curve']))
    print('stall pcs:', [(hex(a), c) for a, c in st['stall_pcs'].most_common(15)])
