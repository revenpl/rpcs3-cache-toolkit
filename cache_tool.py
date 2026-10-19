#!/usr/bin/env python3
"""cache_tool.py — interactive RPCS3 shader-cache manager (RSCA v2).

Flow: auto-discover the RPCS3 data dir (Windows / Linux / macOS)
      -> choose operation: add a cache (import .rsca) or export a game
      -> choose the GAME (not the module — modules are handled automatically).

A game dir (<data>/cache/<GAMEID>/) holds one shaders_cache per PPU module
(EBOOT.BIN, GOWA.SELF, coo.ppu.self, ...). A collection title has one module
per playable game. Export therefore packs ALL non-empty modules of a game
into one <GAMEID>.rsca (RSCA v2); import matches each module to the
local ppu-* dir by name automatically (v1 legacy files still import).

Import picks a .rsca file from the program folder (next to this script) and
merges it into the local cache (idempotent union, via opt_cache.cmd_merge).
Export packs the game's cache into <out>/<GAMEID>.rsca. All format logic
lives in opt_cache.py.

Python 3, stdlib only.

Non-interactive (for scripting):
  cache_tool.py discover
  cache_tool.py list
  cache_tool.py import  FILE [--data DIR] [--ppu DIR]
  cache_tool.py export  [--game BCES01741] [--ppu DIR] [--out DIR]
"""
import os
import sys
import types
import hashlib
import argparse

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


def group_games(trees):
    """Group cache trees by game id. -> {game_id: [tree, ...]} (trees sorted)."""
    games = {}
    for t in trees:
        games.setdefault(t["game_id"], []).append(t)
    return games


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
    """Game id from the RSCA header (bytes 8..23) — same offset in v1 and v2."""
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:4] != opt_cache.MAGIC:
        raise ValueError(f"not an RSCA file: {path}")
    return head[8:24].rstrip(b"\x00").decode()


def rsca_modules(path):
    """Module summary of an RSCA file (v1 or v2) — for menus and diagnostics."""
    try:
        return opt_cache.list_modules(path)
    except (ValueError, OSError):
        return {"version": 0, "game_id": "?", "modules": []}


def game_dir_for(data_dir, game_id):
    """<data_dir>/cache/<game> (case-insensitive match), or None."""
    base = os.path.join(data_dir, "cache")
    if not os.path.isdir(base):
        return None
    for g in sorted(os.listdir(base)):
        if g.lower() == game_id.lower() and os.path.isdir(os.path.join(base, g)):
            return os.path.join(base, g)
    return None


def do_import(rsca_path, data_dir, ppu_dir=None):
    """Merge .rsca into the local cache (idempotent union).
    v2 files: each module is matched to a local ppu-* dir automatically.
    v1 files: merged into the first ppu dir of the game (or --ppu dir).
    Returns a summary line. Raises FileNotFoundError when the game has no
    ppu-* dir yet (must be launched once)."""
    game = read_rsca_game_id(rsca_path)
    if ppu_dir is not None:
        target = ppu_dir
    else:
        target = game_dir_for(data_dir, game)
        if target is None or not opt_cache._ppu_dirs_under(target):
            raise FileNotFoundError(
                f"no ppu-* cache dir for {game} under {os.path.join(data_dir, 'cache')}\n"
                f"  -> launch the game once in RPCS3 (it creates the dir), then re-run,\n"
                f"  or pass --ppu DIR explicitly.")
    opt_cache.cmd_merge(types.SimpleNamespace(pack=rsca_path, target=target, overwrite=False))
    return target


def select_game(games, game=None):
    """Best game to export: the one with the most total entries
    (ties broken by name). None when the game has no usable cache."""
    cands = {g: ts for g, ts in games.items()
             if sum(t["entries"] for t in ts) > 0
             and (game is None or g.lower() == game.lower())}
    if not cands:
        return None
    return max(cands, key=lambda g: (sum(t["entries"] for t in cands[g]), g))


