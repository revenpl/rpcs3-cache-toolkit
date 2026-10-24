#!/usr/bin/env python3
"""cache_tool.py — interactive RPCS3 FORK shader-cache manager (split format).

Targets the fork's SPLIT cache format only (the legacy ".rsca" tar.gz format
is gone — this is an exporter for the fork, so it speaks the fork's native
layout):

    <data>/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
        raw/<HASH>.vp | <HASH>.fp
        pipelines/vulkan/<version>/pipelines.bin
        pipelines/vulkan/<version>/device/<KEY>/props.bin

Flow: auto-discover the RPCS3 data dir (Windows / Linux / macOS)
      -> choose operation: import a .rpo (add a cache) or export a game
      -> choose the GAME (modules are handled automatically).

A game dir holds one split cache per PPU module (EBOOT.BIN, GOWA.SELF, ...).
Export packs ALL split modules of a game into one <GAMEID>.rpo; import
unpacks a .rpo and matches each module to the local ppu-* dir by name.
All format logic lives in opt_cache.py.

Python 3, stdlib only (+ zstandard for compression, optional).

Non-interactive (for scripting):
  cache_tool.py discover
  cache_tool.py list
  cache_tool.py import  FILE.rpo [--data DIR] [--ppu NAME]
  cache_tool.py export  [--game BCES01741] [--data DIR] [--out DIR]
"""
import os
import struct
import sys
import argparse
from pathlib import Path

PROGRAM_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROGRAM_DIR)
import opt_cache  # noqa: E402

SKIP = {".git", "node_modules", ".npm", ".venv", "venv", ".cargo", ".rustup",
        ".gradle", ".m2", "__pycache__", "site-packages",
        "Microsoft", "Google", "Packages",
        "dev_hdd0", "dev_flash", "dev_hdd1"}


def candidate_data_dirs(home, env):
    """Platform data-dir candidates (dir containing cache/<GAMEID>/...), most likely first.
    Mirrors RPCS3's own logic (Utilities/File.cpp fs::get_cache_dir)."""
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
        add(os.path.join(home, "Library", "Application Support", "rpcs3"))
        add(os.path.join(home, "rpcs3"))
    else:
        add(os.path.join(env.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "rpcs3"))
        add(os.path.join(env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "rpcs3"))
        add(os.path.join(home, ".local", "share", "rpcs3"))
        add(os.path.join(home, ".var", "app", "net.rpcs3.RPCS3", ".cache", "rpcs3"))
        add(os.path.join(home, ".var", "app", "net.rpcs3.RPCS3", ".local", "share", "rpcs3"))
        add(os.path.join(home, "rpcs3"))
    return out


def find_shader_caches(data_dir, max_depth=8):
    """VALID split shader caches under <data_dir>/cache/.
    -> sorted [{game_id, ppu_dir, sc, entries, raw_count, device_count, version}, ...].
    'entries' = pipeline (RSPL) record count. Only split caches are listed —
    legacy (per-entry .bin) dirs are ignored."""
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
            split = opt_cache.find_split_under_ppu(Path(ppu))
            if split is not None:
                raw_dir, device_dir, vdir = split
                _rc, device_count, _devs = opt_cache._split_cache_stats(raw_dir, device_dir)
                raw_count = opt_cache._count_files(raw_dir)
                pl = vdir / "pipelines.bin"
                entries = 0
                if pl.is_file():
                    d = pl.read_bytes()
                    if len(d) >= 16:
                        entries = struct.unpack_from("<I", d, 8)[0]
                trees.append({
                    "game_id": os.path.basename(os.path.dirname(ppu)),
                    "ppu_dir": ppu,
                    "sc": sc,
                    "entries": entries,
                    "raw_count": raw_count,
                    "device_count": device_count,
                    "version": vdir.name,
                })
            dirnames[:] = []  # never descend into a shaders_cache
        else:
            dirnames[:] = [d for d in dirnames if d not in SKIP]
    return sorted(trees, key=lambda t: (t["game_id"], t["ppu_dir"]))


def search_hits(root, max_depth=5):
    """Bounded walk from root. A hit = a dir whose child 'cache/' contains a usable
    split shaders_cache. Returns [(data_dir, [trees]), ...] in discovery order."""
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
                dirnames[:] = []
    return hits


