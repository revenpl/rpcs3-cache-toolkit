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
import hashlib
import types

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


def search_hits(root, max_depth=5):
    """Bounded walk from root. A hit = a dir whose child 'cache/' contains a usable
    shaders_cache (RPCS3 always stores them under <data>/cache/). Returns
    [(data_dir, [trees]), ...] in discovery order."""
    hits = []
    seen = set()
    if not os.path.isdir(root):
        return hits
    for dirpath, dirnames, _files in os.walk(root, topdown=True):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == os.curdir else rel.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        if "cache" in dirnames:
            trees = find_shader_caches(dirpath)
            if trees:
                d = os.path.abspath(dirpath)
                if d not in seen:
                    seen.add(d)
                    hits.append((d, trees))
                dirnames[:] = []  # don't descend further through a confirmed hit
    return hits


def discover(home=None, env=None):
    """-> (data_dir | None, [trees]). Pass 1: platform candidates (first one that
    actually contains a usable cache wins). Pass 2: bounded search from home and
    Windows common roots. (None, []) when nothing found."""
    home = home or os.path.expanduser("~")
    env = dict(env) if env is not None else dict(os.environ)
    for cand in candidate_data_dirs(home, env):
        trees = find_shader_caches(cand)
        if trees:
            return cand, trees
    roots = [home]
    if os.name == "nt":
        roots += [os.path.join(env.get("ProgramFiles") or "C:/Program Files"),
                  env.get("LOCALAPPDATA") or "",
                  os.path.join(home, "Desktop"),
                  os.path.join(home, "Downloads")]
    for root in roots:
        for data_dir, trees in search_hits(root):
            return data_dir, trees
    return None, []


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_raw(sc):
    """All shader binaries under <sc>/raw/ (real layout: <HASH>.vp / <HASH>.fp),
    sorted by filename. -> [{path, size, sha256}, ...]"""
    raws = []
    raw_dir = os.path.join(sc, "raw")
    if not os.path.isdir(raw_dir):
        return raws
    for name in sorted(os.listdir(raw_dir)):
        p = os.path.join(raw_dir, name)
        if os.path.isfile(p):
            raws.append({"path": p, "size": os.path.getsize(p), "sha256": _sha256_file(p)})
    return raws


def list_rsca_files(program_dir):
    if not os.path.isdir(program_dir):
        return []
    return sorted(f for f in os.listdir(program_dir) if f.lower().endswith(".rsca"))


def read_rsca_game_id(path):
    """Game id from the RSCA v1 header (bytes 8..23) — no full parse needed."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:4] != opt_cache.MAGIC:
        raise ValueError(f"not an RSCA file: {path}")
    return head[8:24].rstrip(b"\x00").decode()


def pick_ppu_dir(data_dir, game_id):
    """First ppu-* dir (sorted) under <data_dir>/cache/<game_id>/ (case-insensitive
    game match), or None. The RSCA manifest doesn't carry the ppu name, so import
    can only target a dir that already exists on this machine."""
    base = os.path.join(data_dir, "cache")
    if not os.path.isdir(base):
        return None
    for g in sorted(os.listdir(base)):
        if g.lower() != game_id.lower():
            continue
        d = os.path.join(base, g)
        for sub in sorted(os.listdir(d)):
            if sub.startswith("ppu-") and os.path.isdir(os.path.join(d, sub)):
                return os.path.join(d, sub)
    return None


def do_import(rsca_path, data_dir, ppu_dir=None):
    """Merge .rsca into the local cache (idempotent union). Returns the ppu dir used.
    Raises FileNotFoundError when the game has no ppu-* dir yet."""
    game = read_rsca_game_id(rsca_path)
    if ppu_dir is None:
        ppu_dir = pick_ppu_dir(data_dir, game)
        if ppu_dir is None:
            raise FileNotFoundError(
                f"no ppu-* cache dir for {game} under {os.path.join(data_dir, 'cache')}\n"
                f"  -> launch the game once in RPCS3 (it creates the dir), then re-run,\n"
                f"  or pass --ppu DIR explicitly.")
    opt_cache.cmd_merge(types.SimpleNamespace(pack=rsca_path, target=ppu_dir, overwrite=False))
    return ppu_dir
