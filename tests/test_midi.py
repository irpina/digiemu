"""emu/midi.py: MIDI in through UART9's eDMA channel 36 ring and vector 156.

No firmware: channel 36's descriptor is set up as the mk1 builds leave it
(ATTR 0x0078, a 32 KB destination ring), vector 156 points at an `rte`, and
INTC1 source 28 is armed at level 4, as on both builds.
"""
import struct
import unittest

from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC, UC_M68K_REG_SR

from emu import midi, pit
from emu.harness import VBR, Machine

HANDLER = 0x40100000
STACK = 0x40200000
RING = 0x4BC00000
INTC1 = 0xFC04C000


def _machine(level=4):
    m = Machine()
    for addr in (VBR, HANDLER, STACK - 0x100, INTC1, midi.TCD36, RING):
        m.ensure(addr)
    m.uc.mem_write(VBR + midi.RX_VECTOR * 4, struct.pack('>I', HANDLER))
    m.uc.mem_write(HANDLER, b'\x4e\x73')                     # rte
    m.uc.mem_write(INTC1 + pit.ICR_BASE + 28, bytes([level]))
    m.uc.mem_write(midi.TCD36 + midi.TCD_ATTR, struct.pack('>H', 0x0078))
    m.uc.mem_write(midi.TCD36 + midi.TCD_DADDR, struct.pack('>I', RING))
    m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
    m.uc.reg_write(UC_M68K_REG_A7, STACK)
    return m


def _daddr(m):
    return struct.unpack('>I', bytes(m.uc.mem_read(
        midi.TCD36 + midi.TCD_DADDR, 4)))[0]


class MidiInTest(unittest.TestCase):
    def test_bytes_land_in_the_ring_and_raise_the_vector(self):
        m = _machine()
        mi = midi.MidiIn()
        mi.put(b'\x90\x3c\x64')
        self.assertTrue(mi.deliver(m))
        self.assertEqual(bytes(m.uc.mem_read(RING, 3)), b'\x90\x3c\x64')
        self.assertEqual(_daddr(m), RING + 3)
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), HANDLER)
        self.assertEqual((mi.received, mi.delivered, mi.raised), (3, 3, 1))
        self.assertFalse(mi.deliver(m))                     # nothing pending

    def test_the_ring_wraps_at_its_modulo(self):
        m = _machine()
        m.uc.mem_write(midi.TCD36 + midi.TCD_DADDR,
                       struct.pack('>I', RING + 0x7FFE))
        midi.feed(m, b'\xf8\xfa\xfc')
        self.assertEqual(bytes(m.uc.mem_read(RING + 0x7FFE, 2)), b'\xf8\xfa')
        self.assertEqual(bytes(m.uc.mem_read(RING, 1)), b'\xfc')
        self.assertEqual(_daddr(m), RING + 1)

    def test_held_back_while_the_cpu_masks_it(self):
        m = _machine()
        m.uc.reg_write(UC_M68K_REG_SR, 0x2400)              # IPL 4
        mi = midi.MidiIn()
        mi.put(b'\xfa')
        self.assertFalse(mi.deliver(m))
        self.assertEqual((mi.delivered, mi.deferred), (0, 1))
        self.assertEqual(_daddr(m), RING)                   # nothing written
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertTrue(mi.deliver(m))
        self.assertEqual(_daddr(m), RING + 1)

    def test_not_armed_is_held_back(self):
        m = _machine(level=0)
        mi = midi.MidiIn()
        mi.put(b'\xfa')
        self.assertFalse(mi.deliver(m))
        self.assertEqual(mi.deferred, 1)

    def test_a_large_burst_goes_in_slices(self):
        m = _machine()
        mi = midi.MidiIn()
        mi.put(bytes(midi.MAX_FEED + 10))
        mi.deliver(m)
        self.assertEqual(mi.delivered, midi.MAX_FEED)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        mi.deliver(m)
        self.assertEqual(mi.delivered, midi.MAX_FEED + 10)


