"""Core-clock cycle estimates for MCF5441x code, and a clock that runs on them.

The emulator's time base is an instruction count: the timers fire every so
many instructions (emu/pit.py's INSTR_PER_SEC, 64M a second live). That is
fine for watching the firmware and useless for asking whether it is fast
enough, because instructions are not equal: a DIVS.L is 35 core clocks and a
MOVE.L 1, and the audio render is full of the former's kind. This module
counts core clocks instead.

Where the numbers come from. MCF54418RM section 3.3.5, "Instruction Execution
Timing", gives each instruction's cycles by effective-address mode (Tables
3-12 to 3-20), and 3.1.1.1 describes the change-of-flow acceleration the
branch figures depend on: an 8-entry direct-mapped branch cache and a
128-entry prediction table, both 2-bit, and a 4-entry return stack for RTS.
Each figure below cites its table. The manual's own assumptions (3.3.5.1)
carry over and are what an estimate here means:

  * memory is zero-wait: no cache misses, no DDR latency, no bus contention
    (the core's 64 KB SRAM really is single-cycle; DDR is not);
  * no instruction pairs: the V4 core can issue some MOVE pairs in one cycle,
    and the manual gives no rules for it, so every instruction is costed
    alone, which overstates tight MOVE sequences;
  * the stall rule it does give is applied: a register written by one
    instruction and used for the next one's address costs 2 cycles as a base
    or unscaled index, 3 as a scaled index;
  * misaligned operands cost more (Table 3-12); operand addresses are not
    seen here, so they are not charged.

Where the manual gives a range -- BRA, BSR, and JMP/JSR to an absolute or
PC-relative target take 1 to 3 cycles depending on the fetch pipeline --
`accel='max'` (the default) charges 3 and `'min'` charges 1. Interrupt entry
has no table; it is charged as TRAP's 18 cycles, which pushes the same frame
and fetches a vector the same way.

So the estimate leans slow where the manual is silent (pairs, ranges) and
fast where memory is concerned. Treat the margin it reports as a margin
against these tables, not against the device.

How it runs. `CycleClock` puts one UC_HOOK_BLOCK on the machine. Each
translated block is decoded once (emu/cfisa.py) into a static cost, cached by
address; the block hook adds that cost, then resolves the previous block's
branch against where execution actually went, which is what drives the
predictor models. Every decode is checked against the size Unicorn reports
for the block: a mismatch means the decoder is wrong for some instruction,
and it is counted (`decode_mismatches`) rather than silently mis-costed.

`CycleStepper` bounds emu_start by cycles instead of instructions, for
longrun.spin: set every timer source's `ips` to the core clock and the
firmware's timers then tick on core clocks, so a slow render really does fall
behind its audio clock.
"""
import collections

from unicorn import UC_HOOK_BLOCK

from emu import cfisa
from emu.cfisa import (ABS, D16, IDX, IMM, IND, NONE, POST, PRE, R,
                       BASE, INDEX1, INDEXN, EMAC_OP, PCREL)

# MCF54418RM 1.2/1.3: every MCF5441x runs at up to 250 MHz. The Digitakt's
# own PLL setting is not known here, so this is a parameter.
FSYS = 250_000_000

# TRAP #imm, Table 3-17: 18(1/2). Used for interrupt entry too (see above).
EXCEPTION_ENTRY = 18

_COLS = (R, IND, POST, PRE, D16, IDX, ABS, IMM)


def _row(*v):
    """A table row in EA-column order R, (An), (An)+, -(An), (d16,An),
    (d8,An,Xi), xxx.wl, #imm."""
    return dict(zip(_COLS, v))


# Tables 3-13 and 3-14 are identical for MOVE.B/W and MOVE.L: rows are the
# source, columns the destination R, (Ax), (Ax)+, -(Ax), (d16,Ax),
# (d8,Ax,Xi), xxx.wl. Cells the manual leaves empty are combinations
# ColdFire cannot encode; they are filled by the same +1-per-index rule so a
# decode error never becomes a KeyError.
_MOVE = {
    R: (1, 1, 1, 1, 1, 2, 1),
    IND: (1, 2, 2, 2, 2, 3, 2),
    POST: (1, 2, 2, 2, 2, 3, 2),
    PRE: (1, 2, 2, 2, 2, 3, 2),
    D16: (1, 2, 2, 2, 2, 3, 2),
    IDX: (2, 3, 3, 3, 3, 4, 3),
    ABS: (1, 2, 2, 2, 2, 3, 2),
    IMM: (1, 1, 1, 1, 1, 2, 1),
}
_MOVE_COL = {R: 0, IND: 1, POST: 2, PRE: 3, D16: 4, IDX: 5, ABS: 6}

