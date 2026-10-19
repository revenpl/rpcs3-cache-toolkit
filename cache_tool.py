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
