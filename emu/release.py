"""Which firmware release is this .syx, and can this app run it?

emu/device.py answers "which device" by hash and refuses everything else.
That is right for the emulator -- guessing a product would run it under the
wrong panel -- but a launcher adding a firmware has to say WHAT a file is
before it spends a quarter of an hour building it: a release we have tested,
a newer release of a product we do run, another Elektron product, or not an
OS file at all.

The file says most of that itself, in its first few hundred bytes:

  * the SysEx framing message (the first 16 bytes) names the PRODUCT. Byte 4
    is the transport id and byte 8 the OS-stream id the bootstrap checks
    ('Incompatible OS' when it is another machine's). Bytes 12..14 count the
    data messages that follow, which is also the cheapest truncation check
    there is.
  * the ELE3 container header, 8 bytes into the decoded stream, names the
    RELEASE: the build number at +0x08 and the version right-justified in
    the twelve bytes +0x0C..+0x18. It is NOT a 4-byte field at +0x14: that
    reads '1.53' but cuts '1.15C' short. Section id 5 is the raw 15-byte
    build stamp '%y%m%d %H:%M:%S'.

So identification decodes one or two messages, then hashes the file. The
hash decides the status:

  known        a device file lists the hash: tested here, runs as is.
  untested     a device file claims the product's ids but not this hash. It
               runs only once the user agrees, through a per-firmware overlay
               device file that adds the hash (write_device_overlay), so
               emu.device.identify() itself never learns to guess.
  unsupported  no device file for the product. Named from a small table so
               the refusal can say what the file is, then refused.

Nothing here imports unicorn or emu.harness: the launcher calls this on the
Tk thread and it has to answer at once.
"""
import datetime
import hashlib
import os
import re
import struct
from dataclasses import dataclass, field

from dt2.container import MFR, COUNT_OFF, TABLE_OFF, ENTRY_SZ
from emu import device

FRAMING, DATA = 0x7F, 0x7E      # message type, byte 6
FRAMING_LEN, DATA_LEN = 16, 128  # whole messages, F0..F7 inclusive
PAYLOAD = slice(10, 126)         # a data message's 116 8-in-7 bytes
PREAMBLE = 8                     # u32 length, u32 checksum, then 'ELE3'
STAMP_ID, STAMP_FORMAT = 5, '%y%m%d %H:%M:%S'

HEAD_BYTES = 4096                # first read; mk1 needs 16 + 2 * 128 of it
HEAD_CAP = 1 << 17               # never read further than this for a header
STAMP_REACH = 1 << 16            # a stamp further in than this is ignored
MAX_SECTIONS = 64                # real files have 5; more is not a table
MAX_FILE = 64 << 20              # OS files are 1-3 MB; refuse to slurp a video

# (transport id, OS-stream id) -> (product, short). Only used to NAME a
# product no device file claims; a device file's own name and short win.
# Digitakt is measured on OS 1.53 and the Models on OS 1.13; the rest are
# docs/FINDINGS.md's table.
PRODUCTS = {
    (0x0A, 0x05): ('Digitakt', 'dt1'),
    (0x14, 0x0F): ('Digitakt II', 'dt2'),
    (0x15, 0x10): ('Digitone II', 'dn2'),
    (0x16, 0x11): ('Syntakt', 'syn'),
    (0x0D, 0x08): ('Digitone', 'dn1'),
    (0x11, 0x0C): ('Model:Cycles', 'mc'),
    (0x0F, 0x0A): ('Model:Samples', 'ms'),
}

KNOWN, UNTESTED, UNSUPPORTED = 'known', 'untested', 'unsupported'

# Windows refuses these as a file or folder name, with or without an
# extension ('nul.1-x' is still NUL), so a slug or file name must avoid them.
RESERVED = frozenset(['CON', 'PRN', 'AUX', 'NUL']
                     + ['COM%d' % n for n in range(1, 10)]
                     + ['LPT%d' % n for n in range(1, 10)])
SLUG_RE = re.compile(r'[a-z0-9][a-z0-9.-]{0,63}\Z')
SLUG_MAX = 64


class FirmwareError(ValueError):
    """Not an Elektron OS .syx, or a damaged one. The message says which,
    in words meant for the person who picked the file."""