# By the EA that varies, Tables 3-15 to 3-18.
_BY_EA = {
    'clr': _row(1, 1, 1, 1, 1, 2, 1, 1),          # 3-15
    'tst': _row(1, 1, 1, 1, 1, 2, 1, 1),          # 3-15
    'tas': _row(1, 1, 1, 1, 1, 2, 1, 1),          # 3-15
    'alu_ea_r': _row(1, 1, 1, 1, 1, 2, 1, 1),     # 3-16 ADD/SUB/AND/OR/CMP
    'alu_r_ea': _row(1, 1, 1, 1, 1, 2, 1, 1),     # 3-16 op Dy,<ea>, EOR
    'addq': _row(1, 1, 1, 1, 1, 2, 1, 1),         # 3-16 ADDQ/SUBQ
    'bchg': _row(2, 2, 2, 2, 2, 3, 2, 2),         # 3-16 BCHG/BCLR/BSET Dy
    'bchgi': _row(2, 2, 2, 2, 2, 3, 2, 2),        # 3-16 ... #imm
    'btst': _row(2, 1, 1, 1, 1, 2, 1, 1),         # 3-16 BTST Dy
    'btsti': _row(1, 1, 1, 1, 1, 2, 1, 1),        # 3-16 BTST #imm
    'div_w': _row(20, 20, 20, 20, 20, 21, 20, 20),  # 3-16
    'lea': _row(1, 1, 1, 1, 1, 2, 1, 1),          # 3-16
    'mov3q': _row(1, 1, 1, 1, 1, 2, 1, 1),        # 3-17
    'mvsz': _row(1, 1, 1, 1, 1, 2, 1, 1),         # 3-17
    'pea': _row(1, 1, 1, 1, 1, 2, 1, 1),          # 3-17
    'wddata': _row(1, 1, 1, 1, 1, 2, 1, 1),       # 3-17
    'mul_w': _row(4, 4, 4, 4, 4, 5, 4, 4),        # 3-18 MULS.W/MULU.W
}

# One figure whatever the EA, Tables 3-15 to 3-18.
_FIXED = {
    'alu_imm': 1, 'cmpi': 1, 'addx': 1, 'shift': 1, 'moveq': 1,   # 3-16
    'div_l': 35,                                                  # 3-16
    'bitrev': 1, 'byterev': 1, 'ext': 1, 'extb': 1, 'ff1': 1,    # 3-15
    'neg': 1, 'negx': 1, 'not': 1, 'sats': 1, 'scc': 1, 'swap': 1,
    'intouch': 19, 'link': 2, 'move_usp': 3,                      # 3-17
    'move_from_ccr': 1, 'move_to_ccr': 1, 'move_from_sr': 1,
    'movec': 20, 'nop': 6, 'pulse': 1, 'stop': 6, 'trap': 18,
    'tpf': 1, 'unlk': 1, 'wdebug': 3,
    'mul_l': 4,                                                   # 3-18
    'mac': 1, 'to_acc': 1, 'acc_to_acc': 1, 'to_macsr': 8,
    'to_mask': 7, 'to_accext': 1, 'from_acc': 1, 'from_emac': 1,
    'macsr_ccr': 1,
    'rte': 15,                                                    # 3-19
    # Not in the tables: STLDSR is costed as MOVE <ea>,SR; HALT as one
    # cycle (it stops the core); anything that takes an exception as
    # exception entry.
    'stldsr': 4, 'halt': 1,
    'illegal': EXCEPTION_ENTRY, 'linef': EXCEPTION_ENTRY,
    'fpu': EXCEPTION_ENTRY,
}

# The operations whose varying EA (the table column) is the destination.
_DST_EA = frozenset(('clr', 'tas', 'addq', 'mov3q', 'alu_r_ea', 'bchg',
                     'bchgi', 'btst', 'btsti'))

# Table 3-17: CPUSHL by cache field (dc 12, ic 18, bc 18).
_CPUSHL = {1: 12, 2: 18, 3: 18}

