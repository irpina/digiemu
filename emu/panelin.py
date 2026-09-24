"""Front-panel input: buttons and encoders, delivered the way hardware does.

There is no key matrix to model. `tools/mmiotrace.py` measured 60M post-intro
instructions on each build and found zero GPIO, zero DSPI and zero unclaimed
MMIO -- the ColdFire never scans a panel. A separate microcontroller does, and
it talks over UART8: eDMA channel 34 fills a 1024-byte ring and vector 154
hands each byte to the driver's receive callback.

The wire format is a header byte followed by a payload:

    header = (tag << 4) | channel

    tag 0x2   1 byte   buttons -- an 8-bit STATE BITMASK for that channel's
                       eight buttons, not a press/release event. The firmware
                       XORs it against the previous byte for the channel and
                       derives the edges itself, so a caller only has to say
                       what is held right now.
    tag 0x3   1 byte   encoders -- a SIGNED delta, not gray code and not
                       separate inc/dec messages. Channels 0..8 map through a
                       nine-entry table: eight data encoders plus level.
    tag 0x7   8 bytes  a block the console task consumes; not input.

Any other tag is parsed and discarded by the firmware.

The parser is byte-identical in both builds -- only the data addresses move.
That is why every address here comes from `emu/symbols.py` instead of being
written down: `emu/serial.py` hardcoded Digitakt's and so did the wrong thing
on Digitone in silence.
"""
import struct

from unicorn.m68k_const import UC_M68K_REG_PC

# eDMA channel 34's live write pointer. A hardware register, so unlike every
# driver address in this module it really is the same in both builds.
TCD34_DADDR = 0xFC045450

RX_VECTOR = 154
RING_MASK = 0x3FF

TAG_BUTTON = 0x2
TAG_ENCODER = 0x3

# Eight data encoders plus the level encoder. Corroborated independently:
# the firmware's channel->index table has exactly nine valid entries in both
# builds, and runs into unrelated rodata past that.
ENCODERS = 9


def _u32(m, addr):
    return struct.unpack('>I', m.uc.mem_read(addr, 4))[0]


def feed(m, profile, data):
    """Deliver raw bytes as the panel's DMA would. -> new PC.

    Writes into the receive ring, advances the channel's DADDR with the same
    modulo the hardware applies, then raises the RX vector so the firmware's
    own ISR drains it. This is `emu/serial.py`'s mechanism -- proven there by
    the receive callback firing once per byte -- with the ring pointer
    resolved per build rather than hardcoded to Digitakt's.
    """
    base = _u32(m, profile.uart8_ring_ptr)
    daddr = _u32(m, TCD34_DADDR)
    for byte in data:
        m.uc.mem_write(daddr, bytes([byte]))
        daddr = base + (((daddr - base) + 1) & RING_MASK)
    m.uc.mem_write(TCD34_DADDR, struct.pack('>I', daddr))
    m.raise_vector(RX_VECTOR)
    return m.uc.reg_read(UC_M68K_REG_PC)


def encode_buttons(channel, mask):
    """-> the two wire bytes that set one group's button state.

    Split out from `buttons` so a caller holding several queued events can
    concatenate them and deliver the lot with a single `feed`: the firmware's
    ISR drains the whole receive ring, so one raised vector covers every
    message in it. Raising once per event instead would nest exception frames
    for input the ring already holds.
    """
    if not 0 <= channel <= 0x0F:
        raise ValueError('button channel must be 0..15, got %r' % (channel,))
    if not 0 <= mask <= 0xFF:
        raise ValueError('button mask must be a byte, got %r' % (mask,))
    return bytes([(TAG_BUTTON << 4) | channel, mask])


def encode_encoder(channel, delta):
    """-> the two wire bytes that turn one encoder by `delta` detents."""
    if not 0 <= channel < ENCODERS:
        raise ValueError('encoder channel must be 0..%d, got %r'
                         % (ENCODERS - 1, channel))
    if not -128 <= delta <= 127:
        raise ValueError('encoder delta must fit a signed byte, got %r'
                         % (delta,))
    return bytes([(TAG_ENCODER << 4) | channel, delta & 0xFF])


def buttons(m, profile, channel, mask):
    """Set the state of one group of eight buttons. -> new PC.

    `mask` is the whole group's state, bit n meaning button n of `channel` is
    held. The firmware edge-detects against what it last saw, so holding
    button 3 of group 0 and then letting go is `buttons(.., 0, 0x08)` then
    `buttons(.., 0, 0x00)` -- there is no separate release message on the
    wire.
    """
    return feed(m, profile, encode_buttons(channel, mask))


def press(m, profile, channel, bit, held=0):
    """Hold one button down, with `held` (a mask) still held alongside it."""
    if not 0 <= bit <= 7:
        raise ValueError('button bit must be 0..7, got %r' % (bit,))
    return buttons(m, profile, channel, held | (1 << bit))


def release(m, profile, channel, bit, held=0):
    """Let one button up, with `held` (a mask) still held."""
    if not 0 <= bit <= 7:
        raise ValueError('button bit must be 0..7, got %r' % (bit,))
    return buttons(m, profile, channel, held & ~(1 << bit))


def encoder(m, profile, channel, delta):
    """Turn one encoder by `delta` detents; negative is counter-clockwise.

    The firmware accumulates deltas per encoder and clamps what it flushes to
    +/-30, so a value outside a signed byte is refused here rather than
    wrapping silently into the opposite direction.
    """
    return feed(m, profile, encode_encoder(channel, delta))


