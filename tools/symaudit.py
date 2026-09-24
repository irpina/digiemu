#!/usr/bin/env python3
"""Audit emu/symbols.py against an image, instead of finding gaps one hang
at a time.

Two failures this catches, both of which are silent at runtime -- a hook
simply never installs, or installs on the wrong address, and the emulator
goes on looking plausible:

  1. A symbol that resolves to None. The caller reads `profile.foo`, gets
     None, and skips whatever it was going to do. `display_sem` and
     `worker_done_sem` were both found this way upstream, each after chasing
     a hang. Anything unresolved that is not on ALLOWED_NONE is reported.

  2. A `Fixed(...)` alternative inside an `AnyOf` that ALSO resolves, but to
     a different address than the alternative that won. That is not harmless
     redundancy: on the build where the signature stops matching, the fixed
     address answers instead, silently and wrongly. It matters here because
     the mk1 RTOS is the Digitakt II one shifted +0x2e4, so a DT2 literal
     that still verifies on this image verified by luck -- a short `verify`
     string matching arbitrary bytes -- and that is worth seeing.

    python tools/symaudit.py [IMAGE ...]

With no arguments it audits the configured MAIN OS image. Exit status is 1
if anything in either category is reported, so it can gate a rebuild.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emu import config, symbols                                    # noqa: E402

# Unresolved here is expected and harmless. Keep this list short and say why
# for each entry: an allowlist that grows without justification is how the
# check stops meaning anything.
ALLOWED_NONE = {
    # Optional. Only emu/hle.py's pixel-copy accelerator reads it, and it
    # degrades to running the guest's own copy loop, which is correct but
    # slower. Never affects what the firmware computes.
    'px_copy': 'optional accelerator; absence only costs speed',
    # The Digitone's second-CPU boot task (emu/dsplink.py). Every other
    # product has no such task, and unresolved is what keeps the model off.
    'dsp_boot_task': 'Digitone only: no DSP boot task on this image',
    'dsp_request_sem': 'Digitone only: follows dsp_boot_task',
    'dsp_status': 'Digitone only: follows dsp_boot_task',
    '_ssi0_dma_force_tail_dn': 'Digitone only: its transmit ISR tail',
}

# A `verify` shorter than this is weak evidence on its own: a handful of
# common ColdFire prologue bytes match in many places, so a Fixed rule that
# passes on an image it was not written for tells you very little.
WEAK_VERIFY_BYTES = 12


def alternatives(rule):
    """-> the sub-rules of an AnyOf, or just the rule itself."""
    return list(getattr(rule, 'rules', ())) or [rule]


def analyse(img):
    """-> dict of findings for one image. The single source of truth for both
    the report below and tests/test_symaudit.py, so they cannot drift."""
    prof = symbols.resolve(img, load_addr=symbols.LOAD_ADDR)
    got = {n: prof.get(n) for n, _r, _q in symbols.SYMBOLS}
    unexpected = [n for n in prof.unresolved if n not in ALLOWED_NONE]

    disagreements, fixed_hits = [], []
    for name, rule, _required in symbols.SYMBOLS:
        alts = alternatives(rule)
        results = []
        for alt in alts:
            try:
                val, why = alt.resolve(img, symbols.LOAD_ADDR, got)
            except Exception as exc:                               # noqa: BLE001
                val, why = None, 'raised %s' % exc
            results.append((type(alt).__name__, val, why))
            if type(alt).__name__ == 'Fixed' and val is not None:
                fixed_hits.append((
                    name, val, len(alt.verify) if alt.verify else 0,
                    got.get(name) == val))
        if len(alts) < 2:
            continue
        distinct = {v for _k, v, _w in results
                    if v is not None and not isinstance(v, tuple)}
        if len(distinct) > 1:
            disagreements.append((name, results))
    return dict(profile=prof, unresolved=list(prof.unresolved),
                unexpected=unexpected, disagreements=disagreements,
                fixed_hits=fixed_hits)


def audit(path):
    img = open(path, 'rb').read()
    print('=== %s (%d bytes)' % (path, len(img)))
    f = analyse(img)
    problems = len(f['unexpected']) + len(f['disagreements'])

    print('\n%d rules, %d unresolved (%d unexpected)'
          % (len(symbols.SYMBOLS), len(f['unresolved']), len(f['unexpected'])))
    for name in f['unresolved']:
        why = ALLOWED_NONE.get(name)
        print('  %-24s %s' % (name, why or '** UNEXPECTED **'))

    print('\nAnyOf rules whose alternatives disagree:')
    for name, results in f['disagreements']:
        print('  %s:' % name)
        for kind, val, why in results:
            shown = ('0x%08x' % val) if isinstance(val, int) else (
                '%d item(s)' % len(val) if isinstance(val, tuple)
                else 'unresolved')
            print('    %-8s %-12s %s' % (kind, shown, why))
    if not f['disagreements']:
        print('  (none)')

    print('\nFixed alternatives that resolve on this image:')
    for name, val, nbytes, agrees in f['fixed_hits']:
        print('  %-24s 0x%08x  verify %2d bytes%s  %s'
              % (name, val, nbytes,
                 ' WEAK' if nbytes < WEAK_VERIFY_BYTES else '',
                 'agrees with winner' if agrees else '** DISAGREES **'))
    if not f['fixed_hits']:
        print('  (none)')
    weak = [n for n, _v, b, _a in f['fixed_hits'] if b < WEAK_VERIFY_BYTES]
    if weak:
        print('\n%d fixed rule(s) backed by fewer than %d verify bytes. Not an '
              'error here -- each agrees with the rule that won -- but a short\n'
              'verify is weak evidence on a build it was not written for:\n  %s'
              % (len(weak), WEAK_VERIFY_BYTES, ', '.join(weak)))

    print('\n%s: %d problem(s)' % (os.path.basename(path), problems))
    return problems


def main():
    paths = sys.argv[1:] or [config.main_image()]
    total = sum(audit(p) for p in paths)
    print('\ntotal problems: %d' % total)
    return 1 if total else 0


if __name__ == '__main__':
    raise SystemExit(main())
