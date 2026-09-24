# pyright: reportMissingImports=false
"""The eSDHC controller and the eMMC behind it.

`0xFC0CC000`, 16 KB, PBC0 slot 51 (RM chapter 25). Reached only when
`build(sdgate=True)` satisfies the board loopback of `emu/gpio.py` -- without
that the driver is never entered at all. See HANDOVER section 6b.

What the firmware does with it, from the driver map:

  * `0x4011fe10` writes CMDARG then XFERTYP -- **writing XFERTYP issues the
    command** -- and then blocks on semaphore `0x44e26f38`, returning the
    status word `0x44e26f1c` that the ISR is supposed to have written.
  * `0x401208fe` reads blocks with CMD18, `0x40120ae4` writes with CMD25.
  * `XFERTYP[DMAEN]` is never set and DSADDR/ADMASAR are never referenced: the
    controller's own DMA is unused, and bulk data moves through the SoC's eDMA
    with `SADDR = DATPORT`.
  * Init ends with the eMMC **bus test**, and it is a real handshake, not a
    magic number. `0x40120242` writes `0x5A` to DATPORT under CMD19
    (BUSTEST_W), then CMD14 (BUSTEST_R) must read back a word whose low byte
    XORed with `0xA5` is zero (`0x401202c6`). `~0x5A == 0xA5`: the card returns
    the inverse of whatever the host sent, so the model inverts the captured
    pattern rather than hardcoding the constant.

Modelling notes that cost time:

**Reads are served from the backing store, not a read hook.** Every register
this model owns is kept up to date in guest memory as state changes, so an
ordinary read just works and no hook runs on the hot path. Only two addresses
carry write hooks -- SYSCTL, whose self-clearing bits have to be cleared, and
XFERTYP, which is the command trigger.

**`uc.mem_write` from Python does not fire write hooks** (HANDOVER trap 13),
so the model updating its own registers cannot recurse into itself.

**A write hook fires BEFORE the store lands.** Reading the register back from
inside one gives the OLD value, and anything written from inside it is then
overwritten by the store that is still to come. So a write hook must use its
own `value` argument, and a register whose value has to be *corrected* is
handled on READ instead -- which is what SYSCTL's self-clearing bits do here.
Getting this wrong looks exactly like the model not being installed: the INITA
spin stays at 36,988,467 reads and nothing else changes.

**Self-clearing bits are why the driver used to hang.** `SYSCTL` bit 27 INITA
sends 80 init clocks and clears itself; bits 24-26 RSTA/RSTC/RSTD are software
resets that do the same. Backed by plain RAM they read back whatever was
written, and `0x4012001e` spins forever -- measured at 36,988,467 reads.
"""
import mmap
import os
import struct

from emu import sparse
from emu.edma import (
    SERQ,
    TCD_BASE,
    SADDR,
    SOFF,
    NBYTES,
    SLAST,
    DADDR,
    CITER,
    DOFF,
    BITER,
    CSR,
)

BASE = 0xFC0CC000
SIZE = 0x1000

DSADDR, BLKATTR, CMDARG, XFERTYP = 0x00, 0x04, 0x08, 0x0C
CMDRSP0, CMDRSP1, CMDRSP2, CMDRSP3 = 0x10, 0x14, 0x18, 0x1C
DATPORT, PRSSTAT, PROCTL, SYSCTL = 0x20, 0x24, 0x28, 0x2C
IRQSTAT, IRQSTATEN, IRQSIGEN = 0x30, 0x34, 0x38
AUTOC12ERR, HOSTCAPBLT, WML = 0x3C, 0x40, 0x44
FEVT, ADMAESR, ADMASAR, VENDOR, HOSTVER = 0x50, 0x54, 0x58, 0xC0, 0xFC

NAME = {DSADDR: 'DSADDR', BLKATTR: 'BLKATTR', CMDARG: 'CMDARG',
        XFERTYP: 'XFERTYP', CMDRSP0: 'CMDRSP0', CMDRSP1: 'CMDRSP1',
        CMDRSP2: 'CMDRSP2', CMDRSP3: 'CMDRSP3', DATPORT: 'DATPORT',
        PRSSTAT: 'PRSSTAT', PROCTL: 'PROCTL', SYSCTL: 'SYSCTL',
        IRQSTAT: 'IRQSTAT', IRQSTATEN: 'IRQSTATEN', IRQSIGEN: 'IRQSIGEN',
        AUTOC12ERR: 'AUTOC12ERR', HOSTCAPBLT: 'HOSTCAPBLT', WML: 'WML',
        FEVT: 'FEVT', ADMAESR: 'ADMAESR', ADMASAR: 'ADMASAR',
        VENDOR: 'VENDOR', HOSTVER: 'HOSTVER'}

# XFERTYP bits
DMAEN, BCEN, AC12EN = 1 << 0, 1 << 1, 1 << 2
DTDSEL, MSBSEL = 1 << 4, 1 << 5
DPSEL = 1 << 21

# PRSSTAT bits
CIHB, CDIHB, DLA, SDSTB = 1 << 0, 1 << 1, 1 << 2, 1 << 3
BWEN, BREN = 1 << 10, 1 << 11
CINS = 1 << 16
CLSL = 1 << 23
DLSL0 = 1 << 24

# IRQSTAT bits
CC, TC, BGE, DINT = 1 << 0, 1 << 1, 1 << 2, 1 << 3
BWR, BRR = 1 << 4, 1 << 5
CINS_IRQ = 1 << 6

