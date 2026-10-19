#!/usr/bin/env python3
"""Build a synthetic RPCS3 shaders_cache with the REAL layout (v0.0.43):
    <ROOT>/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
        raw/<HASH>.vp|.fp
        pipelines/vulkan/v1.95/<VP>+<FP>+<S>+<H>.bin
    Entry = 672 B (uniform), first 16 bytes = u64 vp_hash + u64 fp_hash (LE).
Includes: 50 entries (1 with missing raw) in GOWA, 3 entries in EBOOT (old
vulkan/v1 dir), and one EMPTY game dir that a correct export must skip.
"""
import os
import struct
import random
import sys

random.seed(42)
ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/test_rpc")
ENTRY = 672


def mk_entry(sc, pdir, vp, fp, size=ENTRY):
    d = os.path.join(sc, *pdir.split("/"))
    os.makedirs(d, exist_ok=True)
    name = f"{vp:X}+{fp:X}+{random.getrandbits(64):X}+{random.getrandbits(64):X}.bin"
    with open(os.path.join(d, name), "wb") as f:
        f.write(struct.pack("<QQ", vp, fp) + os.urandom(size - 16))


def mk_raw(sc, vp, fp):
    raw = os.path.join(sc, "raw")
    os.makedirs(raw, exist_ok=True)
    with open(os.path.join(raw, f"{vp:X}.vp"), "wb") as f:
        f.write(os.urandom(random.randint(32, 512)))
    with open(os.path.join(raw, f"{fp:X}.fp"), "wb") as f:
        f.write(os.urandom(random.randint(32, 1024)))


# Game 1: GOWA.SELF - real version dir v1.95, 50 entries, 1 missing raw
g1 = os.path.join(ROOT, "cache", "BCES01741", "ppu-AAA111-GOWA.SELF", "shaders_cache")
for i in range(50):
    vp, fp = random.getrandbits(64), random.getrandbits(64)
    mk_entry(g1, "pipelines/vulkan/v1.95", vp, fp)
    if i != 49:  # last one: missing raw files
        mk_raw(g1, vp, fp)

# Game 2: EBOOT.BIN - old version dir vulkan/v1, 3 entries
g2 = os.path.join(ROOT, "cache", "BCES01741", "ppu-BBB222-EBOOT.BIN", "shaders_cache")
for i in range(3):
    vp, fp = random.getrandbits(64), random.getrandbits(64)
    mk_entry(g2, "pipelines/vulkan/v1", vp, fp)
    mk_raw(g2, vp, fp)

# Game 3: empty dir - must be skipped by export (raw+pipelines dirs exist, no .bin)
g3 = os.path.join(ROOT, "cache", "BCES01741", "ppu-0000000000000000000000000000-", "shaders_cache")
os.makedirs(os.path.join(g3, "raw"), exist_ok=True)
os.makedirs(os.path.join(g3, "pipelines", "vulkan", "v1.95"), exist_ok=True)

print(f"synthetic cache at {ROOT}: GOWA 50 entries (1 missing raw, v1.95), "
      f"EBOOT 3 entries (v1), empty dir (must be skipped)")
