# RPCS3 FORK shader-cache toolkit (split format)

Portable exporter / importer for the **RPCS3 fork's SPLIT shader cache**. This is
an exporter *for the fork*, so it speaks the fork's native layout only — the stock
RPCS3 "legacy" (per-entry `.bin`) format and the old `.rsca`/`.tar.gz` tools are
gone.

What it does: pack a whole game's split cache (every non-empty module) into **one
self-contained, portable `.rpo` file**, and unpack it back into a fork-ready
directory tree. No RPCS3 install, no version or driver matching, byte-exact,
idempotent.

The file carries the GPU-neutral halves of the cache:
- **`raw/`** — RSX ucode, the shader *source* (GPU-independent).
- **`pipelines.bin`** (RSPL) — the exact pipeline state each shader pair needs.
- **`device/<vendor>_<device>/props.bin`** (RSPT) — the per-GPU props.

It does **not** carry compiled GPU code (SPIR-V / `VkPipeline`) — that lives in the
GPU driver's own on-disk cache and is machine-specific. The recipient's machine
compiles once at launch ("Preloading cached shaders"); the file's job is to say
*what* to compile and do it **before** gameplay, not during. That's the win: the
one-time launch pass happens up front instead of as stutter in the first playthrough.

Python 3 + stdlib only. `zstandard` is optional — when present the `.rpo` is
zstd-compressed (smaller), when absent it falls back to an uncompressed store
(functionality is identical).

## Layout (the fork's split cache, read verbatim)

```
<data>/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
├── raw/<HASH>.vp          ← shader source (GPU-INDEPENDENT)
├── raw/<HASH>.fp
└── pipelines/vulkan/<version>/
    ├── pipelines.bin      ← RSPL manifest (16 B header + N×320 B)
    └── device/<vendor>_<device>/props.bin   ← RSPT (16 B header + N×480 B)
```

- **RSPL** header: `magic=0x4C505352, ver, count, rec=320`. A record is
  `224 B pipeline_core + 96 B live_state`.
- **RSPT** header: `magic=0x54505352, ver, count, rec=480`. A record is
  `32 B keys + 448 B props`.
- `<version>` (e.g. `v1.96`) is part of the path and is preserved exactly — the
  fork only reads its own version dir.
- A game dir holds **one split cache per PPU module** (`EBOOT.BIN`, `GOWA.SELF`,
  `coo.ppu.self`, …). A collection title (e.g. BCES01277) has one module per
  playable game — so a useful export is **all non-empty modules of the game, in
  one file**.
- Empty modules (no `pipelines.bin` / `device/`) and legacy (per-entry `.bin`)
  dirs are **ignored** — this is a split-fork exporter.

## The 3 main operations — `opt_cache.py`

| command  | what it does |
|---|---|
| `export` | game dir (or a `ppu-*` dir) → **one** `<out>.rpo` bundling all non-empty split modules |
| `unpack` | `.rpo` → fork-ready tree (one `ppu-*/shaders_cache` per module) |
| `list`   | show a `.rpo`'s modules, device keys, counts |
| `verify` | validate structure + SHA-256 of every entry |

### Quick start

```bash
# 1) EXPORT  (on the machine where the cache was built)
python3 opt_cache.py export ~/.cache/rpcs3/cache/BCES01741 BCES01741.rpo

# 2) COPY the .rpo to the target machine (scp / USB / cloud)

# 3) UNPACK into the target's data dir (auto-matches each module to a ppu-* dir)
python3 opt_cache.py unpack BCES01741.rpo ~/.cache/rpcs3

# 4) CHECK
python3 opt_cache.py verify BCES01741.rpo
python3 opt_cache.py list   BCES01741.rpo
```

Flags:
- `export --root DIR` — data dir, when `source` is a bare game id.
- `export --level N` — zstd level 1–22 (default 9).
- `unpack --ppu NAME` — force a single `ppu-*` target name.

## Interactive: `cache_tool.py` (auto-discovery + import/export)

The recommended entry point. Finds the RPCS3 data dir automatically (Windows /
Linux / macOS — env overrides like `RPCS3_CONFIG_DIR` / `XDG_CACHE_HOME` first,
then a bounded search of `$HOME`), then guides you: operation → **game** (modules
are handled automatically).

On macOS the cache lives in `~/Library/Caches/rpcs3` but installed games
(`PARAM.SFO`, used for titles) live in `~/Library/Application Support/rpcs3` —
both are auto-detected.

- **Import** lists `*.rpo` files next to the scripts (with their module summary)
  and unpacks the pick into the local cache.
- **Export** picks the game (default: the one with the most entries) and packs ALL
  its non-empty split modules into `<GAMEID>.rpo` next to the scripts.