# SYSCTL self-clearing bits
RSTA, RSTC, RSTD, INITA = 1 << 24, 1 << 25, 1 << 26, 1 << 27

# Reset values, RM Table 25-2. PRSSTAT's documented reset is 0xFF8800F8; we add
# CINS because a card IS inserted, which is the whole point of the model.
RESET = {
    DSADDR: 0, BLKATTR: 0x00010000, CMDARG: 0, XFERTYP: 0,
    CMDRSP0: 0, CMDRSP1: 0, CMDRSP2: 0, CMDRSP3: 0, DATPORT: 0,
    PRSSTAT: 0xFF8800F8 | CINS,
    PROCTL: 0x00000020, SYSCTL: 0x00008008,
    IRQSTAT: 0, IRQSTATEN: 0x117F013F, IRQSIGEN: 0,
    AUTOC12ERR: 0, HOSTCAPBLT: 0x07F30000, WML: 0x08100810,
    ADMAESR: 0, ADMASAR: 0, VENDOR: 1, HOSTVER: 0x00001201,
}

# The driver's own status word, written by the ISR on real hardware. This is
# the DIGITAKT value, kept as the default; per-image callers should instead
# pass drv_status resolved from emu/symbols.py's sd_status (Digitone's is
# 0x44459054). Using Digitakt's literal on Digitone leaves the guest's status
# word never cleared, so every bge/blt check after CMD0/CMD1/CMD19/CMD14/CMD8
# reads a stale "in progress" value.
# 0x4011fe10 sets it to 1 before issuing and returns it after the wait.
DRV_STATUS = 0x44E26F1C

# EXT_CSD is 512 bytes, DMA'd to this buffer by eDMA channel 59; the driver
# programs DADDR at 0x40120302 and arms the channel at 0x4012037e.
EXTCSD_BUF = 0x4FE49100
DMA_CHAN = 59

# Offsets INTO EXT_CSD that this firmware reads. `SLC_OK` is the one that
# matters: 0x4fe49198 is EXTCSD_BUF + 0x98, so the "MMC NOT IN SLC MODE" flag
# of section 1 is simply EXT_CSD byte 152, and build(slc=True) has been poking
# into this buffer all along.
#
# Byte 152 is inside GP_SIZE_MULT in the JEDEC map, which is not an obvious
# home for an SLC flag, so treat the JEDEC identity as UNCONFIRMED and the
# offset as the fact. What is certain is 0x401204a4 reads it and wants 1.
SLC_OK = 0x98
# 156..158, which FUN_400e26a2 packs into ONE 24-bit big-endian value. It is
# part of the card's identity, not a capability: FUN_400e26f2 compares it,
# byte 222 and byte 227 against the row the part table holds for this card and
# returns -3 on any mismatch. See PART below for why that matters.
PART_ID_MULT = 0x9C                  # 156..158, read as one 24-bit value
PART_ID_B = 0xDE                     # 222
PART_ID_C = 0xE3                     # 227
# SEC_COUNT is JEDEC byte 212..215 and the spec calls it little-endian, but
# what decides the model is how THIS firmware reads it. sd_bringup does, at
# 0x400e24d8:
#
#     move.l (0x4ba9f4b4).l,D0     ; a BIG-endian longword
#     beq.b  <fall back to the CSD-derived size>
#     move.l D0,(0x421cca34).l     ; -> the capacity the write path checks
#
# and 0x4ba9f4b4 is this buffer's base plus 0xD4 -- the base being 0x4ba9f3e0,
# which is exactly `slc_status_addr` (0x4ba9f478) minus SLC_OK's 0x98, from a
# different anchor entirely. So the firmware takes these four bytes big-endian.
#
# Packed little-endian, 0x00760000 reaches it as 0x00007600: the card claims
# 30,208 sectors, 14.75 MB. Nothing complains, because the failure is silent
# -- the write primitive at 0x400e2bd2 range-checks `sector >= capacity` and
# returns -10 BEFORE issuing any command, so every +Drive write above ~15 MB
# was dropped with no card traffic at all, while erase (a different path, no
# range check) went through and made a format look like it worked.
#
# Whether real hardware gets here byte-swapped by the controller or the
# firmware simply reads it big-endian cannot be told apart without a scope on
# the real thing. Either way the model's job is the same: make SEC_COUNT read
# back as the card's true size. If a word-swapping data path is ever modelled,
# this has to move with it.
SEC_COUNT = 0xD4                     # 212..215, read big-endian by this OS
ERASE_GROUP_DEF = 0xAF               # 175
BUS_WIDTH = 0xB7                     # 183
HS_TIMING = 0xB9                     # 185


