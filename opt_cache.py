#!/usr/bin/env python3
"""
opt_cache.py — RPCS3 shaders_cache (Vulkan) -> RSCA v1 (RPCS3 Shader Cache Archive).

Layout of one 672-byte pipeline_data entry (from rsx_cache.h, verified):
  0    8  vertex_program_hash
  8    8  fragment_program_hash
  16   8  pipeline_storage_hash
  24   4  vp_ctrl0
  28   4  vp_ctrl1
  32   4  vp_texture_dimensions
  36   4  vp_reserved_0
  40  72  vp_instruction_mask[9]
  112  4  vp_base_address
  116  4  vp_entry
  120 64  vp_jump_table[32]
  184  2  vp_multisampled_textures
  186  2  vp_reserved_1
  188  4  vp_reserved_2
  192  4  fp_ctrl
  196  4  fp_texture_dimensions
  200  4  fp_texcoord_control
  204  2  fp_height
  206  2  fp_pixel_layout
  208  2  fp_lighting_flags
  210  2  fp_shadow_textures
  212  2  fp_redirected_textures
  214  2  fp_multisampled_textures
  216  1  fp_mrt_count
  217  1  fp_reserved0
  218  2  fp_reserved1
  220  4  fp_reserved2
  224 448  pipeline_properties
  672    (end)

Dedup tables:
  vp    row = 8B vph + 168B scalar  (entry[24:192])   key=(vph, scalar)
  fp    row = 8B fph + 32B scalar   (entry[192:224])  key=(fph, scalar)
  props row = 8B psh + 448B block   (entry[224:672])  key=(psh, block)
  entry = 3 x u32 indices (vp, fp, props)

state_hash (4th hash in filename) = FNV-1a 64 over 14 u32 params,
NOT including base_address/entry  -> invariant under normalization.

Modes:
  byte-exact : keep original base_address/entry  -> round-trip byte-identical
  normalized : base_address->0, entry->(entry-base) -> smaller, same decompiler
               semantics (VertexProgramDecompiler only uses entry-base_address)
"""
import os, struct, sys, argparse, collections

VP_SCAL  = 168               # entry[24:192]
FP_SCAL  = 32                # entry[192:224]
PROPS    = 448               # entry[224:672]
MASK=(1<<64)-1; SEED=14695981039346656037; PRIME=1099511628211
MAGIC=b"RSCA"; FORMAT=1     # RSCA v1 = RPCS3 Shader Cache Archive: manifest + raw/ + dedup state

def u8(b,o):  return b[o]
def u16(b,o): return struct.unpack_from("<H",b,o)[0]
def u32(b,o): return struct.unpack_from("<I",b,o)[0]
def u64(b,o): return struct.unpack_from("<Q",b,o)[0]

def state_hash(b):
    params=[u32(b,24),u32(b,28),u32(b,192),u32(b,32),u32(b,196),
            u32(b,200),u16(b,204),u16(b,206),u16(b,208),u16(b,210),
            u16(b,212),u16(b,184),u16(b,214),u32(b,216)]
    h=SEED
    for v in params:
        h ^= v; h = (h*PRIME) & MASK
    return h

def find_pipelines_dir(cache_root):
    best=(None,0)
    for root,dirs,files in os.walk(cache_root):
        if root.endswith("pipelines") is False and "/pipelines/" in root:
            pass
        if files and root.rsplit("/",1)[-1] and "pipelines" in root:
            n=sum(1 for f in files if f.endswith(".bin"))
            # only the leaf dir that holds the .bin files
            if n>best[1]:
                best=(root,n)
    return best

def normalize_vp_scalar(s):
    # s = 168-byte vp scalar (entry[24:192])
    # base_address at entry[112] -> scalar[112-24=88]; entry at entry[116] -> scalar[92]
    s=bytearray(s)
    base=u32(bytes(s),88); ent=u32(bytes(s),92)
    s[88:92]= (0).to_bytes(4,"little")
    s[92:96]= ((ent-base)&0xFFFFFFFF).to_bytes(4,"little")
    return bytes(s)

