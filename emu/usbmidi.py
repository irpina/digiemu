"""USB MIDI on the Digitone (mk1), at the OS's own USB MIDI queue and sender.

The USB device controller is not modelled, so nothing here enumerates or moves
USB transfers. What is modelled is the edge of the firmware's USB MIDI class
driver, both ways, the way the analog-emu Rytm MKII port does it:

In. On the unit, each completed bulk OUT transfer (a run of 4-byte USB-MIDI
event packets) is handed by the USB interrupt to the OS's deferred-call
queue: the interrupt posts (parser, length, buffer), and a task later runs
the parser, which sorts the packets by cable number, unpacks each one by its
code index and hands the bytes of cable 0 (and of cable 1) to the MIDI
input handler registered for it. `UsbMidi` makes that same post, with
packets it built from the host's bytes in buffers of its own, so the
firmware's parser, handlers, SysEx/RPC dispatch and semaphores all run as on
the unit. It posts from the idle task, and from the parser's own task when
the parser hands a buffer back. One post carries at most 128 packets, one
high-speed bulk transfer (512 bytes), and only one is out at a time, as the
unit's endpoint is primed again only once the parser hands its buffer back.

Out. The class driver has two senders, each taking (length, pointer) to
plain MIDI bytes: one packs a message stream into event packets and queues
it on the bulk IN endpoint, the other sends a single short realtime or
program-change message. Both return at once on a unit no host has
configured, which is the emulator's case, so their entries are hooked: the
bytes go to `sink` and the sender returns to its caller.

The addresses come from emu/symbols.py (usb_midi_send, usb_midi_send_short,
usb_rx_done, usb_defer, usb_midi_parse, usb_rx_release, usb_midi_short_ring);
on an image where any of them does not resolve, install() installs nothing.
The input buffers sit in the USB driver's DMA area, past the 32 bytes the
short sender uses: no code in either Digitone build addresses that range.

The host side is a TCP link (`LinkServer`), a plain MIDI byte stream each
way, so an emulator running where the host's MIDI ports are out of reach (in
WSL, under Windows) can still be reached: tools/usbmidi_relay.py joins it to
two MIDI ports on the host, "dn-emu usb in" and "dn-emu usb out".
"""
import collections
import socket
import struct
import threading

from unicorn import UC_HOOK_CODE
from unicorn.m68k_const import UC_M68K_REG_A7, UC_M68K_REG_D0, UC_M68K_REG_PC

from emu.midi import Parser

SYMBOLS = ('usb_midi_send', 'usb_midi_send_short', 'usb_defer',
           'usb_midi_parse', 'usb_rx_release', 'usb_midi_short_ring',
           'idle_spins')
# One high-speed bulk OUT transfer: 512 bytes, 128 event packets. The
# parser's per-cable output buffers hold 384 bytes, which is 128 packets of
# three MIDI bytes, so a post must not carry more.
PACKETS = 128
# The input buffers, as an offset from the short sender's 32-byte ring, and
# how many transfers may wait in the deferred-call queue at once. One: with
# four, long data-store writes (8 KB SysEx blocks) arrived corrupt most of the
# time ("Invalid package checksum"); with one, 4 of 4 passed and an OS upgrade
# took as long as before.
RX_OFFSET = 0x800
BUFFERS = 1
CABLE = 0
# The link's TCP port: analog-emu's Overbridge link uses 7301.
DEFAULT_PORT = 7311


def packets(msg, cable=CABLE):
    """One whole MIDI message (as from emu.midi.Parser) -> its USB-MIDI 1.0
    event packets, 4 bytes each."""
    head = cable << 4
    status = msg[0]
    if status == 0xF0:
        out = bytearray()
        rest = list(msg)
        while len(rest) > 3:
            out += bytes([head | 0x4] + rest[:3])
            rest = rest[3:]
        cin = {1: 0x5, 2: 0x6, 3: 0x7}[len(rest)]
        return bytes(out + bytes([head | cin] + rest + [0] * (3 - len(rest))))
    if status >= 0xF8:
        cin = 0xF
    elif status >= 0xF0:
        cin = {0xF1: 0x2, 0xF3: 0x2, 0xF2: 0x3, 0xF6: 0x5}.get(status)
        if cin is None:
            return b''
    else:
        cin = status >> 4
    body = list(msg[:3])
    return bytes([head | cin] + body + [0] * (3 - len(body)))


