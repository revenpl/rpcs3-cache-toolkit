#!/usr/bin/env python3
"""End-to-end tests for the SPLIT-format .rpo exporter + game-level cache_tool UX.

Synthetic RPCS3 FORK split cache (real layout, two games):
  BCES01741: GOWA.SELF (50 pipelines, 1 AMD device), EBOOT.BIN (3, 1 AMD),
             1 EMPTY module (skipped), 1 LEGACY module (ignored)
  BCES01277: coo.ppu.self (8 pipelines, 2 devices), EBOOT.BIN (4, 1 NVIDIA),
             ghs.ppu.self EMPTY (skipped)

Run: python3 -m unittest test_export -v   (or: python3 test_export.py -v)
"""
import os
import struct
import shutil
import hashlib
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import opt_cache  # noqa: E402
import cache_tool  # noqa: E402

RSPL = 0x4C505352
RSPT = 0x54505352
VERSION = "v1.96"


def _build(data_root):
    script = os.path.join(data_root, "make_test_cache.py")
    shutil.copy(os.path.join(HERE, "make_test_cache.py"), script)
    subprocess.run([sys.executable, script, data_root], check=True, capture_output=True)


def _walk(base):
    out = {}
    for r, _d, fs in os.walk(base):
        for f in fs:
            p = os.path.join(r, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, base)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _write_split(ppu_dir, n=1, devices=("10DE_2184",), version=VERSION):
    """Write a minimal valid split cache under <ppu_dir>/shaders_cache."""
    sc = os.path.join(ppu_dir, "shaders_cache")
    vdir = os.path.join(sc, "pipelines", "vulkan", version)
    os.makedirs(os.path.join(sc, "raw"), exist_ok=True)
    for key in devices:
        os.makedirs(os.path.join(vdir, "device", key), exist_ok=True)
    with open(os.path.join(vdir, "pipelines.bin"), "wb") as f:
        f.write(struct.pack("<IIII", RSPL, 1, n, 320) + os.urandom(n * 320))
    for key in devices:
        with open(os.path.join(vdir, "device", key, "props.bin"), "wb") as f:
            f.write(struct.pack("<IIII", RSPT, 1, n, 480) + os.urandom(n * 480))
    with open(os.path.join(sc, "raw", "A.vp"), "wb") as f:
        f.write(b"vp-bytes")


def _make_rpo(root, data, game="BCES01741"):
    out = os.path.join(root, "work")
    os.makedirs(out, exist_ok=True)
    p, _info = cache_tool.do_export_game(data, game, out)
    assert os.path.isfile(p)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rpo_")
        # .rpo files already in the program folder belong to the user —
        # tearDown must never delete them (2026-10-22: the blanket cleanup
        # wiped a real BCES01741.rpo that sat next to the scripts).
        self._rpo_preserved = {f for f in os.listdir(HERE) if f.lower().endswith(".rpo")}
        self.data = os.path.join(self.root, "data")
        os.makedirs(self.data)
        _build(self.data)
        self.trees = cache_tool.find_shader_caches(self.data)
        self.games = cache_tool.group_games(self.trees)
        self.rpo = _make_rpo(self.root, self.data)
        self.rpo_name = os.path.basename(self.rpo)
        # mirror the program-folder layout: .rpo next to the scripts
        self.prog = os.path.join(self.root, "prog")
        os.makedirs(self.prog, exist_ok=True)
        shutil.copy(self.rpo, os.path.join(self.prog, self.rpo_name))
        shutil.copy(os.path.join(HERE, "opt_cache.py"), self.prog)
        shutil.copy(os.path.join(HERE, "cache_tool.py"), self.prog)
        # destination data dir (fresh "other machine")
        self.dst = os.path.join(self.root, "dst")
        os.makedirs(self.dst)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        for base in (HERE,):
            if not os.path.isdir(base):
                continue
            for f in os.listdir(base):
                if f.lower().endswith(".rpo") and f not in getattr(self, "_rpo_preserved", set()):
                    try:
                        os.remove(os.path.join(base, f))
                    except OSError:
                        pass


