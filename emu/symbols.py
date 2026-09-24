# fmt: off
"""Resolve firmware addresses from the image itself, instead of hardcoding
them per build.

Every address dspboot/longrun/gui/panel needs used to be a literal constant,
good for exactly one firmware: Digitakt II OS 1.15C. A second firmware (same
ColdFire SoC, same bootloader, same RTOS, different application build --
Digitone II 1.10E) runs under those constants today and produces silent
garbage: no error, just five minutes of boot into a blank panel, because every
hook lands on the wrong instruction.

Proof the addresses are derivable rather than fixed: five RTOS entry points
(entry, task_create, task_start, sem_pend, pend_b) sit at byte-identical
offsets in both 1.15C and 1.10E -- the RTOS is linked at a stable base
regardless of what the application above it looks like. Everything else moves
between builds by anywhere from a few bytes to several hundred KB, because the
application code before it changed size. So the five RTOS entry points are
FIXED (checked in as literal addresses, verified against opening bytes at
resolve time so a firmware that does NOT share this RTOS build fails loudly
rather than silently), and everything else is found by what it looks like or
how it is reached from something already resolved:

    * a `jsr <fixed-address>` call site (Xrefs, XrefShape)
    * a big-endian abs32 literal embedded in the instruction stream at a
      known offset from something already resolved (Operand, Operands)
    * a byte sequence unique in the whole image (Opcode)
    * a masked signature: N reference bytes captured from a firmware image,
      with any embedded absolute address in [0x40000000, 0x40400000) --
      i.e. anything that looks like an inlined pointer INTO the loaded image,
      which relocates between builds -- wildcarded out, then searched for a
      unique match in the target image (Sig)

Verified against two extracted MAIN OS images (both load at 0x40000400):
Digitakt II 1.15C and Digitone II 1.10E, and since the mk1 port against
Digitakt OS 1.53. See docs/ for the resolution table.

**No firmware bytes are checked in.** A signature is stored as an `H`: its
length, which offsets are wildcards, one four-byte anchor (a single
instruction, used to find candidates quickly), and a 128-bit SHA-256 digest of
its fixed bytes. Matching finds the anchor and compares the digest, so it
finds exactly what the bytes would have found, but the bytes themselves are
Elektron's and stay in the user's own firmware. Rules still accept literal
hex, for tests and for working out a new signature (tools/mksig.py prints the
`H` to paste in). The module needs no firmware image to import.

REQUIRED symbols (boot cannot progress without them): entry, flash_read,
pend_call, completion_sem, depack_copy, task_start, sem_pend. An unresolved or
ambiguous REQUIRED symbol raises SymbolResolutionError naming exactly which
symbol(s) and why, at `resolve()` time -- not five minutes into a run.

Everything else is OPTIONAL: diagnostic (task_create_sites, call_sites,
idle_spins), late-boot/GUI (panel_diff, fb_front, fb_back), or the UI-trace
hook points (queue_send, ui_queue, ui_key_dispatch, view_offer,
view_activate, view_close, view_closed_mark, view_request_pop, view_sweep,
ui_tick_inc, ui_tick_counter). An unresolved
OPTIONAL symbol is simply None on the Profile; every caller of one must
degrade gracefully (install no hook) rather than crash -- see dspboot.py,
longrun.py and panel.py for the pattern.

    python -m emu.symbols [image]      # print the resolution report
"""
import hashlib
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOAD_ADDR = 0x40000400   # both known builds load MAIN OS here (see emu.config)

# Sig masks out any inlined abs32 that relocates between builds. The default
# window is the loaded image itself; DATA_HI widens it to cover the RAM
# variables above the image -- semaphores, buffers, task blocks -- which
# relocate just as freely. Widening is not free: the window is scanned
# unaligned, so a wider range also masks bytes that merely *look* like an
# address in it (`45f9 4017....`, lea's opcode plus the top half of its
# operand, is one that bites). Use it only where a signature is verified
# unique in both images with it, and prefer the default where that works.
DATA_HI = 0x48000000

REQUIRED = frozenset({
    'entry', 'flash_read', 'pend_call', 'completion_sem', 'depack_copy',
    'task_start', 'sem_pend',
})


class SymbolResolutionError(RuntimeError):
    """A REQUIRED symbol could not be resolved. str(e) names exactly which
    one(s) and what rule failed, so this replaces booting for five minutes
    into a blank panel with a refusal at load time."""


# --------------------------------------------------------------------------
# Signatures without their bytes.
# --------------------------------------------------------------------------

DIGEST_HEX = 32          # 128 bits of SHA-256


class H:
    """A byte signature, checked in as a digest instead of the bytes.

    `n` is its length and `wild` the wildcard offsets, as `'12-15,20'`.
    `anchor` is `'OFF:HEX'`, up to four fixed bytes at offset OFF that every
    match must contain -- at most one instruction, which is what lets the
    search jump from candidate to candidate instead of hashing every offset
    (shorter only when the wildcards leave no four fixed bytes in a row) --
    or None for a signature only ever checked at a known address.
    `digest` is the first 128 bits of SHA-256 over the fixed bytes in order.

    A position matches when its fixed bytes hash to `digest`: the same set of
    positions the literal bytes would match, overlaps included. Build one from
    literal bytes with `H.of` (tools/mksig.py prints them ready to paste).
    """

    def __init__(self, n, wild='', anchor=None, digest=''):
        self.n = n
        self.wild = _parse_offsets(wild)
        self.runs = _fixed_runs(n, self.wild)
        self.anchor_off, self.anchor = None, None
        if anchor:
            off, hx = anchor.split(':')
            self.anchor_off, self.anchor = int(off), bytes.fromhex(hx)
        self.digest = digest

    @classmethod
    def of(cls, raw, wild=(), img=None):
        """-> the H for literal bytes `raw` with offsets `wild` wildcarded.
        The anchor is the widest fixed window available, up to 4 bytes; given
        `img`, the one of those that is rarest in it."""
        wild = set(wild)
        anchor = None
        for k in (4, 3, 2, 1):
            spots = [o for o in range(len(raw) - k + 1)
                     if not wild & set(range(o, o + k))]
            if spots:
                pick = (min(spots, key=lambda o: img.count(raw[o:o + k]))
                        if img is not None else spots[0])
                anchor = '%d:%s' % (pick, raw[pick:pick + k].hex())
                break
        h = cls(len(raw), _format_offsets(wild), anchor)
        h.digest = h.hash_at(raw, 0)
        return h

    def __len__(self):
        return self.n

    def __repr__(self):
        return 'H(%d, %r, %r, %r)' % (
            self.n, _format_offsets(self.wild),
            None if self.anchor is None
            else '%d:%s' % (self.anchor_off, self.anchor.hex()), self.digest)

    def hash_at(self, img, p):
        fixed = b''.join(img[p + s:p + e] for s, e in self.runs)
        return hashlib.sha256(fixed).hexdigest()[:DIGEST_HEX]

    def matches_at(self, img, p):
        if not 0 <= p <= len(img) - self.n:
            return False
        if self.anchor is not None and img[p + self.anchor_off:
                                           p + self.anchor_off + len(self.anchor)] != self.anchor:
            return False
        return self.hash_at(img, p) == self.digest

    def positions(self, img):
        """-> every offset in `img` where the signature matches."""
        if self.anchor is None:
            raise ValueError('an H without an anchor can only be checked at '
                             'a known address')
        out, i = [], img.find(self.anchor)
        while i >= 0:
            p = i - self.anchor_off
            if self.matches_at(img, p):
                out.append(p)
            i = img.find(self.anchor, i + 1)
        return out


def _parse_offsets(text):
    out = set()
    for part in filter(None, (text or '').split(',')):
        a, _, b = part.partition('-')
        out.update(range(int(a), int(b or a) + 1))
    return out


def _format_offsets(offsets):
    parts, run = [], []
    for o in sorted(offsets):
        if run and o == run[-1] + 1:
            run.append(o)
            continue
        if run:
            parts.append('%d-%d' % (run[0], run[-1]) if len(run) > 1 else str(run[0]))
        run = [o]
    if run:
        parts.append('%d-%d' % (run[0], run[-1]) if len(run) > 1 else str(run[0]))
    return ','.join(parts)


def _fixed_runs(n, wild):
    runs, start = [], None
    for k in range(n + 1):
        fixed = k < n and k not in wild
        if fixed and start is None:
            start = k
        elif not fixed and start is not None:
            runs.append((start, k))
            start = None
    return runs


def _mask_offsets(raw, lo, hi, wild=()):
    """The offsets of `raw` to wildcard: every byte of any 4-byte big-endian
    window whose value falls in [lo, hi), plus `wild`."""
    n, masked, i = len(raw), set(wild), 0
    while i <= n - 4:
        if lo <= int.from_bytes(raw[i:i + 4], 'big') < hi:
            masked.update(range(i, i + 4))
            i += 4
        else:
            i += 1
    return masked


def _pattern(ref, lo=1, hi=0, wild=()):
    """An H from either an H (checked in) or literal hex (tests, new work).
    For an H, `lo`/`hi` were applied when it was made and are kept at the
    call site only as a record; `wild` must already be wildcarded in it."""
    if isinstance(ref, H):
        missing = set(wild) - ref.wild
        if missing:
            raise ValueError('offsets %s are not wildcards in %r'
                             % (sorted(missing), ref))
        return ref
    raw = bytes.fromhex(ref)
    return H.of(raw, _mask_offsets(raw, lo, hi, wild))


