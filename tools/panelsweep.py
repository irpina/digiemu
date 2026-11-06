#!/usr/bin/env python3
"""Front-panel code map via the firmware's own `queue_send` event record.

There is no framebuffer to diff here -- and even if there were, it would be
the wrong tool. `tools/panelin.py` (the input side of this: see its module
docstring) delivers button and encoder events the way the UART8 driver does;
this is the *output* side, and it does not watch pixels change. It hooks the
one function the firmware itself calls to hand a decoded panel event to the
rest of the system, and reads the 16-byte record straight out of guest
memory at the moment of the call. That is immune to everything a framebuffer
diff has to fight: render timing (nothing has to have been drawn yet), modal
UI state (a menu screen intercepting a button does not stop it from being
queued), and frame tearing (there is no frame). The record is the firmware's
own classification of the input -- the ground truth this project has been
trying to observe indirectly this whole time.

RECORD LAYOUT (16 bytes). Two single-byte header fields are stable and
verified across dozens of samples on Digitone:

    +0x00  type    a single BYTE (not a 4-byte word -- see the note below),
                   0 for a button record, 1 for an encoder record in the
                   build this was verified against; discovered per build in
                   calibrate(), never assumed
    +0x07  code    a single byte: channel*8 + bit + 1 for buttons, channel +
                   1 for encoders on the verified build

Bytes 0x01-0x06 are unused padding on every sample seen. What comes after
the code byte is NOT a third fixed field, and this is the one genuinely
surprising thing this tool had to work around: a button record's flag byte
(0x01 press / 0x10 release) sits at +0x0B, while an encoder record's signed
delta byte sits at +0x08 -- two different sub-layouts sharing one 16-byte
frame, most likely a union of a button-event struct and an encoder-event
struct behind a common type+code header. Trusting one fixed offset for both
(as an early version of this tool did, following the natural but wrong
assumption that the whole record is four clean 4-byte big-endian words)
silently mis-decodes every encoder value as a huge, wrong constant: delta
+1 read as a 4-byte word starting at +0x08 is 0x01000000 (16777216), not 1,
because the actual delta byte is sitting at the MSB of that word, not the
whole word. Because of that trap, `code` and `value` are read as single
bytes at fixed offsets *within the header* (0x00, 0x07), but each record's
`value` offset (0x08 or 0x0B) is looked up from a table this tool derives
at runtime in calibrate() -- see below -- rather than assumed, so a build
whose layout differs would produce a loud calibration failure instead of a
quietly wrong table.

CALLING CONVENTION: `queue_send(queue*, item*)` is reached by `jsr`. On
entry the stack at A7 holds three big-endian 32-bit words: the return
address pushed by `jsr`, then the caller's `queue` and `item` pointer
arguments (pushed by the caller before the call). `item` points at the
16-byte record above, already filled in by the caller.

WHY NOTHING HERE IS A HARDCODED PER-BUILD ADDRESS. The two `queue_send`
*call sites* for buttons and encoders, and the queue they push onto, are
all inside the application image, which relocates between builds -- exactly
the class of address this project has a standing guardrail against carrying
from one firmware to another (see `emu/symbols.py`'s module docstring;
`emu/panelin.py` hardcoding Digitakt's UART8 ring pointer and silently
doing the wrong thing on Digitone is the concrete case that guardrail
exists because of). Only `queue_send` itself (0x40001896) is used as a
literal here, because it has been verified to sit at the same address in
both known builds -- consistent with `emu/symbols.py`'s finding that
RTOS-linked code sits at byte-identical offsets regardless of what the
application above it looks like, unlike the application's own call sites
into it.

So the two push call sites, and each record variant's value-byte offset,
are *discovered at runtime* instead of typed in:

  1. Hook `queue_send` unconditionally (every call, any caller) and, for
     each one, try to read a 16-byte record at `item`. A hit only becomes a
     *candidate* if the byte at +0x00 is 0 or 1 -- the record-shape filter.
     This alone is not enough: plenty of `queue_send` traffic unrelated to
     the panel can happen to look like a valid-shaped record by accident.
  2. Run a settle window with *no* input injected and record every
     (caller, item) pair that produces a shape-valid candidate anyway. That
     is the ignore-set: background noise that fires whether or not anyone
     touches the panel, built automatically from observed behaviour rather
     than a name -- it would just as well catch a second, unrelated,
     periodic queue_send this build has that Digitone didn't.
  3. Calibrate by injecting a small, known sequence of button and encoder
     events through `emu/panelin.py` and diffing the raw records they
     produce:
       - press(ch0,bit0) vs release(ch0,bit0): same code, different value
         -> the single byte offset where they differ is that build's
         button value_offset.
       - press(ch0,bit0) vs press(ch1,bit0): same value (both "press"),
         different code -> confirms the code offset.
       - encoder(ch0,+1) vs encoder(ch0,+5): same code, different value
         -> that build's encoder value_offset.
       - encoder(ch0,+1) vs encoder(ch1,+1): same value, different code
         -> confirms the code offset.
     Each diff is required to land on EXACTLY one byte; more than one (or
     zero) means the layout assumption does not hold on this build, and
     calibration fails loudly rather than guessing. The caller address of
     whichever probe answered is that build's push call site -- necessarily
     new, since it cannot have been in the settle-built ignore-set.
  4. The full sweep then just checks each probe's new record for one of
     the two calibrated callers, decodes it with that caller's discovered
     offsets, and reports it. A record that matches neither calibrated
     caller and isn't already in the ignore-set is genuinely new behaviour
     worth printing a warning about, not silently dropping.

INTRO HANDOVER: the supplied snapshots have the intro animation still
running. This tool spins with **no instruction cap** until the firmware's
own `intro_done` hook fires -- a capped settle has previously produced
several agents confidently reporting on a panel map that was still showing
the intro. The only thing that stops the wait early is the emulator itself
reporting a non-'limit' stop (a real crash), which is treated as a hard
failure (exit 2), never as "close enough, and moving on."

Usage:
    uv run python tools/panelsweep.py \\
        --syx Digitone_II_OS1.10E.syx \\
        --snapshot snapshots/Digitone_II_OS1.10E/sdboot280M.snap \\
        --json out/panelsweep-dn2.json
"""
import argparse
import collections
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC

from emu import symbols
from emu.longrun import build, spin
from emu.pit import Pits, intro_running
from emu.dtim import Dtims, Timers
import emu.panelin as panelin

from addrtrace import load_main_image

# Verified identical in both known builds -- see the module docstring. This
# is the ONLY literal application/RTOS address in this file; everything else
# (push call sites, the sink queue, the per-record-type field offsets) is
# discovered at runtime in calibrate().
# Digitakt II / Digitone II share an RTOS build, so this address is the same
# in both and was taken as a safe literal. Digitakt (mk1) links the SAME RTOS
# 0x2E4 LATER, so the literal silently hooks nothing there and every sweep
# reports "no record observed". main() overrides it from the resolved profile;
# the literal is only the fallback when queue_send did not resolve.
QUEUE_SEND = 0x40001896

RECORD_SIZE = 16
TYPE_OFFSET = 0x00
CODE_OFFSET = 0x07
BIT_RANGE = range(8)   # buttons per channel group; fixed by the wire format


# --- queue_send instrumentation -------------------------------------------

def make_qs_state():
    return {
        'ignore': None,          # None while collecting; a frozenset once settled
        'candidates': [],        # shape-valid, not-ignored hits since last clear()
        'settle_keys': set(),    # (ret, item) seen as candidates during settle
        'total_calls': 0,        # every queue_send call, for diagnostics
    }


def install_qs_hook(m, at, state):
    """Install the global queue_send hook. Never filters by caller -- see
    the module docstring for why that would defeat the whole point."""

    def on_queue_send(uc, a, size, data):
        state['total_calls'] += 1
        sp = uc.reg_read(UC_M68K_REG_A7)
        try:
            ret, queue, item = struct.unpack('>III', uc.mem_read(sp, 12))
        except Exception:
            return
        try:
            raw = bytes(uc.mem_read(item, RECORD_SIZE))
        except Exception:
            return
        if raw[TYPE_OFFSET] not in (0, 1):
            return  # fails the record-shape filter: +0x00 byte is not 0/1
        key = (ret, item)
        if state['ignore'] is None:
            state['settle_keys'].add(key)
            return
        if key in state['ignore']:
            return
        state['candidates'].append({
            'ret': ret, 'queue': queue, 'item': item, 'key': key, 'raw': raw,
        })

    at(QUEUE_SEND, on_queue_send)


# --- run control -----------------------------------------------------------

def wait_for_handover(m, pc, pits, mark, profile):
    """Spin UNCAPPED until `intro_done` fires. -> (pc, ok, stop_reason).

    `ok` is False only when the emulator itself stopped for a reason other
    than running out of the (nonexistent) budget -- a real crash. There is
    deliberately no instruction ceiling here; see the module docstring.
    """
    CHUNK = 10_000_000
    while mark['intro_done'] == 0:
        pc, executed, stop = spin(m, pc, CHUNK, pits=pits)
        if stop != 'limit':
            return pc, False, stop
    return pc, True, 'limit'