def _looks_like_rpc_install(d):
    """True when dir carries RPCS3-specific artifacts — a genuine install
    (cache dir with logs/ppu_progs, or config dir with config.yml/dev_hdd0),
    possibly one whose shader cache is empty or was deleted."""
    for m in ("config.yml", "dev_hdd0", "RPCS3.log", "RPCS3.log.gz",
              "ppu_progs", "spu_progs", "shaderlog"):
        if os.path.exists(os.path.join(d, m)):
            return True
    return False


def discover(home=None, env=None):
    """-> (data_dir | None, [trees]). (None, []) when nothing found."""
    home = home or os.path.expanduser("~")
    env = dict(env) if env is not None else dict(os.environ)
    real_install = None
    for cand in candidate_data_dirs(home, env):
        trees = find_shader_caches(cand)
        if trees:
            return cand, trees
        if real_install is None and _looks_like_rpc_install(cand):
            real_install = cand
    # A genuine install whose cache is empty/deleted beats a random fixture
    # hit elsewhere in $HOME (test trees & co.) — otherwise import would
    # unpack into the wrong place (2026-10-22 incident: ~/.hermes).
    if real_install is not None:
        return real_install, []
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


# --- .rpo helpers (delegate to opt_cache) ---
def list_rpo_files(program_dir):
    if not os.path.isdir(program_dir):
        return []
    return sorted(f for f in os.listdir(program_dir) if f.lower().endswith(".rpo"))


def rpo_game_id(path):
    manifest, _ = opt_cache.list_rpo(Path(path))
    if manifest is None:
        raise ValueError(f"not an .rpo file (no manifest): {path}")
    return manifest.get("gameId")


def rpo_info(path):
    manifest, _rows = opt_cache.list_rpo(Path(path))
    if manifest is None:
        raise ValueError(f"not an .rpo file (no manifest): {path}")
    return manifest


def game_dir_for(data_dir, game_id):
    """<data_dir>/cache/<game> (case-insensitive match), or None."""
    base = os.path.join(data_dir, "cache")
    if not os.path.isdir(base):
        return None
    for g in sorted(os.listdir(base)):
        if g.lower() == game_id.lower() and os.path.isdir(os.path.join(base, g)):
            return os.path.join(base, g)
    return None


def do_export_game(data_dir, game_id, out_dir, level=9):
    """Pack ALL split modules of a game into <out_dir>/<GAMEID>.rpo.
    Returns (out_path, info)."""
    os.makedirs(out_dir, exist_ok=True)
    gdir = game_dir_for(data_dir, game_id)
    if gdir is None:
        raise FileNotFoundError(
            f"no split cache for {game_id} under {os.path.join(data_dir, 'cache')}")
    out = os.path.join(out_dir, f"{game_id}.rpo")
    info = opt_cache.export_game(Path(gdir), Path(out), level)
    return out, info


def do_import_rpo(rpo_path, data_dir, ppu_name=None):
    """Unpack a .rpo into <data_dir>/cache/<gameId>/, matching each module to a
    local ppu-* dir by name. data_dir is the dir that (or will) hold cache/.
    Returns the unpack report."""
    rpo_game_id(rpo_path)  # validate + surface a clear error on bad files
    return opt_cache.unpack_game(Path(rpo_path), Path(data_dir), ppu_name)


def select_game(games, game=None):
    """Best game to export: the one with the most total entries (ties by name)."""
    cands = {g: ts for g, ts in games.items()
             if sum(t["entries"] for t in ts) > 0
             and (game is None or g.lower() == game.lower())}
    if not cands:
        return None
    return max(cands, key=lambda g: (sum(t["entries"] for t in cands[g]), g))


def _resolve_data(data_arg):
    """(data_dir, trees) from --data or auto-discovery. Exits 2 when unusable."""
    if data_arg:
        d = os.path.abspath(data_arg)
        return d, find_shader_caches(d)
    data, trees = discover()
    if data is None:
        if sys.stdin.isatty():
            print("No RPCS3 split shader cache found automatically.")
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


# --- game title (offline, from the local PARAM.SFO) ---
PSF_MAGIC = 0x46535000  # "\0PSF" little-endian


