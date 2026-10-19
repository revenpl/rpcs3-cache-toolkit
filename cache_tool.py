#!/usr/bin/env python3
"""cache_tool.py — interactive RPCS3 shader-cache manager (RSCA v1).

Flow: auto-discover the RPCS3 data dir (Windows / Linux / macOS)
      -> choose operation: add a cache (import .rsca) or export a game cache
      -> choose the game / cache dir.

Import picks a .rsca file from the program folder (next to this script) and
merges it into the local cache (idempotent union, via opt_cache.cmd_merge).
Export packs one game cache dir into <GAMEID>.rsca in the program folder
(via opt_cache.cmd_pack). All format logic lives in opt_cache.py.

Python 3, stdlib only.

Non-interactive (for scripting):
  cache_tool.py discover
  cache_tool.py list
  cache_tool.py import  FILE [--data DIR] [--ppu DIR]
  cache_tool.py export  [--game BCES01741] [--ppu DIR] [--out DIR]
"""
import os
import sys

PROGRAM_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROGRAM_DIR)
import opt_cache  # noqa: E402

SKIP = {".git", "node_modules", ".npm", ".venv", "venv", ".cargo", ".rustup",
        ".gradle", ".m2", "__pycache__", "site-packages",
        "Microsoft", "Google", "Packages",
        "dev_hdd0", "dev_flash", "dev_hdd1"}


def candidate_data_dirs(home, env):
    """Platform data-dir candidates (dir containing cache/<GAMEID>/...), most likely first.
    Mirrors RPCS3's own logic (Utilities/File.cpp fs::get_cache_dir):
      Windows: $RPCS3_CONFIG_DIR, then common install spots (data lives next to rpcs3.exe)
      macOS  : $HOME/Library/Caches/rpcs3
      Linux  : $XDG_CACHE_HOME else $XDG_CONFIG_HOME else $HOME/.cache, + /rpcs3
    Returns a plain list; existence is NOT checked here (caller validates by content)."""
    out = []

    def add(p):
        if p and os.path.isabs(p) and p not in out:
            out.append(p)

    if os.name == "nt":
        add(env.get("RPCS3_CONFIG_DIR"))
        add(os.path.join(home, "rpcs3"))
        add(os.path.join(home, "AppData", "Local", "rpcs3"))
        add(os.path.join(env.get("ProgramFiles") or "C:/Program Files", "rpcs3"))
    elif sys.platform == "darwin":
        add(os.path.join(home, "Library", "Caches", "rpcs3"))
        add(os.path.join(home, "rpcs3"))
    else:
        add(os.path.join(env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "rpcs3"))
        add(os.path.join(env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "rpcs3"))
        add(os.path.join(home, ".local", "share", "rpcs3"))
        # Flatpak (XDG dirs inside the sandbox)
        add(os.path.join(home, ".var", "app", "net.rpcs3.RPCS3", ".cache", "rpcs3"))
        add(os.path.join(home, ".var", "app", "net.rpcs3.RPCS3", ".local", "share", "rpcs3"))
        add(os.path.join(home, "rpcs3"))
    return out


def _count_bins(pipelines_dir):
    return sum(len([f for f in files if f.endswith(".bin")])
               for _r, _d, files in os.walk(pipelines_dir))


def find_shader_caches(data_dir, max_depth=8):
    """Usable shader caches under <data_dir>/cache/: a 'shaders_cache' dir with
    raw/ AND pipelines/. Returns sorted list of dicts:
      {game_id, ppu_dir, sc (the shaders_cache path), entries (count of .bin)}"""
    base = os.path.join(data_dir, "cache")
    trees = []
    if not os.path.isdir(base):
        return trees
    for dirpath, dirnames, _files in os.walk(base, topdown=True):
        rel = os.path.relpath(dirpath, base)
        depth = 0 if rel == os.curdir else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
            continue
        if (os.path.basename(dirpath) == "shaders_cache"
                and os.path.isdir(os.path.join(dirpath, "raw"))
                and os.path.isdir(os.path.join(dirpath, "pipelines"))):
            sc = dirpath
            ppu = os.path.dirname(sc)
            trees.append({
                "game_id": os.path.basename(os.path.dirname(ppu)),
                "ppu_dir": ppu,
                "sc": sc,
                "entries": _count_bins(os.path.join(sc, "pipelines")),
            })
            dirnames[:] = []  # never descend into a shaders_cache
        else:
            dirnames[:] = [d for d in dirnames if d not in SKIP]
    return sorted(trees, key=lambda t: (t["game_id"], t["ppu_dir"]))
