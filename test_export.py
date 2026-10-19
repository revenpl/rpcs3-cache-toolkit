#!/usr/bin/env python3
"""End-to-end tests for the RSCA v1/v2 format + game-level cache_tool UX.

Synthetic RPCS3 cache (real layout, two games):
  BCES01741: ppu-GOWA.SELF (50, v1.95, last raw pair missing), ppu-EBOOT.BIN (3, vulkan/v1)
  BCES01277: ppu-coo.ppu.self (8), ppu-EBOOT.BIN (4), ppu-ghs.ppu.self (EMPTY — skipped)

Run: python3 -m unittest test_export -v   (or: python3 test_export.py -v)
"""
import os
import struct
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import opt_cache  # noqa: E402
import cache_tool  # noqa: E402

_REAL_CANDIDATES = [
    os.path.expanduser("~/profiles/maciej/attachments/BCES01741.rsca"),
    "/opt/data/profiles/maciej/attachments/BCES01741.rsca",
]
REAL_FILE = next((p for p in _REAL_CANDIDATES if os.path.isfile(p)), _REAL_CANDIDATES[0])


def _build(data_root):
    script = os.path.join(data_root, "make_test_cache.py")
    shutil.copy(os.path.join(HERE, "make_test_cache.py"), script)
    subprocess.run([sys.executable, script, data_root],
                   check=True, capture_output=True)


def _count(dirpath, suffixes=(".bin",)):
    n = 0
    for _r, _d, files in os.walk(dirpath):
        n += sum(1 for f in files if f.endswith(tuple(suffixes)))
    return n


def _make_rsca(root, data):
    """Pack game BCES01741 (both non-empty modules) via the cache_tool API."""
    out = os.path.join(root, "work")
    os.makedirs(out, exist_ok=True)
    p = cache_tool.do_export_game(data, "BCES01741", out)
    assert os.path.isfile(p)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rscav2_")
        self.data = os.path.join(self.root, "data")
        os.makedirs(self.data)
        _build(self.data)
        self.trees = cache_tool.find_shader_caches(self.data)
        self.games = cache_tool.group_games(self.trees)
        self.rsca = _make_rsca(self.root, self.data)
        self.rsca_name = os.path.basename(self.rsca)
        # mirror the program-folder layout: .rsca next to the scripts
        for f in os.listdir(HERE):
            if f.lower().endswith(".rsca"):
                try:
                    os.remove(os.path.join(HERE, f))
                except OSError:
                    pass
        self.prog = os.path.join(self.root, "prog")
        os.makedirs(self.prog, exist_ok=True)
        shutil.copy(self.rsca, os.path.join(self.prog, self.rsca_name))
        shutil.copy(os.path.join(HERE, "opt_cache.py"), self.prog)
        shutil.copy(os.path.join(HERE, "cache_tool.py"), self.prog)
        # destination data dir (fresh "other machine" cache)
        self.dst = os.path.join(self.root, "dst")
        self.dst_g1 = os.path.join(self.dst, "cache", "BCES01741")
        self.dst_gow = os.path.join(self.dst_g1, "ppu-TTT1-GOWA.SELF")
        self.dst_ebo = os.path.join(self.dst_g1, "ppu-UVV2-EBOOT.BIN")
        for d, n in ((self.dst_gow, 2), (self.dst_ebo, 1)):
            for i in range(n):
                vp, fp = 0xA000 + i, 0xB000 + i
                d_sc = os.path.join(d, "shaders_cache")
                pd = os.path.join(d_sc, "pipelines", "vulkan", "v1.95")
                os.makedirs(pd, exist_ok=True)
                with open(os.path.join(pd, f"{vp:X}+{fp:X}+C+D.bin"), "wb") as f:
                    f.write(struct.pack("<QQ", vp, fp) + b"\x00" * 656)
                raw = os.path.join(d_sc, "raw")
                os.makedirs(raw, exist_ok=True)
                with open(os.path.join(raw, f"{vp:X}.vp"), "wb") as f:
                    f.write(b"v" * 40)
                with open(os.path.join(raw, f"{fp:X}.fp"), "wb") as f:
                    f.write(b"f" * 80)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        for base in (HERE, self.prog if hasattr(self, "prog") else None):
            if not base or not os.path.isdir(base):
                continue
            for f in os.listdir(base):
                if f.lower().endswith(".rsca"):
                    try:
                        os.remove(os.path.join(base, f))
                    except OSError:
                        pass


