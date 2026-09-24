"""Stock against custom: the same key presses on two builds, and where they differ.

A custom build is usually meant to change one thing. The quickest way to see
that it changed only that thing is to drive both builds identically and look
at what differs: every screen the firmware drew and every sample it
rendered. Sessions are repeatable (emu/session.py), so with the same script
two runs of the SAME build are identical, and any difference between two
builds is the builds'.

A script is plain text, one step a line, `#` for comments:

    wait 1000              run 1000 emulated ms
    tap PLAY [HOLD] [AFTER] press and release a key (defaults 100 ms, 300 ms)
    press FUNC [AFTER]     hold a key down
    release FUNC [AFTER]   let it go
    turn A +3 [AFTER]      turn an encoder by detents (defaults 150 ms after)
    snap playing           name this moment: its screen is compared and saved

Key names are the device file's [panel.labels] (PLAY, STOP, TRIG, SRC, the
trig keys 1..16 ...); `#24` is a raw code. Encoders are A..H. A script is
checked against the panel before anything boots. The default one visits the
parameter pages the product has (SRC on a Digitakt, SYN1 and SYN2 on a
Digitone).

    python -m emu.fwcompare STOCK_FOLDER CUSTOM_FOLDER [--script FILE] [--out DIR]

where each folder is a firmware folder the app built (it holds the .syx,
the sections and a settled snapshot), or use emu.fwcheck, which builds both.

The report says, for each snap, whether the screens are identical (and if
not how many pixels differ and where), the first moment the two screen
streams diverge, and for the audio, which 100 ms windows differ and by how
much. Differing snaps are written as PNGs: stock, custom and a difference
panel side by side.
"""
import argparse
import json
import math
import os
import struct
import sys

from emu import device as devmod
from emu import panel

# The parameter-page keys in panel order. The products differ here: the
# Digitakt's SRC page is the Digitone's SYN1 and SYN2.
PAGE_KEYS = ('TRIG', 'SRC', 'SYN1', 'SYN2', 'FLTR', 'AMP', 'LFO')


def default_script(labels):
    """-> the default script for a panel with these key labels (a device's
    [panel.labels] values): each parameter page it has, then track 1's trig,
    the pattern played and stopped."""
    have = {str(label).upper() for label in labels}
    lines = ['# Visit each parameter page, play the pattern, trigger track 1.',
             'wait 1000', 'snap start']
    for key in PAGE_KEYS:
        if key in have:
            lines += ['tap %s' % key, 'snap %s-page' % key.lower()]
    lines += ['tap TRIG', 'tap 1 80 700', 'snap trig-1',
              'tap PLAY', 'wait 2000', 'snap playing',
              'tap STOP', 'tap STOP', 'wait 500', 'snap stopped']
    return '\n'.join(lines) + '\n'


WINDOW_MS = 100


class ScriptError(ValueError):
    pass


def _ms(tok, what):
    try:
        v = float(tok)
    except ValueError:
        raise ScriptError('%s must be a number of ms, not %r' % (what, tok))
    if v < 0:
        raise ScriptError('%s must not be negative' % what)
    return v


def parse_script(text):
    """-> [(kind, ...)] steps for emu.session.run_script."""
    steps = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        # An inline comment starts at ' # '; '#24' after a verb is a code.
        cut = line.find(' # ')
        if cut >= 0:
            line = line[:cut].rstrip()
        tok = line.split()
        verb = tok[0].lower()
        try:
            if verb == 'wait' and len(tok) == 2:
                steps.append(('wait', _ms(tok[1], 'wait')))
            elif verb == 'tap' and 2 <= len(tok) <= 4:
                hold = _ms(tok[2], 'hold') if len(tok) > 2 else 100
                after = _ms(tok[3], 'after') if len(tok) > 3 else 300
                steps.append(('tap', tok[1], hold, after))
            elif verb in ('press', 'release') and 2 <= len(tok) <= 3:
                after = _ms(tok[2], 'after') if len(tok) > 2 else 100
                steps.append((verb, tok[1], after))
            elif verb == 'turn' and 3 <= len(tok) <= 4:
                try:
                    detents = int(tok[2])
                except ValueError:
                    raise ScriptError('detents must be a whole number')
                after = _ms(tok[3], 'after') if len(tok) > 3 else 150
                steps.append(('turn', tok[1], detents, after))
            elif verb == 'snap' and len(tok) == 2:
                steps.append(('snap', tok[1]))
            else:
                raise ScriptError('cannot read %r' % line)
        except ScriptError as exc:
            raise ScriptError('line %d: %s' % (n, exc)) from None
    labels = [s[1] for s in steps if s[0] == 'snap']
    if len(labels) != len(set(labels)):
        raise ScriptError('snap names must be unique')
    return steps