class ParserTest(unittest.TestCase):
    def test_running_status_realtime_and_sysex(self):
        p = midi.Parser()
        stream = bytes([0x90, 60, 100, 64, 90,        # running status
                        0x80, 60, 0xF8, 0,            # clock mid-message
                        0xF0, 0x00, 0x20, 0x3C, 0xF7,  # SysEx
                        0xC3, 5, 0xFA, 0xF4, 1])      # undefined F4 dropped
        self.assertEqual(p.feed(stream), [
            [0x90, 60, 100], [0x90, 64, 90], [0xF8], [0x80, 60, 0],
            [0xF0, 0x00, 0x20, 0x3C, 0xF7], [0xC3, 5], [0xFA]])

    def test_split_across_feeds(self):
        p = midi.Parser()
        self.assertEqual(p.feed(b'\xb0\x40'), [])
        self.assertEqual(p.feed(b'\x7f'), [[0xB0, 0x40, 0x7F]])


class HostNamesTest(unittest.TestCase):
    def test_port_name_drops_alsa_numbers(self):
        self.assertEqual(
            midi.port_name('CASIO USB-MIDI:CASIO USB-MIDI MIDI 1 36:0'),
            'CASIO USB-MIDI:CASIO USB-MIDI MIDI 1')
        self.assertEqual(midi.port_name('IAC Driver Bus 1'),
                         'IAC Driver Bus 1')

    def test_port_name_drops_the_winmm_index(self):
        # As RtMidi lists them on Windows: the name, a space, the index.
        self.assertEqual(midi.port_name('Elektron Digitakt 2', winmm=True),
                         'Elektron Digitakt')
        self.assertEqual(midi.port_name('FM-1 Midi 1', winmm=True), 'FM-1 Midi')
        self.assertEqual(midi.port_name('loopMIDI Port 1 3', winmm=True),
                         'loopMIDI Port 1')           # only the index goes

    def test_settings_live_in_the_firmware_folder(self):
        import os
        import tempfile
        from emu.dtpanel import _midi_settings_path
        with tempfile.TemporaryDirectory() as root:
            snaps = os.path.join(root, 'fw', 'snapshots', 'Digitone_OS1.43')
            os.makedirs(snaps)
            snap = os.path.join(snaps, 'resume.snap')
            self.assertEqual(_midi_settings_path(snap),
                             os.path.join(snaps, 'midi.json'))
            open(os.path.join(root, 'fw', 'firmware.json'), 'w').close()
            self.assertEqual(_midi_settings_path(snap),
                             os.path.join(root, 'fw', 'midi.json'))
        self.assertIsNone(_midi_settings_path(None))


TX_RING = 0x4BC09000


def _tx_machine():
    m = _machine()
    for addr in (midi.TCD37, midi.UART9, TX_RING):
        m.ensure(addr)
    for vec in (midi.TX_VECTOR, midi.TX_DMA_VECTOR):
        m.uc.mem_write(VBR + vec * 4, struct.pack('>I', HANDLER))
    m.uc.mem_write(INTC1 + pit.ICR_BASE + 53, bytes([4]))   # UART9
    m.uc.mem_write(INTC1 + pit.ICR_BASE + 29, bytes([4]))   # eDMA 37
    return m


def serq(m, value):
    """move.b #value,SERQ as guest code: the driver's way of starting a
    channel (the mk1 builds never use SSRT for channel 37)."""
    code = HANDLER + 0x100
    m.uc.mem_write(code, b'\x13\xfc' + struct.pack('>HI', value,
                                                  midi.EDMA_SERQ))
    m.uc.emu_start(code, code + 8)