class TestDiscovery(Base):
    def test_games_and_modules(self):
        self.assertEqual(sorted(self.games), ["BCES01277", "BCES01741"])
        self.assertEqual(len(self.trees), 6)  # 3 ppu dirs per game (incl. the empty ones)
        by_game = {g: sorted(os.path.basename(t["ppu_dir"]) for t in ts) for g, ts in self.games.items()}
        self.assertEqual(by_game["BCES01741"],
                         ["ppu-0000000000000000000000000000-", "ppu-AAA111-GOWA.SELF", "ppu-BBB222-EBOOT.BIN"])
        self.assertEqual(by_game["BCES01277"],
                         ["ppu-CCC333-coo.ppu.self", "ppu-DDD444-EBOOT.BIN", "ppu-EEEE555-ghs.ppu.self"])
        # only non-empty modules are usable
        playable = [t for t in self.trees if t["entries"] > 0]
        self.assertEqual(len(playable), 4)

    def test_select_game(self):
        self.assertEqual(cache_tool.select_game(self.games, None), "BCES01741")  # most entries
        self.assertEqual(cache_tool.select_game(self.games, "BCES01277"), "BCES01277")
        self.assertIsNone(cache_tool.select_game(self.games, "NOPE"))

    def test_game_dir_for(self):
        d = cache_tool.game_dir_for(self.data, "bcES01741")
        self.assertEqual(os.path.basename(d), "BCES01741")
        self.assertIsNone(cache_tool.game_dir_for(self.data, "BCES99999"))


class TestRscaV2(Base):
    def test_header_v2(self):
        with open(self.rsca, "rb") as f:
            head = f.read(24)
        self.assertEqual(head[:4], b"RSCA")
        self.assertEqual(struct.unpack_from("<I", head, 4)[0], 2)
        self.assertEqual(head[8:24].rstrip(b"\x00"), b"BCES01741")

    def test_lists_two_modules(self):
        info = opt_cache.list_modules(self.rsca)
        self.assertEqual(info["version"], 2)
        labels = sorted(m["label"] for m in info["modules"])
        self.assertEqual(labels, ["EBOOT.BIN", "GOWA.SELF"])
        entries = {m["label"]: m["entries"] for m in info["modules"]}
        self.assertEqual(entries, {"GOWA.SELF": 50, "EBOOT.BIN": 3})

    def test_raw_counts(self):
        info = opt_cache.list_modules(self.rsca)
        self.assertEqual(len(info["modules"]), 2)
        by = {m["label"]: m for m in info["modules"]}
        self.assertEqual(by["GOWA.SELF"]["raw"], 98)   # 49 pairs — the 50th entry has no raw files
        self.assertEqual(by["EBOOT.BIN"]["raw"], 6)

    def test_export_collection_skips_empty(self):
        out = os.path.join(self.root, "out2")
        p = cache_tool.do_export_game(self.data, "BCES01277", out)
        info = opt_cache.list_modules(p)
        self.assertEqual(sorted(m["label"] for m in info["modules"]), ["EBOOT.BIN", "coo.ppu.self"])
        entries = {m["label"]: m["entries"] for m in info["modules"]}
        self.assertEqual(entries, {"coo.ppu.self": 8, "EBOOT.BIN": 4})


