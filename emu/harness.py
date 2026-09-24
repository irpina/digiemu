# pyright: reportMissingImports=false
# ruff: noqa: I001
# fmt: off
"""Unicorn m68k harness for running real Digitakt II ColdFire code.

Unicorn's m68k core has gaps that matter here. All are handled below:

  * FF1.L (0x04C0-0x04C7) is not implemented. Left alone, boot stalls at 444
    distinct code addresses; emulated, it reaches 36,474. Biggest single win.
  * MOVEC with Rc=0x009 does not fault -- it aborts the process (SIGABRT).
    It must be intercepted in the code hook, before Unicorn's decoder sees it.
  * m68k exceptions are never dispatched through the vector table.
  * `rte` is surfaced as intr number 0x100 (QEMU's EXCP_RTE) and must be
    implemented by hand.

See docs/FINDINGS.md for how each was found.
"""
import struct
from typing import Any
from unicorn import (Uc, UcError, UC_ARCH_M68K, UC_MODE_BIG_ENDIAN,
                     UC_HOOK_CODE, UC_HOOK_INTR, UC_HOOK_MEM_INVALID,
                     UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE)
try:
    from unicorn import UC_HOOK_MEM_READ_AFTER
except ImportError:
    UC_HOOK_MEM_READ_AFTER = None
# Memory-fault type constants, used to label what Machine._fault records.
# Imported defensively: not every unicorn build exposes every one of these,
# and a missing constant should shrink FAULT_KINDS rather than break import.
_FAULT_KIND_NAMES = {
    'UC_MEM_READ_UNMAPPED': 'read-unmapped',
    'UC_MEM_WRITE_UNMAPPED': 'write-unmapped',
    'UC_MEM_FETCH_UNMAPPED': 'fetch-unmapped',
    'UC_MEM_READ_PROT': 'read-prot',
    'UC_MEM_WRITE_PROT': 'write-prot',
    'UC_MEM_FETCH_PROT': 'fetch-prot',
}
import unicorn as _unicorn_mod
FAULT_KINDS = {}
for _name, _label in _FAULT_KIND_NAMES.items():
    _const = getattr(_unicorn_mod, _name, None)
    if _const is not None:
        FAULT_KINDS[_const] = _label
del _name, _label, _const, _unicorn_mod
from unicorn.m68k_const import (UC_CPU_M68K_CFV4E, UC_M68K_REG_A7,
                                UC_M68K_REG_PC, UC_M68K_REG_SR, UC_M68K_REG_D0)

PAGE = 0x100000
EXCP_RTE = 0x100
# The DDR controller's address space (MCF54418RM Table 1-2).
SDRAM_BASE, SDRAM_WINDOW = 0x40000000, 0x40000000

# Exception-entry trampolines. See Machine.install_srtrap for why they exist.
# Unicorn's m68k is in ColdFire mode, so: no predecrement MOVEM, no .W forms
# of ANDI/ORI, and MOVEM only addresses (d16,An). These encodings respect that.
#
# There is one trampoline per interrupt level, with its immediates baked in,
# because patching a shared one is self-modifying code and QEMU caches
# translation blocks: every delivery after the first then re-ran the FIRST
# level and handler ever patched in. Measured -- DTIM3 delivered 283 times and
# its ISR at 0x400c30e4 ran zero times. `ctl_remove_cache` "fixes" that and
# takes the whole run to zero tasks, so the answer is to not write code at
# runtime at all.
SRTRAP_ADDR   = 0x10000000        # away from ROM (0x4000_0000) and all MMIO
SRTRAP_STRIDE = 0x40              # one slot per level, 0..7 then "unchanged"
SRTRAP_SLOTS  = 9
SRTRAP_EXIT   = 30                # the trailing nop, hooked to set PC
SRTRAP_FRAME  = 12                # bytes pushed: 4 scratch + 8 frame
VBR = 0x40000000            # m68k vector table; MAIN OS loads at VBR+0x400


_NATIVE_FF1 = {}


