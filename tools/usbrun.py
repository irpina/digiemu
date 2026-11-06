#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Run a Digitone (mk1) snapshot headless, with its USB MIDI port on a TCP link.

The window (emu/dnpanel.py) needs Tk; this needs nothing but the emulator, so
it runs where the window cannot -- under WSL, for one -- and a host program
reaches the firmware's USB MIDI port through the link (emu/usbmidi.py):
tools/usbmidi_relay.py on the host joins the link to two MIDI ports there,
and elkflash, a SysEx librarian or a screenshot tool talks to those as it
would to a unit.

It builds the machine the way the window's worker does (emu/gui.py
Emulator._run: the same build() flags, saved timer cadence, intro handover
and post-intro timer rate) and paces it to real time. Audio is not played; a
snapshot that carries the audio model is opened with it, muted.

    uv run python tools/usbrun.py SNAPSHOT [--port 7311] [--seconds N]
                                  [--save-on-exit PATH] [--no-realtime]
                                  [--firmware SYX|DIR]

An OS upgrade sent over the link runs as on the unit (emu/osupgrade.py): the
OS writes the new container into the emulated SPI flash and resets, and the
runner then boots that flash through the bootstrap and the updater, the
cold boot, intro and settle, in a firmware folder of its own (about a
minute), and carries on from there with the same card. The link stays up
throughout; the port answers again once the new OS has settled.