class TestImport(Base):
    def test_import_merges_all_modules(self):
        target = cache_tool.do_import(self.rsca, self.dst)
        self.assertEqual(os.path.basename(target), "BCES01741")
        # both modules merged, union semantics (50+0 new in GOWA, 3+0 new in EBOOT)
        gow = _count(os.path.join(self.dst_gow, "shaders_cache", "pipelines"))
        ebo = _count(os.path.join(self.dst_ebo, "shaders_cache", "pipelines"))
        self.assertEqual(gow, 52)   # 50 + 2 existing
        self.assertEqual(ebo, 4)    # 3 + 1 existing
        self.assertEqual(_count(os.path.join(self.dst_gow, "shaders_cache", "raw"), (".vp", ".fp")), 98 + 4)
        self.assertEqual(_count(os.path.join(self.dst_ebo, "shaders_cache", "raw"), (".vp", ".fp")), 6 + 2)

    def test_import_label_match_ignores_ppu_hash(self):
        # destination module names have different hashes — matching must be by label
        self.assertNotEqual(os.path.basename(self.dst_gow), "ppu-AAA111-GOWA.SELF")
        cache_tool.do_import(self.rsca, self.dst)
        self.assertGreater(_count(os.path.join(self.dst_gow, "shaders_cache", "pipelines")), 2)
        self.assertGreater(_count(os.path.join(self.dst_ebo, "shaders_cache", "pipelines")), 1)

    def test_import_no_game_dir_raises(self):
        bare = os.path.join(self.root, "bare")
        os.makedirs(bare)
        with self.assertRaises(FileNotFoundError):
            cache_tool.do_import(self.rsca, bare)

    def test_import_v1_legacy(self):
        # v1 = one module body, fmt=1 — must still import (into the first ppu dir)
        gowa = next(t for t in self.trees if t["ppu_dir"].endswith("GOWA.SELF"))
        m = opt_cache._pack_module(gowa["ppu_dir"], 0)
        v1 = os.path.join(self.root, "legacy.rsca")
        with open(v1, "wb") as f:
            f.write(b"RSCA" + struct.pack("<I", 1))
            f.write(b"BCES01741".ljust(16, b"\x00"))
            f.write(m["cls"].encode().ljust(16, b"\x00"))
            f.write(m["ver"].encode().ljust(16, b"\x00"))
            f.write(m["body"])
        target = cache_tool.do_import(v1, self.dst)
        self.assertEqual(os.path.basename(target), "BCES01741")
        self.assertEqual(_count(os.path.join(self.dst_gow, "shaders_cache", "pipelines")), 52)
        self.assertEqual(_count(os.path.join(self.dst_gow, "shaders_cache", "raw"), (".vp", ".fp")), 102)


class TestCli(Base):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.prog, "cache_tool.py"), *args],
            capture_output=True, text=True, cwd=self.prog)

    def test_export_default_picks_biggest_game(self):
        out = os.path.join(self.root, "out")
        r = self._run("export", "--data", self.data, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("exported", r.stdout)
        self.assertEqual(sorted(os.listdir(out)), ["BCES01741.rsca"])

    def test_export_by_game(self):
        out = os.path.join(self.root, "out_g2")
        r = self._run("export", "--data", self.data, "--game", "BCES01277", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(out)), ["BCES01277.rsca"])
        info = opt_cache.list_modules(os.path.join(out, "BCES01277.rsca"))
        self.assertEqual(len(info["modules"]), 2)

    def test_import_cli(self):
        r = self._run("import", self.rsca_name, "--data", self.dst)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("imported into", r.stdout)
        self.assertEqual(_count(os.path.join(self.dst_gow, "shaders_cache", "pipelines")), 52)

    def test_import_missing_file(self):
        r = self._run("import", "ghost.rsca", "--data", self.dst)
        self.assertEqual(r.returncode, 2)

    def test_import_unknown_game_raises(self):
        r = self._run("import", self.rsca_name, "--data", os.path.join(self.root, "empty"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("no ppu-* cache dir", r.stderr)


class TestInteractive(Base):
    def setUp(self):
        super().setUp()
        # deterministic HOME: discover() must find exactly this cache, nothing else
        self.home = os.path.join(self.root, "home")
        self.home_g1 = os.path.join(self.home, ".cache", "rpcs3", "cache", "BCES01741")
        os.makedirs(os.path.dirname(self.home_g1), exist_ok=True)
        shutil.copytree(self.dst_g1, self.home_g1)
        self.env = dict(os.environ, HOME=self.home,
                        XDG_CACHE_HOME=os.path.join(self.home, ".cache"),
                        XDG_CONFIG_HOME=os.path.join(self.home, ".config"))

    def _run(self, stdin, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.prog, "cache_tool.py"), *args],
            input=stdin, capture_output=True, text=True, timeout=30,
            cwd=self.prog, env=self.env)

    def test_export_flow(self):
        r = self._run("2\n1\n")
        self.assertIn("packed", r.stdout)
        f = "BCES01741.rsca"  # only game in the deterministic home cache
        p = os.path.join(self.prog, f)
        self.assertTrue(os.path.isfile(p))
        info = opt_cache.list_modules(p)
        self.assertEqual(len(info["modules"]), 2)
        os.remove(p)

    def test_import_flow(self):
        r = self._run("1\n1\n")
        self.assertIn("imported into", r.stdout)
        self.assertIn(self.home, r.stdout)
        gow = self.home_g1
        self.assertEqual(_count(os.path.join(gow, "ppu-TTT1-GOWA.SELF", "shaders_cache", "pipelines")), 52)
        self.assertEqual(_count(os.path.join(gow, "ppu-UVV2-EBOOT.BIN", "shaders_cache", "pipelines")), 4)