def state(m, profile):
    """Everything worth looking at when panel input is not landing."""
    base = _u32(m, profile.uart8_ring_ptr)
    daddr = _u32(m, TCD34_DADDR)
    return {
        'ring_base': base,
        'daddr': daddr,
        'produced': daddr - base,
        'consumed': _u32(m, profile.uart8_consume_idx),
        'rx_callback': _u32(m, profile.uart8_rx_callback),
    }


_NAME_CHARS = set(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    'abcdefghijklmnopqrstuvwxyz0123456789 /+-&.:#_')


def _cstr(m, addr, limit=64):
    """-> the NUL-terminated string at `addr`, or None if it is not one.

    An unmapped read is a legitimate answer, not a crash: it means `addr` was
    never a char* at all. Digitakt II's control table ends with a NULL entry,
    but Digitakt (mk1)'s simply stops after its 48th, so walking one past the
    end dereferences whatever data follows the table. Letting UcError escape
    turns "the table ended" into a dead emulator.
    """
    out = bytearray()
    while len(out) < limit:
        try:
            byte = m.uc.mem_read(addr + len(out), 1)[0]
        except Exception:                              # noqa: BLE001
            return None
        if byte == 0:
            break
        out.append(byte)
    if not out:
        return None
    return out.decode('ascii', 'replace')


def control_name(m, profile, code, kind='button'):
    """-> the firmware's own name for a control code, or None past the end.

    Read out of the running image rather than written down here, so it stays
    right on a firmware version this project has never seen and so each
    product describes its own panel. `kind` is 'button' or 'encoder'; they
    are separate code spaces.
    """
    base = (profile.panel_button_names if kind == 'button'
            else profile.panel_encoder_names)
    if base is None or code < 0:
        return None
    ptr = _u32(m, base + 4 * code)
    if ptr in (0, 0xFFFFFFFF):
        return None
    name = _cstr(m, ptr)
    # A "name" full of replacement characters is data being read as a string,
    # i.e. one entry past the end of a table that has no NULL terminator.
    if name is None or any(c not in _NAME_CHARS for c in name):
        return None
    return name


def control_names(m, profile, kind='button', limit=256):
    """-> {code: name} for a whole table, stopping at its terminator.

    Starts at code 0. Each product's table decides what lives there:
    Digitakt II puts an 'UNDEFINED' placeholder at index 0, Digitakt (mk1)
    puts trig 1. Skipping index 0 to avoid the placeholder would drop mk1's
    first real control.

    (The empty-dict failure this once had was _cstr letting a UcError escape
    when walking one entry past a table with no NULL terminator, not the
    starting index.)
    """
    out = {}
    for code in range(0, limit):
        name = control_name(m, profile, code, kind)
        if name is None:
            # The Digitone's encoder table has a NULL at entry 0 (rotation
            # codes start at 1); only a missing entry past it ends the table.
            if code == 0:
                continue
            break
        out[code] = name
    return out


def code_for(channel, bit):
    """-> the control code a (channel, bit) press reports, or None.

    Channels 0..5 are `channel * 8 + bit + 1` on both products, measured with
    tools/panelsweep.py by reading the records the firmware emits. Channel 6
    is not linear and is not the same on the two products, so it returns None
    rather than guessing -- sweep the build in hand, or just read the code out
    of the emitted record.
    """
    if not 0 <= bit < 8:
        raise ValueError('bit must be 0..7, got %r' % (bit,))
    if not 0 <= channel <= 5:
        return None
    return channel * 8 + bit + 1


class Held:
    """Which buttons are currently down, per wire channel.

    The wire carries a whole channel's eight buttons as a single bitmask, so
    pressing a second button in the same group means sending both bits set --
    not a second press message. The modifier chords these devices are built
    around (hold FUNC, tap a page button) only work if something tracks that,
    and this is that something.

    Takes a `device` (see emu/device.py) because which wire position a control
    code sits at is per-product: channel 6 differs between Digitakt and
    Digitone, and Digitakt has no wire position at all for some codes.
    """

    def __init__(self, device):
        self.device = device
        self.masks = {}

    def _apply(self, code, down):
        pos = self.device.wire_for(code)
        if pos is None:
            return None
        channel, bit = pos
        mask = self.masks.get(channel, 0)
        mask = (mask | (1 << bit)) if down else (mask & ~(1 << bit))
        self.masks[channel] = mask
        return channel, mask

    def press(self, code):
        """-> (channel, mask) to send, or None if this product lacks the code."""
        return self._apply(code, True)

    def release(self, code):
        """-> (channel, mask) to send, or None if this product lacks the code."""
        return self._apply(code, False)

    def is_down(self, code):
        pos = self.device.wire_for(code)
        if pos is None:
            return False
        channel, bit = pos
        return bool(self.masks.get(channel, 0) & (1 << bit))

    def down_codes(self):
        """-> every code currently held, ascending."""
        out = []
        for channel, mask in self.masks.items():
            for bit in range(8):
                if mask & (1 << bit):
                    code = self.device.code_at(channel, bit)
                    if code is not None:
                        out.append(code)
        return sorted(out)

    def release_all(self):
        """-> the (channel, mask) messages that let go of everything.

        Worth sending on restart, or whenever the UI loses track of the mouse:
        the firmware believes whatever state it was last told, so a button
        still down in this model is a button still held as far as it knows.
        """
        out = [(channel, 0)
               for channel, mask in sorted(self.masks.items()) if mask]
        self.masks = {}
        return out
