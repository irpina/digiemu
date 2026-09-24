# pyright: reportMissingImports=false
"""Save and restore a whole emulated machine.

Chasing a boot blocker meant re-running from the entry point every time --
replaying ~32M instructions of identical setup (a memory-clear loop alone is
~8M) before reaching anything new. That made each experiment 10-20 minutes.

A snapshot removes that: run once to a checkpoint, dump registers and memory,
then restore and iterate forward from there in seconds.

Snapshots are firmware-derived state, so they are gitignored like everything
else derived from the .syx.
"""

import os
import pickle
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unicorn.m68k_const import (
    UC_M68K_REG_A0,
    UC_M68K_REG_D0,
    UC_M68K_REG_PC,
    UC_M68K_REG_SR,
)

from emu.checkpointver import CHECKPOINT_VERSION
from emu.harness import PAGE, Machine

REGS = (
    [("d%d" % i, UC_M68K_REG_D0 + i) for i in range(8)]
    + [("a%d" % i, UC_M68K_REG_A0 + i) for i in range(8)]
    + [("pc", UC_M68K_REG_PC), ("sr", UC_M68K_REG_SR)]
)


class DeferredComponentRestore:
    """Saved host state awaiting construction of its component.

    Pass this to ``restore_into`` and claim every saved name before execution.
    """

    def __init__(self, names=()):
        self.names = set(names)
        self._pending = {}

    def accepts(self, name):
        return name in self.names

    def defer(self, name, state):
        self._pending[name] = state

    def claim(self, name, component):
        """Restore and consume ``name``; return False for an old snapshot."""
        if name not in self.names:
            raise RuntimeError(
                "checkpoint component %r was not configured for deferral" % name
            )
        if name not in self._pending:
            return False
        _restore_component(component, self._pending.pop(name), name)
        return True

    def claim_constructed(self, name, factory):
        """Construct ``name`` from saved state, restore it, and return it.

        The factory receives validated saved state.  This avoids constructing
        components whose normal constructor mutates restored guest memory.
        """
        if name not in self.names:
            raise RuntimeError(
                "checkpoint component %r was not configured for deferral" % name
            )
        if name not in self._pending:
            return None
        state = self._pending[name]
        component = factory(state)
        self.claim(name, component)
        return component

    def require_claimed(self):
        if self._pending:
            raise RuntimeError(
                "checkpoint has unclaimed deferred components: %s"
                % ", ".join(sorted(self._pending))
            )


def _component_state(component):
    """Return portable state for a supported host-side checkpoint component."""
    if hasattr(component, "checkpoint_state"):
        return component.checkpoint_state()
    from collections import deque

    if isinstance(component, deque):
        return {"type": "deque", "version": 1, "values": list(component)}
    raise TypeError("checkpoint component has no checkpoint_state(): %r" % component)


def _restore_component(component, state, name):
    if state.get("type") == "deque":
        from collections import deque

        if not isinstance(component, deque):
            raise RuntimeError(
                "checkpoint component %r is a deque, not %r"
                % (name, type(component).__name__)
            )
        if state.get("version") != 1:
            raise RuntimeError(
                "unsupported deque checkpoint version %r" % state.get("version")
            )
        component.clear()
        component.extend(state["values"])
        return
    if not hasattr(component, "restore_checkpoint_state"):
        raise RuntimeError(
            "checkpoint component %r cannot be restored into %r"
            % (name, type(component).__name__)
        )
    component.restore_checkpoint_state(state)


def _is_int(value, low=0, high=0xFFFFFFFF):
    return type(value) is int and low <= value <= high


def _validate_timer_source(state):
    if not isinstance(state, dict) or state.get("type") not in ("Pits", "Dtims"):
        raise RuntimeError("invalid timer checkpoint source")
    if state.get("version") != 1:
        raise RuntimeError("unsupported timer checkpoint version")
    channels = state.get("channels")
    if not isinstance(channels, (tuple, list)) or any(
        not _is_int(ch, 0, 3) for ch in channels
    ):
        raise RuntimeError("invalid timer checkpoint channels")
    if (
        not _is_int(state.get("ips"), 1)
        or not isinstance(state.get("next"), list)
        or len(state["next"]) != 4
    ):
        raise RuntimeError("invalid timer checkpoint cadence")
    if any(
        value is not None and not isinstance(value, (int, float))
        for value in state["next"]
    ):
        raise RuntimeError("invalid timer checkpoint deadlines")
    if not _is_int(state.get("now")) or type(state.get("held")) is not bool:
        raise RuntimeError("invalid timer checkpoint clock")
    for key in ("fired", "missed"):
        if not isinstance(state.get(key), dict) or any(
            not _is_int(k, 0, 3) or not _is_int(v) for k, v in state[key].items()
        ):
            raise RuntimeError("invalid timer checkpoint counters")
    if state["type"] == "Dtims":
        if not isinstance(state.get("arm"), list) or not isinstance(
            state.get("stale"), list
        ):
            raise RuntimeError("invalid DTIM checkpoint state")
        if any(not _is_int(ch, 0, 3) for ch in state["arm"] + state["stale"]):
            raise RuntimeError("invalid DTIM checkpoint channels")


