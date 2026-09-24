"""Strict mode: hold the firmware to what the MCF5441x would let it do.

The emulator is lenient by design. An access to an address it does not model
maps a page of zeros and carries on (harness.Machine._fault); Unicorn's CPU
has an FPU the chip lacks; exceptions the device would take into a crash
handler are just more vectors. That is right for running firmware that
already works on the hardware, and wrong for asking whether a custom build
will. Strict mode watches for everything the device would not tolerate and
reports it, and fails the run on the first one unless asked to keep going.

What it checks, and where each rule comes from (MCF54418RM):

  memory map    Table 1-2 and 1.8.1: SDRAM 0x4000_0000-0x7FFF_FFFF (the
                fitted DDR repeats through it; Machine.set_ddr makes every
                alias the same memory, as on the device), internal SRAM at
                RAMBAR (64 KB; the rest of 0x8000_0000-0x8BFF_FFFF is its
                wrap-around alias, which the emulator does not alias), Rapid
                GPIO 0x8C00_0000, reserved 0x9000_0000-0xBFFF_FFFF,
                FlexBus 0x0000_0000-0x3FFF_FFFF and 0xC000_0000-0xDFFF_FFFF
                (only inside a chip select the firmware configured; "the
                device also includes a bus monitor that generates a bus error
                for unterminated cycles", 20.4), and the two peripheral bus
                controllers, whose 16 KB slots are listed in Tables 1-3 and
                1-4 ("any slot not illustrated is reserved").
  execution     code only from DDR or SRAM.
  instructions  no float instruction (the MCF5441x has no FPU: line F), no
                other unimplemented line-F or illegal opcode, and no MOVEC
                outside the image the harness pre-scanned (emu/cfisa.py).
  exceptions    access error, address error, illegal instruction, divide by
                zero, privilege violation, trace, line A, line F, debug,
                format error and spurious interrupt, taken or unhandled.
  watchdog      13.2.1: if the firmware enables the core watchdog, it must
                service CWSR (0x55 then 0xAA) within 2^CWT core clocks.

What it does not do is fault the run the way the device would: the access
still returns zeros, so everything after the first violation is the
emulator's guess, not the device's behaviour. That is why the default stops
at the first one.

It also reports what the emulator stands in for, because a pass is only as
good as the models behind it:

  unmodelled    every peripheral slot the firmware touched that the
                emulator has no model for (its reads returned zeros), with
                counts;
  stand-ins     the host shortcuts and faked waits the run used
                (longrun.build's ev['stand_ins'], the unblock count, the
                depack clamp), so two builds can be compared on them.

A violation the stock firmware also makes is marked `in_stock` when a
baseline report is supplied (emu/fwcheck.py does): the device evidently
tolerates it, or the emulator is wrong about it, and it does not fail a
custom build.
"""
import collections

from unicorn import UC_HOOK_BLOCK, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.m68k_const import UC_M68K_REG_PC

from emu import cfisa

KB, MB = 1024, 1024 * 1024

# Table 1-3: peripheral bus controller 0, 16 KB slots at 0xFC00_0000.
PBC0 = {
    1: 'crossbar switch', 2: 'FlexBus', 8: 'FlexCAN 0', 9: 'FlexCAN 1',
    14: 'I2C 1', 15: 'DSPI 1', 16: 'SCM', 17: 'eDMA controller',
    18: 'interrupt controller 0', 19: 'interrupt controller 1',
    20: 'interrupt controller 2', 21: 'interrupt controller IACK',
    22: 'I2C 0', 23: 'DSPI 0', 24: 'UART0', 25: 'UART1', 26: 'UART2',
    27: 'UART3', 28: 'DMA timer 0', 29: 'DMA timer 1', 30: 'DMA timer 2',
    31: 'DMA timer 3', 32: 'PIT 0', 33: 'PIT 1', 34: 'PIT 2', 35: 'PIT 3',
    36: 'edge port 0', 37: 'ADC', 38: 'DAC 0', 39: 'DAC 1',
    42: 'real-time clock', 43: 'SIM', 44: 'USB On-the-Go', 45: 'USB host',
    46: 'DDR controller', 47: 'SSI 0', 48: 'PLL', 49: 'RNG', 50: 'SSI 1',
    51: 'eSDHC', 53: 'MAC-NET0', 54: 'MAC-NET1', 55: 'L2 switch 0',
    56: 'L2 switch 1', 63: 'NAND flash controller',
}
# Table 1-4: peripheral bus controller 1, 16 KB slots at 0xEC00_0000.
PBC1 = {
    2: '1-Wire', 4: 'I2C 2', 5: 'I2C 3', 6: 'I2C 4', 7: 'I2C 5',
    14: 'DSPI 2', 15: 'DSPI 3', 24: 'UART4', 25: 'UART5', 26: 'UART6',
    27: 'UART7', 28: 'UART8', 29: 'UART9', 34: 'mcPWM',
    36: 'CCM, reset controller, power management', 37: 'GPIO (pin mux)',
}
PBC0_BASE, PBC1_BASE, SLOT = 0xFC000000, 0xEC000000, 0x4000

