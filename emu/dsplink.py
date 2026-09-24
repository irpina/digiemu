"""The Digitone's second CPU: its link to the main one, and the CPU itself.

The Digitone (mk1) has two ColdFire MCF5441x CPUs. The main one runs section
3 (the MAIN OS: UI, sequencer, +Drive, and the mixer, effects and audio out);
the other runs section 7 and renders the FM voices, and the firmware's own
strings call it the DSP ("DSP BOOT FAILURE"). They share a 128 KB dual-port
RAM -- the main CPU sees it on FlexBus at 0x10000000, the DSP has its chip
select 0 at 0x00000000 -- and each interrupts the other through a GPIO wired
to the other's input:

  * main PB6 is the DSP's reset (low holds it);
  * DSP PG2 -> the main edge port, pin 4 (INTC0 source 4, vector 68), both
    edges;
  * main PA4 -> the DSP's DMA timer 0 input, which captures any edge
    (vector 96).

BOOT. The main CPU boots the DSP from a task of its own (`dsp_boot_task` in
emu/symbols.py, 0x4008d56c in OS 1.43): it holds the reset, streams section
6 (the DSP's serial-boot loader) and section 7 out of its DSPI2, set up as an
SPI slave the DSP's serial boot clocks from, and releases the reset. Section
6 loads section 7 at 0x40000400 and jumps to it (0x40000b92). Section 7 sets
up its FlexBus, configures an FPGA over its DSPI1, clears the shared RAM and
shakes hands through it:

  * the DSP leaves 'HO' at +0 and 'HA' at +2 and toggles PG2; the main
    CPU's handler (0x4008d850) answers 1 at +2, then 'B0' at +0;
  * the DSP writes 0xA5A5 at +4 and toggles PG2 again; the handler marks it
    running (status 2), and the main CPU starts its audio blocks.

The status word (`dsp_status`) is 0 while a boot is in progress. It becomes
1, which the main task shows as "DSP BOOT FAILURE", when section 6 or 7 is
missing, or when the boot task's watchdog runs out on the third attempt.

RUNNING. Everything the DSP does after that is one interrupt: each 32-frame
audio block, the interrupt of the SSI1 transmit DMA (eDMA channel 54, vector
174, 0x4009c2e4) toggles PA4 through 0x4008d82a, and the DSP's DMA-timer
capture interrupt (vector 96) runs the render at 0x400008fa. Vector 96 holds
a handshake-phase handler (0x40000406) until the handshake is done, and then
a trampoline section 7 builds on its stack, which passes the shared RAM's
base (0) and jumps to 0x400008fa. The render copies the voice parameters the
main CPU left at +0x000..+0x39F into its SRAM, renders eight voices of 32
samples (signed Q1.31, with the EMAC) and copies them to +0x3A0..+0x79F. The
main CPU's render (vector 191) runs eDMA channel 47 twice a block, through
SSRT: the voices in, and the next parameters out. It mixes the voices with
its own effects into the SSI1 output. The main CPU never waits on the DSP
after boot.

Two models, same main-side wiring:

  DspCpu   runs section 7 on a second Unicorn engine (emu/harness.Machine),
           with the shared RAM mapped into both engines from one host
           buffer. Its boot runs in slices at the main CPU's step
           boundaries; after it, each PA4 edge becomes its vector 96 and it
           runs to its idle loop (0x40000b90) -- a render is ~18k
           instructions, ~0.2 ms on the reference desktop (~0.35 ms beside
           the main CPU), a third to a half of real time at 1500 blocks a
           second. Batch runs render at the next step boundary, so they are
           deterministic; the live window calls start_thread() and renders
           run on a thread of their own, in parallel with the main CPU
           (ctypes lets go of the GIL inside Unicorn), which is what keeps
           the Digitone at real time with audio -- ~80% of a second core,
           one render per block. Section 6 is not run: nothing
           in section 7 depends on the state it leaves beyond the load (the
           reset values, and section 7 sets its own stack first thing). The
           FPGA answers only what section 7 waits for: INIT_B (PE0) and DONE
           (PF7) high, and PIT1's flag for its delay loop.
  DspLink  a stand-in for the handshake alone, for when section 7 is not
           there: the main OS runs as on a Digitone whose DSP came up, with
           no synth voices.

Why the request semaphore is never faked: the main CPU posts it through
0x400019a0, a third post routine (the RTOS's post with interrupts masked)
that emu/semscan.py does not scan, so the scan cannot see that it has a
poster. Faked, the boot task re-uploads the DSP thousands of times a second.
"""
import collections
import ctypes
import struct
import threading
import zlib
from time import perf_counter as _clock

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_PROT_ALL
from unicorn.m68k_const import (UC_M68K_REG_A7, UC_M68K_REG_PC,
                                UC_M68K_REG_SR)

