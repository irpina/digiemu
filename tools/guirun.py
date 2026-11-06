#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Headless reproduction of emu/gui.py's worker configuration.

emu/gui.py imports tkinter at module top and cannot run without a display.
This tool builds the exact same emulator configuration the GUI worker does
-- same build() flags, same fault_sink, same Timers/Pits/Dtims hold/release,
same intro handover, mainloop/job_pump/terminal hooks, same panel_diff latch
hook -- but headlessly, so a boot failure that only shows under the GUI can
be traced from a terminal. On top of that it adds arbitrary code hooks via
`--at ADDR[=NAME]` and, when the firmware lands in its terminal loop, dumps
the most recent hook hits plus a stack scan.

    uv run python tools/guirun.py [snapshot] --at 0x4011d67a=sd_bringup
    uv run python tools/guirun.py [snapshot] --stack-at 0x401d105c
    uv run python tools/guirun.py --patch-machine --input 150M:press:17 --input 160M:press:2 --input 160M:release:2 --png-at 170M:out/list.png

--patch-machine and --exact behave exactly as they do for emu/gui.py; see
its module docstring. `--input` replays panel clicks through the same
inbox, dwell pacing and `panelin.feed` the GUI uses; FUNC is code 17 and
SRC is 2 on Digitakt II.

`--feed WHEN:HEX` delivers raw panel bytes at an exact instruction count
with no dwell; `emu/gui.py` prints every feed it delivers in exactly that
form, so a GUI session can be replayed by pasting its `[gui] input --feed`
arguments.