SLOTS = {}
for _n, _name in PBC0.items():
    SLOTS[PBC0_BASE + _n * SLOT] = _name
for _n, _name in PBC1.items():
    SLOTS[PBC1_BASE + _n * SLOT] = _name
del _n, _name

# What the emulator does with each slot the Digitakt and Digitone use:
# 'model' has a model of its own, 'partial' models some registers or some
# behaviour, 'stand-in' only forces status bits so the firmware does not
# wait. Every other slot reads zeros.
MODELLED = {
    0xFC044000: ('model', 'emu/edma.py, emu/edma_sw.py'),
    0xFC048000: ('partial', 'priority levels read; masks not modelled'),
    0xFC04C000: ('partial', 'priority levels read; masks not modelled'),
    0xFC050000: ('partial', 'priority levels read; masks and INTFRCL2 not modelled'),
    0xFC05C000: ('stand-in', 'DSPI 0 status forced ready (longrun.build)'),
    0xFC070000: ('model', 'emu/dtim.py'), 0xFC074000: ('model', 'emu/dtim.py'),
    0xFC078000: ('model', 'emu/dtim.py'), 0xFC07C000: ('model', 'emu/dtim.py'),
    0xFC080000: ('model', 'emu/pit.py'), 0xFC084000: ('model', 'emu/pit.py'),
    0xFC088000: ('model', 'emu/pit.py'), 0xFC08C000: ('model', 'emu/pit.py'),
    0xFC0BC000: ('model', 'emu/ssi.py'), 0xFC0C8000: ('model', 'emu/ssi.py'),
    0xFC0CC000: ('model', 'emu/esdhc.py'),
    0xEC070000: ('model', 'UART8 panel link (longrun.build, emu/edma.py)'),
    0xEC094000: ('partial', 'emu/gpio.py: the SD card-detect loop only'),
    0xEC038000: ('stand-in', 'DSPI 2 transmit-done forced (longrun.build)'),
}

# Exceptions a working firmware never takes (MCF54418RM Table 3-15 vector
# assignments): the device would run the firmware's crash handler.
ERROR_VECTORS = {
    2: 'access error', 3: 'address error', 4: 'illegal instruction',
    5: 'divide by zero', 8: 'privilege violation', 9: 'trace',
    10: 'unimplemented line-A opcode', 11: 'unimplemented line-F opcode',
    12: 'non-PC breakpoint debug interrupt', 13: 'PC breakpoint debug interrupt',
    14: 'format error', 24: 'spurious interrupt',
}

# Emulator code and data that are not firmware: the srtrap trampolines and
# the stack harness.call() uses, both in the 1 MB page at 0x1000_0000.
EMULATOR_PAGES = ((0x10000000, 0x10100000),)

SCM_CWCR, SCM_CWSR = 0xFC040016, 0xFC04001B
FLEXBUS = 0xFC008000


class StrictViolation(RuntimeError):
    pass


