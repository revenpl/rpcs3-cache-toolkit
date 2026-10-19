#!/usr/bin/env python3
"""RPCS3 shader-cache IMPORT: install a portable game cache (raw ucode + pipeline
state) on another machine. One archive = one game (<GAMEID>.tar.gz).

Runs on the TARGET machine (GPU B). Layout is restored exactly as exported:
    <target-data-dir>/<game_rel>/shaders_cache/{raw,pipelines/<backend>/<version>}
A game archive may contain several ppu-*/shaders_cache trees (different .SELF/.BIN);
all of them are installed.

Install mode is MERGE by default: entries are unique hash-named files, so copying
them alongside an existing cache is always safe (no overwrite). Use --backup to
move any pre-existing ppu dir aside first (clean state).

NOTE: the pipelines/ version dir (e.g. v1.95) is part of the RPCS3 storage format.
If the target build uses a different version dir, the emulator simply won't read
this cache (it will rebuild) - nothing is corrupted. The recipient's machine always
compiles the shaders once at launch (the file carries source + state, not compiled
GPU code) - that's the normal "Preloading cached shaders" pass, now up front.

Legacy v2 archives (one archive holding several games, 'games' manifest list)
are still accepted: everything inside is installed.

Usage:
  python3 import_cache.py BCES01741.tar.gz [--root TARGET_DATA_DIR] [--game SUBSTR] [--backup]
--root:   target RPCS3 data dir (auto: ~/.cache/rpcs3, ~/.local/share/rpcs3, Flatpak,
         ~/rpcs3; created if it doesn't exist yet - first install on GPU B).
--game:   install only if the archive's GAMEID matches this substring (default: install).
--backup: move pre-existing ppu dirs aside as <dir>.bak-<timestamp> (clean state).
"""
import argparse
import json
import os
import shutil
import sys
import tarfile
import time


def find_data_dir():
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".cache", "rpcs3"),
        os.path.join(home, ".local", "share", "rpcs3"),
        os.path.join(home, ".var", "app", "net.rpcs3.RPCS3", ".local", "share", "rpcs3"),
        os.path.join(home, "rpcs3"),
    ]
    for d in candidates:
        if os.path.isdir(d):
            return d
    return candidates[0]  # default target for a fresh install


def load_manifest(archive):
    """Read manifest.json from the archive (with path-safety check)."""
    with tarfile.open(archive, "r:gz") as tar:
        for n in tar.getnames():
            if os.path.normpath(n).startswith("..") or os.path.isabs(n):
                sys.exit(f"ERROR: unsafe path in archive: {n}")
        mf = tar.extractfile("manifest.json")
        if mf is None:
            sys.exit("ERROR: manifest.json missing from archive (not made by export_cache.py?)")
        manifest = json.load(mf)
    return manifest


def normalize_manifest(manifest):
    """-> (game_id, caches): caches = list of per-ppu-dir entries, each with 'dir'."""
    if "caches" in manifest:  # v3: one game per archive
        return manifest.get("game", "UNKNOWN"), manifest["caches"]
    # v2 (legacy): one archive, 'games' list of per-ppu-dir entries
    by_game = {}
    for t in manifest.get("games", []):
        g = t["game"]
        game_id = os.path.basename(os.path.dirname(g)) or os.path.basename(g)
        entry = {"dir": g,
                 "entry_count": t.get("entry_count", 0),
                 "entry_file_size_bytes": t.get("entry_file_size_bytes", []),
                 "pipelines": t.get("pipelines", []),
                 "missing_raw": t.get("missing_raw", 0),
                 "entries": t.get("entries", [])}
        by_game.setdefault(game_id, []).append(entry)
    if not by_game:
        sys.exit("ERROR: no games in archive")
    if len(by_game) == 1:
        game_id, caches = next(iter(by_game.items()))
    else:  # mixed legacy archive: install everything
        game_id = ",".join(sorted(by_game))
        caches = [e for es in (by_game[k] for k in sorted(by_game)) for e in es]
    return game_id, caches


