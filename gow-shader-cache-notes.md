# GoW Ascension (BCES01741) — shader cache & PSARC work notes

> Working reference so details survive session compaction. Everything marked **verified**
> was confirmed against real files/source on 2026-10-17/18. Unverified items are flagged.
> **Moved 2026-10-18 from `cache/scratch/` to the toolkit dir — scratch is pruned after
> 24 h idle, which defeats the point of this file.**

## 0. TL;DR — the four workstreams
1. **PS3 shader-cache dedup** (RPCS3, gh-19810) — the core. Measured 685 KB →
   244 KB (−64%) byte-exact, → 165 KB (−76%) normalized, on a 1044-entry GoW cache.
2. **Cache sharing (distribution toolkit)** — pack a game's cache into one portable
   file (RSCA v1, 3.5× smaller) so someone who has **never played the game** gets a ready
   first playthrough: one launch pass instead of in-game stutter.
3. **PSARC extraction** — GoW ships shaders inside `.PSARC` archives; reverse-engineered
   the container format (header, block table, zlib/AES-CTR) to read entries.
4. **Anchor matching** — find which PSARC file holds each raw `.vp`/`.fp` shader binary
   (`psarc_oracle.py` → `anchor_oracle.json`).

---

## 1. RPCS3 shader cache format  (verified — rsx_cache.h + analyze_cache2.py + opt_cache.py)

### Location (real layout, v0.0.43)
```
~/.cache/rpcs3/cache/<GAMEID>/ppu-<hash>-<SELF>/shaders_cache/
├── raw/<HASH>.vp, raw/<HASH>.fp      ← RSX ucode (shader SOURCE, GPU-independent)
└── pipelines/<api>/<version>/*.bin   ← e.g. pipelines/vulkan/v1.95
```
One `.bin` file per pipeline, **exactly 672 bytes**.

### The 672-byte entry layout (exact byte offsets)
```
  0   8   vertex_program_hash
  8   8   fragment_program_hash
 16   8   pipeline_storage_hash
 24   4   vp_ctrl0
 28   4   vp_ctrl1
 32   4   vp_texture_dimensions
 36   4   vp_reserved_0
 40  72   vp_instruction_mask[9]
112   4   vp_base_address        <- normalizable
116   4   vp_entry               <- normalizable (keep entry-base_address)
120  64   vp_jump_table[32]
184   2   vp_multisampled_textures
186   2   vp_reserved_1
188   4   vp_reserved_2
192   4   fp_ctrl
196   4   fp_texture_dimensions
200   4   fp_texcoord_control
204   2   fp_height
206   2   fp_pixel_layout
208   2   fp_lighting_flags
210   2   fp_shadow_textures
212   2   fp_redirected_textures
214   2   fp_multisampled_textures
216   1   fp_mrt_count
217   1   fp_reserved0
218   2   fp_reserved1
220   4   fp_reserved2
224 448   pipeline_properties     <- the block that mostly varies between pipelines
672       (end)
```

### Three dedup-able blocks
| block              | range         | size  | dedup key              |
|--------------------|---------------|-------|------------------------|
| vp scalars         | entry[24:192] | 168 B | (vp_hash, scalar)      |
| fp scalars         | entry[192:224]|  32 B | (fp_hash, scalar)      |
| pipeline_properties| entry[224:672]| 448 B | (psh, block)  ← biggest |

The 24 B of hashes are the identity (lookup key) — never dedup'd.
**Key insight:** many pipelines reuse the same vertex/fragment program; it's the
`pipeline_properties` block that differs. So properties dedup is the biggest win.

### Measured results (GoW, 1044 entries, 685 KB)
- Block dedup, **byte-exact** (original base_address/entry kept): **685 KB → 244 KB (−64%)**;
  unpack is byte-for-byte identical to the original cache.
