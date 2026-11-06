"""Overbridge audio on the Digitone (mk1), at the OS's own feed.

On the unit the audio block interrupt hands every block of frames to the
Overbridge sender (ob_audio_frames in emu/symbols.py): (frames, side, count),
`count` frames of twelve big-endian signed 32-bit samples -- main L/R, tracks
1-4 L/R and the input L/R, the twelve channels Overbridge shows a host -- 48
bytes a frame. The sender packs them seven at a time into transfers for the
bulk IN endpoint, and returns at once while no host streams, which is the
emulator's case. Its entry is hooked here without changing what it does:
the frames are read and handed to `sink`, the sender runs on as before.

The audio leaves at the emulated rate, 48 kHz of emulated time, so a host
that paces the emulator to real time (tools/usbrun.py) hears it in real time.
`LinkServer` from emu/usbmidi.py carries it to a host as a plain stream of
those frames (tools/usbrun.py --ob-audio-port), the way analog-emu's oblink
carries the Rytm's.
"""
import struct

from unicorn.m68k_const import UC_M68K_REG_A7

CHANNELS = 12
FRAME = CHANNELS * 4          # bytes in one frame
DEFAULT_PORT = 7312           # beside the USB MIDI link's 7311
NAMES = ('Main L', 'Main R', 'Track 1 L', 'Track 1 R', 'Track 2 L', 'Track 2 R',
         'Track 3 L', 'Track 3 R', 'Track 4 L', 'Track 4 R', 'Input L', 'Input R')


class ObAudio:
    """The twelve Overbridge channels as the OS feeds them: `sink(bytes)` gets
    whole frames, big-endian int32, channel-interleaved."""

    def __init__(self, sink=None):
        self.sink = sink
        self.frames = 0               # frames handed to the sink
        self.blocks = 0

    def install(self, m, at, profile):
        """Hook the sender. -> False (nothing installed) when this image lacks it."""
        if profile.get('ob_audio_frames') in (None, ()):
            return False
        at(profile.ob_audio_frames, self._on_frames)
        return True

    def _on_frames(self, uc, address, size, data):
        sp = uc.reg_read(UC_M68K_REG_A7)
        _ret, frames, _side, count = struct.unpack('>IIII', uc.mem_read(sp, 16))
        if not 0 < count <= 256:
            return
        out = bytes(uc.mem_read(frames, count * FRAME)) if frames else bytes(count * FRAME)
        self.blocks += 1
        self.frames += count
        if self.sink is not None:
            self.sink(out)

    def report(self):
        return 'ob audio: %d frames (%.1f s at 48 kHz) in %d blocks' % (
            self.frames, self.frames / 48000.0, self.blocks)
