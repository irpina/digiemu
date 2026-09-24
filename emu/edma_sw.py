"""Software-started eDMA channels: the memory-to-memory moves nothing drove.

Digitakt mk1's audio render ISR (vector 191, `FUN_40077420`) does not only
compute. Partway through it hands a block move to an eDMA channel and then
waits for it:

    4007580e  movea.l (0x80001200).l,A0     ; A0 = the channel's TCD
    40075814  move.w  (0x1e,A0),D0w         ; CSR
    40075818  andi.l  #0x80,D0              ; DONE
    40075820  beq.b   0x40075814            ; spin until the transfer finishes

`0x80001200` holds `0xfc045400`, which is TCD 32. Nothing in the emulator ran
that channel, so DONE never set, and the render ISR -- entered exactly once --
never returned. Measured: of 9,615 audio interrupts, 9,614 found the processor
already at IPL 5, the render ISR's own level, because it was still in there.
Everything downstream stalled behind that one spin.

This is narrow on purpose. It does NOT implement eDMA. It watches one
channel's CSR, and when the guest sets START on a channel with no peripheral
request tied to it, performs the major loop as a plain memory copy and reports
completion the way the hardware would:

  * walk CITER minor loops of NBYTES bytes each, honouring SOFF and DOFF
  * apply SLAST and DLAST at the end, reload CITER from BITER
  * clear START and ACTIVE, set DONE

Field offsets are this SoC's, the same ones emu/ssi.py uses and the audio
init at `FUN_40000f64` writes: ATTR at +0x04, SOFF at +0x06, CITER at +0x14,
DOFF at +0x16, BITER at +0x1C, CSR at +0x1E. That is not the ordering in the
generic reference manual layout, and taking the generic one would move the
wrong words.

The channel number is not guessed: pass it in. Reading the pointer the
firmware itself stores is what identifies it, and a different build could
choose a different channel.

Two rules about WHEN, both learnt the hard way:

  * Nothing may be MAPPED inside the write hook. The hook runs in the middle
    of the guest's store; mapping a page there resizes Unicorn's TLB, and the
    store then finishes against the old table -- AddressSanitizer caught it
    as a heap-buffer-overflow in cputlb.c notdirty_write, and it was the
    random segfault every audio run hit sooner or later. So a transfer runs
    in the hook only when every page it touches -- every descriptor of a
    scatter-gather chain included -- is already mapped; otherwise it is
    deferred to service(), outside emulation, where mapping is safe.
  * DONE cannot be written from inside the guest's own write to the CSR (the
    pending value lands afterwards and erases it), so it is queued. It is
    applied the moment the guest next READS that CSR -- a read hook writes it
    just before the load -- or at the next service(), whichever comes first.
    Waiting for service() alone left the render spinning on DONE until the
    next run-loop boundary, up to a whole SSI period per transfer.
"""

# pyright: reportMissingImports=false
import struct

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE

TCD_BASE = 0xFC045000

# This SoC's TCD field offsets. See the module docstring.
SADDR, ATTR, SOFF, NBYTES = 0x00, 0x04, 0x06, 0x08
SLAST, DADDR, CITER, DOFF = 0x0C, 0x10, 0x14, 0x16
DLAST, BITER, CSR = 0x18, 0x1C, 0x1E

CSR_START, CSR_DONE, CSR_ACTIVE = 0x0001, 0x0080, 0x0040
CSR_ESG = 0x0010          # DLAST is a next-descriptor pointer
MAX_LINKS = 64            # a chain longer than this is a fault

# A transfer that asks for more than this is treated as a programming error
# rather than being run: it would mean the TCD was misread.
MAX_BYTES = 1 << 20

# emu.harness.Machine maps memory on demand in pages of this size.
MAP_PAGE = 0x100000


def _signed(value, bits):
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


# The whole descriptor: SADDR ATTR SOFF NBYTES SLAST DADDR CITER DOFF DLAST
# BITER CSR. Read and written in one piece -- field-at-a-time access cost a
# Python call into Unicorn per field, ~16 per transfer.
TCD_FMT = '>IHhIIIHhIHH'


