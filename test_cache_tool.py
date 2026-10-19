import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import cache_tool  # noqa: E402


def make_synthetic(data_dir):
    """Build a fake RPCS3 data dir with usable shader caches (real layout)."""
    subprocess.check_call([sys.executable, os.path.join(REPO, "make_test_cache.py"), data_dir])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rsca_test_")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.data = os.path.join(self.home, ".cache", "rpcs3")
        make_synthetic(self.data)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **kw):
        e = {"HOME": self.home, "USERPROFILE": self.home}
        e.update(kw)
        return e


class TestScaffold(unittest.TestCase):
    def test_scaffold(self):
        self.assertTrue(os.path.isdir(cache_tool.PROGRAM_DIR))


class TestCandidateDirs(Base):
    def test_linux_candidates_include_home_cache(self):
        cands = cache_tool.candidate_data_dirs(self.home, self.env())
        self.assertIn(self.data, cands)

    def test_xdg_cache_home_respected(self):
        xdg = os.path.join(self.tmp, "xdg")
        cands = cache_tool.candidate_data_dirs(self.home, self.env(XDG_CACHE_HOME=xdg))
        self.assertIn(os.path.join(xdg, "rpcs3"), cands)


class TestFindShaderCaches(Base):
    def test_finds_gowa_and_eboot(self):
        trees = cache_tool.find_shader_caches(self.data)
        self.assertEqual({t["game_id"] for t in trees}, {"BCES01741"})
        self.assertEqual(len(trees), 3)
        gowa = [t for t in trees if t["entries"] == 50]
        self.assertEqual(len(gowa), 1)
        self.assertTrue(gowa[0]["ppu_dir"].endswith("GOWA.SELF"))

    def test_missing_data_dir_returns_empty(self):
        self.assertEqual(cache_tool.find_shader_caches(os.path.join(self.tmp, "nope")), [])


class TestDiscover(Base):
    def test_finds_cache_in_default_candidate(self):
        data, trees = cache_tool.discover(home=self.home, env=self.env())
        self.assertEqual(data, self.data)
        self.assertTrue(any(t["entries"] == 50 for t in trees))

    def test_xdg_cache_home_candidate_wins(self):
        xdg_data = os.path.join(self.tmp, "xdg", "rpcs3")
        make_synthetic(xdg_data)
        data, _t = cache_tool.discover(home=self.home, env=self.env(XDG_CACHE_HOME=os.path.dirname(xdg_data)))
        self.assertEqual(data, xdg_data)

    def test_search_fallback_when_no_candidate(self):
        odd = os.path.join(self.home, "games", "rpcs3")
        shutil.move(self.data, odd)
        data, trees = cache_tool.discover(home=self.home, env=self.env())
        self.assertEqual(data, odd)
        self.assertTrue(any(t["entries"] == 50 for t in trees))

    def test_nothing_found(self):
        shutil.rmtree(self.data)
        data, trees = cache_tool.discover(home=self.home, env=self.env())
        self.assertIsNone(data)
        self.assertEqual(trees, [])


class TestRaw(Base):
    def test_raw_counts_per_tree(self):
        data, trees = cache_tool.discover(home=self.home, env=self.env())
        by_entries = {t["entries"]: t for t in trees}
        gowa, eboot, empty = by_entries[50], by_entries[3], by_entries[0]
        self.assertEqual(len(cache_tool.scan_raw(gowa["sc"])), 98)  # 49 pairs (1 entry missing raw)
        self.assertEqual(len(cache_tool.scan_raw(eboot["sc"])), 6)  # 3 pairs
        self.assertEqual(cache_tool.scan_raw(empty["sc"]), [])

    def test_raw_hash_matches_sha256sum(self):
        import hashlib
        data, trees = cache_tool.discover(home=self.home, env=self.env())
        gowa = [t for t in trees if t["entries"] == 50][0]
        r = cache_tool.scan_raw(gowa["sc"])[0]
        h = hashlib.sha256(open(r["path"], "rb").read()).hexdigest()
        self.assertEqual(r["sha256"], h)
        self.assertGreater(len({x["sha256"] for x in cache_tool.scan_raw(gowa["sc"])}), 1)


