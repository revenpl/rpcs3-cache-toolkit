#!/usr/bin/env python3
"""Build a synthetic RPCS3 FORK shaders_cache in the REAL SPLIT layout:

    <ROOT>/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
        raw/<HASH>.vp | <HASH>.fp
        pipelines/vulkan/v1.96/pipelines.bin          RSPL (16 B header + N*320)
        pipelines/vulkan/v1.96/device/<KEY>/props.bin RSPT (16 B header + N*480)

RSPL/RSPT header = magic(u32) + ver(u32) + count(u32) + rec(u32). The exporter
only needs a valid header + count*rec payload + consistent raw files for a
byte-exact round-trip.

Games:
  BCES01741 (God of War):
    GOWA.SELF   50 pipelines (split, 1 AMD device)
    EBOOT.BIN    3 pipelines (split, 1 AMD device)
    ppu-000...-  EMPTY (raw/ + pipelines/ dirs, no data)  -> must be skipped
    legacy.SELF  LEGACY (per-entry .bin)                  -> must be ignored
  BCES01277 (God of War Collection):
    coo.ppu.self  8 pipelines (split, 2 devices)
    EBOOT.BIN      4 pipelines (split, 1 device)
    ghs.ppu.self   EMPTY -> must be skipped
"""
import os
import struct
import random
import sys

random.seed(42)
ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/test_rpc")

RSPL_MAGIC = 0x4C505352  # "RSPL"
RSPT_MAGIC = 0x54505352  # "RSPT"
RSPL_REC = 320
RSPT_REC = 480
VERSION = "v1.96"
AMD = "1002_67B0"
NVIDIA = "10DE_2184"


def write_split(ppu_dir, n, devices):
    """Create a valid split cache under <ppu_dir>/shaders_cache with n pipelines
    and the given device list (each a (key, count) tuple)."""
    sc = os.path.join(ppu_dir, "shaders_cache")
    raw = os.path.join(sc, "raw")
    vdir = os.path.join(sc, "pipelines", "vulkan", VERSION)
    os.makedirs(raw, exist_ok=True)
    for key, _cnt in devices:
        os.makedirs(os.path.join(vdir, "device", key), exist_ok=True)

    pl = bytearray(struct.pack("<IIII", RSPL_MAGIC, 1, n, RSPL_REC))
    props = {key: bytearray(struct.pack("<IIII", RSPT_MAGIC, 1, cnt, RSPT_REC))
             for key, cnt in devices}

    for i in range(n):
        vph = random.getrandbits(64)
        fph = random.getrandbits(64)
        psh = random.getrandbits(64)
        pl += struct.pack("<QQQ", vph, fph, psh) + os.urandom(RSPL_REC - 24)
        with open(os.path.join(raw, f"{vph:X}.vp"), "wb") as f:
            f.write(os.urandom(random.randint(64, 512)))
        with open(os.path.join(raw, f"{fph:X}.fp"), "wb") as f:
            f.write(os.urandom(random.randint(64, 1024)))
        for key, cnt in devices:
            if i < cnt:
                props[key] += struct.pack("<QQQQ", vph, fph, random.getrandbits(64), psh)
                props[key] += os.urandom(RSPT_REC - 32)

    with open(os.path.join(vdir, "pipelines.bin"), "wb") as f:
        f.write(bytes(pl))
    for key, data in props.items():
        with open(os.path.join(vdir, "device", key, "props.bin"), "wb") as f:
            f.write(bytes(data))


def make_legacy(ppu_dir, n):
    """LEGACY layout (per-entry .bin) — present only to verify the exporter ignores it."""
    sc = os.path.join(ppu_dir, "shaders_cache")
    pdir = os.path.join(sc, "pipelines", "vulkan", "v1.95")
    raw = os.path.join(sc, "raw")
    os.makedirs(pdir, exist_ok=True)
    os.makedirs(raw, exist_ok=True)
    for i in range(n):
        vp, fp = random.getrandbits(64), random.getrandbits(64)
        name = f"{vp:X}+{fp:X}+{random.getrandbits(64):X}+{random.getrandbits(64):X}.bin"
        with open(os.path.join(pdir, name), "wb") as f:
            f.write(struct.pack("<QQ", vp, fp) + os.urandom(672 - 16))
        with open(os.path.join(raw, f"{vp:X}.vp"), "wb") as f:
            f.write(os.urandom(64))


def make_empty(ppu_dir):
    """Empty module: raw/ + pipelines/ dirs exist but no data -> must be skipped."""
    sc = os.path.join(ppu_dir, "shaders_cache")
    os.makedirs(os.path.join(sc, "raw"), exist_ok=True)
    os.makedirs(os.path.join(sc, "pipelines", "vulkan", VERSION), exist_ok=True)


# --- Game 1: BCES01741 (God of War) ---
g1 = os.path.join(ROOT, "cache", "BCES01741")
write_split(os.path.join(g1, "ppu-AAA111-GOWA.SELF"), 50, [(AMD, 50)])
write_split(os.path.join(g1, "ppu-BBB222-EBOOT.BIN"), 3, [(AMD, 3)])
make_empty(os.path.join(g1, "ppu-0000000000000000000000000000-"))
make_legacy(os.path.join(g1, "ppu-LEG111-legacy.SELF"), 6)

# --- Game 2: BCES01277 (God of War Collection) ---
g2 = os.path.join(ROOT, "cache", "BCES01277")
write_split(os.path.join(g2, "ppu-CCC333-coo.ppu.self"), 8, [(AMD, 8), (NVIDIA, 8)])
write_split(os.path.join(g2, "ppu-DDD444-EBOOT.BIN"), 4, [(NVIDIA, 4)])
make_empty(os.path.join(g2, "ppu-EEEE555-ghs.ppu.self"))

print(f"synthetic SPLIT cache at {ROOT}:")
print(f"  BCES01741: GOWA 50 (split), EBOOT 3 (split), 1 empty, 1 legacy (ignored)")
print(f"  BCES01277: coo 8 (split, 2 devices), EBOOT 4 (split), ghs empty (ignored)")
