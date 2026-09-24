"""Which product is this firmware, and what is on its front panel?

`emu/config.py` answers "which file", and `emu/symbols.py` answers "which
addresses". Neither answers "which DEVICE" -- config settles a tie between
firmware files by hardcoded FILENAME, which is how running one product under
another's name happens silently.

A device file under `devices/` is the missing piece. It carries only what the
image cannot tell us:

  * identity, keyed by the firmware's SHA-256. The filename is a hint for
    error messages; people rename firmware files, hashes do not change.
  * the product's SysEx ids (`sysex_id`, `os_stream_id`): bytes 4 and 8 of
    every OS file's framing message. They name the PRODUCT, not a release,
    so emu/release.py can say "an untested Digitakt release" for a hash no
    file lists. identify() below still matches by hash alone.
  * the wire mapping. The link carries (channel, bit) and the firmware reports
    a control code; channels 0..5 are linear and channel 6 is not, differently
    on each product.
  * display grouping, which is editorial -- it says how to arrange controls,
    not what they are.

Deliberately NOT here: control names and guest addresses. Names come from the
firmware's own factory-test tables via `emu/symbols.py`, and addresses are
resolved by signature. Freezing either into a data file would mean a new file
per firmware release and would lose exactly the property that makes both
products work from one code path.
"""
import hashlib
import os
import tomllib

DEVICES_DEFAULT = 'devices'

BUTTON = 'button'
ENCODER = 'encoder'


class DeviceError(SystemExit):
    """No device matched, or a device file is malformed. The message says how."""


class Firmware:
    """One known firmware release of a device."""

    def __init__(self, version, sha256, filename=None, acceptance=None):
        self.version = version
        self.sha256 = sha256.lower()
        self.filename = filename
        # [firmware.acceptance]: name -> guest address of a u32 that
        # emu/bootstrap.py reads from the settled snapshot before it accepts
        # a first boot (validate_acceptance there knows the names). None for
        # a release nobody has measured; then only the card is checked.
        self.acceptance = dict(acceptance) if acceptance else None

    def __repr__(self):
        return '<Firmware %s %s>' % (self.version, self.sha256[:12])


class Group:
    """A set of control codes and how to arrange them. Display only."""

    def __init__(self, name, kind, codes, layout='row', columns=None):
        self.name = name
        self.kind = kind
        self.codes = tuple(codes)
        self.layout = layout
        self.columns = columns

    def __repr__(self):
        return '<Group %s %s %d>' % (self.name, self.kind, len(self.codes))


