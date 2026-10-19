#!/usr/bin/env python3
"""RPCS3 shader-cache EXPORT: portable full game cache (raw ucode + pipeline state).

One archive per game (<GAMEID>.tar.gz). Runs on the machine where the cache was
BUILT. Each archive contains manifest.json + the game's full shaders_cache tree
(raw/ + pipelines/...). The recipient's machine compiles the shaders once at first
launch (the normal "Preloading cached shaders" pass) - the point is that it
happens up front, so their first playthrough has no in-game shader stutter.

A "game" is one <GAMEID> dir (e.g. BCES01741) and may hold several ppu-*/shaders_cache
trees (different .SELF/.BIN); they all land in the same archive.

Real on-disk layout (verified on RPCS3 v0.0.43, AMD/AMD):
    <data-dir>/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
        raw/<VP_HASH>.vp | <FP_HASH>.fp          # portable raw ucode
        pipelines/vulkan/v1.95/<VP>+<FP>+<S>+<H>.bin   # backend/version dir varies!
    data dir: ~/.cache/rpcs3 (AppImage/official), ~/.local/share/rpcs3, Flatpak...
    Entry: first 16 bytes = u64 vp_hash, u64 fp_hash (little-endian),
           then pipeline_storage_hash + full pipeline state (uniform size per build,
           e.g. 672 B on v0.0.43; rsx_cache.h: pipeline_data struct).

Usage:
  python3 export_cache.py -o OUT_DIR [--root DATA_DIR] [--game SUBSTR] [--debug]
  -o/--output: directory for the archives (default: .) — one <GAMEID>.tar.gz per game
  --root: RPCS3 data dir (auto: ~/.cache/rpcs3, ~/.local/share/rpcs3, Flatpak, ~/rpcs3)
  --game: case-insensitive substring match on the GAMEID (default: all games)
  --debug: add provenance to manifest.json (source hostname, absolute data dir).
           Off by default: the archive is meant to be shared, so it stays clean.
"""
import argparse
import io
import json
import os
import struct
import sys
import tarfile
import time

MANIFEST_FORMAT = "rpcs3-shader-cache-transfer"
MANIFEST_VERSION = 3


def find_data_dir():
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".cache", "rpcs3"),
        os.path.join(home, ".local", "share", "rpcs3"),
        os.path.join(home, ".var", "app", "net.rpcs3.RPCS3", ".local", "share", "rpcs3"),
        os.path.join(home, "rpcs3"),
    ]
    for d in candidates:  # prefer candidates that actually contain a cache
        if os.path.isdir(d) and find_game_dirs(d):
            return d
    for d in candidates:
        if os.path.isdir(d):
            return d
    return None


def find_game_dirs(root, max_depth=8):
    """Game dirs containing a usable shaders_cache (has raw/ AND pipelines/).
    The layout is nested (cache/<GAMEID>/ppu-*-<SELF>), so bounded os.walk,
    not a single-level listdir. Returns paths relative to root."""
    out = []
    for dirpath, dirnames, _ in os.walk(root, topdown=True):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if "shaders_cache" in dirnames:
            sc = os.path.join(dirpath, "shaders_cache")
            if os.path.isdir(os.path.join(sc, "raw")) and os.path.isdir(os.path.join(sc, "pipelines")):
                out.append(os.path.relpath(dirpath, root))
        dirnames[:] = [d for d in dirnames if depth < max_depth]
    return sorted(out)


def pipeline_dirs(sc):
    """All pipelines/<backend>/<version> dirs with .bin entries, e.g.
    'pipelines/vulkan/v1.95'. Version dir is NOT hardcoded (build-dependent)."""
    out = []
    base = os.path.join(sc, "pipelines")
    if not os.path.isdir(base):
        return out
    for backend in sorted(os.listdir(base)):
        bp = os.path.join(base, backend)
        if not os.path.isdir(bp):
            continue
        for ver in sorted(os.listdir(bp)):
            vp = os.path.join(bp, ver)
            if os.path.isdir(vp) and any(f.endswith(".bin") for f in os.listdir(vp)):
                out.append(os.path.join("pipelines", backend, ver))
    return out