- **+ normalize** `vp_base_address→0`, `vp_entry→(entry−base_address)`: **→ 165 KB (−76%)**.
- **DEFINITIVELY RE-VERIFIED 2026-10-18** (`finish_a.py`, real cache @
  `ppu-...GOWA.SELF/shaders_cache/pipelines/vulkan/v1.95`, 1044 × 672 B = 685.1 KB):
  - byte-exact round-trip: **1044/1044 byte-identical, name sets identical → PASS**
  - normalized round-trip: 248 files byte-identical (base already 0) + **796 differ ONLY in
    bytes 112..119** with correct semantics (base→0, entry→entry−base), **all 1044 original
    names preserved** → PASS (this proves state_hash is invariant under normalization)
  - `state_hash` check: 1044 OK / 0 BAD (FNV-1a impl validated against all real filenames)
  - unique table rows: vp 704 (byte-exact) → **247 (normalized)**; fp 905; props 169
  - sizes confirmed: 243.9 KB (−64.4%) / 165.4 KB (−75.9%)
  - cache filenames: uppercase **unpadded** hex `<h1>+<h2>+<h3>+<h4>.bin` (4th field is 14–16
    chars depending on leading zeros) — matches `_entry_name`'s `{:X}` naming exactly.
- **Scaling:** 47 % at 261 entries, 64 % at 1044 — ratio improves as the cache grows, because new
  entries mostly reuse existing blocks. A full playthrough should land even better.

### Is normalization safe? — verified against source
- `VertexProgramDecompiler.cpp`: the compiled path only ever uses `entry − base_address`, so
  storing just the offset is safe in the default recompiler.
- The **shader interpreter** reads both values absolute — but it's a fallback, active only when an
  interpreter shader mode is selected, **not** in the default recompiler.
  → Normalized variant is fine for a normal setup; **use the byte-exact variant if you run the
  shader interpreter.**
- **state_hash** = 4th hash in the filename = FNV-1a 64 over a fixed set of 14 u32 params, and
  does **NOT** include base_address/entry (checked against all 1044 filenames). So even the
  normalized variant keeps the **original filenames**.
- `base_address`/`entry` ARE required for recompilation (VertexProgramDecompiler) — both are
  kept in every variant; normalization only stores the offset.

### What the cache file contains — and what it doesn't (verified in v0.0.43 source, 2026-10-18)
- `raw/` = RSX ucode — the PS3 GPU shader **source** (GPU-independent).
- `pipelines/*.bin` = per-shader **state**: RSX registers + `vk::pipeline_props` (a Vulkan
  pipeline *description*: input assembly, rasterization, depth/stencil, multisample, color
  blend, renderpass_key). Also GPU-independent.
- **NOT in the file** (this is the GPU-specific part): the compiled result — GLSL/SPIR-V,
  `VkShaderModule`, `VkPipeline`. That lives only in the GPU driver's own on-disk cache:
  `~/.cache/mesa_shader_cache/` (**98 MB / 11,968 files** on this machine, RADV ICD).
  → The "cache" is a recipe + state list, not cooked dishes; every machine cooks once.

### When raw/ is created, and why it is essential (verified 2026-10-18, v0.0.43 source)
- **When:** NOT at launch — during the FIRST PLAYTHROUGH, the moment the game first uses a given
  shader. The only writer of `raw/` is `store()` (rsx_cache.h:290-342), called from exactly two
  render-path sites — `VKGSRender.cpp:610` / `GLGSRender.cpp:428` — both through the callback
  registered in the `vk::program_cache` constructor (VKProgramBuffer.h:95-97, "Program was linked
  or queued for linking"). raw/ is written once per unique shader (idempotent: only when the file
  is missing or its size differs, :310-318); the pipeline .bin is always rewritten (:341).
- **"RPCS3 compiles something-that-is-not-shaders on first launch" is wrong** — it compiles
  exactly the shaders: decompile (RSX→GLSL), compile (GLSL→SPIRV), driver link (vkCreatePipeline).
  That is the stutter. raw/ = the captured shader source (RSX ucode).
- **raw/ is essential, NOT optional:** the 672 B .bin is state only (ctrl registers +
  pipeline_props) and contains no shader code — its hashes are pointers INTO raw/. If raw/ is
  missing, `load_shaders` silently skips the entry at launch (rsx_cache.h:114-119: `unpack()` →
  empty program → `continue`) — a partially dead cache, stutter returns, and `store()` re-creates
  the file during gameplay.
- The "1.8 MB after 10 min" measurement is consistent: raw/ grows during the first playthrough as
  new shaders appear, then stabilizes.

### Launch flow (verified — rsx_cache.h, ProgramStateCache.h, VKPipelineCompiler.h)
`shaders_cache::load()` runs on **every** launch when the pipeline dir is non-empty:
1. The "Preloading cached shaders from disk" dialog is created **unconditionally**
   (rsx_cache.h:269) — even with a fully warm cache. Two progress steps: "Loading pipeline
   object X/Y" and "Compiling pipeline object X/Y".