```bash
python3 cache_tool.py                          # interactive menu
python3 cache_tool.py discover [--data DIR]    # show the discovered data dir + games
python3 cache_tool.py list     [--data DIR]    # games in the cache + .rpo files in the program folder
python3 cache_tool.py import  BCES01741.rpo    # add a .rpo to the local cache
python3 cache_tool.py export --game BCES01277  # pack one specific game (all its modules)
```

Games are shown by **title + code** (e.g. `God of War (BCES01741)`) and by module
label (`GOWA.SELF`, `EBOOT.BIN`), never by the raw `ppu-<hash>-` dir name. The title
comes from the **local** game's `PARAM.SFO` (`TITLE` key, PSF magic `\0PSF`) — no
network — so the game dir must exist under a library path: the `VFS` section of
`config.yml` (`dev_hdd0`, `dev_bdvd`, `games_dir`), the defaults under the data dir,
or the platform default (`C:\Games\PS3` on Windows, `~/Games\PS3` elsewhere). If it
isn't found, the line falls back to the bare code.

Flags: `import --data DIR --ppu NAME`, `export --data DIR --out DIR --level N`
(out defaults to the program folder).

## The `.rpo` file format

A self-contained container (magic `RSPO`), optional zstd per entry:

```
0    4   magic "RSPO"
4    4   format_version = 1
8    4   entry_count  (1 manifest + all module files)
12   ...  entries, each:
        name bytes + \x00
        flag  (0 = stored, 1 = zstd)
        orig_len  (u64)
        payload_len (u64)
        payload
```

Entry names are namespaced per module (`<ppu-dir>/...`), preserving the full
`shaders_cache`-relative path:

```
manifest.json
<ppu>/pipelines/vulkan/<version>/pipelines.bin
<ppu>/pipelines/vulkan/<version>/device/<KEY>/props.bin
<ppu>/raw/<HASH>.vp
<ppu>/raw/<HASH>.fp
```

`manifest.json`:
```json
{
  "format": "rpo-split", "format_version": 1,
  "gameId": "BCES01741", "module_count": 2,
  "compression": "zstd" | "stored", "zstd_level": 9,
  "modules": [
    { "name": "ppu-AAA-GOWA.SELF",
      "raw_count": 100, "device_count": 50, "pipeline_count": 50,
      "devices": [ {"key": "1002_67B0", "count": 50, "size": 24016} ],
      "sha256": { "pipelines/vulkan/v1.96/pipelines.bin": "<hex>",
                  "pipelines/vulkan/v1.96/device/1002_67B0/props.bin": "<hex>",
                  "raw/...": "<hex>" } }
  ]
}
```

Properties:
- **Lossless** — `unpack` reproduces the exact same bytes (byte-exact).
- **Multi-module** — one `.rpo` per game, every non-empty module namespaced by its
  `ppu-*` dir name; import matches each module to the local dir by name.
- **Integrity** — SHA-256 of every entry is recorded; `verify` re-checks all of it.
- **Portable** — no paths, hostnames, or timestamps embedded; the file is meant to
  be shared.

## Double-click launch (no terminal to type in)
- **macOS** — double-click `cache_tool.command`: opens a Terminal in the folder and
  starts the interactive menu.
- **Windows** — double-click `cache_tool.bat`: console with the interactive menu;
  `pause` keeps the window open after exit.
- **Linux** — `./cache_tool.command` in a terminal, or right-click → "Run in Terminal".

All three are thin wrappers around `python3 cache_tool.py` from the folder they sit
in; the menu itself needs a terminal (it is an `input()` menu), so a GUI shell
around Python (e.g. Pythonw) would break it.

## Tests

`python3 -m unittest test_export -v` — 27 tests: discovery (split-only, empty +
legacy modules ignored), export (manifest, multi-module, multi-device, SHA-256),
import (fresh + overwrite, byte-exact round-trip, stable re-export), CLI
(discover/list/export/import, error paths), interactive export/import flows, and
PSF/SFO title-lookup (synthetic + a real-BCES01741 PARAM.SFO regression, macOS
two-dir layout).

## Limitations
- The recipient's machine always pays one launch pass ("Preloading cached shaders")
  — the file carries source + state, not compiled GPU code. The win: it happens
  once, up front, instead of as stutter during the first playthrough.
- **Same cache-format version** dir on both sides (e.g. `v1.96`) — a fork build
  using a different version dir rejects the entries and rebuilds the cache the
  normal way (nothing breaks).
- Split-fork only: stock-RPCS3 (legacy per-entry `.bin`) caches are not exported.
- The cache is per-game, per-`ppu-*`. `export`/`unpack` operate on one game dir
  (or all non-empty modules of it).
- A `.rpo` is game data — don't import someone else's archive without checking.