def main():
    ap = argparse.ArgumentParser(description="Import an RPCS3 shader-cache archive (one game per archive).")
    ap.add_argument("archive", help="the <GAMEID>.tar.gz from export_cache.py")
    ap.add_argument("--root", help="target RPCS3 data dir")
    ap.add_argument("--game", help="install only if the archive's GAMEID matches this substring")
    ap.add_argument("--backup", action="store_true",
                    help="move pre-existing ppu dirs aside (clean state) instead of merging")
    args = ap.parse_args()

    if not os.path.isfile(args.archive):
        sys.exit(f"ERROR: archive not found: {args.archive}")

    manifest = load_manifest(args.archive)
    game_id, caches = normalize_manifest(manifest)
    if args.game:
        needle = args.game.lower()
        if needle not in game_id.lower():
            sys.exit(f"ERROR: archive is for game '{game_id}', not '{args.game}'")

    target_root = os.path.abspath(args.root) if args.root else find_data_dir()

    mf_info = f"format={manifest['format']} v{manifest.get('version')} game={game_id}"
    if manifest.get("host"):  # only present when the exporter used --debug
        mf_info += f" from {manifest['host']}"
    mf_info += f" at {manifest.get('created')}"
    print(f"manifest   : {mf_info}")
    print(f"target     : {target_root}")

    staging = os.path.join(os.path.dirname(os.path.abspath(args.archive)),
                           f".import_staging_{os.getpid()}")
    os.makedirs(staging, exist_ok=True)
    try:
        with tarfile.open(args.archive, "r:gz") as tar:
            tar.extractall(staging)

        notes = []
        for c in caches:
            g = c["dir"]
            sc_src = os.path.join(staging, g, "shaders_cache")
            sc_dst = os.path.join(target_root, g, "shaders_cache")
            if not os.path.isdir(os.path.join(sc_src, "raw")):
                sys.exit(f"ERROR: archive entry '{g}' missing shaders_cache/raw")
            pdirs = c.get("pipelines", [])
            if not pdirs:
                sys.exit(f"ERROR: archive entry '{g}' has no pipelines list")

            # size compatibility vs an existing cache in the SAME version dirs
            for pdir in pdirs:
                pipe_dst = os.path.join(sc_dst, *pdir.split(os.sep))
                if not os.path.isdir(pipe_dst):
                    continue
                existing = {os.path.getsize(os.path.join(pipe_dst, f))
                            for f in os.listdir(pipe_dst) if f.endswith(".bin")}
                incoming = set(c.get("entry_file_size_bytes", []))
                if existing and incoming and not (existing & incoming):
                    notes.append(f"'{g}': existing {pdir} entries {sorted(existing)} B vs incoming "
                                 f"{sorted(incoming)} B = different cache format. The emulator will drop "
                                 f"its old entries at load and keep the imported ones.")

            if args.backup and os.path.isdir(os.path.join(target_root, g)):
                ts = time.strftime("%Y%m%d_%H%M%S")
                bak = os.path.join(target_root, g) + f".bak-{ts}"
                shutil.move(os.path.join(target_root, g), bak)
                notes.append(f"'{g}': existing cache moved aside -> {bak}")

            # merge: copy raw/ and every pipelines/<backend>/<version> dir
            n_raw = n_pipe = 0
            raw_dst = os.path.join(sc_dst, "raw")
            os.makedirs(raw_dst, exist_ok=True)
            for f in os.listdir(os.path.join(sc_src, "raw")):
                if f.endswith((".vp", ".fp")):
                    shutil.copy2(os.path.join(sc_src, "raw", f), os.path.join(raw_dst, f))
                    n_raw += 1
            for pdir in pdirs:
                src_dir = os.path.join(sc_src, *pdir.split(os.sep))
                dst_dir = os.path.join(sc_dst, *pdir.split(os.sep))
                if not os.path.isdir(src_dir):
                    continue
                os.makedirs(dst_dir, exist_ok=True)
                for f in os.listdir(src_dir):
                    if f.endswith(".bin"):
                        shutil.copy2(os.path.join(src_dir, f), os.path.join(dst_dir, f))
                        n_pipe += 1

            # verify compilability: every installed entry has both raw files
            compilable = skipped = 0
            for vp, fp, fname in c.get("entries", []):
                if os.path.exists(os.path.join(raw_dst, vp + ".vp")) \
                   and os.path.exists(os.path.join(raw_dst, fp + ".fp")):
                    compilable += 1
                else:
                    skipped += 1
            notes.append(f"'{g}': {n_pipe} entries installed ({compilable} compilable, "
                         f"{skipped} will be skipped at load), {n_raw} raw files copied/merged")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print()
    for n in notes:
        print("  -", n)
    print("\nIMPORT OK - launch the game once: the 'Preloading cached shaders' / "
          "'Compiling shader cache' dialog runs up front, then it should be stutter-free.")


if __name__ == "__main__":
    main()