def _validate_component_state(state, name, component=None):
    if not isinstance(state, dict) or not isinstance(state.get("type"), str):
        raise RuntimeError("invalid checkpoint component %r" % name)
    typ = state["type"]
    if typ == "deque":
        if state.get("version") != 1 or not isinstance(state.get("values"), list):
            raise RuntimeError("invalid deque checkpoint state")
        if any(not _is_int(value, 0, 255) for value in state["values"]):
            raise RuntimeError("invalid deque checkpoint values")
    elif typ in ("Pits", "Dtims"):
        _validate_timer_source(state)
    elif typ == "Timers":
        if (
            state.get("version") != 1
            or not isinstance(state.get("sources"), list)
            or not state["sources"]
        ):
            raise RuntimeError("invalid Timers checkpoint state")
        for source in state["sources"]:
            _validate_timer_source(source)
    elif typ == "TxChannel":
        if state.get("version") != 1 or not all(
            _is_int(state.get(key))
            for key in ("chan", "vector", "pending", "bytes", "transfers")
        ):
            raise RuntimeError("invalid TxChannel checkpoint state")
    # Configuration checks are also pre-mutation.  These are deliberately
    # duck-typed so snapshot.py does not import timer/eDMA implementation.
    if component is not None:
        if typ == "deque":
            from collections import deque

            if not isinstance(component, deque):
                raise RuntimeError("checkpoint component %r is not a deque" % name)
        elif typ in ("Pits", "Dtims"):
            if (
                type(component).__name__ != typ
                or tuple(state["channels"]) != component.channels
                or state["ips"] != component.ips
            ):
                raise RuntimeError("%s checkpoint configuration mismatch" % typ)
        elif typ == "Timers":
            if type(component).__name__ != typ or len(state["sources"]) != len(
                component.sources
            ):
                raise RuntimeError("Timers checkpoint source count mismatch")
            for saved, source in zip(state["sources"], component.sources):
                if (
                    saved["type"] != type(source).__name__
                    or tuple(saved["channels"]) != source.channels
                    or saved["ips"] != source.ips
                ):
                    raise RuntimeError("Timers checkpoint source order mismatch")
        elif typ == "TxChannel" and (state["chan"], state["vector"]) != (
            component.chan,
            component.vector,
        ):
            raise RuntimeError("TxChannel checkpoint configuration mismatch")
    # Custom components own their state schema; their restore is only called
    # after the complete guest blob and all built-in component states validate.


