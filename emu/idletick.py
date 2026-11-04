# pyright: reportMissingImports=false
"""The idle-spin stand-ins, for a lowest-priority loop that is not a spin.

The stock firmware's lowest-priority task (the init task, RTOS priority 1 on
the mk1 builds) ends in a `bra.b $self` and waits there for the scheduler's
timer tick to preempt it. The emulator leans on that spin twice: the cold
boot has no timer, so every 20000 passes through a spin raise vector 32 (the
RTOS's reschedule trap) in its place (dspboot.run, longrun.build's
idle_yield); and the cold boot stops once most of a chunk is spin passes
(bootstrap.cold_boot).

A patched firmware can replace that spin with a loop of its own: elekloader's
ev_idle runs the mods' idle handlers over and over in the init task. That
loop is never a spin, so nothing reschedules -- the tasks the init task
creates last are ready and never run -- and the cold boot never parks.

The loop is the patch's code, and the patch's code is not in the MAIN OS
image: it runs from DDR the image does not cover. So this stands in for the
tick there: whenever the CPU is found at IPL 0 in DDR outside the image, in
a task, it raises vector 32 as an idle spin would. The stock firmware never
runs task code outside its image, so a stock run is unchanged: the stock
Digitone 1.43 and 1.44, Digitakt 1.53 and 1.54 and Model:Cycles and
Model:Samples 1.13 set up to byte-identical snapshots with and without it.
A patched build's run does change wherever a mod's code is found running in
a task, idle or not; a reschedule there is what the hardware's tick would
do too.

A sample is `idle` when nothing above the running task is ready and at most
one priority below it is (the RTOS's own idle task, priority 0 on the mk1
builds): the patch's loop is then what the CPU is doing because nothing
else wants it. The cold boot counts those samples as it counts spin passes.
A mod handler that runs a long time in the init task during the boot would
count too, so handlers must stay short, as ev_idle's must.

Used by bootstrap.cold_boot (the first run's ladder) and longrun.build
(intro, settle, the panel, Session). dspboot.run's own fixed-length ladder
(`emu.checkpoint make`) still ticks spins only.

The scheduler state comes from the profile (emu/symbols.py): `current_tcb`
and `prio_heads`, with each TCB's +0x08 pointing at its own priority's slot
(emu/tasks.py). Without them nothing is installed.
"""
import struct

from emu.harness import UC_M68K_REG_PC, UC_M68K_REG_SR

VECTOR = 32                  # trap #0: the RTOS's reschedule
EVERY = 20000                # as the spins: dspboot tick_every, idle_yield
DDR = (0x40000000, 0x48000000)
PRIO_SLOT_OFF = 0x08


class IdleTick:
    """Sample the CPU between steps; reschedule from a patch's idle loop.

    `image` is the MAIN OS span (lo, hi). `count` is a Counter-like that
    gets 'idle_loop_yield' added per reschedule (longrun's ev['stand_ins']).
    Unlike a spin pass it does not deliver longrun's UART console stand-in:
    raising that interrupt under the reschedule stopped the Digitone's UI
    task from ever running after the intro.
    """

    def __init__(self, m, current_tcb, prio_heads, image, count=None,
                 every=EVERY):
        self.m = m
        self.current_tcb = current_tcb
        self.prio_heads = prio_heads
        self.slots = (current_tcb - prio_heads) // 4
        self.lo, self.hi = image
        self.count = count
        self.every = every
        self.yields = 0
        self.samples = {'idle': 0, 'busy': 0}

    @classmethod
    def install(cls, m, profile, image_len, load, **kw):
        """-> an IdleTick for this image, or None when its scheduler symbols
        are not resolved (nothing is then done in their place)."""
        cur = profile.get('current_tcb')
        heads = profile.get('prio_heads')
        if cur is None or heads is None or not 0 < cur - heads <= 4 * 64:
            return None
        return cls(m, cur, heads, (load, load + image_len), **kw)

    def _u32(self, addr):
        try:
            return struct.unpack('>I', self.m.uc.mem_read(addr, 4))[0]
        except Exception:                               # noqa: BLE001
            return None

    def in_loop(self):
        """True when the CPU is at IPL 0 in DDR outside MAIN OS: patch code
        with interrupts on, which no stock run is ever found in."""
        uc = self.m.uc
        if uc.reg_read(UC_M68K_REG_SR) & 0x0700:
            return False
        pc = uc.reg_read(UC_M68K_REG_PC)
        return DDR[0] <= pc < DDR[1] and not self.lo <= pc < self.hi

    def _nothing_else(self):
        """True when no task above the running one is ready and at most one
        priority below it is. None when the scheduler is not up yet."""
        tcb = self._u32(self.current_tcb)
        slot = self._u32(tcb + PRIO_SLOT_OFF) if tcb else None
        if slot is None:
            return None
        prio = (slot - self.prio_heads) // 4
        if not 0 <= prio < self.slots:
            return None
        ready = [p for p in range(self.slots)
                 if self._u32(self.prio_heads + 4 * p)]
        return (all(p <= prio for p in ready)
                and len([p for p in ready if p < prio]) <= 1)

    def sample(self):
        """-> None (stock code, or interrupts masked), 'idle' or 'busy'. In
        the loop it reschedules: raises vector 32, which moves the PC."""
        if not self.in_loop():
            return None
        alone = self._nothing_else()
        if alone is None:
            return None
        if self.m.raise_vector(VECTOR):
            self.yields += 1
            if self.count is not None:
                self.count['idle_loop_yield'] += 1
        kind = 'idle' if alone else 'busy'
        self.samples[kind] += 1
        return kind

    # -- longrun.spin event source --------------------------------------------
    # Stateless: whether a step is cut short depends only on where the CPU
    # is now, so a run resumed from a snapshot steps as the original did.
    def step(self, done, remaining=None):
        """Steps of `every` instructions while the CPU is in the loop; no
        deadline otherwise, so a stock run's steps are unchanged."""
        return self.every if self.in_loop() else None

    def service(self, done):
        return self.sample() is not None