class MidiOutTest(unittest.TestCase):
    def test_a_direct_write_goes_out(self):
        m = _tx_machine()
        got = []
        mo = midi.MidiOut(got.append)
        mo.install(m)
        # move.b #$f8,UTB9 as guest code: the CPU's own store.
        m.uc.mem_write(HANDLER + 0x100, b'\x13\xfc\x00\xf8'
                       + struct.pack('>I', midi.UTB9))
        m.uc.emu_start(HANDLER + 0x100, HANDLER + 0x108)
        self.assertEqual(got, [b'\xf8'])
        self.assertEqual((mo.sent, mo.direct), (1, 1))

    def test_a_dma_run_goes_out_and_completes(self):
        m = _tx_machine()
        got = []
        mo = midi.MidiOut(got.append)
        mo.install(m)
        # A 4 KB ring (SMOD 12) whose three bytes wrap past its end.
        m.uc.mem_write(TX_RING + 0xFFE, b'\x90\x3c')
        m.uc.mem_write(TX_RING, b'\x64')
        m.uc.mem_write(midi.TCD37, struct.pack('>IHhI', TX_RING + 0xFFE,
                                                12 << 11, 1, 1))
        m.uc.mem_write(midi.TCD37 + midi.TCD_CITER, struct.pack('>H', 3))
        serq(m, midi.TX_CHANNEL)
        self.assertEqual(got, [b'\x90\x3c\x64'])
        self.assertTrue(mo.deliver(m))                     # vector 157
        self.assertEqual(m.uc.reg_read(UC_M68K_REG_PC), HANDLER)
        self.assertEqual(mo.raised[midi.TX_DMA_VECTOR], 1)

    def test_the_uart_interrupt_follows_the_drivers_mask(self):
        m = _tx_machine()
        mo = midi.MidiOut(lambda d: None)
        mo.install(m)
        code = HANDLER + 0x100

        def store(addr, value):             # move.b #value,addr.l as guest
            m.uc.mem_write(code, b'\x13\xfc' + struct.pack('>HI', value, addr))
            m.uc.emu_start(code, code + 8)
        self.assertTrue(mo.deliver(m))                     # the owed completion
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        m.uc.mem_write(midi.UIMR9, b'\x01')
        self.assertFalse(mo.deliver(m))                    # not unmasked yet
        store(midi.INTC1_CIMR, midi.TX_SOURCE)             # a byte queued
        m.uc.mem_write(midi.UIMR9, b'\x00')                # TxRDY disabled
        self.assertFalse(mo.deliver(m))
        m.uc.mem_write(midi.UIMR9, b'\x01')
        self.assertTrue(mo.deliver(m))
        self.assertEqual(mo.raised[midi.TX_VECTOR], 1)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        store(midi.INTC1_SIMR, midi.TX_SOURCE)             # queue empty
        self.assertFalse(mo.deliver(m))

    def test_one_completion_is_owed_at_the_start(self):
        m = _tx_machine()
        mo = midi.MidiOut(lambda d: None)
        mo.install(m)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)
        self.assertFalse(mo.deliver(m))                    # CPU masks it
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertTrue(mo.deliver(m))
        self.assertEqual(mo.raised[midi.TX_DMA_VECTOR], 1)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertFalse(mo.deliver(m))
    def test_a_pending_completion_does_not_hold_back_the_uart(self):
        m = _tx_machine()
        mo = midi.MidiOut(lambda d: None)
        mo.install(m)
        m.uc.mem_write(INTC1 + pit.ICR_BASE + 29, bytes([0]))   # 157 unarmed
        mo.tx_enabled = True
        m.uc.mem_write(midi.UIMR9, b'\x01')
        self.assertTrue(mo.deliver(m))                     # 181, not stuck
        self.assertEqual(mo.raised, {midi.TX_VECTOR: 1, midi.TX_DMA_VECTOR: 0})
    def test_the_uart_interrupt_repeats_at_the_wires_pace(self):
        m = _tx_machine()
        mo = midi.MidiOut(lambda d: None)
        mo.install(m)
        mo.attach(m, 64_000_000)
        self.assertTrue(mo.service(0))                     # the owed completion
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        mo.tx_enabled = True
        m.uc.mem_write(midi.UIMR9, b'\x01')
        gap = int(64_000_000 * midi.MidiOut.BYTE_S)
        self.assertTrue(mo.service(1000))
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertFalse(mo.service(1000 + gap - 1))       # too soon
        self.assertEqual(mo.step(1010), gap - 10)
        self.assertTrue(mo.service(1000 + gap))
        self.assertEqual(mo.raised[midi.TX_VECTOR], 2)
    def test_the_event_source_is_idle_when_nothing_is_pending(self):
        m = _tx_machine()
        mo = midi.MidiOut(lambda d: None)
        self.assertIsNone(mo.step(0))                      # not attached
        mo.install(m)
        mo.attach(m, 64_000_000)
        self.assertTrue(mo.service(0))
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertIsNone(mo.step(10))
        self.assertFalse(mo.service(10))
    def test_the_three_ways_out_keep_their_order(self):
        m = _tx_machine()
        got = []
        mo = midi.MidiOut(got.append)
        mo.install(m)
        code = HANDLER + 0x100
        def utb(value):                     # move.b #value,UTB9 as guest
            m.uc.mem_write(code, b'\x13\xfc' + struct.pack('>HI', value,
                                                          midi.UTB9))
            m.uc.emu_start(code, code + 8)
        def dma(data):
            m.uc.mem_write(TX_RING, data)
            m.uc.mem_write(midi.TCD37, struct.pack('>IHhI', TX_RING,
                                                    12 << 11, 1, 1))
            m.uc.mem_write(midi.TCD37 + midi.TCD_CITER,
                           struct.pack('>H', len(data)))
            serq(m, midi.TX_CHANNEL)
        utb(0xF8)
        dma(b'\x90\x3c\x64')
        utb(0xFA)
        dma(b'\x80\x3c\x00')
        self.assertEqual(got, [b'\xf8', b'\x90\x3c\x64', b'\xfa',
                               b'\x80\x3c\x00'])
        self.assertEqual((mo.sent, mo.direct, mo.dma_runs), (8, 2, 2))
    def test_another_channels_enable_is_ignored(self):
        m = _tx_machine()
        got = []
        mo = midi.MidiOut(got.append)
        mo.install(m)
        m.uc.mem_write(TX_RING, b'\x90\x3c\x64')
        m.uc.mem_write(midi.TCD37, struct.pack('>IHhI', TX_RING, 12 << 11, 1, 1))
        m.uc.mem_write(midi.TCD37 + midi.TCD_CITER, struct.pack('>H', 3))
        serq(m, 36)                                        # another channel
        serq(m, 0x80 | midi.TX_CHANNEL)                    # SERQ's no-op bit
        self.assertEqual((got, mo.dma_runs), ([], 0))
        serq(m, midi.TX_CHANNEL)
        self.assertEqual((got, mo.dma_runs), ([b'\x90\x3c\x64'], 1))
    def test_the_descriptor_reads_back_as_the_controller_leaves_it(self):
        m = _tx_machine()
        mo = midi.MidiOut(lambda d: None)
        mo.install(m)
        m.uc.mem_write(TX_RING, b'\x90\x3c\x64')
        m.uc.mem_write(midi.TCD37, struct.pack('>IHhI', TX_RING, 12 << 11, 1, 1))
        m.uc.mem_write(midi.TCD37 + midi.TCD_SLAST, struct.pack('>i', -3))
        m.uc.mem_write(midi.TCD37 + midi.TCD_CITER, struct.pack('>H', 3))
        m.uc.mem_write(midi.TCD37 + midi.TCD_BITER, struct.pack('>H', 3))
        m.uc.mem_write(midi.TCD37 + midi.TCD_CSR, struct.pack('>H', 0x0001))
        serq(m, midi.TX_CHANNEL)
        rd = lambda off, fmt: struct.unpack(fmt, bytes(m.uc.mem_read(
            midi.TCD37 + off, struct.calcsize(fmt))))[0]
        self.assertEqual(rd(midi.TCD_SADDR, '>I'), TX_RING)        # +3, SLAST -3
        self.assertEqual(rd(midi.TCD_CITER, '>H'), 3)              # from BITER
        self.assertEqual(rd(midi.TCD_CSR, '>H'), midi.CSR_DONE)    # START gone
    def test_an_empty_dma_run_sends_nothing_but_completes(self):
        m = _tx_machine()
        got = []
        mo = midi.MidiOut(got.append)
        mo.install(m)
        m.uc.mem_write(midi.TCD37, struct.pack('>IHhI', TX_RING, 12 << 11, 1, 1))
        m.uc.mem_write(midi.TCD37 + midi.TCD_CITER, struct.pack('>H', 0))
        serq(m, midi.TX_CHANNEL)
        self.assertEqual((got, mo.sent), ([], 0))
        self.assertTrue(mo.deliver(m))
        self.assertEqual(mo.raised[midi.TX_DMA_VECTOR], 1)
    def test_the_dma_interrupt_waits_for_the_cpu_level(self):
        m = _tx_machine()
        mo = midi.MidiOut(lambda d: None)
        mo.install(m)
        m.uc.mem_write(TX_RING, b'\x90')
        m.uc.mem_write(midi.TCD37, struct.pack('>IHhI', TX_RING, 12 << 11, 1, 1))
        m.uc.mem_write(midi.TCD37 + midi.TCD_CITER, struct.pack('>H', 1))
        serq(m, midi.TX_CHANNEL)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2700)             # all masked
        self.assertFalse(mo.deliver(m))
        self.assertEqual(mo.raised[midi.TX_DMA_VECTOR], 0)
        m.uc.reg_write(UC_M68K_REG_SR, 0x2000)
        self.assertTrue(mo.deliver(m))                     # still pending
        self.assertFalse(mo.deliver(m))                    # and only once
        self.assertEqual(mo.raised[midi.TX_DMA_VECTOR], 1)