# Table 3-20: Bcc.
BCC_FOLDED = 0        # branch cache correctly predicts taken
BCC_PREDICTED = 1     # prediction table correct, taken or not
BCC_MISPREDICTED = 8
# Table 3-19 note 3: RTS.
RTS_PREDICTED, RTS_MISPREDICTED, RTS_UNPREDICTED = 2, 9, 8
# 3.3.5.6 note: an EMAC store right after a load, MAC or MSAC is 4 cycles.
EMAC_STORE_EXPOSED = 4


def static_cost(ins, accel_max=True):
    """-> the cycles of `ins` that do not depend on where it goes next.

    For a Bcc or RTS that is nothing: their whole cost is the prediction
    outcome, charged by CycleClock once the outcome is known."""
    op = ins.op
    accel = 3 if accel_max else 1
    if op in ('move', 'movea'):
        row = _MOVE.get(ins.src, _MOVE[IND])
        return row[_MOVE_COL.get(ins.dst, 1)]
    if op == 'movem':
        return max(1, ins.extra)                              # 3-17: n(n/0)
    if op in ('bcc', 'rts'):
        return 0
    if op in ('bra', 'bsr'):
        return accel                                          # 3-19 notes 1, 2
    if op in ('jmp', 'jsr'):                                  # 3-19
        ea = ins.src
        if ea == ABS or (op == 'jmp' and ea == D16 and ins.flags & PCREL):
            return accel
        return 6 if ea == IDX else 5
    if op == 'move_to_sr':                                    # 3-17 note 2
        if ins.src == IMM and ins.extra >= 0 and ins.extra & 0x2000:
            return 1
        return 4
    if op == 'cpushl':
        return _CPUSHL.get(ins.extra, 18)
    fixed = _FIXED.get(op)
    if fixed is not None:
        return fixed
    row = _BY_EA.get(op)
    if row is not None:
        ea = ins.dst if op in _DST_EA else ins.src
        return row.get(ea, row[R])
    return 1


def stall(prev_writes, ins):
    """3.3.5.1 (3): cycles `ins` waits for the register `prev_writes` the
    instruction before it wrote, when it needs it to form an address."""
    if prev_writes < 0:
        return 0
    worst = 0
    for reg, use in ins.agen:
        if reg == prev_writes:
            worst = max(worst, 3 if use == INDEXN else 2)
    return worst


class Block:
    """One translated block, decoded once."""
    __slots__ = ('addr', 'size', 'static', 'stalls', 'n', 'last', 'first',
                 'writes', 'emac_tail', 'ok', 'fallthrough', 'flags')

    def __init__(self, addr, size, insns, accel_max):
        self.addr, self.size = addr, size
        self.n = len(insns)
        self.first = insns[0][1] if insns else None
        self.last = insns[-1][1] if insns else None
        self.ok = sum(ins.length for _, ins in insns) == size
        self.fallthrough = addr + size
        static = stalls = 0
        prev_w, prev_emac = -1, False
        flags = 0
        for i, (_, ins) in enumerate(insns):
            flags |= ins.flags
            c = static_cost(ins, accel_max)
            if prev_emac and ins.op in ('from_acc', 'from_emac'):
                c = max(c, EMAC_STORE_EXPOSED)
            if i:
                s = stall(prev_w, ins)
                stalls += s
            static += c
            prev_w = ins.writes
            prev_emac = bool(ins.flags & EMAC_OP)
        self.static = static
        self.stalls = stalls
        self.writes = prev_w
        self.emac_tail = prev_emac
        self.flags = flags