def cmd_verify(args):
    # compare two pipeline dirs
    a=sorted(f for f in os.listdir(args.orig) if f.endswith(".bin"))
    b=sorted(f for f in os.listdir(args.restored) if f.endswith(".bin"))
    sa=set(a); sb=set(b)
    only_a=sa-sb; only_b=sb-sa
    common=sa&sb
    identical=0; diff=0; diff_details=[]
    for f in sorted(common):
        A=open(os.path.join(args.orig,f),"rb").read()
        B=open(os.path.join(args.restored,f),"rb").read()
        if len(A)!=len(B) or A!=B:
            diff+=1
            if len(diff_details)<3:
                if len(A)==len(B):
                    diffs=[i for i in range(len(A)) if A[i]!=B[i]]
                    diff_details.append((f, diffs[:8], "..." if len(diffs)>8 else ""))
                else:
                    diff_details.append((f,"len",f"{len(A)} vs {len(B)}"))
        else:
            identical+=1
    print(f"orig files     : {len(a)}")
    print(f"restored files : {len(b)}")
    print(f"only in orig   : {len(only_a)}")
    print(f"only in restored: {len(only_b)}")
    print(f"common         : {len(common)}")
    print(f"byte-identical : {identical}")
    print(f"differing      : {diff}")
    for d in diff_details: print("   DIFF:",d)
    if diff==0 and only_a==set() and only_b==set():
        print("RESULT: PERFECT ROUND-TRIP (byte-for-byte identical)")
    else:
        print("RESULT: NOT byte-identical (see above)")

# ---------------------------------------------------------------------------
# RSCA v1 — full compact file: manifest + raw/ + dedup state
# ---------------------------------------------------------------------------
def _cache_root_from_pipelines(pipelines_leaf):
    # pipelines_leaf = .../shaders_cache/pipelines/<cls>/<ver>
    parts = pipelines_leaf.split("/")
    return "/".join(parts[:-3])  # .../shaders_cache

def _find_raw_dir(cache_root):
    raw = os.path.join(cache_root, "raw")
    if os.path.isdir(raw):
        return raw
    for root, dirs, files in os.walk(cache_root):
        if "raw" in dirs:
            return os.path.join(root, "raw")
    return None

def _derive_game_id(cache_root):
    # real layout: .../cache/<GAMEID>/ppu-XXX/shaders_cache
    parts = [p for p in cache_root.split("/") if p]
    # if 'shaders_cache' is last and we have >=3 parts, game id is 3rd from end
    if len(parts) >= 3 and parts[-1] == "shaders_cache":
        return parts[-3]
    return None

def _read_raw(raw_dir):
    out = []
    for name in sorted(os.listdir(raw_dir)):
        if not (name.endswith(".vp") or name.endswith(".fp")):
            continue
        data = open(os.path.join(raw_dir, name), "rb").read()
        out.append((name, data))
    return out

def _dedup(entries, mode):
    vptab = collections.OrderedDict(); fptab = collections.OrderedDict(); ptab = collections.OrderedDict()
    entry_idx = []
    for f, b in entries:
        vph = b[0:8]; fph = b[8:16]; psh = b[16:24]
        vs = b[24:192]; fs_ = b[192:224]; ps = b[224:672]
        if mode == 1:
            vs = normalize_vp_scalar(vs)
        vk = (vph, vs)
        if vk not in vptab: vptab[vk] = len(vptab)
        fk = (fph, fs_)
        if fk not in fptab: fptab[fk] = len(fptab)
        pk = (psh, ps)
        if pk not in ptab: ptab[pk] = len(ptab)
        entry_idx.append((vptab[vk], fptab[fk], ptab[pk]))
    return vptab, fptab, ptab, entry_idx

def _emit_tables(out, vptab, fptab, ptab, entry_idx):
    out += struct.pack("<I", len(vptab))
    out += struct.pack("<I", len(fptab))
    out += struct.pack("<I", len(ptab))
    for (vph, vs) in vptab: out += vph + vs
    for (fph, fs_) in fptab: out += fph + fs_
    for (psh, ps) in ptab:   out += psh + ps
    for (a, c_, d) in entry_idx: out += struct.pack("<III", a, c_, d)

def _rebuild_entry(vph, vs, fph, fs_, psh, ps):
    e = vph + fph + psh + vs + fs_ + ps
    assert len(e) == 672, f"entry len {len(e)}"
    return e

