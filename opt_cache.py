#!/usr/bin/env python3
"""
opt_cache.py — portable exporter / importer for the RPCS3 FORK's SPLIT shader cache.

This toolkit targets the fork's SPLIT cache format only (the stock/legacy
tar.gz ".rsca" format is gone — this is an exporter for the fork, so it
speaks the fork's native layout):

    <data>/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
        raw/<HASH>.vp | <HASH>.fp            shader source, byte-exact
        pipelines/vulkan/<version>/pipelines.bin   RSPL manifest (record 320)
        pipelines/vulkan/<version>/device/<KEY>/props.bin  RSPT (record 480)

It packs a whole game (every non-empty module) into ONE self-contained,
portable `.rpo` file, and unpacks it back into a fork-ready directory tree —
no RPCS3 install, no version or driver matching, idempotent.

    .rpo  (RSPO-01, optional zstd)
    ├── manifest.json
    │     gameId, module_count,
    │     modules: [ {name, raw_count, device_count, pipeline_count,
    │                 devices[], sha256{pipelines.bin, raw{}, devices{}}} ]
    ├── <ppu>/pipelines.bin
    ├── <ppu>/device/<KEY>/props.bin
    ├── <ppu>/raw/<HASH>.vp | <HASH>.fp
    └── ... (one namespace per ppu module)

Usage:
  python3 opt_cache.py export <gameId|ppu-dir> <out.rpo> [--level 9] [--root DIR]
  python3 opt_cache.py unpack <in.rpo> <gameDir|dataDir> [--ppu NAME]
  python3 opt_cache.py list   <in.rpo>
  python3 opt_cache.py verify <in.rpo>
"""

import argparse
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:
    zstd = None

# --- split cache format (authoritative: the fork) ---
RSPL_MAGIC = 0x4C505352  # "RSPL" little-endian  (pipelines.bin)
RSPT_MAGIC = 0x54505352  # "RSPT" little-endian  (device/<key>/props.bin)
RSPL_HEADER = 16
RSPT_HEADER = 16
RSPL_RECORD = 320
RSPT_RECORD = 480

# --- .rpo container ---
RSPO_MAGIC = b"RSPO"
RSPO_HEADER = 12  # magic(4) + format_ver(u32) + entry_count(u32)
# every entry (incl. manifest.json) is: name \x00 flag u32-orig u32-payload payload


def _zstd_available() -> bool:
    return zstd is not None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# split cache discovery
# --------------------------------------------------------------------------- #
def _count_files(p: Path) -> int:
    if not p.is_dir():
        return 0
    return sum(1 for f in p.iterdir() if f.is_file())


def _split_cache_stats(raw_dir: Path, device_dir: Path):
    """-> (raw_count, device_count, devices[{key,count,size}])."""
    raw_count = _count_files(raw_dir)
    devices = []
    if device_dir.is_dir():
        for sub in sorted(device_dir.iterdir()):
            if not sub.is_dir():
                continue
            props = sub / "props.bin"
            count = 0
            size = 0
            if props.is_file():
                size = props.stat().st_size
                d = props.read_bytes()
                if len(d) >= RSPT_HEADER:
                    count = struct.unpack_from("<I", d, 8)[0]
            devices.append({"key": sub.name, "count": count, "size": size})
    device_count = sum(d["count"] for d in devices)
    return raw_count, device_count, devices


def find_split_under_ppu(ppu_dir: Path):
    """(raw_dir, device_dir, version_dir) if <ppu_dir>/shaders_cache holds a
    split cache, else None. Picks the highest version dir that has pipelines.bin."""
    cache = ppu_dir / "shaders_cache"
    if not cache.is_dir():
        return None
    vulkan = cache / "pipelines" / "vulkan"
    if not vulkan.is_dir():
        return None
    vdir = None
    for d in sorted(vulkan.iterdir()):
        if d.is_dir() and (d / "pipelines.bin").is_file():
            vdir = d  # keep the last (highest) version
    if vdir is None:
        return None
    raw_dir = cache / "raw"
    device_dir = vdir / "device"
    if not raw_dir.is_dir() or not device_dir.is_dir():
        return None
    return raw_dir, device_dir, vdir