def _sfo_entry(data, key):
    if len(data) < 20:
        return None
    magic, _ver, off_keys, off_data, count = struct.unpack_from("<IIIII", data, 0)
    if magic != PSF_MAGIC or off_keys > off_data or off_data > len(data):
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


def _sfo_title(data):
    for k in ("TITLE", "NPTITLE", "TITLE_08", "TITLE_16"):
        v = _sfo_entry(data, k)
        if v and v.strip():
            return v.strip()
    return None


def _ppu_label(ppu_dir):
    """'ppu-<hash>-GOWA.SELF' -> 'GOWA.SELF' (falls back to the full name)."""
    name = os.path.basename(ppu_dir)
    parts = name.split("-", 2)
    return parts[2] if len(parts) == 3 and parts[0] == "ppu" else name


def _library_base_dirs(data_dir):
    dirs = []

    def add(p):
        if p:
            p = os.path.abspath(p)
            if p not in dirs:
                dirs.append(p)

    if data_dir:
        add(data_dir)
    for c in candidate_data_dirs(os.path.expanduser("~"), dict(os.environ)):
        add(c)
    return dirs


def _game_library_roots(data_dir):
    roots = []

    def add_root(r):
        if r and r not in roots:
            roots.append(r)

    sub_by_key = {
        "dev_hdd0": ("game", "PS3_GAME"),
        "dev_hdd1": ("game", "PS3_GAME"),
        "dev_bdvd": ("PS3_GAME",),
        "games_dir": (None,),
    }
    for base in _library_base_dirs(data_dir):
        cfg = os.path.join(base, "config.yml")
        if os.path.isfile(cfg):
            try:
                import re
                with open(cfg, errors="replace") as f:
                    txt = f.read()
                for key in ("dev_hdd0", "dev_hdd1", "dev_bdvd", "games_dir"):
                    m = re.search(rf"{key}\s*:\s*(\S+)", txt)
                    if m:
                        vbase = m.group(1).strip("'\"")
                        if not vbase:
                            continue
                        for sub in sub_by_key[key]:
                            add_root(os.path.join(vbase, sub) if sub else vbase)
            except OSError:
                pass
        for key in ("dev_hdd0", "dev_bdvd", "games_dir"):
            d = os.path.join(base, "games") if key == "games_dir" else os.path.join(base, key)
            if os.path.isdir(d):
                for sub in sub_by_key[key]:
                    add_root(os.path.join(d, sub) if sub else d)
    if os.name == "nt":
        roots.append(os.path.join("C:", "Games", "PS3"))
    else:
        roots.append(os.path.join(os.path.expanduser("~"), "Games", "PS3"))
    return roots


_TITLE_CACHE = {}


def game_title(game_id, data_dir):
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
                    t = _sfo_title(f.read())
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
    t = game_title(game_id, data_dir) if data_dir else None
    return f"{t} ({game_id})" if t else game_id


def _games_view(games, data_dir=None):
    lines = []
    for g in sorted(games):
        mods = games[g]
        detail = ", ".join(f"{_ppu_label(t['ppu_dir'])} {t['entries']}" for t in mods)
        lines.append(f"  game {_game_head(g, data_dir)}  ({len(mods)} module(s): {detail or 'nothing playable yet'})")
    return lines


def interactive():
    data, trees = discover()
    if data is None:
        print("No RPCS3 split shader cache found automatically.")
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
        print("  (no split shader caches yet — play a game in the fork first)")
    while True:
        print()
        print("  [1] add a cache to RPCS3 (import a .rpo file)")
        print("  [2] export a game (all its split modules) to .rpo")
        print("  [0] quit")
        try:
            c = input("> ").strip()
        except EOFError:
            return 1
        if c == "1":
            rpos = list_rpo_files(PROGRAM_DIR)
            if not rpos:
                print(f"no .rpo files in {PROGRAM_DIR}")
                continue
            for i, f in enumerate(rpos, 1):
                try:
                    info = rpo_info(os.path.join(PROGRAM_DIR, f))
                    mods = ", ".join(f"{m['name'].split('-', 2)[-1]} {m.get('pipeline_count','?')}"
                                     for m in info["modules"])
                    print(f"  [{i}] {f}  ({info.get('gameId')}, {len(info['modules'])} module(s): {mods})")
                except Exception:
                    print(f"  [{i}] {f}")
            i = _pick("file: ", len(rpos))
            f = rpos[i]
            try:
                res = do_import_rpo(os.path.join(PROGRAM_DIR, f), data)
                print(f"imported {res['gameId']} — {res['files']} files into {res['base']}")
                for m in res["modules"]:
                    print(f"  {m['name']}  ({m['files']} files)")
            except (ValueError, FileNotFoundError) as e:
                print(f"ERROR: {e}")
            return 0
        if c == "2":
            if not trees:
                print("no split shader caches to export")
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
                out, info = do_export_game(data, g, PROGRAM_DIR)
                print(f"exported {info['gameId']} — {info['module_count']} module(s) -> {out}")
            except FileNotFoundError as e:
                print(f"ERROR: {e}")
            return 0
        if c == "0":
            return 0