class TestDiscovery(Base):
    def test_games_and_modules(self):
        self.assertEqual(sorted(self.games), ["BCES01277", "BCES01741"])
        # only VALID split caches are listed (empty + legacy modules are NOT)
        self.assertEqual(len(self.trees), 4)
        by_game = {g: {os.path.basename(t["ppu_dir"]): t for t in ts}
                   for g, ts in self.games.items()}
        self.assertEqual(sorted(by_game["BCES01741"]), ["ppu-AAA111-GOWA.SELF", "ppu-BBB222-EBOOT.BIN"])
        self.assertEqual(by_game["BCES01741"]["ppu-AAA111-GOWA.SELF"]["entries"], 50)
        self.assertEqual(by_game["BCES01741"]["ppu-BBB222-EBOOT.BIN"]["entries"], 3)
        self.assertEqual(by_game["BCES01277"]["ppu-CCC333-coo.ppu.self"]["entries"], 8)
        self.assertEqual(by_game["BCES01277"]["ppu-DDD444-EBOOT.BIN"]["entries"], 4)
        # empty + legacy modules must NOT be present
        all_ppu = {os.path.basename(t["ppu_dir"]) for t in self.trees}
        self.assertNotIn("ppu-0000000000000000000000000000-", all_ppu)  # empty
        self.assertNotIn("ppu-LEG111-legacy.SELF", all_ppu)             # legacy
        self.assertNotIn("ppu-EEEE555-ghs.ppu.self", all_ppu)           # empty

    def test_select_game(self):
        self.assertEqual(cache_tool.select_game(self.games, None), "BCES01741")  # 53 > 12
        self.assertEqual(cache_tool.select_game(self.games, "BCES01277"), "BCES01277")
        self.assertIsNone(cache_tool.select_game(self.games, "NOPE"))

    def test_game_dir_for(self):
        d = cache_tool.game_dir_for(self.data, "bcES01741")
        self.assertEqual(os.path.basename(d), "BCES01741")
        self.assertIsNone(cache_tool.game_dir_for(self.data, "BCES99999"))


class TestWipedCacheDiscovery(Base):
    """2026-10-22 incident: after the user deleted the live cache, discover()
    fell through to a fixture tree elsewhere in $HOME (test trees under
    ~/.hermes) and import unpacked into the wrong place. A genuine install
    (RPCS3-specific artifacts) must win even when its cache is empty."""

    def _home(self, with_install=True):
        home = os.path.join(self.root, "home2")
        if with_install:
            install = os.path.join(home, ".cache", "rpcs3")
            os.makedirs(os.path.join(install, "cache"))  # wiped cache dir
            with open(os.path.join(install, "RPCS3.log"), "w") as f:
                f.write("log\n")
            os.makedirs(os.path.join(install, "ppu_progs"))
        # fixture: a usable split cache elsewhere in $HOME
        fx = os.path.join(home, ".hermes", "cache")
        shutil.copytree(os.path.join(self.data, "cache", "BCES01277"),
                        os.path.join(fx, "BCES01277"))
        return home

    def test_wiped_install_beats_fixture(self):
        home = self._home(with_install=True)
        env = dict(os.environ, HOME=home,
                   XDG_CACHE_HOME=os.path.join(home, ".cache"),
                   XDG_CONFIG_HOME=os.path.join(home, ".config"))
        data, trees = cache_tool.discover(home=home, env=env)
        self.assertIsNotNone(data)
        self.assertEqual(os.path.basename(data), "rpcs3")
        self.assertEqual(trees, [])

    def test_fixture_still_found_without_install(self):
        home = self._home(with_install=False)
        env = dict(os.environ, HOME=home,
                   XDG_CACHE_HOME=os.path.join(home, "nope"),
                   XDG_CONFIG_HOME=os.path.join(home, "nope2"))
        data, trees = cache_tool.discover(home=home, env=env)
        self.assertIsNotNone(data)
        self.assertTrue(trees)
        self.assertEqual(os.path.basename(data), ".hermes")