def do_export_game(data_dir, game_id, out_dir, mode="byte-exact"):
    """Pack ALL non-empty modules of a game into <out_dir>/<GAMEID>.rsca (RSCA v2).
    Returns the output path."""
    os.makedirs(out_dir, exist_ok=True)
    gdir = game_dir_for(data_dir, game_id)
    if gdir is None:
        raise FileNotFoundError(f"game dir not found for {game_id} under {os.path.join(data_dir, 'cache')}")
    out = os.path.join(out_dir, f"{game_id}.rsca")
    opt_cache.cmd_pack(types.SimpleNamespace(cache=gdir, out=out, game_id=game_id, mode=mode))
    return out


def _resolve_data(data_arg):
    """(data_dir, trees) from --data or auto-discovery. Exits 2 when unusable."""
    if data_arg:
        d = os.path.abspath(data_arg)
        return d, find_shader_caches(d)
    data, trees = discover()
    if data is None:
        if sys.stdin.isatty():
            print("No RPCS3 shader cache found automatically.")
            p = input("Path to the RPCS3 data dir (containing cache/<GAMEID>/), or empty to abort: ").strip()
            if p:
                data = os.path.abspath(p)
                trees = find_shader_caches(data)
    if data is None:
        sys.stderr.write("ERROR: no RPCS3 data dir found. Pass --data DIR.\n")
        sys.exit(2)
    return data, trees


def _pick(prompt, n):
    while True:
        v = input(prompt).strip()
        if v.isdigit() and 1 <= int(v) <= n:
            return int(v) - 1
        print(f"enter a number 1..{n}")


def _sfo_entry(data, key):
    """Read one string entry from a PSF/SFO file (RPCS3-compatible layout:
    header + 16-byte index entries + null-terminated key table + data region)."""
    import struct
    if len(data) < 20:
        return None
    magic, _ver, off_keys, off_data, count = struct.unpack_from("<IIIII", data, 0)
    if magic != 0x00004653 or off_keys > off_data or off_data > len(data):
        return None
    for i in range(count):
        e = 20 + i * 16
        if e + 16 > len(data):
            return None
        key_off, _fmt, p_len, p_max, d_off = struct.unpack_from("<HHIII", data, e)
        if p_max < p_len or d_off + p_max > len(data) - off_data:
            continue
        ks = data[off_keys + key_off:]
        n = ks.find(b"\x00")
        if n < 0:
            continue
        if ks[:n].decode("utf-8", "replace") == key:
            v = data[off_data + d_off: off_data + d_off + p_len]
            return v.rstrip(b"\x00").decode("utf-8", "replace")
    return None


def _ppu_label(ppu_dir):
    """'ppu-<hash>-GOWA.SELF' -> 'GOWA.SELF' (falls back to the full name)."""
    name = os.path.basename(ppu_dir)
    parts = name.split("-", 2)
    return parts[2] if len(parts) == 3 and parts[0] == "ppu" else name


def _game_library_roots(data_dir):
    """Where RPCS3 looks for game dirs — real VFS keys (rpcs3/Emu/vfs_config.h):
    dev_hdd0 (PS3_GAME/...), dev_bdvd (mounted disc), games_dir — as configured in
    config.yml (VFS section), else their defaults under the data dir; plus the
    platform default library (C:/Games/PS3 or ~/Games/PS3)."""
    roots = []
    cfg = os.path.join(data_dir, "config.yml")
    if os.path.isfile(cfg):
        try:
            import re
            with open(cfg, errors="replace") as f:
                txt = f.read()
            for key in ("dev_hdd0", "dev_hdd1", "dev_bdvd", "games_dir"):
                m = re.search(rf"{key}\s*:\s*(\S+)", txt)
                if m:
                    p = m.group(1).strip("'\"")
                    if p and p not in roots:
                        roots.append(p)
        except OSError:
            pass
    for key, sub in (("dev_hdd0", "PS3_GAME"), ("dev_bdvd", "PS3_GAME"),
                     ("games_dir", None), ("dev_bdvd", None)):
        # defaults live under the data dir when config.yml doesn't override them
        d = os.path.join(data_dir, key) if key != "games_dir" else os.path.join(data_dir, "games")
        if os.path.isdir(d):
            r = os.path.join(d, sub) if sub else d
            if r not in roots:
                roots.append(r)
    if os.name == "nt":
        roots.append(os.path.join("C:", "Games", "PS3"))
    else:
        roots.append(os.path.join(os.path.expanduser("~"), "Games", "PS3"))
    return roots


