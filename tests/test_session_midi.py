# pyright: reportMissingImports=false
"""emu/session.py: an interrupt raised between chunks is taken.

MidiIn.deliver raises vector 156 between Session chunks: it pushes a frame
and moves the PC register to the handler. Session used to resume from the
pc it kept from the previous chunk, so the handler never ran and headless
MIDI never reached the firmware. The unit test needs no firmware; the
Digitone one is opt-in and reads a firmware folder digiemu has set up.
"""
import json
import os
import unittest
from unittest import mock

from emu import longrun, session
from emu.harness import UC_M68K_REG_PC


class _Uc:
    def __init__(self, pc):
        self.pc = pc

    def reg_read(self, reg):
        assert reg == UC_M68K_REG_PC
        return self.pc


class ChunkResumeTest(unittest.TestCase):
    def test_a_chunk_resumes_where_the_pc_register_is(self):
        s = session.Session.__new__(session.Session)
        s.m = mock.Mock(uc=_Uc(0x400025C8))      # moved by a raised vector
        s.pc = 0x40084E64                         # where the last chunk ended
        s.ips, s.pits, s.stepper, s.sources = 1000, mock.Mock(now=0), True, ()
        s.strict, s.halted, s._t0, s._latched, s._raw = None, None, 0, None, b''
        s.frames, s.record_audio = [], False
        starts = []

        def spin(m, pc, instrs, **kw):
            starts.append(pc)
            return 0x40084E80, instrs, 'limit'
        with mock.patch.object(longrun, 'spin', spin):
            s._chunk(1)
        self.assertEqual(starts, [0x400025C8])
        self.assertEqual(s.pc, 0x40084E80)


FOLDER = os.environ.get('DT2_DIGITONE_FOLDER')


@unittest.skipUnless(
    os.environ.get('DT2_SESSION_MIDI_REAL') == '1' and FOLDER,
    'set DT2_SESSION_MIDI_REAL=1 and DT2_DIGITONE_FOLDER to a Digitone '
    'firmware folder digiemu has set up',
)
class DigitoneInquiryTest(unittest.TestCase):
    INQUIRY = bytes.fromhex('f07e7f0601f7')

    def test_a_device_inquiry_on_din_gets_an_answer(self):
        from emu import bootstrap, midi, portable
        with open(os.path.join(FOLDER, 'firmware.json')) as fh:
            name = json.load(fh)['release']['syx_name']
        paths = bootstrap.FirmwarePaths(root=FOLDER, syx_name=name,
                                        devices_dir=portable.devices_dir())
        out = bytearray()
        midi_out = midi.MidiOut(out.extend)
        with mock.patch.dict(os.environ, paths.env()):
            # close() never flushes the card, so the folder is left as it was.
            s = session.Session(paths.gui, paths.syx,
                                on_machine=lambda sess: midi_out.install(sess.m))
            try:
                midi_out.attach(s.m, s.ips)
                s.sources = tuple(s.sources) + (midi_out,)
                s.run_ms(100)
                self.assertEqual(bytes(out), b'')
                midi_in = midi.MidiIn()
                midi_in.put(self.INQUIRY)
                for _ in range(100):
                    if not midi_in._pending:
                        break
                    midi_in.deliver(s.m)
                    s.run_ms(1)
                s.run_ms(300)
                self.assertIsNone(s.halted)
            finally:
                s.close()
        self.assertEqual(midi_in.delivered, len(self.INQUIRY))
        # Digitone OS 1.43 and 1.44 answer on DIN with F0 7E 00 7C 00 F7.
        # Without the fix nothing comes out: the receive interrupt never ran.
        self.assertTrue(out.startswith(b'\xf0\x7e'), out.hex())
        self.assertTrue(out.endswith(b'\xf7'), out.hex())


if __name__ == '__main__':
    unittest.main()
