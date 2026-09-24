"""Check a firmware build before it goes near a device.

    python -m emu.fwcheck CUSTOM.syx [--baseline STOCK.syx] [--out DIR]
                          [--script FILE] [--margin 0.1] [--no-timing]
                          [--no-boot-strict] [--stop]

What a pass means, and what it does not, is in docs/FIRMWARE-CHECK.md. In
short, five stages, each with its own verdict:

  container   the .syx as the device receives it: every SysEx message's
              checksum, the content checksum, the ELE3 header and section
              table, every section decodes to its declared length and byte
              sum, the product ids are a known product's, the container
              fits the flash below the block the bootstrap reads at 0x380000,
              and the bootstrap version is not higher than the stock one --
              a higher one makes the device rewrite its bootstrap, the one
              step that cannot be undone.
  bootloader  the build's own bootstrap run from its reset vector
              (emu/bootrom.py): it must find the OS in the flash, unpack it
              and start it, leave MAIN OS in DDR exactly as the section
              holds it, and write nothing to the flash.
  boot        a cold boot from that handoff, with the DDR model
              (harness.Machine.set_ddr) and strict mode (emu/strict.py)
              watching, through the intro to a settled user interface.
  run         a scripted session (emu/session.py) on the firmware's own
              instructions, with strict mode and the cycle clock
              (emu/cftiming.py): violations, the audio render's time
              against its deadline, the CPU load.
  compare     with --baseline: the same stages on the stock build, the same
              script (emu/fwcompare.py), and violations the stock build
              also makes marked as the stock build's.

The exit status is 0 when every stage passes. The report is written to
DIR/report.json and summarised on stdout; screens that differ from the
baseline are written as PNGs under DIR/compare.
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import time

MB = 1 << 20
# The block the bootstrap reads at 0x380000 (emu/bootrom.py): the container
# written at 0x80000 must end before it.
FLASH_LIMIT = 0x380000
DEFAULT_MARGIN = 0.10


def _root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _devices_dir():
    return os.environ.get('DIGIEMU_DEVICES') or os.path.join(_root(), 'devices')


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _checksums():
    """-> dt2.build, whose checksum functions verify the SysEx transfer and
    the content checksum, or None. The public tree carries no firmware-
    building code, dt2/build.py included, and then those two checks are
    skipped (and say so). Spelled `import dt2.build`, not `from dt2 import
    build`: where the module is absent the packaging scan would otherwise
    read the second as an import of the dt2 package itself."""
    try:
        import dt2.build as build
    except ImportError:
        return None
    return build


class Stage:
    def __init__(self, name):
        self.name = name
        self.errors = []
        self.warnings = []
        self.facts = {}
        self.skipped = None
        self.seconds = None

    def error(self, text):
        self.errors.append(text)

    def warn(self, text):
        self.warnings.append(text)

    @property
    def passed(self):
        return self.skipped is None and not self.errors

    def as_dict(self):
        return {'passed': self.passed, 'skipped': self.skipped,
                'errors': self.errors, 'warnings': self.warnings,
                'seconds': self.seconds, 'facts': self.facts}


# -- 1. the container ---------------------------------------------------------------
def check_container(syx, stage, baseline=None):
    """Static checks of the .syx. -> the section table (id -> dest)."""
    from dt2 import elz
    from dt2.container import MFR, decode_syx, sections
    from emu import release as rmod
    from emu.extract import classify
    with open(syx, 'rb') as fh:
        raw = fh.read()
    msgs, i = [], 0
    while i < len(raw):
        if raw[i] != 0xF0:
            stage.error('byte %d is not the start of a SysEx message' % i)
            return None
        j = raw.find(b'\xF7', i)
        if j < 0:
            stage.error('the file ends inside a SysEx message')
            return None
        msgs.append(raw[i:j + 1])
        i = j + 1
    framing = [m for m in msgs if len(m) == 16]
    data = [m for m in msgs if len(m) == 128]
    other = [m for m in msgs if len(m) not in (16, 128)]
    stage.facts['messages'] = {'framing': len(framing), 'data': len(data),
                               'other': len(other)}
    if other:
        stage.error('%d SysEx messages are neither 16 nor 128 bytes'
                    % len(other))
    if not framing:
        stage.error('there is no framing message')
        return None
    if any(m[1:4] != MFR for m in msgs):
        stage.error('a message does not carry Elektron\'s manufacturer id')
    # The checksum's constant is the framing message's byte 8, the OS
    # stream id (0x05 on Digitakt mk1; docs/FINDINGS.md counts it from after
    # the F0, as byte 7).
    k = framing[0][8]
    build = _checksums()
    if build is None:
        stage.warn('the SysEx and content checksums were not checked: this '
                   'copy of digiemu does not include the checksum code')
    else:
        bad = sum(1 for m in data
                  if build.packet_checksum(m[1:-1], k) != m[-2])
        stage.facts['message_checksums_bad'] = bad
        if bad:
            stage.error('%d of %d data messages fail their checksum: the '
                        'device rejects the transfer' % (bad, len(data)))
    f = framing[0]
    declared = (f[12] << 14) | (f[13] << 7) | f[14]     # emu/release.py
    stage.facts['data_messages_declared'] = declared
    if declared and declared != len(data):
        stage.error('the framing message announces %d data messages, the file '
                    'has %d' % (declared, len(data)))

    dec = decode_syx(syx)
    at = dec.find(b'ELE3')
    if at != 8:
        stage.error('no ELE3 container after the 8-byte preamble')
        return None
    container = dec[at:]
    # The preamble: the length its checksum covers, then the checksum. On
    # Digitakt mk1 1.53 the length stops short of the padding and the
    # 32-byte trailer slot (1,122,672 of 1,122,708 bytes).
    total, want = struct.unpack_from('>II', dec, 0)
    if total > len(container):
        stage.error('the preamble says the container is %d bytes; it is %d'
                    % (total, len(container)))
        total = len(container)
    if build is not None:
        got = build.content_checksum(container[:total])
        stage.facts['content_checksum'] = '0x%08x' % got
        if want != got:
            stage.error('the content checksum is 0x%08x, the preamble says '
                        '0x%08x' % (got, want))
    c, secs = sections(syx)
    # Where the container's own layout ends: the last section, 16-byte
    # alignment, the 32-byte trailer slot.
    # The decoded stream runs on past it with the last message's padding.
    last = max((off + clen for _sid, off, clen, _d in secs), default=0)
    layout_end = last + (-last) % 16 + 32
    stage.facts['container_bytes'] = layout_end
    # A rebuilt container may declare the whole container, trailer slot
    # included; stock 1.53 stops 36 bytes short. Both are self-consistent;
    # whether the device minds the difference is not known.
    covers = total >= layout_end
    stage.facts['preamble_covers_trailer'] = covers
    if baseline and baseline.get('preamble_covers_trailer') not in (None,
                                                                   covers):
        stage.warn("the preamble's length %s the trailer slot and the stock "
                   "one's %s: consistent either way, never tried on a device"
                   % ('covers' if covers else 'stops short of',
                      'does not' if covers else 'does'))
    table = {}
    for sid, off, clen, dest in secs:
        entry = {'offset': off, 'stored': clen, 'dest': '0x%08x' % dest}
        if off + clen > len(c):
            stage.error('section %d runs past the container' % sid)
            continue
        # Packed when the section's own header says so (its length fits and
        # its byte sum matches: emu/extract.py classify). A section the
        # stock build stores packed that no longer classifies as packed is
        # a damaged one.
        kind, _payload = classify(bytes(c[off:off + clen]))
        entry['kind'] = kind
        stock_kind = ((baseline or {}).get('sections') or {}).get(
            str(sid), {}).get('kind')
        if stock_kind and stock_kind != kind:
            stage.error("section %d is stored %s and the stock build's %s: its "
                        "header does not match its bytes"
                        % (sid, kind, stock_kind))
        if kind == 'packed':
            try:
                out = elz.depack_section(bytes(c[off:off + clen]))
            except Exception as exc:                    # noqa: BLE001
                stage.error('section %d does not decode: %s' % (sid, exc))
                continue
            entry['decoded'] = len(out)
        table[sid] = dict(entry, dest_raw=dest)
    stage.facts['sections'] = {str(k): {kk: vv for kk, vv in v.items()
                                        if kk != 'dest_raw'}
                               for k, v in table.items()}
    for need in (2, 3, 4):
        if need not in table:
            stage.error('there is no section %d (%s)' % (
                need, {2: 'bootstrap', 3: 'MAIN OS', 4: 'updater'}[need]))
    if 3 in table and table[3]['dest_raw'] != 0x40000400:
        stage.error('MAIN OS loads at %s, not 0x40000400 where the bootstrap '
                    'starts it' % table[3]['dest'])
    end = 0x80000 + layout_end
    stage.facts['flash_end'] = '0x%06x' % end
    if end > FLASH_LIMIT:
        stage.error('the container ends at flash 0x%06x, past 0x%06x where the '
                    'bootstrap keeps a block of its own' % (end, FLASH_LIMIT))
    try:
        rel = rmod.identify_release(syx, _devices_dir())
        stage.facts['release'] = {'product': rel.product, 'version': rel.version,
                                  'build': rel.build, 'status': rel.status}
        if rel.status == 'unsupported':
            stage.error('the product ids are not a product digiemu runs')
    except Exception as exc:                            # noqa: BLE001
        stage.error('the release cannot be identified: %s' % exc)
    if 2 in table:
        version = table[2]['dest_raw'] >> 16
        stage.facts['bootstrap_version'] = '0x%04x' % version
        if baseline and baseline.get('bootstrap_version'):
            stock = int(baseline['bootstrap_version'], 16)
            if version > stock:
                stage.error('the bootstrap version is 0x%04x, above the stock '
                            '0x%04x: the device would rewrite its bootstrap '
                            '("BOOTSTRAP UPGRADE"), which cannot be undone'
                            % (version, stock))
            elif version < stock:
                stage.warn('the bootstrap version 0x%04x is below the stock '
                           '0x%04x; the device keeps its own' % (version, stock))
    return table


# -- 2. the bootloader ----------------------------------------------------------------
def check_bootloader(paths, device, stage, handoff_path):
    from emu import bootrom
    boot = open(os.path.join(paths.sections, 'section_2_DSP.bin'), 'rb').read()
    main = open(paths.main_img, 'rb').read()
    r = bootrom.boot(paths.syx, boot, main,
                     ddr=device.ddr_bytes or 64 * MB,
                     ui_card=device.ui_card if device.ui_card is not None
                     else 4, straps=device.straps,
                     groups=device.button_groups())
    stage.facts.update({
        'stop': r.stop, 'reached': ('0x%08x' % r.reached) if r.reached else None,
        'instructions': r.instructions,
        'boot_flags': ('0x%08x' % r.boot_flags) if r.boot_flags is not None
        else None,
        'flash_reads': ['0x%06x-0x%06x' % rr for rr in r.flash_reads],
        'flash_writes': r.flash_writes,
        'panel_queries': r.panel_queries,
        'stepped_over': ['0x%08x' % a for a, _n in r.stepped_over],
        'main_os_matches': r.main_os_matches})
    if r.reached is None:
        stage.error('the bootstrap never started the OS: %s' % r.stop)
        return None
    if r.reached != 0x400004e8 and not (0x40000400 <= r.reached < 0x40000400
                                        + len(main)):
        stage.error('the bootstrap jumped to 0x%08x, outside MAIN OS'
                    % r.reached)
    if not r.main_os_matches:
        stage.error('MAIN OS in DDR after the bootstrap is not the section\'s '
                    'bytes: the device would run something else')
    if r.flash_writes:
        stage.error('the boot wrote to the flash: %s' % r.flash_writes[:4])
    if r.boot_flags and r.boot_flags & 0x60:
        stage.error('the bootstrap passed boot flags 0x%08x: 0x40 and 0x20 '
                    'mean it found no front panel, and the OS then parks'
                    % r.boot_flags)
    bootrom.save_handoff(r, handoff_path)
    return r


# -- 3. the boot ------------------------------------------------------------------------
def check_boot(paths, device, stage, handoff_path, strict_boot=True,
               progress=None):
    from emu import bootstrap as bs
    from emu import strict as strictmod
    main_len = os.path.getsize(paths.main_img)
    monitors = []

    def on_machine(m, ev, step):
        s = strictmod.Strict(m, ddr_size=device.ddr_bytes or 64 * MB,
                             stop=False,
                             prescanned=(0x40000400, 0x40000400 + main_len))
        monitors.append((step, s, ev))
    options = {'ddr': device.ddr_bytes or 64 * MB}
    if handoff_path:
        options['start_from'] = handoff_path
    if strict_boot:
        options['on_machine'] = on_machine
    try:
        gui = bs.first_run(paths, progress=progress, options=options)
    except BaseException as exc:                        # noqa: BLE001
        stage.error('the firmware did not boot to a settled user interface: '
                    '%s' % getattr(exc, 'args', [exc])[0])
        gui = None
    violations = []
    for step, s, ev in monitors:
        rep = s.report(stand_ins=strictmod.stand_ins(ev) if ev else None)
        for v in rep['violations']:
            violations.append(dict(v, stage=step))
        stage.facts.setdefault('stand_ins', {})[step] = rep['stand_ins']
        stage.facts.setdefault('unmodelled_peripherals', {})[step] = [
            e['peripheral'] for e in rep['unmodelled_peripherals']]
    stage.facts['violations'] = violations
    return gui


# -- 4. the run -----------------------------------------------------------------------
def check_run(paths, device, stage, snapshot, steps, *, timing=True,
              stop=False, margin=DEFAULT_MARGIN, fsys=None):
    from emu import cftiming, ssi
    from emu.session import Session, run_script
    tkw = None
    if timing:
        tkw = {'accel': 'max'}
        if fsys:
            tkw['fsys'] = fsys
    s = Session(snapshot, paths.syx, hle=False, timing=tkw,
                strict={'stop': stop},
                ddr=device.ddr_bytes or 64 * MB)
    try:
        marks = run_script(s, steps)
        stage.facts['emulated_ms'] = round(s.ms, 1)
        stage.facts['frames'] = len(s.frames)
        stage.facts['audio_seconds'] = round(s.audio_seconds(), 3)
        if s.halted:
            stage.error('the run stopped: %s' % s.halted)
        rep = s.strict_report()
        stage.facts['violations'] = rep['violations']
        stage.facts['unmodelled_peripherals'] = [
            e['peripheral'] for e in rep['unmodelled_peripherals']]
        stage.facts['watchdog'] = rep['watchdog']
        stage.facts['stand_ins'] = rep['stand_ins']
        if s.clock is not None:
            t = s.clock.report()
            stage.facts['timing'] = {k: v for k, v in t.items()
                                     if k != 'vectors'}
            prof = ssi.PROFILES.get((device.audio or {}).get('ssi_profile'))
            vectors = []
            if prof is not None:
                vectors = [prof.tx_vector, ssi.FORCE_VECTOR]
            period = None
            if prof is not None:
                period = cftiming.measured_period(s.clock, prof.tx_vector)
            deadlines = {}
            for vec in vectors:
                if period:
                    deadlines[str(vec)] = cftiming.deadline_report(
                        s.clock, vec, period)
            stage.facts['audio_deadlines'] = deadlines
            stage.facts['vectors'] = {str(k): v for k, v in t['vectors'].items()}
            if t['decode_mismatches']:
                stage.warn('%d blocks the decoder could not cost exactly'
                           % t['decode_mismatches'])
            worst = min((d['margin'] for d in deadlines.values()
                         if 'margin' in d), default=None)
            stage.facts['render_margin'] = worst
            if worst is not None and worst < margin:
                stage.error('the audio render comes within %.0f%% of its '
                            'deadline (margin %.1f%%, need %.0f%%)'
                            % ((1 - worst) * 100, worst * 100, margin * 100))
            late = sum(d.get('late', 0) for d in deadlines.values())
            if late:
                stage.error('%d audio renders ran past their deadline' % late)
        from emu.fwcompare import Run
        return Run(s, marks)
    finally:
        s.close()


# -- one build ----------------------------------------------------------------------
def prepare(syx, workdir, role='build'):
    """-> (FirmwarePaths, Device) for `syx` in a fresh folder of its own
    under `workdir`/`role`, with a device overlay when the build is not a
    known one.

    Fresh because the app's stages skip whatever an earlier run left
    verified: a second check of the same build (or a stock build checked
    against itself) would take the saved boot and check nothing, and a
    boot violation would silently pass."""
    from emu import bootstrap as bs
    from emu import device as devmod
    from emu import release as rmod
    sha = _sha256(syx)
    rel = rmod.identify_release(syx, _devices_dir())
    short = getattr(rel.device, 'short', None) or 'fw'
    folder = os.path.join(workdir, role, '%s-%s' % (short, sha[:10]))
    if os.path.isdir(folder):
        shutil.rmtree(folder)                   # this check's own, from before
    os.makedirs(folder)
    name = os.path.basename(syx)
    dst = os.path.join(folder, name)
    shutil.copyfile(syx, dst)
    devices = _devices_dir()
    if rel.status == 'untested':
        overlay = os.path.join(folder, 'devices')
        rmod.write_device_overlay(rel, overlay, devices)
        devices = overlay
    paths = bs.FirmwarePaths(folder, name, devices)
    os.environ.update(paths.env())
    dev, _fw = devmod.identify(dst, devices)
    return paths, dev


def check_build(syx, workdir, *, steps=None, timing=True, strict_boot=True,
                stop=False, margin=DEFAULT_MARGIN, baseline=None,
                fsys=None, role='build', log=print, on_event=None):
    """Run the stages on one build. -> (report dict, Run or None).

    `steps` is a parsed key script (emu/fwcompare.py); None runs the
    default tour of this build's own panel. `role` names its folder under
    `workdir` ('baseline' or 'build'), so the two never share one.
    `on_event(record)` hears each stage start ({'kind': 'start', 'step'})
    and finish ({'kind': 'done', 'step', 'state', 'passed'}), and the boot's
    notes ({'kind': 'note', 'step', 'text'})."""
    stages = {}
    t_all = time.time()
    tell = on_event or (lambda rec: None)

    def run_stage(name, fn):
        st = Stage(name)
        stages[name] = st
        t = time.time()
        log('== %s' % name)
        tell({'kind': 'start', 'step': name})
        try:
            out = fn(st)
        except Exception as exc:                        # noqa: BLE001
            import traceback
            st.error('the check itself failed: %s' % exc)
            st.facts['traceback'] = traceback.format_exc()
            out = None
        st.seconds = round(time.time() - t, 1)
        for e in st.errors:
            log('   FAIL %s' % e)
        for w in st.warnings:
            log('   warn %s' % w)
        state = 'passed' if st.passed else (
            'skipped: %s' % st.skipped if st.skipped else 'FAILED')
        found = st.facts.get('violations')
        if st.passed and found:
            # Judged after both builds ran: a stock build's own are no fault.
            state = 'ran with %d violation(s), judged below' % len(found)
            for v in found:
                log('   violation %s: %s' % (v['kind'], v['what']))
        log('   %s in %.0f s' % (state, st.seconds))
        tell({'kind': 'done', 'step': name, 'state': state,
              'passed': st.passed, 'seconds': st.seconds})
        return out

    base_facts = (baseline or {}).get('stages', {}).get('container', {}).get(
        'facts', {})
    table = run_stage('container',
                      lambda st: check_container(syx, st, base_facts))
    paths = dev = None
    script = []

    def prepare_stage(st):
        from emu import fwcompare
        folder, device = prepare(syx, workdir, role)
        script[:] = steps if steps is not None else fwcompare.parse_script(
            fwcompare.default_script(device.labels.values()))
        # Before anything boots: a key the panel lacks would otherwise
        # surface only after the boot, minutes in.
        for problem in fwcompare.script_problems(script, device):
            st.error('the key script: %s' % problem)
        return (folder, device) if st.passed else None

    if table is not None:
        paths, dev = run_stage('prepare', prepare_stage) or (None, None)
    handoff = None
    boot_result = None
    if paths is not None:
        from emu import bootstrap as bs
        bs.extract_sections(paths)
        handoff = os.path.join(paths.root, 'bootrom-handoff.bin')
        boot_result = run_stage('bootloader', lambda st: check_bootloader(
            paths, dev, st, handoff))
    gui = None
    if boot_result is not None:
        def progress(ev):
            if ev.kind == 'note' and ev.text and ev.text.strip():
                line = ev.text.strip().splitlines()[0][:120]
                log('   %s' % line)
                tell({'kind': 'note', 'step': 'boot', 'text': line})
        gui = run_stage('boot', lambda st: check_boot(
            paths, dev, st, handoff, strict_boot=strict_boot,
            progress=progress))
    record = None
    if gui:
        record = run_stage('run', lambda st: check_run(
            paths, dev, st, gui, script, timing=timing,
            stop=stop, margin=margin, fsys=fsys))
    for name in ('container', 'prepare', 'bootloader', 'boot', 'run'):
        if name not in stages:
            st = Stage(name)
            st.skipped = 'an earlier stage failed'
            stages[name] = st
    # Violations: a baseline's are the stock build's.
    stock = set()
    for name in ('boot', 'run'):
        for v in (baseline or {}).get('stages', {}).get(name, {}).get(
                'facts', {}).get('violations', ()):
            stock.add((v['kind'], v['what']))
    for name in ('boot', 'run'):
        st = stages[name]
        for v in st.facts.get('violations', ()):
            v['in_stock'] = (v['kind'], v['what']) in stock
            if not v['in_stock']:
                st.error('%s: %s' % (v['kind'], v['what']))
    report = {
        'firmware': {'path': os.path.abspath(syx), 'sha256': _sha256(syx)},
        'passed': all(stages[n].passed for n in ('container', 'bootloader',
                                                  'boot', 'run')),
        'seconds': round(time.time() - t_all, 1),
        'stages': {n: st.as_dict() for n, st in stages.items()},
    }
    return report, record


def summary(report, name='build'):
    lines = ['%s: %s' % (name, 'PASS' if report['passed'] else 'FAIL')]
    for n, st in report['stages'].items():
        state = 'pass' if st['passed'] else ('skip' if st['skipped'] else 'FAIL')
        lines.append('  %-10s %s' % (n, state))
        for e in st['errors'][:8]:
            lines.append('      - %s' % e)
        for w in st['warnings'][:4]:
            lines.append('      ! %s' % w)
    run = report['stages'].get('run', {}).get('facts', {})
    if run.get('render_margin') is not None:
        lines.append('  audio render margin %.1f%% (cycle estimate at %.0f MHz)'
                     % (run['render_margin'] * 100,
                        run.get('timing', {}).get('fsys_hz', 0) / 1e6))
    if run.get('timing'):
        lines.append('  CPU busy %.0f%%' % (run['timing']['busy_fraction'] * 100))
    return lines


def run_check(syx, out, *, baseline=None, steps=None, timing=True,
              strict_boot=True, stop=False, margin=DEFAULT_MARGIN, fsys=None,
              keep_work=True, log=print, on_event=None):
    """Check `syx` -- and first `baseline`, the stock build, the same way --
    compare the two and write OUT/report.json. -> that report: {'build',
    'baseline', 'compare' (with a baseline), 'passed', 'summary' (lines)}.

    `on_event(role, record)` hears check_build's records, `role` being
    'baseline' or 'build', and ('compare', ...) around the comparison.
    Without `keep_work`, each build's work folder (the .syx copy, its
    sections, a card image and snapshots: a gigabyte or more) goes as soon
    as that build is checked; the report and the differing screens stay."""
    from emu import fwcompare
    out = os.path.abspath(out)
    work = os.path.join(out, 'work')
    os.makedirs(work, exist_ok=True)

    def one(path, role, base=None):
        hook = (lambda rec: on_event(role, rec)) if on_event else None
        try:
            return check_build(path, work, steps=steps, timing=timing,
                               strict_boot=strict_boot, stop=stop,
                               margin=margin, fsys=fsys, baseline=base,
                               role=role, log=log, on_event=hook)
        finally:
            if not keep_work:
                shutil.rmtree(os.path.join(work, role), ignore_errors=True)

    base_report = base_run = None
    if baseline:
        log('### baseline %s' % baseline)
        base_report, base_run = one(baseline, 'baseline')
    log('### build %s' % syx)
    report, run = one(syx, 'build', base_report)
    full = {'build': report, 'baseline': base_report}
    if base_run is not None and run is not None:
        if on_event:
            on_event('compare', {'kind': 'start', 'step': 'compare'})
        full['compare'] = fwcompare.compare(base_run, run,
                                            out=os.path.join(out, 'compare'))
        if on_event:
            on_event('compare', {'kind': 'done', 'step': 'compare',
                                 'passed': True,
                                 'state': 'identical'
                                 if full['compare']['identical']
                                 else 'differences found'})
    # The build's verdict first. The stock build's own findings come last,
    # for reference: they never count against the build.
    lines = summary(report)
    if 'compare' in full:
        lines.append('compared with the stock build:')
        lines += ['  ' + x for x in fwcompare.summary(full['compare'])]
    if base_report:
        lines += summary(base_report, 'stock build, for reference')
    full['passed'] = report['passed']
    full['summary'] = lines
    with open(os.path.join(out, 'report.json'), 'w', encoding='utf-8') as fh:
        json.dump(full, fh, indent=1, default=str)
    if not keep_work:
        try:
            os.rmdir(work)
        except OSError:
            pass
    return full


def main(argv=None):
    from emu import fwcompare
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('syx', help='the build to check')
    ap.add_argument('--baseline', help='the stock .syx to compare against')
    ap.add_argument('--out', default='fwcheck-out')
    ap.add_argument('--script', help='a key script (emu/fwcompare.py)')
    ap.add_argument('--margin', type=float, default=DEFAULT_MARGIN,
                    help='the audio render margin a pass needs (default 0.10)')
    ap.add_argument('--fsys', type=float, help='core clock in Hz (250e6)')
    ap.add_argument('--no-timing', action='store_true',
                    help='skip the cycle clock (much faster)')
    ap.add_argument('--no-boot-strict', action='store_true',
                    help='no strict mode during the boot stages')
    ap.add_argument('--stop', action='store_true',
                    help='stop the run at its first violation: what follows '
                         'is the emulator going on with zeros where the '
                         'device would take an exception')
    args = ap.parse_args(argv)
    steps = None                          # each build's own default tour
    if args.script:
        with open(args.script, encoding='utf-8') as fh:
            steps = fwcompare.parse_script(fh.read())
    full = run_check(args.syx, args.out, baseline=args.baseline, steps=steps,
                     timing=not args.no_timing,
                     strict_boot=not args.no_boot_strict, stop=args.stop,
                     margin=args.margin,
                     fsys=int(args.fsys) if args.fsys else None)
    print()
    print('\n'.join(full['summary']))
    print('report: %s' % os.path.join(os.path.abspath(args.out),
                                      'report.json'))
    return 0 if full['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