class Predictor:
    """The V4 core's change-of-flow acceleration (MCF54418RM 3.1.1.1).

    8-entry direct-mapped branch cache holding a target and a 2-bit state,
    backed by a 128-entry direct-mapped 2-bit prediction table; a 4-entry
    LIFO return stack for RTS. The manual gives the sizes and the states,
    not the index functions or the allocation rule, so: both are indexed by
    the branch's word address, a taken branch that missed the branch cache
    is allocated into it, and the table starts weakly not-taken."""

    def __init__(self):
        self.bc = [None] * 8            # [tag, target, state]
        self.pt = [1] * 128
        self.rs = []
        self.counts = collections.Counter()

    def branch(self, addr, target, taken):
        """-> cycles for a Bcc at `addr` that was (not) taken."""
        i = (addr >> 1) & 7
        e = self.bc[i]
        j = (addr >> 1) & 127
        if e is not None and e[0] == addr:
            pred = e[2] >= 2
            if taken and pred and e[1] == target:
                cost, what = BCC_FOLDED, 'bc_folded'
            elif taken == pred:
                cost, what = BCC_PREDICTED, 'bc_predicted'
            else:
                cost, what = BCC_MISPREDICTED, 'mispredicted'
            e[2] = min(3, e[2] + 1) if taken else max(0, e[2] - 1)
            e[1] = target
        else:
            pred = self.pt[j] >= 2
            if taken == pred:
                cost, what = BCC_PREDICTED, 'pt_predicted'
            else:
                cost, what = BCC_MISPREDICTED, 'mispredicted'
            if taken:
                self.bc[i] = [addr, target, max(2, self.pt[j] + 1)]
        self.pt[j] = min(3, self.pt[j] + 1) if taken else max(0, self.pt[j] - 1)
        self.counts[what] += 1
        return cost

    def call(self, ret):
        self.rs.append(ret)
        if len(self.rs) > 4:
            del self.rs[0]

    def ret(self, actual):
        if not self.rs:
            self.counts['rts_unpredicted'] += 1
            return RTS_UNPREDICTED
        if self.rs.pop() == actual:
            self.counts['rts_predicted'] += 1
            return RTS_PREDICTED
        self.counts['rts_mispredicted'] += 1
        return RTS_MISPREDICTED


class ICache:
    """MCF54418RM 6.1: 8 KB, four-way set-associative, 16-byte lines, so 128
    sets. LRU within a set (the manual's replacement is round-robin per set;
    LRU is the closer fit for a model this coarse). Only counts: the cost of
    a miss is a DDR line fill, which the manual does not give; CycleClock
    charges `miss_penalty` per miss, 0 unless asked."""

    def __init__(self, sets=128, ways=4, line=16):
        self.sets, self.ways, self.shift = sets, ways, line.bit_length() - 1
        self.tags = [[] for _ in range(sets)]
        self.hits = self.misses = 0

    def touch(self, addr, size):
        misses = 0
        for ln in range(addr >> self.shift, ((addr + size - 1) >> self.shift) + 1):
            s = self.tags[ln % self.sets]
            if ln in s:
                s.remove(ln)
                s.append(ln)
                self.hits += 1
            else:
                s.append(ln)
                if len(s) > self.ways:
                    del s[0]
                self.misses += 1
                misses += 1
        return misses


class VectorStats:
    __slots__ = ('count', 'wall', 'wall_max', 'self_', 'self_max', 'starts',
                 'walls')

    def __init__(self):
        self.count = self.wall = self.wall_max = self.self_ = self.self_max = 0
        self.starts = collections.deque(maxlen=4096)
        self.walls = collections.deque(maxlen=200_000)

    def as_dict(self, fsys):
        us = 1e6 / fsys
        return {'count': self.count,
                'wall_cycles_max': self.wall_max,
                'wall_cycles_mean': self.wall / self.count if self.count else 0,
                'self_cycles_max': self.self_max,
                'self_cycles_total': self.self_,
                'wall_us_max': round(self.wall_max * us, 3)}


