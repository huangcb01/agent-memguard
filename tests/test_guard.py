import json
import fcntl
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
from agent_memguard.processes import descendants, process_table, read_process


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.cgroup = self.directory / "cgroup"
        self.cgroup.mkdir()
        (self.cgroup / "memory.max").write_text(str(1024**3))
        (self.cgroup / "memory.current").write_text(str(100 * 1024**2))
        (self.cgroup / "memory.stat").write_text("inactive_file 0\nfile_dirty 0\nfile_writeback 0\n")
        self.base = [str(ROOT / "bin/memguard"), "--interval", "0.05",
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

    def wait_registry(self, predicate, processes, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path in (self.directory / "state").glob("*.json"):
                data = json.loads(path.read_text())
                if predicate(data):
                    return data
            if any(p.poll() is not None for p in processes):
                self.fail("guard exited before publishing the expected registry state")
            time.sleep(.01)
        self.fail("registry state did not arrive")

    def set_current(self, value, cgroup=None):
        # Kernel counters never have the empty-file window of write_text().
        path = (cgroup or self.cgroup) / "memory.current"
        temporary = path.with_suffix(".new")
        temporary.write_text(str(value))
        temporary.replace(path)

    def wait_stable_rss(self, processes, allocations):
        def ready(data):
            by_pid = {task["owner"]["pid"]: task for task in data["tasks"].values()}
            return all(process.pid in by_pid and by_pid[process.pid]["root"] and
                       by_pid[process.pid]["rss"] > allocation * 1024**2 and
                       by_pid[process.pid]["growth"] == 0
                       for process, allocation in zip(processes, allocations))
        return self.wait_registry(ready, processes)

    def stop_guard(self, process):
        # Stop the monitor outside its registry critical section, so this
        # fixture tests election liveness rather than a stopped lock holder.
        lock = next((self.directory / "state").glob("*.lock"))
        with lock.open("rb") as stream:
            deadline = time.monotonic() + 1
            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail("registry lock did not become available")
                    time.sleep(.005)
            process.send_signal(signal.SIGSTOP)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            owner = read_process(process.pid)
            if owner and owner.state == "T":
                return
            time.sleep(.01)
        self.fail("guard did not stop")

    def start_guard_in(self, paths, command):
        launcher = (
            "import sys,json; from pathlib import Path; sys.path.insert(0,%r); "
            "from agent_memguard import cli; "
            "cli.discover_cgroups=lambda:[Path(p) for p in json.loads(sys.argv[1])]; "
            "raise SystemExit(cli.main(sys.argv[2:]))" % str(ROOT / "src"))
        return subprocess.Popen(
            [sys.executable, "-c", launcher, json.dumps([str(p) for p in paths]),
             "--mode", "container", "--interval", ".05",
             "--soft-duration", "0", "--term-grace", "2", "--state-dir", str(self.directory / "state"),
             "--", *command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

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

    def test_large_rss_is_allowed_when_environment_is_healthy(self):
        # Simulate RSS above the old 1 GiB budget without allocating GiB.
        launcher = (
            "import os,sys\nfrom dataclasses import replace\n"
            "sys.path.insert(0,%r)\nfrom agent_memguard import cli\n"
            "from agent_memguard.processes import descendants\n"
            "original=cli.process_table\n"
            "def table():\n"
            " data=original()\n"
            " for p in descendants(data,os.getpid()): data[p.pid]=replace(p,rss=2*1024**3)\n"
            " return data\n"
            "cli.process_table=table\nraise SystemExit(cli.main(sys.argv[1:]))\n"
            % str(ROOT / "src"))
        code = "import time; x=bytearray(48*1024*1024); time.sleep(.2); print('allowed')"
        result = subprocess.run([sys.executable, "-c", launcher, *self.base[1:], "--", sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "allowed")
        self.assertEqual(result.stderr, "")

    def test_environment_pressure_kills_workers_and_cleans_all(self):
        worker = "import time; data=bytearray(24*1024*1024); time.sleep(10)"
        pid_file = self.directory / "workers"
        current = self.cgroup / "memory.current"
        code = ("import subprocess,sys,time,pathlib; p=[subprocess.Popen([sys.executable,'-c',%r]) for _ in range(2)]; "
                "pathlib.Path(%r).write_text(','.join(str(x.pid) for x in p)); time.sleep(.2); "
                "temporary=pathlib.Path(%r); temporary.write_text(%r); temporary.replace(%r); time.sleep(10)"
                % (worker, str(pid_file), str(current.with_suffix('.new')), str(940 * 1024**2), str(current)))
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
        current = self.cgroup / "memory.current"
        code = ("import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); data=bytearray(48*1024*1024); "
                "temporary=pathlib.Path(%r); temporary.write_text(%r); temporary.replace(%r); time.sleep(10)"
                % (str(current.with_suffix('.new')), str(850 * 1024**2), str(current)))
        result = self.run_guard([sys.executable, "-c", code], options=("--soft-duration", "0", "--term-grace", "0.1"))
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
            self.wait_stable_rss(processes, (8, 24))
            self.set_current(940 * 1024**2)
            deadline = time.monotonic() + 5
            while all(p.poll() is None for p in processes) and time.monotonic() < deadline:
                time.sleep(0.01)
            # Simulate recovery when the elected task releases memory.
            self.set_current(100 * 1024**2)
            self.assertEqual(sum(p.poll() is not None for p in processes), 1)
            ended = next(p for p in processes if p.poll() is not None)
            _, stderr = ended.communicate()
            self.assertEqual(ended.returncode, 125, stderr)
            self.assertIn("environment_pressure", stderr)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=8)
            for path in pid_files:
                if path.exists():
                    self.assert_dead(int(path.read_text()))

    def test_different_thresholds_cannot_cancel_pressure_request(self):
        processes = []
        try:
            for i, size in enumerate((8, 24)):
                pid_file = self.directory / f"threshold_pid{i}"
                code = "import os,time,pathlib; x=bytearray(%d*1024*1024); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(10)" % (size, str(pid_file))
                options = [] if i == 0 else ["--soft-fraction", ".9", "--hard-fraction", ".99"]
                process = subprocess.Popen([*self.base, *options, "--", sys.executable, "-c", code],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                self.wait_file(pid_file, process)
            # Nonzero RSS can still describe the exec helper. Wait for the
            # allocations to be sampled and the initial growth to settle.
            self.wait_stable_rss(processes, (8, 24))
            self.set_current(int(.89 * 1024**3))
            _, stderr = processes[1].communicate(timeout=5)
            self.set_current(100 * 1024**2)
            self.assertEqual(processes[1].returncode, 125, stderr)
            self.assertIn('"reason": "environment_pressure"', stderr)
            self.assertIsNone(processes[0].poll())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=8)

    def test_peer_kill_reason_survives_recovery_with_two_second_interval(self):
        processes = []
        try:
            # Start the slow victim first, then wait for a sample of its RSS.
            for i, size in enumerate((24, 8)):
                pid_file = self.directory / f"slow_pid{i}"
                code = "import os,time,pathlib; x=bytearray(%d*1024*1024); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(10)" % (size, str(pid_file))
                options = ["--interval", "2"] if i == 0 else []
                process = subprocess.Popen([*self.base, *options, "--", sys.executable, "-c", code],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                self.wait_file(pid_file, process)
            self.wait_registry(lambda d: any(t["owner"]["pid"] == processes[0].pid and
                                            t["rss"] > 24 * 1024**2 for t in d["tasks"].values()), processes)
            victim_pid = int((self.directory / "slow_pid0").read_text())
            self.set_current(940 * 1024**2)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                victim = read_process(victim_pid)
                if victim is None or not victim.alive:
                    break
                time.sleep(.01)
            else:
                self.fail("peer did not kill the sleeping victim")
            self.assertIsNone(processes[0].poll(), "victim guard should still be between samples")
            self.set_current(100 * 1024**2)
            _, stderr = processes[0].communicate(timeout=5)
            self.assertEqual(processes[0].returncode, 125, stderr)
            self.assertIn('"reason": "environment_pressure"', stderr)
            self.assertIn('"signal": "SIGKILL"', stderr)
            self.assertNotIn('"event": "target_signaled"', stderr)
            self.assertIsNone(processes[1].poll())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=8)

    def test_parent_hard_pressure_peer_kills_stalled_soft_victim(self):
        parent = self.directory / "parent"
        parent.mkdir()
        (parent / "memory.max").write_text(str(1024**3))
        (parent / "memory.current").write_text(str(100 * 1024**2))
        (parent / "memory.stat").write_text("inactive_file 0\n")
        # Use real subprocesses with two simulated controller levels.
        launcher = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, %r); from agent_memguard import cli; "
            "cli.discover_cgroups=lambda:[Path(%r),Path(%r)]; "
            "raise SystemExit(cli.main(sys.argv[1:]))"
            % (str(ROOT / "src"), str(self.cgroup), str(parent)))
        processes = []
        try:
            for i, size in enumerate((8, 24)):
                pid_file = self.directory / f"parent_pid{i}"
                code = ("import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                        "x=bytearray(%d*1024*1024); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(15)"
                        % (size, str(pid_file)))
                process = subprocess.Popen(
                    [sys.executable, "-c", launcher, "--mode", "container",
                     "--interval", ".05", "--soft-duration", "0", "--term-grace", "2",
                     "--state-dir", str(self.directory / "state"), "--", sys.executable, "-c", code],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                self.wait_file(pid_file, process)
            self.wait_stable_rss(processes, (8, 24))
            self.set_current(850 * 1024**2)
            self.wait_registry(lambda d: any(task["owner"]["pid"] == processes[1].pid and
                                            task["termination"] and task["termination"]["signal"] == "SIGTERM"
                                            for task in d["tasks"].values()), processes)
            # The owner cannot perform its usual TERM-to-KILL escalation.
            self.stop_guard(processes[1])
            self.set_current(100 * 1024**2)
            self.set_current(940 * 1024**2, parent)
            self.wait_registry(lambda d: any(task["owner"]["pid"] == processes[1].pid and
                                            task["termination"] and task["termination"]["signal"] == "SIGKILL"
                                            for task in d["tasks"].values()), processes, timeout=2)
            victim_pid = int((self.directory / "parent_pid1").read_text())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                victim = read_process(victim_pid)
                if victim is None or not victim.alive:
                    break
                time.sleep(.01)
            self.assert_dead(victim_pid)
            self.assertIsNone(processes[1].poll(), "owner should still be stopped")
            self.set_current(100 * 1024**2, parent)
            processes[1].send_signal(signal.SIGCONT)
            _, stderr = processes[1].communicate(timeout=5)
            self.assertEqual(processes[1].returncode, 125, stderr)
            self.assertIn('"reason": "environment_pressure"', stderr)
            self.assertIn('"signal": "SIGKILL"', stderr)
            self.assertIsNone(processes[0].poll())
        finally:
            self.set_current(100 * 1024**2)
            self.set_current(100 * 1024**2, parent)
            for process in processes:
                if process.poll() is None:
                    process.send_signal(signal.SIGCONT)
                    process.terminate()
                process.communicate(timeout=8)

    def test_sustained_hard_pressure_continues_after_peer_tree_exits(self):
        processes = []
        try:
            for i, size in enumerate((8, 24)):
                pid_file = self.directory / f"sustained_pid{i}"
                code = ("import os,time,pathlib; x=bytearray(%d*1024*1024); "
                        "pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(15)"
                        % (size, str(pid_file)))
                process = self.start_guard_in([self.cgroup], [sys.executable, "-c", code])
                processes.append(process)
                self.wait_file(pid_file, process)
            self.wait_stable_rss(processes, (8, 24))
            self.stop_guard(processes[1])
            self.set_current(940 * 1024**2)
            self.wait_registry(lambda d: any(task["owner"]["pid"] == processes[1].pid and task.get("released")
                                            for task in d["tasks"].values()), processes, timeout=3)
            self.assert_dead(int((self.directory / "sustained_pid1").read_text()))
            self.assertEqual(descendants(process_table(), processes[1].pid), [])
            self.assertIsNone(processes[1].poll(), "released task owner must remain stopped")
            # Do not simulate recovery: pressure remains hard after the first
            # victim's entire task tree has exited.
            _, stderr = processes[0].communicate(timeout=4)
            self.assertEqual(processes[0].returncode, 125, stderr)
            self.assertIn('"reason": "environment_pressure"', stderr)
            self.assert_dead(int((self.directory / "sustained_pid0").read_text()))
            self.set_current(100 * 1024**2)
            processes[1].send_signal(signal.SIGCONT)
            _, stderr = processes[1].communicate(timeout=5)
            self.assertEqual(processes[1].returncode, 125, stderr)
            self.assertIn('"reason": "environment_pressure"', stderr)
            self.assertIn('"signal": "SIGKILL"', stderr)
        finally:
            self.set_current(100 * 1024**2)
            for process in processes:
                if process.poll() is None:
                    process.send_signal(signal.SIGCONT)
                    process.terminate()
                process.communicate(timeout=8)

    def test_sibling_leaf_hard_pressure_bypasses_stalled_soft_request(self):
        sibling = self.directory / "sibling"
        parent = self.directory / "parent"
        for path, maximum in ((sibling, 1024**3), (parent, 4 * 1024**3)):
            path.mkdir()
            (path / "memory.max").write_text(str(maximum))
            (path / "memory.current").write_text(str(100 * 1024**2))
            (path / "memory.stat").write_text("inactive_file 0\n")
        processes = []
        try:
            for i, (size, leaf) in enumerate(((8, self.cgroup), (24, sibling))):
                pid_file = self.directory / f"sibling_pid{i}"
                code = ("import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                        "x=bytearray(%d*1024*1024); pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(15)"
                        % (size, str(pid_file)))
                process = self.start_guard_in([leaf, parent], [sys.executable, "-c", code])
                processes.append(process)
                self.wait_file(pid_file, process)
            self.wait_stable_rss(processes, (8, 24))
            self.set_current(850 * 1024**2)
            self.wait_registry(lambda d: any(task["owner"]["pid"] == processes[0].pid and
                                            task["termination"] and task["termination"]["signal"] == "SIGTERM"
                                            for task in d["tasks"].values()), processes)
            self.stop_guard(processes[0])
            self.set_current(940 * 1024**2, sibling)
            _, stderr = processes[1].communicate(timeout=2)
            self.assertEqual(processes[1].returncode, 125, stderr)
            self.assertIn('"reason": "environment_pressure"', stderr)
            self.assertIn('"signal": "SIGKILL"', stderr)
            self.assert_dead(int((self.directory / "sibling_pid1").read_text()))
            first = read_process(int((self.directory / "sibling_pid0").read_text()))
            self.assertTrue(first and first.alive, "B's leaf pressure must not terminate A")
            self.assertIsNone(processes[0].poll())
            self.set_current(100 * 1024**2)
            self.set_current(100 * 1024**2, sibling)
            processes[0].send_signal(signal.SIGCONT)
            _, stderr = processes[0].communicate(timeout=5)
            self.assertEqual(processes[0].returncode, 125, stderr)
        finally:
            self.set_current(100 * 1024**2)
            self.set_current(100 * 1024**2, sibling)
            for process in processes:
                if process.poll() is None:
                    process.send_signal(signal.SIGCONT)
                    process.terminate()
                process.communicate(timeout=8)


if __name__ == "__main__":
    unittest.main()