def collect_split_modules(game_dir: Path):
    """All non-empty split caches under <game_dir>/ppu-*/shaders_cache.
    -> [ {name, ppu_dir, raw_dir, device_dir, version_dir,
          raw_count, device_count, devices, pipeline_count}, ... ] (sorted)."""
    mods = []
    if not game_dir.is_dir():
        return mods
    for ppu in sorted(game_dir.glob("ppu-*")):
        if not ppu.is_dir():
            continue
        split = find_split_under_ppu(ppu)
        if split is None:
            continue
        raw_dir, device_dir, vdir = split
        raw_count, device_count, devices = _split_cache_stats(raw_dir, device_dir)
        if raw_count == 0 and device_count == 0:
            continue
        pl = vdir / "pipelines.bin"
        if not pl.is_file():
            continue
        pdata = pl.read_bytes()
        if len(pdata) < RSPL_HEADER or struct.unpack_from("<I", pdata, 0)[0] != RSPL_MAGIC:
            continue
        pl_count = struct.unpack_from("<I", pdata, 8)[0]
        mods.append({
            "name": ppu.name,
            "ppu_dir": ppu,
            "raw_dir": raw_dir,
            "device_dir": device_dir,
            "version_dir": vdir,
            "raw_count": raw_count,
            "device_count": device_count,
            "devices": devices,
            "pipeline_count": pl_count,
        })
    return mods


def resolve_game_dir(source: Path, root: "Path | None" = None) -> Path:
    """Locate a game dir (the dir that directly holds ppu-*/) from several inputs:
      - a ppu-* dir        -> its parent
      - a shaders_cache dir -> parent.parent
      - a game dir         -> itself
      - a gameId name      -> resolved against --root
    Raises FileNotFoundError when nothing usable is found."""
    cands = []
    if source.is_dir():
        base = source
        if source.name.startswith("ppu-"):
            cands.append(source.parent)
        elif (source / "shaders_cache").is_dir():
            cands.append(source.parent)
        else:
            cands.append(source)
    if root is not None:
        cands.append(root / source)
        cands.append(root / source / source)  # in case source is a ppu name
    for c in cands:
        if c.is_dir() and any(p.is_dir() for p in c.glob("ppu-*")):
            return c
    raise FileNotFoundError(
        f"no game dir with ppu-* modules found for {source} "
        f"(pass the game dir, a ppu-* dir, or a gameId with --root)")


# --------------------------------------------------------------------------- #
# .rpo container
# --------------------------------------------------------------------------- #
def _add_entry(buf: bytearray, name: bytes, data: bytes, level: int = 9):
    compressed = None
    if _zstd_available() and len(data) > 256:
        compressed = zstd.ZstdCompressor(level=level).compress(data)
        if compressed is None or len(compressed) >= len(data):
            compressed = None
    payload = compressed if compressed is not None else data
    flag = 1 if compressed is not None else 0
    buf += name + b"\x00"
    buf += struct.pack("<B", flag)
    buf += struct.pack("<Q", len(data))
    buf += struct.pack("<Q", len(payload))
    buf += payload


