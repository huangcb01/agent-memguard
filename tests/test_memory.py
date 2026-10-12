import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_memguard.memory import parse_size, read_memory, read_wsl_memory


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
            (path / "memory.stat").write_text("inactive_file 4000\nfile_dirty 200\nfile_writeback 100\n"
                                              "slab_reclaimable 2000\nslab_unreclaimable 300\n")
            memory = read_memory(path)
            # Dirty/writeback and unreclaimable slab stay charged; retain
            # half of reclaimable slab rather than deducting all of it.
            self.assertEqual(memory.working_set, 3200)
            self.assertEqual(memory.metrics()["slab_reclaimable_credit"], 1000)
            self.assertEqual(memory.metrics()["slab_reclaimable"], 2000)
            self.assertEqual(memory.metrics()["slab_unreclaimable"], 300)
            self.assertEqual(memory.metrics()["inactive_file"], 4000)
            # File and slab counters can be ahead of memory.current across
            # separate kernel samples. Never report negative usage.
            (path / "memory.current").write_text("500")
            self.assertEqual(read_memory(path).working_set, 0)
            (path / "memory.max").write_text("max")
            self.assertIsNone(read_memory(path))

    def test_wsl_memory_uses_memavailable_and_keeps_swap_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meminfo"
            path.write_text("MemTotal: 8192 kB\nMemFree: 1 kB\nMemAvailable: 6144 kB\n"
                            "SwapTotal: 65536 kB\nSwapFree: 32768 kB\n")
            memory = read_wsl_memory(path)
            self.assertEqual(memory.maximum, 8192 * 1024)
            self.assertEqual(memory.available, 6144 * 1024)
            self.assertEqual(memory.working_set, 2048 * 1024)
            self.assertEqual(memory.swap_current, 32768 * 1024)
            # A much larger swap never inflates the physical RAM threshold.
            self.assertEqual(memory.source, "wsl")
            path.write_text("MemTotal: 4096 kB\nMemAvailable: 256 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
            memory = read_wsl_memory(path)
            self.assertEqual(memory.maximum, 4096 * 1024)
            self.assertEqual(memory.available, 256 * 1024)
            self.assertEqual(memory.swap_current, 0)

    def test_wsl_memory_rejects_missing_or_invalid_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meminfo"
            for text in ("MemTotal: 8192 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
                         "MemTotal: 8192 kB\nMemAvailable: 9000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
                         "MemTotal: 8192 B\nMemAvailable: 1 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"):
                with self.subTest(text=text):
                    path.write_text(text)
                    with self.assertRaises(ValueError):
                        read_wsl_memory(path)

    def test_native_meminfo_is_not_read_on_a_non_wsl_kernel(self):
        with patch.object(Path, "read_text", return_value="6.8.0-generic"):
            with self.assertRaisesRegex(RuntimeError, "WSL kernel"):
                read_wsl_memory(Path("/proc/meminfo"))

    def test_cgroup_swap_is_reported_without_increasing_memory_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "memory.max").write_text("8000")
            (path / "memory.current").write_text("1000")
            (path / "memory.stat").write_text("inactive_file 0\n")
            (path / "memory.swap.current").write_text("6000")
            (path / "memory.swap.max").write_text("9000")
            memory = read_memory(path)
            self.assertEqual(memory.maximum, 8000)
            self.assertEqual(memory.available, 7000)
            self.assertEqual(memory.swap_current, 6000)
            (path / "memory.swap.max").write_text("max")
            self.assertIsNone(read_memory(path).swap_maximum)


if __name__ == "__main__":
    unittest.main()
