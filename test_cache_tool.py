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


if __name__ == "__main__":
    unittest.main(verbosity=2)