def probe_wait(m, pc, pits, state, cap, wanted_ret=None, min_chunk=300_000,
               quiet_chunks=2):
    """Spin in small pits-paced steps after an injection. -> (pc,
    instrs_used, stop_reason, records).

    Two modes, because Digitakt turned out to need both:

    `wanted_ret=None` (calibration, before any caller is known): injecting
    ANY panel input on Digitakt also kicks off a reactive burst of
    unrelated queue_send calls (empirically: a redraw/job queue -- same
    caller, identical payload, repeated 10-40+ times) that is absent from
    the no-input settle window, so the ignore-set never catches it, and it
    is not present at all on Digitone. Returning on the very first
    candidate would be wrong here: depending on scheduling, either the
    burst or the real record can come first, so "first" sometimes silently
    means "burst, not the real event." Instead this drains until the set
    of candidates stops growing for `quiet_chunks` consecutive steps, so
    by the time it returns, both the burst and the real one-shot record
    (wherever it landed) are present -- deciding which is which is
    calibrate()'s job, not this function's.

    `wanted_ret` given (the normal sweep, once calibration knows the real
    caller): wait specifically for that caller, up to `cap`, then give up.
    Once it HAS answered, this keeps watching for `quiet_chunks` more
    steps with no further instance of it before returning, in case a
    second one is right behind (drained here rather than leaking into the
    next probe).

    IMPORTANT, and the reason a `cap` timeout here is NOT retried with a
    bigger budget: Digitakt's encoder push (and, it turns out, some
    channel-6 buttons) can leave a value sitting un-flushed indefinitely --
    confirmed directly by idling 20M instructions with zero further input
    and getting nothing at all -- and it is the *next* injection into that
    same subsystem, not elapsed time, that flushes it. So a caller that
    gets a timeout here cannot fix it by waiting longer; the record may
    still show up, misattributed, during whatever the NEXT probe injects.
    `sweep_encoders`/`sign_check` account for this by identifying each
    record by its own content after the fact rather than by which probe's
    window it happened to land in -- see their docstrings.
    """
    total = 0
    last_count = (sum(1 for r in state['candidates'] if r['ret'] == wanted_ret)
                  if wanted_ret is not None else len(state['candidates']))
    got = last_count > 0
    quiet = 0
    while total < cap:
        pc, executed, stop = spin(m, pc, min_chunk, pits=pits)
        total += executed
        if stop != 'limit':
            return pc, total, stop, list(state['candidates'])
        cur_count = (sum(1 for r in state['candidates'] if r['ret'] == wanted_ret)
                     if wanted_ret is not None else len(state['candidates']))
        if cur_count > last_count:
            got = True
            quiet = 0
        elif got:
            quiet += 1
            if quiet >= quiet_chunks:
                return pc, total, 'limit', list(state['candidates'])
        last_count = cur_count
    return pc, total, ('limit' if got else 'cap'), list(state['candidates'])


# --- record decoding -------------------------------------------------------

def diff_offset(raw_a, raw_b, label):
    """-> the single byte offset at which two same-shape records differ.

    Raises CalibrationError if that is not exactly one offset -- either
    result means the "one fixed value_offset per record type" assumption
    this tool relies on does not hold on this build, and guessing which
    byte to trust would silently corrupt the whole table.
    """
    # The Syntakt's records carry a 32-bit timestamp in their last four
    # bytes, which differs between any two events; it is not the value.
    diffs = [i for i in range(RECORD_SIZE) if raw_a[i] != raw_b[i]]
    if len(diffs) > 1 and set(diffs) - {12, 13, 14, 15}:
        diffs = [i for i in diffs if i < 12] or diffs
    if len(diffs) != 1:
        raise CalibrationError(
            '%s: expected exactly one differing byte between %s and %s, '
            'found %d at offsets %r' %
            (label, raw_a.hex(), raw_b.hex(), len(diffs), diffs))
    return diffs[0]


def decode(raw, value_offset, signed):
    code = raw[CODE_OFFSET]
    vbyte = raw[value_offset]
    value = struct.unpack('b', bytes([vbyte]))[0] if signed else vbyte
    return code, value


# --- calibration -------------------------------------------------------

class CalibrationError(RuntimeError):
    pass


def pick_singleton(records, what):
    """-> the one record whose caller fired EXACTLY ONCE in this batch.

    A real button/encoder push reaches queue_send once. The reactive
    redraw/job-queue noise this tool discovered on Digitakt (see
    `probe_wait`'s docstring) fires the same caller many times per
    injection with an identical payload. So "exactly one caller answered
    exactly once" is what picks the real event out of a batch that also
    contains that noise -- and it degrades to "the only record" when there
    is no noise at all, which is the whole batch on Digitone.

    Raises CalibrationError, with every record's raw bytes, if that is not
    unambiguous -- guessing here would silently point calibration at the
    wrong caller for the rest of the run.
    """
    counts = collections.Counter(r['ret'] for r in records)
    singles = [ret for ret, n in counts.items() if n == 1]
    if len(singles) != 1:
        detail = '\n'.join(
            '  ret=0x%08x item=0x%08x raw=%s' %
            (r['ret'], r['item'], r['raw'].hex()) for r in records)
        raise CalibrationError(
            'while calibrating %s: expected exactly one queue_send caller '
            'to fire exactly once, found %d such caller(s) among %d '
            'record(s):\n%s' % (what, len(singles), len(records), detail))
    ret = singles[0]
    return next(r for r in records if r['ret'] == ret)


def _one_shot(m, pc, pits, state, action, probe_cap, what):
    """Inject one action, drain until quiet, and pick out the one-shot
    record among whatever else fired. -> (pc, record)."""
    state['candidates'].clear()
    action()
    pc = m.uc.reg_read(UC_M68K_REG_PC)
    pc, instrs, stop, recs = probe_wait(m, pc, pits, state, probe_cap)
    if not recs:
        raise CalibrationError(
            'no queue_send record observed for %s within %d instructions '
            '(stop=%s) -- instrumentation is not landing; everything '
            'downstream would be meaningless' % (what, probe_cap, stop))
    return pc, pick_singleton(recs, what)


def _drain(m, pc, pits, state, action, probe_cap):
    """Inject one action and drain until quiet, discarding the result --
    used only to let a reset settle before the next calibration probe."""
    state['candidates'].clear()
    action()
    pc = m.uc.reg_read(UC_M68K_REG_PC)
    pc, instrs, stop, recs = probe_wait(m, pc, pits, state, probe_cap)
    return pc