def script_problems(steps, device):
    """-> what in `steps` the device's panel cannot do: keys it has no
    label for, encoders it does not have. Checked before a build boots, so a
    misspelt key costs seconds, not a boot and a run."""
    from emu.session import encoder_number
    names = {str(label).upper() for label in device.labels.values()}
    out = []
    for step in steps:
        if step[0] in ('tap', 'press', 'release'):
            key = str(step[1]).strip()
            if key.upper() in names or (key.startswith('#')
                                        and key[1:].isdigit()):
                continue
            out.append('no key called %r on the %s panel'
                       % (key, device.name))
        elif step[0] == 'turn':
            number = encoder_number(step[1])
            if number is None or device.encoder_channel(number) is None:
                out.append('no encoder %r on the %s panel'
                           % (step[1], device.name))
    return out


# -- screens ----------------------------------------------------------------------
def pixel_diff(a, b):
    """-> (count, bbox or None) of the pixels that differ between frames."""
    if a is None or b is None:
        return (None, None) if a is None and b is None else (-1, None)
    pa, pb = panel.lit(a), panel.lit(b)
    diff = pa ^ pb
    if not diff:
        return 0, None
    xs = [x for x, _ in diff]
    ys = [y for _, y in diff]
    return len(diff), (min(xs), min(ys), max(xs), max(ys))


def first_divergence(frames_a, frames_b):
    """-> the first emulated ms at which the frame on screen differs."""
    times = sorted({t for t, _ in frames_a} | {t for t, _ in frames_b})

    def at(frames, t):
        cur = None
        for ft, buf in frames:
            if ft <= t:
                cur = buf
            else:
                break
        return cur
    for t in times:
        if at(frames_a, t) != at(frames_b, t):
            return t
    return None


def diff_png(a, b, scale=4):
    """-> a greyscale PNG: stock | custom | difference, side by side.
    In the difference panel a pixel only stock lights is grey, one only the
    custom build lights is white."""
    from emu.screen import png
    w, h = panel.W, panel.H
    gap = 4
    W_ = (3 * w + 2 * gap) * scale
    H_ = h * scale
    out = bytearray(W_ * H_)
    pa = panel.lit(a) if a else set()
    pb = panel.lit(b) if b else set()

    def put(ox, x, y, v):
        for dy in range(scale):
            row = (y * scale + dy) * W_ + (ox + x) * scale
            for dx in range(scale):
                out[row + dx] = v
    for x, y in pa:
        put(0, x, y, 255)
    for x, y in pb:
        put(w + gap, x, y, 255)
    for x, y in pa - pb:
        put(2 * (w + gap), x, y, 110)
    for x, y in pb - pa:
        put(2 * (w + gap), x, y, 255)
    for x, y in pa & pb:
        put(2 * (w + gap), x, y, 40)
    return png(out, W_, H_)


# -- audio ------------------------------------------------------------------------
def _samples(pcm):
    n = len(pcm) // 2
    return struct.unpack('<%dh' % n, bytes(pcm[:2 * n])) if n else ()


def audio_diff(pcm_a, pcm_b, rate, window_ms=WINDOW_MS):
    """-> a summary of where two 16-bit stereo recordings differ."""
    if bytes(pcm_a) == bytes(pcm_b):
        return {'identical': True, 'seconds': len(pcm_a) / 4 / rate}
    a, b = _samples(pcm_a), _samples(pcm_b)
    n = min(len(a), len(b))
    win = max(2, int(rate * window_ms / 1000) * 2)
    windows = []
    for start in range(0, n, win):
        sa, sb = a[start:start + win], b[start:start + win]
        if sa == sb:
            continue
        m = len(sa)
        rms_a = math.sqrt(sum(v * v for v in sa) / m)
        rms_b = math.sqrt(sum(v * v for v in sb) / m)
        rms_d = math.sqrt(sum((x - y) ** 2 for x, y in zip(sa, sb)) / m)
        windows.append({'at_ms': round(start / 2 / rate * 1000),
                        'rms_stock': round(rms_a, 1),
                        'rms_custom': round(rms_b, 1),
                        'rms_difference': round(rms_d, 1)})
    return {'identical': False,
            'seconds_stock': round(len(a) / 2 / rate, 3),
            'seconds_custom': round(len(b) / 2 / rate, 3),
            'windows_differing': len(windows),
            'first_difference_ms': windows[0]['at_ms'] if windows else None,
            'windows': windows[:50]}


# -- running ------------------------------------------------------------------------
class Run:
    """What one build did under a script."""

    def __init__(self, session, marks):
        self.frames = list(session.frames)
        self.marks = list(marks)
        self.pcm = bytes(session.pcm)
        self.inputs = list(session.inputs)
        self.halted = session.halted
        self.rate = session.audio_cfg['rate'] if session.audio_cfg else 48000
        self.screens = {label: session.screen_at(ms) for label, ms in marks}