The DT2_* variables pick the firmware, as for every stage (emu/bootstrap.py
FirmwarePaths.env()). Ctrl-C stops it cleanly: the card is flushed and, with
--save-on-exit, the session saved.
"""
import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import config, device as devices, obaudio, osupgrade, panel, symbols, usbmidi
from emu.dtim import Dtims, Timers
from emu.longrun import build, never_fake_set_posts, spin
from emu.pit import INSTR_PER_SEC, Pits, intro_running
from emu.snapshot import _SnapshotUnpickler, save as save_snapshot

BUDGET = 400_000


def saved_manifest(path):
    """The snapshot's build manifest ({} when unreadable): which models it
    was saved with, and so must be opened with (emu/gui.py saved_ssi0)."""
    try:
        with open(path, 'rb') as fh:
            blob = _SnapshotUnpickler(fh).load()
        return dict(blob.get('manifest') or {})
    except Exception:                                   # noqa: BLE001
        return {}


def accelerated():
    """-> True when this Unicorn has the digikit accelerators (as emu/gui.py's
    _accelerated, which this cannot import: the window needs Tk)."""
    try:
        from emu import native
        from emu.harness import native_ff1
        from unicorn import UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, Uc
        probe = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        return bool(native.budget_available(probe)
                    and native.edma_available(probe) and native_ff1())
    except Exception:                                   # noqa: BLE001
        return False


def open_machine(snapshot, syx=None, audio=False):
    """-> (m, ev, st, pc, at, profile, device): built as the window builds.
    `audio`: build the audio model even when the snapshot was saved without
    it (the window's legacy upgrade), so the audio block interrupt runs."""
    dev = None
    try:
        dev, _fw = devices.identify(config.firmware(syx))
    except Exception:                                   # noqa: BLE001
        pass
    intro_except = ()
    main_img = open(config.main_image(), 'rb').read()
    profile = symbols.resolve(main_img)
    if dev is not None and not dev.intro_unblocks_frame_sem \
            and profile.frame_sem is not None:
        intro_except = (profile.frame_sem,)
    kw = {'syx': syx} if syx else {}
    manifest = saved_manifest(snapshot)
    saved = manifest.get('ssi0_dma')
    cfg = getattr(dev, 'audio', None)
    if cfg and isinstance(saved, dict) and isinstance(saved.get('request_hz'),
                                                     int):
        kw.update(ssi0_request_hz=saved['request_hz'],
                  ssi0_profile=cfg['ssi_profile'])
    elif cfg and audio:
        hz = cfg['request_hz']
        if not accelerated() and cfg['fallback_request_hz']:
            hz = cfg['fallback_request_hz']
        kw.update(ssi0_request_hz=hz, ssi0_legacy_upgrade=True,
                  ssi0_profile=cfg['ssi_profile'])
    # A boot through the bootstrap (a reboot after an OS upgrade) runs on
    # the aliased DDR model, and its snapshots must reopen on it.
    if isinstance(manifest.get('ddr'), int):
        kw['ddr'] = manifest['ddr']
    m, ev, st, pc, _inq, at = build(
        snapshot, unblock=True, softfloat=True, bitmap=True, dsp=True,
        unblock_except=intro_except, deferred_components=('timers',),
        fast_idle=True, **kw)
    never_fake_set_posts(m, ev, profile.set_post)
    return m, ev, st, pc, at, profile, dev


def timers_for(m, ev, profile, dev):
    pits = None
    restore = ev.get('restore_checkpoint_timers')
    if restore is not None:
        try:
            pits = restore()
        except Exception as exc:                        # noqa: BLE001
            print('[usbrun] saved timer cadence unusable (%s)' % exc, flush=True)
    intro = intro_running(m, profile.intro_pit3_isr)
    if pits is None:
        chans = tuple(getattr(dev, 'intro_channels', ()) or ())
        pit = (Pits(m, channels=chans, hold=False) if (intro and chans)
               else Pits(m, hold=intro))
        pits = Timers(pit, Dtims(m, channels=(3,), hold=intro))
    return pits, intro


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('snapshot')
    ap.add_argument('--syx')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=usbmidi.DEFAULT_PORT)
    ap.add_argument('--seconds', type=float, default=0,
                    help='stop after this much emulated time (0: run until Ctrl-C)')
    ap.add_argument('--save-on-exit')
    ap.add_argument('--no-realtime', action='store_true',
                    help='run as fast as the host can instead of pacing to real time')
    ap.add_argument('--report', type=float, default=10.0,
                    help='seconds between status lines')
    ap.add_argument('--png', metavar='PATH',
                    help='write the panel framebuffer here with each status line')
    ap.add_argument('--firmware', action='append', default=[], metavar='SYX|DIR',
                    help='OS files an upgrade may install: a reboot needs the '
                         'file the flash was written from (the running one is '
                         'always a candidate)')
    ap.add_argument('--workdir', metavar='DIR',
                    help='where a reboot builds its firmware folder (default: '
                         'reboots/N beside the running firmware folder)')
    ap.add_argument('--no-reboot', action='store_true',
                    help='stop when the OS resets instead of booting the flash')
    ap.add_argument('--flash-out', metavar='PATH',
                    help='when the OS resets, write the flash here too')
    ap.add_argument('--ob-audio-port', type=int, metavar='PORT',
                    help='also serve the twelve Overbridge audio channels on this TCP '
                         'port: 48 kHz frames of 12 big-endian int32 (emu/obaudio.py)')
    args = ap.parse_args(argv)

    current = {'usb': None}

    def put(data):
        usb = current['usb']
        if usb is not None:         # between power-ons the port is gone
            usb.put(data)
    link = usbmidi.LinkServer(put, host=args.host, port=args.port)
    if args.ob_audio_port is not None:
        current['ob_link'] = usbmidi.LinkServer(lambda data: None, host=args.host,
                                                port=args.ob_audio_port)
        print('[usbrun] Overbridge audio on %s:%d' % (args.host, current['ob_link'].port),
              flush=True)
    print('[usbrun] USB MIDI link on %s:%d' % (args.host, link.port), flush=True)
    stop = {'now': False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__('now', True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__('now', True))
    snapshot, syx, boots = args.snapshot, args.syx, 0
    try:
        while True:
            status, flash = run(snapshot, syx, args, link, current, stop)
            if status != 'reset' or args.no_reboot or stop['now']:
                return {'limit': 0, 'reset': 3}.get(status, 1)
            boots += 1
            image = flash.image()
            if args.flash_out:
                with open(args.flash_out, 'wb') as fh:
                    fh.write(image)
            old_syx = config.firmware(syx)
            new_syx = osupgrade.find_firmware(
                osupgrade.container_from_flash(image),
                args.firmware + [old_syx, os.path.dirname(old_syx)])
            if new_syx is None:
                print('[usbrun] the flash holds an OS that none of the '
                      '--firmware files holds; cannot boot it', flush=True)
                return 3
            # The bootstrap on the device: an OS upgrade does not change it.
            with open(os.path.join(config.sections_dir(),
                                   'section_2_DSP.bin'), 'rb') as fh:
                boot_section = fh.read()
            workdir = args.workdir or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(old_syx))),
                'reboots', str(boots))
            print('[usbrun] restarting into %s' % os.path.basename(new_syx),
                  flush=True)
            t = time.time()

            def progress(e):
                if e.kind == 'done':
                    print('[reboot] %s' % e.text, flush=True)
            snapshot = osupgrade.reboot(
                image, new_syx, workdir, card=os.environ.get('DT2_PLUSDRIVE'),
                boot_section=boot_section, progress=progress,
                log=lambda text: print(text, flush=True))
            syx = os.environ['DT2_SYX']
            print('[usbrun] up again after %.0f s: %s'
                  % (time.time() - t, snapshot), flush=True)
    finally:
        link.close()
        if current.get('ob_link') is not None:
            current['ob_link'].close()