from emu.pit import PENDING_STEP, interrupt_level

WINDOW = 0x10000000             # the shared RAM, main CPU side (FlexBus)
DSP_WINDOW = 0x00000000         # the same RAM, DSP side (its FlexBus CS0)
SHARED_BYTES = 0x20000          # 128 KB; mapped as one 1 MB page each side
EPORT = 0xFC090000
EPFR = EPORT + 6                # edge port flag register (write 1 to clear)
EPORT_PIN = 4                   # the DSP's line into the main CPU
VECTOR = 64 + EPORT_PIN         # INTC0 source 4
GPIO = 0xEC094000
RESET_SET = GPIO + 0x19         # main PPDSDR_B: writing bit 6 releases reset
RESET_CLR = GPIO + 0x25         # main PCLRR_B: a 0 in bit 6 asserts it
RESET_BIT = 0x40
PA4_SET = GPIO + 0x18           # main PPDSDR_A / PCLRR_A, bit 4: the DSP's
PA4_CLR = GPIO + 0x24           # block clock
PA4_BIT = 0x10
PG2_SET = GPIO + 0x1E           # DSP PPDSDR_G / PCLRR_G, bit 2: the line
PG2_CLR = GPIO + 0x2A           # into the main CPU
PG2_BIT = 0x04

HO, HA, B0, RUNNING = 0x484F, 0x4841, 0x4230, 0xA5A5

# The DSP. Section 7 is linked at 0x40000400 (its first word, the entry,
# says so: 0x40000b92), its VBR is 0x40000000 like the main CPU's, and once
# up it parks in `bra.b *` at 0x40000b90 between interrupts.
DSP_LOAD = 0x40000400
DSP_ENTRY = 0x40000b92
DSP_IDLE = 0x40000b90
DSP_VECTOR, DSP_LEVEL = 96, 3   # DTIM0 capture: ICR32 = 3
DSP_INITIAL_SP = 0x8000FFEC     # what section 6 leaves; section 7 resets it
DSP_PAGES = (0x40000000, 0x47F00000, 0x80000000)
# Reads section 7 waits on before it will shake hands (VERIFIED by removing
# each in turn: without any one it retries for ever): PIT1's PIF for its
# delay loop, and the FPGA's INIT_B on PE0 and DONE on PF7.
DSP_STUBS = ((0xFC084000, 2, 0x0004), (0xEC09401C, 1, 0x01),
             (0xEC09401D, 1, 0x80))
PER_BLOCK = 3.87                # instructions per block, as longrun's
BOOT_SLICE = 200_000            # instructions per step while it boots
RENDER_CAP = 2_000_000          # a render that has not idled by then is stuck

BOOT_DELAY = 1_000_000          # DspLink: reset release -> 'HO'/'HA'
REPLY_DELAY = 50_000            # DspLink: 'B0' -> 0xA5A5

OFF, BOOTING, HELLO, READY = 'off', 'booting', 'hello', 'ready'
STATES = (OFF, BOOTING, HELLO, READY)