_REAL_RTMIDI = midi._rtmidi     # before any test swaps in a fake


class _FakeRtmidi:
    """python-rtmidi's surface HostMidi uses, recording what is sent. `api`
    is the backend the ports report (ALSA's number by default); WinMM's has
    no virtual ports."""
    API_LINUX_ALSA, API_WINDOWS_MM = 2, 4

    def __init__(self, devices=('Synth 20:0',), api=API_LINUX_ALSA):
        self.devices = list(devices)
        self.api = api
        self.outs = []
        rt = self
        class Port:
            def __init__(self, name=None):
                self.name, self.opened, self.sent = name, None, []
            def get_current_api(self):
                return rt.api
            def open_virtual_port(self, name):
                if rt.api == rt.API_WINDOWS_MM:
                    raise NotImplementedError('Virtual ports are not supported '
                                              'by the Windows MultiMedia API.')
                self.opened = 'virtual'
            def open_port(self, index):
                self.opened = rt.devices[index]
            def get_ports(self):
                return list(rt.devices)
            def close_port(self):
                self.opened = None
            def delete(self):
                pass
            def ignore_types(self, **kwargs):
                pass
            def set_callback(self, fn):
                self.callback = fn
        class Out(Port):
            def __init__(self, name=None):
                super().__init__(name)
                rt.outs.append(self)
            def send_message(self, msg):
                self.sent.append(bytes(msg))
        self.MidiIn, self.MidiOut = Port, Out
