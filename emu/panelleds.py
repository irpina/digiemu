"""mk1 key LEDs, decoded from the ColdFire -> panel MCU stream.

The Digitakt's panel has 44 LEDs in 11 selector groups (LEDS, GROUPS); the
Digitone's the same protocol with 72 in 18 (its SEED entry). The notes below
were made on the Digitakt.

Everything the main CPU tells the panel MCU goes out on UART8 through the one
TX ring, and the eDMA model collects it in `ev['uart_out']`. The format below
was read from both ends: the ColdFire code that builds the messages and the
panel MCU's own receive parser (its firmware is embedded in the MAIN OS image
at 0x4024E94C). A decode of 93 checkpoints matched the firmware's own LED
shadow in RAM, and a full cold boot (41,441 bytes) decoded with nothing left
over.

    1p cc d0..d7   10  OLED 8x8 tile, page p, column cc (not LEDs)
    B8              1  end of OLED frame, sent every frame
    2g ss           2  selectors for LEDs 4g..4g+3: 2 bits each, LED 4g in
                       bits 1:0, picking one of that LED's four slots
    Bs id vv        3  slot s (0..3) of LED id := palette index vv
    B4 id v0..v3    6  all four slots (accepted by the MCU, never sent)
    B5 i r g b      5  palette entry i (0..40) := RGB, 0..31 each
    B6 ..           3  accepted, never sent
    B7 x            2  OLED contrast, not an LED
    0x              1  ignored
    anything else   2

An LED shows palette[slot[led][selected slot]]. Colour is resolved when it is
drawn, not when a message arrives: redefining a selected slot recolours the
LED at once, and a B5 recolours every LED using that entry. Blinking and
flashing are the ColdFire resending selectors, so there is no blink to
implement here -- just redraw.
"""
from __future__ import annotations

LEDS = 44
GROUPS = 11

# A resumed snapshot's stream starts empty, so the state the MCU was last told
# is read back from the firmware's own shadow copies. Literal addresses, used
# only for the release they were read from and only if they look right. A
# bare version is the Digitakt mk1's; other products are keyed (name,
# version), with their own LED count.
#
# The Digitone (mk1) 1.43 addresses were found by porting the Digitakt's
# builders to its image (slot cache 0x400f9518, selector flush 0x400f90fc,
# palette 0x400f937a): its selector flush walks 18 groups, so 72 LEDs, and
# its palette has the same 41 entries.
SEED = {
    '1.53': dict(slot_cache=0x439CFF41,   # byte [(led+1)*4 + slot]; FF = undefined
                 selectors=0x421D1E0C,    # 11 bytes, last selector bytes sent
                 palette=0x4020D900),     # 41 x u32 0x00RRGGBB, last palette sent
    ('Digitone', '1.43'): dict(slot_cache=0x43229D43, selectors=0x419CE71C,
                               palette=0x40241A4C, leds=72, groups=18),
}
PALETTE = 41


def seed_table(version, product=None):
    """-> the SEED entry for a release, or None. A bare version key is the
    Digitakt mk1's, so another product never borrows it."""
    if product is not None:
        where = SEED.get((product, version))
        if where is not None or product != 'Digitakt':
            return where
    return SEED.get(version)


def msg_len(h):
    """Total length of the message whose header byte is `h`, or None."""
    t = h >> 4
    if t == 0x0:
        return 1
    if t == 0x1:
        return 10
    if t != 0xB:
        return 2
    return {0xB0: 3, 0xB1: 3, 0xB2: 3, 0xB3: 3, 0xB4: 6, 0xB5: 5, 0xB6: 3,
            0xB7: 2, 0xB8: 1}.get(h)


class LedState:
    """What the panel MCU has been told, fed incrementally."""

    def __init__(self, leds=LEDS, groups=GROUPS):
        self.leds = leds
        self.groups = groups
        self.slot = [[None] * 4 for _ in range(256)]
        self.sel = [None] * groups
        self.palette = {}
        self.contrast = None
        self.tail = b''
        self.skipped = 0

    def feed(self, data):
        buf, i = self.tail + bytes(data), 0
        while i < len(buf):
            h = buf[i]
            n = msg_len(h)
            if n is None:
                self.skipped += 1
                i += 1
                continue
            if i + n > len(buf):
                break
            m = buf[i:i + n]
            i += n
            if h >> 4 == 0x2 and (h & 0xF) < self.groups:
                self.sel[h & 0xF] = m[1]
            elif 0xB0 <= h <= 0xB3:
                self.slot[m[1]][h & 3] = m[2]
            elif h == 0xB4:
                self.slot[m[1]] = list(m[2:6])
            elif h == 0xB5:
                self.palette[m[1]] = (m[2], m[3], m[4])
            elif h == 0xB7:
                self.contrast = m[1]
        self.tail = buf[i:]
        return self

    def index(self, led):
        """Palette index LED `led` shows, or None if not yet defined."""
        if led >> 2 >= len(self.sel):
            return None
        s = self.sel[led >> 2]
        if s is None:
            return None
        return self.slot[led][(s >> ((led & 3) * 2)) & 3]

    def colours(self):
        """{led: (r, g, b) 0..255} for every LED with a defined colour."""
        out = {}
        for led in range(self.leds):
            rgb = self.palette.get(self.index(led))
            if rgb is not None:
                out[led] = tuple(min(31, v) * 255 // 31 for v in rgb)
        return out


def new_state(version, product=None):
    """-> an empty LedState sized for this product's panel."""
    where = seed_table(version, product) or {}
    return LedState(where.get('leds', LEDS), where.get('groups', GROUPS))


def seed(read, version, product=None):
    """-> an LedState equal to what the MCU was last told, or None.

    `read(addr, n) -> bytes`. None for a firmware without known addresses, or
    when what is there does not look like the shadow copies (a palette entry
    over 31, a slot over the palette size), so a wrong guess draws nothing
    rather than garbage.
    """
    where = seed_table(version, product)
    if where is None:
        return None
    leds, groups = where.get('leds', LEDS), where.get('groups', GROUPS)
    try:
        cache = read(where['slot_cache'], (leds + 1) * 4)
        sel = read(where['selectors'], groups)
        pal = read(where['palette'], PALETTE * 4)
    except Exception:                                   # noqa: BLE001
        return None
    st = LedState(leds, groups)
    for i in range(PALETTE):
        z, r, g, b = pal[i * 4:i * 4 + 4]
        if z or r > 31 or g > 31 or b > 31:
            return None
        st.palette[i] = (r, g, b)
    for led in range(leds):
        for s in range(4):
            v = cache[(led + 1) * 4 + s]
            if v != 0xFF and v >= PALETTE:
                return None
            st.slot[led][s] = None if v == 0xFF else v
    st.sel = list(sel)
    return st