_TITLE_CACHE = {}


def game_title(game_id, data_dir):
    """Game display name (NPTITLE) from the LOCAL game's PARAM.SFO — offline,
    no network. None when the game dir isn't found in the library paths."""
    key = game_id.upper()
    if key in _TITLE_CACHE:
        return _TITLE_CACHE[key]
    title = None
    for root in _game_library_roots(data_dir):
        for rel in (key, os.path.join(key, "BLURAY"), os.path.join(key, "PS3_GAME")):
            p = os.path.join(root, rel, "PARAM.SFO")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, "rb") as f:
                    t = _sfo_entry(f.read(), "NPTITLE")
            except OSError:
                t = None
            if t and t.strip():
                title = t.strip()
                break
        if title:
            break
    _TITLE_CACHE[key] = title
    return title


def _game_head(game_id, data_dir):
    """'God of War (BCES01741)' when the title is known locally, else 'BCES01741'."""
    t = game_title(game_id, data_dir) if data_dir else None
    return f"{t} ({game_id})" if t else game_id


def _games_view(games, data_dir=None):
    """'God of War (BCES01741)  (2 modules: GOWA.SELF 622, EBOOT.BIN 3)' style lines."""
    lines = []
    for g in sorted(games):
        mods = [t for t in games[g] if t["entries"] > 0]
        empty = len(games[g]) - len(mods)
        detail = ", ".join(f"{_ppu_label(t['ppu_dir'])} {t['entries']}" for t in mods)
        suffix = f" (+{empty} empty)" if empty else ""
        lines.append(f"  game {_game_head(g, data_dir)}  ({len(mods)} module(s): {detail or 'nothing playable yet'}{suffix})")
    return lines


