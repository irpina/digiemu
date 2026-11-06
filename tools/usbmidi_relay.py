#!/usr/bin/env python3
"""Join the emulated Digitone's USB MIDI link to two MIDI ports on this host.

tools/usbrun.py serves the firmware's USB MIDI port as a TCP link (a plain
MIDI byte stream each way, emu/usbmidi.py). This relay is the host's end:
what a program sends to the port "dn-emu usb in" goes to the emulated
Digitone, and what the Digitone sends comes out of "dn-emu usb out". A
program then talks to the emulator as to a unit with split ports, for
example

    python -m elkflash info --in-port "dn-emu usb out" --out-port "dn-emu usb in"

It exists because the emulator may run where this host's MIDI ports are out
of reach: under WSL, which cannot open Windows MIDI devices. WSL forwards
localhost, so the default address reaches an emulator started there.

The ports: if MIDI ports with those names already exist (loopMIDI loopback
ports, say), the relay opens them through python-rtmidi. On Windows it can
instead create them itself with --create, through the teVirtualMIDI driver
that loopMIDI installs (teVirtualMIDI64.dll); those exist while the relay
runs. Elsewhere, --create makes rtmidi virtual ports.

    python tools/usbmidi_relay.py [--host 127.0.0.1] [--port 7311]
                                  [--name dn-emu] [--create]

Stdlib plus python-rtmidi; nothing in the emulator is imported, so it runs
on a host without Unicorn.
"""
import argparse
import ctypes
import socket
import sys
import threading
import time

DEFAULT_PORT = 7311


class VirtualPorts:
    """Two teVirtualMIDI ports: `<name> usb in` (others send to it) and
    `<name> usb out` (others read from it)."""

    PARSE_RX, RX_ONLY, TX_ONLY = 1, 4, 8
    CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte),
                            ctypes.c_uint32, ctypes.c_void_p)

    def __init__(self, name, sink):
        dll = ctypes.WinDLL('teVirtualMIDI64.dll' if sys.maxsize > 2 ** 32
                            else 'teVirtualMIDI.dll')
        dll.virtualMIDICreatePortEx2.restype = ctypes.c_void_p
        dll.virtualMIDICreatePortEx2.argtypes = [
            ctypes.c_wchar_p, self.CB, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32]
        dll.virtualMIDISendData.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                            ctypes.c_uint32]
        dll.virtualMIDIClosePort.argtypes = [ctypes.c_void_p]
        self._dll = dll
        self._cb = self.CB(lambda _p, data, n, _i: sink(ctypes.string_at(data, n)))
        self._none = self.CB()
        self._parse = bytearray()
        self._in = dll.virtualMIDICreatePortEx2('%s usb in' % name, self._cb,
                                                None, 0x20000, self.RX_ONLY)
        self._out = dll.virtualMIDICreatePortEx2('%s usb out' % name, self._none,
                                                 None, 0x20000, self.TX_ONLY)
        if not self._in or not self._out:
            raise OSError('teVirtualMIDI could not create the ports '
                          '(Windows error %d)' % ctypes.get_last_error())

    def send(self, data):
        for msg in _messages(self._parse, data):
            raw = bytes(msg)
            self._dll.virtualMIDISendData(self._out, raw, len(raw))

    def close(self):
        for port in (self._in, self._out):
            if port:
                self._dll.virtualMIDIClosePort(port)


class RtPorts:
    """Existing ports opened by name, or rtmidi virtual ports with create."""

    def __init__(self, name, sink, create=False):
        import rtmidi
        self._lock = threading.Lock()
        self._in = rtmidi.MidiIn(name='%s relay' % name)
        self._in.ignore_types(sysex=False, timing=False, active_sense=False)
        self._out = rtmidi.MidiOut(name='%s relay' % name)
        want_in, want_out = '%s usb in' % name, '%s usb out' % name
        if create:
            self._in.open_virtual_port(want_in)
            self._out.open_virtual_port(want_out)
        else:
            self._in.open_port(self._find(self._in.get_ports(), want_in))
            self._out.open_port(self._find(self._out.get_ports(), want_out))
        self._in.set_callback(lambda event, _d: sink(bytes(event[0])))
        self._parse = bytearray()

    @staticmethod
    def _find(ports, want):
        for i, p in enumerate(ports):
            if p == want or p.startswith(want + ' '):
                return i
        raise OSError('no MIDI port named %r (have: %s)' % (want, ', '.join(ports)))

    def send(self, data):
        # rtmidi sends whole messages: split the stream.
        with self._lock:
            for msg in _messages(self._parse, data):
                self._out.send_message(msg)

    def close(self):
        self._in.close_port()
        self._out.close_port()


def _messages(pending, data):
    """Split a MIDI byte stream into whole messages; `pending` keeps an
    unfinished one between calls. Running status is not used by the
    firmware's USB sender, so it is not reconstructed."""
    lengths = {0x80: 3, 0x90: 3, 0xA0: 3, 0xB0: 3, 0xC0: 2, 0xD0: 2,
               0xE0: 3, 0xF1: 2, 0xF2: 3, 0xF3: 2, 0xF6: 1}
    out = []
    for b in data:
        if b >= 0xF8:
            out.append([b])
            continue
        if b & 0x80 and b != 0xF7:
            pending.clear()
        if not pending and not b & 0x80:
            continue                            # stray data byte
        pending.append(b)
        head = pending[0]
        if head == 0xF0:
            if b == 0xF7:
                out.append(list(pending))
                pending.clear()
        else:
            need = lengths.get(head if head >= 0xF0 else head & 0xF0, 1)
            if len(pending) >= need:
                out.append(list(pending))
                pending.clear()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--name', default='dn-emu',
                    help='port name prefix: "<name> usb in" and "<name> usb out"')
    ap.add_argument('--create', action='store_true',
                    help='create the ports instead of opening existing ones')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args(argv)

    state = {'sock': None}
    lock = threading.Lock()
    counts = {'to': 0, 'from': 0}

    def to_emulator(data):
        counts['to'] += len(data)
        if args.verbose:
            print('-> %s' % data.hex(' ')[:120], flush=True)
        with lock:
            sock = state['sock']
        if sock is not None:
            try:
                sock.sendall(data)
            except OSError:
                pass

    if args.create and sys.platform == 'win32':
        ports = VirtualPorts(args.name, to_emulator)
    else:
        ports = RtPorts(args.name, to_emulator, create=args.create)
    print('[relay] ports "%s usb in" (to the emulator) and "%s usb out" '
          '(from it)' % (args.name, args.name), flush=True)
    try:
        while True:
            try:
                sock = socket.create_connection((args.host, args.port), timeout=5)
            except OSError:
                time.sleep(1)
                continue
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with lock:
                state['sock'] = sock
            print('[relay] connected to %s:%d' % (args.host, args.port), flush=True)
            while True:
                try:
                    data = sock.recv(65536)
                except OSError:
                    data = b''
                if not data:
                    break
                counts['from'] += len(data)
                if args.verbose:
                    print('<- %s' % data.hex(' ')[:120], flush=True)
                ports.send(data)
            with lock:
                state['sock'] = None
            sock.close()
            print('[relay] link closed (%d B to, %d B from); reconnecting'
                  % (counts['to'], counts['from']), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        ports.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