# --------------------------------------------------------------------------
# Rules. Each is declarative: given the image, its load address, and the
# dict of symbols already resolved, it returns (value, detail) -- value is
# None if the rule failed, detail is a one-line human explanation either way
# (Profile.report() prints it verbatim).
# --------------------------------------------------------------------------

class Fixed:
    """A literal address. Must be present in the image and, if `verify` is
    given, its opening bytes must match -- this is what makes a firmware
    that does NOT share this RTOS build fail loudly instead of silently."""

    def __init__(self, addr, verify=None):
        self.addr = addr
        self.verify = _pattern(verify) if verify else None

    def resolve(self, img, load_addr, got):
        off = self.addr - load_addr
        if not (0 <= off < len(img)):
            return None, '0x%08x is outside the image' % self.addr
        if self.verify and not self.verify.matches_at(img, off):
            n = len(self.verify)
            return None, ('the %d bytes at 0x%08x (%s) do not match the verify '
                          'digest' % (n, self.addr, img[off:off + n].hex()))
        return self.addr, 'fixed 0x%08x (RTOS base, byte-identical across builds)' % self.addr


class Xrefs:
    """Every `jsr <target>` call site (opcode 4EB9 + abs32 operand). Resolves
    to a tuple of call-site (opcode) addresses; unresolved if there are none."""

    def __init__(self, target):
        self.target = target

    def resolve(self, img, load_addr, got):
        target = got.get(self.target)
        if target is None:
            return None, "depends on unresolved '%s'" % self.target
        sites = _find_all(img, b'\x4e\xb9' + struct.pack('>I', target))
        if not sites:
            return None, 'no jsr 0x%08x call sites found' % target
        return tuple(load_addr + i for i in sites), (
            '%d call site(s) to 0x%08x' % (len(sites), target))


class XrefShape:
    """The call site into `target` whose following bytes match `shape` (a hex
    string, '..' per wildcard byte, applied immediately after the 6-byte jsr
    instruction). Must be unique. Resolves to the jsr opcode address."""

    def __init__(self, target, shape):
        self.target = target
        self.shape = shape

    def resolve(self, img, load_addr, got):
        target = got.get(self.target)
        if target is None:
            return None, "depends on unresolved '%s'" % self.target
        needle = b'\x4e\xb9' + struct.pack('>I', target)
        n = len(self.shape) // 2
        hits = []
        for i in _find_all(img, needle):
            tail = img[i + 6:i + 6 + n]
            if len(tail) == n and _shape_match(tail, self.shape):
                hits.append(i)
        if len(hits) != 1:
            return None, ('%d call site(s) to 0x%08x match shape %s, need exactly 1'
                           % (len(hits), target, self.shape))
        return load_addr + hits[0], (
            'unique jsr 0x%08x + shape %s at 0x%08x' % (target, self.shape, load_addr + hits[0]))


class Operand:
    """A big-endian integer read out of the instruction stream at
    `<resolved symbol> + at`, adjusted by `adjust`."""

    def __init__(self, symbol, at, width=4, adjust=0):
        self.symbol, self.at, self.width, self.adjust = symbol, at, width, adjust

    def resolve(self, img, load_addr, got):
        base = got.get(self.symbol)
        if base is None:
            return None, "depends on unresolved '%s'" % self.symbol
        off = (base + self.at) - load_addr
        if not (0 <= off + self.width <= len(img)):
            return None, 'operand read at %s+%d falls outside the image' % (self.symbol, self.at)
        raw = img[off:off + self.width]
        literal = int.from_bytes(raw, 'big')
        val = literal + self.adjust
        return val, ('%s+%d = 0x%0*x, %+d -> 0x%08x'
                      % (self.symbol, self.at, self.width * 2, literal, self.adjust, val))


class Offset:
    """A fixed byte distance from an already-resolved symbol.

    For the case where two names denote the same instruction from two
    directions -- `sleep_pend` is the *return* address of the `jsr sem_pend`
    that `pend_call` names, so it is exactly `pend_call + 6` in any build,
    with no signature of its own to match."""

    def __init__(self, symbol, delta):
        self.symbol, self.delta = symbol, delta

    def resolve(self, img, load_addr, got):
        base = got.get(self.symbol)
        if base is None:
            return None, "'%s' unresolved" % self.symbol
        return base + self.delta, '%s%+d' % (self.symbol, self.delta)


class Opcode:
    """A raw byte pattern that must occur exactly once in the whole image."""

    def __init__(self, ref):
        self.pat = _pattern(ref)

    def resolve(self, img, load_addr, got):
        hits = self.pat.positions(img)
        if len(hits) != 1:
            return None, ('%d occurrence(s) of the %d-byte opcode, need exactly 1'
                           % (len(hits), len(self.pat)))
        return load_addr + hits[0], 'unique opcode at 0x%08x' % (load_addr + hits[0])


class Sig:
    """A masked signature: `ref` is an `H` (or, for tests and new work, the
    literal hex it was made from -- see the module docstring). Any 4-byte
    big-endian window of the reference bytes that fell in [lo, hi) -- an
    inlined address into the loaded image itself, which relocates between
    builds -- is a wildcard. Must find exactly one match in the target image.

    `wild` additionally masks individual byte offsets that are not addresses
    but still move between builds -- a small immediate the compiler chose, for
    instance. Use it sparingly and say in a comment what the byte is, because
    every masked byte is one less thing keeping the match unique. For an `H`,
    `lo`, `hi` and `wild` were applied when it was made; they stay at the call
    site as the record of how, and `wild` is checked against it."""

    def __init__(self, ref, lo=0x40000000, hi=0x40400000, wild=()):
        self.pat = _pattern(ref, lo, hi, wild)
        self.lo, self.hi = lo, hi

    def resolve(self, img, load_addr, got):
        hits = self.pat.positions(img)
        if len(hits) != 1:
            return None, ('%d match(es) for masked signature (%d bytes), need exactly 1'
                           % (len(hits), len(self.pat)))
        return load_addr + hits[0], 'unique masked-signature match at 0x%08x' % (load_addr + hits[0])


class SigWhere:
    """A masked signature that is deliberately NOT unique, narrowed to one
    match by an abs32 operand that ties it to an already-resolved symbol.

    The intro's PIT3 handler and the display module's are the same routine
    compiled twice -- ack the timer, post a semaphore -- so they mask to the
    same signature and no amount of extra context separates them: they differ
    only in *which* semaphore they post. That is the operand at `at`, and
    comparing it to `equals` (plus `adjust`) is what says which one is the
    intro's. Requires exactly one surviving match."""

    def __init__(self, ref, at, equals, adjust=0, lo=0x40000000, hi=0x40400000):
        self.pat = _pattern(ref, lo, hi)
        self.at, self.equals, self.adjust = at, equals, adjust

    def resolve(self, img, load_addr, got):
        want = got.get(self.equals)
        if want is None:
            return None, "'%s' unresolved" % self.equals
        hits = []
        for start in self.pat.positions(img):
            off = start + self.at
            if off + 4 > len(img):
                continue
            if int.from_bytes(img[off:off + 4], 'big') + self.adjust == want:
                hits.append(start)
        if len(hits) != 1:
            return None, ('%d of the masked-signature matches carry %s at +%d, need exactly 1'
                          % (len(hits), self.equals, self.at))
        return load_addr + hits[0], ('masked-signature match at 0x%08x, selected by %s at +%d'
                                     % (load_addr + hits[0], self.equals, self.at))


class OperandGroup:
    """The first `take` DISTINCT big-endian abs32 values in [lo, hi) found by
    scanning `span` bytes starting at `<resolved base symbol>`. Resolves to a
    tuple of up to `take` values, in the order they first appear. Used to
    pull fb_front/fb_back out of panel_diff's own instruction stream -- see
    Pick, which then names each element."""

    def __init__(self, base, span, lo, hi, take):
        self.base, self.span, self.lo, self.hi, self.take = base, span, lo, hi, take

    def resolve(self, img, load_addr, got):
        base = got.get(self.base)
        if base is None:
            return None, "depends on unresolved '%s'" % self.base
        off = base - load_addr
        window = img[off:off + self.span]
        found, i = [], 0
        while i <= len(window) - 4 and len(found) < self.take:
            val = int.from_bytes(window[i:i + 4], 'big')
            if self.lo <= val < self.hi:
                if val not in found:
                    found.append(val)
                i += 4
            else:
                i += 1
        if not found:
            return None, ('no operands in [0x%08x, 0x%08x) within %#x bytes of %s'
                           % (self.lo, self.hi, self.span, self.base))
        return tuple(found), ('%d distinct operand(s) near %s: %s'
                               % (len(found), self.base, ', '.join('0x%08x' % v for v in found)))


class Pick:
    """One element of a symbol that resolved to a tuple (OperandGroup, Xrefs,
    ScanAll). Unresolved if the group is unresolved or too short."""

    def __init__(self, group, index):
        self.group, self.index = group, index

    def resolve(self, img, load_addr, got):
        group = got.get(self.group)
        if group is None or len(group) <= self.index:
            n = 0 if group is None else len(group)
            return None, "'%s' has %d element(s), need index %d" % (self.group, n, self.index)
        return group[self.index], 'element %d of %s' % (self.index, self.group)