class Strict:
    """Install on a Machine before it runs; read `report()` after.

    ddr_size: the fitted DDR (the Machine should decode it too, set_ddr).
    rambar: where the core sees its 64 KB SRAM.
    stop: stop emulation at the first violation (emu_stop from the hook;
        the caller sees `halted`).
    clock: an emu.cftiming.CycleClock, whose block hook is shared; else
        strict mode adds its own.
    cycles: callable -> core cycles elapsed, for the watchdog.
    """

    def __init__(self, m, ddr_size=64 * MB, rambar=0x80000000, stop=True,
                 clock=None, cycles=None, prescanned=None):
        self.m = m
        self.ddr_size = int(ddr_size)
        self.rambar = rambar
        self.stop = stop
        self.cycles = cycles
        self.prescanned = prescanned        # (lo, hi) the ISA hooks cover
        self.violations = collections.OrderedDict()   # key -> record
        self.halted = None
        self.slot_use = collections.Counter()          # (slot, 'r'|'w') -> n
        self.flexbus = [[0, 0, 0] for _ in range(6)]   # CSAR, CSMR, CSCR
        self.watchdog = {'enabled': False, 'cwcr': 0, 'serviced': 0,
                         'last_service': None, 'period': None}
        self._cwsr_last = None
        self._seen_blocks = set()
        self.blocks_checked = 0
        self._install(clock)

    # -- recording ---------------------------------------------------------------
    def _pc(self):
        try:
            return self.m.uc.reg_read(UC_M68K_REG_PC)
        except Exception:                               # noqa: BLE001
            return 0

    def violation(self, kind, what, addr=None, pc=None, key=None):
        pc = self._pc() if pc is None else pc
        key = key or (kind, addr if kind not in ('memory',) else addr, pc)
        rec = self.violations.get(key)
        if rec is None:
            rec = {'kind': kind, 'what': what, 'pc': pc, 'addr': addr,
                   'count': 0}
            self.violations[key] = rec
            if self.stop and self.halted is None:
                self.halted = '%s: %s at pc=0x%08x' % (kind, what, pc)
                try:
                    self.m.uc.emu_stop()
                except Exception:                       # noqa: BLE001
                    pass
        rec['count'] += 1

    # -- the memory map ----------------------------------------------------------
    def _flexbus_hit(self, addr):
        for csar, csmr, _cscr in self.flexbus:
            if not csmr & 1:
                continue
            mask = (csmr & 0xFFFF0000) | 0xFFFF
            if (addr & ~mask) & 0xFFFFFFFF == (csar & ~mask) & 0xFFFFFFFF:
                return True
        return False

    def classify(self, addr):
        """-> (ok, what) for a data access to `addr` by the core."""
        for lo, hi in EMULATOR_PAGES:
            if lo <= addr < hi:
                return True, 'emulator'
        if 0x40000000 <= addr <= 0x7FFFFFFF:
            return True, 'DDR'
        if self.rambar <= addr < self.rambar + 64 * KB:
            return True, 'SRAM'
        if 0x80000000 <= addr <= 0x8BFFFFFF:
            return False, ('the SRAM backdoor outside RAMBAR\'s 64 KB: it '
                           'wraps onto SRAM on the device, and is separate '
                           'memory here')
        if 0x8C000000 <= addr <= 0x8FFFFFFF:
            return True, 'Rapid GPIO'
        if 0x90000000 <= addr <= 0xBFFFFFFF:
            return False, 'reserved space (Table 1-2: must not be accessed)'
        if addr < 0x40000000 or 0xC0000000 <= addr <= 0xDFFFFFFF:
            if self._flexbus_hit(addr):
                return True, 'FlexBus'
            return False, ('FlexBus space with no chip select configured: '
                           'the bus monitor ends it with a bus error')
        slot = addr & ~(SLOT - 1)
        name = SLOTS.get(slot)
        if name is None:
            return False, 'a reserved peripheral slot (Tables 1-3, 1-4)'
        return True, name

    def _on_mem(self, uc, typ, addr, size, value, data):
        ok, what = self.classify(addr)
        if not ok:
            kind = 'write' if typ == UC_MEM_WRITE_T else 'read'
            self.violation('memory', '%s of %d byte(s) at 0x%08x: %s'
                           % (kind, size, addr, what), addr=addr & ~0xF,
                           key=('memory', addr & ~0xF, self._pc()))

    def _on_periph(self, uc, typ, addr, size, value, data):
        slot = addr & ~(SLOT - 1)
        name = SLOTS.get(slot)
        write = typ == UC_MEM_WRITE_T
        if name is None:
            self.violation('memory', '%s of %d byte(s) at 0x%08x: a reserved '
                           'peripheral slot (Tables 1-3, 1-4)'
                           % ('write' if write else 'read', size, addr),
                           addr=addr, key=('memory', addr, self._pc()))
            return
        self.slot_use[(slot, 'w' if write else 'r')] += 1
        if write:
            if FLEXBUS <= addr < FLEXBUS + 6 * 12:
                self._flexbus_write(addr, size, value)
            elif addr == SCM_CWCR or (size == 4 and addr == SCM_CWCR - 2):
                self._cwcr_write(value & 0xFFFF)
            elif addr == SCM_CWSR:
                self._cwsr_write(value & 0xFF)

    def _flexbus_write(self, addr, size, value):
        off = addr - FLEXBUS
        n, reg = off // 12, (off % 12) // 4
        if size == 4 and n < 6:
            self.flexbus[n][reg] = value & 0xFFFFFFFF

    # -- the core watchdog -------------------------------------------------------
    def _now(self):
        return self.cycles() if self.cycles is not None else None

    def _cwcr_write(self, value):
        wd = self.watchdog
        wd['cwcr'] = value
        wd['enabled'] = bool(value & 0x80)
        cwt = value & 0x1F
        wd['period'] = 1 << max(8, cwt)
        wd['last_service'] = self._now()

    def _cwsr_write(self, value):
        if value == 0xAA and self._cwsr_last == 0x55:
            self.watchdog['serviced'] += 1
            self.watchdog['last_service'] = self._now()
        self._cwsr_last = value

    def check_watchdog(self):
        """Call between steps: a violation if the watchdog would have fired."""
        wd = self.watchdog
        now = self._now()
        if not wd['enabled'] or now is None or wd['last_service'] is None:
            return
        if now - wd['last_service'] > wd['period']:
            self.violation('watchdog', 'the core watchdog (CWCR 0x%04x) was '
                           'not serviced within its %d cycles'
                           % (wd['cwcr'], wd['period']), key=('watchdog',))
            wd['last_service'] = now

    # -- instructions and execution ----------------------------------------------
    def check_block(self, addr, insns):
        """Check a newly decoded block: where it runs and what it holds."""
        self.blocks_checked += 1
        ok = (0x40000000 <= addr <= 0x7FFFFFFF
              or self.rambar <= addr < self.rambar + 64 * KB
              or any(lo <= addr < hi for lo, hi in EMULATOR_PAGES))
        if not ok:
            self.violation('execute', 'code runs at 0x%08x, outside DDR and '
                           'SRAM' % addr, addr=addr, pc=addr,
                           key=('execute', addr))
        for at, ins in insns:
            if ins.flags & cfisa.FPU:
                self.violation('instruction', 'a float instruction at 0x%08x: '
                               'the MCF5441x has no FPU, so the device takes '
                               'a line-F exception here' % at, addr=at, pc=at,
                               key=('instruction', at))
            elif ins.flags & cfisa.LINEF:
                self.violation('instruction', 'an unimplemented line-F opcode '
                               'at 0x%08x' % at, addr=at, pc=at,
                               key=('instruction', at))
            elif ins.flags & cfisa.ILLEGAL:
                self.violation('instruction', 'an opcode that is not ColdFire '
                               'ISA_C at 0x%08x' % at, addr=at, pc=at,
                               key=('instruction', at))
            elif ins.flags & cfisa.MOVEC and self.prescanned is not None:
                lo, hi = self.prescanned
                if not lo <= at < hi:
                    self.violation('instruction', 'MOVEC at 0x%08x, outside '
                                   'the image the harness intercepts it in '
                                   '(Unicorn aborts on some control registers)'
                                   % at, addr=at, pc=at,
                                   key=('instruction', at))

    def _on_block(self, uc, addr, size, data):
        if addr in self._seen_blocks:
            return
        self._seen_blocks.add(addr)
        try:
            raw = bytes(uc.mem_read(addr, size + 6))
        except Exception:                               # noqa: BLE001
            raw = bytes(uc.mem_read(addr, size)) + bytes(6)
        read = cfisa.reader(raw, addr)
        insns, off = [], 0
        while off < size:
            ins = cfisa.decode(read(addr + off), addr + off)
            insns.append((addr + off, ins))
            off += ins.length
        self.check_block(addr, insns)

    # -- exceptions --------------------------------------------------------------
    def _raise_vector(self, vec, from_instruction=False, level=None):
        if from_instruction and vec in ERROR_VECTORS:
            pc = self._pc()
            self.violation('exception', '%s (vector %d) at 0x%08x'
                           % (ERROR_VECTORS[vec], vec, pc), pc=pc,
                           key=('exception', vec, pc))
        return self._orig_raise(vec, from_instruction=from_instruction,
                                level=level)

    def check_halt(self):
        """Call between steps: an unhandled vector is a violation."""
        vec = self.m.halt_vec
        if vec is not None:
            self.violation('exception', 'vector %d (%s) has no handler'
                           % (vec, ERROR_VECTORS.get(vec, 'an interrupt')),
                           key=('unhandled', vec))

    # -- installation ------------------------------------------------------------
    def _install(self, clock):
        uc = self.m.uc
        rw = UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE
        ranges = [
            (0x00000000, 0x3FFFFFFF),       # FlexBus (and the emulator page)
            (self.rambar + 64 * KB, 0x8BFFFFFF) if self.rambar == 0x80000000
            else (0x80000000, 0x8BFFFFFF),
            (0x90000000, 0xBFFFFFFF),       # reserved
            (0xC0000000, 0xDFFFFFFF),       # FlexBus
        ]
        for lo, hi in ranges:
            uc.hook_add(rw, self._on_mem, begin=lo, end=hi)
        # Peripheral space: every access, to check the slot and count use.
        uc.hook_add(rw, self._on_periph, begin=0xE0000000, end=0xFFFFFFFF)
        self._orig_raise = self.m.raise_vector
        self.m.raise_vector = self._raise_vector
        if clock is not None:
            prev = clock.on_new_block

            def on_new(block, insns):
                if prev is not None:
                    prev(block, insns)
                self.check_block(block.addr, insns)
            clock.on_new_block = on_new
        else:
            uc.hook_add(UC_HOOK_BLOCK, self._on_block)

    # -- the report --------------------------------------------------------------
    def report(self, baseline=None, stand_ins=None):
        """-> dict. `baseline` is the report of the same check on the stock
        firmware: violations it also has are marked in_stock and do not
        fail. `stand_ins` is a dict of counts to include."""
        stock = set()
        if baseline:
            for v in baseline.get('violations', ()):
                stock.add((v['kind'], v['what']))
        out = []
        for rec in self.violations.values():
            r = dict(rec)
            r['pc'] = '0x%08x' % rec['pc'] if rec['pc'] is not None else None
            if rec['addr'] is not None:
                r['addr'] = '0x%08x' % rec['addr']
            r['in_stock'] = (rec['kind'], rec['what']) in stock
            out.append(r)
        unmodelled, modelled = [], []
        slots = collections.defaultdict(lambda: {'reads': 0, 'writes': 0})
        for (slot, kind), n in self.slot_use.items():
            slots[slot]['reads' if kind == 'r' else 'writes'] += n
        for slot, use in sorted(slots.items()):
            entry = {'slot': '0x%08x' % slot, 'peripheral': SLOTS.get(slot),
                     'reads': use['reads'], 'writes': use['writes']}
            how = MODELLED.get(slot)
            if how is None:
                unmodelled.append(entry)
            else:
                entry['emulator'] = '%s: %s' % how
                modelled.append(entry)
        failing = [v for v in out if not v['in_stock']]
        return {
            'passed': not failing,
            'violations': out,
            'failing': len(failing),
            'halted': self.halted,
            'unmodelled_peripherals': unmodelled,
            'modelled_peripherals': modelled,
            'watchdog': dict(self.watchdog),
            'flexbus_chip_selects': [
                {'cs': n, 'csar': '0x%08x' % a, 'csmr': '0x%08x' % mm}
                for n, (a, mm, _c) in enumerate(self.flexbus) if mm & 1],
            'blocks_checked': self.blocks_checked,
            'ddr_restore_conflicts': getattr(self.m, 'ddr_restore_conflicts', 0),
            'stand_ins': dict(stand_ins or {}),
        }


