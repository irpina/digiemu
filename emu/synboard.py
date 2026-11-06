"""The Syntakt's board: what its OS needs that the other products do not.

Installed only when emu/symbols.py finds `syn_ssi_sync`, the start of the
Syntakt's audio bring-up, so every other product's run is unchanged.

DMA timer 2's counter. The bootstrap writes DTMR2 = 7 (external clock, out of
reset) and clears DTCN2; the OS never programs DTIM2 but reads DTCN2 as a
free-running clock: the audio bring-up waits for it to pass the next 32K
boundary plus 64K, and the USB start-of-frame handler (vector 209) stamps
events with it. The emulator starts the OS without its bootstrap, and nothing
drives the external pin, so the counter would never move and the bring-up
would spin forever (99% of all blocks, measured after the intro).

Which clock the pin carries is not established [O]. Here every read of DTCN2
advances the counter by STEP, so it is monotonic, a wait for N counts
finishes after N / STEP reads, and the value lives only in guest memory: a
run is deterministic and a snapshot needs no extra state.

DSPI1's frames: see _dspi1_done.

eDMA channel 29's completion. The second CPU's boot task streams section 6
and then section 7 out of DSPI2 on channel 29 (TCD 0xFC0453A0, destination
the PUSHR byte at 0xEC038037) and the handshake waits for the channel's
major-loop interrupt (vector 149, INTC1 source 21), whose handler starts the
second stream and marks the first done (0x41b9b160 on 1.40A). The Digitone's
task polls DSPI2 instead, so digiemu never needed this. emu/dsplink.py loads
section 7 straight into the second CPU, so the bytes themselves go nowhere:
a start of channel 29 (29 written to SERQ) completes at once, with DONE set
in the TCD's CSR, and its interrupt is delivered at the next step boundary
once the main CPU's mask allows it, as emu/dsplink.py delivers the edge port.
"""
import struct

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
from unicorn.m68k_const import UC_M68K_REG_SR

from emu.pit import PENDING_STEP, interrupt_level

DTIM2 = 0xFC078000
DTCN2 = DTIM2 + 0x0C
STEP = 0x2000


SERQ = 0xFC044018
DSPI_CHANNEL = 29
TCD29 = 0xFC045000 + DSPI_CHANNEL * 0x20
TCD29_CSR = TCD29 + 0x1E
CSR_DONE = 0x0080
DSPI_VECTOR = 128 + 21          # INTC1 source 21: eDMA channel 29


class Edma29:
    """Channel 29's starts, completed at once; their interrupts delivered on
    the main CPU's step clock (the emu.longrun async source protocol)."""

    def __init__(self, m):
        self.m = m
        self.pending = 0
        self.now = 0
        self.starts = 0
        self.delivered = 0
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_serq, begin=SERQ, end=SERQ)

    def _on_serq(self, uc, access, address, size, value, data):
        if value & 0x7F != DSPI_CHANNEL or value & 0x80:
            return
        self.starts += 1
        csr = struct.unpack('>H', bytes(uc.mem_read(TCD29_CSR, 2)))[0]
        uc.mem_write(TCD29_CSR, struct.pack('>H', csr | CSR_DONE))
        if csr & 0x0002:                # INTMAJOR: it interrupts when done
            self.pending += 1

    def step(self, done, remaining=None):
        self.now = done
        if self.pending:
            return min(PENDING_STEP, remaining) if remaining is not None \
                else PENDING_STEP
        return remaining

    def service(self, done):
        self.now = done
        if not self.pending:
            return False
        level = interrupt_level(self.m, DSPI_VECTOR, respect_mask=False)
        if level is None:
            return False
        sr = self.m.uc.reg_read(UC_M68K_REG_SR)
        if ((sr >> 8) & 7) >= level:
            return False
        if self.m.raise_vector(DSPI_VECTOR, level=level):
            self.pending -= 1
            self.delivered += 1
            return True
        return False


