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
- Same **cache-format version** (`v1.95`) — several RPCS3 builds share it, and
  that's all that matters; a build using a different version dir rejects the
  entries and falls back to the normal stutter-then-cache flow (nothing breaks).
- Any GPU works; the one-time launch pass is just longer on a GPU/driver that has
  never compiled these shaders before.

Everything is Python 3 + stdlib only (`zlib`, `struct`). No `pip`.

## The 3 main operations — `opt_cache.py`

| command | what it does |
|---|---|
| `pack`   | game cache (raw + pipelines) → **one file** `*.rsca` |
| `unpack` | `*.rsca` → a `shaders_cache/` dir (raw + pipelines), ready to be read by RPCS3 |
| `merge`  | `*.rsca` → adds only the missing files to an existing cache (union, idempotent) |

### Quick start

```bash
# 1) PACK  (on the machine where the cache was built)
#    -> writes BCES01741.rsca in the current dir (game id + name auto from the path)
python3 opt_cache.py pack ~/.cache/rpcs3/cache/BCES01741/ppu-*/shaders_cache

# 2) COPY the file to the target machine (scp / USB / cloud)

# 3a) UNPACK  (clean machine, cache from scratch)
python3 opt_cache.py unpack BCES01741.rsca --target ~/target/dir

#    or 3b) MERGE  (you already have a partial cache — adds the missing pieces)
python3 opt_cache.py merge  BCES01741.rsca --target ~/target/dir
```

`--target` is the directory under which `shaders_cache/` is created — usually
`~/.cache/rpcs3/cache/<GAMEID>/ppu-<hash>-<SELF>` (see "Layout" below).

### Useful flags
- `pack -o OUT` — output file (default: `<game-id>.rsca` in the current dir).
- `pack --game-id BCES01741` — override the game id in the manifest (auto from the
  path by default; only needed if the path isn't the standard layout).
- `pack --mode normalized` — normalizes `vp_base_address/entry` (smaller file,
  same decompiler semantics). Default is `byte-exact` (exact copy).
- `merge --overwrite` — overwrite existing files (default: skip them).

## Interactive: `cache_tool.py` (auto-discovery + import/export)
Finds the RPCS3 data dir automatically (Windows / Linux / macOS — env overrides like
`RPCS3_CONFIG_DIR`/`XDG_CACHE_HOME` first, then a bounded search of `$HOME`), then guides
you: operation → game. Import lists `*.rsca` next to the scripts and merges the pick into
the local cache; export packs the pick into `<GAMEID>.rsca` next to the scripts.

```bash
python3 cache_tool.py                          # interactive menu
python3 cache_tool.py discover                 # show the discovered data dir + games
python3 cache_tool.py list                     # games in the cache + .rsca files in the program folder
python3 cache_tool.py import  BCES01741.rsca   # add a .rsca to the local cache (idempotent merge)
python3 cache_tool.py export --game BCES01741  # pack the biggest cache dir of the game
```

Flags: `import --data DIR --ppu DIR`, `export --data DIR --ppu DIR --out DIR` (out
defaults to the program folder). Import merges into the game's first `ppu-*` dir — pick
another with `--ppu`. `python3 test_cache_tool.py -v` runs the suite.

## The `*.rsca` file format (RSCA v1, `format=1`)

```
MAGIC "RSCA" | format (u32)
manifest : game_id, backend (vulkan), cache_version (v1.95)
raw      : 1107 RSX shaders (32-bit bytecode), whole pool = 1 zlib blob (4.2x)
state    : deduplicated vp/fp/props tables + cross-refs (−64%)
```

- **Lossless** — `unpack`/`merge` reproduce the exact same bytes.
- Both halves are GPU-neutral: `raw/` = shader source (RSX ucode);
  `pipelines/<backend>/<version>/` = the state each shader pair needs (blend,
  depth, MSAA, renderpass). No compiled GPU code anywhere in the file.
- What's versioned is the format, not the GPU: entry size (672 B) + the
  `<version>` dir name. RPCS3 only reads its own dir (`v1.95`); a different
  version = entries rejected, cache rebuilt the normal way.

### Verification (GoW: Ascension, 1044 entries + 1107 raw)
| | value |
|---|---|
| original cache | 2.25 MB |
| packed `.rsca` | **0.64 MB** (3.5x) |
| state_hash | 1044/1044 OK |
| round-trip pack→unpack | **1044/1044 + 1107/1107, byte-for-byte identical** |
| merge (empty / full / partial) | adds only the missing ones, idempotent, 100% restore |

## Layout (real, RPCS3 v0.0.43)
```
~/.cache/rpcs3/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
├── raw/<HASH>.vp          ← raw ucode (GPU-INDEPENDENT)
├── raw/<HASH>.fp
└── pipelines/vulkan/v1.95/<VP>+<FP>+<S>+<H>.bin   ← entry = 672 B
```
Entry size (672 B) + the `v1.95` dir are part of the format — the emulator rejects
entries with the wrong size (rsx_cache.h).

## Compatibility method (older) — `export_cache.py` / `import_cache.py`
A raw `tar.gz` of the cache tree (files as-is, with a `manifest.json`; no RSCA v1
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
- **Same cache-format version** (`v1.95`) on both sides — a build using a
  different version dir rejects the entries and rebuilds the cache the normal way
  (nothing breaks).
- The cache is per-game, per-`ppu-*`. `pack`/`unpack` operate on one `shaders_cache`.
- A `.rsca` file is game data — don't import someone else's archive without checking.
