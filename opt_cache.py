#!/usr/bin/env python3
"""
opt_cache.py — RPCS3 shaders_cache (Vulkan) <-> RSCA (RPCS3 Shader Cache Archive).

A game (one <GAMEID> dir) contains one shaders_cache per PPU module
(ppu-<hash>-EBOOT.BIN, ppu-<hash>-GOWA.SELF, ppu-<hash>-coo.ppu.self, ...).
A collection title (e.g. BCES01277 = God of War Collection 2) has one module
per playable game, so a useful export = ALL non-empty modules of the game,
in one file.

File format
-----------
RSCA v2 (current) — one file per game:
    0    4   magic "RSCA"
    4    4   format = 2
    8   16   game_id
    24   4   module_count M
    per module:
          4   name_len, then name bytes   (ppu dir name, e.g. "ppu-AAA-GOWA.SELF")
         16   backend            (e.g. "vulkan")
         16   cache_version      (e.g. "v1.95" — per module, modules may differ)
          ... BODY

RSCA v1 (legacy, import-only) — one file per module:
    0    4   magic "RSCA"
    4    4   format = 1
    8   16   game_id
    24  16   backend
    40  16   cache_version
    56   ... BODY

BODY (same in v1 and v2):
    4    raw_count, then 4 reserved (whole-blob zlib mode)
    per raw file: 4 name_len + name, 4 data_len
    4    blob_c_len, then zlib(9) of all raw files concatenated (sorted by name)
    1    mode (0 = byte-exact, 1 = normalized) + 3 pad
    4    N = entry count
    4    Uvp / Ufp / Upr (unique vp / fp / props rows)
    vp rows  = 8B vph + 168B scalar   (entry[24:192])
    fp rows  = 8B fph +  32B scalar   (entry[192:224])
    props    = 8B psh + 448B block    (entry[224:672])
    N x (4 vp_idx, 4 fp_idx, 4 props_idx)

Layout of one 672-byte pipeline_data entry (from rsx_cache.h, verified):
  0    8  vertex_program_hash
  8    8  fragment_program_hash
  16   8  pipeline_storage_hash
  24  168  vp scalar (ctrl, masks, jump table, ...)
  192  32  fp scalar (ctrl, texcoord, lighting, mrt, ...)
  224 448  pipeline_properties (Vulkan descriptors — parameters, not compiled code)
  672    (end)

state_hash (4th hash in the entry filename) = FNV-1a 64 over 14 u32 params,
NOT including base_address/entry  -> invariant under normalization.

Modes:
  byte-exact : keep original base_address/entry  -> round-trip byte-identical
  normalized : base_address->0, entry->(entry-base) -> smaller, same decompiler
               semantics (VertexProgramDecompiler only uses entry-base_address)
"""
import os
import struct
import sys
import argparse
import collections
import zlib

VP_SCAL  = 168               # entry[24:192]
FP_SCAL  = 32                # entry[192:224]
PROPS    = 448               # entry[224:672]
MASK = (1 << 64) - 1
SEED = 14695981039346656037
PRIME = 1099511628211
MAGIC = b"RSCA"
FORMAT = 2                   # RSCA v2 = one file per game (all non-empty modules)
FORMAT_V1 = 1                # legacy: one file per module (import-only)


def u8(b, o):  return b[o]
def u16(b, o): return struct.unpack_from("<H", b, o)[0]
def u32(b, o): return struct.unpack_from("<I", b, o)[0]
def u64(b, o): return struct.unpack_from("<Q", b, o)[0]


def state_hash(b):
    params = [u32(b, 24), u32(b, 28), u32(b, 192), u32(b, 32), u32(b, 196),
              u32(b, 200), u16(b, 204), u16(b, 206), u16(b, 208), u16(b, 210),
              u16(b, 212), u16(b, 184), u16(b, 214), u32(b, 216)]
    h = SEED
    for v in params:
        h ^= v
        h = (h * PRIME) & MASK
    return h


def find_pipelines_dir(cache_root):
    """Leaf dir under cache_root holding the most .bin pipeline files."""
    best = (None, 0)
    for root, dirs, files in os.walk(cache_root):
        if "pipelines" not in root:
            continue
        n = sum(1 for f in files if f.endswith(".bin"))
        if n > best[1]:
            best = (root, n)
    return best