class ScanAll:
    """Every occurrence of a raw byte pattern, at a fixed `step` alignment.
    Unlike Opcode/Xrefs this never fails -- zero occurrences is itself a
    valid (if surprising) resolution, e.g. a build with no idle spins."""

    def __init__(self, hexstr, step=2):
        self.pattern = bytes.fromhex(hexstr)
        self.step = step

    def resolve(self, img, load_addr, got):
        n = len(self.pattern)
        hits = [off for off in range(0, len(img) - n + 1, self.step)
                if img[off:off + n] == self.pattern]
        return tuple(load_addr + off for off in hits), '%d occurrence(s)' % len(hits)


class StringTable:
    """A `char *` table, located by the literal strings its entries point at.

    A data table has no opcodes to sign, and this one is reached through a
    C++ object field rather than an immediate operand, so there is nothing
    for Xrefs, Operand or Sig to anchor to. What it does have is content:
    entry n points at a known string. Finding an aligned run of big-endian
    pointers whose targets ARE those strings identifies the table without
    depending on where the compiler put it -- which is what keeps it working
    on a firmware version neither known build has seen.

    The strings are read back out of the image and compared, rather than
    their addresses being required unique, because these products reuse
    short labels: `TRIG` is both a page button and the stem of `TRIG 1`,
    and `ENCODER A` appears in two different tables.

    Only the NUL that TERMINATES an anchor is required, never one before it.
    The compiler tail-merges string literals, so a short label is commonly
    the suffix of a longer one -- `SRC` is the last three bytes of
    `PAGE SRC` -- and demanding a leading NUL matches nothing at all.

    Resolves to the address of entry 0. Must match exactly once.
    """

    def __init__(self, strings, skip=0):
        self.strings = tuple(strings)
        # The anchor run starts at table entry `skip`, so entry 0 sits that
        # many pointers earlier. Digitakt (mk1) has no 'UNDEFINED' entry and
        # opens with sixteen numeric trig labels, which anchor nothing.
        self.skip = skip

    def _cstr(self, img, load_addr, addr, limit=64):
        off = addr - load_addr
        if off < 0 or off >= len(img):
            return None
        end = img.find(b'\x00', off)
        if end < 0 or end - off > limit:
            return None
        try:
            return img[off:end].decode('ascii')
        except UnicodeDecodeError:
            return None

    def resolve(self, img, load_addr, got):
        first = self.strings[0].encode()
        found = set()
        for off in _find_all(img, first + b'\x00'):
            ptr = load_addr + off
            for base in _find_all(img, struct.pack('>I', ptr)):
                if base % 4:
                    continue
                if base + 4 * len(self.strings) > len(img):
                    continue
                entries = [struct.unpack_from('>I', img, base + 4 * i)[0]
                           for i in range(len(self.strings))]
                if all(self._cstr(img, load_addr, e) == s
                       for e, s in zip(entries, self.strings)):
                    found.add(base)
        if len(found) != 1:
            return None, ('%d table(s) matching %d anchor string(s), need 1'
                          % (len(found), len(self.strings)))
        base = load_addr + found.pop() - 4 * self.skip
        return base, 'char* table at 0x%08x' % base


class SigAt:
    """A masked signature that must match at a fixed distance from an
    already-resolved symbol.

    For a routine compiled more than once, whose copies mask to the same
    signature and differ only in an operand we do not yet know, but which
    sits in the same place relative to a neighbour in every build. The case
    upstream hit is the display module's PIT3 handler and the intro's: the
    same routine -- ack the timer, post a semaphore -- compiled twice, so a
    plain `Sig` finds two matches and resolves to neither, while a `Fixed`
    would silently take whichever address was right on one build. Anchoring
    to a neighbour picks the right copy and the signature check still stops a
    layout change from quietly resolving to the wrong bytes."""

    def __init__(self, ref, symbol, delta, lo=0x40000000, hi=0x40400000):
        self.pat = _pattern(ref, lo, hi)
        self.symbol, self.delta = symbol, delta

    def resolve(self, img, load_addr, got):
        base = got.get(self.symbol)
        if base is None:
            return None, "'%s' unresolved" % self.symbol
        addr = base + self.delta
        off = addr - load_addr
        if not (0 <= off <= len(img) - len(self.pat)):
            return None, '0x%08x is outside the image' % addr
        if not self.pat.matches_at(img, off):
            return None, ('masked signature does not match at %s%+d (0x%08x)'
                          % (self.symbol, self.delta, addr))
        return addr, ('masked-signature match at %s%+d (0x%08x)'
                      % (self.symbol, self.delta, addr))


class AnyOf:
    """First sub-rule that resolves wins. Lets one symbol name cover products
    whose tables are laid out differently -- Digitakt II opens its control
    table with 'UNDEFINED', Digitakt (mk1) opens it with trig numbers.

    Order matters and is not just a preference: a `Fixed` fallback that still
    resolves on an image the earlier rules also matched is a latent trap, not
    a redundancy -- on the build where the signature stops matching, the
    fallback answers silently and may answer wrong. tools/symaudit.py checks
    every alternative independently and reports the ones that disagree."""

    def __init__(self, *rules):
        self.rules = rules

    def resolve(self, img, load_addr, got):
        whys = []
        for rule in self.rules:
            val, why = rule.resolve(img, load_addr, got)
            if val is not None:
                return val, why
            whys.append(why)
        return None, ' / '.join(whys)


def _find_all(img, needle):
    out, start = [], 0
    while True:
        i = img.find(needle, start)
        if i < 0:
            return out
        out.append(i)
        start = i + 1


