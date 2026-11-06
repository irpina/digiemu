"""A Syntakt-shaped front panel: the Syntakt window.

The machinery is emu/dtpanel.py's, as for the Digitone (emu/dnpanel.py); this
module only places the Syntakt's keys on the same plan: Master Volume and
LEVEL/DATA at the top left, the OLED, the eight encoders, one row of keys
under them (FUNC, TRK, the menus, the parameter pages TRIG, SYN, FLTR, AMP,
LFO, and PAGE), transport, confirm and cursor keys, and sixteen trig keys.
It is a plan, not the Syntakt's faceplate.

Controls are placed by name: devices/syntakt.toml's labels, which are the
firmware's own panel-test names (MAIN OS 1.40A). Any name this map does not
know still gets a key in the overflow block (dtpanel does that).

Sound: the machines added after launch are rendered by the Syntakt's second
CPU, which emu/dsplink.py runs; the original machines run in the FPGA, which
is not modelled, so they are silent here.

    uv run python -m emu.synpanel [snapshot] [--syx PATH]
                                  [--save-on-exit PATH] [--no-audio] [--app]
"""
import sys

from emu import dtpanel
from emu.dtpanel import AMBER, PLAY_C, REC_C, DigitaktPanel

# label -> (x, y, w, h, secondary caption, tint)
BUTTONS = {
    'FUNC': (30, 400, 100, 40, None, AMBER),
    'TRK': (140, 400, 84, 40, None, None),
    'PATTERN MENU': (232, 400, 84, 40, None, None),
    'GLOBAL': (324, 400, 84, 40, None, None),
    'FX': (416, 400, 84, 40, None, None),
    'TEMPO': (508, 400, 84, 40, None, None),
    'TRIG': (600, 400, 84, 40, None, None),
    'SYN': (692, 400, 84, 40, None, None),
    'FLTR': (784, 400, 84, 40, None, None),
    'AMP': (876, 400, 84, 40, None, None),
    'LFO': (968, 400, 84, 40, None, None),
    'PAGE': (1060, 400, 84, 40, None, None),
    'STOP': (150, 460, 76, 46, None, None),
    'PLAY': (236, 460, 76, 46, None, PLAY_C),
    'RECORD': (322, 460, 76, 46, None, REC_C),
    'BANK': (412, 460, 80, 46, None, None),
    'PTN': (500, 460, 80, 46, None, None),
    'YES': dtpanel.BUTTONS['YES'][:4] + (None, None),
    'NO': dtpanel.BUTTONS['NO'][:4] + (None, None),
    'UP': dtpanel.BUTTONS['UP'],
    'LEFT': dtpanel.BUTTONS['LEFT'],
    'DOWN': dtpanel.BUTTONS['DOWN'],
    'RIGHT': dtpanel.BUTTONS['RIGHT'],
    **{k: dtpanel.BUTTONS[k] for k in 'ABCDEFGH'},
    'LEVEL/DATA': dtpanel.BUTTONS['LEVEL/DATA'][:4] + (None, None),
}
for _i in range(16):
    BUTTONS[str(_i + 1)] = (150 + (_i % 8) * 108, 598 + (_i // 8) * 82,
                            98, 72, None, None)

ENCODERS = dict(dtpanel.ENCODERS)


class SyntaktPanel(DigitaktPanel):
    PRODUCT = 'Syntakt'
    TITLE = 'digiemu — Syntakt emulator (unofficial, experimental)'
    SUBTITLE = ('Syntakt emulator · unofficial, not affiliated with Elektron'
                ' · FPGA machines are silent')
    BUTTONS = BUTTONS
    ENCODERS = ENCODERS
    SAMPLES = False


def main(argv):
    """Run the Syntakt panel until its window closes (see emu.dtpanel.main)."""
    return dtpanel.run(argv, SyntaktPanel, prog='python -m emu.synpanel',
                       description='The Syntakt front panel.')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
