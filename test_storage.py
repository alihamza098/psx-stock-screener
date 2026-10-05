#!/usr/bin/env python3
"""PSX_DATA_DIR redirects cache/ and root runtime files to a persistent directory."""

import json
import tempfile
import unittest
from pathlib import Path

import psx_storage


class TestPersistentStorage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.base, self.data = root / "app", root / "disk"
        (self.base / "cache" / "history").mkdir(parents=True)
        (self.base / "cache" / "intelligence.db").write_text("bundled")
        (self.base / "cache" / "history" / "OGDC.json").write_text("{}")
        (self.base / "cache" / "x.db-wal").write_text("wal")
        (self.base / "licenses.json").write_text(json.dumps({"K": 1}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_without_env(self):
        self.assertEqual(psx_storage.init_persistent_storage(self.base, ""), {"enabled": False})
        self.assertFalse((self.base / "cache").is_symlink())

    def test_seeds_then_redirects_and_never_overwrites(self):
        self.data.mkdir()
        (self.data / "intelligence.db").write_text("persisted")  # survives a redeploy
        res = psx_storage.init_persistent_storage(self.base, str(self.data))
        self.assertTrue(res["enabled"])
        self.assertTrue((self.base / "cache").is_symlink())
        self.assertEqual((self.base / "cache" / "intelligence.db").read_text(), "persisted")
        self.assertEqual((self.base / "cache" / "history" / "OGDC.json").read_text(), "{}")
        self.assertFalse((self.data / "x.db-wal").exists(), "stale WAL files are not copied")
        self.assertEqual(json.loads((self.base / "licenses.json").read_text()), {"K": 1})
        self.assertTrue((self.base / "licenses.json").is_symlink())

        # writes through the redirected paths land on the disk, including files that did not exist
        (self.base / "cache" / "new.json").write_text("n")
        (self.base / "trial_data.json").write_text("{}")
        self.assertEqual((self.data / "new.json").read_text(), "n")
        self.assertEqual((self.data / "trial_data.json").read_text(), "{}")

        # second boot (already linked) is a no-op
        self.assertTrue(psx_storage.init_persistent_storage(self.base, str(self.data))["enabled"])
        self.assertEqual((self.base / "cache" / "intelligence.db").read_text(), "persisted")


if __name__ == "__main__":
    unittest.main()
