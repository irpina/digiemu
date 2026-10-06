"""Opt-in SSI0/eDMA48/50 event model.

This is deliberately narrower than a generic SSI or eDMA implementation.  It
models the producer chain recovered from DT2 firmware:

    SSI0 request cadence -> TCD48/TCD50 minor loops -> TCD50 major interrupt
    -> vector 170 -> guest INTFRCH1[31] write -> vector 191

The request rate must be supplied explicitly.  DT2 selects an external
SSI_CLKIN, whose board frequency is not yet recovered; silently assuming an
audio sample rate would turn an exploratory model into false qualification.
RX destination bytes are preserved rather than inventing data from the
external SSI peer.
"""

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
from __future__ import annotations

from fractions import Fraction
import math
import struct
import zlib

from unicorn import UC_HOOK_MEM_WRITE
from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_SR

from emu.edma import (
    ATTR,
    BITER,
    CITER,
    CSR,
    DADDR,
    DLAST,
    DOFF,
    EDMA_BASE,
    NBYTES,
    SADDR,
    SERQ,
    SLAST,
    SOFF,
    TCD_BASE,
)
from emu.pit import interrupt_level


CINT = EDMA_BASE + 0x1C
INTFRCH1 = 0xFC04C010
INTFRCH1_SOURCE63 = 0x80000000
RX_CHAN, TX_CHAN = 48, 50
RX_REGISTER, TX_REGISTER = 0xFC0BC008, 0xFC0BC000
RX_VECTOR, TX_VECTOR, FORCE_VECTOR = 168, 170, 191


class Profile:
    """Which SSI block, eDMA channels and vectors a product wires together.

    `legacy` is the TCD shape arm_legacy() insists on before claiming the
    descriptors the firmware already programmed: (peripheral address, ATTR,
    SOFF for tx / DOFF for rx, NBYTES, the other offset, CSR & csr_mask).
    Refusing to start on an unexpected shape is the point -- it is what stops
    this model from silently driving something that is not the audio path.
    """

    def __init__(self, name, rx_chan, tx_chan, rx_register, tx_register,
                 rx_vector, tx_vector, legacy, csr_mask=0x12):
        self.name = name
        self.rx_chan, self.tx_chan = rx_chan, tx_chan
        self.rx_register, self.tx_register = rx_register, tx_register
        self.rx_vector, self.tx_vector = rx_vector, tx_vector
        self.legacy = legacy
        self.csr_mask = csr_mask

    @property
    def channels(self):
        # A product with no audio input has no receive channel (None).
        return tuple(c for c in (self.rx_chan, self.tx_chan) if c is not None)


# Digitakt II, as originally measured: 32-byte minor loops, and the transmit
# channel wraps by scatter-gather (CSR E_SG 0x10 plus INT_MAJOR 0x02).
DIGITAKT2 = Profile(
    'digitakt2', 48, 50, 0xFC0BC008, 0xFC0BC000, 168, 170,
    legacy={48: (0xFC0BC008, 0x0202, 0, 0x20, 4, 0x10),
            50: (0xFC0BC000, 0x0202, 4, 0x20, 0, 0x12)},
)

# Digitakt mk1, from FUN_40000f64: a 512-byte transmit buffer at 0x4ba8f080
# moved 8 bytes at a time (one stereo pair of 32-bit words), 64 minor loops
# per major loop, SLAST -512 so it wraps in place, and CSR 6 = INT_HALF |
# INT_MAJOR -- a double buffer rather than scatter-gather. The receive side is
# the mirror image into 0x80001000.
MK1 = Profile(
    'digitakt-mk1', 52, 54, 0xFC0C8008, 0xFC0C8000, 172, 174,
    legacy={52: (0xFC0C8008, 0x0202, 0, 8, 4, 0x06),
            54: (0xFC0C8000, 0x0202, 4, 8, 0, 0x06)},
    csr_mask=0x16,
)

# Model:Cycles and Model:Samples: the Digitakt mk1's transmit shape (a 512-byte
# buffer, 8 bytes a minor loop, 64 a major loop, CSR 6), but on SSI0 and eDMA
# channel 50 as on the Digitakt II, and no receive channel -- neither has an
# audio input. Read off the descriptors both program (OS 1.13); their SSI0
# clock and word-length registers are the Digitakt mk1's SSI1 values.
MODELS = Profile(
    'models', None, 50, None, 0xFC0BC000, None, 170,
    legacy={50: (0xFC0BC000, 0x0202, 4, 8, 0, 0x06)},
    csr_mask=0x16,
)

