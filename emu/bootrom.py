"""Boot the way the device boots: through the bootstrap, from the SPI flash.

The emulator normally starts the OS by copying MAIN OS into DDR at
0x40000400 and jumping to its entry (emu/dspboot.py). On the device nothing
does that directly:

  1. The serial boot facility (MCF54418RM chapter 11) reads the SPI flash
     from address 0 at reset -- a clock divider, a load length and a reset
     configuration word -- copies the boot code into the 64 KB SRAM and
     takes the reset vector from it.
  2. That code, the bootstrap (container section id 2), sets up the PLL and
     the DDR controller, reads the OS container from the flash over DSPI 0,
     checks it, unpacks MAIN OS into DDR and jumps to it.

So "the OS runs in the emulator" says nothing about step 2: an image the
bootstrap would reject, or unpack differently, or a container laid out so it
no longer fits where the bootstrap looks, only shows up on the device. This
module runs step 2 for real. The flash holds the container where the updater
writes it (0x80000, the same place the OS's own flash reads find it), the
bootstrap runs from SRAM on the ColdFire as the device's does, and the run
ends when it jumps into DDR. What it wrote there is then compared with what
the emulator's direct load puts there.

Step 1 is not emulated: the facility is hardware with no code to run. Its
effect is: the bootstrap image in SRAM and the reset vector from it. The
section is stored as [u32 length][image], and the image links at
0x80000400: its first longwords are the initial stack pointer
(0x80010000, the top of SRAM), the entry point and the bootstrap version
the updater compares (at 0x80000408, docs/FINDINGS.md "The version gate").

The hardware the bootstrap touches is modelled as far as it needs:

  SpiFlash   the NOR part the bootstrap identifies (RDID 01 20 18: an
             S25FL127S-class 16 MB flash, docs/FINDINGS.md), with READ,
             FAST_READ, status, write enable, page program and erase.
             Program and erase work as on the part (bits only clear;
             erase sets them), and every write is recorded: a boot that
             writes the flash is worth knowing about.
  Dspi       the DSPI master (MCF54418RM chapter 40): PUSHR with its
             command half (continuous chip select, end of queue, chip
             selects), POPR, the status flags and FIFO counters, the
             MCR FIFO clears and HALT. Frames complete at once. The flash
             is on PCS1.
  PanelLink  the front panel's controller on UART8, as far as the
             bootstrap asks: the key groups, the UI card's type and the
             serial number (devices/*.toml [boot]). With no answer the
             bootstrap decides there is no panel and passes the OS boot
             flags that park it.
  BootHardware  the PLL locked (8.2.3), the DDR controller initialised
             (DDR_CR27 bit 3), the PIT delays expired at once, and the
             board's GPIO straps ([boot] straps).
  the PITs   emu.pit.Pits: the Digitone's bootstrap hands over to its
             updater, a small RTOS on the PIT0 tick.

Nothing the bootstrap does to the flash is allowed to go unseen: every
program and erase is recorded. It is not expected to write: a boot that
does fails the check (emu/fwcheck.py).

The result carries the handoff: what the bootstrap leaves the OS (its
stack with the boot flags, SRAM, the DDR it wrote, the peripheral
registers it programmed). emu/dspboot.py run(handoff=...) starts the OS
from there instead of from the emulator's direct load, which passes no
boot flags. See docs/FINDINGS.md, "The mk1 boot chain".
"""
import collections
import struct

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UcError
from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR

MB = 1 << 20
FLASH_SIZE = 16 * MB
CONTAINER_AT = 0x80000          # emu/dspboot.py SLOT: where the OS reads it
SRAM, SRAM_SIZE = 0x80000000, 0x10000
BOOT_LINK = 0x80000400          # where the bootstrap image is linked
DSPI0 = 0xFC05C000
FLASH_PCS = 1                   # the DSPI 0 chip select the flash is on
PLL = 0xFC0C0000
DDRMC = 0xFC0B8000

# RDID: manufacturer 0x01, device 0x2018, then the S25FL127S ID-CFI length
# and a uniform 256 KB sector architecture, family 0x80.
FLASH_ID = bytes((0x01, 0x20, 0x18, 0x4D, 0x00, 0x80))
# WDEBUG's second word (MCF54418RM Table 3-17).
WDEBUG_EXT = bytes((0x00, 0x03))