def _shape_match(data, shape):
    for k in range(0, len(shape), 2):
        pair = shape[k:k + 2]
        if pair == '..':
            continue
        if '%02x' % data[k // 2] != pair:
            return False
    return True


# --------------------------------------------------------------------------
# The symbol table. Order matters: a rule may only depend on a symbol that
# resolves earlier in this list.
# --------------------------------------------------------------------------

# The five RTOS entry points. Verified byte-identical at these addresses in
# both Digitakt II 1.15C and Digitone II 1.10E -- the RTOS is linked at a
# stable base independent of the application above it. `entry` itself is
# application code (the reset handler), not RTOS, but its opening 7 bytes
# happen to match too (its 8th byte does not -- a build-specific operand --
# so only 7 are checked, not 8).
SYMBOLS = [
    ('entry', AnyOf(Sig(H(24, '0-3,6-9,18-21', '10:2e7c4800', 'f975914f1a2c697ea81bb9d1041a7b41'),
                        hi=DATA_HI),
                    Fixed(0x400004e8, verify=H(7, '', '3:0423d040', '458f9c248e6916110a73748092cdd97f'))), True),
    ('task_create', AnyOf(Sig(H(24, '', '3:0c72fcc2', 'c6356aeb22015cf6d412e5b513fb2b66'),
                              hi=DATA_HI),
                          Fixed(0x400015ac, verify=H(8, '', '3:0c72fcc2', '95fd4cf079667d8c430c34aaf331c232'))), False),
    ('task_start', AnyOf(Sig(H(32, '2-5,15-18,26-29', '6:27002f2f', 'd622e42a9ee93f104b08ae0888dc8884'),
                             hi=DATA_HI),
                         Fixed(0x400015f8, verify=H(8, '', '0:2f0240c2', '50b491cfdf3835117fff6bffb9f3a8ca'))), True),
    ('sem_pend', AnyOf(Sig(H(24, '4-7,14-17,20-23', '8:27004a91', '79d3b45690867b71bfafcf3a40f3fdb8'),
                           hi=DATA_HI),
                       Fixed(0x400016fe, verify=H(8, '', '2:000440c0', 'ea58b4bdeb69c932327394de1122462c'))), True),
    # The third alternative is the mk1 family's: one masked 12-byte window
    # that matches exactly once in Digitakt mk1 1.53 (0x4000168a) and in
    # Digitone mk1 1.43 (0x400017ea, the RTOS there sits +0x160 from the
    # Digitakt's), where the Digitakt II signature runs on into code that
    # differs.
    ('pend_b', AnyOf(Sig(H(24, '4-7', '9:0020116f', '6348228ed02ffd938343f88d9cab5754'),
                         hi=DATA_HI),
                     Fixed(0x4000168a, verify=H(8, '', '2:000440c1', '97c23ca96d2662aabfb7c2d69961bc03')),
                     Sig(H(12, '4-7', '8:27002011', '8ec05d55cb0125895e710e92d929f515'),
                         hi=DATA_HI)), False),

    # The two semaphore POST primitives, the counterparts of sem_pend/pend_b.
    # emu/semscan.py finds every call site with a literal semaphore operand to
    # decide which pends `unblock` may fake; see its docstring and longrun's
    # never-fake block. The two routines differ only in one branch offset
    # (674e vs 673e, the 26th byte), and these first 40 bytes carry no inlined
    # image address, so the same signature matches every build: upstream pins
    # them at 0x4000148c/0x400014fc on Digitakt II 1.15C and 1.16 and Digitone
    # II 1.10E and 1.11, and they land at 0x40001770/0x400017e0 here, the
    # documented +0x2e4 mk1 RTOS shift. Each matches exactly once.
    ('give', Sig(H(40, '6-9,15-18,30-33', '26:674e2280', '389c44848f022fc59852e752d4e6c109'), hi=DATA_HI), False),
    ('give_b', Sig(H(40, '6-9,15-18,30-33', '26:673e2280', 'f311034e8e67abf926e1904faca0fc24'), hi=DATA_HI), False),

    # Every static call site into task_create -- diagnostic (dspboot logs
    # entry/prio/tcb at each), not required for boot.
    ('task_create_sites', Xrefs('task_create'), False),

    # The one `jsr sem_pend` immediately followed by `508f` (addq.l #8,a7,
    # the transport routine's stack cleanup) and `203c <abs32>` (the
    # unrelated move that follows it in the same block) -- unique in both
    # builds tested. This is the DSP-transport completion wait: patching the
    # semaphore right before this call is what turns "block forever waiting
    # for hardware that will never reply" into "take the sem_pend fast path
    # and continue" -- see dspboot.py's module docstring for the full story.
    ('pend_call', XrefShape('sem_pend', shape='508f203c........'), True),

    # The abs32 operand of the `move.l #imm,d0` at pend_call+10 (6 bytes of
    # jsr, 2 of addq, 2 of move.l's own opcode) is the completion semaphore's
    # MUTEX field, 8 bytes into the semaphore struct -- so the semaphore
    # itself is that value minus 8.
    ('completion_sem', Operand('pend_call', at=10, adjust=-8), True),

    # A second, unrelated aPLib-style depacker's copy-loop entry, embedded in
    # MAIN OS itself (see dspboot.py). This exact 8-byte opcode idiom occurs
    # exactly once in both images tested.
    ('depack_copy', Opcode(H(8, '', '0:12da12da', '21e52dd3939b470010200632037e76d6')), True),

    # flash_read: HLE'd entirely (dspboot patches its call site to copy
    # straight out of the emulated flash and return), so what is captured
    # here is its whole calling convention -- stack frame setup through the
    # tail jump -- which happens to need no masking at all: none of these 24
    # bytes are an inlined address into the loaded image.
    ('flash_read', Sig(H(24, '', '19:184ebaf7', '711360ae35a3158a4d062f7dda98dbe4')), True),

    # panel_diff: the double-buffer diff/flush/swap (see panel.py). Its own
    # 16-byte opening sequence embeds one inlined pointer -- the FRONT buffer
    # variable's address -- which is exactly the thing that moves between
    # builds, so it is the one window masked out.
    ('panel_diff', Sig(H(16, '10-13', '6:1c7c2479', '8f99d10321c5d0efc7fc03b0115beb1e')), False),

    # fb_front / fb_back: the first two distinct pointer-sized values in
    # [0x40200000, 0x40400000) referenced within 0x120 bytes of panel_diff --
    # the FRONT and BACK buffer-pointer variables, read in that order because
    # the diff reads FRONT before it ever touches BACK.
    ('_fb_pair', OperandGroup('panel_diff', span=0x120, lo=0x40200000, hi=0x40400000, take=2), False),
    ('fb_front', Pick('_fb_pair', 0), False),
    ('fb_back', Pick('_fb_pair', 1), False),

    # transport / call_sites: NOT part of the verified table above -- an
    # extension of the same masked-signature technique, kept OPTIONAL because
    # it does not hold up under it. transport's own 12-byte opening sequence
    # resolves fine (unique in both images tested), but the 4 call sites that
    # `jsr` into it exist purely to log a diagnostic (dspboot's
    # do_transport_call prints which timeout argument was pushed -- it
    # changes no register or memory state) and Digitone was NOT verified to
    # resolve it. Left in specifically so that firmware degrades to "no
    # transport-call logging" rather than failing to boot -- see
    # dspboot.py: this is exactly the OPTIONAL-degrades-gracefully case.
    # The mk1 alternative is 0x400e8a70, and it is the same routine that
    # dspboot.py's header describes step for step: lazily create a mutex,
    # first call only install the completion ISR and zero a semaphore and
    # enable the two INTC sources, then write the microsecond timeout to
    # DTIM1_DTRR (0xfc074004), kick DTIM1_DTMR (0xfc074000) with 0x841b,
    # sem_pend, unlock. Two independent checks pin it: the semaphore it
    # pends on is 0x421ed5c8, which is exactly what `completion_sem` already
    # resolves to from a different anchor, and the two calls it makes are
    # `sem_pend` (0x400016fe) and the recursive-mutex unlock (0x400019b6),
    # both already named. The other candidate sharing the opening twelve
    # bytes, 0x400e6a48, only sets a bitfield in an MMIO byte.
    #
    # NOTE what this symbol is NOT: docs/REMAINING.md B.6 retracted the name.
    # It is a blocking usleep backed by DMA Timer 1, not the ColdFire->SHARC
    # transport; the name is kept because dspboot.py and its diagnostic use
    # it. Ten bytes match both candidates and fourteen match both again once
    # masking widens, so the signature is cut at a length that is stable.
    ('transport', AnyOf(Sig(H(12, '', '7:0c4ab944', '38ad6e96d7a8e2095c0f7b85faef5df2')),
                        Sig(H(42, '10-13,16-19,24-27,32-35,38-41', '28:664a4879', '0e38f3a9f253697e8c4c2b90a50473d0'),
                            hi=DATA_HI)), False),
    ('call_sites', Xrefs('transport'), False),

    # The sequencer's transport state word: 1 playing, 2 stopped (measured:
    # PLAY is the only key that moves it 0 -> 1). Anchored on the start
    # path, which writes 1 to it and to its neighbour and then checks a
    # flag -- three data operands, all masked; the anchor's shape is what
    # is unique. Tools used to hard-code 0x4199DC2C for this.
    ('transport_start', Sig(H(26, '4-7,10-13,16-19', '22:487800fa', '0769fe1929d9ed6433c1c00e4bfd5093'),
                            hi=DATA_HI), False),
    ('transport_state', Operand('transport_start', at=4), False),

    # Optional SLC-status predicate. Its masked signature identifies the
    # helper; its absolute status-byte operand begins at instruction offset 2.
    # The data-RAM operand moves between builds, so it is masked and extracted.
    ('slc_status_predicate',
     Sig(H(20, '2-5,12-15', '16:71004e75', '5fa28195b0aab4dde46d034f13fba31d'), lo=0x48000000, hi=0x50000000),
     False),
    ('slc_status_addr', Operand('slc_status_predicate', at=2), False),

    # ----------------------------------------------------------------
    # The scheduler's two variables. The context switcher is RTOS, so it
    # sits at a fixed address and is byte-identical between builds apart
    # from these two operands -- which is exactly what makes them
    # resolvable: `movea.l <CURRENT_TCB>,a0` at +8 and `movea.l
    # <READY_CURSOR>,a1` at +0x14, whose abs32 operands start two bytes
    # into each. emu/tasks.py and emu/gui.py used to hardcode Digitakt's.
    # ----------------------------------------------------------------
    ('ctx_switch', AnyOf(Sig(H(24, '0-3,10-13', '4:2f48fffc', '48d6876fb107457f4e447689265b420a'),
                             hi=DATA_HI),
                         Fixed(0x40000410, verify=H(10, '', '2:27002f48', 'fd0f1cccd5d2668cc4297a8ef78c88da'))), False),
    # move.l a0,current_tcb inside ctx_switch: the variable still holds the
    # outgoing task, A0 the incoming one (emu/taskprof.py).
    # The store back to current_tcb is at a stable RTOS address, but its
    # abs32 operand relocates with the application's RAM layout.  Resolve the
    # instruction rather than verifying 1.15C's embedded operand.
    ('ctx_switch_load', Sig(H(16, '2-5', '6:22884ce8', '306aefc13677970ebd6ed6a4568ed923'), hi=DATA_HI), False),
    ('current_tcb', Operand('ctx_switch', at=0x0a), False),

    # prio_heads: the base of the RTOS's per-priority ready-list array.
    # task_create's own arithmetic names it -- it computes
    # `TCB+8 = prio*4 + <base>` with an `addi.l #<base>,d0` twenty-six bytes
    # in -- so the base is read out of that instruction rather than
    # hardcoded. Decompiling task_create is also what gave the rest of the
    # TCB layout emu/tasks.py relies on: +0x00 next, +0x04 prev (circular
    # within one priority), +0x08 the address of this task's own head slot,
    # +0x48 saved a7.
    #
    # The array's LENGTH is not a constant anywhere; what bounds it is that
    # `current_tcb` sits immediately above the last slot, which on mk1 makes
    # 16 priorities (0x4399d758..0x4399d794, current_tcb at 0x4399d798).
    # emu/tasks.py derives the count that way instead of guessing, and the
    # words just past current_tcb hold small integers rather than pointers,
    # so a walk that ran off the end would be obvious rather than silent.
    ('prio_heads', Operand('task_create', at=0x1c), False),
    ('ready_cursor', Operand('ctx_switch', at=0x16), False),

    # ----------------------------------------------------------------
    # The intro's frame path. The boot intro paces itself off PIT3 and owns
    # vector 208 until it switches the timer off on its way out, at which
    # point the display module claims the same vector -- so "vector 208
    # still points at the intro's handler" is precisely the window in which
    # the intro is live, and emu/pit.py:intro_running tests exactly that.
    # It hardcoded Digitakt's handler address, so on any other build it
    # answered False throughout the intro and the GUI released the timers
    # into the middle of it.
    #
    # intro_done is the intro's exit sequence -- `clr.w d0` then `pea
    # <frame_sem+8>` then, four instructions later, `move.w d0,$fc08c000`,
    # which is the write that switches PIT3 off. Its two hard literals
    # (the PIT3 base and the INTC address) are what make it unique.
    # ----------------------------------------------------------------
    # mk1 compiles the same exit with the registers reallocated and without
    # the leading `clr.w d0`, so the signature cannot reach it and every
    # symbol below it fails with it -- frame_sem is an Operand on this one,
    # and intro_pit3_isr is narrowed by frame_sem, so one unresolved
    # signature took all three down. The mk1 sequence reads
    #   addq.l #4,a7 / pea frame_sem+8 / clr.w d1 / moveq #16,d4 /
    #   lea (0x400016fe).l,a2 / move.w d1,$fc08c000 / move.b d4,$fc05001c
    # -- the same PIT3-off write and the same INTC byte as the Digitakt II
    # form. It is anchored at the `addq.l #4,a7` and NOT at the `pea`, so
    # that frame_sem's at=4 below still lands on the pea's operand exactly
    # as it does on a signature match. Measured from boot400M with PIT3
    # delivered: 0x4006cb8e and 0x4006cb90 never execute, while 0x4006cb92
    # and 0x4006cb94 are each hit exactly once, on the tick that switches
    # PIT3 off and ends the intro.
    ('intro_done', AnyOf(Sig(H(48, '0-11,30-33,38-41', '12:141a33c0', '4783b8ef4d4e8bfe4ed789770769b354'),
                             hi=DATA_HI),
                         Fixed(0x4006cb92,
                               verify=H(24, '', '5:988bec42', '336ee100621edc97e64e56a82190bf97')),
                         # Digitone mk1 1.43 (0x40091aea): the Digitakt mk1
                         # sequence with d0/d1 where that has d1/d4. Masked:
                         # the pea and lea operands and the high half of the
                         # jsr's; anchored on the PIT3-off store.
                         Sig(H(34, '4-7,14-17,32-33', '18:33c0fc08', 'db80fceb41c3835b0d3fa82a3c57244d'))),
     False),

    # The `pea` operand at intro_done+4 is the frame semaphore's MUTEX
    # field, 8 bytes into the semaphore struct -- the same idiom as
    # completion_sem above, and the same -8.
    ('frame_sem', Operand('intro_done', at=4, adjust=-8), False),

    # Ack PIT3, post a semaphore, return. Two routines in each image match
    # this: the intro's handler and the display module's. They differ only
    # in the semaphore they post, so frame_sem is what tells them apart --
    # see SigWhere. (The other match is the display module's own PIT3
    # handler: 0x40125f3c on Digitakt, 0x40123384 on Digitone.)
    # The mk1 address is a measurement, not a guess: vector 208 holds
    # 0x4006c154 at every ladder rung while PIT3 is still enabled, and holds
    # the display module's 0x400e5cec with PIT3 switched off in a snapshot
    # taken after the intro -- which is the exact distinction intro_running
    # draws. Its pea operand is 0x41988be4, and that IS frame_sem, the same
    # relationship the SigWhere above asserts for the other products.
    ('intro_pit3_isr', AnyOf(SigWhere(H(44, '8-11,20-23,30-33', '12:c0007204', '244d55b2732e06108e8749a4e73c8ec4'),
                                      at=20, equals='frame_sem', hi=DATA_HI),
                             Fixed(0x4006c154,
                                   verify=H(28, '', '21:988be480', 'c5a50aab98be1e903b9f9db1a3681643')),
                             # The mk1 family: matches once in Digitakt mk1
                             # (0x4006c154) and once in Digitone mk1
                             # (0x40090cdc), and still has to post frame_sem.
                             SigWhere(H(48, '8-11,20-23,30-33', '44:4e732f0a', '611cb187032470b929fe76750f62aead'),
                                      at=20, equals='frame_sem', hi=DATA_HI)), False),

    # ----------------------------------------------------------------
    # The pend sites longrun.py must NOT force-satisfy: each is a wait
    # whose caller re-checks a condition and loops, so satisfying it turns
    # a sleep into an infinite spin. See longrun.RECHECK_PENDS for what
    # each one is. queue_recv is RTOS and byte-identical; sleep_pend is
    # the return address of the very `jsr sem_pend` that pend_call names.
    # ----------------------------------------------------------------
    ('queue_recv', AnyOf(Sig(H(24, '4-7,19-22', '11:2a001c22', 'ade0b041ec4a8fddcfc7138adb2a8d8a'),
                             hi=DATA_HI),
                         Fixed(0x40001c2a, verify=H(10, '', '1:8f60f240', 'bf924735cd9101a480914fbaaa8b3caa'))), False),
    # mk1 compiles the same park loop with different surrounding code, so the
    # signature (which runs on past the branch into the next block) cannot
    # reach it. Measured by disassembly at 0x4006cbb8:
    #     pea.l $41988be4   ; frame_sem, the semaphore the intro loop pends
    #     jsr (a2)          ; a2 = sem_pend, loaded at 0x4006cb9e
    #     addq.l #4,a7      ; <- THIS address is what `recheck` needs
    #     bra.b $4006cbb8   ; forever
    # It is the return address of the pend, because longrun's satisfy() tests
    # the return address popped off the stack, not the call site.
    #
    # This matters more than an optional symbol usually does. Unresolved,
    # unblock force-satisfies this pend on every iteration, so the intro task
    # never blocks and never yields the CPU -- and blocking here is precisely
    # what frees the CPU once the intro is over.
    ('intro_park', AnyOf(Sig(H(32, '14-17', '5:0a2f3c40', 'a3fb180d91e68e6d1919f16bca20194f')),
                         Fixed(0x4006cbc0,
                               verify=H(8, '', '1:8f60f442', '1e72e6b3db9d423b8c25de453d77ef55')),
                         # The mk1 family (Digitakt 0x4006cbc0, Digitone
                         # 0x40091b18): addq, bra back, then the next
                         # routine's opening movem, frame_sem's pea masked.
                         Sig(H(12, '4-7', '8:fb7e0000', '8caf0df1fae0bfc2fde00d96224031dc'),
                             hi=DATA_HI)), False),
    ('display_wait', Sig(H(32, '6-9,18-21', '2:60ec4879', '086a090b2539290a4a00499dde6f0fce')), False),

    # The display module's own PIT3 ISR (0x40125f3c) posts the progress
    # screen's frame semaphore here (`pea.l display_sem` before the give);
    # the progress-screen task pends on it once per frame at 0x40126132 as
    # well as at display_wait.
    ('display_frame_post', AnyOf(Sig(H(48, '2-5,12-15,28-31,34-41,44-47', '8:30804eb9', '2201f90568c83b1abfc9267cd0bda10e'),
                                     hi=DATA_HI),
                                 Fixed(0x400e5cfe, verify=H(8, '', '3:1cd06c80', '7c3ec9ff603ddf1134db55b6760b66f6'))), False),
    ('display_sem', Operand('display_frame_post', at=2), False),

    ('pump_wait', Sig(H(32, '8-11,13-16,27-30', '0:42002f43', '0788c725c619a52937679239717e1576')), False),
    ('sleep_pend', Offset('pend_call', 6), False),

    # tick_dispatch is the RTOS tick-paced dispatcher loop: pend_b(tick_sem);
    # mutex_lock(m); run every due callback; mutex_unlock(m); repeat. The
    # pend at the top of that loop IS the pacing -- it is what holds the
    # loop to one pass per real tick. Force-satisfying it (as longrun's
    # unblock briefly did) does not re-check a condition the way the other
    # recheck entries above do; it makes a tick-paced loop free-running,
    # which ran the 54-slot software timer wheel roughly 100x per real tick
    # and starved the priority-6 Main OS task before it could finish
    # initialising. Verified byte-identical at 0x40002a46 in both Digitakt
    # II 1.15C and Digitone II 1.10E, and the 36-byte signature below occurs
    # exactly once in each image. It covers the frame setup, the movem.l,
    # clr.l d2, the three lea.l loads of fixed RTOS routines (pend_b
    # 0x4000168a, mutex_lock 0x40001608, mutex_unlock 0x4000172a -- all at
    # stable RTOS addresses in both builds), the loop-top move.l d2,d3 /
    # addq.l #1,d3 / eor.l d3,d2, and the pea OPCODE only. The two pea
    # OPERANDS just past the signature are deliberately excluded: they are
    # the build-specific tick semaphore and mutex (Digitone 0x46488008 /
    # 0x464880d0, Digitakt 0x47d9ade0 / 0x47d9aea8).
    ('tick_dispatch', AnyOf(Sig(H(24, '8-15,18-21', '3:e848d73c', 'cbfe6c86c4a50cf05cc63dabf8a5525e'),
                                hi=DATA_HI),
                            Fixed(0x40002cfa, verify=H(36, '', '5:d73c0c42', '8a36d5ec77212e7a6adedc6a5af9211f'))), False),
    # tick_pend is the return address of the `jsr (a4)` at tick_dispatch+0x28;
    # jsr (aN) is two bytes, so the pend returns to tick_dispatch+0x2a -- the
    # same idiom as sleep_pend being pend_call + 6 above.
    ('tick_pend', Offset('tick_dispatch', 0x2a), False),

    # ----------------------------------------------------------------
    # Bitmap::setPixel / getPixel, and the blit that pushes a Bitmap into
    # the panel's framebuffer. Each of the pixel routines has a near-twin
    # 0x66 bytes further on that shares its first 48 bytes (the same
    # bounds checks against a different pixel format), which is what made
    # these ambiguous before: 64 bytes is where the two part company, and
    # it picks the right one of the pair in BOTH images -- on Digitakt the
    # one already known correct from measurement.
    # ----------------------------------------------------------------
    ('set_pixel', Sig(H(64, '', '59:10d282e5', '4dd8c25c902d100f2a03aec4deba6fcc')), False),
    ('get_pixel', Sig(H(64, '', '49:10d28274', '877b9a50d0e3f7330bdde5b4a23ec85d')), False),
    ('px_copy', Sig(H(32, '26-29', '15:0c2012b0', 'a997ffe54b73cae8a3282c7042c011b2')), False),

    # ----------------------------------------------------------------
    # The soft-float routines. This ColdFire has no FPU, so every float
    # operation is a libgcc-style routine and the intro's particle
    # simulation spends ~93% of all executed instructions in them --
    # emu/softfloat.py intercepts each entry and returns the host's answer
    # instead. It named all seven by literal Digitakt address, so on any
    # other build the HLE simply never fired and the guest ground through
    # the real arithmetic: measured on Digitone, the intro could not
    # finish a single frame in 20M instructions.
    #
    # 64 bytes each. Every one of these is unique in both images at 48, 64
    # and 80 bytes and resolves to the same address at each, so the window
    # is not sitting on a knife edge -- the short ones (abssf2, subsf3)
    # simply run past their own `rts` into the next routine, which is
    # stable because the whole libgcc block is emitted as a unit.
    # ----------------------------------------------------------------
    ('sf_mulsf3', Sig(H(64, '', '49:0000c208', '65dc2cddd0bb852a361287e2caca5ef0')), False),
    ('sf_subsf3', Sig(H(64, '', '2:001f0008', '0c8c629129804137e8eae8243e121acf')), False),
    ('sf_addsf3', Sig(H(64, '', '15:0c2040d0', 'e3300e2146aed7566d19e94df97f3e20')), False),
    ('sf_divsf3', Sig(H(64, '', '50:00b00881', '0ec8962845eceb70528bf5fd71606fa0')), False),
    ('sf_abssf2', Sig(H(64, '', '5:80001f4e', 'c03e5c8ced5365e71f858e7689fee2d5')), False),
    # fixsfsi and cmpsf2 are the two that the Digitakt II signatures alone
    # could NOT find on Digitakt mk1, because mk1's libgcc emits a different
    # shape for both -- not merely relocated code, which Sig already masks.
    # Each mk1 alternative below was identified by ORACLE rather than by
    # reading: emu/harness.call() runs the candidate on the real image with
    # known bit patterns and the answer is compared against the host's, under
    # exactly the deferral rules install() applies. Both are bit-exact over
    # the selftest's value list (fixsfsi 16/16, cmpsf2 400/400), and the same
    # harness re-confirms the five that were already resolved, so the method
    # is checked against known-good answers and not just against itself.
    #
    # fixsfsi: mk1 builds it out of two PC-relative helpers instead of the
    # compare-against-2^31 that Digitakt II inlines -- there is no 4F000000
    # anywhere in this image. The two `jsr (pc)` displacements are left
    # literal on purpose: masking them makes this collide with __floatsisf
    # at 0x40124a1a, which has the identical instruction shape.
    ('sf_fixsfsi', AnyOf(Sig(H(64, '20-23,34-37,58-61', '9:082f3c4f', 'b48571c2b875caf1c9d7a717765ba77c')),
                         Sig(H(32, '', '9:bafdac2e', 'e9adf5475ce29ed2ee2d7bd7bfae80ce'))), False),

    # cmpsf2: on Digitakt II this name resolves to one of the thin
    # link/pea/bsr WRAPPERS (__eqsf2, __ltsf2 and friends). mk1 has six such
    # wrappers and they all delegate to ONE comparison core, so the mk1
    # alternative deliberately names the core instead -- Ghidra says its only
    # five callers are those wrappers, so hooking it intercepts every
    # comparison rather than one sixth of them.
    #
    # Pointing the HLE at the core is safe for the same reason a wrapper is.
    # The handler pops only the return address and leaves the arguments, which
    # is precisely what an `rts` from the core does; each wrapper then
    # discards its own pushes through `unlk a6` regardless of where a7 sat.
    # The core's extra third argument is the value to return when the operands
    # are unordered, and _fast() declines every NaN before the handler can
    # answer, so that argument can never change an intercepted result.
    ('sf_cmpsf2', AnyOf(Sig(H(64, '', '18:fffffd46', '83b3b9032ee430cbaec26f2c09c4e7e0')),
                        Sig(H(48, '', '17:0c2c0002', 'dd800cd77b9f7a22451918d0246263c5'))), False),

    # ----------------------------------------------------------------
    # sd_bringup: the eSDHC/eMMC bring-up routine (FUN_4011d67a on Digitone,
    # FUN_4011fed6 on Digitakt). The two are instruction-for-instruction
    # identical; they differ only in relocated code addresses, the
    # driver-struct base, and the EXT_CSD DMA destination.
    #
    # It clears the driver's "storage is up" flag on entry and sets it only
    # after the whole init sequence completes. Everything that reads or
    # writes block storage returns -1 immediately while that flag is zero,
    # so nothing downstream works until it is set.
    #
    # sd_flag is that flag: the `clr.l (abs).l` operand 28 bytes into the
    # routine, which is also the driver-struct base. sd_status is the
    # driver's own status word at base+0x30, which emu/esdhc.py has to write
    # on command completion; it was previously hardcoded to Digitakt's
    # 0x44E26F1C, which on Digitone left every command looking permanently
    # in-progress.
    #
    # The signature STOPS at 26 bytes even though the routine's prologue is
    # longer: the 4-byte window at +26 is `42b9` (clr.l abs.l) followed by
    # the top half of the flag address, and 0x42b944e2 falls inside the
    # DATA_HI mask window, so extending the signature masks that window and
    # takes the two real bytes at +30..31 with it -- which differ between
    # builds, so a longer signature matches Digitakt and fails on Digitone.
    # This is exactly the hazard the DATA_HI comment at the top of the file
    # warns about. Measured: 40-byte signature = 1 hit on Digitakt, 0 on
    # Digitone; 26-byte = 1 hit on each, at 0x4011fed6 and 0x4011d67a
    # respectively.
    # ----------------------------------------------------------------
    # The Digitakt (mk1) build of this routine is not instruction-for-
    # instruction identical after all -- it sets d3 with `moveq #21` where the
    # other two use `moveq #-13` -- so the shared signature cannot match it.
    # Its opening bytes pin it instead, and sd_flag still falls out of the
    # operand 28 bytes in, which cross-checks against the command primitive:
    # the semaphore it pends on (0x421cca5c) is base+0x4C and the status word
    # it returns (0x421cca40) is base+0x30, and both give base 0x421cca10.
    ('sd_bringup', AnyOf(Sig(H(26, '8-11,14-17,20-23', '4:48d70c3c', '9a560a1c79cb047d17ab0057a63c2873'),
                             hi=DATA_HI),
                         Fixed(0x400e1f9e, verify=H(14, '', '6:0c3c42a7', 'bae444c67983c27c743bf364f86dcda4')),
                         # The mk1 family: 20 bytes, the jsr target and the
                         # pea operand masked, once in Digitakt mk1
                         # (0x400e1f9e) and once in Digitone mk1 (0x400f4e12;
                         # driver base 0x419c92a8, whose +0x4c is the
                         # semaphore its command primitive pends on).
                         Sig(H(20, '8-11,14-17', '4:48d70c3c', 'ff139347ed973dc4b2fa203ffe2389a2'),
                             hi=DATA_HI)), False),
    ('sd_flag', Operand('sd_bringup', at=28), False),
    ('sd_status', Offset('sd_flag', 0x30), False),

    # sd_capacity (+0x24): the card size in sectors, as sd_bringup computed it
    # from EXT_CSD's SEC_COUNT. Every +Drive write is range-checked against it
    # -- 0x400e2bd2 returns -10 and issues NO command when the target sector is
    # past it -- so a wrong value here silently drops writes rather than
    # failing them visibly. emu/esdhc.py reasserts it on restore because
    # snapshots taken before its SEC_COUNT byte order was fixed carry the
    # 30,208-sector figure in their RAM, and a resume does not re-run bringup.
    ('sd_capacity', Offset('sd_flag', 0x24), False),

    # sd_cmd_sem / sd_data_sem: two of the three RTOS semaphores the bring-up
    # routine creates in its phase-1 setup, via the "create semaphore, initial
    # count 0" primitive, at driver-struct offsets +0x34, +0x44 and +0x4c.
    # sd_cmd_sem (+0x4c) is the one the command primitive (FUN_4011d5b4 on
    # Digitone, FUN_40120... on Digitakt) pends on after every XFERTYP write;
    # sd_data_sem (+0x44) is the one pended after a data transfer completes,
    # including the EXT_CSD DMA read that ends the bring-up. On Digitone these
    # resolve to 0x44459058 / 0x44459068 / 0x44459070 and on Digitakt to the
    # same offsets from 0x44e26eec.
    ('sd_cmd_sem', Offset('sd_flag', 0x4C), False),
    ('sd_data_sem', Offset('sd_flag', 0x44), False),
    # SoC eDMA completion for the eSDHC bulk-data channel.  CMD18 waits for
    # this at sd_flag+0x3c before it waits for sd_data_sem.
    ('sd_dma_sem', Offset('sd_flag', 0x3C), False),

    # ----------------------------------------------------------------
    # The front-panel serial link. There is no memory-mapped key matrix to
    # find: tools/mmiotrace.py measured 60M post-intro instructions on each
    # build and saw zero GPIO, zero DSPI and zero unclaimed MMIO. The panel
    # is a separate microcontroller on UART8, and button and encoder events
    # arrive the way MIDI would -- eDMA channel 34 into a 1024-byte ring,
    # then vector 154 into the driver's receive callback.
    #
    # uart8_init is that driver's init routine, and it sits at the SAME
    # address in both builds: it is BSP-layer code, not relocated
    # application code, so it anchors as Fixed rather than by signature.
    # 81 of its first 96 bytes are identical across the two images and the
    # first 25 are a literal match; the verify window stops at 24 because
    # byte 25 begins the first per-build abs32 operand.
    #
    # Everything else chains off it, so none of it needs its own scan. The
    # instruction at +0x16 is `clr.l (abs).l` (42b9) and its operand at
    # +0x18 is the base of the driver's contiguous globals block. The field
    # offsets inside that block were read off both decompiles side by side
    # and are identical:
    #
    #     +0x10  RX ring base    0x4FE1A000 Digitakt / 0x4E502000 Digitone
    #     +0x30  consume index
    #     +0x40  receive callback pointer
    #
    # emu/serial.py hardcoded Digitakt's 0x4094CD84 / 0x4094CDA4 /
    # 0x4094CDB4 for these three. On Digitone they read 0xFFFFFFFF, so
    # feeding that build through it would have written into unmapped memory
    # rather than a ring.
    # ----------------------------------------------------------------
    ('uart8_init', AnyOf(Sig(H(24, '4-11,16-19', '0:2f02740f', 'd7cb9d4bf7f572dba5e9b510e1718cc9'),
                             hi=DATA_HI),
                         Fixed(0x400026f2, verify=H(24, '', '3:0f41f9ec', '1161d1a1d0e6ec9d1439dffeeb5eb3c7'))), False),
    ('_uart8_globals', Operand('uart8_init', at=0x18), False),
    # The globals block begins with the firmware's "TX transfer armed"
    # flag.  emu.edma's legacy-snapshot kick must read this image-relative
    # address: it is 0x4094cd74 on DT2 1.15C and 0x40964d74 on 1.16.
    ('uart8_tx_state', Offset('_uart8_globals', 0), False),
    ('uart8_ring_ptr', Offset('_uart8_globals', 0x10), False),
    ('uart8_consume_idx', Offset('_uart8_globals', 0x30), False),
    ('uart8_rx_callback', Offset('_uart8_globals', 0x40), False),

    # Head of UART8's free-space loop.  The code is stable across DT2
    # versions while its three globals-block operands relocate, so Sig masks
    # those operands and refuses ambiguity instead of retaining a 1.15C PC.
    ('uart8_tx_wait',
     Sig(H(32, '2-5,10-13,16-19', '25:04547436', 'b719294059a72f6af23c2e3920d7c3a0'),
         hi=DATA_HI), False),

    # The normal SSI0/eDMA50 completion ISR clears CINT50, sets
    # INTC1.INTFRCH bit 31 (software source 63), restores its scratch
    # registers, and returns.  An SSI model may only hand vector 191 over at
    # this narrow RTE boundary; changing PC from the INTFRCH memory-write hook
    # is unsafe.  Anchor on the MMIO/OR/tail sequence, then expose the RTE.
    ('_ssi0_dma_force_tail',
     Sig(H(18, '4-7', '8:4cd70103', '96193612aafb1553b51f8a3f22bf71b4')), False),
    # The Digitone mk1's transmit ISR (0x4009c2e4 in 1.43) toggles its DSP's
    # block clock before forcing source 63, and forces it with an absolute
    # `or.l d0,$fc04c010.l`, so its tail is laid out differently: anchored on
    # the `jsr` to the toggle (target masked), whose absence is what tells it
    # from the test-mode engine's copy of the same tail. Its rte is 0x1a in.
    ('_ssi0_dma_force_tail_dn',
     Sig(H(28, '2-5', '14:fc04c010', '0930956b0ddc8be99bc4f995ee851354')), False),
    ('ssi0_dma_force_rte', AnyOf(Offset('_ssi0_dma_force_tail', 0x10),
                                 Offset('_ssi0_dma_force_tail_dn', 0x1a)),
     False),

    # The factory test mode's own names for the front-panel controls, which
    # is the firmware telling us what each control code means rather than us
    # inferring it from what the screen did. Both are `char *` tables indexed
    # by control code and terminated by 0xFFFFFFFF, and the two products
    # genuinely differ: Digitakt's button table ends at 50 (SAMPLING) while
    # Digitone's continues to 54 (VOICE, ARP, PLUS, STACK, MINUS), which is
    # why Digitakt reports nothing meaningful for channel 6 bits 2..7.
    #
    # Anchored on enough leading entries to separate them from the other two
    # similar tables nearby -- one of which also starts with UNDEFINED, and
    # another of which also contains the ENCODER A..H labels.
    ('panel_button_names',
     AnyOf(StringTable(('UNDEFINED', 'TRIG', 'SRC', 'FLTR', 'AMP', 'FX', 'MOD')),
           # Digitakt (mk1): entries 0..15 are the trig numbers, so anchor on
           # the distinctive run that starts at entry 16.
           # skip=16: the anchors start at table entry 16, and entry 0 is
           # trig 1. Codes here are 0-BASED and index the table directly,
           # which is the firmware's own numbering -- measured from the code
           # byte it writes into its queue_send event record.
           StringTable(('BANK', 'PTN', 'TRK', 'FUNC',
                        'PATTERN MENU', 'GLOBAL', 'SAMPLE', 'TEMPO'), skip=16)),
     False),
    ('panel_encoder_names',
     AnyOf(StringTable(('UNDEFINED', 'ENCODER A', 'ENCODER B', 'ENCODER C')),
           # Digitakt (mk1) keeps the encoders in the same table as the
           # buttons; rotation code 1 is 'A', so entry 0 sits one before it.
           StringTable(('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'LEVEL/DATA'),
                       skip=1)),
     False),

    # ----------------------------------------------------------------
    # Post-intro progress markers. These are what tells you whether the OS
    # actually took over, and emu/gui.py reports them on its status line --
    # it named all three by Digitakt address, so on Digitone the line read
    # "mainloop 0  jobs 0" no matter what the firmware was doing.
    #
    # mainloop is the main application task's message-loop head: it pends on
    # its own queue, whose address is the `pea` operand two bytes in. That
    # queue is the one the DTIM3 handler posts to, and on Digitakt this task
    # is what wakes after the intro and starts everything else -- it spawns
    # the display and job-worker tasks, re-points vector 208 at the display
    # module's own PIT3 handler and arms DTIM3. display_start is that
    # re-pointing routine, recognisable by its two hard MMIO literals (the
    # PIT3 base 0xfc08c000 and the INTC at 0xfc050050/0xfc05001d).
    # ----------------------------------------------------------------
    # Byte +15 is the `moveq #N,%d1` immediately after the queue-receive call
    # -- 40 on Digitakt II 1.15C and Digitone II 1.10E, 41 on Digitone II 1.11.
    # It is a plain immediate, not an address, so the [lo, hi) masking does not
    # reach it and the signature missed 1.11 entirely: bootcheck then reported
    # MISSING: mainloop entered and PARTIAL_MAIN_OS on a run whose main
    # application task was in fact scheduled and drawing. Masking that one byte
    # keeps the match unique on all three builds.
    ('mainloop', Sig(H(24, '2-5,8-11,15,17-20', '21:8065e8', 'd06c26b717e4a7cd3438217ff9875268'),
                     hi=DATA_HI, wild=(15,)), False),
    ('main_queue', Operand('mainloop', at=2), False),
    ('job_pump', Sig(H(24, '', '11:38240f2a', '3b255c5baf95b4d2cc655cca8b5f50b2')), False),
    ('display_start', Sig(H(24, '', '0:701041f9', '458203e044388e3146df97c3328e5884')), False),

    # ----------------------------------------------------------------
    # UI-trace hook points: the UI queue, key dispatch to views, and view
    # activate/close. Verified by disassembly on Digitakt II 1.15C (see
    # emu/uitrace.py).
    # ----------------------------------------------------------------
    ('queue_send', AnyOf(Sig(H(24, '8-11,14-17', '19:28001822', '78d0c898e42c0c3eebe041f011d5d615'),
                             hi=DATA_HI),
                         Fixed(0x40001b7a, verify=H(8, '', '1:0a2f0220', 'a354081ed6518144747e2531c83b96ff'))), False),
    ('ui_queue',         Operand('mainloop', at=2), False),
    # ui_key_dispatch is a CALL SITE, not a function: the jsr inside the main
    # loop that hands a type-0 (key) queue item to the view controller, with
    # A2 still holding the item -- which is the only property emu/uitrace.py
    # actually relies on.
    #
    # Found on mk1 by running it rather than by reading: hook mainloop+0xc to
    # capture the popped item pointer, then record every PC in the main-loop
    # function where A2 equals it. Ten sightings for five taps (press and
    # release each) walked 0x4000b752..0x4000b77e, and the jsr in that run is
    # at 0x4000b764. Ghidra then confirmed the target 0x400c30fc has exactly
    # one caller in the whole image, that one. The main loop switches on the
    # type byte through a 26-way PC-relative jump table, which is why a linear
    # sweep cannot reach this code and an earlier port left the name stale.
    #
    # The signature masks all three call targets, so what stays unique is the
    # shape: dispatch, push the controller, push the 0x48(sp) argument, call,
    # push, call. Note it matches the Digitakt II verify pattern too --
    # `jsr <abs32>; move.l d2,-(a7)` -- which is the independent check that
    # these two names denote the same thing on both builds.
    ('ui_key_dispatch', AnyOf(Fixed(0x40033518, verify=H(8, '', '1:b9401072', '2163d7e26553ccb11c038507efedfbef')),
                              Sig(H(28, '2-5,14-17,22-25', '9:2f00484e', 'c99cd48edd0bb4d31f7cfdae01581d9a'))), False),
    ('view_offer', AnyOf(Sig(H(24, '', '5:0067c24a', 'a8fd81548ea8afd0c90ab9b21e272a5b'),
                             hi=DATA_HI),
                         Fixed(0x400cab78, verify=H(8, '', '4:4a0067c2', '9704d6e7b01881711b1075d0d3f90e77'))), False),
    ('view_activate', AnyOf(Sig(H(24, '0-3,18-21', '12:0034282f', 'f0e8448da4213f700b14fd507b4037e0'),
                                hi=DATA_HI),
                            Fixed(0x400c9a9e, verify=H(8, '', '3:efffd048', '70a587b8ba99951d821e4142113f1a0a')),
                            # The mk1 family (Digitakt 0x400c9a9e, Digitone
                            # 0x400e53b6).
                            Sig(H(20, '0-3', '15:2f003847', 'e5e1a14fada3568f7a603a9131a38e8c'),
                                hi=DATA_HI)), False),
    ('view_close', AnyOf(Sig(H(24, '', '19:00661870', 'e0f261d2517fa5b2abe8c1479a3e9f30'),
                             hi=DATA_HI),
                         Fixed(0x400c98b6, verify=H(8, '', '3:6f000848', '8d9ca5430657d8266ae2fee9b94a0933'))), False),
    ('view_closed_mark', AnyOf(Sig(H(24, '1-4,11-14,18-21', '6:002c670c', '68c7d1873e552e42894ab4abd9ad5fbc'),
                                   hi=DATA_HI),
                               Fixed(0x400c98ce, verify=H(8, '', '0:15400030', '0371163e0202bdf69efc684daa97377b'))), False),
    ('view_request_pop', AnyOf(Sig(H(24, '7-10', '16:10280020', '854d55fbeb3f2e235ae7e946f2993019'),
                                   hi=DATA_HI),
                               Fixed(0x400ca340, verify=H(8, '', '0:7001206f', 'f718821d07c2c66c7cc7f92e7d9ea69b'))), False),
    ('view_sweep', AnyOf(Sig(H(24, '14-21', '10:00342a3c', '31c400c280622ab64e886d77552b2ab6'),
                             hi=DATA_HI),
                         Fixed(0x400caa58, verify=H(8, '', '3:d048d77c', '79b0a9674388ed4ec29e9363dd395f67'))), False),
    # ui_tick_inc: the `addq.l #1,(abs).l` that advances the counter
    # emu/uitrace.py prints as `t=`. Three candidates on mk1 share that exact
    # shape (0x4007848c, 0x400d3574, 0x400eaed2) and disassembly cannot choose
    # between them, so the choice was MEASURED against uitrace's own stated
    # property -- the counter the key-repeat code advances, about 1.8 per
    # DTIM3 tick on Digitakt II. Holding a key for 382 DTIM3 ticks:
    #
    #   0x4007848c -> 0x4199e488     0 increments, never read
    #   0x400d3574 -> 0x439c828c   699 increments = 1.83 per tick   <-- this one
    #   0x400eaed2 -> 0x421ed68c     0 increments, never read
    #
    # The other two are in tasks that this build never runs. Both RAM operands
    # are masked (hi=DATA_HI), so what identifies it is the shape plus the
    # `ble.w` displacement.
    ('ui_tick_inc', AnyOf(Fixed(0x40110828, verify=H(8, '', '0:52b947dc', '7769109646e3a17806db0922f2f46c9d')),
                          Sig(H(18, '2-5,8-11', '12:6f0000d8', 'b055c3740bb6e46caea252fe9362321b'),
                              hi=DATA_HI)), False),
    ('ui_tick_counter',  Operand('ui_tick_inc', at=2), False),

    # ----------------------------------------------------------------
    # The Digitone's second CPU ("DSP" in its own strings; a second ColdFire,
    # not a SHARC). dsp_boot_task is the main CPU's task that uploads the
    # DSP's code and releases its reset (0x4008d56c in Digitone mk1 1.43; see
    # emu/dsplink.py). It loops on dsp_request_sem, whose `pea` operand sits
    # 0x3c bytes in; dsp_status (0 in progress, 1 boot failure, 2 running) is the
    # word the task clears 0x9e bytes in. Only the Digitone has this task, so
    # on every other image all three stay unresolved and nothing is modelled.
    # ----------------------------------------------------------------
    ('dsp_boot_task', Sig(H(48, '20-23,28-35,42-45', '1:ef72104f', '14585ecd817bd684aa190b0a6154acc6'),
                          hi=DATA_HI), False),
    ('dsp_request_sem', Operand('dsp_boot_task', at=0x3c), False),
    ('dsp_status', Operand('dsp_boot_task', at=0x9e), False),

    # Every `bra.b $self` (opcode 60FE) -- the RTOS idiom for "nothing to do,
    # wait for the scheduler's timer tick to preempt me". dspboot.py already
    # computes this itself with the same algorithm (find_idle_spins), because
    # it is needed even when nothing else in this file is -- kept here too so
    # `python -m emu.symbols` reports it and every caller has one place to
    # get it from.
    ('idle_spins', ScanAll('60fe', step=2), False),
]

_NAMES = {name for name, _, _ in SYMBOLS}


class Profile:
    """Symbols by attribute or item access: `profile.pend_call` or
    `profile['pend_call']`. An OPTIONAL symbol that failed to resolve reads
    as None either way -- callers must check, not assume.
    """

    def __init__(self, image_sha256, load_addr, values, detail):
        self._values = values
        self._detail = detail
        self.image_sha256 = image_sha256
        self.load_addr = load_addr
        self.unresolved = [n for n, v in values.items() if v is None]

    def __getattr__(self, name):
        if name in _NAMES:
            return self._values.get(name)
        raise AttributeError(name)

    def __getitem__(self, name):
        return self._values[name]

    def get(self, name, default=None):
        return self._values.get(name, default)

    def report(self):
        lines = ['profile sha256=%s...  load=0x%08x'
                 % (self.image_sha256[:16], self.load_addr)]
        for name, _, required in SYMBOLS:
            val = self._values.get(name)
            tag = 'REQUIRED' if required else 'optional'
            detail = self._detail.get(name, '')
            if val is None:
                lines.append('  %-18s %-8s UNRESOLVED -- %s' % (name, tag, detail))
            elif isinstance(val, tuple):
                lines.append('  %-18s %-8s %-4d item(s) -- %s' % (name, tag, len(val), detail))
            else:
                lines.append('  %-18s %-8s 0x%08x  -- %s' % (name, tag, val, detail))
        if self.unresolved:
            lines.append('unresolved: %s' % ', '.join(self.unresolved))
        return '\n'.join(lines)


_cache = {}   # image sha256 -> Profile


def resolve(image, load_addr=LOAD_ADDR):
    """-> Profile. Raises SymbolResolutionError if any REQUIRED symbol is
    unresolved or ambiguous, naming exactly which one(s) and why.

    Cached per image SHA-256 (an in-process dict) -- resolving scans a ~3MB
    image several times over and this is called from several modules on
    every run, not once at startup.
    """
    h = hashlib.sha256(image).hexdigest()
    cached = _cache.get(h)
    if cached is not None:
        return cached

    got, detail = {}, {}
    for name, rule, required in SYMBOLS:
        val, why = rule.resolve(image, load_addr, got)
        got[name] = val
        detail[name] = why

    missing = [name for name, _, required in SYMBOLS if required and got[name] is None]
    if missing:
        lines = ['%d REQUIRED symbol(s) failed to resolve:' % len(missing)]
        for name in missing:
            lines.append('  %-16s %s' % (name, detail[name]))
        raise SymbolResolutionError('\n'.join(lines))

    profile = Profile(h, load_addr, got, detail)
    _cache[h] = profile
    return profile


if __name__ == '__main__':
    from emu import config

    path = sys.argv[1] if len(sys.argv) > 1 else config.main_image()
    with open(path, 'rb') as fh:
        img = fh.read()
    print('image: %s (%d bytes)' % (path, len(img)))
    try:
        profile = resolve(img)
    except SymbolResolutionError as e:
        print(str(e))
        raise SystemExit(1)
    print(profile.report())