def parse_entry(path):
    """First 16 bytes of pipeline_data: u64 vp_hash, u64 fp_hash (little-endian)."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        if len(head) < 16:
            return None
        return struct.unpack_from("<QQ", head, 0)
    except OSError:
        return None


def entry_filename_hashes(name):
    """'<VP>+<FP>+<S>+<H>.bin' -> (vp, fp) as ints, or None."""
    stem = name[:-4] if name.endswith(".bin") else name
    parts = stem.split("+")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0], 16), int(parts[1], 16)
    except ValueError:
        return None


def scan_pu_dir(root, pu_dir):
    """Scan one ppu-*/shaders_cache tree into a manifest 'caches' entry (dict)."""
    sc = os.path.join(root, pu_dir, "shaders_cache")
    raw_dir = os.path.join(sc, "raw")
    pdirs = pipeline_dirs(sc)
    entries, sizes, missing, unparseable = [], set(), 0, 0
    for pdir in pdirs:
        for b in sorted(os.listdir(os.path.join(sc, pdir))):
            if not b.endswith(".bin"):
                continue
            bp = os.path.join(sc, pdir, b)
            sizes.add(os.path.getsize(bp))
            parsed = parse_entry(bp)
            if parsed is None:
                unparseable += 1
                continue
            vp, fp = parsed
            fname = entry_filename_hashes(b)
            if fname and fname != (vp, fp):
                print(f"  [warn] {pu_dir}/{pdir}/{b}: bytes {vp:X}+{fp:X} != filename {fname[0]:X}+{fname[1]:X}")
            if not (os.path.exists(os.path.join(raw_dir, format(vp, "X") + ".vp"))
                    and os.path.exists(os.path.join(raw_dir, format(fp, "X") + ".fp"))):
                missing += 1
            entries.append([format(vp, "X"), format(fp, "X"), b])
    raw_files = sorted(f for f in os.listdir(raw_dir) if f.endswith((".vp", ".fp")))
    raw_bytes = sum(os.path.getsize(os.path.join(raw_dir, f)) for f in raw_files)
    return {"dir": pu_dir,
            "entry_count": len(entries),
            "entry_file_size_bytes": sorted(sizes),
            "pipelines": pdirs,
            "unparseable_entries": unparseable,
            "raw_files": len(raw_files), "raw_bytes": raw_bytes,
            "missing_raw": missing,
            "entries": entries}


def write_game_archive(out_path, root, game_id, caches, debug):
    """Write one <GAMEID>.tar.gz: manifest.json + the game's cache trees."""
    totals = {"caches": len(caches),
              "entries": sum(c["entry_count"] for c in caches),
              "raw_files": sum(c["raw_files"] for c in caches),
              "raw_bytes": sum(c["raw_bytes"] for c in caches),
              "missing_raw": sum(c["missing_raw"] for c in caches),
              "unparseable_entries": sum(c["unparseable_entries"] for c in caches)}
    manifest = {"format": MANIFEST_FORMAT, "version": MANIFEST_VERSION,
                "game": game_id,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "caches": caches, "totals": totals}
    if debug:  # provenance only on explicit opt-in (archive is meant to be shared)
        import socket
        manifest["host"] = socket.gethostname()
        manifest["data_dir"] = root
    with tarfile.open(out_path, "w:gz") as tar:
        blob = json.dumps(manifest, indent=1).encode()
        ti = tarfile.TarInfo("manifest.json")
        ti.size = len(blob)
        tar.addfile(ti, io.BytesIO(blob))
        keep = lambda i: i if (i.type == tarfile.DIRTYPE
                               or i.name.split(os.sep)[-1].endswith((".vp", ".fp", ".bin"))) else None
        for c in caches:
            base = os.path.join(root, c["dir"], "shaders_cache")
            arcbase = os.path.join(c["dir"], "shaders_cache")
            if os.path.isdir(os.path.join(base, "raw")):
                tar.add(os.path.join(base, "raw"), arcname=os.path.join(arcbase, "raw"), filter=keep)
            for pdir in c["pipelines"]:
                src = os.path.join(base, *pdir.split("/"))
                if os.path.isdir(src):
                    tar.add(src, arcname=os.path.join(arcbase, *pdir.split("/")), filter=keep)
    return totals


def main():
    ap = argparse.ArgumentParser(description="Export an RPCS3 shader cache (one <GAMEID>.tar.gz per game).")
    ap.add_argument("-o", "--output", default=".",
                    help="output directory for <GAMEID>.tar.gz archives (default: .)")
    ap.add_argument("--root", help="RPCS3 data dir (dir containing cache/<GAMEID>/...)")
    ap.add_argument("--game", help="case-insensitive substring match on the GAMEID (default: all games)")
    ap.add_argument("--debug", action="store_true",
                    help="include debug provenance in the manifest (hostname, absolute data dir)")
    args = ap.parse_args()

    root = os.path.abspath(args.root) if args.root else find_data_dir()
    if not root or not os.path.isdir(root):
        sys.exit("ERROR: data dir not found. Pass --root /dir/containing/cache/<GAMEID>/...\n"
                 "(RPCS3 data dir, e.g. ~/.cache/rpcs3 or ~/.local/share/rpcs3).")
    print(f"data dir : {root}")

    pu_dirs = find_game_dirs(root)
    if args.game:
        needle = args.game.lower()
        pu_dirs = [g for g in pu_dirs
                   if needle in os.path.basename(os.path.dirname(g)).lower()
                   or needle in os.path.basename(g).lower()]
    if not pu_dirs:
        sys.exit("ERROR: no usable shader caches (dir/shaders_cache/{raw,pipelines}) under the data dir.")

    by_game = {}
    for g in pu_dirs:
        game_id = os.path.basename(os.path.dirname(g))
        if game_id in (".", ""):
            game_id = os.path.basename(g)
        by_game.setdefault(game_id, []).append(g)

    out_dir = os.path.abspath(args.output)
    os.makedirs(out_dir, exist_ok=True)

    written = 0
    for game_id in sorted(by_game):
        caches = [scan_pu_dir(root, pu) for pu in sorted(by_game[game_id])]
        caches = [c for c in caches if c["entry_count"] > 0]
        if not caches:
            print(f"  [skip] {game_id}: no pipeline entries")
            continue
        out_path = os.path.join(out_dir, f"{game_id}.tar.gz")
        totals = write_game_archive(out_path, root, game_id, caches, args.debug)
        print(f"  [ok] {game_id}: {totals['entries']} entries in {len(caches)} ppu dir(s), "
              f"{totals['raw_files']} raw files ({totals['raw_bytes'] / 1e6:.1f} MB) -> {out_path}")
        if totals["missing_raw"]:
            print("       WARNING: some entries lack raw/ files -> they will be skipped at load.")
        written += 1

    if not written:
        sys.exit("ERROR: nothing to export (no pipeline caches with entries found).")
    print(f"\nEXPORT OK -> {written} archive(s) in {out_dir}")


if __name__ == "__main__":
    main()