def stand_ins(ev):
    """-> the host stand-ins a run used: from longrun.build's `ev`, or from
    the stats dict emu/dspboot.py's cold boot keeps (flash reads served,
    completion-semaphore kicks, idle-loop reschedules)."""
    if 'sem_kicks' in ev and 'reads' in ev:                 # dspboot.run
        return {'flash_read': len(ev.get('reads') or ()),
                'completion_sem': ev.get('sem_kicks', 0),
                'depack_clamps': ev.get('depack_clamps', 0),
                'idle_spin_passes': ev.get('spin', 0)}
    out = dict(ev.get('stand_ins') or {})
    out['unblock_satisfied'] = ev.get('satisfied', 0)
    out['depack_clamps'] = ev.get('depack_clamps', 0)
    for name in ('softfloat', 'bitmap'):
        counter = ev.get(name)
        if counter:
            out[name + '_shortcuts'] = sum(counter.values())
    return out


# unicorn's memory-access type for a write, as the hooks see it.
try:
    from unicorn import UC_MEM_WRITE as UC_MEM_WRITE_T
except ImportError:                                     # pragma: no cover
    UC_MEM_WRITE_T = 17


def pack_violations(report):
    """-> one line per violation, for a console summary."""
    lines = []
    for v in report.get('violations', ()):
        lines.append('%s%-11s x%-6d %s' % ('  (stock) ' if v.get('in_stock')
                                           else '  FAIL    ', v['kind'],
                                           v['count'], v['what']))
    return lines