class CycleClock:
    """Count estimated core cycles on a Machine, and optionally stop at a
    budget of them.

    exclude: (lo, hi) address ranges whose blocks are emulator code, not
    firmware (the srtrap trampolines), charged nothing.
    idle: addresses of idle spins (`bra.b $self`); cycles spent in blocks
    starting there are counted as idle as well as total.
    """

    def __init__(self, m, fsys=FSYS, accel='max', stalls=True, icache=False,
                 miss_penalty=0, idle=(), exclude=()):
        if accel not in ('max', 'min'):
            raise ValueError("accel must be 'max' or 'min'")
        self.m = m
        self.fsys = int(fsys)
        self.accel_max = accel == 'max'
        self.use_stalls = bool(stalls)
        self.icache = ICache() if icache else None
        self.miss_penalty = int(miss_penalty)
        self.idle_addrs = frozenset(idle)
        self.exclude = tuple(exclude)
        self.cycles = 0
        self.instructions = 0       # entered blocks' instructions
        self.idle_cycles = 0
        self.stall_cycles = 0
        self.flow_cycles = 0
        self.entry_cycles = 0
        self.blocks = {}
        self.decode_mismatches = 0
        self.mismatch_at = []
        self.predictor = Predictor()
        self.vectors = collections.defaultdict(VectorStats)
        self._frames = []           # [vec, start, nested]
        self._pend = None           # the previous block, if its tail is a flow
        self._prev_writes = -1
        self._prev_emac = False
        self.left = None            # cycle budget, None = unbounded
        # (flags, addr, op) of every flagged instruction in decoded code.
        self.flagged = collections.Counter()
        self.on_new_block = None    # optional callable(Block), for strict mode
        self._orig_raise = m.raise_vector
        m.raise_vector = self._raise_vector
        self.hook = m.uc.hook_add(UC_HOOK_BLOCK, self._on_block)

    # -- decoding --------------------------------------------------------------
    def _decode(self, uc, addr, size):
        try:
            raw = bytes(uc.mem_read(addr, size + 6))
        except Exception:                               # noqa: BLE001
            try:
                raw = bytes(uc.mem_read(addr, size)) + b'\0' * 6
            except Exception:                           # noqa: BLE001
                raw = b'\0' * (size + 6)
        read = cfisa.reader(raw, addr)
        insns = []
        off = 0
        while off < size:
            ins = cfisa.decode(read(addr + off), addr + off)
            insns.append((addr + off, ins))
            off += ins.length
        b = Block(addr, size, insns, self.accel_max)
        if not b.ok:
            self.decode_mismatches += 1
            if len(self.mismatch_at) < 32:
                self.mismatch_at.append((addr, size, raw[:size].hex()))
        if b.flags:
            for at, ins in insns:
                if ins.flags:
                    self.flagged[(ins.flags, at, ins.op)] += 1
        self.blocks[addr] = b
        if self.on_new_block is not None:
            self.on_new_block(b, insns)
        return b

    # -- the hook ----------------------------------------------------------------
    def _on_block(self, uc, addr, size, data):
        left = self.left
        if left is not None and left <= 0:
            uc.emu_stop()
            return
        for lo, hi in self.exclude:
            if lo <= addr < hi:
                return
        b = self.blocks.get(addr)
        if b is None or b.size != size:
            b = self._decode(uc, addr, size)
        cost = 0
        pend = self._pend
        if pend is not None:
            cost += self._resolve(pend, addr)
            self._pend = None
        cost += b.static
        if self.use_stalls:
            s = b.stalls
            if b.first is not None:
                s += stall(self._prev_writes, b.first)
            self.stall_cycles += s
            cost += s
        if self._prev_emac and b.first is not None and \
                b.first.op in ('from_acc', 'from_emac'):
            cost += EMAC_STORE_EXPOSED - 1
        self._prev_writes = b.writes
        self._prev_emac = b.emac_tail
        last = b.last
        if last is not None and last.flow:
            self._pend = b
        if self.icache is not None:
            misses = self.icache.touch(addr, size)
            cost += misses * self.miss_penalty
        self.cycles += cost
        self.instructions += b.n
        if addr in self.idle_addrs:
            self.idle_cycles += cost
        if left is not None:
            self.left = left - cost

    def _resolve(self, b, nxt):
        """Charge the flow instruction ending block `b` now that execution
        is known to continue at `nxt`."""
        ins = b.last
        flow = ins.flow
        tail_at = b.fallthrough - ins.length
        cost = 0
        p = self.predictor
        if flow == cfisa.F_BCC:
            if nxt == ins.target:
                cost = p.branch(tail_at, ins.target, True)
            elif nxt == b.fallthrough:
                cost = p.branch(tail_at, ins.target, False)
            else:                       # an interrupt came in between
                cost = BCC_PREDICTED
                p.counts['unresolved'] += 1
        elif flow in (cfisa.F_BSR, cfisa.F_JSR):
            p.call(b.fallthrough)
        elif flow == cfisa.F_RTS:
            cost = p.ret(nxt)
        elif flow == cfisa.F_RTE:
            self._exit_frame()
        self.flow_cycles += cost
        return cost

    # -- exceptions ----------------------------------------------------------------
    def _raise_vector(self, vec, from_instruction=False, level=None):
        if not from_instruction and self._pend is not None:
            # An interrupt between two steps: the previous block's tail
            # (an rte most of all) has finished, and where it went is the PC
            # the interrupt is about to push. Settle it first, or its frame
            # pop would take the frame about to be pushed.
            from unicorn.m68k_const import UC_M68K_REG_PC
            pend, self._pend = self._pend, None
            cost = self._resolve(pend, self.m.uc.reg_read(UC_M68K_REG_PC))
            self.cycles += cost
            if self.left is not None:
                self.left -= cost
        taken = self._orig_raise(vec, from_instruction=from_instruction,
                                 level=level)
        if taken:
            if not from_instruction:
                self.cycles += EXCEPTION_ENTRY
                self.entry_cycles += EXCEPTION_ENTRY
                if self.left is not None:
                    self.left -= EXCEPTION_ENTRY
            if len(self._frames) >= 64:
                del self._frames[0]
            self._frames.append([vec, self.cycles, 0])
            self.vectors[vec].starts.append(self.cycles)
        return taken

    def _exit_frame(self):
        if not self._frames:
            return
        vec, start, nested = self._frames.pop()
        wall = self.cycles - start
        own = wall - nested
        s = self.vectors[vec]
        s.count += 1
        s.wall += wall
        s.walls.append(wall)
        s.self_ += own
        if wall > s.wall_max:
            s.wall_max = wall
        if own > s.self_max:
            s.self_max = own
        if self._frames:
            self._frames[-1][2] += wall

    # -- reporting ---------------------------------------------------------------
    def seconds(self):
        return self.cycles / self.fsys

    def report(self):
        busy = self.cycles - self.idle_cycles
        out = {
            'fsys_hz': self.fsys,
            'cycles': self.cycles,
            'instructions': self.instructions,
            'cycles_per_instruction': (round(self.cycles / self.instructions, 3)
                                       if self.instructions else None),
            'seconds': round(self.seconds(), 6),
            'idle_cycles': self.idle_cycles,
            'busy_fraction': round(busy / self.cycles, 4) if self.cycles else 0,
            'stall_cycles': self.stall_cycles,
            'flow_cycles': self.flow_cycles,
            'interrupt_entry_cycles': self.entry_cycles,
            'branches': dict(self.predictor.counts),
            'blocks_decoded': len(self.blocks),
            'decode_mismatches': self.decode_mismatches,
            'accel': 'max' if self.accel_max else 'min',
            'vectors': {v: s.as_dict(self.fsys)
                        for v, s in sorted(self.vectors.items())},
        }
        if self.icache is not None:
            out['icache'] = {'hits': self.icache.hits,
                             'misses': self.icache.misses,
                             'miss_penalty': self.miss_penalty}
        return out

    def close(self):
        try:
            self.m.uc.hook_del(self.hook)
        except Exception:                               # noqa: BLE001
            pass
        self.m.raise_vector = self._orig_raise