@unittest.skipUnless(os.path.isfile(REAL_FILE), "real user .rsca not present")
class TestRealFile(Base):
    """The user's actual BCES01741.rsca (RSCA v1, 622 entries, vulkan/v1.94)."""
    REAL = REAL_FILE

    @classmethod
    def setUpClass(cls):
        if os.path.isfile(cls.REAL):
            with open(cls.REAL, "rb") as f:
                p = opt_cache.parse_pack(f.read())
            m = p["modules"][0]
            cls.n_entries = len(m["ent"])
            cls.cls_ver = m["cls"]
            cls.ver = m["ver"]

    def _mk_dir(self, base, n):
        d = os.path.join(base, "shaders_cache")
        pd = os.path.join(d, "pipelines", self.cls_ver, self.ver)
        os.makedirs(pd, exist_ok=True)
        for i in range(n):
            with open(os.path.join(pd, f"1{i:X}+2{i:X}+3+4.bin"), "wb") as f:
                f.write(os.urandom(672))

    def test_v1_picks_closest_ppu_dir(self):
        g = os.path.join(self.root, "g1", "cache", "BCES01741")
        big = os.path.join(g, "ppu-1111-GOWA.SELF")
        small = os.path.join(g, "ppu-2222-EBOOT.BIN")
        self._mk_dir(big, self.n_entries - 2)   # ~file size -> closest
        self._mk_dir(small, 10)
        cache_tool.do_import(self.REAL, os.path.join(self.root, "g1"))
        self.assertEqual(_count(os.path.join(big, "shaders_cache", "pipelines")),
                         (self.n_entries - 2) + self.n_entries)  # disjoint hashes
        self.assertEqual(_count(os.path.join(small, "shaders_cache", "pipelines")), 10)

    def test_v1_ambiguous_asks_for_ppu(self):
        g = os.path.join(self.root, "g2", "cache", "BCES01741")
        a = os.path.join(g, "ppu-3333-GOWA.SELF")
        b = os.path.join(g, "ppu-4444-EBOOT.BIN")
        self._mk_dir(a, 5)
        self._mk_dir(b, 5)  # same count -> ambiguous
        with self.assertRaises(SystemExit) as ctx:
            cache_tool.do_import(self.REAL, os.path.join(self.root, "g2"))
        self.assertEqual(ctx.exception.code, 1)

    def test_v1_explicit_ppu(self):
        g = os.path.join(self.root, "g3", "cache", "BCES01741")
        big = os.path.join(g, "ppu-5555-GOWA.SELF")
        self._mk_dir(big, 3)
        cache_tool.do_import(self.REAL, os.path.join(self.root, "g3"), ppu_dir=big)
        self.assertEqual(_count(os.path.join(big, "shaders_cache", "pipelines")), 3 + self.n_entries)


def _make_sfo(entries):
    """Build PSF/SFO bytes (RPCS3 format) from [(key, value, max_size), ...]."""
    out = struct.pack("<IIIII", 0x00004653, 0x101, 0, 0, len(entries))  # header, fix offsets below
    keys = b"".join(k.encode() + b"\x00" for k, _v, _m in entries)
    keys += b"\x00" * (-len(keys) % 4)
    off_keys = 20 + 16 * len(entries)
    off_data = off_keys + len(keys)
    out = struct.pack("<IIIII", 0x00004653, 0x101, off_keys, off_data, len(entries))
    data_off = 0
    key_off = 0
    for k, v, m in entries:
        out += struct.pack("<HHIII", key_off, 0x0204, min(m, len(v) + 1), m, data_off)
        key_off += len(k) + 1
        data_off += m
    out += keys
    for _k, v, m in entries:
        b = (v.encode() + b"\x00")[:m]
        out += b + b"\x00" * (m - len(b))
    return out