class _MainSide:
    """What both models share: the reset line and the DSP's interrupts
    into the main CPU, delivered on the main CPU's step clock like the other
    interrupt sources (emu/intfrc.py)."""

    def __init__(self, m, request_sem=None):
        self.m = m
        self.request_sem = request_sem
        self.pulses = 0             # edge-port pulses waiting for delivery
        self.now = 0
        self.fired = collections.Counter()
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_reset_release,
                      begin=RESET_SET, end=RESET_SET)
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_reset_assert,
                      begin=RESET_CLR, end=RESET_CLR)

    def _on_reset_release(self, uc, access, address, size, value, data):
        if value & RESET_BIT:
            self.fired['release'] += 1
            self.reset_released()

    def _on_reset_assert(self, uc, access, address, size, value, data):
        if not value & RESET_BIT:       # PCLRR clears the bits written as 0
            self.fired['reset'] += 1
            self.pulses = 0
            self.reset_asserted()

    def _pulse(self):
        self.pulses += 1
        flags = self.m.peek(EPFR, 1)[0]
        self.m.poke(EPFR, bytes([flags | (1 << EPORT_PIN)]))

    def _deliver(self):
        if not self.pulses:
            return False
        level = interrupt_level(self.m, VECTOR, respect_mask=False)
        if level is None:
            return False            # not armed yet: keep it pending
        sr = self.m.uc.reg_read(UC_M68K_REG_SR)
        if ((sr >> 8) & 7) >= level:
            return False
        if self.m.raise_vector(VECTOR, level=level):
            self.pulses -= 1
            self.fired['irq'] += 1
            return True
        return False

    def _pulse_step(self, remaining):
        return min(PENDING_STEP, remaining) if remaining is not None \
            else PENDING_STEP


class DspLink(_MainSide):
    """The DSP's half of the boot handshake alone, on the shared clock."""

    def __init__(self, m, request_sem=None):
        super().__init__(m, request_sem)
        self.state = OFF
        self.due = None             # instruction count of the next DSP action
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_window,
                      begin=WINDOW, end=WINDOW + 1)

    def reset_released(self):
        if self.state == OFF:
            self.state = BOOTING
            self.due = self.now + BOOT_DELAY

    def reset_asserted(self):
        self.state = OFF
        self.due = None

    def _on_window(self, uc, access, address, size, value, data):
        # The store has not landed yet; `value` is what the main CPU writes.
        if self.state != HELLO or address != WINDOW:
            return
        word = (value >> 16) & 0xFFFF if size == 4 else value & 0xFFFF
        if size >= 2 and word == B0:
            self.state = READY
            self.due = self.now + REPLY_DELAY
            self.fired['b0'] += 1

    # -- the event source protocol (emu.longrun.spin) ---------------------
    def step(self, done, remaining=None):
        self.now = done
        if self.pulses:
            return self._pulse_step(remaining)
        if self.due is None:
            return remaining
        left = max(1, self.due - done)
        return left if remaining is None else min(left, remaining)

    def service(self, done):
        self.now = done
        if self.due is not None and done >= self.due:
            self.due = None
            if self.state == BOOTING:
                self._write(0, HO)
                self._write(2, HA)
                self.state = HELLO
                self._pulse()
            elif self.state == READY:
                self._write(4, RUNNING)
                self._pulse()
        return self._deliver()

    def _write(self, off, word):
        self.m.poke(WINDOW + off, struct.pack('>H', word))

    # -- checkpoints ------------------------------------------------------
    def checkpoint_state(self):
        return {'type': 'DspLink', 'version': 1, 'state': self.state,
                'due': self.due, 'pulses': self.pulses, 'now': self.now,
                'fired': dict(self.fired)}

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'DspLink' or state.get('version') != 1:
            raise RuntimeError('unsupported DspLink checkpoint state')
        if state.get('state') not in STATES:
            raise RuntimeError('invalid DspLink state %r' % state.get('state'))
        due = state.get('due')
        if due is not None and (type(due) is not int or due < 0):
            raise RuntimeError('invalid DspLink deadline')
        pulses = state.get('pulses')
        if type(pulses) is not int or pulses < 0:
            raise RuntimeError('invalid DspLink pulse count')
        self.state = state['state']
        self.due = due
        self.pulses = pulses
        self.now = int(state.get('now') or 0)
        self.fired = collections.Counter(state.get('fired') or {})