class TestExport(Base):
    def test_manifest_g1741(self):
        manifest, rows = opt_cache.list_rpo(self.rpo)
        self.assertEqual(manifest["gameId"], "BCES01741")
        self.assertEqual(manifest["module_count"], 2)
        by = {m["name"]: m for m in manifest["modules"]}
        self.assertEqual(by["ppu-AAA111-GOWA.SELF"]["pipeline_count"], 50)
        self.assertEqual(by["ppu-AAA111-GOWA.SELF"]["raw_count"], 100)  # 50 vp + 50 fp
        self.assertEqual(by["ppu-BBB222-EBOOT.BIN"]["pipeline_count"], 3)
        # each module carries a sha256 map covering pipelines + devices + raw
        for m in manifest["modules"]:
            keys = list(m["sha256"])
            self.assertTrue(any(k.endswith("pipelines.bin") for k in keys), keys)
            self.assertTrue(any(k.endswith("props.bin") for k in keys), keys)
            self.assertTrue(any(k.startswith("raw/") for k in keys), keys)
        # legacy module (ppu-LEG111) and empty module must NOT be in the pack
        self.assertNotIn("ppu-LEG111-legacy.SELF", by)
        self.assertNotIn("ppu-0000000000000000000000000000-", by)

    def test_manifest_g1277_two_devices(self):
        p, _ = cache_tool.do_export_game(self.data, "BCES01277", self.root)
        manifest, _ = opt_cache.list_rpo(p)
        by = {m["name"]: m for m in manifest["modules"]}
        coo = by["ppu-CCC333-coo.ppu.self"]
        self.assertEqual(coo["device_count"], 16)  # 8 (AMD) + 8 (NVIDIA)
        keys = {d["key"] for d in coo["devices"]}
        self.assertEqual(keys, {"1002_67B0", "10DE_2184"})

    def test_roundtrip_byte_exact(self):
        # export -> unpack into a fresh data dir -> every file byte-identical
        dst = os.path.join(self.root, "fresh")
        os.makedirs(dst)
        res = opt_cache.unpack_game(self.rpo, dst)
        self.assertEqual(res["gameId"], "BCES01741")
        base = os.path.join(dst, "cache", "BCES01741")
        src_game = os.path.join(self.data, "cache", "BCES01741")
        for mod in ("ppu-AAA111-GOWA.SELF", "ppu-BBB222-EBOOT.BIN"):
            a = _walk(os.path.join(src_game, mod, "shaders_cache"))
            b = _walk(os.path.join(base, mod, "shaders_cache"))
            self.assertEqual(a, b, f"module {mod} not byte-exact")
            # correct tree (version path preserved)
            self.assertTrue(any(k.startswith("pipelines/vulkan/v1.96/pipelines.bin") for k in b))

    def test_verify_ok(self):
        res = opt_cache.verify_rpo(self.rpo)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["modules"], 2)

    def test_checksum_mismatch_detected(self):
        # corrupt one byte inside the .rpo raw payload -> verify must fail
        data = bytearray(open(self.rpo, "rb").read())
        # find a raw entry's payload region (after the header + manifest) and flip a byte
        data[-20] ^= 0xFF  # near the end = inside some raw payload
        bad = os.path.join(self.root, "bad.rpo")
        with open(bad, "wb") as f:
            f.write(bytes(data))
        res = opt_cache.verify_rpo(bad)
        self.assertFalse(res["ok"])

    def test_bad_rpo_rejected(self):
        fake = os.path.join(self.root, "fake.rpo")
        with open(fake, "wb") as f:
            f.write(b"NOTANRPO" + b"\x00" * 32)
        with self.assertRaises(ValueError):
            list(opt_cache._parse_rpo(open(fake, "rb").read()))


class TestImport(Base):
    def test_import_into_fresh_data_dir(self):
        dst = os.path.join(self.root, "fresh2")
        os.makedirs(dst)
        res = cache_tool.do_import_rpo(self.rpo, dst)
        self.assertEqual(res["gameId"], "BCES01741")
        base = os.path.join(dst, "cache", "BCES01741")
        self.assertTrue(os.path.isdir(os.path.join(base, "ppu-AAA111-GOWA.SELF", "shaders_cache")))
        self.assertTrue(os.path.isfile(os.path.join(base, "ppu-BBB222-EBOOT.BIN", "shaders_cache",
                                                    "pipelines", "vulkan", VERSION, "pipelines.bin")))

    def test_import_overwrites_existing(self):
        # pre-create the destination module, then import (must land, not double-nest)
        dst = os.path.join(self.root, "existing")
        os.makedirs(os.path.join(dst, "cache", "BCES01741"))
        res = cache_tool.do_import_rpo(self.rpo, dst)
        base = os.path.join(dst, "cache", "BCES01741")
        self.assertEqual(os.path.basename(res["base"]), "BCES01741")
        self.assertTrue(os.path.isfile(os.path.join(base, "ppu-AAA111-GOWA.SELF", "shaders_cache",
                                                    "pipelines", "vulkan", VERSION, "pipelines.bin")))
        # NO double cache/cache nesting
        self.assertFalse(os.path.isdir(os.path.join(base, "cache")))

    def test_roundtrip_stable(self):
        """export -> import -> export again: the .rpo must be byte-identical."""
        p1 = self.rpo
        dst = os.path.join(self.root, "rt")
        os.makedirs(dst)
        cache_tool.do_import_rpo(p1, dst)
        out2 = os.path.join(self.root, "rt2")
        p2, _ = cache_tool.do_export_game(dst, "BCES01741", out2)
        with open(p1, "rb") as a, open(p2, "rb") as b:
            self.assertEqual(a.read(), b.read())