class Device:
    """A product: its identity, its wire mapping and its panel arrangement."""

    def __init__(self, name, short, firmwares, linear_channels, encoders,
                 exceptions, groups, path=None, intro_channels=(),
                 intro_unblocks_frame_sem=True, post_intro_ips=0,
                 labels=None, leds=None, page_leds=(), audio=None,
                 sysex_id=None, os_stream_id=None, card_ekfs=True,
                 panel_kind=None, encoder_counts=1, ddr_bytes=None,
                 ui_card=None, straps=None):
        self.name = name
        self.short = short
        # [card] ekfs: whether the +Drive carries an ekFS sample volume that
        # has to be formatted before the cold boot (emu/bootstrap.py). The
        # Digitone has no sample engine and no such volume: its first boot
        # initialises a blank card by itself.
        self.card_ekfs = bool(card_ekfs)
        # [panel] kind: which front panel window draws this product
        # (emu/dnpanel.py for "digitone"). None means the Digitakt's.
        self.panel_kind = panel_kind
        # [panel] encoder_counts: wire counts per detent a window sends. The
        # mk1 panel drivers have a dead zone (see devices/digitone.toml), so
        # one count per mouse-wheel notch needed ~16 notches before anything
        # moved.
        self.encoder_counts = int(encoder_counts)
        # The SysEx framing ids of this product's OS files: byte 4 (the
        # transport/device id) and byte 8 (the OS-stream id the bootstrap
        # checks -- a mismatch is its 'Incompatible OS'). None when the
        # device file does not say, and then no release is matched by ids.
        self.sysex_id = sysex_id
        self.os_stream_id = os_stream_id
        self.firmwares = tuple(firmwares)
        self.linear_channels = linear_channels
        self.encoders = encoders
        self.exceptions = dict(exceptions)
        self.groups = tuple(groups)
        self.path = path
        # How this product's boot intro has to be driven. The two known
        # behaviours are opposites, so there is no default that suits both.
        # These defaults are the Digitakt II ones, so a device file that says
        # nothing keeps exactly the behaviour it had before this existed.
        # See the [intro] table in devices/digitakt.toml.
        self.intro_channels = tuple(intro_channels)
        self.intro_unblocks_frame_sem = bool(intro_unblocks_frame_sem)
        # Timer rate applied once the intro hands over, in instructions per
        # emulated second. 0 means "keep whatever the caller defaults to",
        # which is what every device did before this existed. It decides how
        # much EMULATED time passes per wall second: emulated_rate =
        # host_instructions_per_second / post_intro_ips, so a smaller number
        # is a more responsive window.
        self.post_intro_ips = int(post_intro_ips or 0)
        # Measured control names, code -> name. These override the firmware's
        # own table, which on this product labels the panel-test screen rather
        # than the runtime key map. Empty for a device that has not been
        # measured, which then keeps the table's names.
        self.labels = dict(labels or {})
        # Key LEDs, LED id -> the button code of the key it lights, and the
        # LED ids with no key of their own (pattern-page LEDs), in order.
        # Empty for a device whose LED stream has not been decoded.
        self.leds = dict(leds or {})
        self.page_leds = tuple(page_leds)
        # How to run and record the audio output ([audio] in the device
        # file): ssi_profile, request_hz, rate, sample_bits. None for a
        # device whose audio path has not been modelled.
        self.audio = dict(audio) if audio else None
        # [memory] ddr_mb: the DDR the board has fitted, in bytes, for the
        # strict check's memory model (harness.Machine.set_ddr). None when
        # the device file does not say.
        self.ddr_bytes = ddr_bytes
        # [boot] ui_card: the front-panel card type the bootstrap expects
        # the panel controller to report (emu/bootrom.py PanelLink). None
        # when the device file does not say.
        self.ui_card = ui_card
        # [boot] straps: GPIO bytes the board fixes, {address: value}, read
        # by the bootstrap (emu/bootrom.py BootHardware).
        self.straps = dict(straps or {})

    def __repr__(self):
        return '<Device %s>' % self.name

    def wire_for(self, code):
        """-> (channel, bit) for a BUTTON code, or None if this product has none.

        A code with no wire position is not an error: Digitakt simply has
        fewer channel-6 controls than Digitone.
        """
        if code in self.exceptions:
            return self.exceptions[code]
        if 1 <= code <= self.linear_channels * 8:
            return ((code - 1) // 8, (code - 1) % 8)
        return None

    def code_at(self, channel, bit):
        """-> the BUTTON code reported by a (channel, bit), or None."""
        for code, pos in self.exceptions.items():
            if pos == (channel, bit):
                return code
        if 0 <= channel < self.linear_channels and 0 <= bit < 8:
            return channel * 8 + bit + 1
        return None

    def button_groups(self):
        """-> how many button groups (wire channels) the panel reports: one
        report per group, which is what a bootstrap asks for at power-on
        (emu/bootrom.py PanelLink)."""
        top = max((ch for ch, _bit in self.exceptions.values()), default=-1)
        return max(top + 1, self.linear_channels)

    def encoder_channel(self, code):
        """-> the wire channel for an ENCODER rotation code, or None.

        Rotation codes are a separate space from the encoder push buttons:
        rotation `code` is wire channel `code - 1`.
        """
        if 1 <= code <= self.encoders:
            return code - 1
        return None

    def button_codes(self):
        """-> every button code this product's groups declare, in order."""
        return tuple(c for g in self.groups if g.kind == BUTTON
                     for c in g.codes)

    def firmware_for_sha256(self, sha256):
        for fw in self.firmwares:
            if fw.sha256 == sha256.lower():
                return fw
        return None


def _require(table, key, where):
    if key not in table:
        raise DeviceError('%s: missing [%s]' % (where, key))
    return table[key]


def load(path):
    """-> Device parsed from one TOML device file."""
    with open(path, 'rb') as fh:
        raw = tomllib.load(fh)
    dev = _require(raw, 'device', path)
    panel = _require(raw, 'panel', path)
    firmwares = [Firmware(f.get('version'), _require(f, 'sha256', path),
                          f.get('filename'),
                          _acceptance(f.get('acceptance'), path))
                 for f in raw.get('firmware', ())]
    # TOML bare keys are strings even when they look like integers, so the
    # exception table's control codes arrive as '49' rather than 49.
    exceptions = {}
    for code, pos in panel.get('exceptions', {}).items():
        if len(pos) != 2:
            raise DeviceError('%s: exception %s must be [channel, bit]'
                              % (path, code))
        exceptions[int(code)] = (int(pos[0]), int(pos[1]))
    groups = [Group(g.get('name'), g.get('kind', BUTTON), g.get('codes', ()),
                    g.get('layout', 'row'), g.get('columns'))
              for g in panel.get('group', ())]
    intro = raw.get('intro', {})
    return Device(
        name=_require(dev, 'name', path),
        short=dev.get('short'),
        firmwares=firmwares,
        linear_channels=int(panel.get('linear_channels', 6)),
        encoders=int(panel.get('encoders', 9)),
        exceptions=exceptions,
        groups=groups,
        path=path,
        intro_channels=[int(c) for c in intro.get('channels', ())],
        intro_unblocks_frame_sem=bool(intro.get('unblocks_frame_sem', True)),
        post_intro_ips=int(intro.get('post_intro_ips', 0) or 0),
        labels={int(k): str(v) for k, v in panel.get('labels', {}).items()},
        leds={int(k): int(v) for k, v in panel.get('leds', {}).items()},
        page_leds=[int(v) for v in panel.get('page_leds', ())],
        audio=_audio(raw.get('audio'), path),
        sysex_id=_sysex_byte(dev, 'sysex_id', path),
        os_stream_id=_sysex_byte(dev, 'os_stream_id', path),
        card_ekfs=_bool(raw.get('card', {}), 'ekfs', True, path),
        panel_kind=panel.get('kind'),
        encoder_counts=_counts(panel, path),
        ddr_bytes=_ddr(raw.get('memory', {}), path),
        ui_card=_byte(raw.get('boot', {}), 'ui_card', path),
        straps=_straps(raw.get('boot', {}), path),
    )


def _straps(table, where):
    """-> [boot] straps as {address: byte}. TOML keys are strings, so the
    addresses are written in hex: straps = { "0xEC09401B" = 0x08 }."""
    out = {}
    for key, value in (table.get('straps') or {}).items():
        try:
            addr = int(str(key), 0)
        except ValueError:
            raise DeviceError('%s: [boot] straps key %r is not an address'
                              % (where, key)) from None
        if isinstance(value, bool) or not isinstance(value, int)                 or not 0 <= value <= 0xFF:
            raise DeviceError('%s: [boot] straps %s must be a byte, got %r'
                              % (where, key, value))
        out[addr] = value
    return out


def _byte(table, key, where):
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 0 <= value <= 0xFF:
        raise DeviceError('%s: %s must be a byte, got %r' % (where, key, value))
    return value


def _ddr(table, where):
    """-> [memory] ddr_mb in bytes, or None. The DDR controller takes one
    part of 16 to 256 MB (MCF54418RM 1.7.11), so anything else is a typo."""
    value = table.get('ddr_mb')
    if value is None:
        return None
    if isinstance(value, bool) or value not in (16, 32, 64, 128, 256):
        raise DeviceError('%s: [memory] ddr_mb must be 16, 32, 64, 128 or '
                          '256, got %r' % (where, value))
    return value << 20


def _counts(panel, where):
    value = panel.get('encoder_counts', 1)
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 1 <= value <= 16:
        raise DeviceError('%s: [panel] encoder_counts must be 1..16, got %r'
                          % (where, value))
    return value


def _bool(table, key, default, where):
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise DeviceError('%s: %s must be true or false, got %r'
                          % (where, key, value))
    return value


def _sysex_byte(table, key, where):
    """-> an optional [device] id as an int 0..0x7F, or None if absent.

    These are SysEx data bytes, so anything outside 7 bits cannot be one and
    is a typo worth refusing rather than a product nobody will ever match.
    """
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) \
            or not 0 <= value <= 0x7F:
        raise DeviceError('%s: [device] %s must be an integer 0..0x7f, got %r'
                          % (where, key, value))
    return value