def _validate_blob(blob):
    if not isinstance(blob, dict):
        raise RuntimeError("invalid checkpoint blob")
    version = blob.get("checkpoint_version")
    if version is not None and version != CHECKPOINT_VERSION:
        raise RuntimeError("unsupported checkpoint version %r" % version)
    required = (
        "regs",
        "pages",
        "all_mapped",
        "mmio",
        "ctlregs",
        "ff1_count",
        "movec_count",
        "extra",
    )
    if any(key not in blob for key in required):
        raise RuntimeError("invalid checkpoint blob: missing required state")
    if not isinstance(blob["regs"], dict) or set(blob["regs"]) != {
        name for name, _ in REGS
    }:
        raise RuntimeError("invalid checkpoint registers")
    if any(not _is_int(value) for value in blob["regs"].values()):
        raise RuntimeError("invalid checkpoint registers")
    if not isinstance(blob["all_mapped"], list) or any(
        not _is_int(base) or base % PAGE for base in blob["all_mapped"]
    ):
        raise RuntimeError("invalid checkpoint mapped pages")
    if len(set(blob["all_mapped"])) != len(blob["all_mapped"]) or not isinstance(
        blob["pages"], dict
    ):
        raise RuntimeError("invalid checkpoint pages")
    if any(
        not _is_int(base)
        or base not in blob["all_mapped"]
        or not isinstance(data, bytes)
        for base, data in blob["pages"].items()
    ):
        raise RuntimeError("invalid checkpoint pages")
    try:
        if any(len(zlib.decompress(data)) != PAGE for data in blob["pages"].values()):
            raise RuntimeError("invalid checkpoint page data")
    except (zlib.error, ValueError) as exc:
        raise RuntimeError("invalid checkpoint page data") from exc
    for key in ("mmio", "ctlregs"):
        if not isinstance(blob[key], dict) or any(
            not _is_int(k) or not _is_int(v) for k, v in blob[key].items()
        ):
            raise RuntimeError("invalid checkpoint " + key)
    if (
        not _is_int(blob["ff1_count"])
        or not _is_int(blob["movec_count"])
        or not isinstance(blob["extra"], dict)
    ):
        raise RuntimeError("invalid checkpoint counters")
    if "components" in blob:
        if not isinstance(blob["components"], dict) or any(
            not isinstance(name, str) for name in blob["components"]
        ):
            raise RuntimeError("invalid checkpoint components")
        for name, state in blob["components"].items():
            _validate_component_state(state, name)
    if (
        "manifest" in blob
        and blob["manifest"] is not None
        and not isinstance(blob["manifest"], dict)
    ):
        raise RuntimeError("invalid checkpoint manifest")


# Manifest keys a resume may change: the host shortcuts that are bit-exact
# (emu/softfloat.py, emu/hle.py) leave guest state exactly as the firmware's
# own routines would, so a snapshot saved with them on resumes correctly
# with them off. Timing and strict runs need them off. The DDR model is not
# one of these (see _restore_aliased).
RELAXABLE = frozenset(('softfloat', 'bitmap'))


def _validate_manifest(saved, current, relax=()):
    relax = frozenset(relax)
    if not relax <= RELAXABLE:
        raise ValueError('only %s may be relaxed, not %s'
                         % (sorted(RELAXABLE), sorted(relax - RELAXABLE)))
    if relax and isinstance(saved, dict) and isinstance(current, dict):
        saved = {k: v for k, v in saved.items() if k not in relax}
        current = {k: v for k, v in current.items() if k not in relax}
    if saved != current:
        raise RuntimeError(
            "checkpoint build manifest mismatch: saved=%r current=%r" % (saved, current)
        )


def save(machine, path, extra=None, components=None, manifest=None):
    """Dump guest state plus optional named host components and build manifest."""
    # Writes a hook made to a page the guest had not touched yet
    # (Machine.poke) belong in the snapshot too.
    flush = getattr(machine, 'flush_pending', None)
    if flush is not None:
        flush()
    pages = {}
    for base in sorted(machine.mapped):
        data = bytes(machine.uc.mem_read(base, PAGE))
        if data.strip(b"\x00"):
            pages[base] = zlib.compress(data, 6)
    blob = {
        "regs": {name: machine.uc.reg_read(rid) for name, rid in REGS},
        "pages": pages,
        "all_mapped": sorted(machine.mapped),
        "mmio": dict(machine.mmio),
        "ctlregs": dict(machine.ctlregs),
        "ff1_count": machine.ff1_count,
        "movec_count": machine.movec_count,
        "extra": extra or {},
        "checkpoint_version": CHECKPOINT_VERSION,
        "components": {
            name: _component_state(component)
            for name, component in (components or {}).items()
        },
        "manifest": manifest,
    }
    with open(path, "wb") as f:
        pickle.dump(blob, f, protocol=4)
    raw = sum(len(zlib.decompress(v)) for v in pages.values())
    return {
        "pages": len(pages),
        "mapped": len(blob["all_mapped"]),
        "bytes_on_disk": os.path.getsize(path),
        "bytes_live": raw,
    }


class _SnapshotUnpickler(pickle.Unpickler):
    """Snapshots contain only primitive state, never executable classes."""

    def find_class(self, module, name):
        raise pickle.UnpicklingError(
            "checkpoint may not contain %s.%s" % (module, name)
        )


def _load_blob(path):
    with open(path, "rb") as f:
        blob = _SnapshotUnpickler(f).load()
    _validate_blob(blob)
    return blob


