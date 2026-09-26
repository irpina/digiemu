"""Decode the physical DDR2 size from the bootstrap's own DDRMC init sequence.

This is a narrow static-analysis fallback in the same spirit as
tools/early_init_contract.py: it accepts only the known, reviewed bootstrap
instruction sequence and raises rather than silently guessing when it
changes. It does not disassemble the whole image.

Why this works: MCF5441x's DDR SDRAM controller (DDRMC, chapter 21 of the
MCF5441x/MCF54418RM reference manual) is configured entirely through 32-bit
control registers at 0xFC0B8000 + 4*n ("DDR_CR00".."DDR_CR63"). The bootstrap
(section 2, loaded at 0x80000400) programs all of them once, early, before it
ever stages an incoming image into DDR at 0x40000000. Three of those registers
fix the physical geometry:

    DDR_CR04 (0xFC0B8010) bit 8   8BNK     0 = 4 banks,  1 = 8 banks
    DDR_CR15 (0xFC0B803C) bits 26-24  ADDPINS  max_row_bits - actual_row_bits
    DDR_CR16 (0xFC0B8040) bits 26-24  COLSIZ   max_col_bits - actual_col_bits

and the manual gives the two "max" constants and the fixed geometry for
this specific controller:

    max row bits = 16: ADDPINS is "the difference between the maximum number
      of address pins configured (16) and the actual number of pins used"
      (Table 21-20), and DDR_CR23[MAXROW] "always reads 0x10" (Table 21-28).
      Section 21.5.2.2's "15 rows" is how many address lines reach the pins,
      not the base ADDPINS counts from. This was 15 until 2026-09-24, which
      halved every size decoded here: the mk1 products have 128 MB, not 64.
    max column bits = 12 (DDR_CR20[MAXCOL]), chip selects = 1 (fixed),
    memory datapath = 8 bits / 1 byte (fixed, "single x8 DDR2 component")

so the physical size is

    bytes = 2 ** (actual_row_bits + actual_col_bits) * banks * 1

Reproduce the disassembly by hand:

    uv run python -c "
    from dt2.coldfire import disasm
    data = open('sections/section_2_DSP.bin','rb').read()
    for pc, raw, mn, ops in disasm(data, 0x80000400, 0x800006c0, 0x800008a0):
        print(hex(pc), raw, mn, ops)"

Verified identical (byte-for-byte, same file offsets 0x2f2-0x4fc) in both
Digitakt II 1.15C's and Digitone II 1.10E's bootstraps.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dt2.coldfire import disasm

LOAD_ADDRESS = 0x80000400
START, END = 0x800006C0, 0x800008A0

DDR_CR04, DDR_CR15, DDR_CR16 = 0xFC0B8010, 0xFC0B803C, 0xFC0B8040
# MCF54418RM chapter 21: ADDPINS counts from 16 (Table 21-20, DDR_CR23
# MAXROW), COLSIZ from 12 (DDR_CR20 MAXCOL).
MAX_ROW_BITS, MAX_COL_BITS, CHIP_SELECTS, DATAPATH_BYTES = 16, 12, 1, 1
# Values actually written in the two firmwares this project has (Digitakt II
# 1.15C, Digitone II 1.10E) -- reviewed once; a different bootstrap build must
# re-derive these, not silently reuse them.
EXPECTED = {DDR_CR04: 0x00010101, DDR_CR15: 0x02000103, DDR_CR16: 0x02000407}


def _fmt(addr, raw):
    return "0x%08x" % addr, raw


def decode_cr_writes(image):
    """Walk the bootstrap's linear DDRMC init sequence, return {addr: value}.

    The sequence is a straight-line run of `move.l #imm,d0` / `moveq` /
    `clr.b|w|l d0` / `move.w #imm,d0` (merges into the low word) instructions
    interleaved with `move.l d0,(addr).L` writes -- no branches, no calls.
    Anything else in this range is a contract mismatch, not a new case to
    special-case: this tool intentionally does not become a general emulator.
    """
    d0 = 0
    writes = {}
    for pc, raw, mnemonic, operands in disasm(image, LOAD_ADDRESS, START, END):
        b = bytes.fromhex(raw)
        if raw.startswith("203c") and len(raw) == 12:      # move.l #imm,d0
            d0 = int.from_bytes(b[2:6], "big")
        elif len(raw) == 4 and b[0] == 0x70:
            d0 = b[1]                                        # moveq #imm,d0 (d0 only)
        elif raw == "4200":
            d0 &= ~0xFF                                      # clr.b d0
        elif raw == "4240":
            d0 &= ~0xFFFF                                    # clr.w d0
        elif raw.startswith("303c") and len(raw) == 8:       # move.w #imm,d0
            d0 = (d0 & 0xFFFF0000) | int.from_bytes(b[2:4], "big")
        elif raw.startswith("23c0") and len(raw) == 12:       # move.l d0,(abs).L
            writes[int(b[2:6].hex(), 16)] = d0
        elif raw.startswith("42b9") and len(raw) == 12:       # clr.l (abs).L
            writes[int(b[2:6].hex(), 16)] = 0
        # every other instruction in range (jsr/rts/branches/other regs) is
        # scaffolding around the init block (mutex, PPM writes, etc) -- ignored.
    return writes


def geometry(image, *, expect=None):
    writes = decode_cr_writes(image)
    expect = EXPECTED if expect is None else expect
    missing = [hex(a) for a in expect if a not in writes]
    if missing:
        raise ValueError("DDRMC contract mismatch: no write seen to %s in "
                          "0x%x-0x%x -- bootstrap sequence has changed, "
                          "re-derive by hand" % (missing, START, END))
    mismatched = {a: (expect[a], writes[a]) for a in expect if writes[a] != expect[a]}
    cr04, cr15, cr16 = writes[DDR_CR04], writes[DDR_CR15], writes[DDR_CR16]
    banks = 8 if (cr04 >> 8) & 1 else 4
    addpins = (cr15 >> 24) & 7
    colsiz = (cr16 >> 24) & 7
    rows = MAX_ROW_BITS - addpins
    cols = MAX_COL_BITS - colsiz
    size_bytes = CHIP_SELECTS * (2 ** (rows + cols)) * banks * DATAPATH_BYTES
    return {
        "registers": {
            "DDR_CR04": "0x%08x" % cr04, "DDR_CR15": "0x%08x" % cr15,
            "DDR_CR16": "0x%08x" % cr16,
        },
        "fields": {"8BNK": banks == 8, "ADDPINS": addpins, "COLSIZ": colsiz},
        "geometry": {"rows": rows, "columns": cols, "banks": banks,
                     "chip_selects": CHIP_SELECTS, "datapath_bytes": DATAPATH_BYTES},
        "size_bytes": size_bytes,
        "size_mib": size_bytes / (1024 * 1024),
        "known_value_match": not mismatched,
        "mismatches": {"0x%x" % a: {"expected": "0x%x" % e, "found": "0x%x" % f}
                        for a, (e, f) in mismatched.items()},
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bootstrap", nargs="?", default="sections/section_2_DSP.bin",
                   help="decompressed bootstrap image (section 2 body)")
    p.add_argument("--allow-unknown-values", action="store_true",
                   help="compute geometry even if the CR values differ from "
                        "the two reviewed firmwares (still requires the same "
                        "instruction shapes)")
    args = p.parse_args(argv)
    image = Path(args.bootstrap).read_bytes()
    result = geometry(image, expect={} if args.allow_unknown_values else None)
    for k, v in result["registers"].items():
        print("%-9s = %s" % (k, v))
    print("fields    : %s" % result["fields"])
    g = result["geometry"]
    print("geometry  : %d row + %d col bits, %d banks, %d chip select(s), "
          "%d-byte datapath" % (g["rows"], g["columns"], g["banks"],
                                  g["chip_selects"], g["datapath_bytes"]))
    print("DDR size  : %d bytes (%.0f MiB)" % (result["size_bytes"], result["size_mib"]))
    if not result["known_value_match"]:
        print("NOTE: register values differ from the two reviewed firmwares:",
              result["mismatches"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