def bootstrap_image(section):
    """-> (image, sp, pc, version) from the decoded section id 2 bytes."""
    if len(section) < 16:
        raise ValueError('the bootstrap section is too short')
    length = struct.unpack_from('>I', section, 0)[0]
    image = section[4:4 + length]
    if len(image) != length:
        raise ValueError('the bootstrap section says %d bytes and holds %d'
                         % (length, len(section) - 4))
    sp, pc, version = struct.unpack_from('>III', image, 0)
    return image, sp, pc, version


class SpiFlash:
    """A serial NOR flash on a chip select."""

    PAGE = 512

    def __init__(self, data=None, size=FLASH_SIZE):
        self.mem = bytearray(b'\xff' * size) if data is None else bytearray(data)
        if len(self.mem) < size:
            self.mem += b'\xff' * (size - len(self.mem))
        self.size = len(self.mem)
        self.wel = False
        self.status2 = 0
        self.config = 0
        self.reads = collections.Counter()      # 64 KB block -> bytes read
        self.writes = []                        # (op, addr, length)
        self.commands = collections.Counter()
        self._reset_frame()

    def _reset_frame(self):
        self.cmd = None
        self.addr = 0
        self.need = 0          # address bytes still to come
        self.dummy = 0
        self.pos = 0           # bytes of data phase so far
        self.prog = None

    def select(self):
        self._reset_frame()

    def deselect(self):
        if self.cmd in (0x02, 0x12) and self.prog:
            addr, data = self.prog
            for i, b in enumerate(data):
                a = (addr & ~(self.PAGE - 1)) | ((addr + i) & (self.PAGE - 1))
                if a < self.size:
                    self.mem[a] &= b
            self.writes.append(('program', addr, len(data)))
            self.wel = False
        self._reset_frame()

    def _erase(self, addr, size):
        start = addr & ~(size - 1)
        for a in range(start, min(start + size, self.size)):
            self.mem[a] = 0xFF
        self.writes.append(('erase', start, size))
        self.wel = False

    def transfer(self, byte):
        """One byte out on SI while selected; -> the byte on SO."""
        if self.cmd is None:
            self.cmd = byte
            self.commands[byte] += 1
            if byte in (0x03, 0x0B, 0x02, 0xD8, 0x20, 0x21, 0xDC):
                self.need = 4 if byte in (0x21, 0xDC) else 3
                self.dummy = 1 if byte == 0x0B else 0
            elif byte in (0x13, 0x0C, 0x12):
                self.need = 4
                self.dummy = 1 if byte == 0x0C else 0
            elif byte == 0x06:
                self.wel = True
            elif byte == 0x04:
                self.wel = False
            elif byte in (0xC7, 0x60):
                if self.wel:
                    self._erase(0, self.size)
            return 0xFF
        if self.need:
            self.addr = (self.addr << 8) | byte
            self.need -= 1
            if not self.need and self.cmd in (0xD8, 0xDC, 0x20, 0x21):
                if self.wel:
                    self._erase(self.addr, 0x40000 if self.cmd in (0xD8, 0xDC)
                                else 0x1000)
            return 0xFF
        if self.dummy:
            self.dummy -= 1
            return 0xFF
        cmd = self.cmd
        if cmd in (0x03, 0x0B, 0x13, 0x0C):
            a = (self.addr + self.pos) % self.size
            self.pos += 1
            self.reads[a >> 16] += 1
            return self.mem[a]
        if cmd == 0x9F:
            b = FLASH_ID[self.pos] if self.pos < len(FLASH_ID) else 0xFF
            self.pos += 1
            return b
        if cmd == 0x05:
            return 0x02 if self.wel else 0x00     # never busy: writes are instant
        if cmd == 0x07:
            return self.status2
        if cmd == 0x35:
            return self.config
        if cmd in (0x02, 0x12):
            if self.wel:
                if self.prog is None:
                    self.prog = (self.addr, bytearray())
                self.prog[1].append(byte)
            return 0xFF
        return 0xFF

    def read_summary(self):
        """-> [(start, end)] of the flash ranges read, 64 KB granular."""
        out = []
        for blk in sorted(self.reads):
            a = blk << 16
            if out and out[-1][1] == a:
                out[-1][1] = a + 0x10000
            else:
                out.append([a, a + 0x10000])
        return [tuple(r) for r in out]