def normalize_vp_scalar(s):
    # s = 168-byte vp scalar (entry[24:192])
    # base_address at entry[112] -> scalar[88]; entry at entry[116] -> scalar[92]
    s = bytearray(s)
    base = u32(bytes(s), 88)
    ent = u32(bytes(s), 92)
    s[88:92] = (0).to_bytes(4, "little")
    s[92:96] = ((ent - base) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(s)


# ---------------------------------------------------------------------------
# packing
# ---------------------------------------------------------------------------
def _cache_root_from_pipelines(pipelines_leaf):
    # pipelines_leaf = .../shaders_cache/pipelines/<cls>/<ver>
    parts = pipelines_leaf.split(os.sep)
    return os.sep.join(parts[:-3])


def _find_raw_dir(cache_root):
    raw = os.path.join(cache_root, "raw")
    if os.path.isdir(raw):
        return raw
    for root, dirs, files in os.walk(cache_root):
        if "raw" in dirs:
            return os.path.join(root, "raw")
    return None


def _read_raw(raw_dir):
    out = []
    for name in sorted(os.listdir(raw_dir)):
        if not (name.endswith(".vp") or name.endswith(".fp")):
            continue
        with open(os.path.join(raw_dir, name), "rb") as f:
            out.append((name, f.read()))
    return out


def _dedup(entries, mode):
    vptab = collections.OrderedDict()
    fptab = collections.OrderedDict()
    ptab = collections.OrderedDict()
    entry_idx = []
    for _f, b in entries:
        vph, fph, psh = b[0:8], b[8:16], b[16:24]
        vs, fs_, ps = b[24:192], b[192:224], b[224:672]
        if mode == 1:
            vs = normalize_vp_scalar(vs)
        vk = (vph, vs)
        if vk not in vptab:
            vptab[vk] = len(vptab)
        fk = (fph, fs_)
        if fk not in fptab:
            fptab[fk] = len(fptab)
        pk = (psh, ps)
        if pk not in ptab:
            ptab[pk] = len(ptab)
        entry_idx.append((vptab[vk], fptab[fk], ptab[pk]))
    return vptab, fptab, ptab, entry_idx


def _emit_body(vptab, fptab, ptab, entry_idx, raw, mode):
    """Serialize the BODY section (format-agnostic part of v1/v2)."""
    out = bytearray()
    out += struct.pack("<I", len(raw))
    out += struct.pack("<I", 0)  # reserved (raw = whole-blob zlib)
    for name, _data in raw:
        nb = name.encode()
        out += struct.pack("<I", len(nb))
        out += nb
        out += struct.pack("<I", len(_data))
    raw_blob = b"".join(data for _n, data in raw)
    raw_blob_c = zlib.compress(raw_blob, 9)
    out += struct.pack("<I", len(raw_blob_c))
    out += raw_blob_c
    out += struct.pack("<B", mode)
    out += b"\x00" * 3
    out += struct.pack("<I", len(entry_idx))
    out += struct.pack("<I", len(vptab))
    out += struct.pack("<I", len(fptab))
    out += struct.pack("<I", len(ptab))
    for (vph, vs) in vptab:
        out += vph + vs
    for (fph, fs_) in fptab:
        out += fph + fs_
    for (psh, ps) in ptab:
        out += psh + ps
    for (a, c_, d) in entry_idx:
        out += struct.pack("<III", a, c_, d)
    return bytes(out)


def _module_label(ppu_dir):
    """'ppu-AAA111-GOWA.SELF' -> 'GOWA.SELF' (everything after the 2nd dash)."""
    name = os.path.basename(os.path.abspath(ppu_dir))
    parts = name.split("-", 2)
    return parts[2] if len(parts) == 3 else ""


def _pack_module(ppu_dir, mode):
    """Pack one ppu dir (a shaders_cache tree) into a module dict + BODY bytes.
    Returns None when the module has no entries (nothing to pack)."""
    root, n = find_pipelines_dir(os.path.join(ppu_dir, "shaders_cache"))
    if root is None:
        return None
    cls = os.path.basename(os.path.dirname(root))
    ver = os.path.basename(root)
    cache_root = _cache_root_from_pipelines(root)
    raw_dir = _find_raw_dir(cache_root)
    if raw_dir is None:
        print(f"ERROR: no raw/ dir under {cache_root}")
        sys.exit(1)

    files = sorted(f for f in os.listdir(root) if f.endswith(".bin"))
    entries = []
    st_ok = st_bad = 0
    for f in files:
        with open(os.path.join(root, f), "rb") as fh:
            b = fh.read()
        if len(b) != 672:
            continue
        st_f = int(f[:-4].split("+")[3], 16)
        if st_f == state_hash(b):
            st_ok += 1
        else:
            st_bad += 1
        entries.append((f, b))
    if not entries:
        return None  # empty module — skip

    vptab, fptab, ptab, entry_idx = _dedup(entries, mode)
    raw = _read_raw(raw_dir)
    body = _emit_body(vptab, fptab, ptab, entry_idx, raw, mode)
    return {
        "name": os.path.basename(os.path.abspath(ppu_dir)),
        "label": _module_label(ppu_dir),
        "cls": cls,
        "ver": ver,
        "entries": len(entries),
        "raw": len(raw),
        "body": body,
        "st_ok": st_ok,
        "st_bad": st_bad,
    }


def _find_modules(cache_path):
    """Accepts a game dir (<GAMEID> with ppu-* children) or a single ppu dir.
    -> (game_id, [ppu_dir, ...]) sorted."""
    cache_path = os.path.abspath(cache_path)
    if os.path.isdir(os.path.join(cache_path, "shaders_cache")):
        game_id = os.path.basename(os.path.dirname(cache_path))
        if game_id.lower() == "cache":
            game_id = "UNKNOWN"
        return game_id, [cache_path]
    modules = []
    if os.path.isdir(cache_path):
        for sub in sorted(os.listdir(cache_path)):
            d = os.path.join(cache_path, sub)
            if sub.startswith("ppu-") and os.path.isdir(os.path.join(d, "shaders_cache")):
                modules.append(d)
    return os.path.basename(cache_path), modules


def cmd_pack(args):
    game_id, ppu_dirs = _find_modules(args.cache)
    if not ppu_dirs:
        print("ERROR: no ppu-*/shaders_cache dirs under", args.cache)
        sys.exit(1)
    if not args.out:
        args.out = game_id + ".rsca"

    mode = 1 if args.mode == "normalized" else 0
    packed, skipped = [], []
    for d in ppu_dirs:
        m = _pack_module(d, mode)
        if m is None:
            skipped.append(os.path.basename(d))
        else:
            packed.append(m)
    if not packed:
        print("ERROR: no module with entries found under", args.cache)
        sys.exit(1)

    out = bytearray()
    out += MAGIC
    out += struct.pack("<I", FORMAT)
    out += game_id.encode().ljust(16, b"\x00")[:16]
    out += struct.pack("<I", len(packed))
    for m in packed:
        nb = m["name"].encode()
        out += struct.pack("<I", len(nb))
        out += nb
        out += m["cls"].encode().ljust(16, b"\x00")[:16]
        out += m["ver"].encode().ljust(16, b"\x00")[:16]
        out += m["body"]

    with open(args.out, "wb") as fh:
        fh.write(bytes(out))
    print(f"game_id   : {game_id}")
    print(f"format    : RSCA v2, {len(packed)} module(s)"
          + (f", skipped empty: {', '.join(skipped)}" if skipped else ""))
    for m in packed:
        print(f"  - {m['label'] or m['name']}: {m['entries']} entries, "
              f"{m['raw']} raw, {m['cls']}/{m['ver']} "
              f"(state_hash {m['st_ok']} OK, {m['st_bad']} BAD)")
    print(f"packed    : {len(out):,} B ({len(out)/1024/1024:.2f} MB) -> {args.out}")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def _parse_body(data, off):
    """Parse a BODY section at off. -> (mode, raw, vptab, fptab, ptab, ent, new_off)."""
    raw_count = u32(data, off)
    off += 8  # raw_count + reserved
    raw_index = []
    for _ in range(raw_count):
        nlen = u32(data, off)
        off += 4
        name = data[off:off + nlen].decode()
        off += nlen
        dlen = u32(data, off)
        off += 4
        raw_index.append((name, dlen))
    blob_c_len = u32(data, off)
    off += 4
    raw_blob = zlib.decompress(data[off:off + blob_c_len])
    off += blob_c_len
    raw = []
    pos = 0
    for name, dlen in raw_index:
        raw.append((name, raw_blob[pos:pos + dlen]))
        pos += dlen
    if pos != len(raw_blob):
        raise ValueError(f"raw split mismatch {pos} != {len(raw_blob)}")
    mode = data[off]
    off += 4
    N = u32(data, off)
    off += 4
    Uvp = u32(data, off)
    off += 4
    Ufp = u32(data, off)
    off += 4
    Upr = u32(data, off)
    off += 4
    vptab = []
    for _ in range(Uvp):
        vph = data[off:off + 8]
        vs = data[off + 8:off + 8 + VP_SCAL]
        off += 8 + VP_SCAL
        vptab.append((vph, vs))
    fptab = []
    for _ in range(Ufp):
        fph = data[off:off + 8]
        fs_ = data[off + 8:off + 8 + FP_SCAL]
        off += 8 + FP_SCAL
        fptab.append((fph, fs_))
    ptab = []
    for _ in range(Upr):
        psh = data[off:off + 8]
        ps = data[off + 8:off + 8 + PROPS]
        off += 8 + PROPS
        ptab.append((psh, ps))
    ent = []
    for _ in range(N):
        a, c_, d = struct.unpack_from("<III", data, off)
        off += 12
        ent.append((a, c_, d))
    return mode, raw, vptab, fptab, ptab, ent, off


def parse_pack(data):
    """Parse an RSCA v1 or v2 file.
    -> {"version", "game_id", "modules": [
         {"name" ("": legacy v1), "cls", "ver", "mode",
          "raw", "vptab", "fptab", "ptab", "ent"}]}"""
    if data[0:4] != MAGIC:
        raise ValueError("bad magic (not RSCA)")
    fmt = struct.unpack_from("<I", data, 4)[0]
    game_id = data[8:24].rstrip(b"\x00").decode()
    if fmt == FORMAT_V1:
        cls = data[24:40].rstrip(b"\x00").decode()
        ver = data[40:56].rstrip(b"\x00").decode()
        mode, raw, vptab, fptab, ptab, ent, off = _parse_body(data, 56)
        if off != len(data):
            raise ValueError(f"trailing bytes: {len(data) - off}")
        modules = [{"name": "", "cls": cls, "ver": ver, "mode": mode,
                    "raw": raw, "vptab": vptab, "fptab": fptab, "ptab": ptab, "ent": ent}]
        return {"version": fmt, "game_id": game_id, "modules": modules}
    if fmt != FORMAT:
        raise ValueError(f"unsupported format {fmt}")
    mcount = u32(data, 24)
    off = 28
    modules = []
    for _ in range(mcount):
        nlen = u32(data, off)
        off += 4
        name = data[off:off + nlen].decode()
        off += nlen
        cls = data[off:off + 16].rstrip(b"\x00").decode()
        off += 16
        ver = data[off:off + 16].rstrip(b"\x00").decode()
        off += 16
        mode, raw, vptab, fptab, ptab, ent, off = _parse_body(data, off)
        modules.append({"name": name, "cls": cls, "ver": ver, "mode": mode,
                        "raw": raw, "vptab": vptab, "fptab": fptab, "ptab": ptab,
                        "ent": ent})
    if off != len(data):
        raise ValueError(f"trailing bytes: {len(data) - off}")
    return {"version": fmt, "game_id": game_id, "modules": modules}


def list_modules(path):
    """Summary of the modules inside an RSCA file (for menus/diagnostics)."""
    with open(path, "rb") as f:
        data = f.read()
    p = parse_pack(data)
    out = []
    for m in p["modules"]:
        out.append({"name": m["name"], "label": _module_label(m["name"]) if m["name"] else "(v1 legacy)",
                    "cls": m["cls"], "ver": m["ver"],
                    "entries": len(m["ent"]), "raw": len(m["raw"])})
    return {"version": p["version"], "game_id": p["game_id"], "modules": out}


def _entry_bytes(vptab, fptab, ptab, a, c_, d):
    vph, vs = vptab[a]
    fph, fs_ = fptab[c_]
    psh, ps = ptab[d]
    e = vph + fph + psh + vs + fs_ + ps
    assert len(e) == 672, f"entry len {len(e)}"
    return e


def _entry_name(e):
    st = state_hash(e)
    return f"{u64(e, 0):X}+{u64(e, 8):X}+{u64(e, 16):X}+{st:X}.bin"


def _write_module_to(m, root, overwrite=False):
    """Write one parsed module under <root>/shaders_cache. Returns (raw_added,
    raw_present, ent_added, ent_present)."""
    raw_dir = os.path.join(root, "shaders_cache", "raw")
    target = os.path.join(root, "shaders_cache", "pipelines", m["cls"], m["ver"])
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(target, exist_ok=True)
    ra = rs = ea = es = 0
    for name, d in m["raw"]:
        p = os.path.join(raw_dir, name)
        if os.path.exists(p) and not overwrite:
            rs += 1
        else:
            open(p, "wb").write(d)
            ra += 1
    for (a, c_, d) in m["ent"]:
        e = _entry_bytes(m["vptab"], m["fptab"], m["ptab"], a, c_, d)
        p = os.path.join(target, _entry_name(e))
        if os.path.exists(p) and not overwrite:
            es += 1
        else:
            open(p, "wb").write(e)
            ea += 1
    return ra, rs, ea, es


# ---------------------------------------------------------------------------
# unpack / merge
# ---------------------------------------------------------------------------
def _ppu_dirs_under(d):
    """ppu-* dirs (sorted) that contain a shaders_cache, under dir d."""
    if not os.path.isdir(d):
        return []
    out = []
    for sub in sorted(os.listdir(d)):
        p = os.path.join(d, sub)
        if sub.startswith("ppu-") and os.path.isdir(os.path.join(p, "shaders_cache")):
            out.append(p)
    return out


def _count_entries(p):
    """Number of .bin pipeline files in a version dir (0 when absent)."""
    if not os.path.isdir(p):
        return 0
    return sum(1 for f in os.listdir(p) if f.endswith(".bin"))


def _match_ppu(target, name, label):
    """Find the ppu dir on this machine for a module from the file:
    exact name first, then by module label (suffix after the 2nd dash)."""
    dirs = _ppu_dirs_under(target)
    for d in dirs:
        if os.path.basename(d) == name:
            return d
    for d in dirs:
        if label and _module_label(d) == label:
            return d
    return None


def cmd_unpack(args):
    with open(args.pack, "rb") as f:
        data = f.read()
    p = parse_pack(data)
    for m in p["modules"]:
        if m["name"]:
            root = os.path.join(args.target, m["name"])
        else:
            root = args.target  # v1 legacy: write directly under --target
        ra, _rs, ea, _es = _write_module_to(m, root, overwrite=True)
        lbl = _module_label(m["name"]) if m["name"] else "(v1 legacy)"
        print(f"game_id   : {p['game_id']}  (RSCA v{p['version']})")
        print(f"module    : {lbl} {m['cls']}/{m['ver']}")
        print(f"raw files : {ra} -> {os.path.join(root, 'shaders_cache', 'raw')}")
        print(f"entries   : {ea} -> {os.path.join(root, 'shaders_cache', 'pipelines', m['cls'], m['ver'])}")
        print(f"mode      : {'normalized' if m['mode'] else 'byte-exact'}")


def cmd_merge(args):
    """Union an RSCA file into an existing cache.
    --target: the game dir (<GAMEID> with ppu-* children) or a single ppu dir.
    v1 files: the first usable ppu dir under the target (legacy behavior).
    v2 files: each module is matched to a local ppu dir by name/label."""
    with open(args.pack, "rb") as f:
        data = f.read()
    p = parse_pack(data)
    target = os.path.abspath(args.target)

    missing = []
    if p["version"] == FORMAT_V1:
        if os.path.isdir(os.path.join(target, "shaders_cache")):
            pairs = [(m, target) for m in p["modules"]]
        else:
            dirs = _ppu_dirs_under(target)
            if not dirs:
                print("ERROR: no ppu-*/shaders_cache dir under", target)
                sys.exit(1)
            m = p["modules"][0]
            if len(dirs) == 1:
                choice = dirs[0]
            else:
                # v1 files don't record the module name — pick the ppu dir whose
                # entry count is closest to the file's (a cache only grows)
                file_n = len(m["ent"])
                scored = sorted(
                    (abs(_count_entries(os.path.join(d, "shaders_cache", "pipelines", m["cls"], m["ver"])) - file_n),
                     _count_entries(os.path.join(d, "shaders_cache", "pipelines", m["cls"], m["ver"])), d)
                    for d in dirs)
                if scored[1][0] == scored[0][0]:
                    print(f"ERROR: v1 file has no module name and {target} has several ppu dirs — "
                          f"pass --ppu DIR to choose:")
                    for _s, n, d in scored:
                        print(f"  - {os.path.basename(d)} ({n} entries)")
                    sys.exit(1)
                choice = scored[0][2]
            pairs = [(m, choice) for m in p["modules"]]
    elif os.path.isdir(os.path.join(target, "shaders_cache")):
        if len(p["modules"]) == 1:
            pairs = [(p["modules"][0], target)]
        else:
            print(f"ERROR: target is a single ppu dir but the file has "
                  f"{len(p['modules'])} modules — pass the game dir")
            sys.exit(1)
    else:
        pairs = []
        missing = []
        for m in p["modules"]:
            d = _match_ppu(target, m["name"], _module_label(m["name"]))
            if d:
                pairs.append((m, d))
            else:
                missing.append(m)

    print(f"game_id : {p['game_id']}  (RSCA v{p['version']})")
    for m, d in pairs:
        ra, rs, ea, es = _write_module_to(m, d, overwrite=args.overwrite)
        lbl = _module_label(m["name"]) if m["name"] else "(v1 legacy)"
        print(f"  {lbl}: {ea} entries added, {es} present; "
              f"{ra} raw added, {rs} present  -> {d}")
    if missing:
        for m in missing:
            lbl = _module_label(m["name"])
            print(f"  {lbl or m['name']}: NOT IMPORTED — no matching ppu-* dir for {p['game_id']} "
                  f"under {target}")
        print("  -> launch that game once in RPCS3 (it creates the dir), then re-run")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="RPCS3 shader-cache RSCA packer/extractor.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="compare two pipeline dirs (byte-for-byte)")
    v.add_argument("orig")
    v.add_argument("restored")
    p = sub.add_parser("pack", help="game dir (all non-empty ppu modules) or one ppu dir -> <GAMEID>.rsca")
    p.add_argument("cache")
    p.add_argument("-o", "--out", default=None, help="output file (default: <game-id>.rsca)")
    p.add_argument("--game-id", default=None, help="override the game id in the header")
    p.add_argument("--mode", default="byte-exact", choices=["byte-exact", "normalized"])
    u = sub.add_parser("unpack", help="RSCA file -> dir(s) with shaders_cache/")
    u.add_argument("pack")
    u.add_argument("--target", required=True, help="dir to write under (one subdir per module for v2)")
    l = sub.add_parser("list", help="list the modules inside an RSCA file")
    l.add_argument("pack")
    m = sub.add_parser("merge", help="union an RSCA file into an existing game/ppu cache dir")
    m.add_argument("pack")
    m.add_argument("--target", required=True, help="game dir (<GAMEID>) or a ppu-* dir")
    m.add_argument("--overwrite", action="store_true", help="rewrite files even if present")
    args = ap.parse_args()

    if args.cmd == "verify":
        cmd_verify(args)
    elif args.cmd == "pack":
        cmd_pack(args)
    elif args.cmd == "unpack":
        cmd_unpack(args)
    elif args.cmd == "list":
        info = list_modules(args.pack)
        print(f"game    : {info['game_id']}   (RSCA v{info['version']})")
        for m in info["modules"]:
            print(f"  - {m['label']}: {m['entries']} entries, {m['raw']} raw, {m['cls']}/{m['ver']}")
    elif args.cmd == "merge":
        cmd_merge(args)


def cmd_verify(args):
    a = sorted(f for f in os.listdir(args.orig) if f.endswith(".bin"))
    b = sorted(f for f in os.listdir(args.restored) if f.endswith(".bin"))
    sa, sb = set(a), set(b)
    only_a, only_b = sa - sb, sb - sa
    common = sa & sb
    identical = diff = 0
    diff_details = []
    for f in sorted(common):
        A = open(os.path.join(args.orig, f), "rb").read()
        B = open(os.path.join(args.restored, f), "rb").read()
        if len(A) != len(B) or A != B:
            diff += 1
            if len(diff_details) < 3:
                if len(A) == len(B):
                    diffs = [i for i in range(len(A)) if A[i] != B[i]]
                    diff_details.append((f, diffs[:8], "..." if len(diffs) > 8 else ""))
                else:
                    diff_details.append((f, "len", f"{len(A)} vs {len(B)}"))
        else:
            identical += 1
    print(f"orig files      : {len(a)}")
    print(f"restored files  : {len(b)}")
    print(f"only in orig    : {len(only_a)}")
    print(f"only in restored: {len(only_b)}")
    print(f"common          : {len(common)}")
    print(f"byte-identical  : {identical}")
    print(f"differing       : {diff}")
    for d in diff_details:
        print("   DIFF:", d)
    if diff == 0 and not only_a and not only_b:
        print("RESULT: PERFECT ROUND-TRIP (byte-for-byte identical)")
    else:
        print("RESULT: NOT byte-identical (see above)")


if __name__ == "__main__":
    main()