class UsbMidi:
    """The Digitone's USB MIDI port: `put` from any thread for the host's
    bytes in, `sink(bytes)` for what the firmware sends."""

    def __init__(self, sink=None):
        self.sink = sink
        self._lock = threading.Lock()
        self._parser = Parser()
        self._packets = bytearray()     # encoded, waiting for the guest
        self.m = None
        self.p = None
        self._spins = frozenset()
        self._bufs = ()
        self._busy = set()              # our buffers not handed back yet
        self._parsing = None            # ours the parser is on
        self._calls = {}                # (address, sp on return) -> sp before
        self.received = 0               # host bytes taken by put()
        self.posted = 0                 # transfers handed to the guest
        self.sent = 0                   # bytes the firmware sent

    def put(self, data):
        msgs = self._parser.feed(data)
        with self._lock:
            self.received += len(data)
            for msg in msgs:
                self._packets += packets(msg)

    @property
    def pending(self):
        with self._lock:
            return len(self._packets)

    def install(self, m, at, profile, ev=None):
        """Hook the senders and the idle spins. -> False (and nothing
        installed) when this image lacks any of the addresses.

        `ev` is longrun.build's: under `unblock` the waits a USB request
        makes on another task must be real ones (see _real_waits)."""
        missing = [n for n in SYMBOLS if profile.get(n) in (None, ())]
        if missing:
            self.missing = missing
            return False
        self.m, self.p = m, profile
        base = profile.usb_midi_short_ring + RX_OFFSET
        self._bufs = tuple(base + PACKETS * 4 * k for k in range(BUFFERS))
        m.ensure(base)
        at(profile.usb_midi_send, self._on_send)
        at(profile.usb_midi_send_short, self._on_send)
        at(profile.usb_midi_parse, self._on_parse)
        at(profile.usb_rx_release, self._on_release)
        self._spins = frozenset(profile.idle_spins)
        for addr in self._spins:
            at(addr, self._on_idle)
        if ev is not None:
            self._real_waits(m, profile, ev)
        return True

    @staticmethod
    def _real_waits(m, profile, ev):
        """`unblock` fakes any wait that would block. The USB requests the
        firmware answers (Elektron's RPC: a screenshot, starting an OS
        upgrade) hand work to another task and wait on a semaphore for it,
        and a faked wait answers before the work is done: a blank screen,
        or "Could not initiate OS upgrade". Those semaphores are made at run
        time, mostly on the waiting task's stack, so every semaphore set up
        from here on is a real one. The waits `unblock` exists for, on
        hardware the emulator lacks, were all set up during the boot.

        The settled snapshots also hold a count of 1 on the screenshot's
        semaphore with nobody waiting, left by the emulated boot (on a unit
        that count would make every screenshot blank, and they are not), so
        that count goes back to 0."""
        skip = ev.get('unblock_skip')
        if skip is None:
            return
        if profile.get('sem_init') is not None:
            def init(uc, address, size, data):
                sp = uc.reg_read(UC_M68K_REG_A7)
                skip.add(struct.unpack('>I', uc.mem_read(sp + 4, 4))[0])
            m.uc.hook_add(UC_HOOK_CODE, init, begin=profile.sem_init,
                          end=profile.sem_init)
        sem = profile.get('rpc_screen_sem')
        if sem is not None:
            skip.add(sem)
            if struct.unpack('>i', bytes(m.uc.mem_read(sem, 4)))[0] > 0:
                m.uc.mem_write(sem, bytes(4))

    # -- out -------------------------------------------------------------

    def _on_send(self, uc, address, size, data):
        sp = uc.reg_read(UC_M68K_REG_A7)
        ret, length, ptr = struct.unpack('>III', uc.mem_read(sp, 12))
        if 0 < length <= 0x10000:
            out = bytes(uc.mem_read(ptr, length))
            self.sent += len(out)
            if self.sink is not None:
                self.sink(out)
        uc.reg_write(UC_M68K_REG_D0, 0)
        uc.reg_write(UC_M68K_REG_A7, sp + 4)
        uc.reg_write(UC_M68K_REG_PC, ret)

    # -- in --------------------------------------------------------------

    def _on_parse(self, uc, address, size, data):
        sp = uc.reg_read(UC_M68K_REG_A7)
        buf = struct.unpack('>I', uc.mem_read(sp + 8, 4))[0]
        self._parsing = buf if buf in self._busy else None

    def _on_release(self, uc, address, size, data):
        """The parser ends by handing its buffer back to the endpoint, as
        the USB interrupt's completion would re-arm it: ours is free again,
        and the next transfer can go at once, from the same task."""
        if self._returned(uc, address):
            return
        if self._parsing is None:
            return
        self._busy.discard(self._parsing)
        self._parsing = None
        self._post(uc, address)

    def _on_idle(self, uc, address, size, data):
        """In an idle spin: start the next transfer, as the USB interrupt
        would have made it."""
        if uc.reg_read(UC_M68K_REG_PC) != address:
            return              # something else moved the PC (a reschedule)
        if self._returned(uc, address):
            return
        self._post(uc, address)

    def _returned(self, uc, address):
        """-> True when this is a post coming back to `address`: its three
        arguments are dropped and the code there runs on as before."""
        sp = uc.reg_read(UC_M68K_REG_A7)
        caller = self._calls.pop((address, sp), None)
        if caller is None:
            return False
        uc.reg_write(UC_M68K_REG_A7, caller)
        return True

    def _post(self, uc, address):
        """Hand the guest one transfer: a call to the deferred-call poster,
        (parser, length, buffer), made as if from `address`."""
        free = [b for b in self._bufs if b not in self._busy]
        if not free:
            return
        with self._lock:
            if not self._packets:
                return
            n = min(len(self._packets), PACKETS * 4)
            chunk = bytes(self._packets[:n])
            del self._packets[:n]
        buf = free[0]
        uc.mem_write(buf, chunk)
        sp = uc.reg_read(UC_M68K_REG_A7)
        uc.mem_write(sp - 16, struct.pack('>IIII', address,
                                          self.p.usb_midi_parse, n, buf))
        uc.reg_write(UC_M68K_REG_A7, sp - 16)
        uc.reg_write(UC_M68K_REG_PC, self.p.usb_defer)
        self._calls[(address, sp - 12)] = sp
        self._busy.add(buf)
        self.posted += 1

    # -- clock -------------------------------------------------------------

    # Under fast_idle a step that reaches the idle spin ends there and is
    # credited up to the next timer, which can be tens of milliseconds of
    # emulated time: one transfer per step held a 1.7 MB OS upgrade to
    # 2.8 KB/s. While there is input to hand over, steps are kept short.
    STEP = 20_000

    def step(self, done, remaining=None):
        """longrun.spin's event-source hook: short steps while input waits."""
        with self._lock:
            waiting = bool(self._packets)
        return self.STEP if waiting or self._busy else None

    def service(self, done):
        return False

    def report(self):
        return ('usb midi: in %d B (%d transfers, %d B waiting), out %d B'
                % (self.received, self.posted, self.pending, self.sent))