def calibrate(m, pc, profile, pits, state, probe_cap):
    """Identify the button and encoder push call sites, and each record
    type's value-byte offset, by injecting a small known sequence of events
    and diffing the raw records they produce. -> (pc, calibration dict).

    This is the runtime replacement for what would otherwise be Digitone's
    hardcoded 0x401117cc/0x40111414 caller addresses: discovered here, so
    the same code is correct on a build whose call sites (and, it turns
    out, whose value-byte layout) differ.
    """
    # Button: press/release ch0 bit0 (value_offset), then press ch1 bit0
    # (code_offset cross-check). Release ch1 too, to leave both channels'
    # edge-detect state back at neutral before the real sweep starts -- see
    # emu/panelin.py's docstring on how it derives edges from consecutive
    # absolute masks.
    pc, btn_press0 = _one_shot(
        m, pc, pits, state, lambda: panelin.press(m, profile, 0, 0),
        probe_cap, 'a button press (channel 0, bit 0)')
    pc, btn_release0 = _one_shot(
        m, pc, pits, state, lambda: panelin.release(m, profile, 0, 0),
        probe_cap, 'a button release (channel 0, bit 0)')
    pc, btn_press1 = _one_shot(
        m, pc, pits, state, lambda: panelin.press(m, profile, 1, 0),
        probe_cap, 'a button press (channel 1, bit 0)')
    pc = _drain(m, pc, pits, state,
                lambda: panelin.release(m, profile, 1, 0), probe_cap)

    if btn_press0['ret'] != btn_release0['ret'] or \
            btn_press0['ret'] != btn_press1['ret']:
        raise CalibrationError(
            'button press/release calibration probes were answered by '
            'different queue_send callers (0x%08x / 0x%08x / 0x%08x)' %
            (btn_press0['ret'], btn_release0['ret'], btn_press1['ret']))

    button_caller = btn_press0['ret']
    button_type = btn_press0['raw'][TYPE_OFFSET]
    sink_queue = btn_press0['queue']
    button_value_offset = diff_offset(
        btn_press0['raw'], btn_release0['raw'], 'button value_offset')
    button_code_offset_check = diff_offset(
        btn_press0['raw'], btn_press1['raw'], 'button code_offset check')
    if button_code_offset_check != CODE_OFFSET:
        raise CalibrationError(
            'button code appears at byte offset 0x%02x on this build, not '
            'the expected 0x%02x' % (button_code_offset_check, CODE_OFFSET))

    # Encoder: ch0 delta+1 / delta+5 (value_offset), ch0 vs ch1 (code_offset
    # cross-check).
    pc, enc_ch0_d1 = _one_shot(
        m, pc, pits, state, lambda: panelin.encoder(m, profile, 0, 1),
        probe_cap, 'an encoder turn (channel 0, delta +1)')
    pc, enc_ch0_d5 = _one_shot(
        m, pc, pits, state, lambda: panelin.encoder(m, profile, 0, 5),
        probe_cap, 'an encoder turn (channel 0, delta +5)')
    pc, enc_ch1_d1 = _one_shot(
        m, pc, pits, state, lambda: panelin.encoder(m, profile, 1, 1),
        probe_cap, 'an encoder turn (channel 1, delta +1)')

    if enc_ch0_d1['ret'] != enc_ch0_d5['ret'] or \
            enc_ch0_d1['ret'] != enc_ch1_d1['ret']:
        raise CalibrationError(
            'encoder calibration probes were answered by different '
            'queue_send callers (0x%08x / 0x%08x / 0x%08x)' %
            (enc_ch0_d1['ret'], enc_ch0_d5['ret'], enc_ch1_d1['ret']))

    encoder_caller = enc_ch0_d1['ret']
    encoder_type = enc_ch0_d1['raw'][TYPE_OFFSET]
    encoder_value_offset = diff_offset(
        enc_ch0_d1['raw'], enc_ch0_d5['raw'], 'encoder value_offset')
    encoder_code_offset_check = diff_offset(
        enc_ch0_d1['raw'], enc_ch1_d1['raw'], 'encoder code_offset check')
    if encoder_code_offset_check != CODE_OFFSET:
        raise CalibrationError(
            'encoder code appears at byte offset 0x%02x on this build, not '
            'the expected 0x%02x' % (encoder_code_offset_check, CODE_OFFSET))

    if button_caller == encoder_caller:
        raise CalibrationError(
            'button and encoder injections were answered by the same '
            'queue_send caller (0x%08x) -- the record-shape/ignore-set '
            'filter is not separating them; refusing to guess' %
            button_caller)

    cal = {
        'button_caller': button_caller, 'button_type': button_type,
        'button_value_offset': button_value_offset, 'button_signed': False,
        'encoder_caller': encoder_caller, 'encoder_type': encoder_type,
        'encoder_value_offset': encoder_value_offset, 'encoder_signed': True,
        'sink_queue': sink_queue,
    }
    return pc, cal


# --- sweep -------------------------------------------------------------