class CycleStepper:
    """longrun.spin's stepper, in core cycles: run(pc, step) executes about
    `step` cycles and returns how many it did. The timers must then be on
    the same unit -- set each source's `ips` to the clock's fsys."""

    def __init__(self, clock):
        self.clock = clock
        self.steps = 0

    def run(self, pc, step):
        c = self.clock
        self.steps += 1
        start = c.cycles
        c.left = max(1, int(step))
        try:
            c.m.uc.emu_start(pc, 0)
        finally:
            c.left = None
        return max(1, c.cycles - start)


def deadline_report(clock, vector, period_cycles):
    """How close each run of `vector`'s handler came to `period_cycles`,
    counting from its entry to its rte (so time spent in interrupts that
    preempt it counts too, as it would on the device).

    -> {'period_cycles', 'runs', 'worst_cycles', 'p99_cycles',
        'mean_cycles', 'late', 'worst_fraction', 'margin'}, where margin is
    1 - worst/period (negative: at least one run overran its period) and
    `late` counts the runs that did."""
    s = clock.vectors.get(vector)
    if s is None or not s.count:
        return {'period_cycles': period_cycles, 'runs': 0}
    worst = s.wall_max
    walls = sorted(s.walls)
    p99 = walls[min(len(walls) - 1, int(len(walls) * 0.99))] if walls else 0
    return {'period_cycles': period_cycles, 'runs': s.count,
            'worst_cycles': worst, 'p99_cycles': p99,
            'mean_cycles': round(s.wall / s.count, 1),
            'late': sum(1 for w in walls if w > period_cycles),
            'worst_fraction': round(worst / period_cycles, 4),
            'margin': round(1 - worst / period_cycles, 4)}


def measured_period(clock, vector):
    """Median cycles between successive entries of `vector`, or None."""
    s = clock.vectors.get(vector)
    if s is None or len(s.starts) < 3:
        return None
    st = list(s.starts)
    gaps = sorted(b - a for a, b in zip(st, st[1:]))
    return gaps[len(gaps) // 2]