class DspCpu(_MainSide):
    """The DSP itself: section 7 on a second engine. See the module
    docstring for the wiring and when it runs."""

    def __init__(self, m, section7, request_sem=None):
        super().__init__(m, request_sem)
        self.s7 = bytes(section7)
        self.dsp = None             # the DSP's Machine while out of reset
        self.pc = None
        self.idle = False           # parked in its idle loop
        self.booted = False         # has reached the idle loop since reset
        self.irqs = 0               # PA4 edges waiting to be taken
        self.pa4 = 0                # the level of each line
        self.pg2 = 0
        self.last = 0               # main count at the last service
        self.renders = 0
        self.dsp_instructions = 0
        self.wall = 0.0             # host seconds spent running the DSP
        self.edges = 0              # PG2 edges not yet turned into pulses
        self._budget = None
        # threaded: renders run on a thread of their own, alongside the
        # main CPU's emulation, as the two chips do. The live-audio window
        # turns it on (emu/gui.py); every other run steps the DSP in line,
        # so it is deterministic. See start_thread().
        self.threaded = False
        self._lock = threading.Lock()
        self._job = threading.Condition(self._lock)
        self._busy = False
        self._thread = None
        self._closing = False
        self.error = None           # a render the thread could not run
        # One host buffer, mapped into both engines: what either CPU writes
        # the other reads, as through the dual-port RAM. Mapped before the
        # snapshot is restored, which then fills it through this mapping.
        from emu.harness import PAGE
        self._page = PAGE
        self._buf = ctypes.create_string_buffer(PAGE)
        m.uc.mem_map_ptr(WINDOW, PAGE, UC_PROT_ALL,
                         ctypes.addressof(self._buf))
        m.mapped.add(WINDOW)
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_pa4,
                      begin=PA4_SET, end=PA4_SET)
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_pa4,
                      begin=PA4_CLR, end=PA4_CLR)

    # -- the DSP's machine ------------------------------------------------
    def _new_machine(self):
        from emu import native
        from emu.harness import Machine
        d = Machine()
        d.install_isa_patches_scoped(self.s7, DSP_LOAD)
        d.install_exceptions()
        native.enable_options(d.uc, native.NO_MEM_EXIT)
        d.uc.mem_map_ptr(DSP_WINDOW, self._page, UC_PROT_ALL,
                         ctypes.addressof(self._buf))
        d.mapped.add(DSP_WINDOW)
        for addr, size, _v in DSP_STUBS:
            d.uc.hook_add(UC_HOOK_MEM_READ, self._on_stub,
                          begin=addr, end=addr + size - 1)
        d.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_pg2,
                      begin=PG2_SET, end=PG2_SET)
        d.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_pg2,
                      begin=PG2_CLR, end=PG2_CLR)
        self._budget = (native.NativeBudget(d.uc)
                        if native.budget_available(d.uc) else None)
        return d

    def _on_stub(self, uc, access, address, size, value, data):
        for addr, n, v in DSP_STUBS:
            if addr <= address < addr + n:
                self.dsp.poke(addr, v.to_bytes(n, 'big'))

    def _on_pg2(self, uc, access, address, size, value, data):
        level = 1 if address == PG2_SET else 0
        if address == PG2_SET and not value & PG2_BIT:
            return
        if address == PG2_CLR and value & PG2_BIT:
            return
        if level != self.pg2:
            self.pg2 = level
            # Counted only: this can run on the DSP's thread, and the main
            # CPU's memory (its edge-port flag) is written on the main
            # thread when the pulse is delivered.
            with self._lock:
                self.edges += 1

    def _on_pa4(self, uc, access, address, size, value, data):
        if address == PA4_SET:
            if not value & PA4_BIT:
                return
            level = 1
        else:
            if value & PA4_BIT:
                return
            level = 0
        if level != self.pa4:
            self.pa4 = level
            if self.dsp is not None:
                # DTIM0's capture flag is one latch: edges that arrive while
                # the DSP cannot take them are one interrupt, not several.
                with self._lock:
                    self.irqs = 1
                self.fired['pa4'] += 1

    def reset_released(self):
        if self.dsp is not None:
            return
        self.wait_idle()
        d = self.dsp = self._new_machine()
        d.load(self.s7, DSP_LOAD)
        for page in DSP_PAGES:
            d.ensure(page)
        d.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        d.uc.reg_write(UC_M68K_REG_A7, DSP_INITIAL_SP)
        self.pc = DSP_ENTRY
        self.idle = False
        self.booted = False
        self.irqs = 0
        self.pg2 = 0
        self.fired['boot'] += 1

    def reset_asserted(self):
        self.wait_idle()
        self.dsp = None
        self._budget = None
        self.pc = None
        self.idle = False
        self.booted = False
        self.irqs = 0

    def _run(self, instructions):
        """Run the DSP for up to `instructions`, stopping at its idle loop."""
        uc = self.dsp.uc
        if self._budget is not None:
            self._budget.state.left = max(1, int(instructions / PER_BLOCK))
            self._budget.state.blocks = 0
            uc.emu_start(self.pc, DSP_IDLE)
            ran = int(self._budget.state.blocks * PER_BLOCK)
        else:
            uc.emu_start(self.pc, DSP_IDLE, count=instructions)
            ran = instructions
        self.pc = uc.reg_read(UC_M68K_REG_PC)
        self.idle = self.pc == DSP_IDLE
        if self.idle:
            self.booted = True
        self.dsp_instructions += ran
        return ran

    # -- the event source protocol (emu.longrun.spin) ---------------------
    def step(self, done, remaining=None):
        self.now = done
        if self.pulses or self.edges or (self.irqs and not self._busy):
            return self._pulse_step(remaining)
        if self.dsp is not None and not self.booted:
            # Still booting, or waiting for the main CPU's 'B0': give it
            # slices of time as the main CPU runs.
            return BOOT_SLICE if remaining is None \
                else min(BOOT_SLICE, remaining)
        return remaining

    def service(self, done):
        elapsed = max(0, done - self.last)
        self.last = self.now = done
        did = False
        d = self.dsp
        if d is not None and self.threaded and self.booted:
            # Hand a new edge to the render thread; a render in flight is
            # left to finish on its own.
            if self.irqs:
                did = self._render_async()
        elif d is not None and (self.irqs or not self.idle):
            t0 = _clock()
            did = self._service_dsp(d, elapsed)
            self.wall += _clock() - t0
        with self._lock:
            edges, self.edges = self.edges, 0
        for _ in range(edges):
            self._pulse()
        return self._deliver() or did

    def _service_dsp(self, d, elapsed, taken=False):
        """Take a pending PA4 edge if the DSP's mask allows, then run it.

        `taken`: the render thread's edge, which _render_async has already
        taken off `irqs` on the main thread."""
        if taken or self.irqs:
            sr = d.uc.reg_read(UC_M68K_REG_SR)
            if ((sr >> 8) & 7) < DSP_LEVEL:
                if not taken:
                    with self._lock:
                        self.irqs = 0
                if d.raise_vector(DSP_VECTOR, level=DSP_LEVEL):
                    self.pc = d.uc.reg_read(UC_M68K_REG_PC)
                    self.idle = False
                    self.fired['dsp_irq'] += 1
                    if self.booted:
                        self.renders += 1
            elif taken:
                with self._lock:
                    self.irqs = 1       # masked: back on the latch
        if self.idle:
            return False
        # Once up, the DSP only ever works in its interrupt: run it to its
        # idle loop now. While it boots it runs as long as the main CPU did.
        self._run(RENDER_CAP if self.booted
                  else max(1000, min(elapsed, BOOT_SLICE * 4)))
        return True

    # -- the DSP on a thread of its own -----------------------------------
    # Unicorn runs an engine without the interpreter lock (ctypes releases
    # it around emu_start), and the render calls into Python for nothing,
    # so a render on its own thread runs in parallel with the main CPU's
    # emulation -- measured, the DSP was 59% of a live run's host time done
    # in line. The main CPU reads block k's voices only after it has asked
    # for block k+1, so all a new render has to wait for is the last one.
    def start_thread(self):
        """Run renders on a thread from now on (the live-audio window)."""
        self.threaded = True
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker,
                                            name='digitone-dsp', daemon=True)
            self._thread.start()

    def _render_async(self):
        # The edge is taken off the latch HERE, on the main thread, not when
        # the worker wakes: an edge the main CPU makes before then is the
        # next block's, and leaving the first on the latch merged the two
        # into one render -- a skipped block, from nothing but the host's
        # thread wake-up time (the DSP takes each edge within microseconds).
        with self._job:
            while self._busy:
                self._job.wait()
            if self.dsp is None or not self.irqs:
                return False
            self.irqs = 0
            self._busy = True
            self._job.notify_all()
        return True

    def _worker(self):
        while True:
            with self._job:
                while not (self._busy or self._closing):
                    self._job.wait()
                if not self._busy:
                    return
            t0 = _clock()
            try:
                if self.dsp is not None:
                    self._service_dsp(self.dsp, 0, taken=True)
            except Exception as exc:                  # noqa: BLE001
                self.error = exc
                print('[dsp] render failed: %s' % exc, flush=True)
            with self._job:
                self.wall += _clock() - t0
                self._busy = False
                self._job.notify_all()

    def wait_idle(self):
        """Wait for a render in flight to finish (before a snapshot, a
        reset or teardown touches the DSP's engine)."""
        with self._job:
            while self._busy:
                self._job.wait()

    def close(self):
        """Stop the render thread. Safe to repeat."""
        with self._job:
            self._closing = True
            self._job.notify_all()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    # -- checkpoints ------------------------------------------------------
    def checkpoint_state(self):
        self.wait_idle()
        state = {'type': 'DspCpu', 'version': 1, 'on': self.dsp is not None,
                 'pulses': self.pulses, 'irqs': self.irqs, 'pa4': self.pa4,
                 'pg2': self.pg2, 'now': self.now, 'last': self.last,
                 'renders': self.renders, 'fired': dict(self.fired)}
        if self.dsp is None:
            return state
        from emu.snapshot import REGS
        d = self.dsp
        d.flush_pending()
        pages = {}
        for base in sorted(d.mapped):
            if base == DSP_WINDOW:
                continue            # the main CPU's snapshot has it
            data = bytes(d.uc.mem_read(base, self._page))
            if data.strip(b'\0'):
                pages[base] = zlib.compress(data, 6)
        state.update(
            pc=self.pc, idle=self.idle, booted=self.booted,
            regs={name: d.uc.reg_read(rid) for name, rid in REGS},
            pages=pages, mapped=sorted(b for b in d.mapped if b != DSP_WINDOW),
            ctlregs=dict(d.ctlregs), mmio=dict(d.mmio),
            ff1_count=d.ff1_count, movec_count=d.movec_count)
        return state

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'DspCpu' or state.get('version') != 1:
            raise RuntimeError('unsupported DspCpu checkpoint state')
        for key in ('pulses', 'irqs', 'now', 'last', 'renders'):
            if type(state.get(key)) is not int or state[key] < 0:
                raise RuntimeError('invalid DspCpu checkpoint field %s' % key)
        self.pulses, self.irqs = state['pulses'], state['irqs']
        self.pa4, self.pg2 = int(state['pa4']), int(state['pg2'])
        self.now, self.last = state['now'], state['last']
        self.renders = state['renders']
        self.fired = collections.Counter(state.get('fired') or {})
        if not state.get('on'):
            self.reset_asserted()
            return
        from emu.snapshot import REGS
        d = self.dsp = self._new_machine()
        for base in state['mapped']:
            d.ensure(base)
        for base, comp in state['pages'].items():
            d.uc.mem_write(base, zlib.decompress(comp))
        d.ctlregs.update(state['ctlregs'])
        d.mmio.update(state['mmio'])
        d.ff1_count = state['ff1_count']
        d.movec_count = state['movec_count']
        regs = state['regs']
        d.uc.reg_write(UC_M68K_REG_SR, regs['sr'])
        for name, rid in REGS:
            if name != 'sr':
                d.uc.reg_write(rid, regs[name])
        self.pc = state['pc']
        self.idle = bool(state['idle'])
        self.booted = bool(state.get('booted', self.idle))