# The firmware will only mount the +Drive on a card it recognises. FUN_400e253e
# walks a table of seven parts at 0x4020c2c4, matching the CID's manufacturer
# id against word 0 and its 6-character product name against word 1; no match
# is -1, a manufacturer match with the wrong name is -2. FUN_400e26f2 then
# checks three EXT_CSD identity bytes against the same row (-3) and the card's
# sector count against it (-4). FUN_40068eb6 stores whatever comes back in
# _DAT_41980918 and only mounts when it is 0:
#
#     if (_DAT_41980918 == 0 && FUN_400e2592() == 1) { ...; FUN_400d0f9c(0); }
#
# A nameless CID reported manufacturer 0x11 and an empty name, so this was -2:
# the volume stayed unmounted, the inode bitmap at 0x426876b0 stayed all zeros,
# and every node the UI asked about read back invalid. Opening SETTINGS then
# fed the resulting NULL name to std::string, which threw
# "basic_string::_S_construct null not valid", and with the unwinder unable to
# start, FUN_400ee3f2 -- an empty `while (true)` -- parked the UI task for good.
# That was the frozen screen.
#
# So present one of the parts the table lists. These are the table's own
# values, read back out of the image, not invented ones.
PART = dict(
    manufacturer=0x11,               # the table's first four rows
    name=b'004GE0',                  # one of that manufacturer's four names
    id_mult=0x0001D8,                # EXT_CSD 156..158, as one 24-bit value
    id_b=0x01,                       # EXT_CSD 222
    id_c=0x08,                       # EXT_CSD 227
    sectors_mlc=0x00760000,          # table word 6, expected when not in SLC
    sectors_slc=0x003B0000,          # table word 7, expected in SLC mode
)
# Product revision, serial and manufacturing date are in no comparison and no
# read this firmware makes; they are left at zero rather than given invented
# values, the same way the unread EXT_CSD bytes are.
PRODUCT_REV = 0x00
SERIAL = 0x00000000
MANUFACTURE_DATE = 0x00


def cid_words(manufacturer, name, rev=0, serial=0, date=0, oid=0):
    """-> the four CID longwords in the order the controller returns R2.

    {RSP3[23:0], RSP2, RSP1, RSP0} over CID[127:8]: the manufacturer id and OEM
    id in RSP3, the six name characters spanning RSP2 and the top of RSP1, then
    revision, serial and date.
    """
    n = name.ljust(6, b'\0')[:6]
    return [
        (serial & 0xFFFFFF) << 8 | (date & 0xFF),                       # RSP0
        n[4] << 24 | n[5] << 16 | (rev & 0xFF) << 8 | (serial >> 24),   # RSP1
        n[0] << 24 | n[1] << 16 | n[2] << 8 | n[3],                     # RSP2
        (manufacturer & 0xFF) << 16 | (oid & 0xFFFF),                   # RSP3
    ]


def ext_csd(sectors=PART['sectors_slc'], slc=True):
    """-> 512 bytes of EXT_CSD.

    Deliberately sparse: only the fields this firmware has been observed to
    read are set, so anything else showing up as a dependency will announce
    itself as a spin or a rejected card rather than hiding behind a plausible
    value.

    "Observed" is now exhaustive rather than incidental. Every reference into
    the 512-byte buffer the controller DMAs this into (0x4ba9f3e0..0x4ba9f5e0)
    was enumerated in Ghidra; the firmware reads exactly ten offsets:

      +0x098  byte  slc_status_predicate (0x400e2592) -- MODELLED, wants 1
      +0x09c  byte  \
      +0x09d  byte   > packed into one 24-bit value by FUN_400e26a2
      +0x09e  byte  /
      +0x0d4  long  sd_bringup's capacity; also read byte-wise by FUN_400e27b2
      +0x0d5  byte  \
      +0x0d6  byte   > the same four bytes, for display
      +0x0d7  byte  /
      +0x0de  byte  FUN_400e26a2
      +0x0e3  byte  FUN_400e26a2
      +0x108  byte  \
      +0x10e  byte   > three bytes copied out by FUN_400e28e6
      +0x10f  byte  /

    +0x098, +0x0d4 and the three PART_ID bytes reach something that decides
    behaviour -- between them they are what the +Drive mount is gated on, see
    PART above. The rest are pure getters filling an info struct for the
    console's MMC commands and the SYSTEM > STORAGE page, so leaving them zero
    shows zeros there and changes nothing else. They are deliberately NOT given
    invented values: a plausible figure for a card nobody has read would be
    fabricated hardware data, and the zeros at least say "unknown" honestly.

    Note ERASE_GROUP_DEF, BUS_WIDTH and HS_TIMING below appear in no read at
    all. They are SWITCH-command fields the host writes rather than reads;
    setting them is harmless and kept for documentation.
    """
    b = bytearray(512)
    b[SLC_OK] = 1 if slc else 0
    b[SEC_COUNT:SEC_COUNT + 4] = struct.pack('>I', sectors)
    b[ERASE_GROUP_DEF] = 1
    b[BUS_WIDTH] = 1
    b[HS_TIMING] = 1
    b[PART_ID_MULT:PART_ID_MULT + 3] = struct.pack('>I', PART['id_mult'])[1:]
    b[PART_ID_B] = PART['id_b']
    b[PART_ID_C] = PART['id_c']
    return bytes(b)


# Card.flush zeroes erased ranges inside the file this many bytes at a time.
_ZERO_CHUNK = 1 << 20


