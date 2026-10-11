import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_memguard.processes import read_process


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.cgroup = self.directory / "cgroup"
        self.cgroup.mkdir()
        (self.cgroup / "memory.max").write_text(str(1024**3))
        (self.cgroup / "memory.current").write_text(str(100 * 1024**2))
        (self.cgroup / "memory.stat").write_text("inactive_file 0\nfile_dirty 0\nfile_writeback 0\n")
        self.base = [str(ROOT / "bin/memguard"), "--limit", "64M", "--interval", "0.05",
                     "--state-dir", str(self.directory / "state"), "--cgroup-dir", str(self.cgroup)]

    def tearDown(self):
        self.temporary.cleanup()

    def run_guard(self, command, options=()):
        return subprocess.run([*self.base, *options, "--", *command], capture_output=True, text=True, timeout=12)

    def wait_file(self, path, process, timeout=5):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail("task did not publish its PID")
            time.sleep(0.02)
        return int(path.read_text())

    def assert_dead(self, pid):
        process = read_process(pid)
        self.assertTrue(process is None or not process.alive, f"PID {pid} survived")

    def test_output_and_exit_code(self):
        result = self.run_guard(["/bin/bash", "-c", "printf stdout; printf stderr >&2; exit 7"])
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, "stdout")
        self.assertEqual(result.stderr, "stderr")

    def test_oom_preference_before_exec(self):
        result = self.run_guard([sys.executable, "-c", "from pathlib import Path; print(Path('/proc/self/oom_score_adj').read_text().strip())"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "500")

    def test_stdin_is_preserved(self):
        result = subprocess.run([*self.base, "--", sys.executable, "-c", "print(input())"], input="hello\n",
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.stdout, "hello\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rss_limit_kills_task(self):
        pid_file = self.directory / "pid"
        code = "import os,time,pathlib; pathlib.Path(%r).write_text(str(os.getpid())); data=bytearray(48*1024*1024); time.sleep(10)" % str(pid_file)
        result = self.run_guard([sys.executable, "-c", code], options=("--limit", "40M"))
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn('"reason": "task_rss_limit"', result.stderr)
        self.assert_dead(int(pid_file.read_text()))

    def test_sum_of_workers_triggers_limit_and_cleans_all(self):
        worker = "import time; data=bytearray(24*1024*1024); time.sleep(10)"
        pid_file = self.directory / "workers"
        code = "import subprocess,sys,time,pathlib; p=[subprocess.Popen([sys.executable,'-c',%r]) for _ in range(2)]; pathlib.Path(%r).write_text(','.join(str(x.pid) for x in p)); time.sleep(10)" % (worker, str(pid_file))
        result = self.run_guard([sys.executable, "-c", code])
        self.assertEqual(result.returncode, 125, result.stderr)
        for pid in pid_file.read_text().split(","):
            self.assert_dead(int(pid))

    def test_orphan_sets_id_worker_is_cleaned(self):
        pid_file = self.directory / "orphan"
        code = "import os,time,pathlib; p=os.fork();\nif p==0:\n os.setsid(); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(10)\nelse:\n time.sleep(.3)" % str(pid_file)
        result = self.run_guard([sys.executable, "-c", code])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_dead(int(pid_file.read_text()))

    def test_ignoring_term_escalates_to_kill(self):
        code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); data=bytearray(48*1024*1024); time.sleep(10)"
        result = self.run_guard([sys.executable, "-c", code], options=("--limit", "40M", "--term-grace", "0.1"))
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn('"signal": "SIGKILL"', result.stderr)

    def test_cached_memory_does_not_trigger(self):
        (self.cgroup / "memory.current").write_text(str(1000 * 1024**2))
        (self.cgroup / "memory.stat").write_text(f"inactive_file {900 * 1024**2}\n")
        result = self.run_guard(["/bin/true"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_insufficient_headroom_refuses_before_exec(self):
        (self.cgroup / "memory.current").write_text(str(900 * 1024**2))
        marker = self.directory / "should_not_exist"
        result = self.run_guard(["/usr/bin/touch", str(marker)])
        self.assertEqual(result.returncode, 125)
        self.assertFalse(marker.exists())

    def test_monitor_failure_kills_task(self):
        pid_file = self.directory / "pid"
        code = "import os,time,pathlib; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(10)" % str(pid_file)
        process = subprocess.Popen([*self.base, "--", sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            pid = self.wait_file(pid_file, process)
            (self.cgroup / "memory.current").unlink()
            _, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 125, stderr)
            self.assertIn("guard_error", stderr)
            self.assert_dead(pid)
        finally:
            if process.poll() is None:
                process.kill(); process.wait()

    def test_sigint_is_forwarded_and_preserved(self):
        pid_file = self.directory / "pid"
        code = "import os,time,pathlib; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(10)" % str(pid_file)
        process = subprocess.Popen([*self.base, "--", sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            pid = self.wait_file(pid_file, process)
            process.send_signal(signal.SIGINT)
            _, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 130, stderr)
            self.assert_dead(pid)
        finally:
            if process.poll() is None:
                process.kill(); process.wait()

    def test_hard_pressure_elects_one_of_two_tasks(self):
        processes = []
        pid_files = []
        try:
            for i, size in enumerate((8, 24)):
                pid_file = self.directory / f"pid{i}"
                code = "import os,time,pathlib; x=bytearray(%d*1024*1024); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(10)" % (size, str(pid_file))
                process = subprocess.Popen([*self.base, "--", sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process);pid_files.append(pid_file)
                self.wait_file(pid_file, process)
            (self.cgroup / "memory.current").write_text(str(940 * 1024**2))
            deadline = time.monotonic() + 5
            while all(p.poll() is None for p in processes) and time.monotonic() < deadline:
                time.sleep(0.01)
            # Simulate recovery when the elected task releases memory.
            (self.cgroup / "memory.current").write_text(str(100 * 1024**2))
            self.assertEqual(sum(p.poll() is not None for p in processes), 1)
            ended = next(p for p in processes if p.poll() is not None)
            _, stderr = ended.communicate()
            self.assertEqual(ended.returncode, 125, stderr)
            self.assertIn("container_pressure", stderr)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=8)
            for path in pid_files:
                if path.exists():
                    self.assert_dead(int(path.read_text()))


if __name__ == "__main__":
    unittest.main()
