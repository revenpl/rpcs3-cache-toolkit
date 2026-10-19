# RPCS3 shader-cache toolkit

RPCS3 shader caches (Vulkan) — pack a game's cache into a single portable file and
send it to someone who has **never played the game**: their first launch does all
the shader work up front (one "Preloading cached shaders" pass with a progress bar),
and their first playthrough runs **without shader stutter**. Without the file, that
same work happens lazily mid-gameplay — that's the stutter.

What the file carries — and what it doesn't (verified against the RPCS3 v0.0.43
source, `rsx_cache.h`, `ProgramStateCache.h`, `VKPipelineCompiler.h`):

- **In the file**: `raw/` = RSX ucode, the PS3 GPU shader *source* (GPU-neutral);
  `pipelines/` = the exact pipeline state each shader pair needs (blend, depth,
  MSAA, renderpass…).
- **Not in the file**: the compiled result — GLSL/SPIR-V, `VkShaderModule`,
  `VkPipeline`. That lives in the GPU driver's own on-disk cache
  (`~/.cache/mesa_shader_cache/`) and is machine-specific.

So the recipient's machine always compiles once, at launch — the file's job is to
say *what* to compile (the full set the game actually hits) and do it **before
gameplay**, not during.

Requirements for the recipient:
- Same game (caches are per-game, per-`ppu-*` dir), e.g. BCES01741.
- Same **cache-format version** dir (e.g. `v1.95`) — several RPCS3 builds share a
  version dir, and that's all that matters; a build using a different version dir
  rejects the entries and falls back to the normal stutter-then-cache flow
  (nothing breaks).
- Any GPU works; the one-time launch pass is just longer on a GPU/driver that has
  never compiled these shaders before.

Everything is Python 3 + stdlib only (`zlib`, `struct`). No `pip`.

## Layout (real, RPCS3 v0.0.43)

```
<data>/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
├── raw/<HASH>.vp          ← raw ucode (GPU-INDEPENDENT)
├── raw/<HASH>.fp
└── pipelines/vulkan/v1.95/<VP>+<FP>+<S>+<H>.bin   ← entry = 672 B
```

A game dir holds **one `shaders_cache` per PPU module** (`EBOOT.BIN`, `GOWA.SELF`,
`coo.ppu.self`, …). A collection title (e.g. BCES01277 = God of War Collection 2)
has one module per playable game — so a useful export is **all non-empty modules
of the game, in one file**. Entry size (672 B) + the `v1.95` dir name are part of
the format — the emulator rejects entries with the wrong size (rsx_cache.h).

## The 3 main operations — `opt_cache.py`

| command | what it does |
|---|---|
| `pack`   | game dir (all non-empty `ppu-*` modules) → **one file** `<GAMEID>.rsca` (RSCA v2) |
| `unpack` | `*.rsca` → game dir with one `ppu-*/shaders_cache` per module |
| `merge`  | `*.rsca` → adds only the missing files to an existing cache (union, idempotent) |

### Quick start

```bash
# 1) PACK  (on the machine where the cache was built)
python3 opt_cache.py pack ~/.cache/rpcs3/cache/BCES01741

# 2) COPY the file to the target machine (scp / USB / cloud)

# 3) MERGE — the normal path: adds the missing pieces to the existing cache
#    (v2 files: each module is matched to its local ppu-* dir by module name)
python3 opt_cache.py merge BCES01741.rsca --target ~/.cache/rpcs3/cache/BCES01741
```

`--target` is the **game dir** (`cache/<GAMEID>`) or a single `ppu-*` dir.
`unpack` writes one subdir per module under `--target` (v2) — use it only when
building a cache from scratch.

### Useful flags
- `pack -o OUT` — output file (default: `<game-id>.rsca` in the current dir).
- `pack --mode normalized` — normalizes `vp_base_address/entry` (smaller file,
  same decompiler semantics). Default is `byte-exact` (exact copy).
- `merge --overwrite` — overwrite existing files (default: skip them).

## Interactive: `cache_tool.py` (auto-discovery + import/export)

The recommended entry point. Finds the RPCS3 data dir automatically (Windows /
Linux / macOS — env overrides like `RPCS3_CONFIG_DIR`/`XDG_CACHE_HOME` first, then
a bounded search of `$HOME`), then guides you: operation → **game** (modules are
handled automatically).

- **Import** lists `*.rsca` files next to the scripts (with their module summary)
  and merges the pick into the local cache.
- **Export** picks the game (default: the one with the most entries) and packs ALL
  its non-empty modules into `<GAMEID>.rsca` next to the scripts.

```bash
python3 cache_tool.py                          # interactive menu
python3 cache_tool.py discover                 # show the discovered data dir + games
python3 cache_tool.py list                     # games in the cache + .rsca files in the program folder
python3 cache_tool.py import  BCES01741.rsca   # add a .rsca to the local cache (idempotent merge)
python3 cache_tool.py export --game BCES01277  # pack one specific game (all its modules)
```

Flags: `import --data DIR --ppu DIR`, `export --data DIR --ppu DIR --out DIR`
(out defaults to the program folder). Module-matching rules on import:

- **RSCA v2 files** (current): each module is matched to the local `ppu-*` dir by
  exact dir name first, then by module label (`GOWA.SELF`, `EBOOT.BIN`, …). If a
  module has no matching dir (e.g. a game from the collection you never launched),
  the import reports it as NOT IMPORTED and tells you to launch it once.