class UnexpectedTracker:
    """Records seen during the sweep that match neither calibrated caller.

    On a build with the Digitakt-style reactive redraw/job-queue burst (see
    `probe_wait`), this can legitimately fire dozens of times per probe --
    every one of them correctly excluded from the button/encoder tables by
    `classify()`. Keeping every instance would bloat the report with
    thousands of duplicates of the same handful of (ret, raw) shapes, so
    this keeps a total count plus one example per distinct (ret, raw).
    """

    def __init__(self, cap=20):
        self.count = 0
        self.cap = cap
        self._seen = set()
        self.sample = []

    def note(self, records):
        for r in records:
            self.count += 1
            key = (r['ret'], r['raw'])
            if key not in self._seen and len(self.sample) < self.cap:
                self._seen.add(key)
                self.sample.append(r)


def classify(records, cal):
    """Split a probe's records into (button_recs, encoder_recs, other),
    each decoded to (code, value) with that type's calibrated offsets."""
    buttons_, encoders_, other = [], [], []
    for r in records:
        if r['ret'] == cal['button_caller']:
            code, value = decode(r['raw'], cal['button_value_offset'],
                                  cal['button_signed'])
            buttons_.append(dict(r, code=code, value=value))
        elif r['ret'] == cal['encoder_caller']:
            code, value = decode(r['raw'], cal['encoder_value_offset'],
                                  cal['encoder_signed'])
            encoders_.append(dict(r, code=code, value=value))
        else:
            other.append(r)
    return buttons_, encoders_, other


def sweep_buttons(m, pc, profile, pits, state, cal, lo, hi, probe_cap,
                   unexpected):
    rows = []
    probe_costs = []
    for channel in range(lo, hi + 1):
        for bit in BIT_RANGE:
            state['candidates'].clear()
            panelin.press(m, profile, channel, bit)
            pc = m.uc.reg_read(UC_M68K_REG_PC)
            pc, instrs_p, stop_p, recs_p = probe_wait(
                m, pc, pits, state, probe_cap, wanted_ret=cal['button_caller'])
            probe_costs.append(instrs_p)
            btn_p, enc_p, other_p = classify(recs_p, cal)
            unexpected.note(other_p)

            state['candidates'].clear()
            panelin.release(m, profile, channel, bit)
            pc = m.uc.reg_read(UC_M68K_REG_PC)
            pc, instrs_r, stop_r, recs_r = probe_wait(
                m, pc, pits, state, probe_cap, wanted_ret=cal['button_caller'])
            probe_costs.append(instrs_r)
            btn_r, enc_r, other_r = classify(recs_r, cal)
            unexpected.note(other_r)

            rows.append({
                'channel': channel, 'bit': bit,
                'press_got': bool(btn_p),
                'press_code': btn_p[0]['code'] if btn_p else None,
                'press_value': btn_p[0]['value'] if btn_p else None,
                'press_instrs': instrs_p,
                'release_got': bool(btn_r),
                'release_code': btn_r[0]['code'] if btn_r else None,
                'release_value': btn_r[0]['value'] if btn_r else None,
                'release_instrs': instrs_r,
                'press_extra': len(btn_p) > 1,
                'release_extra': len(btn_r) > 1,
            })

    # Diagnostic, not a fix: flag any code shared by more than one distinct
    # (channel, bit) group. Channels 0-5 never showed this on either build,
    # but a handful of Digitakt's channel-6 entries did -- the same
    # un-flushed-until-the-next-push behaviour confirmed for encoders (see
    # probe_wait's docstring) most likely also reaches some of channel 6's
    # buttons, which route through slower/special-function handling. There
    # is no known-good code formula for buttons the way encoders have
    # `channel+1`, so unlike sweep_encoders this cannot be corrected by
    # reassembling by code -- it is surfaced here instead of silently
    # presented as reliable.
    code_owners = collections.defaultdict(set)
    for row in rows:
        if row['press_code'] is not None:
            code_owners[row['press_code']].add((row['channel'], row['bit']))
        if row['release_code'] is not None:
            code_owners[row['release_code']].add((row['channel'], row['bit']))
    duplicate_codes = {code: sorted(owners)
                        for code, owners in code_owners.items()
                        if len(owners) > 1}
    return pc, rows, probe_costs, duplicate_codes