class TestRscaHelpers(Base):
    def _ppu_with_entries(self):
        trees = [t for t in cache_tool.find_shader_caches(self.data) if t["entries"] > 0]
        return max(trees, key=lambda t: t["entries"])["ppu_dir"]

    def _make_rsca(self, out):
        import types
        opt_cache = cache_tool.opt_cache
        opt_cache.cmd_pack(types.SimpleNamespace(
            cache=self._ppu_with_entries(), out=out, game_id="BCES01741", mode="byte-exact"))
        return out

    def test_read_rsca_game_id(self):
        out = self._make_rsca(os.path.join(self.tmp, "BCES01741.rsca"))
        self.assertEqual(cache_tool.read_rsca_game_id(out), "BCES01741")

    def test_read_rsca_game_id_rejects_non_rsca(self):
        p = os.path.join(self.tmp, "junk.rsca")
        with open(p, "wb") as f:
            f.write(b"NOT-RSCA-JUNK-DATA-12345678901")
        with self.assertRaises(ValueError):
            cache_tool.read_rsca_game_id(p)

    def test_list_rsca_files(self):
        with open(os.path.join(self.tmp, "ABC12345.rsca"), "w") as f:
            f.write("x")
        with open(os.path.join(self.tmp, "notrsca.txt"), "w") as f:
            f.write("x")
        self.assertEqual(cache_tool.list_rsca_files(self.tmp), ["ABC12345.rsca"])

    def test_pick_ppu_dir(self):
        ppu = cache_tool.pick_ppu_dir(self.data, "BCES01741")
        self.assertTrue(ppu.startswith(os.path.join(self.data, "cache", "BCES01741")))
        self.assertTrue(os.path.basename(ppu).startswith("ppu-"))
        self.assertIsNone(cache_tool.pick_ppu_dir(self.data, "NOPE123"))

    def test_import_merges_into_existing_ppu_dir(self):
        rsca = self._make_rsca(os.path.join(self.tmp, "BCES01741.rsca"))
        dst = os.path.join(self.tmp, "dst")
        ppu_name = "ppu-TARGET-GOWA.SELF"  # deliberately different name = other machine
        os.makedirs(os.path.join(dst, "cache", "BCES01741", ppu_name), exist_ok=True)
        target = cache_tool.do_import(rsca, dst)
        self.assertEqual(target, os.path.join(dst, "cache", "BCES01741", ppu_name))
        sc = os.path.join(target, "shaders_cache")
        bins = sum(len([f for f in fs if f.endswith(".bin")])
                   for _r, _d, fs in os.walk(os.path.join(sc, "pipelines")))
        raws = len([f for f in os.listdir(os.path.join(sc, "raw")) if f.endswith((".vp", ".fp"))])
        self.assertEqual(bins, 50)
        self.assertEqual(raws, 98)

    def test_import_twice_is_idempotent(self):
        rsca = self._make_rsca(os.path.join(self.tmp, "BCES01741.rsca"))
        dst = os.path.join(self.tmp, "dst")
        ppu_name = "ppu-TARGET-GOWA.SELF"
        os.makedirs(os.path.join(dst, "cache", "BCES01741", ppu_name), exist_ok=True)
        cache_tool.do_import(rsca, dst)
        cache_tool.do_import(rsca, dst)
        sc = os.path.join(dst, "cache", "BCES01741", ppu_name, "shaders_cache")
        raws = len([f for f in os.listdir(os.path.join(sc, "raw")) if f.endswith((".vp", ".fp"))])
        self.assertEqual(raws, 98)

    def test_import_without_game_dir_raises(self):
        rsca = self._make_rsca(os.path.join(self.tmp, "BCES01741.rsca"))
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(bare)
        with self.assertRaises(FileNotFoundError):
            cache_tool.do_import(rsca, bare)


