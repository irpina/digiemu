# pyright: reportMissingImports=false
"""The Model:Cycles' and Model:Samples' board (emu/modelboard.py, emu/i2c.py).

No firmware: the panel is driven the way the firmware's scan drives it (a
frame selects column 0, reads it and the ADC, selects the next, eight times,
then writes LED rows), and the firmware's own decoders are restated here from
the disassembly -- the quadrature table and step rule (0x40059c74 on
Model:Cycles 1.13), the pad velocity (0x400598b8, 0x40059884) -- so the test
says what the firmware would see, not what the model meant to send.
"""
import collections
import struct
import unittest

from emu import i2c, modelboard
from emu.modelboard import ModelPanel

# 0x4010da30: delta per (old state << 2 | new state), state = (lo << 1) | hi.
QUAD = (0, 1, -1, 0, -1, 0, 0, 1, 1, 0, 0, -1, 0, -1, 1, 0)


class FirmwareView:
    """What the firmware's scan and processing make of the panel."""

    def __init__(self, panel):
        self.panel = panel
        self.keys = [0] * 4
        self.state = [0] * 16       # per encoder: last state
        self.acc = [0] * 16
        self.steps = [0] * 16
        self.adc = [0] * 8

    def frame(self):
        self.panel.on_data_write(0x0001)              # column 0
        cols = []
        for n in range(8):
            cols.append(self.panel.on_data_read())
            self.adc[n] = self.panel.adc(self.panel.select)
            self.panel.on_data_write((((n + 1) & 7) << 5) | 1)
        self.keys = cols[:4]
        for e in range(16):
            byte = cols[4 + e // 4]
            lo = (byte >> (2 * (e % 4))) & 1
            hi = (byte >> (2 * (e % 4) + 1)) & 1
            new = (lo << 1) | hi
            if new == self.state[e] & 3:
                continue
            self.state[e] = ((self.state[e] << 2) | new) & 0xF
            total = QUAD[self.state[e]] + self.acc[e]
            if total > 1:
                self.acc[e] = 0
                self.steps[e] += 1
            elif total < -1:
                self.acc[e] = 0
                self.steps[e] -= 1
            else:
                self.acc[e] = total
        return cols

    def run(self, frames):
        for _ in range(frames):
            self.frame()


def velocity(sample):
    """0x400598b8 / 0x40059884: the velocity a pad sample reads as."""
    arm, top, scale = 0x2147AE00, 0x6CCCCD00, -0x1B1E5   # 0xfffe4e1b
    v = sample * 0x10002
    v = max(v, arm)
    if top is not None:
        v = min(v, top)
    k = (scale * ((v - arm) >> 16)) & 0xFFFFFFFF
    k = -struct.unpack('>h', struct.pack('>H', (k >> 16) & 0xFFFF))[0]
    return max(1, min(127, k >> 8))


class KeyTest(unittest.TestCase):
    def test_a_key_reads_on_its_column_and_bit_from_the_next_frame(self):
        p = ModelPanel()
        fw = FirmwareView(p)
        p.key(2, 5, True)
        self.assertEqual(fw.frame()[2], 1 << 5)
        self.assertEqual([c for i, c in enumerate(fw.frame()) if i != 2],
                         [0] * 7)

    def test_a_quick_release_still_holds_for_the_debounce(self):
        p = ModelPanel()
        fw = FirmwareView(p)
        p.key(1, 0, True)
        p.key(1, 0, False)                # asked for at once
        seen = [fw.frame()[1] & 1 for _ in range(10)]
        self.assertEqual(seen[:modelboard.KEY_FRAMES], [1] * modelboard.KEY_FRAMES)
        self.assertEqual(seen[-1], 0)

    def test_release_all(self):
        p = ModelPanel()
        fw = FirmwareView(p)
        p.key(0, 3, True)
        p.key(3, 7, True)
        p.pad(2, 90)
        fw.run(2)
        p.release_all()
        fw.run(modelboard.PAD_FRAMES + 2)
        self.assertEqual(fw.keys, [0] * 4)
        self.assertEqual(fw.adc[:6], [0] * 6)

    def test_out_of_range_is_refused(self):
        p = ModelPanel()
        for bad in ((4, 0), (0, 8), (-1, 0)):
            with self.assertRaises(ValueError):
                p.key(*bad, True)
        with self.assertRaises(ValueError):
            p.pad(6, 100)
        with self.assertRaises(ValueError):
            p.turn(16, 1)


class EncoderTest(unittest.TestCase):
    def test_each_step_is_one_firmware_step_either_way(self):
        p = ModelPanel()
        fw = FirmwareView(p)
        p.turn(5, 3)
        p.turn(12, -2)
        fw.run(20)
        self.assertEqual(fw.steps[5], 3)
        self.assertEqual(fw.steps[12], -2)
        self.assertEqual([s for e, s in enumerate(fw.steps) if e not in (5, 12)],
                         [0] * 14)

    def test_turning_back_and_forth(self):
        p = ModelPanel()
        fw = FirmwareView(p)
        for steps in (4, -1, -6, 3):
            p.turn(0, steps)
            fw.run(20)
        self.assertEqual(fw.steps[0], 0)

    def test_encoder_bits_live_in_columns_four_to_seven(self):
        p = ModelPanel()
        fw = FirmwareView(p)
        p.turn(15, 1)                     # column 7, bits 6 and 7
        cols = fw.frame()
        self.assertEqual(cols[:7], [0] * 7)
        self.assertEqual(cols[7] & 0x3F, 0)
        self.assertNotEqual(cols[7], 0)


class PadTest(unittest.TestCase):
    def test_velocity_round_trips_through_the_firmware_formula(self):
        for v in (1, 2, 30, 64, 100, 126, 127):
            got = velocity(modelboard.pad_sample(v))
            self.assertLessEqual(abs(got - v), 1, v)

    def test_a_pad_is_on_its_adc_channel_until_held_long_enough(self):
        p = ModelPanel()
        fw = FirmwareView(p)
        p.pad(4, 100)
        p.pad(4, 0)
        fw.frame()
        self.assertGreater(fw.adc[4], modelboard.PAD_ARM)
        self.assertEqual([a for i, a in enumerate(fw.adc) if i != 4], [0] * 7)
        held = 1
        while fw.adc[4]:
            fw.frame()
            held += 1
        # The firmware takes no release before 30 frames down.
        self.assertGreater(held, 30)


class LedTest(unittest.TestCase):
    def test_rows_are_active_low_and_ids_are_row_times_eight_plus_bit(self):
        p = ModelPanel()
        self.assertEqual(p.lit(), set())
        version = p.led_version
        p.on_data_write((0xFE << 8) | (0 << 5))      # row 0, bit 0 low
        p.on_data_write((0x7F << 8) | (6 << 5))      # row 6, bit 7 low
        self.assertEqual(p.lit(), {0, 55})
        self.assertGreater(p.led_version, version)
        version = p.led_version
        p.on_data_write((0x7F << 8) | (6 << 5))      # unchanged
        self.assertEqual(p.led_version, version)

    def test_a_select_is_not_an_led_row(self):
        p = ModelPanel()
        p.on_data_write((3 << 5) | 1)
        self.assertEqual((p.select, p.lit()), (3, set()))


class FakeUc:
    """Enough of a Unicorn for install(): memory, and hooks to call."""

    def __init__(self):
        self.mem = collections.defaultdict(int)
        self.hooks = []

    def hook_add(self, kind, fn, begin=1, end=0):
        self.hooks.append((kind, fn, begin, end))

    def mem_write(self, addr, data):
        for i, b in enumerate(bytes(data)):
            self.mem[addr + i] = b

    def mem_read(self, addr, n):
        return bytearray(self.mem[addr + i] for i in range(n))

    def access(self, kind, addr, size=1, value=0):
        for k, fn, begin, end in self.hooks:
            if k == kind and begin <= addr <= end:
                fn(self, k, addr, size, value, None)
        if kind == WRITE:
            self.mem_write(addr, value.to_bytes(size, 'big'))
        return int.from_bytes(self.mem_read(addr, size), 'big')


class FakeMachine:
    def __init__(self):
        self.uc = FakeUc()

    def ensure(self, addr):
        pass


from unicorn import UC_HOOK_MEM_READ as READ, UC_HOOK_MEM_WRITE as WRITE  # noqa: E402


class InstallTest(unittest.TestCase):
    def test_nothing_is_installed_without_the_models_scan_handler(self):
        m = FakeMachine()
        prof = type('P', (), {'model_scan_start': None})()
        self.assertIsNone(modelboard.install(m, {}, prof))
        self.assertEqual(m.uc.hooks, [])

    def test_the_board_answers_on_the_bus(self):
        m = FakeMachine()
        ev = {}
        prof = type('P', (), {'model_scan_start': 0x40059cd0,
                              'model_codec_sem': 0x404e9ad4})()
        board = modelboard.install(m, ev, prof)
        self.assertIs(ev['modelboard'], board)
        self.assertEqual(board.codec_sem, 0x404e9ad4)
        self.assertIn(board.forced, m.async_sources)
        uc = m.uc
        board.panel.key(0, 2, True)
        board.panel.pad(0, 127)
        uc.access(WRITE, modelboard.PANEL_DATA, 2, 0x0001)       # column 0
        self.assertEqual(uc.access(READ, modelboard.PANEL_DATA), 1 << 2)
        self.assertGreater(uc.access(READ, modelboard.ADC_RESULT, 2),
                           modelboard.PAD_ARM)
        uc.access(WRITE, modelboard.PANEL_DATA, 2, (0xEF << 8) | (2 << 5))
        self.assertEqual(board.panel.lit(), {20})
        # PIT1's PIF reads as set, whatever was written.
        uc.access(WRITE, modelboard.PCSR1, 2, 0x0033)
        self.assertEqual(uc.access(READ, modelboard.PCSR1, 2) & 4, 4)

    def test_checkpoint_round_trip(self):
        m = FakeMachine()
        prof = type('P', (), {'model_scan_start': 1, 'model_codec_sem': None})()
        board = modelboard.install(m, None, prof)
        board.panel.key(3, 1, True)
        board.panel.on_data_write(0x0001)
        board.panel.on_data_read()
        board.panel.on_data_write((0x00 << 8) | (5 << 5))
        board.bus.devices[modelboard.CODEC_ADDRESS].regs[1] = 0x17
        state = board.checkpoint_state()
        self.assertEqual((state['type'], state['version']), ('ModelBoard', 1))
        other = modelboard.install(FakeMachine(), None, prof)
        other.restore_checkpoint_state(state)
        self.assertEqual(other.panel.keys, board.panel.keys)
        self.assertEqual(other.panel.lit(), board.panel.lit())
        self.assertEqual(other.bus.devices[modelboard.CODEC_ADDRESS].regs[1],
                         0x17)
        with self.assertRaises(RuntimeError):
            other.restore_checkpoint_state(dict(state, version=2))


class I2cTest(unittest.TestCase):
    """The codec driver's sequences (Model:Cycles 1.13: write 0x400443ba,
    read 0x40044498), byte for byte, through the controller's registers."""

    def setUp(self):
        self.codec = i2c.RegisterFile()
        self.bus = i2c.I2cBus(devices={0x1A: self.codec})
        self.cr = 0
        self.cr_write(i2c.IEN | i2c.IIEN)

    def cr_write(self, value):
        self.bus.write_cr(value, self.cr)
        self.cr = value

    def wait_iif(self):
        self.assertTrue(self.bus.status() & i2c.IIF)
        self.bus.write_sr(0)

    def start(self):
        while self.bus.status() & i2c.IBB:
            self.fail('bus busy before START')
        self.cr_write(self.cr | i2c.MTX)
        self.cr_write(self.cr | i2c.MSTA)
        self.assertTrue(self.bus.status() & i2c.IBB)

    def write(self, reg, value):
        self.start()
        for byte in (0x34, reg, value):
            self.bus.write_dr(byte)
            self.assertFalse(self.bus.status() & i2c.RXAK)
            self.wait_iif()
        self.cr_write(self.cr & ~i2c.MSTA)

    def read(self, reg):
        self.start()
        for byte in (0x34, reg):
            self.bus.write_dr(byte)
            self.wait_iif()
        self.cr_write(self.cr & ~i2c.MSTA)
        self.assertFalse(self.bus.status() & i2c.IBB)
        self.cr_write(self.cr | i2c.MSTA)
        self.bus.write_dr(0x35)
        self.wait_iif()
        self.cr_write(self.cr & ~i2c.MTX)
        self.cr_write(self.cr | i2c.TXAK)
        self.bus.read_dr()                         # the dummy read
        self.wait_iif()
        self.cr_write(self.cr & ~i2c.MSTA)
        return self.bus.read_dr()

    def test_the_health_check_reads_back_what_set_up_wrote(self):
        self.write(0x2B, 0x50)
        self.write(0x01, 0x17)
        self.assertEqual(self.read(0x01), 0x17)
        self.assertEqual(self.read(0x2B), 0x50)
        self.assertEqual(self.codec.regs[0x2B], 0x50)

    def test_nothing_answers_another_address(self):
        self.start()
        self.bus.write_dr(0x40 << 1)
        self.assertTrue(self.bus.status() & i2c.RXAK)

    def test_a_resumed_run_does_not_need_this_models_copy_of_i2cr(self):
        # The firmware's read-modify-write of I2CR reads guest memory; this
        # model takes the old value from there too, not from its own copy.
        bus = i2c.I2cBus(devices={0x1A: i2c.RegisterFile()})
        bus.write_cr(i2c.IEN | i2c.MTX | i2c.MSTA, i2c.IEN | i2c.MTX)
        self.assertTrue(bus.status() & i2c.IBB)
        bus.write_dr(0x34)
        self.assertFalse(bus.status() & i2c.RXAK)


class SessionInputTest(unittest.TestCase):
    """emu/session.py (Check firmware's runs) drives a Model's keys, pads and
    encoders through its panel, by the device file's positions."""

    def test_press_release_and_turn(self):
        import types
        from emu import session
        calls = []
        fake = types.SimpleNamespace(
            model_panel=types.SimpleNamespace(
                key=lambda c, b, d: calls.append(('key', c, b, d)),
                pad=lambda ch, v: calls.append(('pad', ch, v)),
                turn=lambda e, n: calls.append(('turn', e, n))),
            device=types.SimpleNamespace(
                name='Model', pads={33: 5}, encoder_counts=1,
                wire_for=lambda code: {10: (0, 7)}.get(code),
                encoder_channel=lambda code: code - 1),
            inputs=[], ms=0.0, PAD_VELOCITY=session.Session.PAD_VELOCITY)
        fake._model_key = lambda code, down: session.Session._model_key(
            fake, code, down)
        session.Session.press(fake, 10)
        session.Session.release(fake, 10)
        session.Session.press(fake, 33)
        session.Session.release(fake, 33)
        fake.run_ms = lambda ms: None
        session.Session.turn(fake, 13, -3)
        self.assertEqual(calls, [('key', 0, 7, True), ('key', 0, 7, False),
                                 ('pad', 5, session.Session.PAD_VELOCITY),
                                 ('pad', 5, 0), ('turn', 12, -3)])
        self.assertEqual(len(fake.inputs), 5)
        with self.assertRaises(session.SessionError):
            session.Session.press(fake, 99)


class SsiProfileTest(unittest.TestCase):
    def test_the_models_transmit_only(self):
        from emu import ssi
        prof = ssi.PROFILES['models']
        self.assertEqual(prof.channels, (50,))
        self.assertIsNone(prof.rx_chan)
        self.assertEqual(prof.tx_register, 0xFC0BC000)
        self.assertEqual(set(prof.legacy), {50})
        self.assertEqual(ssi.PROFILES['digitakt-mk1'].channels, (52, 54))


if __name__ == '__main__':
    unittest.main()