def _parse_rpo(buf: bytes):
    """Yield (name, data) entries. Raises ValueError on any structural problem."""
    if len(buf) < RSPO_HEADER:
        raise ValueError("not an .rpo file (too short)")
    if buf[:4] != RSPO_MAGIC:
        raise ValueError("bad RSPO magic — not an .rpo file")
    entry_count = struct.unpack_from("<I", buf, 8)[0]
    offset = RSPO_HEADER
    for _ in range(entry_count):
        if offset >= len(buf):
            raise ValueError("truncated .rpo (entry header past end)")
        nul = buf.index(b"\x00", offset)
        name = buf[offset:nul].decode("utf-8")
        offset = nul + 1
        if offset + 17 > len(buf):
            raise ValueError(f"truncated .rpo (entry '{name}' flags)")
        flag = buf[offset]
        offset += 1
        orig_len, payload_len = struct.unpack_from("<QQ", buf, offset)
        offset += 16
        if offset + payload_len > len(buf):
            raise ValueError(f"truncated .rpo (entry '{name}')")
        payload = bytes(buf[offset:offset + payload_len])
        offset += payload_len
        if flag:
            if zstd is None:
                raise ValueError(f"entry '{name}' is zstd-compressed; pip install zstandard")
            data = zstd.ZstdDecompressor().decompress(payload, max_output_size=orig_len)
        else:
            data = payload
        if len(data) != orig_len:
            raise ValueError(f"entry '{name}': decompressed size mismatch")
        yield name, data


def export_game(game_dir: "Path | str", out_path: "Path | str", level: int = 9) -> dict:
    """Pack every non-empty split module of a game into one portable .rpo."""
    game_dir, out_path = Path(game_dir), Path(out_path)
    mods = collect_split_modules(game_dir)
    if not mods:
        raise ValueError(f"no non-empty split cache found under {game_dir}")

    entries = []  # (name, data)
    manifest_modules = []
    for m in mods:
        sc_dir = m["ppu_dir"] / "shaders_cache"
        # gather all source files: pipelines.bin + device props + raw shaders
        src_files = []
        pl = m["version_dir"] / "pipelines.bin"
        if pl.is_file():
            src_files.append(pl)
        if m["device_dir"].is_dir():
            for sub in sorted(m["device_dir"].iterdir()):
                pf = sub / "props.bin"
                if sub.is_dir() and pf.is_file():
                    src_files.append(pf)
        if m["raw_dir"].is_dir():
            for f in sorted(m["raw_dir"].iterdir()):
                if f.is_file():
                    src_files.append(f)

        entry_hashes = {}  # shaders_cache-relative path -> sha256
        for f in src_files:
            rel = os.path.relpath(f, sc_dir).replace(os.sep, "/")
            data = f.read_bytes()
            if rel.endswith("pipelines.bin") and (
                    len(data) < RSPL_HEADER or struct.unpack_from("<I", data, 0)[0] != RSPL_MAGIC):
                raise ValueError(f"{m['name']}: pipelines.bin bad RSPL magic")
            if rel.endswith("props.bin") and (
                    len(data) < RSPT_HEADER or struct.unpack_from("<I", data, 0)[0] != RSPT_MAGIC):
                raise ValueError(f"{m['name']}: {rel} bad RSPT magic")
            entries.append((f"{m['name']}/{rel}", data))
            entry_hashes[rel] = _sha256(data)

        manifest_modules.append({
            "name": m["name"],
            "raw_count": m["raw_count"],
            "device_count": m["device_count"],
            "pipeline_count": m["pipeline_count"],
            "devices": m["devices"],
            "sha256": entry_hashes,
        })

    manifest = {
        "format": "rpo-split",
        "format_version": 1,
        "gameId": game_dir.name,
        "module_count": len(mods),
        "compression": "zstd" if _zstd_available() else "stored",
        "zstd_level": level if _zstd_available() else 0,
        "modules": manifest_modules,
    }
    manifest_json = json.dumps(manifest, indent=2).encode("utf-8")

    buf = bytearray()
    buf += RSPO_MAGIC
    buf += struct.pack("<I", 1)  # format version
    buf += struct.pack("<I", 1 + len(entries))  # entry count (manifest + modules)
    _add_entry(buf, b"manifest.json", manifest_json, level)
    for name, data in entries:
        _add_entry(buf, name.encode("utf-8"), data, level)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(buf))

    size_in = 0
    for m in mods:
        size_in += _tree_bytes(m["raw_dir"])
        size_in += _tree_bytes(m["device_dir"])
        pl = m["version_dir"] / "pipelines.bin"
        if pl.is_file():
            size_in += pl.stat().st_size
    return {
        "gameId": game_dir.name,
        "module_count": len(mods),
        "modules": [m["name"] for m in mods],
        "raw_count": sum(m["raw_count"] for m in mods),
        "device_count": sum(m["device_count"] for m in mods),
        "pipeline_count": sum(m["pipeline_count"] for m in mods),
        "compressed": _zstd_available(),
        "size_in": size_in,
        "size_out": out_path.stat().st_size,
    }


