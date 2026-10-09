"""The Elektron Model:Cycles and Model:Samples: what their board adds.

Both run the Digitakt mk1's processor (an MCF54418 with 128 MB of DDR), RTOS
and application framework, and boot under the same machinery. Four things on
their board are not on the Digitakt's, and this module models them:

1. The front panel is scanned by the main CPU, not by a panel MCU on UART8.
   A FlexBus device at 0x8C000000 multiplexes the keys, encoders and LEDs,
   and the ADC reads the six pads. PIT3 (vector 208) runs the scan as a chain
   of handlers that re-point their own vector (Model:Cycles 1.13):

     0x40059cd0  a frame starts: column 0 selected, buffer pointers reset.
     0x40059d2c  x8: read the byte at 0x8C000002 (column N of the matrix)
                 and an ADC sample (pad N), then select column N + 1 by
                 writing (N + 1) << 5 | 1 there. After the eighth it forces
                 INTC1 source 2 (INTFRCL1 bit 2), the processing interrupt,
                 vector 130 (0x40059e64).
     0x40059db8  x3: drive one of seven LED rows: 0xFFFF to 0x8C000000, then
                 (row bits << 8) | row << 5 to 0x8C000002, active low.

   Columns 0..3 are keys, a set bit pressed: 31 of their 32 bits are the 16
   trigs and 15 function keys, and the last is the power button, which the
   key callback treats apart. Columns 4..7 are 16 encoders, two bits each
   (encoder e is bits 2(e%4) and 2(e%4)+1 of column 4 + e//4), decoded in
   quadrature by the firmware: a step is two Gray-code transitions. The pads
   are ADC channels 0..5; one arms above about 0x2148 in the 16-bit sample,
   reports its velocity from the next sample, and releases below about
   0x199A once it has been down for 30 frames.

   ModelPanel is that device: what each column and ADC channel reads, from
   the keys, encoder phases and pad pressures a window asks for, and the LED
   rows the firmware writes. It counts scan frames (a read of column 0 starts
   one) and applies input at frame boundaries, holding a press, a pad or an
   encoder phase for as many frames as the firmware needs to see it, however
   quickly a release was asked for.

2. The scan's processing interrupt is software-forced on INTC1. emu/intfrc.py
   delivers INTC0's; here the same class watches INTC1 source 2.

3. The audio codec is set up over I2C0 (emu/i2c.py), a register file at
   address 0x1A. The firmware's codec task reads its register 1 back on every
   pass and sets the codec up again unless it holds what set-up wrote. That
   task waits on a semaphore a software-timer callback posts through a
   primitive emu/semscan.py does not know, so it is never faked here.

4. The busy-wait delay routine (0x4008f46c on Model:Cycles 1.13) counts PIT1
   wraps by polling PCSR1's PIF. PIT1 is not otherwise modelled, so PIF reads
   as set and every delay is instant, as emu/dsplink.py does for the
   Digitone's DSP.

Installed only on an image where emu/symbols.py finds `model_scan_start`, so
the Digitakt's and Digitone's runs and checkpoint manifests are unchanged.
"""
import struct

from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE

from emu import i2c, intfrc

PANEL = 0x8C000000
PANEL_DATA = PANEL + 0x02        # column read; select / LED row write
ADC_RESULT = 0xFC094012          # the ADC result the scan reads per column
PCSR1 = 0xFC084000
INTC1, INTC1_VECTOR, SCAN_SOURCE = 0xFC04C000, 128, 2
CODEC_ADDRESS = 0x1A

COLUMNS = 8
KEY_COLUMNS = 4
ENCODERS = 16
PADS = 6
LED_ROWS = 7
LEDS = LED_ROWS * 8

# Frames an input state is held for before the next change to it is applied.
# The key debounce settles in three; a pad must be down for 30 before the
# firmware accepts its release. One frame is 12 PIT3 periods, about 2 ms.
KEY_FRAMES = 4
PAD_FRAMES = 40
ENCODER_FRAMES = 1              # per Gray-code transition

# Pad pressure, as the 16-bit ADC sample: above PAD_ARM arms a pad, and the
# firmware's velocity is (sample - PAD_ARM) * 1.694 / 256, so a velocity v
# needs PAD_ARM + v * PAD_PER_VELOCITY. Measured on Model:Cycles 1.13 from the
# pad callback's constants (0x2147ae00 arm, 0x199999a0 release, 0x6ccccd00
# top, scale 0xfffe4e1b, all over the sample * 0x10002): velocity 127 is
# sample 27824, under the top of 0x6ccc.
PAD_ARM = 0x2148
PAD_PER_VELOCITY = 152
PAD_MAX = 0x7FF8