def cmd_pack(args):
    root, n = find_pipelines_dir(args.cache)
    if root is None:
        print("ERROR: no pipelines dir with .bin files under", args.cache); sys.exit(1)
    cls = root.split("/")[-2]; ver = root.split("/")[-1]
    cache_root = _cache_root_from_pipelines(root)
    raw_dir = _find_raw_dir(cache_root)
    if raw_dir is None:
        print("ERROR: no raw/ dir found under", cache_root); sys.exit(1)

    game_id = args.game_id or _derive_game_id(cache_root) or "UNKNOWN"
    if not args.out:
        args.out = game_id + ".rsca"

    files = sorted(f for f in os.listdir(root) if f.endswith(".bin"))
    entries = []
    st_ok = st_bad = 0
    for f in files:
        b = open(os.path.join(root, f), "rb").read()
        if len(b) != 672: continue
        st_f = int(f[:-4].split("+")[3], 16)
        if st_f == state_hash(b): st_ok += 1
        else: st_bad += 1
        entries.append((f, b))
    if st_bad:
        print(f"WARN: state_hash mismatch {st_bad}/{len(entries)}")

    mode = 1 if args.mode == "normalized" else 0
    vptab, fptab, ptab, entry_idx = _dedup(entries, mode)
    raw = _read_raw(raw_dir)

    N = len(entries)
    out = bytearray()
    out += MAGIC
    out += struct.pack("<I", FORMAT)
    out += game_id.encode().ljust(16, b"\x00")[:16]
    out += cls.encode().ljust(16, b"\x00")[:16]
    out += ver.encode().ljust(16, b"\x00")[:16]
    import zlib
    raw_blob = b"".join(data for _, data in raw)
    raw_blob_c = zlib.compress(raw_blob, 9)
    out += struct.pack("<I", len(raw))
    out += struct.pack("<I", 0)  # reserved (raw = whole-blob zlib)
    for name, data in raw:
        nb = name.encode()
        out += struct.pack("<I", len(nb)); out += nb
        out += struct.pack("<I", len(data))
    out += struct.pack("<I", len(raw_blob_c))
    out += raw_blob_c
    out += struct.pack("<B", mode)
    out += b"\x00" * 3
    out += struct.pack("<I", N)
    _emit_tables(out, vptab, fptab, ptab, entry_idx)

    open(args.out, "wb").write(bytes(out))
    cur_state = sum(len(b) for _, b in entries)
    cur_raw = sum(len(d) for _, d in raw)
    opt = len(out)
    # deduped state size (tables + entry indices)
    state_opt = len(vptab)*(8+VP_SCAL) + len(fptab)*(8+FP_SCAL) + len(ptab)*(8+PROPS) + N*12
    print(f"game_id         : {game_id}")
    print(f"backend/ver     : {cls} / {ver}")
    print(f"mode            : {args.mode}")
    print(f"pipelines       : {N} entries")
    print(f"raw files       : {len(raw)}")
    print(f"vp/fp/props rows: {len(vptab)} / {len(fptab)} / {len(ptab)}")
    print(f"orig state      : {cur_state:,} B ({cur_state/1024:,.0f} KB)")
    print(f"dedup state     : {state_opt:,} B ({state_opt/1024:,.0f} KB)  [{100*(1-state_opt/cur_state):.0f}% smaller]")
    print(f"raw compressed  : {cur_raw:,} B -> {len(raw_blob_c):,} B  ({cur_raw/len(raw_blob_c):.2f}x, -{100*(1-len(raw_blob_c)/cur_raw):.0f}%)")
    print(f"orig total      : {cur_state+cur_raw:,} B ({(cur_state+cur_raw)/1024/1024:,.2f} MB)")
    print(f"packed total    : {opt:,} B ({opt/1024/1024:,.2f} MB)")
    print(f"state_hash check: {st_ok} OK, {st_bad} BAD")
    print(f"wrote           : {args.out}")

def _parse_pack(data):
    """Parse an RSCA v1 blob -> (game_id, cls, ver, mode, raw, (vptab,fptab,ptab), ent)."""
    import zlib
    assert data[0:4] == MAGIC, "bad magic (not RSCA)"
    fmt = struct.unpack_from("<I", data, 4)[0]
    assert fmt == FORMAT, f"unsupported format {fmt}"
    game_id = data[8:24].rstrip(b"\x00").decode()
    cls     = data[24:40].rstrip(b"\x00").decode()
    ver     = data[40:56].rstrip(b"\x00").decode()
    raw_count = u32(data, 56)
    off = 64
    raw_index = []
    for _ in range(raw_count):
        nlen = u32(data, off); off += 4
        name = data[off:off+nlen].decode(); off += nlen
        dlen = u32(data, off); off += 4
        raw_index.append((name, dlen))
    blob_c_len = u32(data, off); off += 4
    raw_blob = zlib.decompress(data[off:off+blob_c_len]); off += blob_c_len
    raw = []
    pos = 0
    for name, dlen in raw_index:
        raw.append((name, raw_blob[pos:pos+dlen])); pos += dlen
    assert pos == len(raw_blob), f"raw split mismatch {pos} != {len(raw_blob)}"
    mode = data[off]; off += 1; off += 3
    N   = u32(data, off); off += 4
    Uvp = u32(data, off); off += 4
    Ufp = u32(data, off); off += 4
    Upr = u32(data, off); off += 4
    vptab = []
    for _ in range(Uvp):
        vph = data[off:off+8]; vs = data[off+8:off+8+VP_SCAL]; off += 8+VP_SCAL
        vptab.append((vph, vs))
    fptab = []
    for _ in range(Ufp):
        fph = data[off:off+8]; fs_ = data[off+8:off+8+FP_SCAL]; off += 8+FP_SCAL
        fptab.append((fph, fs_))
    ptab = []
    for _ in range(Upr):
        psh = data[off:off+8]; ps = data[off+8:off+8+PROPS]; off += 8+PROPS
        ptab.append((psh, ps))
    ent = []
    for _ in range(N):
        a, c_, d = struct.unpack_from("<III", data, off); off += 12
        ent.append((a, c_, d))
    assert off == len(data), f"trailing bytes: {len(data)-off}"
    return game_id, cls, ver, mode, raw, (vptab, fptab, ptab), ent