def _tree_bytes(root: Path) -> int:
    total = 0
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    return total


def unpack_game(in_path: "Path | str", target: "Path | str", ppu_name: "str | None" = None) -> dict:
    """Unpack a .rpo into a fork-ready tree.
    target may be:
      - a data dir  (contains cache/<gameId>/) -> unpack under cache/<gameId>/
      - a game dir  (contains ppu-*/)          -> unpack under it directly
      - a ppu dir   (ppu-*)                    -> unpack under its shaders_cache
    Returns {gameId, modules:[{name, files, bytes}], target_root}."""
    in_path, target = Path(in_path), Path(target)
    buf = in_path.read_bytes()
    manifest = None
    entries = {}
    for name, data in _parse_rpo(buf):
        if name == "manifest.json":
            manifest = json.loads(data.decode("utf-8"))
        else:
            entries[name] = data
    if manifest is None:
        raise ValueError("missing manifest.json — not a valid .rpo")
    game_id = manifest.get("gameId", "UNKNOWN")

    # Decide the base dir that directly holds ppu-* modules.
    if ppu_name is not None:
        # single explicit ppu: everything goes under <target>/<ppu_name>/shaders_cache
        base = target
    elif (target / "cache").is_dir():
        base = target / "cache" / game_id
    elif any(p.is_dir() for p in target.glob("ppu-*")):
        base = target
    elif (target / game_id).is_dir() and any(p.is_dir() for p in (target / game_id).glob("ppu-*")):
        base = target / game_id
    else:
        # default: treat target as a data dir
        base = target / "cache" / game_id

    written = 0
    bytes_written = 0
    module_report = []
    for mod in manifest.get("modules", []):
        mname = mod["name"]
        prefix = f"{mname}/"
        target_ppu = base / mname / "shaders_cache"
        files = 0
        for name, data in entries.items():
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            dest = target_ppu / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            files += 1
            bytes_written += len(data)
            written += 1
        module_report.append({"name": mname, "files": files,
                              "raw_count": mod.get("raw_count", 0),
                              "device_count": mod.get("device_count", 0)})
    return {
        "gameId": game_id,
        "base": str(base),
        "modules": module_report,
        "files": written,
        "bytes": bytes_written,
    }


def list_rpo(in_path: "Path | str"):
    in_path = Path(in_path)
    buf = in_path.read_bytes()
    manifest = None
    rows = []
    for name, data in _parse_rpo(buf):
        if name == "manifest.json":
            manifest = json.loads(data.decode("utf-8"))
        else:
            rows.append({"name": name, "size": len(data)})
    return manifest, rows


def verify_rpo(in_path: "Path | str") -> dict:
    in_path = Path(in_path)
    buf = in_path.read_bytes()
    entries = {}
    manifest = None
    for name, data in _parse_rpo(buf):
        if name == "manifest.json":
            manifest = json.loads(data.decode("utf-8"))
        else:
            entries[name] = data
    if manifest is None:
        return {"ok": False, "error": "missing manifest.json"}
    mods = manifest.get("modules", [])
    if not mods:
        return {"ok": False, "error": "no modules in manifest"}
    for mod in mods:
        mname = mod["name"]
        sha = mod.get("sha256", {})
        rels = list(sha.keys())
        if not any(r.endswith("pipelines.bin") for r in rels):
            return {"ok": False, "error": f"module {mname}: missing pipelines.bin"}
        if not any(r.startswith("raw/") for r in rels):
            return {"ok": False, "error": f"module {mname}: missing raw shaders"}
        if not any(r.endswith("props.bin") for r in rels):
            return {"ok": False, "error": f"module {mname}: missing device props"}
        for rel, expect in sha.items():
            n = f"{mname}/{rel}"
            if n not in entries:
                return {"ok": False, "error": f"module {mname}: missing {rel}"}
            if _sha256(entries[n]) != expect:
                return {"ok": False, "error": f"module {mname}: {rel} checksum mismatch"}
    return {"ok": True, "gameId": manifest.get("gameId"),
            "modules": len(mods), "entries": len(entries)}