class Board:
    def __init__(self, edma29=None, never_fake=()):
        self.reads = 0
        self.dspi1_polls = 0
        self.edma29 = edma29
        # Semaphores whose posters semscan cannot see (emu/symbols.py
        # syn_midi_sem): emu.longrun must never satisfy a pend on them.
        self.never_fake = tuple(never_fake)

    def checkpoint_state(self):
        e = self.edma29
        return {'type': 'SynBoard', 'version': 1,
                'edma29_pending': e.pending if e else 0}

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'SynBoard' or state.get('version') != 1:
            raise ValueError('not a SynBoard state: %r' % (state,))
        if self.edma29 is not None:
            self.edma29.pending = int(state.get('edma29_pending', 0))


def _dtim2_counter(m, board):
    m.ensure(DTIM2)

    def count(uc, typ, addr, size, val, data):
        if addr != DTCN2:
            return
        board.reads += 1
        cur = struct.unpack('>I', bytes(uc.mem_read(DTCN2, 4)))[0]
        uc.mem_write(DTCN2, struct.pack('>I', (cur + STEP) & 0xFFFFFFFF))
    m.uc.hook_add(UC_HOOK_MEM_READ, count, begin=DTCN2, end=DTCN2 + 3)


DSPI1_SR = 0xFC03C02C
DSPI_EOQF_TCF = 0x90000000


def _dspi1_done(m, board):
    """DSPI1: the main CPU's render sends a frame on it every block (to the
    FPGA's side of the board, not modelled) and spins on SR.EOQF until it has
    gone (0x400f5262 on 1.40A, called from the render at vector 191). With no
    model the flag never sets and the render never returns, so the whole
    audio path stops at its first pass. Every read sees the frame finished:
    EOQF and TCF set. Received words are not modelled (POPR reads 0)."""
    m.ensure(DSPI1_SR)

    def done(uc, typ, addr, size, val, data):
        board.dspi1_polls += 1
        cur = struct.unpack('>I', bytes(uc.mem_read(DSPI1_SR, 4)))[0]
        uc.mem_write(DSPI1_SR, struct.pack('>I', cur | DSPI_EOQF_TCF))
    m.uc.hook_add(UC_HOOK_MEM_READ, done, begin=DSPI1_SR, end=DSPI1_SR + 3)


BOARD_ID = 0x00000000
BOARD_ID_VALUE = 0x04


def _board_id(m, board):
    """The byte at address 0 (FlexBus, on the board): the OS reads it once at
    start-up (0x4000ad40 on 1.40A) as a hardware revision -- low five bits
    the revision, top three a variant. Variants 0x82/0x23/0x43 get "OLD
    HARDWARE, SOUNDS WILL NOT BE COMPATIBLE"; below revision 3 (or 3 with a
    variant below 3) "TOO OLD HARDWARE FOR THIS OS", and the audio engine is
    paused for good (0x400a46b0 sets 0x43bc5364): the render then fetches
    the second CPU's voices but never sends it a parameter frame, so no note
    ever reaches it. Nothing drives the bus here, so it read 0. The real
    board's value is not known [O]; 0x04 (revision 4, variant 0) is one the
    OS accepts."""
    m.ensure(BOARD_ID)

    def rev(uc, typ, addr, size, val, data):
        if addr <= BOARD_ID < addr + size:
            uc.mem_write(BOARD_ID, bytes((BOARD_ID_VALUE,)))
    m.uc.hook_add(UC_HOOK_MEM_READ, rev, begin=BOARD_ID, end=BOARD_ID)


def is_syntakt(profile):
    return getattr(profile, 'syn_ssi_sync', None) is not None


def install(m, ev, profile):
    """Model the Syntakt's board on `m` if `profile` is one's. -> the Board,
    also ev['synboard'], or None (then nothing is installed)."""
    if not is_syntakt(profile):
        return None
    edma29 = Edma29(m)
    m.async_sources = getattr(m, 'async_sources', ()) + (edma29,)
    board = Board(edma29, never_fake=tuple(
        sem for sem in (getattr(profile, 'syn_midi_sem', None),)
        if sem is not None))
    _dtim2_counter(m, board)
    _dspi1_done(m, board)
    _board_id(m, board)
    if ev is not None:
        ev['synboard'] = board
    return board