# Quadrature: the phase sequence a clockwise step walks, as (bit lo, bit hi)
# of the encoder's pair, read by the firmware as state (lo << 1) | hi.
_CW = (0b00, 0b10, 0b11, 0b01)


def pad_sample(velocity):
    """-> the ADC sample that reads as `velocity` (1..127) on a pad."""
    v = max(1, min(127, int(velocity)))
    return min(PAD_MAX, PAD_ARM + v * PAD_PER_VELOCITY)


class ModelPanel:
    """The scan-multiplexed front panel at 0x8C000000, plus the pad ADC."""

    def __init__(self):
        self.select = 0
        self.frames = 0
        self.keys = [0] * KEY_COLUMNS          # what the matrix reads now
        self.phase = [0] * ENCODERS            # index into _CW
        self.pads = [0] * PADS                 # ADC samples
        self.led_rows = [0xFF] * 8             # as written: active low
        self.led_version = 0
        # Input waiting for the frame clock: per key (column, bit) and pad,
        # a queue of wanted states; per encoder, transitions to walk.
        self._key_want = {}
        self._key_since = {}
        self._pad_want = {}
        self._pad_since = {}
        self._turns = [0] * ENCODERS
        self._turn_since = [0] * ENCODERS

    # -- what a window asks for ----------------------------------------------
    def key(self, column, bit, down):
        if not (0 <= column < KEY_COLUMNS and 0 <= bit < 8):
            raise ValueError('no key at column %r bit %r' % (column, bit))
        self._key_want.setdefault((column, bit), []).append(bool(down))

    def pad(self, index, velocity):
        """Press pad `index` (0..5, as ADC channel) at `velocity`; 0 lifts."""
        if not 0 <= index < PADS:
            raise ValueError('no pad %r' % (index,))
        sample = pad_sample(velocity) if velocity else 0
        self._pad_want.setdefault(index, []).append(sample)

    def turn(self, encoder, steps):
        """Turn `encoder` (0..15) by `steps` firmware steps; negative is
        counter-clockwise."""
        if not 0 <= encoder < ENCODERS:
            raise ValueError('no encoder %r' % (encoder,))
        self._turns[encoder] += 2 * int(steps)

    def release_all(self):
        for column in range(KEY_COLUMNS):
            for bit in range(8):
                if self.keys[column] >> bit & 1 or (column, bit) \
                        in self._key_want:
                    self.key(column, bit, False)
        for index in range(PADS):
            if self.pads[index] or index in self._pad_want:
                self.pad(index, 0)

    def lit(self):
        """-> the set of LED ids lit (row * 8 + bit), from the rows as last
        driven."""
        return {row * 8 + bit for row in range(LED_ROWS) for bit in range(8)
                if not self.led_rows[row] >> bit & 1}

    # -- the frame clock ---------------------------------------------------------
    def _frame(self):
        """A frame starts: apply whatever input has waited long enough."""
        self.frames += 1
        now = self.frames
        for pos in list(self._key_want):
            if now - self._key_since.get(pos, -KEY_FRAMES) < KEY_FRAMES:
                continue
            want = self._key_want[pos].pop(0)
            if not self._key_want[pos]:
                del self._key_want[pos]
            column, bit = pos
            mask = 1 << bit
            self.keys[column] = (self.keys[column] | mask) if want \
                else (self.keys[column] & ~mask)
            self._key_since[pos] = now
        for index in list(self._pad_want):
            if now - self._pad_since.get(index, -PAD_FRAMES) < PAD_FRAMES:
                continue
            self.pads[index] = self._pad_want[index].pop(0)
            if not self._pad_want[index]:
                del self._pad_want[index]
            self._pad_since[index] = now
        for e in range(ENCODERS):
            if not self._turns[e] \
                    or now - self._turn_since[e] < ENCODER_FRAMES:
                continue
            step = 1 if self._turns[e] > 0 else -1
            self.phase[e] = (self.phase[e] + step) % 4
            self._turns[e] -= step
            self._turn_since[e] = now

    def column(self, n):
        """-> the byte column `n` reads."""
        if n < KEY_COLUMNS:
            return self.keys[n]
        byte, base = 0, (n - KEY_COLUMNS) * 4
        for k in range(4):
            byte |= _CW[self.phase[base + k]] << (2 * k)
        return byte

    def adc(self, channel):
        return self.pads[channel] if channel < PADS else 0

    # -- the bus -------------------------------------------------------------------
    def on_data_write(self, value):
        """A 16-bit write to 0x8C000002: a column select (bit 0 set) or an
        LED row (bit 0 clear, the row's bits in the high byte)."""
        value &= 0xFFFF
        row = (value >> 5) & 7
        if value & 1:
            self.select = row
        elif row < LED_ROWS:
            bits = (value >> 8) & 0xFF
            if self.led_rows[row] != bits:
                self.led_rows[row] = bits
                self.led_version += 1

    def on_data_read(self):
        if self.select == 0:
            self._frame()
        return self.column(self.select)

    def install(self, m):
        m.ensure(PANEL)
        m.ensure(ADC_RESULT)

        def on_read(uc, typ, addr, size, val, data):
            uc.mem_write(PANEL_DATA, bytes([self.on_data_read()]))

        def on_write(uc, typ, addr, size, val, data):
            if addr == PANEL_DATA and size >= 2:
                self.on_data_write(val >> (8 * (size - 2)))
            elif addr == PANEL_DATA:
                self.on_data_write((val & 0xFF) << 8)

        def on_adc(uc, typ, addr, size, val, data):
            uc.mem_write(ADC_RESULT, struct.pack('>H', self.adc(self.select)))
        # Added after emu/dsp.py's ready-line hook on the same address
        # (longrun.build installs that first), so this one's value is the
        # one the read sees.
        m.uc.hook_add(UC_HOOK_MEM_READ, on_read, begin=PANEL_DATA,
                      end=PANEL_DATA)
        m.uc.hook_add(UC_HOOK_MEM_WRITE, on_write, begin=PANEL_DATA,
                      end=PANEL_DATA + 1)
        m.uc.hook_add(UC_HOOK_MEM_READ, on_adc, begin=ADC_RESULT,
                      end=ADC_RESULT + 1)
        return self

    # -- checkpoint ----------------------------------------------------------------
    def checkpoint_state(self):
        return {'select': self.select, 'frames': self.frames,
                'keys': list(self.keys), 'phase': list(self.phase),
                'pads': list(self.pads), 'led_rows': list(self.led_rows)}

    def restore_checkpoint_state(self, state):
        self.select = int(state['select'])
        self.frames = int(state['frames'])
        self.keys = [int(v) for v in state['keys']]
        self.phase = [int(v) for v in state['phase']]
        self.pads = [int(v) for v in state['pads']]
        self.led_rows = [int(v) for v in state['led_rows']]
        self.led_version += 1