class TestCli(Base):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.prog, "cache_tool.py"), *args],
            capture_output=True, text=True, cwd=self.prog)

    def test_discover_cli(self):
        r = self._run("discover", "--data", self.data)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("BCES01741", r.stdout)
        self.assertIn("GOWA.SELF", r.stdout)

    def test_export_cli_default_biggest(self):
        out = os.path.join(self.root, "out")
        r = self._run("export", "--data", self.data, "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(os.listdir(out)), ["BCES01741.rpo"])

    def test_export_cli_by_game(self):
        out = os.path.join(self.root, "out2")
        r = self._run("export", "--data", self.data, "--game", "BCES01277", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        p = os.path.join(out, "BCES01277.rpo")
        manifest, _ = opt_cache.list_rpo(p)
        self.assertEqual(manifest["module_count"], 2)

    def test_import_cli(self):
        dst = os.path.join(self.root, "idst")
        os.makedirs(dst)
        r = self._run("import", self.rpo_name, "--data", dst)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("imported", r.stdout)
        self.assertTrue(os.path.isdir(os.path.join(dst, "cache", "BCES01741")))

    def test_import_missing_file(self):
        r = self._run("import", "ghost.rpo", "--data", self.dst)
        self.assertEqual(r.returncode, 2)


class TestInteractive(Base):
    def setUp(self):
        super().setUp()
        # deterministic HOME: discover() must find exactly this cache
        self.home = os.path.join(self.root, "home")
        home_cache = os.path.join(self.home, ".cache", "rpcs3", "cache")
        os.makedirs(home_cache)
        shutil.copytree(os.path.join(self.data, "cache", "BCES01741"),
                        os.path.join(home_cache, "BCES01741"))
        self.env = dict(os.environ, HOME=self.home,
                        XDG_CACHE_HOME=os.path.join(self.home, ".cache"),
                        XDG_CONFIG_HOME=os.path.join(self.home, ".config"))

    def _run(self, stdin, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.prog, "cache_tool.py"), *args],
            input=stdin, capture_output=True, text=True, timeout=30,
            cwd=self.prog, env=self.env)

    def test_export_flow(self):
        r = self._run("2\n1\n")  # choose export, pick game 1
        self.assertIn("exported", r.stdout, r.stdout)
        p = os.path.join(self.prog, "BCES01741.rpo")
        self.assertTrue(os.path.isfile(p))
        manifest, _ = opt_cache.list_rpo(p)
        self.assertEqual(manifest["module_count"], 2)
        os.remove(p)

    def test_import_flow(self):
        r = self._run("1\n1\n")  # choose import, pick file 1
        self.assertIn("imported", r.stdout, r.stdout)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".cache", "rpcs3", "cache", "BCES01741")))