def _acceptance(table, where):
    """-> [firmware.acceptance] as {name: int address}, or None if absent."""
    if table is None:
        return None
    if not isinstance(table, dict) or any(
            isinstance(v, bool) or not isinstance(v, int)
            or not 0 <= v <= 0xFFFFFFFF for v in table.values()):
        raise DeviceError('%s: [firmware.acceptance] must map names to '
                          '32-bit addresses' % where)
    return dict(table)


def _audio(table, where):
    """-> the [audio] table checked and typed, or None if there is none."""
    if not table:
        return None
    out = {'ssi_profile': str(_require(table, 'ssi_profile', where)),
           'request_hz': int(_require(table, 'request_hz', where)),
           'rate': int(table.get('rate', 48000)),
           'sample_bits': int(table.get('sample_bits', 24)),
           'ips': int(table.get('ips', 0) or 0),
           'fallback_request_hz': int(table.get('fallback_request_hz', 0)
                                      or 0)}
    if out['request_hz'] <= 0 or out['rate'] <= 0 or out['ips'] < 0 \
            or out['fallback_request_hz'] < 0:
        raise DeviceError('%s: [audio] rates must be positive' % where)
    if not 16 <= out['sample_bits'] <= 32:
        raise DeviceError('%s: [audio] sample_bits must be 16..32' % where)
    return out