@dataclass
class Header:
    """What the first few hundred bytes of an OS .syx say about it."""
    transport_id: int
    stream_id: int
    msg_count: int          # data messages the framing announces
    build: str
    version: str
    stamp: 'datetime.datetime | None'
    sections: list = field(default_factory=list)   # section ids, table order


@dataclass
class Release:
    """One firmware file, identified. See the module docstring for status."""
    device: 'device.Device | None'
    firmware: 'device.Firmware | None'
    product: str
    version: str
    build: str
    stamp: 'datetime.datetime | None'
    sha256: str
    status: str
    label: str
    slug: str


# --- reading -----------------------------------------------------------------

def _text(raw):
    """Header bytes -> printable ASCII, anything else shown as '?'.

    These strings end up in labels, file names and TOML; a stray control
    byte in a damaged header should read as damage, not break any of them.
    """
    return ''.join(chr(b) if 0x20 <= b < 0x7F else '?' for b in raw)


def _unpack_8in7(payload, out):
    """Append one message's decoded bytes to `out`. The same loop as
    dt2/container.decode_syx, which reads the whole file to do it."""
    k, n = 0, len(payload)
    while k < n:
        ms = payload[k]
        k += 1
        for bit in range(7):
            if k >= n:
                break
            out.append(payload[k] | (0x80 if (ms >> (6 - bit)) & 1 else 0))
            k += 1


def _walk(data, path, partial_ok):
    """-> (offset, message) for each F0..F7 message in `data`.

    A byte outside a message means this is not a SysEx file. An unterminated
    last message is a truncated file -- unless `data` is only the start of
    the file (`partial_ok`), when it is just where the read stopped.
    """
    i, n = 0, len(data)
    while i < n:
        if data[i] != 0xF0:
            raise FirmwareError(
                '%s is not an Elektron OS .syx: byte 0x%02x at offset %d is '
                'outside any SysEx message.' % (_name(path), data[i], i))
        j = data.find(b'\xf7', i)
        if j < 0:
            if partial_ok:
                return
            raise FirmwareError(
                '%s is truncated: it ends inside a SysEx message (offset %d). '
                'Download it again.' % (_name(path), i))
        yield i, data[i:j + 1]
        i = j + 1


def _name(path):
    return os.path.basename(os.fspath(path)) or os.fspath(path)


def _framing(data, path):
    """Check the 16-byte framing message that opens every OS file."""
    if data[:1] != b'\xf0' or data[1:4] != MFR:
        raise FirmwareError(
            '%s is not an Elektron OS .syx: it does not start with an '
            'Elektron SysEx message.' % _name(path))
    msg = data[:data.find(b'\xf7') + 1]
    if len(msg) != FRAMING_LEN or msg[6] != FRAMING:
        raise FirmwareError(
            '%s is an Elektron SysEx file but not an OS update: it has no '
            'OS framing message (a sound, pattern or project dump?).'
            % _name(path))
    return msg


def _decode_head(data, path):
    """-> the decoded stream carried by the whole data messages in `data`."""
    out = bytearray()
    for off, msg in _walk(data, path, partial_ok=True):
        kind = msg[6] if len(msg) > 6 else None
        if off == 0:
            continue
        if kind == FRAMING:            # the closing framing message
            break
        if kind == DATA and len(msg) == DATA_LEN:
            _unpack_8in7(msg[PAYLOAD], out)
    return bytes(out)


