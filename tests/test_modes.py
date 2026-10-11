import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_memguard.processes import read_process


class ModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.cgroup = self.directory / "cgroup"
        self.cgroup.mkdir()
        (self.cgroup / "memory.max").write_text("max")
        (self.cgroup / "memory.current").write_text(str(100 * 1024**2))
        (self.cgroup / "memory.stat").write_text("inactive_file 0\n")
        self.meminfo = self.directory / "meminfo"
        self.set_wsl_available(900 * 1024**2)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("MEMGUARD_")}
        self.env.update(MEMGUARD_INTERVAL="0.05", MEMGUARD_MEMINFO_FILE=str(self.meminfo),
                        MEMGUARD_CGROUP_DIR=str(self.cgroup),
                        MEMGUARD_STATE_DIR=str(self.directory / "state"))

    def set_wsl_available(self, available, swap_total=2 * 1024**3, swap_free=None):
        if swap_free is None:
            swap_free = swap_total
        temporary = self.meminfo.with_suffix(".new")
        temporary.write_text(f"MemTotal: {1024**3 // 1024} kB\nMemAvailable: {available // 1024} kB\n"
                             f"SwapTotal: {swap_total // 1024} kB\nSwapFree: {swap_free // 1024} kB\n")
        temporary.replace(self.meminfo)

    def launch(self, args, entry="memguard", env=None, input=None):
        return subprocess.run([str(ROOT / "bin" / entry), *args], env=self.env if env is None else env,
                              input=input, capture_output=True, text=True, timeout=12)

    def finite_cgroup(self, current):
        (self.cgroup / "memory.max").write_text(str(1024**3))
        (self.cgroup / "memory.current").write_text(str(current))

    def test_default_container_mode_refuses_unlimited_before_exec(self):
        marker = self.directory / "marker"
        result = self.launch(["--", "/usr/bin/touch", str(marker)])
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn("no finite cgroup memory limit", result.stderr)
        self.assertFalse(marker.exists())

    def test_wsl_check_reports_vm_pressure_and_available_memory(self):
        result = self.launch(["--mode", "wsl", "--check"])
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads(result.stdout)
        self.assertEqual(settings["mode"], "wsl")
        self.assertEqual(settings["protection"], "environment-pressure")
        self.assertIsNone(settings["task_rss_limit"])
        self.assertEqual(len(settings["environments"]), 1)
        self.assertEqual(settings["environments"][0]["source"], "wsl")
        self.assertEqual(settings["environments"][0]["available"], 900 * 1024**2)

    def test_cli_mode_overrides_environment_in_both_directions(self):
        for mode, environment_mode, expected in (("wsl", "container", 0), ("container", "wsl", 125)):
            with self.subTest(mode=mode):
                result = self.launch(["--mode", mode, "--", "/bin/true"],
                                     env={**self.env, "MEMGUARD_MODE": environment_mode})
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_invalid_modes_never_execute_target(self):
        marker = self.directory / "marker"
        for args, mode in (([], "auto"), (["--mode", "auto"], "container")):
            with self.subTest(args=args):
                result = self.launch([*args, "--", "/usr/bin/touch", str(marker)],
                                     env={**self.env, "MEMGUARD_MODE": mode})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(marker.exists())

    def test_rss_only_and_limit_flags_are_no_longer_supported(self):
        for flag in (["--allow-unlimited"], ["--limit", "1G"]):
            result = self.launch([*flag, "--", "/bin/true"])
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_finite_cgroup_is_retained_in_both_modes(self):
        self.finite_cgroup(100 * 1024**2)
        for mode in ("container", "wsl"):
            with self.subTest(mode=mode):
                result = self.launch(["--mode", mode, "--check"])
                self.assertEqual(result.returncode, 0, result.stderr)
                settings = json.loads(result.stdout)
                self.assertEqual(settings["mode"], mode)
                self.assertEqual(settings["protection"], "environment-pressure")
                self.assertEqual(settings["environments"][0]["source"], "cgroup")
                self.assertEqual(settings["environments"][0]["maximum"], 1024**3)
                self.assertEqual(len(settings["environments"]), 2 if mode == "wsl" else 1)

    def test_wsl_mode_still_enforces_finite_cgroup_admission(self):
        self.finite_cgroup(900 * 1024**2)
        marker = self.directory / "marker"
        result = self.launch(["--mode", "wsl", "--", "/usr/bin/touch", str(marker)])
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn("environment memory pressure", result.stderr)
        self.assertFalse(marker.exists())

    def test_wsl_mode_still_terminates_on_finite_cgroup_pressure(self):
        self.finite_cgroup(940 * 1024**2)
        result = self.launch(["--mode", "wsl", "--no-admission-check", "--", "/bin/sleep", "10"])
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn('"reason": "environment_pressure"', result.stderr)

    def test_wsl_mode_adopts_limit_added_while_task_runs(self):
        marker = self.directory / "pid"
        code = ("import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(10)"
                % str(marker))
        process = subprocess.Popen([str(ROOT / "bin/memguard"), "--", sys.executable, "-c", code],
                                   env={**self.env, "MEMGUARD_MODE": "wsl"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("task did not start without a finite limit")
                time.sleep(.01)
            # Publish complete counter values, matching kernel file semantics.
            for name, value in (("memory.current", 940 * 1024**2), ("memory.max", 1024**3)):
                temporary = self.cgroup / (name + ".new")
                temporary.write_text(str(value))
                temporary.replace(self.cgroup / name)
            _, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 125, stderr)
            self.assertIn('"reason": "environment_pressure"', stderr)
            task = read_process(int(marker.read_text()))
            self.assertTrue(task is None or not task.alive)
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

    def test_wsl_vm_pressure_cleans_descendants(self):
        marker = self.directory / "workers"
        worker = "import os,time; os.setsid(); data=bytearray(24*1024*1024); time.sleep(10)"
        pressure = "MemTotal: 1048576 kB\nMemAvailable: 65536 kB\nSwapTotal: 2097152 kB\nSwapFree: 2097152 kB\n"
        code = ("import subprocess,sys,time,pathlib; "
                "workers=[subprocess.Popen([sys.executable,'-c',%r]) for _ in range(2)]; "
                "pathlib.Path(%r).write_text(','.join(str(p.pid) for p in workers)); time.sleep(.2); "
                "temporary=pathlib.Path(%r); temporary.write_text(%r); temporary.replace(%r); time.sleep(10)"
                % (worker, str(marker), str(self.meminfo.with_suffix('.new')), pressure, str(self.meminfo)))
        result = self.launch(["--mode", "wsl", "--", sys.executable, "-c", code])
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn('"reason": "environment_pressure"', result.stderr)
        for pid in marker.read_text().split(","):
            process = read_process(int(pid))
            self.assertTrue(process is None or not process.alive, f"worker {pid} survived")

    def test_wsl_pressure_is_checked_before_start_and_read_failures_refuse_start(self):
        self.set_wsl_available(64 * 1024**2)
        marker = self.directory / "marker"
        result = self.launch(["--mode", "wsl", "--", "/usr/bin/touch", str(marker)])
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertFalse(marker.exists())
        self.meminfo.unlink()
        result = self.launch(["--mode", "wsl", "--", "/usr/bin/touch", str(marker)])
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertFalse(marker.exists())

    def test_container_mode_does_not_read_host_meminfo(self):
        self.finite_cgroup(100 * 1024**2)
        self.meminfo.write_text("invalid host metrics")
        result = self.launch(["--mode", "container", "--", "/bin/true"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_available_memory_allows_tasks_even_when_swap_is_full(self):
        self.set_wsl_available(900 * 1024**2, swap_free=0)
        result = self.launch(["--mode", "wsl", "--", "/bin/true"])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wsl_monitor_failure_cleans_running_task(self):
        marker = self.directory / "pid"
        code = ("import os,pathlib,time; pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(.1); "
                "pathlib.Path(%r).unlink(); time.sleep(10)" % (str(marker), str(self.meminfo)))
        result = self.launch(["--mode", "wsl", "--", sys.executable, "-c", code])
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn("guard_error", result.stderr)
        task = read_process(int(marker.read_text()))
        self.assertTrue(task is None or not task.alive)

    def test_wsl_soft_pressure_sends_term_after_duration(self):
        self.set_wsl_available(160 * 1024**2)
        result = self.launch(["--mode", "wsl", "--no-admission-check", "--soft-duration", ".1", "--",
                              "/bin/sleep", "10"])
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn('"reason": "environment_pressure"', result.stderr)
        self.assertIn('"signal": "SIGTERM"', result.stderr)

    def test_shell_mode_uses_environment_and_preserves_exit_and_oom_priority(self):
        result = self.launch(["-c", "cat /proc/self/oom_score_adj; exit 7"], entry="memguard-shell",
                             env={**self.env, "MEMGUARD_MODE": "wsl"})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout.strip(), "500")

    def test_shell_default_container_mode_refuses_unlimited(self):
        result = self.launch(["-c", "true"], entry="memguard-shell")
        self.assertEqual(result.returncode, 125, result.stderr)

    def test_hook_pins_wsl_mode_and_preserves_command(self):
        command = "printf '%s\\n' 'a b' '$literal'; exit 7"
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        result = self.launch([], entry="memguard-codex-hook", input=json.dumps(event),
                             env={**self.env, "MEMGUARD_MODE": "wsl"})
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "allow")
        wrapped = output["updatedInput"]["command"]
        self.assertEqual(shlex.split(wrapped)[1:3], ["--mode", "wsl"])
        # The execution side need not inherit the hook's environment mode.
        executed = subprocess.run(["/bin/bash", "-c", wrapped], env=self.env, capture_output=True,
                                  text=True, timeout=12)
        self.assertEqual(executed.returncode, 7, executed.stderr)
        self.assertEqual(executed.stdout, "a b\n$literal\n")

    def test_hook_invalid_mode_returns_deny(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}}
        result = self.launch([], entry="memguard-codex-hook", input=json.dumps(event),
                             env={**self.env, "MEMGUARD_MODE": "auto"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