class TestExport(Base):
    def _ppu_with_entries(self):
        trees = [t for t in cache_tool.find_shader_caches(self.data) if t["entries"] > 0]
        return max(trees, key=lambda t: t["entries"])["ppu_dir"]

    def test_export_writes_rsca_named_by_game(self):
        out = cache_tool.do_export(self.data, self._ppu_with_entries(), self.tmp)
        self.assertEqual(out, os.path.join(self.tmp, "BCES01741.rsca"))
        self.assertTrue(os.path.isfile(out))
        with open(out, "rb") as f:
            self.assertEqual(f.read(4), b"RSCA")

    def test_export_roundtrip_parses(self):
        out = cache_tool.do_export(self.data, self._ppu_with_entries(), self.tmp)
        import opt_cache
        gid, _cls, _ver, _mode, raw, _tabs, ent = opt_cache._parse_pack(open(out, "rb").read())
        self.assertEqual(gid, "BCES01741")
        self.assertEqual(len(ent), 50)
        self.assertEqual(len(raw), 98)

    def test_select_tree_prefers_most_entries(self):
        trees = cache_tool.find_shader_caches(self.data)
        t = cache_tool.select_tree(trees, "BCES01741")
        self.assertEqual(t["entries"], 50)
        self.assertIsNone(cache_tool.select_tree(trees, "NOPE123"))


class TestCli(Base):
    def run_tool(self, *args, stdin=None):
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home)
        return subprocess.run([sys.executable, os.path.join(REPO, "cache_tool.py"), *args],
                              input=stdin, text=True, capture_output=True, env=env)

    def test_cli_discover(self):
        r = self.run_tool("discover")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(self.data, r.stdout)
        self.assertIn("BCES01741", r.stdout)

    def test_cli_export_then_import_roundtrip(self):
        r = self.run_tool("export")  # default tree = most entries (GOWA)
        self.assertEqual(r.returncode, 0, r.stderr)
        repo_rsca = os.path.join(REPO, "BCES01741.rsca")  # default out = program folder
        try:
            self.assertTrue(os.path.isfile(repo_rsca))
            dst = os.path.join(self.tmp, "dst")
            ppu_name = "ppu-T-GOWA.SELF"
            os.makedirs(os.path.join(dst, "cache", "BCES01741", ppu_name), exist_ok=True)
            r2 = self.run_tool("import", repo_rsca, "--data", dst)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            sc = os.path.join(dst, "cache", "BCES01741", ppu_name, "shaders_cache")
            self.assertTrue(os.path.isdir(os.path.join(sc, "raw")))
            self.assertEqual(
                len([f for f in os.listdir(os.path.join(sc, "raw")) if f.endswith((".vp", ".fp"))]), 98)
        finally:
            if os.path.isfile(repo_rsca):
                os.remove(repo_rsca)

    def test_cli_import_missing_file(self):
        r = self.run_tool("import", "definitely_missing.rsca", "--data", self.tmp)
        self.assertEqual(r.returncode, 2)


class TestInteractive(Base):
    def test_interactive_export_flow(self):
        # menu: 2 = export; tree list is sorted: [1] empty, [2] GOWA(50), [3] EBOOT(3)
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home)
        r = subprocess.run([sys.executable, os.path.join(REPO, "cache_tool.py")],
                           input="2\n2\n", text=True, capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("BCES01741.rsca", r.stdout)
        repo_rsca = os.path.join(REPO, "BCES01741.rsca")
        self.assertTrue(os.path.isfile(repo_rsca))
        os.remove(repo_rsca)  # tests must not pollute the repo

    def test_inter_quit(self):
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home)
        r = subprocess.run([sys.executable, os.path.join(REPO, "cache_tool.py")],
                           input="0\n", text=True, capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