class Card:
    """A minimal eMMC. Only what the identification sequence asks for.

    `capacity_blocks` is in 512-byte sectors. CMD3 assigns RCA 2 (a
    host-assigned RCA is illegal for SD and standard for eMMC, which is how we
    know this is an eMMC and not a card).

    The default is the SLC figure from PART, because SLC_OK is 1 by default and
    FUN_400e26f2 then wants the sector count to be the table's SLC number --
    the full-capacity number with the SLC flag set is -4, and an unmounted
    +Drive. The two have to agree.
    """

    def __init__(self, image=None, capacity_blocks=PART['sectors_slc'],
                 slc=True, path=None):
        self.image = image
        # An on-disk image: memory-mapped, so reads cost nothing and the file
        # stays sparse. Writes still land in `overlay` and reach the file at
        # flush(). Created if missing, one sector long, and marked sparse on
        # Windows so later growth and erases do not allocate (emu/sparse.py).
        self.path = path
        self._fh = None
        if path:
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            self._fh = open(path, 'a+b' if new else 'r+b')
            if new:
                sparse.make_sparse(self._fh)
                sparse.extend(self._fh, 512)
            self._fh.seek(0)
            self.image = mmap.mmap(self._fh.fileno(), 0)
        self.blocks = capacity_blocks
        self.ext_csd = ext_csd(capacity_blocks, slc)
        self.rca = 0
        self.selected = False
        # OCR: bit31 power-up done, bit30 sector addressing, voltage window.
        self.ocr = 0xC0FF8080
        self.overlay = {}
        # Erased byte ranges, [lo, hi), merged and kept sorted. NOT expanded
        # into `overlay`: FORMAT +DRIVE erases about 1.5 GB, which as one
        # dict entry per byte would be gigabytes of host memory for data that
        # is all the same value. A range list costs nothing and reads apply it
        # before the overlay, so a write after an erase still wins.
        self.erased = []
        self._erase_lo = None            # CMD35 start, pending its CMD38
        self._erase_hi = None            # CMD36 end
        # CID/CSD as four longwords each, R2 order {RSP3[23:0],RSP2,RSP1,RSP0}.
        # The name is not decoration: it is half of what the firmware's part
        # table matches on, and without it the +Drive never mounts. See PART.
        self.cid = cid_words(PART['manufacturer'], PART['name'],
                             rev=PRODUCT_REV, serial=SERIAL,
                             date=MANUFACTURE_DATE)
        self.csd = [0x00000000, 0x00000000, 0x00000000, 0x00000000]

    def command(self, idx, arg):
        """-> (resp0, resp1, resp2, resp3). R1 is a card-status word."""
        r1 = 0x00000900          # state=transfer(4), READY_FOR_DATA
        if idx == 0:             # GO_IDLE_STATE, no response
            self.selected = False
            return (0, 0, 0, 0)
        if idx == 1:             # SEND_OP_COND -> OCR, bit31 must end set
            return (self.ocr, 0, 0, 0)
        if idx in (2, 9, 10):    # ALL_SEND_CID / SEND_CSD / SEND_CID -> R2
            src = self.csd if idx == 9 else self.cid
            return tuple(src)
        if idx == 3:             # SET_RELATIVE_ADDR (eMMC: host assigns)
            self.rca = (arg >> 16) & 0xFFFF
            return (r1, 0, 0, 0)
        if idx == 7:             # SELECT/DESELECT_CARD
            self.selected = ((arg >> 16) & 0xFFFF) == self.rca
            return (r1, 0, 0, 0)
        # ERASE_GROUP_START / ERASE_GROUP_END / ERASE. Both addresses are
        # sector numbers and the range is INCLUSIVE of the end sector, which
        # is why the end converts with (arg + 1) * 512. The firmware's
        # FORMAT +DRIVE issues these as 515 (35, 36, 38) triples; without
        # them modelled the command still got a valid response and the format
        # appeared to work while leaving every erased byte in place.
        if idx == 35:            # ERASE_GROUP_START
            self._erase_lo = arg * 512
            return (r1, 0, 0, 0)
        if idx == 36:            # ERASE_GROUP_END
            self._erase_hi = (arg + 1) * 512
            return (r1, 0, 0, 0)
        if idx == 38:            # ERASE
            if self._erase_lo is not None and self._erase_hi is not None:
                self.erase(self._erase_lo, self._erase_hi)
            self._erase_lo = self._erase_hi = None
            return (r1, 0, 0, 0)
        return (r1, 0, 0, 0)

    def erase(self, lo, hi):
        """Mark [lo, hi) erased: drop any overlay there and record the range.

        Erased eMMC reads back as a constant. EXT_CSD's ERASED_MEM_CONT is
        left 0 here (see ext_csd()), so that constant is 0x00, which is also
        what an untouched sparse image already reads -- the two agree, and a
        restored snapshot cannot disagree with its own backing file.
        """
        if hi <= lo:
            return
        for key in [k for k in self.overlay if lo <= k < hi]:
            del self.overlay[key]
        merged = []
        for a, b in sorted(self.erased + [(lo, hi)]):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self.erased = [(a, b) for a, b in merged]

    def read_word(self, idx, pattern):
        """-> the next word the host will read out of DATPORT.

        CMD14 is BUSTEST_R: the eMMC returns the bitwise inverse of the
        pattern the host sent under CMD19, which is why the driver's check is
        `read ^ 0xA5 == 0` after writing 0x5A.
        """
        if idx == 14:
            return (~pattern) & 0xFFFFFFFF
        return 0

    def data_for(self, idx, arg=0, length=None):
        """-> the block of data a data-read command hands back, or None."""
        if idx == 8:                 # SEND_EXT_CSD
            return self.ext_csd
        if idx == 18:                # READ_MULTIPLE_BLOCK, sector addressed
            start = arg * 512
            if length is None:
                length = max(0, len(self.image) - start) if self.image else 0
            if self.image is None:
                data = bytearray(length)
            else:
                data = bytearray(self.image[start:start + length])
                data.extend(b'\0' * (length - len(data)))
            # Erases first, then the overlay: a write after an erase wins.
            for lo, hi in self.erased:
                a, b = max(lo, start), min(hi, start + length)
                if a < b:
                    data[a - start:b - start] = bytes(b - a)
            for offset in range(length):
                value = self.overlay.get(start + offset)
                if value is not None:
                    data[offset] = value
            return bytes(data)
        return None

    def write_data(self, idx, arg, payload):
        """Accept block data sent by the host, retaining a sparse overlay."""
        if idx != 25:                 # WRITE_MULTIPLE_BLOCK
            return
        start = arg * 512
        self.overlay.update((start + offset, value)
                            for offset, value in enumerate(payload))

    def flush(self):
        """Fold every overlaid byte into the image file. No-op without a path.

        The overlay is kept: reads prefer it, so this card still reads the
        same bytes afterwards. Snapshots of a file-backed card do NOT carry it
        any more -- see Esdhc.checkpoint_state, which empties it once it is on
        disk.
        """
        if not self.path or not (self.overlay or self.erased):
            return
        if self.image is None:
            raise ValueError('%s: card closed with unwritten data' % self.path)
        # Writes through a mapped view do not move the file's modification
        # time on Windows -- measured on NTFS: not at the write, not at
        # mmap.flush, not when the view or the handle is closed. Whoever
        # decides "has the card changed since this snapshot?" from size and
        # mtime (emu/bootstrap.py's card_stamp) would then never see a
        # session's writes, so stamp the file explicitly -- BEFORE the first
        # byte as well as after the last. A flush killed half way (Cancel
        # terminates the worker; a logoff ends the panel mid-save) then
        # leaves a card whose stamp no longer matches any snapshot, instead
        # of a half-written card that still looks like the old one.
        os.utime(self.path)
        # Only the part of an erase that the file actually covers has to be
        # written: beyond end-of-file the image is sparse and already reads
        # as zero, so a 967 MB erase does not have to allocate 967 MB. On a
        # sparse file (Windows NTFS, emu/sparse.py) the part inside is
        # deallocated rather than written; otherwise it is zeroed a megabyte
        # at a time, not with one bytes(hi - lo): a first boot erases 512 MB
        # inside the file, and one slice assignment allocated all 512 MB of
        # zeros at once.
        zeros = None
        holes = sparse.is_sparse(self._fh)
        if holes and self.erased:
            self.image.flush()           # nothing dirty in a range it drops
        for lo, hi in self.erased:
            hi = min(hi, len(self.image))
            if lo >= hi or (holes and sparse.zero_range(self._fh, lo, hi)):
                continue
            for a in range(lo, hi, _ZERO_CHUNK):
                b = min(hi, a + _ZERO_CHUNK)
                if zeros is None:
                    zeros = bytes(_ZERO_CHUNK)
                self.image[a:b] = zeros[:b - a]
        if self.overlay:
            need = max(self.overlay) + 1
            if need > len(self.image):
                # grow the file (sparse: no zeros written) and remap
                self.image.close()
                sparse.extend(self._fh, need)
                self.image = mmap.mmap(self._fh.fileno(), 0)
            for offset, value in self.overlay.items():
                self.image[offset] = value
        self.image.flush()
        os.utime(self.path)

    def close(self):
        """Release the memory map and then the file handle. Safe to repeat.

        Does NOT flush: call flush() first if the overlay should reach the
        file. Needed because the map pins the file -- on Windows a mapped
        file cannot be renamed, replaced or deleted -- and nothing else
        releases it until the whole Machine is garbage-collected. After this
        the card reads as blank; it is meant for the end of a run.
        """
        if self._fh is None:
            return
        if self.image is not None:
            self.image.close()
            self.image = None
        self._fh.close()
        self._fh = None


