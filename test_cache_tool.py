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