class LinkServer:
    """A TCP server for one client at a time: what it sends goes to `put`,
    and `send` goes to it. Plain MIDI bytes both ways."""

    def __init__(self, put, host='127.0.0.1', port=DEFAULT_PORT):
        self._put = put
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._client = None
        self._lock = threading.Lock()
        self._out = collections.deque()
        self._wake = threading.Event()
        self._closed = False
        self.connected = 0
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()

    def _accept(self):
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                old, self._client = self._client, conn
            if old is not None:
                old.close()
            self.connected += 1
            threading.Thread(target=self._reader, args=(conn,),
                             daemon=True).start()

    def _reader(self, conn):
        while True:
            try:
                data = conn.recv(65536)
            except OSError:
                data = b''
            if not data:
                break
            self._put(data)
        with self._lock:
            if self._client is conn:
                self._client = None

    def _writer(self):
        while not self._closed:
            self._wake.wait()
            self._wake.clear()
            while self._out:
                data = self._out.popleft()
                with self._lock:
                    conn = self._client
                if conn is None:
                    continue        # nobody listening: dropped, as on a port
                try:
                    conn.sendall(data)
                except OSError:
                    pass

    def send(self, data):
        self._out.append(bytes(data))
        self._wake.set()

    def close(self):
        self._closed = True
        self._wake.set()
        try:
            self._sock.close()
        except OSError:
            pass
        with self._lock:
            if self._client is not None:
                self._client.close()