def run(snapshot, syx, args, link, current, stop):
    """One power-on, from `snapshot`. -> (status, FlashWrites)."""
    want_audio = current.get('ob_link') is not None
    m, ev, st, pc, at, profile, dev = open_machine(snapshot, syx, audio=want_audio)
    usb = usbmidi.UsbMidi(sink=link.send)
    if not usb.install(m, at, profile, ev):
        raise SystemExit('[usbrun] this image has no USB MIDI hooks '
                         '(unresolved: %s)' % ', '.join(usb.missing))
    flash = osupgrade.FlashWrites(m, ev)
    current['usb'] = usb
    ob = None
    if current.get('ob_link') is not None:
        ob = obaudio.ObAudio(sink=current['ob_link'].send)
        if not ob.install(m, at, profile):
            print('[usbrun] this image has no Overbridge audio hook: no audio', flush=True)
            ob = None

    pits, intro = timers_for(m, ev, profile, dev)
    post_ips = getattr(dev, 'post_intro_ips', 0) or 4 * INSTR_PER_SEC
    if intro:
        def handover(uc, a, s, d):
            pits.sources[0].channels = (3, 2, 0)
            pits.release()
            for source in pits.sources:
                source.ips = post_ips
        at(profile.intro_done, handover)
    else:
        for source in pits.sources:
            source.ips = post_ips
    audio = tuple(x for x in (ev.get('ssi0_dma'),) if x is not None)
    if audio:
        audio[0].batch = True
        audio[0].align(pits.now)
        if want_audio:
            # As the window renders: the eDMA bank and INTC0 software interrupts
            # (which also clock the sequencer), the codec stream itself unused.
            from emu import edma_sw, intfrc
            audio = audio + (edma_sw.install_bank(m, ev), intfrc.install(m, ev))
            audio[0].sink = lambda data: None
            cfg = getattr(dev, 'audio', None) or {}
            if cfg.get('ips') and accelerated():
                for source in pits.sources:
                    source.ips = cfg['ips']
                audio[0].ips = cfg['ips']
                dsp = ev.get('dspcpu')
                if dsp is not None:
                    dsp.start_thread()
                    dsp.attach_dma(ev['edma_sw_bank'])
    elif want_audio:
        print('[usbrun] no audio model for this device: no Overbridge audio', flush=True)

    emulated = 0.0
    t0 = last = time.time()
    instrs = 0
    status = 'limit'
    while not stop['now']:
        ips = pits.sources[0].ips
        pc, executed, status = spin(m, pc, max(BUDGET, ips // 200), pits=pits,
                                    fast=True, async_events=audio + (usb,))
        if flash.reset:
            print('[usbrun] the OS reset the processor after %d flash writes; %s'
                  % (len(flash.writes), usb.report()), flush=True)
            status = 'reset'
            break
        if status != 'limit':
            print('[usbrun] HALTED: %s at pc=0x%08x after %dM instructions'
                  % (status, pc, (instrs + executed) // 1_000_000), flush=True)
            break
        instrs += executed
        emulated += executed / ips
        if not args.no_realtime:
            # A step can credit far more than one chunk (an idle stretch
            # runs to the next timer), so sleep off all of the lead.
            ahead = emulated - (time.time() - t0)
            while ahead > 0.003 and not stop['now']:
                time.sleep(min(ahead, 0.1))
                ahead = emulated - (time.time() - t0)
        if args.seconds and emulated >= args.seconds:
            break
        if time.time() - last >= args.report:
            last = time.time()
            print('[usbrun] %.1f s emulated, %.0f%% of real time; %s%s'
                  % (emulated, 100 * emulated / max(1e-9, last - t0),
                     usb.report(), '; ' + ob.report() if ob else ''), flush=True)
            if args.png and profile.fb_front is not None:
                buf = panel.read(m, profile.fb_front)
                if buf is not None:
                    with open(args.png, 'wb') as fh:
                        fh.write(panel.png_bytes(buf, scale=4))
    current['usb'] = None

    # The card keeps what the OS wrote, across a reset too.
    esd = ev.get('esdhc')
    if esd is not None:
        esd.card.flush()
    if args.save_on_exit and status == 'limit':
        components = ev['checkpoint_components']
        if components.get('timers') is not pits:
            ev['claim_checkpoint_component']('timers', pits)
        tasks = {'%#010x' % tcb: info
                 for tcb, info in st.get('task_create_hits', {}).items()}
        tasks.update(('%#010x' % tcb, {'entry': e, 'prio': p, 'tcb': tcb})
                     for e, p, tcb in ev.get('tasks', []))
        tmp = args.save_on_exit + '.tmp'
        save_snapshot(m, tmp, extra={'n': st.get('n', 0) + instrs, 'tasks': tasks,
                                     'note': 'saved on exit by tools/usbrun.py'},
                      components=components, manifest=ev.get('checkpoint_manifest'))
        os.replace(tmp, args.save_on_exit)
        print('[usbrun] session saved to %s' % args.save_on_exit, flush=True)
    if status != 'reset':
        print('[usbrun] stopped; %s' % usb.report(), flush=True)
    return status, flash


if __name__ == '__main__':
    sys.exit(main())