def find_section7(sections_dir):
    """-> section 7's bytes from an extracted sections directory, or None."""
    import glob
    import os
    if not sections_dir:
        return None
    hits = sorted(glob.glob(os.path.join(glob.escape(sections_dir),
                                         'section_7_*.bin')))
    if not hits:
        return None
    with open(hits[0], 'rb') as fh:
        data = fh.read()
    # Section 7 is the DSP's program only if its entry word points into it.
    if len(data) < 8:
        return None
    entry = struct.unpack_from('>I', data, 0)[0]
    if not DSP_LOAD <= entry < DSP_LOAD + len(data):
        return None
    return data


def install(m, events, profile, sections_dir=None, real=True):
    """Model the DSP of an image that boots one.

    -> the DspCpu (section 7 found in `sections_dir` and `real`), the
    DspLink stand-in otherwise, or None for an image with no DSP boot task
    (every product but the Digitone).
    """
    if getattr(profile, 'dsp_boot_task', None) is None:
        return None
    s7 = find_section7(sections_dir) if real else None
    if s7 is not None:
        link = DspCpu(m, s7, request_sem=profile.dsp_request_sem)
        events['dspcpu'] = link
    else:
        link = DspLink(m, request_sem=profile.dsp_request_sem)
    events['dsplink'] = link
    return link