2. Load step: per entry, read the `.bin` + raw ucode; `search_vertex/fragment_program`
   misses (fresh process) → `Decompile()` + `Compile()` → RSX→GLSL→SPIRV → VkShaderModule.
   **This happens every launch** (CPU-side, modest).
3. Compile step: `add_pipeline_entry` → `get_graphics_pipeline` → `build_pipeline` →
   `pipe_compiler::compile(pipeline_props, vs, fs)` → `vkCreatePipeline`. The **driver**
   compiles for that GPU — served from the driver's on-disk cache (mesa) if warm (why
   2nd+ launches are fast), full cost otherwise (why a different GPU loads longer).

**Value of the portable file** (the toolkit's whole point): for someone who has never
played the game, the recipient's machine does that one compilation pass **up front**
(progress bar at launch) instead of stutter spread across the first playthrough. After
the pass, the driver cache on their machine is warm → fast forever.

### Cross-GPU test (2026-10-17) — correct interpretation (revised 2026-10-18, verified in source)
- **9070 XT (1st launch):** shader-compilation notifications nearly constant (stutter).
- **7900 XTX (cache from 9070 XT installed):** game loaded **significantly longer**
  (one-time compilation pass on a cold driver cache); during 5 min of gameplay only
  **one** compilation notification.
- **Earlier conclusion "the cache is GPU-specific" is REFUTED.** The cache file is
  portable (source + state, both GPU-independent); the GPU-specific part (compiled code)
  was never in the file — it's the per-machine driver cache.
- (User's intuition was right: later launches are much faster because the cache does real work.)

### Compatibility requirement (corrected 2026-10-18)
- What matters is the **cache format version** (`v1.95`), NOT the RPCS3 binary build —
  several builds share the same format version.
- A build using a different version dir rejects the entries (rsx_cache.h:
  `f.size() != sizeof(pipeline_data)` → entry removed + error logged) and rebuilds the
  cache the normal way — nothing breaks.
- Version history: v1.92 → v1.94 → v1.95. v1.94→v1.95 is a real format change (new
  `fp_mrt_count` field + hash modification). Pickaxe search across 20,334 commits:
  commit `27c56cde2` (2025-01-09, "Track active MRT count per shader") is the only
  commit that set v1.95 in both the Vulkan and OpenGL backends.

### Dolphin's uber-shader — does it apply? (2026-10-17)
RPCS3 is already close to the idea (one on-disk cache of pipeline data), but the
uber-shader approach itself doesn't apply to PS3: the RSX binary-ISA program space is
too large for one uber-shader. Per-shader compilation is the necessary model.

### Distribution format — RSCA v1 (`pack`/`unpack`/`merge`), the primary path
- One portable file per game: `MAGIC "RSCA"`, `format=1`.
- Manifest: `game_id` (16 B) + `backend` (16 B) + `cache_version` (16 B) — only game
  identity + format-engine version (user decision: no date/author/binary-version metadata).
- Body: all raw shaders as **one** zlib blob (~4.2×), then deduped state tables
  (vptab/fptab/ptab) + 3×u32 entry indices.
- Dedup tables (the tabular format, formerly the standalone RPO1):
  - vp row   = 8 B vph  + 168 B scalar   (key=(vph, scalar))
  - fp row   = 8 B fph  +  32 B scalar   (key=(fph, scalar))
  - props row= 8 B psh  + 448 B block    (key=(psh, block))
  - entry    = 3 × u32 indices (vp, fp, props)
- Two modes: `byte-exact` (round-trip identical) and `normalized` (smaller, same decompiler semantics).
- `unpack`/`merge` reproduce the exact bytes (lossless); `merge` = union, idempotent
  (adds only missing files; `--overwrite` forces rewrite).
- `pack` defaults: `-o` → `<game-id>.rsca` in the current dir; `--game-id` auto-derived
  from the standard path (`.../cache/<GAMEID>/ppu-*/shaders_cache`), else `UNKNOWN`.
- **GoW numbers (2026-10-18):** original cache **2.25 MB** → **0.64 MB `.rsca` (3.5×)**;
  round-trip **1044/1044 entries + 1107/1107 raw, byte-for-byte**; merge into empty /
  full / partial cache → 100% restore, idempotent.
- **Renamed 2026-10-18:** standalone RPO1 (`convert`/`restore`) removed; RPO2 → RSCA v1
  (`.rsca`). CLI now: `pack` / `unpack` / `merge` / `verify`.

---

## 2. PSARC container format (GoW)  (verified — psarc_oracle.py, ran clean on all 6 archives)

Big-endian throughout. `d` = file bytes.

### Header
| offset | size | field        |
|--------|------|--------------|
| 0      | 4    | magic `PSAR` |
| 12     | 4    | toc_length   |
| 16     | 4    | entry_size   |
| 20     | 4    | toc_entries  |
| 24     | 4    | max_block    |
| 28     | 4    | flags        |

### Block size table
- width `bt` = smallest int with `256^bt >= max_block` (loop: `bt=1; i=256; while i<max_block: i*=256; bt+=1`).
- `table_off = 32 + toc_entries * entry_size`
- `n_blocks  = (toc_length − table_off) // bt`
- `blens[b]  = int.from_bytes(d[table_off + b*bt : +bt], "big")`

### TOC entries (start at offset 32, each `entry_size` bytes)
for entry `e`, `o = 32 + e*entry_size`:
- `o+16` → `bidx`  (u32) — first block index
- `o+20` → `usize` (5 B, big) — uncompressed size
- `o+25` → `foff`  (5 B, big) — offset into data area

**Entry 0 is the manifest** — a UTF-8 text file, one path per line (e.g. `/DC/PS3_BE/WAD_BOG8CTF.DCB`).

### Reading an entry's data (block walk)
```
while len(out) < usize:
    t   = blens[bidx]
    is64 = (t == 0)                 # *** table value 0 = a FULL 64K RAW block, NOT empty ***
    blen = 65536 if is64 else t
    chunk = d[foff : foff+blen]
    if is64:                          out += chunk
    elif blen == usize - len(out):    out += chunk          # raw tail
    else:                             out += decomp_block(chunk)   # compressed
    bidx += 1; foff += blen
return out[:usize]
```

### decomp_block
```
def decomp_block(block):
    if block[:1] == b"\x78":          return zlib.decompress(block)
    return zlib.decompress(aes_ctr_decrypt(block), -15)   # raw inflate

def aes_ctr_decrypt(block):
    KEY    = bytes.fromhex("7a7fce1f558e39a119412637923de935da7a4d41b7cc35286f56f87ce9dbc392")
    hdr    = (block[0] << 8) | block[1]
    prefix = b"\xde\xad\xbe\xef" + struct.pack(">I", len(block)) + struct.pack(">I", hdr)
    ctr    = Counter.new(32, prefix=prefix, initial_value=0)   # from Crypto.Util
    return AES.new(KEY, AES.MODE_CTR, counter=ctr).decrypt(block[2:])
```

### GoW Ascension BCES01741 — verified
- **6 PSARCs:** `TPPS.PSARC, TPPS1.PSARC, TPPS2.PSARC, TPPS3.PSARC, TPPS4.PSARC, TPPS5.PSARC`
  (under `dev_hdd0/game/BCES01741/USRDIR/`).
- **TPPS.PSARC:** 376 files + manifest = **377 TOC entries** (`tpps_manifest.txt` = 376 lines).
- Shader-relevant extensions kept by the oracle: `.DCB .WAD .PATCH .TS`.
- ⚠️ A number I earlier recalled ("1478 entries") is **NOT verified** — do not reuse it. The
  verified per-archive counts come from running the parser per PSARC.

---

## 3. Anchor matching (raw shader → PSARC file)
`psarc_oracle.py` → writes `/out/anchor_oracle.json`.
- Loads raw `.vp`/`.fp` binaries (from a `raw/` dir), sorted by size desc.
- For each candidate PSARC entry (`.DCB/.WAD/.PATCH/.TS`), decompresses, builds a word-set at
  4-byte stride (hits observed 4B-aligned, some 16B), then `data.find(raw)` for a full hit;
  falls back to a 16→128 B prefix probe.
- Output per PSARC: `scanned`, `bytes`, `entries_with_hits`, `skipped[]`, `covered` map,
  plus a `pref_dist` (how many raws matched at FULL vs N-B prefix) and `raws_missing`.
- Runs 6 workers (one per PSARC) via `multiprocessing` fork context.

---

## 4. Pitfalls & bug fixes (all hit this session)
1. **PSARC block table value 0 = full 64K RAW (uncompressed) block — NOT an empty chunk.**
   Earlier code crashed on the empty chunk or tried to decompress it (`zlib` "invalid distance").
   After the fix, previously-"broken" files decompress to the last byte (verified at 445 blocks
   = 9×64K and 352 blocks = 6×64K).
2. **Name collision:** `from Crypto.Util import Counter` shadows `collections.Counter` →
   `Counter()` raises `TypeError: 'module' object is not callable`. Fix: import as
   `from Crypto.Util import Counter as Ctr`.
3. **manifest_of offset bug:** entry 0 lives at offset **32** (after the 32-byte header); read
   its fields at `o+16 / o+20 / o+25`. Reading at raw offsets 16/20/25 lands inside the header
   → garbage bidx/usize/foff → OOB walk / `IndexError` in `aes_ctr_decrypt`.
4. **Search engines flag headless Chrome by fingerprint (NOT VPN — corrected diagnosis):**
   Google/DDG CAPTCHA, Mojeek 403, Bing irrelevant. Navigate directly to target pages, or ask the
   user for the URL.
5. **Don't claim "the cache contains compiled shaders"** — refuted 2026-10-18 against source:
   compiled code lives only in the driver cache (mesa). The file = source + state. (The README
   and this file were wrong for a while; the user caught it twice.)
6. **"Same RPCS3 build" is the wrong requirement** — it's the cache format version (v1.95);
   builds share it. (User correction, 2026-10-18.)

---

## 5. Environment / how to run
- Game files live on host **reven** at `/home/reven/.config/rpcs3/dev_hdd0/game/BCES01741/USRDIR`.
- Parser runs in Docker image `nousresearch/hermes-agent:main` with volumes:
  - game dir → `/game` (ro)
  - scratch  → `/out` (rw)
  - raw shader dir → `/cache` (ro)
  ```
  ssh -i /opt/data/profiles/maciej/home/.ssh/pc_power_key reven@127.0.0.1 \
    'docker run --rm --entrypoint python3 \
       -v /home/reven/.config/rpcs3/dev_hdd0/game/BCES01741/USRDIR:/game:ro \
       -v /home/reven/.hermes/profiles/maciej/cache/scratch:/out \
       nousresearch/hermes-agent:main /out/<script>.py'
  ```

### Key files
| file                          | what it is |
|-------------------------------|------------|
| `/opt/data/tools/rpcs3-cache-toolkit/opt_cache.py` | RSCA v1 pack/unpack/merge + verify (stdlib only) |
| `/opt/data/tools/rpcs3-cache-toolkit/export_cache.py`, `import_cache.py` | older tar.gz path (one archive per game, manifest.json, no dedup) |
| `/opt/data/tools/rpcs3-cache-toolkit/make_test_cache.py` | synthetic cache (GOWA 50 entries v1.95 + EBOOT 3 v1 + empty dir) |
| `/opt/data/tools/rpcs3-cache-toolkit/README.md` | usage guide (main use: never-played recipient) |
| `psarc_oracle.py` (scratch)   | **verified** full PSARC parser + anchor matcher (the reference impl) |
| `analyze_cache2.py` (scratch) | cache-entry field analysis + final size model |
| `exts.py` (scratch)           | extension distribution across the 6 PSARCs (bug fixed, ready to run) |
| `anchor_oracle.json` (scratch)| anchor-matching results (43 KB) |
| `tpps_manifest.txt` (scratch) | TPPS.PSARC file list (376 lines) |
| `tpps_list.txt` (scratch)     | TPPS.PSARC TOC dump (377 entries, with blk/usize/foff) |
| `gh-19810-dedup-comment.md` (scratch) | **PR comment ready to publish** (natural voice, concrete numbers) |
| `gh-19810-comment.md` (scratch) | earlier/alternate comment draft |

---

## 6. Open / next
- PR comment is written & saved (`gh-19810-dedup-comment.md`) — **awaiting user go-ahead to
  publish** (user rule: no posting without explicit consent; also asked to be written in a
  natural, non-AI voice with concrete numbers and a casual "Can share if useful" tone).
- The `exts.py` cross-PSARC extension distribution was mid-run when the Counter bug was hit —
  re-run to get the final per-extension counts across all 6 PSARCs.
- Optionally: run `psarc_oracle.py` to refresh `anchor_oracle.json` and report
  `raws_with_full_hit / raws_missing`.
- The distribution file for GoW (BCES01741.rsca, 0.64 MB) is ready to hand to a never-played
  recipient — only needs the same game + v1.95 format (any GPU).