def interactive():
    data, trees = discover()
    if data is None:
        print("No RPCS3 shader cache found automatically.")
        try:
            p = input("Path to the RPCS3 data dir (containing cache/<GAMEID>/), or empty to abort: ").strip()
        except EOFError:
            return 1
        if not p:
            return 1
        data = os.path.abspath(p)
        trees = find_shader_caches(data)
    print(f"data dir : {data}")
    games = group_games(trees)
    for line in _games_view(games, data):
        print(line)
    if not trees:
        print("  (no shader caches yet — play a game first)")
    while True:
        print()
        print("  [1] add a cache to RPCS3 (import a .rsca file)")
        print("  [2] export a game (all its modules) to .rsca")
        print("  [0] quit")
        try:
            c = input("> ").strip()
        except EOFError:
            return 1
        if c == "1":
            rsca = list_rsca_files(PROGRAM_DIR)
            if not rsca:
                print(f"no .rsca files in {PROGRAM_DIR}")
                continue
            for i, f in enumerate(rsca, 1):
                try:
                    info = rsca_modules(os.path.join(PROGRAM_DIR, f))
                    mods = ", ".join(f"{m['label']} {m['entries']}" for m in info["modules"])
                    print(f"  [{i}] {f}  ({info['game_id']}, {len(info['modules'])} module(s): {mods})")
                except Exception:
                    print(f"  [{i}] {f}")
            i = _pick("file: ", len(rsca))
            f = rsca[i]
            try:
                target = do_import(os.path.join(PROGRAM_DIR, f), data)
                print(f"imported into: {target}")
            except FileNotFoundError as e:
                print(f"ERROR: {e}")
            return 0
        if c == "2":
            if not trees:
                print("no shader caches to export")
                continue
            playable = {g: ts for g, ts in games.items() if sum(t["entries"] for t in ts) > 0}
            if not playable:
                print("no game with cached entries yet")
                continue
            names = sorted(playable)
            for i, g in enumerate(names, 1):
                mods = ", ".join(f"{_ppu_label(t['ppu_dir'])} {t['entries']}" for t in playable[g])
                print(f"  [{i}] {_game_head(g, data)}  ({mods})")
            i = _pick("game: ", len(names))
            g = names[i]
            try:
                do_export_game(data, g, PROGRAM_DIR)
            except FileNotFoundError as e:
                print(f"ERROR: {e}")
            return 0
        if c == "0":
            return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Interactive RPCS3 shader-cache manager (RSCA v2).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("discover", help="print the auto-discovered data dir and games")
    sub.add_parser("list", help="list cache games and .rsca files in the program folder")
    p_in = sub.add_parser("import", help="add a .rsca file to the local RPCS3 cache")
    p_in.add_argument("file")
    p_in.add_argument("--data", help="RPCS3 data dir (default: auto-discover)")
    p_in.add_argument("--ppu", help="merge into this exact ppu-* dir (default: auto-match per module)")
    p_ex = sub.add_parser("export", help="pack a whole game (all non-empty modules) into <GAMEID>.rsca")
    p_ex.add_argument("--data", help="RPCS3 data dir (default: auto-discover)")
    p_ex.add_argument("--game", help="game id, e.g. BCES01741 (default: game with most entries)")
    p_ex.add_argument("--ppu", help="export only this exact ppu-* dir (overrides --game)")
    p_ex.add_argument("--out", default=PROGRAM_DIR, help="output dir (default: the program folder)")
    args = ap.parse_args(argv)

    if args.cmd is None:
        return interactive()

    if args.cmd == "discover":
        data, trees = _resolve_data(None)
        print(f"data dir : {data}")
        games = group_games(trees)
        for line in _games_view(games, data):
            print(line)
        return 0

    if args.cmd == "list":
        data, trees = _resolve_data(None)
        print(f"data dir : {data}")
        games = group_games(trees)
        for line in _games_view(games, data):
            print(line)
        print(f"program  : {PROGRAM_DIR}")
        for f in list_rsca_files(PROGRAM_DIR):
            print(f"  .rsca {f}")
        return 0

    if args.cmd == "import":
        data, _trees = _resolve_data(args.data)
        f = args.file if os.path.isabs(args.file) else os.path.join(os.getcwd(), args.file)
        if not os.path.isfile(f):
            alt = os.path.join(PROGRAM_DIR, args.file)
            if os.path.isfile(alt):
                f = alt
            else:
                sys.stderr.write(f"ERROR: file not found: {args.file}\n")
                return 2
        try:
            target = do_import(f, data, args.ppu)
        except FileNotFoundError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 2
        print(f"imported into: {target}")
        return 0

    # export
    data, trees = _resolve_data(args.data)
    os.makedirs(args.out, exist_ok=True)
    if args.ppu:
        ppu = os.path.abspath(args.ppu)
        game = os.path.basename(os.path.dirname(ppu))
        if game.lower() == "cache":
            game = "UNKNOWN"
        out = os.path.join(args.out, f"{game}.rsca")
        opt_cache.cmd_pack(types.SimpleNamespace(cache=ppu, out=out, game_id=game, mode="byte-exact"))
    else:
        games = group_games(trees)
        g = select_game(games, args.game)
        if g is None:
            sys.stderr.write("ERROR: no game with cached entries found.\n")
            return 2
        out = do_export_game(data, g, args.out)
    print(f"exported    : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