PROFILES = {p.name: p for p in (DIGITAKT2, MK1, MODELS)}


def _signed(value, bits):
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


class Ssi0Dma:
    """Exact-deadline SSI request source for the observed DT2 descriptors."""

    def __init__(self, machine, request_hz, instr_per_sec, at=None, force_rte=None,
                 profile=DIGITAKT2, batch=False):
        if request_hz <= 0 or instr_per_sec <= 0:
            raise ValueError("SSI request and instruction rates must be positive")
        self.p = profile
        self.m = machine
        self.request_hz = int(request_hz)
        self.ips = int(instr_per_sec)
        self.now = 0
        self.next = None
        # batch: serve the requests up to the transmit channel's next half or
        # major-loop boundary in one step, instead of one step a frame. The
        # guest only observes the DMA at those boundaries -- they are where
        # its interrupts fire, and the render reads the position only at its
        # own entry -- so what it sees is unchanged, at 1/32 of the steps on
        # Digitakt mk1. The minor loops of a batch are moved in bulk.
        self.batch = bool(batch)
        self._due = None
        self.enabled = set()
        self.int50_asserted = False
        self.int50_delivered = False
        self.force_asserted = False
        self.force_delivered = False
        self.requests = 0
        self.major_loops = {c: 0 for c in profile.channels}
        self.scatter_gathers = {c: 0 for c in profile.channels}
        self.half_loops = 0
        self.tx_bytes = 0
        self.tx_crc32 = 0
        # Optional callable receiving every byte the transmit channel moves,
        # i.e. the PCM actually leaving for the codec.
        self.sink = None
        # Optional callable supplying the receive FIFO: rx_source(n) -> n
        # bytes. Left None, the receive channel copies whatever is already at
        # the FIFO address, which is what it has always done -- this model
        # does not invent data from an absent peer unless told to.
        self.rx_source = None
        self.rx_bytes = 0
        self.vector170 = 0
        self.vector191 = 0
        self._checkpoint_restored = False

        machine.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_serq, begin=SERQ, end=SERQ)
        machine.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_cint, begin=CINT, end=CINT)
        machine.uc.hook_add(
            UC_HOOK_MEM_WRITE,
            self._on_intfrch1,
            begin=INTFRCH1,
            end=INTFRCH1 + 3,
        )
        if at is not None and force_rte is not None:
            at(force_rte, self._on_force_rte)

    # The period and the current deadline are cached: `step` and `service`
    # run on every step of the live-audio GUI, several thousand a second,
    # and rebuilding the same Fractions each time was a measurable part of
    # its wall time. The values are exactly those computed before.
    @property
    def ips(self):
        return self._ips

    @ips.setter
    def ips(self, v):
        self._ips = v
        self._period = None

    @property
    def period(self):
        p = self.__dict__.get('_period')
        if p is None:
            p = self._period = Fraction(self.ips, self.request_hz)
            self._period_ceil = max(1, math.ceil(p))
        return p

    def align(self, now):
        """Start a fresh SSI clock at an explicit legacy-upgrade boundary."""
        self.now = int(now)
        if self.enabled and (self.next is None or not self._checkpoint_restored):
            self.next = Fraction(self.now) + self.period

    def arm_legacy(self):
        """Claim the already-programmed DT2 descriptors at an explicit upgrade."""
        expected = self.p.legacy
        for channel in self.p.channels:
            peripheral = (
                self._u32(channel, SADDR)
                if channel == self.p.rx_chan
                else self._u32(channel, DADDR)
            )
            actual = (
                peripheral,
                self._u16(channel, ATTR),
                _signed(self._u16(channel, SOFF), 16),
                self._u32(channel, NBYTES),
                _signed(self._u16(channel, DOFF), 16),
                self._u16(channel, CSR) & self.p.csr_mask,
            )
            if actual != expected[channel]:
                raise RuntimeError(
                    f"SSI legacy upgrade TCD{channel} shape mismatch: "
                    f"actual={actual!r} expected={expected[channel]!r}"
                )
            if not self._u16(channel, CITER) or not self._u16(channel, BITER):
                raise RuntimeError(f"SSI legacy upgrade found inactive TCD{channel}")
        self.enabled.update(self.p.channels)
        if self.next is None:
            self.next = Fraction(self.now) + self.period

    def checkpoint_state(self):
        next_value = None
        if self.next is not None:
            next_value = [self.next.numerator, self.next.denominator]
        return {
            "type": "Ssi0Dma",
            "version": 1,
            "profile": self.p.name,
            "half_loops": self.half_loops,
            "request_hz": self.request_hz,
            "ips": self.ips,
            "now": self.now,
            "next": next_value,
            "enabled": sorted(self.enabled),
            "int50_asserted": self.int50_asserted,
            "int50_delivered": self.int50_delivered,
            "force_asserted": self.force_asserted,
            "force_delivered": self.force_delivered,
            "requests": self.requests,
            "major_loops": dict(self.major_loops),
            "scatter_gathers": dict(self.scatter_gathers),
            "tx_bytes": self.tx_bytes,
            "tx_crc32": self.tx_crc32,
            "vector170": self.vector170,
            "vector191": self.vector191,
        }

    def restore_checkpoint_state(self, state):
        if state.get("type") != "Ssi0Dma" or state.get("version") != 1:
            raise RuntimeError("unsupported Ssi0Dma checkpoint state")
        if state.get("request_hz") != self.request_hz:
            raise RuntimeError("Ssi0Dma request-rate mismatch")
        saved = state.get("profile", DIGITAKT2.name)
        if saved != self.p.name:
            raise RuntimeError("Ssi0Dma profile mismatch: %s vs %s"
                               % (saved, self.p.name))
        self.half_loops = state.get("half_loops", 0)
        self.ips = state["ips"]
        self.now = state["now"]
        raw_next = state["next"]
        self.next = None if raw_next is None else Fraction(*raw_next)
        self.enabled = set(state["enabled"])
        self.int50_asserted = state["int50_asserted"]
        self.int50_delivered = state["int50_delivered"]
        self.force_asserted = state["force_asserted"]
        self.force_delivered = state["force_delivered"]
        self.requests = state["requests"]
        self.major_loops = {int(k): v for k, v in state["major_loops"].items()}
        self.scatter_gathers = {
            int(k): v for k, v in state["scatter_gathers"].items()
        }
        self.tx_bytes = state["tx_bytes"]
        self.tx_crc32 = state["tx_crc32"]
        self.vector170 = state["vector170"]
        self.vector191 = state["vector191"]
        self._checkpoint_restored = True

    def _frames_to_event(self):
        """Requests until the transmit channel's next half/major boundary."""
        if not self.batch or self.p.tx_chan not in self.enabled:
            return 1
        ch = self.p.tx_chan
        citer = self._u16(ch, CITER) & 0x7FFF
        biter = self._u16(ch, BITER) & 0x7FFF
        if not citer:
            return 1
        half = biter // 2
        if self._u16(ch, CSR) & 0x0004 and citer > half:
            return citer - half
        return citer

    def _deadline(self):
        if self._due is None:
            self._due = self._frames_to_event()
        period = self.period
        key = self.__dict__.get('_dl_key')
        if key is not None and key[0] is self.next and key[1] == self._due \
                and key[2] is period:
            return self._dl
        self._dl = self.next + period * (self._due - 1)
        self._dl_ceil = math.ceil(self._dl)
        self._dl_key = (self.next, self._due, period)
        return self._dl

    def _deadline_ceil(self):
        """-> ceil(_deadline()): for an integer `done`, `done >= deadline`
        and `ceil(deadline - done)` are both exact against it."""
        self._deadline()
        return self._dl_ceil

    def step(self, done, remaining=None):
        self.now = int(done)
        if not self.enabled:
            return remaining
        if self.next is None:
            self.next = Fraction(done) + self.period
        if type(done) is int:
            step = max(1, self._deadline_ceil() - done)
        else:
            step = max(1, math.ceil(self._deadline() - done))
        if self.batch and ((self.int50_asserted and not self.int50_delivered)
                           or (self.force_asserted
                               and not self.force_delivered)):
            # An interrupt the current IPL blocked is retried at the next
            # step boundary. Unbatched that is a frame away; batched it could
            # be a whole half-buffer, which makes the render late and sends
            # one half twice (measured). Keep retries a frame apart.
            self.period
            step = min(step, self._period_ceil)
        return min(step, remaining) if remaining is not None else step

    def service(self, done):
        self.now = int(done)
        if self.next is None:
            due = False
        elif type(done) is int:
            due = done >= self._deadline_ceil()
        else:
            due = done >= self._deadline()
        if due:
            n = self._due
            self.requests += n
            self._run_minors(self.p.rx_chan, False, n)
            self._run_minors(self.p.tx_chan, True, n)
            self.next += self.period * n
            if self.next <= done:
                self.next = Fraction(done) + self.period
            self._due = None
        self._deliver_vector170()
        self._deliver_vector191()

    def _run_minors(self, channel, capture_tx, n):
        """`n` minor loops of one channel, in bulk when its shape allows.

        The bulk path covers the mk1 shapes: transmit reads a contiguous
        buffer into a fixed register, receive writes a contiguous buffer from
        a fixed register. `n` never crosses a half or major boundary (see
        _frames_to_event), so the copy is one contiguous run. Anything else
        is run a loop at a time.
        """
        if n == 1 or channel not in self.enabled:
            for _ in range(n):
                self._run_minor(channel, capture_tx)
            return
        tcd = self._tcd(channel)
        raw = bytes(self.m.uc.mem_read(tcd, 0x20))
        (source, attr, soff, nbytes, _slast, dest, citer_raw, doff, _dlast,
         biter_raw, csr) = struct.unpack('>IHhIIIHhIHH', raw)
        citer = citer_raw & 0x7FFF
        simple = (not citer_raw & 0x8000 and not biter_raw & 0x8000
                  and attr & 0x7 == 2 and (attr >> 8) & 0x7 == 2
                  and nbytes and not nbytes % 4 and n <= citer)
        if capture_tx:
            simple = simple and soff == 4 and doff == 0
        else:
            simple = simple and soff == 0 and doff == 4
        if not simple:
            for _ in range(n):
                self._run_minor(channel, capture_tx)
            return
        total = nbytes * n
        if capture_tx:
            captured = bytes(self.m.uc.mem_read(source, total))
            source = (source + total) & 0xFFFFFFFF
        else:
            captured = b''
            if self.rx_source is not None and \
                    self._u32(channel, SADDR) == self.p.rx_register:
                supplied = bytes(self.rx_source(total) or b'')
                if supplied:
                    self.m.uc.mem_write(dest, supplied[:total])
                    self.rx_bytes += len(supplied)
            dest = (dest + total) & 0xFFFFFFFF
        citer -= n
        # SADDR, DADDR and CITER in one write of the descriptor just read
        # (nothing runs in between to change the rest of it).
        new = bytearray(raw)
        struct.pack_into('>I', new, SADDR, source & 0xFFFFFFFF)
        struct.pack_into('>I', new, DADDR, dest & 0xFFFFFFFF)
        struct.pack_into('>H', new, CITER, citer & 0xFFFF)
        self.m.uc.mem_write(tcd, bytes(new))
        if captured:
            self.tx_bytes += len(captured)
            self.tx_crc32 = zlib.crc32(captured, self.tx_crc32)
            if self.sink is not None:
                self.sink(captured)
        if citer:
            half = (biter_raw & 0x7FFF) // 2
            if (channel == self.p.tx_chan and csr & 0x0004
                    and citer + n > half >= citer):
                self.half_loops += 1
                self.int50_asserted = True
                self.int50_delivered = False
            return
        self._complete_major(channel, source, dest, biter_raw)

    def _tcd(self, channel):
        return TCD_BASE + channel * 0x20

    def _u32(self, channel, offset):
        return struct.unpack(">I", self.m.uc.mem_read(self._tcd(channel) + offset, 4))[0]

    def _u16(self, channel, offset):
        return struct.unpack(">H", self.m.uc.mem_read(self._tcd(channel) + offset, 2))[0]

    def _w32(self, channel, offset, value):
        self.m.uc.mem_write(
            self._tcd(channel) + offset, struct.pack(">I", value & 0xFFFFFFFF)
        )

    def _w16(self, channel, offset, value):
        self.m.uc.mem_write(
            self._tcd(channel) + offset, struct.pack(">H", value & 0xFFFF)
        )

    def _run_minor(self, channel, capture_tx):
        if channel not in self.enabled:
            return False
        citer_raw = self._u16(channel, CITER)
        biter_raw = self._u16(channel, BITER)
        if citer_raw & 0x8000 or biter_raw & 0x8000:
            raise RuntimeError("Ssi0Dma does not support linked CITER/BITER")
        citer = citer_raw & 0x7FFF
        if not citer:
            return False
        attr = self._u16(channel, ATTR)
        source_size = 1 << (attr & 0x7)
        dest_size = 1 << ((attr >> 8) & 0x7)
        if source_size != 4 or dest_size != 4:
            raise RuntimeError("Ssi0Dma only supports the observed 32-bit transfers")
        nbytes = self._u32(channel, NBYTES)
        if not nbytes or nbytes % source_size:
            raise RuntimeError("invalid SSI eDMA minor-loop byte count")
        source = self._u32(channel, SADDR)
        dest = self._u32(channel, DADDR)
        source_offset = _signed(self._u16(channel, SOFF), 16)
        dest_offset = _signed(self._u16(channel, DOFF), 16)
        # A supplied receive signal, one minor loop's worth.
        supplied = None
        if (not capture_tx and self.rx_source is not None
                and source == self.p.rx_register):
            supplied = bytes(self.rx_source(nbytes) or b'')
            if supplied:
                self.m.uc.mem_write(source, supplied[:source_size])
                self.rx_bytes += len(supplied)
        captured = bytearray()
        for beat in range(nbytes // source_size):
            if capture_tx:
                captured += self.m.uc.mem_read(source, source_size)
            elif supplied:
                # Deliver it. Only done when something is actually supplying
                # the FIFO; with no peer the destination is left untouched.
                chunk = supplied[beat * source_size:(beat + 1) * source_size]
                if len(chunk) < source_size:
                    chunk = (chunk + supplied[:source_size])[:source_size]
                self.m.uc.mem_write(dest, chunk)
            source = (source + source_offset) & 0xFFFFFFFF
            dest = (dest + dest_offset) & 0xFFFFFFFF
        self._w32(channel, SADDR, source)
        self._w32(channel, DADDR, dest)
        citer -= 1
        self._w16(channel, CITER, citer)
        if captured:
            self.tx_bytes += len(captured)
            self.tx_crc32 = zlib.crc32(captured, self.tx_crc32)
            if self.sink is not None:
                self.sink(bytes(captured))
        if citer:
            # INT_HALF: the half-way interrupt a double-buffered descriptor
            # uses instead of scatter-gather. The hardware raises it as the
            # minor loop that crosses BITER/2 completes.
            csr = self._u16(channel, CSR)
            if (channel == self.p.tx_chan and csr & 0x0004
                    and citer == (biter_raw & 0x7FFF) // 2):
                self.half_loops += 1
                self.int50_asserted = True
                self.int50_delivered = False
            return False
        return self._complete_major(channel, source, dest, biter_raw)

    def _complete_major(self, channel, source, dest, biter_raw):
        """Major loop done: SLAST, then DLAST or scatter-gather, then INT."""
        csr = self._u16(channel, CSR)
        source = (source + _signed(self._u32(channel, SLAST), 32)) & 0xFFFFFFFF
        self._w32(channel, SADDR, source)
        self.major_loops[channel] += 1
        if csr & 0x0010:  # E_SG
            pointer = self._u32(channel, DLAST)
            if pointer & 0x1F:
                raise RuntimeError("SSI scatter/gather pointer is not 32-byte aligned")
            descriptor = bytes(self.m.uc.mem_read(pointer, 0x20))
            self.m.uc.mem_write(self._tcd(channel), descriptor)
            self.scatter_gathers[channel] += 1
        else:
            dest = (dest + _signed(self._u32(channel, DLAST), 32)) & 0xFFFFFFFF
            self._w32(channel, DADDR, dest)
            self._w16(channel, CITER, biter_raw)
        if channel == self.p.tx_chan and csr & 0x0002:  # INT_MAJOR
            self.int50_asserted = True
            self.int50_delivered = False
        return True

    def _deliver_vector170(self):
        if not self.int50_asserted or self.int50_delivered:
            return False
        level = interrupt_level(self.m, self.p.tx_vector)
        if level is None:
            return False
        sr = self.m.uc.reg_read(UC_M68K_REG_SR)
        if ((sr >> 8) & 0x07) >= level:
            return False
        if self.m.raise_vector(self.p.tx_vector, level=level):
            self.int50_delivered = True
            self.vector170 += 1
            # The transmit ISR runs above the render's level and ends in the
            # hooked rte, where the render is taken without the IPL dropping
            # below its level. So the render window opens here: what waits at
            # or below the render's level waits until the render returns (or,
            # if this pass forces no render, until this ISR's rte).
            render = interrupt_level(self.m, FORCE_VECTOR, respect_mask=False)
            if render is not None and render <= level:
                self._render_started(render)
            return True
        return False

    def _on_serq(self, uc, access, address, size, value, user_data):
        if size != 1 or value & 0x80:
            return
        channels = self.p.channels if value & 0x40 else (value & 0x3F,)
        self.enabled.update(ch for ch in channels if ch in self.p.channels)
        if self.enabled and self.next is None:
            self.next = Fraction(self.now) + self.period

    def _on_cint(self, uc, access, address, size, value, user_data):
        if size == 1 and (value & 0x40 or (value & 0x3F) == self.p.tx_chan):
            self.int50_asserted = False
            self.int50_delivered = False

    def _on_intfrch1(self, uc, access, address, size, value, user_data):
        current = bytearray(uc.mem_read(INTFRCH1, 4))
        offset = address - INTFRCH1
        current[offset:offset + size] = int(value).to_bytes(size, "big")
        asserted = bool(int.from_bytes(current, "big") & INTFRCH1_SOURCE63)
        if asserted and not self.force_asserted:
            self.force_delivered = False
        self.force_asserted = asserted
        if not asserted:
            self.force_delivered = False

    def _on_force_rte(self, uc, address, size, user_data):
        if not self.force_asserted or self.force_delivered:
            self._render_returned()
            return
        # INTFRCH requests explicitly bypass the INTC mask registers. At this
        # hook the channel-50 ISR is about to restore the interrupted SR; take
        # the pending source as the post-RTE interrupt, via a nested frame that
        # returns to this same RTE after vector 191 clears the force bit.
        level = interrupt_level(self.m, FORCE_VECTOR, respect_mask=False)
        if level is None:
            return
        sp = uc.reg_read(UC_M68K_REG_A7)
        saved_sr = struct.unpack(">H", uc.mem_read(sp + 2, 2))[0]
        if ((saved_sr >> 8) & 0x07) < level and self.m.raise_vector(
            FORCE_VECTOR, level=level
        ):
            self.force_delivered = True
            self.vector191 += 1
            self._render_started(level)

    # The render window. Vector 191 is the audio render: it runs for most of
    # each pass at its own level, and every interrupt that falls due meanwhile
    # at that level or below waits for it -- measured under live audio with a
    # pattern playing, not one such interrupt was taken inside the window.
    # So while it runs, the waiting sources need not re-check the mask every
    # PENDING_STEP (see emu.pit.render_holds): this hook ends the step as the
    # render returns, right before the rte that drops the IPL, and they are
    # served then. Fast stepping only: a counted run has no step to end, so
    # the window is never opened there and nothing changes.
    def _render_started(self, level):
        m = self.m
        if getattr(m, '_fast_stepper_obj', None) is None:
            return
        if getattr(m, 'render_ipl', None) is None:
            # A new window. One already open (the transmit ISR's, now
            # handing over to the render) keeps its waiters.
            m.render_since = self.now
            m.render_waiting = False
        m.render_ipl = level

    def _render_returned(self):
        m = self.m
        if getattr(m, 'render_ipl', None) is None:
            return
        m.render_ipl = None
        if m.render_waiting:
            m.render_waiting = False
            stepper = getattr(m, '_fast_stepper_obj', None)
            if stepper is not None:
                stepper.left = 0

    def _deliver_vector191(self):
        """Retry a software-forced source that the interrupted IPL blocked."""
        if not self.force_asserted or self.force_delivered:
            return False
        level = interrupt_level(self.m, FORCE_VECTOR, respect_mask=False)
        if level is None:
            return False
        sr = self.m.uc.reg_read(UC_M68K_REG_SR)
        if ((sr >> 8) & 0x07) >= level:
            return False
        if self.m.raise_vector(FORCE_VECTOR, level=level):
            self.force_delivered = True
            self.vector191 += 1
            return True
        return False


def install(machine, at, events, request_hz, instr_per_sec, force_rte,
            profile=DIGITAKT2):
    source = Ssi0Dma(
        machine,
        request_hz=request_hz,
        instr_per_sec=instr_per_sec,
        at=at,
        force_rte=force_rte,
        profile=profile,
    )
    events["ssi0_dma"] = source
    return source