def _table(c):
    """-> [(id, offset, stored_len, dest), ...] as far as `c` holds it."""
    n = struct.unpack_from('>I', c, COUNT_OFF)[0]
    have = min(n, max(0, (len(c) - TABLE_OFF) // ENTRY_SZ))
    return n, [struct.unpack_from('>IIII', c, TABLE_OFF + ENTRY_SZ * k)
               for k in range(have)]


def _needed(c):
    """-> how many container bytes the header fields need, given what `c`
    already shows; 0 when `c` is enough or can never be (then the checks in
    read_header say why)."""
    if len(c) < TABLE_OFF:
        return TABLE_OFF
    if c[:4] != b'ELE3':
        return 0
    n, entries = _table(c)
    if n > MAX_SECTIONS:
        return 0
    end = TABLE_OFF + n * ENTRY_SZ
    if len(entries) < n:
        return end
    for sid, off, ln, _dest in entries:
        if sid == STAMP_ID and ln <= 64 and off + ln <= STAMP_REACH:
            end = max(end, off + ln)
    return end if end > len(c) else 0


def _parse_stamp(c, entries):
    for sid, off, ln, _dest in entries:
        if sid != STAMP_ID:
            continue
        raw = c[off:off + ln]
        if ln > 64 or len(raw) != ln:
            return None
        try:
            return datetime.datetime.strptime(
                raw.decode('ascii').strip(' \0'), STAMP_FORMAT)
        except (UnicodeDecodeError, ValueError):
            return None
    return None


def read_header(path):
    """-> Header, from the start of the file only (well under 0.1 s).

    Raises FirmwareError for anything that is not an Elektron OS .syx. It
    does NOT check the rest of the file; identify_release() does that.
    """
    try:
        with open(path, 'rb') as fh:
            size = os.fstat(fh.fileno()).st_size
            want = HEAD_BYTES
            data = fh.read(want)
            framing = _framing(data, path)
            while True:
                c = _decode_head(data, path)[PREAMBLE:]
                need = _needed(c)
                if not need or len(data) >= size or want >= HEAD_CAP:
                    break
                # 101 decoded bytes per 128-byte message, plus slack.
                want = min(HEAD_CAP, max(want * 4, FRAMING_LEN + 128 * (
                    (need + PREAMBLE) // 101 + 2)))
                fh.seek(0)
                data = fh.read(want)
    except OSError as exc:
        raise FirmwareError('cannot read %s: %s'
                            % (_name(path), exc.strerror or exc)) from exc

    if c[:4] != b'ELE3':
        if len(c) < TABLE_OFF and len(data) >= size:
            raise FirmwareError('%s is truncated: it ends before its firmware '
                                'header. Download it again.' % _name(path))
        raise FirmwareError(
            '%s is not an Elektron OS .syx: its first data messages do not '
            'hold an ELE3 container.' % _name(path))
    n, entries = _table(c)
    if n > MAX_SECTIONS or len(entries) < n:
        raise FirmwareError(
            '%s is damaged: its section table (%d entries) does not fit the '
            'file.' % (_name(path), n))
    return Header(
        transport_id=framing[4],
        stream_id=framing[8],
        msg_count=(framing[12] << 14) | (framing[13] << 7) | framing[14],
        build=_text(c[0x08:0x0C].strip(b' \0')),
        version=_text(c[0x0C:0x18].strip(b' \0')),
        stamp=_parse_stamp(c, entries),
        sections=[e[0] for e in entries],
    )


def _scan(path, header):
    """-> (sha256, data messages): hash the file and walk every message.

    Integrity here is structural: every message is Elektron's, for this one
    device, of the right length, and there are as many data messages as the
    framing announced. That catches a truncated or spliced download before
    the first run spends minutes on it. (Per-message checksums are not
    checked: a home-built image the emulator would run may not carry them.)
    """
    try:
        if os.path.getsize(path) > MAX_FILE:
            raise FirmwareError('%s is far too large for an OS update.'
                                % _name(path))
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as exc:
        raise FirmwareError('cannot read %s: %s'
                            % (_name(path), exc.strerror or exc)) from exc
    sha = hashlib.sha256(data).hexdigest()
    count = 0
    for off, msg in _walk(data, path, partial_ok=False):
        kind = msg[6] if len(msg) > 7 else None
        if msg[1:4] != MFR or msg[4] != header.transport_id:
            raise FirmwareError(
                '%s is damaged: the message at offset %d is not for the same '
                'device as the rest.' % (_name(path), off))
        if (kind, len(msg)) not in ((DATA, DATA_LEN), (FRAMING, FRAMING_LEN)):
            raise FirmwareError(
                '%s is damaged: unexpected %d-byte message at offset %d. '
                'Download it again.' % (_name(path), len(msg), off))
        if kind == DATA:
            count += 1
    return sha, count


# --- naming ------------------------------------------------------------------

def valid_slug(slug):
    """True if `slug` is safe as a firmware folder name on every system."""
    return (bool(SLUG_RE.match(slug)) and not slug.endswith('.')
            and slug.split('.')[0].upper() not in RESERVED)


def _slug_part(text):
    s = re.sub(r'[^a-z0-9.]+', '-', str(text).lower())
    s = re.sub(r'\.{2,}', '.', s)
    return s.strip('.-')


def make_slug(short, version, sha256):
    """-> '<short>-<version>-<sha256[:8]>', lower-cased and made safe.

    The hash prefix keeps two builds that share a version string apart (a
    re-issue, or a home-built image). A version that sanitises to nothing is
    left out rather than invented. Raises FirmwareError when no safe name
    comes out, which only a malformed device file's `short` can cause.
    """
    head = _slug_part(short)[:20].strip('.-')
    tail = _slug_part(sha256[:8])
    room = SLUG_MAX - len(head) - len(tail) - 2
    ver = _slug_part(version)[:max(room, 0)].strip('.-')
    slug = '-'.join(p for p in (head, ver, tail) if p)
    if not valid_slug(slug):
        raise FirmwareError('cannot make a safe folder name from %r, %r'
                            % (short, version))
    return slug


def _safe_filename(name):
    """-> `name` as a portable '<stem>.syx': letters, digits, '._-' only."""
    stem = name[:-4] if name.lower().endswith('.syx') else name
    stem = re.sub(r'[^A-Za-z0-9._-]+', '_', stem)
    stem = re.sub(r'\.{2,}', '.', stem).strip('._-')[:80].strip('._-')
    if not stem:
        stem = 'firmware'
    if stem.split('.')[0].upper() in RESERVED:
        stem = 'fw_' + stem
    return stem + '.syx'


def _default_syx_name(product, version):
    return _safe_filename('%s_OS%s' % (product, version or 'unknown'))


def canonical_syx_name(release):
    """-> the file name the .syx is stored under in its firmware folder.

    A known release keeps the name its device file gives it, so its snapshot
    directory (snapshots/<stem>) is the one the rest of the tree expects.
    Anything else is '<Product>_OS<version>.syx', made safe -- which is also
    the pattern the device files' own names follow.
    """
    fw = release.firmware
    if release.status == KNOWN and fw is not None and fw.filename:
        base = fw.filename.replace('\\', '/').rsplit('/', 1)[-1]
        if base.strip('. '):
            return _safe_filename(base)
    return _default_syx_name(release.product, release.version)


def _label(product, version, build, stamp, status):
    base = '%s OS %s' % (product, version or '(no version)')
    if status == KNOWN:
        return base
    if status == UNSUPPORTED:
        return '%s (not supported yet)' % base
    notes = ['build %s' % build] if build else []
    if stamp is not None:
        notes.append('built %s' % stamp.strftime('%Y-%m-%d'))
    notes.append('untested')
    return '%s (%s)' % (base, ', '.join(notes))


# --- identifying -------------------------------------------------------------

def _ids_of(dev):
    return (dev.sysex_id, dev.os_stream_id)


def _ids_differ(dev, ids):
    """True only if the device file DECLARES ids and they are not `ids`."""
    return any(have is not None and have != want
               for have, want in zip(_ids_of(dev), ids))


def identify_release(path, devices_dir=None):
    """-> Release for the .syx at `path`, checked against `devices_dir`.

    Raises FirmwareError if the file is not an Elektron OS .syx, is damaged
    or truncated, or if the device files contradict it (its hash listed
    under a product whose ids are not the file's).
    """
    header = read_header(path)
    sha, count = _scan(path, header)
    if count != header.msg_count:
        raise FirmwareError(
            '%s is truncated or damaged: it holds %d data messages, its '
            'header announces %d. Download it again.'
            % (_name(path), count, header.msg_count))
    ids = (header.transport_id, header.stream_id)
    try:
        devices = device.load_all(devices_dir)
    except device.DeviceError as exc:   # a SystemExit: never let it escape
        raise FirmwareError('cannot read the device files: %s' % exc) from None

    listed = [(d, d.firmware_for_sha256(sha)) for d in devices
              if d.firmware_for_sha256(sha) is not None]
    for dev, _fw in listed:
        if _ids_differ(dev, ids):
            raise FirmwareError(
                '%s is listed in %s as a %s release, but its SysEx ids '
                '0x%02x/0x%02x say otherwise. One of the device files is '
                'wrong.' % (_name(path), _name(dev.path or dev.name),
                            dev.name, ids[0], ids[1]))
    if len(listed) > 1:
        raise FirmwareError('%s is listed by more than one device file: %s.'
                            % (_name(path),
                               ', '.join(d.name for d, _ in listed)))

    table_name, table_short = PRODUCTS.get(ids, (None, None))
    if listed:
        dev, fw = listed[0]
        status = KNOWN
        version = header.version or fw.version or ''
    else:
        claims = [d for d in devices if _ids_of(d) == ids]
        dev = claims[0] if claims else None
        version = header.version
        if dev is not None:
            status = UNTESTED
            fw = device.Firmware(version, sha,
                                 _default_syx_name(dev.name, version))
        else:
            status, fw = UNSUPPORTED, None

    if dev is not None:
        product = dev.name
        short = dev.short or table_short or dev.name
    else:
        product = table_name or 'Elektron device 0x%02x/0x%02x' % ids
        short = table_short or 'elektron-%02x%02x' % ids
    return Release(
        device=dev, firmware=fw, product=product, version=version,
        build=header.build, stamp=header.stamp, sha256=sha, status=status,
        label=_label(product, version, header.build, header.stamp, status),
        slug=make_slug(short, version, sha))


# --- the overlay for an untested release -------------------------------------

def _toml_string(text):
    out = ['"']
    for ch in text:
        if ch in '"\\':
            out.append('\\' + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


def _same_dir(a, b):
    def norm(p):
        return os.path.normcase(os.path.realpath(os.path.abspath(p)))
    return norm(a) == norm(b)


def write_device_overlay(release, overlay_dir, devices_dir):
    """Copy the release's device file into `overlay_dir`, adding its hash.

    -> the path of the overlay device file.

    Pointing DT2_DEVICES at `overlay_dir` then makes emu.device.identify()
    accept this one file and nothing new besides: the shipped device files
    are never edited, and identify() never learns to guess. The file is
    written to a .tmp and renamed, and read back before this returns.
    """
    devices_dir = devices_dir or device.devices_dir()
    dev = release.device
    if dev is None:
        raise FirmwareError('%s is not supported yet: there is no device file '
                            'to extend.' % release.label)
    if _same_dir(overlay_dir, devices_dir):
        raise FirmwareError('refusing to write an overlay into the shipped '
                            'devices directory %s' % devices_dir)
    src = dev.path
    if dev.path:
        candidate = os.path.join(devices_dir, os.path.basename(dev.path))
        if os.path.isfile(candidate):
            src = candidate
    if not src or not os.path.isfile(src):
        raise FirmwareError('the device file for %s is missing (%s)'
                            % (dev.name, src))
    try:
        base = device.load(src)
    except device.DeviceError as exc:
        raise FirmwareError('cannot read %s: %s' % (src, exc)) from None
    if base.name != dev.name:
        raise FirmwareError('%s describes %s, not %s'
                            % (src, base.name, dev.name))

    with open(src, 'rb') as fh:
        text = fh.read()
    if base.firmware_for_sha256(release.sha256) is None:
        entry = (
            '\n# Added by digiemu (emu/release.py) for an UNTESTED release '
            'the user chose\n# to run: identified by its SysEx ids, not '
            'mapped by hand.\n'
            '[[firmware]]\nversion = %s\nsha256 = %s\nfilename = %s\n'
            % (_toml_string(release.version), _toml_string(release.sha256),
               _toml_string(canonical_syx_name(release))))
        if text and not text.endswith(b'\n'):
            text += b'\n'
        text += entry.encode('utf-8')

    os.makedirs(overlay_dir, exist_ok=True)
    dst = os.path.join(overlay_dir, os.path.basename(src))
    tmp = dst + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(text)
    os.replace(tmp, dst)
    try:
        check = device.load(dst)
    except device.DeviceError as exc:
        raise FirmwareError('the overlay %s does not parse: %s'
                            % (dst, exc)) from None
    if check.firmware_for_sha256(release.sha256) is None:
        raise FirmwareError('the overlay %s does not list %s'
                            % (dst, release.sha256))
    return dst