class Board:
    """Everything this module installs, as one checkpoint component."""

    def __init__(self, panel, bus, forced, codec_sem):
        self.panel = panel
        self.bus = bus
        self.forced = forced
        self.codec_sem = codec_sem

    def checkpoint_state(self):
        return {'type': 'ModelBoard', 'version': 1,
                'panel': self.panel.checkpoint_state(),
                'i2c': self.bus.checkpoint_state(),
                'intfrc1': self.forced.checkpoint_state()}

    def restore_checkpoint_state(self, state):
        if state.get('type') != 'ModelBoard' or state.get('version') != 1:
            raise RuntimeError('unsupported ModelBoard checkpoint state')
        self.panel.restore_checkpoint_state(state['panel'])
        self.bus.restore_checkpoint_state(state['i2c'])
        self.forced.restore_checkpoint_state(state['intfrc1'])


def _pit1_delay(m):
    m.ensure(PCSR1)

    def pif(uc, typ, addr, size, val, data):
        cur = struct.unpack('>H', bytes(uc.mem_read(PCSR1, 2)))[0]
        uc.mem_write(PCSR1, struct.pack('>H', cur | 0x0004))
    m.uc.hook_add(UC_HOOK_MEM_READ, pif, begin=PCSR1, end=PCSR1 + 1)


def is_model(profile):
    return getattr(profile, 'model_scan_start', None) is not None


def install(m, ev, profile):
    """Model the Models' board on `m` if `profile` is one's. -> the Board, also
    ev['modelboard'], or None (then nothing is installed). The caller adds it
    to its checkpoint components and manifest, and never fakes
    `board.codec_sem`."""
    if not is_model(profile):
        return None
    bus = i2c.I2cBus(devices={CODEC_ADDRESS: i2c.RegisterFile()}).install(m)
    _pit1_delay(m)
    panel = ModelPanel().install(m)
    forced = intfrc.ForcedInterrupts(m, sources=(SCAN_SOURCE,), base=INTC1,
                                     first_vector=INTC1_VECTOR)
    regs = bytes(m.uc.mem_read(INTC1 + intfrc.INTFRCH, 8))
    forced.asserted = forced._forced(regs)
    m.async_sources = getattr(m, 'async_sources', ()) + (forced,)
    board = Board(panel, bus, forced, getattr(profile, 'model_codec_sem', None))
    if ev is not None:
        ev['modelboard'] = board
    return board