def modulo_add(addr, delta, mod):
    """addr + delta with the low `mod` bits wrapping, as ATTR SMOD/DMOD do.

    The render's channels use it for ring buffers of 4 KB, 64 KB and 4 MB
    (mod 12, 16, 22); ignoring it ran a transfer straight past the ring.
    """
    if not mod:
        return (addr + delta) & 0xFFFFFFFF
    mask = (1 << mod) - 1
    return (addr & ~mask & 0xFFFFFFFF) | ((addr + delta) & mask)


def iteration_count(raw):
    """-> the minor-loop count encoded in a CITER/BITER word.

    Bit 15 is ELINK. Set, the count is only bits 8:0 and bits 14:9 name a
    channel to link to after each minor loop; clear, the count is bits 14:0.
    Reading the wide form unconditionally turns channel 32's nine-iteration
    move into 16,393 of them.
    """
    return (raw & 0x01FF) if raw & 0x8000 else (raw & 0x7FFF)


def link_channel(raw):
    """-> the channel CITER/BITER links to, or None."""
    return ((raw >> 9) & 0x3F) if raw & 0x8000 else None


class SoftwareChannel:
    """One software-started eDMA channel, run to completion on START."""

    def __init__(self, machine, channel):
        self.m = machine
        self.channel = int(channel)
        self.tcd = TCD_BASE + self.channel * 0x20
        self.transfers = 0
        self.bytes_moved = 0
        self.refused = 0
        self.links_ignored = 0
        self.links_followed = 0
        self.last_error = None
        self._pending = None
        self._deferred = False
        self.deferred = 0
        machine.uc.hook_add(
            UC_HOOK_MEM_WRITE, self._on_csr,
            begin=self.tcd + CSR, end=self.tcd + CSR + 1,
        )
        machine.uc.hook_add(
            UC_HOOK_MEM_READ, self._on_csr_read,
            begin=self.tcd + CSR, end=self.tcd + CSR + 1,
        )

    # -- TCD access -----------------------------------------------------
    def _u32(self, off):
        return struct.unpack('>I', bytes(self.m.uc.mem_read(self.tcd + off, 4)))[0]

    def _u16(self, off):
        return struct.unpack('>H', bytes(self.m.uc.mem_read(self.tcd + off, 2)))[0]

    def _w32(self, off, v):
        self.m.uc.mem_write(self.tcd + off, struct.pack('>I', v & 0xFFFFFFFF))

    def _w16(self, off, v):
        self.m.uc.mem_write(self.tcd + off, struct.pack('>H', v & 0xFFFF))

    # -- the transfer ---------------------------------------------------
    def _on_csr(self, uc, access, address, size, value, user_data):
        # Reconstruct the 16-bit CSR the guest is writing, whether it wrote
        # the whole word or one byte of it.
        current = bytearray(uc.mem_read(self.tcd + CSR, 2))
        offset = address - (self.tcd + CSR)
        current[offset:offset + size] = int(value).to_bytes(size, 'big')
        csr = int.from_bytes(current, 'big')
        if not csr & CSR_START:
            return
        # Publish the guest's own value before running, so the transfer reads
        # the CSR the guest meant. Its pending write lands the same bytes.
        self._w16(CSR, csr)
        if not self._mapped():
            # A page to map (or an unreadable chain): not in this hook.
            self._deferred = True
            self.deferred += 1
            return
        self._transfer(uc, ensure=False)

    def _on_csr_read(self, uc, access, address, size, value, user_data):
        """The guest is polling: let it see a finished transfer's DONE now."""
        self._apply()

    def _transfer(self, uc, ensure=True):
        """Run the major loop (and any chain), then queue the completion.

        `ensure` maps pages on demand; the in-hook path passes False, having
        checked with _mapped() that there is nothing to map.
        """
        try:
            moved = 0
            seen = set()
            tcd = list(struct.unpack(TCD_FMT, bytes(uc.mem_read(self.tcd,
                                                                0x20))))
            for _ in range(MAX_LINKS):
                moved += self._run(tcd, ensure)
                if not tcd[10] & CSR_ESG:
                    break
                nxt = tcd[8]
                if nxt in seen or nxt % 0x20:
                    raise RuntimeError('channel %d scatter-gather pointer '
                                       '0x%08x repeats or is unaligned'
                                       % (self.channel, nxt))
                seen.add(nxt)
                # Hardware loads the whole 32-byte descriptor over this one.
                raw = bytes(uc.mem_read(nxt, 0x20))
                uc.mem_write(self.tcd, raw)
                tcd = list(struct.unpack(TCD_FMT, raw))
                self.links_followed += 1
            else:
                raise RuntimeError('channel %d chain exceeded %d links'
                                   % (self.channel, MAX_LINKS))
        except Exception as exc:                          # noqa: BLE001
            self.refused += 1
            self.last_error = str(exc)
            return
        self.transfers += 1
        self.bytes_moved += moved
        # START and ACTIVE clear, DONE sets. Take the rest of the bits from
        # the descriptor the chain ENDED on, not from the value the guest
        # wrote: a scatter-gather chain replaces the CSR each time it links,
        # and the last descriptor is where E_SG goes away. Deriving from the
        # guest's own write instead puts E_SG back, and channel 30's caller
        # waits on exactly that bit.
        final = tcd[10]
        self._pending = (final & ~(CSR_START | CSR_ACTIVE)) | CSR_DONE

    @staticmethod
    def _span(base, off, citer, nbytes, mod=0):
        """-> the MAP_PAGE-aligned pages a channel's major loop will touch.

        With a modulo the addresses stay inside the 2**mod ring holding
        `base`, so the ring is what is covered.
        """
        span = abs(off) * citer * max(1, nbytes // max(1, abs(off) or 1))
        span = max(span, citer * nbytes, 1)
        lo = min(base, base + (off * citer * nbytes if off < 0 else 0))
        if mod:
            ring = 1 << mod
            lo = base & ~(ring - 1) & 0xFFFFFFFF
            span = min(span, ring)
            if (base & (ring - 1)) + span > ring:
                span = ring             # it wraps: the whole ring
            else:
                lo = base
        first = lo & ~(MAP_PAGE - 1)
        last = (lo + span - 1) & ~(MAP_PAGE - 1)
        return [(first + page) & 0xFFFFFFFF
                for page in range(0, last - first + MAP_PAGE, MAP_PAGE)]

    def _ensure_span(self, base, off, citer, nbytes, mod=0):
        """Map every page a channel's major loop will touch."""
        ensure = getattr(self.m, 'ensure', None)
        if ensure is None:
            return
        for page in self._span(base, off, citer, nbytes, mod):
            try:
                ensure(page)
            except Exception:                                 # noqa: BLE001
                return

    def _mapped(self):
        """-> True when the programmed transfer needs no page mapped.

        Follows a scatter-gather chain descriptor by descriptor, so a chain
        whose every descriptor and buffer is already mapped can still run in
        the hook. A machine that does not map on demand (no `mapped` set) has
        nothing to map, so any transfer is fine.
        """
        mapped = getattr(self.m, 'mapped', None)
        if mapped is None or getattr(self.m, 'ensure', None) is None:
            return True
        raw = bytes(self.m.uc.mem_read(self.tcd, 0x20))
        seen = set()
        for _ in range(MAX_LINKS):
            saddr, attr, soff, nbytes, _slast, daddr, citer, doff, dlast, \
                _biter, csr = struct.unpack(TCD_FMT, raw)
            citer = iteration_count(citer)
            pages = (self._span(saddr, soff, citer, nbytes, attr >> 11)
                     + self._span(daddr, doff, citer, nbytes,
                                  (attr >> 3) & 0x1F))
            if not all(p in mapped for p in pages):
                return False
            if not csr & CSR_ESG:
                return True
            if dlast in seen or dlast % 0x20 \
                    or (dlast & ~(MAP_PAGE - 1)) not in mapped:
                return False
            seen.add(dlast)
            raw = bytes(self.m.uc.mem_read(dlast, 0x20))
        return False

    def _read_ring(self, addr, n, mod):
        """n bytes from addr, wrapping inside its 2**mod ring if mod."""
        uc = self.m.uc
        if not mod:
            return bytes(uc.mem_read(addr, n))
        out = bytearray()
        ring = 1 << mod
        while n:
            c = min(n, ring - (addr & (ring - 1)))
            out += uc.mem_read(addr, c)
            addr = modulo_add(addr, c, mod)
            n -= c
        return bytes(out)

    def _write_ring(self, addr, data, mod):
        """data to addr, wrapping inside its 2**mod ring if mod."""
        uc = self.m.uc
        if not mod:
            uc.mem_write(addr, data)
            return
        ring = 1 << mod
        pos = 0
        while pos < len(data):
            c = min(len(data) - pos, ring - (addr & (ring - 1)))
            uc.mem_write(addr, data[pos:pos + c])
            addr = modulo_add(addr, c, mod)
            pos += c

    def _run(self, tcd, ensure=True):
        """One major loop of the descriptor `tcd` (a TCD_FMT list).

        Updates `tcd` in place and writes it back to the channel's TCD in one
        piece. -> bytes moved.
        """
        saddr, attr, soff, nbytes, slast, daddr, citer_raw, doff, dlast, \
            biter_raw, csr = tcd
        citer = iteration_count(citer_raw)
        if link_channel(citer_raw) is not None:
            # Counted, not performed. A minor-loop link would start another
            # channel; nothing here has needed it, and a silent no-op would be
            # the kind of omission that looks like working code.
            self.links_ignored += 1
        if not citer or not nbytes:
            raise RuntimeError('channel %d started with CITER=%d NBYTES=%d'
                               % (self.channel, citer, nbytes))
        if citer * nbytes > MAX_BYTES:
            raise RuntimeError('channel %d would move %d bytes'
                               % (self.channel, citer * nbytes))
        ssize = 1 << ((attr >> 8) & 0x7)
        dsize = 1 << (attr & 0x7)
        smod, dmod = attr >> 11, (attr >> 3) & 0x1F
        if nbytes % ssize or nbytes % dsize:
            raise RuntimeError('channel %d minor loop %d is not a whole '
                               'number of %d/%d-byte beats'
                               % (self.channel, nbytes, ssize, dsize))
        uc = self.m.uc
        if ensure:
            # Demand-map both ends first. The guest may not have touched this
            # SDRAM yet, and to the hardware the memory is simply there; see
            # the same call in emu/esdhc.py. Page granularity, so it is cheap.
            self._ensure_span(saddr, soff, citer, nbytes, smod)
            self._ensure_span(daddr, doff, citer, nbytes, dmod)
        total = citer * nbytes
        if soff == ssize and doff == dsize:
            # Both sides contiguous: the whole major loop is one block copy
            # (and, where the beat sizes differ, that is what the hardware
            # produces), split only where a ring wraps.
            self._write_ring(daddr, self._read_ring(saddr, total, smod), dmod)
            source = modulo_add(saddr, total, smod)
            dest = modulo_add(daddr, total, dmod)
        else:
            # Minor loop by minor loop: gather NBYTES in source beats, then
            # scatter it in destination beats, each side on its own offset.
            source, dest = saddr, daddr
            for _ in range(citer):
                buf = bytearray()
                for _k in range(nbytes // ssize):
                    buf += uc.mem_read(source, ssize)
                    source = modulo_add(source, soff, smod)
                for k in range(nbytes // dsize):
                    uc.mem_write(dest, bytes(buf[k * dsize:(k + 1) * dsize]))
                    dest = modulo_add(dest, doff, dmod)
        tcd[0] = (source + _signed(slast, 32)) & 0xFFFFFFFF
        if not csr & CSR_ESG:
            # Only a non-scatter-gather descriptor adjusts the destination
            # with DLAST; with E_SG that word is the next descriptor.
            tcd[5] = (dest + _signed(dlast, 32)) & 0xFFFFFFFF
        else:
            tcd[5] = dest
        tcd[6] = biter_raw
        uc.mem_write(self.tcd, struct.pack(TCD_FMT, *tcd))
        return total

    # -- the run loop -----------------------------------------------------
    def step(self, done, remaining=None):
        """No deadline of its own; never shortens the caller's step."""
        return None

    def service(self, done):
        """Outside emulation: run a deferred transfer, apply its completion."""
        if self._deferred:
            self._deferred = False
            self._transfer(self.m.uc)
        return self._apply()

    def _apply(self):
        """Write a queued completion, now that the guest's write has landed."""
        if self._pending is None:
            return False
        self._w16(CSR, self._pending)
        self._pending = None
        return True

    # -- snapshots ------------------------------------------------------
    def checkpoint_state(self):
        return {'type': 'SoftwareChannel', 'version': 1,
                'channel': self.channel, 'transfers': self.transfers,
                'bytes_moved': self.bytes_moved, 'refused': self.refused,
                'links_ignored': self.links_ignored,
                'links_followed': self.links_followed}

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'SoftwareChannel' or state.get('version') != 1:
            raise RuntimeError('unsupported SoftwareChannel checkpoint state')
        if state.get('channel') != self.channel:
            raise RuntimeError('SoftwareChannel number mismatch')
        self.transfers = state['transfers']
        self.bytes_moved = state['bytes_moved']
        self.refused = state['refused']
        self.links_ignored = state.get('links_ignored', 0)
        self.links_followed = state.get('links_followed', 0)


# Channels another model already drives. Running their descriptors here too
# would duplicate a peripheral's transfers.
CLAIMED = frozenset((35, 48, 50, 52, 54, 59))

# The controller's Set START Request register: writing a channel number sets
# that descriptor's CSR.START without touching the CSR itself (bit 6 means
# every channel). The Digitone's audio handler starts its DSP transfers on
# channel 47 this way (0x4009d18c and 0x4009e030 in OS 1.43), never through
# the CSR, so a bank watching only the CSRs never ran them and the handler
# spun on DONE for ever.
EDMA_SSRT = 0xFC04401E


class SoftwareBank:
    """Every software-started channel, under one hook over the TCD region.

    The guest starts these by writing START into a descriptor's CSR. One hook
    across all 64 descriptors costs no more than one per channel and, unlike a
    fixed list, does not have to be told which channels the firmware will use.
    """

    def __init__(self, machine, channels=64, claimed=CLAIMED, native=None):
        self.m = machine
        self.claimed = frozenset(claimed)
        self.channels = {}
        self.pending = []
        self.ssrt_starts = 0
        self._native = None
        # SSRT starts go through Python on either path: the native engine
        # watches the CSRs only.
        machine.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_ssrt,
                            begin=EDMA_SSRT, end=EDMA_SSRT)
        for ch in range(channels):
            if ch in self.claimed:
                continue
            # Construct without its own hook; the bank does the watching.
            c = SoftwareChannel.__new__(SoftwareChannel)
            c.m = machine
            c.channel = ch
            c.tcd = TCD_BASE + ch * 0x20
            c.transfers = c.bytes_moved = c.refused = 0
            c.links_ignored = c.links_followed = 0
            c.last_error = None
            c._pending = None
            c._deferred = False
            c.deferred = 0
            self.channels[ch] = c
        # The patched Unicorn can run these natively (emu/native.py): same
        # rules, no Python call per descriptor access. The Python channels
        # above then only run a transfer the native side deferred because a
        # page has to be mapped first -- which only Python can do, outside
        # emulation. native=False forces the Python hooks.
        from emu import native as _native
        if native is not False and channels == 64 \
                and _native.edma_available(machine.uc):
            self._native = _native.NativeEdma(machine.uc, TCD_BASE, MAP_PAGE,
                                              self.claimed)
            return
        # One hook pair over the whole region, not one per CSR: Unicorn walks
        # its hook list on every slow-path access, so 128 tiny hooks made the
        # render slower (measured 2.0 -> 2.8 ms a pass) than one wide one.
        machine.uc.hook_add(
            UC_HOOK_MEM_WRITE, self._on_write,
            begin=TCD_BASE, end=TCD_BASE + channels * 0x20 - 1,
        )
        machine.uc.hook_add(
            UC_HOOK_MEM_READ, self._on_read,
            begin=TCD_BASE, end=TCD_BASE + channels * 0x20 - 1,
        )

    def _channel_for_csr(self, address, size):
        """-> the channel whose CSR half-word this access touches, or None."""
        offset = (address - TCD_BASE) % 0x20
        if offset + size <= CSR or offset > CSR + 1:
            return None
        return self.channels.get((address - TCD_BASE) // 0x20)

    def _on_write(self, uc, access, address, size, value, user_data):
        # Only a write touching the CSR half-word can start a channel.
        channel = self._channel_for_csr(address, size)
        if channel is None:
            return
        channel._on_csr(uc, access, address, size, value, user_data)
        if ((channel._pending is not None or channel._deferred)
                and channel not in self.pending):
            self.pending.append(channel)

    def _on_read(self, uc, access, address, size, value, user_data):
        # A poll of a CSR whose transfer has finished sees DONE at once.
        channel = self._channel_for_csr(address, size)
        if channel is not None and channel._pending is not None:
            channel._apply()

    def _on_ssrt(self, uc, access, address, size, value, user_data):
        """SSRT: start the channel written, as a CSR START would. The guest's
        store is to SSRT, not to the CSR, so the completion can be written
        straight away; a transfer that needs a page mapped waits for
        service(), like any other."""
        v = value & 0xFF
        if v & 0x40:
            return                      # 'all channels': nothing uses it
        channel = self.channels.get(v & 0x3F)
        if channel is None:
            return
        channel._w16(CSR, channel._u16(CSR) | CSR_START)
        self.ssrt_starts += 1
        if not channel._mapped():
            channel._deferred = True
            channel.deferred += 1
            if channel not in self.pending:
                self.pending.append(channel)
            return
        channel._transfer(uc, ensure=False)
        channel._apply()

    # -- the run loop ---------------------------------------------------
    def step(self, done, remaining=None):
        return None

    def service(self, done):
        did = False
        if self.pending:
            for channel in self.pending:
                channel.service(done)
            self.pending = []
            did = True
        if self._native is not None:
            did = self._service_native() or did
        return did

    def _service_native(self):
        """Run what the native side deferred; show any unpolled DONE."""
        e = self._native.state
        did = False
        if e.any_deferred:
            e.any_deferred = 0
            for ch in range(64):
                if e.deferred[ch]:
                    e.deferred[ch] = 0
                    channel = self.channels.get(ch)
                    if channel is not None:
                        channel.deferred += 1
                        channel._transfer(self.m.uc)
                        channel._apply()
                        did = True
        pending = bytes(e.pending)
        if pending.strip(b'\0'):
            for ch in range(64):
                v = e.pending[ch]
                if v:
                    e.pending[ch] = 0
                    self.m.uc.mem_write(TCD_BASE + ch * 0x20 + CSR,
                                        struct.pack('>H', v))
                    did = True
        return did

    def _native_sum(self, field):
        if self._native is None:
            return 0
        return sum(getattr(self._native.state, field))

    # -- reporting ------------------------------------------------------
    @property
    def native(self):
        """True when the transfers run in the patched Unicorn."""
        return self._native is not None

    @property
    def transfers(self):
        return (sum(c.transfers for c in self.channels.values())
                + self._native_sum('transfers'))

    @property
    def bytes_moved(self):
        return (sum(c.bytes_moved for c in self.channels.values())
                + self._native_sum('bytes'))

    @property
    def refused(self):
        return (sum(c.refused for c in self.channels.values())
                + self._native_sum('refused'))

    @property
    def deferred(self):
        """Transfers run at a step boundary instead of in the guest's store."""
        return sum(c.deferred for c in self.channels.values())

    def used(self):
        """-> {channel: transfers} for channels that actually ran."""
        e = self._native.state if self._native is not None else None
        out = {}
        for ch, c in sorted(self.channels.items()):
            n = c.transfers + (e.transfers[ch] if e is not None else 0)
            bad = c.refused + (e.refused[ch] if e is not None else 0)
            if n or bad:
                out[ch] = n
        return out

    def errors(self):
        return {ch: c.last_error for ch, c in sorted(self.channels.items())
                if c.last_error}

    def checkpoint_state(self):
        return {'type': 'SoftwareBank', 'version': 1,
                'channels': {str(ch): c.checkpoint_state()
                             for ch, c in self.channels.items()}}

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'SoftwareBank' or state.get('version') != 1:
            raise RuntimeError('unsupported SoftwareBank checkpoint state')
        for key, sub in state['channels'].items():
            channel = self.channels.get(int(key))
            if channel is not None:
                channel.restore_checkpoint_state(sub)


def install(machine, events, channel):
    source = SoftwareChannel(machine, channel)
    events['edma_sw_%d' % channel] = source
    return source


def install_bank(machine, events, **kwargs):
    source = SoftwareBank(machine, **kwargs)
    events['edma_sw_bank'] = source
    return source
