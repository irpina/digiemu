"""A Digitone-shaped front panel: the Digitone (mk1) window.

The machinery is emu/dtpanel.py's -- the emulator thread, multitouch input,
key LEDs, audio controls, session save and shutdown -- and this module only
draws a different instrument: the OLED, eight encoders in two rows with
LEVEL/DATA beside them, the six parameter-page keys (TRIG, SYN1, SYN2, FLTR,
AMP, LFO) down the right, the four track keys T1..T4 and MIDI down the left,
the menu keys, transport and cursor keys, and sixteen trig keys.

Controls are placed by the measured names in devices/digitone.toml, not by
the firmware's panel-test tables: the Digitone builds those at run time, and
on this product (as on the Digitakt) they label the test screen, not the keys
(see the device file for how each code was identified). Every encoder
pushes: each knob's name sits on its push switch below it.

No LOAD SAMPLES: the Digitone has no sample engine and its +Drive no sample
volume.

Sound: the Digitone's FM voices are rendered by its second CPU, which the
firmware calls the DSP. emu/dsplink.py runs that CPU's own code on a second
engine, on its own thread while audio is live, and the main OS mixes its
voices with the effects and plays them as on the Digitakt.

    uv run python -m emu.dnpanel [snapshot] [--syx PATH]
                                 [--save-on-exit PATH] [--no-audio] [--app]
"""
import sys

from emu import dtpanel
from emu.dtpanel import AMBER, PLAY_C, REC_C, DigitaktPanel

# Firmware-independent label -> (x, y, w, h, secondary caption, tint): the
# labels are devices/digitone.toml's [panel.labels].
BUTTONS = {
    # right-hand page column: the six parameter pages, PAGE below
    'TRIG': (1052, 90, 92, 40, None, None),
    'SYN1': (1052, 142, 92, 40, None, None),
    'SYN2': (1052, 194, 92, 40, None, None),
    'FLTR': (1052, 246, 92, 40, None, None),
    'AMP': (1052, 298, 92, 40, None, None),
    'LFO': (1052, 350, 92, 40, None, None),
    'PAGE': (1052, 410, 92, 38, None, None),
    # left column: FUNC, then the tracks
    'FUNC': (30, 406, 88, 40, None, AMBER),
    'T1': (30, 460, 88, 40, None, None),
    'T2': (30, 510, 88, 40, None, None),
    'T3': (30, 560, 88, 40, None, None),
    'T4': (30, 610, 88, 40, None, None),
    'MIDI': (30, 668, 88, 40, None, None),
    # menu row
    'KEYBOARD': (150, 406, 100, 40, 'Keyboard Setup', None),
    'SONG': (258, 406, 86, 40, None, None),
    'GLOBAL': (352, 406, 86, 40, None, None),
    'VOICE': (446, 406, 86, 40, None, None),
    'TEMPO': (540, 406, 86, 40, None, None),
    'BANK': (634, 406, 86, 40, None, None),
    'PTN': (728, 406, 86, 40, None, None),
    # transport
    'STOP': (150, 470, 76, 46, None, None),
    'PLAY': (236, 470, 76, 46, None, PLAY_C),
    'RECORD': (322, 470, 76, 46, None, REC_C),
    # confirm and cursor
    'YES': (430, 470, 76, 46, None, None),
    'NO': (516, 470, 76, 46, None, None),
    'UP': (664, 464, 56, 40, None, None),
    'LEFT': (604, 510, 56, 40, None, None),
    'DOWN': (664, 510, 56, 40, None, None),
    'RIGHT': (724, 510, 56, 40, None, None),
    # Every encoder pushes (46..54): the push switch carries the knob's name.
    'A': (596, 190, 48, 18, None, None),
    'B': (700, 190, 48, 18, None, None),
    'C': (804, 190, 48, 18, None, None),
    'D': (908, 190, 48, 18, None, None),
    'E': (596, 312, 48, 18, None, None),
    'F': (700, 312, 48, 18, None, None),
    'G': (804, 312, 48, 18, None, None),
    'H': (908, 312, 48, 18, None, None),
    'LEVEL/DATA': (872, 520, 90, 18, None, None),
}
# sixteen trig keys, two rows of eight
for _i in range(16):
    BUTTONS[str(_i + 1)] = (150 + (_i % 8) * 108, 598 + (_i // 8) * 82,
                            98, 72, None, None)

# Encoder label -> (centre x, centre y, radius).
ENCODERS = {
    'A': (620, 150, 34), 'B': (724, 150, 34),
    'C': (828, 150, 34), 'D': (932, 150, 34),
    'E': (620, 272, 34), 'F': (724, 272, 34),
    'G': (828, 272, 34), 'H': (932, 272, 34),
    'LEVEL/DATA': (917, 482, 30),
}


class DigitonePanel(DigitaktPanel):
    PRODUCT = 'Digitone'
    TITLE = 'digiemu — Digitone mk1 emulator (unofficial)'
    SUBTITLE = ('Digitone mk1 emulator · unofficial, not affiliated with '
                'Elektron')
    BUTTONS = BUTTONS
    ENCODERS = ENCODERS
    SAMPLES = False


def main(argv):
    """Run the Digitone panel until its window closes. -> the process exit
    code, as emu.dtpanel.main documents (SAMPLES_ADDED never happens here)."""
    return dtpanel.run(argv, DigitonePanel, prog='python -m emu.dnpanel',
                       description='The Digitone mk1 front panel.')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