- **RSCA v1 files** (legacy, one module, no module name recorded): with one local
  `ppu-*` dir it goes there; with several, the dir whose entry count is closest to
  the file's is chosen (a cache only grows); a tie → the tool asks for `--ppu DIR`.
  Never guesses silently.

`python3 -m unittest test_export -v` runs the suite (21 tests, incl. a real-file
round-trip when the user's `BCES01741.rsca` is present).

### Double-click launch (no terminal to type in)
- **macOS** — double-click `cache_tool.command`: it opens a Terminal in the folder and
  starts the interactive menu. (Want a Dock-style app instead? `osacompile -o "Cache
  Tool.app"` on a 4-line AppleScript that runs `do script "cd <folder> && python3
  cache_tool.py"` in Terminal — note: the path gets baked in, so the .app is not portable.)
- **Windows** — double-click `cache_tool.bat`: console with the interactive menu; `pause`
  keeps the window open after exit.
- **Linux** — `./cache_tool.command` in a terminal, or right-click → "Run in Terminal".

All three are just thin wrappers around `python3 cache_tool.py` from the folder they sit
in; the menu itself needs a terminal (it is an `input()` menu), so a GUI shell around
Python (e.g. Pythonw) would break it. On a fresh Mac without `python3`, run
`xcode-select --install` first.

## The `*.rsca` file format

### RSCA v2 (current, `format=2`) — one file per game

```
0    4   magic "RSCA"
4    4   format = 2
8   16   game_id
24   4   module_count M
per module:
       4   name_len, then name bytes   (ppu dir name, e.g. "ppu-AAA-GOWA.SELF")
      16   backend            (e.g. "vulkan")
      16   cache_version      (e.g. "v1.95" — per module, modules may differ)
       ... BODY
```

### RSCA v1 (legacy, `format=1`) — one file per module, import-only

```
0    4   magic "RSCA"
4    4   format = 1
8   16   game_id
24  16   backend
40  16   cache_version
56   ... BODY
```

### BODY (identical in v1 and v2)

```
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
```

Layout of one 672-byte pipeline_data entry (from rsx_cache.h, verified):

```
0    8  vertex_program_hash
8    8  fragment_program_hash
16   8  pipeline_storage_hash
24  168  vp scalar (ctrl, masks, jump table, ...)
192  32  fp scalar (ctrl, texcoord, lighting, mrt, ...)
224 448  pipeline_properties (Vulkan descriptors — parameters, not compiled code)
672    (end)
```

`state_hash` (4th hash in the entry filename) = FNV-1a 64 over 14 u32 params,
NOT including base_address/entry → invariant under normalization; used as a
per-entry integrity check on pack (`state_hash N OK / M BAD`).

- **Lossless** — `unpack`/`merge` reproduce the exact same bytes (byte-exact mode).
- Both halves are GPU-neutral: `raw/` = shader source (RSX ucode);
  `pipelines/<backend>/<version>/` = the state each shader pair needs. No compiled
  GPU code anywhere in the file.
- What's versioned is the format, not the GPU: entry size (672 B) + the
  `<version>` dir name. RPCS3 only reads its own dir; a different version =
  entries rejected, cache rebuilt the normal way.

### Verification

Synthetic suite (two games, 6 ppu dirs incl. empties, v1 + v2, import/export/CLI/
interactive — 21 tests, `test_export.py`):

| check | result |
|---|---|
| v2 pack: empty modules skipped, both games | ok (empty dirs reported, never packed) |
| v2 import: all modules matched by label, union semantics | ok (counts exact) |
| v1 legacy import into a 2-module game | ok (closest-entry-count disambiguation; tie → asks `--ppu`) |
| interactive import + export flows | ok |
| CLI import/export (default game, by game, error paths) | ok |

Real file (user's `BCES01741.rsca`, RSCA v1, 622 entries, 739 raw, `vulkan/v1.94`):

| check | result |
|---|---|
| state_hash | **622/622 OK** |
| v1 → unpack → repack (v2) → unpack | **622/622 + 739/739, byte-for-byte identical** |
| file size | 456,401 B → 456,427 B (v2 header overhead) |

## Compatibility method (older) — `export_cache.py` / `import_cache.py`
A raw `tar.gz` of the cache tree (files as-is, with a `manifest.json`; no RSCA
dedup), **one archive per game** (`<GAMEID>.tar.gz`) — "export all" produces one
file per game, so a whole library stays a tidy set of small archives. Kept for
compatibility; use `opt_cache.py` for new work. `--debug` adds source hostname +
data dir to the manifest (off by default — the archive is meant to be shared).
```bash
python3 export_cache.py -o outdir/                       # one <GAMEID>.tar.gz per game
python3 import_cache.py BCES01741.tar.gz --root ~/target # install (merge)
```

## Limitations
- The recipient's machine always pays one launch pass ("Preloading cached shaders")
  — the file carries source + state, not compiled GPU code. The win: it happens
  once, up front, instead of as stutter during the first playthrough.
- **Same cache-format version** dir on both sides (e.g. `v1.95`) — a build using a
  different version dir rejects the entries and rebuilds the cache the normal way
  (nothing breaks).
- The cache is per-game, per-`ppu-*`. `pack`/`unpack` operate on one `shaders_cache`
  (or all non-empty ones of a game dir).
- A `.rsca` file is game data — don't import someone else's archive without checking.