class HostOutTest(unittest.TestCase):
    def setUp(self):
        import os
        from unittest import mock
        self.rt = _FakeRtmidi()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('DIGIEMU_MIDI_IN', None)
        os.environ.pop('DIGIEMU_MIDI_OUT', None)
        patch = mock.patch.object(midi, '_rtmidi', lambda: self.rt)
        patch.start()
        self.addCleanup(patch.stop)
        self.host = midi.HostMidi('Digitone (digiemu)', lambda d: None)
    def virtual(self):
        return next(o for o in self.rt.outs if o.opened == 'virtual')
    def test_the_firmwares_stream_goes_out_as_whole_messages(self):
        self.host.send(b'\x90\x3c')                # half a note: held back
        self.assertEqual(self.virtual().sent, [])
        self.host.send(b'\x64\xf8\x3d\x40')      # rest, clock, running status
        self.assertEqual(self.virtual().sent,
                         [b'\x90\x3c\x64', b'\xf8', b'\x90\x3d\x40'])
    def test_a_chosen_device_gets_them_too_and_can_be_dropped(self):
        self.host.set_output('Synth')              # ALSA's 20:0 is not needed
        self.assertEqual(self.host.output, 'Synth')
        self.host.send(b'\x90\x3c\x64')
        dev = next(o for o in self.rt.outs if o.opened == 'Synth 20:0')
        self.assertEqual(dev.sent, [b'\x90\x3c\x64'])
        self.assertEqual(self.virtual().sent, [b'\x90\x3c\x64'])
        self.host.set_output(None)
        self.host.send(b'\x80\x3c\x00')
        self.assertEqual(len(dev.sent), 1)
        self.assertEqual(len(self.virtual().sent), 2)
    def test_a_missing_device_raises_and_leaves_no_output(self):
        self.host.set_output('Synth')
        with self.assertRaises(OSError):
            self.host.set_output('Unplugged')
        self.assertIsNone(self.host.output)
    def test_the_output_is_picked_from_the_environment(self):
        import os
        os.environ['DIGIEMU_MIDI_OUT'] = 'Syn'
        host = midi.HostMidi('Digitone (digiemu)', lambda d: None)
        self.assertEqual(host.output, 'Synth')
    def test_without_rtmidi_it_says_how_to_get_it(self):
        from unittest import mock
        with mock.patch.dict('sys.modules', {'rtmidi': None}):
            with self.assertRaisesRegex(OSError, 'extra midi'):
                _REAL_RTMIDI()


