"""Tests for the disk-space guard (P1-3)."""

import unittest
from unittest.mock import patch

from src._util import Error, check_disk_space


class TestDiskGuard(unittest.TestCase):
    def test_ok_when_plenty_free(self):
        usage = type("U", (), {"free": 10 * 1024 * 1024 * 1024})()
        with patch("src._util.shutil.disk_usage", return_value=usage):
            self.assertEqual(check_disk_space("/tmp"), usage.free)

    def test_blocks_when_below_floor(self):
        usage = type("U", (), {"free": 1024})()
        with patch("src._util.shutil.disk_usage", return_value=usage):
            with self.assertRaises(Error) as ctx:
                check_disk_space("/tmp")
            self.assertEqual(ctx.exception.code, "ENVIRONMENT_ERROR")

    def test_error_when_unreadable(self):
        with patch("src._util.shutil.disk_usage", side_effect=OSError("no fs")):
            with self.assertRaises(Error) as ctx:
                check_disk_space("/nonexistent")
            self.assertEqual(ctx.exception.code, "ENVIRONMENT_ERROR")


if __name__ == "__main__":
    unittest.main()
