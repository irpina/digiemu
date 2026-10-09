"""I2C0 as a bus master, and the register-file devices hung off it.

The Model:Cycles and Model:Samples set their audio codec up over I2C0
(MCF54418RM chapter 30); the Digitakt and Digitone never touch it. Nothing
backed the controller, so the first transfer waited forever for its
interrupt flag, and the boot stopped with two tasks created.

The firmware drives the controller by polling, never by interrupt:

  write   START (I2CR MTX|MSTA), the address byte, wait for IIF and clear
          it, then the register and its value the same way, then STOP.
  read    the register write as above but STOP instead of a value, wait for
          the bus to go idle (IBB clear), START again, the address byte with
          R/W set, then MTX and TXAK cleared/set for receive, a dummy read of
          I2DR to start the byte, wait for IIF, STOP, and read I2DR.

So the model is the controller's flags, not its timing: every byte moves at
once, IIF and ICF are set after it and stay set until the firmware writes
I2SR, IBB follows MSTA, and RXAK says whether anything answered the address.
A read returns the byte received by the previous one (the dummy read is what
fetches the first), as on the hardware.

A device is a RegisterFile: the first byte written after its address sets
the register pointer, each further byte is stored there and advances it, and
reads come back from it, advancing the same way. That is what the Models'
codec needs: its health check (task 0x40044188 on Model:Cycles 1.13) reads
back register 1 and sets the codec up again, every pass, unless it reads the
0x17 that set-up wrote there.
"""
from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE

I2C0 = 0xFC058000
I2ADR, I2FDR, I2CR, I2SR, I2DR = 0x00, 0x04, 0x08, 0x0C, 0x10

# I2CR
IEN, IIEN, MSTA, MTX, TXAK, RSTA = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04
# I2SR
ICF, IAAS, IBB, IAL, SRW, IIF, RXAK = 0x80, 0x40, 0x20, 0x10, 0x04, 0x02, 0x01


class RegisterFile:
    """An I2C device with an auto-incrementing register pointer."""

    def __init__(self, size=256):
        self.regs = bytearray(size)
        self.pointer = 0
        self._fresh = True          # the next written byte is the pointer

    def start(self):
        self._fresh = True

    def write(self, byte):
        if self._fresh:
            self.pointer = byte % len(self.regs)
            self._fresh = False
            return
        self.regs[self.pointer] = byte
        self.pointer = (self.pointer + 1) % len(self.regs)

    def read(self):
        byte = self.regs[self.pointer]
        self.pointer = (self.pointer + 1) % len(self.regs)
        return byte


class I2cBus:
    """One I2C controller in master mode, with `devices` = {7-bit address:
    device}. Install with `install`; the state is a checkpoint component."""

    def __init__(self, base=I2C0, devices=None):
        self.base = base
        self.devices = dict(devices or {})
        # I2CR is not shadowed: guest memory holds what was last written
        # (and a snapshot restores it), and a read-modify-write of a stale
        # copy cleared IEN on a resumed run -- the next transfer never
        # started and the codec task spun on IIF. `cr` is the value as of
        # the last write this model saw, for MTX.
        self.cr = 0
        self.sr = 0                 # IIF / ICF / RXAK / IAL; IBB is derived
        self.busy = False           # IBB: between START and STOP
        self.expect_address = False
        self.target = None          # the addressed device, or None (NACK)
        self.reading = False
        self.rx = 0xFF              # the byte the next I2DR read returns
        self.transfers = 0

    # -- the controller ------------------------------------------------------
    def status(self):
        return (self.sr & ~IBB) | (IBB if self.busy else 0)

    def write_cr(self, value, old=None):
        """I2CR <- `value`; `old` is what it held (default: the last write
        this model saw)."""
        if old is None:
            old = self.cr
        self.cr = value & 0xFF
        if not (value & IEN):
            self.busy = False
            return
        if (value & MSTA) and not (old & MSTA):
            self._start()
        elif (value & MSTA) and (value & RSTA):
            self._start()
        elif (old & MSTA) and not (value & MSTA):
            self.busy = False       # STOP
            self.target = None

    def _start(self):
        self.busy = True
        self.expect_address = True
        self.reading = False
        self.target = None

    def write_sr(self, value):
        # IIF and IAL are cleared by writing 0 to them; the rest read only.
        self.sr &= value | ~(IIF | IAL)

    def write_dr(self, byte):
        byte &= 0xFF
        if not self.busy or not (self.cr & MTX):
            return
        if self.expect_address:
            self.expect_address = False
            self.target = self.devices.get(byte >> 1)
            self.reading = bool(byte & 1)
            if self.target is not None:
                self.target.start()
        elif self.target is not None:
            self.target.write(byte)
        self.sr = (self.sr & ~RXAK) | ICF | IIF \
            | (0 if self.target is not None else RXAK)
        self.transfers += 1

    def read_dr(self):
        """-> the byte this read returns; receiving the next one sets IIF."""
        byte = self.rx
        if self.busy and not (self.cr & MTX) and self.reading:
            self.rx = self.target.read() if self.target is not None else 0xFF
            self.sr |= ICF | IIF
            self.transfers += 1
        return byte

    # -- the hooks -------------------------------------------------------------
    def install(self, m):
        base = self.base
        m.ensure(base)

        def on_read(uc, typ, addr, size, val, data):
            off = addr - base
            if off <= I2SR < off + size:
                uc.mem_write(base + I2SR, bytes([self.status()]))
            if off <= I2DR < off + size:
                uc.mem_write(base + I2DR, bytes([self.read_dr()]))

        def on_write(uc, typ, addr, size, val, data):
            for k in range(size):
                off = addr + k - base
                byte = (val >> (8 * (size - 1 - k))) & 0xFF
                if off == I2CR:
                    # The hook runs before the store lands: memory still
                    # holds the old value.
                    self.write_cr(byte, bytes(uc.mem_read(base + I2CR, 1))[0])
                elif off == I2SR:
                    self.write_sr(byte)
                elif off == I2DR:
                    self.write_dr(byte)
        m.uc.hook_add(UC_HOOK_MEM_READ, on_read, begin=base + I2SR,
                      end=base + I2DR)
        m.uc.hook_add(UC_HOOK_MEM_WRITE, on_write, begin=base + I2CR,
                      end=base + I2DR)
        return self

    # -- checkpoint ------------------------------------------------------------
    def checkpoint_state(self):
        return {'cr': self.cr, 'sr': self.sr, 'busy': self.busy,
                'rx': self.rx,
                'devices': {str(a): {'regs': list(d.regs),
                                     'pointer': d.pointer}
                            for a, d in self.devices.items()
                            if isinstance(d, RegisterFile)}}

    def restore_checkpoint_state(self, state):
        self.cr, self.sr = int(state['cr']), int(state['sr'])
        self.busy, self.rx = bool(state['busy']), int(state['rx'])
        self.expect_address, self.target, self.reading = False, None, False
        for addr, saved in state.get('devices', {}).items():
            dev = self.devices.get(int(addr))
            if isinstance(dev, RegisterFile) \
                    and len(saved['regs']) == len(dev.regs):
                dev.regs[:] = bytes(saved['regs'])
                dev.pointer = int(saved['pointer'])