class Esdhc:
    """The controller. `log` collects (command index, argument) in order."""

    def __init__(self, m, card=None, trace=False, drv_status=None,
                 capacity_addr=None,
                 cmd_sem=None, data_sem=None, dma_sem=None):
        from unicorn import UC_HOOK_MEM_WRITE
        self.m = m
        self.uc = m.uc
        # Default to the configured +Drive image, not a blank in-memory card.
        # This is the cold-boot path, and the cold boot is where the firmware
        # identifies the card and decides whether to mount the volume -- it
        # never re-runs either on a resume, so whatever the cold boot saw is
        # baked into every snapshot taken from it. Booting against a blank
        # card and only attaching the real image afterwards means the mount
        # was attempted against an empty drive and the real one is never
        # looked at. The resume path in emu/longrun.py already passes this
        # image explicitly; this makes the two agree.
        from emu import config as _config
        self.card = card or Card(path=_config.plusdrive_image())
        self.trace = trace
        self.drv_status = DRV_STATUS if drv_status is None else drv_status
        self.cmd_sem = cmd_sem
        self.data_sem = data_sem
        self.dma_sem = dma_sem
        # Where sd_bringup stashed the card size; see `sd_capacity` in
        # emu/symbols.py. Kept so a restore can correct a stale one.
        self.capacity_addr = capacity_addr
        self.log = []
        self.pattern = 0           # last word the host wrote to DATPORT
        self.armed = None          # eDMA channel armed via SERQ for this cmd
        self.dma_bytes = 0
        # Machine.ensure(addr) maps the 1MB page containing addr. Guest
        # accesses to an unmapped page go through Machine._fault, which maps
        # the page and resumes -- but host-side uc.mem_write from Python does
        # not fire _fault, so it raises UC_ERR_WRITE_UNMAPPED instead. On the
        # longrun.py resume path this never showed up, because restore_into
        # pre-maps every page the earlier boot touched. On the cold-boot path
        # (dspboot.py) nothing has touched this page yet, so without this the
        # _put loop below died immediately seeding its own reset values. This
        # is what makes the model usable cold, not just on a snapshot resume.
        m.ensure(BASE)
        for off, val in RESET.items():
            self._put(off, val)
        from unicorn import UC_HOOK_MEM_READ
        # SYSCTL is corrected on READ: see the note above about write hooks
        # running before the store.
        self.uc.hook_add(UC_HOOK_MEM_READ, self._on_sysctl_read,
                         begin=BASE + SYSCTL, end=BASE + SYSCTL + 3)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_xfertyp,
                         begin=BASE + XFERTYP, end=BASE + XFERTYP + 3)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_datport_write,
                         begin=BASE + DATPORT, end=BASE + DATPORT + 3)
        # The driver arms an eDMA channel BEFORE issuing the command, so the
        # transfer has to run when the command is issued, not when SERQ is
        # written. emu/edma.py hooks the same byte for channel 35; Unicorn is
        # happy with more than one hook on an address.
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_serq,
                         begin=SERQ, end=SERQ)

    def checkpoint_state(self):
        """Preserve card state that is not represented in guest memory.

        For a FILE-BACKED card the file is the truth once flushed, so the
        snapshot stores an empty overlay and no erased ranges, and the card
        drops both from memory too. Carrying them made a snapshot replay its
        own card writes over the file on every restore: a gui.snap built by a
        first boot held ~3.9M written bytes and a 512 MB erase, which hid or
        zeroed whatever a later session had written there, and cost hundreds
        of MB of host memory as a per-byte dict. An in-memory card (no path)
        has nowhere else to keep its bytes, so it still carries them.
        """
        self.card.flush()
        if self.card.path:
            self.card.overlay = {}
            self.card.erased = []
        return {
            'type': 'Esdhc',
            'version': 1,
            'pattern': self.pattern,
            'armed': self.armed,
            'dma_bytes': self.dma_bytes,
            'card_blocks': self.card.blocks,
            'card_rca': self.card.rca,
            'card_selected': self.card.selected,
            'card_overlay': dict(self.card.overlay),
            # Ranges, not expanded bytes; see Card.erased.
            'card_erased': [list(r) for r in self.card.erased],
        }

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'Esdhc' or state.get('version') != 1:
            raise RuntimeError('unsupported Esdhc checkpoint state')
        if state.get('card_blocks') != self.card.blocks:
            raise RuntimeError('Esdhc card capacity mismatch')
        overlay = state.get('card_overlay')
        if not isinstance(overlay, dict) or any(
            type(offset) is not int
            or offset < 0
            or type(value) is not int
            or not 0 <= value <= 0xFF
            for offset, value in overlay.items()
        ):
            raise RuntimeError('invalid Esdhc card overlay')
        # Absent in states written before erase was modelled; an older
        # snapshot simply has nothing erased, which is what it recorded.
        erased = state.get('card_erased', [])
        if not isinstance(erased, list) or any(
            not isinstance(r, (list, tuple))
            or len(r) != 2
            or type(r[0]) is not int
            or type(r[1]) is not int
            or not 0 <= r[0] <= r[1]
            for r in erased
        ):
            raise RuntimeError('invalid Esdhc card erased ranges')
        armed = state.get('armed')
        if armed is not None and (type(armed) is not int or not 0 <= armed < 64):
            raise RuntimeError('invalid Esdhc armed channel')
        for key in ('pattern', 'dma_bytes', 'card_rca'):
            if type(state.get(key)) is not int or state[key] < 0:
                raise RuntimeError('invalid Esdhc checkpoint field %s' % key)
        if type(state.get('card_selected')) is not bool:
            raise RuntimeError('invalid Esdhc selected state')
        self.pattern = state['pattern']
        # Early v1 checkpoints recorded every SERQ writer, including UART8
        # channel 35. Only channel 59 can belong to this controller.
        self.armed = armed if armed == DMA_CHAN else None
        self.dma_bytes = state['dma_bytes']
        self.card.rca = state['card_rca']
        self.card.selected = state['card_selected']
        self.card.overlay = dict(overlay)
        self.card.erased = [(int(a), int(b)) for a, b in erased]
        self._reassert_capacity()

    def _reassert_capacity(self):
        """Put the card's real size back into the driver's own word.

        The size is a property of the modelled card, not of guest progress,
        and sd_bringup only computes it once at boot -- a resumed snapshot
        brings back whatever the EXT_CSD of the day produced. Snapshots taken
        before SEC_COUNT's byte order was corrected carry 30,208 sectors, and
        with that in place every +Drive write above ~15 MB is refused by
        0x400e2bd2 before any command is issued, so a format erases and then
        writes nothing. Correcting it here keeps those snapshots usable.
        """
        if not self.capacity_addr:
            return
        try:
            self.m.ensure(self.capacity_addr)
            was = struct.unpack(
                '>I', bytes(self.uc.mem_read(self.capacity_addr, 4)))[0]
        except Exception:
            return
        if was == self.card.blocks:
            return
        self.uc.mem_write(self.capacity_addr,
                          struct.pack('>I', self.card.blocks))
        print('[esdhc] driver capacity %d -> %d sectors (stale snapshot)'
              % (was, self.card.blocks))

    def _post(self, sem):
        """Post an RTOS semaphore, as the eSDHC ISR does on real hardware.

        On real hardware the eSDHC ISR does two things on command/transfer
        completion: writes the driver's status word (modelled above via
        drv_status) and posts the driver's completion semaphore. This is the
        second half, previously unmodelled -- which is why the command
        primitive (FUN_4011d5b4 on Digitone) issued its command and then
        blocked forever in sem_pend on the cold-boot path. Mirrors exactly
        what emu/longrun.py's `unblock` does for pends generically -- it
        writes 1 when the count is <= 0 -- but scoped to the two semaphores
        this controller owns, so the model is correct on the cold-boot path
        too and does not depend on a global bypass. When `unblock` is also
        active this is simply a no-op, since it only acts on counts <= 0.
        """
        if sem is None:
            return
        try:
            count = struct.unpack('>i', self._read(sem, 4))[0]
            if count <= 0:
                self._write(sem, struct.pack('>i', 1))
        except Exception:
            pass

    # -- guest memory, from inside a hook ----------------------------------
    # Every caller below runs inside the XFERTYP write hook, where mapping a
    # page is unsafe (see Machine.poke). The driver's status word, its
    # semaphores and a DMA buffer can all sit in SDRAM the guest has not
    # touched yet -- on the Digitone the EXT_CSD buffer does, and mapping it
    # here crashed the host.
    def _write(self, addr, data):
        poke = getattr(self.m, 'poke', None)
        if poke is not None:
            poke(addr, data)
            return
        self.m.ensure(addr)
        self.uc.mem_write(addr, data)

    def _read(self, addr, n):
        peek = getattr(self.m, 'peek', None)
        if peek is not None:
            return peek(addr, n)
        self.m.ensure(addr)
        return bytes(self.uc.mem_read(addr, n))

    # -- register access -------------------------------------------------
    def _put(self, off, val):
        self.uc.mem_write(BASE + off, struct.pack('>I', val & 0xFFFFFFFF))

    def _get(self, off):
        return struct.unpack('>I', bytes(self.uc.mem_read(BASE + off, 4)))[0]

    def _set_bits(self, off, bits):
        self._put(off, self._get(off) | bits)

    def _clr_bits(self, off, bits):
        self._put(off, self._get(off) & ~bits)

    # -- hooks -----------------------------------------------------------
    def _on_sysctl_read(self, uc, typ, addr, size, val, data):
        """INITA and the three software resets have already self-cleared.

        Done on read rather than on write because a write hook runs before the
        store: anything cleared there is put straight back by the store that
        follows. The driver sets INITA and then polls, so the first poll sees
        it clear, which is the behaviour the manual describes.
        """
        cur = self._get(SYSCTL)
        if cur & (INITA | RSTA | RSTC | RSTD):
            self._put(SYSCTL, cur & ~(INITA | RSTA | RSTC | RSTD))

    def _on_xfertyp(self, uc, typ, addr, size, val, data):
        """Writing XFERTYP issues the command.

        `val` is the value being stored; the store has not happened yet, so
        reading XFERTYP back here would give the previous command.
        """
        xfer = val & 0xFFFFFFFF if size == 4 else self._get(XFERTYP)
        idx = (xfer >> 24) & 0x3F
        arg = self._get(CMDARG)
        self.log.append((idx, arg))
        r0, r1, r2, r3 = self.card.command(idx, arg)
        self._put(CMDRSP0, r0)
        self._put(CMDRSP1, r1)
        self._put(CMDRSP2, r2)
        self._put(CMDRSP3, r3)
        # Command completes instantly: never leave the inhibit bits set, or
        # the driver's `(PRSSTAT & 3) == 0` waits never finish.
        self._clr_bits(PRSSTAT, CIHB | CDIHB | DLA)
        self._set_bits(IRQSTAT, CC | TC)
        # A data command has to make the buffer look ready, or the driver
        # spins on PRSSTAT: BWEN at 0x40120236 before it writes, BREN at
        # 0x401202b2 before it reads.
        if xfer & DPSEL:
            if xfer & DTDSEL:                       # card -> host
                self._set_bits(PRSSTAT, BREN)
                self._set_bits(IRQSTAT, BRR)
                self._put(DATPORT, self.card.read_word(idx, self.pattern))
                payload = self.card.data_for(idx, arg, self._dma_size())
                if payload is not None and self.armed is not None:
                    n = self._dma_out(payload)
                    self.armed = None
                    # Channel 59's completion ISR posts this before the
                    # eSDHC transfer-complete ISR posts data_sem.  Model the
                    # two producers separately instead of satisfying every
                    # blocked wait globally.
                    self._post(self.dma_sem)
                    if self.trace:
                        print('[esdhc]   CMD%d -> %d bytes by eDMA' % (idx, n))
                # The bring-up pends on this one after the EXT_CSD DMA read.
                self._post(self.data_sem)
            else:                                   # host -> card
                self._set_bits(PRSSTAT, BWEN)
                self._set_bits(IRQSTAT, BWR)
                if self.armed is not None:
                    payload = self._dma_in()
                    self.card.write_data(idx, arg, payload)
                    self.armed = None
                    self._post(self.dma_sem)
                    if self.trace:
                        print('[esdhc]   CMD%d <- %d bytes by eDMA'
                              % (idx, len(payload)))
                self._post(self.data_sem)
        # The ISR's bookkeeping. 0x4011fe10 pre-sets this to 1 and returns it
        # after the wait; `unblock` satisfies the wait, so without this the
        # caller always sees "still in progress".
        # This word lives in SDRAM the guest may not have touched yet at this
        # point; _write does not map from the hook (see Machine.poke).
        self._write(self.drv_status, struct.pack('>I', 0))
        # The command-completion half of the ISR: this is what lets
        # FUN_4011d5b4 return from its sem_pend on the cold-boot path.
        self._post(self.cmd_sem)
        if self.trace:
            print('[esdhc] CMD%-2d arg=%#010x xfertyp=%#010x -> %#010x'
                  % (idx, arg, xfer, r0))

    def _on_serq(self, uc, typ, addr, size, val, data):
        """Remember when the eSDHC's channel is armed. Bit 6 means all."""
        if not (val & 0x40) and (val & 0x3F) == DMA_CHAN:
            self.armed = DMA_CHAN

    def _dma_out(self, payload):
        """Push `payload` through the armed channel's TCD, as the eDMA would.

        SOFF is zero for these transfers -- the source is the DATPORT register
        read over and over -- and DOFF equals NBYTES, so the destination is
        contiguous and this is a straight copy plus TCD bookkeeping.
        """
        if self.armed is None:
            return 0
        tcd = TCD_BASE + self.armed * 0x20
        def u32(o):
            return struct.unpack('>I', bytes(self.uc.mem_read(tcd + o, 4)))[0]
        def u16(o):
            return struct.unpack('>H', bytes(self.uc.mem_read(tcd + o, 2)))[0]
        citer, nbytes = u16(CITER) & 0x7FFF, u32(NBYTES)
        total = citer * nbytes
        if not total:
            return 0
        dst = u32(DADDR)
        chunk = payload[:total].ljust(total, b'\x00')
        # dst is the firmware's EXT_CSD buffer, which the guest has not
        # necessarily touched yet; _write does not map from the hook.
        self._write(dst, chunk)
        self.uc.mem_write(tcd + DADDR, struct.pack('>I', dst + total))
        self.uc.mem_write(tcd + CITER, struct.pack('>H', u16(BITER) & 0x7FFF))
        # The bring-up routine's EXT_CSD read (CMD8 SEND_EXT_CSD) does not
        # poll any eSDHC register for completion -- it arms eDMA channel 59,
        # issues the command, then polls TCD59's own CSR bit 7 (DONE) with a
        # bound of 20 retries of 1000 ticks each. Without this the poll
        # always times out, the routine bails to its error exit, and the
        # storage flag at the end of the routine is never set -- so storage
        # never comes up even though every eSDHC register was serviced
        # correctly. This is the one poll in the whole routine that has a
        # timeout, which is why the symptom was a silent failure rather than
        # a hang.
        self.uc.mem_write(tcd + CSR, struct.pack('>H', u16(CSR) | 0x80))
        self.dma_bytes += total
        return total

    def _dma_size(self):
        """Return the armed channel's remaining major-loop byte count."""
        if self.armed is None:
            return 0
        tcd = TCD_BASE + self.armed * 0x20
        citer = struct.unpack(
            '>H', bytes(self.uc.mem_read(tcd + CITER, 2))
        )[0] & 0x7FFF
        nbytes = struct.unpack(
            '>I', bytes(self.uc.mem_read(tcd + NBYTES, 4))
        )[0]
        return citer * nbytes

    def _dma_in(self):
        """Pull the armed channel's host buffer into the card."""
        if self.armed is None:
            return b''
        tcd = TCD_BASE + self.armed * 0x20

        def u32(o):
            return struct.unpack('>I', bytes(self.uc.mem_read(tcd + o, 4)))[0]

        def s32(o):
            return struct.unpack('>i', bytes(self.uc.mem_read(tcd + o, 4)))[0]

        def u16(o):
            return struct.unpack('>H', bytes(self.uc.mem_read(tcd + o, 2)))[0]

        def s16(o):
            return struct.unpack('>h', bytes(self.uc.mem_read(tcd + o, 2)))[0]

        citer, nbytes = u16(CITER) & 0x7FFF, u32(NBYTES)
        src, soff = u32(SADDR), s16(SOFF)
        payload = bytearray()
        for _ in range(citer):
            payload.extend(self._read(src, nbytes))
            src += soff
        src += s32(SLAST)
        self.uc.mem_write(tcd + SADDR, struct.pack('>I', src & 0xFFFFFFFF))
        self.uc.mem_write(tcd + CITER, struct.pack('>H', u16(BITER) & 0x7FFF))
        self.uc.mem_write(tcd + CSR, struct.pack('>H', u16(CSR) | 0x80))
        self.dma_bytes += len(payload)
        return bytes(payload)

    def _on_datport_write(self, uc, typ, addr, size, val, data):
        """Capture what the host puts in the buffer, for the bus test."""
        self.pattern = val & 0xFFFFFFFF

    def __repr__(self):
        return 'Esdhc(commands=%d, %s)' % (
            len(self.log), ' '.join('CMD%d' % c for c, _ in self.log[:16]))