def run(snapshot, syx, steps, **session_kw):
    """Run `steps` on a fresh Session of `snapshot`. -> (Run, Session)."""
    from emu.session import Session, run_script
    s = Session(snapshot, syx, **session_kw)
    marks = run_script(s, steps)
    return Run(s, marks), s


def compare(stock, custom, out=None):
    """-> the comparison report of two Runs; writes PNGs to `out` if given."""
    snaps = []
    for label, _ms_ in stock.marks:
        a, b = stock.screens.get(label), custom.screens.get(label)
        count, box = pixel_diff(a, b)
        entry = {'snap': label, 'identical': count == 0,
                 'pixels_differing': count, 'box': box}
        if out:
            os.makedirs(out, exist_ok=True)
            if a:
                panel.write_png(a, os.path.join(out, '%s-stock.png' % label), 4)
            if b:
                panel.write_png(b, os.path.join(out, '%s-custom.png' % label), 4)
            if count:
                path = os.path.join(out, '%s-diff.png' % label)
                with open(path, 'wb') as fh:
                    fh.write(diff_png(a, b))
                entry['png'] = path
        snaps.append(entry)
    return {
        'identical': (all(s['identical'] for s in snaps)
                      and bytes(stock.pcm) == bytes(custom.pcm)
                      and first_divergence(stock.frames, custom.frames) is None),
        'snaps': snaps,
        'screens_first_differ_ms': first_divergence(stock.frames, custom.frames),
        'frames': {'stock': len(stock.frames), 'custom': len(custom.frames)},
        'audio': audio_diff(stock.pcm, custom.pcm, stock.rate),
        'halted': {'stock': stock.halted, 'custom': custom.halted},
    }


def summary(report):
    """-> printable lines."""
    lines = ['screens and sound are identical' if report['identical'] else
             'the builds differ']
    for s in report['snaps']:
        if s['identical']:
            lines.append('  snap %-14s same screen' % s['snap'])
        else:
            lines.append('  snap %-14s %s pixels differ in %s%s'
                         % (s['snap'], s['pixels_differing'], s['box'],
                            ('  -> ' + s['png']) if s.get('png') else ''))
    if report['screens_first_differ_ms'] is not None:
        lines.append('  screens first differ at %.0f ms'
                     % report['screens_first_differ_ms'])
    au = report['audio']
    if au['identical']:
        lines.append('  audio identical (%.1f s)' % au['seconds'])
    else:
        lines.append('  audio differs in %d windows of %d ms, first at %s ms'
                     % (au['windows_differing'], WINDOW_MS,
                        au['first_difference_ms']))
    for who, why in report['halted'].items():
        if why:
            lines.append('  %s HALTED: %s' % (who, why))
    return lines


def folder_paths(folder):
    """-> FirmwarePaths for a firmware folder the app built (its
    firmware.json names the .syx)."""
    from emu import bootstrap as bs
    with open(os.path.join(folder, 'firmware.json'), encoding='utf-8') as fh:
        rec = json.load(fh)
    name = (rec.get('release') or {}).get('syx_name') or rec.get('syx_name')
    if not name:
        raise SystemExit('%s: firmware.json names no .syx' % folder)
    devices = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'devices')
    return bs.FirmwarePaths(folder, name, devices)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('stock', help='the stock firmware folder')
    ap.add_argument('custom', help='the custom firmware folder')
    ap.add_argument('--script', help='a key script (default: a page tour)')
    ap.add_argument('--out', default='fwcompare-out')
    args = ap.parse_args(argv)
    text = None
    if args.script:
        with open(args.script, encoding='utf-8') as fh:
            text = fh.read()
    runs = []
    for folder in (args.stock, args.custom):
        paths = folder_paths(folder)
        os.environ.update(paths.env())
        # The folder's device overlay, as the session will see it.
        dev, _fw = devmod.identify(paths.syx)
        steps = parse_script(text if text is not None
                             else default_script(dev.labels.values()))
        problems = script_problems(steps, dev)
        if problems:
            raise SystemExit('the key script: ' + '; '.join(problems))
        # gui.snap, the settled first boot: never a played session.
        r, s = run(paths.gui, paths.syx, steps)
        s.close()
        runs.append(r)
    report = compare(runs[0], runs[1], out=args.out)
    with open(os.path.join(args.out, 'compare.json'), 'w') as fh:
        json.dump(report, fh, indent=1, default=str)
    print('\n'.join(summary(report)))
    return 0 if report['identical'] else 1


if __name__ == '__main__':
    sys.exit(main())