def native_ff1(cpu=UC_CPU_M68K_CFV4E):
    """-> True if this Unicorn executes FF1 itself for `cpu`.

    The MCF5441x is ISA_C, which has FF1; stock Unicorn's CFV4E model lacks
    it, and the ISA patches below emulate it in a Python hook. A Unicorn with
    patches/unicorn-2.1.4-m68k-digikit-accel.patch runs it natively (and,
    unlike the hook, sets N and Z as the manual says), so it must not be
    intercepted. Probed once on a scratch engine: `ff1 d0` of 0x00010000 is 15.
    """
    if cpu not in _NATIVE_FF1:
        try:
            uc = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
            uc.ctl_set_cpu_model(cpu)
            uc.mem_map(0, 0x1000)
            uc.mem_write(0x100, b'\x04\xc0')          # ff1 d0
            uc.reg_write(UC_M68K_REG_SR, 0x2700)
            uc.reg_write(UC_M68K_REG_D0, 0x00010000)
            uc.emu_start(0x100, 0x102)
            _NATIVE_FF1[cpu] = uc.reg_read(UC_M68K_REG_D0) == 15
        except UcError:
            _NATIVE_FF1[cpu] = False
    return _NATIVE_FF1[cpu]


class Machine:
    """A ColdFire machine with memory mapped on demand."""

    def __init__(self, cpu=UC_CPU_M68K_CFV4E, ddr=None):
        from emu.unicorn_compat import require_compatible_unicorn
        require_compatible_unicorn()
        self.uc = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        self.uc.ctl_set_cpu_model(cpu)
        # The DDR model: None maps every 1 MB page of the SDRAM space as
        # memory of its own, which is how this emulator has always run. A
        # size makes the space decode the way the controller does (see
        # set_ddr): every alias of a location is that location.
        self.ddr = None
        self._ddr_buf = None
        # Same calls, less binding overhead per call; see emu/fastuc.py.
        from emu import fastuc
        fastuc.install(self.uc)
        # No exit check after every guest load and store: none of this
        # emulator's memory hooks stops emulation, and the check is a helper
        # call per access (emu/native.py). A no-op on a library without it.
        from emu import native
        native.enable_options(self.uc, native.NO_MEM_EXIT)
        self.mapped = set()
        # An unmodeled peripheral page is invisible once auto-mapped: reads
        # return 0 and writes vanish, so the firmware looking for hardware
        # the emulator doesn't have looks identical to a firmware bug. These
        # record every such fault so it can be reported instead of hidden.
        self.faults = []            # ordered first-touch records, capped below
        self.fault_pages = {}       # page base -> record dict, for aggregation
        self.fault_sink = None      # optional callable(record) for live streaming
        self.max_fault_records = 4096
        self.mmio = {}          # addr -> int, forced on read
        self.srtrap = None      # set by install_srtrap
        self._srtrap_target = 0
        self.ctlregs = {}       # MOVEC control registers
        self.ff1_count = 0
        self.movec_count = 0
        self.mmio_trace_hooks = []
        self._owned_trace = None
        # Set by longrun.build when a saved host component must be claimed
        # before execution (for example, timers constructed after restore).
        self._checkpoint_deferred_restore: Any = None
        # Set by install_exceptions when a vector has no handler and the run
        # is stopped from inside the hook. emu_start then returns *without
        # raising*, so a caller that assumes it executed its full budget will
        # race to the end of a run that never happened.
        self.halt_vec = None
        # Writes poke() could not make yet because their page was not mapped,
        # page base -> [(addr, bytes)] in the order they were made. ensure()
        # applies them the moment the page is mapped.
        self.pending = {}
        self.uc.hook_add(UC_HOOK_MEM_INVALID, self._fault)
        if ddr is not None:
            self.set_ddr(ddr)

    # -- memory ------------------------------------------------------------
    def set_ddr(self, size, base=SDRAM_BASE, window=SDRAM_WINDOW):
        """Back the SDRAM space with `size` bytes of DDR, repeated.

        MCF54418RM Table 1-2 gives the DDR controller 0x4000_0000-0x7FFF_FFFF,
        and 1.7.11 gives it one chip select and an 8-bit port: 16 to 256 MB.
        The controller decodes only the address bits its part needs, so the
        fitted memory repeats through the whole space, and firmware uses that
        on purpose. Digitakt mk1 has 64 MB (tools/ddr_geometry.py, from the
        bootstrap's own DDRMC writes); its OS keeps its stack at 0x47FFxxxx,
        which is 0x43FFxxxx, and reads DMA buffers through 0x48000000 and up,
        which ACR0 = 0x4007E020 leaves cache-inhibited: an uncached view of
        the same memory.

        Without this every 1 MB page of the space is memory of its own, so a
        write through one alias is invisible through another: a DMA buffer
        filled through the cached view reads as zeros through the uncached
        one. With it, each page is mapped onto one shared host buffer at its
        physical offset (Unicorn's mem_map_ptr), so all aliases are the same
        bytes, as on the device.

        Must be called before any page of the window is mapped."""
        import ctypes
        import mmap
        if size <= 0 or size & (size - 1) or size % PAGE:
            raise ValueError('DDR size must be a power of two, whole pages')
        clash = [b for b in self.mapped if base <= b < base + window]
        if clash:
            raise RuntimeError('set_ddr after pages of the SDRAM window were '
                               'mapped (%s)' % ', '.join(hex(b) for b in clash[:4]))
        # Anonymous memory: zeroed and page-aligned, which the engine's TLB
        # needs (a ctypes buffer is neither guaranteed aligned).
        self._ddr_buf = mmap.mmap(-1, size)
        self._ddr_ptr = ctypes.addressof(ctypes.c_char.from_buffer(self._ddr_buf))
        self.ddr = (base, size, window)

    def ddr_physical(self, addr):
        """-> the offset into DDR that `addr` decodes to, or None."""
        if self.ddr is None:
            return None
        base, size, window = self.ddr
        if base <= addr < base + window:
            return (addr - base) % size
        return None

    def ensure(self, addr):
        base = addr & ~(PAGE - 1)
        if base in self.mapped:
            return
        try:
            off = self.ddr_physical(base)
            if off is None:
                self.uc.mem_map(base, PAGE)
            else:
                from unicorn import UC_PROT_ALL
                self.uc.mem_map_ptr(base, PAGE, UC_PROT_ALL,
                                    self._ddr_ptr + off)
            self.mapped.add(base)
        except UcError:
            return
        for at, data in self.pending.pop(base, ()):
            self.uc.mem_write(at, data)

    def _pages(self, addr, n):
        first = addr & ~(PAGE - 1)
        last = (addr + max(n, 1) - 1) & ~(PAGE - 1)
        return range(first, last + PAGE, PAGE)

    def poke(self, addr, data):
        """Write guest memory from inside a memory hook.

        A memory hook runs in the middle of the guest's own load or store, and
        mapping a page there resizes Unicorn's TLB under that access: the
        store then lands on a stale host pointer (an access violation at the
        guest address, seen on the Digitone's first EXT_CSD read, whose
        buffer page nothing had touched yet). So a write to a page that is not
        mapped is held in `pending` instead and made when the page is mapped:
        by the guest's own first access to it (_fault), which is where the
        data is first needed, or by ensure()/flush_pending() outside
        emulation. Mapped pages are written at once.
        """
        data = bytes(data)
        pages = self._pages(addr, len(data))
        if all(p in self.mapped for p in pages):
            self.uc.mem_write(addr, data)
            return
        for p in pages:
            lo, hi = max(addr, p), min(addr + len(data), p + PAGE)
            if lo >= hi:
                continue
            chunk = data[lo - addr:hi - addr]
            if p in self.mapped:
                self.uc.mem_write(lo, chunk)
            else:
                self.pending.setdefault(p, []).append((lo, chunk))

    def peek(self, addr, n):
        """Read guest memory from inside a memory hook, without mapping.

        An unmapped page reads as the zeros it will be mapped with, with any
        pending poke() writes applied on top.
        """
        if all(p in self.mapped for p in self._pages(addr, n)):
            return bytes(self.uc.mem_read(addr, n))
        out = bytearray(n)
        for p in self._pages(addr, n):
            lo, hi = max(addr, p), min(addr + n, p + PAGE)
            if lo >= hi:
                continue
            if p in self.mapped:
                out[lo - addr:hi - addr] = self.uc.mem_read(lo, hi - lo)
                continue
            for at, data in self.pending.get(p, ()):
                s, e = max(at, lo), min(at + len(data), hi)
                if s < e:
                    out[s - addr:e - addr] = data[s - at:e - at]
        return bytes(out)

    def flush_pending(self):
        """Map every page poke() is still holding writes for. Only outside
        emulation, e.g. before a snapshot is saved."""
        for base in sorted(self.pending):
            self.ensure(base)

    def _fault(self, uc, typ, addr, size, val, data):
        """Record the access, then map a zero page and continue.

        An unmodeled peripheral is otherwise invisible -- reads return 0 and
        writes vanish, so a firmware stall caused by missing hardware looks
        identical to a firmware bug. Recording is effectively free: this hook
        only fires on an unmapped access, and ensure() maps the page, so it
        fires at most once per page.
        """
        try:
            base = addr & ~(PAGE - 1)
            rec = self.fault_pages.get(base)
            if rec is None:
                rec = {'page': base, 'first_addr': addr,
                       'first_pc': uc.reg_read(UC_M68K_REG_PC),  # PC, not SR --
                       # an SR read here would clobber lazy CCR state (see
                       # docs on the unicorn m68k SR-read bug) and this is
                       # just a diagnostic hook, not worth that risk.
                       'kinds': {}, 'count': 0, 'addrs': set()}
                self.fault_pages[base] = rec
                if len(self.faults) < self.max_fault_records:
                    self.faults.append(rec)
            kind = FAULT_KINDS.get(typ, 'type-%d' % typ)
            rec['kinds'][kind] = rec['kinds'].get(kind, 0) + 1
            rec['count'] += 1
            if len(rec['addrs']) < 64:
                rec['addrs'].add(addr)
            if self.fault_sink is not None:
                self.fault_sink(rec)
        except Exception:
            pass
        self.ensure(addr)
        return True

    def fault_report(self):
        """Summarize which unmodeled hardware the firmware touched, as JSON-friendly data."""
        return [
            {
                'page': '0x%08x' % rec['page'],
                'first_addr': '0x%08x' % rec['first_addr'],
                'first_pc': '0x%08x' % rec['first_pc'],
                'count': rec['count'],
                'kinds': rec['kinds'],
                'addrs': sorted('0x%08x' % a for a in rec['addrs']),
            }
            for rec in sorted(self.faults, key=lambda r: r['page'])
        ]

    def install_mmio(self, scoped=True):
        """Force `self.mmio` values on read. Use for status registers whose
        ready bits the firmware polls (e.g. UART8 USR8, DSPI0 SR).

        scoped=True registers one narrow hook per address instead of a single
        global UC_HOOK_MEM_READ. The global form runs a Python callback -- and
        a loop over the mmio dict -- on *every memory read the firmware makes*,
        which is the same mistake install_isa_patches makes for instructions.
        There are only a handful of MMIO addresses, so narrow hooks cost
        nothing between hits.

        Addresses are read from `self.mmio` at install time, so populate it
        before calling this -- including anything a snapshot restore merges in
        (see longrun.build, which installs after restoring for that reason). scoped=False keeps
        the old global behaviour, which does pick up later additions.
        """
        def make(addr):
            def h(uc, typ, a, size, val, data):
                try:
                    uc.mem_write(addr, struct.pack('>I', self.mmio[addr]))
                except UcError:
                    pass
            return h

        if scoped:
            for addr in self.mmio:
                self.uc.hook_add(UC_HOOK_MEM_READ, make(addr),
                                 begin=addr, end=addr + 3)
            return

        def on_read(uc, typ, addr, size, val, data):
            for a, v in self.mmio.items():
                if a <= addr < a + 4:
                    try:
                        uc.mem_write(a, struct.pack('>I', v))
                    except UcError:
                        pass
        self.uc.hook_add(UC_HOOK_MEM_READ, on_read)

    def install_mmio_trace(self, sink=None, ranges=(), registers=None, owned=False):
        """Install read-only observer hooks for explicit, narrow MMIO ranges."""
        if sink is None:
            return []
        ranges = tuple(ranges)
        if not ranges:
            raise ValueError("MMIO tracing requires at least one narrow range")
        if owned:
            if self._owned_trace is not None:
                raise ValueError("Machine already owns an MMIO trace sink")
            self._owned_trace = sink
        registers = registers or {}
        read_hook = UC_HOOK_MEM_READ_AFTER or UC_HOOK_MEM_READ
        read_value_available = bool(UC_HOOK_MEM_READ_AFTER)
        read_phase = 'after' if read_value_available else 'before-value-unknown'

        def on_read(uc, typ, addr, size, value, data):
            sink.event(pc=uc.reg_read(UC_M68K_REG_PC), address=addr, width=size * 8,
                       direction='read', value=value if read_value_available else None,
                       register=registers.get(addr), read_phase=read_phase)

        def on_write(uc, typ, addr, size, value, data):
            sink.event(pc=uc.reg_read(UC_M68K_REG_PC), address=addr, width=size * 8,
                       direction='write', value=value, register=registers.get(addr))

        try:
            for begin, end in ranges:
                try:
                    hook = self.uc.hook_add(read_hook, on_read, begin=begin, end=end)
                except UcError:
                    read_value_available = False
                    read_phase = 'before-value-unknown'
                    hook = self.uc.hook_add(UC_HOOK_MEM_READ, on_read, begin=begin, end=end)
                self.mmio_trace_hooks.append(hook)
                self.mmio_trace_hooks.append(self.uc.hook_add(
                    UC_HOOK_MEM_WRITE, on_write, begin=begin, end=end))
        except Exception:
            if owned:
                self.close()
            raise
        return list(self.mmio_trace_hooks)

    def close(self):
        """Close a trace sink this Machine explicitly owns; safe to repeat."""
        if self._owned_trace is not None:
            sink, self._owned_trace = self._owned_trace, None
            sink.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def load(self, image, addr):
        for off in range(0, len(image), PAGE):
            self.ensure(addr + off)
        self.ensure(addr)
        self.uc.mem_write(addr, image)

    # -- ISA gaps (scoped, fast) --------------------------------------------
    def install_isa_patches_scoped(self, image, load_addr):
        """Same FF1/MOVEC emulation as install_isa_patches, but registered as
        per-address hooks (Unicorn `begin=addr, end=addr`) instead of one
        global UC_HOOK_CODE that runs a Python callback -- with a mem_read +
        struct.unpack -- on *every single instruction executed*, just to see
        if it happens to be one of these two rare opcodes.

        Pre-scans `image` once for the exact addresses where these opcodes
        occur and hooks only those. A found offset that never actually ends
        up as a real instruction boundary (e.g. it's the operand byte of some
        other instruction) simply never fires -- PC only ever equals real
        instruction-boundary addresses during execution, so this is safe.
        This was the single biggest cost in long dspboot.py runs: removing it
        from the hot path is roughly a 2-4x wall-clock win on decompression-
        and allocator-loop-heavy stretches of boot.
        """
        # Each hook first checks the opcode is still there: code can be
        # replaced at run time (the Digitakt bootstrap loads its updater over
        # itself, emu/bootrom.py), and a hook acting on whatever instruction
        # now sits at its address would skip it.
        def still(uc, addr, lo, hi):
            try:
                w = struct.unpack('>H', uc.mem_read(addr, 2))[0]
            except UcError:
                return False
            return lo <= w <= hi

        def make_ff1(reg):
            def h(uc, addr, size, data):
                if not still(uc, addr, 0x04C0, 0x04C7):
                    return
                v = uc.reg_read(reg) & 0xFFFFFFFF
                out = 32 if v == 0 else 31 - v.bit_length() + 1
                uc.reg_write(reg, out)
                uc.reg_write(UC_M68K_REG_PC, addr + 2)
                self.ff1_count += 1
            return h

        def make_movec(addr, w):
            def h(uc, addr_, size, data):
                if not still(uc, addr, w, w):
                    return
                ext = struct.unpack('>H', uc.mem_read(addr + 2, 2))[0]
                rc = ext & 0x0FFF
                reg = UC_M68K_REG_D0 + ((ext >> 12) & 7)
                if w == 0x4E7B:
                    self.ctlregs[rc] = uc.reg_read(reg)
                else:
                    uc.reg_write(reg, self.ctlregs.get(rc, 0))
                uc.reg_write(UC_M68K_REG_PC, addr + 4)
                self.movec_count += 1
            return h

        n_ff1 = n_movec = 0
        ff1_native = native_ff1()
        for off in range(0, len(image) - 1, 2):
            w = image[off] << 8 | image[off + 1]
            addr = load_addr + off
            if 0x04C0 <= w <= 0x04C7 and not ff1_native:
                reg = UC_M68K_REG_D0 + (w & 7)
                self.uc.hook_add(UC_HOOK_CODE, make_ff1(reg), begin=addr, end=addr)
                n_ff1 += 1
            elif w in (0x4E7A, 0x4E7B):
                self.uc.hook_add(UC_HOOK_CODE, make_movec(addr, w), begin=addr, end=addr)
                n_movec += 1
        return n_ff1, n_movec

    # -- ISA gaps ------------------------------------------------------------
    def install_isa_patches(self, extra_code_hook=None):
        """Emulate the ColdFire instructions Unicorn lacks."""
        ff1_native = native_ff1()

        def on_code(uc, addr, size, data):
            w = struct.unpack('>H', uc.mem_read(addr, 2))[0]
            if 0x04C0 <= w <= 0x04C7 and not ff1_native:   # FF1.L Dn
                n = w & 7
                reg = UC_M68K_REG_D0 + n
                v = uc.reg_read(reg) & 0xFFFFFFFF
                # count leading zeros; FF1 of 0 is defined as 32
                out = 32 if v == 0 else 31 - v.bit_length() + 1
                uc.reg_write(reg, out)
                uc.reg_write(UC_M68K_REG_PC, addr + 2)
                self.ff1_count += 1
                return
            if w in (0x4E7A, 0x4E7B):                 # MOVEC -- crashes Unicorn
                ext = struct.unpack('>H', uc.mem_read(addr + 2, 2))[0]
                rc = ext & 0x0FFF
                reg = UC_M68K_REG_D0 + ((ext >> 12) & 7)   # data regs only
                if w == 0x4E7B:
                    self.ctlregs[rc] = uc.reg_read(reg)
                else:
                    uc.reg_write(reg, self.ctlregs.get(rc, 0))
                uc.reg_write(UC_M68K_REG_PC, addr + 4)
                self.movec_count += 1
                return
            if extra_code_hook:
                extra_code_hook(uc, addr, size)
        self.uc.hook_add(UC_HOOK_CODE, on_code)

    def install_exceptions(self, on_unhandled=None):
        """Dispatch exceptions via the vector table and implement `rte`."""
        def on_intr(uc, vec, data):
            if vec == EXCP_RTE:
                # Only reached on a Unicorn without native rte (see below).
                sp = uc.reg_read(UC_M68K_REG_A7)
                _fmt, sr, pc = struct.unpack('>HHI', uc.mem_read(sp, 8))
                uc.reg_write(UC_M68K_REG_SR, sr)
                uc.reg_write(UC_M68K_REG_PC, pc)
                uc.reg_write(UC_M68K_REG_A7, sp + 8)
                return
            # A synchronous `trap #N` must resume *after* the trap, so let
            # raise_vector advance the pushed PC past it. Asynchronous
            # injections (the timer tick) keep the interrupted PC.
            if not self.raise_vector(vec, from_instruction=True):
                if on_unhandled:
                    on_unhandled(vec)
                self.halt_vec = vec
                uc.emu_stop()
        self.uc.hook_add(UC_HOOK_INTR, on_intr)
        # Let the engine do `rte` itself where it can: QEMU's ColdFire return
        # pops the same frame (format/SR, PC) and adds 8 to A7, as the branch
        # above does, without a Python call per return. ColdFire here has one
        # stack pointer (CACR.EUSP is never set: MOVEC is intercepted), so the
        # order of the SR and A7 writes cannot matter.
        from emu import native
        native.enable_options(self.uc, native.NATIVE_RTE)

    def _ensure_frame(self, sp, size):
        """Map the pages an exception frame is about to be pushed onto.

        Guest accesses to an unmapped page are mapped lazily by `_fault`, but
        that is a UC_HOOK_MEM_INVALID hook and `uc.mem_write` from Python does
        not trigger it. So a frame pushed onto a task stack the guest has not
        touched yet dies with UC_ERR_WRITE_UNMAPPED instead of being mapped
        the way the identical write from guest code would be. Digitakt II
        1.15C never hit this -- its task stacks share pages the guest has
        already written -- which is exactly the kind of firmware-specific luck
        that hides a general defect until a second firmware runs.

        The frame can straddle a page boundary, so map both ends.
        """
        self.ensure(sp)
        self.ensure(sp + size - 1)

    def raise_vector(self, vec, from_instruction=False, level=None):
        """Push an exception frame and jump to the handler. -> bool taken.

        `from_instruction` marks an exception raised BY the instruction at PC
        rather than injected asynchronously. It matters for `trap #N`, which
        the RTOS uses as its scheduler yield: Unicorn reports the trap with PC
        still pointing at the trap instruction, so pushing that PC unmodified
        makes the frame resume *onto the trap again*. A task that blocked in
        sem_pend then re-traps the instant the scheduler restores it and can
        never leave the wait -- which is exactly why only one task ever ran,
        no matter how long the boot was left going. TRAP #N is 2 bytes
        (0x4E40-0x4E4F), so the frame has to resume at PC+2.
        """
        # The table the firmware pointed VBR at (MOVEC 0x801), else the OS's.
        # A handler has to be code: in the cached DDR or the SRAM (where the
        # bootstrap runs, emu/bootrom.py); anything else is an empty slot.
        vbr = self.ctlregs.get(0x801, VBR)
        handler = struct.unpack('>I', self.uc.mem_read(vbr + vec * 4, 4))[0]
        if handler == 0 or not (handler < 0x48000000
                                or 0x80000000 <= handler < 0x80010000):
            return False
        pc = self.uc.reg_read(UC_M68K_REG_PC)
        if from_instruction:
            try:
                w = struct.unpack('>H', self.uc.mem_read(pc, 2))[0]
                if 0x4E40 <= w <= 0x4E4F:        # trap #0 .. trap #15
                    pc += 2
            except UcError:
                pass
        # The SR slot is filled in by the guest, not from here. Writing what
        # `reg_read(UC_M68K_REG_SR)` returns puts a stale condition-code byte
        # in the frame, and `rte` then installs it over the flags of the code
        # being resumed. See install_srtrap.
        if self.srtrap is None:
            sr = self.uc.reg_read(UC_M68K_REG_SR)
            sp = self.uc.reg_read(UC_M68K_REG_A7) - 8
            self._ensure_frame(sp, 8)
            self.uc.mem_write(sp, struct.pack(
                '>HHI', 0x4000 | ((vec << 2) & 0x0FFC), sr, pc))
            self.uc.reg_write(UC_M68K_REG_A7, sp)
            # The frame keeps the INTERRUPTED sr untouched (above); the CPU's
            # LIVE sr during the handler is a different value, and hardware
            # always sets S on any exception entry and, for an asynchronous
            # interrupt, raises the IPL to the level being serviced so a
            # same-or-lower-level source cannot re-enter. Leaving live SR at
            # the interrupted value -- what this branch did before -- was
            # wrong only here, in the srtrap-OFF host-frame path: the
            # srtrap-ON path below already sets S and the IPL live, via its
            # `ori.l`/`move.w d0,sr` trampoline (see install_srtrap). This
            # mirrors that existing trampoline behaviour for the case where
            # no trampoline runs, now that patched Unicorn's SR read is not
            # destructive (docs/UNICORN.md). `level=None` marks a synchronous
            # trap, which sets S but leaves the IPL mask alone, matching the
            # trampoline's slot for that case.
            live_sr = sr | 0x2000
            if level is not None:
                live_sr = (live_sr & ~0x0700) | ((level & 7) << 8)
            self.uc.reg_write(UC_M68K_REG_SR, live_sr)
            self.uc.reg_write(UC_M68K_REG_PC, handler)
            return True

        # Twelve bytes: a scratch longword holding d0, then the frame. The
        # trampoline restores d0 from the scratch and then `lea`s past it, so
        # the handler sees A7 at the frame base exactly as before -- the
        # layout emu/tasks.py and the RTOS context switcher at 0x40000410
        # both read.
        sp = self.uc.reg_read(UC_M68K_REG_A7) - SRTRAP_FRAME
        self._ensure_frame(sp, SRTRAP_FRAME)
        self.uc.mem_write(sp, struct.pack('>I', self.uc.reg_read(UC_M68K_REG_D0)))
        # Format nibble 4. The ColdFire PRM is explicit: an RTE whose frame
        # format is not 4-7 raises a format error. The SR word is a
        # placeholder; the trampoline overwrites it with the real one.
        self.uc.mem_write(sp + 4, struct.pack(
            '>HHI', 0x4000 | ((vec << 2) & 0x0FFC), 0, pc))
        self.uc.reg_write(UC_M68K_REG_A7, sp)
        # Pick the trampoline for this level. `trap #N` does not raise the
        # interrupt mask on m68k, so it uses the "leave the mask alone" slot.
        slot = SRTRAP_SLOTS - 1 if level is None else (level & 7)
        self._srtrap_target = handler
        self.uc.reg_write(UC_M68K_REG_PC, self.srtrap + slot * SRTRAP_STRIDE)
        return True

    def install_srtrap(self, addr=SRTRAP_ADDR):
        """Route exception entry through guest code that saves the true SR.

        Unicorn's m68k keeps the condition codes in TCG's lazy `cc_op` form
        and `reg_read(UC_M68K_REG_SR)` returns only what was last *written* --
        never the computed flags. Proved directly: after `tst.l d0` with d0
        non-zero, SR still reads back the seeded value with Z set. So an SR
        read is stale, and an SR *write* built from one installs wrong flags.
        An identity `reg_write(SR, reg_read(SR))` around a `tst.l`/`beq` flips
        the branch.

        That is not a hypothetical. Every delivered interrupt used to corrupt
        the flags of the code it interrupted three times over: `raise_vector`
        pushed a stale SR into the frame, `Pits.service` wrote a stale-derived
        SR back, and `rte` restored the stale frame value onto the resumed
        instruction stream. It is almost certainly the whole of HANDOVER
        section 10 -- "a `bgt` at 0x40111070 taking opposite branches from
        identical PC, A7, D0 and A0" is what a corrupted Z looks like.

        The CPU can compute what we cannot read: `move.w sr,d0` executed by
        the guest returns the true flags. So exception entry goes through this
        trampoline instead of straight to the handler:

            move.w  sr,d0         ; the TRUE SR, flags materialised by the CPU
            move.w  d0,$6(a7)     ; -> the frame's SR slot
            andi.l  #$f8ff,d0     ; clear the old interrupt mask
            ori.l   #level,d0     ; set S and this source's level
            move.w  d0,sr         ; a guest-side SR write, so the CCR survives
            movem.l $0(a7),d0     ; restore d0; MOVEM has no CCR effect
            lea.l   $4(a7),a7     ; drop the scratch longword
            jmp     handler.l

        `move.w d0,sr` is the trick that makes this faithful: the low byte of
        the value it writes is the CCR the CPU itself just produced, so the
        handler starts with the interrupted code's flags, exactly as hardware
        leaves them.

        The handler address and the level word are patched per delivery.
        Returns the trampoline address, also kept on `self.srtrap`.
        """
        try:
            self.uc.mem_map(addr, PAGE)
        except UcError:
            pass                      # already mapped

        def slot_code(mask, ipl):
            return (b'\x40\xc0'                          #  0 move.w sr,d0
                    b'\x3f\x40\x00\x06'                  #  2 move.w d0,$6(a7)
                    + b'\x02\x80' + struct.pack('>I', mask)   #  6 andi.l
                    + b'\x00\x80' + struct.pack('>I', ipl)    # 12 ori.l
                    + b'\x46\xc0'                        # 18 move.w d0,sr
                    b'\x4c\xef\x00\x01\x00\x00'          # 20 movem.l $0(a7),d0
                    b'\x4f\xef\x00\x04'                  # 26 lea.l $4(a7),a7
                    b'\x4e\x71')                         # 30 nop  <- hooked

        for slot in range(SRTRAP_SLOTS):
            if slot == SRTRAP_SLOTS - 1:
                mask, ipl = 0xFFFFFFFF, 0x2000          # leave the mask alone
            else:
                mask, ipl = 0x0000F8FF, 0x2000 | (slot << 8)
            base = addr + slot * SRTRAP_STRIDE
            self.uc.mem_write(base, slot_code(mask, ipl))
            self.uc.hook_add(UC_HOOK_CODE, self._srtrap_exit,
                             begin=base + SRTRAP_EXIT, end=base + SRTRAP_EXIT)
        self.srtrap = addr
        return addr

    def _srtrap_exit(self, uc, addr, size, data):
        """Leave the trampoline for the handler the last raise_vector chose.

        A jump would have to be patched per delivery, and patching is the
        self-modifying-code problem the constants above describe. Redirecting
        PC from a hook is what the rest of this codebase already does.
        """
        uc.reg_write(UC_M68K_REG_PC, self._srtrap_target)


def call(machine, func, args, ret_magic=0xDEADBEE0, stack_top=None, limit=800_000_000):
    """Call a firmware routine with C-style stacked args. -> D0."""
    deferred = getattr(machine, '_checkpoint_deferred_restore', None)
    if deferred is not None:
        deferred.require_claimed()
    uc = machine.uc
    if stack_top is None:
        machine.ensure(0x10000000)
        stack_top = 0x10000000 + PAGE - 0x100
    frame = struct.pack('>I', ret_magic) + b''.join(struct.pack('>I', a) for a in args)
    uc.mem_write(stack_top, frame)
    # SR before A7: m68k banks SSP/USP, so switching mode after setting the
    # stack pointer silently writes the register the CPU is about to stop using.
    uc.reg_write(UC_M68K_REG_SR, 0x2700)
    uc.reg_write(UC_M68K_REG_A7, stack_top)
    uc.emu_start(func, ret_magic, count=limit)
    return uc.reg_read(UC_M68K_REG_D0) & 0xFFFFFFFF
# fmt: on