class HostWindowsTest(unittest.TestCase):
    """HostMidi on RtMidi's WinMM backend: no virtual ports, and port names
    that carry an index which moves when devices come and go."""

    def setUp(self):
        import os
        from unittest import mock
        self.rt = _FakeRtmidi(devices=('Focusrite USB MIDI 0', 'Elektron Digitakt 1'),
                              api=_FakeRtmidi.API_WINDOWS_MM)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('DIGIEMU_MIDI_IN', None)
        os.environ.pop('DIGIEMU_MIDI_OUT', None)
        patch = mock.patch.object(midi, '_rtmidi', lambda: self.rt)
        patch.start()
        self.addCleanup(patch.stop)
        self.host = midi.HostMidi('Digitakt (digiemu)', lambda d: None)

    def test_no_virtual_ports_is_not_an_error(self):
        self.assertFalse(self.host.virtual)
        self.assertEqual(len(self.host.problems), 2)       # in and out, logged
        self.host.send(b'\x90\x3c\x64')                    # nowhere to go yet

    def test_devices_are_listed_and_chosen_without_the_index(self):
        self.assertEqual(self.host.inputs(), ['Focusrite USB MIDI', 'Elektron Digitakt'])
        self.host.set_output('Elektron Digitakt')
        self.host.send(b'\x90\x3c\x64')
        dev = next(o for o in self.rt.outs if o.opened == 'Elektron Digitakt 1')
        self.assertEqual(dev.sent, [b'\x90\x3c\x64'])

    def test_a_saved_choice_survives_the_index_moving(self):
        self.rt.devices = ['Elektron Digitakt 0']          # the interface unplugged
        self.host.set_input('Elektron Digitakt')
        self.assertEqual(self.host.input, 'Elektron Digitakt')

    def test_two_devices_of_one_name_are_listed_once(self):
        self.rt.devices = ['USB MIDI 0', 'USB MIDI 1']
        self.assertEqual(self.host.outputs(), ['USB MIDI'])

    def test_where_virtual_ports_exist_they_are_reported(self):
        from unittest import mock
        with mock.patch.object(self.rt, 'api', _FakeRtmidi.API_LINUX_ALSA):
            self.assertTrue(midi.HostMidi('Digitakt (digiemu)', lambda d: None).virtual)


if __name__ == '__main__':
    unittest.main()