def _entry_bytes(vptab, fptab, ptab, a, c_, d):
    vph, vs = vptab[a]; fph, fs_ = fptab[c_]; psh, ps = ptab[d]
    return _rebuild_entry(vph, vs, fph, fs_, psh, ps)

def _entry_name(e):
    st = state_hash(e)
    return f"{u64(e,0):X}+{u64(e,8):X}+{u64(e,16):X}+{st:X}.bin"

def cmd_unpack(args):
    data = open(args.pack, "rb").read()
    game_id, cls, ver, mode, raw, (vptab, fptab, ptab), ent = _parse_pack(data)

    cache_root = os.path.join(args.target, "shaders_cache")
    raw_dir = os.path.join(cache_root, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    for name, d in raw:
        open(os.path.join(raw_dir, name), "wb").write(d)
    target = os.path.join(cache_root, "pipelines", cls, ver)
    os.makedirs(target, exist_ok=True)
    written = 0
    for (a, c_, d) in ent:
        e = _entry_bytes(vptab, fptab, ptab, a, c_, d)
        open(os.path.join(target, _entry_name(e)), "wb").write(e)
        written += 1
    print(f"game_id   : {game_id}")
    print(f"backend/ver: {cls} / {ver}")
    print(f"raw files : {len(raw)} -> {raw_dir}")
    print(f"entries   : {written} -> {target}")
    print(f"mode      : {'normalized' if mode else 'byte-exact'}")

def cmd_merge(args):
    """Union the pack into an existing cache: add files not already present."""
    data = open(args.pack, "rb").read()
    game_id, cls, ver, mode, raw, (vptab, fptab, ptab), ent = _parse_pack(data)
    cache_root = os.path.join(args.target, "shaders_cache")
    raw_dir  = os.path.join(cache_root, "raw")
    target   = os.path.join(cache_root, "pipelines", cls, ver)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(target, exist_ok=True)
    ra = rs = ea = es = 0
    for name, d in raw:
        p = os.path.join(raw_dir, name)
        if os.path.exists(p) and not args.overwrite:
            rs += 1
        else:
            open(p, "wb").write(d); ra += 1
    for (a, c_, d) in ent:
        e = _entry_bytes(vptab, fptab, ptab, a, c_, d)
        p = os.path.join(target, _entry_name(e))
        if os.path.exists(p) and not args.overwrite:
            es += 1
        else:
            open(p, "wb").write(e); ea += 1
    print(f"game_id    : {game_id}")
    print(f"backend/ver: {cls} / {ver}")
    print(f"raw        : {ra} added, {rs} already present  -> {raw_dir}  (now {len(os.listdir(raw_dir))})")
    print(f"entries    : {ea} added, {es} already present  -> {target}  (now {len(os.listdir(target))})")
    print(f"mode       : {'normalized' if mode else 'byte-exact'}")


def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)
    v=sub.add_parser("verify", help="compare two pipeline dirs (byte-for-byte)")
    v.add_argument("orig"); v.add_argument("restored")
    v.set_defaults(fn=cmd_verify)
    p=sub.add_parser("pack", help="cache (raw+pipelines) -> one RSCA v1 file")
    p.add_argument("cache"); p.add_argument("-o","--out",default=None, help="output file (default: <game-id>.rsca)")
    p.add_argument("--game-id",default=None, help="e.g. BCES01741 (else auto-derived/UNKNOWN)")
    p.add_argument("--mode",default="byte-exact",choices=["byte-exact","normalized"])
    p.set_defaults(fn=cmd_pack)
    u=sub.add_parser("unpack", help="RSCA v1 file -> raw/ + pipelines/")
    u.add_argument("pack"); u.add_argument("--target",required=True, help="dir to write shaders_cache/ under")
    u.set_defaults(fn=cmd_unpack)
    m=sub.add_parser("merge", help="union an RSCA v1 file into an existing cache (skip files already present)")
    m.add_argument("pack"); m.add_argument("--target",required=True, help="dir holding (or to receive) shaders_cache/")
    m.add_argument("--overwrite",action="store_true", help="rewrite files even if present")
    m.set_defaults(fn=cmd_merge)
    args=ap.parse_args()
    args.fn(args)

if __name__=="__main__":
    main()