def restore(path):
    """-> (Machine, extra). Hooks are NOT installed; the caller installs the
    same ones it would use for a fresh run, then calls uc.emu_start(regs['pc'])."""
    blob = _load_blob(path)
    m = Machine()
    for base in blob["all_mapped"]:
        m.ensure(base)
    for base, comp in blob["pages"].items():
        m.uc.mem_write(base, zlib.decompress(comp))
    m.mmio.update(blob["mmio"])
    m.ctlregs.update(blob["ctlregs"])
    m.ff1_count = blob["ff1_count"]
    m.movec_count = blob["movec_count"]
    m.uc.reg_write(UC_M68K_REG_SR, blob["regs"]["sr"])
    for name, rid in REGS:
        if name != "sr":
            m.uc.reg_write(rid, blob["regs"][name])
    return m, blob["extra"], blob["regs"]


def _restore_aliased(machine, pages):
    """Write saved pages into a Machine whose DDR aliases (Machine.set_ddr).

    Pages that decode to the same DDR are one memory, so a snapshot saved
    under the DDR model holds identical copies of them; each group is
    written once. A snapshot saved WITHOUT the model can hold different
    data in two aliases of one location, which no merge can make right
    (measured: merging them left the audio engine reading a buffer another
    alias had overwritten), so it is refused. Such a snapshot is a
    different machine: boot the firmware from reset with the model on
    (emu/fwcheck.py does)."""
    groups = {}
    for base, comp in pages.items():
        off = machine.ddr_physical(base)
        groups.setdefault(base if off is None else ("ddr", off), []).append(base)
    for key, bases in groups.items():
        bases.sort()
        data = zlib.decompress(pages[bases[0]])
        for other in bases[1:]:
            if zlib.decompress(pages[other]) != data:
                raise RuntimeError(
                    "checkpoint pages 0x%08x and 0x%08x are one DDR location "
                    "but hold different data: the snapshot was not saved "
                    "under the DDR model" % (bases[0], other))
        machine.uc.mem_write(bases[0], data)


def restore_into(machine, path, st=None, components=None, manifest=None, deferred=None,
                 relax=()):
    """Load a snapshot onto an already configured Machine.

    ``deferred`` permits construction-dependent host components (such as
    Timers) to claim their saved state after guest state is installed.
    ``relax`` names manifest keys (only those in RELAXABLE) that may differ
    from the ones the snapshot was saved with.
    """
    blob = _load_blob(path)
    if deferred is not None and not isinstance(deferred, DeferredComponentRestore):
        raise TypeError("deferred must be a DeferredComponentRestore")
    if blob.get("manifest") is not None:
        if manifest is None:
            raise RuntimeError("checkpoint requires a build manifest")
        _validate_manifest(blob["manifest"], manifest, relax)
    saved_components = blob.get("components", {})
    supplied = components or {}
    deferred_names = deferred.names if deferred is not None else set()
    missing = sorted(set(saved_components) - set(supplied) - deferred_names)
    if missing:
        raise RuntimeError(
            "checkpoint missing configured components: %s" % ", ".join(missing)
        )
    # Validate every component before changing the guest or a supplied host
    # object.  This makes malformed files fail atomically at the restore seam.
    for name, state in saved_components.items():
        _validate_component_state(state, name, supplied.get(name))
    for base in blob["all_mapped"]:
        machine.ensure(base)
    if getattr(machine, "ddr", None) is not None:
        _restore_aliased(machine, blob["pages"])
    else:
        for base, comp in blob["pages"].items():
            machine.uc.mem_write(base, zlib.decompress(comp))
    machine.mmio.update(blob["mmio"])
    machine.ctlregs.update(blob["ctlregs"])
    machine.ff1_count = blob["ff1_count"]
    machine.movec_count = blob["movec_count"]
    machine.uc.reg_write(UC_M68K_REG_SR, blob["regs"]["sr"])
    for name, rid in REGS:
        if name != "sr":
            machine.uc.reg_write(rid, blob["regs"][name])
    for name, state in saved_components.items():
        if deferred is not None and deferred.accepts(name):
            deferred.defer(name, state)
        else:
            _restore_component(supplied[name], state, name)
    if st is not None:
        st["seen"].update(blob["extra"].get("seen", []))
        st["n"] = blob["extra"].get("n", 0)
        for k, v in blob["extra"].get("tasks", {}).items():
            st["task_create_hits"].setdefault(int(k, 16), v)
    return blob["regs"]["pc"]