def _diag(data_dir, games):
    roots = _game_library_roots(data_dir)
    print(f"data dir : {data_dir}")
    print("library roots searched:")
    for r in roots:
        print(f"  [{'ok' if os.path.isdir(r) else 'no'}] {r}")
    print()
    for g in sorted(games):
        key = g.upper()
        found = None
        for root in roots:
            for rel in (key, os.path.join(key, "BLURAY"), os.path.join(key, "PS3_GAME")):
                p = os.path.join(root, rel, "PARAM.SFO")
                if os.path.isfile(p):
                    try:
                        with open(p, "rb") as f:
                            t = _sfo_title(f.read())
                    except OSError:
                        t = None
                    if t and t.strip():
                        found = (p, t.strip())
                        break
            if found:
                break
        if found:
            print(f"{g}: TITLE '{found[1]}'  <- {found[0]}")
        else:
            print(f"{g}: NOT FOUND (no readable PARAM.SFO)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Interactive RPCS3 FORK shader-cache manager (split format).")
    sub = ap.add_subparsers(dest="cmd")
    p_d = sub.add_parser("discover", help="print the auto-discovered data dir and games")
    p_d.add_argument("--data", help="RPCS3 data dir (default: auto-discover)")
    p_l = sub.add_parser("list", help="list split cache games and .rpo files in the program folder")
    p_l.add_argument("--data", help="RPCS3 data dir (default: auto-discover)")
    p_in = sub.add_parser("import", help="add a .rpo file to the local RPCS3 cache")
    p_in.add_argument("file")
    p_in.add_argument("--data", help="RPCS3 data dir (default: auto-discover)")
    p_in.add_argument("--ppu", help="force a single ppu-* target name")
    p_ex = sub.add_parser("export", help="pack a whole game (all split modules) into <GAMEID>.rpo")
    p_ex.add_argument("--data", help="RPCS3 data dir (default: auto-discover)")
    p_ex.add_argument("--game", help="game id, e.g. BCES01741 (default: game with most entries)")
    p_ex.add_argument("--out", default=PROGRAM_DIR, help="output dir (default: the program folder)")
    p_ex.add_argument("--level", type=int, default=9, help="zstd level (1-22)")
    args = ap.parse_args(argv)

    if args.cmd is None:
        return interactive()

    if args.cmd == "discover":
        data, trees = _resolve_data(args.data)
        print(f"data dir : {data}")
        for line in _games_view(group_games(trees), data):
            print(line)
        return 0

    if args.cmd == "list":
        data, trees = _resolve_data(args.data)
        print(f"data dir : {data}")
        for line in _games_view(group_games(trees), data):
            print(line)
        print(f"program  : {PROGRAM_DIR}")
        for f in list_rpo_files(PROGRAM_DIR):
            print(f"  .rpo {f}")
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
            res = do_import_rpo(f, data, args.ppu)
        except (ValueError, FileNotFoundError) as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 2
        print(f"imported {res['gameId']} — {res['files']} files into {res['base']}")
        return 0

    # export
    data, trees = _resolve_data(args.data)
    games = group_games(trees)
    g = select_game(games, args.game)
    if g is None:
        sys.stderr.write("ERROR: no game with cached split entries found.\n")
        return 2
    out, info = do_export_game(data, g, args.out, args.level)
    print(f"exported    : {out}  ({info['module_count']} module(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