class Dspi:
    """A DSPI in master mode whose frames complete immediately.

    devices: {pcs bit number: device with select/deselect/transfer}."""

    MCR, TCR, CTAR, SR, RSER, PUSHR, POPR = 0x00, 0x08, 0x0C, 0x2C, 0x30, 0x34, 0x38

    def __init__(self, m, base=DSPI0, devices=None):
        self.m = m
        self.base = base
        self.devices = dict(devices or {})
        self.mcr = 0x00004001        # MDIS and HALT at reset
        self.ctar = [0x78000000] * 8  # FMSZ 15 (16-bit) at reset
        self.sr = 0x02000000          # TFFF
        self.rser = 0
        self.tcr = 0
        self.rx = collections.deque()
        self.cmd_half = 0
        self.selected = None
        self.frames = 0
        self.unknown = collections.Counter()
        uc = m.uc
        uc.hook_add(UC_HOOK_MEM_READ, self._on_read, begin=base, end=base + 0xFF)
        uc.hook_add(UC_HOOK_MEM_WRITE, self._on_write, begin=base, end=base + 0xFF)

    # -- status -----------------------------------------------------------------
    def status(self):
        sr = self.sr & ~(0x0000FFFF | 0x40000000 | 0x00020000)
        sr |= 0x02000000                                   # TFFF: never full
        if not (self.mcr & 1) and not (sr & 0x10000000):   # HALT / EOQF
            sr |= 0x40000000                               # TXRXS
        if self.rx:
            sr |= 0x00020000                               # RFDF
        sr |= min(len(self.rx), 15) << 4                   # RXCTR
        return sr

    # -- frames -----------------------------------------------------------------
    def _frame(self, command, data):
        pcs = (command >> 16) & 0xFF
        dev_bit = None
        for bit in range(8):
            if pcs & (1 << bit):
                dev_bit = bit
                break
        dev = self.devices.get(dev_bit)
        if self.selected is not None and self.selected is not dev:
            self.selected.deselect()
            self.selected = None
        if dev is not None and self.selected is None:
            dev.select()
            self.selected = dev
        bits = ((self.ctar[(command >> 28) & 7] >> 27) & 0xF) + 1
        nbytes = max(1, (bits + 7) // 8)
        out = 0
        for k in range(nbytes - 1, -1, -1):
            byte = (data >> (8 * k)) & 0xFF
            got = dev.transfer(byte) if dev is not None else 0xFF
            out = (out << 8) | got
        self.rx.append(out & ((1 << bits) - 1))
        self.frames += 1
        self.sr |= 0x80000000                              # TCF
        if command & 0x08000000:                           # EOQ
            self.sr |= 0x10000000
        if not command & 0x80000000 and self.selected is not None:
            self.selected.deselect()
            self.selected = None

    # -- hooks --------------------------------------------------------------------
    def _put(self, addr, size, value):
        try:
            if size == 4:
                self.m.uc.mem_write(addr, struct.pack('>I', value & 0xFFFFFFFF))
            elif size == 2:
                self.m.uc.mem_write(addr, struct.pack('>H', value & 0xFFFF))
            else:
                self.m.uc.mem_write(addr, bytes((value & 0xFF,)))
        except UcError:
            pass

    def _on_read(self, uc, typ, addr, size, value, data):
        off = addr - self.base
        if self.SR <= off < self.SR + 4:
            full = self.status()
            self._put(self.base + self.SR, 4, full)
        elif self.POPR <= off < self.POPR + 4:
            if off == self.POPR or off == self.POPR + 2:
                v = self.rx.popleft() if self.rx else 0
                self._put(self.base + self.POPR, 4, v)
        elif off == self.MCR:
            self._put(addr, 4, self.mcr)

    def _on_write(self, uc, typ, addr, size, value, data):
        off = addr - self.base
        if off == self.MCR and size == 4:
            if value & 0x800:                               # CLR_TXF
                pass
            if value & 0x400:                               # CLR_RXF
                self.rx.clear()
            self.mcr = value & ~0xC00
        elif off == self.TCR and size == 4:
            self.tcr = value
        elif self.CTAR <= off < self.CTAR + 32 and size == 4:
            self.ctar[(off - self.CTAR) // 4] = value
        elif off == self.SR and size == 4:
            self.sr &= ~(value & 0x9A0A0000)                # write 1 to clear
        elif off == self.RSER and size == 4:
            self.rser = value
        elif off == self.PUSHR and size == 4:
            self._frame(value >> 16 << 16, value & 0xFFFF)
        elif off == self.PUSHR and size == 2:
            self.cmd_half = value & 0xFFFF
        elif off == self.PUSHR + 2 and size == 2:
            self._frame(self.cmd_half << 16, value & 0xFFFF)
        else:
            self.unknown[(off, size)] += 1


def wdebug_sites(image, base):
    """-> [(addr, length)] of every WDEBUG in `image` loaded at `base`.

    WDEBUG (MCF54418RM Table 3-17; 0xFBC0 | EA, then 0x0003) writes the debug
    module's registers. QEMU's ColdFire translator aborts the whole process
    on it -- at translation, so no code hook can step over it the way
    harness.Machine does for MOVEC. The bootstrap has one, right after it
    points VBR at SRAM, configuring the debug module the emulator does not
    have."""
    out = []
    for off in range(0, len(image) - 3, 2):
        w = image[off] << 8 | image[off + 1]
        if (w & 0xFFC0) == 0xFBC0 and image[off + 2:off + 4] == WDEBUG_EXT:
            mode = (w >> 3) & 7
            out.append((base + off, 4 + (2 if mode in (5, 6) else 0)))
    return out


def step_over(m, sites):
    """Replace each (addr, length) instruction with a BRA.B past it, in guest
    memory only: the firmware image is not touched."""
    for addr, length in sites:
        m.uc.mem_write(addr, bytes((0x60, length - 2)))


class BootHardware:
    """What the bootstrap needs to read back from the clock module and the
    timers.

    Time is not modelled before the OS runs: the bootstrap's delays are
    busy-waits on a PIT's expiry flag, and every one of them expires at once.
    """

    PIT_BASES = (0xFC080000, 0xFC084000, 0xFC088000, 0xFC08C000)

    def __init__(self, m, straps=None):
        self.m = m
        # Board straps: GPIO pins whose level the board fixes, {address:
        # byte} (devices/*.toml [boot] straps). The Digitone's bootstrap
        # reads bit 3 of 0xEC09401B: low on a Digitone Keys, high on a
        # Digitone. Unmodelled GPIO reads zero, which made every Digitone a
        # Keys, waiting for nine key groups from a panel that has seven.
        self.straps = dict(straps or {})
        for addr in self.straps:
            m.uc.hook_add(UC_HOOK_MEM_READ, self._strap, begin=addr, end=addr)
        # PLL_SR: LOCK and LOCKS (MCF54418RM 8.2.3).
        m.mmio[PLL + 0x08] = 0x00000030
        # DDR_CR27: after programming the controller the bootstrap polls it
        # (0x80000910: `move.w d0,ccr; bmi`) for bit 3, the DRAM
        # initialisation-complete interrupt status, up to 1000 times; if it
        # never sets it records a DDR failure (0x80007706) and halts later.
        m.mmio[DDRMC + 0x6C] = 0x00000008
        self.pcsr = {base: 0 for base in self.PIT_BASES}
        self.pit_waits = collections.Counter()
        for base in self.PIT_BASES:
            m.uc.hook_add(UC_HOOK_MEM_WRITE, self._pcsr_write, begin=base,
                          end=base + 1)
            m.uc.hook_add(UC_HOOK_MEM_READ, self._pcsr_read, begin=base,
                          end=base + 1)

    def _strap(self, uc, typ, addr, size, value, data):
        try:
            uc.mem_write(addr, bytes((self.straps[addr] & 0xFF,)))
        except UcError:
            pass

    def _pcsr_write(self, uc, typ, addr, size, value, data):
        base = addr & ~0x3FFF
        v = value & 0xFFFF if size >= 2 else value & 0xFF
        # PIF (bit 2) is write-one-to-clear; the rest is control.
        self.pcsr[base] = (v & ~0x0004) | (self.pcsr[base] & 0x0004 & ~v)

    def _pcsr_read(self, uc, typ, addr, size, value, data):
        base = addr & ~0x3FFF
        v = self.pcsr[base]
        if v & 0x0001 and not v & 0x0008:     # EN, polled (PIE clear)
            v |= 0x0004                       # the count has run out
            self.pit_waits[base] += 1
        try:
            uc.mem_write(base, struct.pack('>H', v))
        except UcError:
            pass


class PanelLink:
    """The front-panel controller on UART8, as far as the bootstrap asks.

    The bootstrap sends `60 01` and waits for the panel to report every
    button group: six messages `[0x20 | group, mask]`, the same button
    report the OS reads (emu/panelin.py). It keeps them as a 16-byte key
    bitmap (0x8000771e) its startup checks test, and with no answer at all
    it decides there is no panel and passes the OS boot flags 0x60, which
    park the OS (0x4006932e). Its receive handler (0x800010de) takes one
    byte per UART8 interrupt, vector 180.

    `held` maps a group to the mask held at power-on: keys down while the
    device starts, which is how its startup menu is reached."""

    UART8 = 0xEC070000
    USR, UCR, URB, UIMR = (UART8 + 0x04, UART8 + 0x08, UART8 + 0x0C,
                           UART8 + 0x14)
    VECTOR = 180
    GROUPS = 6
    INTC1, SOURCE = 0xFC04C000, 52      # IMRH +0x08, SIMR +0x1C, CIMR +0x1D

    def __init__(self, m, held=None, card=4, groups=6):
        self.m = m
        self.held = dict(held or {})
        # One report per button group: 6 on the Digitakt, 7 on the Digitone
        # (Device.button_groups). The Digitone's bootstrap waits for all
        # seven, a Digitone Keys' for nine.
        self.groups = groups
        # The UI card type: 4 on the Digitakt, 8 on the Digitone (0xA for a
        # Digitone Keys), from each bootstrap's own check; devices/*.toml
        # [boot] ui_card.
        self.card = bytes((card & 0xFF,)) + self.CARD[1:]
        self.tx = bytearray()
        self.rx = collections.deque()
        self.queries = 0
        self.delivered = 0
        # UIMR (write-only): the receive interrupt is raised only while
        # RXRDY/FFULL (bit 1) is enabled. The updater does not enable it,
        # and raising vector 180 into its default handler anyway starved
        # its RTOS tick (measured on the Digitone).
        self.uimr = 0
        # The interrupt controller's mask for UART8 (INTC1 source 52),
        # masked at reset. The emulator does not model the INTC masks in
        # general, but this one decides whether the panel can interrupt:
        # the bootstrap unmasks it (CIMR = 52) and masks every source
        # (SIMR = 0x40) before it hands over, and the updater never
        # unmasks it, so without it the Digitone's updater took the panel's
        # leftover bytes as an interrupt storm into its default handler and
        # its RTOS tick never ran.
        self.masked = True
        uc = m.uc
        uc.hook_add(UC_HOOK_MEM_WRITE, self._on_tx, begin=self.URB, end=self.URB)
        uc.hook_add(UC_HOOK_MEM_WRITE, self._on_control, begin=self.UCR,
                    end=self.UIMR)
        uc.hook_add(UC_HOOK_MEM_WRITE, self._on_intc, begin=self.INTC1 + 0x08,
                    end=self.INTC1 + 0x1D)
        uc.hook_add(UC_HOOK_MEM_READ, self._on_read, begin=self.USR, end=self.USR)
        uc.hook_add(UC_HOOK_MEM_READ, self._on_read, begin=self.URB, end=self.URB)

    # The UI card's identity, as `70 00` / `71 00` return it: card type (the
    # bootstrap's test report says WRONG CARD unless it is 4), "UI FIRMWARE:
    # 1.%d.%d" from the next two bytes, and a nonzero "tested" flag. The
    # bootstrap checks only the type; the rest it prints. `74 00` returns
    # the serial number the report prints as "SN: %.12s-%.2s".
    CARD = bytes((0x04, 0x02, 0x00, 0x01))
    SERIAL = b'EMULATED0000'

    def _on_tx(self, uc, typ, addr, size, value, data):
        self.tx.append(value & 0xFF)
        last = bytes(self.tx[-2:])
        if last == bytes((0x60, 0x01)):
            self.queries += 1
            for group in range(self.groups):
                self.rx.extend((0x20 | group, self.held.get(group, 0) & 0xFF))
        elif last in (bytes((0x70, 0x00)), bytes((0x71, 0x00))):
            self.queries += 1
            self.rx.append(0x70)
            self.rx.extend(self.card)
        elif last == bytes((0x74, 0x00)):
            self.queries += 1
            self.rx.append(0x70)
            self.rx.extend(self.SERIAL[:9])

    def _on_intc(self, uc, typ, addr, size, value, data):
        off = addr - self.INTC1
        if off == 0x1C and size == 1:                       # SIMR
            if value & 0x40 or value & 0x3F == self.SOURCE:
                self.masked = True
        elif off == 0x1D and size == 1:                     # CIMR
            if value & 0x40 or value & 0x3F == self.SOURCE:
                self.masked = False
        elif off == 0x08 and size == 4:                     # IMRH, 63..32
            self.masked = bool(value >> (self.SOURCE - 32) & 1)

    def _on_control(self, uc, typ, addr, size, value, data):
        if addr == self.UIMR:
            self.uimr = value & 0xFF
        elif addr == self.UCR and (value >> 4) & 7 == 2:    # reset receiver
            self.rx.clear()

    def _on_read(self, uc, typ, addr, size, value, data):
        try:
            if addr == self.USR:
                uc.mem_write(addr, bytes((0x04 | (0x01 if self.rx else 0),)))
            else:
                uc.mem_write(addr, bytes((self.rx.popleft() if self.rx else 0,)))
        except UcError:
            pass

    # -- an event source for longrun.spin ---------------------------------------
    def pending(self):
        return bool(self.rx) and self.uimr & 0x02 and not self.masked

    def step(self, done, remaining=None):
        if not self.pending():
            return remaining
        return min(200, remaining) if remaining is not None else 200

    def service(self, done):
        if not self.pending():
            return False
        from emu.pit import interrupt_level
        level = interrupt_level(self.m, self.VECTOR)
        if level is None:
            return False
        sr = self.m.uc.reg_read(UC_M68K_REG_SR)
        if ((sr >> 8) & 7) >= level:
            return False
        if self.m.raise_vector(self.VECTOR, level=level):
            self.delivered += 1
            return True
        return False


class BootResult:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def as_dict(self):
        return dict(self.__dict__)


def flash_image(syx_path, bootstrap_section=None, size=FLASH_SIZE):
    """-> bytes of the SPI flash as an update would leave it: the container
    at CONTAINER_AT (emu/dspboot.py's layout), erased elsewhere."""
    from dt2.container import container
    flash = bytearray(b'\xff' * size)
    c = container(syx_path)
    if CONTAINER_AT + len(c) > size:
        raise ValueError('the container (%d bytes) does not fit the %d MB '
                         'flash after 0x%x' % (len(c), size >> 20, CONTAINER_AT))
    flash[CONTAINER_AT:CONTAINER_AT + len(c)] = c
    return bytes(flash)


# Where a call into the bootstrap returns to if it ever returns: an address
# nothing maps, so a return shows up as a fault at a known PC.
RETURN_SENTINEL = 0x0BADB007


def container_section(syx_path, section_id):
    """-> the stored bytes of one container section (header included)."""
    from dt2.container import sections
    c, secs = sections(syx_path)
    for sid, off, clen, _dest in secs:
        if sid == section_id:
            return bytes(c[off:off + clen])
    return None


# The updater (section id 4) is stored raw: a longword with its entry, a
# longword of zero, then the image, which the bootstrap loads with the
# header at the section's dest (0x80000400). So the image is at 0x80000408.
UPDATER_ID, UPDATER_IMAGE = 4, 0x80000408


def boot(syx_path, bootstrap_section, main_image, *, ddr=64 * MB,
         limit=400_000_000, chunk=2_000_000, on_machine=None, trace=None,
         mode=0, held=None, ui_card=4, straps=None, groups=6):
    """Run the bootstrap from its reset vector until it jumps into DDR.

    The bootstrap loads the updater (section id 4) over itself in SRAM and
    calls it; the updater is a small RTOS whose tick is PIT0, so the PITs
    are delivered (emu.pit.Pits) from the start.

    -> BootResult: reached (the PC it entered DDR at, or None), stop (why
    the run ended), instructions, the flash reads and writes, whether
    MAIN OS in DDR matches `main_image` at 0x40000400, and the machine
    (m) for a caller that wants to continue from there."""
    from unicorn import UC_HOOK_BLOCK, UC_HOOK_CODE
    from emu import longrun
    from emu.harness import Machine
    from emu.pit import Pits
    image, sp, pc, version = bootstrap_image(bootstrap_section)
    m = Machine(ddr=ddr)
    m.ensure(SRAM)
    # The peripheral pages, which the timer model reads from the start.
    m.ensure(0xFC000000)
    m.ensure(0xEC000000)
    m.uc.mem_write(BOOT_LINK, image)
    skipped = wdebug_sites(image, BOOT_LINK)
    step_over(m, skipped)
    m.install_isa_patches_scoped(image, BOOT_LINK)
    updater = container_section(syx_path, UPDATER_ID)
    upd_image = updater[8:] if updater else b''
    if upd_image:
        m.install_isa_patches_scoped(upd_image, UPDATER_IMAGE)
        upd_entry = struct.unpack_from('>I', updater, 0)[0]
        upd_wdebug = wdebug_sites(upd_image, UPDATER_IMAGE)
        if upd_wdebug:
            # Patched once the bootstrap has loaded it, before it runs.
            def patch_updater(uc, a, s_, d):
                step_over(m, upd_wdebug)
                skipped.extend(upd_wdebug)
            m.uc.hook_add(UC_HOOK_CODE, patch_updater, begin=upd_entry,
                          end=upd_entry)
    flash = SpiFlash(flash_image(syx_path))
    # The flash answers on PCS1: every frame the bootstrap sends selects it.
    dspi = Dspi(m, devices={FLASH_PCS: flash})
    hw = BootHardware(m, straps=straps)
    panel = PanelLink(m, held=held, card=ui_card, groups=groups)
    # The last value the bootstrap wrote to each peripheral register: the
    # controller setup it leaves for the OS (see handoff()).
    mmio_writes = {}

    def record(uc, typ, addr, size, value, data):
        mmio_writes[(addr, size)] = value
    m.uc.hook_add(UC_HOOK_MEM_WRITE, record, begin=0xE0000000, end=0xFFFFFFFF)
    m.install_mmio()
    m.install_exceptions()
    m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
    # The entry is a function of one argument, the boot mode, which its
    # switch at 0x80000f82 dispatches on (0 and 1 the normal path, 0 with
    # the debug module configured first). Call it as its caller would.
    m.uc.mem_write(sp - 8, struct.pack('>II', RETURN_SENTINEL, mode))
    m.uc.reg_write(UC_M68K_REG_A7, sp - 8)
    hit = {'pc': None}

    def on_block(uc, addr, size, data):
        if 0x40000000 <= addr < 0x80000000 and hit['pc'] is None:
            hit['pc'] = addr
            m.halt_vec = -1              # ends longrun.spin's loop too
            uc.emu_stop()
    m.uc.hook_add(UC_HOOK_BLOCK, on_block)
    if on_machine is not None:
        on_machine(m, flash, dspi)
    pits = Pits(m, channels=(3, 2, 1, 0), hold=False)
    done, stop = 0, 'limit'
    cur = pc
    while done < limit and hit['pc'] is None:
        cur, executed, why = longrun.spin(m, cur, chunk, pits=pits,
                                          async_events=(panel,))
        done += executed
        if why != 'limit':
            stop = why
            break
        if trace is not None:
            trace(m, done, cur)
    if hit['pc'] is not None:
        stop = 'entered DDR'
    matches = None
    flags = handoff_sp = None
    if hit['pc'] is not None:
        # The updater calls the OS entry as a function of one argument, the
        # boot flags (MAIN OS stores 4(a7) at its entry, 0x400004ec).
        handoff_sp = m.uc.reg_read(UC_M68K_REG_A7)
        flags = struct.unpack('>I', m.peek(handoff_sp + 4, 4))[0]
        if main_image is not None:
            got = m.peek(0x40000400, len(main_image))
            matches = got == main_image
    return BootResult(reached=hit['pc'], stop=stop, instructions=done,
                      boot_flags=flags, handoff_sp=handoff_sp,
                      version=version, entry=pc, stack=sp,
                      flash_reads=flash.read_summary(),
                      flash_writes=list(flash.writes),
                      flash_commands=dict(flash.commands),
                      dspi_frames=dspi.frames, pit_ticks=dict(pits.fired),
                      main_os_matches=matches, stepped_over=skipped,
                      panel_queries=panel.queries, mmio_writes=mmio_writes,
                      m=m, flash=flash, dspi=dspi, hw=hw, panel=panel)


def handoff(result):
    """-> what the bootstrap leaves the OS, as data: the CPU registers at the
    OS entry (its stack holds the boot flags), the control registers, the
    SRAM, the DDR it wrote, and the last value it wrote to each peripheral
    register.

    Not a snapshot of the whole machine: a snapshot carries every register
    of every peripheral page, and restoring one overwrites the reset values
    the emulator's own models seed for the ones the bootstrap never touched
    (measured: the eSDHC's present-state register read back zero and the OS
    waited on its DAT0 line for ever). The cold boot builds its machine as
    usual and then applies this (emu/dspboot.py run(handoff=...))."""
    from unicorn.m68k_const import UC_M68K_REG_D0, UC_M68K_REG_A0
    if result.reached is None:
        raise ValueError('the bootstrap never reached the OS')
    m = result.m
    regs = {'d%d' % i: m.uc.reg_read(UC_M68K_REG_D0 + i) for i in range(8)}
    regs.update({'a%d' % i: m.uc.reg_read(UC_M68K_REG_A0 + i) for i in range(8)})
    regs['sr'] = m.uc.reg_read(UC_M68K_REG_SR)
    ddr = {}
    for base in sorted(m.mapped):
        if 0x40000000 <= base < 0x80000000:
            ddr[base] = m.peek(base, 0x100000)
    return {'pc': result.reached, 'regs': regs, 'ctlregs': dict(m.ctlregs),
            'sram': m.peek(SRAM, SRAM_SIZE), 'ddr': ddr,
            'mmio': sorted((a, s_, v) for (a, s_), v in result.mmio_writes.items()),
            'boot_flags': result.boot_flags}


def save_handoff(result, path):
    """Write handoff(result) to `path` (zlib-compressed pickle). It holds
    firmware bytes: keep it with the firmware folder, never in the repo."""
    import pickle
    import zlib
    with open(path, 'wb') as fh:
        fh.write(zlib.compress(pickle.dumps(handoff(result), protocol=4)))
    return path


def load_handoff(path):
    import pickle
    import zlib
    with open(path, 'rb') as fh:
        data = pickle.loads(zlib.decompress(fh.read()))
    if not isinstance(data, dict) or 'pc' not in data or 'regs' not in data:
        raise ValueError('%s is not a bootstrap handoff' % path)
    return data


def apply_handoff(m, data):
    """Put a machine where the bootstrap left the device. -> the entry PC.

    DDR and SRAM first, then the peripheral registers the bootstrap wrote
    (as memory: a model's read hook still decides what a read returns), then
    the control and CPU registers."""
    from unicorn.m68k_const import UC_M68K_REG_D0, UC_M68K_REG_A0
    for base, page in data['ddr'].items():
        m.ensure(base)
        m.uc.mem_write(base, page)
    m.ensure(SRAM)
    m.uc.mem_write(SRAM, data['sram'])
    for addr, size, value in data['mmio']:
        m.ensure(addr)
        m.uc.mem_write(addr, (value & ((1 << (8 * size)) - 1)).to_bytes(size, 'big'))
    m.ctlregs.update(data['ctlregs'])
    regs = data['regs']
    for i in range(8):
        m.uc.reg_write(UC_M68K_REG_D0 + i, regs['d%d' % i])
        m.uc.reg_write(UC_M68K_REG_A0 + i, regs['a%d' % i])
    # SR before A7: the stack pointer banks with the mode (harness.call).
    m.uc.reg_write(UC_M68K_REG_SR, regs['sr'])
    m.uc.reg_write(UC_M68K_REG_A7, regs['a7'])
    return data['pc']