`--dump-at ADDR:ARG:LEN[=NAME]` dumps LEN bytes from the pointer in stack
argument ARG (1, 2 or 3) on every hit.
`--watch ADDR:LEN[=NAME]` and `--watch-max N` install a memory write watch
over LEN bytes at ADDR, printing up to N hits with a stack scan each.
`--ips N` overrides the emulator's instructions-per-second timer rate
(accepts `18.72M`-style suffixes).
`--ips-at WHEN:N` changes the timer rate to N instructions per emulated
second at instruction count WHEN, e.g. after boot, so the boot itself is not
stretched.
`--post-intro-ips N` sets the timer rate applied when the intro hands over;
default 18720000 (4x INSTR_PER_SEC); 0 keeps the default rate; ignored when
--ips or --ips-at is given.
`--intro-timers pit3` drives an in-progress intro with its real PIT3 while
holding DTIM until handover.  The historical default, `held`, keeps every
timer held for exploratory runs that use semaphore unblocking.
`--trace-ui` prints the firmware UI path (UI queue sends/pops with wait,
key dispatch offers to views, view activate/close; see emu/uitrace.py) and
a window summary with each progress line.
`--trace-ui-verbose` also prints tick records and offers outside key
dispatch.
`--trace-tasks` charges emulated instructions to RTOS tasks at each context
switch and prints a per-task window summary with each progress line (see
emu/taskprof.py).
`--idle-yield N` raises the reschedule vector every N idle-spin passes
instead of 20000 (see emu/longrun.py build).
`--save-at WHEN:PATH` saves a snapshot (emu/snapshot.py, via
emu.checkpoint.save_longrun) at the first chunk boundary at or after
instruction count WHEN. It can be resumed with this tool or
emu.longrun.build. With --patch-machine the patch is already in the
saved memory, so do not pass --patch-machine again when resuming.
"""
import argparse
import collections
import json
import os
import struct
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))

from emu.longrun import build, spin
from emu.checkpoint import save_longrun
from emu.dtim import Dtims, Timers
from emu import config, panel, symbols, taskprof, uitrace
from emu import device as devices, panelin
from emu.pit import INSTR_PER_SEC, Pits, intro_running
from unicorn import UC_HOOK_BLOCK, UC_HOOK_MEM_WRITE
from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_PC
try:
    from machinepatch import patch_b, DEFAULT_CAVE_B, spec_from_arg, DEFAULT_SPEC
except ImportError:          # the public tree carries no firmware-modification tools
    patch_b = None

BUDGET = 400_000   # same as emu/gui.py


def parse_args(argv):
    p = argparse.ArgumentParser(
        description='Headless reproduction of emu/gui.py, for tracing boot '
                     'failures that only show under the GUI.')
    p.add_argument('snapshot', nargs='?', default='snapshots/boot400M.snap')
    p.add_argument(
        '--unblock', action=argparse.BooleanOptionalAction, default=True,
        help='force-satisfy blocked semaphore waits (default: enabled; use '
             '--no-unblock for fidelity runs)',
    )
    p.add_argument('--weakptr', action='store_true')
    p.add_argument('--slc', action='store_true')
    p.add_argument('--exact', action='store_true')
    p.add_argument(
        '--ssi0-request-hz', type=int,
        help='opt-in SSI0/eDMA48/50 request rate; no board-clock default exists',
    )
    p.add_argument(
        '--ssi0-upgrade-legacy', action='store_true',
        help='explicitly add fresh SSI0 state at this legacy checkpoint boundary',
    )
    p.add_argument('--syx')
    p.add_argument('--patch-machine', nargs='?',
                    const='list+dispatch+group+name+rank+permit'
                          '+hint+pertype+clone',
                    default=None)
    p.add_argument('--machine', default=None,
                    help='NAME:SHORT[:CLONE_OF[:POSITION]] for the new '
                         'machine (see machinepatch.MachineSpec); default '
                         'is Placeholder/PLC cloned from type 6')
    p.add_argument('--at', action='append', default=[])
    p.add_argument('--stack-at', action='append', default=[],
                    type=lambda s: int(s, 0))
    p.add_argument('--stack-depth', type=int, default=128)
    p.add_argument('--dump-at', action='append', default=[], type=parse_dump_at)
    p.add_argument('--watch', action='append', default=[], type=parse_watch)
    p.add_argument('--watch-max', type=int, default=16)
    p.add_argument('--ips', type=parse_when, default=None)
    p.add_argument('--ips-at', action='append', default=[], type=parse_ips_at)
    # Timer rate applied once the intro hands over to the OS. At the default
    # rate the UI task falls behind the 30 Hz DTIM3 tick (FINDINGS: "MACHINE
    # SEL closes itself"). 0 keeps the default rate. Ignored when --ips or
    # --ips-at is given.
    p.add_argument('--post-intro-ips', type=parse_when,
                   default=4 * INSTR_PER_SEC)
    p.add_argument(
        '--intro-timers', choices=('held', 'pit3', 'all'), default='held',
        help='while the intro is active: hold PITs (historical default), '
             'drive only PIT3, or drive all PIT channels; DTIM stays held '
             'until intro handover',
    )
    p.add_argument('--trace-ui', action='store_true')
    p.add_argument('--trace-ui-verbose', action='store_true')
    p.add_argument('--trace-tasks', action='store_true')
    p.add_argument('--idle-yield', type=int, default=None)
    p.add_argument('--limit', type=lambda s: int(s, 0), default=600_000_000)
    p.add_argument('--ring', type=int, default=64)
    p.add_argument('--input', action='append', default=[])
    p.add_argument('--feed', action='append', default=[])
    p.add_argument('--png-at', action='append', default=[])
    p.add_argument('--panel-raw-at', action='append', default=[])
    p.add_argument('--save-at', action='append', default=[])
    p.add_argument('--block-profile', help='PERTURBING block-entry JSON output')
    p.add_argument('--trace-ui-json')
    # In CHUNKS, kept as the replay contract for recorded sessions and
    # experiment recipes. emu/gui.py itself now dwells in emulated
    # milliseconds (PANEL_DWELL_MS) and exempts encoders.
    p.add_argument('--panel-dwell', type=int, default=16)
    return p.parse_args(argv)


def parse_when(s):
    if s and s[-1] in ('M', 'm'):
        return int(float(s[:-1]) * 1_000_000)
    return int(s, 0)


def restore_or_construct_timers(ev, construct, requested_ips=None):
    """Restore checkpoint cadence rather than constructing over it."""
    timers = ev['restore_checkpoint_timers']()
    if timers is not None:
        if requested_ips is not None and any(
                source.ips != requested_ips for source in timers.sources):
            raise RuntimeError('--ips conflicts with checkpoint timer rate')
        return timers, True
    timers = construct()
    ev['checkpoint_components']['timers'] = timers
    return timers, False


def run_timer_clock(timers, origin):
    """Return live timer time relative to the start of this resumed run."""
    return timers.now - origin


def release_intro_timers(timers):
    """Release intro holds and restore the complete post-intro PIT topology."""
    pit_source = timers.sources[0]
    if tuple(pit_source.channels) == (3,):
        pit_source.channels = (3, 2, 0)
    timers.release()


def construct_timers(machine, args, intro):
    """Construct the requested pre/post-intro timer topology."""
    pit_channels = ((3,) if intro and args.intro_timers == 'pit3'
                    else (3, 2, 0))
    pit_hold = intro and args.intro_timers == 'held'
    dtim_hold = intro
    if args.ips is not None:
        return Timers(
            Pits(machine, channels=pit_channels, hold=pit_hold,
                 instr_per_sec=args.ips),
            Dtims(machine, channels=(3,), hold=dtim_hold,
                  instr_per_sec=args.ips),
        )
    return Timers(
        Pits(machine, channels=pit_channels, hold=pit_hold),
        Dtims(machine, channels=(3,), hold=dtim_hold),
    )


def parse_input(spec):
    when_str, kind, code_str = spec.split(':', 2)
    return parse_when(when_str), kind, int(code_str, 0)


def parse_feed(spec):
    when_str, hex_str = spec.split(':', 1)
    return parse_when(when_str), bytes.fromhex(hex_str)


def parse_ips_at(spec):
    if ':' not in spec:
        raise argparse.ArgumentTypeError(
            '--ips-at expects WHEN:N, got %r' % spec)
    when_str, n_str = spec.split(':', 1)
    return parse_when(when_str), parse_when(n_str)


def parse_png_at(spec):
    when_str, path = spec.split(':', 1)
    return parse_when(when_str), path


def parse_save_at(spec):
    when_str, path = spec.split(':', 1)
    return parse_when(when_str), path


def parse_panel_raw_at(spec):
    when_str, path = spec.split(':', 1)
    return parse_when(when_str), path


def write_block_profile(path, entries):
    """Write an explicitly perturbing, address-sorted block-entry profile."""
    with open(path, 'w') as f:
        json.dump({
            'perturbing': True,
            'kind': 'basic-block entries',
            'entries': [
                {'address': address, 'hits': entries[address]}
                for address in sorted(entries)
            ],
        }, f, sort_keys=True)


def parse_patch_machine(value):
    if ':' in value:
        parts_str, eighth_str = value.split(':', 1)
        eighth = int(eighth_str, 0)
    else:
        parts_str = value
        eighth = 7
    return tuple(parts_str.split('+')), eighth


def parse_at(spec):
    if '=' in spec:
        addr_str, name = spec.split('=', 1)
    else:
        addr_str, name = spec, None
    addr = int(addr_str, 0)
    if name is None:
        name = '0x%08x' % addr
    return addr, name


def parse_dump_at(spec):
    if '=' in spec:
        rest, name = spec.split('=', 1)
    else:
        rest, name = spec, None
    parts = rest.split(':')
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            '--dump-at expects ADDR:ARG:LEN[=NAME], got %r' % spec)
    addr_str, arg_str, len_str = parts
    addr = int(addr_str, 0)
    arg = int(arg_str, 0)
    if arg not in (1, 2, 3):
        raise argparse.ArgumentTypeError(
            '--dump-at ARG must be 1, 2 or 3, got %r' % arg_str)
    length = int(len_str, 0)
    if not 1 <= length <= 256:
        raise argparse.ArgumentTypeError(
            '--dump-at LEN must be 1..256, got %r' % len_str)
    if name is None:
        name = '0x%08x' % addr
    return addr, arg, length, name


def parse_watch(spec):
    if '=' in spec:
        rest, name = spec.split('=', 1)
    else:
        rest, name = spec, None
    addr_str, len_str = rest.split(':', 1)
    addr = int(addr_str, 0)
    length = int(len_str, 0)
    if not 1 <= length <= 0x1000:
        raise argparse.ArgumentTypeError(
            '--watch LEN must be 1..0x1000, got %r' % len_str)
    if name is None:
        name = '0x%08x' % addr
    return addr, length, name


def stack_scan(uc, depth):
    """-> (a7, [(offset, word), ...]) for longwords above A7 that fall in the
    MAIN OS code span."""
    a7 = uc.reg_read(UC_M68K_REG_A7)
    found = []
    for i in range(depth):
        offset = i * 4
        try:
            word = struct.unpack('>I', uc.mem_read(a7 + offset, 4))[0]
        except Exception:
            break
        if 0x40000400 <= word <= 0x40307f60:
            found.append((offset, word))
    return a7, found


def main():
    args = parse_args(sys.argv[1:])
    if args.ssi0_upgrade_legacy and args.ssi0_request_hz is None:
        raise SystemExit('--ssi0-upgrade-legacy requires --ssi0-request-hz')
    fast = not args.exact

    extra = {'syx': args.syx} if args.syx else {}
    if args.idle_yield is not None:
        extra['idle_yield'] = args.idle_yield
        print('[guirun] idle-yield %d' % args.idle_yield)
    m, ev, st, pc, inq, at = build(args.snapshot, unblock=args.unblock,
                                    softfloat=True,
                                    bitmap=True, dsp=True, on_pixel=None,
                                    weakptr=args.weakptr, slc=args.slc,
                                    deferred_components=('timers',),
                                    ssi0_request_hz=args.ssi0_request_hz,
                                    ssi0_legacy_upgrade=args.ssi0_upgrade_legacy,
                                    **extra)

    if args.patch_machine is not None:
        if patch_b is None:
            print('[guirun] --patch-machine: machine patching is not available '
                  'in this tree')
            sys.exit(2)
        parts, eighth = parse_patch_machine(args.patch_machine)
        try:
            spec = spec_from_arg(args.machine) if args.machine else DEFAULT_SPEC
            diffs = patch_b(m, DEFAULT_CAVE_B, parts=parts, eighth=eighth, spec=spec)
        except SystemExit as exc:
            print('[guirun] machine patch refused: %s' % exc)
            sys.exit(2)
        print('[guirun] patched: %s (8th=%d, machine=%s/%s)'
              % ('+'.join(parts), eighth, spec.name, spec.short))
        if diffs:
            for line in diffs:
                print(line)

    main_img = open(config.main_image(), 'rb').read()
    profile = symbols.resolve(main_img)

    device = None
    if args.input:
        try:
            device, _fw = devices.identify(config.firmware(args.syx))
            held = panelin.Held(device)
        except Exception as exc:                        # noqa: BLE001
            print('[guirun] cannot identify device for --input: %s' % exc)
            sys.exit(2)
    else:
        held = None

    inbox = collections.deque()
    chunks_since_delivery = 0
    delivered_before = False
    pending_inputs = [parse_input(spec) for spec in args.input]

    faulted_pages = set()

    def fault_sink(rec):
        # Called from inside a Unicorn hook: no locks, no guest memory
        # access, and nothing may raise into the run -- same as gui.py.
        try:
            page = rec['page']
            if page in faulted_pages:
                return
            faulted_pages.add(page)
            kinds = '+'.join(sorted(rec['kinds'])) if rec['kinds'] else '?'
            print('[guirun] FAULT page=0x%08x first=0x%08x pc=0x%08x %s'
                  % (page, rec['first_addr'], rec['first_pc'], kinds),
                  flush=True)
        except Exception:
            pass
    m.fault_sink = fault_sink

    if args.ips is not None:
        print('[guirun] ips %d' % args.ips)

    intro = intro_running(m, profile.intro_pit3_isr)

    pits, restored_timers = restore_or_construct_timers(
        ev, lambda: construct_timers(m, args, intro), args.ips)
    ssi0 = ev.get('ssi0_dma')
    # Panel input waits for the CPU to take its interrupt, as in the GUI.
    panel_in = panelin.PanelIn()
    panel_in.attach(m, profile)
    events = ((ssi0,) if ssi0 is not None else ()) + (panel_in,)
    if ssi0 is not None:
        if ssi0._checkpoint_restored and ssi0.now != pits.now:
            raise RuntimeError('SSI0 and timer checkpoint clocks disagree')
        timer_ips = pits.sources[0].ips
        if ssi0._checkpoint_restored and ssi0.ips != timer_ips:
            raise RuntimeError('SSI0 and timer checkpoint instruction rates disagree')
        if not ssi0._checkpoint_restored:
            ssi0.ips = timer_ips
        ssi0.align(pits.now)
        print('[guirun] SSI0 requests %d Hz%s'
              % (ssi0.request_hz,
                 ' (fresh legacy upgrade)' if args.ssi0_upgrade_legacy else ''))
    timer_origin = pits.now
    post_intro_ips = (0 if restored_timers else args.post_intro_ips
                      if args.ips is None and not args.ips_at else 0)
    print('[guirun] timer rate %d, after intro %s'
          % (pits.sources[0].ips, post_intro_ips or 'unchanged'))
    if intro:
        print('[guirun] intro timers %s' % args.intro_timers)
    if intro and profile.intro_done is not None:
        def handover(uc, a, s_, d):
            # A restored PIT3-only checkpoint retains channels=(3,) even if
            # this invocation uses the default CLI mode. Derive handover from
            # live checkpoint topology, not from today's command line.
            release_intro_timers(pits)
            print('[guirun] intro handover at %dM' % (state['instrs'] // 1_000_000))
            if post_intro_ips:
                # Applied by the main loop at the next chunk boundary, the
                # same way as --ips-at.
                pending_ips.append((state['instrs'], post_intro_ips))
        at(profile.intro_done, handover)

    state = {'instrs': 0, 'mainloop': 0, 'jobs': 0, 'terminal': False, 'seq': 0}
    if profile.mainloop is not None:
        at(profile.mainloop, lambda uc, a, s, d: state.__setitem__(
            'mainloop', state['mainloop'] + 1))
    if profile.job_pump is not None:
        at(profile.job_pump, lambda uc, a, s, d: state.__setitem__(
            'jobs', state['jobs'] + 1))

    def terminal_hit(uc, a, s, d):
        if not state['terminal']:
            state['terminal'] = True
            print('[guirun] TERMINAL LOOP reached at ~%dM' % (state['instrs'] // 1_000_000))
    at(0x4012d2fa, terminal_hit)

    def drain_input(pc):
        """Queue panel input at a chunk boundary for panel_in. -> the PC.

        Ported line for line from emu/gui.py's Emulator._drain_input; see its
        docstring there for the pacing rationale.
        """
        nonlocal chunks_since_delivery, delivered_before
        if held is None:
            return pc
        assert device is not None
        paced = args.panel_dwell > 0
        if paced and delivered_before and (
                chunks_since_delivery < args.panel_dwell):
            chunks_since_delivery += 1
            return pc
        out = bytearray()
        took_button = False
        deferred = []
        while inbox:
            kind, code, arg = inbox.popleft()
            if kind == 'encoder':
                channel = device.encoder_channel(code)
                if channel is not None:
                    out += panelin.encode_encoder(channel, arg)
            elif paced and took_button:
                deferred.append((kind, code, arg))
            else:
                took_button = True
                if kind == 'press':
                    pos = held.press(code)
                    if pos is not None:
                        out += panelin.encode_buttons(*pos)
                elif kind == 'release':
                    pos = held.release(code)
                    if pos is not None:
                        out += panelin.encode_buttons(*pos)
                elif kind == 'release_all':
                    for pos in held.release_all():
                        out += panelin.encode_buttons(*pos)
        for item in reversed(deferred):
            inbox.appendleft(item)
        if not out:
            return pc
        chunks_since_delivery = 0
        delivered_before = True
        panel_in.put(bytes(out))
        print('[guirun] input ~%.1fM: %s' % (state['instrs'] / 1e6, bytes(out).hex()))
        return pc

    latched: dict[str, Any] = {'buf': None, 'clock': None}
    if profile.panel_diff is not None and profile.fb_front is not None:
        # Matches the GUI's panel_diff hook, and feeds --png-at: this stores
        # the untorn frame instead of discarding it.
        def latch_frame(uc, a, s_, d):
            buf = panel.read(m, profile.fb_front)
            if buf is not None:
                latched['buf'] = buf
                # Live in-spin timer time, normalized to this resumed run.
                latched['clock'] = run_timer_clock(pits, timer_origin)
        at(profile.panel_diff, latch_frame)

    pending_pngs = [parse_png_at(spec) for spec in args.png_at]
    pending_raw_panels = [parse_panel_raw_at(spec)
                          for spec in args.panel_raw_at]
    pending_saves = [parse_save_at(spec) for spec in args.save_at]
    pending_feeds = [parse_feed(s) for s in args.feed]

    at_targets = [parse_at(spec) for spec in args.at]
    hit_counts = collections.Counter()
    hits = collections.deque(maxlen=args.ring)

    def make_at_hook(addr, name):
        def hook(uc, a, s, d):
            hit_counts[addr] += 1
            a7 = uc.reg_read(UC_M68K_REG_A7)

            def read_long(offset):
                try:
                    return struct.unpack('>I', uc.mem_read(a7 + offset, 4))[0]
                except Exception:
                    return None
            ret = read_long(0)
            arg1 = read_long(4)
            arg2 = read_long(8)
            arg3 = read_long(12)
            # Does NOT read SR -- reg_read(SR) between emu_start calls
            # clobbers condition codes and has deadlocked a guest mutex
            # before. See emu/gui.py's _stack_backtrace docstring.
            hits.append((state['seq'], state['instrs'], addr, name, a7, ret,
                         arg1, arg2, arg3))
            state['seq'] += 1
        return hook

    for addr, name in at_targets:
        at(addr, make_at_hook(addr, name))

    stack_scans = {}

    def make_stack_at_hook(addr):
        def hook(uc, a, s, d):
            if addr in stack_scans:
                return
            stack_scans[addr] = (state['seq'], state['instrs'],
                                  stack_scan(uc, args.stack_depth))
        return hook

    for addr in args.stack_at:
        at(addr, make_stack_at_hook(addr))

    def print_stack_lines(found):
        for offset, word in found:
            print('  +0x%03x  0x%08x' % (offset, word))

    dump_hit_counts = collections.Counter()

    def make_dump_hook(addr, arg, length, name):
        arg_offset = arg * 4
        def hook(uc, a, s, d):
            dump_hit_counts[addr] += 1
            a7 = uc.reg_read(UC_M68K_REG_A7)
            try:
                ret = struct.unpack('>I', uc.mem_read(a7, 4))[0]
                ptr = struct.unpack('>I', uc.mem_read(a7 + arg_offset, 4))[0]
            except Exception:
                print('[dump] %d %s unreadable' % (state['instrs'], name),
                      flush=True)
                return
            try:
                data = uc.mem_read(ptr, length)
                groups = []
                for i in range(0, len(data), 4):
                    groups.append(' '.join('%02x' % b for b in data[i:i + 4]))
                hexbytes = '  '.join(groups)
            except Exception:
                hexbytes = 'unreadable'
            print('[dump] %d %s ret=0x%08x arg%d=0x%08x %s'
                  % (state['instrs'], name, ret, arg, ptr, hexbytes),
                  flush=True)
        return hook

    for addr, arg, length, name in args.dump_at:
        at(addr, make_dump_hook(addr, arg, length, name))

    watch_targets = args.watch
    watch_hit_counts = collections.Counter()
    watch_seen = collections.Counter()

    def make_watch_hook(addr, name):
        def hook(uc, access, address, size, value, user_data):
            watch_hit_counts[addr] += 1
            if watch_seen[addr] >= args.watch_max:
                return
            watch_seen[addr] += 1
            pc = uc.reg_read(UC_M68K_REG_PC)
            print('[watch] %d %s addr=0x%08x size=%d value=0x%x pc=0x%08x'
                  % (state['instrs'], name, address, size, value, pc),
                  flush=True)
            a7, found = stack_scan(uc, args.stack_depth)
            print_stack_lines(found)
        return hook

    for addr, length, name in watch_targets:
        m.uc.hook_add(UC_HOOK_MEM_WRITE, make_watch_hook(addr, name),
                       begin=addr, end=addr + length - 1)

    def clock():
        stepper = getattr(m, '_fast_stepper_obj', None)
        if fast and stepper is not None:
            return pits.now + int(stepper.blocks * stepper.PER_BLOCK)
        return pits.now

    button_names = {}

    def button_name(code):
        if code in button_names:
            return button_names[code]
        try:
            name = panelin.control_name(m, profile, code)
        except Exception:
            name = None
        button_names[code] = name
        return name

    ui_events = []

    def ui_out(line):
        ui_events.append(line)
        print(line)

    if args.trace_ui or args.trace_ui_verbose or args.trace_ui_json:
        trace = uitrace.UiTrace(m, at, profile, clock, out=ui_out,
                                 verbose=args.trace_ui_verbose,
                                 button_name=button_name)
        if trace.missing:
            print(trace.summary())
    else:
        trace = None

    block_entries = collections.Counter()
    if args.block_profile:
        m.uc.hook_add(
            UC_HOOK_BLOCK,
            lambda uc, address, size, data: block_entries.update([address]))

    task_prof = taskprof.TaskProfile(
        m, at, profile, clock, ev['tasks'], spins=ev.get('idle_spins')
    ) if args.trace_tasks else None
    if task_prof is not None and task_prof.missing:
        for line in task_prof.summary(pits.now):
            print(line)

    pending_ips = sorted(args.ips_at)
    if post_intro_ips and not intro:
        pending_ips.append((0, post_intro_ips))  # snapshot is past the intro

    prev = collections.Counter(ev['satisfied_by'])
    reported = -1
    wall_t0 = time.monotonic()
    wall_prev = (wall_t0, 0)
    while state['instrs'] < args.limit:
        due_ips, pending_ips[:] = (
            [e for e in pending_ips if e[0] <= state['instrs']],
            [e for e in pending_ips if e[0] > state['instrs']])
        for when, n in due_ips:
            for source in pits.sources:
                source.ips = n
            if ssi0 is not None:
                ssi0.ips = n
            print('[guirun] ips -> %d at %d' % (n, state['instrs']))
        due_feeds, pending_feeds[:] = (
            [e for e in pending_feeds if e[0] <= state['instrs']],
            [e for e in pending_feeds if e[0] > state['instrs']])
        for when, data in due_feeds:
            panel_in.put(data)
            print('[guirun] input --feed %d:%s (asked %d)'
                  % (state['instrs'], data.hex(), when))
        ready, pending_inputs[:] = (
            [e for e in pending_inputs if e[0] <= state['instrs']],
            [e for e in pending_inputs if e[0] > state['instrs']])
        for when, kind, code in ready:
            inbox.append((kind, code, 0))
        pc = drain_input(pc)
        pc, executed, stop = spin(
            m, pc, BUDGET, pits=pits, fast=fast,
            async_events=events,
        )
        state['instrs'] += executed
        due, pending_pngs[:] = (
            [e for e in pending_pngs if e[0] <= state['instrs']],
            [e for e in pending_pngs if e[0] > state['instrs']])
        for when, path in due:
            if latched['buf'] is None:
                print('[guirun] png ~%dM: no latched frame yet (%s not written)'
                      % (when // 1_000_000, path))
            else:
                panel.write_png(latched['buf'], path)
                print('[guirun] png ~%dM -> %s' % (when // 1_000_000, path))
        due, pending_raw_panels[:] = (
            [e for e in pending_raw_panels if e[0] <= state['instrs']],
            [e for e in pending_raw_panels if e[0] > state['instrs']])
        for when, path in due:
            if latched['buf'] is None:
                print('[guirun] panel raw asked %d: no latched frame '%
                      when + '(%s not written)' % path, flush=True)
            else:
                with open(path, 'wb') as raw:
                    raw.write(latched['buf'])
                print('[guirun] panel raw asked %d latched %d -> %s'
                      % (when, latched['clock'], path), flush=True)
        due, pending_saves[:] = ([e for e in pending_saves if e[0] <= state['instrs']],
                                 [e for e in pending_saves if e[0] > state['instrs']])
        for when, path in due:
            save_longrun(m, ev, pits, path, extra={'instrs': state['instrs']})
            print('saved snapshot at %d instrs -> %s' % (state['instrs'], path), flush=True)
        if stop != 'limit':
            print('[guirun] HALTED: %s at pc=0x%08x' % (stop, pc))
            break
        step = state['instrs'] // 20_000_000
        if step != reported:
            reported = step
            now = time.monotonic()
            rate = ((state['instrs'] - wall_prev[1])
                    / max(1e-6, now - wall_prev[0]))
            wall_prev = (now, state['instrs'])
            print('[guirun] %dM tasks=%d dtim3=%d mainloop=%d jobs=%d pc=0x%08x'
                  ' wall=%.1fs rate=%.2fM/s real=%.0f%%'
                  % (state['instrs'] // 1_000_000, len(ev['tasks']),
                     pits.fired.get('DTIM3', 0), state['mainloop'],
                     state['jobs'], pc, now - wall_t0, rate / 1e6,
                     100.0 * rate / pits.sources[0].ips))
            if trace is not None:
                print(trace.summary())
            if task_prof is not None:
                for line in task_prof.summary(pits.now):
                    print(line)
                delta = ev['satisfied_by'] - prev
                if delta:
                    print('[guirun] satisfied window: %s' % ', '.join(
                        'ret=0x%08x*%d' % (ret, n)
                        for ret, n in delta.most_common(5)))
                prev = collections.Counter(ev['satisfied_by'])
        if state['terminal']:
            pc, executed, stop = spin(
                m, pc, BUDGET, pits=pits, fast=fast,
                async_events=events,
            )
            state['instrs'] += executed
            break

    print('[guirun] end: instrs=%dM terminal=%s tasks=%d dtim3=%d mainloop=%d '
          'jobs=%d pc=0x%08x'
          % (state['instrs'] // 1_000_000, state['terminal'], len(ev['tasks']),
             pits.fired.get('DTIM3', 0), state['mainloop'], state['jobs'], pc))
    wall = time.monotonic() - wall_t0
    print('[guirun] wall: %.1fs, %.2fM instr/s average'
          % (wall, state['instrs'] / max(1e-6, wall) / 1e6))
    if trace is not None:
        print(trace.summary())
    if task_prof is not None:
        for line in task_prof.summary(pits.now):
            print(line)
    if ev.get('satisfied_by'):
        print('[guirun] pends force-satisfied by unblock: %d' % ev['satisfied'])
        for ret, n in ev['satisfied_by'].most_common(15):
            print('  ret=0x%08x  %d' % (ret, n))
    print('[guirun] faults: %d distinct pages touched' % len(m.fault_pages))
    if args.trace_ui_json:
        with open(args.trace_ui_json, 'w') as f:
            json.dump({
                'kind': 'scoped dynamic call/view evidence',
                'events': ui_events,
            }, f, sort_keys=True)
    if args.block_profile:
        write_block_profile(args.block_profile, block_entries)

    if at_targets or args.dump_at or watch_targets:
        for addr, name in at_targets:
            print('  0x%08x  %s  hits=%d' % (addr, name, hit_counts[addr]))
        for addr, arg, length, name in args.dump_at:
            print('  0x%08x  %s  hits=%d' % (addr, name, dump_hit_counts[addr]))
        for addr, length, name in watch_targets:
            print('  0x%08x  %s  hits=%d' % (addr, name, watch_hit_counts[addr]))

    if hits:
        print('[guirun] last %d hook hits (oldest first):' % len(hits))
        for seq, instrs, addr, name, a7, ret, arg1, arg2, arg3 in hits:
            def fmt(v):
                return '0x%08x' % v if v is not None else '--------'
            print('  #%d ~%dM  %s  a7=0x%08x ret=%s args=%s %s %s'
                  % (seq, instrs // 1_000_000, name, a7, fmt(ret), fmt(arg1),
                     fmt(arg2), fmt(arg3)))

    for addr in args.stack_at:
        if addr in stack_scans:
            seq, instrs, (a7, found) = stack_scans[addr]
            print('[guirun] stack at first hit of 0x%08x (~%dM, after hook '
                  'seq #%d, A7=0x%08x):'
                  % (addr, instrs // 1_000_000, seq, a7))
            print_stack_lines(found)
        else:
            print('[guirun] 0x%08x never reached; no stack scan' % addr)

    if state['terminal']:
        a7, found = stack_scan(m.uc, 64)
        print('[guirun] stack at terminal loop (A7=0x%08x):' % a7)
        print_stack_lines(found)


if __name__ == '__main__':
    main()