class TestGameNames(Base):
    """Game title lookup (local PARAM.SFO / NPTITLE) + cleaner display lines."""

    def setUp(self):
        super().setUp()
        self.lib = os.path.join(self.root, "library")
        g = os.path.join(self.lib, "BCES01741", "BLURAY")
        os.makedirs(g)
        sfo = _make_sfo([("NPID", "BCES99999", 10), ("NPTITLE", "Test Game Title", 40),
                         ("CATEGORY", "GD", 4)])
        with open(os.path.join(g, "PARAM.SFO"), "wb") as f:
            f.write(sfo)
        self._orig_roots = cache_tool._game_library_roots
        cache_tool._game_library_roots = lambda data_dir: [self.lib]
        cache_tool._TITLE_CACHE.clear()

    def tearDown(self):
        cache_tool._game_library_roots = self._orig_roots
        cache_tool._TITLE_CACHE.clear()
        super().tearDown()

    def test_sfo_entry(self):
        with open(os.path.join(self.lib, "BCES01741", "BLURAY", "PARAM.SFO"), "rb") as f:
            data = f.read()
        self.assertEqual(cache_tool._sfo_entry(data, "NPTITLE"), "Test Game Title")
        self.assertEqual(cache_tool._sfo_entry(data, "NPID"), "BCES99999")
        self.assertEqual(cache_tool._sfo_entry(data, "CATEGORY"), "GD")
        self.assertIsNone(cache_tool._sfo_entry(data, "NOPE"))
        self.assertIsNone(cache_tool._sfo_entry(b"garbage", "NPTITLE"))

    def test_ppu_label(self):
        self.assertEqual(cache_tool._ppu_label("/x/ppu-abc123-GOWA.SELF"), "GOWA.SELF")
        self.assertEqual(cache_tool._ppu_label("/x/ppu-abc-EBOOT.BIN"), "EBOOT.BIN")
        self.assertEqual(cache_tool._ppu_label("/x/ppu-abc"), "ppu-abc")  # fallback

    def test_game_title(self):
        self.assertEqual(cache_tool.game_title("BCES01741", self.data), "Test Game Title")
        self.assertEqual(cache_tool.game_title("bcES01741", self.data), "Test Game Title")  # case-insensitive
        self.assertIsNone(cache_tool.game_title("BCZZ00000", self.data))

    def test_games_view_labels_and_title(self):
        lines = cache_tool._games_view(self.games, self.data)
        l1741 = next(l for l in lines if "BCES01741" in l)
        self.assertIn("Test Game Title (BCES01741)", l1741)
        self.assertIn("GOWA.SELF", l1741)
        self.assertNotIn("ppu-AAA111", l1741)  # full ppu dir name must not leak
        l1277 = next(l for l in lines if "BCES01277" in l)
        self.assertIn("coo.ppu.self", l1277)
        self.assertTrue(l1277.startswith("  game BCES01277"))  # no title -> bare id

    def test_config_dev_disk_root(self):
        # dev_disk0 path from config.yml must be used as library root
        lib2 = os.path.join(self.root, "library2")
        g2 = os.path.join(lib2, "BCES01277", "PS3_GAME")
        os.makedirs(g2)
        sfo = _make_sfo([("NPTITLE", "God of War Collection 2", 64)])
        with open(os.path.join(g2, "PARAM.SFO"), "wb") as f:
            f.write(sfo)
        with open(os.path.join(self.data, "config.yml"), "w") as f:
            f.write("VFS:\n  dev_disk0: '%s'\n" % lib2)
        cache_tool._game_library_roots = self._orig_roots  # real fn: config.yml + platform default
        cache_tool._TITLE_CACHE.clear()
        self.assertEqual(cache_tool.game_title("BCES01277", self.data), "God of War Collection 2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
