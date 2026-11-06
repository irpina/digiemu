"""The OS's own flash writes, and the reset that ends an OS upgrade.

The running OS normally reads the SPI flash through a shortcut
(longrun.build's flash_read stand-in, served from the flash image). An OS
upgrade over USB also erases and programs it, through the OS's own driver
for DSPI 0, and then resets the processor. `FlashWrites` gives the running
OS emu/bootrom.py's flash and DSPI models for that: the flash's memory is
the same image the read shortcut serves, so what the OS writes, it (and
everything after it) reads back. A write to the reset controller's soft
reset bit stops the run, the way the device stops executing the OS, and
the flash as written is what the next boot starts from (`reboot`).

The reboot is the device's own: emu/bootrom.py runs the bootstrap from the
flash, which loads the updater from the flash, which unpacks MAIN OS and
starts it. That needs nothing from the old firmware.
"""
import struct

from unicorn import UC_HOOK_MEM_WRITE

from emu import bootrom

RCR = 0xEC090000            # reset controller, RCR (MCF54418RM 9.3.1)
SOFTRST = 0x80


class FlashWrites:
    """The flash on DSPI 0 for a running OS; `reset` is set when it resets."""

    def __init__(self, m, ev):
        flash = ev.get('flash')
        if flash is None:
            raise RuntimeError('this build() has no flash image to write')
        self.m = m
        self.flash = bootrom.SpiFlash(size=len(flash))
        self.flash.mem = flash          # one image: the read shortcut's too
        self.dspi = bootrom.Dspi(m, base=bootrom.DSPI0,
                                 devices={bootrom.FLASH_PCS: self.flash})
        # The OS set the controller up long before: its frame sizes (8 bits
        # for the flash) are in the registers, not the model's reset values.
        regs = bytes(m.uc.mem_read(bootrom.DSPI0, 0x2C))
        self.dspi.mcr = struct.unpack_from('>I', regs, bootrom.Dspi.MCR)[0]
        self.dspi.ctar = list(struct.unpack_from('>8I', regs, bootrom.Dspi.CTAR))
        # longrun.build forces DSPI 0's status register for the read
        # shortcut's sake; the model's own status is installed after it and
        # so is the one a read sees.
        self.reset = False
        m.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_rcr, begin=RCR, end=RCR)

    def _on_rcr(self, uc, typ, addr, size, value, data):
        if value & SOFTRST:
            self.reset = True
            self.m.halt_vec = -1            # ends longrun.spin's loop
            uc.emu_stop()

    @property
    def writes(self):
        return self.flash.writes

    def image(self):
        return bytes(self.flash.mem)


def container_from_flash(flash, at=bootrom.CONTAINER_AT):
    """-> the ELE3 container the flash holds at `at`, cut to its sections."""
    c = bytes(flash[at:])
    if c[:4] != b'ELE3':
        raise ValueError('no ELE3 container at flash 0x%x' % at)
    n = struct.unpack_from('>I', c, 0x1C)[0]
    end = 0x20 + 16 * n
    for k in range(n):
        _sid, off, clen, _dest = struct.unpack_from('>IIII', c, 0x20 + 16 * k)
        end = max(end, off + clen)
    return c[:end]


def section(container, section_id):
    """-> one section's stored bytes from a container, or None."""
    n = struct.unpack_from('>I', container, 0x1C)[0]
    for k in range(n):
        sid, off, clen, _dest = struct.unpack_from('>IIII', container,
                                                   0x20 + 16 * k)
        if sid == section_id:
            return bytes(container[off:off + clen])
    return None


def find_firmware(container, candidates):
    """-> the first .syx among `candidates` (files or folders of them) that
    holds `container`, or None. The emulator runs an OS from its own .syx
    (sections, symbols, the device file), so a reboot needs the file the
    flash was written from."""
    import glob
    import os
    from dt2.container import container as syx_container
    files = []
    for c in candidates:
        if c and os.path.isdir(c):
            files += sorted(glob.glob(os.path.join(c, '*.syx')))
        elif c and os.path.isfile(c):
            files.append(c)
    for path in dict.fromkeys(files):
        try:
            got = syx_container(path)
        except (ValueError, OSError):
            continue
        if got[:len(container)] == container:
            return path
    return None


def reboot(flash, syx, workdir, *, card=None, boot_section=None,
           progress=None, log=print):
    """Boot the flash as the device does after its reset. -> the gui.snap
    the boot settled to.

    1. The bootstrap (`boot_section`: the one already on the device, which
       an OS upgrade leaves alone) runs from the flash (emu/bootrom.py): it
       loads the updater from the new container, which unpacks MAIN OS and
       starts it.
    2. From that handoff the usual stages run (emu/bootstrap.py first_run
       with start_from): the OS's cold boot, its intro and the settle, in a
       folder of its own under `workdir` for `syx`, the file the flash was
       written from. `card`, the +Drive image the device had, goes with it,
       and is marked as having finished its first boot already.
    """
    import os
    import shutil
    from emu import bootstrap as bs
    from emu import fwcheck
    paths, dev = fwcheck.prepare(syx, workdir, role='reboot')
    bs.extract_sections(paths)
    if boot_section is None:
        with open(os.path.join(paths.sections, 'section_2_DSP.bin'), 'rb') as fh:
            boot_section = fh.read()
    with open(paths.main_img, 'rb') as fh:
        main = fh.read()
    ddr = dev.ddr_bytes or fwcheck.DEFAULT_DDR
    r = bootrom.boot(syx, boot_section, main, ddr=ddr,
                     ui_card=dev.ui_card if dev.ui_card is not None else 4,
                     straps=dev.straps, groups=dev.button_groups(),
                     container_at=dev.container_at
                     if dev.container_at is not None
                     else bootrom.CONTAINER_AT, flash=flash)
    if r.reached is None:
        raise RuntimeError('the bootstrap did not start the OS from the '
                           'flash: %s' % r.stop)
    log('[reboot] the bootstrap started the OS at 0x%08x (boot flags 0x%08x, '
        'MAIN OS %s the file\'s)' % (r.reached, r.boot_flags or 0,
                                    'matches' if r.main_os_matches
                                    else 'DIFFERS from'))
    handoff = os.path.join(paths.root, 'bootrom-handoff.bin')
    bootrom.save_handoff(r, handoff)
    if card is not None and os.path.exists(card):
        shutil.copyfile(card, paths.card)
        bs._update_state(paths, lambda s: s.setdefault(
            'first_boot', {'at': bs._now(), 'carried_over': True}))
    return bs.first_run(paths, progress=progress,
                        options={'start_from': handoff, 'ddr': ddr})