def sweep_encoders(m, pc, profile, pits, state, cal, lo, hi, probe_cap,
                    unexpected):
    """One turn per channel -- reassembled by CODE, not by which probe's
    window a record happened to land in.

    Digitakt can leave an encoder push un-flushed until the NEXT encoder
    push arrives, on any channel -- confirmed directly (see probe_wait's
    docstring): idling 20M instructions with nothing further injected
    produced nothing, but the very next push (a different channel) came
    back holding the STALE push's value instead of its own. So a probe for
    channel N can silently report channel N-1's answer, or nothing, if
    read naively per-probe. Since each channel's code (`channel + 1`,
    confirmed for channels 0 and 1 during calibration) is unique, this
    sidesteps the question entirely: every record seen across the WHOLE
    sweep is filed into `by_code` under its own decoded code regardless of
    which push caused it to surface, and each row is assembled afterwards
    by looking up `channel + 1` in that map. Extra trailing turns (bounded,
    same channel) are injected purely to flush out whatever is still
    pending once the main loop is done -- by induction on the single
    global slot this behaviour implies, one is enough to recover the very
    last channel pushed, and the bound just guards against needing a
    second if more than one stayed pending.
    """
    by_code = {}
    probe_costs = []

    def turn_and_file(channel):
        state['candidates'].clear()
        panelin.encoder(m, profile, channel, 1)
        pc_local = m.uc.reg_read(UC_M68K_REG_PC)
        pc_local, instrs, stop, recs = probe_wait(
            m, pc_local, pits, state, probe_cap,
            wanted_ret=cal['encoder_caller'])
        probe_costs.append(instrs)
        btn, enc, other = classify(recs, cal)
        unexpected.note(other)
        for rec in enc:
            by_code[rec['code']] = rec
        return pc_local, bool(enc)

    for channel in range(lo, hi + 1):
        pc, _ = turn_and_file(channel)

    expected_codes = {channel + 1 for channel in range(lo, hi + 1)}
    max_extra = hi - lo + 1
    for _ in range(max_extra):
        if expected_codes.issubset(by_code.keys()):
            break
        pc, got_any = turn_and_file(hi)
        if not got_any:
            break  # nothing at all came back; further flushing won't help

    rows = []
    for channel in range(lo, hi + 1):
        rec = by_code.get(channel + 1)
        rows.append({
            'channel': channel,
            'got': rec is not None,
            'code': rec['code'] if rec else None,
            'value_signed': rec['value'] if rec else None,
        })

    orphans = [{'code': code, 'value_signed': rec['value']}
               for code, rec in by_code.items() if code not in expected_codes]
    return pc, rows, probe_costs, orphans


def sign_check(m, pc, profile, pits, state, cal, channel, probe_cap):
    """Verify the encoder value field really is a signed delta, on one
    channel, with +1/+5/-1 -- the check that caught the sign question in
    the original Digitone exploration this tool formalises.

    Reassembled by VALUE, for the same reason `sweep_encoders` reassembles
    by code: every probe here targets the same channel, so the code field
    cannot distinguish which injection a delayed record belongs to (see
    probe_wait's docstring). The three test deltas are distinct from each
    other, though, so this collects every value seen across the whole
    sequence -- plus one trailing flush turn (delta +1 again) -- and
    reports a delta as confirmed if its exact value showed up anywhere,
    independent of which injection's window it happened to arrive in.
    """
    seen = {}
    for delta in (1, 5, -1, 1):   # last +1 is the trailing flush
        state['candidates'].clear()
        panelin.encoder(m, profile, channel, delta)
        pc = m.uc.reg_read(UC_M68K_REG_PC)
        pc, instrs, stop, recs = probe_wait(
            m, pc, pits, state, probe_cap, wanted_ret=cal['encoder_caller'])
        btn, enc, other = classify(recs, cal)
        for rec in enc:
            seen[rec['value']] = rec

    results = []
    for delta in (1, 5, -1):
        rec = seen.get(delta)
        results.append({
            'delta': delta, 'got': rec is not None,
            'code': rec['code'] if rec else None,
            'value_signed': rec['value'] if rec else None,
        })
    return pc, results


# --- reporting -----------------------------------------------------------

def print_button_table(rows, top):
    print('\n--- buttons (%d channel/bit groups) ---' % len(rows))
    print('%-4s %-4s  %-6s %-8s %-8s   %-6s %-8s %-8s' %
          ('ch', 'bit', 'p_got', 'p_code', 'p_value', 'r_got', 'r_code',
           'r_value'))
    for row in rows[:top]:
        print('%-4d %-4d  %-6s %-8s %-8s   %-6s %-8s %-8s' % (
            row['channel'], row['bit'],
            row['press_got'],
            '-' if row['press_code'] is None else '0x%02x' % row['press_code'],
            '-' if row['press_value'] is None else '0x%02x' % row['press_value'],
            row['release_got'],
            '-' if row['release_code'] is None else '0x%02x' % row['release_code'],
            '-' if row['release_value'] is None else '0x%02x' % row['release_value'],
        ))