def main():
    ap = argparse.ArgumentParser(description="RPCS3 fork split-cache toolkit (.rpo)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export", help="pack a game's split cache into one .rpo")
    p_exp.add_argument("source", help="gameId, game dir, or ppu-* dir")
    p_exp.add_argument("output", help="output .rpo path")
    p_exp.add_argument("--root", default=None, help="data dir (when source is a gameId)")
    p_exp.add_argument("--level", type=int, default=9, help="zstd level (1-22)")

    p_un = sub.add_parser("unpack", help="unpack a .rpo into a fork-ready tree")
    p_un.add_argument("input", help=".rpo file")
    p_un.add_argument("output", help="data dir, game dir, or ppu dir")
    p_un.add_argument("--ppu", default=None, help="force a single ppu-* target name")

    p_li = sub.add_parser("list", help="list a .rpo's modules and entries")
    p_li.add_argument("input", help=".rpo file")

    p_ver = sub.add_parser("verify", help="verify a .rpo (structure + checksums)")
    p_ver.add_argument("input", help=".rpo file")

    args = ap.parse_args()

    if args.cmd == "export":
        root = Path(args.root) if args.root else None
        game_dir = resolve_game_dir(Path(args.source), root)
        info = export_game(game_dir, Path(args.output), args.level)
        print(f"exported {info['gameId']} — {info['module_count']} module(s): "
              f"{', '.join(info['modules'])}")
        print(f"  {info['raw_count']} raw / {info['device_count']} device entries, "
              f"{info['pipeline_count']} pipeline records")
        if info["compressed"]:
            ratio = info["size_in"] / info["size_out"] if info["size_out"] else 1
            print(f"  {info['size_in']/1048576:.1f} MiB -> {info['size_out']/1048576:.1f} MiB "
                  f"({ratio:.1f}x, zstd L{args.level})")
        else:
            print("  warning: zstandard not installed — .rpo is uncompressed (pip install zstandard)")
    elif args.cmd == "unpack":
        res = unpack_game(Path(args.input), Path(args.output), args.ppu)
        print(f"unpacked {res['gameId']} — {res['files']} files, "
              f"{res['bytes']/1048576:.1f} MiB -> {res['base']}")
        for m in res["modules"]:
            print(f"  {m['name']}  ({m['files']} files, {m['raw_count']} raw, "
                  f"{m['device_count']} device)")
    elif args.cmd == "list":
        manifest, rows = list_rpo(Path(args.input))
        if manifest:
            print(f"{manifest.get('gameId')} — {manifest.get('module_count')} module(s), "
                  f"{manifest.get('compression') or 'stored'}")
            for mod in manifest.get("modules", []):
                print(f"  module {mod['name']}: {mod['raw_count']} raw, "
                      f"{mod['device_count']} device, {mod['pipeline_count']} pipeline")
                for d in mod.get("devices", []):
                    print(f"      device {d['key']}: {d['count']} entries, {d['size']/1048576:.2f} MiB")
        print(f"  {len(rows)} .rpo entries total")
    elif args.cmd == "verify":
        res = verify_rpo(Path(args.input))
        if res["ok"]:
            print(f"OK — {res['gameId']}, {res['modules']} module(s), {res['entries']} entries, "
                  f"structure + checksums valid")
        else:
            print(f"FAIL — {res.get('error')}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
