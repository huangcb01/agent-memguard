import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_memguard.memory import parse_size, read_memory


class MemoryTests(unittest.TestCase):
    def test_binary_sizes(self):
        self.assertEqual(parse_size("1GiB"), 1024**3)
        self.assertEqual(parse_size("1.5G"), 3 * 1024**3 // 2)
        for value in ("0", "-1G", "nan", "12watts"):
            with self.assertRaises(ValueError):
                parse_size(value)

    def test_cache_and_dirty_pages(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)
            (path / "memory.max").write_text("8000")
            (path / "memory.current").write_text("7900")
            (path / "memory.stat").write_text("inactive_file 4000\nfile_dirty 200\nfile_writeback 100\n")
            self.assertEqual(read_memory(path).working_set, 4200)
            (path / "memory.max").write_text("max")
            self.assertIsNone(read_memory(path))


if __name__ == "__main__":
    unittest.main()