def print_encoder_table(rows, top):
    print('\n--- encoders (%d channels) ---' % len(rows))
    print('%-4s %-6s %-8s %-12s' % ('ch', 'got', 'code', 'value_signed'))
    for row in rows[:top]:
        print('%-4d %-6s %-8s %-12s' % (
            row['channel'], row['got'],
            '-' if row['code'] is None else '0x%02x' % row['code'],
            '-' if row['value_signed'] is None else row['value_signed']))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--syx', required=True)
    ap.add_argument('--snapshot', required=True)
    ap.add_argument('--channels', default='0-6',
                     help='button channel range, inclusive, LO-HI (default 0-6)')
    ap.add_argument('--encoders', default='0-%d' % (panelin.ENCODERS - 1),
                     help='encoder channel range, inclusive, LO-HI '
                          '(default 0-%d)' % (panelin.ENCODERS - 1))
    ap.add_argument('--settle', type=int, default=40_000_000,
                     help='post-handover settle window, instructions, used '
                          'to build the queue_send ignore-set (default 40M)')
    ap.add_argument('--probe', type=int, default=8_000_000,
                     help='instruction cap per individual probe (default 8M)')
    ap.add_argument('--top', type=int, default=200,
                     help='max rows to print per table (default 200)')
    ap.add_argument('--json', help='write the full report here')
    ap.add_argument('--gui', action='store_true',
                    help='the snapshot is a gui.snap from emu.portable')
    args = ap.parse_args(argv)

    def parse_span(spec, label):
        lo_s, _, hi_s = spec.partition('-')
        if not hi_s:
            print('error: --%s must be LO-HI, e.g. 0-6, got %r' %
                  (label, spec), file=sys.stderr)
            sys.exit(2)
        return int(lo_s, 0), int(hi_s, 0)

    ch_lo, ch_hi = parse_span(args.channels, 'channels')
    en_lo, en_hi = parse_span(args.encoders, 'encoders')

    t_start = time.time()

    print('== loading image / resolving symbols for this build ==', flush=True)
    main_img, _sections_dir = load_main_image(args.syx)
    profile = symbols.resolve(main_img)
    if profile.unresolved:
        print('unresolved OPTIONAL symbols: %s' % sorted(profile.unresolved),
              flush=True)

    global QUEUE_SEND

    if getattr(profile, 'queue_send', None):

        if profile.queue_send != QUEUE_SEND:

            print('   queue_send: 0x%08x for this build (literal was '

                  '0x%08x)' % (profile.queue_send, QUEUE_SEND), flush=True)

        QUEUE_SEND = profile.queue_send


    print('== build() ==', flush=True)
    # deferred_components=('timers',) so a snapshot taken MID-RUN (one that
    # already reached the UI) can be opened at all: restore_into refuses a
    # checkpoint whose components the build did not declare. Constructing
    # fresh Dtims here instead would also disarm the DTIM3 the firmware set
    # up for itself -- see emu/uiresume.py.
    # Flags must MATCH the snapshot's build manifest or restore_into refuses
    # it outright. A mid-run UI snapshot taken through emu/uiresume.py is made
    # with longrun.build's defaults (unblock/softfloat/bitmap/dsp/slc all
    # False), and those defaults are also what that snapshot needs on the way
    # back in -- unblock in particular force-satisfies pends, which is a boot
    # aid and wrong once the UI is live.
    if args.gui:
        # A gui.snap from emu.portable's first run: the GUI's flags, with the
        # intro frame semaphore excepted, exactly as emu/bootstrap.py builds it.
        from emu import bootstrap as _bs
        _flags = dict(_bs.GUI_FLAGS, deferred_components=('timers',))
        if profile.frame_sem is not None:
            _flags['unblock_except'] = (profile.frame_sem,)
        m, ev, st, pc, inq, at = build(args.snapshot, syx=args.syx, **_flags)
    else:
        m, ev, st, pc, inq, at = build(
            args.snapshot, syx=args.syx, sdgate=True, esdhc=True,
            deferred_components=('timers',))

    intro = intro_running(m, profile.intro_pit3_isr)
    pits = ev['restore_checkpoint_timers']()
    if pits is None:
        pits = Timers(Pits(m, hold=intro), Dtims(m, channels=(3,), hold=intro))
        print('   timers: fresh', flush=True)
    else:
        print('   timers: restored saved cadence', flush=True)

    mark = collections.Counter()
    if intro and profile.intro_done is not None:
        def handover(uc, a, s_, d):
            pits.release()
            mark['intro_done'] += 1
        at(profile.intro_done, handover)

    qs_state = make_qs_state()
    install_qs_hook(m, at, qs_state)

    if intro:
        if profile.intro_done is None:
            print('FATAL: intro is live at restore but this build\'s '
                  'intro_done symbol did not resolve -- cannot detect '
                  'handover.', file=sys.stderr)
            return 2
        print('== intro is LIVE at restore; spinning UNCAPPED until '
              'intro_done fires (no instruction ceiling) ==', flush=True)
        t0 = time.time()
        pc, ok, stop = wait_for_handover(m, pc, pits, mark, profile)
        if not ok:
            print('FATAL: the intro never handed over -- emulator stopped '
                  '(stop=%r) before intro_done fired. This is not a timeout; '
                  'it is a real halt. Refusing to report a panel map that '
                  'may still be showing the intro.' % stop, file=sys.stderr)
            return 2
        print('intro handed over (%.1fs)' % (time.time() - t0), flush=True)
    else:
        print('intro already not live at restore', flush=True)

    print('== post-handover settle: %d instrs, building the queue_send '
          'ignore-set ==' % args.settle, flush=True)
    t0 = time.time()
    done = 0
    while done < args.settle:
        pc, executed, stop = spin(m, pc, 10_000_000, pits=pits)
        done += executed
        if stop != 'limit':
            print('FATAL: unexpected stop during settle: %r' % stop,
                  file=sys.stderr)
            return 2
    ignore_set = frozenset(qs_state['settle_keys'])
    qs_state['ignore'] = ignore_set
    qs_state['candidates'].clear()
    settle_wall = time.time() - t0
    print('settled: %d instrs, %.1fs, ignore-set has %d (caller,item) '
          'pair(s), %d total queue_send calls seen' %
          (done, settle_wall, len(ignore_set), qs_state['total_calls']),
          flush=True)

    print('== calibrating: identifying push call sites and record layout '
          'at runtime ==', flush=True)
    try:
        pc, cal = calibrate(m, pc, profile, pits, qs_state, args.probe)
    except CalibrationError as exc:
        print('FATAL: %s' % exc, file=sys.stderr)
        return 1
    print('  button caller  = 0x%08x  type=%d  value_offset=0x%02x' %
          (cal['button_caller'], cal['button_type'],
           cal['button_value_offset']), flush=True)
    print('  encoder caller = 0x%08x  type=%d  value_offset=0x%02x' %
          (cal['encoder_caller'], cal['encoder_type'],
           cal['encoder_value_offset']), flush=True)
    print('  sink queue     = 0x%08x  (informational only, never hardcoded)' %
          cal['sink_queue'], flush=True)

    unexpected = UnexpectedTracker()

    print('== sign check: encoder channel %d, deltas +1/+5/-1 ==' % en_lo,
          flush=True)
    pc, sign_rows = sign_check(m, pc, profile, pits, qs_state, cal, en_lo,
                                args.probe)
    for r in sign_rows:
        print('  delta=%+d got=%s code=%s value_signed=%s' %
              (r['delta'], r['got'],
               '-' if r['code'] is None else '0x%02x' % r['code'],
               r['value_signed']), flush=True)

    print('== full sweep: buttons ch%d-%d x bit0-7, encoders ch%d-%d ==' %
          (ch_lo, ch_hi, en_lo, en_hi), flush=True)
    t_sweep = time.time()
    pc, button_rows, btn_costs, duplicate_codes = sweep_buttons(
        m, pc, profile, pits, qs_state, cal, ch_lo, ch_hi, args.probe,
        unexpected)
    pc, encoder_rows, enc_costs, encoder_orphans = sweep_encoders(
        m, pc, profile, pits, qs_state, cal, en_lo, en_hi, args.probe,
        unexpected)
    sweep_wall = time.time() - t_sweep
    probe_costs = btn_costs + enc_costs
    print('sweep done: %.1fs over %d probes, avg %.0f instrs/probe' %
          (sweep_wall, len(probe_costs),
           sum(probe_costs) / max(len(probe_costs), 1)), flush=True)

    if unexpected.count:
        print('\n*** %d record(s) (%d distinct shape(s)) matched neither '
              'calibrated caller -- excluded from the tables below; this is '
              'expected on a build with reactive redraw/job-queue traffic, '
              'see the module docstring ***' %
              (unexpected.count, len(unexpected.sample)), flush=True)
        for r in unexpected.sample[:10]:
            print('  ret=0x%08x item=0x%08x raw=%s' %
                  (r['ret'], r['item'], r['raw'].hex()), flush=True)

    if duplicate_codes:
        print('\n*** %d button code(s) shared by more than one (channel, '
              'bit) -- see the module/sweep_buttons docstring; these rows '
              'are shown as captured but are NOT reliable ***' %
              len(duplicate_codes), flush=True)
        for code, owners in sorted(duplicate_codes.items()):
            print('  code=0x%02x: %s' % (code, owners), flush=True)

    if encoder_orphans:
        print('\n*** %d encoder record(s) with a code outside the expected '
              'channel+1 range -- see sweep_encoders docstring ***' %
              len(encoder_orphans), flush=True)
        for o in encoder_orphans:
            print('  code=0x%02x value_signed=%s' %
                  (o['code'], o['value_signed']), flush=True)

    print_button_table(button_rows, args.top)
    print_encoder_table(encoder_rows, args.top)

    total_wall = time.time() - t_start
    print('\nTOTAL WALL CLOCK: %.1fs (settle %.1fs + sweep %.1fs + calibration/other)'
          % (total_wall, settle_wall, sweep_wall), flush=True)

    report = {
        'syx': args.syx,
        'snapshot': args.snapshot,
        'intro_live_at_restore': bool(intro),
        'settle_instrs': done,
        'settle_wall_s': round(settle_wall, 1),
        'ignore_set_size': len(ignore_set),
        'total_queue_send_calls': qs_state['total_calls'],
        'calibration': {
            'button_caller': '0x%08x' % cal['button_caller'],
            'button_type': cal['button_type'],
            'button_value_offset': '0x%02x' % cal['button_value_offset'],
            'encoder_caller': '0x%08x' % cal['encoder_caller'],
            'encoder_type': cal['encoder_type'],
            'encoder_value_offset': '0x%02x' % cal['encoder_value_offset'],
            'sink_queue': '0x%08x' % cal['sink_queue'],
        },
        'sign_check': sign_rows,
        'unexpected_count': unexpected.count,
        'unexpected_sample': [
            {'ret': '0x%08x' % r['ret'], 'item': '0x%08x' % r['item'],
             'raw': r['raw'].hex()}
            for r in unexpected.sample],
        'channels': [ch_lo, ch_hi],
        'encoders': [en_lo, en_hi],
        'button_rows': button_rows,
        'button_duplicate_codes': {
            '0x%02x' % code: owners for code, owners in duplicate_codes.items()},
        'encoder_rows': encoder_rows,
        'encoder_orphans': encoder_orphans,
        'sweep_wall_s': round(sweep_wall, 1),
        'total_wall_s': round(total_wall, 1),
    }
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)) or '.',
                     exist_ok=True)
        with open(args.json, 'w') as fh:
            json.dump(report, fh, indent=2)
        print('wrote %s' % args.json, flush=True)

    return 0


if __name__ == '__main__':
    sys.exit(main())