def devices_dir():
    return os.environ.get('DT2_DEVICES', DEVICES_DEFAULT)


def load_all(dirpath=None):
    """-> every Device under `dirpath`, sorted by name."""
    dirpath = dirpath or devices_dir()
    if not os.path.isdir(dirpath):
        raise DeviceError('No devices directory at %s' % dirpath)
    out = [load(os.path.join(dirpath, n))
           for n in sorted(os.listdir(dirpath)) if n.endswith('.toml')]
    return sorted(out, key=lambda d: d.name)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def identify(firmware_path, dirpath=None):
    """-> (Device, Firmware) for a firmware file, by content hash.

    Refuses rather than guesses. An unknown hash is a real answer: it means
    this is a firmware release nobody has mapped, not that it is one of the
    two we know.
    """
    sha = sha256_of(firmware_path)
    for device in load_all(dirpath):
        fw = device.firmware_for_sha256(sha)
        if fw is not None:
            return device, fw
    raise DeviceError(
        'No device file matches %s\n\n'
        '  sha256 %s\n\n'
        'Known releases are listed in %s/. This is either a firmware version\n'
        'nobody has mapped yet or a different product; either way, guessing\n'
        'which one would run it under the wrong panel.'
        % (firmware_path, sha, dirpath or devices_dir()))