def _make_sfo(entries):
    """Build PSF/SFO bytes (RPCS3 format) from [(key, value, max_size), ...]."""
    keys = b"".join(k.encode() + b"\x00" for k, _v, _m in entries)
    keys += b"\x00" * (-len(keys) % 4)
    off_keys = 20 + 16 * len(entries)
    off_data = off_keys + len(keys)
    out = struct.pack("<IIIII", 0x46535000, 0x101, off_keys, off_data, len(entries))
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
    """Game title lookup (local PARAM.SFO) + cleaner display lines (format-independent)."""

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
        self._orig_bases = cache_tool._library_base_dirs
        cache_tool._game_library_roots = lambda data_dir: [self.lib]
        cache_tool._TITLE_CACHE.clear()

    def tearDown(self):
        cache_tool._game_library_roots = self._orig_roots
        cache_tool._library_base_dirs = self._orig_bases
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
        self.assertEqual(cache_tool._ppu_label("/x/ppu-abc"), "ppu-abc")

    def test_game_title(self):
        self.assertEqual(cache_tool.game_title("BCES01741", self.data), "Test Game Title")
        self.assertEqual(cache_tool.game_title("bcES01741", self.data), "Test Game Title")
        self.assertIsNone(cache_tool.game_title("BCZZ00000", self.data))

    def test_games_view_labels_and_title(self):
        lines = cache_tool._games_view(self.games, self.data)
        l1741 = next(l for l in lines if "BCES01741" in l)
        self.assertIn("Test Game Title (BCES01741)", l1741)
        self.assertIn("GOWA.SELF", l1741)
        self.assertNotIn("ppu-AAA111", l1741)
        l1277 = next(l for l in lines if "BCES01277" in l)
        self.assertIn("coo.ppu.self", l1277)
        self.assertTrue(l1277.startswith("  game BCES01277"))

    def _hermetic_bases(self, data_dir):
        cache_tool._library_base_dirs = lambda dd: [dd] if dd else []

    def test_config_dev_hdd0_root(self):
        lib2 = os.path.join(self.root, "library2")
        g2 = os.path.join(lib2, "game", "BCES01277")
        os.makedirs(g2)
        sfo = _make_sfo([("TITLE", "God of War Collection 2", 64), ("CATEGORY", "GD", 4)])
        with open(os.path.join(g2, "PARAM.SFO"), "wb") as f:
            f.write(sfo)
        with open(os.path.join(self.data, "config.yml"), "w") as f:
            f.write("VFS:\n  dev_hdd0: '%s'\n" % lib2)
        cache_tool._game_library_roots = self._orig_roots
        self._hermetic_bases(self.data)
        cache_tool._TITLE_CACHE.clear()
        self.assertEqual(cache_tool.game_title("BCES01277", self.data), "God of War Collection 2")

    def test_data_dir_dev_hdd0_default(self):
        g = os.path.join(self.data, "dev_hdd0", "game", "BCES01741")
        os.makedirs(g)
        sfo = _make_sfo([("TITLE", "God of War", 64), ("TITLE_ID", "BCES01741", 16)])
        with open(os.path.join(g, "PARAM.SFO"), "wb") as f:
            f.write(sfo)
        cache_tool._game_library_roots = self._orig_roots
        self._hermetic_bases(self.data)
        cache_tool._TITLE_CACHE.clear()
        self.assertEqual(cache_tool.game_title("BCES01741", self.data), "God of War")

    def test_real_sfo_file(self):
        self.assertEqual(cache_tool.PSF_MAGIC, 0x46535000)
        sfo = _make_sfo([("TITLE", "Primary Title", 32), ("NPTITLE", "Fallback", 32)])
        self.assertEqual(cache_tool._sfo_title(sfo), "Primary Title")
        sfo2 = _make_sfo([("NPTITLE", "Fallback Only", 32)])
        self.assertEqual(cache_tool._sfo_title(sfo2), "Fallback Only")
        self.assertIsNotNone(cache_tool._sfo_entry(sfo, "TITLE"))

    def test_macos_two_dir_layout(self):
        """RPCS3 on macOS keeps cache and games in DIFFERENT dirs. Titles must
        resolve from the second dir; discovery must find the split cache."""
        home = os.path.join(self.root, "mhome")
        data = os.path.join(home, "Library", "Caches", "rpcs3")
        games = os.path.join(home, "Library", "Application Support", "rpcs3")
        # SPLIT cache (so split-only discovery finds it)
        _write_split(os.path.join(data, "cache", "BCES01277", "ppu-x-APP.SELF"), 1)
        g = os.path.join(games, "dev_hdd0", "game", "BCES01277")
        os.makedirs(g)
        with open(os.path.join(g, "PARAM.SFO"), "wb") as f:
            f.write(_make_sfo([("TITLE", "God of War Collection 2", 64)]))
        real = cache_tool.sys.platform
        cache_tool.sys.platform = "darwin"
        try:
            cache_tool._game_library_roots = self._orig_roots
            cache_tool._library_base_dirs = lambda dd: [dd] + cache_tool.candidate_data_dirs(home, {})
            cache_tool._TITLE_CACHE.clear()
            data_dir, _ = cache_tool.discover(home=home, env={})
            self.assertEqual(data_dir, data)
            self.assertEqual(cache_tool.game_title("BCES01277", data_dir), "God of War Collection 2")
        finally:
            cache_tool.sys.platform = real
            cache_tool._game_library_roots = lambda data_dir: [self.lib]
            cache_tool._library_base_dirs = self._orig_bases
            cache_tool._TITLE_CACHE.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
